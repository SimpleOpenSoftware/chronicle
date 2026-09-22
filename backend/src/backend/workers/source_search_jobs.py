"""Durable cursors make search rebuilding resumable without a migration."""

from datetime import datetime, timezone
from time import monotonic

import backend.services.timeline.recording_sessions as recording_sessions
from backend.models.job import async_job
from backend.models.session_memory import UndatedSession
from backend.models.timeline import TimelineDay
from backend.services import privacy
from backend.services.redis_lock import distributed_lock


@async_job(redis=True, beanie=True)
async def assess_context_job(identifier, *, redis_client=None):
    # Defer this dependency to break the import cycle through
    # backend.services.timeline.accepted_context -> backend.workers.source_search_jobs.
    from backend.services.timeline.accepted_context import assess_scope

    user_id, space = identifier.split("|", 1)
    async with distributed_lock(
        "context-assessment:" + identifier, timeout=1700, blocking_timeout=1
    ):
        return await assess_scope(user_id, None if space == "main" else space)


@async_job(redis=True, beanie=True)
async def index_sources_job(identifier="recovery", *, redis_client=None):
    started = monotonic()
    for _ in range(10):
        more = await _index_batch(identifier)
        if not more or monotonic() - started >= 45:
            break


async def _index_batch(identifier):
    # Defer this dependency to break the import cycle through backend.services.source_search
    # -> backend.workers.source_search_jobs.
    from backend.services.source_search import (
        VERSION,
        db,
        ensure_indexes,
        index_day,
        index_recording,
    )

    async with distributed_lock("source-search-index", timeout=280, blocking_timeout=1):
        await ensure_indexes()
        state = await db().source_search_jobs.find_one({"_id": identifier}) or {}
        if state.get("version") != VERSION:
            state = {
                "version": VERSION,
                "state": "queued",
                "initialized": False,
                "completed": 0,
            }
            await db().source_search_jobs.replace_one(
                {"_id": identifier}, {"_id": identifier, **state}, upsert=True
            )
        if state.get("attempts", 0) >= 3:
            return 0
        collection = state.get("collection", "conversations")
        after = state.get("after")
        query = {"_id": {"$gt": after}} if after else {}
        rows = (
            await db()[collection]
            .find(query, {"_id": 1, "conversation_id": 1})
            .sort("_id", 1)
            .limit(200 if collection == "conversations" else 5)
            .to_list()
        )
        await db().source_search_jobs.update_one(
            {"_id": identifier},
            {"$set": {"state": "running", "error": None, "privacy_waiting": False}},
            upsert=True,
        )
        completed = state.get("completed", 0)
        privacy_held = state.get("privacy_held", 0)
        visibility = privacy.ConversationPrivacyFilter()
        try:
            for row in rows:
                if collection == "conversations":
                    indexed = await index_recording(
                        row["conversation_id"], visibility=visibility
                    )
                elif collection == "timeline_days":
                    day = await TimelineDay.get(row["_id"])
                    indexed = await index_day(day, visibility=visibility)
                else:

                    session = await UndatedSession.get(row["_id"])
                    latest = (
                        await UndatedSession.find({"session_key": session.session_key})
                        .sort("-revision")
                        .first_or_none()
                    )
                    indexed = None
                    if latest.id == session.id:
                        indexed = await recording_sessions.index_undated(
                            session, visibility=visibility
                        )
                if indexed is False:
                    privacy_held += 1
                completed += 1
                await db().source_search_jobs.update_one(
                    {"_id": identifier},
                    {
                        "$set": {
                            "after": row["_id"],
                            "completed": completed,
                            "privacy_held": privacy_held,
                            "collection": collection,
                            "attempts": 0,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            if not rows:
                done = collection == "undated_sessions"
                await db().source_search_jobs.update_one(
                    {"_id": identifier},
                    {
                        "$set": {
                            "after": None,
                            "collection": {
                                "conversations": "timeline_days",
                                "timeline_days": "undated_sessions",
                                "undated_sessions": "conversations",
                            }[collection],
                            "state": "complete" if done else "queued",
                            "completed": 0 if done else completed,
                            "initialized": done or state.get("initialized", False),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            else:
                await db().source_search_jobs.update_one(
                    {"_id": identifier}, {"$set": {"state": "queued"}}
                )
        except privacy.PrivacyHeld:
            # Preserve the cursor for retry without exhausting failure attempts.
            await db().source_search_jobs.update_one(
                {"_id": identifier},
                {
                    "$set": {
                        "state": "queued",
                        "error": None,
                        "privacy_waiting": True,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            return False
        except Exception as exc:
            await db().source_search_jobs.update_one(
                {"_id": identifier},
                {
                    "$set": {"state": "failed", "error": type(exc).__name__},
                    "$inc": {"attempts": 1},
                },
            )
            raise
        return len(rows) == (200 if collection == "conversations" else 5)
