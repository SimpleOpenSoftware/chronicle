"""Saved enrollments survive source lifecycle changes; incoming audio stays guarded."""

import pytest
from test_privacy_enrollment import allow, evidence, revoke
from test_privacy_enrollment_operations import enroll, enrollment_setup
from test_privacy_gallery_dependencies import derived
from test_privacy_gallery_reads import gallery

from backend.services import privacy, speaker_enrollment


@pytest.mark.asyncio
async def test_saved_clip_survives_source_deletion_and_exclusion(evidence, gallery):
    row, _ = await derived(evidence, gallery)
    await revoke(evidence)
    await evidence.db.conversations.delete_one(
        {"conversation_id": "synthetic-recording"}
    )
    result = await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert result["speakers"]
    await privacy.require_record(row)


@pytest.mark.asyncio
async def test_recovery_keeps_completed_enrollment_without_source(
    evidence, enrollment_setup, monkeypatch
):
    await allow(evidence)
    await enroll(enrollment_setup)
    await revoke(evidence)
    await evidence.db.conversations.delete_one(
        {"conversation_id": "synthetic-recording"}
    )
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    assert (await speaker_enrollment.recover_speaker_enrollments())["quarantined"] == 0


@pytest.mark.asyncio
async def test_direct_clip_enrollment_has_optional_source_metadata(
    evidence, enrollment_setup
):
    result = await enrollment_setup.client.enroll_new_speaker(
        "Synthetic speaker", b"synthetic audio", "review-admin"
    )
    assert result["status"] == "enrolled"
    row = await evidence.db.speaker_enrollment_operations.find_one({})
    assert row["conversation_ids"] == []
    assert row["binding"]["evidence"] is None
