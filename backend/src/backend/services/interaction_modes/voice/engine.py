"""Bounded modular speech generation using Chronicle's existing LLM and TTS.

The engine owns only one generation. The caller owns engagement, cancellation,
tool execution, heard context, and the response's persistent Opus encoder. Close
the iterator when abandoning output (``contextlib.aclosing`` is recommended).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

import backend.llm_client as llm_client
import backend.services.playback_audio as playback_audio
import backend.services.tts_client as tts_client
from backend.services.voice_diagnostics import cadence_span

from .engine_types import (
    VoiceActivityObserver,
    VoiceAudio,
    VoiceCompleted,
    VoiceEngineError,
    VoiceEngineEvent,
    VoiceEngineSettings,
    VoiceText,
    VoiceToolCall,
)
from .speech_text import SpeechTextNormalizer

LLMStream = Callable[..., AsyncIterator[dict[str, Any]]]
Synthesizer = Callable[[str], Awaitable[bytes | None]]
_T = TypeVar("_T")
_SENTENCE_END = re.compile(r"[.!?](?:\s+|$)|\n+")
_SYSTEM = (
    "You are Chronicle's conversational voice assistant. Reply naturally and "
    "concisely in the user's language (English, Hindi or Hinglish), usually one or two short sentences. Use plain spoken "
    "text without Markdown. Use Hindi script for Hindi words in Hinglish replies. Never speak hidden reasoning. Use only the supplied "
    "tools when their capabilities are needed. Do not claim a task succeeded "
    "before its tool result arrives."
)


@dataclass(frozen=True)
class _Phrase:
    text: str


@dataclass(frozen=True)
class _PreparedPhrase:
    text: str
    pcm: bytes


@dataclass(frozen=True)
class _Finished:
    text: str
    finish_reason: str


@dataclass(frozen=True)
class _Failure:
    error: Exception


async def _with_cancellation(
    operation: Awaitable[_T], cancellation: asyncio.Event | None
) -> _T:
    """Interrupt even a stalled provider, and await its cleanup before returning."""
    task = asyncio.ensure_future(operation)
    cancelled = (
        asyncio.create_task(cancellation.wait()) if cancellation is not None else None
    )
    try:
        if cancelled is None:
            return await task
        if cancellation.is_set():
            raise asyncio.CancelledError
        await asyncio.wait((task, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cancelled.done():
            raise asyncio.CancelledError
        return task.result()
    finally:
        pending = [task] + ([cancelled] if cancelled is not None else [])
        for item in pending:
            if not item.done():
                item.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _take_phrase(pending: str, limit: int, *, final: bool = False) -> tuple[str, str]:
    boundary = _SENTENCE_END.search(pending)
    if boundary and boundary.end() <= limit:
        cut = boundary.end()
    elif len(pending) >= limit:
        cut = pending.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
    elif final and pending:
        cut = len(pending)
    else:
        return "", pending
    return pending[:cut].strip(), pending[cut:].lstrip()


def _tool_call(value: Mapping[str, Any], allowed_names: set[str]) -> VoiceToolCall:
    function = value.get("function")
    if not isinstance(function, Mapping):
        raise VoiceEngineError("voice model returned an invalid tool call")
    name, call_id = function.get("name"), value.get("id")
    if name not in allowed_names or not isinstance(call_id, str) or not call_id:
        raise VoiceEngineError("voice model returned an unadvertised or unnamed tool")
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (TypeError, ValueError) as exc:
        raise VoiceEngineError("voice model returned malformed tool arguments") from exc
    if not isinstance(arguments, dict):
        raise VoiceEngineError("voice tool arguments must be an object")
    return VoiceToolCall(call_id=call_id, name=name, arguments=arguments)


class ModularVoiceEngine:
    def __init__(
        self,
        *,
        llm_stream: LLMStream | None = None,
        synthesize: Synthesizer | None = None,
        settings: VoiceEngineSettings | None = None,
    ) -> None:
        self._llm_stream = llm_stream
        self._synthesize = synthesize
        self.settings = settings or VoiceEngineSettings()

    async def speak(self, text, *, cancellation=None):
        """Render a committed utterance through the same bounded TTS pipeline."""

        async def committed(**_kwargs):
            yield {"type": "content", "text": text}
            yield {
                "type": "done",
                "content": text,
                "finish_reason": "stop",
                "tool_calls": [],
            }

        renderer = ModularVoiceEngine(
            llm_stream=committed, synthesize=self._synthesize, settings=self.settings
        )
        async with aclosing(
            renderer.generate(text=text, cancellation=cancellation)
        ) as events:
            async for event in events:
                yield event

    async def generate(
        self,
        *,
        text: str | None,
        history: Sequence[Mapping[str, Any]] = (),
        tool_schemas: Sequence[Mapping[str, Any]] = (),
        cancellation: asyncio.Event | None = None,
        activity: VoiceActivityObserver | None = None,
        generated: Callable[[str], None] | None = None,
    ) -> AsyncIterator[VoiceEngineEvent]:
        """Generate a response; tools are returned to the caller without execution.

        ``history`` excludes the new utterance when ``text`` is supplied. For a
        tool-result continuation, pass ``text=None`` and include assistant tool
        calls and their tool messages in history. No user message is then added.
        """

        if self._llm_stream is None:

            llm_stream = llm_client.async_chat_with_tools_stream
        else:
            llm_stream = self._llm_stream
        if self._synthesize is None:

            async def synthesize(value):
                return await tts_client.synthesize_speech(
                    value, language=normalizer.language
                )

        else:
            synthesize = self._synthesize

        messages = [{"role": "system", "content": _SYSTEM}] + [
            dict(message) for message in history
        ]
        if text is not None:
            if not text.strip():
                raise ValueError("voice utterance must not be empty")
            messages.append({"role": "user", "content": text})
        elif not history:
            raise ValueError("voice continuation requires history")
        schemas = [dict(schema) for schema in tool_schemas]
        allowed_names = {
            schema["function"]["name"]
            for schema in schemas
            if isinstance(schema.get("function"), Mapping)
            and isinstance(schema["function"].get("name"), str)
        }

        @contextmanager
        def observing(stage):
            if activity is not None:
                activity(stage, True)
            try:
                yield
            finally:
                if activity is not None:
                    activity(stage, False)

        language_text = text or next(
            (
                str(m.get("content", ""))
                for m in reversed(history)
                if m.get("role") == "user"
            ),
            "",
        )
        normalizer = SpeechTextNormalizer(
            limit=self.settings.max_response_chars,
            language=(
                "hi" if any("\u0900" <= c <= "\u097f" for c in language_text) else "en"
            ),
        )
        queue: asyncio.Queue[_Phrase | VoiceToolCall | _Finished | _Failure] = (
            asyncio.Queue(maxsize=1)
        )

        async def produce() -> None:
            pending = ""
            content: list[str] = []
            content_length = 0
            try:
                async with aclosing(
                    llm_stream(
                        messages=messages,
                        tools=schemas or None,
                        operation=self.settings.operation,
                        allow_fallback=False,
                    )
                ) as events:
                    while True:
                        try:
                            with observing("generating_text"), cadence_span("llm_next"):
                                event = await asyncio.wait_for(
                                    anext(events),
                                    self.settings.provider_timeout_seconds,
                                )
                        except StopAsyncIteration:
                            raise VoiceEngineError(
                                "voice model ended without a completion event"
                            ) from None
                        if event.get("type") == "content":
                            delta = event.get("text", "")
                            if not isinstance(delta, str):
                                raise VoiceEngineError("invalid voice content delta")
                            content_length += len(delta)
                            if content_length > self.settings.max_response_chars:
                                raise VoiceEngineError(
                                    "voice response exceeds text bound"
                                )
                            content.append(delta)
                            if generated is not None:
                                generated(delta)
                            pending += normalizer.feed(delta)
                            while pending:
                                phrase, remainder = _take_phrase(
                                    pending, self.settings.max_phrase_chars
                                )
                                if remainder == pending:
                                    break
                                pending = remainder
                                if phrase:
                                    await queue.put(_Phrase(phrase))
                        elif event.get("type") == "done":
                            reason = event.get("finish_reason")
                            if reason not in {"stop", "tool_calls"}:
                                raise VoiceEngineError(
                                    f"voice model response incomplete: {reason}"
                                )
                            pending += normalizer.feed("", final=True)
                            while pending:
                                phrase, pending = _take_phrase(
                                    pending, self.settings.max_phrase_chars, final=True
                                )
                                if phrase:
                                    await queue.put(_Phrase(phrase))
                            calls = event.get("tool_calls") or []
                            if not isinstance(calls, list) or len(calls) > 8:
                                raise VoiceEngineError("invalid voice tool call list")
                            # Validate the complete set before exposing any task intent.
                            parsed = [_tool_call(call, allowed_names) for call in calls]
                            if len({call.call_id for call in parsed}) != len(parsed):
                                raise VoiceEngineError(
                                    "duplicate voice tool call identity"
                                )
                            for call in parsed:
                                await queue.put(call)
                            await queue.put(_Finished("".join(content), reason))
                            return
                        # Reasoning and provider metadata are never speech input.
            except Exception as exc:
                await queue.put(_Failure(exc))

        async def prepare(phrase: _Phrase, index: int) -> _PreparedPhrase:
            with observing("synthesizing_speech"), cadence_span(
                "tts", phrase_index=index
            ):
                wav = await _with_cancellation(
                    asyncio.wait_for(
                        synthesize(phrase.text), self.settings.provider_timeout_seconds
                    ),
                    cancellation,
                )
            if not wav:
                raise VoiceEngineError("voice synthesis unavailable or failed")
            if len(wav) > self.settings.max_wav_bytes:
                raise VoiceEngineError("voice synthesis exceeds WAV byte bound")
            with cadence_span("normalize", phrase_index=index, bytes=len(wav)):
                pcm = playback_audio.normalize_wav_for_playback(wav)
            if len(pcm) > self.settings.max_phrase_audio_seconds * 24_000 * 2:
                raise VoiceEngineError("voice synthesis exceeds phrase audio bound")
            if not pcm:
                raise VoiceEngineError("voice synthesis produced no audio")
            with cadence_span("pcm_ready", phrase_index=index, samples=len(pcm) // 2):
                pass
            return _PreparedPhrase(phrase.text, pcm)

        async def prepare_next(index):
            event = await _with_cancellation(queue.get(), cancellation)
            return await prepare(event, index) if isinstance(event, _Phrase) else event

        producer = asyncio.create_task(produce(), name="voice-llm-phrases")
        prefetch: asyncio.Task | None = None
        total_samples = 0
        phrase_index = 0
        try:
            while True:
                if prefetch is None:
                    event = await _with_cancellation(queue.get(), cancellation)
                else:
                    event = await _with_cancellation(prefetch, cancellation)
                    prefetch = None
                if isinstance(event, _Failure):
                    raise event.error
                if isinstance(event, _Finished):
                    yield VoiceCompleted(event.text, event.finish_reason, total_samples)
                    return
                if isinstance(event, VoiceToolCall):
                    yield event
                    continue
                yield VoiceText(event.text, phrase_index)
                prepared = (
                    event
                    if isinstance(event, _PreparedPhrase)
                    else await prepare(event, phrase_index)
                )
                pcm = prepared.pcm
                # One task is the complete prefetch slot: it owns synthesis and
                # its prepared PCM until this consumer advances to that phrase.
                # A producer loop plus Queue(1) would permit a third synthesis
                # while the second WAV still occupies the queue.
                prefetch = asyncio.create_task(
                    prepare_next(phrase_index + 1), name="voice-tts-prefetch"
                )
                for offset in range(0, len(pcm), 960):
                    if cancellation is not None and cancellation.is_set():
                        raise asyncio.CancelledError
                    chunk = pcm[offset : offset + 960]
                    yield VoiceAudio(
                        pcm=chunk,
                        phrase_index=phrase_index,
                        phrase_text=event.text,
                        start_sample=total_samples,
                        phrase_final=offset + len(chunk) == len(pcm),
                    )
                    total_samples += len(chunk) // 2
                phrase_index += 1
        finally:
            producer.cancel()
            if prefetch is not None:
                prefetch.cancel()
            await asyncio.gather(
                producer,
                *([prefetch] if prefetch is not None else []),
                return_exceptions=True,
            )
