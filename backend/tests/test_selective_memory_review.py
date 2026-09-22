"""Selection, freshness and crash recovery through the production review entry points."""

import os
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from fastapi import BackgroundTasks, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.memory_audit import MemoryAuditEntry
from backend.models.session_memory import MemorySourceDecision, SessionPreparation
from backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    MemoryFreshnessResult,
    MemoryReviewProposal,
    TimelineDay,
    TimelineDaySnapshot,
    TimelineEpisode,
    TimelinePublicationJournal,
)
from backend.routers.modules import timeline_routes
from backend.services.memory import note_review
from backend.services.memory.agent import review_agent
from backend.services.memory.agent.memory_agent import build_write_task
from backend.services.memory.base import DayWriteOutcome
from backend.services.memory.session_write import SessionDraftResult
from backend.services.memory.vault_manager import ConvDocVaultManager
from backend.services.timeline import review, sessions
from backend.services.timeline.review_storage import assert_memory_review_storage_ready
from backend.services.timeline.vault_day_index import ensure_day_episode_index

_enqueue_session_job = sessions._enqueue


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


@pytest.fixture
async def selection_db(monkeypatch):
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27018"),
        serverSelectionTimeoutMS=2000,
    )
    database = client["test_selective_memory_review"]
    await init_beanie(
        database=database,
        document_models=[
            DirtyEvidenceRange,
            MemoryReviewProposal,
            TimelineEpisode,
            TimelineDay,
            TimelinePublicationJournal,
            MemoryAuditEntry,
            MemorySourceDecision,
            SessionPreparation,
        ],
    )
    monkeypatch.setattr(
        sessions, "_enqueue", lambda *args, **kwargs: "isolated-session-job"
    )

    async def run_decision(p):
        from backend.workers.session_jobs import apply_session_memory_job

        return await apply_session_memory_job.__wrapped__(p.proposal_id)

    monkeypatch.setattr(sessions, "enqueue_decision", run_decision)
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(review, "vault_run_lock", lambda _: nullcontext())
    monkeypatch.setattr(note_review, "vault_run_lock", lambda _: nullcontext())
    yield database
    await client.drop_database(database.name)
    client.close()


def proposal(**kwargs):
    values = dict(
        request_id="request-one",
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        snapshot_id="a" * 64,
        selected_episodes=[EpisodeRevisionRef(episode_key="ep-one", revision=1)],
        selected_tokens=["ep-one:1"],
        selection_hash="b" * 64,
    )
    values.update(kwargs)
    return MemoryReviewProposal(**values)


@pytest.fixture
async def vault(selection_db, tmp_path, monkeypatch):
    class Service:
        config = SimpleNamespace()

        def __init__(self, config=None):
            self.vault = ConvDocVaultManager(tmp_path)

        async def draft_session_memory(self, source, user):
            return SessionDraftResult(outcome="complete")

    service = Service()
    monkeypatch.setattr(review, "ChronicleMemoryService", Service)
    monkeypatch.setattr(review, "get_memory_service", lambda: service)
    root = service.vault.user_root("user-one")
    root.mkdir()
    return root


async def pending(vault, **kwargs):
    p = proposal(state="pending", **kwargs)
    before = review._snapshot(vault)
    p.vault_base_hash = review._retain_snapshot(vault, before)
    p.changes = review.build_potential_changes(
        before,
        {**before, "Topics/Plan.md": "Plan from September"},
        source_episode_keys_by_path={"Topics/Plan.md": ["ep-one"]},
    )
    await p.insert()
    return p


@pytest.mark.asyncio
async def test_new_adjacent_note_regenerates_without_applying(vault, monkeypatch):
    p = await pending(vault)
    (vault / "Topics").mkdir()
    (vault / "Topics/Existing Plan.md").write_text("Same plan under another name")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(
            verdict="affected",
            reason="Reuse Existing Plan",
            relevant_paths=["Topics/Existing Plan.md"],
        )
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    assert await review.resolve_memory_review(p, [p.changes[0].change_id]) == "checking"
    assert await review.process_memory_review_decision(p) == "regenerating"
    old = await MemoryReviewProposal.get(p.id)
    new = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.request_id == p.request_id,
        MemoryReviewProposal.generation == 2,
    )
    assert old.changes == p.changes
    assert new.proposal_id != p.proposal_id and new.requested_change_ids == []
    assert new.supersedes_proposal_id == p.proposal_id
    assert not (vault / "Topics/Plan.md").exists()
    assert "Topics/Existing Plan.md" in checker.call_args.args[2]
    with pytest.raises(review.MemoryReviewError, match="no longer pending"):
        await review.resolve_memory_review(old, [p.changes[0].change_id])


@pytest.mark.asyncio
async def test_unrelated_note_passes_check_and_exact_diff_is_applied(
    vault, monkeypatch
):
    p = await pending(vault)
    (vault / "People").mkdir()
    (vault / "People/Other.md").write_text("Unrelated")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        review,
        "check_freshness",
        AsyncMock(
            return_value=MemoryFreshnessResult(
                verdict="unaffected", reason="Unrelated person"
            )
        ),
    )
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applied"
    assert (vault / "Topics/Plan.md").read_text() == p.changes[0].after_text
    assert await MemoryAuditEntry.find_all().count() == 1
    assert (vault / "People/Other.md").read_text() == "Unrelated"


@pytest.mark.asyncio
async def test_failed_checker_preserves_retryable_diff(vault, monkeypatch):
    p = await pending(vault)
    (vault / "extra.md").write_text("Changed")
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        review,
        "check_freshness",
        AsyncMock(side_effect=RuntimeError("checker offline")),
    )
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "pending"
    stored = await MemoryReviewProposal.get(p.id)
    assert stored.changes == p.changes and stored.requested_change_ids == []
    assert not (vault / "Topics/Plan.md").exists()


@pytest.mark.asyncio
async def test_file_race_rechecks_before_write(vault, monkeypatch):
    p = await pending(vault)
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    apply = review._apply_review_sync
    calls = []

    def racing(p, root):
        calls.append(1)
        if len(calls) == 1:
            (root / "Other.md").write_text("arrived after check")
        return apply(p, root)

    monkeypatch.setattr(review, "_apply_review_sync", racing)
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(verdict="unaffected", reason="Unrelated")
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applied"
    assert len(calls) == 2 and checker.await_count == 2


@pytest.mark.asyncio
async def test_apply_recovers_after_audit_failure_without_duplicate_write(
    vault, monkeypatch
):
    p = await pending(vault)
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    real_audit = review.record_vault_change
    audit = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(review, "record_vault_change", audit)
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applying"
    assert (vault / "Topics/Plan.md").exists()
    monkeypatch.setattr(review, "record_vault_change", real_audit)
    assert (await review.process_memory_review_queue())["applied"] == 1
    assert await MemoryAuditEntry.find_all().count() == 1
    assert (await review.process_memory_review_queue())["considered"] == 0


@pytest.mark.asyncio
async def test_partial_changes_do_not_mark_whole_selection_remembered(
    vault, monkeypatch
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Daily/2026-09-05.md": "daily"},
        source_episode_keys_by_path={"Daily/2026-09-05.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    await review.process_memory_review_decision(p)
    stored = await MemoryReviewProposal.get(p.id)
    status = review.episode_review_outcomes([stored])["ep-one:1"]
    assert status["state"] == "partial" and not status["daily_recorded"]
    assert not (vault / "Daily/2026-09-05.md").exists()


def episode(key, day=5):
    return TimelineEpisode.model_construct(
        episode_id=key,
        episode_key=key,
        revision=1,
        user_id="user-one",
        run_id="run-one",
        local_date=date(2026, 9, day),
        timezone="Asia/Kolkata",
        started_at=datetime(2026, 9, day, 9, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, day, 10, tzinfo=timezone.utc),
        kind="work",
        title=key,
        summary="Bounded work summary",
        status="settled",
        confidence=0.9,
        activity_mode="foreground",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_selection_uses_committed_midnight_evidence_but_waits_for_requested_reconciliation(
    vault, monkeypatch, authorized
):
    a = episode("cross-midnight")
    a.started_at = datetime(2026, 9, 5, 18, 25, tzinfo=timezone.utc)
    a.ended_at = datetime(2026, 9, 5, 18, 35, tzinfo=timezone.utc)
    await a.insert()
    refs = [EpisodeRevisionRef(episode_key=a.episode_key, revision=1)]
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64, episode_revisions=refs, evidence_state_hash="c" * 64
    )
    day = TimelineDay(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    start = datetime(2026, 9, 5, 18, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 5, 18, 45, tzinfo=timezone.utc)
    await DirtyEvidenceRange(
        user_id="user-one",
        started_at=start,
        ended_at=end,
        evidence_revision=1,
        not_before=start,
        force_after=end,
        state="authorized_pending" if authorized else "pending",
        **(
            {
                "dispatch_authorized_at": start,
                "reconciliation_request_id": "requested",
                "authorized_started_at": start,
                "authorized_ended_at": end,
            }
            if authorized
            else {}
        ),
    ).insert()
    published = AsyncMock(return_value=True)
    monkeypatch.setattr(review, "episode_revision_is_published", published)

    async def select():
        return await timeline_routes.create_timeline_memory_selection(
            day.local_date,
            timeline_routes.CreateMemorySelectionRequest(
                timezone=day.timezone,
                snapshot_id=day.current_snapshot_id,
                episodes=refs,
            ),
            BackgroundTasks(),
            SimpleNamespace(id="user-one"),
        )

    if authorized:
        with pytest.raises(HTTPException) as error:
            await select()
        assert error.value.status_code == 409
        assert await MemoryReviewProposal.find_all().count() == 0
    else:
        result = await select()
        row = await MemoryReviewProposal.find_one(
            {"proposal_id": result["proposals"][0]["proposal_id"]}
        )
        assert row.state == "queued"
        published.return_value = False
        with pytest.raises(review.SelectionChanged, match="committed"):
            await review.validate_selection(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_present", [False, True])
async def test_registered_session_recovery_checks_worker_ownership(
    selection_db, monkeypatch, worker_present
):
    from fakeredis import FakeStrictRedis
    from rq import Queue, Worker

    from backend.controllers import queue_controller
    from backend.services import source_search
    from backend.workers.session_jobs import generate_session_memory_job

    redis = FakeStrictRedis()
    queue = Queue("memory", connection=redis)
    job = queue.enqueue(generate_session_memory_job, "recover-proposal")
    worker = Worker([queue], connection=redis, name="previous-worker")
    worker.register_birth()
    worker.prepare_job_execution(job)
    if not worker_present:
        redis.delete(worker.key)
    monkeypatch.setattr(queue_controller, "redis_conn", redis)
    monkeypatch.setattr(source_search, "db", lambda: selection_db)
    p = proposal(
        proposal_id="recover-proposal",
        state="generating",
        session_key="session",
        job_id=job.id,
        attempts=1,
        completed_sources=1,
        total_sources=3,
    )
    await p.insert()
    enqueue = AsyncMock()
    monkeypatch.setattr(sessions, "enqueue_memory", enqueue)
    await sessions.prepare_recent_sessions()
    current = await MemoryReviewProposal.get(p.id)
    assert current.state == ("generating" if worker_present else "queued")
    assert current.completed_sources == 1
    assert enqueue.await_count == (0 if worker_present else 1)


@pytest.mark.asyncio
async def test_session_continuation_does_not_overtake_waiting_work(
    selection_db, monkeypatch
):
    from fakeredis import FakeStrictRedis
    from rq import Queue

    from backend.controllers import queue_controller

    redis = FakeStrictRedis()
    queue = Queue("memory", connection=redis)
    monkeypatch.setattr(queue_controller, "redis_conn", redis)
    monkeypatch.setattr(queue_controller, "memory_queue", queue)
    monkeypatch.setattr(sessions, "_enqueue", _enqueue_session_job)
    waiting = await proposal(
        proposal_id="waiting", request_id="waiting", priority=100, state="queued"
    ).insert()
    continuing = await proposal(
        proposal_id="continuing",
        request_id="continuing",
        selected_tokens=["ep-two:1"],
        priority=100,
        state="queued",
        stage="combining",
        completed_sources=2,
        total_sources=2,
    ).insert()
    await sessions.enqueue_memory(waiting)
    await sessions.enqueue_memory(continuing)
    assert queue.job_ids == [waiting.job_id, continuing.job_id]
    await sessions.enqueue_memory(continuing)
    assert queue.count == 2
    requested = await proposal(
        proposal_id="new-request",
        request_id="new-request",
        selected_tokens=["ep-three:1"],
        priority=100,
        state="queued",
    ).insert()
    await sessions.enqueue_memory(requested)
    assert queue.job_ids == [requested.job_id, waiting.job_id, continuing.job_id]


@pytest.mark.asyncio
async def test_paused_generation_retry_uses_durable_queue(selection_db, monkeypatch):
    p = await proposal(
        state="paused",
        failure_kind="budget_exhausted",
        error="Saved investigation limit",
    ).insert()
    monkeypatch.setattr(timeline_routes, "distributed_lock", unlocked)
    enqueue = AsyncMock()
    monkeypatch.setattr(sessions, "enqueue_memory", enqueue)
    tasks = BackgroundTasks()
    response = await timeline_routes.regenerate_timeline_memory_review(
        p.proposal_id, tasks, SimpleNamespace(id=p.user_id)
    )
    replacement = enqueue.call_args.args[0]
    assert replacement.proposal_id != p.proposal_id
    assert replacement.generation == p.generation + 1
    assert replacement.state == "queued"
    assert not tasks.tasks
    old = await MemoryReviewProposal.get(p.id)
    assert old.failure_kind == "budget_exhausted" and old.error == p.error
    assert response["proposal"]["proposal_id"] == replacement.proposal_id


@pytest.mark.asyncio
async def test_creation_duplicates_and_partial_selection_leave_siblings(
    vault, monkeypatch
):
    a, b = episode("a"), episode("b")
    for e in [a, b]:
        await e.insert()
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64,
        episode_revisions=[
            EpisodeRevisionRef(episode_key=e.episode_key, revision=1) for e in [a, b]
        ],
        evidence_state_hash="c" * 64,
    )
    day = TimelineDay(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    refs = [snapshot.episode_revisions[0]]
    first = await review.create_memory_selection(
        "user-one", day.local_date, day.timezone, snapshot.snapshot_id, refs
    )
    second = await review.create_memory_selection(
        "user-one", day.local_date, day.timezone, snapshot.snapshot_id, refs
    )
    assert first[0].proposal_id == second[0].proposal_id
    assert await MemoryReviewProposal.find_all().count() == 1
    assert "b:1" not in review.episode_review_outcomes(first)
    assert (await TimelineDay.get(day.id)).review_state == "episodes_pending"


@pytest.mark.asyncio
async def test_generation_fifo_not_source_date_and_pending_does_not_block(
    vault, monkeypatch
):
    one = proposal(request_id="sept", selected_tokens=["a:1"])
    two = proposal(
        request_id="jan", selected_tokens=["b:1"], local_date=date(2026, 1, 1)
    )
    await one.insert()
    await two.insert()
    monkeypatch.setattr(review, "refresh_memory_selection_states", AsyncMock())
    calls = []

    async def generate(p):
        calls.append(p.request_id)
        p.state = "pending"
        await p.save()
        return "pending"

    monkeypatch.setattr(sessions, "enqueue_memory", generate)
    assert (await review.process_memory_review_queue())["queued"] == 2
    assert calls == ["sept", "jan"]


@pytest.mark.asyncio
async def test_reference_generation_does_not_require_daily_entries(vault, monkeypatch):
    e = episode("ep-one")
    p = proposal(selection_hash=review.selection_hash([e], []))
    await p.insert()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([e], [])))
    (vault / "Daily").mkdir()
    (vault / "Daily/2026-09-05.md").write_text(
        "# 2026-09-05\n\n## Episodes\n\n- 08:00–09:00 · work · routine — Earlier <!-- episode_key:earlier -->\n"
    )
    assert await review.generate_memory_review(p) == "no_changes"
    generated = await MemoryReviewProposal.get(p.id)
    assert generated.changes == [] and not generated.active
    assert "episode_key:earlier" in (vault / "Daily/2026-09-05.md").read_text()
    assert "episode_key:ep-one" not in (vault / "Daily/2026-09-05.md").read_text()


def test_cumulative_daily_keeps_accepted_and_ignores_unselected():
    before = "# Day\n\n## Episodes\n\n- 08:00–09:00 · work <!-- episode_key:old -->\n\n## Notes\n\nHuman note\n"
    generated = (
        "# Day\n\n## Episodes\n\n- 10:00–11:00 · work <!-- episode_key:new -->\n"
    )
    result = review.cumulative_daily(before, generated, {"new"})
    assert result.index("08:00") < result.index("10:00")
    assert "Human note" in result and result.count("episode_key:old") == 1


def test_snapshot_includes_templates_and_detects_new_notes(tmp_path):
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates/Person.md").write_text("Guidance")
    baseline = review._snapshot(tmp_path)
    assert baseline["Templates/Person.md"] == "Guidance"
    (tmp_path / "New.md").write_text("New")
    assert review._snapshot_hash(baseline) != review._snapshot_hash(
        review._snapshot(tmp_path)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", [1, 2])
async def test_every_note_boundary_recovers_from_persisted_intent(
    vault, monkeypatch, failure_at
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    write = review._atomic_write
    calls = []

    def fail(target, content):
        if target.suffix == ".md":
            calls.append(target)
            if len(calls) == failure_at:
                raise RuntimeError("interrupted note write")
        write(target, content)

    monkeypatch.setattr(note_review, "_atomic_write", fail)
    await review.resolve_memory_review(p, [c.change_id for c in p.changes])
    assert await review.process_memory_review_decision(p) == "applying"
    monkeypatch.setattr(note_review, "_atomic_write", write)
    assert (await review.process_memory_review_queue())["applied"] == 1
    assert (vault / "Topics/Second.md").read_text() == "Second fact"
    assert await MemoryAuditEntry.find_all().count() == 2


@pytest.mark.asyncio
async def test_old_source_changes_require_correction_but_sibling_snapshot_does_not(
    vault, monkeypatch
):
    e = episode("ep-one")
    p = await pending(vault, selection_hash=review.selection_hash([e], []))
    selection = AsyncMock(return_value=([e], []))
    monkeypatch.setattr(review, "_selection", selection)
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "pending"
    from backend.models.timeline import TimelineEvidenceRef

    e.evidence_refs = [
        TimelineEvidenceRef(
            evidence_id="corrected-source",
            locator={"capture_source_id": "phone", "modality": "screen"},
            kind="observation",
            role="user_statement",
            started_at=e.started_at,
            ended_at=e.ended_at,
            excerpt="Corrected source evidence",
            content_hash="changed-content",
        )
    ]
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "stale"
    p = await MemoryReviewProposal.get(p.id)
    p.state = "applied"
    p.accepted_change_ids = [p.changes[0].change_id]
    await p.save()
    await review.refresh_memory_selection_states()
    assert (await MemoryReviewProposal.get(p.id)).state == "correction_required"
    assert not (
        vault / "Topics/Plan.md"
    ).exists()  # state reconciliation never writes notes


@pytest.mark.asyncio
async def test_same_key_home_date_correction_preserves_other_daily_entries(
    vault, monkeypatch
):
    old_day = "2026-09-04"
    old_note = "# Old day\n\n## Episodes\n\n- 08:00–09:00 · work <!-- episode_key:ep-one -->\n- 10:00–11:00 · work <!-- episode_key:unrelated -->\n"
    (vault / "Daily").mkdir()
    (vault / f"Daily/{old_day}.md").write_text(old_note)
    old = proposal(
        state="correction_required", active=False, local_date=date(2026, 9, 4)
    )
    old.changes = review.build_potential_changes(
        {},
        {f"Daily/{old_day}.md": old_note},
        source_episode_keys_by_path={f"Daily/{old_day}.md": ["ep-one"]},
    )
    old.accepted_change_ids = [old.changes[0].change_id]
    await old.insert()
    e = episode("ep-one")
    current = proposal(
        request_id="correction",
        correction_of=[old.proposal_id],
        correction_episode_keys=["ep-one"],
    )
    await current.insert()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([e], [])))
    assert await review.generate_memory_review(current) == "pending"
    generated = await MemoryReviewProposal.get(current.id)
    changes = {c.note_path: c for c in generated.changes}
    assert "episode_key:ep-one" not in changes[f"Daily/{old_day}.md"].after_text
    assert "episode_key:unrelated" in changes[f"Daily/{old_day}.md"].after_text
    assert "Daily/2026-09-05.md" not in changes
    assert (vault / f"Daily/{old_day}.md").read_text() == old_note


@pytest.mark.asyncio
async def test_readonly_checker_sees_new_names_and_fails_closed_on_incomplete_output(
    vault, monkeypatch
):
    p = await pending(vault)
    current = {"Topics/Other Name.md": "The plan already exists here"}
    calls = []

    async def assess(root, *, task, schema):
        calls.append(task)
        assert (root / "Topics/Other Name.md").exists()
        return review_agent.ReviewResult(
            reported=True,
            assessment={
                "verdict": "affected",
                "reason": "Reuse Other Name",
                "relevant_paths": ["Topics/Other Name.md"],
            },
        )

    monkeypatch.setattr(review_agent, "assess_vault_context", assess)
    result = await review.check_freshness(p, {}, current)
    assert result.verdict == "affected" and "Other Name.md" in calls[0]

    async def incomplete(*args, **kwargs):
        return review_agent.ReviewResult(
            reported=True,
            assessment={"verdict": "unaffected", "reason": "ok"},
            warnings=["truncated"],
        )

    monkeypatch.setattr(review_agent, "assess_vault_context", incomplete)
    assert (await review.check_freshness(p, {}, current)).verdict == "uncertain"


def test_temporal_task_distinguishes_capture_and_processing_and_individual_claims():
    task = build_write_task(
        "On this January recording: yesterday I left Company A. A June note says Company B.",
        "2026-01-05",
        date="2026-01-05T10:00:00+05:30",
        record="day",
    )
    assert "Processing time is not event time" in task
    assert (
        "yesterday/tomorrow relative to the timestamp of the supporting evidence"
        in task
    )
    assert "individual claims, not note last_seen" in task
    assert "2026-01-05T10:00:00+05:30" in task


@pytest.mark.asyncio
async def test_storage_guard_refuses_implicit_day_proposal_conversion(selection_db):
    await assert_memory_review_storage_ready(selection_db)
    await selection_db.memory_review_proposals.insert_one(
        {"proposal_id": "old-human-decision", "state": "applied"}
    )
    with pytest.raises(RuntimeError, match="explicit cutover"):
        await assert_memory_review_storage_ready(selection_db)
    assert (
        await selection_db.memory_review_proposals.count_documents(
            {"proposal_id": "old-human-decision"}
        )
        == 1
    )


@pytest.mark.asyncio
async def test_selection_routes_authorize_exact_revisions_and_generation(
    vault, monkeypatch
):
    a = episode("ep-one")
    await a.insert()
    refs = [EpisodeRevisionRef(episode_key=a.episode_key, revision=1)]
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64, episode_revisions=refs, evidence_state_hash="c" * 64
    )
    day = TimelineDay(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    tasks = BackgroundTasks()
    result = await timeline_routes.create_timeline_memory_selection(
        day.local_date,
        timeline_routes.CreateMemorySelectionRequest(
            timezone=day.timezone, snapshot_id=day.current_snapshot_id, episodes=refs
        ),
        tasks,
        SimpleNamespace(id="user-one"),
    )
    await tasks()
    row = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == result["proposals"][0]["proposal_id"]
    )
    assert row.state == "queued" and row.job_id == "isolated-session-job"
    row.state = "pending"
    await row.save()
    with pytest.raises(HTTPException) as wrong_owner:
        await timeline_routes.resolve_timeline_memory_review(
            row.proposal_id,
            timeline_routes.ResolveMemoryReviewRequest(
                generation=1, accepted_change_ids=[]
            ),
            BackgroundTasks(),
            SimpleNamespace(id="another-user"),
        )
    assert wrong_owner.value.status_code == 404
    with pytest.raises(HTTPException) as old_generation:
        await timeline_routes.resolve_timeline_memory_review(
            row.proposal_id,
            timeline_routes.ResolveMemoryReviewRequest(
                generation=2, accepted_change_ids=[]
            ),
            BackgroundTasks(),
            SimpleNamespace(id="user-one"),
        )
    assert old_generation.value.status_code == 409
    listed = await timeline_routes.list_timeline_memory_selections(
        day.local_date, day.timezone, SimpleNamespace(id="user-one")
    )
    assert listed["outcomes"]["ep-one:1"]["state"] == "pending"
    assert listed["proposals"][0]["selected_episodes"] == [refs[0].model_dump()]


@pytest.mark.asyncio
async def test_uncommitted_successor_is_not_treated_as_withdrawn_source(
    selection_db, monkeypatch
):
    original = episode("original")
    original.status = "superseded"
    original.successor_keys = ["successor"]
    successor = episode("successor")
    await original.insert()
    await successor.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=False)
    )
    with pytest.raises(review.SelectionNotReady, match="not committed"):
        await review._current_successors(original)


@pytest.mark.asyncio
async def test_registered_queue_recovers_interrupted_generation(vault, monkeypatch):
    p = proposal(state="generating")
    await p.insert()
    await review.process_memory_review_queue()
    old = await MemoryReviewProposal.get(p.id)
    assert old.state == "queued"
    assert old.replacement_proposal_id is None
    assert old.job_id == "isolated-session-job"
    assert old.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["unaffected", "affected"])
async def test_partial_apply_rechecks_external_edits_and_preserves_completed_writes(
    vault, monkeypatch, verdict
):
    p = await pending(vault)
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    write = review._atomic_write

    def interrupt(target, content):
        if target.name == "Second.md":
            raise RuntimeError("crash after first note")
        write(target, content)

    monkeypatch.setattr(note_review, "_atomic_write", interrupt)
    await review.resolve_memory_review(p, [c.change_id for c in p.changes])
    assert await review.process_memory_review_decision(p) == "applying"
    monkeypatch.setattr(note_review, "_atomic_write", write)
    (vault / "Topics/External.md").write_text("External accepted change")
    checker = AsyncMock(
        return_value=MemoryFreshnessResult(
            verdict=verdict, reason="External evidence checked"
        )
    )
    monkeypatch.setattr(review, "check_freshness", checker)
    result = await review.process_memory_review_queue()
    assert (vault / "Topics/Plan.md").read_text() == "Plan from September"
    assert (vault / "Topics/External.md").read_text() == "External accepted change"
    assert checker.await_count == 1
    if verdict == "unaffected":
        assert result["applied"] == 1
        assert (vault / "Topics/Second.md").read_text() == "Second fact"
        assert await MemoryAuditEntry.find_all().count() == 2
    else:
        assert result["stale"] == 1
        assert not (vault / "Topics/Second.md").exists()
        assert await MemoryAuditEntry.find_all().count() == 1
        old = await MemoryReviewProposal.get(p.id)
        assert old.accepted_change_ids == [p.changes[0].change_id]
        assert old.replacement_proposal_id
        monkeypatch.setattr(
            review,
            "validate_selection",
            AsyncMock(side_effect=review.SelectionChanged("Evidence revised")),
        )
        await review.refresh_memory_selection_states()
        assert (await MemoryReviewProposal.get(old.id)).state == "correction_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_correction_resolves_predecessor_only_after_full_acceptance(
    vault, monkeypatch, partial
):
    old = proposal(
        state="correction_required", active=False, accepted_change_ids=["old-change"]
    )
    await old.insert()
    p = await pending(
        vault,
        request_id="correction",
        correction_of=[old.proposal_id],
        correction_episode_keys=["ep-one"],
    )
    p.changes += review.build_potential_changes(
        {},
        {"Topics/Second.md": "Corrected second fact"},
        source_episode_keys_by_path={"Topics/Second.md": ["ep-one"]},
    )
    await p.save()
    monkeypatch.setattr(review, "validate_selection", AsyncMock(return_value=([], [])))
    await review.resolve_memory_review(
        p, [p.changes[0].change_id] if partial else [c.change_id for c in p.changes]
    )
    assert await review.process_memory_review_decision(p) == "applied"
    old = await MemoryReviewProposal.get(old.id)
    assert old.state == ("correction_required" if partial else "corrected")
    if not partial:
        assert old.corrected_by_proposal_id == p.proposal_id


@pytest.mark.asyncio
async def test_preparation_worker_publishes_automatic_singleton_without_human_confirmation(
    selection_db, monkeypatch
):
    from backend.models.timeline import EvidenceLocator, TimelineEvidenceRef
    from backend.services.timeline import publication, session_organization
    from backend.services.timeline.snapshots import build_day_snapshot
    from backend.workers import session_jobs

    ep = episode("automatic-session")
    ep.status = "provisional"
    ep.evidence_refs = [
        TimelineEvidenceRef(
            evidence_id="automatic-call",
            kind="transcript",
            locator=EvidenceLocator(
                capture_source_id="phone", modality="transcript", track_id="input"
            ),
            started_at=ep.started_at,
            ended_at=ep.ended_at,
            role="user_statement",
            excerpt="We agreed to ship the fix tomorrow.",
            content_hash="call-hash",
        )
    ]
    await ep.insert()
    snapshot = build_day_snapshot(
        user_id="user-one",
        local_date=ep.local_date,
        timezone_name=ep.timezone,
        evidence_state_hash="c" * 64,
        episode_revisions=[
            EpisodeRevisionRef(episode_key=ep.episode_key, revision=ep.revision)
        ],
    )
    day = TimelineDay(
        user_id=ep.user_id,
        local_date=ep.local_date,
        timezone=ep.timezone,
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
        snapshot_state="ready",
    )
    await day.insert()
    item = SessionPreparation(
        user_id=ep.user_id,
        local_date=ep.local_date,
        timezone=ep.timezone,
        snapshot_id=snapshot.snapshot_id,
    )
    await item.insert()
    monkeypatch.setattr(session_jobs, "distributed_lock", unlocked)
    monkeypatch.setattr(publication, "distributed_lock", unlocked)
    await session_jobs.prepare_sessions_job.__wrapped__(str(item.id))
    stored = await TimelineDay.get(day.id)
    assert len(stored.semantic_group_history) == 1
    assert stored.semantic_group_history[0].origin == "automatic"
    assert stored.semantic_group_history[0].episode_ids == [ep.episode_id]
    assert stored.review_decisions[-1].action == "session_organized"
    assert not (await TimelineEpisode.get(ep.id)).confirmed_fields
    assert (await SessionPreparation.get(item.id)).state == "complete"

    # The real preparation entry point must recover a stale draft, without
    # reviving its old proposal or manufacturing a structural confirmation.
    prior = await MemoryReviewProposal.find_one(
        {"session_key": stored.semantic_group_history[0].group_key}
    )
    assert prior is not None
    prior.state, prior.active = "stale", False
    await prior.save()
    recovery = await sessions.request_preparation(stored)
    await session_jobs.prepare_sessions_job.__wrapped__(str(recovery.id))
    current = (
        await MemoryReviewProposal.find({"session_key": prior.session_key})
        .sort("created_at")
        .to_list()
    )
    assert len(current) == 2
    assert current[-1].state == "queued"
    assert current[-1].proposal_id != prior.proposal_id
    assert (await MemoryReviewProposal.get(prior.id)).state == "stale"
    assert not (await TimelineEpisode.get(ep.id)).confirmed_fields


@pytest.mark.asyncio
async def test_cancelled_worker_cannot_revive_an_older_generation(selection_db):
    old = proposal(state="generating")
    await old.insert()
    stored = await MemoryReviewProposal.get(old.id)
    stored.state = "stale"
    stored.active = False
    await stored.save()
    replacement = proposal(
        request_id="new-scope", source_scope=[{"key": "new-evidence"}]
    )
    await replacement.insert()
    old.account = {"summary": "Late model result"}
    with pytest.raises(review.SelectionChanged, match="cancelled or superseded"):
        await review.persist_generation(old)
    assert (await MemoryReviewProposal.get(old.id)).state == "stale"
    assert (await MemoryReviewProposal.get(replacement.id)).active


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_offset", [0, 1])
async def test_session_spanning_dates_has_one_owner_and_one_memory_job(
    selection_db, monkeypatch, owner_offset
):
    from backend.models.timeline import GroupRevisionRef, TimelineSemanticGroupRevision

    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    first, second = episode("before-midnight", 4), episode("after-midnight", 5)
    for ep in (first, second):
        await ep.insert()
    owner_date = (first, second)[owner_offset].local_date
    refs = [
        EpisodeRevisionRef(episode_key=ep.episode_key, revision=ep.revision)
        for ep in (first, second)
    ]
    group = TimelineSemanticGroupRevision(
        group_key="one-continued-session",
        member_revisions=refs,
        episode_ids=[first.episode_id, second.episode_id],
        title="Continued investigation",
        summary="Same specific investigation continued.",
        source_snapshot_id="a" * 64,
        started_at=first.started_at,
        ended_at=second.ended_at,
        origin="automatic",
    )
    days = []
    for ep, char in ((first, "a"), (second, "b")):
        snapshot = TimelineDaySnapshot(
            snapshot_id=char * 64,
            evidence_state_hash="c" * 64,
            episode_revisions=[
                EpisodeRevisionRef(episode_key=ep.episode_key, revision=1)
            ],
            semantic_group_revisions=[
                GroupRevisionRef(
                    owner_local_date=owner_date,
                    group_key=group.group_key,
                    revision=1,
                )
            ],
        )
        day = TimelineDay(
            user_id=ep.user_id,
            local_date=ep.local_date,
            timezone=ep.timezone,
            current_snapshot=snapshot,
            current_snapshot_id=snapshot.snapshot_id,
            semantic_group_history=[group] if ep.local_date == owner_date else [],
        )
        await day.insert()
        days.append(day)
    left = await sessions.project_sessions(days[0], [first])
    right = await sessions.project_sessions(days[1], [second])
    assert left[0]["session_key"] == right[0]["session_key"]
    assert len(right[0]["episodes"]) == 2
    made = await sessions.request_session_memory(
        first.user_id, second.local_date, first.timezone, group.group_key, 1
    )
    again = await sessions.request_session_memory(
        first.user_id, first.local_date, first.timezone, group.group_key, 1
    )
    assert made[0].proposal_id == again[0].proposal_id
    assert made[0].local_date == owner_date
    await review.validate_selection(made[0])


@pytest.mark.asyncio
async def test_correction_arriving_during_preparation_survives_worker_save(
    selection_db, monkeypatch
):
    from backend.workers import session_jobs

    item = SessionPreparation(
        user_id="user-one",
        local_date=date(2026, 9, 5),
        timezone="Asia/Kolkata",
        snapshot_id="a" * 64,
    )
    await item.insert()
    monkeypatch.setattr(session_jobs, "distributed_lock", unlocked)

    async def prepare(stale_worker_item):
        day = SimpleNamespace(
            user_id=item.user_id,
            local_date=item.local_date,
            timezone=item.timezone,
            current_snapshot_id=item.snapshot_id,
        )
        await sessions.request_preparation(day, force=True, priority=100)
        stale_worker_item.state = "complete"
        await sessions.persist_preparation(stale_worker_item)

    monkeypatch.setattr(sessions, "prepare_sessions", prepare)
    await session_jobs.prepare_sessions_job.__wrapped__(str(item.id))
    stored = await SessionPreparation.get(item.id)
    assert stored.requested_revision == 1
    assert stored.completed_revision == 0
    assert stored.priority == 100
    assert stored.state == "queued"


@pytest.mark.asyncio
async def test_published_open_activity_can_draft_without_fabricating_confirmation(
    selection_db, monkeypatch
):
    ep = episode("ongoing-checkpoint")
    ep.status = "open"
    await ep.insert()
    ref = EpisodeRevisionRef(episode_key=ep.episode_key, revision=ep.revision)
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64, evidence_state_hash="c" * 64, episode_revisions=[ref]
    )
    day = TimelineDay(
        user_id=ep.user_id,
        local_date=ep.local_date,
        timezone=ep.timezone,
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
    )
    await day.insert()
    monkeypatch.setattr(
        review, "episode_revision_is_published", AsyncMock(return_value=True)
    )
    rows = await review.create_memory_selection(
        ep.user_id, day.local_date, day.timezone, snapshot.snapshot_id, [ref]
    )
    assert rows[0].state == "queued"
    stored = await TimelineEpisode.get(ep.id)
    assert stored.status == "open"
    assert not stored.confirmed_fields


def test_derived_episode_summary_cannot_invalidate_source_based_session_memory():
    ep = episode("source-based")
    before = review.selection_hash([ep], [])
    ep.detailed_summary = (
        "A newly generated long account of the same retained evidence."
    )
    ep.summary = "New display summary"
    assert review.selection_hash([ep], []) == before
    ep.memory_policy = "reference"
    assert review.selection_hash([ep], []) != before


@pytest.mark.asyncio
async def test_regeneration_is_single_flight_without_waiting_for_other_session_work(
    selection_db, monkeypatch
):
    import asyncio

    from backend.services.redis_lock import distributed_lock

    p = await proposal(state="paused").insert()
    monkeypatch.setattr(review, "distributed_lock", distributed_lock)
    enqueue = AsyncMock()
    monkeypatch.setattr(sessions, "enqueue_memory", enqueue)
    async with distributed_lock(f"memory:review-work:{p.user_id}", timeout=30):
        responses = await asyncio.wait_for(
            asyncio.gather(
                *[
                    timeline_routes.regenerate_timeline_memory_review(
                        p.proposal_id, BackgroundTasks(), SimpleNamespace(id=p.user_id)
                    )
                    for _ in range(2)
                ]
            ),
            timeout=3,
        )
    ids = {r["proposal"]["proposal_id"] for r in responses}
    assert len(ids) == 1
    assert (
        await MemoryReviewProposal.find(
            {"supersedes_proposal_id": p.proposal_id}
        ).count()
        == 1
    )
    assert (await MemoryReviewProposal.get(p.id)).active is False


@pytest.mark.asyncio
async def test_public_regeneration_cannot_replace_an_approval_in_progress(selection_db):
    p = await proposal(state="checking").insert()
    with pytest.raises(HTTPException) as exc:
        await timeline_routes.regenerate_timeline_memory_review(
            p.proposal_id, BackgroundTasks(), SimpleNamespace(id=p.user_id)
        )
    assert exc.value.status_code == 409
    assert (await MemoryReviewProposal.get(p.id)).state == "checking"
    assert (
        await MemoryReviewProposal.find(
            {"supersedes_proposal_id": p.proposal_id}
        ).count()
        == 0
    )


@pytest.mark.asyncio
async def test_regeneration_feedback_survives_crash_before_successor_insert(
    selection_db, monkeypatch
):
    p = await proposal(state="pending").insert()
    original_insert = MemoryReviewProposal.insert
    monkeypatch.setattr(
        MemoryReviewProposal,
        "insert",
        AsyncMock(side_effect=RuntimeError("interrupted insert")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await timeline_routes.regenerate_timeline_memory_review(
            p.proposal_id,
            BackgroundTasks(),
            SimpleNamespace(id=p.user_id),
            timeline_routes.RegenerateMemoryReviewRequest(
                feedback="Remove unsupported details"
            ),
        )
    old = await MemoryReviewProposal.get(p.id)
    assert old.replacement_feedback == "Remove unsupported details"
    assert old.state == "regenerating"
    monkeypatch.setattr(MemoryReviewProposal, "insert", original_insert)
    replacement = await review.queue_memory_review_regeneration(
        old, decision_owned=True
    )
    assert replacement.revision_feedback == old.replacement_feedback
    again = await review.queue_memory_review_regeneration(
        await MemoryReviewProposal.get(p.id)
    )
    assert again.proposal_id == replacement.proposal_id
    with pytest.raises(review.MemoryReviewError, match="current draft"):
        await review.queue_memory_review_regeneration(
            await MemoryReviewProposal.get(p.id), feedback="Different correction"
        )
