from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from test_privacy_entrypoints import START, db, result

from backend.models.audio_capture import AudioRangeRef
from backend.services import capture_rebuilds, privacy


@pytest.fixture
async def evidence(db, monkeypatch):
    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield

    monkeypatch.setattr(privacy, "distributed_lock", lock)
    await db.privacy_screening.insert_one(
        {
            **result("allowed").model_dump(),
            "user_id": "owner",
            "source_id": "screenpipe-test",
        }
    )
    await db.audio_capture_sessions.insert_one(
        {
            "user_id": "owner",
            "capture_session_id": "old-capture",
            "capture_source_id": "screenpipe-test:input:mic",
            "started_at": START,
            "ended_at": START + timedelta(seconds=10),
        }
    )
    await db.audio_chunks.insert_one(
        {
            "_id": ObjectId("a" * 24),
            "user_id": "owner",
            "capture_session_id": "old-capture",
            "capture_source_id": "screenpipe-test:input:mic",
            "audio_data": b"synthetic-raw",
            "captured_at": START,
        }
    )
    return db


def claim(*, corrected=False):
    return AudioRangeRef(
        capture_source_id="screenpipe-test:input:mic",
        time_basis="recorded",
        capture_session_ids=["new-capture" if corrected else "old-capture"],
        chunk_ids=[("b" if corrected else "a") * 24],
        started_at=START,
        ended_at=START + timedelta(seconds=10),
    )


async def retire():
    await capture_rebuilds.hold_for_rebuild(
        SimpleNamespace(user_id="owner", source_id="screenpipe-test"),
        ["old-capture"],
        "source_audio_alignment_rebuild",
    )


async def test_retired_capture_holds_all_claims_but_corrected_capture_can_proceed(
    evidence,
):
    before = await privacy.load_snapshot("owner")
    assert before.permits_record({"audio_ranges": [claim()]})
    original = await evidence.audio_chunks.find_one({})
    await retire()
    after = await privacy.load_snapshot("owner")
    assert not after.permits_record({"audio_ranges": [claim()]})
    assert not after.permits_record({"audio_ranges": [{"chunk_ids": ["a" * 24]}]})
    assert not after.permits_record(
        {"evidence_refs": [{"capture_session_ids": ["old-capture"]}]}
    )
    assert after.permits_record({"audio_ranges": [claim(corrected=True)]})
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_audio_ranges([claim()], "owner")
    await privacy.require_audio_ranges([claim(corrected=True)], "owner")
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    assert await evidence.audio_chunks.find_one({}) == original
    revision = (await evidence.capture_sources.find_one({}))["privacy_revision"]
    await retire()
    assert (await evidence.capture_sources.find_one({}))["privacy_revision"] == revision


async def test_time_override_does_not_release_old_derivatives_or_notes(evidence):
    await evidence.conversations.insert_one(
        {
            "conversation_id": "old-result",
            "user_id": "owner",
            "audio_ranges": [claim().model_dump()],
            "vault_paths": ["Synthetic.md"],
        }
    )
    await retire()
    await evidence.privacy_overrides.insert_one(
        {
            "user_id": "owner",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=10),
            "override": "allowed",
            "revision": 10,
        }
    )
    assert (
        await privacy.filter_conversation_documents(
            [{"conversation_id": "old-result"}], "owner"
        )
        == []
    )
    assert "Synthetic.md" in await privacy.quarantined_vault_paths("owner")
    other = await privacy.load_snapshot("another-owner")
    assert other.permits_record({"audio_ranges": [claim()]})


@pytest.mark.parametrize(
    "field,value",
    [("user_id", "other-owner"), ("capture_source_id", "other-source:input:mic")],
)
async def test_rebuild_rejects_foreign_capture(evidence, field, value):
    await evidence.audio_capture_sessions.update_one({}, {"$set": {field: value}})
    with pytest.raises(ValueError):
        await retire()
    assert await evidence.privacy_capture_holds.count_documents({}) == 0
    assert not (await evidence.capture_sources.find_one({})).get("privacy_updating")


async def test_interrupted_rebuild_keeps_source_held_until_exact_replay(
    evidence, monkeypatch
):
    from backend.services.timeline import dirty_ranges

    monkeypatch.setattr(
        dirty_ranges,
        "mark_evidence_dirty",
        AsyncMock(side_effect=RuntimeError("synthetic failure")),
    )
    with pytest.raises(RuntimeError):
        await retire()
    assert (await evidence.capture_sources.find_one({}))["privacy_updating"]
    assert not (await privacy.load_snapshot("owner")).permits_record(
        {"audio_ranges": [claim(corrected=True)]}
    )
    monkeypatch.setattr(dirty_ranges, "mark_evidence_dirty", AsyncMock())
    await retire()
    assert not (await evidence.capture_sources.find_one({}))["privacy_updating"]
    assert not (await privacy.load_snapshot("owner")).permits_record(
        {"audio_ranges": [claim()]}
    )


async def test_retired_capture_stops_transcription_before_provider_lookup(
    evidence, monkeypatch
):
    from backend.workers import transcription_jobs

    await retire()
    provider = AsyncMock(
        side_effect=AssertionError("Retired samples must not reach ASR")
    )
    monkeypatch.setattr(transcription_jobs, "get_transcription_provider", provider)
    with pytest.raises(privacy.PrivacyHeld):
        await transcription_jobs.transcribe_audio_range(None, audio_ranges=[claim()])
    provider.assert_not_called()


async def test_retired_conversation_audio_route_stops_before_decode(
    evidence, monkeypatch
):
    from fastapi import HTTPException

    from backend.routers.modules import audio_routes

    row = {
        "user_id": "owner",
        "audio_ranges": [claim()],
        "conversation_id": "old-result",
    }
    document = SimpleNamespace(user_id="owner", model_dump=lambda: row)
    monkeypatch.setattr(
        audio_routes,
        "Conversation",
        SimpleNamespace(
            conversation_id="old-result", find_one=AsyncMock(return_value=document)
        ),
    )
    decode = AsyncMock(
        side_effect=AssertionError("Retired samples must not be decoded")
    )
    monkeypatch.setattr(audio_routes, "get_opus_for_conversation", decode)
    await retire()
    with pytest.raises(HTTPException) as raised:
        await audio_routes.get_conversation_audio(
            "old-result",
            None,
            "opus",
            None,
            SimpleNamespace(user_id="owner", is_superuser=False),
        )
    assert raised.value.status_code == 423
    decode.assert_not_awaited()
