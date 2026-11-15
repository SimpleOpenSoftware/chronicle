"""An unreachable speaker service must not run speaker recognition.

Before this check, the job reached the enrollment-catalog call, which failed on
DNS and was reported as ``PrivacyHeld``: a misleading failure for what is only a
missing service. The automatic chain now skips; a manual reprocess fails clearly.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.models import job as job_model
from backend.workers import speaker_jobs as jobs


def _version(version_id="v1"):
    return jobs.Conversation.TranscriptVersion(
        version_id=version_id,
        transcript="Synthetic spoken words",
        segments=[
            jobs.Conversation.SpeakerSegment(
                start=0, end=3, text="Synthetic spoken words", speaker="Speaker 0"
            )
        ],
        words=[
            jobs.Conversation.Word(word=word, start=i, end=i + 1)
            for i, word in enumerate(["Synthetic", "spoken", "words"])
        ],
        provider="synthetic",
        created_at=datetime.now(timezone.utc),
        metadata={},
    )


@pytest.fixture
def unreachable(monkeypatch):
    source = _version("source")
    conversation = SimpleNamespace(
        conversation_id="c1",
        user_id="u1",
        client_id="u1-phone",
        audio_ranges=[],
        audio_total_duration=10,
        get_transcript_version=lambda vid: source if vid == "source" else None,
        active_transcript=source,
        transcript_integrity_error=None,
        save=AsyncMock(),
    )
    client = SimpleNamespace(
        enabled=True,
        service_url="http://speaker.example.ts.net:8085",
        health_check=AsyncMock(return_value=False),
        enrollment_catalog=AsyncMock(),
        identify_provider_segments=AsyncMock(),
        diarize_identify_match=AsyncMock(),
    )
    events = Mock()
    snapshot = SimpleNamespace(permits_record=lambda _row: True)

    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
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
    monkeypatch.setattr(jobs.privacy, "load_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        jobs, "load_transcript_audio_ranges", AsyncMock(return_value=[(0.0, 10.0)])
    )
    monkeypatch.setattr(jobs, "record_event_sync", events)
    return SimpleNamespace(client=client, events=events)


async def test_automatic_chain_skips_without_calling_the_service(unreachable):
    result = await asyncio.to_thread(jobs.recognise_speakers_job, "c1", "source")

    assert result["success"] is True
    assert result["skip_reason"] == "Speaker service unavailable"
    unreachable.client.enrollment_catalog.assert_not_awaited()
    unreachable.client.identify_provider_segments.assert_not_awaited()
    unreachable.client.diarize_identify_match.assert_not_awaited()
    (call,) = unreachable.events.call_args_list
    assert call.kwargs["incident_key"] == "speaker-service-unavailable"


async def test_manual_reprocess_fails_clearly(unreachable):
    with pytest.raises(jobs.SpeakerReprocessFailed, match="not reachable"):
        await asyncio.to_thread(
            jobs.recognise_speakers_job, "c1", "new-version", source_version_id="source"
        )

    unreachable.client.enrollment_catalog.assert_not_awaited()
