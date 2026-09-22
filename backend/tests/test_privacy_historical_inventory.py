"""Missing historical inventory stays held until exact inventory recovery."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_entrypoints import START, db  # noqa: F401

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy

SOURCE = SimpleNamespace(
    user_id="owner", source_id="screenpipe-test", provider="screenpipe"
)
LOW, HIGH = START - timedelta(days=1), START - timedelta(hours=23)


def obligation(tracks, *, start=LOW, end=HIGH):
    return privacy.PrivacyRequiredRange(
        started_at=start, ended_at=end, track_ids=tracks
    )


async def screened(db, track="display"):
    await db.privacy_screening.insert_one(
        dict(
            user_id="owner",
            source_id=SOURCE.source_id,
            track_id=track,
            started_at=LOW,
            ended_at=HIGH,
            segments=[
                dict(
                    started_at=LOW,
                    ended_at=HIGH - timedelta(seconds=1),
                    state="allowed",
                ),
                dict(
                    started_at=HIGH - timedelta(seconds=1),
                    ended_at=HIGH,
                    state="pending",
                    reason="missing_frame",
                ),
            ],
        )
    )


async def test_exact_inventory_recovery_releases_only_screened_pieces(db):
    unknown, known = obligation([]), obligation(["display"])
    await routes.submit_privacy_required_range(unknown, SOURCE)
    await screened(db)
    before = await privacy.load_snapshot("owner")
    assert not before.permits(SOURCE.source_id, LOW, HIGH)
    await routes.submit_privacy_required_range(known, SOURCE)
    await routes.submit_privacy_required_range(known, SOURCE)
    # A delayed retry of the original unknown inventory cannot restore the hold.
    await routes.submit_privacy_required_range(unknown, SOURCE)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    snapshot = await privacy.load_snapshot("owner")
    for suffix in ("", ":input:microphone", ":output:system"):
        assert snapshot.allowed_spans(SOURCE.source_id + suffix, LOW, HIGH) == [
            (LOW, HIGH - timedelta(seconds=1))
        ]
    assert snapshot.permits("another-device", LOW, HIGH)
    rows = await db.privacy_required_ranges.find({}).to_list(None)
    assert len(rows) == 2  # preserve the original unknown-inventory record
    old = next(row for row in rows if not row["track_ids"])
    new = next(row for row in rows if row["track_ids"])
    assert old["superseded_by"] == new["_id"]
    assert len((await privacy.load_snapshot("owner")).required_ranges) == 1


@pytest.mark.parametrize("change", ["shorter", "another_source"])
async def test_partial_or_other_device_inventory_cannot_release_unknown_range(
    db, change
):
    await routes.submit_privacy_required_range(obligation([]), SOURCE)
    await screened(db)
    source, body = SOURCE, obligation(["display"])
    if change == "shorter":
        body = obligation(["display"], end=HIGH - timedelta(seconds=1))
    else:
        source = SimpleNamespace(
            user_id="owner", source_id="screenpipe-other", provider="screenpipe"
        )
        await db.capture_sources.insert_one(
            dict(source_id=source.source_id, user_id="owner", privacy_revision=0)
        )
    await routes.submit_privacy_required_range(body, source)
    assert not (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )
    assert (
        await db.privacy_required_ranges.count_documents(
            {"track_ids": [], "superseded_by": {"$exists": True}}
        )
        == 0
    )


async def test_known_display_obligations_are_never_retired_by_recovery(db):
    for tracks in ([], ["second"], ["display"]):
        await routes.submit_privacy_required_range(obligation(tracks), SOURCE)
    await screened(db)
    assert not (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )
    await screened(db, "second")
    assert (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )


async def test_recovery_invalidation_failure_keeps_unknown_inventory_held(
    db, monkeypatch
):
    await routes.submit_privacy_required_range(obligation([]), SOURCE)
    await screened(db)
    dirty = AsyncMock(side_effect=RuntimeError("Synthetic interrupted invalidation"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", dirty)
    with pytest.raises(RuntimeError):
        await routes.submit_privacy_required_range(obligation(["display"]), SOURCE)
    assert (
        await db.privacy_required_ranges.count_documents(
            {"superseded_by": {"$exists": True}}
        )
        == 0
    )
    assert not (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH
    )
    dirty.side_effect = None
    await routes.submit_privacy_required_range(obligation(["display"]), SOURCE)
    assert (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )


async def test_failure_after_supersession_stays_held_until_retry(db, monkeypatch):
    await routes.submit_privacy_required_range(obligation([]), SOURCE)
    await screened(db)
    collection_type = type(db.privacy_required_ranges)
    update = collection_type.update_many

    async def interrupted(collection, *args, **kwargs):
        result = await update(collection, *args, **kwargs)
        if collection.name == "privacy_required_ranges":
            raise RuntimeError("Synthetic crash after inventory replacement")
        return result

    monkeypatch.setattr(collection_type, "update_many", interrupted)
    with pytest.raises(RuntimeError):
        await routes.submit_privacy_required_range(obligation(["display"]), SOURCE)
    assert (
        await db.privacy_required_ranges.count_documents(
            {"superseded_by": {"$exists": True}}
        )
        == 1
    )
    assert not (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )
    monkeypatch.setattr(collection_type, "update_many", update)
    await routes.submit_privacy_required_range(obligation(["display"]), SOURCE)
    assert (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, HIGH - timedelta(seconds=1)
    )
