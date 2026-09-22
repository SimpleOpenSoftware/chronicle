"""Run authenticated collector orchestration with synthetic inventory evidence."""

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
LOW = START - timedelta(days=1)
HIGH = LOW + timedelta(seconds=20)


def request(**change):
    from backend.services.privacy_history_inventory import InventoryRefinement

    data = dict(
        original=dict(started_at=LOW, ended_at=HIGH, track_ids=["first", "second"]),
        observations=[
            dict(
                observed_at=LOW,
                transition_started_at=LOW,
                track_ids=["first"],
                evidence_sha256="a" * 64,
            ),
            dict(
                observed_at=LOW + timedelta(seconds=10),
                transition_started_at=LOW + timedelta(seconds=5),
                track_ids=["second"],
                evidence_sha256="b" * 64,
            ),
        ],
    )
    data.update(change)
    return InventoryRefinement(**data)


async def prepare(db):
    await routes.submit_privacy_required_range(
        privacy.PrivacyRequiredRange(
            started_at=LOW, ended_at=HIGH, track_ids=["first", "second"]
        ),
        SOURCE,
    )
    for track, low, high in [
        ("first", LOW, LOW + timedelta(seconds=10)),
        ("second", LOW + timedelta(seconds=10), HIGH),
    ]:
        await db.privacy_screening.insert_one(
            dict(
                user_id="owner",
                source_id=SOURCE.source_id,
                track_id=track,
                started_at=low,
                ended_at=high,
                segments=[dict(started_at=low, ended_at=high, state="allowed")],
            )
        )


@pytest.mark.parametrize("activated_before_history", [False, True])
async def test_refinement_preserves_unknown_transition_and_same_device_policy(
    db, activated_before_history
):
    await prepare(db)
    if activated_before_history:
        await db.capture_sources.update_one({}, {"$set": {"privacy_enabled_from": LOW}})
    before = await privacy.load_snapshot("owner")
    assert not before.permits(SOURCE.source_id, LOW, HIGH)
    body = request()
    await routes.refine_privacy_required_range(body, SOURCE)
    snapshot = await privacy.load_snapshot("owner")
    for suffix in ["", ":input:microphone", ":output:system"]:
        assert snapshot.allowed_spans(SOURCE.source_id + suffix, LOW, HIGH) == [
            (LOW, LOW + timedelta(seconds=5)),
            (LOW + timedelta(seconds=10), HIGH),
        ]
    assert snapshot.permits("another-device", LOW, HIGH)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    assert (
        await db.privacy_required_ranges.count_documents(
            {"superseded_by": {"$exists": True}}
        )
        == 1
    )
    assert await db.privacy_inventory_refinements.count_documents({}) == 1
    # Late retries of the original obligation cannot undo the refinement.
    await routes.submit_privacy_required_range(body.original, SOURCE)
    revision = (await db.capture_sources.find_one({"source_id": SOURCE.source_id}))[
        "privacy_revision"
    ]
    await routes.refine_privacy_required_range(body, SOURCE)
    assert (await db.capture_sources.find_one({"source_id": SOURCE.source_id}))[
        "privacy_revision"
    ] == revision
    assert (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    ) == snapshot.allowed_spans(SOURCE.source_id, LOW, HIGH)


@pytest.mark.parametrize("activated_before_history", [False, True])
async def test_refinement_never_releases_positive_or_missing_evidence(
    db, activated_before_history
):
    await prepare(db)
    if activated_before_history:
        await db.capture_sources.update_one({}, {"$set": {"privacy_enabled_from": LOW}})
    for state, reason, start in [
        ("excluded", None, LOW),
        ("pending", "missing_frame", HIGH - timedelta(seconds=1)),
    ]:
        await db.privacy_screening.insert_one(
            dict(
                user_id="owner",
                source_id=SOURCE.source_id,
                track_id="first" if start == LOW else "second",
                started_at=start,
                ended_at=start + timedelta(seconds=1),
                segments=[
                    dict(
                        started_at=start,
                        ended_at=start + timedelta(seconds=1),
                        state=state,
                        reason=reason,
                    )
                ],
            )
        )
    await routes.refine_privacy_required_range(request(), SOURCE)
    spans = (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    )
    assert spans == [
        (LOW + timedelta(seconds=1), LOW + timedelta(seconds=5)),
        (LOW + timedelta(seconds=10), HIGH - timedelta(seconds=1)),
    ]


async def test_invalidation_failure_stays_held_until_exact_retry(db, monkeypatch):
    await prepare(db)
    dirty = AsyncMock(side_effect=RuntimeError("Synthetic failure"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", dirty)
    with pytest.raises(RuntimeError):
        await routes.refine_privacy_required_range(request(), SOURCE)
    assert (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    ) == []
    assert (
        await db.privacy_required_ranges.count_documents(
            {"superseded_by": {"$exists": True}}
        )
        == 0
    )
    dirty.side_effect = None
    await routes.refine_privacy_required_range(request(), SOURCE)
    assert (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, LOW + timedelta(seconds=5)
    )


@pytest.mark.parametrize(
    "change",
    ["other_source", "other_owner", "shorter", "different_original", "other_provider"],
)
async def test_refinement_cannot_replace_another_obligation(db, change):
    await prepare(db)
    source = SOURCE
    body = request()
    if change == "other_source":
        source = SimpleNamespace(
            user_id="owner", source_id="other", provider="screenpipe"
        )
    elif change == "other_owner":
        source = SimpleNamespace(
            user_id="other", source_id=SOURCE.source_id, provider="screenpipe"
        )
    elif change == "other_provider":
        source = SimpleNamespace(
            user_id="owner", source_id=SOURCE.source_id, provider="mobile"
        )
    elif change == "shorter":
        body.original.ended_at = HIGH - timedelta(seconds=1)
    else:
        body.original.track_ids = ["first"]
    with pytest.raises(routes.HTTPException):
        await routes.refine_privacy_required_range(body, source)
    assert (
        await db.privacy_required_ranges.count_documents(
            {"superseded_by": {"$exists": True}}
        )
        == 0
    )


@pytest.mark.parametrize(
    "change",
    ["naive", "missing_track", "backwards", "duplicate_time", "bad_hash", "future"],
)
def test_inventory_rejects_ambiguous_evidence(change):
    data = request().model_dump()
    if change == "naive":
        data["observations"][0]["observed_at"] = LOW.replace(tzinfo=None)
    elif change == "missing_track":
        data["observations"][1]["track_ids"] = ["first"]
    elif change == "backwards":
        data["observations"][1]["transition_started_at"] = LOW - timedelta(seconds=1)
    elif change == "duplicate_time":
        data["observations"][1]["observed_at"] = LOW
    elif change == "bad_hash":
        data["observations"][0]["evidence_sha256"] = "not-an-evidence-hash"
    else:
        data["observations"][1]["observed_at"] = HIGH + timedelta(seconds=1)
    with pytest.raises(ValueError):
        request(**data)


async def test_failure_after_original_retirement_is_recoverable(db, monkeypatch):
    await prepare(db)
    collection_type = type(db.privacy_required_ranges)
    update = collection_type.update_one

    async def fail_after_retirement(collection, query, changes, *args, **kwargs):
        result = await update(collection, query, changes, *args, **kwargs)
        if (
            collection.name == "privacy_required_ranges"
            and "superseded_by" in changes.get("$set", {})
        ):
            raise RuntimeError("Synthetic interruption after retirement")
        return result

    monkeypatch.setattr(collection_type, "update_one", fail_after_retirement)
    with pytest.raises(RuntimeError):
        await routes.refine_privacy_required_range(request(), SOURCE)
    assert not (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, LOW + timedelta(seconds=1)
    )
    monkeypatch.setattr(collection_type, "update_one", update)
    await routes.refine_privacy_required_range(request(), SOURCE)
    assert (await privacy.load_snapshot("owner")).permits(
        SOURCE.source_id, LOW, LOW + timedelta(seconds=1)
    )


@pytest.mark.parametrize("decision", ["allowed", "excluded"])
async def test_refinement_preserves_independent_user_override(db, decision):
    await prepare(db)
    revision = (await db.capture_sources.find_one({"source_id": SOURCE.source_id}))[
        "privacy_revision"
    ]
    await routes.override_privacy(
        routes.PrivacyOverride(
            source_id=SOURCE.source_id,
            started_at=LOW,
            ended_at=HIGH,
            revision=revision,
            decision=decision,
        ),
        SimpleNamespace(id="owner", user_id="owner"),
    )
    await routes.refine_privacy_required_range(request(), SOURCE)
    spans = (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    )
    assert spans == ([(LOW, HIGH)] if decision == "allowed" else [])


async def test_other_overlapping_obligation_is_not_retired(db):
    await prepare(db)
    await routes.submit_privacy_required_range(
        privacy.PrivacyRequiredRange(
            started_at=LOW, ended_at=HIGH, track_ids=["unverified-third"]
        ),
        SOURCE,
    )
    await routes.refine_privacy_required_range(request(), SOURCE)
    assert (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    ) == []
    assert (
        await db.privacy_required_ranges.count_documents(
            {"track_ids": ["unverified-third"], "superseded_by": {"$exists": False}}
        )
        == 1
    )


async def test_conflicting_replacement_cannot_rewrite_history(db):
    await prepare(db)
    await routes.refine_privacy_required_range(request(), SOURCE)
    body = request()
    body.observations[1].transition_started_at = LOW + timedelta(seconds=6)
    with pytest.raises(routes.HTTPException) as exc:
        await routes.refine_privacy_required_range(body, SOURCE)
    assert exc.value.status_code == 409
    assert await db.privacy_inventory_refinements.count_documents({}) == 1


def test_unobserved_leading_time_is_an_explicit_empty_inventory():
    body = request()
    body.observations[0].observed_at = LOW + timedelta(seconds=2)
    body.observations[0].transition_started_at = LOW + timedelta(seconds=2)
    assert body.periods()[0] == dict(
        started_at=LOW, ended_at=LOW + timedelta(seconds=2), track_ids=[]
    )


async def test_competing_refinement_between_read_and_lock_is_rejected(db, monkeypatch):
    await prepare(db)
    requested = request()
    winner = request()
    winner.observations[1].transition_started_at = LOW + timedelta(seconds=6)
    begin = privacy.begin_update
    intervened = False

    async def interleave(owner, query, update):
        nonlocal intervened
        if not intervened:
            intervened = True
            await routes.refine_privacy_required_range(winner, SOURCE)
        return await begin(owner, query, update)

    monkeypatch.setattr(privacy, "begin_update", interleave)
    with pytest.raises(routes.HTTPException) as exc:
        await routes.refine_privacy_required_range(requested, SOURCE)
    assert exc.value.status_code == 409
    assert await db.privacy_inventory_refinements.count_documents({}) == 1
    source = await db.capture_sources.find_one({"source_id": SOURCE.source_id})
    assert not source["privacy_updating"]
    assert (await privacy.load_snapshot("owner")).allowed_spans(
        SOURCE.source_id, LOW, HIGH
    ) == [
        (LOW, LOW + timedelta(seconds=6)),
        (LOW + timedelta(seconds=10), HIGH),
    ]
