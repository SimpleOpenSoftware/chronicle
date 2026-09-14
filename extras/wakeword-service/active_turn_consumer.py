"""Active-turn consumer for generated Chronicle audio-v2 realtime frames."""

from __future__ import annotations

import asyncio
import importlib.resources as ir
import json
import logging
import os
import time
from collections.abc import Callable

import numpy as np
import redis.asyncio as redis
from audio_contract.v2 import audio_pb2
from pipecat.audio.turn.smart_turn.base_smart_turn import (
    EndOfTurnState,
    SmartTurnParams,
)
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroOnnxModel
from redis import exceptions as redis_exceptions

from turn_segmenter import TurnEvent, TurnFrame, TurnPolicy, TurnSegmenter

logger = logging.getLogger(__name__)

FRAME_STREAM_PATTERN = "audio:v2:realtime:*"
GROUP_NAME = "interactive-turn-v2"
COMMITTED_TURNS_STREAM = "voice:turns:committed"
TURN_EVENTS_STREAM = "voice:turns:events"
DISCOVERY_SECONDS = 0.5
PENDING_CLAIM_MIN_IDLE_MS = 30_000
PENDING_RECOVERY_INTERVAL_SECONDS = 5.0
VAD_FRAME_SAMPLES = 512
SAMPLE_RATE = 16_000


class SileroSmartTurnModels:
    """Per-session Silero and Smart Turn state used by the active consumer."""

    def __init__(
        self,
        *,
        smart_turn_model_path: str | None,
        silero_vad_model_path: str | None,
        vad_threshold: float = 0.5,
    ):
        if silero_vad_model_path is None:
            silero_vad_model_path = str(
                ir.files("pipecat.audio.vad.data").joinpath("silero_vad.onnx")
            )
        self.vad = SileroOnnxModel(silero_vad_model_path, force_onnx_cpu=True)
        self.analyzer = LocalSmartTurnAnalyzerV3(
            smart_turn_model_path=smart_turn_model_path,
            params=SmartTurnParams(stop_secs=4.0),
        )
        self.analyzer.set_sample_rate(SAMPLE_RATE)
        self.vad_threshold = vad_threshold
        self.remainder = np.empty(0, dtype=np.int16)
        self.last_speech = False

    async def evaluate(self, pcm: bytes) -> tuple[bool, bool | None]:
        audio = np.frombuffer(pcm, dtype=np.int16)
        buffered = (
            np.concatenate([self.remainder, audio]) if self.remainder.size else audio
        )
        full_size = (buffered.size // VAD_FRAME_SAMPLES) * VAD_FRAME_SAMPLES
        self.remainder = buffered[full_size:].copy()
        saw_frame = False
        speech = False
        semantic_complete: bool | None = None
        for offset in range(0, full_size, VAD_FRAME_SAMPLES):
            saw_frame = True
            frame = buffered[offset : offset + VAD_FRAME_SAMPLES]
            confidence = float(
                np.asarray(
                    self.vad(frame.astype(np.float32) / 32768.0, SAMPLE_RATE)
                ).flatten()[0]
            )
            is_speech = confidence >= self.vad_threshold
            speech = speech or is_speech
            backstop = self.analyzer.append_audio(frame.tobytes(), is_speech)
            if backstop == EndOfTurnState.COMPLETE:
                semantic_complete = True
            elif not is_speech and self.last_speech:
                model_state, _ = await self.analyzer.analyze_end_of_turn()
                semantic_complete = model_state == EndOfTurnState.COMPLETE
            self.last_speech = is_speech
        return (speech if saw_frame else self.last_speech), semantic_complete


class ActiveTurnConsumer:
    """Consume bounded ephemeral frames without becoming capture evidence."""

    def __init__(
        self,
        *,
        redis_client=None,
        redis_url: str | None = None,
        model_factory: Callable[[], object] | None = None,
        smart_turn_model_path: str | None = None,
        silero_vad_model_path: str | None = None,
        vad_threshold: float = 0.5,
        monotonic_ms: Callable[[], float] | None = None,
    ):
        if redis_client is None and redis_url is None:
            raise ValueError("redis_client or redis_url is required")
        self.redis_client = redis_client
        self.redis_url = redis_url
        self.model_factory = model_factory or (
            lambda: SileroSmartTurnModels(
                smart_turn_model_path=smart_turn_model_path,
                silero_vad_model_path=silero_vad_model_path,
                vad_threshold=vad_threshold,
            )
        )
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic() * 1000)
        self.running = False
        self.consumer_name = f"active-turn-{os.getpid()}"
        self._tasks: dict[str, asyncio.Task] = {}
        self._segmenters: dict[str, TurnSegmenter] = {}
        self._models: dict[str, object] = {}
        self._data_purposes: dict[str, str] = {}
        self._last_frame_offsets_ms: dict[str, float] = {}
        self._last_frame_received_ms: dict[str, float] = {}
        self.frames_consumed = 0
        self.turns_committed = 0
        self.error_count = 0
        self.last_success_at: float | None = None
        self.last_consumed_id: str | None = None

    def health(self) -> dict:
        return {
            "running": self.running,
            "active_streams": sum(not task.done() for task in self._tasks.values()),
            "frames_consumed": self.frames_consumed,
            "turns_committed": self.turns_committed,
            "error_count": self.error_count,
            "last_success_at": self.last_success_at,
            "last_consumed_id": self.last_consumed_id,
        }

    async def start(self) -> None:
        if self.redis_client is None:
            self.redis_client = redis.from_url(self.redis_url)
        self.running = True
        try:
            while self.running:
                try:
                    await self._discover()
                    await self.flush_due()
                    await asyncio.sleep(DISCOVERY_SECONDS)
                except asyncio.CancelledError:
                    raise
                except redis_exceptions.RedisError as error:
                    self.error_count += 1
                    logger.warning("Active-turn Redis error: %s", error)
                    await asyncio.sleep(1)
        finally:
            for task in self._tasks.values():
                task.cancel()
            if self.redis_url and self.redis_client is not None:
                await self.redis_client.aclose()

    async def stop(self) -> None:
        self.running = False

    async def _discover(self) -> None:
        cursor = 0
        streams: list[str] = []
        while True:
            cursor, keys = await self.redis_client.scan(
                cursor, match=FRAME_STREAM_PATTERN, count=100
            )
            streams.extend(
                key.decode() if isinstance(key, bytes) else key for key in keys
            )
            if cursor in {0, b"0", "0"}:
                break
        for stream in streams:
            task = self._tasks.get(stream)
            if task is None or task.done():
                self._tasks[stream] = asyncio.create_task(self._consume(stream))

    async def _consume(self, stream: str) -> None:
        try:
            await self.redis_client.xgroup_create(
                stream, GROUP_NAME, "0", mkstream=True
            )
        except redis_exceptions.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        await self.recover_pending(stream)
        last_pending_recovery = time.monotonic()
        while self.running:
            if (
                time.monotonic() - last_pending_recovery
                >= PENDING_RECOVERY_INTERVAL_SECONDS
            ):
                await self.recover_pending(stream)
                last_pending_recovery = time.monotonic()
            messages = await self.redis_client.xreadgroup(
                GROUP_NAME,
                self.consumer_name,
                {stream: ">"},
                count=20,
                block=1000,
            )
            for _stream, entries in messages or []:
                for message_id, fields in entries:
                    await self._handle_entry(stream, message_id, fields)

    async def recover_pending(
        self,
        stream: str,
        *,
        claim_min_idle_ms: int = PENDING_CLAIM_MIN_IDLE_MS,
    ) -> int:
        """Replay this consumer's pending frames, then claim frames from dead peers."""
        recovered = 0
        while True:
            messages = await self.redis_client.xreadgroup(
                GROUP_NAME,
                self.consumer_name,
                {stream: "0"},
                count=20,
            )
            entries = [entry for _stream, batch in messages or [] for entry in batch]
            if not entries:
                break
            for message_id, fields in entries:
                if not await self._handle_entry(stream, message_id, fields):
                    return recovered
                recovered += 1

        cursor = "0-0"
        for _ in range(100):
            response = await self.redis_client.xautoclaim(
                stream,
                GROUP_NAME,
                self.consumer_name,
                claim_min_idle_ms,
                start_id=cursor,
                count=20,
            )
            cursor, entries = response[0], response[1]
            if not entries:
                break
            for message_id, fields in entries:
                if not await self._handle_entry(stream, message_id, fields):
                    return recovered
                recovered += 1
            if cursor in {"0-0", b"0-0"}:
                break
        return recovered

    async def _handle_entry(self, stream: str, message_id, fields: dict) -> bool:
        try:
            if set(fields) != {b"event"}:
                raise ValueError("audio-v2 turn entry has untyped fields")
            event = audio_pb2.CaptureStreamEvent()
            event.ParseFromString(fields[b"event"])
            event_kind = event.WhichOneof("event")
            if event_kind == "frame":
                data_purpose = {
                    audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE: "normal_capture",
                    audio_pb2.DATA_PURPOSE_ANNOTATION: "annotation",
                }.get(event.frame.data_purpose)
                if data_purpose is None:
                    raise ValueError("audio-v2 frame has no data purpose")
                if event.frame.binding.voice_session_id.value:
                    await self.handle_frame(
                        event.frame,
                        data_purpose=data_purpose,
                    )
        except Exception:
            logger.exception("Active-turn frame failed on %s", stream)
            return False
        await self.redis_client.xack(stream, GROUP_NAME, message_id)
        self.last_consumed_id = (
            message_id.decode() if isinstance(message_id, bytes) else message_id
        )
        return True

    async def handle_frame(
        self,
        frame_event: audio_pb2.CanonicalPcmFrame,
        *,
        data_purpose: str,
    ) -> None:
        try:
            voice_session_id = frame_event.binding.voice_session_id.value
            audio_session_id = frame_event.binding.capture_session_id.value
            capture_epoch = frame_event.binding.capture_epoch
            sequence = frame_event.sequence
            offset_ms = frame_event.monotonic_offset_us / 1000.0
            sample_rate = SAMPLE_RATE
            pcm = frame_event.pcm_s16le
            sample_count = len(pcm) // 2
            if not voice_session_id or not audio_session_id or not pcm:
                raise ValueError("active-turn frame has empty identity or PCM")
            if data_purpose not in {"normal_capture", "annotation"}:
                raise ValueError("active-turn frame has invalid data_purpose")
            previous_purpose = self._data_purposes.get(voice_session_id)
            if previous_purpose is not None and previous_purpose != data_purpose:
                raise ValueError("active-turn session changed data_purpose")
            self._data_purposes[voice_session_id] = data_purpose
            duration_ms = sample_count * 1000 / sample_rate
            segmenter = self._segmenters.setdefault(
                voice_session_id, TurnSegmenter(TurnPolicy.conversational())
            )
            models = self._models.get(voice_session_id)
            if models is None:
                models = self.model_factory()
                self._models[voice_session_id] = models
            speech, semantic_complete = await models.evaluate(pcm)
            frame = TurnFrame(
                voice_session_id=voice_session_id,
                audio_session_id=audio_session_id,
                capture_epoch=capture_epoch,
                sequence=sequence,
                monotonic_offset_ms=offset_ms,
                duration_ms=duration_ms,
                pcm=pcm,
                speech=speech,
                captured_at_ms=frame_event.captured_at.ToMilliseconds(),
                device_monotonic_ms=(
                    frame_event.device_monotonic_timestamp_us / 1000
                    if frame_event.HasField("device_monotonic_timestamp_us")
                    else None
                ),
            )
            events = await segmenter.push(frame, semantic_complete=semantic_complete)
            await self._publish_events(events, data_purpose=data_purpose)
            self._last_frame_offsets_ms[voice_session_id] = frame.end_ms
            self._last_frame_received_ms[voice_session_id] = self.monotonic_ms()
            self.frames_consumed += 1
            self.last_success_at = time.time()
        except Exception:
            self.error_count += 1
            raise

    async def flush_due(self) -> None:
        now_ms = self.monotonic_ms()
        for voice_session_id, segmenter in self._segmenters.items():
            last_offset_ms = self._last_frame_offsets_ms.get(voice_session_id)
            last_received_ms = self._last_frame_received_ms.get(voice_session_id)
            if last_offset_ms is None or last_received_ms is None:
                continue
            session_offset_ms = last_offset_ms + max(0, now_ms - last_received_ms)
            await self._publish_events(
                await segmenter.advance(session_offset_ms),
                data_purpose=self._data_purposes[voice_session_id],
            )

    async def _publish_events(
        self,
        events: list[TurnEvent],
        *,
        data_purpose: str,
    ) -> None:
        if data_purpose == "annotation":
            return
        for event in events:
            metadata = {
                "kind": event.kind,
                "turn_id": event.turn_id,
                "turn_revision": str(event.revision),
                "voice_session_id": event.voice_session_id,
                "audio_session_id": event.audio_session_id,
                "capture_epoch": str(event.capture_epoch),
                "start_sequence": str(event.start_sequence),
                "end_sequence": str(event.end_sequence),
                "started_at_ms": str(event.started_at_ms),
                "ended_at_ms": str(event.ended_at_ms),
                "reason": event.reason,
            }
            metadata.update(
                {
                    key: str(value)
                    for key, value in {
                        "speech_started_device_ms": event.speech_started_device_ms,
                        "speech_ended_device_ms": event.speech_ended_device_ms,
                        "speech_started_at_ms": event.speech_started_at_ms,
                        "speech_ended_at_ms": event.speech_ended_at_ms,
                    }.items()
                    if value is not None
                }
            )
            metadata["committed_at_ms"] = str(time.time() * 1000)
            await self.redis_client.xadd(
                TURN_EVENTS_STREAM,
                {"event": json.dumps(metadata, separators=(",", ":"))},
                maxlen=10_000,
                approximate=True,
            )
            if event.kind == "committed":
                await self.redis_client.xadd(
                    COMMITTED_TURNS_STREAM,
                    {
                        **metadata,
                        "sample_rate": str(SAMPLE_RATE),
                        "channels": "1",
                        "sample_width": "2",
                        "pcm": event.pcm,
                    },
                )
                self.turns_committed += 1
