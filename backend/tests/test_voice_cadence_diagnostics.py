"""Diagnostic timings exercise the real response worker and downlink entrypoints."""

import asyncio
import io
import json
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.protobuf import json_format
from test_voice_conversation_runtime import command, effects, setup, turn

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.audio_contract.v2.codec import parse_media_envelope
from backend.controllers.audio_v2_controller import _subscribe_v2_downlink
from backend.services import client_diagnostics, playback_audio
from backend.services.interaction_modes.voice.engine import ModularVoiceEngine
from backend.services.response_coordinator import ResponseCoordinator
from backend.services.voice_diagnostics import MAX_TIMELINE_EVENTS, VoiceCadenceRecorder
from backend.services.voice_sessions import VoiceSessionCoordinator

pytestmark = pytest.mark.unit


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(bytes(4800))
    return output.getvalue()


@pytest.mark.parametrize("storage_fails", [False, True])
async def test_registered_response_and_downlink_record_delayed_boundaries_without_affecting_audio(
    setup, monkeypatch, storage_fails
):
    redis, runtime, voice, _ = setup

    async def llm(**kwargs):
        await asyncio.sleep(0.015)
        yield {"type": "content", "text": "First. Second."}
        yield {"type": "done", "finish_reason": "stop"}

    async def synthesize(text):
        await asyncio.sleep(0.025)
        return wav()

    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=synthesize)
    original_normalize = playback_audio.normalize_wav_for_playback

    def normalize(data):
        time.sleep(0.003)
        return original_normalize(data)

    monkeypatch.setattr(playback_audio, "normalize_wav_for_playback", normalize)
    original_thread = asyncio.to_thread

    async def encoding_thread(function, *args, **kwargs):
        if getattr(function, "__qualname__", "").startswith(
            "StreamingPlaybackEncoder."
        ):
            await asyncio.sleep(0.005)
        return await original_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", encoding_thread)
    original_update = ResponseCoordinator._stream_update

    async def delayed_update(self, *args, **kwargs):
        await asyncio.sleep(0.008)
        return await original_update(self, *args, **kwargs)

    monkeypatch.setattr(ResponseCoordinator, "_stream_update", delayed_update)
    original_binding = VoiceSessionCoordinator.binding_matches

    async def delayed_binding(self, **kwargs):
        await asyncio.sleep(0.004)
        return await original_binding(self, **kwargs)

    monkeypatch.setattr(VoiceSessionCoordinator, "binding_matches", delayed_binding)
    storage = (
        AsyncMock(side_effect=OSError("disk unavailable")) if storage_fails else None
    )
    if storage:
        monkeypatch.setattr(client_diagnostics, "store_client_diagnostic", storage)

    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0][2]
    ack_base = dict(
        user_id="user",
        client_id="client",
        audio_session_id="audio",
        voice_session_id=voice.voice_session_id,
        capture_epoch=1,
        socket_id="socket",
        generation=effect.generation,
    )
    offered = None
    count = 0

    async def send_text(raw):
        nonlocal offered
        control = pb.ServerControl()
        json_format.Parse(raw, control)
        if control.HasField("playback_offer"):
            offered = control.playback_offer
            await runtime.responses.playback(
                **ack_base,
                response_id=offered.response_id.value,
                state="started",
                monotonic_timestamp_ms=1
            )
        elif control.HasField("playback_finished"):
            total = control.playback_finished.total_samples
            await runtime.responses.playback(
                **ack_base,
                response_id=offered.response_id.value,
                state="done",
                monotonic_timestamp_ms=count + 2,
                rendered_samples=total
            )

    async def send_bytes(raw):
        nonlocal count
        await asyncio.sleep(0.012)
        packet = parse_media_envelope(raw).playback
        count += 1
        # Exclude encoder pre-skip and hold the tail, as the real renderer does.
        rendered = max(0, (count - 1) * 480 - offered.pre_skip_samples)
        await runtime.responses.playback(
            **ack_base,
            response_id=packet.response_id.value,
            state="progress",
            monotonic_timestamp_ms=count + 1,
            rendered_samples=rendered
        )

    downlink = asyncio.create_task(
        _subscribe_v2_downlink(
            websocket=SimpleNamespace(send_text=send_text, send_bytes=send_bytes),
            redis_client=redis,
            voice_sessions=runtime.voices,
            responses=runtime.responses,
            client_state=SimpleNamespace(socket_id="socket"),
            user_id="user",
            client_id="client",
        )
    )
    await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(runtime.execute_effect(effect), timeout=4)
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
    record = await runtime.responses.get(offered.response_id.value)
    assert (
        record.state == "done"
        and record.rendered_samples == record.total_samples == 4800
    )
    if storage_fails:
        assert storage.await_count == 2
        return
    receipts = await client_diagnostics.list_client_diagnostics("user")
    reports = {
        x["platform"]: json.loads(
            await client_diagnostics.read_client_diagnostic("user", x["upload_id"])
        )
        for x in receipts
    }
    assert set(reports) == {"worker-voice", "backend-voice"}
    worker, backend = reports["worker-voice"], reports["backend-voice"]
    for report in reports.values():
        assert report["identity"]["response_id"] == record.response_id
        assert report["identity"]["capture_session_id"] == "audio"
        assert report["identity"]["generation"] == effect.generation
        assert report["clock_domain"].startswith("process:")
        assert report["audio_coordinates"]["sample_rate_hz"] == 24000
        assert report["audio_coordinates"]["opus_pre_skip_samples"] == 156
        assert report["ended_monotonic_ms"] >= report["started_monotonic_ms"]
        assert all(x["end_ms"] >= x["start_ms"] for x in report["timeline"])
        assert "First." not in json.dumps(
            report
        ) and "Hello Chronicle" not in json.dumps(report)
    for stage, minimum in [
        ("tts", 20),
        ("normalize", 2),
        ("encoding", 4),
        ("publish_cas", 7),
        ("publish_credit_check", 3),
        ("producer_wait", 0),
    ]:
        assert worker["aggregates"][stage]["max_ms"] >= minimum
    assert worker["outcome"] == "completed"
    assert worker["aggregates"]["publish_transaction"]["count"] == count
    assert worker["aggregates"]["tts"]["count"] == 2
    assert backend["aggregates"]["downlink_validation"]["max_ms"] >= 3
    assert backend["aggregates"]["socket_send"]["max_ms"] >= 11
    assert backend["aggregates"]["socket_send"]["count"] == count
    assert worker["aggregates"]["publish_cas"]["count"] == count


async def test_recorder_is_bounded_and_storage_errors_do_not_escape(monkeypatch):
    diagnostic = VoiceCadenceRecorder(
        user_id="user", client_id="client", capture_session_id="capture"
    )
    for index in range(10000):
        diagnostic.observe(
            "encoding",
            index * 100,
            index * 100 + 80,
            sequence=index,
            text="must never be retained",
            pcm=b"private",
            provider_url="secret",
        )
    result = diagnostic.snapshot("done")
    assert len(result["timeline"]) == MAX_TIMELINE_EVENTS
    assert result["timeline_evictions"] == 10000 - MAX_TIMELINE_EVENTS
    assert result["aggregates"]["encoding"]["count"] == 10000
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
    store = AsyncMock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(client_diagnostics, "store_client_diagnostic", store)
    await diagnostic.flush("done")
    await diagnostic.flush("done")
    store.assert_awaited_once()
