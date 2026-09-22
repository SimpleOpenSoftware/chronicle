"""Only references used by a recording participate in its admission fence."""

import asyncio
from copy import deepcopy

import pytest
from test_privacy_background_audio import background  # noqa: F401
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.services import privacy
from backend.services.reference_dependencies import seal
from backend.workers import background_index_jobs as index


async def receipt_and_ordinary(evidence):
    await allow(evidence)
    original = await evidence.db.conversations.find_one({}, privacy._RECORD_PROJECTION)
    receipt = await seal("evidence-owner", [original])
    target = await evidence.db.conversations.find_one({})
    target.pop("_id")
    target.update(conversation_id="ordinary-recording", client_id="ordinary-device")
    await evidence.db.conversations.insert_one(deepcopy(target))
    return receipt, target


@pytest.mark.parametrize("entry", ["require", "filter", "listing", "records", "worker"])
async def test_unused_reference_changing_during_load_does_not_hold_other_device(
    evidence, background, monkeypatch, entry
):
    _, row = await receipt_and_ordinary(evidence)
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    if entry == "require":
        await privacy.require_record(row)
    elif entry == "filter":
        assert await privacy.ConversationPrivacyFilter().filter([row])
    elif entry == "listing":
        assert await privacy.filter_conversation_documents([row], "evidence-owner")
    elif entry == "records":
        assert await privacy.filter_records([row], "evidence-owner")
    else:
        # The original private source stays excluded; the independent recording
        # still reaches reconstruction, embedding and durable publication.
        await revoke(evidence)
        result = await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
        assert result["embedded"] == 1
        assert background.reconstruct.await_args.args[0] == "ordinary-recording"


@pytest.mark.parametrize("entry", ["require", "filter", "listing", "records"])
async def test_used_reference_changing_during_load_is_held_before_return(
    evidence, background, monkeypatch, entry
):
    receipt, row = await receipt_and_ordinary(evidence)
    row["privacy_reference_receipt"] = receipt
    await evidence.db.conversations.update_one(
        {"conversation_id": "ordinary-recording"},
        {"$set": {"privacy_reference_receipt": receipt}},
    )
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    with pytest.raises(privacy.PrivacyHeld):
        if entry == "require":
            await privacy.require_record(row)
        elif entry == "filter":
            await privacy.ConversationPrivacyFilter().filter([row])
        elif entry == "listing":
            await privacy.filter_conversation_documents([row], "evidence-owner")
        else:
            await privacy.filter_records([row], "evidence-owner")


async def test_enumeration_race_does_not_discard_independent_work(
    evidence, background, monkeypatch
):
    await receipt_and_ordinary(evidence)
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    with pytest.raises(privacy.PrivacyHeld):
        await asyncio.to_thread(
            index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
        )
    rows = await evidence.db.background_corpus_embeddings.find({}).to_list(None)
    assert len(rows) == 1 and rows[0]["conversation_id"] == "ordinary-recording"
    assert background.client.extract_speaker_embedding.await_count == 1
    assert await evidence.db.background_index_runs.count_documents({}) == 0
