"""Run lineage through the production chat entry point, with external IO faked."""

import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import chat_service, llm_client
from backend.chat_service import ChatService
from backend.routers.modules import chat_routes
from backend.services import chat_runs
from backend.services.chat_sources import SourceUnavailable


class Collection:
    def __init__(self):
        self.rows = []

    async def insert_one(self, row):
        self.rows.append(copy.deepcopy(row))

    def find(self, query, projection=None):
        rows = [
            copy.deepcopy(r)
            for r in self.rows
            if all(r.get(k) == v for k, v in query.items())
        ]
        return Cursor(rows)

    async def find_one(self, query):
        return next(iter(self.find(query).rows), None)

    async def update_one(self, query, update):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                row.update(copy.deepcopy(update.get("$set", {})))
                return

    async def delete_many(self, query):
        self.rows = [
            r for r in self.rows if not all(r.get(k) == v for k, v in query.items())
        ]

    delete_one = delete_many


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, key, direction):
        self.rows.sort(key=lambda r: r[key], reverse=direction < 0)
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    async def to_list(self, length=None):
        return self.rows[:length]


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    cs = ChatService()
    cs._initialized = True
    cs.db = SimpleNamespace(
        chat_runs=Collection(),
        chat_run_steps=Collection(),
        chat_messages=SimpleNamespace(update_many=AsyncMock()),
    )
    from mongomock_motor import AsyncMongoMockClient

    dialogue_db = AsyncMongoMockClient().dialogue_test
    cs.db.dialogue_state = dialogue_db.dialogue_state
    cs.db.dialogue_interpretations = dialogue_db.dialogue_interpretations
    cs.sessions_collection = Collection()
    cs.sessions_collection.rows.append(
        {
            "session_id": "chat",
            "user_id": "owner",
            "metadata": {"interaction_version": 2},
            "created_at": chat_runs.now(),
            "updated_at": chat_runs.now(),
        }
    )
    cs.add_message = AsyncMock(return_value=True)
    cs.get_session_messages = AsyncMock(return_value=[])
    cs._get_tool_mode_system_prompt = AsyncMock(return_value="system")
    monkeypatch.setattr(chat_routes, "get_chat_service", lambda: cs)
    return cs


from contextlib import asynccontextmanager

from backend.services.chat_context import (
    ChatContext,
    VaultNoteEvidence,
    VaultRetrieval,
    source_id,
)


@pytest.fixture(autouse=True)
def isolated_chat_claim(monkeypatch):
    @asynccontextmanager
    async def unlocked(*args, **kwargs):
        yield

    monkeypatch.setattr("backend.chat_service.distributed_lock", unlocked)


def provider(monkeypatch, *, finish="stop", failure=None, wait=None):
    class Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="Answer", tool_calls=None),
                    )
                ]
            )
            if wait:
                await wait.wait()
            if failure:
                raise failure
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=finish,
                        delta=SimpleNamespace(content=None, tool_calls=None),
                    )
                ]
            )

        async def close(self):
            pass

    create = AsyncMock(return_value=Stream())
    op = SimpleNamespace(
        model_def=SimpleNamespace(model_provider="local"),
        model_name="test",
        get_client=lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
        to_api_params=lambda: {"model": "test", "max_tokens": 100},
        prepare_messages=lambda m: m,
    )
    registry = SimpleNamespace(
        get_llm_operation=lambda _: op, get_fallback_llm_operation=lambda *a, **k: None
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)
    return create


async def collect(cs, question="What did I agree to?"):
    return [e async for e in cs.generate_response_stream("chat", "owner", question)]


async def test_success_retains_exact_prepared_exchange_and_message_join(
    service, monkeypatch
):
    provider(monkeypatch)
    events = await collect(service)
    run = service.db.chat_runs.rows[0]
    assert run["status"] == "succeeded"
    assert events[-1]["data"]["run_id"] == run["run_id"]
    assert service.add_message.call_args.args[0].metadata["run_id"] == run["run_id"]
    detail = await chat_runs.run_detail(service.db, run)
    model = next(s for s in detail["steps"] if s["kind"] == "model")
    parent = next(s for s in detail["steps"] if s["step_id"] == model["parent_id"])
    assert parent["kind"] == "round"
    assert (
        model["request_payload"]["parameters"]["messages"][-1]["content"]
        == "What did I agree to?"
    )
    assert model["response_payload"]["output"]["content"] == "Answer"
    assert model["response_payload"]["output"]["finish_reason"] == "stop"
    assert all(s["status"] == "succeeded" for s in detail["steps"])


@pytest.mark.parametrize(
    "mode", ["truncated", "provider_failure", "save_failure", "source_failure"]
)
async def test_failures_have_durable_terminal_outcomes(service, monkeypatch, mode):
    provider(
        monkeypatch,
        finish="length" if mode == "truncated" else "stop",
        failure=RuntimeError("provider broke") if mode == "provider_failure" else None,
    )
    if mode == "save_failure":
        service.add_message.side_effect = [True, False]
    if mode == "source_failure":
        service.sessions_collection.rows[0]["metadata"]["sources"] = [
            {"kind": "recording", "key": "gone"}
        ]
        monkeypatch.setattr(
            chat_service,
            "resolve_context",
            AsyncMock(side_effect=SourceUnavailable("gone")),
        )
    events = await collect(service)
    run = service.db.chat_runs.rows[0]
    assert run["status"] == ("incomplete" if mode == "truncated" else "failed")
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "complete" for e in events)
    detail = await chat_runs.run_detail(service.db, run)
    assert detail["steps"]
    if mode != "source_failure":
        model = next(s for s in detail["steps"] if s["kind"] == "model")
        assert "Answer" in str(model["response_payload"])


async def test_cancellation_retains_partial_provider_output(service, monkeypatch):
    gate = asyncio.Event()
    provider(monkeypatch, wait=gate)
    got_content = asyncio.Event()

    async def consume():
        async for event in service.generate_response_stream(
            "chat", "owner", "question"
        ):
            if event["type"] == "token":
                got_content.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got_content.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = service.db.chat_runs.rows[0]
    assert run["status"] == "cancelled"
    detail = await chat_runs.run_detail(service.db, run)
    model = next(s for s in detail["steps"] if s["kind"] == "model")
    assert model["status"] == "cancelled"
    assert "Answer" in str(model["response_payload"])
    assert chat_runs.current_run() is None


async def test_trace_storage_failure_is_visible_without_losing_answer(
    service, monkeypatch
):
    provider(monkeypatch)

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(chat_runs, "persist_inference_run", fail)
    events = await collect(service)
    assert events[-1]["type"] == "complete"
    assert events[-1]["data"]["recording_degraded"] is True
    assert service.db.chat_runs.rows[0]["recording_degraded"] is True


async def test_nested_tool_model_lineage_and_concurrent_isolation(service, monkeypatch):
    provider(monkeypatch)

    async def nested(question):
        run = chat_runs.ChatRun(service.db, "chat", "owner", None)
        await run.start(question)
        with run.activate():
            async with chat_runs.run_step(
                "tool", "search_memories", {"query": question}
            ) as tool:
                async for _ in llm_client.async_chat_with_tools_stream(
                    [{"role": "user", "content": question}], operation="chat"
                ):
                    pass
                tool.output = {"note": question}
        await run.finish("succeeded")
        return await chat_runs.run_detail(
            service.db, await service.db.chat_runs.find_one({"run_id": run.id})
        )

    a, b = await asyncio.gather(nested("alpha"), nested("beta"))
    for detail, question in [(a, "alpha"), (b, "beta")]:
        assert detail["question"] == question
        assert len(detail["steps"]) == 2
        tool, model = detail["steps"]
        assert model["parent_id"] == tool["step_id"]
        assert (
            model["request_payload"]["parameters"]["messages"][0]["content"] == question
        )


async def test_owner_scoping_and_stale_run_visibility(service, monkeypatch):
    provider(monkeypatch)
    await collect(service)
    row = service.db.chat_runs.rows[0]
    owner = SimpleNamespace(id="owner")
    detail = await chat_routes.get_chat_run("chat", row["run_id"], owner)
    assert detail["steps"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        await chat_routes.get_chat_run(
            "chat", row["run_id"], SimpleNamespace(id="someone-else")
        )
    assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        await chat_routes.get_chat_run("another-chat", row["run_id"], owner)
    row.update(status="running", lease_until=chat_runs.now() - timedelta(seconds=1))
    assert chat_runs.public_run(row)["status"] == "interrupted"


async def test_deleting_history_removes_payloads(service, monkeypatch, tmp_path):
    provider(monkeypatch)
    await collect(service)
    assert list(tmp_path.rglob("*.gz"))
    await chat_runs.delete_runs(service.db, "chat", "owner")
    assert not list(tmp_path.rglob("*.gz"))
    assert not service.db.chat_run_steps.rows
    assert not service.db.chat_runs.rows


async def test_closing_at_a_streamed_token_finishes_all_steps(service, monkeypatch):
    provider(monkeypatch)
    events = service.generate_response_stream("chat", "owner", "question")
    async for event in events:
        if event["type"] == "token":
            break
    await events.aclose()
    row = service.db.chat_runs.rows[0]
    detail = await chat_runs.run_detail(service.db, row)
    assert row["status"] == "cancelled"
    assert all(step["status"] != "running" for step in service.db.chat_run_steps.rows)
    assert chat_runs.current_run() is None


async def test_sse_disconnect_under_anyio_cancellation_saves_outcome(
    service, monkeypatch
):
    from contextlib import aclosing

    import anyio

    provider(monkeypatch)
    with anyio.CancelScope() as scope:
        async with aclosing(
            chat_routes._stream_openai_format(
                service, "chat", "owner", "question", "completion", 1, "local"
            )
        ) as stream:
            async for chunk in stream:
                if "Answer" in chunk:
                    scope.cancel()
                    await anyio.sleep(0)
    assert service.db.chat_runs.rows[0]["status"] == "cancelled"
    assert all(s["status"] != "running" for s in service.db.chat_run_steps.rows)
    assert chat_runs.current_run() is None


async def test_budget_final_pass_and_source_tool_recorded(service, monkeypatch):
    from backend.services.chat_sources import (
        ChatSourceContext,
        ChatSourceRef,
        SourcePassage,
    )

    source = ChatSourceContext(
        ref=ChatSourceRef(kind="recording", key="recording"),
        title="Meeting",
        url="/recordings/recording",
        started_at=None,
        revision="original",
        passages=[
            SourcePassage(
                id="S1",
                text="Send the report.",
                url="/recordings/recording",
                label="Ankush",
                revision="v1",
            )
        ],
        coverage="All available source passages included.",
        total_passages=1,
    )
    service.sessions_collection.rows[0]["metadata"]["sources"] = [
        source.ref.model_dump()
    ]
    monkeypatch.setattr(
        chat_service,
        "resolve_context",
        AsyncMock(return_value=ChatContext(sources=[source])),
    )
    calls = []

    async def stream(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) <= 5:
            yield {
                "type": "done",
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": str(len(calls)),
                        "function": {
                            "name": "read_selected_source",
                            "arguments": json.dumps(
                                {"query": "report", "source_id": source_id(source.ref)}
                            ),
                        },
                    }
                ],
            }
        else:
            yield {
                "type": "done",
                "content": "Send the report [S1].",
                "finish_reason": "stop",
                "tool_calls": [],
            }

    monkeypatch.setattr(chat_service, "async_chat_with_tools_stream", stream)
    await collect(service)
    source.passages[0].text = "Changed after the answer"
    detail = await chat_runs.run_detail(service.db, service.db.chat_runs.rows[0])
    assert detail["status"] == "succeeded"
    rounds = [s for s in detail["steps"] if s["kind"] == "round"]
    assert len(rounds) == 6 and rounds[-1]["request_payload"]["answer_only"]
    assert calls[-1]["tools"] is None
    source_step = detail["steps"][0]
    assert (
        source_step["response_payload"]["output"]["sources"][0]["passages"][0]["text"]
        == "Send the report."
    )
    tool = next(s for s in detail["steps"] if s["kind"] == "tool")
    assert (
        tool["response_payload"]["output"]["result"]["passages"][0]["text"]
        == "Send the report."
    )


async def test_memory_space_access_is_checked_before_trace_read(service, monkeypatch):
    from fastapi import HTTPException

    service.sessions_collection.rows[0]["memory_space_id"] = "sealed"
    denied = AsyncMock(side_effect=HTTPException(403, "Space unavailable"))
    monkeypatch.setattr(chat_routes.MemoryScopeResolver, "require_space", denied)
    with pytest.raises(HTTPException) as exc:
        await chat_routes.get_chat_runs("chat", SimpleNamespace(id="owner"))
    assert exc.value.status_code == 403
    denied.assert_awaited_once()


async def test_close_after_complete_preserves_success(service, monkeypatch):
    provider(monkeypatch)
    events = service.generate_response_stream("chat", "owner", "question")
    async for event in events:
        if event["type"] == "complete":
            break
    await events.aclose()
    assert service.db.chat_runs.rows[0]["status"] == "succeeded"
    assert all(s["status"] == "succeeded" for s in service.db.chat_run_steps.rows)


async def test_nonstream_error_preserves_failed_run(service, monkeypatch):
    from fastapi import HTTPException

    provider(monkeypatch, failure=RuntimeError("failed"))
    with pytest.raises(HTTPException):
        await chat_routes._non_streaming_response(
            service, "chat", "owner", "question", "c", 1, "local"
        )
    assert service.db.chat_runs.rows[0]["status"] == "failed"


async def test_slow_recording_marks_degraded_before_completion(service, monkeypatch):
    provider(monkeypatch)
    monkeypatch.setattr(chat_runs, "_FINALIZE_TIMEOUT", 0.01)
    original = chat_runs.ChatRun.artifact

    async def slow(self, step_id, phase, payload):
        if phase == "response":
            await asyncio.sleep(1)
        return await original(self, step_id, phase, payload)

    monkeypatch.setattr(chat_runs.ChatRun, "artifact", slow)
    events = await collect(service)
    assert events[-1]["data"]["recording_degraded"]
    assert service.db.chat_runs.rows[0]["recording_degraded"]
    assert service.db.chat_runs.rows[0]["status"] == "succeeded"


async def test_actual_vault_retrieval_keeps_note_and_model_children(
    service, monkeypatch, tmp_path
):
    from openai.types.chat import ChatCompletion

    from backend.services.memory.agent import memory_agent

    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Alex.md").write_text("Alex agreed to send the report Friday.")
    create = provider(monkeypatch)
    create.side_effect = [
        ChatCompletion(
            id="lookup",
            object="chat.completion",
            created=1,
            model="test",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read",
                                "type": "function",
                                "function": {
                                    "name": "read_note",
                                    "arguments": '{"path":"People/Alex.md"}',
                                },
                            }
                        ],
                    },
                }
            ],
        ),
        ChatCompletion(
            id="answer",
            object="chat.completion",
            created=2,
            model="test",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Alex agreed to send the report Friday.",
                    },
                }
            ],
        ),
    ]
    monkeypatch.setattr(
        memory_agent, "_get_prompt", AsyncMock(return_value="Read notes")
    )

    async def retrieve(*args, **kwargs):
        result = await memory_agent.search_vault("Alex", vault, user_id="owner")
        assert result.answer
        return VaultRetrieval(
            answer=result.answer, notes=[], coverage="Consulted notes"
        )

    service.get_relevant_memories = retrieve
    calls = []

    async def stream(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield {
                "type": "done",
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "search",
                        "function": {
                            "name": "search_memories",
                            "arguments": '{"query":"Alex","limit":100}',
                        },
                    }
                ],
            }
        else:
            yield {
                "type": "done",
                "content": "Friday",
                "finish_reason": "stop",
                "tool_calls": [],
            }

    monkeypatch.setattr(chat_service, "async_chat_with_tools_stream", stream)
    await collect(service)
    detail = await chat_runs.run_detail(service.db, service.db.chat_runs.rows[0])
    search = next(s for s in detail["steps"] if s["name"] == "search_memories")
    note = next(s for s in detail["steps"] if s["name"] == "read_note")
    assert note["parent_id"] == search["step_id"]
    assert "Alex agreed" in str(note["response_payload"])
    models = [s for s in detail["steps"] if s["kind"] == "model"]
    assert len(models) == 2 and all(m["parent_id"] == search["step_id"] for m in models)
    assert (
        search["request_payload"]["function"]["arguments"]
        == '{"query":"Alex","limit":100}'
    )
    assert search["response_payload"]["output"]["effective_arguments"] == {
        "query": "Alex"
    }


def test_database_utc_timestamps_have_explicit_offsets():
    from datetime import datetime, timezone

    row = chat_runs.public_run(
        {
            "run_id": "run",
            "status": "succeeded",
            "started_at": datetime(2026, 9, 12, 23, 4),
        }
    )
    assert row["started_at"].tzinfo == timezone.utc
    assert row["started_at"].isoformat().endswith("+00:00")


async def test_optional_tracer_failure_does_not_break_durable_runs(
    service, monkeypatch
):
    from backend.observability import otel_setup

    provider(monkeypatch)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Exporter unavailable")

    monkeypatch.setattr(otel_setup, "get_tracer", unavailable)
    events = await collect(service)
    assert events[-1]["type"] == "complete"
    row = service.db.chat_runs.rows[0]
    assert row["status"] == "succeeded" and not row["recording_degraded"]
    assert "trace_id" not in row
    assert (await chat_runs.run_detail(service.db, row))["steps"]
