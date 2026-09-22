import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mongomock_motor import AsyncMongoMockClient

from backend.controllers import data_audit_controller as controller
from backend.services import privacy

START = datetime(2026, 9, 16, tzinfo=timezone.utc)


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient().privacy_exports
    monkeypatch.setattr(privacy, "database", lambda: database)
    await database.capture_sources.insert_one(
        {
            "source_id": "screenpipe-test",
            "user_id": "owner",
            "privacy_enabled_from": START,
            "privacy_revision": 1,
        }
    )
    return database


def conversation(source="screenpipe-test"):
    return dict(
        conversation_id="recording",
        user_id="owner",
        client_id=source,
        created_at=START,
        ended_at=START + timedelta(seconds=10),
        title="Synthetic private title",
        transcript="Synthetic private transcript",
        deleted=False,
        audio_archived=False,
        audio_chunks_count=1,
    )


def fake_conversation(monkeypatch, row):
    value = SimpleNamespace(**row)
    value.model_dump = lambda: dict(row)
    monkeypatch.setattr(
        controller,
        "Conversation",
        SimpleNamespace(
            conversation_id="recording",
            find_one=AsyncMock(return_value=value),
        ),
    )


def archive(tmp_path, monkeypatch, *, creator="admin"):
    metadata = {
        "created_by": creator,
        "export_id": "synthetic-export",
        "conversations": [
            {"conversation_id": "recording", "title": "Synthetic private title"}
        ],
    }
    folder = tmp_path / "synthetic-export"
    folder.mkdir()
    (folder / controller.META_NAME).write_text(json.dumps(metadata))
    (folder / controller.ZIP_NAME).write_bytes(b"a" * 65536 + b"b" * 65536)
    monkeypatch.setattr(controller, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(controller, "export_dir", lambda _: folder)
    monkeypatch.setattr(controller, "validate_export_id", lambda _: True)
    return metadata


async def test_admin_export_list_and_download_obey_evidence_owner(
    db, tmp_path, monkeypatch
):
    await db.conversations.insert_one(conversation())
    archive(tmp_path, monkeypatch)
    admin = SimpleNamespace(user_id="admin", is_superuser=True)
    assert await controller.list_exports(admin) == {"exports": []}
    response = await controller.download_export(admin, "synthetic-export")
    assert response.status_code == 423
    assert b"Synthetic private" not in response.body


async def test_export_without_canonical_provenance_remains_held(
    db, tmp_path, monkeypatch
):
    archive(tmp_path, monkeypatch)
    response = await controller.download_export(
        SimpleNamespace(user_id="admin", is_superuser=True), "synthetic-export"
    )
    assert response.status_code == 423


@pytest.mark.parametrize("superuser", [False, True])
async def test_preview_withholds_title_and_text_before_clip_planning(
    db, monkeypatch, superuser
):
    fake_conversation(monkeypatch, conversation())
    planner = AsyncMock(
        side_effect=AssertionError("Private content must not reach clip planning")
    )
    monkeypatch.setattr(controller, "plan_conversation_clips", planner)
    user = SimpleNamespace(
        user_id="admin" if superuser else "owner", is_superuser=superuser
    )
    result = await controller.preview_export(user, ["recording"])
    assert "Synthetic private" not in json.dumps(result)
    assert result["conversations"][0]["skipped_reason"].startswith(
        "Private or unscreened"
    )
    planner.assert_not_awaited()


async def test_audit_audio_detail_stops_before_chunk_lookup(db, monkeypatch):
    fake_conversation(monkeypatch, conversation())
    chunks = AsyncMock(side_effect=AssertionError("Private audio must not be read"))
    monkeypatch.setattr(controller, "_chunk_timeline", chunks)
    result = await controller.get_silence_gaps(
        SimpleNamespace(user_id="owner", is_superuser=False), "recording"
    )
    assert result.status_code == 423
    chunks.assert_not_awaited()


async def test_download_stops_on_revision_change_and_cannot_use_path_send(
    db, tmp_path, monkeypatch
):
    await db.capture_sources.insert_one(
        {
            "source_id": "another-device",
            "user_id": "owner",
            "privacy_revision": 1,
            "privacy_enabled_from": START + timedelta(days=1),
        }
    )
    await db.conversations.insert_one(conversation(source="another-device"))
    archive(tmp_path, monkeypatch)
    response = await controller.download_export(
        SimpleNamespace(user_id="admin", is_superuser=True), "synthetic-export"
    )
    messages = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            await db.capture_sources.update_one(
                {"user_id": "owner", "source_id": "another-device"},
                {"$inc": {"privacy_revision": 1}},
            )

    with pytest.raises(privacy.PrivacyHeld):
        await response(
            {
                "type": "http",
                "method": "GET",
                "headers": [],
                "extensions": {"http.response.pathsend": {}},
            },
            AsyncMock(),
            send,
        )
    assert all(m["type"] != "http.response.pathsend" for m in messages)
    assert b"".join(m.get("body", b"") for m in messages) == b"a" * 65536


async def test_download_preserves_range_response_for_allowed_other_device(
    db, tmp_path, monkeypatch
):
    await db.conversations.insert_one(conversation(source="another-device"))
    archive(tmp_path, monkeypatch)
    response = await controller.download_export(
        SimpleNamespace(user_id="admin", is_superuser=True), "synthetic-export"
    )
    messages = []

    async def send(message):
        messages.append(message)

    await response(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"range", b"bytes=65536-65539")],
        },
        AsyncMock(),
        send,
    )
    assert messages[0]["status"] == 206
    assert b"".join(m.get("body", b"") for m in messages) == b"bbbb"


@pytest.mark.parametrize("format", ["opus", "wav"])
async def test_audio_range_response_rechecks_after_reconstruction(
    db, monkeypatch, format
):
    from backend.routers.modules import audio_routes

    await db.capture_sources.insert_one(
        {
            "source_id": "another-device",
            "user_id": "owner",
            "privacy_revision": 1,
            "privacy_enabled_from": START + timedelta(days=1),
        }
    )
    row = conversation(source="another-device")
    row["audio_total_duration"] = 10
    conv = SimpleNamespace(**row)
    conv.model_dump = lambda: dict(row)
    monkeypatch.setattr(
        audio_routes,
        "Conversation",
        SimpleNamespace(
            conversation_id="recording",
            find_one=AsyncMock(return_value=conv),
        ),
    )

    async def reconstruct(*args):
        await db.capture_sources.update_one(
            {"user_id": "owner", "source_id": "another-device"},
            {"$inc": {"privacy_revision": 1}},
        )
        return b"Synthetic audio must not be published after the revision change"

    monkeypatch.setattr(audio_routes, "get_trimmed_opus_for_time_range", reconstruct)
    monkeypatch.setattr(audio_routes, "reconstruct_audio_segment", reconstruct)
    response = await audio_routes.get_audio_chunk_range(
        "recording",
        0,
        1,
        format,
        None,
        SimpleNamespace(user_id="owner", is_superuser=False),
    )
    messages = []

    async def send(message):
        messages.append(message)

    await response({"type": "http", "method": "GET", "headers": []}, AsyncMock(), send)
    assert messages[0]["status"] == 423
    assert b"Synthetic audio" not in b"".join(m.get("body", b"") for m in messages)


@pytest.fixture
async def audit_rows(db, monkeypatch):
    private = conversation()
    private.update(
        # The projection omits range claims: only the canonical document proves
        # this otherwise ordinary client contains held ScreenPipe evidence.
        client_id="ordinary-client",
        audio_ranges=[
            {
                "source_id": "screenpipe-test",
                "chunk_id": "synthetic-chunk",
                "started_at": START,
                "ended_at": START + timedelta(seconds=10),
            }
        ],
        audio_total_duration=10,
        external_source_type="annotation_dataset",
        external_source_id="private-dataset:clip",
        active_transcript_version="active",
        transcript_versions=[
            {
                "version_id": "active",
                "segments": [
                    {
                        "start": 0,
                        "end": 10,
                        "speaker": "Synthetic private speaker",
                        "identified_as": "Synthetic private speaker",
                        "confidence": 0.52,
                        "text": "Synthetic private transcript",
                        "segment_type": "speech",
                    }
                ],
            }
        ],
    )
    allowed = conversation(source="another-device")
    allowed.update(
        conversation_id="allowed",
        title="Ordinary control",
        audio_total_duration=10,
        external_source_type="annotation_dataset",
        external_source_id="allowed-dataset:clip",
        active_transcript_version="active",
        transcript_versions=[
            {
                "version_id": "active",
                "segments": [
                    {
                        "start": 0,
                        "end": 10,
                        "speaker": "Control speaker",
                        "identified_as": "Control speaker",
                        "confidence": 0.54,
                        "text": "Ordinary control text",
                        "segment_type": "speech",
                    }
                ],
            }
        ],
    )
    await db.conversations.insert_many([private, allowed])
    monkeypatch.setattr(
        controller.Conversation, "get_pymongo_collection", lambda: db.conversations
    )
    monkeypatch.setattr(
        controller.Annotation, "get_pymongo_collection", lambda: db.annotations
    )
    monkeypatch.setattr(controller, "_latest_exports_by_conversation", lambda _: {})
    monkeypatch.setattr(
        controller, "get_diarization_settings", lambda: {"similarity_threshold": 0.5}
    )
    monkeypatch.setattr(
        controller,
        "SpeakerRecognitionClient",
        lambda: SimpleNamespace(
            get_enrolled_speakers=AsyncMock(return_value={"speakers": []})
        ),
    )
    return private, allowed


@pytest.mark.parametrize("superuser", [False, True])
async def test_audit_listing_filters_canonical_ranges_before_facets_and_counts(
    db,
    audit_rows,
    superuser,
):
    result = await controller.list_for_audit(
        SimpleNamespace(
            user_id="admin" if superuser else "owner", is_superuser=superuser
        )
    )
    assert [r["conversation_id"] for r in result["conversations"]] == ["allowed"]
    assert result["speakers"] == ["Control speaker"]
    assert result["datasets"] == ["allowed-dataset"]
    assert result["unanalyzed_count"] == (0 if superuser else 1)
    assert "Synthetic private" not in json.dumps(result)


async def test_audit_statistics_and_review_batch_omit_held_evidence(db, audit_rows):
    admin = SimpleNamespace(user_id="admin", is_superuser=True)
    result = await controller.speaker_confidence_overview(admin)
    assert [r["name"] for r in result["speakers"]] == ["Control speaker"]
    assert result["total_identified"] == 1
    batch = await controller.next_speaker_label_reviews(admin)
    assert [r["conversation_id"] for r in batch["batch"]] == ["allowed"]
    assert "Synthetic private" not in json.dumps(batch)


async def test_review_metrics_apply_original_owner_policy_to_admin_reviews(
    db, audit_rows
):
    await db.speaker_label_reviews.insert_many(
        [
            {
                "user_id": "admin",
                "conversation_id": "recording",
                "review_key": "held",
                "claimed_speaker": "Synthetic private speaker",
                "verdict": "correct",
            },
            {
                "user_id": "admin",
                "conversation_id": "allowed",
                "review_key": "control",
                "claimed_speaker": "Control speaker",
                "verdict": "correct",
            },
            {
                "user_id": "admin",
                "conversation_id": "missing",
                "review_key": "missing",
                "claimed_speaker": "Unverifiable speaker",
                "verdict": "correct",
            },
        ]
    )
    admin = SimpleNamespace(user_id="admin", is_superuser=True)
    result = await controller.speaker_label_review_metrics(admin)
    assert result["overall"]["reviewed"] == 1
    assert [r["speaker"] for r in result["speakers"]] == ["Control speaker"]
    batch = await controller.next_speaker_label_reviews(admin)
    assert batch["reviewed_total"] == 1


@pytest.mark.parametrize("changed_source", ["screenpipe-test", "another-device"])
async def test_audit_fences_only_admitted_sources_during_speaker_service_call(
    db,
    audit_rows,
    monkeypatch,
    changed_source,
):
    await db.capture_sources.insert_one(
        {
            "source_id": "another-device",
            "user_id": "owner",
            "privacy_revision": 1,
            "privacy_enabled_from": START + timedelta(days=1),
        }
    )

    async def enrolled():
        await db.capture_sources.update_one(
            {"user_id": "owner", "source_id": changed_source},
            {"$inc": {"privacy_revision": 1}},
        )
        return {"speakers": []}

    monkeypatch.setattr(
        controller,
        "SpeakerRecognitionClient",
        lambda: SimpleNamespace(get_enrolled_speakers=enrolled),
    )
    if changed_source == "another-device":
        with pytest.raises(privacy.PrivacyHeld):
            await controller.speaker_confidence_overview(
                SimpleNamespace(user_id="admin", is_superuser=True)
            )
    else:
        result = await controller.speaker_confidence_overview(
            SimpleNamespace(user_id="admin", is_superuser=True)
        )
        assert "Synthetic private" not in json.dumps(result)
        assert "Control speaker" in json.dumps(result)


@pytest.mark.parametrize("endpoint", ["get_segments", "get_speech_regions"])
async def test_audit_detail_routes_hold_before_reading_segments_or_vad(
    db, monkeypatch, endpoint
):
    fake_conversation(monkeypatch, conversation())
    with pytest.raises(privacy.PrivacyHeld):
        await getattr(controller, endpoint)(
            SimpleNamespace(user_id="admin", is_superuser=True), "recording"
        )


async def test_speaker_suggestion_rechecks_before_external_call(db, monkeypatch):
    await db.capture_sources.insert_one(
        {
            "source_id": "another-device",
            "user_id": "owner",
            "privacy_revision": 1,
            "privacy_enabled_from": START + timedelta(days=1),
        }
    )
    row = conversation(source="another-device")
    row["derived_into"] = []
    fake_conversation(monkeypatch, row)

    async def reconstruct(*args):
        await db.capture_sources.update_one(
            {"user_id": "owner", "source_id": "another-device"},
            {"$inc": {"privacy_revision": 1}},
        )
        return b"synthetic audio"

    identify = AsyncMock(
        side_effect=AssertionError("Stale audio must not leave the backend")
    )
    monkeypatch.setattr(controller, "reconstruct_audio_segment", reconstruct)
    monkeypatch.setattr(
        controller,
        "SpeakerRecognitionClient",
        lambda: SimpleNamespace(enabled=True, identify_segment=identify),
    )
    with pytest.raises(privacy.PrivacyHeld):
        await controller.identify_segment_clip(
            SimpleNamespace(user_id="admin", is_superuser=True), "recording", 0, 1
        )
    identify.assert_not_awaited()
