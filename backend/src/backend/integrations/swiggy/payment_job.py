"""Durable background work for the Instamart interaction mode."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from rq import Queue

import backend.services.dialogue.payment as payment
from backend.integrations.swiggy import FileTokenStore, Server, SwiggyClient
from backend.models.job import async_job
from backend.redis_factory import create_sync_redis
from backend.redis_keys import ClientId, SessionId
from backend.services.interaction_modes import InteractionStore
from backend.services.interaction_modes.store import interaction_lock_key
from backend.services.wakeword.executor import publish_sse, speak_on_device

logger = logging.getLogger(__name__)
PAYMENT_JOB_TIMEOUT_SECONDS = 7 * 60


def enqueue_instamart_payment_monitor(
    *,
    interaction_id: str,
    user_id: str,
    client_id: str,
    audio_session_id: str,
    token_directory: str,
    order_id: str,
    paas_id: str,
    polling_interval_ms: int,
    max_polling_ms: int,
    dialogue_thread_id: str | None = None,
) -> str:
    """Enqueue one bounded poller and return its deterministic RQ job id."""
    safe_order = re.sub(r"[^A-Za-z0-9_-]", "", order_id)[:80]
    job_id = f"swiggy-payment-{safe_order}"
    connection = create_sync_redis()
    try:
        queue = Queue("default", connection=connection)
        queue.enqueue(
            monitor_instamart_payment_job,
            interaction_id=interaction_id,
            user_id=user_id,
            client_id=client_id,
            audio_session_id=audio_session_id,
            token_directory=token_directory,
            order_id=order_id,
            paas_id=paas_id,
            polling_interval_ms=polling_interval_ms,
            max_polling_ms=max_polling_ms,
            dialogue_thread_id=dialogue_thread_id,
            job_id=job_id,
            job_timeout=PAYMENT_JOB_TIMEOUT_SECONDS,
            result_ttl=24 * 60 * 60,
            description=f"Monitor Instamart payment {safe_order}",
        )
    finally:
        connection.close()
    return job_id


@async_job(redis=True, beanie=False, timeout=PAYMENT_JOB_TIMEOUT_SECONDS)
async def monitor_instamart_payment_job(
    *,
    interaction_id: str,
    user_id: str,
    client_id: str,
    audio_session_id: str,
    token_directory: str,
    order_id: str,
    paas_id: str,
    polling_interval_ms: int,
    max_polling_ms: int,
    dialogue_thread_id: str | None = None,
    redis_client=None,
) -> dict[str, Any]:
    """Poll gently to a bounded deadline, then finalize exactly once if needed."""
    if dialogue_thread_id:

        interaction_store = await payment.DialoguePaymentStore.open(
            dialogue_thread_id, user_id
        )
    else:
        interaction_store = InteractionStore(redis_client)
    # Checkout enqueues this job just before the interaction processor commits the
    # returned awaiting-payment state. A fast RQ worker can start first, so wait for
    # that state barrier before monitoring or trying to end the session.
    state_committed = False
    for _ in range(40):
        session = await interaction_store.get(interaction_id)
        if (
            session is not None
            and str(session.plugin_state.get("order_id") or "") == order_id
        ):
            state_committed = True
            break
        await asyncio.sleep(0.25)

    status_data: dict[str, Any] = {}
    status = "unknown"
    terminal = False
    try:
        if session is not None and session.status == "ended":
            return {
                "status": session.plugin_state.get("payment_status", "unknown"),
                "reason": "already_resolved",
                "order_id": order_id,
            }
        if not state_committed:
            raise RuntimeError(
                "payment monitor started before its order state was committed"
            )
        store = FileTokenStore(Path(token_directory))
        if not store.configured:
            raise RuntimeError(
                "Swiggy token files are not configured for payment monitoring"
            )
        client = SwiggyClient(store)
        interval_seconds = max(5.0, min(float(polling_interval_ms) / 1000.0, 30.0))
        window_seconds = max(10.0, min(float(max_polling_ms) / 1000.0, 5 * 60.0))
        deadline = time.monotonic() + window_seconds

        while time.monotonic() < deadline:
            result = await client.call(
                Server.INSTAMART,
                "check_payment_status",
                paasId=paas_id,
                orderId=order_id,
            )
            status_data = result.data if isinstance(result.data, dict) else {}
            if status_data.get("terminal"):
                break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(interval_seconds, remaining))

        status = str(status_data.get("status") or "pending").strip().lower()
        terminal = bool(status_data.get("terminal"))
        if status in {"success", "paid"}:
            if not status_data.get("confirmed"):
                await client.call(
                    Server.INSTAMART,
                    "confirm_order",
                    orderId=order_id,
                    paasId=paas_id,
                )
            reason = "payment_success"
            reply = "Payment succeeded. Your Instamart order is confirmed."
        elif terminal and status == "refund-initiated":
            reason = "payment_refund_initiated"
            reply = "The payment was reversed and a refund has been initiated. The order was not placed."
        elif terminal and status == "cart_changed":
            reason = "payment_cart_changed"
            reply = "The cart changed during payment, so the order was not placed. Please review it again."
        elif terminal and status in {"failed", "cancelled"}:
            reason = f"payment_{status}"
            reply = f"The Instamart payment was {status}. The order was not placed."
        else:
            # The official headless contract requires one confirm at our polling cap;
            # it finalizes a still-pending order safely and reconciles any late success.
            await client.call(
                Server.INSTAMART,
                "confirm_order",
                orderId=order_id,
                paasId=paas_id,
            )
            reason = "payment_timeout"
            reply = "The payment window timed out. I finalized the pending attempt; please check Swiggy before retrying."
    except Exception as exc:  # noqa: BLE001 - the user needs a direct failure signal
        logger.error(
            "Instamart payment monitoring failed for %s: %s",
            order_id,
            exc,
            exc_info=True,
        )
        reason = "payment_monitor_error"
        reply = "I could not determine the Instamart payment result. Check the Swiggy app before retrying or placing another order."

    # Serialize the background terminal transition with ordinary spoken turns.
    # Without this lock, a concurrently processed "resend link" turn could save an
    # older active snapshot after this job ended the mode and resurrect it.
    lock_key = interaction_lock_key(interaction_id)
    lock_token = f"payment:{order_id}"
    acquired = False
    mode_ended = False
    for _ in range(120):
        acquired = bool(await redis_client.set(lock_key, lock_token, ex=60, nx=True))
        if acquired:
            break
        await asyncio.sleep(0.25)
    if acquired:
        try:
            session = await interaction_store.get(interaction_id)
            if session is not None and session.status == "ended":
                mode_ended = True
            elif session is not None and session.status == "active":
                state_order_id = str(session.plugin_state.get("order_id") or "")
                if state_order_id == order_id:
                    session.phase = "finished"
                    session.plugin_state = {
                        **session.plugin_state,
                        "payment_status": status,
                        "payment_terminal": terminal,
                    }
                    await interaction_store.end(session, reason=reason)
                    mode_ended = True
        finally:
            current_lock = await redis_client.get(lock_key)
            if isinstance(current_lock, bytes):
                current_lock = current_lock.decode()
            if current_lock == lock_token:
                await redis_client.delete(lock_key)
    else:
        logger.error(
            "Could not acquire interaction lock to store payment result for %s",
            order_id,
        )

    payload = {
        "interaction_id": interaction_id,
        "mode_id": "swiggy_order",
        "status": "ended" if mode_ended else "active",
        "end_reason": reason,
        "reply": reply,
        "order_id": order_id,
    }
    event_type = "interaction.ended" if mode_ended else "interaction.payment"
    await publish_sse(redis_client, user_id, event_type, payload)
    if dialogue_thread_id:
        await interaction_store.publish(interaction_id, order_id, reply)
    else:
        await speak_on_device(
            redis_client,
            ClientId.from_value(client_id),
            SessionId.from_value(audio_session_id),
            reply,
            generation=session.response_generation if session is not None else None,
            turn_id=session.response_turn_id if session is not None else None,
            turn_revision=(
                session.response_turn_revision if session is not None else 0
            ),
        )
    return {"status": status, "reason": reason, "order_id": order_id}
