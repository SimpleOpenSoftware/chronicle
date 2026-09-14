"""One shared Chronicle audio V2 WebSocket adapter for Python capture clients."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

import websockets
from audio_contract.v2 import audio_pb2
from google.protobuf import duration_pb2, json_format, timestamp_pb2

ControlHandler = Callable[[audio_pb2.ServerControl], Awaitable[None]]
PlaybackHandler = Callable[[audio_pb2.PlaybackMediaPacket], Awaitable[None]]


def _timestamp(epoch_seconds: float) -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromMilliseconds(round(epoch_seconds * 1_000))
    return value


def _opus_spec(sample_rate_hz: int, frame_duration_ms: int) -> audio_pb2.AudioSpec:
    return audio_pb2.AudioSpec(
        codec=audio_pb2.AUDIO_CODEC_OPUS,
        sample_rate_hz=sample_rate_hz,
        channel_count=1,
        frame_duration=duration_pb2.Duration(nanos=frame_duration_ms * 1_000_000),
        bitrate_bps=24_000,
    )


class AudioV2Client:
    """Own connection, capture binding, sequencing, and generated wire encoding."""

    def __init__(
        self,
        *,
        websocket_url: str,
        bearer_token: str,
        source_id: str,
        display_name: str,
        device_kind: int,
        uplink_frame_duration_ms: int,
        ssl_context=None,
        on_control: ControlHandler | None = None,
        on_playback: PlaybackHandler | None = None,
    ) -> None:
        if uplink_frame_duration_ms not in {20, 60}:
            raise ValueError("uplink_frame_duration_ms must be 20 or 60")
        self.websocket_url = websocket_url
        self.bearer_token = bearer_token
        self.source_id = source_id
        self.display_name = display_name
        self.device_kind = device_kind
        self.uplink_frame_duration_ms = uplink_frame_duration_ms
        self.ssl_context = ssl_context
        self.on_control = on_control
        self.on_playback = on_playback
        self.websocket = None
        self.binding: audio_pb2.CaptureBinding | None = None
        self._receive_task: asyncio.Task | None = None
        self._waiters: dict[str, asyncio.Future] = {}
        self._sequence = 0
        self._delivery_class = audio_pb2.DELIVERY_CLASS_UNSPECIFIED
        self._started_monotonic = 0.0
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        connect_options = dict(
            subprotocols=["chronicle.audio.v2"],
            ping_interval=20,
            ping_timeout=120,
            close_timeout=10,
        )
        if self.ssl_context is not None:
            connect_options["ssl"] = self.ssl_context
        self.websocket = await websockets.connect(self.websocket_url, **connect_options)
        self._receive_task = asyncio.create_task(self._receive())
        hello = self._wait_for("hello")
        await self._send_control(
            hello=audio_pb2.ClientHello(
                bearer_token=self.bearer_token,
                source_id=audio_pb2.CaptureSourceId(value=self.source_id),
                device_kind=self.device_kind,
                display_name=self.display_name,
                supported_uplink=[_opus_spec(16_000, self.uplink_frame_duration_ms)],
                supported_downlink=[_opus_spec(24_000, 20)],
            )
        )
        await hello

    async def start_capture(
        self,
        *,
        capture_epoch: int,
        processing_profile: int = audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose: int = audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
        delivery_class: int = audio_pb2.DELIVERY_CLASS_LIVE,
        capabilities: audio_pb2.CaptureCapabilities | None = None,
        recovery_batch_id: str = "",
    ) -> audio_pb2.CaptureBinding:
        started = self._wait_for("capture_started")
        await self._send_control(
            start_capture=audio_pb2.StartCapture(
                capture_epoch=capture_epoch,
                processing_profile=processing_profile,
                data_purpose=data_purpose,
                delivery_class=delivery_class,
                audio_spec=_opus_spec(16_000, self.uplink_frame_duration_ms),
                capabilities=capabilities,
                recovery_batch_id=recovery_batch_id,
            )
        )
        control = await started
        self.binding = control.capture_started.binding
        self._sequence = 0
        self._delivery_class = delivery_class
        self._started_monotonic = time.monotonic()
        return self.binding

    async def send_opus(
        self, payload: bytes, *, captured_at: float | None = None
    ) -> None:
        if self.binding is None or self.websocket is None:
            raise RuntimeError("capture is not active")
        packet = audio_pb2.MediaEnvelope(
            capture=audio_pb2.CaptureMediaPacket(
                binding=self.binding,
                sequence=self._sequence,
                captured_at=_timestamp(captured_at or time.time()),
                monotonic_offset_us=round(
                    (time.monotonic() - self._started_monotonic) * 1_000_000
                ),
                delivery_class=self._delivery_class,
                opus_payload=payload,
            )
        )
        self._sequence += 1
        async with self._send_lock:
            await self.websocket.send(packet.SerializeToString(deterministic=True))

    async def voice_ready(self, capabilities: audio_pb2.CaptureCapabilities) -> None:
        if self.binding is None:
            raise RuntimeError("capture is not active")
        await self._send_control(
            voice_ready=audio_pb2.VoiceReady(
                binding=self.binding, capabilities=capabilities
            )
        )

    async def send_button(self, state: int) -> None:
        if state == audio_pb2.BUTTON_STATE_UNSPECIFIED:
            raise ValueError("button state must be specified")
        await self._send_control(button_event=audio_pb2.ButtonEvent(state=state))

    async def acknowledge_playback(
        self,
        *,
        response_id: str,
        generation: int,
        state: int,
        monotonic_timestamp: float | None = None,
    ) -> None:
        if self.binding is None:
            raise RuntimeError("playback acknowledgement requires active capture")
        await self._send_control(
            playback_acknowledgement=audio_pb2.PlaybackAcknowledgement(
                binding=self.binding,
                response_id=audio_pb2.ResponseId(value=response_id),
                generation=generation,
                state=state,
                monotonic_timestamp_us=round(
                    (monotonic_timestamp or time.monotonic()) * 1_000_000
                ),
            )
        )

    async def stop_capture(self) -> None:
        if self.binding is None:
            return
        stopped = self._wait_for("capture_stopped")
        await self._send_control(
            stop_capture=audio_pb2.StopCapture(
                binding=self.binding,
                reason=audio_pb2.STOP_REASON_USER_REQUESTED,
            )
        )
        await stopped
        self.binding = None

    async def close(self) -> None:
        if self.binding is not None and self.websocket is not None:
            try:
                await self.stop_capture()
            except (ConnectionError, websockets.WebSocketException):
                self.binding = None
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except websockets.WebSocketException:
                pass
            self.websocket = None
        if self._receive_task is not None:
            await asyncio.gather(self._receive_task, return_exceptions=True)
            self._receive_task = None

    async def _send_control(self, **event) -> None:
        if self.websocket is None:
            raise RuntimeError("audio V2 socket is not connected")
        control = audio_pb2.ClientControl(
            event_id=audio_pb2.EventId(value=str(uuid.uuid4())),
            sent_at=_timestamp(time.time()),
            **event,
        )
        rendered = json_format.MessageToJson(
            control, preserving_proto_field_name=True, indent=None
        )
        async with self._send_lock:
            await self.websocket.send(rendered)

    async def _receive(self) -> None:
        try:
            async for raw in self.websocket:
                if isinstance(raw, bytes):
                    envelope = audio_pb2.MediaEnvelope.FromString(raw)
                    if envelope.WhichOneof("media") == "playback" and self.on_playback:
                        await self.on_playback(envelope.playback)
                    continue
                control = audio_pb2.ServerControl()
                json_format.Parse(raw, control, ignore_unknown_fields=False)
                kind = control.WhichOneof("event")
                if kind == "error":
                    error = RuntimeError(control.error.detail)
                    for waiter in self._waiters.values():
                        if not waiter.done():
                            waiter.set_exception(error)
                    self._waiters.clear()
                    continue
                waiter = self._waiters.pop(kind, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(control)
                if self.on_control:
                    await self.on_control(control)
        finally:
            error = ConnectionError("audio V2 socket closed")
            for waiter in self._waiters.values():
                if not waiter.done():
                    waiter.set_exception(error)
            self._waiters.clear()

    def _wait_for(self, kind: str) -> asyncio.Future:
        if kind in self._waiters:
            raise RuntimeError(f"already waiting for {kind}")
        future = asyncio.get_running_loop().create_future()
        self._waiters[kind] = future
        return future
