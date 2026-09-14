from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.protobuf import duration_pb2, timestamp_pb2

from backend.audio_contract.v2 import audio_pb2
from backend.audio_contract.v2.codec import AudioProtocolV2Error
from backend.controllers import audio_v2_controller, capture_lifecycle

pytestmark = pytest.mark.unit


class Decoder:
    def decode_frames(self, payload):
        assert payload == b"raw-opus"
        return (b"\x00\x00" * 320,)


def _packet(delivery_class=audio_pb2.DELIVERY_CLASS_LIVE):
    captured_at = timestamp_pb2.Timestamp()
    captured_at.FromDatetime(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))
    return audio_pb2.CaptureMediaPacket(
        binding=audio_pb2.CaptureBinding(
            capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
            voice_session_id=audio_pb2.VoiceSessionId(value="voice-1"),
            capture_epoch=9,
        ),
        sequence=12,
        captured_at=captured_at,
        monotonic_offset_us=240_000,
        device_monotonic_timestamp_us=900_240_000,
        delivery_class=delivery_class,
        opus_payload=b"raw-opus",
    )


def test_v2_start_preserves_noninteractive_source_native_provenance():
    start = audio_pb2.StartCapture(
        capture_epoch=0,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_ANNOTATION,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        audio_spec=audio_pb2.AudioSpec(
            codec=audio_pb2.AUDIO_CODEC_OPUS,
            sample_rate_hz=16_000,
            channel_count=1,
            frame_duration=duration_pb2.Duration(nanos=20_000_000),
        ),
    )

    provenance = audio_v2_controller._start_provenance(start)

    assert provenance.protocol == 2
    assert provenance.capture_epoch == 0
    assert provenance.processing_profile == "source_native"
    assert provenance.data_purpose == "annotation"
    assert provenance.effects.aec.reporting == "unreported"
    assert provenance.memory_space_id is None


def test_v2_start_preserves_typed_memory_space_id():
    start = audio_pb2.StartCapture(
        capture_epoch=0,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        memory_space_id=audio_pb2.MemorySpaceId(
            value="9f3523c8-af75-469d-995a-7179531f3fc8"
        ),
    )

    provenance = audio_v2_controller._start_provenance(start)

    assert provenance.memory_space_id == "9f3523c8-af75-469d-995a-7179531f3fc8"


def test_v2_rejects_nonzero_source_native_epoch_at_protocol_boundary():
    start = audio_pb2.StartCapture(
        capture_epoch=1,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        audio_spec=audio_pb2.AudioSpec(
            codec=audio_pb2.AUDIO_CODEC_OPUS,
            sample_rate_hz=16_000,
            channel_count=1,
            frame_duration=duration_pb2.Duration(nanos=20_000_000),
        ),
    )

    with pytest.raises(AudioProtocolV2Error, match="source-native.*epoch zero"):
        audio_v2_controller._start_provenance(start)


async def test_v2_opus_decodes_once_then_crosses_realtime_and_durable_seams(
    monkeypatch,
):
    monkeypatch.setattr(
        audio_v2_controller,
        "_decode_opus_frames",
        AsyncMock(return_value=(b"\x00\x00" * 320,)),
    )
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id="voice-1",
        capture_epoch=9,
        data_purpose="normal_capture",
        client_id="client-1",
    )
    producer = SimpleNamespace(redis_client=object())
    streams = SimpleNamespace(publish_frame=AsyncMock())

    next_sequence = await audio_v2_controller.ingest_capture_packet(
        packet=_packet(),
        client_state=state,
        normalizer=Decoder(),
        v2_streams=streams,
        canonical_sequence=7,
    )

    assert (
        streams.publish_frame.await_args.args[0].frame.device_monotonic_timestamp_us
        == 900_240_000
    )
    assert next_sequence == 8
    assert streams.publish_frame.await_args.args[0].WhichOneof("event") == "frame"
    assert streams.publish_frame.await_args.args[0].frame.sequence == 7
    assert streams.publish_frame.await_args.args[0].frame.pcm_s16le == b"\x00\x00" * 320


async def test_recovered_packet_enters_only_typed_durable_stream(monkeypatch):
    monkeypatch.setattr(
        audio_v2_controller,
        "_decode_opus_frames",
        AsyncMock(return_value=(b"\x00\x00" * 320,)),
    )
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id="voice-1",
        capture_epoch=9,
        data_purpose="normal_capture",
        client_id="client-1",
    )

    streams = SimpleNamespace(publish_frame=AsyncMock())
    await audio_v2_controller.ingest_capture_packet(
        packet=_packet(audio_pb2.DELIVERY_CLASS_RECOVERED),
        client_state=state,
        normalizer=Decoder(),
        v2_streams=streams,
        canonical_sequence=0,
    )

    streams.publish_frame.assert_awaited_once()


async def test_v2_media_rejects_stale_connection_binding():
    packet = _packet()
    packet.binding.capture_session_id.value = "old-capture"

    with pytest.raises(AudioProtocolV2Error, match="stale session"):
        await audio_v2_controller.ingest_capture_packet(
            packet=packet,
            client_state=SimpleNamespace(
                stream_session_id="capture-1",
                voice_session_id="voice-1",
                capture_epoch=9,
                data_purpose="normal_capture",
                client_id="client-1",
            ),
            normalizer=Decoder(),
            v2_streams=SimpleNamespace(publish_frame=AsyncMock()),
            canonical_sequence=0,
        )


async def test_v2_60_ms_packet_publishes_three_contiguous_canonical_frames(
    monkeypatch,
):
    monkeypatch.setattr(
        audio_v2_controller,
        "_decode_opus_frames",
        AsyncMock(
            return_value=(
                b"\x01\x00" * 320,
                b"\x02\x00" * 320,
                b"\x03\x00" * 320,
            )
        ),
    )
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id="voice-1",
        capture_epoch=9,
        data_purpose="normal_capture",
    )
    streams = SimpleNamespace(publish_frame=AsyncMock())

    next_sequence = await audio_v2_controller.ingest_capture_packet(
        packet=_packet(),
        client_state=state,
        normalizer=Decoder(),
        v2_streams=streams,
        canonical_sequence=30,
    )

    assert next_sequence == 33
    frames = [call.args[0].frame for call in streams.publish_frame.await_args_list]
    assert [frame.sequence for frame in frames] == [30, 31, 32]
    assert [frame.monotonic_offset_us for frame in frames] == [
        240_000,
        260_000,
        280_000,
    ]
    assert [frame.captured_at.nanos for frame in frames] == [0, 20_000_000, 40_000_000]


async def test_protocol_rejection_marks_the_capture_failed(monkeypatch):
    updates = []

    class QueryField:
        def __eq__(self, value):
            return value

    class Capture:
        async def set(self, values):
            updates.append(values)

    class CaptureModel:
        capture_session_id = QueryField()
        find_one = AsyncMock(return_value=Capture())

    monkeypatch.setattr(capture_lifecycle, "AudioCaptureSession", CaptureModel)
    monkeypatch.setattr(capture_lifecycle, "publish_sse_event_async", AsyncMock())
    state = SimpleNamespace(
        stream_session_id="capture-1",
        markers=[],
        last_persistence_healthcheck=5.0,
    )
    producer = SimpleNamespace(
        finalize_session=AsyncMock(),
        store=SimpleNamespace(mark_complete=AsyncMock(), set_markers=AsyncMock()),
    )

    await capture_lifecycle.finalize_capture_session(
        client_state=state,
        producer=producer,
        user_id="user-1",
        client_id="client-1",
        completion_reason="protocol_error",
        failure="invalid raw Opus packet",
    )

    producer.finalize_session.assert_awaited_once_with(
        "capture-1", completion_reason="protocol_error"
    )
    assert updates == [{"status": "failed", "failure": "invalid raw Opus packet"}]
    assert state.stream_session_id is None
