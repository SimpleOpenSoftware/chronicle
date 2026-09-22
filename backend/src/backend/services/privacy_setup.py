"""Hold a source during local screening setup, independently of worker readiness.

The open-ended obligation is not a prediction. Activation replaces it with
normal screen coverage requirements starting at the same (or earlier) time.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.services import privacy
from backend.services.timeline import dirty_ranges

WAITING_COVERAGE = "awaiting_screening_worker"
OPEN_END = datetime.max.replace(tzinfo=timezone.utc, microsecond=999000)


def waiting_query(owner, source_ids):
    return {
        "user_id": str(owner),
        "source_id": {"$in": list(source_ids)},
        "coverage": WAITING_COVERAGE,
        "superseded_by": {"$exists": False},
    }


async def waiting_starts(owner, source_ids):
    rows = (
        await privacy.database()
        .privacy_required_ranges.find(
            waiting_query(owner, source_ids), {"source_id": 1, "started_at": 1}
        )
        .to_list(length=None)
    )
    starts = {}
    for row in rows:
        start = privacy.utc(row["started_at"])
        starts[row["source_id"]] = min(starts.get(row["source_id"], start), start)
    return starts


async def prepare(source, started_at):
    owner, source_id = str(source.user_id), source.source_id
    if source.provider != "screenpipe":
        raise HTTPException(422, "Local screen privacy requires a ScreenPipe source")
    start = privacy.utc(started_at)
    now = privacy.utc(datetime.now(timezone.utc))
    if start > now:
        raise HTTPException(422, "Privacy setup cannot start in the future")
    await privacy.ensure_indexes()
    db = privacy.database()
    scope = {"user_id": owner, "source_id": source_id}
    operation = hashlib.sha256(
        f"{owner}:{source_id}:awaiting-screening-worker:{start.isoformat()}".encode()
    ).hexdigest()
    current = await db.capture_sources.find_one(scope)
    if current is None:
        raise HTTPException(404, "Source not found")
    existing = await db.privacy_required_ranges.find_one({"_id": operation, **scope})
    pending = {**scope, "privacy_operation": operation, "privacy_updating": True}
    resuming = (
        current.get("privacy_updating")
        and current.get("privacy_operation") == operation
    )
    if current.get("privacy_updating") and not resuming:
        raise HTTPException(409, "Privacy policy update in progress")
    if existing and not resuming:
        return {
            "active": bool(current.get("privacy_enabled_from")),
            "started_at": start,
        }
    enabled = current.get("privacy_enabled_from")
    if enabled:
        if start < privacy.utc(enabled):
            raise HTTPException(
                409, "Use historical screening to cover earlier captures"
            )
        return {"active": True, "started_at": privacy.utc(enabled)}
    if not existing and not resuming and now - start > timedelta(days=32):
        raise HTTPException(422, "Prepare at most 32 days of earlier captures")
    if not resuming:
        changed = await privacy.begin_update(
            owner,
            {
                **scope,
                "privacy_enabled_from": None,
                "privacy_operation": None,
                "privacy_revision": current.get("privacy_revision", 0),
            },
            {
                "$inc": {"privacy_revision": 1},
                "$set": {"privacy_operation": operation, "privacy_updating": True},
            },
        )
        if not changed.matched_count:
            raise HTTPException(409, "Privacy settings changed; retry setup")
    await db.privacy_required_ranges.update_one(
        {"_id": operation},
        {
            "$setOnInsert": {
                **scope,
                "started_at": start,
                "ended_at": OPEN_END,
                "track_ids": [],
                "coverage": WAITING_COVERAGE,
                "policy_version": "screen-privacy-v3",
            }
        },
        upsert=True,
    )
    # Future captures have no derivatives yet. Never invalidate to OPEN_END.
    await dirty_ranges.mark_evidence_dirty(
        owner, start, now, operation, "privacy_screening_setup", source_kind="privacy"
    )
    await db.capture_sources.update_one(
        pending, {"$set": {"privacy_operation": None, "privacy_updating": False}}
    )
    return {"active": False, "started_at": start}
