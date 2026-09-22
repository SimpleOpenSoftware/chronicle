"""Real screening HTTP entry points and durable policy state; synthetic data only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_privacy_entrypoints import START, db, result  # noqa: F401

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy


@pytest.fixture
async def client(db):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._device_source] = lambda: SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def submit(client, identifier, state="pending", **changes):
    body = result(state).model_dump(mode="json")
    body.update(interval_id=identifier, **changes)
    response = await client.post("/device-input/screening", json=body)
    assert response.status_code == 200
    return body


async def replace(client, previous="old", replacement="new"):
    return await client.post(
        "/device-input/screening/replace",
        json={
            "previous_interval_ids": [previous],
            "replacement_interval_id": replacement,
        },
    )


async def allowed():
    return (await privacy.load_snapshot("owner")).permits(
        "screenpipe-test:input:mic", START, START + timedelta(seconds=10)
    )


async def test_replacement_releases_only_completed_exact_interval_and_is_replayable(
    db, client
):
    old = await submit(client, "old")
    assert (await replace(client)).status_code == 409
    assert not await allowed()
    await submit(client, "new", "allowed", policy_version="screen-privacy-v2")
    assert not await allowed()  # New overlapping prediction alone is insufficient.
    before = await privacy.load_snapshot("owner")
    assert (await replace(client)).status_code == 200
    assert await allowed()
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    revision = (await db.capture_sources.find_one({}))["privacy_revision"]
    assert (await replace(client)).status_code == 200
    assert (await db.capture_sources.find_one({}))["privacy_revision"] == revision
    assert (await client.post("/device-input/screening", json=old)).status_code == 200
    assert await allowed()
    assert await db.privacy_screening.count_documents({}) == 2
    assert (await db.privacy_screening.find_one({"interval_id": "old"}))[
        "superseded_by"
    ] == "new"


@pytest.mark.parametrize("mismatch", ["track", "bounds", "source", "owner"])
async def test_replacement_cannot_release_another_interval_or_owner(
    db, client, mismatch
):
    await submit(client, "old")
    await submit(client, "new", "allowed")
    changes = {
        "track": {"track_id": "another-display"},
        "bounds": {"ended_at": START + timedelta(seconds=11)},
        "source": {"source_id": "another-source"},
        "owner": {"user_id": "another-owner"},
    }[mismatch]
    await db.privacy_screening.update_one({"interval_id": "new"}, {"$set": changes})
    revision = (await db.capture_sources.find_one({}))["privacy_revision"]
    assert (await replace(client)).status_code == 409
    assert (await db.capture_sources.find_one({}))["privacy_revision"] == revision
    assert not await allowed()


@pytest.mark.parametrize("independent_hold", ["override", "overlap"])
async def test_supersession_preserves_unrelated_exclusions(
    db, client, independent_hold
):
    await submit(client, "old")
    await submit(client, "new", "allowed")
    if independent_hold == "override":
        await db.privacy_overrides.insert_one(
            {
                "user_id": "owner",
                "source_id": "screenpipe-test",
                "started_at": START,
                "ended_at": START + timedelta(seconds=10),
                "override": "excluded",
                "revision": 20,
            }
        )
    else:
        await submit(client, "independent", "excluded")
    assert (await replace(client)).status_code == 200
    assert not await allowed()
    assert (await privacy.load_snapshot("other-owner")).permits(
        "screenpipe-test", START, START + timedelta(seconds=10)
    )


async def test_interrupted_replacement_holds_until_same_request_finishes(
    db, client, monkeypatch
):
    await submit(client, "old")
    await submit(client, "new", "allowed")
    marker = AsyncMock(side_effect=RuntimeError("Synthetic invalidation interruption"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", marker)
    with pytest.raises(RuntimeError):
        await replace(client)
    assert (await db.capture_sources.find_one({}))["privacy_updating"]
    assert not await allowed()
    marker.side_effect = None
    assert (await replace(client)).status_code == 200
    assert await allowed()


async def test_stale_fork_and_cycle_rejected_but_completed_replay_remains_idempotent(
    db, client
):
    await submit(client, "old")
    await submit(client, "new", "allowed")
    await submit(client, "later", "excluded")
    assert (await replace(client)).status_code == 200
    assert (await replace(client, "old", "later")).status_code == 409
    assert (await replace(client, "new", "old")).status_code == 409
    assert (await replace(client, "new", "later")).status_code == 200
    assert (await replace(client)).status_code == 200
    assert not await allowed()


async def test_metadata_listing_scopes_owner_source_version_and_active_results(
    db, client
):
    await submit(client, "old")
    await submit(client, "new", "allowed", policy_version="screen-privacy-v2")
    row = await db.privacy_screening.find_one({"interval_id": "old"})
    other = deepcopy(row)
    other.update(_id="f" * 64, user_id="another-owner")
    await db.privacy_screening.insert_one(other)
    await db.privacy_screening.update_one(
        {"interval_id": "old", "user_id": "owner"},
        {"$set": {"unrelated_private_text": "synthetic sentinel"}},
    )
    params = {
        "start_at": START.isoformat(),
        "end_at": (START + timedelta(seconds=10)).isoformat(),
        "policy_version": "screen-privacy-v1",
    }
    response = await client.get("/device-input/screening/results", params=params)
    assert response.status_code == 200
    assert [r["interval_id"] for r in response.json()["results"]] == ["old"]
    returned = response.json()["results"][0]
    for record in [returned, *returned["segments"], *returned["evidence"]]:
        for field in ("started_at", "ended_at", "captured_at"):
            if field in record:
                assert datetime.fromisoformat(record[field]).tzinfo == timezone.utc
    assert (
        "synthetic sentinel" not in response.text
        and "another-owner" not in response.text
    )
    assert response.json()["next_cursor"] is None
    assert (await replace(client)).status_code == 200
    assert (await client.get("/device-input/screening/results", params=params)).json()[
        "results"
    ] == []


async def test_history_coverage_accepts_current_policy_version(db, client):
    response = await client.post(
        "/device-input/screening/required-range",
        json={
            "started_at": START.isoformat(),
            "ended_at": (START + timedelta(seconds=10)).isoformat(),
            "track_ids": ["display"],
            "policy_version": "screen-privacy-v2",
        },
    )
    assert response.status_code == 200
    assert not await allowed()


async def test_replacement_rechecks_after_another_writer_wins(db, client, monkeypatch):
    await submit(client, "old")
    await submit(client, "new", "allowed")
    await submit(client, "winner", "excluded")
    begin = privacy.begin_update
    raced = False

    async def other_writer(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert (await replace(client, "old", "winner")).status_code == 200
        return await begin(*args, **kwargs)

    monkeypatch.setattr(privacy, "begin_update", other_writer)
    assert (await replace(client)).status_code == 409
    assert not (await db.capture_sources.find_one({}))["privacy_updating"]
    assert (await db.privacy_screening.find_one({"interval_id": "old"}))[
        "superseded_by"
    ] == "winner"
    assert not await allowed()


async def test_collector_revision_routes_require_authentication():
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/device-input/screening/replace",
            json={
                "previous_interval_ids": ["old"],
                "replacement_interval_id": "new",
            },
        )
        assert response.status_code in (401, 403)
        response = await client.get(
            "/device-input/screening/results",
            params={
                "start_at": START.isoformat(),
                "end_at": (START + timedelta(seconds=10)).isoformat(),
                "policy_version": "screen-privacy-v1",
            },
        )
        assert response.status_code in (401, 403)
