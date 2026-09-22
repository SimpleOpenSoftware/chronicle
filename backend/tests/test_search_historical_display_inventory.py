"""Search recovery must admit screened history predating live display inventory."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_privacy_inventory_refinement import (  # noqa: F401
    HIGH,
    LOW,
    SOURCE,
    db,
    prepare,
    request,
)

from backend.models import job as job_model
from backend.routers.modules import device_input_routes as routes
from backend.services import privacy
from backend.services import source_search as search
from backend.workers import source_search_jobs as jobs


@pytest.mark.parametrize(
    "case",
    [
        "screened_history",
        "no_inventory_refinement",
        "unproven_inventory",
        "missing_screening",
        "excluded",
        "pending",
        "empty_live_inventory",
        "additional_live_display",
        "live_transition",
        "additional_empty_requirement",
        "outside_historical_range",
    ],
)
async def test_registered_recovery_finds_screened_historical_transcript(
    db, monkeypatch, case
):
    await prepare(db)
    await db.capture_sources.update_one({}, {"$set": {"privacy_enabled_from": LOW}})
    if case != "no_inventory_refinement":
        await routes.refine_privacy_required_range(request(), SOURCE)

    low, high = LOW + timedelta(seconds=1), LOW + timedelta(seconds=4)
    if case == "unproven_inventory":
        await db.privacy_required_ranges.update_many(
            {"superseded_by": {"$exists": False}},
            {"$set": {"coverage": "historical_observed_displays"}},
        )
    elif case == "missing_screening":
        await db.privacy_screening.delete_many({})
    elif case in {"excluded", "pending"}:
        await db.privacy_screening.update_one(
            {"track_id": "first"}, {"$set": {"segments.0.state": case}}
        )
    elif case in {"empty_live_inventory", "additional_live_display", "live_transition"}:
        await db.privacy_display_sets.insert_one(
            dict(
                user_id="owner",
                source_id=SOURCE.source_id,
                observed_at=high if case == "live_transition" else LOW,
                transition_started_at=LOW,
                track_ids=[] if case == "empty_live_inventory" else ["unverified"],
            )
        )
    elif case == "additional_empty_requirement":
        await db.privacy_required_ranges.insert_one(
            dict(
                user_id="owner",
                source_id=SOURCE.source_id,
                started_at=LOW,
                ended_at=HIGH,
                track_ids=[],
            )
        )
    elif case == "outside_historical_range":
        low, high = HIGH, HIGH + timedelta(seconds=1)

    text = "This has been slower than expected."
    await db.conversations.insert_one(
        dict(
            conversation_id="historical-huddle",
            user_id="owner",
            client_id="screenpipe-client",
            deleted=False,
            started_at=low,
            ended_at=high,
            audio_ranges=[
                dict(
                    capture_source_id=SOURCE.source_id + ":input:microphone",
                    started_at=low,
                    ended_at=high,
                )
            ],
            active_transcript_version="huddle-transcript",
            transcript_versions=[
                dict(
                    version_id="huddle-transcript",
                    transcript=text,
                    segments=[dict(start=0, end=3, text=text)],
                )
            ],
        )
    )
    monkeypatch.setattr(
        search, "Conversation", NS(get_pymongo_collection=lambda: db.conversations)
    )
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(job_model, "create_async_redis", lambda: NS(close=AsyncMock()))
    monkeypatch.setattr(jobs, "distributed_lock", privacy.distributed_lock)

    await asyncio.to_thread(jobs.index_sources_job, "recovery")
    result = await search.search(
        "owner", "slower", kinds=["recording"], fields=["transcript"]
    )
    assert [hit["key"] for hit in result["items"]] == (
        ["historical-huddle"] if case == "screened_history" else []
    )
    state = await db.source_search_jobs.find_one({"_id": "recovery"})
    assert state["completed"] == 1
    assert state["privacy_held"] == (0 if case == "screened_history" else 1)
