"""Shared voice-command executor and follow-up window state.

Both the wake-word dispatcher (acoustic path) and the streaming follow-up handler
funnel resolved commands through :func:`execute_voice_command`, so command
dispatch, the spoken reply, the live-recording SSE event, and the follow-up
window behave identically regardless of how the command arrived.

The follow-up window is a short, per-session Redis key opened only after a
command actually acted on something. While it is open the streaming
follow-up handler treats the next utterance as a contextual follow-up (see
``followup.py``). Executing a follow-up re-opens the window, so follow-ups
chain naturally ("warmer" ... "warmer" ... "a bit cooler").
"""

import base64
import functools
import io
import json
import logging
import math
import os
import struct
import time
import uuid
import wave
from typing import Any, Awaitable, Callable, List, Optional

import redis.asyncio as redis

import backend.plugins.base as base
from backend.plugins.events import PluginEvent
from backend.plugins.router import PluginRouter
from backend.redis_keys import ClientId, SessionId, device_downlink_channel
from backend.services.audio_stream.session_store import SessionStore
from backend.services.response_coordinator import ResponseCoordinator, StaleResponse
from backend.services.response_delivery import (
    deliver_text_response,
    deliver_wav_response,
)
from backend.services.voice_latency import timing_span
from backend.services.voice_sessions import VoiceSessionCoordinator
from backend.services.wakeword.timing import WakeTimer

logger = logging.getLogger(__name__)

# How long a follow-up window stays open after a successful command (seconds).
FOLLOWUP_WINDOW_SECS = int(os.getenv("FOLLOWUP_WINDOW_SECS", "12"))


_TONE_SAMPLE_RATE = 16000


def _append_note(
    frames: bytearray,
    freq: float,
    ms: int,
    volume: float,
    *,
    envelope: str = "hann",
    harmonics: Optional[List[tuple]] = None,
) -> None:
    """Append one int16 note to ``frames``.

    ``envelope``: "hann" gives a smooth raised-cosine swell (soft, click-free,
    inviting); "blip" gives a quick 5ms attack/decay (snappy, attention-grabbing).
    ``harmonics`` is a list of ``(freq_multiple, amplitude)`` partials added on top
    of the fundamental — a little 2nd harmonic warms a pure sine without harshness.
    """
    n = int(_TONE_SAMPLE_RATE * ms / 1000)
    if n <= 0:
        return
    partials = harmonics or [(1.0, 1.0)]
    attack = max(1, int(_TONE_SAMPLE_RATE * 0.005))
    for i in range(n):
        if envelope == "hann":
            env = 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1))) if n > 1 else 1.0
        else:  # "blip"
            env = min(1.0, i / attack, (n - i) / attack)
        sample = sum(
            amp * math.sin(2 * math.pi * freq * mult * i / _TONE_SAMPLE_RATE)
            for mult, amp in partials
        )
        sample *= volume * env
        frames.extend(struct.pack("<h", max(-32768, min(32767, int(sample * 32767)))))


def _silence(frames: bytearray, ms: int) -> None:
    frames.extend(b"\x00\x00" * int(_TONE_SAMPLE_RATE * ms / 1000))


def _wav_b64(frames: bytearray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_TONE_SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode("ascii")


@functools.lru_cache(maxsize=1)
def _thinking_tone_b64() -> str:
    """Soft, inviting two-note rising swell for the agentic handoff.

    Played when a fast handler (Home Assistant) declines and a slower one (the
    Hermes agent) takes over, so the unavoidable wait reads as a warm "hold on,
    working on it" rather than an alert. Low register, gentle Hann swells, quiet,
    with a touch of 2nd harmonic for warmth — deliberately the opposite of the
    sharp error tone.
    """
    frames = bytearray()
    # Gentle rising perfect-fourth (G4 → C5), smooth swells, soft.
    _append_note(
        frames, 392.00, 200, 0.20, envelope="hann", harmonics=[(1.0, 1.0), (2.0, 0.12)]
    )
    _silence(frames, 45)
    _append_note(
        frames, 523.25, 260, 0.20, envelope="hann", harmonics=[(1.0, 1.0), (2.0, 0.10)]
    )
    return _wav_b64(frames)


@functools.lru_cache(maxsize=1)
def _error_tone_b64() -> str:
    """Sharp two-blip cue for a mistake / not-found / failure.

    The crisper, more attention-grabbing tone — appropriate when something didn't
    work (command not understood, target not found, a handler errored).
    """
    frames = bytearray()
    _append_note(frames, 660.0, 90, 0.30, envelope="blip")
    _silence(frames, 60)
    _append_note(frames, 880.0, 110, 0.30, envelope="blip")
    return _wav_b64(frames)


@functools.lru_cache(maxsize=1)
def _armed_tone_b64() -> str:
    """Short rising cue confirming that command capture has started."""
    frames = bytearray()
    _append_note(frames, 523.25, 85, 0.22, envelope="blip")
    _silence(frames, 25)
    _append_note(frames, 783.99, 110, 0.22, envelope="blip")
    return _wav_b64(frames)


@functools.lru_cache(maxsize=1)
def _done_tone_b64() -> str:
    """Short falling cue confirming that command capture has ended."""
    frames = bytearray()
    _append_note(frames, 783.99, 75, 0.20, envelope="blip")
    _silence(frames, 20)
    _append_note(frames, 523.25, 95, 0.20, envelope="blip")
    return _wav_b64(frames)


# Logical tone name -> base64 WAV generator.
_TONE_GENERATORS = {
    "armed": _armed_tone_b64,
    "done": _done_tone_b64,
    "thinking": _thinking_tone_b64,  # soft handoff to the agentic path
    "error": _error_tone_b64,  # mistake / not-found / failure
}


async def play_tone_on_device(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    tone: str = "thinking",
    *,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
) -> None:
    """Play a bound notification tone through the response coordinator."""
    if not isinstance(client_id, ClientId):
        raise TypeError("play_tone_on_device requires ClientId")
    if not isinstance(session_id, SessionId):
        raise TypeError("play_tone_on_device requires SessionId")
    generator = _TONE_GENERATORS.get(tone)
    if generator is None:
        logger.debug(f"Unknown tone '{tone}'; not playing")
        return
    encoded = generator()
    if not encoded:
        return

    async def load_tone() -> bytes:
        return base64.b64decode(encoded)

    try:
        await deliver_wav_response(
            redis_client,
            client_id,
            session_id,
            load_tone,
            kind="tone",
            generation=generation,
            turn_id=turn_id,
            turn_revision=turn_revision,
            barge_in_allowed=False,
        )
        logger.info(f"🔔 Sent '{tone}' tone to device {client_id}")
    except Exception as e:  # noqa: BLE001 - tone output is best-effort
        logger.debug(f"Failed to publish '{tone}' tone downlink for {client_id}: {e}")


# LED ring feedback colours (0..1 RGB) the firmware tints its animation with.
# "Error" is hardcoded red on-device, so its colour is irrelevant.
_LED_LISTEN_COLOR = {"r": 0.09, "g": 0.73, "b": 0.95}  # cyan
_LED_THINK_COLOR = {"r": 1.0, "g": 0.45, "b": 0.0}  # amber


async def set_device_led(
    redis_client: redis.Redis,
    client_id: ClientId,
    *,
    effect: str,
    color: Optional[dict] = None,
    brightness: float = 0.45,
    duration: float = 10.0,
) -> None:
    """Drive an LED-capable device's ring to a named effect via its downlink.

    Visual analogue of :func:`play_tone_on_device`: the ``led-control`` frame goes
    to every client type, but only the HAVPE relay acts on it (other clients ignore
    unknown downlink types). The firmware reverts to its connectivity colour after
    ``duration`` seconds. Best-effort; never raises.
    """
    if not isinstance(client_id, ClientId):
        raise TypeError("set_device_led requires ClientId")
    data: dict[str, Any] = {
        "effect": effect,
        "brightness": brightness,
        "duration": duration,
    }
    if color:
        data.update(color)
    msg = {"type": "led-control", "data": data}
    try:
        await redis_client.publish(
            str(device_downlink_channel(client_id)), json.dumps(msg)
        )
    except Exception as e:  # noqa: BLE001 - LED feedback is best-effort
        logger.debug(f"Failed to publish led-control downlink for {client_id}: {e}")


def _ctx_key(session_id: str | SessionId) -> str:
    return f"followup:ctx:{session_id}"


async def open_followup_window(
    redis_client: redis.Redis, session_id: str | SessionId, command: str
) -> None:
    """Open/refresh the follow-up window, recording the command just executed."""
    if not session_id:
        return
    payload = json.dumps({"command": command, "ts": time.time()})
    await redis_client.set(_ctx_key(session_id), payload, ex=FOLLOWUP_WINDOW_SECS)


async def get_followup_ctx(
    redis_client: redis.Redis, session_id: str | SessionId
) -> Optional[dict]:
    """Return the open follow-up context for a session, or None if the window is closed."""
    if not session_id:
        return None
    raw = await redis_client.get(_ctx_key(session_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def clear_followup_window(
    redis_client: redis.Redis, session_id: str | SessionId
) -> None:
    if not session_id:
        return
    await redis_client.delete(_ctx_key(session_id))


async def get_active_conversation_id(
    redis_client: redis.Redis, session_id: str | SessionId
) -> Optional[str]:
    """Resolve the active conversation id for a session, if any."""
    if not session_id:
        return None
    try:
        return await SessionStore(redis_client).get_active_conversation_id(session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read current conversation for {session_id}: {e}")
    return None


async def publish_sse(
    redis_client: redis.Redis, user_id: str, event_type: str, data: dict
) -> None:
    """Publish an SSE event to the user's channel (best-effort, never raises)."""
    if not user_id:
        return
    try:
        message = json.dumps(
            {"event": event_type, "data": data, "timestamp": time.time()}
        )
        await redis_client.publish(f"sse:{user_id}", message)
    except Exception as e:  # noqa: BLE001 - SSE is best-effort, never break dispatch
        logger.debug(f"Failed to publish SSE {event_type}: {e}")


async def speak_on_device(
    redis_client: redis.Redis,
    client_id: ClientId,
    session_id: SessionId,
    text: str,
    timer: Optional[WakeTimer] = None,
    *,
    generation: int | None = None,
    turn_id: str | None = None,
    turn_revision: int = 0,
    wake_trace_id: str | None = None,
) -> None:
    """Synthesize and deliver one fully bound coordinated response."""
    if not isinstance(client_id, ClientId):
        raise TypeError("speak_on_device requires ClientId")
    if not isinstance(session_id, SessionId):
        raise TypeError("speak_on_device requires SessionId")
    await deliver_text_response(
        redis_client,
        client_id,
        session_id,
        text,
        generation=generation,
        turn_id=turn_id,
        turn_revision=turn_revision,
        timer=timer,
        wake_trace_id=wake_trace_id,
    )


async def execute_voice_command(
    redis_client: redis.Redis,
    plugin_router: PluginRouter,
    *,
    user_id: str,
    session_id: SessionId,
    client_id: ClientId,
    command: str,
    conversation_id: Optional[str] = None,
    source: str = "wake",
    asr_status: str = "transcribed",
    has_speech: bool = True,
    wakeword: Optional[str] = None,
    also_fired: Optional[List[str]] = None,
    score: Optional[float] = None,
    reason: Optional[str] = None,
    capture_secs: Optional[float] = None,
    asr_ms: Optional[float] = None,
    wake_trace_id: Optional[str] = None,
    quiet: bool = False,
    response_generation: int | None = None,
    response_turn_id: str | None = None,
    response_turn_revision: int = 0,
    interaction_stage_callback: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """Dispatch a voice command, speak the reply, emit SSE, and manage the window.

    ``source`` is "wake" for acoustic wake commands and "followup" for follow-ups.
    Returns the spoken reply (may be empty). Opens/refreshes the follow-up window
    iff a plugin actually acted (any result success), satisfying the "only follow
    up after an action was done" rule.

    ``capture_secs`` and ``asr_ms`` are pre-dispatch stage durations supplied by
    the acoustic path (the wakeword-service capture window and the batch ASR), so
    the per-command latency line covers the full pipeline. A :class:`WakeTimer`
    traces the rest (per-plugin routing, TTS, downlink) and emits one structured
    log line when the command finishes. When the fast handler (Home Assistant)
    declines and a slower one takes over, a "thinking" tone plays on the device so
    the agentic wait reads as intentional.
    """
    if not isinstance(session_id, SessionId):
        raise TypeError("execute_voice_command requires SessionId")
    if not isinstance(client_id, ClientId):
        raise TypeError("execute_voice_command requires ClientId")
    session_id_value = str(session_id)
    client_id_value = str(client_id)
    timer = WakeTimer(
        session_id=session_id_value,
        source=source,
        asr_status=asr_status,
        command=command,
        capture_secs=capture_secs,
        asr_ms=asr_ms,
    )
    responses = ResponseCoordinator(redis_client, VoiceSessionCoordinator(redis_client))
    if response_generation is not None:
        try:
            await responses.assert_generation(
                user_id, client_id_value, response_generation
            )
        except StaleResponse:
            return ""
    capture_view = await SessionStore(redis_client).read(session_id_value)
    allow_handoff_tone = bool(
        capture_view is not None and not capture_view.voice_session_id
    )
    # Play the handoff "thinking" tone at most once per command — the instant the
    # first handler declines (should_continue / None result) and another handler
    # is still queued to run.
    handoff_tone_sent = False

    async def _on_plugin_done(
        plugin_id: str, duration_ms: float, result, *, is_last: bool
    ) -> None:
        nonlocal handoff_tone_sent
        timer.record_plugin(plugin_id, duration_ms, result)
        declined = result is None or getattr(result, "should_continue", False)
        if response_generation is not None:
            await responses.assert_generation(
                user_id, client_id_value, response_generation
            )
        if declined and not is_last and not handoff_tone_sent and allow_handoff_tone:
            handoff_tone_sent = True
            await play_tone_on_device(
                redis_client,
                client_id,
                session_id,
                "thinking",
                generation=response_generation,
                turn_id=response_turn_id,
                turn_revision=response_turn_revision,
            )

    data: dict[str, Any] = {
        "dialogue_input_id": response_turn_id or wake_trace_id or str(uuid.uuid4()),
        "response_generation": response_generation,
        "command": command,
        "client_id": client_id_value,
        "session_id": session_id_value,
        "conversation_id": conversation_id,
        "wakeword": wakeword,
        "also_fired": also_fired or [],
        "score": score,
        "reason": reason,
        "asr_status": asr_status,
        "transcript": command,  # alias for plugins that read transcript
        "source": source,
        "wake_trace_id": wake_trace_id,
    }

    logger.info(
        f"🎙️ Executing voice command (source={source}, user={user_id}, "
        f"session={session_id_value}, command='{command[:50]}')"
    )
    # "Thinking" ring while we dispatch — the agent path (e.g. Hermes) can take many
    # seconds, so the wait should read as intentional. Refreshes the end-of-turn cue.
    # Suppressed for quiet sources (e.g. the dial), whose feedback is the result
    # itself (the lights changing) plus the device's own local dial animation.
    if not quiet:
        await set_device_led(
            redis_client,
            client_id,
            effect="Thinking",
            color=_LED_THINK_COLOR,
            duration=15.0,
        )
    try:
        _dispatch_start = time.perf_counter()
        try:
            async with timing_span("routing"):
                # Defer this dependency to break the import cycle through
                # backend.services.dialogue.capture -> backend.services.wakeword.executor.
                from backend.services.dialogue.capture import accept

                routed = None
                if (
                    has_speech
                    and command.strip()
                    and capture_view
                    and capture_view.voice_session_id
                ):
                    routed = await accept(
                        redis_client,
                        user_id=user_id,
                        client_id=client_id_value,
                        capture_id=session_id_value,
                        input_id=data["dialogue_input_id"],
                        text=command,
                        generation=response_generation,
                    )
                results = (
                    [
                        base.PluginResult(
                            success=True,
                            message="",
                            should_continue=False,
                            data={"thread_id": routed.id},
                        )
                    ]
                    if routed
                    else await plugin_router.dispatch_event(
                        event=PluginEvent.WAKE_WORD_DETECTED,
                        user_id=user_id,
                        data=data,
                        metadata={
                            "client_id": client_id_value,
                            "session_id": session_id_value,
                            "conversation_id": conversation_id,
                            "command": command,
                            "wakeword": wakeword,
                            "asr_status": asr_status,
                            "has_speech": has_speech,
                            "score": score,
                            "reason": reason,
                            "source": source,
                            "wake_trace_id": wake_trace_id,
                        },
                        on_plugin_done=_on_plugin_done,
                    )
                )
            if response_generation is not None:
                await responses.assert_generation(
                    user_id, client_id_value, response_generation
                )
        except StaleResponse:
            logger.info(
                "Suppressed superseded voice command for %s generation %s",
                client_id_value,
                response_generation,
            )
            return ""
        timer.dispatch_ms = (time.perf_counter() - _dispatch_start) * 1000.0

        reply = (
            next((r.message for r in results if getattr(r, "message", None)), "") or ""
        )
        acted = any(getattr(r, "success", False) for r in results)
        if interaction_stage_callback is not None:
            await interaction_stage_callback(
                "dispatched",
                {
                    "plugin_result_count": len(results),
                    "acted": acted,
                    "dispatch_ms": timer.dispatch_ms,
                    "plugins": [
                        {
                            "plugin_id": span.plugin_id,
                            "duration_ms": span.duration_ms,
                            "handled": span.handled,
                            "success": span.success,
                        }
                        for span in timer.plugin_spans
                    ],
                },
            )
            if acted:
                await interaction_stage_callback("acted", {"acted": True})

        await publish_sse(
            redis_client,
            user_id,
            "wake.command",
            {
                "command": command,
                "reply": reply,
                "conversation_id": conversation_id,
                "client_id": client_id_value,
                "asr_status": asr_status,
                "source": source,
            },
        )

        if reply and not quiet:
            await speak_on_device(
                redis_client,
                client_id,
                session_id,
                reply,
                timer,
                generation=response_generation,
                turn_id=response_turn_id,
                turn_revision=response_turn_revision,
                wake_trace_id=wake_trace_id,
            )

        # Only arm follow-ups when the command actually did something.
        if acted:
            await open_followup_window(redis_client, session_id, command)
            if interaction_stage_callback is not None:
                await interaction_stage_callback(
                    "followup_opened", {"window_secs": FOLLOWUP_WINDOW_SECS}
                )
            # Tell the UI a follow-up window is open (no wake word needed for the
            # next utterance). window_secs lets the client self-expire the indicator.
            await publish_sse(
                redis_client,
                user_id,
                "wake.followup",
                {
                    "open": True,
                    "window_secs": FOLLOWUP_WINDOW_SECS,
                    "client_id": client_id_value,
                    "command": command,
                },
            )
            # "Listening" ring for the open follow-up window (no wake word needed).
            if not quiet:
                await set_device_led(
                    redis_client,
                    client_id,
                    effect="Listening For Command",
                    color=_LED_LISTEN_COLOR,
                    duration=float(FOLLOWUP_WINDOW_SECS),
                )

        return reply
    finally:
        timer.log()
