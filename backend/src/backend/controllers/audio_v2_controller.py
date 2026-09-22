"""Typed Chronicle audio-v2 WebSocket ingress.

This controller owns only transport translation. Capture lifecycle, durability,
voice framing, and inference remain behind their existing application services while
those services are migrated to generated Redis messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import partial

from fastapi import WebSocket, WebSocketDisconnect
from google.protobuf import timestamp_pb2

import backend.services.interaction_modes.voice.runtime as runtime
from backend.audio_contract.v2 import audio_pb2
from backend.audio_contract.v2.codec import (
    AudioProtocolV2Error,
    RawOpusNormalizer,
    frame_duration_ms,
    parse_client_control_json,
    parse_media_envelope,
    serialize_media_envelope,
    serialize_server_control_json,
)
from backend.auth import websocket_auth
from backend.client_manager import generate_client_id
from backend.controllers.capture_lifecycle import (
    DECODER_EXECUTOR,
    cleanup_client_state,
    create_client_state,
    finalize_capture_session,
    handle_button_event,
    initialize_capture_session,
)
from backend.controllers.queue_controller import start_streaming_jobs
from backend.models.audio_capabilities import VoiceCapabilities
from backend.models.audio_capture import CaptureEffects, CaptureStartProvenance
from backend.redis_keys import ClientId, device_downlink_channel
from backend.services.audio_stream.producer import get_audio_stream_producer
from backend.services.audio_stream.v2_streams import AudioV2Streams
from backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from backend.services.response_coordinator import (
    ResponseCoordinator,
    ResponseCoordinatorError,
)
from backend.services.voice_diagnostics import VoiceCadenceRecorder, cadence_span
from backend.services.voice_sessions import VoiceSessionCoordinator, VoiceSessionError

AUDIO_SUBPROTOCOL = "chronicle.audio.v2"
logger = logging.getLogger(__name__)
_memory_scopes = MemoryScopeResolver()

_PROFILE_TO_DOMAIN = {
    audio_pb2.PROCESSING_PROFILE_AMBIENT: "ambient",
    audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE: "source_native",
    audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC: "duplex_aec",
    audio_pb2.PROCESSING_PROFILE_DUPLEX_ISOLATED: "duplex_isolated",
    audio_pb2.PROCESSING_PROFILE_HALF_DUPLEX: "half_duplex",
    audio_pb2.PROCESSING_PROFILE_IMPORTED: "imported",
}
_PURPOSE_TO_DOMAIN = {
    audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE: "normal_capture",
    audio_pb2.DATA_PURPOSE_ANNOTATION: "annotation",
}
_STOP_REASON_TO_DOMAIN = {
    audio_pb2.STOP_REASON_USER_REQUESTED: "user_stopped",
    audio_pb2.STOP_REASON_AUDIO_DISCONNECT: "websocket_disconnect",
    audio_pb2.STOP_REASON_INTERACTION_COMPLETE: "user_stopped",
    audio_pb2.STOP_REASON_TEMPORARILY_UNAVAILABLE: "websocket_disconnect",
}


def _now() -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromDatetime(datetime.now(timezone.utc))
    return value


def _event_id() -> audio_pb2.EventId:
    return audio_pb2.EventId(value=str(uuid.uuid4()))


async def _send_control(websocket: WebSocket, **event) -> None:
    message = audio_pb2.ServerControl(event_id=_event_id(), sent_at=_now(), **event)
    await websocket.send_text(serialize_server_control_json(message))


async def _handle_playback_acknowledgement(
    websocket, control, *, responses, user_id, client_id, socket_id
) -> None:
    acknowledgement = control.playback_acknowledgement
    binding = acknowledgement.binding
    state = {
        audio_pb2.PLAYBACK_STATE_STARTED: "started",
        audio_pb2.PLAYBACK_STATE_DONE: "done",
        audio_pb2.PLAYBACK_STATE_CANCELLED: "cancelled",
        audio_pb2.PLAYBACK_STATE_FAILED: "failed",
        audio_pb2.PLAYBACK_STATE_PROGRESS: "progress",
    }.get(acknowledgement.state)
    if state is None:
        raise AudioProtocolV2Error("unsupported playback state")
    try:
        await responses.playback(
            response_id=acknowledgement.response_id.value,
            generation=acknowledgement.generation,
            state=state,
            rendered_samples=acknowledgement.rendered_samples,
            buffered_samples=acknowledgement.buffered_samples,
            user_id=user_id,
            client_id=client_id,
            audio_session_id=binding.capture_session_id.value,
            voice_session_id=binding.voice_session_id.value,
            capture_epoch=binding.capture_epoch,
            socket_id=socket_id,
            monotonic_timestamp_ms=(acknowledgement.monotonic_timestamp_us // 1_000),
        )
    except ResponseCoordinatorError as error:
        # A progress ACK may already be in flight when onset or
        # End advances the response generation. The coordinator
        # has rejected it without changing playback state; this
        # response race must never stop canonical audio capture.
        await _send_control(
            websocket,
            error=audio_pb2.ProtocolError(
                code=audio_pb2.PROTOCOL_ERROR_CODE_INVALID_TRANSITION,
                detail=str(error),
                rejected_event_id=control.event_id,
            ),
        )


# One bounded receive queue per socket: about ten seconds of 20 ms media.
# The byte cap also bounds queued control messages (whose wire limit is larger).
CAPTURE_INPUT_MAX_MESSAGES = 512
CAPTURE_INPUT_MAX_BYTES = 2 * 1024 * 1024


class _CaptureInput:
    """Read ACKs promptly while one ordered consumer persists capture messages.

    There is one reader task, never a task per packet. Overflow ends admission
    explicitly; already queued messages drain before the failure is surfaced.
    """

    def __init__(self, websocket, acknowledge):
        self.websocket = websocket
        self.acknowledge = acknowledge
        self.pending = deque()
        self.pending_bytes = 0
        self.changed = asyncio.Event()
        self.finished = False
        self.disconnected = False
        self.error = None
        self.task = asyncio.create_task(self._read(), name="audio-v2-control-reader")

    async def _read(self):
        try:
            while True:
                incoming = await self.websocket.receive()
                if incoming.get("type") == "websocket.disconnect":
                    self.disconnected = True
                    return
                raw_text = incoming.get("text")
                raw_bytes = incoming.get("bytes")
                if raw_text is not None:
                    control = parse_client_control_json(raw_text)
                    if control.WhichOneof("event") == "playback_acknowledgement":
                        await self.acknowledge(control)
                        continue
                size = (
                    len(raw_text.encode())
                    if raw_text is not None
                    else len(raw_bytes or b"")
                )
                if (
                    len(self.pending) >= CAPTURE_INPUT_MAX_MESSAGES
                    or self.pending_bytes + size > CAPTURE_INPUT_MAX_BYTES
                ):
                    raise AudioProtocolV2Error(
                        "capture input overloaded; queued audio will drain, but further packets were not accepted"
                    )
                self.pending.append((incoming, size))
                self.pending_bytes += size
                self.changed.set()
        except (WebSocketDisconnect, OSError):
            self.disconnected = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
        finally:
            self.finished = True
            self.changed.set()

    async def receive(self):
        while not self.pending:
            if self.finished:
                if self.error is not None:
                    raise self.error
                return {"type": "websocket.disconnect"}
            await self.changed.wait()
        incoming, size = self.pending.popleft()
        self.pending_bytes -= size
        if not self.pending:
            self.changed.clear()
        return incoming

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def _subscribe_v2_downlink(
    *,
    websocket: WebSocket,
    redis_client,
    voice_sessions,
    responses,
    client_state,
    user_id: str,
    client_id: str,
) -> None:
    """Validate typed Redis downlink events and forward them to one bound socket."""

    diagnostics = {}
    flush_tasks = set()

    def report(response_id, outcome):
        diagnostic = diagnostics.pop(response_id, None)
        if diagnostic is None:
            return
        if len(flush_tasks) >= 16:
            logger.warning("Voice backend diagnostic flush capacity exhausted")
            return
        task = asyncio.create_task(
            diagnostic.flush(outcome), name="voice-backend-diagnostic-flush"
        )
        flush_tasks.add(task)
        task.add_done_callback(flush_tasks.discard)

    def diagnostic_for(response_id, binding, generation, turn_id=""):
        if response_id not in diagnostics:
            if len(diagnostics) >= 16:
                report(next(iter(diagnostics)), "diagnostic_capacity")
            diagnostics[response_id] = VoiceCadenceRecorder(
                user_id=user_id,
                client_id=client_id,
                capture_session_id=binding.capture_session_id.value,
                voice_session_id=binding.voice_session_id.value,
                capture_epoch=binding.capture_epoch,
                response_id=response_id,
                generation=generation,
                turn_id=turn_id,
                platform="backend-voice",
            )
        return diagnostics[response_id]

    channel = str(device_downlink_channel(ClientId.from_value(client_id)))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message["type"] != "message":
                continue
            event = audio_pb2.DeviceDownlinkEvent()
            try:
                event.ParseFromString(message["data"])
            except Exception:
                continue
            kind = event.WhichOneof("event")
            if kind == "playback_offer":
                offer = event.playback_offer
                binding = offer.binding
                validation_started = time.perf_counter() * 1000
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=binding.capture_session_id.value,
                    voice_session_id=binding.voice_session_id.value,
                    capture_epoch=binding.capture_epoch,
                    socket_id=client_state.socket_id,
                    require_ready=True,
                ):
                    continue
                diagnostic = diagnostic_for(
                    offer.response_id.value,
                    binding,
                    offer.generation,
                    offer.turn_id.value,
                )
                diagnostic.pre_skip_samples = offer.pre_skip_samples
                diagnostic.observe(
                    "offer_validation", validation_started, time.perf_counter() * 1000
                )
                with diagnostic.bind(), cadence_span("offer_socket_send"):
                    await _send_control(websocket, playback_offer=offer)
            elif kind == "playback":
                packet = event.playback
                validation_started = time.perf_counter() * 1000
                record = await responses.get(packet.response_id.value)
                if record is None or record.generation != packet.generation:
                    continue
                try:
                    await responses.assert_current(record)
                except ResponseCoordinatorError:
                    continue
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=record.audio_session_id,
                    voice_session_id=record.voice_session_id,
                    capture_epoch=record.capture_epoch,
                    socket_id=client_state.socket_id,
                    require_ready=True,
                ):
                    continue
                diagnostic = diagnostic_for(
                    record.response_id,
                    audio_pb2.CaptureBinding(
                        capture_session_id=audio_pb2.CaptureSessionId(
                            value=record.audio_session_id
                        ),
                        voice_session_id=audio_pb2.VoiceSessionId(
                            value=record.voice_session_id
                        ),
                        capture_epoch=record.capture_epoch,
                    ),
                    record.generation,
                    record.turn_id,
                )
                diagnostic.pre_skip_samples = record.pre_skip_samples
                diagnostic.observe(
                    "downlink_validation",
                    validation_started,
                    time.perf_counter() * 1000,
                    sequence=packet.sequence,
                    sample_end=(packet.sequence + 1) * 480,
                )
                with diagnostic.bind(), cadence_span(
                    "socket_send",
                    sequence=packet.sequence,
                    sample_end=(packet.sequence + 1) * 480,
                ):
                    await websocket.send_bytes(
                        serialize_media_envelope(
                            audio_pb2.MediaEnvelope(playback=packet)
                        )
                    )
            elif kind in {
                "playback_finished",
                "conversation_state",
                "voice_processing_update",
            }:
                payload = getattr(event, kind)
                binding = payload.binding
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=binding.capture_session_id.value,
                    voice_session_id=binding.voice_session_id.value,
                    capture_epoch=binding.capture_epoch,
                    socket_id=client_state.socket_id,
                    require_ready=True,
                ):
                    continue
                if kind == "voice_processing_update":
                    try:
                        await responses.assert_generation(
                            user_id, client_id, payload.generation
                        )
                    except ResponseCoordinatorError:
                        continue
                if kind == "playback_finished":
                    record = await responses.get(payload.response_id.value)
                    if record is None or record.generation != payload.generation:
                        continue
                    try:
                        await responses.assert_current(record)
                    except ResponseCoordinatorError:
                        continue
                await _send_control(websocket, **{kind: payload})
                if kind == "playback_finished":
                    report(payload.response_id.value, "producer_finished_sent")
            elif kind == "cancel_playback":
                cancel = event.cancel_playback
                binding = cancel.binding
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=binding.capture_session_id.value,
                    voice_session_id=binding.voice_session_id.value,
                    capture_epoch=binding.capture_epoch,
                    socket_id=client_state.socket_id,
                ):
                    continue
                await _send_control(websocket, cancel_playback=cancel)
                report(cancel.response_id.value, "cancel_sent")
    finally:
        for response_id in list(diagnostics):
            report(response_id, "socket_ended")
        if flush_tasks:
            await asyncio.gather(*list(flush_tasks), return_exceptions=True)
        await pubsub.unsubscribe(channel)
        await pubsub.close()


async def _subscribe_v2_transcripts(
    *,
    websocket: WebSocket,
    redis_client,
    binding: audio_pb2.CaptureBinding,
    subscribed: asyncio.Event | None = None,
) -> None:
    """Forward streaming STT pub/sub messages as typed, capture-bound controls."""

    channel = f"transcription:interim:{binding.capture_session_id.value}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    if subscribed is not None:
        subscribed.set()
    logger.info("Subscribed Audio V2 transcript channel: %s", channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.warning("Ignored malformed transcript update on %s", channel)
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            await _send_control(
                websocket,
                transcript_update=audio_pb2.TranscriptUpdate(
                    binding=binding,
                    text=text,
                    is_final=bool(payload.get("is_final", False)),
                    confidence=float(payload.get("confidence") or 0.0),
                    speaker_name=(
                        payload.get("speaker_name")
                        if isinstance(payload.get("speaker_name"), str)
                        else ""
                    ),
                ),
            )
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


async def _decode_opus_frames(
    normalizer: RawOpusNormalizer, payload: bytes
) -> tuple[bytes, ...]:
    return await asyncio.get_running_loop().run_in_executor(
        DECODER_EXECUTOR,
        partial(normalizer.decode_frames, payload),
    )


def _effects(start: audio_pb2.StartCapture) -> CaptureEffects:
    interactive = start.processing_profile in {
        audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC,
        audio_pb2.PROCESSING_PROFILE_DUPLEX_ISOLATED,
        audio_pb2.PROCESSING_PROFILE_HALF_DUPLEX,
    }
    if not interactive:
        return CaptureEffects.unreported()
    capabilities = start.capabilities
    return CaptureEffects.model_validate(
        {
            "aec": {
                "reporting": "reported",
                "requested": capabilities.acoustic_echo_cancellation.requested,
                "available": capabilities.acoustic_echo_cancellation.available,
                "enabled": capabilities.acoustic_echo_cancellation.enabled,
            },
            "noise_suppression": {
                "reporting": "reported",
                "requested": capabilities.noise_suppression.requested,
                "available": capabilities.noise_suppression.available,
                "enabled": capabilities.noise_suppression.enabled,
            },
        }
    )


def _start_provenance(start: audio_pb2.StartCapture) -> CaptureStartProvenance:
    profile = _PROFILE_TO_DOMAIN[start.processing_profile]
    if profile == "source_native" and start.capture_epoch != 0:
        raise AudioProtocolV2Error("source-native capture requires epoch zero")
    return CaptureStartProvenance(
        protocol=2,
        capture_epoch=start.capture_epoch,
        processing_profile=profile,
        effects=_effects(start),
        data_purpose=_PURPOSE_TO_DOMAIN[start.data_purpose],
        memory_space_id=start.memory_space_id.value or None,
    )


def _voice_capabilities(
    capabilities: audio_pb2.CaptureCapabilities,
) -> VoiceCapabilities:
    return VoiceCapabilities.model_validate(
        {
            "mode": {
                audio_pb2.DUPLEX_MODE_FULL: "duplex_full",
                audio_pb2.DUPLEX_MODE_ISOLATED: "duplex_isolated",
                audio_pb2.DUPLEX_MODE_HALF: "duplex_half",
            }[capabilities.duplex_mode],
            "input_route": {
                audio_pb2.INPUT_ROUTE_BUILT_IN_MIC: "built_in_mic",
                audio_pb2.INPUT_ROUTE_BLUETOOTH_HFP: "bluetooth_hfp",
                audio_pb2.INPUT_ROUTE_WIRED_MIC: "wired_mic",
                audio_pb2.INPUT_ROUTE_USB: "usb",
                audio_pb2.INPUT_ROUTE_REMOTE: "unknown",
                audio_pb2.INPUT_ROUTE_UNKNOWN: "unknown",
            }[capabilities.input_route],
            "output_route": {
                audio_pb2.OUTPUT_ROUTE_SPEAKERPHONE: "speakerphone",
                audio_pb2.OUTPUT_ROUTE_EARPIECE: "earpiece",
                audio_pb2.OUTPUT_ROUTE_HEADPHONES: "headphones",
                audio_pb2.OUTPUT_ROUTE_BLUETOOTH_HFP: "bluetooth_hfp",
                audio_pb2.OUTPUT_ROUTE_USB: "usb",
                audio_pb2.OUTPUT_ROUTE_REMOTE: "remote",
            }[capabilities.output_route],
            "native_sample_rate": capabilities.native_sample_rate_hz,
            "incremental_playback": capabilities.incremental_playback,
            "aec": {
                "requested": capabilities.acoustic_echo_cancellation.requested,
                "available": capabilities.acoustic_echo_cancellation.available,
                "enabled": capabilities.acoustic_echo_cancellation.enabled,
            },
            "noise_suppression": {
                "requested": capabilities.noise_suppression.requested,
                "available": capabilities.noise_suppression.available,
                "enabled": capabilities.noise_suppression.enabled,
            },
            "fallback_reason": (
                "aec_unavailable"
                if capabilities.duplex_mode == audio_pb2.DUPLEX_MODE_HALF
                else None
            ),
        }
    )


async def ingest_capture_packet(
    *,
    packet: audio_pb2.CaptureMediaPacket,
    client_state,
    normalizer: RawOpusNormalizer,
    v2_streams: AudioV2Streams,
    canonical_sequence: int,
    previous_monotonic_offset_us: int | None = None,
) -> int:
    """Normalize one bound Opus packet and publish canonical 20 ms frames."""

    session_id = client_state.stream_session_id
    if session_id is None:
        raise AudioProtocolV2Error("media arrived before capture start")
    if packet.binding.capture_session_id.value != session_id:
        raise AudioProtocolV2Error("capture packet has a stale session binding")
    if packet.binding.capture_epoch != client_state.capture_epoch:
        raise AudioProtocolV2Error("capture packet has a stale epoch binding")
    expected_voice = client_state.voice_session_id or ""
    if packet.binding.voice_session_id.value != expected_voice:
        raise AudioProtocolV2Error("capture packet has a stale voice binding")
    if (
        packet.delivery_class == audio_pb2.DELIVERY_CLASS_LIVE
        and previous_monotonic_offset_us is not None
        and packet.monotonic_offset_us <= previous_monotonic_offset_us
    ):
        raise AudioProtocolV2Error("live capture clock did not advance")

    frames = await _decode_opus_frames(normalizer, packet.opus_payload)
    for index, pcm in enumerate(frames):
        captured_at = timestamp_pb2.Timestamp()
        captured_at.FromDatetime(
            packet.captured_at.ToDatetime(tzinfo=timezone.utc)
            + timedelta(milliseconds=index * 20)
        )
        await v2_streams.publish_frame(
            audio_pb2.CaptureStreamEvent(
                frame=audio_pb2.CanonicalPcmFrame(
                    binding=packet.binding,
                    sequence=canonical_sequence + index,
                    captured_at=captured_at,
                    monotonic_offset_us=packet.monotonic_offset_us + index * 20_000,
                    device_monotonic_timestamp_us=(
                        packet.device_monotonic_timestamp_us + index * 20_000
                        if packet.HasField("device_monotonic_timestamp_us")
                        else None
                    ),
                    delivery_class=packet.delivery_class,
                    pcm_s16le=pcm,
                    data_purpose={
                        "normal_capture": audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
                        "annotation": audio_pb2.DATA_PURPOSE_ANNOTATION,
                    }[client_state.data_purpose],
                )
            )
        )
    return canonical_sequence + len(frames)


async def handle_audio_v2_websocket(websocket: WebSocket) -> None:
    offered = websocket.scope.get("subprotocols", [])
    if AUDIO_SUBPROTOCOL not in offered:
        await websocket.close(code=1002, reason="chronicle.audio.v2 required")
        return
    await websocket.accept(subprotocol=AUDIO_SUBPROTOCOL)

    client_state = None
    client_id = None
    producer = None
    interim_task = None
    downlink_task = None
    v2_streams = None
    active_binding = None
    capture_input = None

    async def send_control(**event):
        # Once the peer disconnects, drain queued media without attempting ACK
        # writes on a closed socket. Only durable frames were ever acknowledged.
        if capture_input is not None and capture_input.disconnected:
            return
        try:
            await _send_control(websocket, **event)
        except (WebSocketDisconnect, OSError):
            if capture_input is None:
                raise
            # Overflow may have ended the reader before it could observe EOF.
            # A send-side disconnect is equally authoritative: stop admission
            # and ACK writes, while the ordered consumer drains its queued prefix.
            capture_input.disconnected = True
            await capture_input.close()

    try:
        first = await websocket.receive_text()
        hello_control = parse_client_control_json(first)
        if hello_control.WhichOneof("event") != "hello":
            raise AudioProtocolV2Error("first control must be ClientHello")
        hello = hello_control.hello
        user, _failure = await websocket_auth(websocket, hello.bearer_token)
        if user is None:
            await send_control(
                error=audio_pb2.ProtocolError(
                    code=audio_pb2.PROTOCOL_ERROR_CODE_AUTHENTICATION_FAILED,
                    detail="authentication failed",
                    rejected_event_id=hello_control.event_id,
                ),
            )
            await websocket.close(code=1008, reason="authentication failed")
            return

        device_name = hello.display_name or hello.source_id.value
        client_id = generate_client_id(user, device_name)
        client_state = await create_client_state(client_id, user, device_name)
        client_state.socket_id = f"audio-v2-{uuid.uuid4()}"
        producer = get_audio_stream_producer()
        voice_sessions = VoiceSessionCoordinator(producer.redis_client)
        responses = ResponseCoordinator(producer.redis_client, voice_sessions)
        downlink_task = asyncio.create_task(
            _subscribe_v2_downlink(
                websocket=websocket,
                redis_client=producer.redis_client,
                voice_sessions=voice_sessions,
                responses=responses,
                client_state=client_state,
                user_id=user.user_id,
                client_id=client_id,
            )
        )
        await send_control(
            hello=audio_pb2.ServerHello(
                client_id=audio_pb2.ClientId(value=client_id),
                connection_id=audio_pb2.ConnectionId(value=client_state.socket_id),
            ),
        )

        capture_input = _CaptureInput(
            websocket,
            partial(
                _handle_playback_acknowledgement,
                websocket,
                responses=responses,
                user_id=user.user_id,
                client_id=client_id,
                socket_id=client_state.socket_id,
            ),
        )
        normalizer = None
        active_delivery_class = audio_pb2.DELIVERY_CLASS_UNSPECIFIED
        last_sequence = -1
        last_monotonic_offset_us = None
        canonical_sequence = 0
        while True:
            incoming = await capture_input.receive()
            if incoming.get("type") == "websocket.disconnect":
                # The reader surfaces disconnect only after the admitted prefix
                # has drained. Publish its exact durable terminal boundary.
                if v2_streams is not None and active_binding is not None:
                    await finalize_capture_session(
                        client_state=client_state,
                        producer=producer,
                        user_id=user.user_id,
                        client_id=client_id,
                        completion_reason="websocket_disconnect",
                    )
                    await v2_streams.end(
                        audio_pb2.CaptureStreamEvent(
                            ended=audio_pb2.CaptureStreamEnded(
                                binding=active_binding,
                                reason=audio_pb2.STOP_REASON_AUDIO_DISCONNECT,
                            )
                        )
                    )
                break
            if incoming.get("text") is not None:
                control = parse_client_control_json(incoming["text"])
                event = control.WhichOneof("event")
                if event == "start_capture":
                    if client_state.stream_session_id is not None:
                        raise AudioProtocolV2Error("capture is already active")
                    start = control.start_capture
                    active_delivery_class = start.delivery_class
                    source_frame_duration_ms = frame_duration_ms(start.audio_spec)
                    normalizer = RawOpusNormalizer(source_frame_duration_ms)
                    provenance = _start_provenance(start)
                    if provenance.memory_space_id:
                        try:
                            await _memory_scopes.require_space(
                                MemoryScope(
                                    str(user.user_id), provenance.memory_space_id
                                ),
                                writable=True,
                            )
                        except MemoryScopeError as exc:
                            raise AudioProtocolV2Error(str(exc)) from exc
                    await initialize_capture_session(
                        client_state=client_state,
                        producer=producer,
                        user_id=user.user_id,
                        user_email=user.email,
                        client_id=client_id,
                        source_format={
                            "codec": "opus",
                            "rate": 16_000,
                            "width": 2,
                            "channels": 1,
                            "frame_duration_ms": source_frame_duration_ms,
                            "mode": "streaming",
                        },
                        provenance=provenance,
                    )
                    binding = audio_pb2.CaptureBinding(
                        capture_session_id=audio_pb2.CaptureSessionId(
                            value=client_state.stream_session_id
                        ),
                        voice_session_id=audio_pb2.VoiceSessionId(
                            value=client_state.voice_session_id or ""
                        ),
                        capture_epoch=client_state.capture_epoch,
                    )
                    active_binding = binding
                    v2_streams = await AudioV2Streams.open(
                        producer.redis_client,
                        event=audio_pb2.CaptureStreamEvent(
                            opened=audio_pb2.CaptureStreamOpened(
                                binding=binding,
                                client_id=audio_pb2.ClientId(value=client_id),
                                source_id=hello.source_id,
                                source_spec=start.audio_spec,
                                processing_profile=start.processing_profile,
                                data_purpose=start.data_purpose,
                                memory_space_id=start.memory_space_id,
                            )
                        ),
                        delivery_class=start.delivery_class,
                    )
                    transcript_subscribed = asyncio.Event()
                    interim_task = asyncio.create_task(
                        _subscribe_v2_transcripts(
                            websocket=websocket,
                            redis_client=producer.redis_client,
                            binding=binding,
                            subscribed=transcript_subscribed,
                        )
                    )
                    await transcript_subscribed.wait()
                    job_ids = await asyncio.to_thread(
                        start_streaming_jobs,
                        session_id=client_state.stream_session_id,
                        user_id=user.user_id,
                        client_id=client_id,
                        speech_detection_enabled=(
                            start.data_purpose != audio_pb2.DATA_PURPOSE_ANNOTATION
                        ),
                        contract_version=2,
                    )
                    await producer.update_session_job_ids(
                        session_id=client_state.stream_session_id,
                        speech_detection_job_id=job_ids["speech_detection"],
                        audio_persistence_job_id=job_ids["audio_persistence"],
                    )
                    await send_control(
                        capture_started=audio_pb2.CaptureStarted(
                            binding=binding, audio_spec=start.audio_spec
                        ),
                    )
                elif event == "stop_capture":
                    if control.stop_capture.binding.capture_session_id.value != (
                        client_state.stream_session_id or ""
                    ):
                        raise AudioProtocolV2Error("stop has a stale capture binding")
                    binding = control.stop_capture.binding

                    await runtime.VoiceConversationRuntime(
                        producer.redis_client
                    ).end_for_capture(
                        user_id=str(user.user_id),
                        client_id=client_id,
                        binding=binding,
                        reason="capture_stopped",
                    )
                    await finalize_capture_session(
                        client_state=client_state,
                        producer=producer,
                        user_id=user.user_id,
                        client_id=client_id,
                    )
                    if v2_streams is None:
                        raise AudioProtocolV2Error("capture stream was not opened")
                    await v2_streams.end(
                        audio_pb2.CaptureStreamEvent(
                            ended=audio_pb2.CaptureStreamEnded(
                                binding=binding,
                                reason=control.stop_capture.reason,
                            )
                        )
                    )
                    await send_control(
                        capture_stopped=audio_pb2.CaptureStopped(binding=binding),
                    )
                    if interim_task is not None:
                        interim_task.cancel()
                        await asyncio.gather(interim_task, return_exceptions=True)
                        interim_task = None
                    active_delivery_class = audio_pb2.DELIVERY_CLASS_UNSPECIFIED
                    last_sequence = -1
                    last_monotonic_offset_us = None
                    canonical_sequence = 0
                    normalizer = None
                    v2_streams = None
                    active_binding = None
                elif event == "heartbeat":
                    await send_control(heartbeat=control.heartbeat)
                elif event == "voice_ready":
                    ready = control.voice_ready
                    if (
                        ready.binding.capture_session_id.value
                        != (client_state.stream_session_id or "")
                        or ready.binding.voice_session_id.value
                        != (client_state.voice_session_id or "")
                        or ready.binding.capture_epoch != client_state.capture_epoch
                    ):
                        raise AudioProtocolV2Error("voice-ready has a stale binding")
                    await voice_sessions.ready(
                        voice_session_id=client_state.voice_session_id,
                        user_id=user.user_id,
                        client_id=client_id,
                        audio_session_id=client_state.stream_session_id,
                        capture_epoch=client_state.capture_epoch,
                        socket_id=client_state.socket_id,
                        capabilities=_voice_capabilities(ready.capabilities),
                    )
                elif event == "conversation_command":

                    try:
                        snapshot = await runtime.VoiceConversationRuntime(
                            producer.redis_client
                        ).control(
                            control.conversation_command,
                            user_id=str(user.user_id),
                            client_id=client_id,
                            socket_id=client_state.socket_id,
                            event_id=control.event_id.value,
                        )
                    except (ValueError, VoiceSessionError) as error:
                        # A rejected dialogue action must not tear down durable capture.
                        await send_control(
                            error=audio_pb2.ProtocolError(
                                code=audio_pb2.PROTOCOL_ERROR_CODE_INVALID_TRANSITION,
                                detail=str(error),
                                rejected_event_id=control.event_id,
                            )
                        )
                    else:
                        await send_control(conversation_state=snapshot)
                elif event == "button_event":
                    button_state = {
                        audio_pb2.BUTTON_STATE_SINGLE_PRESS: "SINGLE_PRESS",
                        audio_pb2.BUTTON_STATE_DOUBLE_PRESS: "DOUBLE_PRESS",
                        audio_pb2.BUTTON_STATE_LONG_PRESS: "LONG_PRESS",
                    }.get(control.button_event.state)
                    if button_state is None:
                        raise AudioProtocolV2Error("button event requires a state")
                    await handle_button_event(
                        client_state=client_state,
                        button_state=button_state,
                        user_id=user.user_id,
                        client_id=client_id,
                    )
                else:
                    raise AudioProtocolV2Error(
                        f"unsupported client control during capture: {event}"
                    )
            elif incoming.get("bytes") is not None:
                envelope = parse_media_envelope(incoming["bytes"])
                if envelope.WhichOneof("media") != "capture":
                    raise AudioProtocolV2Error("client may only send capture media")
                packet = envelope.capture
                if packet.delivery_class != active_delivery_class:
                    raise AudioProtocolV2Error(
                        "packet delivery class differs from capture start"
                    )
                if packet.sequence <= last_sequence:
                    raise AudioProtocolV2Error("packet sequence is not increasing")
                if v2_streams is None:
                    raise AudioProtocolV2Error("media arrived before stream open")
                if normalizer is None:
                    raise AudioProtocolV2Error("media arrived before capture start")
                canonical_sequence = await ingest_capture_packet(
                    packet=packet,
                    client_state=client_state,
                    normalizer=normalizer,
                    v2_streams=v2_streams,
                    canonical_sequence=canonical_sequence,
                    previous_monotonic_offset_us=last_monotonic_offset_us,
                )
                await send_control(
                    capture_packet_accepted=audio_pb2.CapturePacketAccepted(
                        binding=packet.binding,
                        sequence=packet.sequence,
                    ),
                )
                last_sequence = packet.sequence
                last_monotonic_offset_us = packet.monotonic_offset_us
            else:
                raise AudioProtocolV2Error("unsupported WebSocket message")
    except WebSocketDisconnect:
        pass
    except AudioProtocolV2Error as error:
        if capture_input is not None:
            await capture_input.close()
        logger.warning(
            "Rejecting audio-v2 client=%s session=%s: %s",
            client_id,
            getattr(client_state, "stream_session_id", None),
            error,
        )
        if (
            client_state is not None
            and client_state.stream_session_id is not None
            and producer is not None
            and v2_streams is not None
            and active_binding is not None
        ):
            await finalize_capture_session(
                client_state=client_state,
                producer=producer,
                user_id=user.user_id,
                client_id=client_id,
                completion_reason="protocol_error",
                failure=str(error),
            )
            await v2_streams.end(
                audio_pb2.CaptureStreamEvent(
                    ended=audio_pb2.CaptureStreamEnded(
                        binding=active_binding,
                        reason=audio_pb2.STOP_REASON_AUDIO_DISCONNECT,
                    )
                )
            )
        try:
            await send_control(
                error=audio_pb2.ProtocolError(
                    code=audio_pb2.PROTOCOL_ERROR_CODE_INVALID_MEDIA,
                    detail=str(error),
                ),
            )
            await websocket.close(code=1008, reason="invalid audio-v2 message")
        except Exception:
            pass
    finally:
        if capture_input is not None:
            await capture_input.close()
        forwarding_tasks = [
            task for task in (interim_task, downlink_task) if task is not None
        ]
        for task in forwarding_tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*forwarding_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning(
                    "Audio-v2 forwarding stopped during socket cleanup: %s", result
                )
        if client_id is not None and client_state is not None:
            if producer is not None and active_binding is not None:

                try:
                    await runtime.VoiceConversationRuntime(
                        producer.redis_client
                    ).end_for_capture(
                        user_id=str(user.user_id),
                        client_id=client_id,
                        binding=active_binding,
                        reason="connection_lost",
                    )
                except Exception:
                    logger.exception("Could not end voice engagement after socket loss")
            await cleanup_client_state(client_id, client_state.socket_id)
