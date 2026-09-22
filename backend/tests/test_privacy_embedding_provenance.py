"""Original capture receipts survive cached vectors and mutable conversation claims."""

import asyncio

import pytest
from test_privacy_background_audio import background, reference  # noqa: F401
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import background_bucket_controller as bucket
from backend.services import privacy
from backend.workers import background_index_jobs as index
from backend.workers import background_suppression_jobs as suppression


async def retarget_and_exclude(evidence):
    await evidence.db.conversations.update_one(
        {"conversation_id": "synthetic-recording"},
        {"$set": {"client_id": "ordinary-device"}},
    )
    await revoke(evidence)


@pytest.mark.parametrize("proof", ["original", "missing", "empty", "corrupt"])
async def test_matching_requires_original_capture_proof(evidence, background, proof):
    await allow(evidence)
    await reference(evidence)
    if proof == "original":
        initial = await bucket.match_embeddings(
            background.user, [[1.0, 0.0]], "background_speech", "synthetic-model"
        )
        assert initial["bucket_size"] == 1
        await retarget_and_exclude(evidence)
    elif proof == "missing":
        await evidence.db.background_clips.update_one(
            {}, {"$unset": {"privacy_reference_receipt": ""}}
        )
    else:
        receipt = [] if proof == "empty" else ["0" * 64]
        await evidence.db.background_clips.update_one(
            {}, {"$set": {"privacy_reference_receipt": receipt}}
        )
    result = await bucket.match_embeddings(
        background.user, [[1.0, 0.0]], "background_speech", "synthetic-model"
    )
    assert result["bucket_size"] == 0
    assert all(row["nearest_exemplar"] is None for row in result["results"])


@pytest.mark.parametrize("producer", ["manual", "corpus_worker"])
async def test_real_vector_producers_pin_original_source(
    evidence, background, producer
):
    await allow(evidence)
    if producer == "manual":
        await bucket.add_background_clip(
            "synthetic-recording", 0, 10, "background_speech", user=background.user
        )
        row = await evidence.db.background_clips.find_one({})
    else:
        result = await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
        assert result["embedded"] == 1
        row = await evidence.db.background_corpus_embeddings.find_one({})
    assert row["privacy_reference_receipt"]
    assert await privacy.ConversationPrivacyFilter().filter_embeddings([row])
    await retarget_and_exclude(evidence)
    assert not await privacy.ConversationPrivacyFilter().filter_embeddings([row])
    journal = await evidence.db.privacy_reference_dependencies.find_one({})
    assert "embedding" not in str(journal) and "Synthetic transcript" not in str(
        journal
    )


@pytest.mark.parametrize("proof", ["missing", "excluded_original"])
async def test_registered_index_rebuilds_unproven_cached_vectors(
    evidence, background, proof
):
    await allow(evidence)
    await reference(
        evidence, collection="background_corpus_embeddings", embedding=[0.0, 1.0]
    )
    if proof == "missing":
        await evidence.db.background_corpus_embeddings.update_one(
            {}, {"$unset": {"privacy_reference_receipt": ""}}
        )
    else:
        await retarget_and_exclude(evidence)
    result = await asyncio.to_thread(
        index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
    )
    assert result["cached"] == 0 and result["embedded"] == 1
    background.client.extract_speaker_embedding.assert_awaited_once()
    row = await evidence.db.background_corpus_embeddings.find_one({})
    assert row["embedding"] == [1.0, 0.0]
    assert await privacy.ConversationPrivacyFilter().filter_embeddings([row])
    assert await evidence.db.background_corpus_embeddings.count_documents({}) == 1


async def test_vector_receipts_are_scoped_and_preserve_first_admission(
    evidence, background
):
    await allow(evidence)
    ordinary = dict(
        evidence.row, conversation_id="ordinary-recording", client_id="ordinary-device"
    )
    await evidence.db.conversations.insert_one(ordinary)
    view = privacy.ConversationPrivacyFilter()
    assert (
        len(
            await view.filter(
                [
                    {"conversation_id": cid}
                    for cid in ["synthetic-recording", "ordinary-recording"]
                ]
            )
        )
        == 2
    )
    receipt = await view.reference_receipt(
        "evidence-owner", conversation_ids=["ordinary-recording"]
    )
    assert len(receipt) == 1
    await retarget_and_exclude(evidence)
    await privacy.require_record(
        {"user_id": "evidence-owner", "privacy_reference_receipt": receipt}
    )
    # Re-reading a retargeted conversation must not rewrite the original admission.
    with pytest.raises(privacy.PrivacyHeld):
        await view.filter([{"conversation_id": "synthetic-recording"}])


async def test_backfill_rejects_target_vector_from_excluded_original(
    evidence, background
):
    await allow(evidence)
    await reference(evidence, collection="background_corpus_embeddings")
    ordinary = dict(
        evidence.row, conversation_id="ordinary-recording", client_id="ordinary-device"
    )
    await evidence.db.conversations.insert_one(ordinary)
    await reference(evidence, cid="ordinary-recording")
    await retarget_and_exclude(evidence)
    result = await suppression.backfill_conversation_suppressions(
        "evidence-owner", "synthetic-recording"
    )
    assert result["skipped"] == "no_indexed_clips"
    background.record.assert_not_awaited()


async def test_cluster_promotions_preserve_cached_receipt(evidence, background):
    await allow(evidence)
    await reference(evidence, collection="background_corpus_embeddings")
    row = await evidence.db.background_corpus_embeddings.find_one({})
    cluster = {"cluster_id": "synthetic-cluster", "member_keys": [row["clip_key"]]}
    await bucket.decide_background_cluster(
        background.user, cluster, "background_speech"
    )
    promoted = await evidence.db.background_clips.find_one({})
    assert promoted["privacy_reference_receipt"] == row["privacy_reference_receipt"]
    await retarget_and_exclude(evidence)
    assert not await privacy.ConversationPrivacyFilter().filter_embeddings([promoted])
