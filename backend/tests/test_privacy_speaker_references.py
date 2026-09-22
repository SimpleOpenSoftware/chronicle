"""The registered speaker job retains background-reference policy until commit."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_background_audio import background, reference  # noqa: F401
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.models import job as job_model
from backend.services import privacy
from backend.services.speaker_gallery_privacy import GalleryRead, GalleryResult
from backend.workers import speaker_jobs as jobs


@pytest.mark.parametrize(
    "change", ["matching", "ledger", "publication", "allowed", "later"]
)
async def test_registered_speaker_job_retains_background_reference_fence(
    evidence,
    background,
    monkeypatch,
    change,
):
    await allow(evidence)
    await reference(evidence)
    target = dict(
        evidence.row, conversation_id="synthetic-target", client_id="ordinary-device"
    )
    await evidence.db.conversations.insert_one(target)
    version = jobs.Conversation.TranscriptVersion(
        version_id="synthetic-version",
        transcript="Synthetic spoken words",
        segments=[
            jobs.Conversation.SpeakerSegment(
                start=0,
                end=3,
                text="Synthetic spoken words",
                speaker="Speaker 0",
            )
        ],
        words=[
            jobs.Conversation.Word(word=word, start=i, end=i + 1)
            for i, word in enumerate(["Synthetic", "spoken", "words"])
        ],
        provider="synthetic",
        created_at=datetime.now(timezone.utc),
        diarization_source="provider",
        metadata={"provider_capabilities": {"diarization": True}},
    )
    conversation = SimpleNamespace(
        **target,
        audio_ranges=[],
        audio_total_duration=10,
        get_transcript_version=lambda _: version,
        active_transcript=version,
        transcript_versions=[version],
        transcript_integrity_error=None,
        save=AsyncMock(),
        apply_status=lambda **kwargs: None,
    )
    catalog = {
        "catalog_id": "c" * 32,
        "revision": "a" * 64,
        "unverified_speaker_ids": [],
        "held_speaker_ids": [],
        "active_operation_ids": [],
    }
    client = SimpleNamespace(
        enabled=True,
        service_url="http://synthetic.invalid",
        enrollment_catalog=AsyncMock(return_value=catalog),
    )

    async def identify(**kwargs):
        scope = GalleryRead(client, user_id="evidence-owner")
        await scope.start()
        return GalleryResult(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 3.0,
                        "text": "Synthetic spoken words",
                        "speaker": "Speaker 0",
                        "identified_as": None,
                        "confidence": 0.0,
                        "_evaluation_embedding": [1.0, 0.0],
                        "_embedding_model": "synthetic-model",
                    }
                ]
            },
            scope,
        )

    client.identify_provider_segments = AsyncMock(side_effect=identify)
    monkeypatch.setattr(jobs, "SpeakerRecognitionClient", lambda: client)
    monkeypatch.setattr(
        jobs,
        "Conversation",
        SimpleNamespace(
            conversation_id="conversation_id",
            find_one=AsyncMock(return_value=conversation),
            SpeakerSegment=jobs.Conversation.SpeakerSegment,
            Word=jobs.Conversation.Word,
        ),
    )
    monkeypatch.setattr(jobs, "get_user_by_id", AsyncMock(return_value=background.user))
    monkeypatch.setattr(
        jobs, "load_transcript_audio_ranges", AsyncMock(return_value=[(0.0, 10.0)])
    )
    monkeypatch.setattr(
        jobs, "get_diarization_settings", lambda: {"diarization_source": "provider"}
    )
    monkeypatch.setattr(jobs, "get_misc_settings", lambda: {})
    monkeypatch.setattr(
        jobs, "compute_cluster_centroids", AsyncMock(return_value=({}, {}))
    )
    monkeypatch.setattr(
        jobs, "resolve_transcript_artifact_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(jobs, "note_conversation_dirty", AsyncMock())
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(
        jobs.background_suppression,
        "get_subject_override",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        jobs.background_suppression, "load_sticky_segments", AsyncMock(return_value={})
    )
    ledger = AsyncMock()
    if change == "ledger":
        ledger.side_effect = privacy.PrivacyHeld()
    monkeypatch.setattr(
        jobs.background_suppression, "record_conversation_suppressions", ledger
    )
    actual_match = jobs.background_bucket_controller.match_embeddings

    async def match(*args, **kwargs):
        if change == "matching":
            raise privacy.PrivacyHeld()
        return await actual_match(*args, **kwargs)

    monkeypatch.setattr(jobs.background_bucket_controller, "match_embeddings", match)

    async def annotations(*args):
        if change == "publication":
            await revoke(evidence)
        return []

    monkeypatch.setattr(jobs, "_human_speaker_annotations", annotations)
    artifact = AsyncMock(return_value=SimpleNamespace(artifact_id="synthetic-artifact"))
    revision = AsyncMock(return_value=SimpleNamespace(revision_id="synthetic-revision"))
    monkeypatch.setattr(jobs, "persist_diarization_artifact", artifact)
    monkeypatch.setattr(jobs, "persist_conversation_revision", revision)
    if change in {"allowed", "later"}:
        result = await asyncio.to_thread(
            jobs.recognise_speakers_job, "synthetic-target", "synthetic-version"
        )
        assert result["success"]
        artifact.assert_awaited_once()
        conversation.save.assert_awaited_once()
        if change == "later":
            saved = {
                "user_id": "evidence-owner",
                "configuration": artifact.await_args.kwargs["configuration"],
            }
            await privacy.require_record(saved)
            await revoke(evidence)
            with pytest.raises(privacy.PrivacyHeld):
                await privacy.require_record(saved)
    else:
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                jobs.recognise_speakers_job, "synthetic-target", "synthetic-version"
            )
        artifact.assert_not_awaited()
        revision.assert_not_awaited()
        conversation.save.assert_not_awaited()
        if change == "matching":
            ledger.assert_not_awaited()
