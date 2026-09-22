"""Fake WebSocket fixtures; no provider calls or private audio."""

import asyncio
import base64
from contextlib import asynccontextmanager

import pytest

from backend.services.interaction_modes.voice.engine_types import (
    VoiceAudio,
    VoiceCompleted,
    VoiceEngineError,
    VoiceToolCall,
)
from backend.services.interaction_modes.voice.realtime import OpenAIRealtimeEngine

pytestmark = pytest.mark.unit
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memories",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }
]


class Socket:
    def __init__(self):
        self.sent, self.events = [], asyncio.Queue()
        self.closed, self.request, self.responses = False, None, 0

    async def send(self, event):
        self.sent.append(event)
        if event["type"] == "session.update":
            self.events.put_nowait({"type": "session.created"})
            self.events.put_nowait({"type": "session.updated"})
        if event["type"] == "response.create":
            self.responses += 1
            self.request = event["response"]["metadata"]["chronicle_request_id"]
            self.events.put_nowait(
                {
                    "type": "response.created",
                    "response": {
                        "id": f"response-{self.responses}",
                        "metadata": {"chronicle_request_id": self.request},
                    },
                }
            )

        if event["type"] == "response.cancel":
            self.events.put_nowait(
                {
                    "type": "response.done",
                    "response": {
                        "id": f"response-{self.responses}",
                        "status": "cancelled",
                    },
                }
            )

    async def recv(self):
        return await self.events.get()

    def push(self, kind, **fields):
        self.events.put_nowait(
            {"type": kind, "response_id": f"response-{self.responses}", **fields}
        )

    def audio(self, samples=480):
        self.push(
            "response.output_audio.delta",
            item_id="item-1",
            content_index=0,
            delta=base64.b64encode(b"\0\0" * samples).decode(),
        )

    def done(self):
        self.push(
            "response.done",
            response={"id": f"response-{self.responses}", "status": "completed"},
        )


@pytest.fixture
async def engine():
    socket = Socket()

    @asynccontextmanager
    async def connect():
        try:
            yield socket
        finally:
            socket.closed = True

    engine = OpenAIRealtimeEngine(connection_factory=connect, timeout_seconds=0.5)
    await engine.open(tool_schemas=TOOLS)
    yield engine, socket
    await engine.close()


async def prime(engine, socket, commit_audio=True):
    if commit_audio:
        await engine.append_audio(b"\0\0" * 1600)
    iterator = engine.generate(text=None, tool_schemas=TOOLS, commit_audio=commit_audio)
    event = asyncio.create_task(anext(iterator))
    while socket.request is None:
        await asyncio.sleep(0)
    return iterator, event


async def test_native_audio_commit_and_manual_response_do_not_wait_for_transcript(
    engine,
):
    engine, socket = engine
    configuration = socket.sent[0]["session"]
    assert configuration["audio"]["input"]["turn_detection"] is None
    assert configuration["audio"]["input"]["format"]["rate"] == 24000
    iterator, first = await prime(engine, socket)
    socket.audio()
    audio = await first
    assert isinstance(audio, VoiceAudio) and audio.start_sample == 0
    assert [e["type"] for e in socket.sent][-2:] == [
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert (
        sum(
            len(base64.b64decode(e["audio"]))
            for e in socket.sent
            if e["type"] == "input_audio_buffer.append"
        )
        == 4800
    )
    socket.done()
    assert isinstance(await anext(iterator), VoiceCompleted)
    await iterator.aclose()


async def test_interrupt_truncates_unconsumed_part_of_current_delta(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.audio(2400)
    await first
    await engine.interrupt(480)
    assert socket.sent[-2]["type"] == "response.cancel"
    assert socket.sent[-2]["response_id"] == "response-1"
    assert socket.sent[-1] == {
        "type": "conversation.item.truncate",
        "item_id": "item-1",
        "content_index": 0,
        "audio_end_ms": 20,
    }
    with pytest.raises(asyncio.CancelledError):
        await anext(iterator)
    await iterator.aclose()


async def test_tools_emit_once_and_result_continuation_does_not_commit_audio(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.push(
        "response.function_call_arguments.done",
        name="search_memories",
        call_id="call-1",
        arguments='{"query":"meeting"}',
    )
    await asyncio.sleep(0)
    assert not first.done()
    socket.done()
    assert await first == VoiceToolCall(
        "call-1", "search_memories", {"query": "meeting"}
    )
    assert (await anext(iterator)).finish_reason == "tool_calls"
    await iterator.aclose()
    await engine.submit_tool_result("call-1", "No matching notes")
    await engine.submit_tool_result("call-1", "No matching notes")
    assert sum(e["type"] == "conversation.item.create" for e in socket.sent) == 1
    socket.request = None
    iterator, first = await prime(engine, socket, False)
    socket.audio()
    assert isinstance(await first, VoiceAudio)
    socket.done()
    await anext(iterator)
    await iterator.aclose()
    assert sum(e["type"] == "input_audio_buffer.commit" for e in socket.sent) == 1


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("delete_notes", "{}"),
        ("search_memories", "[]"),
        ("search_memories", "bad json"),
    ],
)
async def test_invalid_tool_call_rejected_before_execution(engine, name, arguments):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.push(
        "response.function_call_arguments.done",
        name=name,
        call_id="call-1",
        arguments=arguments,
    )
    with pytest.raises(VoiceEngineError):
        await first
    assert socket.sent[-1]["type"] == "response.cancel"
    await iterator.aclose()


async def test_oversized_provider_delta_fails_without_buffer_growth(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.audio(12001)
    with pytest.raises(VoiceEngineError, match="buffer budget|oversized"):
        await first
    assert socket.sent[-1]["type"] == "response.cancel"
    await iterator.aclose()


async def test_transcript_input_is_not_a_native_fallback(engine):
    engine, socket = engine
    with pytest.raises(VoiceEngineError, match="audio input"):
        await anext(engine.generate(text="What is the weather?"))
    assert not any(e["type"] == "response.create" for e in socket.sent)


async def test_cancel_event_interrupts_stalled_socket(engine):
    engine, socket = engine
    await engine.append_audio(b"\0\0" * 1600)
    cancellation = asyncio.Event()
    iterator = engine.generate(text=None, cancellation=cancellation)
    first = asyncio.create_task(anext(iterator))
    while socket.request is None:
        await asyncio.sleep(0)
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, 1)
    assert socket.sent[-1]["type"] == "response.cancel"
    await iterator.aclose()
    await engine.close()
    assert socket.closed


async def test_completed_response_truncates_if_playback_interrupts_later(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.audio()
    await first
    socket.done()
    await anext(iterator)
    await iterator.aclose()
    await engine.interrupt(120)
    assert socket.sent[-1]["audio_end_ms"] == 5
    assert not any(e["type"] == "response.cancel" for e in socket.sent)


async def test_clear_input_discards_abandoned_audio_and_resampling_state(engine):
    engine, socket = engine
    await engine.append_audio(b"\x01\x00" * 800)
    await engine.clear_input()
    assert socket.sent[-1]["type"] == "input_audio_buffer.clear"
    iterator, first = await prime(engine, socket)
    socket.audio()
    await first
    socket.done()
    await anext(iterator)
    await iterator.aclose()
    clear_index = next(
        i for i, e in enumerate(socket.sent) if e["type"] == "input_audio_buffer.clear"
    )
    assert (
        sum(
            len(base64.b64decode(e["audio"]))
            for e in socket.sent[clear_index:]
            if e["type"] == "input_audio_buffer.append"
        )
        == 4800
    )


async def test_native_session_reused_and_stale_audio_cannot_enter_new_response(engine):
    engine, socket = engine
    for turn in range(2):
        socket.request = None
        iterator, first = await prime(engine, socket)
        if turn:
            socket.events.put_nowait(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "response-1",
                    "delta": "malformed stale payload",
                }
            )
        socket.audio()
        assert (await first).start_sample == 0
        socket.done()
        await anext(iterator)
        await iterator.aclose()
    assert sum(e["type"] == "session.update" for e in socket.sent) == 1
    assert sum(e["type"] == "input_audio_buffer.commit" for e in socket.sent) == 2


async def test_reentrant_response_rejected_without_committing_new_audio(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    with pytest.raises(VoiceEngineError, match="active response reader"):
        await anext(engine.generate(text=None))
    assert sum(e["type"] == "input_audio_buffer.commit" for e in socket.sent) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await iterator.aclose()


@pytest.mark.parametrize("second", ["invalid", "conflict", "failed"])
async def test_entire_native_tool_set_validates_before_any_intent_escapes(
    engine, second
):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.push(
        "response.function_call_arguments.done",
        name="search_memories",
        call_id="call-1",
        arguments='{"query":"safe"}',
    )
    if second == "failed":
        socket.push("response.done", response={"id": "response-1", "status": "failed"})
    else:
        socket.push(
            "response.function_call_arguments.done",
            name="blocked" if second == "invalid" else "search_memories",
            call_id="call-1",
            arguments='{"query":"different"}',
        )
    with pytest.raises(VoiceEngineError):
        await first
    with pytest.raises(VoiceEngineError, match="issued tool"):
        await engine.submit_tool_result("call-1", "Should never execute")
    await iterator.aclose()


async def test_matching_cancel_race_error_does_not_poison_next_turn(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.audio()
    await first
    await iterator.aclose()
    cancel = next(e for e in socket.sent if e["type"] == "response.cancel")
    socket.events.put_nowait(
        {
            "type": "error",
            "error": {
                "code": "response_cancel_not_active",
                "event_id": cancel["event_id"],
            },
        }
    )
    socket.request = None
    iterator, first = await prime(engine, socket)
    socket.audio()
    assert isinstance(await first, VoiceAudio)
    socket.done()
    await anext(iterator)
    await iterator.aclose()


async def test_unrelated_cancel_error_is_not_suppressed(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.events.put_nowait(
        {
            "type": "error",
            "error": {"code": "response_cancel_not_active", "event_id": "not-ours"},
        }
    )
    with pytest.raises(VoiceEngineError, match="rejected"):
        await first
    await iterator.aclose()


async def test_stalled_open_closes_provider_and_cannot_reopen():
    closed = asyncio.Event()
    socket = Socket()

    async def send_without_ack(event):
        socket.sent.append(event)

    socket.send = send_without_ack

    @asynccontextmanager
    async def connect():
        try:
            yield socket
        finally:
            closed.set()

    engine = OpenAIRealtimeEngine(connection_factory=connect, timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        await engine.open()
    assert closed.is_set()
    with pytest.raises(VoiceEngineError, match="cannot reopen"):
        await engine.open()


async def test_cancel_drain_truncates_late_unheard_item_before_next_response(engine):
    engine, socket = engine
    iterator, first = await prime(engine, socket)
    socket.audio()
    await first
    # The second item was produced remotely but never read into local playback.
    socket.push(
        "response.output_audio.delta",
        item_id="late-item",
        content_index=0,
        delta=base64.b64encode(b"\0\0" * 480).decode(),
    )
    await engine.interrupt(120)
    await iterator.aclose()
    socket.request = None
    iterator, first = await prime(engine, socket)
    socket.audio()
    await first
    socket.done()
    await anext(iterator)
    await iterator.aclose()
    late = next(
        i
        for i, e in enumerate(socket.sent)
        if e["type"] == "conversation.item.truncate" and e["item_id"] == "late-item"
    )
    second_create = [
        i for i, e in enumerate(socket.sent) if e["type"] == "response.create"
    ][1]
    assert socket.sent[late]["audio_end_ms"] == 0
    assert late < second_create


async def test_registry_selected_model_and_transport_bounds_are_authoritative(
    monkeypatch,
):
    from types import SimpleNamespace

    from backend import model_registry

    socket = Socket()
    calls = []

    @asynccontextmanager
    async def connect(**kwargs):
        calls.append(kwargs)
        yield socket

    class Client:
        realtime = SimpleNamespace(connect=connect)
        closed = False

        async def close(self):
            self.closed = True

    client = Client()

    class Registry:
        def get_llm_operation(self, operation):
            assert operation == "voice_realtime"
            return SimpleNamespace(
                model_def=SimpleNamespace(
                    model_provider="openai", api_key="fake-not-a-key"
                ),
                model_name="gpt-realtime-test-snapshot",
                get_client=lambda is_async: client,
            )

    monkeypatch.setattr(model_registry, "get_models_registry", lambda: Registry())
    engine = OpenAIRealtimeEngine()
    await engine.open()
    await engine.open()
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-realtime-test-snapshot"
    assert calls[0]["websocket_connection_options"]["max_queue"] == 1
    await engine.close()
    assert client.closed


async def test_live_frame_resampling_matches_one_canonical_utterance(engine):
    engine, socket = engine
    import struct

    pcm = b"".join(struct.pack("<h", (i % 100) * 20) for i in range(1600))
    for offset in range(0, len(pcm), 640):
        await engine.append_audio(pcm[offset : offset + 640])
    iterator = engine.generate(text=None)
    first = asyncio.create_task(anext(iterator))
    while socket.request is None:
        await asyncio.sleep(0)
    import audioop

    expected, _ = audioop.ratecv(pcm, 2, 1, 16000, 24000, None)
    expected += expected[-2:]
    actual = b"".join(
        base64.b64decode(e["audio"])
        for e in socket.sent
        if e["type"] == "input_audio_buffer.append"
    )
    assert actual == expected
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await iterator.aclose()


async def test_partial_send_timeout_closes_engagement_without_retry(engine):
    engine, socket = engine
    engine._timeout = 0.01

    async def stalled_send(event):
        socket.sent.append(event)
        await asyncio.Event().wait()

    socket.send = stalled_send
    with pytest.raises(TimeoutError):
        await engine.append_audio(b"\0\0" * 320)
    assert socket.closed
    with pytest.raises(VoiceEngineError, match="not open"):
        await engine.append_audio(b"\0\0" * 320)
    assert sum(e["type"] == "input_audio_buffer.append" for e in socket.sent) == 1


async def test_input_transcript_is_bound_to_committed_item_without_gating_audio(engine):
    engine, socket = engine
    transcripts = []
    await engine.append_audio(b"\0\0" * 1600)
    iterator = engine.generate(
        text=None, tool_schemas=TOOLS, input_transcript=transcripts.append
    )
    pending = asyncio.create_task(anext(iterator))
    while socket.request is None:
        await asyncio.sleep(0)
    socket.push("input_audio_buffer.committed", item_id="input-1")
    socket.audio()
    assert isinstance(await pending, VoiceAudio)
    assert not transcripts
    socket.push(
        "conversation.item.input_audio_transcription.completed",
        item_id="unrelated",
        transcript="Ignore",
    )
    socket.done()
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    assert not pending.done()
    socket.push(
        "conversation.item.input_audio_transcription.completed",
        item_id="input-1",
        transcript="नहीं धन्यवाद",
    )
    assert isinstance(await pending, VoiceCompleted)
    assert transcripts == ["नहीं धन्यवाद"]
    await iterator.aclose()
