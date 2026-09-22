"""Corpus indexing makes independent progress without relaxing per-result holds."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from test_privacy_background_audio import background  # noqa: F401
from test_privacy_background_audio import reference
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import background_bucket_controller as bucket
from backend.routers.modules import data_audit_routes
from backend.services import privacy
from backend.workers import background_index_jobs as index


async def ordinary_recording(evidence):
    original = await evidence.db.conversations.find_one({})
    row = deepcopy(original)
    row.pop("_id")
    row.update(conversation_id="ordinary-recording", client_id="ordinary-device")
    await evidence.db.conversations.insert_one(row)


@pytest.mark.parametrize("change", ["exclude", "retarget"])
async def test_registered_index_drains_unaffected_recordings_then_resumes(
    evidence, background, monkeypatch, change
):
    await allow(evidence)
    await ordinary_recording(evidence)
    monkeypatch.setattr(index, "EMBED_CONCURRENCY", 1)
    changed = False

    async def embedding(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            if change == "exclude":
                await revoke(evidence)
            else:
                await evidence.db.conversations.update_one(
                    {"conversation_id": "synthetic-recording"},
                    {"$set": {"client_id": "changed-device"}},
                )
        return {"embedding": [1.0, 0.0], "embedding_model": "synthetic-model"}

    background.client.extract_speaker_embedding.side_effect = embedding
    with pytest.raises(privacy.PrivacyHeld):
        await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
    rows = await evidence.db.background_corpus_embeddings.find({}).to_list(None)
    assert len(rows) == 1 and rows[0]["conversation_id"] == "ordinary-recording"
    assert rows[0]["privacy_reference_receipt"]
    assert background.client.extract_speaker_embedding.await_count == 2
    assert await evidence.db.background_index_runs.count_documents({}) == 0
    if change == "exclude":
        await allow(evidence)
    result = await asyncio.to_thread(
        index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
    )
    assert result["cached"] == 1 and result["embedded"] == 1
    assert result["total"] == 2
    assert background.client.extract_speaker_embedding.await_count == 3


async def test_corpus_revision_changes_when_privacy_admission_changes(
    evidence, background
):
    before = await bucket._corpus_revision("evidence-owner")
    assert await bucket._corpus_revision("evidence-owner") == before
    await revoke(evidence)
    assert await bucket._corpus_revision("evidence-owner") != before


async def enqueue_request(user):
    app = FastAPI()
    app.include_router(data_audit_routes.router)
    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)
    app.dependency_overrides[data_audit_routes.current_active_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/data-audit/background/index")


async def test_index_enqueue_fingerprints_revisions_without_loading_history(
    evidence, background, monkeypatch
):
    await evidence.db.capture_sources.insert_one(
        {
            "source_id": "unrelated-screen",
            "user_id": "another-owner",
            "privacy_revision": 1,
            "privacy_updating": True,
        }
    )
    expected_revision = await bucket._corpus_revision("evidence-owner")
    queue = Mock(return_value=SimpleNamespace(id="synthetic-index-job"))
    monkeypatch.setattr(bucket, "default_queue", SimpleNamespace(enqueue=queue))

    class MetadataDatabase:
        def __getattr__(self, name):
            if name == "privacy_screening":
                raise AssertionError("Enqueue must not load screening history")
            return getattr(evidence.db, name)

    monkeypatch.setattr(privacy, "database", lambda: MetadataDatabase())
    response = await enqueue_request(background.user)
    assert response.status_code == 200
    queue.assert_called_once()
    assert queue.call_args.kwargs["source_revision"] == expected_revision
    assert queue.call_args.kwargs["requested_by"] == "evidence-owner"
    background.reconstruct.assert_not_awaited()
    background.client.extract_speaker_embedding.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["revision", "updating", "already_updating", "activation", "deletion"]
)
async def test_index_enqueue_holds_changed_policy_before_creating_job(
    evidence, background, monkeypatch, change
):
    queue = Mock(return_value=SimpleNamespace(id="synthetic-index-job"))
    monkeypatch.setattr(bucket, "default_queue", SimpleNamespace(enqueue=queue))
    original = privacy._assert_capture_current
    if change == "already_updating":
        await evidence.db.capture_sources.update_one(
            {"source_id": "screenpipe-test"}, {"$set": {"privacy_updating": True}}
        )

    async def changed(owner, snapshot):
        query = {"source_id": "screenpipe-test", "user_id": "evidence-owner"}
        if change == "activation":
            await evidence.db.capture_sources.insert_one(
                {
                    "source_id": "new-screen",
                    "user_id": owner,
                    "privacy_revision": 1,
                    "privacy_tracks": ["test-display"],
                }
            )
        elif change == "deletion":
            await evidence.db.capture_sources.delete_one(query)
        elif change != "already_updating":
            update = (
                {"$inc": {"privacy_revision": 1}}
                if change == "revision"
                else {"$set": {"privacy_updating": True}}
            )
            await evidence.db.capture_sources.update_one(query, update)
        await original(owner, snapshot)

    monkeypatch.setattr(privacy, "_assert_capture_current", changed)
    response = await enqueue_request(background.user)
    assert response.status_code == 423
    assert set(response.json()) == {"detail"}
    queue.assert_not_called()
    assert await evidence.db.background_index_runs.count_documents({}) == 0


async def test_refresh_retains_both_cached_audio_and_new_metadata_evidence(
    evidence, background
):
    await allow(evidence)
    await ordinary_recording(evidence)
    await reference(
        evidence, collection="background_corpus_embeddings", cid="ordinary-recording"
    )
    scope = privacy.ConversationPrivacyFilter()
    assert await scope.filter([{"conversation_id": "synthetic-recording"}])
    receipt = await scope.reference_receipt("evidence-owner")
    await evidence.db.conversations.update_one(
        {"conversation_id": "ordinary-recording"},
        {"$set": {"transcript_versions.0.metadata.privacy_reference_receipt": receipt}},
    )
    result = await asyncio.to_thread(
        index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
    )
    assert result["cached"] == 1
    row = await evidence.db.background_corpus_embeddings.find_one(
        {"conversation_id": "ordinary-recording"}
    )
    await evidence.db.conversations.update_one(
        {"conversation_id": "ordinary-recording"},
        {"$unset": {"transcript_versions.0.metadata.privacy_reference_receipt": ""}},
    )
    await revoke(evidence)
    assert not await privacy.ConversationPrivacyFilter().filter_embeddings([row])


async def test_changed_embedding_model_does_not_mark_index_complete(
    evidence, background
):
    await allow(evidence)
    background.client.extract_speaker_embedding.return_value = {
        "embedding": [1.0, 0.0],
        "embedding_model": "changed-model",
    }
    result = await asyncio.to_thread(
        index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
    )
    assert result["failures"] == 1 and result["embedded"] == 0
    assert await evidence.db.background_corpus_embeddings.count_documents({}) == 0
    assert await evidence.db.background_index_runs.count_documents({}) == 0
