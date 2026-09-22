"""Background audio entry points exclude held captures and cached exemplars."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import background_bucket_controller as bucket
from backend.models import job as job_model
from backend.services import privacy
from backend.workers import background_index_jobs as index
from backend.workers import background_suppression_jobs as suppression


@pytest.fixture
async def background(evidence, monkeypatch):
    doc = {
        "title": "Synthetic title",
        "audio_chunks_count": 1,
        "audio_total_duration": 10,
        "active_transcript_version": "synthetic-version",
        "transcript_versions": [
            {
                "version_id": "synthetic-version",
                "segments": [
                    {
                        "start": 0,
                        "end": 10,
                        "text": "Synthetic transcript",
                        "speaker": "Speaker 1",
                    }
                ],
            }
        ],
    }
    await evidence.db.conversations.update_one({}, {"$set": doc})
    conversation = SimpleNamespace(
        get_pymongo_collection=lambda: evidence.db.conversations
    )
    for module in (bucket, index, suppression):
        monkeypatch.setattr(module, "Conversation", conversation)
    client = SimpleNamespace(
        enabled=True,
        get_embedding_info=AsyncMock(
            return_value={"embedding_model": "synthetic-model"}
        ),
        extract_speaker_embedding=AsyncMock(
            return_value={"embedding": [1.0, 0.0], "embedding_model": "synthetic-model"}
        ),
    )
    reconstruct = AsyncMock(return_value=b"synthetic audio")
    for module in (bucket, index):
        monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: client)
        monkeypatch.setattr(module, "reconstruct_audio_segment", reconstruct)
    monkeypatch.setattr(bucket, "_wav_snr_db", lambda _: 0.0)
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(index, "get_current_job", lambda: None)
    record = AsyncMock(return_value=1)
    monkeypatch.setattr(
        suppression.background_suppression, "record_conversation_suppressions", record
    )
    monkeypatch.setattr(
        suppression.background_suppression,
        "get_subject_override",
        AsyncMock(return_value=None),
    )

    async def revoke_call(*args, **kwargs):
        await revoke(evidence)
        return b"synthetic audio"

    return SimpleNamespace(
        client=client,
        reconstruct=reconstruct,
        record=record,
        revoke=revoke_call,
        user=SimpleNamespace(user_id="evidence-owner", id="evidence-owner"),
    )


async def reference(
    evidence,
    *,
    collection="background_clips",
    cid="synthetic-recording",
    embedding=None,
):
    from backend.services.reference_dependencies import seal

    original = await evidence.db.conversations.find_one(
        {"conversation_id": cid}, privacy._RECORD_PROJECTION
    )
    receipt = await seal("evidence-owner", [original]) if original else []
    await evidence.db[collection].insert_one(
        {
            "user_id": "evidence-owner",
            "requested_by": "evidence-owner",
            "conversation_id": cid,
            "segment_start": 0,
            "segment_end": 10,
            "start": 0,
            "end": 10,
            "clip_key": cid + ":0.000:10.000:speech",
            "privacy_reference_receipt": receipt,
            "embedding": embedding or [1.0, 0.0],
            "embedding_model": "synthetic-model",
            "bucket_type": "background_speech",
            "candidate_type": "background_speech",
            "text": "Synthetic cached text",
        }
    )


@pytest.mark.parametrize(
    "entry",
    [
        "add_background_clip",
        "suggest_background_candidates",
        "scan_background_candidates",
    ],
)
@pytest.mark.parametrize("stage", ["entry", "decode", "response", "allowed"])
async def test_background_entrypoints_check_before_embedding(
    evidence, background, entry, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "decode":
        background.reconstruct.side_effect = background.revoke
    if stage == "response":

        async def response(*args, **kwargs):
            await revoke(evidence)
            return {"embedding": [1.0, 0.0], "embedding_model": "synthetic-model"}

        background.client.extract_speaker_embedding.side_effect = response
    if entry == "add_background_clip":
        call = lambda: bucket.add_background_clip(
            "synthetic-recording", 0, 10, "background_speech", user=background.user
        )
    elif entry == "suggest_background_candidates":
        call = lambda: bucket.suggest_background_candidates(
            background.user, "synthetic-recording"
        )
    else:
        call = lambda: bucket.scan_background_candidates(background.user)
    if stage == "allowed":
        result = await call()
        assert result is not None
        background.client.extract_speaker_embedding.assert_awaited_once()
    elif stage == "entry" and entry == "scan_background_candidates":
        result = await call()
        assert result["candidates"] == []
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await call()
    if stage in {"entry", "decode"}:
        background.client.extract_speaker_embedding.assert_not_awaited()
    if stage == "entry":
        background.reconstruct.assert_not_awaited()
    if stage != "allowed":
        assert await evidence.db.background_clips.count_documents({}) == 0


async def test_cached_bucket_filters_canonical_owner_and_missing_provenance(
    evidence, background
):
    await reference(evidence)
    await reference(evidence, cid="missing-recording")
    await evidence.db.conversations.insert_one(
        dict(
            evidence.row,
            conversation_id="ordinary-recording",
            client_id="ordinary-device",
        )
    )
    await reference(evidence, cid="ordinary-recording", embedding=[0.0, 1.0])
    result = await bucket.match_embeddings(
        background.user, [[1.0, 0.0]], "background_speech", "synthetic-model"
    )
    assert result["bucket_size"] == 1
    assert result["results"][0]["bucket_similarity"] == 0
    assert (
        result["results"][0]["nearest_exemplar"]["conversation_id"]
        == "ordinary-recording"
    )


@pytest.mark.parametrize(
    "entry", ["match_embeddings", "backfill_conversation_suppressions"]
)
async def test_cached_reference_revision_change_is_not_swallowed(
    evidence, background, monkeypatch, entry
):
    await allow(evidence)
    await reference(evidence)
    await reference(evidence, collection="background_corpus_embeddings")
    original = privacy.ConversationPrivacyFilter.filter

    async def filtered(self, rows):
        result = await original(self, rows)
        if any("embedding" in row for row in rows):
            await revoke(evidence)
        return result

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "filter", filtered)
    with pytest.raises(privacy.PrivacyHeld):
        if entry == "match_embeddings":
            await bucket.match_embeddings(
                background.user, [[1.0, 0.0]], "background_speech", "synthetic-model"
            )
        else:
            await suppression.backfill_conversation_suppressions(
                "evidence-owner", "synthetic-recording"
            )
    background.record.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
async def test_suppression_backfill_and_seed_report_holds(
    evidence, background, allowed
):
    if allowed:
        await allow(evidence)
    await reference(evidence)
    await reference(evidence, collection="background_corpus_embeddings")
    result = await suppression.backfill_all_suppressions("evidence-owner")
    assert result["privacy_held"] == int(not allowed)
    assert result["written"] == int(allowed)
    await evidence.db.annotations.insert_one(
        {
            "user_id": "evidence-owner",
            "conversation_id": "synthetic-recording",
            "annotation_type": "diarization",
            "corrected_speaker": "Noise",
            "status": "accepted",
            "segment_start_time": 1,
        }
    )
    seeded = await bucket.seed_from_annotations(background.user)
    assert seeded["privacy_held"] == int(not allowed)
    assert seeded["added"] == int(allowed)


@pytest.mark.parametrize(
    "stage", ["entry", "decode", "response", "cache_write", "allowed"]
)
async def test_registered_corpus_worker_gates_audio_and_publication(
    evidence, background, monkeypatch, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "decode":
        background.reconstruct.side_effect = background.revoke
    if stage == "response":

        async def response(*args, **kwargs):
            await revoke(evidence)
            return {"embedding": [1.0, 0.0], "embedding_model": "synthetic-model"}

        background.client.extract_speaker_embedding.side_effect = response
    if stage == "cache_write":
        collection_type = type(evidence.db.background_corpus_embeddings)
        original = collection_type.update_one

        async def write(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if self.name == "background_corpus_embeddings":
                await revoke(evidence)
            return result

        monkeypatch.setattr(collection_type, "update_one", write)
    if stage in {"entry", "allowed"}:
        result = await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
        assert result["embedded"] == int(stage == "allowed")
        assert result["total"] == int(stage == "allowed")
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                index.index_background_corpus_job,
                "evidence-owner",
                "synthetic-revision",
            )
        assert await evidence.db.background_index_runs.count_documents({}) == 0
    if stage in {"entry", "decode"}:
        background.client.extract_speaker_embedding.assert_not_awaited()
    if stage == "entry":
        background.reconstruct.assert_not_awaited()
    if stage in {"entry", "decode", "response"}:
        assert await evidence.db.background_corpus_embeddings.count_documents({}) == 0


async def cluster_rows(evidence):
    for number in range(3):
        await reference(evidence, collection="background_corpus_embeddings")
        await evidence.db.background_corpus_embeddings.update_one(
            {"start": 0, "clip_key": "synthetic-recording:0.000:10.000:speech"},
            {
                "$set": {
                    "start": number * 3,
                    "end": number * 3 + 2,
                    "clip_key": f"synthetic-clip-{number}",
                    "text": f"Synthetic line {number}",
                    "conversation_title": "Synthetic title",
                }
            },
        )


async def test_cluster_cache_rebuilds_after_privacy_change(
    evidence, background, monkeypatch
):
    await cluster_rows(evidence)
    held = await bucket.get_background_clusters(background.user)
    assert held["clusters"] == [] and held["remaining"] == 0
    await allow(evidence)
    # The fixture helper inserts an override directly; a real override also advances revision.
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    result = await bucket.get_background_clusters(background.user)
    assert len(result["clusters"]) == 1 and result["remaining"] == 3
    original = bucket._cluster_rows

    def unexpected(*args, **kwargs):
        raise AssertionError("Current cache should be reused")

    monkeypatch.setattr(bucket, "_cluster_rows", unexpected)
    cached = await bucket.get_background_clusters(background.user)
    assert cached == result
    monkeypatch.setattr(bucket, "_cluster_rows", original)
    await revoke(evidence)
    held = await bucket.get_background_clusters(background.user)
    assert held["clusters"] == [] and held["remaining"] == 0
    assert "Synthetic line" not in str(held)


@pytest.mark.parametrize("stage", ["entry", "write", "allowed"])
async def test_cluster_decisions_reject_held_members_and_stale_publication(
    evidence, background, monkeypatch, stage
):
    await cluster_rows(evidence)
    if stage != "entry":
        await allow(evidence)
    if stage == "write":
        cls = type(evidence.db.background_clips)
        original = cls.update_one

        async def write(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if self.name == "background_clips":
                await revoke(evidence)
            return result

        monkeypatch.setattr(cls, "update_one", write)
    cluster = {
        "cluster_id": "synthetic-cluster",
        "member_keys": [f"synthetic-clip-{n}" for n in range(3)],
    }
    if stage == "allowed":
        result = await bucket.decide_background_cluster(
            background.user, cluster, "background_speech"
        )
        assert result["exemplars_added"] == 3
        reviews = await bucket.list_background_decisions(background.user)
        assert len(reviews["decisions"]) == 1
        await revoke(evidence)
        assert (await bucket.list_background_decisions(background.user))[
            "decisions"
        ] == []
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await bucket.decide_background_cluster(
                background.user, cluster, "background_speech"
            )
        assert await evidence.db.background_cluster_reviews.count_documents({}) == 0
        if stage == "entry":
            assert await evidence.db.background_clips.count_documents({}) == 0


@pytest.mark.parametrize("stage", ["held", "refresh", "allowed"])
async def test_registered_index_cache_reuse_obeys_privacy(
    evidence, background, monkeypatch, stage
):
    await reference(evidence, collection="background_corpus_embeddings")
    if stage != "held":
        await allow(evidence)
    cls = type(evidence.db.background_corpus_embeddings)

    original = cls.update_one

    async def write(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if stage == "refresh" and self.name == "background_corpus_embeddings":
            await revoke(evidence)
        return result

    monkeypatch.setattr(cls, "update_one", write)
    if stage == "refresh":
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                index.index_background_corpus_job,
                "evidence-owner",
                "synthetic-revision",
            )
        assert await evidence.db.background_index_runs.count_documents({}) == 0
    else:
        result = await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
        assert result["cached"] == int(stage == "allowed")
        assert result["total"] == int(stage == "allowed")
    background.reconstruct.assert_not_awaited()
    background.client.extract_speaker_embedding.assert_not_awaited()
