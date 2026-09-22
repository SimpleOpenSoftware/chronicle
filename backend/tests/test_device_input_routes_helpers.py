import hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError
from starlette.datastructures import Headers, UploadFile

import backend.routers.modules.device_input_routes as device_input_routes
from backend.models.timeline import EvidenceLocator
from backend.routers.modules.device_input_routes import (
    _effective_source_status,
    _utc_iso,
    ingest_audio,
)


@pytest.fixture
def device_input_model(monkeypatch):
    """Exercise route orchestration without initializing a Beanie collection."""

    class Expression:
        def __eq__(self, _other):
            return self

    class DeviceInputFixture:
        user_id = Expression()
        source_id = Expression()
        kind = Expression()
        source_item_id = Expression()

        def __init__(self, **values):
            self.id = "requested-item"
            self.ended_at = None
            self.content_hash = None
            for name, value in values.items():
                setattr(self, name, value)

        @staticmethod
        async def find_one(*_args):
            return None

        async def insert(self):
            return None

    class AudioSpanFixture:
        user_id = Expression()
        source_id = Expression()

        @staticmethod
        async def find_one(*_args):
            return None

    monkeypatch.setattr(device_input_routes, "DeviceInputItem", DeviceInputFixture)
    monkeypatch.setattr(device_input_routes, "AudioEvidenceSpan", AudioSpanFixture)
    return DeviceInputFixture


def test_utc_iso_marks_naive_mongo_datetimes_as_utc():
    assert _utc_iso(datetime(2026, 7, 24, 16, 0, 23, 834000)) == (
        "2026-07-24T16:00:23.834000Z"
    )


def test_utc_iso_converts_aware_datetimes_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))
    assert _utc_iso(datetime(2026, 7, 24, 21, 30, tzinfo=india)) == (
        "2026-07-24T16:00:00Z"
    )


def test_online_source_becomes_offline_when_heartbeat_is_stale():
    now = datetime(2026, 7, 24, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="screenpipe",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "offline"


def test_recent_source_remains_online():
    now = datetime(2026, 7, 24, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="screenpipe",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 4, 30, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "online"


def test_immich_source_uses_last_seen_as_sync_time_not_heartbeat():
    now = datetime(2026, 7, 31, 16, 5, tzinfo=timezone.utc)
    source = SimpleNamespace(
        provider="immich",
        status="online",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source, now) == "online"


def test_immich_source_preserves_explicit_error_status():
    source = SimpleNamespace(
        provider="immich",
        status="error",
        last_seen_at=datetime(2026, 7, 24, 16, 2, tzinfo=timezone.utc),
    )

    assert _effective_source_status(source) == "error"


@pytest.mark.asyncio
async def test_audio_route_persists_explicit_provider_local_track(monkeypatch):
    payload = b"fixture-audio"
    captured: dict = {}

    class Expression:
        def __eq__(self, _other):
            return self

    class DeviceInputFixture:
        user_id = Expression()
        source_id = Expression()
        kind = Expression()
        source_item_id = Expression()

        def __init__(self, **values):
            captured.update(values)
            self.id = "item-1"

        @staticmethod
        async def find_one(*_args):
            return None

        async def insert(self):
            return None

    class AudioSpanFixture:
        user_id = Expression()
        source_id = Expression()

        @staticmethod
        async def find_one(*_args):
            return None

    monkeypatch.setattr(device_input_routes, "DeviceInputItem", DeviceInputFixture)
    monkeypatch.setattr(device_input_routes, "AudioEvidenceSpan", AudioSpanFixture)

    response = await ingest_audio(
        file=UploadFile(
            BytesIO(payload),
            filename="Desk Mic (input)_42.wav",
            headers=Headers({"content-type": "audio/wav"}),
        ),
        source_item_id="42",
        captured_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        duration_seconds=10,
        device_name="Desk Mic (input)_42",
        track_id="Desk Mic (input)",
        direction="input",
        content_hash=hashlib.sha256(payload).hexdigest(),
        meeting_id=None,
        source=SimpleNamespace(user_id="user", source_id="screenpipe-rainbow"),
    )

    assert response == {"status": "accepted", "item_id": "item-1"}
    assert captured["locator"].model_dump() == {
        "capture_source_id": "screenpipe-rainbow",
        "modality": "audio",
        "track_id": "Desk Mic (input)",
    }
    assert captured["metadata"]["direction"] == "input"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["locator", "captured_at"])
async def test_observation_open_race_rejects_conflicting_immutable_identity(
    monkeypatch, device_input_model, conflict
):
    captured_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
    existing = SimpleNamespace(
        user_id="user",
        source_id="screenpipe-rainbow",
        kind="observation",
        source_item_id="observation:42",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-rainbow",
            modality="screen",
            track_id="display-one",
        ),
        captured_at=captured_at,
        lifecycle="open",
    )
    responses = iter([None, existing])

    async def find_one(*_args):
        return next(responses)

    async def collide(_self):
        raise DuplicateKeyError("race")

    monkeypatch.setattr(device_input_routes.DeviceInputItem, "find_one", find_one)
    monkeypatch.setattr(device_input_routes.DeviceInputItem, "insert", collide)
    incoming_locator = existing.locator.model_copy()
    incoming_time = captured_at
    if conflict == "locator":
        incoming_locator = incoming_locator.model_copy(
            update={"track_id": "display-two"}
        )
    else:
        incoming_time += timedelta(seconds=1)

    with pytest.raises(device_input_routes.HTTPException) as exc_info:
        await device_input_routes.ingest_observations(
            device_input_routes.ObservationBatch(
                events=[
                    device_input_routes.ObservationEvent(
                        event="open",
                        source_item_id="observation:42",
                        locator=incoming_locator,
                        captured_at=incoming_time,
                    )
                ]
            ),
            SimpleNamespace(user_id="user", source_id="screenpipe-rainbow"),
        )

    assert exc_info.value.status_code == 409
    assert "Observation identity conflicts" in exc_info.value.detail


def _audio_upload(payload: bytes = b"fixture-audio") -> UploadFile:
    return UploadFile(
        BytesIO(payload),
        filename="Desk Mic (input)_42.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )


def _audio_item(*, track_id: str = "Desk Mic (input)", digest: str):
    captured_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
    return SimpleNamespace(
        user_id="user",
        source_id="screenpipe-rainbow",
        kind="audio",
        source_item_id="42",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-rainbow",
            modality="audio",
            track_id=track_id,
        ),
        captured_at=captured_at,
        ended_at=captured_at + timedelta(seconds=10),
        content_hash=digest,
    )


async def _ingest_audio(payload: bytes = b"fixture-audio"):
    return await ingest_audio(
        file=_audio_upload(payload),
        source_item_id="42",
        captured_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        duration_seconds=10,
        device_name="Desk Mic (input)_42",
        track_id="Desk Mic (input)",
        direction="input",
        content_hash=hashlib.sha256(payload).hexdigest(),
        meeting_id=None,
        source=SimpleNamespace(user_id="user", source_id="screenpipe-rainbow"),
    )


@pytest.mark.asyncio
async def test_audio_precheck_rejects_duplicate_from_another_track(
    monkeypatch, device_input_model
):
    digest = hashlib.sha256(b"fixture-audio").hexdigest()
    existing = _audio_item(track_id="System Audio (output)", digest=digest)

    async def find_one(*_args):
        return existing

    monkeypatch.setattr(device_input_routes.DeviceInputItem, "find_one", find_one)

    with pytest.raises(device_input_routes.HTTPException) as exc_info:
        await _ingest_audio()

    assert exc_info.value.status_code == 409
    assert "Audio identity conflicts" in exc_info.value.detail


@pytest.mark.asyncio
async def test_audio_insert_race_rejects_different_content_hash(
    monkeypatch, device_input_model
):
    existing = _audio_item(
        digest=hashlib.sha256(b"different-audio").hexdigest(),
    )
    responses = iter([None, existing])

    async def find_one(*_args):
        return next(responses)

    async def no_compacted_span(*_args):
        return None

    async def collide(_self):
        raise DuplicateKeyError("race")

    monkeypatch.setattr(device_input_routes.DeviceInputItem, "find_one", find_one)
    monkeypatch.setattr(device_input_routes.DeviceInputItem, "insert", collide)
    monkeypatch.setattr(
        device_input_routes.AudioEvidenceSpan, "find_one", no_compacted_span
    )

    with pytest.raises(device_input_routes.HTTPException) as exc_info:
        await _ingest_audio()

    assert exc_info.value.status_code == 409
    assert "Audio identity conflicts" in exc_info.value.detail


@pytest.mark.asyncio
async def test_audio_compacted_duplicate_requires_the_same_track(
    monkeypatch, device_input_model
):
    async def no_item(*_args):
        return None

    async def compacted(*_args):
        return SimpleNamespace(
            locator=EvidenceLocator(
                capture_source_id="screenpipe-rainbow",
                modality="audio",
                track_id="System Audio (output)",
            )
        )

    monkeypatch.setattr(device_input_routes.DeviceInputItem, "find_one", no_item)
    monkeypatch.setattr(device_input_routes.AudioEvidenceSpan, "find_one", compacted)

    with pytest.raises(device_input_routes.HTTPException) as exc_info:
        await _ingest_audio()

    assert exc_info.value.status_code == 409
    assert "Audio locator conflicts" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [None, "captured_at", "ended_at", "locator"])
async def test_complete_job_replays_bson_timestamp_precision(
    monkeypatch, device_input_model, conflict
):
    from unittest.mock import AsyncMock

    from bson import BSON

    captured_at = datetime(2026, 9, 15, 20, 23, 5, 29538, tzinfo=timezone.utc)
    ended_at = captured_at + timedelta(seconds=1, microseconds=123)
    stored_times = BSON.encode(
        {"captured_at": captured_at, "ended_at": ended_at}
    ).decode()
    locator = EvidenceLocator(
        capture_source_id="screenpipe-rainbow", modality="screen", track_id="monitor_35"
    )
    existing = device_input_model(
        user_id="user",
        source_id="screenpipe-rainbow",
        kind="screen_context",
        source_item_id="frame:130882",
        locator=locator,
        **stored_times,
    )
    incoming_time = captured_at
    incoming_end = ended_at
    incoming_locator = locator
    if conflict == "captured_at":
        incoming_time += timedelta(milliseconds=1)
    elif conflict == "ended_at":
        incoming_end += timedelta(milliseconds=1)
    elif conflict == "locator":
        incoming_locator = locator.model_copy(update={"track_id": "monitor_1309"})
    job = SimpleNamespace(
        source_id="screenpipe-rainbow",
        context_request_id=None,
        status="claimed",
        purpose="conversation_enrichment",
        payload={"conversation_id": "conversation"},
        save=AsyncMock(),
    )
    monkeypatch.setattr(
        device_input_routes,
        "DeviceInputJob",
        SimpleNamespace(get=AsyncMock(return_value=job)),
    )
    monkeypatch.setattr(
        device_input_model,
        "insert",
        AsyncMock(side_effect=DuplicateKeyError("same frame")),
    )
    monkeypatch.setattr(
        device_input_model, "find_one", AsyncMock(return_value=existing)
    )
    body = device_input_routes.JobCompletion(
        items=[
            device_input_routes.ActivityItem(
                source_item_id="frame:130882",
                locator=incoming_locator,
                captured_at=incoming_time,
                ended_at=incoming_end,
                metadata={"app_name": "Browser", "text": "Captured screen context"},
            )
        ]
    )
    source = SimpleNamespace(user_id="user", source_id="screenpipe-rainbow")
    if conflict:
        with pytest.raises(device_input_routes.HTTPException) as exc:
            await device_input_routes.complete_job("job", body, source)
        assert exc.value.status_code == 409
        job.save.assert_not_awaited()
    else:
        result = await device_input_routes.complete_job("job", body, source)
        assert result["stored"] == 1
        assert job.status == "complete"
        assert job.payload["result_evidence_ids"] == ["observation:requested-item"]
        job.save.assert_awaited_once()
