"""Per-turn voice timing: typed observations, monotonic spans and one report.

The existing interaction-ledger consumer persists these events. Audio clocks are
never subtracted from process clocks. Wall time is only a waterfall alignment aid.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from google.protobuf.json_format import MessageToDict, ParseDict

from backend.audio_contract.v2 import audio_pb2

STREAM = "wakeword:interaction-events"
COLLECTION = "voice_interaction_events"
PROCESS_CLOCK = f"process:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
logger = logging.getLogger(__name__)
STAGES = {
    "speech_started",
    "speech_ended",
    "turn_committed",
    "turn_received",
    "stt",
    "stt_wait",
    "stt_batch",
    "routing",
    "agent",
    "notification",
    "tts",
    "encoding",
    "downlink",
    "mode_queue",
    "mode_handler",
    "response_queued",
    "response_ready",
    "response_offered",
    "response_started",
    "response_done",
    "response_cancelled",
    "response_failed",
    "turn_failed",
    "turn_ignored",
}


@dataclass(frozen=True)
class TimingIdentity:
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    turn_id: str
    turn_revision: int = 0

    @classmethod
    def from_turn(cls, turn, user_id: str, client_id: str):
        interval = turn.interval
        return cls(
            user_id,
            client_id,
            interval.audio_session_id,
            interval.capture_epoch,
            interval.turn_id,
            interval.turn_revision,
        )

    @classmethod
    def from_response(cls, response):
        return cls(
            response.user_id,
            response.client_id,
            response.audio_session_id,
            response.capture_epoch,
            response.turn_id,
            response.turn_revision,
        )

    @property
    def device_clock(self) -> str:
        return f"device:{self.audio_session_id}:{self.capture_epoch}"


_current: ContextVar[VoiceTrace | None] = ContextVar("voice_trace", default=None)


class VoiceTrace:
    def __init__(
        self, redis, identity: TimingIdentity, *, response_id="", generation=0
    ):
        self.redis = redis
        self.identity = identity
        self.response_id = response_id
        self.generation = generation

    @contextmanager
    def bind(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def event(
        self,
        stage: str,
        *,
        timestamp_ms=None,
        clock_domain=PROCESS_CLOCK,
        observed_at_ms=None,
        duration_ms=None,
        outcome="ok",
        detail="",
    ):
        if stage not in STAGES:
            raise ValueError(f"unknown voice timing stage: {stage}")
        return audio_pb2.InteractionTimingEvent(
            event_id=str(uuid.uuid4()),
            **self.identity.__dict__,
            response_id=self.response_id,
            generation=self.generation,
            stage=stage,
            timestamp_ms=(
                time.perf_counter() * 1000 if timestamp_ms is None else timestamp_ms
            ),
            clock_domain=clock_domain,
            observed_at_ms=(
                time.time() * 1000 if observed_at_ms is None else observed_at_ms
            ),
            duration_ms=duration_ms,
            outcome=outcome,
            detail=detail,
        )

    async def emit(self, stage, **kwargs):
        event = self.event(stage, **kwargs)
        try:
            await self.redis.xadd(STREAM, {"timing": event.SerializeToString()})
        except Exception:
            # Telemetry failure must not replay an action or suppress the answer.
            logger.exception(
                "Voice timing publication failed for %s", self.identity.turn_id
            )

    @asynccontextmanager
    async def span(self, stage, *, detail=""):
        start, wall = time.perf_counter() * 1000, time.time() * 1000
        outcome = "ok"
        await self.emit(
            stage,
            timestamp_ms=start,
            observed_at_ms=wall,
            outcome="running",
            detail=detail,
        )
        try:
            yield
        except BaseException:
            outcome = "failed"
            raise
        finally:
            await self.emit(
                stage,
                timestamp_ms=start,
                observed_at_ms=wall,
                duration_ms=time.perf_counter() * 1000 - start,
                outcome=outcome,
                detail=detail,
            )


@asynccontextmanager
async def timing_span(stage: str, *, detail=""):
    trace = _current.get()
    if trace is None:
        yield
    else:
        async with trace.span(stage, detail=detail):
            yield


async def mark_timing(stage: str, *, detail="", outcome="ok"):
    trace = _current.get()
    if trace is not None:
        await trace.emit(stage, detail=detail, outcome=outcome)


def timing_document(event: audio_pb2.InteractionTimingEvent) -> dict:
    if (
        not all(
            (
                event.event_id,
                event.user_id,
                event.client_id,
                event.audio_session_id,
                event.turn_id,
                event.clock_domain,
            )
        )
        or event.stage not in STAGES
    ):
        raise ValueError("invalid voice timing identity/stage")
    for value in (event.timestamp_ms, event.observed_at_ms):
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid voice timing clock")
    if event.HasField("duration_ms") and (
        not math.isfinite(event.duration_ms) or event.duration_ms < 0
    ):
        raise ValueError("invalid voice timing duration")
    return MessageToDict(
        event,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


class VoiceTimingLedger:
    def __init__(self, collection):
        self.collection = collection

    async def initialize(self):
        await self.collection.create_index("event_id", unique=True)
        await self.collection.create_index("stored_at", expireAfterSeconds=30 * 86400)
        await self.collection.create_index([("user_id", 1), ("observed_at_ms", -1)])
        await self.collection.create_index(
            [
                ("user_id", 1),
                ("client_id", 1),
                ("audio_session_id", 1),
                ("capture_epoch", 1),
                ("turn_id", 1),
                ("turn_revision", 1),
            ]
        )

    async def append(self, event):
        document = {**timing_document(event), "stored_at": datetime.now(timezone.utc)}
        await self.collection.update_one(
            {"event_id": event.event_id}, {"$setOnInsert": document}, upsert=True
        )


def build_voice_report(documents: Iterable[dict]) -> dict:
    events = []
    for document in documents:
        document = {k: v for k, v in document.items() if k not in {"_id", "stored_at"}}
        event = ParseDict(document, audio_pb2.InteractionTimingEvent())
        events.append(timing_document(event))
    events = list({event["event_id"]: event for event in events}.values())
    if not events:
        raise ValueError("voice report requires events")
    identities = {
        (
            e["user_id"],
            e["client_id"],
            e["audio_session_id"],
            e["capture_epoch"],
            e["turn_id"],
            e["turn_revision"],
        )
        for e in events
    }
    if len(identities) != 1:
        raise ValueError("voice report requires exactly one user/turn/revision")
    events.sort(key=lambda e: (e["observed_at_ms"], e["event_id"]))
    first = events[0]
    metrics = {}
    missing = []
    invalid = []

    def point(stage, response_id=None):
        matches = [
            e
            for e in events
            if e["stage"] == stage
            and (response_id is None or e["response_id"] == response_id)
        ]
        return min(matches, key=lambda e: e["timestamp_ms"]) if matches else None

    def elapsed(name, start, end, *, require_device=False):
        if start is None or end is None:
            missing.append(name)
            return None
        if start["clock_domain"] != end["clock_domain"] or (
            require_device and not start["clock_domain"].startswith("device:")
        ):
            missing.append(name)
            return None
        value = end["timestamp_ms"] - start["timestamp_ms"]
        if value < 0:
            invalid.append(name)
            return None
        # Device durations use one clock, but speech detection and renderer-to-ear
        # latency remain estimates until a physical acoustic calibration exists.
        result = {
            "value_ms": round(value, 3),
            "quality": "estimated" if require_device else "measured",
        }
        metrics[name] = result
        return result

    speech_start, speech_end = point("speech_started"), point("speech_ended")
    elapsed("speaking", speech_start, speech_end, require_device=True)
    responses = []
    for response_id in dict.fromkeys(
        e["response_id"] for e in events if e["response_id"]
    ):
        response_events = [e for e in events if e["response_id"] == response_id]
        start, done = point("response_started", response_id), point(
            "response_done", response_id
        )
        terminal = next(
            (
                e
                for e in reversed(response_events)
                if e["stage"]
                in {"response_done", "response_failed", "response_cancelled"}
            ),
            None,
        )
        responses.append(
            {
                "response_id": response_id,
                "status": (
                    terminal["stage"].removeprefix("response_")
                    if terminal
                    else "pending"
                ),
                "started": start,
                "done": done,
            }
        )
    # A turn may produce replacements. Each response is retained; the headline
    # spans first heard audio to final completion (including any intervening gap).
    started = [r["started"] for r in responses if r["started"]]
    completed = [r["done"] for r in responses if r["done"]]
    response_start = min(started, key=lambda e: e["timestamp_ms"]) if started else None
    response_end = (
        max(completed, key=lambda e: e["timestamp_ms"]) if completed else None
    )
    if any(r["status"] != "done" for r in responses):
        response_end = None
    elapsed("waiting", speech_end, response_start, require_device=True)
    elapsed("reply", response_start, response_end, require_device=True)
    elapsed("total", speech_start, response_end, require_device=True)

    # Intermediate milestones cross the phone/server seam. Preserve their
    # uncertainty instead of disguising wall-clock subtraction as a local span.
    for name, stage in (
        ("speech_to_tts_request", "tts"),
        ("speech_to_audio_ready", "response_ready"),
        ("endpoint_and_ingress", "turn_received"),
    ):
        end = point(stage)
        if speech_end is not None and end is not None:
            value = end["observed_at_ms"] - speech_end["observed_at_ms"]
            if value >= 0:
                metrics[name] = {
                    "value_ms": round(value, 3),
                    "quality": "cross_clock_estimate",
                }

    for stage in (
        "stt_wait",
        "stt_batch",
        "stt",
        "routing",
        "agent",
        "notification",
        "tts",
        "encoding",
        "downlink",
        "mode_handler",
    ):
        spans = [e for e in events if e["stage"] == stage and "duration_ms" in e]
        if spans:
            metrics[stage] = {
                "value_ms": round(sum(e["duration_ms"] for e in spans), 3),
                "quality": "measured",
                "attempts": len(spans),
            }
    terminal_failure = any(
        e["stage"] in {"turn_failed", "response_failed"} for e in events
    )
    ignored = any(e["stage"] == "turn_ignored" for e in events)
    status = (
        "ignored"
        if ignored
        else (
            "failed"
            if terminal_failure
            else "complete" if not missing and not invalid else "incomplete"
        )
    )
    return {
        "turn_id": first["turn_id"],
        "turn_revision": first["turn_revision"],
        "audio_session_id": first["audio_session_id"],
        "capture_epoch": first["capture_epoch"],
        "client_id": first["client_id"],
        "started_at_ms": first["observed_at_ms"],
        "status": status,
        "metrics": metrics,
        "missing": missing,
        "invalid": invalid,
        "responses": [
            {k: v for k, v in r.items() if k not in {"started", "done"}}
            for r in responses
        ],
        "events": events,
        "alignment": "Wall-clock waterfall alignment is approximate; durations use their source clock.",
    }


async def recent_voice_reports(
    database, *, user_id: str, client_id: str | None, limit: int
):
    collection = database[COLLECTION]
    match = {
        "user_id": user_id,
        "observed_at_ms": {"$gte": (time.time() - 30 * 86400) * 1000},
    }
    if client_id:
        match["client_id"] = client_id
    turns = await collection.aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {
                        "client_id": "$client_id",
                        "audio_session_id": "$audio_session_id",
                        "capture_epoch": "$capture_epoch",
                        "turn_id": "$turn_id",
                        "turn_revision": "$turn_revision",
                    },
                    "latest": {"$max": "$observed_at_ms"},
                    "stages": {"$addToSet": "$stage"},
                }
            },
            {"$match": {"stages": {"$ne": "turn_ignored"}}},
            {"$sort": {"latest": -1}},
            {"$limit": limit},
        ]
    ).to_list(length=limit)
    reports = []
    for turn in turns:
        documents = (
            await collection.find({"user_id": user_id, **turn["_id"]})
            .sort("observed_at_ms", 1)
            .limit(1001)
            .to_list(length=1001)
        )
        report = build_voice_report(documents[:1000])
        report["truncated"] = len(documents) > 1000
        if report["truncated"]:
            report["status"] = "incomplete"
            report["missing"].append("trace_limit")
        reports.append(report)
    values = sorted(
        r["metrics"]["waiting"]["value_ms"]
        for r in reports
        if r["status"] == "complete" and "waiting" in r["metrics"]
    )

    def percentile(p):
        if not values:
            return None
        index = (len(values) - 1) * p
        low = int(index)
        high = min(low + 1, len(values) - 1)
        return round(values[low] + (values[high] - values[low]) * (index - low), 3)

    return {
        "reports": reports,
        "summary": {
            "sample_count": len(reports),
            "complete_count": sum(r["status"] == "complete" for r in reports),
            "failed_count": sum(r["status"] == "failed" for r in reports),
            "wait_sample_count": len(values),
            "wait_p50_ms": percentile(0.5),
            "wait_p95_ms": percentile(0.95),
            "wait_p99_ms": percentile(0.99),
        },
        "window_days": 30,
    }
