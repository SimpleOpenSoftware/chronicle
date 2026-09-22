"""Search indexing, ranking and recovery must respect source privacy."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401

from backend.models import job as job_model
from backend.services import privacy
from backend.services import source_search as search
from backend.services.timeline import (
    consolidation,
    memory_sources,
    recording_sessions,
    sessions,
)
from backend.workers import source_search_jobs as jobs


@pytest.fixture
async def indexed(evidence, monkeypatch):
    await evidence.db.conversations.update_one(
        {},
        {
            "$set": {
                "deleted": False,
                "title": "Synthetic secret marker",
                "summary": "Synthetic summary",
                "active_transcript_version": "synthetic-version",
                "audio_total_duration": 10,
                "transcript_versions": [
                    {
                        "version_id": "synthetic-version",
                        "transcript": "Synthetic secret marker",
                        "segments": [],
                    }
                ],
            }
        },
    )
    model = NS(get_pymongo_collection=lambda: evidence.db.conversations)
    monkeypatch.setattr(search, "Conversation", model)
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(job_model, "create_async_redis", lambda: NS(close=AsyncMock()))
    monkeypatch.setattr(jobs, "distributed_lock", privacy.distributed_lock)
    row = await evidence.db.conversations.find_one({})
    await evidence.db.source_search.insert_one(search.recording_projection(row))
    ordinary = dict(
        row, conversation_id="ordinary-recording", client_id="ordinary-device"
    )
    ordinary.pop("_id", None)
    await evidence.db.conversations.insert_one(ordinary)
    await evidence.db.source_search.insert_one(search.recording_projection(ordinary))
    return row


@pytest.mark.parametrize("stage", ["entry", "projection", "write", "allowed"])
async def test_indexing_rejects_private_input_and_cleans_racing_publication(
    evidence, indexed, monkeypatch, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "projection":
        original = search.asyncio.to_thread

        async def projected(fn, *args, **kwargs):
            result = await original(fn, *args, **kwargs)
            if fn is search.recording_projection:
                await revoke(evidence)
            return result

        monkeypatch.setattr(search.asyncio, "to_thread", projected)
    if stage == "write":
        cls = type(evidence.db.source_search)
        original = cls.replace_one

        async def written(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if self.name == "source_search":
                await revoke(evidence)
            return result

        monkeypatch.setattr(cls, "replace_one", written)
    if stage in {"entry", "allowed"}:
        assert await search.index_recording("synthetic-recording") is (
            stage == "allowed"
        )
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await search.index_recording("synthetic-recording")
    assert bool(
        await evidence.db.source_search.find_one({"key": "synthetic-recording"})
    ) is (stage == "allowed")


async def test_search_filters_before_matching_and_retains_allowed_control(
    evidence, indexed, monkeypatch
):
    original = search.match
    seen = []

    def match(row, *args):
        seen.append(row["key"])
        return original(row, *args)

    monkeypatch.setattr(search, "match", match)
    result = await search.search(
        "evidence-owner", "secret", kinds=["recording"], fields=["title"]
    )
    assert [row["key"] for row in result["items"]] == ["ordinary-recording"]
    assert result["total"] == 1 and seen == ["ordinary-recording"]


@pytest.mark.parametrize("stage", ["rank", "status"])
async def test_search_discards_results_when_policy_changes_during_request(
    evidence, indexed, monkeypatch, stage
):
    await allow(evidence)
    if stage == "rank":
        original = search.asyncio.to_thread

        async def rank(fn, *args, **kwargs):
            result = await original(fn, *args, **kwargs)
            await revoke(evidence)
            return result

        monkeypatch.setattr(search.asyncio, "to_thread", rank)
    else:

        async def status(*args):
            await revoke(evidence)
            return {}

        monkeypatch.setattr(search, "index_status", status)
    with pytest.raises(privacy.PrivacyHeld):
        await search.search(
            "evidence-owner", "secret", kinds=["recording"], fields=["title"]
        )


@pytest.mark.parametrize("allowed", [False, True])
async def test_undated_index_uses_original_recording_policy(
    evidence, indexed, monkeypatch, allowed
):
    if allowed:
        await allow(evidence)
    owned = AsyncMock()
    monkeypatch.setattr(recording_sessions, "owned_recording", owned)
    session = NS(
        user_id="evidence-owner",
        recording_id="synthetic-recording",
        memory_space_id=None,
        session_key="synthetic-session",
        title="Synthetic title",
        sources=[],
        revision=1,
        created_at=START,
        source_hash="synthetic-hash",
    )
    await evidence.db.source_search.insert_one(
        {
            "_id": "session:synthetic-session",
            "kind": "session",
            "key": "synthetic-session",
        }
    )
    result = await recording_sessions.index_undated(session)
    assert result is allowed
    assert (
        bool(await evidence.db.source_search.find_one({"key": "synthetic-session"}))
        is allowed
    )
    if not allowed:
        owned.assert_not_awaited()


@pytest.fixture
async def screen_day(evidence, indexed, monkeypatch):
    end = START + timedelta(seconds=10)
    episode = NS(
        episode_id="synthetic-episode",
        status="settled",
        related_conversation_ids=[],
        source_ids=["screenpipe-test"],
        started_at=START,
        ended_at=end,
        evidence_refs=[
            {
                "source_id": "screenpipe-test",
                "started_at": START,
                "ended_at": end,
                "excerpt": "Synthetic private OCR",
            }
        ],
        title="Synthetic title",
        summary="Synthetic summary",
        revision=1,
    )
    day = NS(
        user_id="evidence-owner",
        current_snapshot="synthetic",
        pending_publication_id=None,
        local_date=START.date(),
        timezone="UTC",
        revised_at=START,
    )
    monkeypatch.setattr(
        consolidation, "snapshot_episodes", AsyncMock(return_value=[episode])
    )
    monkeypatch.setattr(sessions, "resolved_sessions", AsyncMock(return_value=[]))
    sources = Mock(return_value=[])
    monkeypatch.setattr(memory_sources, "evidence_sources", sources)
    return NS(day=day, sources=sources)


@pytest.mark.parametrize("allowed", [False, True])
async def test_screen_only_timeline_index_checks_ranges_before_excerpts(
    evidence, screen_day, allowed
):
    if allowed:
        await allow(evidence)
    result = await search.index_day(screen_day.day)
    assert result is allowed
    row = await evidence.db.source_search.find_one({"key": "synthetic-episode"})
    if allowed:
        assert row is not None and row["privacy_evidence"]
        assert "Synthetic private OCR" not in str(row["privacy_evidence"])
    else:
        assert row is None
        screen_day.sources.assert_not_called()


async def test_screen_only_cached_hit_is_not_ranked_after_exclusion(
    evidence, screen_day, monkeypatch
):
    await allow(evidence)
    await search.index_day(screen_day.day)
    await revoke(evidence)
    matched = Mock()
    monkeypatch.setattr(search, "match", matched)
    result = await search.search(
        "evidence-owner", "synthetic", kinds=["episode"], fields=["title"]
    )
    assert result["items"] == [] and result["total"] == 0
    matched.assert_not_called()


@pytest.mark.parametrize("stage", ["entry", "revision"])
async def test_registered_index_worker_advances_held_rows_but_retries_revision_changes(
    evidence, indexed, monkeypatch, stage
):
    if stage == "revision":
        await allow(evidence)
        original = search.publish_projection

        async def publish(row, visibility):
            await revoke(evidence)
            await original(row, visibility)

        monkeypatch.setattr(search, "publish_projection", publish)
    await asyncio.to_thread(jobs.index_sources_job, "recovery")
    state = await evidence.db.source_search_jobs.find_one({"_id": "recovery"})
    if stage == "entry":
        assert state["completed"] == 2 and state["privacy_held"] == 1
        assert (
            await evidence.db.source_search.find_one({"key": "synthetic-recording"})
            is None
        )
        assert await evidence.db.source_search.find_one({"key": "ordinary-recording"})
    else:
        assert state["state"] == "queued" and state["privacy_waiting"]
        assert (
            state.get("completed", 0) == 0
            and state.get("attempts", 0) == 0
            and state.get("after") is None
        )
