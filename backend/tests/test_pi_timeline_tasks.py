"""Exercise the real task boundary with only Pi and accepted storage faked."""

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pi_task_helpers import install_pi, review_result
from pydantic import BaseModel, Field

from backend.services.inference_artifacts import read_inference_artifact
from backend.services.memory.agent.pi_agent import _PiEventResult, _VaultToolGateway
from backend.services.memory.agent.vault_tools import VaultToolError
from backend.services.timeline import accepted_context, pi_tasks, session_accounts


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_task_retires_reads_before_reserving_final_calls(monkeypatch, tmp_path):
    def investigate(tools):
        for key in ["task.json", "source-index.json"]:
            assert "read_material" in tools.available_tools
            tools.dispatch("read_material", {"store": "evidence", "key": key})
        assert set(tools.available_tools) == {"finish_task"}
        tools.dispatch("finish_task", {"result": {"answer": "Grounded result"}})

    install_pi(monkeypatch, tmp_path, [investigate])
    monkeypatch.setattr(pi_tasks, "settings", lambda: {"max_tool_calls": 4})
    result = (
        await pi_tasks.run_task(
            stage="read-budget", instruction="Assess", payload={}, result_type=Answer
        )
    ).result
    assert result.answer == "Grounded result"


@pytest.mark.asyncio
async def test_rejected_result_can_be_edited_without_reemitting_valid_content(
    monkeypatch, tmp_path
):
    class Result(BaseModel):
        claims: list[str]

    def validate(result):
        if "unsupported" in result.claims:
            raise ValueError("Claim 2 is unsupported")

    def investigate(tools):
        page = json.loads(
            tools.dispatch("read_material", {"store": "vault", "key": "Known.md"})
        )
        assert "revise_result" not in tools.available_tools
        candidate = {
            "result": {"claims": ["grounded", "unsupported"]},
            "accepted_vault_result_refs": [page["result_ref"]],
        }
        with pytest.raises(VaultToolError, match="unsupported"):
            tools.dispatch("finish_task", candidate)
        assert tools.result is None
        assert "revise_result" in tools.available_tools
        assert "finish_task" not in tools.available_tools
        saved = json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "draft.json"})
        )
        assert json.loads(saved["text"]) == candidate["result"]
        tools.dispatch(
            "revise_result", {"edits": [{"op": "remove", "path": "/claims/1"}]}
        )
        assert tools.is_complete("revise_result", "Task result accepted.")
        assert tools.result.claims == ["grounded"]
        assert tools.trace[1]["arguments"] == candidate
        assert tools.context["notes"][0]["path"] == "Known.md"

    monkeypatch.setattr(
        accepted_context, "snapshot", lambda *a: {"Known.md": "Accepted context"}
    )
    install_pi(monkeypatch, tmp_path, [investigate])
    result = (
        await pi_tasks.run_task(
            stage="editable",
            instruction="Assess",
            payload={},
            result_type=Result,
            user_id="u",
            validate=validate,
        )
    ).result
    assert result.claims == ["grounded"]


def test_invalid_patch_is_atomic_and_cannot_publish_a_rejected_draft(tmp_path):
    tools = pi_tasks.InvestigationTools(tmp_path, {}, {}, Answer)
    with pytest.raises(VaultToolError):
        tools.dispatch("finish_task", {"result": {"answer": 9}})
    with pytest.raises(VaultToolError):
        tools.dispatch(
            "revise_result",
            {
                "edits": [
                    {"op": "replace", "path": "/answer", "value": "valid"},
                    {"op": "remove", "path": "/missing"},
                ]
            },
        )
    assert tools.result is None
    page = json.loads(
        tools.dispatch("read_material", {"store": "evidence", "key": "draft.json"})
    )
    assert json.loads(page["text"])["answer"] == 9


@pytest.mark.asyncio
async def test_alternating_repeat_reads_can_recover_without_losing_sources(
    monkeypatch, tmp_path
):
    def investigate(tools):
        queries = [{"store": "evidence", "query": q} for q in ["alpha", "beta"]]
        for _ in range(3):
            for query in queries:
                assert json.loads(tools.dispatch("search_material", query))["matches"]
        for query in queries:
            with pytest.raises(VaultToolError, match="Repeated read limit"):
                tools.dispatch("search_material", query)
        page = json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "task.json"})
        )
        assert "alpha beta" in page["text"]
        tools.dispatch("finish_task", {"result": {"answer": "Sources retained"}})

    install_pi(monkeypatch, tmp_path, [investigate])
    result = (
        await pi_tasks.run_task(
            stage="repeat-loop",
            instruction="Assess",
            payload={"text": "alpha beta"},
            result_type=Answer,
        )
    ).result
    assert result.answer == "Sources retained"


@pytest.mark.asyncio
async def test_agent_can_follow_notes_and_keep_exact_context_without_preselection(
    monkeypatch, tmp_path
):
    notes = {
        "Index.md": "The owner introduction is in People/Avery.md.",
        "People/Avery.md": "Avery owns this vault and works on navigation.",
    }
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: notes)
    context = await accepted_context.for_sources("owner", [])
    assert not context["notes"]

    def investigate(tools):
        found = json.loads(
            tools.dispatch(
                "search_material", {"store": "vault", "query": "introduction"}
            )
        )
        assert found["matches"][0]["key"] == "Index.md"
        page = json.loads(
            tools.dispatch(
                "read_material", {"store": "vault", "key": "People/Avery.md"}
            )
        )
        tools.dispatch(
            "search_material", {"store": "vault", "query": "unknown project"}
        )
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "Avery"},
                "accepted_vault_result_refs": [page["result_ref"]],
            },
        )

    calls = install_pi(monkeypatch, tmp_path, [investigate])
    record = AsyncMock()
    outcome = await pi_tasks.run_task(
        stage="test",
        instruction="Identify the owner",
        payload={},
        result_type=Answer,
        accepted_context=context,
        record=record,
    )
    result = outcome.result
    assert not context["notes"]
    context = outcome.context
    assert result.answer == "Avery"
    assert context["unresolved_lookups"] == ["unknown project"]
    assert context["notes"][0]["passage"] == notes["People/Avery.md"]
    artifact = read_inference_artifact(
        "pi_test", record.call_args.args[0]["artifact_hash"]
    )
    assert len(artifact["metadata"]["tool_calls"]) == 4
    assert "Avery owns" not in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_negative_lookup_invalidates_cached_answer_on_new_note(
    monkeypatch, tmp_path
):
    notes = {}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: dict(notes))

    def lookup(tools):
        found = json.loads(
            tools.dispatch("search_material", {"store": "vault", "query": "owner"})
        )
        tools.dispatch(
            "finish_task",
            {"result": {"answer": "known" if found["total"] else "unknown"}},
        )

    calls = install_pi(monkeypatch, tmp_path, [lookup, lookup])

    async def run():
        return (
            await pi_tasks.run_task(
                stage="lookup",
                instruction="Find context",
                payload={},
                result_type=Answer,
                user_id="u",
            )
        ).result

    assert (await run()).answer == "unknown"
    assert (await run()).answer == "unknown"
    assert len(calls) == 1
    notes["Owner.md"] = "The owner is Avery."
    assert (await run()).answer == "known"
    assert len(calls) == 2


def test_tools_confine_sources_and_validate_retained_context(tmp_path):
    tools = pi_tasks.InvestigationTools(
        tmp_path, {"selected": "source"}, {"Known.md": "Accepted fact"}, Answer
    )
    for args in [
        {"store": "vault", "key": "../secret"},
        {"store": "evidence", "key": "excluded"},
    ]:
        with pytest.raises(VaultToolError):
            tools.dispatch("read_material", args)
    with pytest.raises(VaultToolError, match="inspected"):
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "x"},
                "accepted_vault_result_refs": ["R001"],
            },
        )
    tools.dispatch("read_material", {"store": "vault", "key": "Known.md"})
    with pytest.raises(VaultToolError, match="inspected"):
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "x"},
                "accepted_vault_result_refs": ["R999"],
            },
        )
    assert tools.result is None


def test_terminal_tool_closes_only_after_a_valid_result(tmp_path):
    tools = pi_tasks.InvestigationTools(tmp_path, {}, {}, Answer)
    gateway = _VaultToolGateway(tmp_path, tools.schemas, tool_handler=tools)
    assert not gateway.should_terminate("finish_task", "Task result accepted.")
    result = tools.dispatch("finish_task", {"result": {"answer": "ok"}})
    assert gateway.should_terminate("finish_task", result)
    assert gateway.terminal_completion
    assert not tools.mutating_tools


@pytest.mark.asyncio
async def test_incomplete_exit_after_terminal_result_is_not_cached(
    monkeypatch, tmp_path
):
    def interrupted(tools):
        tools.dispatch("finish_task", {"result": {"answer": "candidate"}})
        return _PiEventResult(returncode=1, fatal_errors=["worker stopped"])

    calls = install_pi(monkeypatch, tmp_path, [interrupted, {"answer": "complete"}])
    params = dict(
        stage="restart", instruction="Inspect", payload={}, result_type=Answer
    )
    record = AsyncMock()
    with pytest.raises(ValueError, match="incomplete"):
        (await pi_tasks.run_task(**params, record=record)).result
    failed = read_inference_artifact(
        "pi_restart", record.call_args.args[0]["artifact_hash"]
    )
    assert not failed["reusable"] and failed["result"] is None
    assert ((await pi_tasks.run_task(**params)).result).answer == "complete"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_reviewer_has_independent_context_and_original_sources(
    monkeypatch, tmp_path
):
    notes = {"Owner.md": "The owner is Avery."}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: notes)
    context = await accepted_context.for_sources("u", [])
    context["notes"] = [
        {"path": "Owner.md", "passage": "Investigator handoff", "hash": "h"}
    ]
    candidate = session_accounts.SessionAccount(
        title="Routine", summary="", claims=[], questions=[], useful=False
    )
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [review_result("ready", "No useful changes or unresolved questions")],
    )
    await session_accounts.verify_session_claims(
        candidate,
        [{"key": "s", "excerpt": "Source", "participation": "supporting"}],
        record=AsyncMock(),
        accepted_context=context,
    )
    assert "Investigator handoff" not in calls[0]["prompt"]
    assert calls[0]["tool_handler"].notes == notes
    assert "S001" in calls[0]["tool_handler"].materials
    assert (
        json.loads(calls[0]["tool_handler"].materials["account-contract.json"])
        == session_accounts.SessionAccount.model_json_schema()
    )
    assert context["review"]["verdict"] == "ready"


@pytest.mark.asyncio
async def test_refresh_assessment_investigates_changes_without_keyword_overlap(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: {})
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            {
                "verdict": "useful",
                "reason": "The alias resolves the unanswered reference",
                "relevant_paths": ["People/Avery.md"],
            }
        ],
    )
    from backend.services.timeline import memory_sources, review

    monkeypatch.setattr(memory_sources, "source_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(review, "validate_selection", AsyncMock())
    p = NS(
        excluded_source_keys=[],
        account={},
        questions=["Who is the expedition lead?"],
        accepted_context={},
        source_scope=[],
        user_id="u",
        memory_space_id=None,
    )
    result = await accepted_context.assess(
        p,
        {
            "People/Avery.md": {
                "before": None,
                "after": "Avery coordinated the polar journey.",
            }
        },
    )
    assert result["verdict"] == "useful" and len(calls) == 1


@pytest.mark.asyncio
async def test_unrelated_note_edit_reuses_cached_investigation(monkeypatch, tmp_path):
    notes = {"Owner.md": "The owner is Avery.", "Garden.md": "Water weekly."}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: dict(notes))

    def inspect(tools):
        tools.dispatch("search_material", {"store": "vault", "query": "owner"})
        tools.dispatch("finish_task", {"result": {"answer": "Avery"}})

    calls = install_pi(monkeypatch, tmp_path, [inspect])
    params = dict(
        stage="unrelated",
        instruction="Identify",
        payload={},
        result_type=Answer,
        user_id="u",
    )
    (await pi_tasks.run_task(**params)).result
    notes["Garden.md"] = "Water twice weekly."
    (await pi_tasks.run_task(**params)).result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cached_and_fresh_tasks_preserve_earlier_lookup_dependencies(
    monkeypatch, tmp_path
):
    def first(tools):
        tools.dispatch(
            "search_material", {"store": "vault", "query": "unanswered identity"}
        )
        tools.dispatch("finish_task", {"result": {"answer": "first"}})

    calls = install_pi(monkeypatch, tmp_path, [first, {"answer": "second"}])
    context = {}
    for stage in ["first", "second", "second"]:
        outcome = await pi_tasks.run_task(
            stage=stage,
            instruction="Inspect",
            payload={},
            result_type=Answer,
            accepted_context=context,
        )
        context = outcome.context
    assert context["unresolved_lookups"] == ["unanswered identity"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_changed_dependency_during_run_is_incomplete(monkeypatch, tmp_path):
    notes = {"Owner.md": "Avery is the owner."}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: dict(notes))

    def change(tools):
        tools.dispatch("read_material", {"store": "vault", "key": "Owner.md"})
        tools.dispatch("finish_task", {"result": {"answer": "Avery"}})
        notes["Owner.md"] = "Morgan is the owner."

    install_pi(monkeypatch, tmp_path, [change])
    with pytest.raises(ValueError, match="stale"):
        (
            await pi_tasks.run_task(
                stage="freshness",
                instruction="Inspect",
                payload={},
                result_type=Answer,
                user_id="u",
            )
        ).result


@pytest.mark.asyncio
async def test_corrupt_cached_result_is_invalidated_and_recomputed(
    monkeypatch, tmp_path
):
    install_pi(monkeypatch, tmp_path, [{"answer": "recovered"}])
    monkeypatch.setattr(
        pi_tasks, "load_reusable_run", lambda *a: NS(result={"bad": "result"})
    )
    result = (
        await pi_tasks.run_task(
            stage="corrupt", instruction="Inspect", payload={}, result_type=Answer
        )
    ).result
    assert result.answer == "recovered"


def test_model_schema_does_not_expand_bounded_strings_but_validation_remains(tmp_path):
    class BoundedResult(BaseModel):
        answer: str = Field(max_length=600)

    tools = pi_tasks.InvestigationTools(tmp_path, {}, {}, BoundedResult)
    schema = tools.schemas[-1]["function"]["parameters"]
    assert "maxLength" not in json.dumps(schema)
    assert "$defs" in schema and "required" in schema
    with pytest.raises(VaultToolError):
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "x" * 601},
            },
        )
    assert tools.result is None


@pytest.mark.asyncio
async def test_refresh_of_existing_clarification_is_idempotent_and_exclusion_stops_inference(
    monkeypatch, tmp_path
):
    from test_session_memory import START, episode

    from backend.services.timeline import memory_sources as source
    from backend.services.timeline import review

    raw = source.evidence_sources([episode()])
    clarification = NS(
        id="decision",
        action="clarify",
        clarification="The project owner is Avery.",
        sources=raw,
        created_at=START,
    )
    resolved = source.apply_dispositions(raw, [clarification])
    decisions = AsyncMock(return_value=[clarification])
    monkeypatch.setattr(source, "source_decisions", decisions)
    monkeypatch.setattr(review, "validate_selection", AsyncMock())
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: {})
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            {
                "verdict": "useful",
                "reason": "The note adds context",
                "relevant_paths": ["Owner.md"],
            }
        ],
    )
    p = NS(
        excluded_source_keys=[],
        account={},
        questions=[],
        accepted_context={},
        source_scope=resolved,
        user_id="u",
        memory_space_id="space",
    )
    change = {
        "Owner.md": {"before": None, "after": "Avery leads the navigation project."}
    }
    result = await accepted_context.assess(p, change)
    assert result["verdict"] == "useful" and len(calls) == 1
    decisions.assert_awaited_once_with("u", resolved, "space")
    assert source.scope_hash(
        source.apply_dispositions(resolved, [clarification])
    ) == source.scope_hash(resolved)
    p.excluded_source_keys = [raw[0]["key"]]
    result = await accepted_context.assess(p, change)
    assert (
        result["verdict"] == "uncertain" and result["error"] == "source_scope_changed"
    )
    assert len(calls) == 1


def test_repeated_reads_restore_text_after_compaction_without_extra_request(
    tmp_path,
):
    tools = pi_tasks.InvestigationTools(
        tmp_path, {}, {"Note.md": "Exact accepted text."}, Answer
    )
    args = {"store": "vault", "key": "Note.md"}
    first = json.loads(tools.dispatch("read_material", args))
    second = json.loads(tools.dispatch("read_material", args))
    assert second["result_ref"] == first["result_ref"]
    assert second["text"] == first["text"]
    restored = json.loads(tools.dispatch("read_material", args))
    assert restored["text"] == first["text"]
    assert restored["result_ref"] == first["result_ref"]
    assert restored["result_ref"] == first["result_ref"]
    assert second["identical_reads"] == 2
    assert restored["identical_reads"] == 3
    assert "no new evidence" in restored["read_status"]
    assert restored["remaining_tool_calls"] == first["remaining_tool_calls"] - 2
    tools.dispatch(
        "finish_task",
        {
            "result": {"answer": "complete"},
            "accepted_vault_result_refs": [first["result_ref"]],
        },
    )
    assert tools.context["notes"][0]["passage"] == "Exact accepted text."


@pytest.mark.asyncio
async def test_evidence_pages_address_excerpt_and_preserve_explicit_provenance(
    monkeypatch, tmp_path
):
    source = {
        "key": "original",
        "excerpt": "Speaker: Useful evidence. अगला",
        "kind": "transcript",
        "role": "user_statement",
        "capture_chunk_ids": ["chunk" * 100] * 100,
    }

    def inspect(tools):
        page = json.loads(
            tools.dispatch(
                "read_material", {"store": "evidence", "key": "S001", "limit": 16}
            )
        )
        assert page["text"] == source["excerpt"][:16]
        assert page["source"]["role"] == "user_statement"
        assert "capture_chunk_ids" not in page["source"]
        rest = json.loads(
            tools.dispatch(
                "read_material",
                {"store": "evidence", "key": "S001", "offset": page["next_offset"]},
            )
        )
        assert page["text"] + rest["text"] == source["excerpt"]
        assert rest["end_of_material"]
        with pytest.raises(VaultToolError, match="length"):
            tools.dispatch(
                "read_material", {"store": "evidence", "key": "S001", "offset": 10000}
            )
        found = json.loads(
            tools.dispatch("search_material", {"store": "evidence", "query": "Useful"})
        )
        hit = next(x for x in found["matches"] if x["key"] == "S001")
        assert hit["excerpt"] == source["excerpt"][hit["offset"] : hit["offset"] + 500]
        provenance = json.loads(
            tools.dispatch(
                "read_material",
                {"store": "evidence", "key": "S001", "view": "provenance"},
            )
        )
        assert "capture_chunk_ids" in provenance["text"]
        tools.dispatch("finish_task", {"result": {"answer": "ok"}})

    install_pi(monkeypatch, tmp_path, [inspect])
    (
        await pi_tasks.run_task(
            stage="paging",
            instruction="Inspect",
            payload={},
            sources=[source],
            result_type=Answer,
        )
    ).result


@pytest.mark.asyncio
async def test_account_partitions_enforce_owned_passages_and_initialize_progress(
    monkeypatch, tmp_path
):
    sources = [
        {
            "key": "long",
            "excerpt": "a" * 7000 + "Unique second passage",
            "kind": "transcript",
            "role": "user_statement",
            "participation": "supporting",
            "metadata": {},
        }
    ]
    progress = AsyncMock()
    empty = dict(title="Activity", summary="", claims=[], questions=[], useful=False)

    def first(tools):
        assert progress.call_args_list[0].args == (0, 2)
        with pytest.raises(VaultToolError, match="verbatim"):
            tools.dispatch(
                "finish_task",
                {
                    "result": {
                        **empty,
                        "useful": True,
                        "claims": [
                            {
                                "text": "A fact",
                                "personal": True,
                                "citations": [
                                    {
                                        "source_key": "S001",
                                        "quote": "Unique second passage",
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        tools.dispatch("finish_task", {"result": empty})

    install_pi(
        monkeypatch,
        tmp_path,
        [first, empty, empty, review_result("ready", "Grounded")],
    )
    monkeypatch.setattr(session_accounts, "SOURCE_BUDGET", 7100)
    await session_accounts.build_session_account(
        sources, record=AsyncMock(), progress=progress
    )


@pytest.mark.asyncio
async def test_source_tools_supply_computed_local_times_without_changing_utc_identity(
    monkeypatch, tmp_path
):
    def inspect(tools):
        source = json.loads(tools.materials["S001"])
        assert source["started_at"] == "2026-09-04T08:15:02+00:00"
        assert source["local_times"]["started_at"] == "2026-09-04T13:45:02+05:30"
        tools.dispatch("finish_task", {"result": {"answer": "complete"}})

    install_pi(monkeypatch, tmp_path, [inspect])
    (
        await pi_tasks.run_task(
            stage="local-time",
            instruction="Inspect",
            payload={},
            result_type=Answer,
            sources=[{"key": "source", "started_at": "2026-09-04T08:15:02+00:00"}],
            accepted_context={"timezone": "Asia/Kolkata"},
        )
    ).result


def test_repeated_search_keeps_negative_result_visible(tmp_path):
    tools = pi_tasks.InvestigationTools(tmp_path, {}, {}, Answer)
    args = {"store": "vault", "query": "unresolved reference"}
    first = json.loads(tools.dispatch("search_material", args))
    repeat = json.loads(tools.dispatch("search_material", args))
    assert first["matches"] == repeat["matches"] == []
    assert first["total"] == repeat["total"] == 0
    assert first["result_ref"] == repeat["result_ref"]
    assert repeat["remaining_tool_calls"] == first["remaining_tool_calls"] - 1


@pytest.mark.asyncio
async def test_account_initial_brief_exposes_assigned_source_permissions(
    monkeypatch, tmp_path
):
    sources = [
        {
            "key": "screen",
            "excerpt": "A decision",
            "role": "application_state",
            "participation": "supporting",
        },
        {
            "key": "audio",
            "excerpt": "A local speaker",
            "role": "uncertain",
            "participation": "uncertain",
        },
    ]
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            {
                "title": "No changes",
                "summary": "",
                "claims": [],
                "questions": [],
                "useful": False,
            }
        ],
    )
    await session_accounts.prepare_source_account(sources, sources, record=AsyncMock())
    prompt = calls[0]["prompt"]
    brief, _ = json.JSONDecoder().raw_decode(prompt.split("Task brief:\n", 1)[1])
    assert [row["key"] for row in brief["source_scope"]] == ["S001", "S002"]
    assert brief["source_scope"][1]["claim_use"] == session_accounts.claim_use(
        sources[1]
    )
    assert brief["source_scope"][0]["participation"] == "supporting"
    assert brief["source_scope"][1]["participation"] == "uncertain"


def test_result_reference_repair_preserves_result_and_requires_inspected_notes(
    tmp_path,
):
    tools = pi_tasks.InvestigationTools(
        tmp_path, {}, {"Note.md": "Accepted fact"}, Answer
    )
    candidate = {
        "result": {"answer": "grounded"},
        "accepted_vault_result_refs": ["R999"],
    }
    with pytest.raises(VaultToolError, match="Unknown context reference"):
        tools.dispatch("finish_task", candidate)
    with pytest.raises(VaultToolError, match="Unknown context reference"):
        tools.dispatch(
            "revise_result", {"edits": [], "accepted_vault_result_refs": ["R001"]}
        )
    page = json.loads(
        tools.dispatch("read_material", {"store": "vault", "key": "Note.md"})
    )
    tools.dispatch(
        "revise_result",
        {"edits": [], "accepted_vault_result_refs": [page["result_ref"]]},
    )
    assert tools.result.answer == "grounded"
    assert tools.context["notes"][0]["path"] == "Note.md"
    assert tools.trace[0]["arguments"] == candidate


@pytest.mark.asyncio
async def test_source_inventory_is_complete_and_empty_search_exposes_navigation(
    monkeypatch, tmp_path
):
    source = {
        "key": "source-1",
        "excerpt": "Relevant evidence",
        "kind": "transcript",
        "role": "user_statement",
        "participation": "supporting",
    }

    def investigate(tools):
        index = json.loads(
            tools.dispatch(
                "read_material", {"store": "evidence", "key": "source-index.json"}
            )
        )
        rows = json.loads(index["text"])
        assert [row["key"] for row in rows] == ["S001"]
        assert rows[0]["length"] == len(source["excerpt"])
        missing = json.loads(
            tools.dispatch("search_material", {"store": "evidence", "query": "S999"})
        )
        assert missing["total"] == 0
        assert missing["next_offset"] is None
        assert missing["search_complete"]
        assert "S001" in missing["available_keys"]
        assert "source-index.json" in missing["available_keys"]
        tools.dispatch("finish_task", {"result": {"answer": "Account complete"}})

    install_pi(monkeypatch, tmp_path, [investigate])
    result = (
        await pi_tasks.run_task(
            stage="inventory",
            instruction="Assess",
            payload={},
            sources=[source],
            result_type=Answer,
        )
    ).result
    assert result.answer == "Account complete"


@pytest.mark.asyncio
async def test_account_uses_combined_budget_without_per_field_character_rewrites(
    monkeypatch, tmp_path
):
    text = "Supported source passage. " * 30
    source = {
        "key": "evidence",
        "excerpt": text,
        "kind": "transcript",
        "role": "user_statement",
        "participation": "supporting",
    }
    summary = "A concise account can allocate its available narrative space. " * 32
    claim = "A supported observation with necessary qualifications. " * 11

    def investigate(tools):
        tools.dispatch(
            "finish_task",
            {
                "result": {
                    "title": "Source account",
                    "summary": summary,
                    "claims": [
                        {
                            "text": claim,
                            "source_keys": ["S001"],
                            "personal": True,
                            "citations": [{"source_key": "S001", "quote": text}],
                        }
                    ],
                    "questions": [],
                    "useful": True,
                }
            },
        )

    install_pi(monkeypatch, tmp_path, [investigate])
    account = await session_accounts.prepare_source_account(
        [source], [source], record=None
    )
    assert account.summary == summary
    assert account.claims[0].text == claim
    assert account.claims[0].citations[0].quote == text


@pytest.mark.asyncio
async def test_source_tools_distinguish_task_claim_scope_from_source_attribution(
    monkeypatch, tmp_path
):
    sources = [
        {
            "key": key,
            "excerpt": "A supported statement",
            "kind": "transcript",
            "role": "user_statement",
            "participation": "supporting",
        }
        for key in ["assigned", "context"]
    ]

    def investigate(tools):
        assigned = json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "S001"})
        )
        context = json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "S002"})
        )
        assert assigned["source"]["task_claim_scope"] == {
            "use": "assigned_passages",
            "passages": [{"offset": 0, "length": len(sources[0]["excerpt"])}],
        }
        assert context["source"]["task_claim_scope"] == {
            "use": "context_only",
            "passages": [],
        }
        assert context["source"]["participation"] == "supporting"
        assert context["text"] == sources[1]["excerpt"]
        tools.dispatch(
            "finish_task",
            {
                "result": {
                    "title": "No new facts",
                    "summary": "",
                    "claims": [],
                    "questions": [],
                    "useful": False,
                }
            },
        )

    install_pi(monkeypatch, tmp_path, [investigate])
    await session_accounts.prepare_source_account(sources[:1], sources, record=None)


@pytest.mark.asyncio
async def test_supplied_accepted_context_is_citable_without_redundant_vault_reads(
    monkeypatch, tmp_path
):
    notes = {"Known.md": "Already accepted knowledge"}
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: notes)
    context = {
        "scope": {"user_id": "u"},
        "notes": [
            {
                "path": "Known.md",
                "hash": pi_tasks.canonical_hash(notes["Known.md"]),
                "offset": 0,
                "passage": notes["Known.md"],
            }
        ],
    }

    def investigate(tools):
        assert tools.passages["R001"][0]["passage"] == notes["Known.md"]
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "Uses supplied context"},
                "accepted_vault_result_refs": ["R001"],
            },
        )
        assert not any(row["tool"].startswith("read") for row in tools.trace)

    calls = install_pi(monkeypatch, tmp_path, [investigate])
    (
        await pi_tasks.run_task(
            stage="retained-context",
            instruction="Assess",
            payload={},
            result_type=Answer,
            accepted_context=context,
        )
    ).result
    assert '"result_ref": "R001"' in calls[0]["prompt"]
    assert context["notes"][0]["passage"] == notes["Known.md"]


def test_wrong_store_read_identifies_available_store_without_cross_store_fallback(
    tmp_path,
):
    tools = pi_tasks.InvestigationTools(tmp_path, {"draft.json": "{}"}, {}, Answer)
    with pytest.raises(VaultToolError, match="available in the evidence store"):
        tools.dispatch("read_material", {"store": "vault", "key": "draft.json"})
    assert not tools.reads


@pytest.mark.asyncio
async def test_repair_can_copy_exact_inspected_text_without_retyping_ocr(
    monkeypatch, tmp_path
):
    source = {
        "key": "ocr",
        "excerpt": "Heading \ue066 notebook\nAn exact supported detail.",
        "kind": "observation",
        "role": "application_state",
        "participation": "supporting",
    }

    def validate(citation):
        if citation.source_key != "ocr" or citation.quote != source["excerpt"]:
            raise ValueError("Quotation must match the source")

    def investigate(tools):
        page = json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "S001"})
        )
        with pytest.raises(VaultToolError):
            tools.dispatch(
                "finish_task",
                {"result": {"source_key": "S001", "quote": "Altered text"}},
            )
        with pytest.raises(VaultToolError, match="Unknown inspected result"):
            tools.dispatch(
                "revise_result",
                {
                    "edits": [
                        {
                            "op": "replace",
                            "path": "/quote",
                            "value_from": {"result_ref": "R999", "pointer": "/text"},
                        }
                    ]
                },
            )
        assert tools.result is None
        tools.dispatch(
            "revise_result",
            {
                "edits": [
                    {
                        "op": "replace",
                        "path": "/quote",
                        "value_from": {
                            "result_ref": page["result_ref"],
                            "pointer": "/text",
                        },
                    }
                ]
            },
        )
        assert "value" not in tools.trace[-1]["arguments"]["edits"][0]

    install_pi(monkeypatch, tmp_path, [investigate])
    result = (
        await pi_tasks.run_task(
            stage="exact-copy",
            instruction="Cite evidence",
            payload={},
            sources=[source],
            result_type=session_accounts.SourceQuote,
            validate=validate,
        )
    ).result
    assert result.quote == source["excerpt"]


@pytest.mark.asyncio
async def test_citation_feedback_supplies_copy_edits_without_echoing_bad_quote(
    monkeypatch, tmp_path
):
    source = {
        "key": "ocr",
        "excerpt": "Original \ue066 evidence with exact details.",
        "kind": "observation",
        "role": "application_state",
        "participation": "supporting",
    }
    wrong = "Invented text that should never be echoed as repair guidance"

    def investigate(tools):
        tools.dispatch("read_material", {"store": "evidence", "key": "S001"})
        with pytest.raises(VaultToolError) as failure:
            tools.dispatch(
                "finish_task",
                {
                    "result": {
                        "title": "Account",
                        "summary": "Source-backed detail",
                        "claims": [
                            {
                                "text": "A supported detail",
                                "source_keys": ["S001"],
                                "personal": True,
                                "citations": [{"source_key": "S001", "quote": wrong}],
                            }
                        ],
                        "questions": [],
                        "useful": True,
                    }
                },
            )
        message = str(failure.value)
        assert wrong not in message
        options, _ = json.JSONDecoder().raw_decode(message.split("\n", 1)[1])
        edit = options[0]["copy_options"][0]["edit"]
        assert edit["path"] == "/claims/0/citations/0/quote"
        tools.dispatch("revise_result", {"edits": [edit]})

    install_pi(monkeypatch, tmp_path, [investigate])
    account = await session_accounts.prepare_source_account(
        [source], [source], record=None
    )
    assert account.claims[0].citations[0].quote == source["excerpt"]


@pytest.mark.asyncio
async def test_account_budget_feedback_identifies_unchanged_edit_and_field_sizes(
    monkeypatch, tmp_path
):
    summary = "x" * session_accounts.ACCOUNT_TEXT_BUDGET

    def investigate(tools):
        result = {
            "title": "Title",
            "summary": summary,
            "claims": [],
            "questions": [],
            "useful": False,
        }
        with pytest.raises(
            VaultToolError, match="Reduce it by at least 5 characters"
        ) as first:
            tools.dispatch("finish_task", {"result": result})
        assert f'"/summary": {len(summary)}' in str(first.value)
        with pytest.raises(
            VaultToolError, match="No change: these edits equal the saved draft values"
        ) as repeated:
            tools.dispatch(
                "revise_result",
                {"edits": [{"op": "replace", "path": "/summary", "value": summary}]},
            )
        assert "Reduce it by at least 5 characters" in str(repeated.value)
        tools.dispatch(
            "revise_result",
            {
                "edits": [
                    {"op": "replace", "path": "/summary", "value": "No useful facts"}
                ]
            },
        )

    install_pi(monkeypatch, tmp_path, [investigate])
    account = await session_accounts.prepare_source_account([], [], record=None)
    assert account.summary == "No useful facts"


def test_vault_context_uses_the_same_result_reference_as_reads_and_searches(tmp_path):
    tools = pi_tasks.InvestigationTools(
        tmp_path,
        {"task.json": "Evidence is not accepted knowledge"},
        {"Known.md": "Accepted first. Accepted second.", "Other.md": "Accepted third."},
        Answer,
    )
    evidence = json.loads(
        tools.dispatch("read_material", {"store": "evidence", "key": "task.json"})
    )
    first = json.loads(
        tools.dispatch(
            "read_material", {"store": "vault", "key": "Known.md", "limit": 15}
        )
    )
    search = json.loads(
        tools.dispatch("search_material", {"store": "vault", "query": "Accepted"})
    )
    with pytest.raises(VaultToolError, match="Unknown context reference"):
        tools.dispatch(
            "finish_task",
            {
                "result": {"answer": "complete"},
                "accepted_vault_result_refs": [evidence["result_ref"]],
            },
        )
    tools.dispatch(
        "revise_result",
        {
            "edits": [],
            "accepted_vault_result_refs": [
                first["result_ref"],
                search["result_ref"],
                first["result_ref"],
            ],
        },
    )
    assert {(n["path"], n["passage"]) for n in tools.context["notes"]} == {
        ("Known.md", "Accepted first."),
        ("Known.md", "Accepted first. Accepted second."),
        ("Other.md", "Accepted third."),
    }
    assert all("context_ref" not in result for result in [first, search])


def test_provenance_read_preserves_metadata_without_repeating_source_text(tmp_path):
    source = {
        "key": "S001",
        "excerpt": "Long source text " * 1000,
        "evidence_id": "source-1",
        "kind": "observation",
        "role": "application_state",
        "metadata": {
            "text_source": "accessibility",
            "app_name": "Example",
            "frame_id": 17,
        },
        "locator": {"capture_source_id": "device-1", "modality": "screen"},
    }
    tools = pi_tasks.InvestigationTools(
        tmp_path,
        {"S001": json.dumps(source)},
        {},
        Answer,
        source_records={"S001": source},
    )
    result = json.loads(
        tools.dispatch(
            "read_material", {"store": "evidence", "key": "S001", "view": "provenance"}
        )
    )
    provenance = json.loads(result["text"])
    assert "excerpt" not in provenance
    assert provenance["metadata"] == source["metadata"]
    assert result["end_of_material"] is True
    assert result["source"]["context"]["text_source"] == "accessibility"
    assert (
        json.loads(
            tools.dispatch("read_material", {"store": "evidence", "key": "S001"})
        )["text"]
        == source["excerpt"][:4000]
    )
