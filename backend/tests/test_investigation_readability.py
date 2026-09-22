"""Public investigation behavior preserved while simplifying its implementation."""

import json

import pytest
from pi_task_helpers import install_pi
from pydantic import BaseModel

from backend.services.memory.agent.pi_agent import _PiEventResult
from backend.services.memory.agent.vault_tools import VaultToolError
from backend.services.timeline import accepted_context, pi_tasks
from backend.services.timeline.investigation_state import InvestigationIncomplete


class Answer(BaseModel):
    answer: str


def test_mixed_read_responses_keep_gateway_shape_and_store_identity(tmp_path):
    tools = pi_tasks.InvestigationTools(
        tmp_path,
        {"S001": "first passage", "S002": "second passage"},
        {"Known.md": "accepted fact"},
        Answer,
    )
    requests = {"pages": [{"key": "S001", "limit": 5}, {"key": "S002"}]}
    response = tools.dispatch("read_materials", requests)
    page = json.loads(response)
    assert page == {
        "result_ref": "R001",
        "store": "evidence",
        "pages": [
            {
                "key": "S001",
                "offset": 0,
                "text": "first",
                "end_of_material": False,
                "length": 13,
                "next_offset": 5,
            },
            {
                "key": "S002",
                "offset": 0,
                "text": "second passage",
                "end_of_material": True,
                "length": 14,
                "next_offset": None,
            },
        ],
        "unread_requests": [],
        "remaining_tool_calls": 95,
    }
    assert tools.trace[-1]["result"] == response
    repeated = json.loads(tools.dispatch("read_materials", requests))
    assert repeated["result_ref"] == "R001"
    assert repeated["identical_reads"] == 2
    assert repeated["remaining_tool_calls"] == 94
    vault = json.loads(
        tools.dispatch("search_material", {"store": "vault", "query": "fact"})
    )
    assert vault["result_ref"] == "R002"
    assert vault["store"] == "vault"
    with pytest.raises(VaultToolError, match="not an inspected accepted-vault result"):
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "first"},
                "accepted_vault_result_refs": ["R001"],
            },
        )
    tools.dispatch(
        "revise_result", {"edits": [], "accepted_vault_result_refs": ["R002"]}
    )
    assert tools.context["notes"][0]["passage"] == "accepted fact"


def _native_turn_for_trace(trace):
    rows = [{"type": "session", "id": "readability-resume"}]
    for index, call in enumerate(trace):
        call_id = f"call-{index}"
        rows.extend(
            [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": call_id,
                                "name": call["tool"],
                                "arguments": call["arguments"],
                            }
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": call_id,
                        "isError": "error" in call,
                        "content": [
                            {
                                "type": "text",
                                "text": call.get("result", call.get("error")),
                            }
                        ],
                    },
                },
            ]
        )
    return "".join(json.dumps(row) + "\n" for row in rows)


@pytest.mark.asyncio
async def test_task_resume_keeps_mixed_source_refs_draft_and_original_trace(
    monkeypatch, tmp_path
):
    install_pi(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        accepted_context, "snapshot", lambda *args: {"Known.md": "accepted fact"}
    )
    captured_trace = []
    attempts = []

    def validate(answer):
        if answer.answer != "source fact":
            raise ValueError("Answer must quote the inspected source")

    async def invoke(root, **kwargs):
        attempts.append(kwargs["max_tool_calls"])
        tools = kwargs["tool_handler"]
        await kwargs["on_event"]({"type": "turn_start"})
        if len(attempts) == 1:
            tools.dispatch("read_materials", {"pages": [{"key": "S001"}]})
            tools.dispatch("read_material", {"store": "vault", "key": "Known.md"})
            with pytest.raises(VaultToolError, match="must quote"):
                tools.dispatch(
                    "finish_task",
                    {
                        "result": {"answer": "unverified"},
                        "accepted_vault_result_refs": ["R002"],
                    },
                )
            captured_trace.extend(tools.trace)
            kwargs["session_file"].write_text(_native_turn_for_trace(tools.trace))
            await kwargs["on_event"]({"type": "turn_end"})
            return (
                _PiEventResult(
                    failure_kind="time_slice",
                    fatal_errors=["Interrupted"],
                    returncode=-9,
                ),
                None,
            )

        assert tools.trace == captured_trace
        assert tools.draft["result"] == {"answer": "unverified"}
        assert "revise_result" in tools.available_tools
        tools.dispatch(
            "revise_result",
            {
                "edits": [
                    {
                        "op": "replace",
                        "path": "/answer",
                        "value_from": {
                            "result_ref": "R001",
                            "pointer": "/pages/0/text",
                        },
                    }
                ]
            },
        )
        return _PiEventResult(returncode=0), None

    monkeypatch.setattr(pi_tasks, "_invoke_pi", invoke)
    params = dict(
        stage="readability-recovery",
        instruction="Find grounded information",
        payload={},
        result_type=Answer,
        user_id="u",
        validate=validate,
        sources=[{"key": "canonical-source", "excerpt": "source fact"}],
    )
    with pytest.raises(InvestigationIncomplete):
        (await pi_tasks.run_task(**params)).result
    context = {}
    outcome = await pi_tasks.run_task(**params, accepted_context=context)
    result = outcome.result
    assert context == {}
    context = outcome.context
    assert result.answer == "source fact"
    assert attempts == [96, 93]
    assert context["notes"][0]["passage"] == "accepted fact"
    # A later caller reuses the validated completion, not the interrupted attempt.
    assert ((await pi_tasks.run_task(**params)).result).answer == "source fact"
    assert attempts == [96, 93]


@pytest.mark.asyncio
async def test_task_outcome_returns_context_without_mutating_input_on_live_or_cached_run(
    monkeypatch, tmp_path
):
    from copy import deepcopy

    notes = {"Known.md": "accepted fact"}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *args: dict(notes))

    def investigate(tools):
        tools.dispatch("search_material", {"store": "vault", "query": "missing fact"})
        page = json.loads(
            tools.dispatch("read_material", {"store": "vault", "key": "Known.md"})
        )
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "grounded"},
                "accepted_vault_result_refs": [page["result_ref"]],
            },
        )

    calls = install_pi(monkeypatch, tmp_path, [investigate])
    supplied = {
        "scope": {"user_id": "u"},
        "notes": [],
        "unresolved_questions": ["Which fact?"],
    }
    original = deepcopy(supplied)
    contexts = []
    for _ in range(2):
        outcome = await pi_tasks.run_task(
            stage="explicit-context",
            instruction="Assess",
            payload={},
            result_type=Answer,
            accepted_context=supplied,
        )
        assert outcome.result.answer == "grounded"
        assert supplied == original
        assert outcome.context["notes"][0]["passage"] == "accepted fact"
        assert outcome.context["unresolved_lookups"] == ["missing fact"]
        assert outcome.context is not supplied
        assert outcome.context["scope"] is not supplied["scope"]
        contexts.append(outcome.context)
    assert contexts[0] == contexts[1]
    assert len(calls) == 1
