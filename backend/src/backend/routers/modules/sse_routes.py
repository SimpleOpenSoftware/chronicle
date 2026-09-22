"""
SSE (Server-Sent Events) streaming endpoint.

Provides a single event stream per authenticated user. The browser connects once
and receives all real-time events (conversation updates, job status changes, etc.)
via Redis pub/sub.

Authentication uses JWT token as a query parameter since EventSource API
does not support custom headers.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from backend.auth import get_user_from_token_param
from backend.redis_factory import create_async_redis
from backend.server import shutdown_requested
from backend.services import privacy
from backend.services.queue_privacy import QueuePrivacyFilter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

HEARTBEAT_INTERVAL = 30  # seconds


async def _sse_generator(user_id: str):
    """Async generator that subscribes to a user's SSE channel and yields events."""
    r = create_async_redis(decode_responses=True)
    pubsub = r.pubsub()
    channel = f"sse:{user_id}"

    try:
        await pubsub.subscribe(channel)
        logger.info("SSE stream opened for user %s", user_id[:12])

        # Send initial connected event
        yield f"event: connected\ndata: {json.dumps({'user_id': user_id})}\n\n"

        while not shutdown_requested():
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=HEARTBEAT_INTERVAL,
                )

                if message and message["type"] == "message":
                    payload = json.loads(message["data"])
                    event_type = payload.get("event", "message")
                    if not isinstance(event_type, str) or not re.fullmatch(
                        r"[A-Za-z0-9_.:-]{1,100}", event_type
                    ):
                        event_type = "message"
                    try:
                        row = (
                            await QueuePrivacyFilter().project(
                                [
                                    {
                                        "user_id": user_id,
                                        "event": event_type,
                                        "data": payload.get("data", {}),
                                    }
                                ],
                                default_owner=user_id,
                                event=True,
                            )
                        )[0]
                        data = row["data"]
                        if row.get("privacy_held"):
                            data = {"privacy_held": True}
                    except privacy.PrivacyHeld:
                        # A changed policy cannot expose this payload or terminate
                        # the notification stream. The next message gets a fresh snapshot.
                        data = {"privacy_held": True}
                    event_data = json.dumps(data)
                    yield f"event: {event_type}\ndata: {event_data}\n\n"

            except asyncio.TimeoutError:
                # Send heartbeat comment (SSE spec: lines starting with ':' are comments)
                yield ": heartbeat\n\n"

    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for user %s", user_id[:12])
    except Exception as exc:
        logger.warning("SSE stream error: %s", type(exc).__name__)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await r.aclose()
        logger.info("SSE stream closed for user %s", user_id[:12])


@router.get("/stream")
async def event_stream(
    token: Optional[str] = Query(None, description="JWT token for authentication"),
):
    """
    SSE endpoint — single event stream per authenticated user.

    Connect via EventSource:
        const source = new EventSource('/api/events/stream?token=JWT_TOKEN')

    Events:
        - connected: Initial connection confirmation
        - conversation.created: New conversation started
        - conversation.updated: Conversation title/summary/speakers changed
        - conversation.completed: All processing finished
        - memory.processed: Memories extracted
        - plugin.event: Plugin event logged (for queue page)
        - job.progress: Live progress update (word count, batch %, throttled)
        - jobs.queued: Batch of jobs enqueued (streaming or post-conversation)
        - session.started: Audio streaming session opened
        - session.ended: Audio streaming session closed
        - conversation.closed: Conversation ended, post-processing starting
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token required. Pass as ?token=JWT_TOKEN",
        )

    user = await get_user_from_token_param(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    return StreamingResponse(
        _sse_generator(str(user.id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
