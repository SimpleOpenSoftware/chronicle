"""Cross-boundary failure regressions from the conversational voice audit."""

import pytest
from test_response_coordinator import (
    _queued_response,
    _ready_voice,
    coordinator,
    redis_client,
    voice_coordinator,
)

from backend.audio_contract.v2 import audio_pb2
from backend.models.audio_capabilities import VoiceCapabilities
from backend.redis_keys import ClientId, device_downlink_channel

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("action", ["barge_in", "producer_failure"])
async def test_cancel_reaches_socket_even_if_caller_loses_commit_reply(
    redis_client, voice_coordinator, coordinator, monkeypatch, action
):
    if action == "producer_failure":
        started = await voice_coordinator.start(
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            capture_epoch=3,
            socket_id="socket-1",
            advertised_protocol=2,
        )
        voice = await voice_coordinator.ready(
            voice_session_id=started.session.voice_session_id,
            user_id="user-1",
            client_id="client-1",
            audio_session_id="audio-1",
            capture_epoch=3,
            socket_id="socket-1",
            capabilities=VoiceCapabilities(
                mode="duplex_isolated",
                input_route="built_in_mic",
                output_route="headphones",
                native_sample_rate=48000,
                incremental_playback=True,
                fallback_reason=None,
                aec={"requested": False, "available": False, "enabled": False},
                noise_suppression={
                    "requested": False,
                    "available": False,
                    "enabled": False,
                },
            ),
        )
    else:
        voice = await _ready_voice(voice_coordinator)
    response = await _queued_response(coordinator, voice)
    if action == "producer_failure":
        await coordinator.open_stream(response.response_id)
    subscriber = redis_client.pubsub()
    await subscriber.subscribe(
        str(device_downlink_channel(ClientId.from_value("client-1")))
    )
    await subscriber.get_message(timeout=1)
    pipeline = redis_client.pipeline

    def interrupted_pipeline(*args, **kwargs):
        pipe = pipeline(*args, **kwargs)
        execute = pipe.execute

        async def commit_then_lose_reply(*args, **kwargs):
            await execute(*args, **kwargs)
            raise ConnectionError("caller lost connection after Redis committed")

        pipe.execute = commit_then_lose_reply
        return pipe

    monkeypatch.setattr(redis_client, "pipeline", interrupted_pipeline)
    with pytest.raises(ConnectionError, match="after Redis committed"):
        if action == "barge_in":
            await coordinator.begin_turn("user-1", "client-1", reason="barge_in")
        else:
            await coordinator.fail(response.response_id, "producer_stall_timeout")
    assert (await coordinator.get(response.response_id)).state == (
        "cancelled" if action == "barge_in" else "failed"
    )
    message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=0.1)
    assert (
        message is not None
    ), "committed cancellation must include the browser stop event"
    event = audio_pb2.DeviceDownlinkEvent.FromString(message["data"])
    assert event.WhichOneof("event") == "cancel_playback"
    assert event.cancel_playback.response_id.value == response.response_id
    assert event.cancel_playback.generation == response.generation + (
        1 if action == "barge_in" else 0
    )
    await subscriber.aclose()
