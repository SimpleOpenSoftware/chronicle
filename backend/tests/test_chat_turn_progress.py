"""Unit tests for the chat tool loop's streaming and progress events.

A turn on a local model runs for minutes, so the events emitted while it works
are the only thing the user sees until the reply arrives. These tests pin that
sequence, and the retraction rule that stops a tool round's self-narration from
being mistaken for the answer.

LLM-independent: async_chat_with_tools_stream is replaced with scripted rounds.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from backend.chat_service import ChatService
from backend.services.chat_context import (
    ChatContext,
    VaultNoteEvidence,
    VaultRetrieval,
    source_id,
)
from backend.services.memory.base import VaultSearchUnavailable


@pytest.fixture(autouse=True)
def isolated_chat_claim(monkeypatch):
    @asynccontextmanager
    async def unlocked(*args, **kwargs):
        yield

    monkeypatch.setattr("backend.chat_service.distributed_lock", unlocked)


def _tool_round(query, *, prose=None, call_id="call-1"):
    """One scripted round that asks for a vault search, optionally narrating first."""
    events = []
    if prose:
        events.append({"type": "content", "text": prose})
    events.append(
        {
            "type": "done",
            "content": prose or "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "search_memories",
                        "arguments": json.dumps({"query": query}),
                    },
                }
            ],
            "finish_reason": "tool_calls",
        }
    )
    return events


def _text_round(*chunks):
    """One scripted round that streams prose and stops."""
    return [{"type": "content", "text": c} for c in chunks] + [
        {
            "type": "done",
            "content": "".join(chunks),
            "tool_calls": [],
            "finish_reason": "stop",
        }
    ]


def _stage(event):
    """Stage of a status event, or None. `token` carries a str, `token_reset` no data."""
    data = event.get("data")
    return data.get("stage") if isinstance(data, dict) else None


def _service(memories):
    cs = ChatService()
    cs._initialized = True
    cs.add_message = AsyncMock(return_value=True)
    cs.get_session_messages = AsyncMock(return_value=[])
    cs._get_tool_mode_system_prompt = AsyncMock(return_value="system")
    cs.get_relevant_memories = AsyncMock(
        return_value=VaultRetrieval(
            answer=next(
                (
                    m.content
                    for m in memories
                    if getattr(m, "metadata", {}).get("kind") == "vault_search_answer"
                ),
                "",
            ),
            notes=[
                VaultNoteEvidence(
                    id="Vnote",
                    path=m.id,
                    title=m.id,
                    text=m.content,
                    revision="r1",
                    coverage="Full note",
                )
                for m in memories
                if getattr(m, "metadata", {}).get("kind") != "vault_search_answer"
            ],
            coverage="Consulted notes",
        )
    )
    return cs


def _scripted_stream(rounds):
    """Return a stand-in for async_chat_with_tools_stream that replays `rounds`."""
    remaining = list(rounds)

    async def _stream(*_args, **_kwargs):
        for event in remaining.pop(0):
            yield event

    return _stream


async def _collect(cs, rounds):
    with patch(
        "backend.chat_service.async_chat_with_tools_stream",
        _scripted_stream(rounds),
    ), patch("backend.chat_service.set_trace_io"):
        return [
            event
            async for event in cs._generate_response_tool_mode(
                session_id="sess-1", user_id="user-1", message_content="who is Alex?"
            )
        ]


@pytest.mark.asyncio
async def test_search_then_answer_emits_progress_before_any_text():
    """The user must learn a search is running long before the reply exists."""
    entry = type(
        "E",
        (),
        {
            "id": "search:user-1",
            "content": "He is an AI engineer.",
            "metadata": {"kind": "vault_search_answer"},
        },
    )()
    note = type(
        "E", (), {"id": "People/alex.md", "content": "note body", "metadata": {}}
    )()
    cs = _service([entry, note])

    events = await _collect(
        cs, [_tool_round("Alex"), _text_round("He is ", "an AI engineer.")]
    )

    stages = [_stage(e) for e in events if e["type"] == "status"]
    assert stages == ["thinking", "searching", "searched", "thinking", "writing"]

    # Progress must precede the first token, or the screen is blank while it works.
    first_status = next(i for i, e in enumerate(events) if e["type"] == "status")
    first_token = next(i for i, e in enumerate(events) if e["type"] == "token")
    assert first_status < first_token

    searching = next(e for e in events if _stage(e) == "searching")
    assert searching["data"]["query"] == "Alex"
    searched = next(e for e in events if _stage(e) == "searched")
    assert searched["data"] == {
        "stage": "searched",
        "query": "Alex",
        "note_count": 1,
        "found": True,
    }

    # Tokens carry accumulated text, which the SSE layer turns back into deltas.
    assert [e["data"] for e in events if e["type"] == "token"][:2] == [
        "He is ",
        "He is an AI engineer.",
    ]
    assert events[-1]["type"] == "complete"


@pytest.mark.asyncio
async def test_tool_round_prose_is_retracted_before_the_search_runs():
    """Narration like "Let me check…" must not linger as if it were the answer."""
    cs = _service([])

    events = await _collect(
        cs,
        [
            _tool_round("Alex", prose="Let me check your vault."),
            _text_round("Nothing found."),
        ],
    )

    types = [e["type"] for e in events]
    reset_at = types.index("token_reset")
    # The narration streamed, was retracted, and only then did the search start.
    assert types.index("token") < reset_at
    searching_at = next(i for i, e in enumerate(events) if _stage(e) == "searching")
    assert reset_at < searching_at

    # Everything after the retraction is the real answer, not the narration.
    tokens_after = [e["data"] for e in events[reset_at:] if e["type"] == "token"]
    assert tokens_after == ["Nothing found."]

    saved = cs.add_message.await_args_list[-1].args[0]
    assert saved.content == "Nothing found."


@pytest.mark.asyncio
async def test_empty_search_is_reported_as_not_found():
    """An empty result set must surface as found=False, not a silent zero."""
    cs = _service([])

    events = await _collect(cs, [_tool_round("Alex"), _text_round("No luck.")])

    searched = next(e for e in events if _stage(e) == "searched")
    assert searched["data"]["found"] is False
    assert searched["data"]["note_count"] == 0
    assert "failed" not in searched["data"]


@pytest.mark.asyncio
async def test_broken_search_is_never_presented_as_an_empty_vault():
    """The regression that made chat announce "your vault is empty" while it held 801 notes.

    A retrieval agent that fails must reach the model as an explicit error, not as
    zero results, because the model resolves zero results into a confident claim
    that nothing is stored.
    """
    cs = _service([])
    cs.get_relevant_memories = AsyncMock(
        side_effect=VaultSearchUnavailable("pi agent produced no usable answer")
    )

    captured = {}

    def _capture(rounds):
        base = _scripted_stream(rounds)

        async def _stream(messages, *a, **kw):
            captured["messages"] = list(messages)
            async for e in base(messages, *a, **kw):
                yield e

        return _stream

    with patch(
        "backend.chat_service.async_chat_with_tools_stream",
        _capture([_tool_round("Alex"), _text_round("The search failed.")]),
    ), patch("backend.chat_service.set_trace_io"):
        events = [
            e
            async for e in cs._generate_response_tool_mode(
                session_id="sess-1", user_id="user-1", message_content="who is Alex?"
            )
        ]

    searched = next(e for e in events if _stage(e) == "searched")
    assert searched["data"]["failed"] is True

    # What the model was actually handed on the second round.
    tool_msg = next(m for m in captured["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["error"] == "vault_search_failed"
    assert "empty" in payload["instruction"].lower()
    # It must not look like a successful search that found nothing.
    assert "answer" not in payload and "notes" not in payload

    # The turn still completes rather than hanging or erroring out.
    assert events[-1]["type"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_attached", [False, True])
async def test_tool_budget_finishes_with_a_saved_evidence_based_answer(source_attached):
    """Reading five times must still produce an answer that survives reload."""
    from backend.chat_service import MAX_TOOL_ROUNDS
    from backend.services.chat_sources import (
        ChatSourceContext,
        ChatSourceRef,
        SourcePassage,
    )

    cs = _service([])
    source = (
        ChatSourceContext(
            ref=ChatSourceRef(kind="recording", key="recording-a"),
            title="Session",
            url="/recordings/recording-a",
            started_at=None,
            revision="revision-a",
            coverage="All available source passages included.",
            total_passages=1,
            passages=[
                SourcePassage(
                    id="S1",
                    text="I will send the update on Friday.",
                    url="/recordings/recording-a?t=10",
                    label="Speaker",
                    revision="r1",
                )
            ],
        )
        if source_attached
        else None
    )
    calls = []

    async def stream(messages, **kwargs):
        calls.append({"messages": list(messages), **kwargs})
        if kwargs.get("tools"):
            tool = "read_selected_source" if source_attached else "search_memories"
            yield {"type": "content", "text": "Let me check again."}
            yield {
                "type": "done",
                "content": "Let me check again.",
                "tool_calls": [
                    {
                        "id": f"read-{len(calls)}",
                        "type": "function",
                        "function": {
                            "name": tool,
                            "arguments": json.dumps({"query": "update"}),
                        },
                    }
                ],
            }
        else:
            assert any(m["role"] == "tool" for m in messages)
            answer = (
                "Send the update on Friday [S1]."
                if source_attached
                else "No commitment was found in the available evidence."
            )
            for event in _text_round(answer):
                yield event

    with patch("backend.chat_service.async_chat_with_tools_stream", stream), patch(
        "backend.chat_service.set_trace_io"
    ):
        events = [
            e
            async for e in cs._generate_response_tool_mode(
                session_id="sess-1",
                user_id="user-1",
                message_content="What were my action items?",
                source_context=ChatContext(sources=[source]) if source else None,
            )
        ]

    saved = [
        call.args[0]
        for call in cs.add_message.await_args_list
        if call.args[0].role == "assistant"
    ]
    assert len(saved) == 1, "Tool budget ran out without saving an assistant answer"
    assert len(calls) == MAX_TOOL_ROUNDS + 1
    assert not calls[-1].get("tools"), "Final answer must not start another tool round"
    assert "Let me check" not in saved[0].content
    assert events[-1]["data"]["message_id"] == saved[0].message_id
    if source_attached:
        assert (
            saved[0].metadata["evidence"]["conversations"][0]["passages"][0]["id"]
            == "S1"
        )
        assert (
            saved[0].metadata["evidence"]["conversations"][0]["revision"]
            == "revision-a"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("final_round", [_text_round(""), _tool_round("Alex")])
async def test_invalid_final_answer_is_an_error_not_a_success(final_round):
    from backend.chat_service import MAX_TOOL_ROUNDS

    cs = _service([])
    events = await _collect(cs, [_tool_round("Alex")] * MAX_TOOL_ROUNDS + [final_round])
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "complete" for e in events)
    assert not any(
        call.args[0].role == "assistant" for call in cs.add_message.await_args_list
    )
