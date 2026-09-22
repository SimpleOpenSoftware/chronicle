import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fakeredis import aioredis
from fastapi import FastAPI
from test_committed_turn_routing import _turn_fields
from test_response_coordinator import _queued_response, _ready_voice

from backend.audio_contract.v2 import audio_pb2
from backend.auth import current_active_user
from backend.routers.modules.wakeword_routes import router
from backend.services import voice_latency
from backend.services.interaction_modes.committed_turns import (
    CommittedAudioTurn,
    CommittedTranscriptAssembler,
)
from backend.services.response_coordinator import ResponseCoordinator
from backend.services.voice_latency import (
    STREAM,
    TimingIdentity,
    VoiceTimingLedger,
    VoiceTrace,
    build_voice_report,
    recent_voice_reports,
    timing_document,
)
from backend.services.voice_sessions import VoiceSessionCoordinator
from backend.services.wakeword.interaction_event_consumer import (
    WakeInteractionEventConsumer,
)

pytestmark = pytest.mark.unit
IDENTITY = TimingIdentity("user-1", "client-1", "audio-1", 3, "turn-1")


def sample(stage, at, *, response_id="", clock=None, wall=100000, **kwargs):
    trace = VoiceTrace(None, IDENTITY, response_id=response_id)
    return timing_document(
        trace.event(
            stage,
            timestamp_ms=at,
            observed_at_ms=wall,
            clock_domain=clock or IDENTITY.device_clock,
            **kwargs
        )
    )


def complete_trace():
    return [
        sample("speech_started", 1000),
        sample("speech_ended", 4000),
        sample("response_started", 6000, response_id="reply", wall=500000),
        sample("response_done", 11000, response_id="reply", wall=900000),
    ]


def test_three_plus_two_plus_five_uses_device_clock_not_ack_receipt():
    events = complete_trace()
    report = build_voice_report([*reversed(events), events[-1]])
    assert report["status"] == "complete"
    assert {k: v["value_ms"] for k, v in report["metrics"].items()} == {
        "speaking": 3000,
        "waiting": 2000,
        "reply": 5000,
        "total": 10000,
    }
    assert len(report["events"]) == 4
    assert (
        sum(report["metrics"][k]["value_ms"] for k in ("speaking", "waiting", "reply"))
        == 10000
    )


def test_unrelated_clocks_and_missing_playback_are_not_reported_as_zero():
    events = complete_trace()
    events[2]["clock_domain"] = "another-phone"
    report = build_voice_report(events)
    assert report["status"] == "incomplete"
    assert "waiting" not in report["metrics"]
    assert "waiting" in report["missing"]
    report = build_voice_report(events[:2])
    assert "reply" not in report["metrics"]
    assert "total" not in report["metrics"]


def test_negative_latency_is_invalid_not_clamped():
    events = complete_trace()
    events[2]["timestamp_ms"] = 3000
    report = build_voice_report(events)
    assert report["invalid"] == ["waiting"]
    assert report["status"] == "incomplete"


def test_responses_do_not_overwrite_each_other_or_hide_cancellation():
    events = complete_trace() + [
        sample("response_cancelled", 12000, response_id="replacement")
    ]
    report = build_voice_report(events)
    assert len(report["responses"]) == 2
    assert report["status"] == "incomplete"
    assert "total" not in report["metrics"]


def test_nested_stt_spans_are_reported_separately_not_added_to_total():
    report = build_voice_report(
        complete_trace()
        + [
            sample("stt", 4000, clock="worker", duration_ms=800),
            sample("stt_wait", 4000, clock="worker", duration_ms=200),
            sample("stt_batch", 4200, clock="worker", duration_ms=600),
        ]
    )
    assert report["metrics"]["stt"]["value_ms"] == 800
    assert report["metrics"]["total"]["value_ms"] == 10000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "another-user"),
        ("client_id", "another-device"),
        ("audio_session_id", "another-session"),
        ("capture_epoch", 999),
        ("turn_revision", 999),
    ],
)
def test_cross_identity_merge_is_rejected(field, value):
    events = complete_trace()
    events[-1][field] = value
    with pytest.raises(ValueError, match="exactly one"):
        build_voice_report(events)


async def read_events(redis):
    events = []
    for _, fields in await redis.xrange(STREAM):
        if b"timing" in fields:
            event = audio_pb2.InteractionTimingEvent()
            event.ParseFromString(fields[b"timing"])
            events.append(event)
    return events


@pytest.mark.parametrize("streaming", [True, False])
async def test_stt_entry_point_records_wait_and_only_the_batch_calls_that_ran(
    streaming,
):
    redis = aioredis.FakeRedis(decode_responses=False)
    if streaming:
        await redis.xadd(
            "transcription:results:audio-1",
            {"words": json.dumps([{"word": "hello", "start": 0.2, "end": 1.1}])},
        )
    batch = AsyncMock(return_value="hello")
    assembler = CommittedTranscriptAssembler(
        redis, exact_transcriber=batch, watermark_wait_seconds=0
    )
    with VoiceTrace(redis, IDENTITY).bind():
        result = await assembler.resolve(CommittedAudioTurn.from_fields(_turn_fields()))
    events = await read_events(redis)
    assert result.text == "hello"
    assert {e.stage for e in events} == (
        {"stt", "stt_wait"} if streaming else {"stt", "stt_wait", "stt_batch"}
    )
    assert all(e.duration_ms >= 0 for e in events if e.HasField("duration_ms"))
    assert len([e for e in events if e.HasField("duration_ms")]) == (
        2 if streaming else 3
    )
    assert all(e.user_id == "user-1" and e.turn_id == "turn-1" for e in events)
    assert batch.await_count == (0 if streaming else 1)
    await redis.aclose()


async def test_failed_stt_keeps_timing_and_original_exception():
    redis = aioredis.FakeRedis(decode_responses=False)
    assembler = CommittedTranscriptAssembler(
        redis,
        watermark_wait_seconds=0,
        exact_transcriber=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )
    with VoiceTrace(redis, IDENTITY).bind(), pytest.raises(
        RuntimeError, match="provider unavailable"
    ):
        await assembler.resolve(CommittedAudioTurn.from_fields(_turn_fields()))
    events = await read_events(redis)
    assert {e.stage for e in events if e.outcome == "failed"} == {"stt", "stt_batch"}
    await redis.aclose()


async def test_real_response_coordinator_preserves_ack_clock_and_duplicate_ack():
    redis = aioredis.FakeRedis(decode_responses=False)
    voices = VoiceSessionCoordinator(redis)
    coordinator = ResponseCoordinator(redis, voices)
    voice = await _ready_voice(voices)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id, byte_length=12, duration_ms=5000, sample_rate=24000
    )
    await coordinator.offer(response.response_id, (b"opus-packet!",))
    args = dict(
        response_id=response.response_id,
        generation=response.generation,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
    )
    await coordinator.playback(**args, state="started", monotonic_timestamp_ms=6000)
    await coordinator.playback(**args, state="done", monotonic_timestamp_ms=11000)
    await coordinator.playback(**args, state="done", monotonic_timestamp_ms=11000)
    events = await read_events(redis)
    playback = [e for e in events if e.stage in {"response_started", "response_done"}]
    assert [e.timestamp_ms for e in playback] == [6000, 11000]
    assert all(e.clock_domain == IDENTITY.device_clock for e in playback)
    report = build_voice_report(
        complete_trace()[:2] + [timing_document(e) for e in events]
    )
    assert report["metrics"]["total"]["value_ms"] == 10000
    await redis.aclose()


async def test_registered_consumer_persists_typed_timing_and_acks_after_write():
    redis = aioredis.FakeRedis(decode_responses=False)
    collection = SimpleNamespace(update_one=AsyncMock())
    ledger = VoiceTimingLedger(collection)
    consumer = WakeInteractionEventConsumer(redis, SimpleNamespace(), ledger)
    await VoiceTrace(redis, IDENTITY).emit("turn_received")

    async def persisted(*args, **kwargs):
        await consumer.stop()

    collection.update_one.side_effect = persisted
    await consumer.run()
    pending = await redis.xpending(STREAM, "wake-interaction-ledger")
    assert pending["pending"] == 0
    document = collection.update_one.call_args.args[1]["$setOnInsert"]
    assert document["turn_id"] == "turn-1"
    assert "transcript" not in document
    await redis.aclose()


async def test_report_query_always_scopes_both_lookup_steps_to_authenticated_user():
    collection = MagicMock()
    collection.aggregate.return_value.to_list = AsyncMock(
        return_value=[
            {
                "_id": {
                    "audio_session_id": "audio-1",
                    "turn_id": "turn-1",
                    "turn_revision": 0,
                }
            }
        ]
    )
    collection.find.return_value.sort.return_value.limit.return_value.to_list = (
        AsyncMock(return_value=complete_trace())
    )
    result = await recent_voice_reports(
        {"voice_interaction_events": collection},
        user_id="user-1",
        client_id="someone-elses-device",
        limit=20,
    )
    match = collection.aggregate.call_args.args[0][0]["$match"]
    assert match["user_id"] == "user-1"
    assert match["client_id"] == "someone-elses-device"
    assert collection.find.call_args.args[0]["user_id"] == "user-1"
    assert result["summary"]["wait_p95_ms"] == 2000


async def test_http_report_entry_point_uses_authenticated_owner_and_validates_limit(
    monkeypatch,
):
    query = AsyncMock(return_value={"reports": [], "summary": {"sample_count": 0}})
    monkeypatch.setattr(voice_latency, "recent_voice_reports", query)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[current_active_user] = lambda: SimpleNamespace(
        id="owner-id"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/wakeword/latency?client_id=device&limit=5")
        assert response.status_code == 200
        assert query.call_args.kwargs == {
            "user_id": "owner-id",
            "client_id": "device",
            "limit": 5,
        }
        assert (await client.get("/api/wakeword/latency?limit=0")).status_code == 422
        assert (await client.get("/api/wakeword/latency?limit=101")).status_code == 422
