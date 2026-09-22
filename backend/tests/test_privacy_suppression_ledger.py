"""Suppression reads, decisions and publication obey capture privacy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_annotations import Field
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import background_suppression_controller as controller
from backend.services import privacy
from backend.workers import background_suppression as ledger


@pytest.fixture
async def suppression(evidence, monkeypatch):
    evidence.conv.save = AsyncMock()
    evidence.conv.transcript_versions = [evidence.conv.active_transcript]
    lookup = AsyncMock(return_value=evidence.conv)
    model = SimpleNamespace(
        get_pymongo_collection=lambda: evidence.db.conversations,
        conversation_id=Field(),
        find_one=lookup,
    )
    for module in (ledger, controller):
        monkeypatch.setattr(module, "Conversation", model)
    add = AsyncMock(return_value={"added": True})
    monkeypatch.setattr(controller, "add_background_clip", add)
    row = {
        "user_id": "evidence-owner",
        "conversation_id": "synthetic-recording",
        "segment_start": 0,
        "segment_end": 10,
        "status": "queued",
        "zone": "unsure",
        "text": "Synthetic ledger text",
        "cluster_signature": "synthetic-cluster",
        "background_similarity": 0.8,
        "privacy_reference_receipt": [],
        "bucket_type": "background_speech",
        "previous_identified_as": "Synthetic speaker",
    }
    await evidence.db.background_suppressions.insert_one(row)
    return SimpleNamespace(
        user=SimpleNamespace(user_id="evidence-owner"), row=row, add=add, lookup=lookup
    )


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize(
    "entry",
    ["get_conversation_suppressions", "load_sticky_segments", "get_subject_override"],
)
async def test_suppression_reads_require_original_record(
    evidence, suppression, entry, allowed
):
    if allowed:
        await allow(evidence)
    if entry == "get_conversation_suppressions":
        call = lambda: controller.get_conversation_suppressions(
            suppression.user, "synthetic-recording"
        )
    else:
        call = lambda: getattr(ledger, entry)("evidence-owner", "synthetic-recording")
    if allowed:
        result = await call()
        if entry == "get_conversation_suppressions":
            assert result["total"] == 1
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await call()


@pytest.mark.parametrize("stage", ["entry", "lookup", "write", "allowed"])
async def test_suppression_record_checks_every_publication_boundary(
    evidence, suppression, monkeypatch, stage
):
    await evidence.db.background_suppressions.delete_many({})
    if stage != "entry":
        await allow(evidence)
    cls = type(evidence.db.background_suppressions)
    if stage in {"lookup", "write"}:
        method = "find_one" if stage == "lookup" else "update_one"
        original = getattr(cls, method)

        async def changing(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if self.name == "background_suppressions":
                await revoke(evidence)
            return result

        monkeypatch.setattr(cls, method, changing)
    records = [dict(suppression.row, foreground_similarity=0.0, embedding=[1.0, 0.0])]
    if stage == "allowed":
        result = await ledger.record_conversation_suppressions(
            "synthetic-recording", "evidence-owner", records, "backfill"
        )
        assert result == 1
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await ledger.record_conversation_suppressions(
                "synthetic-recording", "evidence-owner", records, "backfill"
            )
    assert await evidence.db.background_suppressions.count_documents({}) == int(
        stage in {"write", "allowed"}
    )


@pytest.mark.parametrize("decision", ["restore", "confirm"])
@pytest.mark.parametrize("stage", ["entry", "side_effect", "allowed"])
async def test_suppression_review_stops_before_next_mutation(
    evidence, suppression, monkeypatch, decision, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "side_effect":
        if decision == "confirm":

            async def add(*args, **kwargs):
                await revoke(evidence)
                return {"added": True}

            suppression.add.side_effect = add
        else:
            cls = type(evidence.db.media_role_overrides)
            original = cls.update_one

            async def changing(self, *args, **kwargs):
                result = await original(self, *args, **kwargs)
                if self.name == "media_role_overrides":
                    await revoke(evidence)
                return result

            monkeypatch.setattr(cls, "update_one", changing)
    if stage == "allowed":
        result = await controller.decide_suppression_cluster(
            suppression.user, "synthetic-recording", "synthetic-cluster", decision
        )
        assert result["decision"] == decision
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await controller.decide_suppression_cluster(
                suppression.user, "synthetic-recording", "synthetic-cluster", decision
            )
    row = await evidence.db.background_suppressions.find_one({})
    assert row["status"] == (
        {"restore": "restored", "confirm": "confirmed"}[decision]
        if stage == "allowed"
        else "queued"
    )
    if stage == "entry":
        suppression.add.assert_not_awaited()


@pytest.mark.parametrize("stage", ["entry", "save", "allowed"])
async def test_restore_labels_fences_transcript_save(evidence, suppression, stage):
    if stage != "entry":
        await allow(evidence)
    if stage == "save":

        async def save():
            await revoke(evidence)

        evidence.conv.save.side_effect = save
    if stage == "allowed":
        await controller._restore_segment_labels(
            "synthetic-recording", [suppression.row]
        )
        evidence.conv.save.assert_awaited_once()
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await controller._restore_segment_labels(
                "synthetic-recording", [suppression.row]
            )
        if stage == "entry":
            suppression.lookup.assert_not_awaited()
            evidence.conv.save.assert_not_awaited()
