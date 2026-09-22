"""Exercise annotation mutations with real privacy decisions and fake side effects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_privacy_annotations import Field, Query
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.constants import NOISE_LABEL
from backend.models.annotation import Annotation as AnnotationDocument
from backend.models.annotation import AnnotationStatus, AnnotationType, AnnotationUpdate
from backend.models.conversation import Conversation as ConversationDocument
from backend.routers.modules import annotation_routes as routes
from backend.services import privacy


@pytest.fixture
async def mutations(evidence, monkeypatch):
    saved = AsyncMock()
    deleted = AsyncMock()

    def item(**kwargs):
        values = AnnotationDocument.model_construct(
            user_id="evidence-owner", **kwargs
        ).model_dump()
        result = SimpleNamespace(**values, save=saved, delete=deleted)
        for name in (
            "is_memory_annotation",
            "is_transcript_annotation",
            "is_speech_suggestion_correction",
            "is_title_annotation",
        ):
            setattr(result, name, getattr(AnnotationDocument, name).__get__(result))
        return result

    annotation = item(
        conversation_id="synthetic-recording",
        segment_index=0,
        annotation_type=AnnotationType.TRANSCRIPT,
        corrected_text="Synthetic correction",
        status=AnnotationStatus.PENDING,
    )

    class Annotation:
        id = user_id = conversation_id = annotation_type = processed = Field()
        find_one = AsyncMock(return_value=annotation)
        query = Query([annotation])

        @staticmethod
        def find(*args):
            return Annotation.query

        def __new__(cls, **kwargs):
            kwargs.pop("user_id", None)
            return item(**kwargs)

    monkeypatch.setattr(routes, "Annotation", Annotation)
    evidence.conv.save = AsyncMock()
    evidence.conv.add_transcript_version = Mock(return_value=SimpleNamespace())
    evidence.conv.active_transcript = SimpleNamespace(
        segments=[
            ConversationDocument.SpeakerSegment(
                start=0, end=10, text="Synthetic text", speaker="Synthetic speaker"
            )
        ],
        metadata={},
        transcript="Synthetic text",
        words=[],
        version_id="synthetic-version",
        provider="synthetic",
        model="synthetic",
        diarization_source=None,
    )
    lookup = AsyncMock(return_value=evidence.conv)
    monkeypatch.setattr(
        routes,
        "Conversation",
        SimpleNamespace(
            conversation_id=Field(),
            user_id=Field(),
            find_one=lookup,
            SpeakerSegment=ConversationDocument.SpeakerSegment,
            SegmentType=ConversationDocument.SegmentType,
        ),
    )
    memory = SimpleNamespace(
        get_memory=AsyncMock(return_value={"content": "Synthetic note"}),
        update_memory=AsyncMock(),
    )
    monkeypatch.setattr(routes, "get_memory_service", lambda: memory)
    enqueue = Mock()
    monkeypatch.setattr(routes, "enqueue_memory_processing", enqueue)
    monkeypatch.setattr(routes, "conversation_edit_chain_in_flight", lambda _: None)
    bucket = AsyncMock()
    monkeypatch.setattr(
        routes.background_bucket_controller, "add_background_clip", bucket
    )

    async def revoke_on_call(*args, **kwargs):
        await revoke(evidence)

    return SimpleNamespace(
        revoke=revoke_on_call,
        annotation=annotation,
        model=Annotation,
        saved=saved,
        deleted=deleted,
        lookup=lookup,
        memory=memory,
        enqueue=enqueue,
        bucket=bucket,
        user=SimpleNamespace(user_id="evidence-owner"),
    )


def creation_data():
    return SimpleNamespace(
        conversation_id="synthetic-recording",
        segment_index=0,
        original_text="Synthetic original",
        corrected_text="Synthetic corrected",
        status=AnnotationStatus.PENDING,
        insert_after_index=0,
        insert_text="Synthetic insertion",
        insert_segment_type="note",
        insert_speaker=None,
        insert_start=2,
        insert_end=3,
        original_speaker="Synthetic speaker",
        corrected_speaker=NOISE_LABEL,
        segment_start_time=0,
        new_start=1,
        new_end=9,
    )


@pytest.mark.parametrize(
    "kind", ["transcript", "insert", "title", "diarization", "timing", "deletion"]
)
@pytest.mark.parametrize("stage", ["entry", "save", "allowed"])
async def test_create_checks_before_content_and_after_save(
    evidence, mutations, kind, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "save":
        mutations.saved.side_effect = mutations.revoke
    call = getattr(routes, f"create_{kind}_annotation")
    if stage == "allowed":
        result = await call(creation_data(), mutations.user)
        assert result.conversation_id == "synthetic-recording"
        mutations.saved.assert_awaited_once()
        assert evidence.conv.save.await_count == int(kind == "title")
        assert mutations.bucket.await_count == int(kind == "diarization")
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await call(creation_data(), mutations.user)
        assert mutations.saved.await_count == int(stage == "save")
        evidence.conv.save.assert_not_awaited()
        mutations.bucket.assert_not_awaited()


@pytest.mark.parametrize(
    "entry", ["apply_all_annotations", "apply_diarization_annotations"]
)
@pytest.mark.parametrize(
    "stage", ["entry", "read", "conversation_save", "annotation_save", "allowed"]
)
async def test_apply_rechecks_before_revision_and_memory(
    evidence, mutations, entry, stage
):
    mutations.annotation.annotation_type = AnnotationType.DIARIZATION
    mutations.annotation.corrected_speaker = "Synthetic revised speaker"
    if stage != "entry":
        await allow(evidence)
    if stage == "read":

        async def read():
            await revoke(evidence)
            return [mutations.annotation]

        mutations.model.query.to_list = read
    if stage == "conversation_save":
        evidence.conv.save.side_effect = mutations.revoke
    if stage == "annotation_save":
        mutations.saved.side_effect = mutations.revoke
    call = getattr(routes, entry)
    if stage == "allowed":
        result = await call("synthetic-recording", mutations.user)
        assert result.status_code == 200
        evidence.conv.save.assert_awaited_once()
        mutations.saved.assert_awaited_once()
        mutations.enqueue.assert_called_once()
        assert mutations.annotation.processed
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await call("synthetic-recording", mutations.user)
        mutations.enqueue.assert_not_called()
        if stage in {"entry", "read"}:
            evidence.conv.add_transcript_version.assert_not_called()
            evidence.conv.save.assert_not_awaited()
        if stage != "annotation_save":
            mutations.saved.assert_not_awaited()


@pytest.mark.parametrize("entry", ["update_annotation", "update_annotation_status"])
@pytest.mark.parametrize("stage", ["entry", "save", "allowed"])
async def test_edits_follow_original_owner_and_fence_response(
    evidence, mutations, entry, stage, caplog
):
    # An annotation's owner is not a substitute for the capture's privacy owner.
    mutations.annotation.user_id = "review-admin"
    if stage != "entry":
        await allow(evidence)
    if stage == "save":
        mutations.saved.side_effect = mutations.revoke
    call = getattr(routes, entry)
    update = (
        AnnotationUpdate(corrected_text="Synthetic private correction")
        if entry == "update_annotation"
        else AnnotationStatus.REJECTED
    )
    if stage == "allowed":
        await call(
            "synthetic-annotation", update, SimpleNamespace(user_id="review-admin")
        )
        mutations.saved.assert_awaited_once()
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await call(
                "synthetic-annotation", update, SimpleNamespace(user_id="review-admin")
            )
        assert mutations.saved.await_count == int(stage == "save")
    assert "Synthetic private correction" not in caplog.text


@pytest.mark.parametrize("stage", ["entry", "read", "save", "update", "allowed"])
async def test_memory_create_rechecks_original_note(evidence, mutations, stage):
    await evidence.db.conversations.update_one(
        {}, {"$set": {"vault_paths": ["Topics/Synthetic.md"]}}
    )
    if stage != "entry":
        await allow(evidence)
    if stage == "read":

        async def read(*args):
            await revoke(evidence)
            return {"content": "Synthetic note"}

        mutations.memory.get_memory.side_effect = read
    if stage == "save":
        mutations.saved.side_effect = mutations.revoke
    if stage == "update":

        async def update(**kwargs):
            await revoke(evidence)

        mutations.memory.update_memory.side_effect = update
    data = SimpleNamespace(
        memory_id="Topics/Synthetic.md",
        original_text="Synthetic original",
        corrected_text="Synthetic correction",
        status=AnnotationStatus.ACCEPTED,
    )
    if stage == "allowed":
        await routes.create_memory_annotation(data, mutations.user)
        mutations.memory.update_memory.assert_awaited_once()
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await routes.create_memory_annotation(data, mutations.user)
        if stage in {"entry", "read"}:
            mutations.saved.assert_not_awaited()
        if stage in {"entry", "read", "save"}:
            mutations.memory.update_memory.assert_not_awaited()
        if stage == "entry":
            mutations.memory.get_memory.assert_not_awaited()


async def test_missing_original_provenance_holds_edit(evidence, mutations):
    await evidence.db.conversations.delete_many({})
    with pytest.raises(privacy.PrivacyHeld):
        await routes.update_annotation(
            "synthetic-annotation",
            AnnotationUpdate(corrected_text="Synthetic"),
            mutations.user,
        )
    mutations.saved.assert_not_awaited()


async def test_user_can_delete_held_unprocessed_annotation(evidence, mutations):
    result = await routes.delete_annotation("synthetic-annotation", mutations.user)
    assert result["status"] == "deleted"
    mutations.deleted.assert_awaited_once()


@pytest.mark.parametrize(
    "kind", [AnnotationType.TRANSCRIPT, AnnotationType.TITLE, AnnotationType.MEMORY]
)
@pytest.mark.parametrize("stage", ["entry", "lookup", "save", "allowed"])
async def test_accepting_suggestion_checks_before_applying(
    evidence, mutations, kind, stage
):
    mutations.annotation.annotation_type = kind
    if kind == AnnotationType.MEMORY:
        mutations.annotation.conversation_id = None
        mutations.annotation.memory_id = "Topics/Synthetic.md"
        await evidence.db.conversations.update_one(
            {}, {"$set": {"vault_paths": ["Topics/Synthetic.md"]}}
        )
    if stage != "entry":
        await allow(evidence)
    if stage == "lookup":

        async def lookup(*args):
            await revoke(evidence)
            return evidence.conv

        mutations.lookup.side_effect = lookup
        if kind == AnnotationType.MEMORY:
            # The memory provider is the async boundary in this branch.
            mutations.memory.update_memory.side_effect = mutations.revoke
    if stage == "save":
        evidence.conv.save.side_effect = mutations.revoke
        if kind == AnnotationType.MEMORY:
            mutations.memory.update_memory.side_effect = mutations.revoke
    if stage == "allowed":
        await routes.update_annotation_status(
            "synthetic-annotation", AnnotationStatus.ACCEPTED, mutations.user
        )
        mutations.saved.assert_awaited_once()
        if kind == AnnotationType.MEMORY:
            mutations.memory.update_memory.assert_awaited_once()
        else:
            evidence.conv.save.assert_awaited_once()
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await routes.update_annotation_status(
                "synthetic-annotation", AnnotationStatus.ACCEPTED, mutations.user
            )
        mutations.saved.assert_not_awaited()
        if stage == "entry":
            evidence.conv.save.assert_not_awaited()
            mutations.memory.update_memory.assert_not_awaited()
        if stage == "lookup" and kind != AnnotationType.MEMORY:
            evidence.conv.save.assert_not_awaited()


async def test_policy_transition_waits_for_in_progress_annotation_save(
    evidence, mutations
):
    import asyncio
    from contextlib import asynccontextmanager

    await allow(evidence)
    save_entered, finish_save, transition_attempted = (
        asyncio.Event() for _ in range(3)
    )
    ordering = []

    async def save():
        save_entered.set()
        await finish_save.wait()
        ordering.append("saved")

    mutations.saved.side_effect = save
    edit = asyncio.create_task(
        routes.create_transcript_annotation(creation_data(), mutations.user)
    )
    await asyncio.wait_for(save_entered.wait(), 2)

    async def change_policy():
        transition_attempted.set()
        await privacy.begin_update(
            "evidence-owner",
            {"source_id": "screenpipe-test"},
            {"$inc": {"privacy_revision": 1}},
        )
        ordering.append("policy_changed")

    transition = asyncio.create_task(change_policy())
    try:
        await asyncio.wait_for(transition_attempted.wait(), 2)
        assert not transition.done()
    finally:
        finish_save.set()
        outcomes = await asyncio.gather(edit, transition, return_exceptions=True)
    assert ordering == ["saved", "policy_changed"]
    # Either the response precedes the revision change or its final check holds it.
    assert not isinstance(outcomes[1], BaseException)
    assert not isinstance(outcomes[0], BaseException) or isinstance(
        outcomes[0], privacy.PrivacyHeld
    )
