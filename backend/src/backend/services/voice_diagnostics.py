"""Bounded metadata-only cadence observations; never await telemetry per audio packet."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

import backend.services.client_diagnostics as client_diagnostics
from backend.services.voice_latency import PROCESS_CLOCK

LOGGER = logging.getLogger(__name__)
_CURRENT: ContextVar[VoiceCadenceRecorder | None] = ContextVar(
    "voice_cadence", default=None
)
MAX_TIMELINE_EVENTS = 2048
MAX_STAGES = 24
BUCKETS_MS = (1, 5, 10, 20, 40, 100, 250, 500, 1000, 5000)
_NUMERIC_FIELDS = {
    "sequence",
    "sample_start",
    "sample_end",
    "samples",
    "phrase_index",
    "bytes",
    "rendered_samples",
    "buffered_samples",
}


class VoiceCadenceRecorder:
    """One response/effect, finite aggregates and sampled monotonic timeline.

    Timeline retains the first 32 observations plus a bounded recent ring. Every
    observation contributes to aggregates, even when timeline sampling drops it.
    No transcript, audio, URL, provider request, or exception message is retained.
    """

    def __init__(
        self,
        *,
        user_id,
        client_id,
        capture_session_id,
        voice_session_id="",
        capture_epoch=0,
        turn_id="",
        generation=0,
        response_id="",
        interaction_id="",
        effect_id="",
        platform="worker-voice",
    ):
        self.identity = dict(
            user_id=str(user_id),
            client_id=str(client_id),
            capture_session_id=str(capture_session_id),
            voice_session_id=str(voice_session_id),
            capture_epoch=int(capture_epoch),
            turn_id=str(turn_id),
            generation=int(generation),
            response_id=str(response_id),
            interaction_id=str(interaction_id),
            effect_id=str(effect_id),
        )
        self.platform = platform
        self.started_ms = time.perf_counter() * 1000
        self.started_wall_ms = time.time() * 1000
        self.aggregates = {}
        self.first_events = []
        self.recent_events = deque(maxlen=MAX_TIMELINE_EVENTS - 32)
        self.sampled_at = {}
        self.observations = 0
        self.sampled_events = 0
        self.flushed = False
        self.terminal_outcome = None
        self.pre_skip_samples = None

    @contextmanager
    def bind(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def set_response(self, response_id):
        self.identity["response_id"] = str(response_id)

    def observe(self, stage, start_ms, end_ms, **fields):
        if stage not in self.aggregates and len(self.aggregates) >= MAX_STAGES:
            return
        duration = max(0, end_ms - start_ms)
        stats = self.aggregates.setdefault(
            stage,
            dict(
                count=0,
                total_ms=0.0,
                max_ms=0.0,
                first_start_ms=start_ms,
                last_end_ms=end_ms,
                max_inter_completion_ms=0.0,
                duration_histogram=[0] * (len(BUCKETS_MS) + 1),
            ),
        )
        if stats["count"]:
            stats["max_inter_completion_ms"] = max(
                stats["max_inter_completion_ms"], end_ms - stats["last_end_ms"]
            )
        stats["count"] += 1
        stats["total_ms"] += duration
        stats["max_ms"] = max(stats["max_ms"], duration)
        stats["last_end_ms"] = end_ms
        bucket = next(
            (i for i, bound in enumerate(BUCKETS_MS) if duration <= bound),
            len(BUCKETS_MS),
        )
        stats["duration_histogram"][bucket] += 1
        self.observations += 1
        # Preserve slow awaits plus one ordinary observation per stage/100 ms.
        if duration < 40 and end_ms - self.sampled_at.get(stage, float("-inf")) < 100:
            return
        self.sampled_at[stage] = end_ms
        event = dict(
            stage=stage, start_ms=start_ms, end_ms=end_ms, duration_ms=duration
        )
        event.update(
            {
                k: v
                for k, v in fields.items()
                if k in _NUMERIC_FIELDS and isinstance(v, (int, float))
            }
        )
        if len(self.first_events) < 32:
            self.first_events.append(event)
        else:
            self.recent_events.append(event)
        self.sampled_events += 1

    def snapshot(self, outcome):
        return dict(
            schema="chronicle.voice-cadence.v1",
            identity=self.identity,
            clock_domain=PROCESS_CLOCK,
            audio_coordinates={
                "sample_rate_hz": 24000,
                "opus_frame_samples": 480,
                "opus_pre_skip_samples": self.pre_skip_samples,
                "source_pcm": "zero-based unpadded PCM; producer_wait/encoding sample_start",
                "encoded_packets": "zero-based sequence; sample_end=(sequence+1)*480 includes codec lookahead/final padding",
            },
            clock_alignment="Local wall/monotonic anchor pairs; no cross-process clock synchronization assumed",
            started_monotonic_ms=self.started_ms,
            started_wall_ms=self.started_wall_ms,
            ended_monotonic_ms=time.perf_counter() * 1000,
            ended_wall_ms=time.time() * 1000,
            outcome=str(outcome)[:80],
            histogram_upper_bounds_ms=BUCKETS_MS,
            aggregates=self.aggregates,
            timeline=self.first_events + list(self.recent_events),
            observations=self.observations,
            sampled_events=self.sampled_events,
            timeline_evictions=max(0, self.sampled_events - MAX_TIMELINE_EVENTS),
        )

    async def flush(self, outcome):
        if self.flushed:
            return
        self.flushed = True
        try:

            content = json.dumps(
                self.snapshot(self.terminal_outcome or outcome), separators=(",", ":")
            ).encode()
            await asyncio.wait_for(
                client_diagnostics.store_client_diagnostic(
                    user_id=self.identity["user_id"],
                    content=content,
                    platform=self.platform,
                    device_id=self.identity["client_id"],
                    app_version="voice-cadence-v1",
                ),
                1.0,
            )
        except Exception as error:
            # Diagnostics failure must never retry a paid turn or interrupt capture.
            LOGGER.warning(
                "Voice cadence diagnostics unavailable: %s", type(error).__name__
            )


def current_recorder():
    return _CURRENT.get()


@contextmanager
def cadence_span(stage, **fields):
    recorder = _CURRENT.get()
    if recorder is None:
        yield
        return
    started = time.perf_counter() * 1000
    try:
        yield
    finally:
        try:
            recorder.observe(stage, started, time.perf_counter() * 1000, **fields)
        except Exception:
            LOGGER.warning("Voice cadence observation failed", exc_info=False)
