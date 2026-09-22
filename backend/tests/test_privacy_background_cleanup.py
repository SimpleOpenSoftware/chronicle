"""Cleanup proposals must retain the privacy of their reference evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_privacy_background_audio import background, reference  # noqa: F401
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import background_bucket_controller as bucket
from backend.services import privacy
from backend.workers import background_benchmark as benchmark
from backend.workers import background_cleanup_jobs as cleanup


@pytest.fixture
async def proposal(evidence, background, monkeypatch):
    conversation = SimpleNamespace(
        get_pymongo_collection=lambda: evidence.db.conversations
    )
    for module in (cleanup, benchmark):
        monkeypatch.setattr(module, "Conversation", conversation)
    monkeypatch.setattr(cleanup, "get_current_job", lambda: None)
    enqueue = Mock(return_value=SimpleNamespace(id="synthetic-job"))
    monkeypatch.setattr(bucket, "default_queue", SimpleNamespace(enqueue=enqueue))
    original = await evidence.db.conversations.find_one({})
    original.pop("_id")
    original.update(conversation_id="ordinary-recording", client_id="ordinary-device")
    await evidence.db.conversations.insert_one(original)
    await reference(evidence)
    await reference(
        evidence, collection="background_corpus_embeddings", cid="ordinary-recording"
    )
    await evidence.db.background_corpus_embeddings.update_one(
        {}, {"$set": {"segment_index": 0}}
    )
    return SimpleNamespace(enqueue=enqueue)


async def stored_report(evidence, background):
    report = await cleanup.build_background_cleanup_report("evidence-owner")
    assert report["ready"] and report["high_confidence"] == 1
    return await evidence.db.background_cleanup_reports.find_one(
        {"report_id": report["report_id"]}
    )


async def test_private_reference_is_not_used_to_recommend_allowed_transcript_edit(
    evidence, background, proposal
):
    result = await cleanup.build_background_cleanup_report("evidence-owner")
    assert result["ready"] is False
    assert "Synthetic cached text" not in str(result)
    assert await evidence.db.background_cleanup_reports.count_documents({}) == 0


@pytest.mark.parametrize("stage", ["entry", "lookup", "publication", "allowed"])
async def test_registered_cleanup_job_rechecks_reference_policy(
    evidence, background, proposal, monkeypatch, stage
):
    await allow(evidence)
    report = await stored_report(evidence, background)
    if stage == "entry":
        await revoke(evidence)
    cls = type(evidence.db.conversations)
    if stage == "lookup":
        original = cls.find_one

        async def find(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if (
                self.name == "conversations"
                and args
                and args[0].get("conversation_id") == "ordinary-recording"
            ):
                await revoke(evidence)
            return result

        monkeypatch.setattr(cls, "find_one", find)
    if stage == "publication":
        original = cls.update_one

        async def update(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if self.name == "conversations" and "$push" in args[1]:
                await revoke(evidence)
            return result

        monkeypatch.setattr(cls, "update_one", update)
    if stage == "allowed":
        result = await asyncio.to_thread(
            cleanup.apply_background_cleanup_job, "evidence-owner", report["report_id"]
        )
        assert result["conversations_updated"] == 1 and result["segments_changed"] == 1
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                cleanup.apply_background_cleanup_job,
                "evidence-owner",
                report["report_id"],
            )
    doc = await evidence.db.conversations.find_one(
        {"conversation_id": "ordinary-recording"}
    )
    assert len(doc["transcript_versions"]) == (
        2 if stage in {"publication", "allowed"} else 1
    )


@pytest.mark.parametrize("stage", ["stale", "missing", "allowed"])
async def test_cleanup_enqueue_requires_original_report_receipt(
    evidence, background, proposal, stage
):
    await allow(evidence)
    report = await stored_report(evidence, background)
    if stage == "stale":
        await revoke(evidence)
    if stage == "missing":
        await evidence.db.background_cleanup_reports.update_one(
            {}, {"$unset": {"privacy_revisions": ""}}
        )
    if stage == "allowed":
        result = await bucket.enqueue_background_cleanup(
            background.user, report["report_id"]
        )
        assert result["status"] == "queued"
        proposal.enqueue.assert_called_once()
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await bucket.enqueue_background_cleanup(
                background.user, report["report_id"]
            )
        proposal.enqueue.assert_not_called()


async def test_policy_change_during_report_storage_prevents_return_and_enqueue(
    evidence, background, proposal, monkeypatch
):
    await allow(evidence)
    cls = type(evidence.db.background_cleanup_reports)
    original = cls.update_one

    async def update(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if self.name == "background_cleanup_reports":
            await revoke(evidence)
        return result

    monkeypatch.setattr(cls, "update_one", update)
    with pytest.raises(privacy.PrivacyHeld):
        await cleanup.build_background_cleanup_report("evidence-owner")
    report = await evidence.db.background_cleanup_reports.find_one({})
    with pytest.raises(privacy.PrivacyHeld):
        await bucket.enqueue_background_cleanup(background.user, report["report_id"])
    proposal.enqueue.assert_not_called()


async def test_new_report_identity_changes_with_privacy_revision(
    evidence, background, proposal
):
    await allow(evidence)
    first = await stored_report(evidence, background)
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    second = await stored_report(evidence, background)
    assert first["report_id"] != second["report_id"]
    with pytest.raises(privacy.PrivacyHeld):
        await cleanup.require_cleanup_report(first, "evidence-owner")
    await cleanup.require_cleanup_report(second, "evidence-owner")


async def test_background_benchmark_omits_mixed_held_reviews(
    evidence, background, proposal
):
    await reference(evidence, collection="background_corpus_embeddings")
    keys = [
        "ordinary-recording:0.000:10.000:speech",
        "synthetic-recording:0.000:10.000:speech",
    ]
    await evidence.db.background_cluster_reviews.insert_one(
        {
            "requested_by": "evidence-owner",
            "cluster_id": "synthetic-cluster",
            "member_keys": keys,
            "decision": "background_speech",
        }
    )
    report = await benchmark.build_background_benchmark("evidence-owner")
    assert report["reviewed_clusters"] == 0 and report["background_speech_samples"] == 0
    await allow(evidence)
    report = await benchmark.build_background_benchmark("evidence-owner")
    assert report["reviewed_clusters"] == 1 and report["background_speech_samples"] == 2


async def test_editing_held_review_does_not_undo_it_first(
    evidence, background, proposal, monkeypatch
):
    await reference(evidence, collection="background_corpus_embeddings")
    row = await evidence.db.background_cluster_reviews.insert_one(
        {
            "requested_by": "evidence-owner",
            "cluster_id": "synthetic-cluster",
            "member_keys": ["synthetic-recording:0.000:10.000:speech"],
            "decision": "background_speech",
        }
    )
    from unittest.mock import AsyncMock

    undo = AsyncMock()
    monkeypatch.setattr(bucket, "undo_background_decision", undo)
    with pytest.raises(privacy.PrivacyHeld):
        await bucket.edit_background_decision(
            background.user, str(row.inserted_id), "noise"
        )
    undo.assert_not_awaited()
