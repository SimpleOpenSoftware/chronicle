"""All-source discovery through the registered index worker and HTTP search API."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_session_memory import evidence
from test_source_search_flow import database, recording  # noqa: F401

from backend.models.timeline import (
    EpisodeRevisionRef,
    TimelineDay,
    TimelineDaySnapshot,
    TimelineEpisode,
)
from backend.routers.modules import source_search_routes as routes
from backend.services import source_search
from backend.workers import source_search_jobs


@pytest.mark.asyncio
async def test_all_scopes_index_episode_evidence_and_reject_stale_publications(
    database,
):
    await recording(database, text="We discussed pottery at the workshop.", dated=True)
    now = datetime(2026, 9, 4, 7, tzinfo=timezone.utc)
    ref = evidence("pottery", text="We discussed pottery at the workshop.", start=now)
    ref.metadata["conversation_id"] = "intro"
    episode = TimelineEpisode(
        user_id="owner",
        run_id="run",
        local_date=now.date(),
        timezone="Asia/Kolkata",
        title="Pottery workshop",
        summary="Practising pottery together.",
        started_at=now,
        ended_at=now + timedelta(minutes=30),
        kind="activity",
        confidence=1,
        activity_mode="foreground",
        evidence_refs=[ref],
    )
    await episode.insert()
    snapshot = TimelineDaySnapshot(
        snapshot_id="a" * 64,
        evidence_state_hash="b" * 64,
        episode_revisions=[
            EpisodeRevisionRef(
                episode_key=episode.episode_key, revision=episode.revision
            )
        ],
    )
    day = TimelineDay(
        user_id="owner",
        local_date=now.date(),
        timezone="Asia/Kolkata",
        current_snapshot=snapshot,
        current_snapshot_id=snapshot.snapshot_id,
    )
    await day.insert()
    # Exercise real registered job orchestration, including its durable collection cursor.
    for _ in range(6):
        await source_search_jobs.index_sources_job.__wrapped__("recovery")

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.current_active_user] = lambda: NS(id="owner")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/search", params={"q": "pottery"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert {item["kind"] for item in items} == {"recording", "episode", "session"}
        hit = next(item for item in items if item["kind"] == "episode")
        assert hit["url"] == f"/timeline/{episode.episode_id}"
        assert "pottery" in hit["excerpt"]
        for kind in ("recording", "episode", "session"):
            scoped = await client.get(
                "/api/search", params={"q": "pottery", "kinds": kind}
            )
            assert {item["kind"] for item in scoped.json()["items"]} == {kind}
        assert (
            await client.get(
                "/api/search", params={"q": "pottery", "kinds": "capture_session"}
            )
        ).status_code == 422

        app.dependency_overrides[routes.current_active_user] = lambda: NS(id="other")
        assert (await client.get("/api/search", params={"q": "pottery"})).json()[
            "items"
        ] == []
        app.dependency_overrides[routes.current_active_user] = lambda: NS(id="owner")

        await day.set({"pending_publication_id": "publishing"})
        assert (
            await client.get("/api/search", params={"q": "pottery", "kinds": "episode"})
        ).json()["items"] == []
        await day.set({"pending_publication_id": None})
        assert (
            await client.get("/api/search", params={"q": "pottery", "kinds": "episode"})
        ).json()["items"]
        await database.conversations.update_one(
            {"conversation_id": "intro"}, {"$set": {"active_transcript_version": "v1"}}
        )
        assert (
            await client.get("/api/search", params={"q": "pottery", "kinds": "episode"})
        ).json()["items"] == []
        await database.conversations.update_one(
            {"conversation_id": "intro"}, {"$set": {"active_transcript_version": "v2"}}
        )
        await episode.set({"status": "superseded"})
        assert (
            await client.get("/api/search", params={"q": "pottery", "kinds": "episode"})
        ).json()["items"] == []
