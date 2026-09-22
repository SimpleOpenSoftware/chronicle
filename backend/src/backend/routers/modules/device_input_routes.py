"""Pairing, ingestion, bounded source jobs, and timeline APIs for capture devices."""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import backend.services.screening_revisions as screening_revisions
from backend.auth import current_active_user
from backend.config import get_screen_context_settings
from backend.models.conversation import Conversation
from backend.models.device_input import (
    MAX_FRAME_CANDIDATES,
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    PairingCode,
    utcnow,
)
from backend.models.timeline import AudioEvidenceSpan, EvidenceLocator, TimelineEpisode
from backend.models.user import User
from backend.services.device_context import (
    request_conversation_context_jobs,
    select_context_items,
)
from backend.services.memory.vault_manager import ConvDocVaultManager
from backend.services.memory.vault_media import promote_image_bytes, write_media_note
from backend.services.timeline import dirty_ranges

logger = logging.getLogger(__name__)
from backend.services import privacy, privacy_history_inventory, privacy_setup

router = APIRouter(prefix="/device-input", tags=["device-input"])
_PAIRING_TTL = timedelta(minutes=10)
_SOURCE_ONLINE_TTL = timedelta(minutes=2)
# Device input is staged in a MongoDB document before it is assembled into a
# Chronicle conversation. Stay below MongoDB's 16 MiB BSON document limit.
_MAX_AUDIO_BYTES = 12 * 1024 * 1024
_ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "video/mp4",  # ScreenPipe audio-only chunks use an MP4 container.
    "audio/ogg",
}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
# Frames an episode may stage for its picker; mirrors FRAMES_PER_EPISODE.
_MAX_EPISODE_FRAMES = 6


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_id(user: User) -> str:
    return str(user.user_id)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _effective_source_status(
    source: CaptureSource, now: Optional[datetime] = None
) -> str:
    if source.status != "online":
        return source.status
    # Immich is polled by Chronicle on a schedule; it does not send the frequent
    # heartbeats expected from live ScreenPipe capture agents.  Its last_seen_at
    # therefore means "last successful sync", not "last heartbeat".
    if source.provider == "immich":
        return "online"
    checked_at = _as_utc(now or utcnow())
    if source.last_seen_at is None:
        return "offline"
    if checked_at - _as_utc(source.last_seen_at) > _SOURCE_ONLINE_TTL:
        return "offline"
    return "online"


async def _device_source(authorization: str = Header(default="")) -> CaptureSource:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing device token")
    source = await CaptureSource.find_one(CaptureSource.token_hash == _digest(token))
    if source is None:
        raise HTTPException(status_code=401, detail="Invalid device token")
    return source


async def _mobile_source(user: User) -> CaptureSource:
    """Get or create the synthetic capture source a user's phone shares through.

    The phone already holds a JWT, so it never pairs: pairing would mint a second
    long-lived credential on the same device for no gain. ``token_hash`` is therefore
    deliberately unmatchable — ``_device_source`` only ever looks up a 64-char sha256
    hex digest, and this is not one — while still satisfying the unique index.
    """
    user_id = _user_id(user)
    source_id = f"mobile-{user_id}"
    source = await CaptureSource.find_one(
        CaptureSource.user_id == user_id,
        CaptureSource.source_id == source_id,
    )
    if source is None:
        source = CaptureSource(
            user_id=user_id,
            source_id=source_id,
            name="Phone",
            provider="mobile",
            platform="mobile",
            token_hash=f"unusable:{secrets.token_hex(32)}",
            capabilities=["manual_memory"],
        )
        try:
            await source.insert()
        except DuplicateKeyError:
            source = await CaptureSource.find_one(
                CaptureSource.user_id == user_id,
                CaptureSource.source_id == source_id,
            )
            if source is None:
                raise
    # "Last seen" for a share target genuinely means "last share", so the ordinary
    # heartbeat staleness rule in _effective_source_status applies without a special case.
    source.status = "online"
    source.last_seen_at = utcnow()
    await source.set(
        {
            CaptureSource.health: source.health,
            CaptureSource.status: source.status,
            CaptureSource.last_seen_at: source.last_seen_at,
        }
    )
    return source


class PairRequest(BaseModel):
    code: str
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)
    provider: Literal["screenpipe", "immich"] = "screenpipe"
    capabilities: list[str] = Field(default_factory=list)


class HeartbeatRequest(BaseModel):
    status: Literal["online", "offline", "error"] = "online"
    health: dict[str, Any] = Field(default_factory=dict)


class ActivityItem(BaseModel):
    source_item_id: str
    locator: EvidenceLocator
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityBatch(BaseModel):
    items: list[ActivityItem] = Field(max_length=1000)


class ObservationSample(BaseModel):
    captured_at: datetime
    elapsed_seconds: float = Field(ge=0)
    capture_trigger: str = Field(default="", max_length=100)
    text: str = Field(default="", max_length=2000)
    text_source: Optional[str] = Field(default=None, max_length=50)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frame_id: int
    liveness: bool = False
    inactive: bool = False


class ObservationEvent(BaseModel):
    event: Literal["open", "sample", "close"]
    source_item_id: str = Field(min_length=1, max_length=200)
    locator: EvidenceLocator
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    frame_candidates: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_FRAME_CANDIDATES
    )
    sample: Optional[ObservationSample] = None


class ObservationBatch(BaseModel):
    events: list[ObservationEvent] = Field(min_length=1, max_length=1000)


class JobRequest(BaseModel):
    source_id: str
    kind: Literal["screen_context", "thumbnail", "source_media"] = "screen_context"
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    purpose: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobCompletion(BaseModel):
    success: bool = True
    items: list[ActivityItem] = Field(default_factory=list)
    error: Optional[str] = None


def _source_locator(locator: EvidenceLocator, source: CaptureSource) -> EvidenceLocator:
    if locator.capture_source_id != source.source_id:
        raise HTTPException(
            status_code=422,
            detail="Evidence locator does not match authenticated capture source",
        )
    return locator


def _same_device_input_identity(
    existing: DeviceInputItem, requested: DeviceInputItem
) -> bool:
    """Return whether a duplicate key names the same immutable evidence item."""

    def stored_time(value: datetime) -> datetime:
        # BSON stores milliseconds, while collectors send sub-millisecond times.
        # Compare the persisted identity so replaying the same frame is idempotent.
        value = _as_utc(value)
        return value.replace(microsecond=value.microsecond // 1000 * 1000)

    def same_optional_time(left: Optional[datetime], right: Optional[datetime]) -> bool:
        if left is None or right is None:
            return left is right
        return stored_time(left) == stored_time(right)

    return (
        existing.user_id == requested.user_id
        and existing.source_id == requested.source_id
        and existing.kind == requested.kind
        and existing.source_item_id == requested.source_item_id
        and existing.locator == requested.locator
        and stored_time(existing.captured_at) == stored_time(requested.captured_at)
        and same_optional_time(existing.ended_at, requested.ended_at)
        and existing.content_hash == requested.content_hash
    )


@router.post("/pairing-codes")
async def create_pairing_code(user: User = Depends(current_active_user)):
    raw = secrets.token_urlsafe(9)
    expires_at = utcnow() + _PAIRING_TTL
    await PairingCode(
        user_id=_user_id(user), code_hash=_digest(raw), expires_at=expires_at
    ).insert()
    return {"code": raw, "expires_at": _utc_iso(expires_at)}


@router.post("/pair")
async def pair_source(body: PairRequest):
    code = await PairingCode.find_one(PairingCode.code_hash == _digest(body.code))
    if code is None or _as_utc(code.expires_at) <= utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code")
    if body.provider not in {"screenpipe", "immich"}:
        raise HTTPException(status_code=422, detail="Unsupported provider")
    raw_token = secrets.token_urlsafe(32)
    source_id = f"{body.provider}-{secrets.token_hex(8)}"
    source = CaptureSource(
        user_id=code.user_id,
        source_id=source_id,
        name=body.name,
        provider=body.provider,
        platform=body.platform,
        capabilities=body.capabilities,
        token_hash=_digest(raw_token),
        status="online",
        last_seen_at=utcnow(),
    )
    await source.insert()
    await code.delete()
    return {"source_id": source_id, "token": raw_token}


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, source: CaptureSource = Depends(_device_source)
):
    source.status = (
        body.status if body.status in {"online", "offline", "error"} else "error"
    )
    source.health = body.health
    source.last_seen_at = utcnow()
    await source.set(
        {
            CaptureSource.health: source.health,
            CaptureSource.status: source.status,
            CaptureSource.last_seen_at: source.last_seen_at,
        }
    )
    return {"ok": True, "source_id": source.source_id}


@router.post("/activity")
async def ingest_activity(
    body: ActivityBatch, source: CaptureSource = Depends(_device_source)
):
    accepted, duplicates = 0, 0
    for incoming in body.items:
        item = DeviceInputItem(
            user_id=source.user_id,
            source_id=source.source_id,
            kind="activity",
            source_item_id=incoming.source_item_id,
            locator=_source_locator(incoming.locator, source),
            captured_at=incoming.captured_at,
            ended_at=incoming.ended_at,
            metadata=incoming.metadata,
        )
        try:
            await item.insert()
            accepted += 1
            frame_id = incoming.metadata.get("representative_frame_id")
            if (
                frame_id is not None
                and "screen_context" in source.capabilities
                and any(
                    incoming.metadata.get(key)
                    for key in ("app_name", "window_name", "text")
                )
            ):
                await DeviceInputJob(
                    user_id=source.user_id,
                    source_id=source.source_id,
                    kind="thumbnail",
                    start_at=incoming.captured_at,
                    end_at=incoming.ended_at,
                    purpose="timeline_thumbnail",
                    payload={
                        "item_id": str(item.id),
                        "frame_id": frame_id,
                        "width": 960,
                    },
                ).insert()
        except DuplicateKeyError:
            duplicates += 1
            existing = await DeviceInputItem.find_one(
                DeviceInputItem.user_id == source.user_id,
                DeviceInputItem.source_id == source.source_id,
                DeviceInputItem.kind == "activity",
                DeviceInputItem.source_item_id == incoming.source_item_id,
            )
            if existing is not None:
                if existing.locator != incoming.locator:
                    raise HTTPException(
                        status_code=409,
                        detail="Activity locator changed for an existing source item",
                    )
                existing.ended_at = incoming.ended_at
                existing.metadata = incoming.metadata
                await existing.save()
    return {"accepted": accepted, "duplicates": duplicates}


def _frame_id_from_filename(filename: str | None) -> int | None:
    """Read the frame id out of a ``frame-<id>.<ext>`` upload name."""

    match = re.fullmatch(r"frame-(\d+)\.[A-Za-z0-9]+", filename or "")
    return int(match.group(1)) if match else None


def _candidate_seconds(candidate: dict[str, Any]) -> float:
    """Capture time of a candidate; frames without one sort to the head."""

    raw = candidate.get("captured_at")
    if isinstance(raw, datetime):
        return _as_utc(raw).timestamp()
    if isinstance(raw, str):
        try:
            return _as_utc(
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
            ).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _merge_frame_candidates(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union two shortlists, keeping one frame per slice of the observation's span.

    Truncating the union by score would undo the collector's stratification on every
    sample append: an observation that accumulates candidates over hours would end up
    holding whichever few frames happened to score highest, which are typically
    neighbours. Bucketing by capture time keeps the shortlist spanning the session.
    """

    by_frame: dict[int, dict[str, Any]] = {}
    for candidate in [*existing, *incoming]:
        frame_id = candidate.get("frame_id")
        if not isinstance(frame_id, int):
            continue
        current = by_frame.get(frame_id)
        if current is None or float(candidate.get("score") or 0) > float(
            current.get("score") or 0
        ):
            by_frame[frame_id] = candidate
    if len(by_frame) <= MAX_FRAME_CANDIDATES:
        return sorted(by_frame.values(), key=lambda candidate: candidate["frame_id"])
    moments = {
        frame_id: _candidate_seconds(candidate)
        for frame_id, candidate in by_frame.items()
    }
    start, end = min(moments.values()), max(moments.values())
    width = (end - start) / MAX_FRAME_CANDIDATES or 1.0
    best: dict[int, dict[str, Any]] = {}
    for frame_id, candidate in by_frame.items():
        bucket = min(int((moments[frame_id] - start) / width), MAX_FRAME_CANDIDATES - 1)
        current = best.get(bucket)
        if current is None or float(candidate.get("score") or 0) > float(
            current.get("score") or 0
        ):
            best[bucket] = candidate
    return sorted(best.values(), key=lambda candidate: candidate["frame_id"])


def _append_observation_sample(
    item: DeviceInputItem, sample: ObservationSample | None
) -> bool:
    if sample is None:
        return False
    incoming = sample.model_dump(mode="json")
    identity = (incoming["content_fingerprint"], incoming["captured_at"])
    if any(
        (row.get("content_fingerprint"), row.get("captured_at")) == identity
        for row in item.samples
    ):
        return False
    item.samples.append(incoming)
    item.samples.sort(key=lambda row: row["captured_at"])
    return True


def _observation_has_visual_context(item: DeviceInputItem) -> bool:
    if item.metadata.get("inactive") or not item.frame_candidates:
        return False
    latest_text = item.samples[-1].get("text") if item.samples else ""
    return bool(
        latest_text
        or item.metadata.get("app_name")
        or item.metadata.get("window_name")
        or item.metadata.get("browser_url")
    )


async def _ensure_observation_preview(
    item: DeviceInputItem, source: CaptureSource
) -> None:
    if (
        "screen_context" not in source.capabilities
        or item.media_data
        or not _observation_has_visual_context(item)
    ):
        return
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.kind == "thumbnail",
        {"payload.item_id": str(item.id)},
        {"status": {"$in": ["pending", "claimed", "complete"]}},
    )
    if existing:
        return
    frame_id = item.frame_candidates[0]["frame_id"]
    await DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="observation_preview",
        payload={
            "item_id": str(item.id),
            "frame_id": frame_id,
            "width": 960,
            "preview_index": 1,
        },
    ).insert()


@router.post("/screening")
async def submit_screening(
    body: privacy.ScreeningResult, source: CaptureSource = Depends(_device_source)
):
    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    try:
        await privacy.save_screening(source, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}


class ScreeningReplacement(BaseModel):
    replacement_interval_id: str = Field(min_length=1, max_length=256)
    previous_interval_ids: list[str] = Field(min_length=1, max_length=64)


@router.post("/screening/replace")
async def replace_screening(
    body: ScreeningReplacement, source: CaptureSource = Depends(_device_source)
):

    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    try:
        await screening_revisions.supersede(
            source, body.replacement_interval_id, body.previous_interval_ids
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}


@router.get("/screening/results")
async def screening_results(
    start_at: datetime,
    end_at: datetime,
    policy_version: str,
    after: str = "",
    source: CaptureSource = Depends(_device_source),
):
    """Collector-only metadata for rechecking its original frames locally."""
    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    start_at, end_at = privacy.utc(start_at), privacy.utc(end_at)
    if not timedelta(0) < end_at - start_at <= timedelta(days=32):
        raise HTTPException(422, "Choose a range of at most 32 days")
    if not 0 < len(policy_version) <= 64 or (
        after and not re.fullmatch(r"[a-f0-9]{64}", after)
    ):
        raise HTTPException(422, "Invalid screening query")
    query = {
        "user_id": source.user_id,
        "source_id": source.source_id,
        "started_at": {"$gte": start_at},
        "ended_at": {"$lte": end_at},
        "policy_version": policy_version,
        "superseded_by": {"$exists": False},
    }
    if after:
        query["_id"] = {"$gt": after}
    fields = {
        key: 1
        for key in (
            "interval_id",
            "track_id",
            "started_at",
            "ended_at",
            "policy_version",
            "model_version",
            "segments",
            "evidence.frame_id",
            "evidence.captured_at",
            "evidence.state",
        )
    }
    rows = (
        await privacy.database()
        .privacy_screening.find(query, fields)
        .sort("_id", 1)
        .limit(500)
        .to_list(None)
    )
    cursor = str(rows[-1]["_id"]) if len(rows) == 500 else None
    for row in rows:
        row.pop("_id", None)
        # Mongo's naïve BSON datetimes are UTC. Always put that timezone on the
        # wire so a collector in another timezone finds the original captures.
        for record in [row, *row.get("segments", []), *row.get("evidence", [])]:
            for field in ("started_at", "ended_at", "captured_at"):
                if field in record:
                    record[field] = privacy.utc(record[field])
    return {"results": rows, "next_cursor": cursor}


@router.post("/screening/displays")
async def submit_privacy_displays(
    body: privacy.PrivacyDisplaySet, source: CaptureSource = Depends(_device_source)
):
    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    try:
        await privacy.save_display_set(source, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}


@router.post("/screening/required-range")
async def submit_privacy_required_range(
    body: privacy.PrivacyRequiredRange, source: CaptureSource = Depends(_device_source)
):
    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    try:
        await privacy.save_required_range(source, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}


@router.post("/screening/required-range/inventory")
async def refine_privacy_required_range(
    body: privacy_history_inventory.InventoryRefinement,
    source: CaptureSource = Depends(_device_source),
):
    if source.provider != "screenpipe":
        raise HTTPException(400, "Screening requires a ScreenPipe source")
    try:
        await privacy_history_inventory.refine(source, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}


class PrivacyActivation(BaseModel):
    started_at: datetime


@router.post("/sources/{source_id}/privacy/prepare")
async def prepare_privacy(
    source_id: str, body: PrivacyActivation, user: User = Depends(current_active_user)
):
    source = await CaptureSource.find_one(
        {"source_id": source_id, "user_id": _user_id(user)}
    )
    if source is None:
        raise HTTPException(404, "Source not found")
    return await privacy_setup.prepare(source, body.started_at)


@router.post("/sources/{source_id}/privacy/activate")
async def activate_privacy(
    source_id: str, body: PrivacyActivation, user: User = Depends(current_active_user)
):

    source = await CaptureSource.find_one(
        {"source_id": source_id, "user_id": _user_id(user)}
    )
    if source is None:
        raise HTTPException(404, "Source not found")
    await privacy.ensure_indexes()
    db = privacy.database()
    owner = _user_id(user)
    query = {"source_id": source_id, "user_id": owner}
    current = await db.capture_sources.find_one(query)
    started_at = privacy.utc(body.started_at)
    now = privacy.utc(datetime.now(timezone.utc))
    if started_at > now:
        raise HTTPException(422, "Privacy activation cannot start in the future")
    operation = hashlib.sha256(
        f"{owner}:{source_id}:activate:{started_at.isoformat()}".encode()
    ).hexdigest()
    if (
        current.get("privacy_updating")
        and current.get("privacy_operation") != operation
    ):
        raise HTTPException(409, "Privacy policy update in progress")
    if current.get("privacy_enabled_from"):
        if not current.get("privacy_updating"):
            return {
                "active": True,
                "started_at": privacy.utc(current["privacy_enabled_from"]),
            }
        # The operation identity uses the requested start; the durable start
        # includes earlier waiting time and must survive every retry.
        started_at = privacy.utc(current["privacy_enabled_from"])
    else:
        waiting = await privacy_setup.waiting_starts(owner, [source_id])
        started_at = min(started_at, waiting.get(source_id, started_at))
        health = current.get("health", {}).get("privacy_screening", {})

        def fresh(value):
            try:
                return timedelta(0) <= now - privacy.utc(value) <= timedelta(minutes=2)
            except (AttributeError, TypeError, ValueError):
                return False

        if (
            source.provider != "screenpipe"
            or health.get("state") != "ready"
            or not health.get("model_version")
            or not fresh(current.get("last_seen_at"))
            or not fresh(health.get("inventory_checked_at"))
        ):
            raise HTTPException(409, "Screening worker readiness is missing or stale")
        inventory = await db.privacy_display_sets.find_one(
            query, sort=[("observed_at", -1)]
        )
        if not inventory or not inventory["track_ids"]:
            raise HTTPException(409, "Display coverage is not ready")
        if privacy.utc(inventory["observed_at"]) > privacy.utc(
            health["inventory_checked_at"]
        ):
            raise HTTPException(
                409, "Display coverage changed; wait for worker readiness"
            )
        for track in inventory["track_ids"]:
            completed = await db.privacy_screening.find_one(
                {
                    **query,
                    "track_id": track,
                    "model_version": health["model_version"],
                    "ended_at": {"$gte": now - timedelta(minutes=2), "$lte": now},
                }
            )
            if not completed:
                raise HTTPException(
                    409, "Waiting for successful screening on every display"
                )
        updated = await privacy.begin_update(
            owner,
            {
                **query,
                "privacy_enabled_from": None,
                "privacy_revision": current.get("privacy_revision", 0),
                "privacy_operation": None,
                "health": current.get("health", {}),
                "last_seen_at": current.get("last_seen_at"),
            },
            {
                "$set": {
                    "privacy_enabled_from": started_at,
                    "privacy_operation": operation,
                    "privacy_updating": True,
                },
                "$inc": {"privacy_revision": 1},
            },
        )
        if not updated.matched_count:
            raise HTTPException(409, "Privacy readiness changed; retry activation")
    # Historical activation changes eligibility immediately. Keep the fence held
    # until downstream invalidation is durably queued; the same request resumes it.
    await dirty_ranges.mark_evidence_dirty(
        owner, started_at, now, operation, "privacy_activation", source_kind="privacy"
    )
    # Normal activated coverage now holds every unresolved portion, including
    # pre-readiness time. Retain the setup evidence and retire only this type of
    # obligation, while the source is still fenced against publication.
    await db.privacy_required_ranges.update_many(
        privacy_setup.waiting_query(owner, [source_id]),
        {"$set": {"superseded_by": operation}},
    )
    await db.capture_sources.update_one(
        {**query, "privacy_operation": operation},
        {"$set": {"privacy_updating": False, "privacy_operation": None}},
    )
    return {"active": True, "started_at": started_at}


@router.get("/privacy/intervals")
async def privacy_intervals(
    start_at: datetime, end_at: datetime, user: User = Depends(current_active_user)
):
    if end_at <= start_at or end_at - start_at > timedelta(days=32):
        raise HTTPException(422, "Choose a range of at most 32 days")
    return {"intervals": await privacy.list_intervals(_user_id(user), start_at, end_at)}


class PrivacyOverride(BaseModel):
    source_id: str
    started_at: datetime
    ended_at: datetime
    revision: int = Field(ge=0)
    decision: Literal["allowed", "excluded"]


@router.post("/privacy/override")
async def override_privacy(
    body: PrivacyOverride, user: User = Depends(current_active_user)
):

    if body.ended_at <= body.started_at:
        raise HTTPException(422, "Invalid interval")
    owner = _user_id(user)
    operation = hashlib.sha256((owner + body.model_dump_json()).encode()).hexdigest()
    db = privacy.database()
    updated = await privacy.begin_update(
        owner,
        {
            "source_id": body.source_id,
            "user_id": owner,
            "privacy_revision": body.revision,
            "privacy_operation": None,
        },
        {
            "$inc": {"privacy_revision": 1},
            "$set": {"privacy_operation": operation, "privacy_updating": True},
        },
    )
    if not updated.matched_count:
        interrupted = await db.capture_sources.find_one(
            {
                "source_id": body.source_id,
                "user_id": owner,
                "privacy_revision": body.revision + 1,
                "privacy_operation": operation,
                "privacy_updating": True,
            }
        )
        if not interrupted:
            raise HTTPException(
                409, "Privacy decision changed; reload before reviewing"
            )
    await db.privacy_overrides.update_one(
        {"_id": operation},
        {
            "$setOnInsert": {
                "source_id": body.source_id,
                "user_id": owner,
                "started_at": privacy.utc(body.started_at),
                "ended_at": privacy.utc(body.ended_at),
                "override": body.decision,
                "revision": body.revision + 1,
            }
        },
        upsert=True,
    )
    await dirty_ranges.mark_evidence_dirty(
        owner,
        privacy.utc(body.started_at),
        privacy.utc(body.ended_at),
        operation,
        "privacy_decision",
        source_kind="privacy",
    )
    await db.capture_sources.update_one(
        {"user_id": owner, "source_id": body.source_id, "privacy_operation": operation},
        {"$set": {"privacy_updating": False, "privacy_operation": None}},
    )
    return {"revision": body.revision + 1, "decision": body.decision}


@router.post("/observations")
async def ingest_observations(
    body: ObservationBatch, source: CaptureSource = Depends(_device_source)
):
    accepted = 0
    duplicate_samples = 0
    touched: dict[str, DeviceInputItem] = {}
    for incoming in body.events:
        item = await DeviceInputItem.find_one(
            DeviceInputItem.user_id == source.user_id,
            DeviceInputItem.source_id == source.source_id,
            DeviceInputItem.kind == "observation",
            DeviceInputItem.source_item_id == incoming.source_item_id,
        )
        if item is None:
            if incoming.event != "open":
                raise HTTPException(
                    status_code=409,
                    detail=f"Observation {incoming.source_item_id} must be opened first",
                )
            item = DeviceInputItem(
                user_id=source.user_id,
                source_id=source.source_id,
                kind="observation",
                source_item_id=incoming.source_item_id,
                locator=_source_locator(incoming.locator, source),
                captured_at=incoming.captured_at,
                ended_at=incoming.ended_at,
                metadata=incoming.metadata,
                lifecycle="open",
                curation="pending",
                frame_candidates=incoming.frame_candidates,
            )
            _append_observation_sample(item, incoming.sample)
            try:
                await item.insert()
                accepted += 1
            except DuplicateKeyError:
                requested = item
                existing = await DeviceInputItem.find_one(
                    DeviceInputItem.user_id == source.user_id,
                    DeviceInputItem.source_id == source.source_id,
                    DeviceInputItem.kind == "observation",
                    DeviceInputItem.source_item_id == incoming.source_item_id,
                )
                if existing is None:
                    raise
                if not _same_device_input_identity(existing, requested):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Observation identity conflicts with an existing source item"
                        ),
                    )
                item = existing
        else:
            if item.locator != incoming.locator:
                raise HTTPException(
                    status_code=409,
                    detail="Observation locator changed after it was opened",
                )
            _source_locator(incoming.locator, source)
            item.metadata = {**item.metadata, **incoming.metadata}
            item.frame_candidates = _merge_frame_candidates(
                item.frame_candidates, incoming.frame_candidates
            )
            if incoming.ended_at is not None:
                item.ended_at = incoming.ended_at
            if _append_observation_sample(item, incoming.sample):
                item.curation = "pending"
                accepted += 1
            elif incoming.sample is not None:
                duplicate_samples += 1
            if incoming.event == "close":
                item.lifecycle = "closed"
                item.ended_at = incoming.ended_at or incoming.captured_at
                item.curation = "pending"
            await item.save()
        touched[str(item.id)] = item

    for item in touched.values():
        await _ensure_observation_preview(item, source)
    source.last_seen_at = utcnow()
    source.status = "online"
    await source.set(
        {
            CaptureSource.health: source.health,
            CaptureSource.status: source.status,
            CaptureSource.last_seen_at: source.last_seen_at,
        }
    )
    return {
        "accepted": accepted,
        "duplicate_samples": duplicate_samples,
        "observations": list(touched),
    }


@router.post("/audio")
async def ingest_audio(
    file: UploadFile = File(...),
    source_item_id: str = Form(...),
    captured_at: datetime = Form(...),
    duration_seconds: float = Form(..., ge=0),
    device_name: str = Form(...),
    track_id: str = Form(..., min_length=1, max_length=300),
    direction: Literal["input", "output", "unknown"] = Form(...),
    content_hash: str = Form(..., pattern=r"^[0-9a-fA-F]{64}$"),
    meeting_id: Optional[str] = Form(None, min_length=1, max_length=200),
    source: CaptureSource = Depends(_device_source),
):
    locator = EvidenceLocator(
        capture_source_id=source.source_id,
        modality="audio",
        track_id=track_id,
    )
    requested = DeviceInputItem(
        user_id=source.user_id,
        source_id=source.source_id,
        kind="audio",
        source_item_id=source_item_id,
        locator=locator,
        captured_at=captured_at,
        ended_at=captured_at + timedelta(seconds=duration_seconds),
        metadata={
            "device_name": device_name,
            "track_id": track_id,
            "direction": direction,
            "duration_seconds": duration_seconds,
            **({"meeting_id": meeting_id} if meeting_id else {}),
        },
        content_hash=content_hash.lower(),
    )
    existing = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == source.user_id,
        DeviceInputItem.source_id == source.source_id,
        DeviceInputItem.kind == "audio",
        DeviceInputItem.source_item_id == source_item_id,
    )
    if existing:
        if not _same_device_input_identity(existing, requested):
            raise HTTPException(
                status_code=409,
                detail="Audio identity conflicts with an existing source item",
            )
        return {"status": "duplicate", "item_id": str(existing.id)}
    compacted = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.user_id == source.user_id,
        AudioEvidenceSpan.source_id == source.source_id,
        {"source_item_ids": source_item_id},
    )
    if compacted:
        if compacted.locator != locator:
            raise HTTPException(
                status_code=409,
                detail="Audio locator conflicts with an existing compacted span",
            )
        return {"status": "duplicate", "compacted": True}
    if file.content_type not in _ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    data = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio chunk is too large")
    actual_hash = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_hash, content_hash.lower()):
        raise HTTPException(status_code=422, detail="Content hash mismatch")
    requested.media_data = data
    requested.media_filename = file.filename or "chunk.wav"
    requested.media_content_type = file.content_type
    requested.content_hash = actual_hash
    try:
        await requested.insert()
    except DuplicateKeyError:
        existing = await DeviceInputItem.find_one(
            DeviceInputItem.user_id == source.user_id,
            DeviceInputItem.source_id == source.source_id,
            DeviceInputItem.kind == "audio",
            DeviceInputItem.source_item_id == source_item_id,
        )
        if existing is None:
            raise
        if not _same_device_input_identity(existing, requested):
            raise HTTPException(
                status_code=409,
                detail="Audio identity conflicts with an existing source item",
            )
        return {"status": "duplicate", "item_id": str(existing.id)}
    return {"status": "accepted", "item_id": str(requested.id)}


@router.get("/jobs/next")
async def next_job(source: CaptureSource = Depends(_device_source)):
    await DeviceInputJob.find(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.status == "claimed",
        DeviceInputJob.claimed_at < utcnow() - timedelta(minutes=5),
    ).update_many({"$set": {"status": "pending", "claimed_at": None}})
    row = await DeviceInputJob.get_pymongo_collection().find_one_and_update(
        {"user_id": source.user_id, "source_id": source.source_id, "status": "pending"},
        {"$set": {"status": "claimed", "claimed_at": utcnow()}},
        sort=[("priority", -1), ("created_at", 1), ("_id", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if row is None:
        return {"job": None}
    job = DeviceInputJob.model_validate(row)
    return {
        "job": {
            "id": str(job.id),
            "kind": job.kind,
            "start_at": job.start_at,
            "end_at": job.end_at,
            "purpose": job.purpose,
            "payload": job.payload,
        }
    }


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str, body: JobCompletion, source: CaptureSource = Depends(_device_source)
):
    job = await DeviceInputJob.get(job_id)
    if job is None or job.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.context_request_id and job.status in {"complete", "failed"}:
        await dirty_ranges.notify_context_job_terminal(
            context_request_id=job.context_request_id,
            job_id=job_id,
            result_evidence_ids=job.payload.get("result_evidence_ids") or [],
        )
        return {
            "ok": True,
            "stored": int(job.payload.get("items_stored") or 0),
            "filtered": job.payload.get("items_filtered") or {},
        }
    settings = get_screen_context_settings()
    kept, report = select_context_items(
        body.items,
        max_bytes=settings["max_bytes_per_conversation"],
        similarity_threshold=settings["similarity_threshold"],
    )
    conversation_id = job.payload.get("conversation_id")
    result_evidence_ids: list[str] = []
    for incoming in kept:
        locator = _source_locator(incoming.locator, source)
        requested_locator = job.payload.get("locator")
        if requested_locator is not None and locator != EvidenceLocator.model_validate(
            requested_locator
        ):
            raise HTTPException(
                status_code=422,
                detail="Context result locator does not match the requested track",
            )
        item = DeviceInputItem(
            user_id=source.user_id,
            source_id=source.source_id,
            kind="screen_context",
            source_item_id=incoming.source_item_id,
            locator=locator,
            captured_at=incoming.captured_at,
            ended_at=incoming.ended_at,
            metadata={
                **incoming.metadata,
                "job_id": job_id,
                "purpose": job.purpose,
            },
            conversation_id=conversation_id,
            state="linked" if conversation_id else "received",
        )
        try:
            await item.insert()
        except DuplicateKeyError:
            existing = await DeviceInputItem.find_one(
                DeviceInputItem.user_id == source.user_id,
                DeviceInputItem.source_id == source.source_id,
                DeviceInputItem.kind == "screen_context",
                DeviceInputItem.source_item_id == incoming.source_item_id,
            )
            if existing is None:
                raise
            if not _same_device_input_identity(existing, item):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Context result identity conflicts with an existing source item"
                    ),
                )
            item = existing
        result_evidence_ids.append(f"observation:{item.id}")
    if report["over_budget"]:
        logger.warning(
            "Screen-context job %s hit the %d-byte budget; %d frames not stored",
            job_id,
            settings["max_bytes_per_conversation"],
            report["over_budget"],
        )
    if job.context_request_id and result_evidence_ids:
        await dirty_ranges.mark_evidence_dirty(
            source.user_id,
            job.start_at or min(item.captured_at for item in kept),
            job.end_at or max(item.ended_at or item.captured_at for item in kept),
            job_id,
            "timeline_context_acquired",
            source_kind="device_input",
            context_request_id=job.context_request_id,
        )
    job.status = "complete" if body.success else "failed"
    job.error = body.error
    job.completed_at = utcnow()
    job.payload = {
        **job.payload,
        "items_received": len(body.items),
        "items_stored": len(kept),
        "items_filtered": report,
        "result_evidence_ids": result_evidence_ids,
    }
    await job.save()
    if job.context_request_id:
        await dirty_ranges.notify_context_job_terminal(
            context_request_id=job.context_request_id,
            job_id=job_id,
            result_evidence_ids=result_evidence_ids,
        )
    return {"ok": True, "stored": len(kept), "filtered": report}


async def _store_episode_frames(
    job: DeviceInputJob, episode_id: str, files: list[UploadFile]
) -> dict[str, Any]:
    """Stage frames sampled across an episode's interval for its picker.

    Unlike an observation shortlist there is no pre-agreed set of ids to validate
    against — the node resolved these from its own frame store — so the filename is
    read for the id and nothing else is assumed.
    """

    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == job.user_id,
    )
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    frames: list[dict[str, Any]] = []
    for upload in files[:_MAX_EPISODE_FRAMES]:
        content_type = (upload.content_type or "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Frames must be images")
        frame_id = _frame_id_from_filename(upload.filename)
        if frame_id is None:
            raise HTTPException(
                status_code=422, detail="Frame filename must name an id"
            )
        data = await upload.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Frame exceeds the media limit")
        frames.append(
            {"frame_id": frame_id, "data": data, "content_type": content_type}
        )
    episode.frame_shortlist = sorted(frames, key=lambda frame: frame["frame_id"])
    # An interval the recorder never covered yields nothing; say so rather than
    # leaving the episode waiting on a request that already came back empty.
    episode.thumbnail_state = "requested" if frames else "unavailable"
    await episode.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "episode_id": episode_id, "frames": len(frames)}


@router.post("/jobs/{job_id}/previews")
async def complete_preview_batch_job(
    job_id: str,
    files: list[UploadFile] = File(...),
    source: CaptureSource = Depends(_device_source),
):
    """Store the shortlist of frames the curation agent will choose between.

    One request carries every frame of one observation, so a shortlist costs a single
    node round trip instead of one job per frame. This deliberately does not set
    ``media_data``: which frame represents the observation is the agent's decision,
    made after looking at them, not the scorer's.

    Each filename must be ``frame-<frame_id>.<ext>`` — that is how a stored preview is
    tied back to the candidate it came from.
    """

    job = await DeviceInputJob.get(job_id)
    if job is None or job.source_id != source.source_id or job.kind != "thumbnail":
        raise HTTPException(status_code=404, detail="Preview job not found")
    if job.purpose == "memory_space_note_review":
        # Lazy import keeps the optional vision path out of ordinary device uploads.
        from backend.services.memory_space_context import (
            FRAMES_PER_REVIEW,
            store_contact_sheet,
        )

        frames: list[dict[str, Any]] = []
        for upload in files[:FRAMES_PER_REVIEW]:
            content_type = (upload.content_type or "").split(";", 1)[0]
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=415, detail="Frames must be images")
            frame_id = _frame_id_from_filename(upload.filename)
            if frame_id is None:
                raise HTTPException(
                    status_code=422, detail="Frame filename must name an id"
                )
            data = await upload.read(_MAX_IMAGE_BYTES + 1)
            if len(data) > _MAX_IMAGE_BYTES:
                raise HTTPException(
                    status_code=413, detail="Frame exceeds the media limit"
                )
            frames.append(
                {"frame_id": frame_id, "data": data, "content_type": content_type}
            )
        try:
            conversation = await store_contact_sheet(job, frames)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        job.status = "complete"
        job.completed_at = utcnow()
        job.payload = {**job.payload, "frames_stored": len(frames)}
        await job.save()
        return {
            "ok": True,
            "conversation_id": conversation.conversation_id,
            "frames": len(frames),
        }
    episode_id = job.payload.get("episode_id")
    if episode_id:
        return await _store_episode_frames(job, episode_id, files)
    item_id = job.payload.get("item_id")
    item = await DeviceInputItem.get(item_id) if item_id else None
    if item is None or item.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Timeline item not found")
    requested = {int(frame_id) for frame_id in job.payload.get("frame_ids") or []}
    captured_at = {
        int(candidate["frame_id"]): candidate.get("captured_at")
        for candidate in item.frame_candidates
        if isinstance(candidate.get("frame_id"), int)
    }
    previews: list[dict[str, Any]] = []
    for upload in files[:MAX_FRAME_CANDIDATES]:
        content_type = (upload.content_type or "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Previews must be images")
        frame_id = _frame_id_from_filename(upload.filename)
        if frame_id is None or frame_id not in requested:
            raise HTTPException(
                status_code=422, detail="Preview filename must name a requested frame"
            )
        data = await upload.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="Preview exceeds the media limit"
            )
        previews.append(
            {
                "frame_id": frame_id,
                "data": data,
                "content_type": content_type,
                "captured_at": captured_at.get(frame_id),
            }
        )
    item.media_previews = sorted(previews, key=lambda preview: preview["frame_id"])
    item.metadata = {
        **item.metadata,
        "preview_shortlist_count": len(previews),
        # Frames that could not be served are gone from ScreenPipe's store for good.
        # Recording the shortfall stops the curation gate waiting on them.
        "preview_shortlist_missing": sorted(
            requested - {preview["frame_id"] for preview in previews}
        ),
    }
    await item.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "item_id": str(item.id), "previews": len(previews)}


@router.post("/jobs/{job_id}/thumbnail")
async def complete_thumbnail_job(
    job_id: str,
    file: UploadFile = File(...),
    source: CaptureSource = Depends(_device_source),
):
    job = await DeviceInputJob.get(job_id)
    if (
        job is None
        or job.source_id != source.source_id
        or job.kind not in {"thumbnail", "source_media"}
    ):
        raise HTTPException(status_code=404, detail="Thumbnail job not found")
    item_id = job.payload.get("item_id")
    item = await DeviceInputItem.get(item_id) if item_id else None
    if item is None or item.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Timeline item not found")
    content_type = (file.content_type or "").split(";", 1)[0]
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Thumbnail must be an image")
    data = await file.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Thumbnail exceeds the media limit")
    item.media_data = data
    item.media_filename = file.filename or "screenpipe-thumbnail.jpg"
    item.media_content_type = content_type
    item.content_hash = hashlib.sha256(data).hexdigest()
    item.metadata = {
        **item.metadata,
        "thumbnail_available": True,
        "preview_frame_id": job.payload.get("frame_id"),
        "preview_count": max(
            int(item.metadata.get("preview_count") or 0),
            int(job.payload.get("preview_index") or 1),
        ),
        "source_media_available": job.kind == "source_media"
        or bool(item.metadata.get("source_media_available")),
    }
    await item.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "item_id": str(item.id)}


@router.get("/sources")
async def list_sources(user: User = Depends(current_active_user)):
    rows = (
        await CaptureSource.find(CaptureSource.user_id == _user_id(user))
        .sort("-last_seen_at")
        .to_list()
    )
    waiting = await privacy_setup.waiting_starts(
        _user_id(user), [row.source_id for row in rows]
    )
    return {
        "sources": [
            {
                "source_id": row.source_id,
                "name": row.name,
                "provider": row.provider,
                "platform": row.platform,
                "status": _effective_source_status(row),
                "health": row.health,
                "last_seen_at": _utc_iso(row.last_seen_at),
                "capabilities": row.capabilities,
                "privacy_enabled_from": _utc_iso(row.privacy_enabled_from),
                "privacy_waiting_from": _utc_iso(waiting.get(row.source_id)),
                "privacy_revision": row.privacy_revision,
            }
            for row in rows
        ]
    }


@router.post("/jobs")
async def create_job(body: JobRequest, user: User = Depends(current_active_user)):
    source = await CaptureSource.find_one(
        CaptureSource.user_id == _user_id(user),
        CaptureSource.source_id == body.source_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    job = DeviceInputJob(
        user_id=_user_id(user),
        source_id=body.source_id,
        kind=body.kind,
        start_at=body.start_at,
        end_at=body.end_at,
        purpose=body.purpose,
        payload=body.payload,
        priority=100,
    )
    await job.insert()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/timeline")
async def timeline(
    start_at: datetime, end_at: datetime, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.captured_at <= end_at,
            {"$or": [{"ended_at": None}, {"ended_at": {"$gte": start_at}}]},
        )
        .sort("captured_at")
        .to_list()
    )
    rows = await privacy.filter_records(rows, _user_id(user))
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "source_item_id": row.source_item_id,
                "captured_at": _utc_iso(row.captured_at),
                "ended_at": _utc_iso(row.ended_at),
                "metadata": row.metadata,
                "state": row.state,
                "lifecycle": row.lifecycle,
                "curation": row.curation,
                "samples": row.samples,
                "frame_candidates": row.frame_candidates,
                "related_conversation_ids": row.related_conversation_ids,
                "duplicate_of": row.duplicate_of,
                "agent_reason": row.agent_reason,
                "vault_paths": row.vault_paths,
            }
            for row in rows
        ]
    }


@router.get("/conversations/{conversation_id}/context")
async def conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.conversation_id == conversation_id,
        )
        .sort("captured_at")
        .to_list()
    )
    rows = await privacy.filter_records(rows, _user_id(user))
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "captured_at": _utc_iso(row.captured_at),
                "ended_at": _utc_iso(row.ended_at),
                "metadata": row.metadata,
                "state": row.state,
            }
            for row in rows
        ]
    }


@router.post("/conversations/{conversation_id}/request-context")
async def request_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    owner = _user_id(user)
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id,
        Conversation.user_id == owner,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    jobs = await request_conversation_context_jobs(conversation)
    return {"jobs": jobs}


@router.delete("/conversations/{conversation_id}/context")
async def clear_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    result = await DeviceInputItem.find(
        DeviceInputItem.user_id == _user_id(user),
        DeviceInputItem.conversation_id == conversation_id,
        DeviceInputItem.state != "promoted",
    ).update_many({"$set": {"conversation_id": None, "state": "received"}})
    return {"cleared": result.modified_count}


async def _owned_item(item_id: str, user: User) -> DeviceInputItem:
    try:
        item = await DeviceInputItem.get(item_id)
    except Exception:
        item = None
    if item is None or item.user_id != _user_id(user):
        raise HTTPException(status_code=404, detail="Context item not found")
    try:
        await privacy.require_record(item)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc
    return item


async def _immich_bytes(asset_id: str, endpoint: str) -> tuple[bytes, str]:
    base = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    if not base or not key:
        raise HTTPException(status_code=503, detail="Immich is not configured")
    async with httpx.AsyncClient(
        timeout=60, headers={"x-api-key": key, "Accept": "image/*"}
    ) as client:
        response = await client.get(f"{base}/api/assets/{asset_id}/{endpoint}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Source asset is unavailable")
        response.raise_for_status()
        if len(response.content) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="Source image exceeds the media limit"
            )
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[
            0
        ]
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=415, detail="Source did not return an image"
            )
        return response.content, content_type


@router.get("/items/{item_id}/thumbnail")
async def context_thumbnail(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    if (
        item.media_data
        and item.media_content_type
        and item.media_content_type.startswith("image/")
    ):
        return Response(
            content=item.media_data,
            media_type=item.media_content_type,
            headers={"Cache-Control": "private, no-store"},
        )
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409, detail="This source does not expose an immediate thumbnail"
        )
    data, content_type = await _immich_bytes(str(asset_id), "thumbnail?size=thumbnail")
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/items/{item_id}/request-thumbnail")
async def request_item_thumbnail(
    item_id: str, user: User = Depends(current_active_user)
):
    item = await _owned_item(item_id, user)
    if item.kind not in {"activity", "observation"}:
        raise HTTPException(
            status_code=409, detail="Only screen items have source frames"
        )
    if item.media_data:
        return {"status": "complete"}
    frame_id = (
        item.metadata.get("representative_frame_id")
        or item.metadata.get("last_frame_id")
        or item.metadata.get("first_frame_id")
    )
    if frame_id is None:
        raise HTTPException(status_code=409, detail="Activity has no source frame")
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == item.source_id,
        DeviceInputJob.kind == "thumbnail",
        {"payload.item_id": item_id},
        {"status": {"$in": ["pending", "claimed"]}},
    )
    if existing:
        return {"status": existing.status, "job_id": str(existing.id)}
    job = DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="timeline_thumbnail",
        payload={"item_id": item_id, "frame_id": frame_id, "width": 960},
        priority=100,
    )
    await job.insert()
    return {"status": "pending", "job_id": str(job.id)}


@router.post("/items/{item_id}/promote")
async def promote_context_item(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    if item.promoted_path:
        return {"status": "duplicate", "path": item.promoted_path}
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409,
            detail="Source-media retrieval is not available for this item",
        )
    data, content_type = await _immich_bytes(str(asset_id), "original")
    root = ConvDocVaultManager().user_root(_user_id(user))
    try:
        media_path, digest = await asyncio.to_thread(
            promote_image_bytes, data, content_type, root
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    note_path = await asyncio.to_thread(
        write_media_note,
        media_path,
        digest,
        root,
        frontmatter={
            "source": "immich",
            "asset_id": asset_id,
            "captured_at": item.captured_at.isoformat(),
        },
    )
    item.promoted_path = media_path
    item.state = "promoted"
    await item.save()
    return {"status": "promoted", "path": item.promoted_path, "note": note_path}
