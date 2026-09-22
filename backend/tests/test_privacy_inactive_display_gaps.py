"""Recorded display absence must not become an unrelated capture outage."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_privacy_inventory_refinement import db  # noqa: F401
from test_privacy_inventory_refinement import HIGH, LOW, SOURCE, prepare, request

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy


def gap_result(reason="missing_frame", *, track="first", start=LOW, duration=80):
    end = start + timedelta(seconds=duration)
    first = dict(frame_id=1, captured_at=start, state="pending", reason=reason)
    if reason == "capture_gap":
        first.update(state="allowed", reason="none", score=0.01, input_hash="a" * 64)
    return privacy.ScreeningResult(
        interval_id="synthetic-disconnected-display-gap",
        track_id=track,
        started_at=start,
        ended_at=end,
        model_version="synthetic-model",
        policy_version="synthetic-policy",
        segments=[
            dict(
                started_at=start,
                ended_at=end,
                state="pending",
                coverage="unverified",
                reason=reason,
            )
        ],
        evidence=[
            first,
            dict(
                frame_id=2,
                captured_at=end,
                state="allowed",
                score=0.01,
                input_hash="b" * 64,
                reason="none",
            ),
        ],
    )


@pytest.mark.parametrize("reason", ["missing_frame", "screening_failed", "capture_gap"])
@pytest.mark.parametrize("duration", [20, 80])
async def test_late_gap_preserves_screened_time_when_display_is_recorded_absent(
    db,
    reason,
    duration,
):
    await prepare(db)
    await routes.refine_privacy_required_range(request(), SOURCE)
    before = await privacy.load_snapshot("owner")
    assert before.allowed_spans(SOURCE.source_id, LOW, HIGH) == [
        (LOW, LOW + timedelta(seconds=5)),
        (LOW + timedelta(seconds=10), HIGH),
    ]
    await routes.submit_screening(
        gap_result(reason, duration=duration),
        SOURCE,
    )
    snapshot = await privacy.load_snapshot("owner")
    assert not snapshot.source(SOURCE.source_id).get("privacy_updating")
    for suffix in ("", ":input:microphone", ":output:system"):
        assert snapshot.allowed_spans(SOURCE.source_id + suffix, LOW, HIGH) == [
            (LOW + timedelta(seconds=10), HIGH),
        ]
    assert snapshot.permits("another-device", LOW, HIGH)
    assert await privacy.capture_screening_spans(
        "owner",
        SOURCE.source_id + ":input:microphone",
        LOW + timedelta(seconds=10),
        HIGH,
    ) == [(LOW + timedelta(seconds=10), HIGH)]
    listing = await routes.privacy_intervals(
        LOW, HIGH, SimpleNamespace(user_id="owner")
    )
    assert all(
        row["ended_at"] <= LOW + timedelta(seconds=10) for row in listing["intervals"]
    )


@pytest.mark.parametrize(
    "case",
    [
        "no_refinement",
        "active_display",
        "unrecorded_display",
        "missing_left",
        "missing_right",
        "intermediate_frame",
        "contradicting_capture",
        "missing_required_screen",
        "unproven_inventory",
    ],
)
async def test_inventory_cannot_clear_unproven_or_conflicting_gap(db, case):
    await prepare(db)
    if case != "no_refinement":
        await routes.refine_privacy_required_range(request(), SOURCE)
    body = gap_result()
    if case == "active_display":
        body.track_id = "second"
    elif case == "unrecorded_display":
        body.track_id = "unrecorded-display"
    elif case == "missing_left":
        body.evidence = body.evidence[1:]
    elif case == "missing_right":
        body.evidence = body.evidence[:1]
    elif case == "intermediate_frame":
        body.evidence.insert(
            1,
            privacy.ScreeningEvidence(
                frame_id=3,
                captured_at=LOW + timedelta(seconds=15),
                state="pending",
                reason="missing_frame",
            ),
        )
    elif case == "contradicting_capture":
        body = gap_result(start=LOW + timedelta(seconds=12))
    elif case == "missing_required_screen":
        await db.privacy_screening.delete_many({"track_id": "second"})
    elif case == "unproven_inventory":
        await db.privacy_required_ranges.update_many(
            {"superseded_by": {"$exists": False}},
            {"$set": {"coverage": "historical_observed_displays"}},
        )
    body = privacy.ScreeningResult.model_validate(body.model_dump())
    await routes.submit_screening(body, SOURCE)
    snapshot = await privacy.load_snapshot("owner")
    assert not snapshot.permits(SOURCE.source_id, LOW + timedelta(seconds=15), HIGH)
    # A narrower read must not reinterpret contradictory capture evidence as safe.
    assert not snapshot.permits(
        SOURCE.source_id + ":output:system",
        LOW + timedelta(seconds=16),
        LOW + timedelta(seconds=17),
    )


@pytest.mark.parametrize("state", ["excluded", "needs_review"])
async def test_recorded_absence_cannot_dismiss_actual_private_evidence(db, state):
    await prepare(db)
    await routes.refine_privacy_required_range(request(), SOURCE)
    body = gap_result().model_dump()
    body["segments"][0].update(state=state, coverage="verified", reason=None)
    for evidence in body["evidence"]:
        evidence.update(
            state=state, reason="nsfw_content", score=0.9, input_hash="c" * 64
        )
    await routes.submit_screening(privacy.ScreeningResult(**body), SOURCE)
    snapshot = await privacy.load_snapshot("owner")
    assert not snapshot.permits(SOURCE.source_id, LOW + timedelta(seconds=10), HIGH)


async def test_listing_reports_active_gap_instead_of_absent_display_failure(db):
    await prepare(db)
    await routes.refine_privacy_required_range(request(), SOURCE)
    await routes.submit_screening(gap_result(), SOURCE)
    active = gap_result("capture_gap", track="second").model_dump()
    active["interval_id"] = "synthetic-active-display-gap"
    await routes.submit_screening(privacy.ScreeningResult(**active), SOURCE)
    listing = await routes.privacy_intervals(
        LOW + timedelta(seconds=10),
        HIGH,
        SimpleNamespace(user_id="owner"),
    )
    assert listing["intervals"]
    assert all("too far apart" in row["reason"] for row in listing["intervals"])


async def test_live_gap_uses_recorded_disconnect_and_preserves_transition_hold(db):
    from test_privacy_entrypoints import START

    await routes.submit_privacy_displays(
        privacy.PrivacyDisplaySet(
            observed_at=START + timedelta(seconds=10),
            transition_started_at=START + timedelta(seconds=5),
            track_ids=["live-second"],
        ),
        SOURCE,
    )
    await db.privacy_screening.insert_one(
        dict(
            user_id="owner",
            source_id=SOURCE.source_id,
            track_id="live-second",
            started_at=START + timedelta(seconds=10),
            ended_at=START + timedelta(seconds=20),
            segments=[
                dict(
                    started_at=START + timedelta(seconds=10),
                    ended_at=START + timedelta(seconds=20),
                    state="allowed",
                )
            ],
        )
    )
    await routes.submit_screening(gap_result(track="display", start=START), SOURCE)
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.allowed_spans(
        SOURCE.source_id, START, START + timedelta(seconds=20)
    ) == [
        (START + timedelta(seconds=10), START + timedelta(seconds=20)),
    ]
    assert not snapshot.permits(
        SOURCE.source_id,
        START + timedelta(seconds=6),
        START + timedelta(seconds=7),
    )


async def test_daily_listing_does_not_rescan_disjoint_capture_segments(db, monkeypatch):
    from test_privacy_entrypoints import START

    count = 1000
    await db.privacy_screening.insert_many(
        [
            dict(
                user_id="owner",
                source_id=SOURCE.source_id,
                track_id="display",
                started_at=START + timedelta(seconds=i),
                ended_at=START + timedelta(seconds=i + 1),
                segments=[
                    dict(
                        started_at=START + timedelta(seconds=i),
                        ended_at=START + timedelta(seconds=i + 1),
                        state="excluded",
                        coverage="verified",
                    )
                ],
            )
            for i in range(count)
        ]
    )
    examined = 0
    original = privacy.PrivacySnapshot._applicable_segments

    def measured(segments, *args):
        nonlocal examined
        examined += len(segments)
        return original(segments, *args)

    monkeypatch.setattr(
        privacy.PrivacySnapshot, "_applicable_segments", staticmethod(measured)
    )
    result = await routes.privacy_intervals(
        START,
        START + timedelta(seconds=count),
        SimpleNamespace(user_id="owner"),
    )
    assert result["intervals"]
    assert all(row["state"] == "excluded" for row in result["intervals"])
    assert examined <= count * 4, "Day listing repeatedly scanned unrelated captures"
