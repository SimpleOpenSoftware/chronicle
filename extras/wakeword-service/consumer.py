"""Redis stream consumer for the Hermes wake-word service.

Consumes generated events from ``audio:v2:realtime:*`` with the dedicated
``wakeword-v2`` consumer group.

For each session stream it maintains per-session wake state in
:class:`HermesDetector`. On a captured turn it resolves the command text from
the existing ``transcription:results:{session_id}`` stream (no second ASR) and
publishes a ``wake_word.detected`` message to the ``wakeword:detections`` stream
for the backend-side dispatcher to forward to the Hermes plugin.
"""

import asyncio
import base64
import dataclasses
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Dict

import redis.asyncio as redis
from audio_contract.v2 import audio_pb2
from detector import (
    RECEPTIVE_FIELD_SECONDS,
    SAMPLE_RATE,
    ClientWakeState,
    HermesDetector,
    WakeEvent,
)
from identities import (
    AudioChunkRef,
    AudioSessionRef,
    AudioStreamName,
    ClientId,
    SessionId,
    audio_session_key,
    device_downlink_channel,
    parse_audio_stream_name,
)
from redis import exceptions as redis_exceptions
from samples import PENDING, SampleStore

logger = logging.getLogger(__name__)

STREAM_PATTERN = "audio:v2:realtime:*"
GROUP_NAME = "wakeword-v2"
DETECTIONS_STREAM = "wakeword:detections"

# Stop processing a stream after this long with no new chunks (zombie guard).
STREAM_IDLE_TIMEOUT_SECONDS = 300

# Dev: persist the interpreter buffer state at arm (embeddings + ~10 s raw audio)
# alongside each captured clip, so a false positive is exactly reproducible
# offline. On by default; set WAKEWORD_SAVE_BUFFER_STATE=0 to disable.
SAVE_BUFFER_STATE = os.getenv("WAKEWORD_SAVE_BUFFER_STATE", "1").lower() not in (
    "0",
    "false",
    "no",
)

PROBE_RESULT_RETENTION_SECONDS = 300
# A wake decision arriving several seconds behind live audio is already a user-visible
# miss even though the task is alive. Redis stream ids are server wall-clock
# milliseconds, so they are a reliable queue-age signal independent of device clocks.
MAX_DELIVERY_LAG_MS = float(os.getenv("WAKEWORD_MAX_DELIVERY_LAG_MS", "2000"))
PERFORMANCE_WINDOW = 1200


def _percentile_ms(values: deque[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1))))
    return round(ordered[index], 1)


@dataclasses.dataclass
class WakeProbe:
    probe_id: str
    client_id: ClientId
    session_id: SessionId
    wakeword: str
    status: str
    created_at: float
    deadline_at: float
    created_monotonic: float
    deadline_monotonic: float
    completed_at: float | None = None
    detection: dict | None = None


class WakeWordConsumer:
    """Discovers audio streams and runs acoustic wake detection on each."""

    def __init__(
        self, detector: HermesDetector, redis_url: str, sample_store: SampleStore
    ):
        """Initialize the consumer.

        Args:
            detector: Loaded :class:`HermesDetector`.
            redis_url: Redis connection URL (shared with the backend).
            sample_store: Store for captured wake-word clips (the training loop).
        """
        self.detector = detector
        self.redis_url = redis_url
        self.sample_store = sample_store
        self.redis_client: redis.Redis | None = None
        self.consumer_name = f"wakeword-worker-{os.getpid()}"
        self.running = False
        # session_id -> asyncio.Task processing that stream
        self._stream_tasks: Dict[SessionId, asyncio.Task] = {}
        # session_id -> live wake state, so HTTP handlers can prime a stream.
        self._states: Dict[SessionId, ClientWakeState] = {}
        # session_id -> stable device client_id, resolved from session metadata.
        self._client_ids: Dict[SessionId, ClientId] = {}
        self._probes: dict[str, WakeProbe] = {}
        self._active_probe_by_session: dict[SessionId, str] = {}
        self._monotonic = time.monotonic
        self._wall_time = time.time
        self.frames_consumed = 0
        self.error_count = 0
        self.last_success_at: float | None = None
        self.last_consumed_id: str | None = None
        self._stream_progress: dict[SessionId, dict] = {}
        self._lagging_sessions: set[SessionId] = set()
        self._processing_ms: deque[float] = deque(maxlen=PERFORMANCE_WINDOW)

    def _record_stream_progress(
        self,
        session_id: SessionId,
        stream_name: AudioStreamName,
        message_id: str,
        *,
        processed_at_ms: float | None = None,
        processing_ms: float | None = None,
    ) -> None:
        """Record server-side delivery age after one frame finishes scoring."""
        if processed_at_ms is None:
            processed_at_ms = self._wall_time() * 1000
        try:
            produced_at_ms = float(message_id.split("-", 1)[0])
        except (TypeError, ValueError, IndexError):
            produced_at_ms = processed_at_ms
        delivery_lag_ms = max(0.0, processed_at_ms - produced_at_ms)
        progress = self._stream_progress.setdefault(
            session_id,
            {
                "stream": str(stream_name),
                "frames_consumed": 0,
            },
        )
        progress.update(
            {
                "last_consumed_id": message_id,
                "last_success_at": processed_at_ms / 1000,
                "delivery_lag_ms": round(delivery_lag_ms, 1),
            }
        )
        if processing_ms is not None:
            processing_ms = max(0.0, processing_ms)
            progress["processing_ms"] = round(processing_ms, 1)
            self._processing_ms.append(processing_ms)
        progress["frames_consumed"] += 1
        self.frames_consumed += 1
        self.last_success_at = processed_at_ms / 1000
        self.last_consumed_id = message_id
        if delivery_lag_ms > MAX_DELIVERY_LAG_MS:
            if session_id not in self._lagging_sessions:
                logger.warning(
                    "Wake detector is behind real time for '%s': %.0fms delivery "
                    "lag (threshold %.0fms, last id %s)",
                    session_id,
                    delivery_lag_ms,
                    MAX_DELIVERY_LAG_MS,
                    message_id,
                )
            self._lagging_sessions.add(session_id)
        elif session_id in self._lagging_sessions:
            self._lagging_sessions.remove(session_id)
            logger.info("Wake detector caught up for '%s'", session_id)

    def health(self) -> dict:
        """Liveness plus real-time progress for currently processed streams."""
        active_sessions = {
            session_id
            for session_id, task in self._stream_tasks.items()
            if not task.done()
        }
        streams = [
            dict(progress)
            for session_id, progress in self._stream_progress.items()
            if session_id in active_sessions
        ]
        lagging_streams = sum(
            progress.get("delivery_lag_ms", 0) > MAX_DELIVERY_LAG_MS
            for progress in streams
        )
        lags = [progress.get("delivery_lag_ms", 0.0) for progress in streams]
        return {
            "healthy": self.running and lagging_streams == 0,
            "running": self.running,
            "active_streams": len(active_sessions),
            "frames_consumed": self.frames_consumed,
            "error_count": self.error_count,
            "last_success_at": self.last_success_at,
            "last_consumed_id": self.last_consumed_id,
            "delivery_lag_threshold_ms": MAX_DELIVERY_LAG_MS,
            "delivery_lag_max_ms": max(lags) if lags else None,
            "lagging_streams": lagging_streams,
            "processing_p50_ms": _percentile_ms(self._processing_ms, 0.50),
            "processing_p95_ms": _percentile_ms(self._processing_ms, 0.95),
            "processing_max_ms": _percentile_ms(self._processing_ms, 1.0),
            "streams": streams,
        }

    def start_probe(
        self,
        client_id: str,
        audio_session_id: str,
        wakeword: str,
        *,
        timeout_seconds: float = 15,
    ) -> dict:
        """Start one production-equivalent, side-effect-free probe on a live stream."""
        if wakeword not in self.detector.wakewords:
            raise ValueError(f"unknown wake word '{wakeword}'")
        if wakeword in getattr(self.detector, "disabled", set()):
            raise ValueError(f"wake word '{wakeword}' is disabled")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        self._expire_probes()
        client_ref = ClientId.from_value(client_id)
        session_id = SessionId.from_value(audio_session_id)
        task = self._stream_tasks.get(session_id)
        state = self._states.get(session_id)
        if (
            self._client_ids.get(session_id) != client_ref
            or state is None
            or task is None
            or task.done()
        ):
            raise LookupError("no active stream for client and audio session")
        if getattr(state, "armed", False) or getattr(state, "priming", False):
            raise ValueError("stream is already armed or priming")
        if session_id in self._active_probe_by_session:
            raise ValueError("a wake probe is already active for this session")
        now_mono = self._monotonic()
        now_wall = self._wall_time()
        probe = WakeProbe(
            probe_id=str(uuid.uuid4()),
            client_id=client_ref,
            session_id=session_id,
            wakeword=wakeword,
            status="listening",
            created_at=now_wall,
            deadline_at=now_wall + timeout_seconds,
            created_monotonic=now_mono,
            deadline_monotonic=now_mono + timeout_seconds,
        )
        self._probes[probe.probe_id] = probe
        self._active_probe_by_session[session_id] = probe.probe_id
        return self._probe_payload(probe)

    def get_probe(self, probe_id: str) -> dict:
        self._expire_probes()
        probe = self._probes.get(probe_id)
        if probe is None:
            raise LookupError("wake probe not found")
        return self._probe_payload(probe)

    def cancel_probe(self, probe_id: str) -> dict:
        self._expire_probes()
        probe = self._probes.get(probe_id)
        if probe is None:
            raise LookupError("wake probe not found")
        if probe.status == "listening":
            self._complete_probe(probe, "cancelled")
        return self._probe_payload(probe)

    def _probe_payload(self, probe: WakeProbe) -> dict:
        return {
            "probe_id": probe.probe_id,
            "client_id": str(probe.client_id),
            "session_id": str(probe.session_id),
            "wakeword": probe.wakeword,
            "status": probe.status,
            "created_at_ms": round(probe.created_at * 1000),
            "deadline_at_ms": round(probe.deadline_at * 1000),
            "completed_at_ms": (
                round(probe.completed_at * 1000)
                if probe.completed_at is not None
                else None
            ),
            "detection": probe.detection,
        }

    def _expire_probes(self) -> None:
        now_mono = self._monotonic()
        now_wall = self._wall_time()
        for probe in list(self._probes.values()):
            if probe.status == "listening" and now_mono >= probe.deadline_monotonic:
                self._complete_probe(probe, "timed_out")
            elif (
                probe.completed_at is not None
                and now_wall - probe.completed_at > PROBE_RESULT_RETENTION_SECONDS
            ):
                self._probes.pop(probe.probe_id, None)

    def _complete_probe(
        self, probe: WakeProbe, status: str, detection: dict | None = None
    ) -> None:
        if probe.status != "listening":
            return
        probe.status = status
        probe.completed_at = self._wall_time()
        probe.detection = detection
        self._active_probe_by_session.pop(probe.session_id, None)

    def _active_probe(self, session_id: SessionId) -> WakeProbe | None:
        self._expire_probes()
        probe_id = self._active_probe_by_session.get(session_id)
        return self._probes.get(probe_id) if probe_id else None

    def _close_probe_for_session(self, session_id: SessionId) -> None:
        probe = self._active_probe(session_id)
        if probe is not None:
            self._complete_probe(probe, "stream_closed")

    def active_clients(self) -> list[dict]:
        """List currently-processing streams (for the data-collection UI)."""
        out = []
        for session_id, task in self._stream_tasks.items():
            if task.done():
                continue
            client_id = self._client_ids.get(session_id)
            state = self._states.get(session_id)
            if client_id is None or state is None:
                continue
            out.append(
                {
                    "client_id": str(client_id),
                    "session_id": str(session_id),
                    "priming": bool(state and state.priming),
                    "prime_wakeword": (
                        state.prime_wakeword if state and state.priming else None
                    ),
                    "armed": bool(state and state.armed),
                }
            )
        return out

    def prime(self, client_id: str, wakeword: str) -> bool:
        """Arm a one-shot positive capture of ``wakeword`` on an active stream.

        Returns False if the stream is unknown. Raises ValueError if ``wakeword``
        is not a configured wake word.
        """
        # A reconnect can briefly leave an older session draining. Walk newest
        # first so the command targets the device's current live stream.
        client_ref = ClientId.from_value(client_id)
        for session_id in reversed(self._stream_tasks):
            if self._client_ids.get(session_id) != client_ref:
                continue
            state = self._states.get(session_id)
            task = self._stream_tasks.get(session_id)
            if state is not None and task is not None and not task.done():
                self.detector.start_priming(state, client_ref, wakeword)
                return True
        return False

    def unprime(self, client_id: str) -> bool:
        """Manually end an in-progress prime capture (UI 'stop'). False if unknown.

        The per-stream task finalizes and saves on its next frame, so the captured
        attempt always lands in the review queue rather than being dropped.
        """
        client_ref = ClientId.from_value(client_id)
        for session_id in reversed(self._stream_tasks):
            if self._client_ids.get(session_id) != client_ref:
                continue
            state = self._states.get(session_id)
            task = self._stream_tasks.get(session_id)
            if state is not None and task is not None and not task.done():
                self.detector.stop_priming(state)
                return True
        return False

    async def start(self) -> None:
        """Connect to Redis and run the discovery + processing loop."""
        self.redis_client = redis.from_url(self.redis_url)
        self.running = True
        logger.info(
            f"WakeWordConsumer started (group={GROUP_NAME}, redis={self.redis_url})"
        )
        try:
            while self.running:
                # A transient Redis failure (e.g. Redis restarting during a stack
                # restart) must NOT permanently kill the discovery loop — otherwise
                # the HTTP server stays up reporting "healthy" while no audio is ever
                # consumed again. Catch, log, back off, and retry; the redis.asyncio
                # connection pool reconnects on the next command.
                try:
                    await self._discover_and_spawn()
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    raise
                except redis_exceptions.RedisError as e:
                    logger.warning(
                        f"Redis error in discovery loop (retrying in 2s): {e}"
                    )
                    await asyncio.sleep(2.0)
                except Exception as e:  # noqa: BLE001 - loop must never die silently
                    logger.error(
                        f"Unexpected error in discovery loop (retrying in 2s): {e}",
                        exc_info=True,
                    )
                    await asyncio.sleep(2.0)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Signal the consumer to stop."""
        self.running = False

    async def _discover_and_spawn(self) -> None:
        streams = await self._discover_streams()
        live_sessions: set[SessionId] = set()
        for raw_stream_name in streams:
            stream_name = AudioStreamName.from_value(raw_stream_name)
            session_id = parse_audio_stream_name(stream_name)
            # A device that drops without a clean end-marker leaves its
            # audio:stream key behind (session stuck "active"). Without this
            # check we'd re-spawn a task for that dead key every time the
            # previous one idled out, so it perpetually shows as an "active
            # stream". Only process streams that got a chunk recently.
            if not await self._stream_is_live(stream_name):
                continue
            live_sessions.add(session_id)
            task = self._stream_tasks.get(session_id)
            if task is None or task.done():
                if task is not None and task.done():
                    # Surface any exception from the finished task.
                    exc = task.exception()
                    if exc is not None:
                        logger.error(f"Stream task for '{session_id}' failed: {exc}")
                self._stream_tasks[session_id] = asyncio.create_task(
                    self._process_stream(stream_name, session_id)
                )
        # Reap tasks whose stream is gone or has gone stale, so they stop being
        # reported as active streams (and free their per-client detector state).
        for session_id, task in list(self._stream_tasks.items()):
            if task.done():
                self._stream_tasks.pop(session_id, None)
                self._client_ids.pop(session_id, None)
            elif session_id not in live_sessions:
                logger.info(f"Reaping wake stream task for stale '{session_id}'")
                task.cancel()
                self._stream_tasks.pop(session_id, None)
                self._client_ids.pop(session_id, None)

    async def _stream_is_live(self, stream_name: AudioStreamName) -> bool:
        """True if the stream received a chunk within the idle window.

        Redis stream entry ids are wall-clock-ms based (server-assigned on XADD),
        so the last entry's id tells us how long ago audio last arrived — the
        signal that distinguishes a live stream from an abandoned one whose key
        Redis still holds.
        """
        try:
            entries = await self.redis_client.xrevrange(str(stream_name), count=1)
        except redis_exceptions.ResponseError:
            return False
        if not entries:
            return False
        last_id = entries[0][0]
        last_id = last_id.decode() if isinstance(last_id, bytes) else last_id
        try:
            ts_ms = int(last_id.split("-")[0])
        except (ValueError, IndexError):
            return False
        return (time.time() * 1000 - ts_ms) < STREAM_IDLE_TIMEOUT_SECONDS * 1000

    async def _discover_streams(self) -> list[str]:
        streams: list[str] = []
        cursor = b"0"
        while cursor:
            cursor, keys = await self.redis_client.scan(
                cursor, match=STREAM_PATTERN, count=100
            )
            streams.extend(k.decode() if isinstance(k, bytes) else k for k in keys)
        return streams

    async def _setup_group(self, stream_name: AudioStreamName) -> None:
        try:
            await self.redis_client.xgroup_create(
                str(stream_name), GROUP_NAME, "0", mkstream=True
            )
            logger.debug(f"Created group {GROUP_NAME} for {stream_name}")
        except redis_exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def _process_stream(
        self, stream_name: AudioStreamName, session_id: SessionId
    ) -> None:
        await self._setup_group(stream_name)
        session_ref = await self._lookup_audio_session_ref(session_id)
        state = self.detector.new_client_state()
        self._states[session_id] = state
        self._client_ids[session_id] = session_ref.client_id
        last_activity = time.time()
        logger.info(
            f"▶ Processing wake stream '{stream_name}' for client '{session_ref.client_id}'"
        )

        try:
            while self.running:
                try:
                    messages = await self.redis_client.xreadgroup(
                        GROUP_NAME,
                        self.consumer_name,
                        {str(stream_name): ">"},
                        count=10,
                        block=1000,
                    )
                except redis_exceptions.ResponseError as error:
                    if "NOGROUP" not in str(error):
                        raise
                    logger.info(
                        "Wake stream '%s' was reclaimed after drainage — ending",
                        stream_name,
                    )
                    return

                if not messages:
                    if time.time() - last_activity > STREAM_IDLE_TIMEOUT_SECONDS:
                        await self._flush(state, session_ref)
                        logger.info(
                            f"Stream '{stream_name}' idle — ending wake processing"
                        )
                        return
                    continue

                for _stream, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        msg_id = (
                            message_id.decode()
                            if isinstance(message_id, bytes)
                            else message_id
                        )
                        try:
                            event = audio_pb2.CaptureStreamEvent()
                            if set(fields) != {b"event"}:
                                raise RuntimeError(
                                    f"audio-v2 entry {msg_id} has untyped fields"
                                )
                            event.ParseFromString(fields[b"event"])
                            event_kind = event.WhichOneof("event")
                            if event_kind in {"ended", "failed"}:
                                await self.redis_client.xack(
                                    str(stream_name), GROUP_NAME, msg_id
                                )
                                await self._flush(state, session_ref)
                                logger.info(f"End marker on '{stream_name}' — ending")
                                return

                            pcm = (
                                event.frame.pcm_s16le if event_kind == "frame" else b""
                            )
                            if pcm:
                                last_activity = time.time()
                                chunk_ref = AudioChunkRef(
                                    captured_at=(
                                        event.frame.captured_at.ToMilliseconds()
                                        / 1000.0
                                    ),
                                    time_basis="captured",
                                    sample_rate=16000,
                                    channels=1,
                                    sample_width=2,
                                )
                                processing_started = time.perf_counter()
                                await self._process_detector_frame(
                                    state, session_ref, chunk_ref, pcm
                                )
                                self._record_stream_progress(
                                    session_id,
                                    stream_name,
                                    msg_id,
                                    processing_ms=(
                                        time.perf_counter() - processing_started
                                    )
                                    * 1000,
                                )
                        except Exception:
                            self.error_count += 1
                            raise
                        finally:
                            await self.redis_client.xack(
                                str(stream_name), GROUP_NAME, msg_id
                            )
        finally:
            self._close_probe_for_session(session_id)
            self._states.pop(session_id, None)
            self._client_ids.pop(session_id, None)

    async def _process_detector_frame(
        self,
        state: ClientWakeState,
        session_ref: AudioSessionRef,
        chunk_ref: AudioChunkRef,
        pcm: bytes,
    ) -> None:
        """Run production detection, with the probe intercept at the arm seam."""
        was_armed = state.armed
        probe = self._active_probe(session_ref.session_id)
        if probe is None:
            event = await self.detector.process_frame(
                state, session_ref, chunk_ref, pcm
            )
        else:
            event = await self.detector.process_frame(
                state,
                session_ref,
                chunk_ref,
                pcm,
                probe_wakeword=probe.wakeword,
            )
        if event is not None and getattr(event, "collect_only", False) and probe:
            # Collect-only is deliberately not production-equivalent: it bypasses the
            # verifier. Ignore and suppress its sample-farming side effect during a probe.
            return
        if state.armed and not was_armed and not state.priming:
            if probe is not None:
                detected_word = state.armed_wakeword or ""
                self._complete_probe(
                    probe,
                    "detected" if detected_word == probe.wakeword else "wrong_word",
                    {
                        "wakeword": detected_word,
                        "score": round(state.arm_score, 6),
                        "verifier_passed": state.arm_verifier_passed,
                        "verifier_score": (
                            round(state.arm_verifier_score, 6)
                            if state.arm_verifier_score is not None
                            else None
                        ),
                        "wake_trace_id": state.wake_trace_id,
                        "capture_epoch": state.arm_capture_epoch,
                        "armed_at_ms": (
                            round(state.arm_occurred_at * 1000)
                            if state.arm_occurred_at is not None
                            else None
                        ),
                        "arm_offset_ms": state.arm_offset_ms,
                    },
                )
                self.detector.reset_armed_state(state)
                return
            await self._on_armed(state, session_ref)
        if event is not None:
            await self._handle_event(event)

    async def _flush(self, state, session_ref: AudioSessionRef) -> None:
        """Finalize an armed-but-uncaptured turn when the stream ends/goes idle."""
        probe = self._active_probe(session_ref.session_id)
        if probe is not None:
            self._complete_probe(probe, "stream_closed")
            if state.armed:
                self.detector.reset_armed_state(state)
            return
        event = self.detector.flush(state, session_ref)
        if event is not None:
            await self._handle_event(event)

    async def _handle_event(self, event: WakeEvent) -> None:
        """Route a captured event: persist training data and (for real arms)
        dispatch the command to the Hermes plugin via Redis."""
        if event.kind == "primed_positive":
            # "Prime + say it" capture -> review queue (pending), same as a real
            # arm, so the user confirms wake / not-wake before it rolls into
            # training. Rescued false-negatives become positives once labeled.
            # Off-loop: the disk write must not stall the per-client frame loop.
            await asyncio.to_thread(self._save_sample, PENDING, event, event.audio)
            return
        # Real acoustic arm. Play the end-of-listening tone FIRST — it's a pure ack
        # that needs only client_id, so it must never wait behind the disk write /
        # user lookup / SSE / XADD below (a slow/near-full disk would otherwise make
        # the tone lag). Collect-only (shadow) arms farm FP data silently: no tone,
        # no command dispatch.
        if not getattr(event, "collect_only", False):
            await self._publish_tone_request(event.client_id, event.session_id, "done")
        # Snapshot the trigger window for false-positive review. Off-loop so the
        # synchronous WAV/JSON writes don't block the frame loop.
        await asyncio.to_thread(self._save_sample, PENDING, event, event.trigger_audio)
        if getattr(event, "collect_only", False):
            return
        await self._publish_detection(event)

    def _save_sample(self, bucket: str, event: WakeEvent, pcm: bytes) -> None:
        """Persist a captured clip into the on-disk training store, scoped to the
        event's wake word."""
        if not pcm:
            return
        meta = {
            "client_id": str(event.client_id),
            "session_id": str(event.session_id),
            "score": round(event.score, 4),
            "reason": event.reason,
            "kind": event.kind,
            "also_fired": list(event.also_fired),
            "collect_only": getattr(event, "collect_only", False),
            "source": (
                "prime"
                if event.kind == "primed_positive"
                else ("shadow" if getattr(event, "collect_only", False) else "arm")
            ),
            # The model's receptive field is ~1.96 s, and the arm fires at the END
            # of the pre-roll, so the activation is the LAST ~1.96 s of this clip.
            "activation_window_secs": RECEPTIVE_FIELD_SECONDS,
            "activation_at": "end",
        }
        if event.kind == "primed_positive":
            # Left UNLABELED so it surfaces in the pending review queue; the score
            # still flags whether the live model under-scored this utterance.
            meta["false_negative"] = event.is_false_negative
        else:
            # Real arm: record whether the captured turn held speech. A near-silent
            # arm (has_speech=False) is a false positive whose command ASR the
            # backend skipped — flagged here so it's visible in the review store.
            meta["has_speech"] = event.has_speech
        # Dev: attach the interpreter buffer state captured at arm so the FP is
        # exactly reproducible offline (command arms only carry it).
        features = event.buffer_features if SAVE_BUFFER_STATE else None
        context_pcm = event.buffer_context if SAVE_BUFFER_STATE else None
        try:
            rec = self.sample_store.save(
                event.wakeword,
                bucket,
                pcm,
                SAMPLE_RATE,
                int(time.time() * 1000),
                meta,
                features=features,
                context_pcm=context_pcm,
            )
            logger.info(
                f"💾 saved {bucket} sample {rec['id']} ({len(pcm)}B"
                f"{', +buffer-state' if rec.get('has_buffer_state') else ''})"
            )
        except (
            Exception
        ) as e:  # noqa: BLE001 - data collection must never break dispatch
            logger.error(f"Failed to save {bucket} sample: {e}", exc_info=True)

    async def _publish_detection(self, event: WakeEvent) -> None:
        """Publish a wake_word.detected message carrying the captured command audio.

        The backend dispatcher batch-transcribes ``audio_b64`` for a higher-quality
        command than the streaming transcript would give, and avoids fragile
        timestamp alignment against the transcription results stream.
        """
        user_id = await self._lookup_user_id(event.session_id)
        if (
            not event.wake_trace_id
            or event.capture_epoch is None
            or event.armed_at is None
            or event.end_of_turn_at is None
            or event.trigger_interval is None
            or event.command_interval is None
        ):
            raise RuntimeError(
                "dispatch wake event lacks absolute interaction identity"
            )
        # Immediate UI pulse for end-of-turn, before the (slower) batch ASR + plugin
        # dispatch the backend does — keeps the live-recording feedback snappy.
        await self._publish_sse(
            user_id,
            "wake.end_of_turn",
            {
                "client_id": str(event.client_id),
                "session_id": str(event.session_id),
                "reason": event.reason,
                "duration": round(event.eot_time - event.arm_time, 2),
            },
        )
        # Amber "Thinking" ring on LED-capable devices: capture is done, the backend
        # is now batch-transcribing + dispatching. The executor refreshes this when
        # dispatch actually starts; it reverts on its own if no command follows.
        await self._publish_downlink(
            event.client_id,
            "led-control",
            {
                "effect": "Thinking",
                "r": 1.0,
                "g": 0.45,
                "b": 0.0,
                "brightness": 0.45,
                "duration": 8.0,
            },
        )
        # NOTE: the end-of-listening ("done") tone is played up front in
        # _handle_event, before this bookkeeping, so it never lags under load.
        payload = {
            "client_id": str(event.client_id),
            "session_id": str(event.session_id),
            "user_id": user_id,
            "wakeword": event.wakeword,
            "also_fired": list(event.also_fired),
            "score": round(event.score, 4),
            "reason": event.reason,
            "sample_rate": SAMPLE_RATE,
            "audio_b64": base64.b64encode(event.audio).decode("ascii"),
            "has_speech": event.has_speech,
            "wake_trace_id": event.wake_trace_id,
            "capture_epoch": event.capture_epoch,
            "armed_at": event.armed_at,
            "end_of_turn_at": event.end_of_turn_at,
            "trigger_interval": dataclasses.asdict(event.trigger_interval),
            "command_interval": dataclasses.asdict(event.command_interval),
        }
        await self.redis_client.xadd(
            DETECTIONS_STREAM,
            {b"event": json.dumps(payload).encode()},
        )
        logger.info(
            f"📤 Published wake_word.detected for '{event.client_id}' "
            f"({len(event.audio)}B audio, reason={event.reason})"
        )

    async def _on_armed(
        self, state: ClientWakeState, session_ref: AudioSessionRef
    ) -> None:
        """Push a UI pulse the instant the wake word arms (before capture/ASR)."""
        # Listening tone FIRST — a pure ack needing only client_id, so it never waits
        # behind the user lookup / SSE below (keeps the cue instant under load).
        await self._publish_tone_request(
            session_ref.client_id, session_ref.session_id, "armed"
        )
        # Cyan "Listening" ring on LED-capable devices (HAVPE). Like the tone it only
        # needs client_id, so it stays snappy; non-LED clients ignore the frame.
        await self._publish_downlink(
            session_ref.client_id,
            "led-control",
            {
                "effect": "Listening For Command",
                "r": 0.09,
                "g": 0.73,
                "b": 0.95,
                "brightness": 0.45,
                "duration": 12.0,
            },
        )
        user_id = await self._lookup_user_id(session_ref.session_id)
        await self._publish_sse(
            user_id,
            "wake.armed",
            {
                "client_id": str(session_ref.client_id),
                "session_id": str(session_ref.session_id),
                "score": round(getattr(state, "arm_score", 0.0), 4),
            },
        )
        logger.info(f"🔔 wake.armed SSE for '{session_ref.client_id}'")

    async def _publish_tone_request(
        self, client_id: ClientId, session_id: SessionId, tone: str
    ) -> None:
        """Publish a semantic cue; the backend response coordinator owns audio."""
        if tone not in {"armed", "done"}:
            raise ValueError(f"unsupported wake tone: {tone}")
        await self.redis_client.xadd(
            DETECTIONS_STREAM,
            {
                "event": json.dumps(
                    {
                        "kind": "tone",
                        "client_id": str(client_id),
                        "session_id": str(session_id),
                        "tone": tone,
                    },
                    separators=(",", ":"),
                )
            },
        )

    async def _publish_downlink(
        self, client_id: ClientId, msg_type: str, data: dict
    ) -> None:
        """Push a control message to the device via ``device:downlink:{client_id}``.

        The backend's WebSocket handler subscribes to this channel and forwards the
        frame down to the HAVPE relay, which plays it on the device. Best-effort —
        a missing/audio-only device just ignores it.
        """
        if not isinstance(client_id, ClientId):
            raise TypeError("_publish_downlink requires ClientId")
        try:
            message = json.dumps({"type": msg_type, "data": data})
            await self.redis_client.publish(
                str(device_downlink_channel(client_id)), message
            )
        except (
            Exception
        ) as e:  # noqa: BLE001 - downlink is best-effort, never break dispatch
            logger.debug(f"Failed to publish downlink {msg_type}: {e}")

    async def _publish_sse(self, user_id: str, event_type: str, data: dict) -> None:
        """Publish an SSE event to the user's channel (best-effort, never raises).

        Mirrors the backend ``sse_publisher`` wire format ({event, data, timestamp})
        so the ``/api/events/stream`` endpoint relays it to the browser unchanged.
        """
        if not user_id:
            return
        try:
            message = json.dumps(
                {"event": event_type, "data": data, "timestamp": time.time()}
            )
            await self.redis_client.publish(f"sse:{user_id}", message)
        except (
            Exception
        ) as e:  # noqa: BLE001 - SSE is best-effort, never break dispatch
            logger.debug(f"Failed to publish SSE {event_type}: {e}")

    async def _lookup_user_id(self, session_id: SessionId) -> str:
        """Read user_id from the session metadata hash."""
        try:
            val = await self.redis_client.hget(
                str(audio_session_key(session_id)), "user_id"
            )
            if val is not None:
                return val.decode() if isinstance(val, bytes) else val
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read user_id for {session_id}: {e}")
        return ""

    async def _lookup_audio_session_ref(self, session_id: SessionId) -> AudioSessionRef:
        """Resolve the stable device id from authoritative session metadata."""
        if self.redis_client is None:
            raise RuntimeError("Redis is not connected")
        client_value, epoch_value, started_value = await self.redis_client.hmget(
            str(audio_session_key(session_id)),
            "client_id",
            "capture_epoch",
            "started_at",
        )
        if client_value is None or epoch_value is None or started_value is None:
            raise RuntimeError(
                f"Audio session '{session_id}' lacks client/epoch/start identity"
            )
        client_id = ClientId.from_value(client_value, "client_id")
        epoch = int(
            epoch_value.decode() if isinstance(epoch_value, bytes) else epoch_value
        )
        started_at = float(
            started_value.decode()
            if isinstance(started_value, bytes)
            else started_value
        )
        return AudioSessionRef(
            session_id=session_id,
            client_id=client_id,
            capture_epoch=epoch,
            started_at=started_at,
        )

    async def _shutdown(self) -> None:
        for task in self._stream_tasks.values():
            task.cancel()
        self._stream_tasks.clear()
        self._states.clear()
        self._client_ids.clear()
        for probe in list(self._probes.values()):
            self._complete_probe(probe, "stream_closed")
        if self.redis_client is not None:
            await self.redis_client.aclose()
        logger.info("WakeWordConsumer stopped")
