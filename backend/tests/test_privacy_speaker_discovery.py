"""Registered speaker-discovery workers exclude held and stale corpus evidence."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401
from test_privacy_guided_enrollment import guided_setup  # noqa: F401

from backend.controllers import guided_enrollment_controller as guided
from backend.models import job as job_model
from backend.services import privacy
from backend.workers import speaker_discovery_jobs as corpus
from backend.workers import unknown_speaker_jobs as unknown


@pytest.fixture
async def discovery(evidence, guided_setup, monkeypatch):
    await evidence.db.conversations.update_many(
        {}, {"$set": {"transcript_versions.0.segments.0.speaker": "Unknown Speaker 4"}}
    )
    client = NS(
        get_embedding_info=AsyncMock(
            return_value={"embedding_model": "synthetic-model"}
        ),
        extract_speaker_embedding=AsyncMock(return_value={"embedding": [1.0, 0.0]}),
        score_cached_embeddings=AsyncMock(
            side_effect=lambda _id, vectors: {
                "scores": [{"sim_centroid": 0.8, "max_clip_sim": 0.7} for _ in vectors]
            }
        ),
    )
    for module in (corpus, unknown):
        monkeypatch.setattr(
            module,
            "Conversation",
            NS(get_pymongo_collection=lambda: evidence.db.conversations),
        )
        monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: client)
        monkeypatch.setattr(module, "reconstruct_audio_segment", evidence.reconstruct)
        monkeypatch.setattr(module, "get_current_job", lambda: None)
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    return NS(client=client, user=NS(user_id="evidence-owner", is_superuser=False))


async def run_worker(kind):
    if kind == "corpus":
        return await asyncio.to_thread(
            corpus.discover_speaker_candidates_job,
            "evidence-owner",
            "synthetic-gallery",
            "Synthetic speaker",
        )
    return await asyncio.to_thread(
        unknown.discover_unknown_speakers_job, "evidence-owner"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corpus", "unknown"])
async def test_registered_worker_filters_before_transcript_and_external_calls(
    evidence, discovery, monkeypatch, kind
):
    module = corpus if kind == "corpus" else unknown
    original = module._active_segments

    def segments(doc):
        assert doc["conversation_id"] != "synthetic-recording"
        return original(doc)

    monkeypatch.setattr(module, "_active_segments", segments)
    result = await run_worker(kind)
    assert result["privacy_held_recordings"] == 1
    assert [call.args[0] for call in evidence.reconstruct.await_args_list] == [
        "ordinary-recording"
    ]
    discovery.client.extract_speaker_embedding.assert_awaited_once()
    if kind == "corpus":
        assert result["scored"] == 1
        matches = await evidence.db.speaker_corpus_matches.find({}).to_list()
        assert [r["conversation_id"] for r in matches] == ["ordinary-recording"]
    else:
        assert result["local_identities"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corpus", "unknown"])
@pytest.mark.parametrize("stage", ["decode", "embedding"])
async def test_registered_worker_discards_policy_race(evidence, discovery, kind, stage):
    await evidence.db.conversations.delete_one(
        {"conversation_id": "ordinary-recording"}
    )
    await allow(evidence)

    async def changed(*args, **kwargs):
        await revoke(evidence)
        return b"synthetic audio" if stage == "decode" else {"embedding": [1.0, 0.0]}

    target = (
        evidence.reconstruct
        if stage == "decode"
        else discovery.client.extract_speaker_embedding
    )
    target.side_effect = changed
    with pytest.raises(privacy.PrivacyHeld):
        await run_worker(kind)
    assert discovery.client.extract_speaker_embedding.await_count == (
        stage == "embedding"
    )
    discovery.client.score_cached_embeddings.assert_not_awaited()
    for collection in [
        "speaker_corpus_embeddings",
        "speaker_corpus_matches",
        "unknown_speaker_clusters",
    ]:
        assert await evidence.db[collection].count_documents({}) == 0


@pytest.mark.asyncio
async def test_cached_private_embedding_never_reaches_scoring(evidence, discovery):
    await evidence.db.speaker_corpus_embeddings.insert_one(
        {
            "clip_key": "synthetic-recording:0.000:10.000",
            "embedding_model": "synthetic-model",
            "embedding": [0.0, 1.0],
        }
    )
    result = await run_worker("corpus")
    assert result["privacy_held_recordings"] == 1
    discovery.client.score_cached_embeddings.assert_awaited_once_with(
        "synthetic-gallery", [[1.0, 0.0]]
    )


@pytest.mark.asyncio
async def test_allowed_cache_reused_without_decoding(evidence, discovery):
    await evidence.db.speaker_corpus_embeddings.insert_one(
        {
            "clip_key": "ordinary-recording:0.000:10.000",
            "embedding_model": "synthetic-model",
            "embedding": [1.0, 0.0],
        }
    )
    result = await run_worker("corpus")
    assert result["scored"] == 1
    evidence.reconstruct.assert_not_awaited()
    discovery.client.extract_speaker_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_change_after_cache_enumeration_prevents_scoring(
    evidence, discovery, monkeypatch
):
    await evidence.db.conversations.delete_one(
        {"conversation_id": "ordinary-recording"}
    )
    await allow(evidence)
    await evidence.db.speaker_corpus_embeddings.insert_one(
        {
            "clip_key": "synthetic-recording:0.000:10.000",
            "embedding_model": "synthetic-model",
            "embedding": [1.0, 0.0],
        }
    )
    original = corpus._speech_clips

    async def changed(*a, **kw):
        rows = await original(*a, **kw)
        await revoke(evidence)
        return rows

    monkeypatch.setattr(corpus, "_speech_clips", changed)
    with pytest.raises(privacy.PrivacyHeld):
        await run_worker("corpus")
    evidence.reconstruct.assert_not_awaited()
    discovery.client.score_cached_embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_change_during_scoring_prevents_match_publication(
    evidence, discovery
):
    await allow(evidence)

    async def changed(*a, **kw):
        await revoke(evidence)
        return {"scores": [{"sim_centroid": 0.8} for _ in a[1]]}

    discovery.client.score_cached_embeddings.side_effect = changed
    with pytest.raises(privacy.PrivacyHeld):
        await run_worker("corpus")
    assert await evidence.db.speaker_corpus_matches.count_documents({}) == 0


@pytest.mark.asyncio
async def test_policy_change_during_clustering_prevents_publication(
    evidence, discovery, monkeypatch
):
    await allow(evidence)
    original = unknown.cluster_local_identities

    # This CPU function runs in a thread. Change policy in the same fake database
    # through a local loop to exercise the real post-thread revision boundary.
    def changed(*a, **kw):
        asyncio.run(revoke(evidence))
        return original(*a, **kw)

    monkeypatch.setattr(unknown, "cluster_local_identities", changed)
    with pytest.raises(privacy.PrivacyHeld):
        await run_worker("unknown")
    assert await evidence.db.unknown_speaker_clusters.count_documents({}) == 0


@pytest.mark.asyncio
async def test_cluster_retains_full_corpus_provenance_and_stale_review_is_held(
    evidence, discovery
):
    await allow(evidence)
    result = await run_worker("unknown")
    assert result["clusters"] == 1
    row = await evidence.db.unknown_speaker_clusters.find_one({})
    assert row["evidence_conversation_ids"] == [
        "ordinary-recording",
        "synthetic-recording",
    ]
    visible = await guided.list_unknown_clusters(discovery.user)
    assert len(visible["clusters"]) == 1
    assert "privacy_revisions" not in visible["clusters"][0]
    await revoke(evidence)
    assert (await guided.list_unknown_clusters(discovery.user))["clusters"] == []
    with pytest.raises(privacy.PrivacyHeld):
        await guided.decide_unknown_cluster(
            discovery.user,
            row["cluster_id"],
            row["run_fingerprint"],
            "confirm",
            "Synthetic speaker",
            ["ordinary-recording"],
            [{"identity_key": "ordinary-recording"}],
        )


@pytest.mark.asyncio
async def test_cluster_depends_on_other_corpus_groups(evidence, discovery):
    await allow(evidence)
    result = await run_worker("unknown")
    row = await evidence.db.unknown_speaker_clusters.find_one({})
    # Keep an allowed displayed member but preserve the actual full run receipt.
    await evidence.db.unknown_speaker_clusters.update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "members": [
                    m
                    for m in row["members"]
                    if m["conversation_id"] == "ordinary-recording"
                ]
            }
        },
    )
    await revoke(evidence)
    assert (await guided.list_unknown_clusters(discovery.user))["clusters"] == []


@pytest.mark.asyncio
async def test_unproven_old_cluster_is_held(evidence, discovery):
    await evidence.db.unknown_speaker_clusters.insert_one(
        {
            "requested_by": "evidence-owner",
            "status": "pending",
            "cluster_id": "synthetic-old",
            "members": [{"conversation_id": "ordinary-recording"}],
        }
    )
    assert (await guided.list_unknown_clusters(discovery.user))["clusters"] == []


@pytest.mark.asyncio
async def test_rebuilt_cluster_rejects_old_review_fingerprint(evidence, discovery):
    await allow(evidence)
    before = await run_worker("unknown")
    row = await evidence.db.unknown_speaker_clusters.find_one({})
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    after = await run_worker("unknown")
    assert before["run_fingerprint"] != after["run_fingerprint"]
    response = await guided.decide_unknown_cluster(
        discovery.user,
        row["cluster_id"],
        before["run_fingerprint"],
        "confirm",
        "Synthetic speaker",
        [],
        [],
    )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corpus", "unknown"])
async def test_failed_embedding_tasks_drain_siblings_before_return(
    evidence, discovery, kind
):
    await allow(evidence)
    entered = asyncio.Event()
    completed = []

    async def embed(wav):
        # Reconstruction uses unique fixture bytes so sibling lifetimes are observable.
        if wav == b"synthetic-recording":
            await entered.wait()
            raise privacy.PrivacyHeld()
        entered.set()
        await asyncio.sleep(0.01)
        completed.append(True)
        return {"embedding": [1.0, 0.0]}

    evidence.reconstruct.side_effect = lambda cid, *a: cid.encode()
    discovery.client.extract_speaker_embedding.side_effect = embed
    with pytest.raises(privacy.PrivacyHeld):
        await run_worker(kind)
    assert completed == [True]
    assert await evidence.db.speaker_corpus_matches.count_documents({}) == 0
    assert await evidence.db.unknown_speaker_clusters.count_documents({}) == 0
