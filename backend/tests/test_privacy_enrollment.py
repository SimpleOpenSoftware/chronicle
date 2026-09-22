"""Exercise manual and scheduled enrollment with synthetic evidence only."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mongomock_motor import AsyncMongoMockClient

from backend.models.annotation import AnnotationType
from backend.routers.modules import finetuning_routes as routes
from backend.services import privacy
from backend.workers import finetuning_jobs as jobs

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
async def evidence(monkeypatch):
    db = AsyncMongoMockClient().privacy_enrollment
    monkeypatch.setattr(privacy, "database", lambda: db)
    await db.capture_sources.insert_one(
        dict(
            user_id="evidence-owner",
            source_id="screenpipe-test",
            privacy_enabled_from=START,
            privacy_revision=1,
        )
    )
    row = dict(
        conversation_id="synthetic-recording",
        user_id="evidence-owner",
        client_id="screenpipe-test",
        created_at=START,
        ended_at=START + timedelta(seconds=10),
    )
    await db.conversations.insert_one(dict(row))
    conv = SimpleNamespace(
        **row,
        title="Synthetic title",
        audio_total_duration=10,
        active_transcript=SimpleNamespace(
            segments=[
                SimpleNamespace(
                    start=0.0,
                    end=10.0,
                    speaker="Synthetic speaker",
                    text="Synthetic text",
                )
            ]
        ),
    )
    conv.model_dump = lambda: dict(row)
    annotation = SimpleNamespace(
        conversation_id=conv.conversation_id,
        annotation_type=AnnotationType.DIARIZATION,
        processed_by="apply",
        corrected_speaker="Synthetic speaker",
        segment_index=0,
        segment_start_time=0.0,
        source="user",
        save=AsyncMock(),
        delete=AsyncMock(),
    )
    query = SimpleNamespace(to_list=AsyncMock(return_value=[annotation]))
    for module in (routes, jobs):
        monkeypatch.setattr(
            module,
            "Annotation",
            SimpleNamespace(
                annotation_type="annotation_type",
                processed="processed",
                source="source",
                find=lambda *a, **k: query,
            ),
        )
        monkeypatch.setattr(
            module,
            "Conversation",
            SimpleNamespace(
                conversation_id="conversation_id",
                find_one=AsyncMock(return_value=conv),
                find=lambda *a, **k: SimpleNamespace(
                    to_list=AsyncMock(return_value=[conv])
                ),
            ),
        )
    client = SimpleNamespace(
        enabled=True,
        get_speaker_by_name=AsyncMock(return_value=None),
        enroll_new_speaker=AsyncMock(return_value={"status": "enrolled"}),
        append_to_speaker=AsyncMock(return_value={"status": "enrolled"}),
    )
    reconstruct = AsyncMock(return_value=b"synthetic audio")
    for module in (routes, jobs):
        monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: client)
        monkeypatch.setattr(module, "reconstruct_audio_segment", reconstruct)
    return SimpleNamespace(
        db=db,
        row=row,
        conv=conv,
        annotation=annotation,
        client=client,
        reconstruct=reconstruct,
    )


async def allow(evidence):
    await evidence.db.privacy_overrides.insert_one(
        dict(
            source_id="screenpipe-test",
            user_id="evidence-owner",
            started_at=START,
            ended_at=START + timedelta(seconds=10),
            override="allowed",
        )
    )


async def revoke(evidence):
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    await evidence.db.privacy_overrides.delete_many({})


async def invoke(entry):
    if entry == "scheduled":
        return await jobs.run_speaker_finetuning_job()
    response = await routes.enroll_selected_clips(
        routes.EnrollSelectedRequest(
            clips=[
                routes.SelectedClip(
                    conversation_id="synthetic-recording",
                    segment_index=0,
                    start=0.0,
                    end=10.0,
                    speaker="Synthetic speaker",
                )
            ]
        ),
        current_user=SimpleNamespace(user_id="review-admin", is_superuser=True),
    )
    return json.loads(response.body)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("entry", ["manual", "scheduled"])
@pytest.mark.parametrize("stage", ["entry", "decode", "lookup", "result"])
async def test_enrollment_holds_before_egress_and_before_marking_trained(
    evidence, entry, stage, existing
):
    evidence.client.get_speaker_by_name.return_value = (
        {"id": "synthetic-speaker"} if existing else None
    )
    if stage != "entry":
        await allow(evidence)
    if stage == "decode":

        async def decode(**kwargs):
            await revoke(evidence)
            return b"synthetic audio"

        evidence.reconstruct.side_effect = decode
    if stage == "lookup":

        async def lookup(**kwargs):
            await revoke(evidence)
            return None

        evidence.client.get_speaker_by_name.side_effect = lookup
    if stage == "result":

        async def enroll(**kwargs):
            await revoke(evidence)
            return {"status": "enrolled"}

        target = (
            evidence.client.append_to_speaker
            if existing
            else evidence.client.enroll_new_speaker
        )
        target.side_effect = enroll
    result = await invoke(entry)
    assert result["privacy_held"] == 1
    evidence.annotation.save.assert_not_awaited()
    evidence.annotation.delete.assert_not_awaited()
    if stage == "entry":
        evidence.reconstruct.assert_not_awaited()
    if stage in {"entry", "decode"}:
        evidence.client.get_speaker_by_name.assert_not_awaited()
    if stage != "result":
        evidence.client.enroll_new_speaker.assert_not_awaited()
    if stage != "result" or not existing:
        evidence.client.append_to_speaker.assert_not_awaited()


@pytest.mark.parametrize("entry", ["manual", "scheduled"])
async def test_allowed_audio_can_still_be_enrolled(evidence, entry):
    await allow(evidence)
    result = await invoke(entry)
    assert result["privacy_held"] == 0
    evidence.client.enroll_new_speaker.assert_awaited_once()
    evidence.annotation.save.assert_awaited_once()


async def test_candidate_listing_filters_by_evidence_owner_not_admin(evidence):
    args = dict(
        current_user=SimpleNamespace(user_id="review-admin", is_superuser=True),
        min_duration=3.0,
        include_identified=False,
    )
    result = json.loads((await routes.get_enrollment_candidates(**args)).body)
    assert result["candidates"] == []
    assert result["conversation_count"] == 0
    assert "Synthetic" not in json.dumps(result)
    await allow(evidence)
    result = json.loads((await routes.get_enrollment_candidates(**args)).body)
    assert result["conversation_count"] == 1
    assert result["candidates"][0]["clips"][0]["text"] == "Synthetic text"


async def test_candidate_listing_rechecks_revision_before_returning(
    evidence, monkeypatch
):
    await allow(evidence)
    original = privacy.ConversationPrivacyFilter.filter

    async def filter_then_revoke(self, rows):
        result = await original(self, rows)
        await revoke(evidence)
        return result

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "filter", filter_then_revoke)
    with pytest.raises(privacy.PrivacyHeld):
        await routes.get_enrollment_candidates(
            current_user=SimpleNamespace(user_id="review-admin", is_superuser=True),
            min_duration=3.0,
            include_identified=False,
        )
