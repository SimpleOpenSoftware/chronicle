"""Duplex control progress must survive a stalled canonical media publisher."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from google.protobuf import duration_pb2
from opuslib import Encoder
from starlette.websockets import WebSocketDisconnect
from test_audio_v2_websocket_entrypoint import (
    PhoneWebSocket,
    _binding,
    _control,
    _parse_server_control,
    _timestamp,
)

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.audio_contract.v2.codec import (
    AudioProtocolV2Error,
    serialize_media_envelope,
)
from backend.controllers import audio_v2_controller as controller
from backend.routers.modules.websocket_routes import audio_v2_endpoint
from backend.services.audio_stream.v2_streams import parse_stream_event
from backend.services.interaction_modes.voice import runtime
from backend.services.response_coordinator import StaleResponse

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "priority",
        "decode_failure",
        "disconnect",
        "disconnect_downlink_failure",
        "overflow_count",
        "overflow_bytes",
        "overflow_disconnect",
        "overflow_oserror",
        "ack_error_disconnect",
        "stale_ack",
        "foreign_user_id",
        "foreign_client_id",
        "foreign_socket_id",
        "foreign_audio_session_id",
        "foreign_voice_session_id",
        "foreign_capture_epoch",
        "foreign_generation",
    ],
)
async def test_playback_ack_bypasses_stalled_capture_but_stop_drains_in_order(
    monkeypatch,
    case,
):
    """Real registered route, codec and canonical writes; external services faked."""
    redis = FakeRedis()
    state = SimpleNamespace(
        stream_session_id=None,
        voice_session_id=None,
        capture_epoch=0,
        data_purpose=None,
        socket_id=None,
    )
    producer = SimpleNamespace(redis_client=redis, update_session_job_ids=AsyncMock())
    entered, release, acknowledged, finalized = (asyncio.Event() for _ in range(4))
    order = []
    finalization_details = []
    ack_calls = []
    continue_reading = asyncio.Event()
    decoded = 0
    downlink_closed, transcripts_closed = asyncio.Event(), asyncio.Event()
    original_playback = controller.ResponseCoordinator.playback
    original_decode = controller._decode_opus_frames

    async def delayed_decode(normalizer, payload):
        nonlocal decoded
        decoded += 1
        if case == "decode_failure" and decoded == 2:
            raise AudioProtocolV2Error("invalid raw Opus packet")
        if not entered.is_set():
            entered.set()
            # Bounded dependency delay exceeding the production progress deadline.
            # Its cause is intentionally unspecified: this tests the HOL mechanism.
            await asyncio.wait_for(release.wait(), timeout=6)
        return await original_decode(normalizer, payload)

    async def playback(**kwargs):
        ack_calls.append(kwargs)
        order.append(("playback", kwargs["rendered_samples"]))
        acknowledged.set()
        if case == "ack_error_disconnect":
            raise AudioProtocolV2Error("invalid playback control")
        if case.startswith("foreign_"):
            # Exercise the production coordinator's owner/binding fence while
            # the socket reader bypasses queued media. Only its data lookup is fake.
            record = {
                key: kwargs[key]
                for key in (
                    "user_id",
                    "client_id",
                    "socket_id",
                    "audio_session_id",
                    "voice_session_id",
                    "capture_epoch",
                    "generation",
                )
            }
            key = case.removeprefix("foreign_")
            record[key] = (
                99 if key in {"capture_epoch", "generation"} else "another-owner"
            )
            monkeypatch.setattr(
                controller.ResponseCoordinator,
                "get",
                AsyncMock(return_value=SimpleNamespace(**record)),
            )
            await original_playback(
                controller.ResponseCoordinator(
                    redis, controller.VoiceSessionCoordinator(redis)
                ),
                **kwargs,
            )
            raise AssertionError("foreign playback ACK was accepted")
        if case == "stale_ack":
            raise StaleResponse("response generation was superseded")

    async def initialize(**kwargs):
        state.stream_session_id, state.voice_session_id = "capture-1", "voice-1"
        state.capture_epoch, state.data_purpose = 1, "normal_capture"

    async def finalize(**kwargs):
        order.append(("finalize", None))
        finalization_details.append(kwargs)
        finalized.set()
        state.stream_session_id = None

    async def downlink(**kwargs):
        try:
            if case == "disconnect_downlink_failure":
                await continue_reading.wait()
                raise RuntimeError("closed websocket during downlink send")
            await asyncio.Event().wait()
        finally:
            downlink_closed.set()

    async def transcripts(**kwargs):
        kwargs["subscribed"].set()
        try:
            await asyncio.Event().wait()
        finally:
            transcripts_closed.set()

    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(controller.asyncio, "to_thread", inline)
    monkeypatch.setattr(controller, "_decode_opus_frames", delayed_decode)
    monkeypatch.setattr(controller, "_subscribe_v2_downlink", downlink)
    monkeypatch.setattr(controller, "_subscribe_v2_transcripts", transcripts)
    monkeypatch.setattr(
        controller,
        "websocket_auth",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    id="69b80e5894aa9ec334a421c9",
                    user_id="69b80e5894aa9ec334a421c9",
                    email="duplex@example.test",
                ),
                None,
            )
        ),
    )
    monkeypatch.setattr(
        controller, "create_client_state", AsyncMock(return_value=state)
    )
    monkeypatch.setattr(controller, "get_audio_stream_producer", lambda: producer)
    monkeypatch.setattr(controller, "initialize_capture_session", initialize)
    monkeypatch.setattr(controller, "finalize_capture_session", finalize)
    monkeypatch.setattr(controller, "cleanup_client_state", AsyncMock())
    monkeypatch.setattr(
        controller,
        "start_streaming_jobs",
        lambda **kwargs: {
            "speech_detection": "speech-job",
            "audio_persistence": "persist-job",
        },
    )
    monkeypatch.setattr(
        controller.ResponseCoordinator, "playback", AsyncMock(side_effect=playback)
    )
    monkeypatch.setattr(
        runtime,
        "VoiceConversationRuntime",
        lambda redis: SimpleNamespace(end_for_capture=AsyncMock()),
    )

    encoder = Encoder(16000, 1, "audio")
    packets = [
        serialize_media_envelope(
            pb.MediaEnvelope(
                capture=pb.CaptureMediaPacket(
                    binding=_binding(),
                    sequence=i,
                    monotonic_offset_us=i * 20000,
                    captured_at=_timestamp(),
                    delivery_class=pb.DELIVERY_CLASS_LIVE,
                    opus_payload=encoder.encode(bytes(640), 320),
                )
            )
        )
        for i in range(4)
    ]
    start = _control(
        start_capture=pb.StartCapture(
            capture_epoch=1,
            processing_profile=pb.PROCESSING_PROFILE_DUPLEX_AEC,
            data_purpose=pb.DATA_PURPOSE_NORMAL_CAPTURE,
            delivery_class=pb.DELIVERY_CLASS_LIVE,
            audio_spec=pb.AudioSpec(
                codec=pb.AUDIO_CODEC_OPUS,
                sample_rate_hz=16000,
                channel_count=1,
                bitrate_bps=24000,
                frame_duration=duration_pb2.Duration(nanos=20000000),
            ),
        )
    )
    ack = _control(
        playback_acknowledgement=pb.PlaybackAcknowledgement(
            binding=_binding(),
            response_id=pb.ResponseId(value="active-response"),
            generation=4,
            state=pb.PLAYBACK_STATE_PROGRESS,
            rendered_samples=24000,
            monotonic_timestamp_us=1000000,
        )
    )
    stop = _control(
        stop_capture=pb.StopCapture(
            binding=_binding(), reason=pb.STOP_REASON_USER_REQUESTED
        )
    )

    class GatedWebSocket(PhoneWebSocket):
        first_media_received = False
        peer_disconnected = False

        async def receive(self):
            if self.first_media_received:
                await continue_reading.wait()
            message = await super().receive()
            if message.get("bytes") is not None:
                self.first_media_received = True
            if message.get("type") == "websocket.disconnect":
                self.peer_disconnected = True
            return message

        async def send_text(self, value):
            if case in {
                "overflow_disconnect",
                "overflow_oserror",
                "ack_error_disconnect",
            } and _parse_server_control(value).HasField("capture_packet_accepted"):
                # The reader already exited on overload/control failure, so the
                # first failed ACK write is the only disconnect notification.
                assert (
                    not self.peer_disconnected
                ), "retried socket write after disconnect"
                self.peer_disconnected = True
                if case == "overflow_oserror":
                    raise OSError("socket closed")
                raise WebSocketDisconnect(code=1006)
            assert (
                not self.peer_disconnected
            ), "attempted ACK write after socket disconnect"
            await super().send_text(value)

    tail = [{"type": "websocket.receive", "text": stop}]
    if case.startswith("disconnect"):
        tail = [{"type": "websocket.disconnect"}]
    elif case.startswith("overflow"):
        tail = [{"type": "websocket.receive", "bytes": packets[3]}]
    socket = GatedWebSocket(
        [
            {"type": "websocket.receive", "text": start},
            {"type": "websocket.receive", "bytes": packets[0]},
            {"type": "websocket.receive", "bytes": packets[1]},
            {"type": "websocket.receive", "text": ack},
            {"type": "websocket.receive", "bytes": packets[2]},
            *tail,
        ]
    )
    task = asyncio.create_task(audio_v2_endpoint(socket))
    ack_before_media_release = False
    stop_waited_for_media = False
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        # The first packet is in-flight; two more can queue while ACK passes.
        if case in {"overflow_count", "overflow_disconnect", "overflow_oserror"}:
            monkeypatch.setattr(controller, "CAPTURE_INPUT_MAX_MESSAGES", 2)
        elif case == "overflow_bytes":
            monkeypatch.setattr(
                controller, "CAPTURE_INPUT_MAX_BYTES", len(packets[1]) + len(packets[2])
            )
        assert not any(
            _parse_server_control(x).HasField("capture_packet_accepted")
            for x in socket.sent_text
        )
        continue_reading.set()
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=5.1)
            ack_before_media_release = True
        except asyncio.TimeoutError:
            pass
        stop_waited_for_media = not finalized.is_set()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)

    events = [
        parse_stream_event(fields)
        for _, fields in await redis.xrange("audio:v2:durable:capture-1")
    ]
    controls = [_parse_server_control(raw) for raw in socket.sent_text]
    await redis.aclose()
    # Verify preservation before reporting the red control-priority assertion.
    expected_sequences = [0] if case == "decode_failure" else [0, 1, 2]
    if case == "ack_error_disconnect":
        expected_sequences = [0, 1]
    lost_during_drain = case in {
        "overflow_disconnect",
        "overflow_oserror",
        "ack_error_disconnect",
    }
    assert [e.WhichOneof("event") for e in events] == [
        "opened",
        *["frame" for _ in expected_sequences],
        "ended",
    ]
    assert [
        e.frame.sequence for e in events if e.HasField("frame")
    ] == expected_sequences
    accepted = [
        e.capture_packet_accepted.sequence
        for e in controls
        if e.HasField("capture_packet_accepted")
    ]
    assert accepted == (
        [] if case.startswith("disconnect") or lost_during_drain else expected_sequences
    )
    if lost_during_drain:
        assert finalization_details[0]["completion_reason"] == "protocol_error"
        assert (
            "overloaded" if case.startswith("overflow") else "invalid playback control"
        ) in finalization_details[0]["failure"]
    elif case.startswith("disconnect"):
        assert events[-1].ended.reason == pb.STOP_REASON_AUDIO_DISCONNECT
    elif case in {"decode_failure", "overflow_count", "overflow_bytes"}:
        assert controls[-1].HasField("error")
        assert socket.closed[0] == 1008
        assert (
            "overloaded" if case.startswith("overflow") else "invalid raw Opus"
        ) in controls[-1].error.detail
    else:
        assert controls[-1].HasField("capture_stopped")
    if case == "stale_ack" or case.startswith("foreign_"):
        errors = [e.error for e in controls if e.HasField("error")]
        assert len(errors) == 1
        assert errors[0].code == pb.PROTOCOL_ERROR_CODE_INVALID_TRANSITION
    assert stop_waited_for_media, "Stop finalized before preceding audio was durable"
    assert order == [("playback", 24000), ("finalize", None)]
    assert ack_calls[0] == {
        "response_id": "active-response",
        "generation": 4,
        "state": "progress",
        "rendered_samples": 24000,
        "buffered_samples": 0,
        "user_id": "69b80e5894aa9ec334a421c9",
        "client_id": "a421c9-phone",
        "audio_session_id": "capture-1",
        "voice_session_id": "voice-1",
        "capture_epoch": 1,
        "socket_id": state.socket_id,
        "monotonic_timestamp_ms": 1000,
    }
    assert downlink_closed.is_set() and transcripts_closed.is_set()
    controller.cleanup_client_state.assert_awaited_once()
    assert not [
        t for t in asyncio.all_tasks() if t.get_name() == "audio-v2-control-reader"
    ]
    assert ack_before_media_release, (
        "Playback ACK was blocked behind canonical audio ingestion beyond the "
        "five-second response progress deadline; capture itself remained ordered/durable"
    )
