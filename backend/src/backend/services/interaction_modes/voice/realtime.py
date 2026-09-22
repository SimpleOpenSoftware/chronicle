"""Engagement-owned OpenAI Realtime adapter over Chronicle's existing audio stream.

Chronicle decides turn boundaries, executes tools, and owns audible delivery. The
provider consumes audio directly; transcripts never gate native responses. The
single event reader advances only when the downstream PCM consumer has credit.

Protocol: https://developers.openai.com/api/docs/guides/realtime-conversations
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import math
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

import backend.model_registry as model_registry

from .engine import _SYSTEM, _with_cancellation
from .engine_types import (
    VoiceAudio,
    VoiceCompleted,
    VoiceEngineError,
    VoiceEngineEvent,
    VoiceText,
    VoiceToolCall,
)

ConnectionFactory = Callable[[], AbstractAsyncContextManager[Any]]
_INPUT_RATE = 16_000
_OUTPUT_RATE = 24_000
_MAX_INPUT_SAMPLES = _INPUT_RATE * 60
_MAX_AUDIO_DELTA_SAMPLES = _OUTPUT_RATE // 2
_MAX_TEXT_CHARS = 8192
_MAX_TOOL_ARGUMENT_CHARS = 16384


@dataclass
class _AudioItem:
    item_id: str
    content_index: int
    start_sample: int
    end_sample: int


class OpenAIRealtimeEngine:
    """One provider connection per engagement; no capture or task-execution owner."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        operation: str = "voice_realtime",
        voice: str = "marin",
        timeout_seconds: float = 15.0,
    ) -> None:
        if (
            not operation
            or not voice
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "realtime operation, voice and finite positive timeout are required"
            )
        self._connection_factory = connection_factory
        self._operation = operation
        self._voice = voice
        self._timeout = timeout_seconds
        self._manager = None
        self._client = None
        self._connection = None
        self._open_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._tool_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._allowed_tools: set[str] = set()
        self._rate_state = None
        self._input_samples = 0
        self._resampled_samples = 0
        self._last_sample = b"\0\0"
        self._active_response: str | None = None
        self._active_request: str | None = None
        self._response_finished = True
        self._needs_cancel_drain = False
        self._interrupted_cursor = 0
        self._cancelled_requests: set[str] = set()
        self._audio_items: list[_AudioItem] = []
        self._received_samples = 0
        self._submitted_tools: dict[str, str] = {}
        self._issued_tools: set[str] = set()
        self._closed = False
        self._cancel_event_ids: set[str] = set()
        self._unbound_transcripts = deque()
        self._input_transcripts = {}

    async def _send(self, event: dict) -> None:
        if self._connection is None or self._closed:
            raise VoiceEngineError("realtime session is not open")
        async with self._send_lock:
            if self._connection is None or self._closed:
                raise VoiceEngineError("realtime session is not open")
            try:
                await asyncio.wait_for(self._connection.send(event), self._timeout)
            except BaseException:
                # A partial transport write has an ambiguous remote outcome.
                # End this engagement instead of resending or continuing from
                # a potentially different input/context boundary.
                await self.close()
                raise

    async def _recv(self) -> dict[str, Any]:
        async with asyncio.timeout(self._timeout):
            while True:
                event = await self._connection.recv()
                value = event if isinstance(event, dict) else event.model_dump()
                if (
                    value.get("type") == "input_audio_buffer.committed"
                    and self._unbound_transcripts
                ):
                    self._input_transcripts[value["item_id"]] = (
                        self._unbound_transcripts.popleft()
                    )
                if value.get("type") in {
                    "conversation.item.input_audio_transcription.completed",
                    "conversation.item.input_audio_transcription.failed",
                }:
                    pending = self._input_transcripts.pop(value.get("item_id"), None)
                    if pending and not pending[1].done():
                        callback, completed = pending
                        text = value.get("transcript", "")
                        if (
                            isinstance(text, str)
                            and text.strip()
                            and len(text) <= _MAX_TEXT_CHARS
                        ):
                            callback(text)
                            completed.set_result(True)
                        else:
                            completed.set_result(False)
                if value.get("type") != "error":
                    return value
                error = value.get("error", {})
                # Cancellation can race a response.done already queued at the
                # socket. Only this acknowledged, locally issued cancel error
                # is benign; never suppress unrelated provider failures.
                if (
                    error.get("code") == "response_cancel_not_active"
                    and error.get("event_id") in self._cancel_event_ids
                ):
                    self._cancel_event_ids.discard(error["event_id"])
                    continue
                # Do not include provider messages or arbitrary codes that can
                # contain supplied conversation content.
                raise VoiceEngineError("realtime provider rejected the request")

    @staticmethod
    def _tools(tool_schemas: Sequence[Mapping[str, Any]]) -> list[dict]:
        result = []
        for schema in tool_schemas:
            function = schema.get("function")
            if schema.get("type") != "function" or not isinstance(function, Mapping):
                raise ValueError("realtime tools require function schemas")
            if not isinstance(function.get("name"), str):
                raise ValueError("realtime tool requires a name")
            result.append({"type": "function", **dict(function)})
        return result

    async def open(
        self,
        *,
        tool_schemas: Sequence[Mapping[str, Any]] = (),
        history: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        async with self._open_lock:
            if self._connection is not None:
                return
            if self._closed:
                raise VoiceEngineError("ended realtime engagement cannot reopen")
            tools = self._tools(tool_schemas)
            if self._connection_factory is None:

                registry = model_registry.get_models_registry()
                if registry is None:
                    raise VoiceEngineError("realtime model registry is unavailable")
                operation = registry.get_llm_operation(self._operation)
                if (
                    operation.model_def.model_provider != "openai"
                    or not operation.model_name.startswith("gpt-realtime")
                ):
                    raise VoiceEngineError(
                        "voice_realtime must explicitly select an OpenAI realtime model"
                    )
                if not operation.model_def.api_key:
                    raise VoiceEngineError("realtime provider credentials are missing")
                self._client = operation.get_client(is_async=True)
                self._manager = self._client.realtime.connect(
                    model=operation.model_name,
                    # Bound the SDK's WebSocket reader as well as decoded deltas.
                    websocket_connection_options={
                        "max_queue": 1,
                        "max_size": 40_000,
                        "close_timeout": 2,
                        "open_timeout": self._timeout,
                    },
                )
            else:
                self._manager = self._connection_factory()
            try:
                self._connection = await asyncio.wait_for(
                    self._manager.__aenter__(), self._timeout
                )
                await self._send(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": _SYSTEM,
                            "output_modalities": ["audio"],
                            "max_output_tokens": 512,
                            "audio": {
                                "input": {
                                    "format": {
                                        "type": "audio/pcm",
                                        "rate": _OUTPUT_RATE,
                                    },
                                    "turn_detection": None,
                                    "transcription": {
                                        "model": "gpt-4o-mini-transcribe"
                                    },
                                },
                                "output": {
                                    "format": {
                                        "type": "audio/pcm",
                                        "rate": _OUTPUT_RATE,
                                    },
                                    "voice": self._voice,
                                },
                            },
                            "tools": tools,
                            "tool_choice": "auto",
                        },
                    }
                )
                async with asyncio.timeout(self._timeout):
                    while (await self._recv()).get("type") != "session.updated":
                        pass
                self._allowed_tools = {tool["name"] for tool in tools}
                for message in history:
                    role, content = message.get("role"), message.get("content")
                    if (
                        role not in {"user", "assistant", "system"}
                        or not isinstance(content, str)
                        or not content
                    ):
                        continue
                    await self._send(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": role,
                                "content": [
                                    {
                                        "type": (
                                            "output_text"
                                            if role == "assistant"
                                            else "input_text"
                                        ),
                                        "text": content,
                                    }
                                ],
                            },
                        }
                    )
            except BaseException:
                await self.close()
                raise

    async def append_audio(self, pcm16k: bytes) -> None:
        """Append canonical mono PCM16LE, either live frames or a committed range."""
        if not isinstance(pcm16k, bytes) or len(pcm16k) % 2:
            raise ValueError("realtime input must contain complete PCM16 samples")
        if not pcm16k:
            return
        async with self._input_lock:
            if self._input_samples + len(pcm16k) // 2 > _MAX_INPUT_SAMPLES:
                raise VoiceEngineError(
                    "realtime input exceeded maximum utterance duration"
                )
            for offset in range(0, len(pcm16k), 3200):
                pcm, self._rate_state = audioop.ratecv(
                    pcm16k[offset : offset + 3200],
                    2,
                    1,
                    _INPUT_RATE,
                    _OUTPUT_RATE,
                    self._rate_state,
                )
                await self._send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
                self._input_samples += len(pcm16k[offset : offset + 3200]) // 2
                self._resampled_samples += len(pcm) // 2
                self._last_sample = pcm[-2:]

    async def clear_input(self) -> None:
        """Discard an abandoned turn without resetting engagement context.

        The runtime serializes its canonical turn boundary against append calls;
        this lock prevents a clear/commit from splitting a single append call.
        Silence outside addressed turns must not be accumulated indefinitely.
        """
        async with self._input_lock:
            await self._send({"type": "input_audio_buffer.clear"})
            self._input_samples = self._resampled_samples = 0
            self._rate_state = None
            self._last_sample = b"\0\0"

    async def _commit_input(self) -> None:
        async with self._input_lock:
            if self._input_samples < _INPUT_RATE // 10:
                raise VoiceEngineError(
                    "realtime utterance must contain at least 100 ms of audio"
                )
            # ratecv holds the final fractional interpolation sample. Extend the
            # boundary sample so the committed duration matches canonical input.
            missing = (
                round(self._input_samples * _OUTPUT_RATE / _INPUT_RATE)
                - self._resampled_samples
            )
            if missing:
                await self._send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(self._last_sample * missing).decode(
                            "ascii"
                        ),
                    }
                )
            await self._send({"type": "input_audio_buffer.commit"})
            self._input_samples = self._resampled_samples = 0
            self._rate_state = None

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        async with self._tool_lock:
            if call_id not in self._issued_tools or not isinstance(output, str):
                raise VoiceEngineError(
                    "realtime result does not name an issued tool call"
                )
            if call_id in self._submitted_tools:
                if self._submitted_tools[call_id] != output:
                    raise VoiceEngineError("conflicting realtime tool result")
                return
            if len(output) > 32768:
                raise VoiceEngineError("realtime tool result exceeds context limit")
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            )
            self._submitted_tools[call_id] = output

    async def generate(
        self,
        *,
        text: str | None,
        history: Sequence[Mapping[str, Any]] = (),
        tool_schemas: Sequence[Mapping[str, Any]] = (),
        cancellation: asyncio.Event | None = None,
        commit_audio: bool = True,
        input_transcript: Callable[[str], None] | None = None,
    ) -> AsyncIterator[VoiceEngineEvent]:
        if text is not None:
            raise VoiceEngineError(
                "native realtime responses require audio input, not an STT transcript"
            )
        if self._response_lock.locked():
            raise VoiceEngineError(
                "realtime engagement already has an active response reader"
            )
        async with self._response_lock:
            await self.open(tool_schemas=tool_schemas, history=history)
            if cancellation is not None and cancellation.is_set():
                raise asyncio.CancelledError
            tools = self._tools(tool_schemas)
            self._allowed_tools = {tool["name"] for tool in tools}
            await self._drain_cancelled_response()
            input_recorded = None
            if commit_audio:
                if input_transcript is not None:
                    input_recorded = asyncio.get_running_loop().create_future()
                    self._unbound_transcripts.append((input_transcript, input_recorded))
                await self._commit_input()
            request_id = str(uuid.uuid4())
            self._active_request = request_id
            self._active_response = None
            self._response_finished = False
            self._audio_items = []
            self._received_samples = 0
            self._interrupted_cursor = 0
            transcript = ""
            called: dict[str, VoiceToolCall] = {}
            await self._send(
                {
                    "type": "response.create",
                    "response": {
                        "output_modalities": ["audio"],
                        "tools": tools,
                        "metadata": {"chronicle_request_id": request_id},
                    },
                }
            )
            try:
                async with asyncio.timeout(60):
                    while True:
                        event = await _with_cancellation(self._recv(), cancellation)
                        kind = event.get("type")
                        if kind == "response.created":
                            response = event.get("response", {})
                            if (
                                response.get("metadata", {}).get("chronicle_request_id")
                                != request_id
                            ):
                                continue
                            self._active_response = response.get("id")
                            if request_id in self._cancelled_requests:
                                continue
                        if request_id in self._cancelled_requests:
                            raise asyncio.CancelledError
                        response_id = event.get("response_id") or event.get(
                            "response", {}
                        ).get("id")
                        if (
                            not self._active_response
                            or response_id != self._active_response
                        ):
                            continue
                        if kind == "response.output_audio.delta":
                            encoded = event.get("delta", "")
                            if (
                                not isinstance(encoded, str)
                                or len(encoded) > _MAX_AUDIO_DELTA_SAMPLES * 8 // 3 + 4
                            ):
                                raise VoiceEngineError(
                                    "realtime audio delta exceeds buffer budget"
                                )
                            try:
                                pcm = base64.b64decode(encoded, validate=True)
                            except (ValueError, TypeError) as exc:
                                raise VoiceEngineError(
                                    "realtime audio is malformed"
                                ) from exc
                            if (
                                not pcm
                                or len(pcm) % 2
                                or len(pcm) // 2 > _MAX_AUDIO_DELTA_SAMPLES
                            ):
                                raise VoiceEngineError(
                                    "realtime audio delta is invalid or oversized"
                                )
                            if (
                                self._received_samples + len(pcm) // 2
                                > _OUTPUT_RATE * 60
                            ):
                                raise VoiceEngineError(
                                    "realtime response exceeds duration budget"
                                )
                            item_id, content_index = event.get("item_id"), event.get(
                                "content_index", 0
                            )
                            if not isinstance(item_id, str) or not isinstance(
                                content_index, int
                            ):
                                raise VoiceEngineError(
                                    "realtime audio is missing its conversation item"
                                )
                            if (
                                not self._audio_items
                                or self._audio_items[-1].item_id != item_id
                                or self._audio_items[-1].content_index != content_index
                            ):
                                self._audio_items.append(
                                    _AudioItem(
                                        item_id,
                                        content_index,
                                        self._received_samples,
                                        self._received_samples,
                                    )
                                )
                            start = self._received_samples
                            self._received_samples += len(pcm) // 2
                            self._audio_items[-1].end_sample = self._received_samples
                            for offset in range(0, len(pcm), 960):
                                if request_id in self._cancelled_requests or (
                                    cancellation is not None and cancellation.is_set()
                                ):
                                    raise asyncio.CancelledError
                                chunk = pcm[offset : offset + 960]
                                yield VoiceAudio(
                                    chunk, 0, "", start + offset // 2, False
                                )
                        elif kind == "response.output_audio_transcript.delta":
                            delta = event.get("delta", "")
                            if (
                                not isinstance(delta, str)
                                or len(transcript) + len(delta) > _MAX_TEXT_CHARS
                            ):
                                raise VoiceEngineError(
                                    "realtime transcript exceeds budget"
                                )
                            transcript += delta
                            yield VoiceText(delta, 0)
                        elif kind == "response.function_call_arguments.done":
                            name, call_id, arguments = (
                                event.get("name"),
                                event.get("call_id"),
                                event.get("arguments", ""),
                            )
                            if (
                                name not in self._allowed_tools
                                or not isinstance(call_id, str)
                                or not call_id
                            ):
                                raise VoiceEngineError(
                                    "realtime returned an unadvertised tool"
                                )
                            if (
                                not isinstance(arguments, str)
                                or len(arguments) > _MAX_TOOL_ARGUMENT_CHARS
                            ):
                                raise VoiceEngineError(
                                    "realtime tool arguments exceed budget"
                                )
                            try:
                                arguments = json.loads(arguments)
                            except ValueError as exc:
                                raise VoiceEngineError(
                                    "realtime tool arguments are malformed"
                                ) from exc
                            if not isinstance(arguments, dict):
                                raise VoiceEngineError(
                                    "realtime tool arguments must be an object"
                                )
                            call = VoiceToolCall(call_id, name, arguments)
                            if call_id in called and called[call_id] != call:
                                raise VoiceEngineError(
                                    "conflicting realtime tool identity"
                                )
                            if call_id not in called:
                                if call_id in self._issued_tools or len(called) >= 8:
                                    raise VoiceEngineError(
                                        "duplicate or excessive realtime tool calls"
                                    )
                                called[call_id] = call
                        elif kind == "response.done":
                            self._response_finished = True
                            status = event.get("response", {}).get("status")
                            if status != "completed":
                                raise VoiceEngineError(
                                    f"realtime response ended with {status}"
                                )
                            if input_recorded is not None:
                                while not input_recorded.done():
                                    await self._recv()
                                if not input_recorded.result():
                                    raise VoiceEngineError(
                                        "realtime input transcription unavailable"
                                    )
                            # Do not expose an executable intent from a response
                            # later cancelled or failed, or before validating its
                            # complete tool set.
                            for call in called.values():
                                self._issued_tools.add(call.call_id)
                                yield call
                            yield VoiceCompleted(
                                transcript,
                                "tool_calls" if called else "stop",
                                self._received_samples,
                            )
                            return
            except BaseException:
                # Stop provider generation, but the runtime supplies the final
                # rendered cursor separately after physical playback cancellation.
                try:
                    await self._cancel_generation()
                except Exception:
                    await self.close()
                raise
            finally:
                if input_recorded is not None and not input_recorded.done():
                    input_recorded.cancel()

    async def _cancel_generation(self) -> None:
        async with self._cancel_lock:
            if self._active_request:
                self._cancelled_requests.add(self._active_request)
            if self._connection is not None and not self._response_finished:
                event_id = "cancel_" + uuid.uuid4().hex
                event = {"type": "response.cancel", "event_id": event_id}
                if self._active_response:
                    event["response_id"] = self._active_response
                self._cancel_event_ids.add(event_id)
                await self._send(event)
                self._needs_cancel_drain = True
                self._response_finished = True

    async def _drain_cancelled_response(self) -> None:
        """Reconcile late provider output before admitting another response.

        A cancel acknowledgement can trail bytes already on the socket. Those
        bytes must never enter playback or remain as unheard provider context.
        This runs under the sole response-reader lock, after local output has
        already stopped, so it cannot delay physical interruption.
        """
        if not self._needs_cancel_drain:
            return
        known = {(item.item_id, item.content_index) for item in self._audio_items}
        unseen: set[tuple[str, int]] = set()
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    event = await self._recv()
                    kind = event.get("type")
                    response = event.get("response", {})
                    if (
                        kind == "response.created"
                        and response.get("metadata", {}).get("chronicle_request_id")
                        == self._active_request
                    ):
                        self._active_response = response.get("id")
                    response_id = event.get("response_id") or response.get("id")
                    if (
                        not self._active_response
                        or response_id != self._active_response
                    ):
                        continue
                    if kind == "response.output_audio.delta":
                        item = (event.get("item_id"), event.get("content_index", 0))
                        if not isinstance(item[0], str) or not isinstance(item[1], int):
                            raise VoiceEngineError(
                                "late realtime audio is missing its item"
                            )
                        if item not in known:
                            unseen.add(item)
                    elif kind == "response.done":
                        break
            for item_id, content_index in unseen:
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item_id,
                        "content_index": content_index,
                        "audio_end_ms": 0,
                    }
                )
            # Includes an item whose last received delta was fully rendered but
            # which acquired additional, unheard audio before cancel completed.
            for item in self._audio_items:
                heard = min(
                    max(self._interrupted_cursor - item.start_sample, 0),
                    item.end_sample - item.start_sample,
                )
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item.item_id,
                        "content_index": item.content_index,
                        "audio_end_ms": heard * 1000 // _OUTPUT_RATE,
                    }
                )
            self._needs_cancel_drain = False
        except BaseException:
            # A session whose heard context cannot be reconciled must not answer
            # using that potentially unseen context on the next turn.
            await self.close()
            raise

    async def interrupt(self, rendered_samples: int) -> None:
        """Remove unheard audio from provider context using Chronicle's render clock."""
        if not 0 <= rendered_samples <= self._received_samples:
            raise ValueError("rendered cursor exceeds received realtime audio")
        self._interrupted_cursor = rendered_samples
        await self._cancel_generation()
        for item in self._audio_items:
            heard = min(
                max(rendered_samples - item.start_sample, 0),
                item.end_sample - item.start_sample,
            )
            if heard < item.end_sample - item.start_sample:
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item.item_id,
                        "content_index": item.content_index,
                        "audio_end_ms": heard * 1000 // _OUTPUT_RATE,
                    }
                )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        manager, self._manager = self._manager, None
        self._connection = None
        try:
            if manager is not None:
                await asyncio.wait_for(
                    manager.__aexit__(None, None, None), self._timeout
                )
        finally:
            if self._client is not None:
                client, self._client = self._client, None
                await asyncio.wait_for(client.close(), self._timeout)
