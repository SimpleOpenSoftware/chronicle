"""Generation remains scoped and isolated across its readable workflow phases."""

from contextlib import asynccontextmanager, nullcontext
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.memory.session_write import SessionDraftResult
from backend.services.memory.vault_manager import ConvDocVaultManager
from backend.services.timeline import (
    accepted_context,
    pi_tasks,
    review,
    session_accounts,
    sessions,
)
from backend.workers import session_jobs


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "writer_result, expected",
    [
        ("valid", "pending"),
        ("missing_evidence", "failed"),
        ("foreign_episode", "failed"),
        ("incomplete", "failed"),
        ("changed_selection", "stale"),
    ],
)
async def test_registered_generation_never_publishes_unvalidated_staged_changes(
    monkeypatch, tmp_path, writer_result, expected
):
    """Exercise the worker, account preparation, real staging and diff validation."""
    owner = "review-owner"
    episode = SimpleNamespace(
        episode_key="episode",
        episode_id="episode-id",
        started_at=review.utcnow(),
        audio_ranges=[],
        related_conversation_ids=[],
    )
    source = {
        "key": "selected-source",
        "participation": "supporting",
        "excerpt": "A supported decision.",
    }
    proposal = SimpleNamespace(
        id="proposal",
        proposal_id="proposal",
        user_id=owner,
        memory_space_id=None,
        state="queued",
        active=True,
        attempts=0,
        source_kind="timeline",
        timezone="Asia/Kolkata",
        local_date=date(2026, 9, 4),
        session_key="session",
        excluded_source_keys=[],
        inference_runs=[],
        writer_inference_artifacts=[],
        revision_feedback=None,
        supersedes_proposal_id=None,
        correction_of=[],
        withdrawn=False,
        recording_id=None,
        questions=[],
        changes=[],
    )
    proposal.model_dump = lambda: {"user_id": owner}
    live_root = tmp_path / owner
    live_root.mkdir()
    (live_root / "Existing.md").write_text("Accepted content stays intact.")
    staging_roots = []

    class Writer:
        config = SimpleNamespace()

        def __init__(self, config=None):
            self.vault = ConvDocVaultManager(tmp_path)
            self.last_day_source_episode_keys_by_path = {}
            self.last_day_source_evidence_keys_by_path = {}

        async def draft_session_memory(self, source, user):
            assert source.permissions.claim_sources == {"C001": ["selected-source"]}
            stage = self.vault.user_root(user)
            staging_roots.append(stage)
            assert stage != live_root
            assert (
                stage / "Existing.md"
            ).read_text() == "Accepted content stays intact."
            (stage / "Decision.md").write_text("A supported decision.")
            self.last_day_source_episode_keys_by_path = {
                "Decision.md": [
                    "other-episode" if writer_result == "foreign_episode" else "episode"
                ]
            }
            self.last_day_source_evidence_keys_by_path = {
                "Decision.md": (
                    [] if writer_result == "missing_evidence" else ["selected-source"]
                )
            }
            if writer_result == "incomplete":
                raise RuntimeError("Writer interrupted after a staged write")
            return SessionDraftResult(
                outcome="complete",
                touched=["Decision.md"],
                source_episode_keys_by_path=self.last_day_source_episode_keys_by_path,
                source_evidence_keys_by_path=self.last_day_source_evidence_keys_by_path,
            )

    async def validate_selection(p):
        if writer_result == "changed_selection" and staging_roots:
            raise review.SelectionChanged(
                "The selected evidence changed during drafting"
            )
        return [episode], []

    account = session_accounts.SessionAccount(
        title="Decision",
        summary="A supported decision.",
        useful=True,
        questions=[],
        claims=[
            session_accounts.AccountClaim(
                text="A supported decision.",
                personal=True,
                source_keys=["selected-source"],
                citations=[
                    session_accounts.SourceQuote(
                        source_key="selected-source", quote="A supported decision."
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(review, "vault_run_lock", lambda *_: nullcontext())
    monkeypatch.setattr(
        review.MemoryReviewProposal, "get", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(
        review.MemoryReviewProposal, "find_one", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(review, "persist_generation", AsyncMock())
    monkeypatch.setattr(review, "validate_selection", validate_selection)
    monkeypatch.setattr(review, "evidence_sources", lambda *_: [source])
    monkeypatch.setattr(review, "source_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(review, "apply_dispositions", lambda sources, *_: sources)
    monkeypatch.setattr(
        review, "build_session_account", AsyncMock(return_value=account)
    )
    monkeypatch.setattr(review, "get_memory_service", lambda: Writer())
    monkeypatch.setattr(review, "ChronicleMemoryService", Writer)
    monkeypatch.setattr(accepted_context, "for_sources", AsyncMock(return_value={}))
    monkeypatch.setattr(accepted_context, "snapshot", lambda *_: {})
    monkeypatch.setattr(pi_tasks, "context_is_current", lambda *_: True)
    monkeypatch.setattr(sessions, "publish_progress", AsyncMock())

    if expected == "failed":
        with pytest.raises(RuntimeError):
            await session_jobs.generate_session_memory_job.__wrapped__(
                proposal.proposal_id
            )
    else:
        assert (
            await session_jobs.generate_session_memory_job.__wrapped__(
                proposal.proposal_id
            )
            == expected
        )

    assert proposal.state == expected
    assert (live_root / "Existing.md").read_text() == "Accepted content stays intact."
    assert not (live_root / "Decision.md").exists()
    assert staging_roots and all(not root.exists() for root in staging_roots)
    if expected == "pending":
        assert len(proposal.changes) == 1
        assert proposal.changes[0].source_evidence_keys == ["selected-source"]
    else:
        assert proposal.changes == []
        assert proposal.error
