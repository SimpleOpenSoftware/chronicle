import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401
from test_privacy_enrollment_operations import enrollment_setup  # noqa: F401
from test_privacy_gallery_reads import gallery  # noqa: F401

from backend.services import privacy
from backend.services.speaker_gallery_privacy import result_scope


async def derived(evidence, gallery):
    result = await gallery.client.get_enrolled_speakers(user_id="review-admin")
    receipt = result_scope(result).receipt()
    row = {
        "conversation_id": "synthetic-derived",
        "user_id": "review-admin",
        "client_id": "ordinary-other-device",
        "created_at": START + timedelta(days=1),
        "ended_at": START + timedelta(days=1, seconds=10),
        "transcript_versions": [
            {
                "metadata": {
                    "speaker_recognition": {
                        "enabled": True,
                        "privacy_gallery_receipt": receipt,
                        "privacy_reference_receipt": [],
                    }
                }
            }
        ],
    }
    await evidence.db.conversations.insert_one(deepcopy(row))
    return row, receipt


@pytest.mark.asyncio
async def test_explicitly_quarantined_enrollment_holds_saved_projection_on_other_device(
    evidence, gallery
):
    row, receipt = await derived(evidence, gallery)
    await privacy.require_record(row)
    visibility = privacy.ConversationPrivacyFilter()
    assert await visibility.filter([{"conversation_id": row["conversation_id"]}])
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    # Rebuilding the current enrollment conversation cannot release old audio.
    await evidence.db.conversations.update_one(
        {"conversation_id": "synthetic-recording"},
        {"$set": {"client_id": "ordinary-device"}},
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)
    assert (
        await privacy.ConversationPrivacyFilter().filter(
            [{"conversation_id": row["conversation_id"]}]
        )
        == []
    )
    assert (
        await privacy.filter_conversation_documents(
            [{"conversation_id": row["conversation_id"]}], "review-admin"
        )
        == []
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.guard_payload(
            "review-admin", {"conversation_id": row["conversation_id"]}
        )
    assert (
        "Conversations/synthetic-derived.md"
        in await privacy.quarantined_vault_paths("review-admin")
    )
    ordinary = {k: v for k, v in row.items() if k != "transcript_versions"}
    await privacy.require_record(ordinary)


@pytest.mark.asyncio
async def test_bounded_read_checks_enrollment_asset_state(evidence, gallery):
    row, _ = await derived(evidence, gallery)
    snapshot = await privacy.load_snapshot(
        "review-admin", row["created_at"], row["ended_at"]
    )
    assert snapshot.permits_record(row)
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    snapshot = await privacy.load_snapshot(
        "review-admin", row["created_at"], row["ended_at"]
    )
    assert not snapshot.permits_record(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["quarantined", "quarantine_pending", "deleted", "corrupt"]
)
async def test_durable_receipt_cannot_outlive_missing_or_invalid_journal(
    evidence, gallery, change
):
    row, receipt = await derived(evidence, gallery)
    identifier = receipt["operation_ids"][0]
    snapshot = await privacy.load_snapshot("review-admin")
    assert snapshot.permits_record(row)
    query = {"_id": identifier}
    if change == "deleted":
        await evidence.db.speaker_enrollment_operations.delete_one(query)
    elif change == "corrupt":
        await evidence.db.speaker_enrollment_operations.update_one(
            query, {"$set": {"user_id": "wrong-owner"}}
        )
    else:
        await evidence.db.speaker_enrollment_operations.update_one(
            query, {"$set": {"state": change}}
        )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("review-admin", snapshot)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)


@pytest.mark.asyncio
async def test_old_mixed_recognition_requires_rebuild_even_without_own_screen_source(
    evidence,
):
    row = {
        "user_id": "ordinary-owner",
        "metadata": {"speaker_recognition": {"enabled": True}},
    }
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)
    row["metadata"]["speaker_recognition"]["enabled"] = False
    await privacy.require_record(row)


@pytest.mark.asyncio
async def test_diarization_artifact_and_wrong_owner_receipts_are_checked(
    evidence, gallery
):
    row, receipt = await derived(evidence, gallery)
    artifact = {
        "user_id": "review-admin",
        "configuration": {
            "privacy_gallery_receipt": receipt,
            "privacy_reference_receipt": [],
        },
    }
    await privacy.require_record(artifact)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record({**artifact, "user_id": "other-owner"})
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(artifact)


@pytest.mark.asyncio
async def test_publication_does_not_lock_descriptive_source_owner(evidence, gallery):
    row, _ = await derived(evidence, gallery)
    visibility = privacy.ConversationPrivacyFilter()
    assert await visibility.filter([{"conversation_id": row["conversation_id"]}])
    entered = asyncio.Event()

    async def change_policy():
        entered.set()
        return await privacy.begin_update(
            "evidence-owner",
            {"source_id": "screenpipe-test"},
            {"$inc": {"privacy_revision": 1}},
        )

    async with visibility.publication():
        changing = asyncio.create_task(change_policy())
        await entered.wait()
        await asyncio.sleep(0)
        await asyncio.wait_for(changing, 1)
    await asyncio.wait_for(changing, 1)


@pytest.mark.asyncio
async def test_registered_speaker_entry_holds_before_transcript_or_provider_read(
    evidence, monkeypatch
):
    from backend.workers import speaker_jobs

    row = await evidence.db.conversations.find_one(
        {"conversation_id": "synthetic-recording"}
    )
    conversation = SimpleNamespace(**row)
    conversation.get_transcript_version = lambda *_: pytest.fail(
        "Must hold before transcript read"
    )
    monkeypatch.setattr(
        speaker_jobs,
        "Conversation",
        SimpleNamespace(
            conversation_id=object(), find_one=AsyncMock(return_value=conversation)
        ),
    )
    with pytest.raises(privacy.PrivacyHeld):
        await speaker_jobs.recognise_speakers_job.__wrapped__(
            "synthetic-recording", "version"
        )


@pytest.mark.asyncio
async def test_source_metadata_is_not_a_recursive_recognition_dependency(
    evidence, gallery
):
    from backend.services.speaker_enrollment import _evidence_hash

    row, receipt = await derived(evidence, gallery)
    op = await evidence.db.speaker_enrollment_operations.find_one({})
    original = deepcopy(op["evidence_records"][0])
    original.update(
        user_id="review-admin",
        client_id="ordinary-device",
        metadata={
            "speaker_recognition": {"enabled": True, "privacy_gallery_receipt": receipt}
        },
    )
    await evidence.db.speaker_enrollment_operations.update_one(
        {"_id": op["_id"]},
        {
            "$set": {
                "evidence_records": [original],
                "binding.evidence.capture_hash": _evidence_hash([original]),
            }
        },
    )
    await privacy.require_record(row)


@pytest.mark.asyncio
async def test_saved_private_recognition_does_not_reach_detail_response(
    evidence, gallery, monkeypatch
):
    from backend.controllers import conversation_controller

    row, _ = await derived(evidence, gallery)
    conversation = SimpleNamespace(
        **row,
        memory_space_id=None,
        published_to_main_at=None,
        model_dump=lambda: deepcopy(row)
    )
    monkeypatch.setattr(
        conversation_controller,
        "Conversation",
        SimpleNamespace(
            conversation_id=object(), find_one=AsyncMock(return_value=conversation)
        ),
    )
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    response = await conversation_controller.get_conversation(
        row["conversation_id"],
        SimpleNamespace(user_id="review-admin", is_superuser=False),
        dataset=True,
    )
    assert response.status_code == 423
    assert b"privacy_gallery_receipt" not in response.body
    assert b"synthetic-derived" not in response.body
