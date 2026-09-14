import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from active_turn_consumer import (
    COMMITTED_TURNS_STREAM,
    TURN_EVENTS_STREAM,
    ActiveTurnConsumer,
)
from audio_contract.v2 import audio_pb2


class FakeRedis:
    def __init__(self):
        self.added = []

    async def xadd(self, stream, fields, **kwargs):
        self.added.append((stream, fields, kwargs))
        return b"1-0"


class FakeModels:
    async def evaluate(self, pcm):
        speech = pcm.startswith(b"speech")
        return speech, not speech


class FakeClock:
    def __init__(self):
        self.now_ms = 10_000.0

    def __call__(self):
        return self.now_ms


def _frame(
    sequence: int,
    pcm: bytes,
    *,
    base_offset_ms: int = 0,
) -> audio_pb2.CanonicalPcmFrame:
    pcm = pcm + (b"\x00" * max(0, 1280 - len(pcm)))
    return audio_pb2.CanonicalPcmFrame(
        binding=audio_pb2.CaptureBinding(
            capture_session_id=audio_pb2.CaptureSessionId(value="audio-1"),
            voice_session_id=audio_pb2.VoiceSessionId(value="voice-1"),
            capture_epoch=2,
        ),
        sequence=sequence,
        monotonic_offset_us=(base_offset_ms + sequence * 40) * 1_000,
        device_monotonic_timestamp_us=900_000_000
        + (base_offset_ms + sequence * 40) * 1_000,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        pcm_s16le=pcm,
        data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
    )


@pytest.mark.asyncio
async def test_active_consumer_publishes_only_committed_turns():
    redis = FakeRedis()
    clock = FakeClock()
    consumer = ActiveTurnConsumer(
        redis_client=redis,
        model_factory=FakeModels,
        monotonic_ms=clock,
    )
    for sequence, pcm in enumerate(
        [b"speech-1", b"speech-2", b"silence", b"silence", b"silence"]
    ):
        await consumer.handle_frame(
            _frame(sequence, pcm), data_purpose="normal_capture"
        )

    assert all(stream != COMMITTED_TURNS_STREAM for stream, _, _ in redis.added)
    clock.now_ms += 2_000
    await consumer.flush_due()

    committed = [
        fields for stream, fields, _ in redis.added if stream == COMMITTED_TURNS_STREAM
    ]
    assert len(committed) == 1
    assert committed[0]["voice_session_id"] == "voice-1"
    assert committed[0]["audio_session_id"] == "audio-1"
    assert committed[0]["turn_revision"] == "0"
    assert committed[0]["pcm"].startswith(b"speech")
    assert float(committed[0]["speech_started_device_ms"]) == 900_000
    assert float(committed[0]["speech_ended_device_ms"]) == 900_080


@pytest.mark.asyncio
async def test_phone_coordinates_survive_into_a_wake_matchable_committed_turn():
    redis = FakeRedis()
    clock = FakeClock()
    consumer = ActiveTurnConsumer(
        redis_client=redis,
        model_factory=FakeModels,
        monotonic_ms=clock,
    )
    for sequence in range(48):
        await consumer.handle_frame(
            _frame(
                sequence,
                b"speech" if sequence < 45 else b"silence",
                base_offset_ms=23_000,
            ),
            data_purpose="normal_capture",
        )

    clock.now_ms += 2_000
    await consumer.flush_due()

    committed = [
        fields for stream, fields, _ in redis.added if stream == COMMITTED_TURNS_STREAM
    ]
    assert len(committed) == 1
    assert float(committed[0]["started_at_ms"]) == 23_000
    assert float(committed[0]["ended_at_ms"]) == 24_920
    assert float(committed[0]["started_at_ms"]) <= 23_048.65
    assert float(committed[0]["ended_at_ms"]) >= 24_748.65


@pytest.mark.asyncio
async def test_consumer_health_records_fresh_success_and_errors():
    redis = FakeRedis()
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)

    await consumer.handle_frame(_frame(0, b"speech"), data_purpose="normal_capture")
    with pytest.raises(ValueError):
        await consumer.handle_frame(
            audio_pb2.CanonicalPcmFrame(), data_purpose="normal_capture"
        )

    health = consumer.health()
    assert health["frames_consumed"] == 1
    assert health["error_count"] == 1
    assert health["last_success_at"] is not None


@pytest.mark.asyncio
async def test_active_consumer_reuses_one_model_bundle_per_voice_session():
    redis = FakeRedis()
    created = []

    def model_factory():
        model = FakeModels()
        created.append(model)
        return model

    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=model_factory)

    await consumer.handle_frame(_frame(0, b"speech-1"), data_purpose="normal_capture")
    await consumer.handle_frame(_frame(1, b"speech-2"), data_purpose="normal_capture")

    assert len(created) == 1


@pytest.mark.asyncio
async def test_annotation_probe_exercises_models_without_publishing_turn_events():
    redis = FakeRedis()
    created = []

    def model_factory():
        model = FakeModels()
        created.append(model)
        return model

    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=model_factory)

    await consumer.handle_frame(_frame(0, b"speech-1"), data_purpose="annotation")
    await consumer.handle_frame(_frame(1, b"speech-2"), data_purpose="annotation")

    assert len(created) == 1
    assert all(
        stream not in {TURN_EVENTS_STREAM, COMMITTED_TURNS_STREAM}
        for stream, _, _ in redis.added
    )


@pytest.mark.asyncio
async def test_consumer_recovers_own_and_stranded_peer_frames_before_acknowledging():
    own_event = audio_pb2.CaptureStreamEvent(frame=_frame(10, b"speech-own"))
    peer_event = audio_pb2.CaptureStreamEvent(frame=_frame(11, b"speech-peer"))
    own = (b"10-0", {b"event": own_event.SerializeToString()})
    peer = (b"11-0", {b"event": peer_event.SerializeToString()})
    redis = SimpleNamespace(
        xreadgroup=AsyncMock(side_effect=[[(b"audio:v2:realtime:audio-1", [own])], []]),
        xautoclaim=AsyncMock(return_value=(b"0-0", [peer], [])),
        xack=AsyncMock(),
    )
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)
    consumer.handle_frame = AsyncMock()

    recovered = await consumer.recover_pending(
        "audio:v2:realtime:audio-1",
        claim_min_idle_ms=0,
    )

    assert recovered == 2
    assert consumer.handle_frame.await_count == 2
    assert redis.xack.await_count == 2
    redis.xautoclaim.assert_awaited_once_with(
        "audio:v2:realtime:audio-1",
        "interactive-turn-v2",
        consumer.consumer_name,
        0,
        start_id="0-0",
        count=20,
    )


@pytest.mark.asyncio
async def test_failed_active_frame_remains_pending_for_retry():
    event = audio_pb2.CaptureStreamEvent(frame=_frame(12, b"speech"))
    entry = (b"12-0", {b"event": event.SerializeToString()})
    redis = SimpleNamespace(
        xreadgroup=AsyncMock(return_value=[(b"audio:v2:realtime:audio-1", [entry])]),
        xautoclaim=AsyncMock(),
        xack=AsyncMock(),
    )
    consumer = ActiveTurnConsumer(redis_client=redis, model_factory=FakeModels)
    consumer.handle_frame = AsyncMock(side_effect=RuntimeError("model reset"))

    recovered = await consumer.recover_pending("audio:v2:realtime:audio-1")

    assert recovered == 0
    redis.xack.assert_not_awaited()
    redis.xautoclaim.assert_not_awaited()
