"""Exercise source setup and activation through registered route entry points."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_entrypoints import db  # noqa: F401

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy, privacy_setup

USER = SimpleNamespace(id="owner", user_id="owner")
SCOPE = {"user_id": "owner", "source_id": "screenpipe-test"}


@pytest.fixture
async def setup(db, monkeypatch):
    now = privacy.utc(datetime.now(timezone.utc))
    start = now - timedelta(minutes=10)
    await db.capture_sources.update_one(
        SCOPE,
        {
            "$set": {
                "provider": "screenpipe",
                "privacy_enabled_from": None,
                "health": {},
                "last_seen_at": now,
            }
        },
    )
    await db.privacy_display_sets.delete_many({})

    async def find(query):
        row = await db.capture_sources.find_one(query)
        return SimpleNamespace(**row) if row else None

    monkeypatch.setattr(routes.CaptureSource, "find_one", find)
    return start, now


async def ready(db, now):
    await db.capture_sources.update_one(
        SCOPE,
        {
            "$set": {
                "health": {
                    "privacy_screening": {
                        "state": "ready",
                        "model_version": "synthetic-model",
                        "inventory_checked_at": now.isoformat(),
                    }
                },
                "last_seen_at": now,
            }
        },
    )
    await db.privacy_display_sets.insert_one(
        {
            **SCOPE,
            "track_ids": ["display"],
            "observed_at": now - timedelta(minutes=1),
            "transition_started_at": now - timedelta(minutes=1),
        }
    )
    await db.privacy_screening.insert_one(
        {
            **SCOPE,
            "track_id": "display",
            "model_version": "synthetic-model",
            "started_at": now - timedelta(seconds=30),
            "ended_at": now,
            "segments": [
                {
                    "started_at": now - timedelta(seconds=30),
                    "ended_at": now,
                    "state": "allowed",
                }
            ],
        }
    )


async def hold(start):
    return await routes.prepare_privacy(
        SCOPE["source_id"], routes.PrivacyActivation(started_at=start), USER
    )


async def activate(start):
    return await routes.activate_privacy(
        SCOPE["source_id"], routes.PrivacyActivation(started_at=start), USER
    )


async def test_prepare_holds_all_tracks_and_future_without_claiming_detection(
    db, setup
):
    start, now = setup
    result = await hold(start)
    assert result == {"active": False, "started_at": start}
    snapshot = await privacy.load_snapshot("owner")
    for suffix in ["", ":input:microphone", ":output:system"]:
        for low in [start, now + timedelta(hours=1)]:
            assert not snapshot.permits(
                SCOPE["source_id"] + suffix, low, low + timedelta(seconds=1)
            )
    assert snapshot.permits("unrelated-device", start, now)
    assert snapshot.permits(SCOPE["source_id"], start - timedelta(seconds=1), start)
    assert not (await db.capture_sources.find_one(SCOPE))["privacy_enabled_from"]
    invalidation = routes.dirty_ranges.mark_evidence_dirty.call_args.args
    assert invalidation[1] == start
    assert now <= invalidation[2] <= privacy.utc(datetime.now(timezone.utc))
    rows = await routes.privacy_intervals(start, now, USER)
    assert all(
        r["reason"]
        == "Local screening is not ready; screen and audio processing remain held."
        for r in rows["intervals"]
    )
    await db.privacy_overrides.insert_one(
        {
            **SCOPE,
            "started_at": start,
            "ended_at": now,
            "override": "allowed",
            "revision": 3,
        }
    )
    assert (await privacy.load_snapshot("owner")).permits(
        SCOPE["source_id"], start, now
    )


async def test_readiness_failure_keeps_setup_and_activation_preserves_earlier_time(
    db, setup
):
    start, now = setup
    await hold(start)
    with pytest.raises(routes.HTTPException, match="readiness"):
        await activate(now)
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]]) == {
        SCOPE["source_id"]: start
    }
    await ready(db, now)
    result = await activate(now)
    assert result["started_at"] == start
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]]) == {}
    snapshot = await privacy.load_snapshot("owner")
    assert not snapshot.permits(SCOPE["source_id"], start, start + timedelta(seconds=1))
    assert snapshot.permits(SCOPE["source_id"], now - timedelta(seconds=10), now)
    assert not snapshot.permits(SCOPE["source_id"], now, now + timedelta(seconds=1))
    revision = (await db.capture_sources.find_one(SCOPE))["privacy_revision"]
    await hold(start)  # Late setup retries cannot recreate retired obligations.
    await activate(now)
    assert (await db.capture_sources.find_one(SCOPE))["privacy_revision"] == revision
    assert (
        await db.privacy_required_ranges.count_documents(
            {**SCOPE, "superseded_by": {"$exists": True}}
        )
        == 1
    )


@pytest.mark.parametrize("phase", ["prepare", "activate"])
async def test_interrupted_invalidation_remains_held_until_exact_retry(
    db, setup, monkeypatch, phase
):
    start, now = setup
    if phase == "activate":
        await hold(start)
        await ready(db, now)
    dirty = AsyncMock(side_effect=RuntimeError("synthetic interruption"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", dirty)
    action, requested = (hold, start) if phase == "prepare" else (activate, now)
    with pytest.raises(RuntimeError):
        await action(requested)
    assert (await db.capture_sources.find_one(SCOPE))["privacy_updating"]
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]])
    assert not (await privacy.load_snapshot("owner")).permits(
        SCOPE["source_id"], now - timedelta(seconds=10), now
    )
    dirty.side_effect = None
    await action(requested)
    assert not (await db.capture_sources.find_one(SCOPE))["privacy_updating"]
    assert dirty.call_args.args[1] == start
    revision = (await db.capture_sources.find_one(SCOPE))["privacy_revision"]
    await action(requested)
    assert (await db.capture_sources.find_one(SCOPE))["privacy_revision"] == revision
    assert dirty.await_count == 2


async def test_restart_after_retirement_still_uses_durable_earlier_start(
    db, setup, monkeypatch
):
    start, now = setup
    await hold(start)
    await ready(db, now)
    collection = type(db.capture_sources)
    original = collection.update_one

    async def fail_clear(self, query, update, **kwargs):
        if (
            self.name == "capture_sources"
            and update.get("$set", {}).get("privacy_updating") is False
        ):
            raise RuntimeError("synthetic interruption after retirement")
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection, "update_one", fail_clear)
    with pytest.raises(RuntimeError):
        await activate(now)
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]]) == {}
    assert not (await privacy.load_snapshot("owner")).permits(
        SCOPE["source_id"], now - timedelta(seconds=10), now
    )
    monkeypatch.setattr(collection, "update_one", original)
    assert (await activate(now))["started_at"] == start
    assert routes.dirty_ranges.mark_evidence_dirty.call_args.args[1] == start


async def test_setup_resumes_failure_before_obligation_write(db, setup, monkeypatch):
    start, _ = setup
    collection = type(db.privacy_required_ranges)
    original = collection.update_one

    async def fail_insert(self, *args, **kwargs):
        if self.name == "privacy_required_ranges":
            raise RuntimeError("synthetic write failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(collection, "update_one", fail_insert)
    with pytest.raises(RuntimeError):
        await hold(start)
    assert (await db.capture_sources.find_one(SCOPE))["privacy_updating"]
    revision = (await db.capture_sources.find_one(SCOPE))["privacy_revision"]
    monkeypatch.setattr(collection, "update_one", original)
    await hold(start)
    assert (await db.capture_sources.find_one(SCOPE))["privacy_revision"] == revision


@pytest.mark.parametrize(
    "change,code",
    [
        ("other_owner", 404),
        ("other_provider", 422),
        ("future", 422),
        ("too_old", 422),
        ("other_update", 409),
    ],
)
async def test_setup_rejects_invalid_scope_or_state(db, setup, change, code):
    start, now = setup
    if change == "other_owner":
        await db.capture_sources.update_one(SCOPE, {"$set": {"user_id": "other"}})
    elif change == "other_provider":
        await db.capture_sources.update_one(SCOPE, {"$set": {"provider": "mobile"}})
    elif change == "future":
        start = now + timedelta(days=1)
    elif change == "too_old":
        start = now - timedelta(days=33)
    else:
        await db.capture_sources.update_one(
            SCOPE, {"$set": {"privacy_updating": True, "privacy_operation": "other"}}
        )
    with pytest.raises(routes.HTTPException) as exc:
        await hold(start)
    assert exc.value.status_code == code
    assert await db.privacy_required_ranges.count_documents({}) == 0


async def test_preparation_cannot_backdate_already_activated_source(db, setup):
    start, now = setup
    await ready(db, now)
    await activate(now)
    with pytest.raises(routes.HTTPException) as exc:
        await hold(start)
    assert exc.value.status_code == 409
    assert (await hold(now))["active"]


async def test_waiting_listing_scopes_owner_sources_and_retirement(db, setup):
    start, now = setup
    await hold(start)
    await db.privacy_required_ranges.insert_many(
        [
            {
                "user_id": "other",
                "source_id": SCOPE["source_id"],
                "started_at": start - timedelta(days=1),
                "coverage": privacy_setup.WAITING_COVERAGE,
            },
            {
                **SCOPE,
                "source_id": "other-device",
                "started_at": start,
                "coverage": privacy_setup.WAITING_COVERAGE,
            },
            {
                **SCOPE,
                "started_at": start - timedelta(days=1),
                "coverage": privacy_setup.WAITING_COVERAGE,
                "superseded_by": "completed",
            },
        ]
    )
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]]) == {
        SCOPE["source_id"]: start
    }


async def test_revision_change_during_readiness_does_not_retire_waiting_hold(
    db, setup, monkeypatch
):
    start, now = setup
    await hold(start)
    await ready(db, now)
    original = privacy.begin_update

    async def race(owner, query, update):
        await db.capture_sources.update_one(SCOPE, {"$inc": {"privacy_revision": 1}})
        return await original(owner, query, update)

    monkeypatch.setattr(privacy, "begin_update", race)
    with pytest.raises(routes.HTTPException) as exc:
        await activate(now)
    assert exc.value.status_code == 409
    assert await privacy_setup.waiting_starts("owner", [SCOPE["source_id"]])
    assert not (await db.capture_sources.find_one(SCOPE))["privacy_enabled_from"]


async def test_registered_prepare_activate_and_source_listing(db, setup, monkeypatch):
    import httpx
    from beanie.odm.fields import ExpressionField
    from fastapi import FastAPI

    start, now = setup
    monkeypatch.setattr(
        routes.CaptureSource, "user_id", ExpressionField("user_id"), raising=False
    )

    class Sources:
        def sort(self, _sort):
            return self

        async def to_list(self):
            rows = await db.capture_sources.find({"user_id": "owner"}).to_list(None)
            return [
                SimpleNamespace(
                    **{
                        **r,
                        "name": "Synthetic display",
                        "platform": "test",
                        "status": "online",
                        "capabilities": [],
                    }
                )
                for r in rows
            ]

    def find(query):
        assert query == {"user_id": "owner"}
        return Sources()

    monkeypatch.setattr(routes.CaptureSource, "find", find)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.current_active_user] = lambda: USER
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        base = "/device-input/sources/screenpipe-test/privacy"
        response = await client.post(
            base + "/prepare", json={"started_at": start.isoformat()}
        )
        assert response.status_code == 200
        listing = await client.get("/device-input/sources")
        assert listing.status_code == 200
        row = listing.json()["sources"][0]
        assert privacy.utc(row["privacy_waiting_from"]) == start
        assert row["privacy_enabled_from"] is None
        await ready(db, now)
        response = await client.post(
            base + "/activate", json={"started_at": now.isoformat()}
        )
        assert response.status_code == 200
        row = (await client.get("/device-input/sources")).json()["sources"][0]
        assert row["privacy_waiting_from"] is None
        assert privacy.utc(row["privacy_enabled_from"]) == start
