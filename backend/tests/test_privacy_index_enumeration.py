"""Real index jobs amortize enumeration without sharing inference admission."""

import asyncio
from copy import deepcopy

import pytest
from test_privacy_background_audio import background  # noqa: F401
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.services import privacy
from backend.workers import background_index_jobs as index


async def add_ordinary_recordings(evidence, count):
    original = await evidence.db.conversations.find_one({})
    for number in range(count):
        row = deepcopy(original)
        row.pop("_id")
        row.update(conversation_id=f"ordinary-{number}", client_id="ordinary-device")
        await evidence.db.conversations.insert_one(row)


async def test_registered_index_loads_policy_once_per_enumeration_batch(
    evidence, background, monkeypatch
):
    await allow(evidence)
    await add_ordinary_recordings(evidence, 19)
    monkeypatch.setattr(index, "CORPUS_BATCH_SIZE", 8, raising=False)
    original_load = privacy.load_snapshot
    original_enumerate = index._corpus_candidates
    loads = 0
    enumeration_loads = []

    async def counted_load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return await original_load(*args, **kwargs)

    async def measured_enumeration(*args):
        before = loads
        result = await original_enumerate(*args)
        enumeration_loads.append(loads - before)
        return result

    monkeypatch.setattr(privacy, "load_snapshot", counted_load)
    monkeypatch.setattr(index, "_corpus_candidates", measured_enumeration)
    result = await asyncio.to_thread(
        index.index_background_corpus_job, "evidence-owner", "synthetic-revision"
    )
    assert result["embedded"] == 20
    assert enumeration_loads == [3]
    # Each recording's actual processing still obtains fresh admission.
    assert loads >= 23
    assert await evidence.db.background_corpus_embeddings.count_documents({}) == 20


@pytest.mark.parametrize("change", ["exclude", "retarget"])
async def test_registered_index_rechecks_batch_changes_and_preserves_other_device(
    evidence, background, monkeypatch, change
):
    await allow(evidence)
    await add_ordinary_recordings(evidence, 1)
    original_filter = privacy.ConversationPrivacyFilter.filter
    changed = False

    async def change_after_batch_admission(self, rows):
        nonlocal changed
        admitted = await original_filter(self, rows)
        if len(rows) > 1 and not changed:
            changed = True
            if change == "exclude":
                await revoke(evidence)
            else:
                await evidence.db.conversations.update_one(
                    {"conversation_id": "synthetic-recording"},
                    {"$set": {"client_id": "retargeted-device"}},
                )
        return admitted

    monkeypatch.setattr(
        privacy.ConversationPrivacyFilter, "filter", change_after_batch_admission
    )
    if change == "retarget":
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                index.index_background_corpus_job,
                "evidence-owner",
                "synthetic-revision",
            )
    else:
        result = await asyncio.to_thread(
            index.index_background_corpus_job,
            "evidence-owner",
            "synthetic-revision",
        )
        assert result["embedded"] == 1
    assert changed
    rows = await evidence.db.background_corpus_embeddings.find({}).to_list(None)
    assert len(rows) == 1 and rows[0]["conversation_id"] == "ordinary-0"
    assert rows[0]["privacy_reference_receipt"]
    assert background.client.extract_speaker_embedding.await_count == 1
