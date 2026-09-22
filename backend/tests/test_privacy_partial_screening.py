"""Real ingest preserves failure holds and exact coverage through recovery."""

from datetime import timedelta

import pytest
from test_privacy_entrypoints import START, db, result  # noqa: F401
from test_privacy_screening_revisions import client  # noqa: F401

from backend.services import privacy


def partial():
    body = result("allowed").model_dump(mode="json", exclude_none=True)
    at = lambda n: (START + timedelta(seconds=n)).isoformat()
    body.update(interval_id="partial", policy_version="screen-privacy-v3")
    body["segments"] = [
        {
            "started_at": at(0),
            "ended_at": at(3),
            "state": "pending",
            "coverage": "unverified",
            "reason": "missing_frame",
        },
        {
            "started_at": at(3),
            "ended_at": at(10),
            "state": "allowed",
            "coverage": "verified",
        },
    ]
    body["evidence"] = [
        {
            "frame_id": 1,
            "captured_at": at(0),
            "state": "pending",
            "reason": "missing_frame",
        },
        {
            "frame_id": 2,
            "captured_at": at(3),
            "state": "allowed",
            "score": 0.0,
            "input_hash": "a" * 64,
        },
        {
            "frame_id": 3,
            "captured_at": at(10),
            "state": "allowed",
            "score": 0.0,
            "input_hash": "b" * 64,
        },
    ]
    return body


async def test_partial_ingest_holds_both_audio_tracks_and_recovery_requires_replacement(
    db, client
):
    body = partial()
    for _ in range(2):
        assert (
            await client.post("/device-input/screening", json=body)
        ).status_code == 200
    snapshot = await privacy.load_snapshot("owner")
    for track in ["input:microphone", "output:system"]:
        assert snapshot.allowed_spans(
            "screenpipe-test:" + track, START, START + timedelta(seconds=10)
        ) == [(START + timedelta(seconds=3), START + timedelta(seconds=10))]
    assert snapshot.permits("another-device", START, START + timedelta(seconds=10))
    spans = await privacy.list_intervals("owner", START, START + timedelta(seconds=10))
    assert (
        len(spans) == 1
        and spans[0]["state"] == "pending"
        and "original frame" in spans[0]["reason"]
    )
    assert not any(key in spans[0] for key in ["evidence", "input_hash", "score"])
    final = result("allowed").model_dump(mode="json", exclude_none=True)
    final["interval_id"] = "recovered"
    assert (await client.post("/device-input/screening", json=final)).status_code == 200
    assert not (await privacy.load_snapshot("owner")).permits(
        "screenpipe-test", START, START + timedelta(seconds=10)
    )
    replacement = {
        "previous_interval_ids": ["partial"],
        "replacement_interval_id": "recovered",
    }
    for _ in range(2):
        assert (
            await client.post("/device-input/screening/replace", json=replacement)
        ).status_code == 200
    assert (await privacy.load_snapshot("owner")).permits(
        "screenpipe-test", START, START + timedelta(seconds=10)
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", snapshot)
    assert await db.privacy_screening.count_documents({}) == 2


@pytest.mark.parametrize(
    "change",
    [
        "allowed_failure",
        "invented_hash",
        "invented_score",
        "allowed_segment",
        "verified_segment",
        "prediction_without_identity",
    ],
)
async def test_ingest_rejects_failure_disguised_as_clearance(db, client, change):
    body = partial()
    if change == "allowed_failure":
        body["evidence"][0]["state"] = "allowed"
    elif change == "invented_hash":
        body["evidence"][0]["input_hash"] = "a" * 64
    elif change == "invented_score":
        body["evidence"][0]["score"] = 0.0
    elif change == "allowed_segment":
        body["segments"][0].update(state="allowed", coverage="sampled", reason=None)
    elif change == "verified_segment":
        body["segments"][0].update(coverage="verified", reason=None)
    else:
        body["evidence"][1].pop("input_hash")
    response = await client.post("/device-input/screening", json=body)
    assert response.status_code == 422
    assert await db.privacy_screening.count_documents({}) == 0
