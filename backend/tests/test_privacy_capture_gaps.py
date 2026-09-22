"""Capture gaps are distinct from inspected samples and transient model failures."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_privacy_entrypoints import START, db, result  # noqa: F401
from test_privacy_screening_revisions import client  # noqa: F401

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy


def gap_result(**changes):
    payload = result("allowed").model_dump()
    payload["segments"][0].update(
        state="pending", coverage="unverified", reason="capture_gap", **changes
    )
    return privacy.ScreeningResult.model_validate(payload)


async def test_collector_gap_submission_is_held_and_explained_without_retry_claim(db):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    body = gap_result()
    await routes.submit_screening(body, source)
    await routes.submit_screening(body, source)
    snapshot = await privacy.load_snapshot("owner")
    for suffix in ["", ":input:microphone", ":output:system"]:
        assert not snapshot.permits(
            source.source_id + suffix, START, START + timedelta(seconds=10)
        )
    assert snapshot.permits("unrelated-source", START, START + timedelta(seconds=10))
    rows = await routes.privacy_intervals(
        START,
        START + timedelta(seconds=10),
        SimpleNamespace(id="owner", user_id="owner"),
    )
    assert (
        rows["intervals"][0]["reason"]
        == "Screen captures are too far apart to establish coverage; screen and audio processing remain held."
    )
    assert (
        await db.privacy_screening.count_documents({"interval_id": body.interval_id})
        == 1
    )


@pytest.mark.parametrize(
    "state,coverage",
    [("allowed", "unverified"), ("pending", "verified"), ("pending", "sampled")],
)
def test_capture_gap_cannot_claim_verified_or_allowed_coverage(state, coverage):
    payload = result("allowed").model_dump()
    payload["segments"][0].update(state=state, coverage=coverage, reason="capture_gap")
    with pytest.raises(ValueError):
        privacy.ScreeningResult.model_validate(payload)


def long_gap_payload():
    payload = result("allowed").model_dump(mode="json")
    low = START + timedelta(seconds=1)
    high = START + timedelta(hours=36)
    payload["ended_at"] = high.isoformat()
    payload["segments"] = [
        dict(
            started_at=START.isoformat(),
            ended_at=low.isoformat(),
            state="allowed",
            coverage="verified",
        ),
        dict(
            started_at=low.isoformat(),
            ended_at=high.isoformat(),
            state="pending",
            coverage="unverified",
            reason="capture_gap",
        ),
    ]
    payload["evidence"] += [
        {**payload["evidence"][0], "frame_id": 2, "captured_at": low.isoformat()},
        {**payload["evidence"][0], "frame_id": 3, "captured_at": high.isoformat()},
    ]
    return payload


@pytest.mark.parametrize("reason", ["capture_gap", "missing_frame", "screening_failed"])
async def test_http_accepts_long_recorder_outage_without_authorizing_gap(
    db, client, reason
):
    payload = long_gap_payload()
    payload["segments"][1]["reason"] = reason
    for _ in range(2):
        response = await client.post("/device-input/screening", json=payload)
        assert response.status_code == 200
    assert await db.privacy_screening.count_documents({}) == 1
    snapshot = await privacy.load_snapshot("owner")
    low, high = START + timedelta(seconds=1), START + timedelta(hours=36)
    for suffix in ["", ":input:microphone", ":output:system"]:
        source = "screenpipe-test" + suffix
        assert snapshot.allowed_spans(source, START, high) == [(START, low)]
        assert not snapshot.permits(source, low, high)
    assert snapshot.permits("unrelated-source", low, high)
    rows = await routes.privacy_intervals(
        low, high, SimpleNamespace(id="owner", user_id="owner")
    )
    if reason == "capture_gap":
        assert any(
            "too far apart" in row.get("reason", "") for row in rows["intervals"]
        )
    else:
        assert any(row.get("state") == "pending" for row in rows["intervals"])


@pytest.mark.parametrize(
    "state,coverage,reason",
    [
        ("allowed", "sampled", None),
        ("excluded", "verified", None),
        ("pending", "sampled", None),
        ("pending", "verified", None),
        ("allowed", "unverified", "capture_gap"),
        ("pending", "verified", "capture_gap"),
    ],
)
async def test_long_interval_exception_applies_only_to_unverified_holds(
    client, state, coverage, reason
):
    payload = long_gap_payload()
    payload["segments"][1].update(state=state, coverage=coverage, reason=reason)
    assert (
        await client.post("/device-input/screening", json=payload)
    ).status_code == 422
