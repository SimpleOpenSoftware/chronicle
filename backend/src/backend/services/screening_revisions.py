"""Explicit, replayable replacement of completed automated screening results."""

import hashlib
import json

from backend.services import privacy


async def supersede(source, replacement_id, previous_ids):
    previous_ids = sorted(set(previous_ids))
    if not previous_ids or replacement_id in previous_ids:
        raise ValueError("A replacement must reference distinct previous results")
    db = privacy.database()
    owner, source_id = source.user_id, source.source_id
    scope = {"user_id": owner, "source_id": source_id}
    operation = hashlib.sha256(
        json.dumps([owner, source_id, replacement_id, previous_ids]).encode()
    ).hexdigest()

    async def validate():
        replacement = await db.privacy_screening.find_one(
            {**scope, "interval_id": replacement_id}
        )
        previous = await db.privacy_screening.find(
            {**scope, "interval_id": {"$in": previous_ids}}
        ).to_list(None)
        if not replacement or len(previous) != len(previous_ids):
            raise ValueError("Screening replacement evidence is missing")
        replay = all(row.get("superseded_by") == replacement_id for row in previous)
        if replacement.get("superseded_by") and not replay:
            raise ValueError("The replacement result has already been superseded")
        for row in previous:
            if any(
                row[key] != replacement[key]
                for key in ("track_id", "started_at", "ended_at")
            ):
                raise ValueError(
                    "Screening replacements must cover the exact same track and interval"
                )
            if row.get("superseded_by") not in (None, replacement_id):
                raise ValueError("A previous result already has another replacement")
        return replacement, previous

    replacement, previous = await validate()
    current = await db.capture_sources.find_one(scope)
    if current is None:
        raise ValueError("Screening source is unavailable")
    replay = all(row.get("superseded_by") == replacement_id for row in previous)
    if replay and current.get("privacy_operation") != operation:
        return
    # The source-wide hold serializes replacement against other policy writes.
    # Its revision invalidates jobs even when the replacement relaxes a hold.
    acquired = await privacy.begin_update(
        owner,
        {
            **scope,
            "$or": [{"privacy_operation": None}, {"privacy_operation": operation}],
        },
        {
            "$inc": {
                "privacy_revision": int(current.get("privacy_operation") != operation)
            },
            "$set": {"privacy_operation": operation, "privacy_updating": True},
        },
    )
    if not acquired.matched_count:
        raise ValueError("Privacy policy update in progress")
    try:
        # Recheck after acquiring the fence: another replacement may have won
        # between the initial read and acquisition. No rows have changed yet.
        replacement, previous = await validate()
    except ValueError:
        await db.capture_sources.update_one(
            {**scope, "privacy_operation": operation},
            {"$set": {"privacy_operation": None, "privacy_updating": False}},
        )
        raise
    await db.privacy_screening.update_many(
        {**scope, "interval_id": {"$in": previous_ids}},
        {
            "$set": {
                "superseded_by": replacement_id,
                "supersession_operation": operation,
            }
        },
    )
    # Any failure after the write leaves the source held. The identical request
    # resumes invalidation and releases the hold only once it succeeds.
    from backend.services.timeline import dirty_ranges

    await dirty_ranges.mark_evidence_dirty(
        owner,
        replacement["started_at"],
        replacement["ended_at"],
        operation,
        "privacy_screening_replacement",
        source_kind="privacy",
    )
    await db.capture_sources.update_one(
        {**scope, "privacy_operation": operation},
        {"$set": {"privacy_operation": None, "privacy_updating": False}},
    )
