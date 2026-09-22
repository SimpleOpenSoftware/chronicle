"""Policy writes on one device must not invalidate unrelated device audio."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_entrypoints import START, db, result

from backend.services import privacy

END = START + timedelta(seconds=10)


@pytest.fixture
async def devices(db):
    for source_id in ("screenpipe-test", "screenpipe-control"):
        await db.capture_sources.update_one(
            {"source_id": source_id},
            {
                "$set": {
                    "user_id": "owner",
                    "privacy_enabled_from": START,
                    "privacy_revision": 1,
                    "privacy_tracks": ["display"],
                }
            },
            upsert=True,
        )
        await db.privacy_display_sets.update_one(
            {"source_id": source_id},
            {
                "$set": {
                    "user_id": "owner",
                    "observed_at": START,
                    "transition_started_at": START,
                    "track_ids": ["display"],
                }
            },
            upsert=True,
        )
        await db.privacy_screening.insert_one(
            {
                **result("allowed").model_dump(),
                "user_id": "owner",
                "source_id": source_id,
            }
        )
    return db


async def revise(db, source):
    await db.capture_sources.update_one(
        {"source_id": source},
        {"$inc": {"privacy_revision": 1}, "$set": {"privacy_updating": True}},
    )


async def test_snapshot_read_race_holds_only_changed_source(devices, monkeypatch):
    original_find = devices.privacy_screening.find

    class Cursor:
        async def to_list(self, length=None):
            await revise(devices, "screenpipe-test")
            return await original_find({}).to_list(length=None)

    class DatabaseProxy:
        privacy_screening = SimpleNamespace(find=lambda *a, **kw: Cursor())

        def __getattr__(self, name):
            return getattr(devices, name)

    monkeypatch.setattr(privacy, "database", DatabaseProxy)
    snapshot = await privacy._load_capture_snapshot("owner")
    assert snapshot.permits("screenpipe-control:output:track", START, END)
    assert not snapshot.permits("screenpipe-test:input:track", START, END)
    # A virtual crossed-revision hold must survive the writer finishing.
    await devices.capture_sources.update_one(
        {"source_id": "screenpipe-test"}, {"$set": {"privacy_updating": False}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy._assert_capture_current("owner", snapshot)


@pytest.mark.parametrize(
    "changed,held",
    [
        ("screenpipe-test", False),
        ("screenpipe-control", True),
    ],
)
async def test_single_source_fence(devices, changed, held):
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.permits("screenpipe-control:output:track", START, END)
    await revise(devices, changed)
    if held:
        with pytest.raises(privacy.PrivacyHeld):
            await privacy.assert_current("owner", snapshot)
    else:
        await privacy.assert_current("owner", snapshot)


@pytest.mark.parametrize(
    "activated,held",
    [
        ("new-source", True),
        ("unrelated-source", False),
    ],
)
async def test_activation_fences_previously_unprotected_source(
    devices, activated, held
):
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.permits("new-source:input:track", START, END)
    await devices.capture_sources.insert_one(
        {
            "user_id": "owner",
            "source_id": activated,
            "privacy_revision": 1,
            "privacy_enabled_from": START,
        }
    )
    if held:
        with pytest.raises(privacy.PrivacyHeld):
            await privacy.assert_current("owner", snapshot)
    else:
        await privacy.assert_current("owner", snapshot)


async def test_disappearing_source_invalidates_checked_audio(devices):
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.permits("screenpipe-control", START, END)
    await devices.capture_sources.delete_one({"source_id": "screenpipe-control"})
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", snapshot)


@pytest.mark.parametrize("vault_scan", [False, True])
async def test_broad_reads_keep_whole_owner_fence(devices, vault_scan):
    snapshot = await privacy.load_snapshot("owner")
    if vault_scan:
        assert snapshot.permits("screenpipe-control", START, END)
        assert (
            await privacy.quarantined_vault_paths("owner", snapshot=snapshot) == set()
        )
    await revise(devices, "screenpipe-test")
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", snapshot)


@pytest.mark.parametrize(
    "changed,status",
    [
        ("screenpipe-test", 200),
        ("screenpipe-control", 423),
    ],
)
async def test_audio_http_publication_rechecks_its_device(
    devices, monkeypatch, changed, status
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.routers.modules import audio_routes

    row = {
        "user_id": "owner",
        "conversation_id": "synthetic-audio",
        "title": "Synthetic audio",
        "source_id": "screenpipe-control",
        "started_at": START,
        "ended_at": END,
        "audio_chunks_count": 1,
    }
    conversation = SimpleNamespace(**row, model_dump=lambda: row)
    monkeypatch.setattr(
        audio_routes,
        "Conversation",
        SimpleNamespace(
            conversation_id="conversation_id",
            find_one=AsyncMock(return_value=conversation),
        ),
    )

    async def read_audio(*args, **kwargs):
        await revise(devices, changed)
        return b"synthetic-audio-bytes"

    monkeypatch.setattr(audio_routes, "get_opus_for_conversation", read_audio)
    app = FastAPI()
    app.include_router(audio_routes.router)
    app.dependency_overrides[audio_routes.current_active_user_optional] = (
        lambda: SimpleNamespace(
            user_id="owner",
            is_superuser=False,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/audio/get_audio/synthetic-audio")
    assert response.status_code == status
    if status == 200:
        assert response.content == b"synthetic-audio-bytes"
    else:
        assert b"synthetic-audio-bytes" not in response.content
