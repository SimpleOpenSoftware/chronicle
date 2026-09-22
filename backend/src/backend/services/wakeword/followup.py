"""Streaming-transcript follow-up handling.

After a successful wake command opens a follow-up window (see ``executor.py``),
the user can issue a bare follow-up — "warmer", "a bit dimmer", "no, the kitchen
instead" — with no wake word. The always-on streaming transcription consumer
calls :func:`maybe_handle_followup` on each final result; if a window is open and
the utterance isn't the wake echo, we resolve it into a standalone command and
run it through the same path as a wake command.

Resolution is hybrid:
  - a fast rules path rewrites common light adjustments, inheriting the target
    room from the previous command (no LLM), then
  - a GPT-5.5 (minimal-reasoning) fallback handles arbitrary phrasing, corrections,
    and validity ("warmer" doesn't apply after "turn on the fans").
"""

import json
import logging
import os
import re
import uuid as uuid
from typing import Optional

import redis.asyncio as redis

import backend.services.dialogue.capture as capture
from backend.llm_client import async_generate
from backend.plugins.router import PluginRouter, normalize_text_for_wake_word
from backend.redis_keys import ClientId, SessionId
from backend.services.wakeword.executor import (
    execute_voice_command,
    get_active_conversation_id,
    get_followup_ctx,
)

logger = logging.getLogger(__name__)

# Spoken wake words. A streaming transcript containing one of these is the
# original wake interaction (or a fresh one), never a bare follow-up.
_WAKE_WORDS = [
    normalize_text_for_wake_word(w)
    for w in os.getenv("FOLLOWUP_WAKE_WORDS", "hey hermes,hermes").split(",")
    if w.strip()
]

# Common light adjustments handled without an LLM. The rewritten command still
# flows through the full HA cascade, so exact phrasing tolerance lives there.
_LIGHT_ADJUSTMENTS = [
    "warmer",
    "cooler",
    "brighter",
    "dimmer",
    "darker",
    "lighter",
    "softer",
    "too bright",
    "too dark",
    "more light",
    "less light",
    "brighten",
    "dim",
]

# Room/label words, multi-word first so "living room" wins over "living".
_AREA_WORDS = [
    "living room",
    "dining room",
    "study",
    "office",
    "bedroom",
    "kitchen",
    "hall",
    "living",
    "dining",
]

_LLM_PROMPT = """You interpret a FOLLOW-UP utterance in a smart-home voice assistant.

The user previously said: "{last}"
That command succeeded. They then said: "{follow_up}"

Decide how to handle the follow-up:
- If it refines/adjusts the previous action, output the FULL standalone command,
  reusing the same target/room as before (e.g. previous "make the living room
  lights warmer" + "a bit dimmer" -> "make the living room lights dimmer").
- If it is a brand-new command unrelated to the previous one, output it standalone.
- If it does NOT make sense as a follow-up to that action (e.g. asking to make
  fans "warmer", adjusting brightness of lights that were turned off), or it is
  just conversation rather than a command, choose not_applicable.

Respond with JSON only:
{{"action": "adjust" | "new" | "not_applicable", "command": "<standalone command, or empty>"}}"""


def _normalize(text: str) -> str:
    return normalize_text_for_wake_word(text or "").strip()


def _contains_wake_word(text_norm: str) -> bool:
    return any(w and w in text_norm for w in _WAKE_WORDS)


def _rules_resolve(last_norm: str, follow_up_norm: str) -> Optional[str]:
    """Fast, no-LLM rewrite for clear light adjustments. None -> defer to LLM."""
    if "light" not in last_norm:
        return None
    # Lights that were turned off can't be made warmer/brighter.
    if "turn off" in last_norm or re.search(r"\boff\b", last_norm):
        return None
    if not any(adj in follow_up_norm for adj in _LIGHT_ADJUSTMENTS):
        return None
    # Keep it to short adjustment utterances; richer phrasing goes to the LLM.
    if len(follow_up_norm.split()) > 4:
        return None
    area = next((a for a in _AREA_WORDS if a in last_norm), None)
    if area:
        return f"make the {area} lights {follow_up_norm}"
    return f"make the lights {follow_up_norm}"


def _parse_llm_json(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        logger.warning("follow-up LLM returned non-JSON: %r", text[:200])
        return None


async def _llm_resolve(last_command: str, follow_up: str) -> Optional[str]:
    prompt = _LLM_PROMPT.format(last=last_command, follow_up=follow_up)
    try:
        raw = await async_generate(prompt, operation="followup_resolution")
    except Exception as e:  # noqa: BLE001 - degrade gracefully if the LLM is down
        logger.warning("follow-up LLM resolve failed: %s", e)
        return None
    data = _parse_llm_json(raw)
    if not data:
        return None
    action = (data.get("action") or "").strip().lower()
    command = (data.get("command") or "").strip()
    if action == "not_applicable" or not command:
        return None
    return command


async def resolve_followup(last_command: str, follow_up: str) -> Optional[str]:
    """Resolve a follow-up utterance into a standalone command, or None to ignore."""
    last_norm = _normalize(last_command)
    follow_up_norm = _normalize(follow_up)
    rewritten = _rules_resolve(last_norm, follow_up_norm)
    if rewritten:
        return rewritten
    return await _llm_resolve(last_command, follow_up)


async def maybe_handle_followup(
    redis_client: redis.Redis,
    plugin_router: PluginRouter,
    *,
    user_id: str,
    session_id: str,
    client_id: str,
    text: str,
) -> bool:
    """Handle ``text`` as a follow-up if a window is open. Returns True if consumed.

    When True, the caller should NOT run normal transcript-event dispatch for this
    result (we've taken responsibility for it).
    """
    text = (text or "").strip()
    if not text:
        return False
    ctx = await get_followup_ctx(redis_client, session_id)
    if not ctx:
        return False
    last_command = ctx.get("command", "")
    text_norm = _normalize(text)
    last_norm = _normalize(last_command)

    # Echo guard: the streaming transcript of the original wake interaction
    # contains the full command (and the wake word); a real follow-up does not.
    if last_norm and last_norm in text_norm:
        logger.debug("Follow-up skip: echo of last command (%r)", text)
        return False
    if _contains_wake_word(text_norm):
        logger.debug("Follow-up skip: contains wake word (%r)", text)
        return False

    routed = await capture.accept(
        redis_client,
        user_id=user_id,
        client_id=client_id,
        capture_id=session_id,
        input_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"followup:{session_id}:{ctx.get('ts')}:{text}"
            )
        ),
        text=text,
    )
    if routed:
        return True
    resolved = await resolve_followup(last_command, text)
    if not resolved:
        logger.info("Follow-up %r not applicable to %r", text, last_command)
        return True  # consume — it was meant for us, just nothing to do

    logger.info("Follow-up %r -> %r (prev=%r)", text, resolved, last_command)
    conversation_id = await get_active_conversation_id(redis_client, session_id)
    session_ref = SessionId.from_value(session_id)
    client_ref = ClientId.from_value(client_id)
    await execute_voice_command(
        redis_client,
        plugin_router,
        user_id=user_id,
        session_id=session_ref,
        client_id=client_ref,
        command=resolved,
        conversation_id=conversation_id,
        source="followup",
    )
    return True


# ─────────────────────────── Dial (physical) follow-ups ───────────────────────
#
# A rotary detent during an open follow-up window is a *physical* follow-up: it
# carries no words, so we synthesize the adjustment word from the dial direction
# and the axis the last command was about, then run it through the same path as a
# spoken "warmer"/"dimmer". CW = "more", CCW = "less".

# Direction -> adjustment word, per axis.
_DIAL_WORDS = {
    "color_temp": {"CW": "warmer", "CCW": "cooler"},
    "brightness": {"CW": "brighter", "CCW": "dimmer"},
}

# Words in the last command that mean "the dial should adjust colour temperature".
# Anything else about lights defaults to brightness (the intuitive dial action,
# and what "turn on"/"brighten study lights" implies).
_COLOR_TEMP_HINTS = (
    "warm",
    "cool",
    "colour temp",
    "color temp",
    "temperature",
    "white",
)


def _followup_axis(last_command_norm: str) -> str:
    """Pick the axis the dial controls (color_temp | brightness) from last command."""
    if any(h in last_command_norm for h in _COLOR_TEMP_HINTS):
        return "color_temp"
    return "brightness"


async def handle_dial_followup(
    redis_client: redis.Redis,
    plugin_router: PluginRouter,
    *,
    user_id: str,
    session_id: str,
    client_id: str,
    direction: str,
) -> dict:
    """Map a dial detent to a contextual light adjustment during the follow-up window.

    Only acts while a follow-up window is open AND the last command was about
    lights. Returns a small status dict (handled/resolved/axis/reason) so the
    simulation endpoint can surface what happened; the live WS handler just logs.
    """
    direction = (direction or "").strip().upper()
    if direction not in ("CW", "CCW"):
        return {"handled": False, "reason": f"unknown direction {direction!r}"}

    ctx = await get_followup_ctx(redis_client, session_id)
    if not ctx:
        # No open window — the dial means nothing to the assistant right now.
        return {"handled": False, "reason": "no follow-up window open"}

    last_command = ctx.get("command", "")
    last_norm = _normalize(last_command)
    # Lights only, for now. (A future axis could carry the target/axis explicitly
    # in the follow-up context so non-light devices are dial-adjustable too.)
    if "light" not in last_norm:
        logger.info(
            "Dial follow-up ignored: last command not about lights (%r)", last_command
        )
        return {"handled": False, "reason": "last command not about lights"}

    axis = _followup_axis(last_norm)
    word = _DIAL_WORDS[axis][direction]
    area = next((a for a in _AREA_WORDS if a in last_norm), None)
    resolved = f"make the {area} lights {word}" if area else f"make the lights {word}"

    logger.info(
        "Dial %s -> %r (axis=%s, prev=%r)", direction, resolved, axis, last_command
    )
    conversation_id = await get_active_conversation_id(redis_client, session_id)
    session_ref = SessionId.from_value(session_id)
    client_ref = ClientId.from_value(client_id)
    # quiet=True: no spoken "warmer" per detent and no backend LED effect (the
    # lights changing is the feedback; the device shows its own local dial ring).
    # source="followup" re-opens the window on success, so consecutive turns work.
    await execute_voice_command(
        redis_client,
        plugin_router,
        user_id=user_id,
        session_id=session_ref,
        client_id=client_ref,
        command=resolved,
        conversation_id=conversation_id,
        source="followup",
        quiet=True,
    )
    return {"handled": True, "resolved": resolved, "axis": axis, "reason": "ok"}
