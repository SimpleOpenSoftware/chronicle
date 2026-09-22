"""Search, undated selection and refresh jobs through their production entry points."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.conversation import Conversation
from backend.models.memory_audit import MemoryAuditEntry
from backend.models.memory_space import MemorySpace
from backend.models.session_memory import MemorySourceDecision, UndatedSession
from backend.models.timeline import MemoryReviewProposal, TimelineDay, TimelineEpisode
from backend.routers.modules import source_search_routes as routes
from backend.services import privacy
from backend.services import source_search as search
from backend.services.timeline import accepted_context as context
from backend.services.timeline import recording_sessions as recordings
from backend.services.timeline import review, sessions
from backend.workers import session_jobs
from backend.workers import source_search_jobs as jobs

real_enqueue = sessions._enqueue


@pytest.mark.asyncio
async def test_clarification_api_retry_persists_once_and_rejects_stale_scope(
    database,
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from test_session_memory import episode

    from backend.routers.modules import timeline_routes
    from backend.services.redis_lock import LockUnavailable
    from backend.services.timeline import memory_sources

    day = NS(local_date=datetime(2026, 9, 4).date())
    members = [episode()]
    monkeypatch.setattr(
        sessions, "choose_session", AsyncMock(return_value=(day, NS(), members))
    )
    prepare = AsyncMock()
    monkeypatch.setattr(sessions, "request_preparation", prepare)
    attempts = 0

    @asynccontextmanager
    async def busy_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LockUnavailable("Publication in progress")
        yield

    monkeypatch.setattr(sessions, "distributed_lock", busy_once)
    app = FastAPI()
    app.include_router(timeline_routes.router, prefix="/api")
    app.dependency_overrides[timeline_routes.current_active_user] = lambda: NS(
        id="owner"
    )
    sources = memory_sources.evidence_sources(members)
    payload = {
        "timezone": "Asia/Kolkata",
        "session_key": "session",
        "revision": 1,
        "action": "clarify",
        "source_keys": [sources[0]["key"]],
        "scope_hash": memory_sources.scope_hash(sources),
        "clarification": "I performed the installation.",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        url = "/api/timeline/sessions/2026-09-04/disposition"
        busy = await client.post(url, json=payload)
        assert busy.status_code == 503
        assert await database.memory_source_decisions.count_documents({}) == 0
        prepare.assert_not_awaited()
        saved = await client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        row = await database.memory_source_decisions.find_one({"user_id": "owner"})
        assert row["clarification"] == payload["clarification"]
        assert row["sources"][0]["key"] == sources[0]["key"]
        prepare.assert_awaited_once_with(day, priority=100, force=True)
        stale = await client.post(url, json=payload)
        assert stale.status_code == 409
        assert await database.memory_source_decisions.count_documents({}) == 1


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


@pytest.fixture
async def database(monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client.test_source_search_flow
    monkeypatch.setattr(privacy, "database", lambda: database)
    await init_beanie(
        database=database,
        document_models=[
            Conversation,
            MemorySourceDecision,
            UndatedSession,
            MemoryReviewProposal,
            TimelineDay,
            TimelineEpisode,
            MemoryAuditEntry,
            MemorySpace,
        ],
    )
    monkeypatch.setattr(recordings, "distributed_lock", unlocked)
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(jobs, "distributed_lock", unlocked)
    monkeypatch.setattr(sessions, "_enqueue", lambda *a, **k: "test-queued")
    yield database
    await client.drop_database(database.name)
    client.close()


async def recording(
    database,
    identifier="intro",
    user="owner",
    text="I'm Avery, an AI engineer. This is an introduction about myself and my wife.",
    dated=False,
):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    row = {
        "conversation_id": identifier,
        "user_id": user,
        "client_id": "upload",
        "started_at": now,
        "created_at": now,
        "origin": "deliberate",
        "deleted": False,
        "title": "Personal introduction",
        "summary": "Background",
        "memory_excluded": False,
        "active_transcript_version": "v2",
        "audio_ranges": [
            {
                "capture_source_id": "upload",
                "time_basis": "captured" if dated else "unknown",
                "chunk_ids": ["012345678901234567890123"],
                "started_at": now,
                "ended_at": now + timedelta(seconds=90),
            }
        ],
        "transcript_versions": [
            {
                "version_id": "v1",
                "created_at": now,
                "transcript": "Superseded banana statement",
                "provider": "test",
                "segments": [],
            },
            {
                "version_id": "v2",
                "created_at": now,
                "provider": "test",
                "transcript": text,
                "segments": [
                    {"text": text, "speaker": "avery", "start": 20.0, "end": 80.0}
                ],
            },
        ],
    }
    await database.conversations.insert_one(row)
    return row


@pytest.mark.asyncio
async def test_search_worker_and_api_find_transcript_words_typos_and_scope(database):
    await recording(database)
    await recording(database, "other", user="someone-else")
    await jobs.index_sources_job.__wrapped__("recovery")
    for query in [
        "intro avery ai engineer",
        "engineer ai avery intro",
        "avery enginer",
        "AI",
    ]:
        result = await routes.search_sources(
            q=query,
            kinds=["recording"],
            fields=list(search.FIELDS),
            limit=20,
            offset=0,
            memory_space_id=None,
            user=NS(id="owner"),
        )
        assert [r["key"] for r in result["items"]] == ["intro"]
        assert result["items"][0]["match_start"] == 20
    result = await search.search(
        "owner", "banana", kinds=["recording"], fields=["transcript"]
    )
    assert not result["items"]
    result = await search.search(
        "owner", "avery", kinds=["recording"], fields=["title"]
    )
    assert not result["items"]
    await database.conversations.update_one(
        {"conversation_id": "intro"}, {"$set": {"deleted": True}}
    )
    assert not (
        await search.search(
            "owner", "avery", kinds=["recording"], fields=["transcript"]
        )
    )["items"]


@pytest.mark.asyncio
async def test_index_cursor_resumes_and_revision_change_is_not_shown(database):
    await recording(database)
    await jobs.index_sources_job.__wrapped__("recovery")
    state = await database.source_search_jobs.find_one({"_id": "recovery"})
    assert state["after"] and state["completed"] == 1
    await jobs.index_sources_job.__wrapped__("recovery")
    assert (await database.source_search_jobs.find_one({"_id": "recovery"}))[
        "collection"
    ] == "timeline_days"
    await database.conversations.update_one(
        {"conversation_id": "intro"}, {"$set": {"active_transcript_version": "v1"}}
    )
    assert not (
        await search.search(
            "owner", "avery", kinds=["recording"], fields=["transcript"]
        )
    )["items"]


@pytest.mark.asyncio
async def test_recording_save_updates_search_projection_immediately(database):
    await recording(database)
    row = await Conversation.find_one({"conversation_id": "intro"})
    row.title = "Personal background for Chronicle"
    await row.save()
    result = await search.search(
        "owner", "chronicle", kinds=["recording"], fields=["title"]
    )
    assert [r["key"] for r in result["items"]] == ["intro"]


@pytest.mark.asyncio
async def test_http_search_serializes_index_status_and_applies_auth(database):
    import httpx
    from fastapi import FastAPI

    from backend.auth import current_active_user

    await recording(database)
    await jobs.index_sources_job.__wrapped__("recovery")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_active_user] = lambda: NS(id="owner")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/search", params={"q": "intro avery ai engineer"}
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["key"] == "intro"
        assert "after" not in response.json()["indexing"]
        app.dependency_overrides[current_active_user] = lambda: NS(id="other")
        assert (await client.get("/api/search", params={"q": "avery"})).json()[
            "items"
        ] == []


@pytest.mark.asyncio
async def test_undated_api_deduplicates_and_fences_changed_source(
    database, monkeypatch
):
    await recording(database)
    first = await routes.prepare_undated(
        "intro", routes.RecordingScope(), NS(id="owner")
    )
    second = await routes.prepare_undated(
        "intro", routes.RecordingScope(), NS(id="owner")
    )
    assert (
        first["session_key"] == second["session_key"]
        and first["revision"] == second["revision"]
    )
    body = routes.UndatedMemoryRequest(revision=first["revision"])
    proposal = await routes.generate_undated(first["session_key"], body, NS(id="owner"))
    same = await routes.generate_undated(first["session_key"], body, NS(id="owner"))
    assert same["proposal_id"] == proposal["proposal_id"]
    p = await MemoryReviewProposal.find_one({"proposal_id": proposal["proposal_id"]})
    assert p.local_date is None and p.selected_episodes == []
    assert await review.validate_selection(p) == ([], [])
    await database.conversations.update_one(
        {"conversation_id": "intro"}, {"$set": {"active_transcript_version": "v1"}}
    )
    with pytest.raises(review.SelectionChanged):
        await review.validate_selection(p)


@pytest.mark.asyncio
async def test_undated_exclusion_survives_new_session_revision(database, monkeypatch):
    await recording(database)
    monkeypatch.setattr("backend.services.redis_lock.distributed_lock", unlocked)
    session = await recordings.prepare_undated("owner", "intro")
    source = session.sources[0]
    await routes.decide_undated(
        session.session_key,
        routes.UndatedDecision(
            revision=1, action="exclude", source_keys=[source["key"]]
        ),
        NS(id="owner"),
    )
    await database.conversations.update_one(
        {"conversation_id": "intro"}, {"$set": {"title": "Renamed introduction"}}
    )
    proposal = await recordings.generate_undated(
        "owner", session.session_key, session.revision
    )
    assert proposal.source_scope[0]["participation"] == "excluded"


@pytest.mark.asyncio
async def test_undated_search_checks_complete_revision_with_word_timings(database):
    await recording(database)
    await database.conversations.update_one(
        {"conversation_id": "intro"},
        {
            "$set": {
                "transcript_versions.1.segments.0.words": [
                    {"word": "Avery", "start": 20.0, "end": 20.4}
                ]
            }
        },
    )
    session = await recordings.prepare_undated("owner", "intro")
    result = await search.search(
        "owner",
        "intro avery ai engineer",
        kinds=["session"],
        fields=list(search.FIELDS),
    )
    assert [r["key"] for r in result["items"]] == [session.session_key]
    await database.conversations.update_one(
        {"conversation_id": "intro"},
        {"$set": {"transcript_versions.1.segments.0.words.0.end": 21.0}},
    )
    assert not (
        await search.search(
            "owner", "intro", kinds=["session"], fields=list(search.FIELDS)
        )
    )["items"]


@pytest.mark.asyncio
async def test_missing_lookup_can_match_new_identity_note(monkeypatch):
    notes = {}
    monkeypatch.setattr(context, "snapshot", lambda *a: dict(notes))
    empty = await context.for_sources("owner", [])
    notes["People/Avery.md"] = "Avery is an engineer."
    fresh = await context.for_sources("owner", [])
    assert fresh["scope_hash"] != empty["scope_hash"]
    assert fresh["notes"] == []  # Pi selects passages through tools.


def test_accepted_daily_facts_are_context_but_hidden_artifacts_are_not(
    tmp_path, monkeypatch
):
    for name in ["Daily/2026-09-04.md", ".history/draft.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Avery met a colleague.")
    monkeypatch.setattr(context, "vault_root", lambda *a: tmp_path)
    notes = context.snapshot("owner")
    assert "Daily/2026-09-04.md" in notes
    assert ".history/draft.md" not in notes


@pytest.mark.asyncio
async def test_context_scope_does_not_preselect_notes_from_evidence(monkeypatch):
    monkeypatch.setattr(
        context, "snapshot", lambda *a: {"People/Avery.md": "Avery is an engineer"}
    )
    result = await context.for_sources(
        "owner", [{"excerpt": "excluded content", "participation": "excluded"}]
    )
    assert result["notes"] == []
    assert result["lookup_terms"] == []
    assert result["scope"]["user_id"] == "owner"


@pytest.mark.asyncio
async def test_memory_space_isolation_and_archived_write_fence(database):
    from fastapi import HTTPException

    space = MemorySpace(user_id="owner", name="Private")
    await space.insert()
    await recording(database)
    await database.conversations.update_one(
        {"conversation_id": "intro"}, {"$set": {"memory_space_id": space.space_id}}
    )
    await jobs.index_sources_job.__wrapped__("recovery")
    assert not (
        await search.search(
            "owner", "avery", kinds=["recording"], fields=["transcript"]
        )
    )["items"]
    assert (
        await search.search(
            "owner",
            "avery",
            kinds=["recording"],
            fields=["transcript"],
            memory_space_id=space.space_id,
        )
    )["items"]
    with pytest.raises(HTTPException) as error:
        await routes.check_space("someone-else", space.space_id)
    assert error.value.status_code == 404
    session = await recordings.prepare_undated("owner", "intro", space.space_id)
    await space.set({"state": "archived"})
    with pytest.raises(review.SelectionChanged, match="not active"):
        await recordings.generate_undated(
            "owner", session.session_key, 1, memory_space_id=space.space_id
        )


@pytest.mark.asyncio
async def test_dated_route_only_organizes_and_missing_audio_is_explicit(
    database, monkeypatch
):
    import backend.services.timeline.explicit_reconciliation as reconciliation

    await recording(database, "dated", dated=True)
    request = AsyncMock(return_value=(NS(request_id="organize-only"), True))
    monkeypatch.setattr(reconciliation, "request_explicit_reconciliation", request)
    monkeypatch.setattr(
        reconciliation,
        "reconciliation_request_payload",
        lambda row: {"request_id": row.request_id, "state": "queued"},
    )
    result = await routes.organize_recording_day(
        "dated", routes.OrganizeRecordingDay(local_date="2026-06-13"), NS(id="owner")
    )
    assert result["state"] == "queued"
    assert (
        await database.recording_organization_intents.count_documents(
            {"request_id": "organize-only"}
        )
        == 1
    )
    assert await MemoryReviewProposal.find_all().count() == 0
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await routes.prepare_undated("dated", routes.RecordingScope(), NS(id="owner"))
    assert error.value.status_code == 409
    await recording(database, "missing")
    await database.conversations.update_one(
        {"conversation_id": "missing"}, {"$set": {"audio_ranges": []}}
    )
    with pytest.raises(HTTPException) as error:
        await routes.prepare_undated("missing", routes.RecordingScope(), NS(id="owner"))
    assert "audio references" in error.value.detail


@pytest.mark.asyncio
async def test_cross_midnight_recording_links_actual_ranges_once(database, monkeypatch):
    import copy

    from backend.services.timeline import consolidation

    row = await recording(database, dated=True)
    start = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)
    await database.conversations.update_one(
        {"conversation_id": "intro"},
        {
            "$set": {
                "audio_ranges.0.started_at": start,
                "audio_ranges.0.ended_at": start + timedelta(hours=2),
            }
        },
    )
    span = NS(
        chunk_ids=row["audio_ranges"][0]["chunk_ids"],
        started_at=start,
        ended_at=start + timedelta(hours=2),
    )
    outside = NS(
        chunk_ids=span.chunk_ids,
        started_at=start + timedelta(hours=3),
        ended_at=start + timedelta(hours=4),
    )
    episodes = [
        NS(episode_key="correct", audio_ranges=[span]),
        NS(episode_key="outside", audio_ranges=[outside]),
    ]
    monkeypatch.setattr(
        recordings.TimelineDay,
        "find_one",
        AsyncMock(return_value=NS(current_snapshot=True, pending_publication_id=None)),
    )
    monkeypatch.setattr(
        consolidation, "snapshot_episodes", AsyncMock(return_value=episodes)
    )

    async def projected(*args, **kwargs):
        return [
            {
                "session_key": e.episode_key,
                "revision": 1,
                "episodes": [{"episode_key": e.episode_key}],
                "sources": [],
            }
            for e in episodes
        ]

    monkeypatch.setattr(sessions, "project_sessions", projected)
    result = await recordings.recording_context("owner", "intro", "Asia/Kolkata")
    assert result["dates"] == ["2026-09-04", "2026-09-05"]
    assert [s["session_key"] for s in result["sessions"]] == ["correct"]


@pytest.mark.asyncio
async def test_registered_context_worker_offers_refresh_without_generating(
    database, monkeypatch
):
    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    p = await recordings.generate_undated("owner", session.session_key, 1)
    await p.set(
        {
            "state": "needs_attention",
            "questions": ["Who is Avery?"],
            "accepted_context": {"lookup_terms": ["avery"], "notes": []},
        }
    )
    monkeypatch.setattr(
        context,
        "snapshot",
        lambda *a: {"People/Avery.md": "Avery is an AI engineer."},
    )
    assessment = AsyncMock(
        return_value={
            "verdict": "useful",
            "reason": "New identity note may answer this question",
            "relevant_paths": ["People/Avery.md"],
        }
    )
    monkeypatch.setattr(context, "assess", assessment)
    await jobs.assess_context_job.__wrapped__("owner|main")
    saved = await MemoryReviewProposal.get(p.id)
    assert saved.refresh_assessment["verdict"] == "useful"
    assert saved.state == "needs_attention"
    assert await database.memory_review_proposals.count_documents({}) == 1
    await jobs.assess_context_job.__wrapped__("owner|main")
    assert assessment.await_count == 1


@pytest.mark.asyncio
async def test_incomplete_assessment_is_not_unrelated(monkeypatch, tmp_path):
    from backend.services.timeline import pi_tasks

    monkeypatch.setattr(
        pi_tasks, "run_task", AsyncMock(side_effect=ValueError("incomplete"))
    )
    result = await context.assess(
        NS(
            account={},
            questions=["Who?"],
            accepted_context={},
            source_scope=[],
            excluded_source_keys=[],
            user_id="owner",
            memory_space_id=None,
        ),
        {"People/Avery.md": {"before": None, "after": "Avery is an engineer"}},
    )
    assert result["verdict"] == "uncertain"
    assert result["error"]


@pytest.mark.asyncio
async def test_context_assessment_uses_real_rq_job_creation_and_deduplication(
    monkeypatch,
):
    import fakeredis
    from rq import Queue

    from backend.controllers import queue_controller

    connection = fakeredis.FakeRedis()
    queue = Queue("memory", connection=connection)
    monkeypatch.setattr(queue_controller, "redis_conn", connection)
    monkeypatch.setattr(queue_controller, "memory_queue", queue)
    monkeypatch.setattr(sessions, "_enqueue", real_enqueue)
    await context.queue_context_assessment("owner")
    await context.queue_context_assessment("owner")
    assert queue.count == 1
    job = queue.get_jobs()[0]
    assert job.args == ("owner|main",)
    assert job.func_name.endswith("assess_context_job")


@pytest.mark.asyncio
async def test_undated_generation_uses_shared_worker_and_reviewed_diff(
    database, monkeypatch, tmp_path
):
    from backend.services.memory.base import DayWriteOutcome
    from backend.services.memory.vault_manager import ConvDocVaultManager
    from backend.services.timeline.session_accounts import (
        AccountClaim,
        SessionAccount,
        SourceQuote,
    )

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    p = await recordings.generate_undated("owner", session.session_key, 1)
    source_key = p.source_scope[0]["key"]

    class Service:
        def __init__(self, config=None):
            self.config = NS()
            self.vault = ConvDocVaultManager(tmp_path)

        async def draft_session_memory(self, source, user):
            from backend.services.memory.session_write import SessionDraftResult

            assert "2026-06-13" not in source.render()
            assert source.event_date is None and source.source_date == "unknown"
            assert source.episode_ids == ()
            assert source.conversation_ids == ("intro",)
            root = self.vault.user_root(user)
            (root / "People").mkdir(exist_ok=True)
            (root / "People/Avery.md").write_text(
                "Avery described himself as an AI engineer in an undated introduction."
            )
            return SessionDraftResult(
                outcome="complete",
                source_evidence_keys_by_path={"People/Avery.md": [source_key]},
            )

    service = Service()
    monkeypatch.setattr(review, "get_memory_service", lambda: service)
    monkeypatch.setattr(review, "ChronicleMemoryService", Service)
    monkeypatch.setattr(
        context, "vault_root", lambda *a: service.vault.user_root("owner")
    )
    from pi_task_helpers import install_pi, review_result

    calls = install_pi(
        monkeypatch,
        tmp_path / "inference",
        [
            SessionAccount(
                title="Introduction",
                summary="Avery introduced himself.",
                claims=[
                    AccountClaim(
                        text="Avery is an AI engineer",
                        personal=True,
                        source_keys=[source_key],
                        citations=[
                            SourceQuote(source_key=source_key, quote="AI engineer")
                        ],
                    )
                ],
                questions=[],
                useful=True,
            ).model_dump(),
            review_result("ready", "The introduction directly supports the claim."),
        ],
    )
    result = await session_jobs.generate_session_memory_job.__wrapped__(p.proposal_id)
    assert result == "pending"
    assert len(calls) == 2
    p = await MemoryReviewProposal.get(p.id)
    assert p.changes[0].source_session_keys == [session.session_key]
    assert p.changes[0].source_episode_keys == []
    assert p.changes[0].source_evidence_keys == [source_key]
    assert not (service.vault.user_root("owner") / "People/Avery.md").exists()
    assert p.accepted_change_ids == []
    await review.resolve_memory_review(p, [p.changes[0].change_id])
    assert await review.process_memory_review_decision(p) == "applied"
    saved_text = (service.vault.user_root("owner") / "People/Avery.md").read_text()
    assert "AI engineer" in saved_text
    assert await MemoryAuditEntry.find_all().count() == 1
    await p.set(
        {"refresh_assessment": {"verdict": "useful", "reason": "Updated context"}}
    )
    correction = await review.request_memory_correction(p)
    assert correction.correction_of == [p.proposal_id]
    assert correction.local_date is None
    assert (
        service.vault.user_root("owner") / "People/Avery.md"
    ).read_text() == saved_text


@pytest.mark.asyncio
async def test_opened_old_session_is_assessed_without_regeneration(
    database, monkeypatch
):
    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    p = await recordings.generate_undated("owner", session.session_key, 1)
    await p.set(
        {
            "state": "no_changes",
            "active": False,
            "priority": 0,
            "accepted_context": {"lookup_terms": ["avery"], "notes": []},
        }
    )
    monkeypatch.setattr(
        context, "snapshot", lambda *a: {"People/Avery.md": "Avery is an AI engineer"}
    )
    assess = AsyncMock(
        return_value={
            "verdict": "useful",
            "reason": "Identity context is now available",
            "relevant_paths": ["People/Avery.md"],
        }
    )
    monkeypatch.setattr(context, "assess", assess)
    await routes.check_opened_context(
        routes.ContextCheck(recording_id="intro"), NS(id="owner")
    )
    await jobs.assess_context_job.__wrapped__("owner|main")
    current = await MemoryReviewProposal.get(p.id)
    assert (
        current.state == "no_changes"
        and current.refresh_assessment["verdict"] == "useful"
    )
    assert await MemoryReviewProposal.find_all().count() == 1


@pytest.mark.asyncio
async def test_unrelated_changes_and_bounded_failure_recovery(database, monkeypatch):
    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    p = await recordings.generate_undated("owner", session.session_key, 1)
    await p.set(
        {
            "state": "needs_attention",
            "accepted_context": {"lookup_terms": ["avery"], "notes": []},
        }
    )
    notes = {"Topics/Gardening.md": "Water tomatoes weekly"}
    monkeypatch.setattr(context, "snapshot", lambda *a: notes)
    assess = AsyncMock(
        return_value={
            "verdict": "uncertain",
            "reason": "Incomplete",
            "relevant_paths": [],
            "error": "provider timeout",
        }
    )
    monkeypatch.setattr(context, "assess", assess)
    await jobs.assess_context_job.__wrapped__("owner|main")
    assert (await MemoryReviewProposal.get(p.id)).refresh_assessment[
        "verdict"
    ] == "uncertain"
    assert assess.await_count == 1
    notes["People/Avery.md"] = "Avery is an engineer"
    for _ in range(9):
        await jobs.assess_context_job.__wrapped__("owner|main")
    current = await MemoryReviewProposal.get(p.id)
    assert current.refresh_assessment["verdict"] == "uncertain"
    assert current.refresh_assessment["attempts"] == 3
    assert assess.await_count == 4


@pytest.mark.asyncio
async def test_training_boundary_api_search_and_dataset_inspection(database):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.auth import current_active_user
    from backend.routers.modules import conversation_routes, data_audit_routes

    await recording(database, "personal")
    training = await recording(database, "training")
    await database.conversations.update_one(
        {"conversation_id": "training"},
        {
            "$set": {
                "data_purpose": "annotation",
                "memory_excluded": False,
                "title": "renamed training intro",
            }
        },
    )
    # Personal memory opt-out is not a visibility opt-out, even with a chunk filename.
    await database.conversations.update_one(
        {"conversation_id": "personal"},
        {"$set": {"memory_excluded": True, "title": "chunk_001"}},
    )
    # An old projection must not disclose a newly training-only source.
    await database.source_search.insert_one(search.recording_projection(training))
    app = FastAPI()
    for router in [routes.router, conversation_routes.router, data_audit_routes.router]:
        app.include_router(router, prefix="/api")
    user = NS(id="owner", user_id="owner", is_superuser=False)
    app.dependency_overrides[current_active_user] = lambda: user
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/search", params={"q": "training"})
        assert response.status_code == 200
        assert response.json()["total"] == 0
        for url in [
            "/api/conversations/training",
            "/api/conversations/training/memories",
            "/api/recordings/training/context",
        ]:
            assert (await client.get(url)).status_code == 404
        for url, body in [
            ("/api/conversations/training/reprocess-memory", None),
            ("/api/conversations/training/star", None),
            ("/api/recordings/training/undated-session", {}),
        ]:
            assert (await client.post(url, json=body)).status_code == 404
        detail = await client.get("/api/data-audit/recordings/training")
        assert detail.status_code == 200
        assert detail.json()["conversation"]["data_purpose"] == "annotation"
        assert detail.json()["conversation"]["transcript"]
        for params in [{}, {"include_deleted": True, "include_unprocessed": True}]:
            response = await client.get("/api/conversations", params=params)
            assert response.status_code == 200
            assert response.json()["total"] == 1
            assert response.json()["conversations"][0]["conversation_id"] == "personal"
        assert (
            await client.get("/api/conversations/search", params={"q": "training"})
        ).json()["total"] == 0
        assert (await client.get("/api/conversations/personal")).status_code == 200
        app.dependency_overrides[current_active_user] = lambda: NS(
            id="other", user_id="other", is_superuser=False
        )
        assert (
            await client.get("/api/data-audit/recordings/training")
        ).status_code == 403
        app.dependency_overrides[current_active_user] = lambda: NS(
            id="owner", user_id="owner", is_superuser=True
        )
        assert (await client.get("/api/conversations/training")).status_code == 404
        assert (await client.get("/api/conversations")).json()["total"] == 1

    await jobs.index_sources_job.__wrapped__("recovery")
    assert await database.source_search.count_documents({"key": "training"}) == 0
    assert await database.source_search.count_documents({"key": "personal"}) == 1
    assert await database.conversations.count_documents({}) == 2


@pytest.mark.asyncio
async def test_training_purpose_change_invalidates_undated_selection(
    database, monkeypatch
):
    from backend.workers import memory_jobs

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    await database.conversations.update_one(
        {"conversation_id": "intro"},
        {"$set": {"data_purpose": "annotation", "memory_excluded": False}},
    )
    proposal = NS(
        user_id="owner",
        memory_space_id=None,
        session_key=session.session_key,
        session_revision=session.revision,
    )
    with pytest.raises(review.SelectionChanged, match="Recording not found"):
        await recordings.validate_undated(proposal)
    with pytest.raises(LookupError, match="Recording not found"):
        await recordings.generate_undated(
            "owner", session.session_key, session.revision
        )
    await jobs.index_sources_job.__wrapped__("recovery")
    assert await database.source_search.count_documents({}) == 0
    assert await database.undated_sessions.count_documents({}) == 1
    memory = AsyncMock()
    monkeypatch.setattr(memory_jobs, "get_memory_service", memory)
    result = await memory_jobs.process_memory_job.__wrapped__.__wrapped__(
        "intro", redis_client=None
    )
    assert result["skipped"] and result["reason"] == "training_only"
    memory.assert_not_called()


@pytest.mark.asyncio
async def test_training_episode_and_mixed_merge_cannot_become_personal(
    database, monkeypatch
):
    from backend.controllers import data_audit_controller
    from backend.models.timeline import EpisodeRevisionRef
    from backend.services.timeline.evidence import _conversation_audio_bounds

    training = await recording(database, "training", dated=True)
    await database.conversations.update_one(
        {"conversation_id": "training"}, {"$set": {"data_purpose": "annotation"}}
    )
    await recording(database, "personal", dated=True)
    start = training["started_at"]
    bounds = await _conversation_audio_bounds("owner", start, start + timedelta(days=1))
    assert "personal" in bounds and "training" not in bounds
    episode = TimelineEpisode(
        user_id="owner",
        run_id="old-run",
        local_date=start.date(),
        timezone="Asia/Kolkata",
        started_at=start,
        ended_at=start + timedelta(seconds=90),
        kind="conversation",
        title="Old training episode",
        activity_mode="foreground",
        summary="Introduction",
        confidence=0,
        related_conversation_ids=["training"],
    )
    await episode.insert()
    with pytest.raises(review.SelectionChanged, match="Training-only sources"):
        await review._selection(
            "owner",
            [
                EpisodeRevisionRef(
                    episode_key=episode.episode_key, revision=episode.revision
                )
            ],
            "Asia/Kolkata",
        )
    sources = [NS(data_purpose="annotation"), NS(data_purpose=None)]
    monkeypatch.setattr(
        data_audit_controller,
        "_load_operable_conversation",
        AsyncMock(side_effect=[(source, None) for source in sources]),
    )
    result = await data_audit_controller.merge_conversations(
        NS(user_id="owner"), ["training", "personal"]
    )
    assert result.status_code == 409
    assert await database.conversations.count_documents({}) == 2


@pytest.mark.asyncio
async def test_refresh_investigation_trace_is_available_through_owned_proposal_route(
    database, monkeypatch, tmp_path
):
    from fastapi import HTTPException

    from backend.routers.modules import timeline_routes
    from backend.services.inference_artifacts import persist_inference_run

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    proposal = await recordings.generate_undated("owner", session.session_key, 1)
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path / "inference"))
    _, artifact = persist_inference_run(
        operation="pi_refresh_assessment",
        request={"task": "assess"},
        stdout="complete",
        stderr="",
        result={"verdict": "useful"},
        metadata={
            "model_input": {"prompt": "Inspect changed context"},
            "tool_calls": [{"tool": "search_material"}],
        },
        reusable=True,
    )
    await proposal.set(
        {
            "refresh_assessment": {
                "verdict": "useful",
                "operation": "pi_refresh_assessment",
                "artifact_hash": artifact,
            }
        }
    )
    result = await timeline_routes.get_memory_model_exchanges(
        proposal.proposal_id, NS(id="owner")
    )
    assert (
        result["inference_runs"][0]["model_input"]["prompt"]
        == "Inspect changed context"
    )
    assert result["inference_runs"][0]["tool_calls"][0]["tool"] == "search_material"
    with pytest.raises(HTTPException) as denied:
        await timeline_routes.get_memory_model_exchanges(
            proposal.proposal_id, NS(id="other")
        )
    assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "proposal"])
async def test_context_worker_rejects_changes_arriving_during_investigation(
    database, monkeypatch, tmp_path, change
):
    from pi_task_helpers import install_pi

    from backend.services.timeline import pi_tasks

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    proposal = await recordings.generate_undated("owner", session.session_key, 1)
    await proposal.set({"state": "needs_attention"})
    monkeypatch.setattr(
        context, "snapshot", lambda *a: {"Owner.md": "New accepted context"}
    )
    install_pi(
        monkeypatch,
        tmp_path,
        [
            {
                "verdict": "useful",
                "reason": "New context helps",
                "relevant_paths": ["Owner.md"],
            }
        ],
    )
    invoke = pi_tasks._invoke_pi

    async def change_during_run(*args, **kwargs):
        result = await invoke(*args, **kwargs)
        if change == "source":
            await UndatedSession.get_pymongo_collection().update_one(
                {"session_key": session.session_key}, {"$set": {"revision": 2}}
            )
        else:
            await MemoryReviewProposal.get_pymongo_collection().update_one(
                {"_id": proposal.id}, {"$set": {"state": "excluded", "active": False}}
            )
        return result

    monkeypatch.setattr(pi_tasks, "_invoke_pi", change_during_run)
    await jobs.assess_context_job.__wrapped__("owner|main")
    current = await MemoryReviewProposal.get(proposal.id)
    if change == "source":
        assert current.refresh_assessment["verdict"] == "uncertain"
        assert current.refresh_assessment["error"]
    else:
        assert current.state == "excluded"
        assert current.refresh_assessment is None


@pytest.mark.asyncio
async def test_context_worker_retains_complete_long_explanation(
    database, monkeypatch, tmp_path
):
    from pi_task_helpers import install_pi

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    proposal = await recordings.generate_undated("owner", session.session_key, 1)
    await proposal.set({"state": "needs_attention"})
    monkeypatch.setattr(
        context, "snapshot", lambda *a: {"Owner.md": "New accepted context"}
    )
    reason = (
        "This accepted context resolves the prior lookup while preserving source attribution. "
        * 16
    )
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [{"verdict": "useful", "reason": reason, "relevant_paths": ["Owner.md"]}],
    )
    await jobs.assess_context_job.__wrapped__("owner|main")
    current = await MemoryReviewProposal.get(proposal.id)
    assert current.refresh_assessment["verdict"] == "useful"
    assert current.refresh_assessment["reason"] == reason
    assert not current.refresh_assessment.get("error")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reviewed, previous_verdict",
    [(True, "uncertain"), (True, "useful"), (False, "uncertain")],
)
async def test_context_worker_does_not_treat_unconsulted_existing_notes_as_new(
    database, monkeypatch, tmp_path, reviewed, previous_verdict
):
    from pi_task_helpers import install_pi

    await recording(database)
    session = await recordings.prepare_undated("owner", "intro")
    proposal = await recordings.generate_undated("owner", session.session_key, 1)
    notes = {
        "Owner.md": "Existing accepted identity",
        "Project.md": "Existing accepted project",
    }
    monkeypatch.setattr(context, "snapshot", lambda *a: notes)
    await proposal.set(
        {
            "state": "pending",
            "priority": 100,
            "account": {
                "title": "Prepared",
                "summary": "Prepared account",
                "claims": [],
                "questions": [],
                "useful": False,
            },
            "accepted_context": {
                "scope_hash": context.canonical_hash(notes),
                "notes": [],
                "review": {"verdict": "ready" if reviewed else "revise"},
            },
            "refresh_assessment": (
                {
                    "verdict": previous_verdict,
                    "attempts": 3,
                    **(
                        {"error": "Old cosmetic length rejection"}
                        if previous_verdict == "uncertain"
                        else {}
                    ),
                    "relevant_paths": ["Other.md"],
                    "change_hash": context.canonical_hash(
                        {
                            path: {"before": None, "after": text}
                            for path, text in notes.items()
                        }
                    ),
                    "artifact_hash": "historic-failure",
                    "operation": "pi_refresh_assessment",
                }
                if reviewed
                else None
            ),
        }
    )
    calls = install_pi(
        monkeypatch,
        tmp_path,
        (
            []
            if reviewed
            else [
                {
                    "verdict": "uncertain",
                    "reason": "Account investigation remains incomplete",
                    "relevant_paths": [],
                }
            ]
        ),
    )
    await jobs.assess_context_job.__wrapped__("owner|main")
    current = await MemoryReviewProposal.get(proposal.id)
    if reviewed:
        assert calls == []
        assert current.refresh_assessment["context_unchanged"]
        assert current.refresh_assessment["verdict"] == "unrelated"
        if previous_verdict == "uncertain":
            assert (
                current.refresh_assessment["previous_failure"]["artifact_hash"]
                == "historic-failure"
            )
    else:
        assert len(calls) == 1
        assert current.refresh_assessment["verdict"] == "uncertain"
        assert not current.refresh_assessment.get("context_unchanged")
