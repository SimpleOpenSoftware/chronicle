"""Bounded metadata-first discovery of potential Immich memories."""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pymongo.errors import DuplicateKeyError

from backend.models.conversation import Conversation
from backend.models.device_input import CaptureSource, DeviceInputItem, utcnow
from backend.models.timeline import EvidenceLocator
from backend.users import User

from .timeline.photo_sampling import photo_metadata, sample_photos

_DAILY_LIMIT = 12
_BURST_GAP = timedelta(minutes=15)
logger = logging.getLogger(__name__)


@dataclass
class _ConversationWindow:
    """A projected conversation, satisfying ``device_context.ConversationWindow``."""

    user_id: str
    conversation_id: str
    created_at: datetime
    audio_total_duration: float | None = None


@dataclass
class ImmichDayReadiness:
    ready: bool
    reason: str
    target_asset_count: int
    latest_asset_local_date: date | None
    checked_at: datetime
    target_assets: list[dict[str, Any]]


def _settings() -> tuple[str, str] | None:
    url = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    return (url, key) if url and key else None


def immich_configured() -> bool:
    return _settings() is not None


async def resolve_immich_user_id() -> str | None:
    """Chronicle user that owns imported Immich references.

    ``IMMICH_USER_ID`` when set; otherwise the admin account, so the wizard can
    configure Immich before the first backend start has minted any ObjectId.
    """
    configured = os.getenv("IMMICH_USER_ID", "")
    if configured:
        return configured
    admin = await User.find_one(User.is_superuser == True)  # noqa: E712
    return str(admin.id) if admin else None


def _asset_time(asset: dict[str, Any]) -> datetime | None:
    # Immich's ``localDateTime`` is a wall-clock capture value but is serialized
    # with a trailing Z. Treating that as a UTC instant and then applying the
    # Chronicle timezone shifts the asset a second time. ``fileCreatedAt`` is the
    # real UTC capture instant and is therefore the authoritative timeline clock.
    raw = asset.get("fileCreatedAt") or asset.get("localDateTime")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _file_created_at(asset: dict[str, Any]) -> datetime | None:
    raw = asset.get("fileCreatedAt")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return _as_utc(value)


def _eligible_backup_asset(asset: dict[str, Any]) -> bool:
    """Server-visible upload evidence, excluding trash and external libraries."""

    return (
        asset.get("type") in {None, "IMAGE"}
        and not asset.get("isTrashed", False)
        and asset.get("libraryId") in {None, ""}
        and _file_created_at(asset) is not None
    )


async def _search_immich_assets(
    client: httpx.AsyncClient,
    url: str,
    *,
    taken_after: datetime,
    taken_before: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    page: int | None = 1
    assets: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    seen_ids: set[str] = set()
    while page is not None and (limit is None or len(assets) < limit):
        if page in seen_pages or len(seen_pages) >= 10000:
            raise ValueError("Immich inventory pagination did not complete")
        seen_pages.add(page)
        payload: dict[str, Any] = {
            "type": "IMAGE",
            "takenAfter": _as_utc(taken_after).isoformat(),
            "page": page,
            "size": 250 if limit is None else min(250, limit - len(assets)),
            "withExif": True,
            "withPeople": True,
            "order": "desc",
        }
        if taken_before is not None:
            # Immich v3.0's flat search uses an inclusive <= comparison. Subtract a
            # millisecond so the next local day's exact midnight is not counted here.
            payload["takenBefore"] = (
                _as_utc(taken_before) - timedelta(milliseconds=1)
            ).isoformat()
        response = await client.post(f"{url}/api/search/metadata", json=payload)
        response.raise_for_status()
        body = response.json().get("assets", {})
        batch = body.get("items", [])
        ids = {str(item["id"]) for item in batch}
        if seen_ids.intersection(ids) or len(ids) != len(batch):
            raise ValueError("Immich inventory changed during pagination; retry")
        seen_ids.update(ids)
        assets.extend(batch)
        next_page = body.get("nextPage")
        page = int(next_page) if next_page is not None else None
    return assets


async def check_immich_day_readiness(
    local_date: date, timezone_name: str
) -> ImmichDayReadiness:
    """Live, request-scoped readiness check; this function is never scheduled."""

    checked_at = utcnow()
    settings = _settings()
    if settings is None:
        # Immich is optional. A deployment without it has no backup to wait for.
        return ImmichDayReadiness(True, "immich_unconfigured", 0, None, checked_at, [])
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as error:
        raise ValueError(f"Invalid timezone: {timezone_name}") from error
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    end = min(end, checked_at)
    if end <= start:
        raise ValueError("Cannot reconcile a future evidence interval")
    url, key = settings
    try:
        async with httpx.AsyncClient(
            timeout=30,
            headers={"x-api-key": key, "Accept": "application/json"},
        ) as client:
            # Immich applies takenAfter/takenBefore to its localDateTime-shaped
            # search field. Query a padded interval, then enforce the exact local
            # day against fileCreatedAt below. The one-day pad covers every IANA
            # offset and DST transition without making the final result fuzzy.
            search_margin = timedelta(days=1)
            target_assets = [
                item
                for item in await _search_immich_assets(
                    client,
                    url,
                    taken_after=start - search_margin,
                    taken_before=end + search_margin,
                )
                if _eligible_backup_asset(item)
                and (captured := _asset_time(item)) is not None
                and start <= captured < end
            ]
            later_assets = [
                item
                for item in await _search_immich_assets(
                    client, url, taken_after=end - search_margin, limit=1000
                )
                if _eligible_backup_asset(item)
                and (captured := _asset_time(item)) is not None
                and end <= captured <= checked_at
            ]
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        logger.warning("Immich readiness query failed", exc_info=True)
        return ImmichDayReadiness(False, "immich_unreachable", 0, None, checked_at, [])

    latest = max((_asset_time(item) for item in later_assets), default=None)
    latest_local_date = latest.astimezone(zone).date() if latest else None
    if target_assets:
        reason = "assets_on_day"
        ready = True
    elif latest_local_date and latest_local_date > local_date:
        reason = "later_asset_watermark"
        ready = True
    else:
        reason = "no_immich_evidence"
        ready = False
    return ImmichDayReadiness(
        ready,
        reason,
        len(target_assets),
        latest_local_date,
        checked_at,
        target_assets,
    )


async def import_immich_day_candidates(
    user_id: str, assets: list[dict[str, Any]]
) -> int:
    """Persist the existing bounded/sampled memory candidates for one ready day."""

    source_id = "immich-default"
    source = await CaptureSource.find_one(
        CaptureSource.user_id == user_id, CaptureSource.source_id == source_id
    )
    if source is None:
        source = CaptureSource(
            user_id=user_id,
            source_id=source_id,
            name="Immich",
            provider="immich",
            platform="server",
            token_hash=hashlib.sha256(f"immich:{user_id}".encode()).hexdigest(),
            capabilities=["photos", "thumbnails"],
            status="online",
        )
        await source.insert()
    accepted = 0
    for asset in assets:
        if await _store_immich_candidate(user_id, source_id, asset):
            accepted += 1
    source.status = "online"
    source.last_seen_at = utcnow()
    source.health = {**source.health, "last_explicit_day_import": accepted}
    await source.save()
    return accepted


async def _store_immich_candidate(
    user_id: str, source_id: str, asset: dict[str, Any]
) -> bool:
    """Insert one candidate or repair its absolute capture metadata in place."""

    source_metadata = photo_metadata(asset)
    captured = _asset_time(asset)
    if captured is None:
        return False
    try:
        await DeviceInputItem(
            user_id=user_id,
            source_id=source_id,
            kind="immich_memory",
            source_item_id=str(asset["id"]),
            locator=EvidenceLocator(
                capture_source_id=source_id,
                modality="photo",
                track_id=None,
            ),
            captured_at=captured,
            metadata={
                "asset_id": asset["id"],
                "filename": asset.get("originalFileName"),
                "review_state": "candidate",
                "photo_metadata": source_metadata,
            },
        ).insert()
        return True
    except DuplicateKeyError:
        # Discovery is also a repair pass: an existing source item must move to
        # its corrected absolute capture instant instead of remaining permanently
        # stranded on the old local day. Preserve completed visual analysis.
        await DeviceInputItem.get_pymongo_collection().update_one(
            {
                "user_id": user_id,
                "source_id": source_id,
                "kind": "immich_memory",
                "source_item_id": str(asset["id"]),
            },
            {
                "$set": {
                    "captured_at": captured,
                    "metadata.asset_id": asset["id"],
                    "metadata.filename": asset.get("originalFileName"),
                    "metadata.photo_metadata": source_metadata,
                    "metadata.review_state": "candidate",
                }
            },
        )
        return False


def _as_utc(value: datetime) -> datetime:
    """Restore UTC stripped by MongoDB before serializing times for Immich."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def select_candidates(
    assets: list[dict[str, Any]], limit: int = _DAILY_LIMIT
) -> list[dict[str, Any]]:
    """Bound pixel work with coverage across the interval; keep metadata separately."""
    eligible = [
        x for x in assets if x.get("type") in {None, "IMAGE"} and _file_created_at(x)
    ]
    by_id = {str(x["id"]): x for x in eligible}
    return [
        by_id[x["asset_id"]]
        for x in sample_photos([photo_metadata(x) for x in eligible], limit)
    ]


async def scan_immich_memories() -> dict[str, Any]:
    settings = _settings()
    if settings is None:
        return {
            "status": "disabled",
            "reason": "IMMICH_URL and IMMICH_API_KEY are required",
        }
    url, key = settings
    user_id = await resolve_immich_user_id()
    if user_id is None:
        return {"status": "disabled", "reason": "no IMMICH_USER_ID and no admin user"}
    source_id = "immich-default"
    source = await CaptureSource.find_one(
        CaptureSource.user_id == user_id, CaptureSource.source_id == source_id
    )
    if source is None:
        source = CaptureSource(
            user_id=user_id,
            source_id=source_id,
            name="Immich",
            provider="immich",
            platform="server",
            token_hash=hashlib.sha256(f"immich:{user_id}".encode()).hexdigest(),
            capabilities=["photos", "thumbnails"],
            status="online",
        )
        await source.insert()

    newest = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == user_id,
        DeviceInputItem.source_id == source_id,
        sort=[("captured_at", -1)],
    )
    newest_at = _as_utc(newest.captured_at) if newest else utcnow() - timedelta(days=2)
    since = newest_at - timedelta(hours=48)
    page: int | None = 1
    assets: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=30, headers={"x-api-key": key, "Accept": "application/json"}
    ) as client:
        while page is not None and len(assets) < 1000:
            response = await client.post(
                f"{url}/api/search/metadata",
                json={
                    "type": "IMAGE",
                    "takenAfter": since.isoformat(),
                    "page": page,
                    "size": 250,
                },
            )
            response.raise_for_status()
            body = response.json().get("assets", {})
            assets.extend(body.get("items", []))
            next_page = body.get("nextPage")
            page = int(next_page) if next_page is not None else None

    accepted = 0
    for asset in select_candidates(assets):
        if await _store_immich_candidate(user_id, source_id, asset):
            accepted += 1
    source.status = "online"
    source.last_seen_at = utcnow()
    source.health = {
        "last_scan_candidates": len(assets),
        "last_scan_accepted": accepted,
    }
    await source.save()
    # A daily scan may discover photos after the matching conversation has
    # already completed. Re-run the idempotent bounded linker for recent ones.
    from backend.services.device_context import request_conversation_context_jobs

    # The linker needs a window and an owner, not the recordings themselves. This
    # scan covers 48 hours, which is 69 conversations and 63 MB of transcript here;
    # hydrating that on the cron's loop stalled the backend for 1.7 seconds.
    conversations = [
        _ConversationWindow(**row)
        async for row in Conversation.get_pymongo_collection().find(
            {
                "user_id": user_id,
                "created_at": {"$gte": since},
                "deleted": {"$ne": True},
            },
            {
                "_id": 0,
                "user_id": 1,
                "conversation_id": 1,
                "created_at": 1,
                "audio_total_duration": 1,
            },
        )
    ]
    for conversation in conversations:
        await request_conversation_context_jobs(conversation)
    if accepted:
        observations = await DeviceInputItem.find(
            DeviceInputItem.user_id == user_id,
            DeviceInputItem.kind == "observation",
            DeviceInputItem.captured_at >= since - timedelta(minutes=30),
        ).to_list()
        for observation in observations:
            observation.curation = "pending"
            await observation.save()
    return {"status": "ok", "assets": len(assets), "accepted": accepted}
