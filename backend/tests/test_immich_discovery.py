import os
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.device_input import DeviceInputItem
from backend.services import immich_discovery
from backend.services.immich_discovery import _as_utc, select_candidates


@pytest.fixture
async def immich_db(mongo_service):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_immich_discovery_db"]
    await init_beanie(database=database, document_models=[DeviceInputItem])
    yield
    await client.drop_database("test_immich_discovery_db")
    client.close()


def asset(identifier: str, when: datetime, name: str = "photo.jpg"):
    return {
        "id": identifier,
        "type": "IMAGE",
        "localDateTime": when.isoformat(),
        "fileCreatedAt": (when + timedelta(minutes=5)).isoformat(),
        "originalFileName": name,
    }


def test_small_selection_preserves_distinct_photos_including_screenshots():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    result = select_candidates(
        [
            asset("one", start),
            asset("burst", start + timedelta(minutes=2)),
            asset("screen", start + timedelta(hours=1), "Screenshot_1.png"),
            asset("two", start + timedelta(hours=2)),
        ]
    )
    assert [row["id"] for row in result] == ["one", "burst", "screen", "two"]


def test_candidate_selection_honors_analysis_budget():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [asset(str(i), start + timedelta(hours=i)) for i in range(20)]
    assert len(select_candidates(rows)) == 12


def test_mongo_datetime_is_restored_to_utc_for_immich_search():
    stored = datetime(2026, 7, 27, 16, 46, 7)

    assert _as_utc(stored).isoformat() == "2026-07-27T16:46:07+00:00"


@pytest.mark.asyncio
async def test_search_uses_deployed_immich_v3_date_bounds():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"assets": {"items": [], "nextPage": None}}

    class Client:
        async def post(self, url, json):
            calls.append((url, json))
            return Response()

    start = datetime(2026, 8, 15, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    await immich_discovery._search_immich_assets(
        Client(), "https://immich", taken_after=start, taken_before=end
    )

    payload = calls[0][1]
    assert payload["type"] == "IMAGE"
    assert payload["takenAfter"] == start.isoformat()
    assert payload["takenBefore"] == (end - timedelta(milliseconds=1)).isoformat()


@pytest.mark.asyncio
async def test_target_day_asset_makes_reconciliation_ready(monkeypatch):
    target = asset("target", datetime(2026, 8, 15, 8, tzinfo=timezone.utc))
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )

    async def search(_client, _url, *, taken_before=None, **_kwargs):
        return [target] if taken_before is not None else []

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", search)

    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 8, 15), "Asia/Kolkata"
    )

    assert result.ready is True
    assert result.reason == "assets_on_day"
    assert result.target_asset_count == 1


@pytest.mark.asyncio
async def test_target_day_uses_file_created_utc_when_immich_local_time_has_z(
    monkeypatch,
):
    """Immich localDateTime is wall time even though it is serialized with Z."""

    target = {
        "id": "late-evening",
        "type": "IMAGE",
        "localDateTime": "2026-06-09T19:05:53.117Z",
        "fileCreatedAt": "2026-06-09T13:35:53.117Z",
        "originalFileName": "IMG_0040.HEIC",
    }
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )

    async def search(_client, _url, *, taken_after, taken_before=None, **_kwargs):
        # Reproduce Immich's search behavior: its date filter compares the
        # localDateTime-shaped value, while Chronicle must ultimately classify the
        # asset by the real UTC capture instant in fileCreatedAt.
        searched_at = datetime.fromisoformat(
            target["localDateTime"].replace("Z", "+00:00")
        )
        if searched_at < taken_after:
            return []
        if taken_before is not None and searched_at >= taken_before:
            return []
        return [target]

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", search)

    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 6, 9), "Asia/Kolkata"
    )

    assert result.reason == "assets_on_day"
    assert result.target_asset_count == 1


@pytest.mark.asyncio
async def test_existing_candidate_capture_time_is_repaired_without_losing_visual_data(
    immich_db,
):
    existing = DeviceInputItem(
        user_id="user",
        source_id="immich-default",
        kind="immich_memory",
        source_item_id="late-evening",
        locator={
            "capture_source_id": "immich-default",
            "modality": "photo",
            "track_id": None,
        },
        captured_at=datetime(2026, 6, 9, 19, 5, 53, 117000, tzinfo=timezone.utc),
        metadata={
            "asset_id": "late-evening",
            "filename": "IMG_0040.HEIC",
            "description": "Already analyzed",
            "visual_analysis": {"state": "complete", "version": "v1"},
        },
    )
    await existing.insert()

    created = await immich_discovery._store_immich_candidate(
        "user",
        "immich-default",
        {
            "id": "late-evening",
            "type": "IMAGE",
            "localDateTime": "2026-06-09T19:05:53.117Z",
            "fileCreatedAt": "2026-06-09T13:35:53.117Z",
            "originalFileName": "IMG_0040.HEIC",
        },
    )

    repaired = await DeviceInputItem.find_one(
        DeviceInputItem.source_item_id == "late-evening"
    )
    assert created is False
    assert repaired is not None
    assert _as_utc(repaired.captured_at) == datetime(
        2026, 6, 9, 13, 35, 53, 117000, tzinfo=timezone.utc
    )
    assert repaired.metadata["description"] == "Already analyzed"
    assert repaired.metadata["visual_analysis"] == {
        "state": "complete",
        "version": "v1",
    }


@pytest.mark.asyncio
async def test_later_capture_watermark_makes_empty_day_ready(monkeypatch):
    later = asset("later", datetime(2026, 8, 16, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )

    async def search(_client, _url, *, taken_before=None, **_kwargs):
        return [] if taken_before is not None else [later]

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", search)

    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 8, 15), "Asia/Kolkata"
    )

    assert result.ready is True
    assert result.reason == "later_asset_watermark"
    assert result.latest_asset_local_date == date(2026, 8, 17)


@pytest.mark.asyncio
async def test_external_library_assets_do_not_satisfy_gate(monkeypatch):
    external = {
        **asset("external", datetime(2026, 8, 15, 8, tzinfo=timezone.utc)),
        "libraryId": "external-library",
    }
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )
    monkeypatch.setattr(
        immich_discovery,
        "_search_immich_assets",
        lambda *_args, **_kwargs: _async_value([external]),
    )

    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 8, 15), "Asia/Kolkata"
    )

    assert result.ready is False
    assert result.reason == "no_immich_evidence"


@pytest.mark.asyncio
async def test_unconfigured_immich_is_ready_without_network(monkeypatch):
    monkeypatch.setattr(immich_discovery, "_settings", lambda: None)

    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 8, 15), "Asia/Kolkata"
    )

    assert result.ready is True
    assert result.reason == "immich_unconfigured"
    assert result.target_assets == []


@pytest.mark.asyncio
async def test_unreachable_immich_blocks(monkeypatch):
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )

    async def fail(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", fail)
    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 8, 15), "Asia/Kolkata"
    )

    assert result.ready is False
    assert result.reason == "immich_unreachable"


@pytest.mark.asyncio
async def test_dst_day_search_padding_contains_exact_local_day(monkeypatch):
    bounds = []
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )

    async def search(_client, _url, *, taken_after, taken_before=None, **_kwargs):
        bounds.append((taken_after, taken_before))
        return []

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", search)
    await immich_discovery.check_immich_day_readiness(
        date(2026, 3, 8), "America/New_York"
    )

    searched_start, searched_end = bounds[0]
    exact_start = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    exact_end = datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
    assert exact_end - exact_start == timedelta(hours=23)
    assert searched_start == exact_start - timedelta(days=1)
    assert searched_end == exact_end + timedelta(days=1)


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_ongoing_day_readiness_stops_at_request_time(monkeypatch):
    checkpoint = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(immich_discovery, "utcnow", lambda: checkpoint)
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich", "key")
    )
    earlier = asset("earlier", checkpoint - timedelta(minutes=10))
    later = asset("later", checkpoint + timedelta(minutes=1))

    async def search(*args, **kwargs):
        return [earlier, later]

    monkeypatch.setattr(immich_discovery, "_search_immich_assets", search)
    result = await immich_discovery.check_immich_day_readiness(
        date(2026, 9, 5), "Asia/Kolkata"
    )
    assert result.checked_at == checkpoint
    assert result.target_assets == [earlier]
    assert result.latest_asset_local_date is None


@pytest.mark.asyncio
async def test_inventory_exhausts_pages_beyond_old_thousand_asset_limit():
    pages = []

    class Response:
        def __init__(self, page):
            self.page = page

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "assets": {
                    "items": [
                        {"id": str((self.page - 1) * 250 + i)} for i in range(250)
                    ],
                    "nextPage": self.page + 1 if self.page < 5 else None,
                }
            }

    class Client:
        async def post(self, _url, json):
            pages.append(json["page"])
            assert json["withExif"] and json["withPeople"]
            return Response(json["page"])

    rows = await immich_discovery._search_immich_assets(
        Client(), "http://immich", taken_after=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert len(rows) == 1250
    assert pages == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_inventory_rejects_repeated_assets_in_mutating_pagination():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"assets": {"items": [{"id": "same"}], "nextPage": 2}}

    class Client:
        async def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(ValueError, match="changed during pagination"):
        await immich_discovery._search_immich_assets(
            Client(),
            "http://immich",
            taken_after=datetime(2023, 1, 1, tzinfo=timezone.utc),
        )
