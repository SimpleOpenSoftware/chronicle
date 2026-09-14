"""Route only complete audio-v2 turns into interaction modes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from backend.redis_keys import SessionId, transcription_results_stream
from backend.services.audio_stream.session_store import SessionStore
from backend.services.response_coordinator import ResponseCoordinator
from backend.services.transcription import get_transcription_provider
from backend.services.voice_latency import (
    TimingIdentity,
    VoiceTrace,
    mark_timing,
    timing_span,
)
from backend.services.voice_sessions import VoiceSessionCoordinator
from backend.services.wakeword.activations import WakeActivation, WakeActivationStore
from backend.utils.audio_utils import pcm_to_wav_bytes

from .contracts import AudioInterval
from .episode_claims import AudioEpisodeArbiter
from .ingress import InteractionIngress, InteractionIngressResult
from .registry import InteractionRegistry

COMMITTED_TURNS_STREAM = "voice:turns:committed"
GROUP_NAME = "committed-turn-router"
CONSUMER_NAME = "interaction-mode-worker"
STT_POLL_SECONDS = 0.05
STT_WATERMARK_WAIT_SECONDS = 1.5
PENDING_CLAIM_MIN_IDLE_MS = 130_000
PENDING_RECOVERY_INTERVAL_SECONDS = 15

logger = logging.getLogger(__name__)


def _value(fields: dict, key: str):
    value = fields.get(key) or fields.get(key.encode())
    return value.decode() if isinstance(value, bytes) and key != "pcm" else value


@dataclass(frozen=True)
class CommittedAudioTurn:
    interval: AudioInterval
    start_sequence: int
    end_sequence: int
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int
    speech_started_device_ms: float | None = None
    speech_ended_device_ms: float | None = None
    speech_started_at_ms: float | None = None
    speech_ended_at_ms: float | None = None
    committed_at_ms: float | None = None

    @classmethod
    def from_fields(cls, fields: dict) -> "CommittedAudioTurn":
        required = {
            key: _value(fields, key)
            for key in (
                "turn_id",
                "turn_revision",
                "voice_session_id",
                "audio_session_id",
                "capture_epoch",
                "start_sequence",
                "end_sequence",
                "started_at_ms",
                "ended_at_ms",
                "sample_rate",
                "channels",
                "sample_width",
                "pcm",
            )
        }
        missing = [key for key, value in required.items() if value is None]
        if missing:
            raise ValueError("committed turn missing fields: " + ", ".join(missing))
        pcm = required["pcm"]
        if not isinstance(pcm, bytes) or not pcm:
            raise ValueError("committed turn PCM must be non-empty bytes")
        interval = AudioInterval(
            audio_session_id=str(required["audio_session_id"]),
            capture_epoch=int(required["capture_epoch"]),
            start_ms=float(required["started_at_ms"]),
            end_ms=float(required["ended_at_ms"]),
            voice_session_id=str(required["voice_session_id"]),
            turn_id=str(required["turn_id"]),
            turn_revision=int(required["turn_revision"]),
        )
        return cls(
            interval=interval,
            start_sequence=int(required["start_sequence"]),
            end_sequence=int(required["end_sequence"]),
            pcm=pcm,
            sample_rate=int(required["sample_rate"]),
            channels=int(required["channels"]),
            sample_width=int(required["sample_width"]),
            **{
                key: float(_value(fields, key))
                for key in (
                    "speech_started_device_ms",
                    "speech_ended_device_ms",
                    "speech_started_at_ms",
                    "speech_ended_at_ms",
                    "committed_at_ms",
                )
                if _value(fields, key) is not None
            },
        )


@dataclass(frozen=True)
class TranscriptResolution:
    text: str
    source: str
    watermark_ms: float


ExactTranscriber = Callable[[bytes, int, int, int], Awaitable[str]]
CommittedCommandDispatcher = Callable[
    [CommittedAudioTurn, str, str, str, int, WakeActivation], Awaitable[None]
]


class CommittedTranscriptAssembler:
    """Wait for a final STT watermark or transcribe the exact committed PCM."""

    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        exact_transcriber: ExactTranscriber | None = None,
        watermark_wait_seconds: float = STT_WATERMARK_WAIT_SECONDS,
    ):
        self.redis = redis_client
        self.exact_transcriber = exact_transcriber or self._batch_transcribe
        self.watermark_wait_seconds = watermark_wait_seconds

    async def resolve(self, turn: CommittedAudioTurn) -> TranscriptResolution:
        async with timing_span("stt"):
            async with timing_span("stt_wait", detail="streaming_final_watermark"):
                text, watermark_ms = await self._wait_for_final(turn)
            if text:
                return TranscriptResolution(text, "streaming_final", watermark_ms)
            async with timing_span("stt_batch", detail="exact_range_batch"):
                text = await self.exact_transcriber(
                    turn.pcm,
                    turn.sample_rate,
                    turn.channels,
                    turn.sample_width,
                )
            return TranscriptResolution(text.strip(), "exact_range_batch", watermark_ms)

    async def _wait_for_final(self, turn: CommittedAudioTurn) -> tuple[str, float]:
        deadline = time.monotonic() + self.watermark_wait_seconds
        watermark_ms = 0.0
        while True:
            words, watermark_ms = await self._final_words(
                turn.interval.audio_session_id
            )
            if watermark_ms >= turn.interval.end_ms:
                selected = [
                    word
                    for word in words
                    if float(word.get("end", 0)) * 1000 > turn.interval.start_ms
                    and float(word.get("start", 0)) * 1000 < turn.interval.end_ms
                ]
                text = " ".join(
                    str(word.get("word") or word.get("punctuated_word") or "").strip()
                    for word in selected
                ).strip()
                if text:
                    return text, watermark_ms
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(STT_POLL_SECONDS)

        return "", watermark_ms

    async def _final_words(self, audio_session_id: str) -> tuple[list[dict], float]:
        stream = str(
            transcription_results_stream(
                SessionId.from_value(audio_session_id, "audio_session_id")
            )
        )
        words: list[dict] = []
        watermark_ms = 0.0
        for _message_id, fields in await self.redis.xrange(stream):
            raw = _value(fields, "words")
            if not raw:
                continue
            parsed = json.loads(raw)
            for word in parsed:
                if not isinstance(word, dict):
                    continue
                end = word.get("end")
                if end is None:
                    continue
                words.append(word)
                watermark_ms = max(watermark_ms, float(end) * 1000)
        return words, watermark_ms

    @staticmethod
    async def _batch_transcribe(
        pcm: bytes, sample_rate: int, channels: int, sample_width: int
    ) -> str:
        provider = get_transcription_provider(mode="batch")
        if provider is None:
            raise RuntimeError("No batch transcription provider configured")
        result = await provider.transcribe(
            pcm_to_wav_bytes(pcm, sample_rate, channels, sample_width),
            sample_rate,
            priority=True,
        )
        return str(result.get("text") or "")


class CommittedTurnRouter:
    """Validate the full binding, assemble complete text, then claim and enqueue."""

    def __init__(
        self,
        redis_client: redis.Redis,
        registry: InteractionRegistry,
        *,
        transcript_assembler: CommittedTranscriptAssembler | None = None,
        command_dispatcher: CommittedCommandDispatcher | None = None,
        activation_store: WakeActivationStore | None = None,
    ):
        self.redis = redis_client
        self.ingress = InteractionIngress(redis_client, registry)
        self.transcripts = transcript_assembler or CommittedTranscriptAssembler(
            redis_client
        )
        self.command_dispatcher = command_dispatcher
        self.activations = activation_store or WakeActivationStore(redis_client)
        self.running = False

    async def route(self, fields: dict) -> InteractionIngressResult:
        turn = CommittedAudioTurn.from_fields(fields)
        session = await SessionStore(self.redis).read(turn.interval.audio_session_id)
        if session is None:
            raise ValueError("committed turn has no audio session")
        voice = await VoiceSessionCoordinator(self.redis).get(
            turn.interval.voice_session_id or ""
        )
        if voice is None or voice.state not in {
            "ready_full",
            "ready_isolated",
            "ready_half",
        }:
            raise ValueError("committed turn voice session is not ready")
        if (
            session.user_id != voice.user_id
            or session.client_id != voice.client_id
            or session.connection_id != voice.socket_id
            or session.voice_session_id != voice.voice_session_id
            or session.capture_epoch != voice.capture_epoch
            or session.session_id != voice.audio_session_id
            or turn.interval.voice_session_id != voice.voice_session_id
            or turn.interval.capture_epoch != voice.capture_epoch
            or turn.interval.audio_session_id != voice.audio_session_id
        ):
            raise ValueError(
                "committed turn does not match authenticated capture binding"
            )

        trace = VoiceTrace(
            self.redis, TimingIdentity.from_turn(turn, voice.user_id, voice.client_id)
        )
        await trace.emit("turn_received")
        if turn.committed_at_ms is not None:
            await trace.emit(
                "turn_committed",
                timestamp_ms=turn.committed_at_ms,
                observed_at_ms=turn.committed_at_ms,
                clock_domain="wall:wake-service",
            )
        for stage, value, wall in (
            (
                "speech_started",
                turn.speech_started_device_ms,
                turn.speech_started_at_ms,
            ),
            ("speech_ended", turn.speech_ended_device_ms, turn.speech_ended_at_ms),
        ):
            if value is not None:
                await trace.emit(
                    stage,
                    timestamp_ms=value,
                    observed_at_ms=wall,
                    clock_domain=trace.identity.device_clock,
                    detail="vad_frame_estimate",
                )
        with trace.bind():
            try:
                return await self._route_turn(turn, voice)
            except Exception as error:
                await trace.emit(
                    "turn_failed", outcome="failed", detail=type(error).__name__
                )
                raise

    async def _route_turn(self, turn, voice):
        claimed = await AudioEpisodeArbiter(self.redis).claim(
            user_id=voice.user_id,
            client_id=voice.client_id,
            interval=turn.interval,
            source="committed",
        )
        if not claimed.accepted:
            return InteractionIngressResult(
                consumed=True,
                accepted=False,
                reason="episode_already_claimed",
            )
        transcript = await self.transcripts.resolve(turn)
        if not transcript.text:
            await mark_timing("turn_ignored", detail="empty_transcript")
            return InteractionIngressResult(
                consumed=False, reason="empty_exact_transcript"
            )
        result = await self.ingress.submit(
            user_id=voice.user_id,
            client_id=voice.client_id,
            audio_interval=turn.interval,
            text=transcript.text,
            source="committed",
            episode_claim=claimed.claim,
        )
        if not result.consumed:
            activation = await self.activations.claim(turn.interval)
            if activation is not None and self.command_dispatcher is not None:
                response_generation = await ResponseCoordinator(
                    self.redis, VoiceSessionCoordinator(self.redis)
                ).begin_turn(voice.user_id, voice.client_id)
                await self.command_dispatcher(
                    turn,
                    transcript.text,
                    voice.user_id,
                    voice.client_id,
                    response_generation,
                    activation,
                )
                return InteractionIngressResult(
                    consumed=True,
                    accepted=True,
                    reason="wake_command",
                )
            await mark_timing("turn_ignored", detail="not_addressed")
            return InteractionIngressResult(
                consumed=True,
                accepted=False,
                reason="not_addressed",
            )
        return result

    async def run(self) -> None:
        try:
            await self.redis.xgroup_create(
                COMMITTED_TURNS_STREAM,
                GROUP_NAME,
                "0",
                mkstream=True,
            )
        except redis_exceptions.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        self.running = True
        await self.recover_pending()
        last_pending_recovery = time.monotonic()
        while self.running:
            if (
                time.monotonic() - last_pending_recovery
                >= PENDING_RECOVERY_INTERVAL_SECONDS
            ):
                await self.recover_pending()
                last_pending_recovery = time.monotonic()
            messages = await self.redis.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {COMMITTED_TURNS_STREAM: ">"},
                count=10,
                block=1000,
            )
            for _stream, entries in messages or []:
                for message_id, fields in entries:
                    await self._handle_entry(message_id, fields)

    async def recover_pending(
        self, *, claim_min_idle_ms: int = PENDING_CLAIM_MIN_IDLE_MS
    ) -> int:
        """Replay this consumer's deliveries, then claim work stranded by peers."""
        recovered = 0
        while True:
            messages = await self.redis.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {COMMITTED_TURNS_STREAM: "0"},
                count=10,
            )
            entries = [entry for _stream, batch in messages or [] for entry in batch]
            if not entries:
                break
            for message_id, fields in entries:
                await self._handle_entry(message_id, fields)
                recovered += 1

        cursor = "0-0"
        for _ in range(100):
            response = await self.redis.xautoclaim(
                COMMITTED_TURNS_STREAM,
                GROUP_NAME,
                CONSUMER_NAME,
                claim_min_idle_ms,
                start_id=cursor,
                count=10,
            )
            cursor, entries = response[0], response[1]
            if not entries:
                break
            for message_id, fields in entries:
                await self._handle_entry(message_id, fields)
                recovered += 1
            if cursor in {"0-0", b"0-0"}:
                break
        return recovered

    async def _handle_entry(self, message_id, fields: dict) -> None:
        try:
            await self.route(fields)
        except ValueError as exc:
            # Schema/binding failures are permanent poison entries. Acknowledge
            # them after logging so one stale turn cannot kill and restart the
            # process forever.
            logger.warning("Dropping invalid committed voice turn: %s", exc)
            await self.redis.xack(COMMITTED_TURNS_STREAM, GROUP_NAME, message_id)
        except Exception as exc:  # noqa: BLE001 - retry transient dependencies later
            # Keep transient failures pending for the bounded recovery sweep, but
            # isolate them from the long-lived worker loop.
            logger.error(
                "Committed voice turn routing failed; leaving it pending: %s",
                exc,
                exc_info=True,
            )
        else:
            await self.redis.xack(COMMITTED_TURNS_STREAM, GROUP_NAME, message_id)

    async def stop(self) -> None:
        self.running = False
