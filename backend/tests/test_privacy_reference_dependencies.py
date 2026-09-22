"""Saved reference-derived results follow immutable original capture evidence."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401

from backend.controllers import background_suppression_controller as controller
from backend.services import privacy
from backend.services.reference_dependencies import seal
from backend.workers import background_suppression as ledger


async def derived(evidence):
    await allow(evidence)
    visibility = privacy.ConversationPrivacyFilter()
    assert await visibility.filter([{"conversation_id": "synthetic-recording"}])
    receipt = await visibility.reference_receipt("review-owner")
    row = {
        "conversation_id": "synthetic-result",
        "user_id": "review-owner",
        "client_id": "ordinary-device",
        "created_at": START + timedelta(days=1),
        "ended_at": START + timedelta(days=1, seconds=10),
        "privacy_reference_receipt": receipt,
    }
    await evidence.db.conversations.insert_one(deepcopy(row))
    return row


@pytest.mark.parametrize(
    "change", ["excluded", "retargeted", "missing", "corrupt", "wrong_owner"]
)
async def test_saved_reference_holds_after_original_changes(evidence, change):
    row = await derived(evidence)
    await privacy.require_record(row)
    receipt = row["privacy_reference_receipt"]
    if change in {"excluded", "retargeted"}:
        await revoke(evidence)
        if change == "retargeted":
            await evidence.db.conversations.update_one(
                {"conversation_id": "synthetic-recording"},
                {"$set": {"client_id": "ordinary-device"}},
            )
    elif change == "missing":
        await evidence.db.privacy_reference_dependencies.delete_one({"_id": receipt[0]})
    elif change == "corrupt":
        await evidence.db.privacy_reference_dependencies.update_one(
            {"_id": receipt[0]},
            {"$set": {"evidence_records": []}},
        )
    else:
        row["user_id"] = "different-owner"
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)


async def test_bounded_reads_and_nested_derivatives_use_original_owner_time(evidence):
    row = await derived(evidence)
    view = privacy.ConversationPrivacyFilter()
    assert await view.filter([{"conversation_id": row["conversation_id"]}])
    next_row = {
        "user_id": "third-owner",
        "privacy_reference_receipt": await view.reference_receipt("third-owner"),
    }
    await privacy.require_record(next_row)
    await revoke(evidence)
    snapshot = await privacy.load_snapshot(
        "review-owner", row["created_at"], row["ended_at"]
    )
    assert not snapshot.permits_record(row)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(next_row)
    assert not await privacy.filter_conversation_documents([row], "review-owner")
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.guard_payload(
            "review-owner", {"conversation_id": row["conversation_id"]}
        )
    assert "Conversations/synthetic-result.md" in await privacy.quarantined_vault_paths(
        "review-owner"
    )


async def test_unrelated_policy_change_does_not_invalidate_saved_reference(evidence):
    row = await derived(evidence)
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    await privacy.require_record(row)
    assert (
        await seal("review-owner", [evidence.row]) == row["privacy_reference_receipt"]
    )
    assert await evidence.db.privacy_reference_dependencies.count_documents({}) == 1


@pytest.mark.parametrize("change", ["excluded", "deleted"])
async def test_running_derived_read_retains_original_policy_and_journal_fences(
    evidence, change
):
    row = await derived(evidence)
    view = privacy.ConversationPrivacyFilter()
    assert await view.filter([{"conversation_id": row["conversation_id"]}])
    if change == "excluded":
        await revoke(evidence)
    else:
        await evidence.db.privacy_reference_dependencies.delete_many({})
    with pytest.raises(privacy.PrivacyHeld):
        await view.assert_current()


async def test_background_ledger_hides_reference_derived_text_after_later_exclusion(
    evidence, monkeypatch
):
    row = await derived(evidence)
    # The target's own capture is allowed and has no reference-dependent transcript.
    await evidence.db.conversations.update_one(
        {"conversation_id": row["conversation_id"]},
        {"$unset": {"privacy_reference_receipt": ""}},
    )
    model = SimpleNamespace(get_pymongo_collection=lambda: evidence.db.conversations)
    monkeypatch.setattr(ledger, "Conversation", model)
    monkeypatch.setattr(controller, "Conversation", model)
    doc = {
        "user_id": "review-owner",
        "conversation_id": row["conversation_id"],
        "segment_start": 0.0,
        "segment_end": 3.0,
        "status": "confirmed",
        "zone": "confident_background",
        "text": "Synthetic reference-derived text",
        "background_similarity": 0.9,
        "bucket_type": "background_speech",
        "privacy_reference_receipt": row["privacy_reference_receipt"],
    }
    await evidence.db.background_suppressions.insert_one(doc)
    user = SimpleNamespace(user_id="review-owner")
    visible = await controller.get_conversation_suppressions(
        user, row["conversation_id"]
    )
    assert visible["total"] == 1 and "privacy_reference_receipt" not in str(visible)
    await revoke(evidence)
    hidden = await controller.get_conversation_suppressions(
        user, row["conversation_id"]
    )
    assert hidden["total"] == 0 and "Synthetic reference-derived text" not in str(
        hidden
    )
    assert (
        await ledger.load_sticky_segments("review-owner", row["conversation_id"]) == {}
    )


async def test_old_recognition_and_ledger_rows_without_reference_proof_are_held(
    evidence,
):
    for row in [
        {
            "metadata": {
                "speaker_recognition": {
                    "enabled": True,
                    "privacy_gallery_receipt": {
                        "catalog_id": "c" * 32,
                        "gallery_revision": "a" * 64,
                        "operation_ids": [],
                        "user_id": "ordinary-owner",
                    },
                }
            }
        },
        {"segment_start": 0.0, "background_similarity": 0.9},
    ]:
        with pytest.raises(privacy.PrivacyHeld):
            await privacy.require_record({"user_id": "ordinary-owner", **row})


async def test_cached_derivative_receipt_survives_another_processing_hop(evidence):
    row = await derived(evidence)
    await evidence.db.conversations.update_one(
        {"conversation_id": row["conversation_id"]},
        {"$unset": {"privacy_reference_receipt": ""}},
    )
    view = privacy.ConversationPrivacyFilter()
    assert await view.filter([row])
    receipt = await view.reference_receipt("third-owner")
    result = {"user_id": "third-owner", "privacy_reference_receipt": receipt}
    await privacy.require_record(result)
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(result)


async def test_reference_publication_locks_original_owner(evidence):
    import asyncio

    row = await derived(evidence)
    view = privacy.ConversationPrivacyFilter()
    assert await view.filter([{"conversation_id": row["conversation_id"]}])
    entered = asyncio.Event()

    async def change():
        entered.set()
        return await privacy.begin_update(
            "evidence-owner",
            {"source_id": "screenpipe-test"},
            {"$inc": {"privacy_revision": 1}},
        )

    async with view.publication():
        task = asyncio.create_task(change())
        await entered.wait()
        await asyncio.sleep(0)
        assert not task.done()
    await asyncio.wait_for(task, 1)


async def test_mixed_reference_cluster_cannot_silently_review_held_members(
    evidence, monkeypatch
):
    row = await derived(evidence)
    await evidence.db.conversations.update_one(
        {"conversation_id": row["conversation_id"]},
        {"$unset": {"privacy_reference_receipt": ""}},
    )
    monkeypatch.setattr(
        ledger,
        "Conversation",
        SimpleNamespace(get_pymongo_collection=lambda: evidence.db.conversations),
    )
    base = {
        "user_id": "review-owner",
        "conversation_id": row["conversation_id"],
        "segment_end": 3.0,
        "status": "queued",
        "zone": "unsure",
        "cluster_signature": "synthetic-cluster",
        "background_similarity": 0.9,
        "bucket_type": "background_speech",
    }
    await evidence.db.background_suppressions.insert_many(
        [
            dict(base, segment_start=0.0, privacy_reference_receipt=[]),
            dict(
                base,
                segment_start=1.0,
                privacy_reference_receipt=row["privacy_reference_receipt"],
            ),
        ]
    )
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await controller.decide_suppression_cluster(
            SimpleNamespace(user_id="review-owner"),
            row["conversation_id"],
            "synthetic-cluster",
            "restore",
        )
    assert (
        await evidence.db.background_suppressions.count_documents({"status": "queued"})
        == 2
    )
    assert await evidence.db.media_role_overrides.count_documents({}) == 0
