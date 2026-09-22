"""Annotation entry points must not expose or analyze held transcript copies."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.models.annotation import AnnotationSource, AnnotationStatus, AnnotationType
from backend.routers.modules import annotation_routes as routes
from backend.services import privacy
from backend.workers import annotation_jobs as jobs


class Field:
    def __eq__(self, value):
        return {}

    def __ne__(self, value):
        return {}

    def __ge__(self, value):
        return {}


class Query:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self):
        return self.rows


@pytest.fixture
async def annotations(evidence, monkeypatch):
    item = SimpleNamespace(
        id="synthetic-annotation",
        user_id="review-admin",
        conversation_id="synthetic-recording",
        memory_id=None,
        annotation_type=AnnotationType.TRANSCRIPT,
        segment_index=0,
        original_text="Synthetic copied text",
        corrected_text="Synthetic correction",
        status=AnnotationStatus.PENDING,
        source=AnnotationSource.MODEL_SUGGESTION,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    model = SimpleNamespace(
        **{
            k: Field()
            for k in [
                "annotation_type",
                "user_id",
                "conversation_id",
                "source",
                "status",
                "memory_id",
            ]
        },
        find=lambda *args: Query([item]),
    )
    monkeypatch.setattr(routes, "Annotation", model)
    monkeypatch.setattr(
        routes,
        "Conversation",
        SimpleNamespace(find=lambda *args: Query([evidence.conv])),
    )
    return item


@pytest.mark.parametrize(
    "entry",
    [
        "get_transcript_annotations",
        "get_insert_annotations",
        "get_title_annotations",
        "get_timing_annotations",
        "get_deletion_annotations",
        "get_diarization_annotations",
    ],
)
async def test_annotation_lists_follow_capture_owner(evidence, annotations, entry):
    function = getattr(routes, entry)
    args = dict(
        conversation_id="synthetic-recording",
        current_user=SimpleNamespace(user_id="review-admin"),
    )
    assert await function(**args) == []
    await allow(evidence)
    result = await function(**args)
    assert len(result) == 1 and result[0].original_text == "Synthetic copied text"


async def test_suggestions_filter_before_building_context(evidence, annotations):
    user = SimpleNamespace(user_id="review-admin")
    assert await routes.get_pending_suggestions(current_user=user, limit=20) == []
    await allow(evidence)
    rows = await routes.get_pending_suggestions(current_user=user, limit=20)
    assert len(rows) == 1 and rows[0]["original_text"] == "Synthetic copied text"


async def test_annotation_list_does_not_swallow_privacy_revision_change(
    evidence, annotations, monkeypatch
):
    await allow(evidence)
    original = privacy.ConversationPrivacyFilter.filter

    async def filter_then_revoke(self, rows):
        result = await original(self, rows)
        await revoke(evidence)
        return result

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "filter", filter_then_revoke)
    with pytest.raises(privacy.PrivacyHeld):
        await routes.get_transcript_annotations(
            "synthetic-recording", SimpleNamespace(user_id="review-admin")
        )


async def test_memory_annotation_copy_is_held_with_its_note(evidence, annotations):
    annotations.conversation_id = None
    annotations.memory_id = "Topics/Synthetic.md"
    annotations.annotation_type = AnnotationType.MEMORY
    annotations.user_id = "evidence-owner"
    await evidence.db.conversations.update_one(
        {}, {"$set": {"vault_paths": ["Topics/Synthetic.md"]}}
    )
    result = await routes.get_memory_annotations(
        "Topics/Synthetic.md", SimpleNamespace(user_id="evidence-owner")
    )
    assert result == []
    await allow(evidence)
    result = await routes.get_memory_annotations(
        "Topics/Synthetic.md", SimpleNamespace(user_id="evidence-owner")
    )
    assert len(result) == 1


@pytest.mark.parametrize("stage", ["entry", "prompt", "response", "allowed"])
async def test_suggestion_cron_holds_before_llm_and_before_save(
    evidence, monkeypatch, stage, caplog
):
    if stage != "entry":
        await allow(evidence)
    monkeypatch.setattr(
        jobs,
        "User",
        SimpleNamespace(
            find_all=lambda: Query([SimpleNamespace(id="evidence-owner", email=None)])
        ),
    )
    monkeypatch.setattr(
        jobs,
        "Conversation",
        SimpleNamespace(
            user_id=Field(),
            created_at=Field(),
            deleted=Field(),
            find=lambda *args: Query([evidence.conv]),
        ),
    )
    saved = AsyncMock()

    class Annotation:
        user_id = source = status = Field()

        @staticmethod
        def find(*args):
            return Query([])

        def __init__(self, **kwargs):
            self.save = saved

    monkeypatch.setattr(jobs, "Annotation", Annotation)

    async def prompt(*args, **kwargs):
        if stage == "prompt":
            await revoke(evidence)
        return "Synthetic prompt"

    monkeypatch.setattr(
        jobs, "get_prompt_registry", lambda: SimpleNamespace(get_prompt=prompt)
    )

    async def generate(*args):
        if stage == "response":
            await revoke(evidence)
        return json.dumps(
            [
                dict(
                    segment_index=0,
                    original_text="Synthetic original",
                    corrected_text="Synthetic correction",
                    reason="Synthetic private reason",
                )
            ]
        )

    llm = AsyncMock(side_effect=generate)
    monkeypatch.setattr(jobs, "async_generate", llm)
    result = await jobs.surface_error_suggestions()
    assert result["created"] == (1 if stage == "allowed" else 0)
    assert result["privacy_held"] == (0 if stage == "allowed" else 1)
    assert "Synthetic private reason" not in caplog.text
    assert "Synthetic title" not in caplog.text
    if stage != "allowed":
        saved.assert_not_awaited()
    if stage in {"entry", "prompt"}:
        llm.assert_not_awaited()
