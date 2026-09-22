import asyncio
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fake_aioredis
from google.protobuf import duration_pb2, json_format, timestamp_pb2
from opuslib import Encoder

from backend.audio_contract.v2 import audio_pb2
from backend.audio_contract.v2.codec import (
    serialize_client_control_json,
    serialize_media_envelope,
)
from backend.controllers import audio_v2_controller
from backend.routers.modules.websocket_routes import audio_v2_endpoint
from backend.services.audio_stream.v2_streams import parse_stream_event
from backend.services.response_coordinator import StaleResponse

pytestmark = pytest.mark.unit


def _timestamp() -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromDatetime(datetime(2026, 9, 8, 18, 27, 29, tzinfo=timezone.utc))
    return value


def _control(**event) -> str:
    return serialize_client_control_json(
        audio_pb2.ClientControl(
            event_id=audio_pb2.EventId(value=f"event-{next(_EVENT_IDS)}"),
            sent_at=_timestamp(),
            **event,
        )
    )


def _binding() -> audio_pb2.CaptureBinding:
    return audio_pb2.CaptureBinding(
        capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
        voice_session_id=audio_pb2.VoiceSessionId(value="voice-1"),
        capture_epoch=1,
    )


def _parse_server_control(raw: str) -> audio_pb2.ServerControl:
    control = audio_pb2.ServerControl()
    json_format.Parse(raw, control)
    return control


_EVENT_IDS = itertools.count(1)


class PhoneWebSocket:
    def __init__(self, messages):
        self.scope = {"subprotocols": ["chronicle.audio.v2"]}
        self._messages = iter(messages)
        self.accepted_subprotocol = None
        self.sent_text = []
        self.closed = None
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._received_start = False

    async def accept(self, *, subprotocol):
        self.accepted_subprotocol = subprotocol

    async def receive_text(self):
        return _control(
            hello=audio_pb2.ClientHello(
                bearer_token="phone-token",
                source_id=audio_pb2.CaptureSourceId(value="a421c9-phone"),
                device_kind=audio_pb2.DEVICE_KIND_IOS_PHONE,
                display_name="phone",
            )
        )

    async def receive(self):
        if self._received_start:
            await self._started.wait()
        message = next(self._messages, None)
        if message is None:
            # A real client keeps the socket open until the Stop handshake.
            await self._stopped.wait()
            return {"type": "websocket.disconnect"}
        self._received_start = True
        return message

    async def send_text(self, value):
        self.sent_text.append(value)
        event = _parse_server_control(value).WhichOneof("event")
        if event == "capture_started":
            self._started.set()
        elif event == "capture_stopped":
            self._stopped.set()

    async def send_bytes(self, _value):
        raise AssertionError("the capture path must not send binary downlink")

    async def close(self, *, code, reason):
        self.closed = (code, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("dialogue", [None, "end", "rejected", "stale_ack"])
async def test_registered_audio_websocket_accepts_phone_frame_and_stops(
    monkeypatch, dialogue
):
    """The server-testable phone path must survive hello through durable ingress."""

    async def run_inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    # The registered entrypoint owns the orchestration under test; the executor is
    # only a process boundary here and otherwise leaves pytest's loop teardown
    # waiting on an idle default-executor thread.
    monkeypatch.setattr(audio_v2_controller.asyncio, "to_thread", run_inline)

    audio_spec = audio_pb2.AudioSpec(
        codec=audio_pb2.AUDIO_CODEC_OPUS,
        sample_rate_hz=16_000,
        channel_count=1,
        frame_duration=duration_pb2.Duration(nanos=20_000_000),
        bitrate_bps=24_000,
    )
    start = _control(
        start_capture=audio_pb2.StartCapture(
            capture_epoch=1,
            processing_profile=audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC,
            data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
            delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
            audio_spec=audio_spec,
            capabilities=audio_pb2.CaptureCapabilities(
                duplex_mode=audio_pb2.DUPLEX_MODE_FULL,
                input_route=audio_pb2.INPUT_ROUTE_BUILT_IN_MIC,
                output_route=audio_pb2.OUTPUT_ROUTE_SPEAKERPHONE,
                native_sample_rate_hz=48_000,
                acoustic_echo_cancellation=audio_pb2.EffectStatus(
                    requested=True, available=True, enabled=True
                ),
                noise_suppression=audio_pb2.EffectStatus(
                    requested=True, available=True, enabled=True
                ),
            ),
        )
    )
    opus_silence = Encoder(16_000, 1, "audio").encode(bytes(640), 320)
    media = serialize_media_envelope(
        audio_pb2.MediaEnvelope(
            capture=audio_pb2.CaptureMediaPacket(
                binding=_binding(),
                sequence=0,
                captured_at=_timestamp(),
                delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
                opus_payload=opus_silence,
            )
        )
    )
    stop = _control(
        stop_capture=audio_pb2.StopCapture(
            binding=_binding(), reason=audio_pb2.STOP_REASON_USER_REQUESTED
        )
    )
    extra = []
    if dialogue == "stale_ack":
        # An in-flight progress ACK can arrive after server-side barge-in has
        # superseded its response. It must not tear down microphone ingestion.
        extra.append(
            {
                "type": "websocket.receive",
                "text": _control(
                    playback_acknowledgement=audio_pb2.PlaybackAcknowledgement(
                        binding=_binding(),
                        response_id=audio_pb2.ResponseId(value="superseded"),
                        generation=1,
                        state=audio_pb2.PLAYBACK_STATE_PROGRESS,
                        rendered_samples=480,
                        monotonic_timestamp_us=100000,
                    )
                ),
            }
        )
        monkeypatch.setattr(
            audio_v2_controller.ResponseCoordinator,
            "playback",
            AsyncMock(side_effect=StaleResponse("response generation was superseded")),
        )
    elif dialogue:
        extra.append(
            {
                "type": "websocket.receive",
                "text": _control(
                    conversation_command=audio_pb2.ConversationCommand(
                        binding=_binding(), action=audio_pb2.CONVERSATION_ACTION_END
                    )
                ),
            }
        )
    websocket = PhoneWebSocket(
        [
            {"type": "websocket.receive", "text": start},
            *extra,
            {"type": "websocket.receive", "bytes": media},
            {"type": "websocket.receive", "text": stop},
        ]
    )

    redis_client = fake_aioredis.FakeRedis()
    state = SimpleNamespace(
        stream_session_id=None,
        voice_session_id=None,
        capture_epoch=0,
        data_purpose=None,
        socket_id=None,
    )
    producer = SimpleNamespace(
        redis_client=redis_client,
        update_session_job_ids=AsyncMock(),
    )

    async def initialize_capture_session(**kwargs):
        initialized_state = kwargs["client_state"]
        initialized_state.stream_session_id = "capture-1"
        initialized_state.voice_session_id = "voice-1"
        initialized_state.capture_epoch = 1
        initialized_state.data_purpose = "normal_capture"

    async def finalize_capture_session(**kwargs):
        kwargs["client_state"].stream_session_id = None

    monkeypatch.setattr(
        audio_v2_controller,
        "websocket_auth",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    id="69b80e5894aa9ec334a421c9",
                    user_id="69b80e5894aa9ec334a421c9",
                    email="phone@example.test",
                ),
                None,
            )
        ),
    )
    monkeypatch.setattr(
        audio_v2_controller, "create_client_state", AsyncMock(return_value=state)
    )
    monkeypatch.setattr(
        audio_v2_controller, "get_audio_stream_producer", lambda: producer
    )
    monkeypatch.setattr(
        audio_v2_controller, "initialize_capture_session", initialize_capture_session
    )
    monkeypatch.setattr(
        audio_v2_controller, "finalize_capture_session", finalize_capture_session
    )
    monkeypatch.setattr(
        audio_v2_controller,
        "start_streaming_jobs",
        lambda **_kwargs: {
            "speech_detection": "speech-job-1",
            "audio_persistence": "persistence-job-1",
        },
    )
    monkeypatch.setattr(
        audio_v2_controller, "cleanup_client_state", AsyncMock(return_value=True)
    )

    from backend.services.interaction_modes.voice import runtime

    voice_runtime = SimpleNamespace(
        control=AsyncMock(
            return_value=audio_pb2.ConversationState(
                binding=_binding(), phase=audio_pb2.CONVERSATION_PHASE_ENDED
            )
        ),
        end_for_capture=AsyncMock(),
    )
    if dialogue == "rejected":
        voice_runtime.control.side_effect = ValueError("stale dialogue action")
    monkeypatch.setattr(
        runtime, "VoiceConversationRuntime", lambda redis: voice_runtime
    )

    await asyncio.wait_for(audio_v2_endpoint(websocket), timeout=2)

    controls = [_parse_server_control(raw) for raw in websocket.sent_text]
    assert [control.WhichOneof("event") for control in controls] == [
        "hello",
        "capture_started",
        *(
            ["error" if dialogue in {"rejected", "stale_ack"} else "conversation_state"]
            if dialogue
            else []
        ),
        "capture_packet_accepted",
        "capture_stopped",
    ]
    assert controls[1].capture_started.binding == _binding()
    assert controls[-2].capture_packet_accepted.sequence == 0
    if dialogue and dialogue != "stale_ack":
        voice_runtime.control.assert_awaited_once()
        assert (
            voice_runtime.control.await_args.kwargs["user_id"]
            == "69b80e5894aa9ec334a421c9"
        )
        assert voice_runtime.control.await_args.kwargs["socket_id"] == state.socket_id
    voice_runtime.end_for_capture.assert_awaited_once()
    assert (
        voice_runtime.end_for_capture.await_args.kwargs["reason"] == "capture_stopped"
    )
    assert websocket.accepted_subprotocol == "chronicle.audio.v2"
    assert websocket.closed is None

    durable_entries = await redis_client.xrange("audio:v2:durable:capture-1")
    durable_events = [
        parse_stream_event(fields) for _entry_id, fields in durable_entries
    ]
    assert [event.WhichOneof("event") for event in durable_events] == [
        "opened",
        "frame",
        "ended",
    ]
    assert durable_events[1].frame.pcm_s16le == bytes(640)
