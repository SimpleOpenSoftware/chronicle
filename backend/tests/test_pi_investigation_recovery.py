import asyncio
import json

import pytest
from pydantic import BaseModel

from backend.services.memory.agent.pi_agent import _stream_pi_process
from backend.services.timeline.investigation_state import (
    InvestigationIncomplete,
    complete_native_prefix,
    own_investigation,
)
from backend.services.timeline.pi_tasks import InvestigationTools, context_is_current


class Answer(BaseModel):
    answer: str


def native_turn(call_id="call-1", text="fact"):
    return "".join(
        json.dumps(entry) + "\n"
        for entry in [
            {"type": "session", "id": "native-session"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": call_id,
                            "name": "read_material",
                            "arguments": {"store": "vault", "key": "Note.md"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "content": [{"type": "text", "text": text}],
                },
            },
        ]
    )


def test_checkpoint_restores_native_history_references_and_cumulative_cost(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    request = {"scope": "u", "revision": 1}
    with own_investigation(request) as state:
        tools = InvestigationTools(state.root, {}, {"Note.md": "fact"}, Answer)
        tools.on_call = lambda: state.reserve("calls")
        first = json.loads(
            tools.dispatch("read_material", {"store": "vault", "key": "Note.md"})
        )
        state.session_file.write_text(native_turn())
        assert state.checkpoint(tools)
        # Work after the safe boundary costs budget even if the process dies.
        state.reserve("calls")
        state.session_file.write_text(native_turn() + '{"type":"message","message":')
        with pytest.raises(InvestigationIncomplete, match="already running"):
            with own_investigation(request):
                pass
    with own_investigation(request) as state:
        restored = InvestigationTools(state.root, {}, {"Note.md": "fact"}, Answer)
        assert state.restore(restored, restored.notes, context_is_current)
        assert state.session_file.read_text() == native_turn()
        assert state.cost["calls"] == 2
        assert restored.passages[first["result_ref"]][0]["passage"] == "fact"
        assert len(restored.trace) == 1


def test_checkpoint_negative_lookup_rejects_newly_available_knowledge(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    with own_investigation({"task": "identity"}) as state:
        tools = InvestigationTools(state.root, {}, {}, Answer)
        tools.on_call = lambda: state.reserve("calls")
        tools.dispatch("search_material", {"store": "vault", "query": "owner"})
        state.session_file.write_text(native_turn())
        assert state.checkpoint(tools)
    with own_investigation({"task": "identity"}) as state:
        notes = {"New.md": "The owner introduced themselves."}
        restored = InvestigationTools(state.root, {}, notes, Answer)
        assert not state.restore(restored, notes, context_is_current)
        assert not restored.trace and not state.pointer.exists()


def test_checkpoint_does_not_include_unfinished_tool_turn():
    complete = native_turn()
    pending = (
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "pending"}],
                },
            }
        )
        + "\n"
    )
    assert complete_native_prefix(complete + pending) == (complete, 1)


@pytest.mark.asyncio
async def test_stream_forwards_complete_large_events_before_process_finishes():
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        "import json,time; print(json.dumps({'type':'message_end','text':'x'*100000}),flush=True); time.sleep(0.2)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    observed = []

    async def event(value):
        observed.append(value)
        assert process.returncode is None

    stdout, _ = await _stream_pi_process(process, b"", event)
    assert len(observed[0]["text"]) == 100000
    assert json.loads(stdout) == observed[0]


@pytest.mark.asyncio
async def test_task_yields_only_after_a_complete_checkpoint(monkeypatch, tmp_path):
    from pi_task_helpers import install_pi

    from backend.services.memory.agent.pi_agent import _PiEventResult
    from backend.services.timeline import pi_tasks, session_accounts

    install_pi(monkeypatch, tmp_path, [])
    monkeypatch.setattr(session_accounts, "remaining_work_seconds", lambda: 0)

    async def interrupted(root, **kwargs):
        assert kwargs["config"].timeout_seconds == 30
        signal = kwargs["yield_signal"]
        await kwargs["on_event"]({"type": "message_update"})
        assert not signal.is_set()
        kwargs["tool_handler"].dispatch(
            "read_material", {"store": "evidence", "key": "task.json"}
        )
        kwargs["session_file"].write_text(native_turn())
        await kwargs["on_event"]({"type": "turn_end"})
        assert signal.is_set()
        return (
            _PiEventResult(
                failure_kind="time_slice",
                fatal_errors=["Saved checkpoint"],
                returncode=-9,
            ),
            None,
        )

    monkeypatch.setattr(pi_tasks, "_invoke_pi", interrupted)
    with pytest.raises(InvestigationIncomplete) as failure:
        (
            await pi_tasks.run_task(
                stage="canary", instruction="Assess", payload={}, result_type=Answer
            )
        ).result
    assert failure.value.kind == "time_slice"
    assert failure.value.checkpoint


@pytest.mark.asyncio
async def test_explicit_generation_owns_budget_but_completed_results_are_reusable(
    monkeypatch, tmp_path
):
    from pi_task_helpers import install_pi

    from backend.services.memory.agent.pi_agent import _PiEventResult
    from backend.services.timeline import pi_tasks

    install_pi(monkeypatch, tmp_path, [])
    seen = []

    async def invoke(root, **kwargs):
        seen.append((root, kwargs["max_tool_calls"]))
        if len(seen) < 3:
            kwargs["tool_handler"].dispatch(
                "read_material", {"store": "evidence", "key": "task.json"}
            )
            kwargs["session_file"].write_text(native_turn())
            await kwargs["on_event"]({"type": "turn_end"})
            return (
                _PiEventResult(
                    failure_kind="time_slice",
                    fatal_errors=["Interrupted"],
                    returncode=-9,
                ),
                None,
            )
        kwargs["tool_handler"].dispatch(
            "finish_task", {"result": {"answer": "Complete"}}
        )
        return _PiEventResult(returncode=0), None

    monkeypatch.setattr(pi_tasks, "_invoke_pi", invoke)
    params = dict(
        stage="generation-test", instruction="Assess", payload={}, result_type=Answer
    )
    for _ in range(2):
        with pi_tasks.investigation_activity(None, owner="generation-one"):
            with pytest.raises(InvestigationIncomplete):
                (await pi_tasks.run_task(**params)).result
    with pi_tasks.investigation_activity(None, owner="generation-two"):
        assert ((await pi_tasks.run_task(**params)).result).answer == "Complete"
    with pi_tasks.investigation_activity(None, owner="generation-three"):
        assert ((await pi_tasks.run_task(**params)).result).answer == "Complete"
    assert seen[0][0] == seen[1][0] and seen[2][0] != seen[0][0]
    assert [calls for _, calls in seen] == [96, 95, 96]


def test_checkpoint_restores_rejected_draft_for_incremental_repair(
    monkeypatch, tmp_path
):
    from backend.services.memory.agent.vault_tools import VaultToolError

    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    request = {"task": "repair", "revision": 1}
    candidate = {"result": {"answer": 7}}
    with own_investigation(request) as state:
        tools = InvestigationTools(state.root, {}, {}, Answer)
        tools.on_call = lambda: state.reserve("calls")
        with pytest.raises(VaultToolError):
            tools.dispatch("finish_task", candidate)
        rows = [json.loads(line) for line in native_turn().splitlines()]
        rows[1]["message"]["content"][0].update(name="finish_task", arguments=candidate)
        rows[2]["message"].update(toolName="finish_task", isError=True)
        state.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
        assert state.checkpoint(tools)
    with own_investigation(request) as state:
        restored = InvestigationTools(state.root, {}, {}, Answer)
        assert state.restore(restored, {}, context_is_current)
        assert restored.draft == candidate
        assert state.cost["calls"] == 1
        restored.dispatch(
            "revise_result",
            {"edits": [{"op": "replace", "path": "/answer", "value": "grounded"}]},
        )
        assert restored.result.answer == "grounded"
        assert restored.trace[0]["arguments"] == candidate


def test_checkpoint_preserves_supplied_context_before_later_vault_references(
    monkeypatch, tmp_path
):
    from backend.services.inference_artifacts import canonical_hash

    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    notes = {"Owner.md": "Accepted owner context", "Note.md": "fact"}
    brief = [
        {
            "path": "Owner.md",
            "hash": canonical_hash(notes["Owner.md"]),
            "offset": 0,
            "passage": notes["Owner.md"],
            "result_ref": "R001",
        }
    ]
    with own_investigation({"task": "retained"}) as state:
        tools = InvestigationTools(
            state.root, {}, notes, Answer, retained_context=brief
        )
        page = json.loads(
            tools.dispatch("read_material", {"store": "vault", "key": "Note.md"})
        )
        assert page["result_ref"] == "R002"
        state.session_file.write_text(native_turn())
        assert state.checkpoint(tools)
        saved = json.loads(state.pointer.read_text())
        assert {r["path"] for r in saved["context"]["consulted_notes"]} == set(notes)
    with own_investigation({"task": "retained"}) as state:
        restored = InvestigationTools(
            state.root, {}, notes, Answer, retained_context=brief
        )
        assert state.restore(restored, notes, context_is_current)
        assert restored.passages["R001"][0]["path"] == "Owner.md"
        assert restored.passages["R002"][0]["path"] == "Note.md"
        from backend.services.memory.agent.vault_tools import VaultToolError

        with pytest.raises(VaultToolError):
            restored.dispatch(
                "finish_task",
                {
                    "result": {"answer": 9},
                    "accepted_vault_result_refs": ["R001", "R002"],
                },
            )
        restored.dispatch(
            "revise_result",
            {
                "edits": [
                    {
                        "op": "replace",
                        "path": "/answer",
                        "value_from": {
                            "result_ref": page["result_ref"],
                            "pointer": "/text",
                        },
                    }
                ]
            },
        )
        assert restored.result.answer == "fact"
        assert len(restored.context["notes"]) == 2


def test_checkpoint_restores_edits_to_seeded_candidate(monkeypatch, tmp_path):
    from backend.services.memory.agent.vault_tools import VaultToolError

    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))
    seed = {"answer": "old"}

    def validate(answer):
        if answer.answer != "grounded":
            raise ValueError("Needs supported meaning")

    edits = {
        "edits": [{"op": "replace", "path": "/answer", "value": "still unsupported"}]
    }
    with own_investigation({"task": "seeded-repair"}) as state:
        tools = InvestigationTools(
            state.root, {}, {}, Answer, validate, initial_result=seed
        )
        with pytest.raises(VaultToolError):
            tools.dispatch("revise_result", edits)
        rows = [json.loads(line) for line in native_turn().splitlines()]
        rows[1]["message"]["content"][0].update(name="revise_result", arguments=edits)
        rows[2]["message"].update(toolName="revise_result", isError=True)
        state.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
        assert state.checkpoint(tools)
    with own_investigation({"task": "seeded-repair"}) as state:
        tools = InvestigationTools(
            state.root, {}, {}, Answer, validate, initial_result=seed
        )
        assert state.restore(tools, {}, context_is_current)
        assert tools.draft["result"]["answer"] == "still unsupported"
        assert "finish_task" not in tools.available_tools
        tools.dispatch(
            "revise_result",
            {"edits": [{"op": "replace", "path": "/answer", "value": "grounded"}]},
        )
        assert tools.result.answer == "grounded"
        assert seed == {"answer": "old"}
