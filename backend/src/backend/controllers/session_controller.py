"""
Session controller for handling audio session-related business logic.

This module manages Redis-based audio streaming sessions, including:
- Session metadata and status
- Conversation counts per session
- Session lifecycle tracking
"""

import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from backend.controllers.queue_controller import (
    PendingWork,
    default_queue,
    memory_queue,
    pending_work_owners,
    transcription_queue,
)
from backend.services import privacy
from backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
    session_append_closed,
)
from backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
    SessionView,
)

logger = logging.getLogger(__name__)

# How long a settled session's hash is kept before Redis reclaims it. Applied once,
# at the moment the session is first observed drained, so it never counts down on
# live work. Without it the store only grows: the oldest completed session found
# here was 50 days old and was still being re-examined on every poll.
SETTLED_SESSION_RETENTION_SECONDS = 7 * 24 * 3600


def _is_uninitialized(view: SessionView) -> bool:
    """Whether a hash exists but never became a session.

    Test probes and abandoned initializations leave hashes carrying no device, no
    status, and no start time. They can never reach FINISHED, so they were reported
    as active indefinitely — two such hashes here were being shown as live
    recordings with an age of 56 years.
    """
    return not view.client_id and view.status is None and view.started_at == 0.0


def _newest_session_per_client(views: list) -> dict:
    """Map each device to its most recently started session."""
    newest: dict = {}
    for view in views:
        if not view.client_id:
            continue
        current = newest.get(view.client_id)
        if current is None or view.started_at > current[1]:
            newest[view.client_id] = (view.session_id, view.started_at)
    return {client: session for client, (session, _) in newest.items()}


def _jobs_drained(
    view: SessionView, pending: PendingWork, newest_by_client: dict
) -> bool:
    """Whether this session's work has all reached a terminal state.

    Drainage is monotonic, so a recorded observation is authoritative and is never
    recomputed — that is what keeps a long-settled session free.

    Otherwise the session is matched against the owners of the work actually in
    flight. Jobs stamped with a ``session_id`` answer exactly. A job that knows only
    its device is attributed to that device's *newest* session, because a job
    enqueued now belongs to the recording happening now: attributing it to every
    session the device ever had is what previously kept finished sessions pinned
    open behind their successor's work.
    """
    if view.jobs_drained_at is not None:
        return True
    if view.session_id in pending.session_ids:
        return False
    if view.client_id in pending.client_ids:
        return newest_by_client.get(view.client_id) != view.session_id
    return True


def _session_info_dict(view: SessionView, conversation_count: int) -> dict:
    """Shape a SessionView into the session-info response dict used by the API."""
    now = time.time()
    started = view.started_at if math.isfinite(view.started_at) else None
    last_chunk = view.last_chunk_at if math.isfinite(view.last_chunk_at) else None
    return {
        "session_id": view.session_id,
        "user_id": view.user_id,
        "client_id": view.client_id,
        "provider": view.provider,
        "mode": view.mode,
        "status": view.status.value if view.status else "",
        "websocket_connected": view.websocket_connected,
        "completion_reason": view.completion_reason,
        "chunks_published": view.chunks_published,
        "started_at": started,
        "last_chunk_at": last_chunk,
        "age_seconds": now - started if started is not None else None,
        "idle_seconds": now - last_chunk if last_chunk is not None else None,
        "conversation_count": conversation_count,
        # Speech detection events
        "last_event": view.last_event,
        "speech_detected_at": view.speech_detected_at,
        "speaker_check_status": (
            view.speaker_check_status.value if view.speaker_check_status else ""
        ),
        "identified_speakers": ",".join(view.identified_speakers),
    }


def _redis_text(value):
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


async def stream_diagnostics(redis_client, stream_name):
    """Read Redis metadata without returning first/last message payloads."""
    info = await redis_client.xinfo_stream(stream_name)
    groups = []
    for group in await redis_client.xinfo_groups(stream_name):
        name = _redis_text(group["name"])
        consumers = [
            {
                "name": _redis_text(row["name"]),
                "pending": int(row["pending"]),
                "idle_ms": int(row["idle"]),
            }
            for row in await redis_client.xinfo_consumers(stream_name, name)
        ]
        groups.append(
            {
                "name": name,
                "consumers": consumers,
                "pending": max(
                    int(group["pending"]), sum(row["pending"] for row in consumers)
                ),
                "last_delivered_id": _redis_text(group["last-delivered-id"]),
            }
        )
    return {
        "stream_length": int(info["length"]),
        "first_entry_id": (
            _redis_text(info["first-entry"][0]) if info["first-entry"] else None
        ),
        "last_entry_id": (
            _redis_text(info["last-entry"][0]) if info["last-entry"] else None
        ),
        "consumer_groups": groups,
        "total_pending": sum(group["pending"] for group in groups),
    }


async def _allowed_session_details(views, visibility):
    """Check session diagnostics under the capture owner and canonical recording."""
    references = {
        view.active_conversation_id for view in views if view.active_conversation_id
    }
    allowed_recordings = {
        row["conversation_id"]
        for row in await visibility.filter(
            [{"conversation_id": identifier} for identifier in references]
        )
    }
    allowed = set()
    for view in views:
        if not view.user_id or not view.client_id or not view.started_at:
            continue
        if (
            view.active_conversation_id
            and view.active_conversation_id not in allowed_recordings
        ):
            continue
        owner = str(view.user_id)
        if owner not in visibility.snapshots:
            visibility.snapshots[owner] = await privacy.load_snapshot(owner)
        end = view.completed_at or max(view.last_chunk_at, time.time())
        try:
            permitted = visibility.snapshots[owner].permits(
                view.client_id,
                datetime.fromtimestamp(view.started_at, timezone.utc),
                datetime.fromtimestamp(end, timezone.utc),
            )
        except (ValueError, TypeError, OverflowError):
            permitted = False
        if permitted:
            allowed.add(view.session_id)
    return allowed


async def get_streaming_status(request, current_user, *, visibility=None):
    """Get status of active streaming sessions and Redis Streams health."""
    try:
        # Get Redis client from request.app.state (initialized during startup)
        redis_client = request.app.state.redis_audio_stream

        if not redis_client:
            return JSONResponse(
                status_code=503,
                content={"error": "Redis client for audio streaming not initialized"},
            )

        # Get all sessions (both active and completed)
        store = SessionStore(redis_client)
        active_sessions = []
        completed_sessions_from_redis = []

        visibility = visibility or privacy.ConversationPrivacyFilter()
        views = [
            v
            async for v in store.iter_views()
            if not _is_uninitialized(v)
            and (current_user.is_superuser or v.user_id == str(current_user.user_id))
        ]
        allowed_details = await _allowed_session_details(views, visibility)
        newest_by_client = _newest_session_per_client(views)

        # One scan for the whole response, off the event loop. The registries do not
        # vary by session, so the previous per-session call repeated an identical
        # scan once per view — 67 of them here — with blocking redis-py inside an
        # `async def`, which pins the single uvicorn loop thread and stalls every
        # other request in the process. Skipped entirely once every session has
        # already settled, which is the steady state.
        pending = (
            await asyncio.to_thread(pending_work_owners)
            if any(v.jobs_drained_at is None for v in views)
            else PendingWork(frozenset(), frozenset())
        )

        for view in views:
            conversation_count = await store.get_conversation_count(view.session_id)
            session_obj = _session_info_dict(view, conversation_count)
            if view.session_id not in allowed_details:
                session_obj.update(
                    completion_reason="",
                    last_event="",
                    identified_speakers="",
                    privacy_held=True,
                    privacy_reason="Private or unscreened session details held",
                )

            # Separate active and completed sessions
            # Check if all jobs are complete (including failed jobs)
            all_jobs_done = _jobs_drained(view, pending, newest_by_client)

            # Session is completed ONLY when:
            # 1. Status was already set to "finished" by an authoritative source
            #    (WebSocket disconnect handler or job handler), AND
            # 2. All RQ jobs are in terminal state
            #
            # IMPORTANT: Do NOT mark sessions as finished here. Between conversations
            # (after open_conversation_job finishes, before speech detection restarts),
            # all jobs are briefly terminal. Writing "finished" during this gap kills
            # the session permanently.
            if view.status == SessionStatus.FINISHED and all_jobs_done:
                if view.jobs_drained_at is None:
                    # Both terminal conditions hold, and neither can revert, so the
                    # answer is recorded rather than re-derived on the next poll —
                    # and the hash starts its retention countdown.
                    await store.mark_jobs_drained(
                        view.session_id, retention=SETTLED_SESSION_RETENTION_SECONDS
                    )
                completed_sessions_from_redis.append(
                    {
                        "session_id": view.session_id,
                        "client_id": view.client_id,
                        "completed_at": view.completed_at or view.last_chunk_at,
                        "conversation_count": conversation_count,
                    }
                )
            else:
                # Active session (including inter-conversation gaps where all jobs
                # are temporarily terminal but status is still "active")
                active_sessions.append(session_obj)

        # Get stream health for all session-scoped streams.
        # Categorize as active or completed based on consumer activity
        active_streams = {}
        completed_streams = {}

        # Create a map of session_id to session for quick lookup.
        session_by_id = {}
        for session in active_sessions + completed_sessions_from_redis:
            session_id = session.get("session_id")
            if session_id:
                session_by_id[session_id] = session

        # Discover all audio streams
        stream_keys = await redis_client.keys("audio:stream:*")
        current_time = time.time()

        for stream_key in stream_keys:
            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )
            if (
                not current_user.is_superuser
                and stream_name.removeprefix("audio:stream:") not in session_by_id
            ):
                continue
            try:
                stream_data = await stream_diagnostics(redis_client, stream_name)
                last_entry_id = stream_data["last_entry_id"]
                stream_age_seconds = (
                    current_time - int(last_entry_id.split("-", 1)[0]) / 1000
                    if last_entry_id
                    else 0
                )
                session_id = stream_name.removeprefix("audio:stream:")
                session_data = session_by_id.get(session_id, {})
                stream_data.update(
                    session_id=session_id,
                    client_id=session_data.get("client_id", ""),
                    session_age_seconds=session_data.get("age_seconds", 0),
                    session_idle_seconds=session_data.get("idle_seconds", 0),
                )
                has_active_consumer = any(
                    consumer["idle_ms"] < 300000
                    for group in stream_data["consumer_groups"]
                    for consumer in group["consumers"]
                )
                if (
                    has_active_consumer
                    or stream_data["total_pending"] > 0
                    or stream_age_seconds < 300
                ):
                    active_streams[stream_name] = stream_data
                else:
                    stream_data["idle_seconds"] = stream_age_seconds
                    completed_streams[stream_name] = stream_data

            except Exception as e:
                # Stream doesn't exist or error getting info
                logger.debug("Error processing stream: %s", type(e).__name__)
                continue

        # Get RQ queue stats - include all registries
        rq_stats = {
            "transcription_queue": {
                "queued": transcription_queue.count,
                "started": len(transcription_queue.started_job_registry),
                "finished": len(transcription_queue.finished_job_registry),
                "failed": len(transcription_queue.failed_job_registry),
                "canceled": len(transcription_queue.canceled_job_registry),
                "deferred": len(transcription_queue.deferred_job_registry),
            },
            "memory_queue": {
                "queued": memory_queue.count,
                "started": len(memory_queue.started_job_registry),
                "finished": len(memory_queue.finished_job_registry),
                "failed": len(memory_queue.failed_job_registry),
                "canceled": len(memory_queue.canceled_job_registry),
                "deferred": len(memory_queue.deferred_job_registry),
            },
            "default_queue": {
                "queued": default_queue.count,
                "started": len(default_queue.started_job_registry),
                "finished": len(default_queue.finished_job_registry),
                "failed": len(default_queue.failed_job_registry),
                "canceled": len(default_queue.canceled_job_registry),
                "deferred": len(default_queue.deferred_job_registry),
            },
        }

        if any(session["session_id"] in allowed_details for session in active_sessions):
            await visibility.assert_current()
        return {
            "active_sessions": active_sessions,
            "completed_sessions": completed_sessions_from_redis,
            "active_streams": active_streams,
            "completed_streams": completed_streams,
            "stream_health": active_streams,  # Backward compatibility - use active_streams
            "rq_queues": rq_stats,
            "timestamp": time.time(),
        }

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error("Error getting streaming status: %s", type(e).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to get streaming status"},
        )
