"""One text/WAV delivery facade over coordinated phone and wearable transports."""

from __future__ import annotations

import time
import uuid
from contextlib import nullcontext
from typing import TYPE_CHECKING, Awaitable, Callable

import redis.asyncio as redis

from backend.redis_keys import ClientId, SessionId
from backend.services.audio_stream.session_store import SessionStore
from backend.services.playback_audio import encode_wav_for_playback
from backend.services.response_coordinator import (
    ResponseCoordinator,
    ResponseRecord,
    StaleResponse,
)
from backend.services.tts_client import synthesize_speech
from backend.services.voice_latency import TimingIdentity, VoiceTrace
from backend.services.voice_sessions import (
    ClientUpgradeRequired,
    VoiceSessionCoordinator,
)

if TYPE_CHECKING:
    from backend.services.wakeword.timing import WakeTimer

WavOperation = Callable[[], Awaitable[bytes]]


async def deliver_wav_response(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    operation: WavOperation,
    *,
    kind: str,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
    barge_in_allowed: bool = True,
    timer: WakeTimer | None = None,
    wake_trace_id: str | None = None,
) -> ResponseRecord:
    """Fence production, routing, and delivery against one authenticated target."""

    if not isinstance(client_id, ClientId):
        raise TypeError("deliver_wav_response requires ClientId")
    if not isinstance(session_id, SessionId):
        raise TypeError("deliver_wav_response requires SessionId")
    view = await SessionStore(redis_client).read(str(session_id))
    if (
        view is None
        or view.client_id != str(client_id)
        or not view.user_id
        or not view.connection_id
    ):
        raise StaleResponse("response target is not an authenticated audio session")
    if not view.voice_session_id:
        raise ClientUpgradeRequired(
            "interactive responses require an audio-v2 voice binding"
        )

    voice_sessions = VoiceSessionCoordinator(redis_client)
    coordinator = ResponseCoordinator(redis_client, voice_sessions)
    if generation is None:
        generation = await coordinator.begin_turn(
            view.user_id,
            view.client_id,
            reason="replacement",
        )
    response_turn_id = turn_id or str(uuid.uuid4())
    trace_id = str(uuid.uuid4())

    voice = await voice_sessions.get(view.voice_session_id)
    if voice is None:
        raise StaleResponse("audio-v2 response has no voice session")
    response = await coordinator.queue(
        user_id=view.user_id,
        client_id=view.client_id,
        audio_session_id=view.session_id,
        voice_session_id=view.voice_session_id,
        capture_epoch=view.capture_epoch,
        socket_id=view.connection_id,
        turn_id=response_turn_id,
        turn_revision=turn_revision,
        generation=generation,
        kind=kind,
        barge_in_allowed=barge_in_allowed if kind == "speech" else False,
        trace_id=trace_id,
        causation_id=wake_trace_id or response_turn_id,
        wake_trace_id=wake_trace_id,
    )

    trace = VoiceTrace(
        redis_client,
        TimingIdentity.from_response(response),
        response_id=response.response_id,
        generation=response.generation,
    )
    if kind != "speech":
        trace = None
    started = time.perf_counter()
    try:
        async with trace.span("tts") if trace else nullcontext():
            wav = await coordinator.synthesize(response.response_id, operation)
        if timer is not None:
            timer.tts_ms = (time.perf_counter() - started) * 1000
        async with trace.span("encoding") if trace else nullcontext():
            playback = encode_wav_for_playback(wav)
        duration_ms = playback.duration_ms
        await coordinator.mark_ready(
            response.response_id,
            byte_length=sum(len(packet) for packet in playback.packets),
            duration_ms=duration_ms,
            sample_rate=24_000,
        )
        async with trace.span("downlink") if trace else nullcontext():
            delivered = await coordinator.offer(response.response_id, playback.packets)
        if timer is not None:
            timer.est_play_secs = duration_ms / 1000
            timer.mark_downlink()
        return delivered
    except Exception as error:
        try:
            await coordinator.fail(response.response_id, type(error).__name__)
        except StaleResponse:
            pass
        raise


async def deliver_text_response(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    text: str,
    *,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
    timer: WakeTimer | None = None,
    wake_trace_id: str | None = None,
) -> ResponseRecord | None:
    if not text:
        return None

    async def synthesize() -> bytes:
        return await synthesize_speech(text)

    return await deliver_wav_response(
        redis_client,
        client_id,
        session_id,
        synthesize,
        kind="speech",
        generation=generation,
        turn_id=turn_id,
        turn_revision=turn_revision,
        barge_in_allowed=True,
        timer=timer,
        wake_trace_id=wake_trace_id,
    )
