"""Real snapshot resolution must hold one day without breaking corpus recovery."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from fastapi.encoders import jsonable_encoder
from test_privacy_enrollment import START, evidence  # noqa: F401
from test_privacy_source_search import indexed  # noqa: F401

from backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    MemoryReviewProposal,
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
    TimelineReconciliationRequest,
    TimelineSemanticGroupRevision,
)
from backend.routers.modules import timeline_routes
from backend.services import privacy
from backend.services import source_search as search
from backend.services.timeline import consolidation
from backend.services.timeline.snapshots import snapshot_from_projection
from backend.workers import source_search_jobs as jobs


@pytest.fixture
async def days(evidence, indexed):
    await init_beanie(
        database=evidence.db,
        document_models=[
            TimelineDay,
            TimelineEpisode,
            AudioEvidenceSpan,
            DirtyEvidenceRange,
            MemoryReviewProposal,
            TimelineAnalysisRun,
            TimelineReconciliationRequest,
        ],
    )
    rows = []
    for offset, source, marker in [
        (0, "screenpipe-test", "PRIVATE_FIXTURE"),
        (1, "ordinary-device", "ORDINARY_FIXTURE"),
    ]:
        start = START + timedelta(days=offset)
        episode = TimelineEpisode(
            user_id="evidence-owner",
            run_id="synthetic",
            local_date=start.date(),
            timezone="Etc/UTC",
            started_at=start,
            ended_at=start + timedelta(seconds=10),
            kind="work",
            title=marker,
            summary=marker,
            confidence=0.9,
            activity_mode="foreground",
            source_ids=[source],
            revision=1,
        )
        await episode.insert()
        group = TimelineSemanticGroupRevision(
            group_key="synthetic-group-" + str(offset),
            member_revisions=[
                EpisodeRevisionRef(episode_key=episode.episode_key, revision=1)
            ],
            episode_ids=[episode.episode_id],
            source_snapshot_id="a" * 64,
            title=marker,
            summary=marker,
            started_at=start,
            ended_at=episode.ended_at,
        )
        snapshot = snapshot_from_projection(
            user_id="evidence-owner",
            local_date=start.date(),
            timezone_name="Etc/UTC",
            episodes=[episode],
            semantic_group_revisions=[group],
        )
        day = TimelineDay(
            user_id="evidence-owner",
            local_date=start.date(),
            timezone="Etc/UTC",
            current_snapshot=snapshot,
            current_snapshot_id=snapshot.snapshot_id,
            snapshot_state="ready",
            semantic_group_history=[group],
            consolidation_error=marker,
            review_error=marker,
        )
        await day.insert()
        rows.append((day, episode))
    return rows


async def test_snapshot_privacy_hold_uses_the_shared_policy_signal(days):
    with pytest.raises(privacy.PrivacyHeld):
        await consolidation.snapshot_episodes(days[0][0])


async def test_registered_recovery_advances_private_day_and_indexes_next_day(
    evidence, days
):
    await evidence.db.source_search_jobs.insert_one(
        {
            "_id": "recovery",
            "version": search.VERSION,
            "state": "queued",
            "collection": "timeline_days",
            "completed": 0,
        }
    )
    private_day = days[0][0]
    await evidence.db.source_search.insert_one(
        {
            "_id": "session:stale",
            "kind": "session",
            "user_id": "evidence-owner",
            "owner_date": private_day.local_date.isoformat(),
            "timezone": private_day.timezone,
            "memory_space_id": None,
            "title": "PRIVATE_FIXTURE",
        }
    )
    await evidence.db.source_search.insert_one(
        {
            "_id": "session:other-owner",
            "kind": "session",
            "user_id": "other-owner",
            "owner_date": private_day.local_date.isoformat(),
            "timezone": private_day.timezone,
            "memory_space_id": None,
        }
    )
    await asyncio.to_thread(jobs.index_sources_job, "recovery")
    state = await evidence.db.source_search_jobs.find_one({"_id": "recovery"})
    assert (
        state["completed"] == 2
        and state["privacy_held"] == 1
        and state["attempts"] == 0
    )
    assert state["state"] == "queued" and not state["privacy_waiting"]
    assert await evidence.db.source_search.find_one({"_id": "session:stale"}) is None
    assert (
        await evidence.db.source_search.find_one({"_id": "session:other-owner"})
        is not None
    )
    assert (
        await evidence.db.source_search.find_one({"key": days[1][1].episode_id})
        is not None
    )


async def test_private_day_route_omits_saved_group_and_error_text_but_keeps_markers(
    days,
):
    day = days[0][0]
    result = await timeline_routes.get_timeline_day(
        day.local_date, day.timezone, SimpleNamespace(id="evidence-owner")
    )
    assert result["semantic_groups"] == [] and result["episodes"] == []
    assert result["coverage"]["privacy_intervals"]
    assert "PRIVATE_FIXTURE" not in json.dumps(jsonable_encoder(result))


@pytest.mark.parametrize("missing", [False, True])
async def test_allowed_day_omits_group_with_private_or_missing_member(days, missing):
    day, ordinary = days[1]
    private = days[0][1]
    group = day.semantic_group_history[0]
    group.member_revisions.append(
        EpisodeRevisionRef(episode_key=private.episode_key, revision=1)
    )
    group.episode_ids.append(private.episode_id)
    group.summary = "PRIVATE_FIXTURE"
    snapshot = snapshot_from_projection(
        user_id=day.user_id,
        local_date=day.local_date,
        timezone_name=day.timezone,
        episodes=[ordinary],
        semantic_group_revisions=[group],
    )
    day.current_snapshot = snapshot
    day.current_snapshot_id = snapshot.snapshot_id
    await day.save()
    if missing:
        await private.delete()
    result = await timeline_routes.get_timeline_day(
        day.local_date, day.timezone, SimpleNamespace(id="evidence-owner")
    )
    assert result["semantic_groups"] == []
    assert len(result["episodes"]) == 1
    assert "PRIVATE_FIXTURE" not in json.dumps(jsonable_encoder(result))


async def test_day_route_rechecks_privacy_before_returning_visible_content(
    evidence, days, monkeypatch
):
    day = days[1][0]
    original = privacy.list_intervals

    async def changed(*args, **kwargs):
        rows = await original(*args, **kwargs)
        await evidence.db.capture_sources.insert_one(
            dict(
                user_id="evidence-owner",
                source_id="ordinary-device",
                privacy_enabled_from=START,
                privacy_revision=1,
            )
        )
        return rows

    monkeypatch.setattr(privacy, "list_intervals", changed)
    with pytest.raises(privacy.PrivacyHeld):
        await timeline_routes.get_timeline_day(
            day.local_date, day.timezone, SimpleNamespace(id="evidence-owner")
        )


async def test_missing_snapshot_revision_remains_a_structural_failure(days):
    day, episode = days[1]
    await episode.delete()
    with pytest.raises(consolidation.ConsolidationResolutionError):
        await search.index_day(day)
