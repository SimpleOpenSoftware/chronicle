import asyncio
import io
import json
import wave
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis

from backend.audio_contract.v2 import audio_pb2
from backend.models.audio_capabilities import VoiceCapabilities
from backend.plugins.services import PluginServices
from backend.redis_keys import ClientId, SessionId, device_downlink_channel
from backend.services import response_delivery
from backend.services.audio_stream.session_store import SessionStore
from backend.services.response_coordinator import ResponseCoordinator, StaleResponse
from backend.services.response_delivery import deliver_text_response
from backend.services.voice_sessions import (
    ClientUpgradeRequired,
    VoiceSessionCoordinator,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def redis_client():
    return fake_aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def voice_coordinator(redis_client):
    return VoiceSessionCoordinator(redis_client)


@pytest.fixture
def coordinator(redis_client, voice_coordinator):
    return ResponseCoordinator(redis_client, voice_coordinator)


async def _ready_voice(voice_coordinator: VoiceSessionCoordinator):
    started = await voice_coordinator.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        advertised_protocol=2,
    )
    capabilities = VoiceCapabilities(
        mode="duplex_full",
        input_route="built_in_mic",
        output_route="speakerphone",
        native_sample_rate=48000,
        aec={"requested": True, "available": True, "enabled": True},
        noise_suppression={
            "requested": True,
            "available": True,
            "enabled": True,
        },
        fallback_reason=None,
    )
    return await voice_coordinator.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        capabilities=capabilities,
    )


async def _queued_response(coordinator, voice):
    generation = await coordinator.begin_turn("user-1", "client-1")
    return await coordinator.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        turn_id="turn-1",
        turn_revision=0,
        generation=generation,
        kind="speech",
        barge_in_allowed=True,
        trace_id="trace-1",
        causation_id="turn-1",
    )


async def _interaction_events(redis_client):
    rows = await redis_client.xrange("wakeword:interaction-events")
    return [json.loads(fields[b"event"]) for _, fields in rows if b"event" in fields]


async def test_wake_response_lifecycle_is_durably_emitted_in_the_state_transaction(
    coordinator, voice_coordinator, redis_client
):
    voice = await _ready_voice(voice_coordinator)
    generation = await coordinator.begin_turn("user-1", "client-1")
    trace_id = "7ce4d46b-232f-47f9-8148-d595ed344cf2"
    response = await coordinator.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        turn_id="turn-1",
        turn_revision=0,
        generation=generation,
        kind="speech",
        barge_in_allowed=True,
        trace_id="response-trace",
        causation_id=trace_id,
        wake_trace_id=trace_id,
    )
    await coordinator.mark_ready(
        response.response_id, byte_length=12, duration_ms=250, sample_rate=24000
    )
    await coordinator.offer(response.response_id, (b"opus-packet!",))
    await coordinator.playback(
        response_id=response.response_id,
        generation=response.generation,
        state="started",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=10,
    )
    await coordinator.playback(
        response_id=response.response_id,
        generation=response.generation,
        state="done",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=260,
    )

    events = await _interaction_events(redis_client)
    assert [event["stage"] for event in events] == [
        "response_queued",
        "response_ready",
        "response_offered",
        "response_playing",
        "response_done",
    ]
    assert {event["wake_trace_id"] for event in events} == {trace_id}
    assert {event["response_id"] for event in events} == {response.response_id}


async def test_generations_are_client_wide_and_strictly_monotonic(coordinator):
    observed = [await coordinator.begin_turn("user-1", "client-1") for _ in range(20)]

    assert observed == list(range(1, 21))


async def test_new_turn_terminally_cancels_the_current_response(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)

    next_generation = await coordinator.begin_turn("user-1", "client-1")

    old = await coordinator.get(response.response_id)
    assert next_generation == response.generation + 1
    assert old.state == "cancelled"
    assert old.terminal_reason == "new_turn"


async def test_synthesis_result_is_dropped_when_generation_changes_during_await(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def synthesize():
        entered.set()
        await release.wait()
        return b"RIFFstale"

    task = asyncio.create_task(coordinator.synthesize(response.response_id, synthesize))
    await entered.wait()
    await coordinator.begin_turn("user-1", "client-1")
    release.set()

    with pytest.raises(StaleResponse):
        await task
    assert (await coordinator.get(response.response_id)).state == "cancelled"


async def test_offer_publishes_typed_offer_then_raw_opus_packets(
    coordinator, voice_coordinator, redis_client
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24000,
    )
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    offered = await coordinator.offer(response.response_id, (b"opus-packet!",))
    offer_message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    media_message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    offer = audio_pb2.DeviceDownlinkEvent.FromString(offer_message["data"])
    media = audio_pb2.DeviceDownlinkEvent.FromString(media_message["data"])

    assert offered.state == "offered"
    assert offer.WhichOneof("event") == "playback_offer"
    assert offer.playback_offer.audio_spec.sample_rate_hz == 24_000
    assert media.WhichOneof("event") == "playback"
    assert media.playback.opus_payload == b"opus-packet!"
    assert media.playback.final_packet


async def test_stale_socket_cannot_offer_or_ack_playback(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24000,
    )
    await voice_coordinator.disconnect(
        voice_session_id=voice.voice_session_id, socket_id="socket-1"
    )

    with pytest.raises(StaleResponse):
        await coordinator.offer(response.response_id, (b"opus-packet!",))
    with pytest.raises(StaleResponse):
        await coordinator.playback(
            response_id=response.response_id,
            generation=response.generation,
            state="started",
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            voice_session_id=voice.voice_session_id,
            capture_epoch=3,
            socket_id="socket-1",
            monotonic_timestamp_ms=10,
        )


async def test_only_one_response_can_reach_playing(coordinator, voice_coordinator):
    voice = await _ready_voice(voice_coordinator)
    first = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        first.response_id, byte_length=12, duration_ms=250, sample_rate=24000
    )
    await coordinator.offer(first.response_id, (b"opus-packet!",))
    playing = await coordinator.playback(
        response_id=first.response_id,
        generation=first.generation,
        state="started",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=10,
    )
    assert playing.state == "playing"

    await coordinator.begin_turn("user-1", "client-1")
    second = await coordinator.queue(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        turn_id="turn-2",
        turn_revision=0,
        generation=first.generation + 1,
        kind="speech",
        barge_in_allowed=True,
        trace_id="trace-2",
        causation_id="turn-2",
    )

    with pytest.raises(StaleResponse):
        await coordinator.playback(
            response_id=first.response_id,
            generation=first.generation,
            state="done",
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            voice_session_id=voice.voice_session_id,
            capture_epoch=3,
            socket_id="socket-1",
            monotonic_timestamp_ms=20,
        )
    assert second.state == "queued"


async def test_physical_cancellation_ack_is_idempotent_after_generation_fence(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id, byte_length=12, duration_ms=250, sample_rate=24000
    )
    await coordinator.offer(response.response_id, (b"opus-packet!",))
    await coordinator.playback(
        response_id=response.response_id,
        generation=response.generation,
        state="started",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=10,
    )

    await coordinator.begin_turn("user-1", "client-1", reason="barge_in")
    acknowledged = await coordinator.playback(
        response_id=response.response_id,
        generation=response.generation,
        state="cancelled",
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
        monotonic_timestamp_ms=20,
    )

    assert acknowledged.state == "cancelled"
    assert acknowledged.terminal_reason == "barge_in"
    assert acknowledged.playback_monotonic_ms == 20


async def test_plugin_stop_playback_uses_generation_fenced_v2_cancellation(
    coordinator, voice_coordinator, redis_client
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id, byte_length=12, duration_ms=250, sample_rate=24_000
    )
    await coordinator.offer(response.response_id, (b"opus-packet!",))
    services = PluginServices.__new__(PluginServices)
    services._async_redis = redis_client

    assert await services.stop_playback("user-1", "client-1")

    cancelled = await coordinator.get(response.response_id)
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.terminal_reason == "barge_in"


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00" * 1_600)
    return buffer.getvalue()


async def test_capture_only_client_must_upgrade_before_interactive_delivery(
    redis_client, monkeypatch
):
    await SessionStore(redis_client).init_session(
        "audio-wearable",
        user_id="user-1",
        client_id="wearable-1",
        connection_id="socket-wearable",
        stream_name="audio:stream:audio-wearable",
        capture_epoch=0,
        processing_profile="ambient",
        effects={
            "aec": {"reporting": "unreported"},
            "noise_suppression": {"reporting": "unreported"},
        },
        voice_session_id=None,
    )
    synthesize = AsyncMock(return_value=_wav())
    monkeypatch.setattr(response_delivery, "synthesize_speech", synthesize)
    with pytest.raises(ClientUpgradeRequired):
        await deliver_text_response(
            redis_client,
            ClientId.from_value("wearable-1"),
            SessionId.from_value("audio-wearable"),
            "hello",
        )
    synthesize.assert_not_awaited()


@pytest.mark.parametrize("kind", ["speech", "tone"])
async def test_delivery_publishes_audio_v2_and_only_speech_enters_timing_report(
    coordinator, voice_coordinator, redis_client, monkeypatch, kind
):
    voice = await _ready_voice(voice_coordinator)
    await SessionStore(redis_client).init_session(
        "audio-1",
        user_id="user-1",
        client_id="client-1",
        connection_id="socket-1",
        stream_name="audio:stream:audio-1",
        capture_epoch=3,
        processing_profile="duplex_aec",
        effects={
            "aec": {"requested": True, "available": True, "enabled": True},
            "noise_suppression": {
                "requested": True,
                "available": True,
                "enabled": True,
            },
        },
        voice_session_id=voice.voice_session_id,
    )
    monkeypatch.setattr(
        response_delivery, "synthesize_speech", AsyncMock(return_value=_wav())
    )
    generation = await coordinator.begin_turn("user-1", "client-1")
    channel = str(device_downlink_channel(ClientId.from_value("client-1")))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    if kind == "speech":
        offered = await deliver_text_response(
            redis_client,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            "hello",
            generation=generation,
            turn_id="turn-1",
        )
    else:
        offered = await response_delivery.deliver_wav_response(
            redis_client,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            AsyncMock(return_value=_wav()),
            kind="tone",
            generation=generation,
            turn_id="turn-1",
        )
    timings = [
        audio_pb2.InteractionTimingEvent.FromString(fields[b"timing"])
        for _, fields in await redis_client.xrange("wakeword:interaction-events")
        if b"timing" in fields
    ]
    assert bool(timings) == (kind == "speech")
    if kind == "speech":
        assert {e.stage for e in timings if e.HasField("duration_ms")} == {
            "tts",
            "encoding",
            "downlink",
        }
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    downlink = audio_pb2.DeviceDownlinkEvent.FromString(message["data"])

    assert offered.state == "offered"
    assert offered.transport == "audio_v2"
    assert downlink.WhichOneof("event") == "playback_offer"


async def test_missing_native_playback_ack_fails_current_response_and_health_reports_it(
    coordinator, voice_coordinator
):
    voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    await coordinator.mark_ready(
        response.response_id,
        byte_length=12,
        duration_ms=250,
        sample_rate=24_000,
    )
    offered = await coordinator.offer(response.response_id, (b"opus-packet!",))

    failed = await coordinator.expire_stalled(
        response.response_id,
        now=offered.updated_at + 6,
    )
    health = await coordinator.health("user-1", "client-1")

    assert failed.state == "failed"
    assert failed.terminal_reason == "playback_start_ack_timeout"
    assert health == {
        "generation": response.generation,
        "current_response_id": None,
        "current_state": None,
        "current_updated_at": None,
    }
