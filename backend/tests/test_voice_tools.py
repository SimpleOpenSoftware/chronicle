import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.services.chat_context import VaultNoteEvidence, VaultRetrieval
from backend.services.interaction_modes.voice.settings import VoiceSettings
from backend.services.interaction_modes.voice.tools import VoiceToolContext, VoiceTools


def context(**kwargs):
    return VoiceToolContext(
        user_id="owner",
        memory_space_id="space",
        interaction_id="dialogue",
        task_id="task",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_disabled_vault_has_no_schema_or_execution():
    retrieve = AsyncMock()
    tools = VoiceTools(retrieve=retrieve)
    assert {t["function"]["name"] for t in tools.schemas()} == {
        "ask_user",
        "start_task",
    }
    result = await tools.execute(
        "search_memories",
        {"query": "where?"},
        context=context(),
        checkpoint=AsyncMock(),
    )
    assert result["status"] == "failed"
    retrieve.assert_not_called()


@pytest.mark.asyncio
async def test_vault_uses_server_scope_and_notes_only_evidence():
    value = VaultRetrieval(
        answer="Consulted answer",
        notes=[
            VaultNoteEvidence(
                id="V1",
                path="People/A.md",
                title="A",
                text="An excerpt",
                revision="hash",
                coverage="Consulted excerpt",
            )
        ],
        coverage="partial",
    )
    retrieve = AsyncMock(return_value=value)
    tools = VoiceTools(vault_enabled=True, retrieve=retrieve)
    result = await tools.execute(
        "search_memories",
        {"query": "where?"},
        context=context(),
        checkpoint=AsyncMock(),
    )
    retrieve.assert_awaited_once_with(
        "where?", "owner", memory_space_id="space", notes_only=True
    )
    assert result["evidence"][0]["revision"] == "hash"
    assert result["coverage"] == "partial"
    result = await tools.execute(
        "search_memories",
        {"query": "where?", "user_id": "other"},
        context=context(),
        checkpoint=AsyncMock(),
    )
    assert result["status"] == "failed"
    assert retrieve.await_count == 1


@pytest.mark.asyncio
async def test_vault_deadline_does_not_wait_for_slow_cleanup():
    cleanup = asyncio.Event()

    async def retrieve(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await cleanup.wait()
            raise

    tools = VoiceTools(
        retrieve=retrieve,
        settings=VoiceSettings(
            vault_retrieval_enabled=True, vault_timeout_seconds=0.01
        ),
    )
    result = await asyncio.wait_for(
        tools.execute(
            "search_memories", {"query": "q"}, context=context(), checkpoint=AsyncMock()
        ),
        0.2,
    )
    assert result["coverage"] == "unavailable"
    cleanup.set()
    await tools.aclose()


@pytest.mark.asyncio
async def test_hermes_checkpoint_before_submit_and_remote_identity_before_result():
    checkpoints, requests = [], []
    state = {}

    async def checkpoint(update):
        checkpoints.append(update)
        state.update(update)

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            assert checkpoints[0]["submission_started"] is True
            assert checkpoints[0]["observation_deadline"] > 0
            return httpx.Response(200, json={"run_id": "remote-run"})
        assert checkpoints[-1] == {"remote_run_id": "remote-run"}
        if request.url.path.endswith("events"):
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"status": "completed", "output": "Finished."})

    plugin = SimpleNamespace(api_url="http://hermes/v1", api_key="", enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = VoiceTools(hermes_plugin=plugin, client=client)
        result = await tools.execute(
            "delegate_to_hermes",
            {"request": "Do task"},
            context=context(state=state),
            checkpoint=checkpoint,
        )
    assert result == {"status": "completed", "answer": "Finished."}
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert all(
        "discord" not in str(r.url) and "chat/completions" not in str(r.url)
        for r in requests
    )


@pytest.mark.asyncio
async def test_uncertain_submission_never_repeats_post():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("uncertain")

    plugin = SimpleNamespace(api_url="http://hermes", api_key="", enabled=True)
    state = {}

    async def checkpoint(update):
        state.update(update)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = VoiceTools(hermes_plugin=plugin, client=client)
        first = await tools.execute(
            "delegate_to_hermes",
            {"request": "Do task"},
            context=context(state=state),
            checkpoint=checkpoint,
        )
        second = await tools.execute(
            "delegate_to_hermes",
            {"request": "Do task"},
            context=context(state=state),
            checkpoint=checkpoint,
        )
    assert first["status"] == second["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hermes_restart_unknown_and_explicit_stop_is_only_requested():
    requests = []

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(200, json={"status": "stopping"})
            if request.method == "POST"
            else httpx.Response(404)
        )

    plugin = SimpleNamespace(api_url="http://hermes", api_key="", enabled=True)
    ctx = context(state={"remote_run_id": "old-run", "submission_started": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = VoiceTools(hermes_plugin=plugin, client=client)
        result = await tools.execute(
            "delegate_to_hermes",
            {"request": "Do task"},
            context=ctx,
            checkpoint=AsyncMock(),
        )
        assert result["status"] == "unknown"
        assert not any(r.method == "POST" for r in requests)
        result = await tools.cancel("delegate_to_hermes", ctx)
        assert result["status"] == "cancel_requested"
        assert requests[-1].url.path.endswith("/old-run/stop")


@pytest.mark.asyncio
async def test_resumed_hermes_observation_does_not_restart_deadline():
    import time

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text="")

    plugin = SimpleNamespace(api_url="http://hermes", api_key="", enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = VoiceTools(hermes_plugin=plugin, client=client)
        result = await tools.execute(
            "delegate_to_hermes",
            {"request": "Do task"},
            context=context(
                state={"remote_run_id": "run", "observation_deadline": time.time() - 1}
            ),
            checkpoint=AsyncMock(),
        )
    assert result["status"] == "unknown"
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [None, [], "query", {"query": 12}])
async def test_invalid_tool_payload_is_a_task_failure(arguments):
    tools = VoiceTools(vault_enabled=True)
    result = await tools.execute(
        "search_memories", arguments, context=context(), checkpoint=AsyncMock()
    )
    assert result["status"] == "failed"


def test_multilingual_voice_evidence_is_bounded_without_losing_note_identity():
    import json

    from backend.services.interaction_modes.voice.tools import _voice_retrieval

    notes = [
        {
            "id": f"V{i}",
            "title": f"Person {i}",
            "path": f"People/{i}.md",
            "revision": str(i),
            "text": "अ" * 8000,
            "coverage": "Consulted excerpt",
        }
        for i in range(20)
    ]
    result = _voice_retrieval(
        {"answer": "अ" * 20000, "notes": notes, "coverage": "complete"}
    )
    assert len(json.dumps(result).encode()) < 200_000
    assert len(result["evidence"]) == 20
    assert result["evidence"][-1]["revision"] == "19"
    assert result["coverage"] == "partial"
    assert all(len(note["text"].encode()) <= 2048 for note in result["evidence"])
