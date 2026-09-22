"""Strict codecs for the Chronicle audio v2 wire contract.

Callers exchange generated messages, never dictionaries. JSON exists only as the
human-readable encoding of a generated control envelope; media remains one atomic
binary Protobuf message per WebSocket frame.
"""

from __future__ import annotations

import opuslib
from google.protobuf import json_format
from google.protobuf.message import DecodeError, Message

from . import audio_pb2

MAX_CONTROL_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 4 * 1024
MAX_RAW_OPUS_PACKET_BYTES = 1_500
_OGG_CAPTURE_PATTERN = b"OggS"


class AudioProtocolV2Error(ValueError):
    """A v2 envelope failed schema or Chronicle invariant validation."""


CANONICAL_FRAME_MS = 20
UPLINK_FRAME_MS = frozenset({20, 60})


class RawOpusNormalizer:
    """Decode one declared uplink packet into canonical 20 ms PCM frames."""

    def __init__(self, frame_duration_ms: int) -> None:
        if frame_duration_ms not in UPLINK_FRAME_MS:
            raise AudioProtocolV2Error("uplink Opus requires 20 or 60 ms packets")
        self._frame_duration_ms = frame_duration_ms
        self._decoder = opuslib.Decoder(16_000, 1)

    def decode_frames(self, payload: bytes) -> tuple[bytes, ...]:
        if not payload:
            raise AudioProtocolV2Error("capture packet has no Opus payload")
        try:
            pcm = self._decoder.decode(
                payload,
                16 * self._frame_duration_ms,
                decode_fec=False,
            )
        except Exception as error:
            raise AudioProtocolV2Error("invalid raw Opus packet") from error
        expected_bytes = 32 * self._frame_duration_ms
        if len(pcm) != expected_bytes:
            raise AudioProtocolV2Error(
                f"Opus packet decoded to {len(pcm)} bytes; expected {expected_bytes}"
            )
        canonical_bytes = 32 * CANONICAL_FRAME_MS
        return tuple(
            pcm[offset : offset + canonical_bytes]
            for offset in range(0, len(pcm), canonical_bytes)
        )


def _require_id(value: str, label: str) -> None:
    if not value or len(value) > 255:
        raise AudioProtocolV2Error(f"{label} must contain 1-255 characters")


def _require_binding(binding: audio_pb2.CaptureBinding) -> None:
    _require_id(binding.capture_session_id.value, "capture_session_id")


def _require_event(message: Message, field: str = "event") -> str:
    selected = message.WhichOneof(field)
    if selected is None:
        raise AudioProtocolV2Error(f"{message.DESCRIPTOR.name} requires one {field}")
    return selected


def parse_client_control_json(payload: str) -> audio_pb2.ClientControl:
    encoded = payload.encode("utf-8")
    if not encoded or len(encoded) > MAX_CONTROL_BYTES:
        raise AudioProtocolV2Error("control frame has an invalid size")
    message = audio_pb2.ClientControl()
    try:
        json_format.Parse(payload, message, ignore_unknown_fields=False)
    except (json_format.ParseError, UnicodeError) as error:
        raise AudioProtocolV2Error("invalid ClientControl JSON") from error
    _require_id(message.event_id.value, "event_id")
    if not message.HasField("sent_at"):
        raise AudioProtocolV2Error("ClientControl requires sent_at")
    event = _require_event(message)
    if event == "hello":
        _require_id(message.hello.bearer_token, "bearer_token")
        _require_id(message.hello.source_id.value, "source_id")
        if message.hello.device_kind == audio_pb2.DEVICE_KIND_UNSPECIFIED:
            raise AudioProtocolV2Error("hello requires device_kind")
    elif event == "start_capture":
        validate_start_capture(message.start_capture)
    elif event == "stop_capture":
        _require_binding(message.stop_capture.binding)
        if message.stop_capture.reason == audio_pb2.STOP_REASON_UNSPECIFIED:
            raise AudioProtocolV2Error("stop_capture requires reason")
    elif event == "voice_ready":
        _require_binding(message.voice_ready.binding)
    elif event == "playback_acknowledgement":
        acknowledgement = message.playback_acknowledgement
        _require_binding(acknowledgement.binding)
        _require_id(acknowledgement.response_id.value, "response_id")
        if acknowledgement.state == audio_pb2.PLAYBACK_STATE_UNSPECIFIED:
            raise AudioProtocolV2Error("playback acknowledgement requires state")
    elif event == "conversation_command":
        command = message.conversation_command
        _require_binding(command.binding)
        if command.action not in {
            audio_pb2.CONVERSATION_ACTION_START,
            audio_pb2.CONVERSATION_ACTION_END,
            audio_pb2.CONVERSATION_ACTION_CANCEL_TASK,
            audio_pb2.CONVERSATION_ACTION_SNAPSHOT,
        }:
            raise AudioProtocolV2Error("invalid conversation action")
        if (
            command.action == audio_pb2.CONVERSATION_ACTION_START
            and command.engine
            not in {audio_pb2.SPEECH_ENGINE_MODULAR, audio_pb2.SPEECH_ENGINE_REALTIME}
        ):
            raise AudioProtocolV2Error("start conversation requires an engine")
        if command.action == audio_pb2.CONVERSATION_ACTION_CANCEL_TASK:
            _require_id(command.task_id, "task_id")
    return message


def serialize_client_control_json(message: audio_pb2.ClientControl) -> str:
    # Validate through the same public decoder so outbound test clients cannot emit
    # a shape the backend itself would reject.
    rendered = json_format.MessageToJson(
        message,
        preserving_proto_field_name=True,
        indent=None,
        sort_keys=True,
        always_print_fields_with_no_presence=False,
    )
    parse_client_control_json(rendered)
    return rendered


def serialize_server_control_json(message: audio_pb2.ServerControl) -> str:
    _require_id(message.event_id.value, "event_id")
    if not message.HasField("sent_at"):
        raise AudioProtocolV2Error("ServerControl requires sent_at")
    _require_event(message)
    return json_format.MessageToJson(
        message,
        preserving_proto_field_name=True,
        indent=None,
        sort_keys=True,
        always_print_fields_with_no_presence=False,
    )


def frame_duration_ms(spec: audio_pb2.AudioSpec) -> int:
    if not spec.HasField("frame_duration"):
        raise AudioProtocolV2Error("audio_spec requires frame_duration")
    if spec.frame_duration.seconds != 0:
        raise AudioProtocolV2Error("audio frame duration must be below one second")
    return spec.frame_duration.nanos // 1_000_000


def validate_audio_spec(spec: audio_pb2.AudioSpec, *, live_uplink: bool) -> None:
    if spec.codec != audio_pb2.AUDIO_CODEC_OPUS:
        raise AudioProtocolV2Error("live audio requires raw Opus")
    expected_rate = 16_000 if live_uplink else 24_000
    if spec.sample_rate_hz != expected_rate or spec.channel_count != 1:
        raise AudioProtocolV2Error(f"live audio requires {expected_rate} Hz mono Opus")
    duration_ms = frame_duration_ms(spec)
    allowed = UPLINK_FRAME_MS if live_uplink else {CANONICAL_FRAME_MS}
    if spec.frame_duration.nanos % 1_000_000 or duration_ms not in allowed:
        expected = "20 or 60 ms" if live_uplink else "20 ms"
        raise AudioProtocolV2Error(f"live Opus requires {expected} packets")


def validate_start_capture(message: audio_pb2.StartCapture) -> None:
    if message.processing_profile == audio_pb2.PROCESSING_PROFILE_UNSPECIFIED:
        raise AudioProtocolV2Error("start_capture requires processing_profile")
    if message.data_purpose == audio_pb2.DATA_PURPOSE_UNSPECIFIED:
        raise AudioProtocolV2Error("start_capture requires data_purpose")
    if message.delivery_class == audio_pb2.DELIVERY_CLASS_UNSPECIFIED:
        raise AudioProtocolV2Error("start_capture requires delivery_class")
    if (
        message.delivery_class == audio_pb2.DELIVERY_CLASS_RECOVERED
        and not message.recovery_batch_id
    ):
        raise AudioProtocolV2Error("recovered capture requires recovery_batch_id")
    validate_audio_spec(message.audio_spec, live_uplink=True)


def parse_media_envelope(payload: bytes) -> audio_pb2.MediaEnvelope:
    if not payload or len(payload) > MAX_MEDIA_BYTES:
        raise AudioProtocolV2Error("media frame has an invalid size")
    message = audio_pb2.MediaEnvelope()
    try:
        message.ParseFromString(payload)
    except DecodeError as error:
        raise AudioProtocolV2Error("invalid MediaEnvelope Protobuf") from error
    media = _require_event(message, "media")
    if media == "capture":
        packet = message.capture
        _require_binding(packet.binding)
        if not packet.HasField("captured_at"):
            raise AudioProtocolV2Error("capture packet requires captured_at")
        if packet.delivery_class == audio_pb2.DELIVERY_CLASS_UNSPECIFIED:
            raise AudioProtocolV2Error("capture packet requires delivery_class")
        if not packet.opus_payload:
            raise AudioProtocolV2Error("capture packet has no Opus payload")
        if len(packet.opus_payload) > MAX_RAW_OPUS_PACKET_BYTES:
            raise AudioProtocolV2Error("Opus packet is too large")
        if packet.opus_payload.startswith(_OGG_CAPTURE_PATTERN):
            raise AudioProtocolV2Error("containerized Ogg is not a live Opus packet")
    else:
        packet = message.playback
        _require_id(packet.response_id.value, "response_id")
        if not packet.opus_payload:
            raise AudioProtocolV2Error("playback packet has no Opus payload")
    return message


def serialize_media_envelope(message: audio_pb2.MediaEnvelope) -> bytes:
    payload = message.SerializeToString(deterministic=True)
    parse_media_envelope(payload)
    return payload
