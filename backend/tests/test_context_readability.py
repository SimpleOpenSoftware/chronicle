"""Full-note changes and historical lookup excerpts remain distinct assessment inputs."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.services import source_search
from backend.services.timeline import accepted_context as context
from backend.services.timeline import memory_sources, pi_tasks, review
from backend.workers import source_search_jobs


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, _limit):
        return self

    async def to_list(self):
        return self.rows


def test_full_snapshot_diff_retains_complete_before_and_after():
    before = {"Edited.md": "Complete old note", "Deleted.md": "Deleted content"}
    current = {"Edited.md": "Complete new note", "Added.md": "Added content"}
    assert context._changed_notes(before, current) == {
        "Added.md": {"before": None, "after": "Added content"},
        "Deleted.md": {"before": "Deleted content", "after": None},
        "Edited.md": {"before": "Complete old note", "after": "Complete new note"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["ready", "failed", "same_snapshot"])
async def test_registered_historical_check_preserves_lookup_context_without_inventing_note_history(
    monkeypatch, execution
):
    current = {
        "Known.md": "Full current note, including sections not previously consulted.",
        "Unconsulted.md": "An existing note that was not previously read.",
        "Unchanged.md": "Already consulted current note.",
    }
    accepted = {
        "notes": [
            {
                "path": "Known.md",
                "hash": "old-full-note-hash",
                "passage": "First inspected passage",
            },
            {
                "path": "Known.md",
                "hash": "old-full-note-hash",
                "passage": "Second inspected passage",
            },
            {
                "path": "Deleted.md",
                "hash": "deleted-full-note-hash",
                "passage": "Prior deleted-note passage",
            },
            {
                "path": "Unchanged.md",
                "hash": context.canonical_hash(current["Unchanged.md"]),
                "passage": "consulted current note",
            },
        ],
        "scope_hash": (
            context.canonical_hash(current)
            if execution == "same_snapshot"
            else "previous-vault-hash"
        ),
        "review": {"verdict": "ready"},
    }
    proposal = SimpleNamespace(
        id="proposal-id",
        proposal_id="proposal",
        user_id="owner",
        memory_space_id=None,
        source_scope=[],
        source_scope_hash="source-hash",
        selection_hash="selection-hash",
        excluded_source_keys=[],
        generation=1,
        active=False,
        state="no_changes",
        accepted_context=accepted,
        account={"title": "Prepared account"},
        questions=[],
        refresh_assessment=None,
    )
    request_updates = AsyncMock()
    proposal_updates = AsyncMock()
    assessment_updates = AsyncMock()
    database = SimpleNamespace(
        context_assessment_requests=SimpleNamespace(
            find=Mock(
                return_value=Rows([{"_id": "request", "proposal_id": "proposal"}])
            ),
            update_one=request_updates,
        ),
        vault_context_checks=SimpleNamespace(
            find_one=AsyncMock(return_value={"notes": current, "pending_changes": {}}),
        ),
        session_context_assessments=SimpleNamespace(update_one=assessment_updates),
    )
    inspected = []

    async def investigate(**kwargs):
        inspected.append(kwargs["payload"])
        await kwargs["record"](
            {
                "artifact_hash": "exact-investigation",
                "operation": "pi_refresh_assessment",
            }
        )
        if execution == "failed":
            raise RuntimeError("Investigation incomplete")
        result = context.Assessment(
            verdict="useful",
            reason="Relevant accepted context can improve the account.",
            relevant_paths=["Unconsulted.md"],
        )
        kwargs["validate"](result)
        return pi_tasks.TaskOutcome(result=result, context={})

    monkeypatch.setattr(source_search_jobs, "distributed_lock", unlocked)
    monkeypatch.setattr(source_search, "db", lambda: database)
    monkeypatch.setattr(context, "snapshot", lambda *_: current)
    monkeypatch.setattr(
        context.MemoryReviewProposal, "find_one", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(
        context.MemoryReviewProposal,
        "get_pymongo_collection",
        lambda: SimpleNamespace(update_one=proposal_updates),
    )
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(memory_sources, "source_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(pi_tasks, "run_task", investigate)

    assert await source_search_jobs.assess_context_job.__wrapped__("owner|main") == 1
    saved = assessment_updates.call_args.args[1]["$set"]
    material = saved["changes"]
    assert material["Known.md"]["previously_consulted_passages"] == [
        "First inspected passage",
        "Second inspected passage",
    ]
    assert material["Known.md"]["previous_note_hashes"] == ["old-full-note-hash"]
    assert material["Known.md"]["after"] == current["Known.md"]
    assert "before" not in material["Known.md"]
    assert material["Unconsulted.md"]["comparison"] == "previous_content_unavailable"
    assert material["Unconsulted.md"]["previously_consulted_passages"] == []
    assert "before" not in material["Unconsulted.md"]
    assert material["Deleted.md"]["after"] is None
    assert "Unchanged.md" not in material
    assert proposal.state == "no_changes"
    assert request_updates.call_args.args[1] == {"$set": {"state": "complete"}}
    result = saved["result"]
    if execution == "same_snapshot":
        assert inspected == []
        assert result["context_unchanged"] and result["verdict"] == "unrelated"
    else:
        assert inspected[0]["changes"] == material
        assert result["artifact_hash"] == "exact-investigation"
        assert result["verdict"] == ("uncertain" if execution == "failed" else "useful")
        assert bool(result.get("error")) == (execution == "failed")
