"""Dynamic ASR vocabulary retains exact original evidence through publication."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_asr_context import Redis
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401

from backend.models import job as job_model
from backend.services import privacy
from backend.services.transcription import context
from backend.workers import conversation_jobs
from backend.workers import transcription_jobs as jobs


async def note_receipt(evidence):
    await allow(evidence)
    await evidence.db.memory_audit.insert_one(
        {
            "user_id": "evidence-owner",
            "note_path": "Topics/Synthetic.md",
            "conversation_id": "synthetic-recording",
        }
    )
    return await privacy.vault_reference_receipt(
        "evidence-owner",
        ["Topics/Synthetic.md"],
        snapshot=await privacy.load_snapshot("evidence-owner"),
    )


@pytest.mark.parametrize("location", ["metadata", "raw_response", "version"])
async def test_saved_context_keeps_original_capture_after_retarget(evidence, location):
    receipt = await note_receipt(evidence)
    payload = {"privacy_reference_receipt": receipt}
    row = {"user_id": "evidence-owner", "client_id": "ordinary-device"}
    if location == "version":
        row["transcript_versions"] = [{"metadata": payload}]
    else:
        row[location] = payload
    await privacy.require_record(row)
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    await privacy.require_record(row)  # revision alone does not taint saved results
    await evidence.db.conversations.update_one(
        {}, {"$set": {"client_id": "ordinary-device"}}
    )
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)


async def test_mixed_note_with_missing_original_is_held(evidence):
    await note_receipt(evidence)
    await evidence.db.conversations.delete_many({})
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.vault_reference_receipt(
            "evidence-owner",
            ["Topics/Synthetic.md"],
            snapshot=await privacy.load_snapshot("evidence-owner"),
        )


async def test_context_cache_rejects_unsealed_and_revoked_references(evidence):
    receipt = await note_receipt(evidence)
    redis = Redis()
    snapshot = await privacy.load_snapshot("evidence-owner")
    key = context.jargon_cache_key("evidence-owner", snapshot)
    redis.values[key] = "Synthetic unsealed vocabulary"
    with pytest.raises(privacy.PrivacyHeld):
        await context.cached_jargon("evidence-owner", redis)
    redis.values[key] = json.dumps(
        {"text": "Synthetic terms", "privacy_reference_receipt": receipt}
    )
    assert (await context.cached_jargon("evidence-owner", redis))[2] == receipt
    await evidence.db.privacy_reference_dependencies.delete_many({})
    with pytest.raises(privacy.PrivacyHeld):
        await context.cached_jargon("evidence-owner", redis)


@pytest.mark.parametrize("stage", ["allowed", "provider", "plugin", "later"])
async def test_registered_transcription_job_persists_context_dependencies(
    evidence, monkeypatch, stage
):
    receipt = await note_receipt(evidence)
    snapshot = await privacy.load_snapshot("evidence-owner")
    target = dict(
        evidence.row, conversation_id="synthetic-target", client_id="ordinary-device"
    )
    await evidence.db.conversations.insert_one(deepcopy(target))
    versions = []
    conversation = SimpleNamespace(
        **target,
        audio_ranges=[],
        audio_total_duration=10.0,
        active_transcript=None,
        transcript_versions=versions,
        title="Synthetic title",
        summary="",
        memory_excluded=False,
        memory_space_id=None,
        get_transcript_version=lambda _: versions[-1],
    )

    def add(**kwargs):
        kwargs.pop("set_as_active")
        version = SimpleNamespace(**kwargs, diarization_source=None)
        versions.append(version)
        return version

    conversation.add_transcript_version = add

    async def save():
        if versions:
            await evidence.db.conversations.update_one(
                {"conversation_id": "synthetic-target"},
                {
                    "$set": {
                        "transcript_versions": [{"metadata": versions[-1].metadata}]
                    }
                },
            )

    conversation.save = AsyncMock(side_effect=save)
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
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "gather_transcription_context",
        AsyncMock(
            return_value=context.TranscriptionContext(
                user_jargon="Synthetic vocabulary",
                privacy_checks=[("evidence-owner", snapshot)],
                reference_receipt=receipt,
            )
        ),
    )

    async def transcribe(**kwargs):
        assert kwargs["context_info"] == "Synthetic vocabulary"
        if stage == "provider":
            await revoke(evidence)
        return {
            "text": "Synthetic spoken words",
            "segments": [{"start": 0.0, "end": 3.0, "text": "Synthetic spoken words"}],
            "words": [
                {"start": i, "end": i + 1, "word": word}
                for i, word in enumerate(["Synthetic", "spoken", "words"])
            ],
            "provider_name": "synthetic",
            "provider_capabilities": {},
            "wav_size": 32,
        }

    monkeypatch.setattr(
        jobs, "transcribe_audio_range", AsyncMock(side_effect=transcribe)
    )
    monkeypatch.setattr(
        jobs, "load_transcript_audio_ranges", AsyncMock(return_value=[(0.0, 10.0)])
    )
    monkeypatch.setattr(
        jobs, "get_diarization_settings", lambda: {"diarization_source": "provider"}
    )
    monkeypatch.setattr(
        jobs,
        "analyze_speech",
        lambda _: {"has_speech": True, "word_count": 3, "duration": 3.0},
    )
    artifact = AsyncMock(return_value=SimpleNamespace(artifact_id="synthetic-artifact"))
    monkeypatch.setattr(jobs, "persist_transcript_artifact", artifact)
    monkeypatch.setattr(
        jobs,
        "persist_conversation_revision",
        AsyncMock(return_value=SimpleNamespace(revision_id="synthetic-revision")),
    )
    monkeypatch.setattr(jobs, "_settle_audio_evidence_span", AsyncMock())
    monkeypatch.setattr(jobs, "note_conversation_dirty", AsyncMock())
    monkeypatch.setattr(jobs, "update_job_meta", lambda **kwargs: None)
    monkeypatch.setattr(
        conversation_jobs, "maybe_trim_silence", AsyncMock(return_value=None)
    )

    async def plugin(**kwargs):
        assert kwargs["metadata"]["privacy_reference_receipt"] == receipt
        if stage == "plugin":
            await revoke(evidence)
            raise privacy.PrivacyHeld()

    dispatch = AsyncMock(side_effect=plugin)
    monkeypatch.setattr(jobs, "dispatch_or_defer_space_event", dispatch)
    if stage in {"provider", "plugin"}:
        with pytest.raises(privacy.PrivacyHeld):
            await asyncio.to_thread(
                jobs.transcribe_full_audio_job, "synthetic-target", "synthetic-version"
            )
        conversation.save.assert_not_awaited()
        if stage == "provider":
            artifact.assert_not_awaited()
            dispatch.assert_not_awaited()
    else:
        result = await asyncio.to_thread(
            jobs.transcribe_full_audio_job, "synthetic-target", "synthetic-version"
        )
        assert result["success"]
        assert versions[-1].metadata["privacy_reference_receipt"] == receipt
        raw = artifact.await_args.kwargs["raw_response"]
        assert raw["privacy_reference_receipt"] == receipt
        await privacy.require_conversation("synthetic-target")
        if stage == "later":
            await revoke(evidence)
            with pytest.raises(privacy.PrivacyHeld):
                await privacy.require_conversation("synthetic-target")
            with pytest.raises(privacy.PrivacyHeld):
                await privacy.require_record(
                    {"user_id": "evidence-owner", "raw_response": raw}
                )
