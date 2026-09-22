"""Materialization retries retain immutable identity after semantic trimming."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.controllers import audio_controller as controller
from backend.models import audio_capture
from backend.models.audio_capture import AudioRangeRef


@pytest.fixture
def replay(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original = AudioRangeRef(
        capture_source_id="test-device:input",
        time_basis="captured",
        chunk_ids=["test-chunk-1", "test-chunk-2"],
        capture_session_ids=["test-session"],
        started_at=start,
        ended_at=start + timedelta(minutes=10),
    )
    trimmed = original.model_copy(
        update={
            "chunk_ids": ["test-chunk-2"],
            "started_at": start + timedelta(minutes=9),
        }
    )
    conversation = SimpleNamespace(
        user_id="test-user",
        conversation_id="test-conversation",
        audio_ranges=[trimmed],
        active_transcript_revision_id="test-revision",
        processing_enqueued_at=start,
        save=AsyncMock(),
    )
    revision = SimpleNamespace(
        revision_id="test-revision",
        conversation_id="test-conversation",
        transcript_artifact_ids=["test-artifact"],
        metadata={
            "audio_projection": {
                "operation": "silence_trim",
                "audio_ranges": [trimmed.model_dump(mode="json")],
            }
        },
    )
    artifact = SimpleNamespace(
        artifact_id="test-artifact",
        user_id="test-user",
        status="complete",
        audio_ranges=[original],
    )

    async def find_revision(query):
        return (
            revision
            if all(getattr(revision, k) == v for k, v in query.items())
            else None
        )

    async def find_artifact(query):
        return (
            artifact
            if all(getattr(artifact, k) == v for k, v in query.items())
            else None
        )

    monkeypatch.setattr(
        controller,
        "Conversation",
        SimpleNamespace(
            segmentation_key="segmentation_key",
            find_one=AsyncMock(return_value=conversation),
        ),
    )
    monkeypatch.setattr(
        audio_capture.ConversationTranscriptRevision, "find_one", find_revision
    )
    monkeypatch.setattr(audio_capture.TranscriptArtifact, "find_one", find_artifact)
    monkeypatch.setattr(controller, "generate_client_id", lambda *_: "test-client")
    apply = AsyncMock(side_effect=AssertionError("retry must not change claims"))
    enqueue = Mock(side_effect=AssertionError("retry must not enqueue"))
    monkeypatch.setattr(controller, "apply_audio_ranges", apply)
    monkeypatch.setattr(controller, "_batch_transcription_job", enqueue)
    monkeypatch.setattr(controller, "start_post_conversation_jobs", enqueue)

    async def run():
        return await controller.materialize_and_process_audio_claim(
            SimpleNamespace(user_id="test-user"),
            original,
            device_name="test-device",
            title="Synthetic recording",
            segmentation_key="test-segmentation",
            external_source_id="test-source",
            external_source_type="screenpipe",
        )

    return SimpleNamespace(
        run=run,
        original=original,
        conversation=conversation,
        revision=revision,
        artifact=artifact,
        apply=apply,
        enqueue=enqueue,
    )


@pytest.mark.asyncio
async def test_trimmed_materialization_retry_preserves_claim_and_jobs(replay):
    before = [item.model_dump() for item in replay.conversation.audio_ranges]
    assert await replay.run() is replay.conversation
    assert [item.model_dump() for item in replay.conversation.audio_ranges] == before
    replay.conversation.save.assert_not_awaited()
    replay.apply.assert_not_awaited()
    replay.enqueue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "revision_owner",
        "artifact_owner",
        "artifact_pending",
        "missing_revision",
        "missing_artifact",
        "wrong_operation",
        "changed_projection",
        "malformed_projection",
        "different_source",
        "different_chunks",
        "different_start",
        "different_session",
    ],
)
async def test_trimmed_retry_requires_exact_immutable_evidence(replay, invalid):
    if invalid == "revision_owner":
        replay.revision.conversation_id = "another-conversation"
    elif invalid == "artifact_owner":
        replay.artifact.user_id = "another-user"
    elif invalid == "artifact_pending":
        replay.artifact.status = "pending"
    elif invalid == "missing_revision":
        replay.conversation.active_transcript_revision_id = None
    elif invalid == "missing_artifact":
        replay.revision.transcript_artifact_ids = []
    elif invalid == "wrong_operation":
        replay.revision.metadata["audio_projection"]["operation"] = "merge"
    elif invalid == "changed_projection":
        replay.revision.metadata["audio_projection"]["audio_ranges"] = [
            replay.original.model_dump(mode="json")
        ]
    elif invalid == "malformed_projection":
        replay.revision.metadata["audio_projection"]["audio_ranges"] = [None]
    else:
        changes = {
            "different_source": {"capture_source_id": "another-device:input"},
            "different_chunks": {"chunk_ids": ["different-chunk"]},
            "different_start": {
                "started_at": replay.original.started_at + timedelta(seconds=1)
            },
            "different_session": {"capture_session_ids": ["another-session"]},
        }
        replay.artifact.audio_ranges = [
            replay.original.model_copy(update=changes[invalid])
        ]
    with pytest.raises(ValueError, match="different audio claim"):
        await replay.run()
    replay.conversation.save.assert_not_awaited()
    replay.enqueue.assert_not_called()
