"""Rejected listing candidates must not fence independent returned evidence."""

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401
from test_privacy_source_search import indexed  # noqa: F401

from backend.services import privacy, source_search


@pytest.mark.parametrize("entry", ["search", "filter", "listing", "records"])
async def test_changing_rejected_source_does_not_hold_ordinary_results(
    evidence, indexed, monkeypatch, entry
):
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    rows = await evidence.db.conversations.find({}).to_list(None)
    if entry == "search":
        result = await source_search.search(
            "evidence-owner", "secret", kinds=["recording"], fields=["title"]
        )
        assert [row["key"] for row in result["items"]] == ["ordinary-recording"]
        assert result["total"] == 1
    else:
        if entry == "filter":
            result = await privacy.ConversationPrivacyFilter().filter(rows)
        elif entry == "listing":
            result = await privacy.filter_conversation_documents(rows, "evidence-owner")
        else:
            result = await privacy.filter_records(rows, "evidence-owner")
        assert [row["conversation_id"] for row in result] == ["ordinary-recording"]


@pytest.mark.parametrize("entry", ["filter", "listing", "records"])
async def test_rejected_derivative_does_not_retain_its_reference_dependencies(
    evidence, indexed, monkeypatch, entry
):
    from test_privacy_enrollment import START

    from backend.services.reference_dependencies import seal

    await allow(evidence)
    original = await evidence.db.conversations.find_one(
        {"conversation_id": "synthetic-recording"}
    )
    receipt = await seal("evidence-owner", [original])
    ordinary = await evidence.db.conversations.find_one(
        {"conversation_id": "ordinary-recording"}
    )
    rejected = {
        **ordinary,
        "conversation_id": "rejected-derivative",
        "client_id": "held-derivative",
        "privacy_reference_receipt": receipt,
    }
    rejected.pop("_id")
    await evidence.db.conversations.insert_one(rejected)
    await evidence.db.capture_sources.insert_one(
        {
            "user_id": "evidence-owner",
            "source_id": "held-derivative",
            "privacy_enabled_from": START,
            "privacy_revision": 1,
        }
    )
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {"source_id": "screenpipe-test"}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    rows = [rejected, ordinary]
    if entry == "filter":
        guard = privacy.ConversationPrivacyFilter()
        result = await guard.filter(rows)
        await guard.assert_current()
        assert not guard.snapshots[
            "evidence-owner"
        ].gallery_dependencies.publication_owners
    elif entry == "listing":
        result = await privacy.filter_conversation_documents(rows, "evidence-owner")
    else:
        result = await privacy.filter_records(rows, "evidence-owner")
    assert [row["conversation_id"] for row in result] == ["ordinary-recording"]


async def test_rejected_candidate_cannot_remove_previously_admitted_reference(
    evidence, indexed
):
    from test_privacy_enrollment import START

    from backend.services.reference_dependencies import seal

    await allow(evidence)
    original = await evidence.db.conversations.find_one(
        {"conversation_id": "synthetic-recording"}
    )
    receipt = await seal("evidence-owner", [original])
    ordinary = await evidence.db.conversations.find_one(
        {"conversation_id": "ordinary-recording"}
    )
    admitted = {**ordinary, "privacy_reference_receipt": receipt}
    rejected = {**admitted, "client_id": "held-derivative"}
    await evidence.db.capture_sources.insert_one(
        {
            "user_id": "evidence-owner",
            "source_id": "held-derivative",
            "privacy_enabled_from": START,
            "privacy_revision": 1,
        }
    )
    snapshot = await privacy.load_snapshot("evidence-owner")
    assert snapshot.permits_record(admitted)
    assert not snapshot.permits_record(rejected)
    await evidence.db.privacy_reference_dependencies.delete_one({"_id": receipt[0]})
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("evidence-owner", snapshot)


async def test_rejected_other_owner_does_not_hold_returned_owner(
    evidence, indexed, monkeypatch
):
    from test_privacy_enrollment import START

    held = await evidence.db.conversations.find_one(
        {"conversation_id": "synthetic-recording"}
    )
    await evidence.db.conversations.update_one(
        {"_id": held["_id"]}, {"$set": {"user_id": "other-owner"}}
    )
    await evidence.db.capture_sources.insert_one(
        {
            "user_id": "other-owner",
            "source_id": "screenpipe-test",
            "privacy_enabled_from": START,
            "privacy_revision": 1,
        }
    )
    load = privacy._load_capture_snapshot

    async def changing(owner, *args, **kwargs):
        snapshot = await load(owner, *args, **kwargs)
        await evidence.db.capture_sources.update_one(
            {"user_id": "other-owner"}, {"$inc": {"privacy_revision": 1}}
        )
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", changing)
    guard = privacy.ConversationPrivacyFilter()
    rows = [
        {"conversation_id": value}
        for value in ["synthetic-recording", "ordinary-recording"]
    ]
    result = await guard.filter(rows)
    await guard.assert_current()
    assert result == [{"conversation_id": "ordinary-recording"}]


from test_privacy_enrollment_operations import enrollment_setup  # noqa: E402, F401
from test_privacy_gallery_reads import gallery  # noqa: E402, F401


@pytest.mark.parametrize("admitted_first", [False, True])
async def test_rejected_gallery_dependencies_preserve_prior_admissions(
    evidence, gallery, admitted_first
):
    from test_privacy_gallery_dependencies import derived

    row, _ = await derived(evidence, gallery)
    await evidence.db.capture_sources.insert_one(
        {
            "user_id": "review-admin",
            "source_id": "held-derivative",
            "privacy_enabled_from": row["created_at"],
            "privacy_revision": 1,
        }
    )
    snapshot = await privacy.load_snapshot("review-admin")
    if admitted_first:
        assert snapshot.permits_record(row)
    assert not snapshot.permits_record({**row, "client_id": "held-derivative"})
    ordinary = {k: v for k, v in row.items() if k != "transcript_versions"}
    assert snapshot.permits_record(ordinary)
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    if admitted_first:
        with pytest.raises(privacy.PrivacyHeld):
            await privacy.assert_current("review-admin", snapshot)
    else:
        await privacy.assert_current("review-admin", snapshot)
        assert not snapshot.gallery_dependencies.publication_owners
