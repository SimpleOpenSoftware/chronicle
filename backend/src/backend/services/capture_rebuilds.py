"""Retire derived use of an immutable capture while rebuilding from its source.

Holds target original capture identities, so a corrected capture at the same
source/time can be processed under the ordinary screen policy. No raw bytes or
timestamps are edited, and a time override cannot release stale derivatives.
"""

import hashlib
import json

import backend.services.timeline.dirty_ranges as dirty_ranges
from backend.services import privacy


async def hold_for_rebuild(source, capture_session_ids, reason):
    identifiers = sorted(set(capture_session_ids))
    if not identifiers or not reason or len(reason) > 100:
        raise ValueError("Capture rebuild requires explicit identities and a reason")
    db = privacy.database()
    owner, source_id = str(source.user_id), source.source_id
    scope = {"user_id": owner, "source_id": source_id}
    operation = hashlib.sha256(
        json.dumps([owner, source_id, identifiers, reason]).encode()
    ).hexdigest()

    async def evidence():
        conflict = await db.privacy_capture_holds.find_one(
            {
                **scope,
                "capture_session_id": {"$in": identifiers},
                "operation": {"$ne": operation},
            }
        )
        if conflict:
            raise ValueError("Capture already belongs to another rebuild operation")
        captures = await db.audio_capture_sessions.find(
            {
                "user_id": owner,
                "capture_session_id": {"$in": identifiers},
            }
        ).to_list(None)
        if len(captures) != len(identifiers):
            raise ValueError("Capture rebuild evidence is missing")
        rows = []
        for capture in captures:
            capture_source = capture["capture_source_id"]
            if capture_source != source_id and not capture_source.startswith(
                source_id + ":"
            ):
                raise ValueError("Capture rebuild cannot cross sources")
            chunks = await db.audio_chunks.find(
                {
                    "user_id": owner,
                    "capture_session_id": capture["capture_session_id"],
                    "capture_source_id": capture_source,
                },
                {"_id": 1},
            ).to_list(None)
            if not chunks or not capture.get("ended_at"):
                raise ValueError("Capture rebuild requires retained, closed evidence")
            rows.append(
                {
                    **scope,
                    "capture_session_id": capture["capture_session_id"],
                    "chunk_ids": sorted(str(row["_id"]) for row in chunks),
                    "started_at": privacy.utc(capture["started_at"]),
                    "ended_at": privacy.utc(capture["ended_at"]),
                    "reason": reason,
                    "operation": operation,
                }
            )
        return rows

    rows = await evidence()
    current = await db.capture_sources.find_one(scope)
    if current is None:
        raise ValueError("Capture source is unavailable")
    stored = await db.privacy_capture_holds.find(
        {**scope, "operation": operation}
    ).to_list(None)
    if len(stored) == len(rows) and current.get("privacy_operation") != operation:
        return
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
    # A failure from here leaves the source held until identical replay succeeds.
    rows = await evidence()
    for row in rows:
        identity = hashlib.sha256(
            json.dumps([owner, source_id, row["capture_session_id"]]).encode()
        ).hexdigest()
        await db.privacy_capture_holds.update_one(
            {"_id": identity},
            {"$setOnInsert": row},
            upsert=True,
        )

    await dirty_ranges.mark_evidence_dirty(
        owner,
        min(row["started_at"] for row in rows),
        max(row["ended_at"] for row in rows),
        operation,
        "privacy_capture_rebuild",
        source_kind="privacy",
    )
    await db.capture_sources.update_one(
        {**scope, "privacy_operation": operation},
        {
            "$set": {"privacy_operation": None, "privacy_updating": False},
        },
    )
