"""Explicit, Immich-gated authorization for Timeline reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.models.timeline import (
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
    utcnow,
)
from backend.services.immich_discovery import (
    check_immich_day_readiness,
    immich_configured,
    import_immich_day_candidates,
    resolve_immich_user_id,
)
from backend.services.notifications import NotificationCommand, enqueue_notification
from backend.services.redis_lock import distributed_lock
from backend.services.timeline.dirty_ranges import (
    authorize_explicit_range,
    cancel_range,
)
from backend.users import User

# The readiness check can page through several Immich search windows before importing
# bounded candidates. Keep the cross-process claim longer than that full HTTP budget.
EXPLICIT_REQUEST_LOCK_SECONDS = 15 * 60
CONCURRENT_REQUEST_REUSE_SECONDS = 10


def day_bounds(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    started_at = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    ended_at = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return started_at, ended_at


def evidence_cutoff(request: TimelineReconciliationRequest) -> datetime:
    """The fixed exclusive end of the source interval, never a moving 'today'."""
    _, day_end = day_bounds(request.local_date, request.timezone)
    checked = request.checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return min(day_end, checked)


async def request_explicit_reconciliation(
    *,
    user: User,
    local_date: date,
    timezone_name: str,
    skip_immich: bool = False,
) -> tuple[TimelineReconciliationRequest, bool]:
    """Check Immich now and either block or authorize exactly one day."""

    user_id = str(user.id)
    start, _ = day_bounds(local_date, timezone_name)
    if start >= utcnow():
        raise ValueError("Cannot reconcile a future evidence interval")
    if not skip_immich and immich_configured():
        immich_owner_id = await resolve_immich_user_id()
        if immich_owner_id != user_id:
            raise PermissionError(
                "Immich reconciliation is available only to its configured Chronicle owner"
            )
    claim = hashlib.sha256(
        f"{user_id}\0{local_date.isoformat()}\0{timezone_name}".encode()
    ).hexdigest()
    async with distributed_lock(
        f"timeline:explicit-request:{claim}",
        timeout=EXPLICIT_REQUEST_LOCK_SECONDS,
        blocking_timeout=EXPLICIT_REQUEST_LOCK_SECONDS,
    ):
        return await _request_explicit_reconciliation_locked(
            user=user,
            local_date=local_date,
            timezone_name=timezone_name,
            skip_immich=skip_immich,
        )


async def _request_explicit_reconciliation_locked(
    *,
    user: User,
    local_date: date,
    timezone_name: str,
    skip_immich: bool,
) -> tuple[TimelineReconciliationRequest, bool]:
    """Create or reuse a request while its cross-process day claim is held."""

    user_id = str(user.id)
    # Reuse durable work, plus a just-created blocked result from another request that
    # was waiting on this same Redis claim. A later explicit "Check again" remains a
    # fresh live Immich check rather than being pinned to an old blocked snapshot.
    reusable_states: list[dict] = [{"state": {"$in": ["queued", "running"]}}]
    if not skip_immich:
        reusable_states.append(
            {
                "state": "blocked",
                "created_at": {
                    "$gte": utcnow()
                    - timedelta(seconds=CONCURRENT_REQUEST_REUSE_SECONDS)
                },
            }
        )
    active = (
        await TimelineReconciliationRequest.find(
            TimelineReconciliationRequest.user_id == user_id,
            TimelineReconciliationRequest.local_date == local_date,
            TimelineReconciliationRequest.timezone == timezone_name,
            {"$or": reusable_states},
        )
        .sort("-created_at")
        .first_or_none()
    )
    if active is not None:
        return active, False

    if skip_immich:
        request = TimelineReconciliationRequest(
            user_id=user_id,
            local_date=local_date,
            timezone=timezone_name,
            state="queued",
            reason="user_bypassed_immich",
            target_asset_count=0,
            checked_at=utcnow(),
            immich_visual=ImmichVisualPreparationStatus(state="not_needed"),
        )
        await request.insert()
    else:
        readiness = await check_immich_day_readiness(local_date, timezone_name)
        request = TimelineReconciliationRequest(
            user_id=user_id,
            local_date=local_date,
            timezone=timezone_name,
            state="queued" if readiness.ready else "blocked",
            reason=readiness.reason,
            target_asset_count=readiness.target_asset_count,
            latest_asset_local_date=readiness.latest_asset_local_date,
            checked_at=readiness.checked_at,
            immich_visual=ImmichVisualPreparationStatus(
                state="pending" if readiness.target_asset_count else "not_needed"
            ),
        )
        await request.insert()

    if not skip_immich and not readiness.ready:
        if readiness.reason == "no_immich_evidence":
            try:
                intent, _created = await enqueue_notification(
                    user_id=user_id,
                    command=NotificationCommand(
                        notification_type="priority",
                        title="Timeline is waiting for Immich",
                        body=(
                            f"Open Immich and back up photos taken before "
                            f"{evidence_cutoff(request).astimezone(ZoneInfo(timezone_name)).isoformat()}, "
                            "then choose Check again in Chronicle."
                        ),
                        action="open_immich",
                        expires_at=utcnow() + timedelta(hours=2),
                        dedupe_key=f"immich-backup:{local_date.isoformat()}",
                    ),
                    source="timeline_immich_gate",
                    actor_id=user_id,
                )
                request.notification_id = intent.notification_id
                request.notification_status = intent.state
            except Exception as error:
                request.notification_status = "failed"
                request.last_error = f"Backup reminder could not be queued: {error}"
            request.updated_at = utcnow()
            await request.save()
        return request, True

    # Importing also registers an Immich capture source, so skip it without Immich.
    if not skip_immich and readiness.reason != "immich_unconfigured":
        await import_immich_day_candidates(user_id, readiness.target_assets)
    started_at, _ = day_bounds(local_date, timezone_name)
    ended_at = evidence_cutoff(request)
    dirty_range = await authorize_explicit_range(
        user_id=user_id,
        started_at=started_at,
        ended_at=ended_at,
        reconciliation_request_id=request.request_id,
        reason=(
            "explicit_user_bypass_day" if skip_immich else "explicit_immich_ready_day"
        ),
        source_kind="manual",
    )
    request.dirty_range_id = dirty_range.dirty_range_id

    job_id = await asyncio.to_thread(_queue_request, request.request_id)
    if not job_id:
        request.state = "failed"
        request.last_error = "failed to enqueue explicit reconciliation"
    else:
        request.job_id = job_id
    request.updated_at = utcnow()
    await request.save()
    return request, True


class ReconciliationNotCancellable(ValueError):
    """The request already finished, so there is nothing to stop."""


CANCELLABLE_REQUEST_STATES = ("queued", "running")


async def cancel_explicit_reconciliation(
    *, user: User, request_id: str
) -> TimelineReconciliationRequest:
    """Stop one queued or running day request so the day can be requested again.

    Order matters: the range is fenced first, so a worker that outlives the stop
    command loses its lease rather than publishing; then the RQ job is stopped; the
    request is marked last, because a still-active request is what blocks a retry.
    """

    user_id = str(user.id)
    request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request_id,
        TimelineReconciliationRequest.user_id == user_id,
    )
    if request is None:
        raise LookupError("Reconciliation request not found")
    if request.state not in CANCELLABLE_REQUEST_STATES:
        raise ReconciliationNotCancellable(
            f"Only a queued or running reconciliation can be cancelled "
            f"(state={request.state})"
        )
    if request.dirty_range_id:
        await cancel_range(
            request.dirty_range_id,
            user_id=user_id,
            reason=f"Cancelled by user (request {request_id})",
        )
    if request.job_id:
        await asyncio.to_thread(_stop_job, request.job_id)
    request.state = "cancelled"
    request.last_error = "Cancelled by user"
    request.updated_at = utcnow()
    await request.save()
    return request


def _stop_job(job_id: str) -> str:
    # Lazy for the same worker-bootstrap reason as _queue_request.
    from backend.controllers.queue_controller import (
        stop_explicit_timeline_reconciliation,
    )

    return stop_explicit_timeline_reconciliation(job_id)


def _queue_request(request_id: str) -> str | None:
    # Lazy to avoid coupling the Timeline service import graph to worker bootstrap.
    from backend.controllers.queue_controller import (
        enqueue_explicit_timeline_reconciliation,
    )

    return enqueue_explicit_timeline_reconciliation(request_id)


def reconciliation_request_payload(request: TimelineReconciliationRequest) -> dict:
    def utc_timestamp(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    return {
        "request_id": request.request_id,
        "date": request.local_date,
        "timezone": request.timezone,
        "state": request.state,
        "reason": request.reason,
        "target_asset_count": request.target_asset_count,
        "latest_eligible_asset_date": request.latest_asset_local_date,
        "checked_at": utc_timestamp(request.checked_at),
        "evidence_cutoff": evidence_cutoff(request),
        "notification_id": request.notification_id,
        "notification_status": request.notification_status,
        "job_id": request.job_id,
        "dirty_range_id": request.dirty_range_id,
        "run_id": request.run_id,
        "immich_visual": (
            request.immich_visual.model_dump(mode="json")
            if request.immich_visual
            else None
        ),
        "immich_evidence": (
            request.immich_evidence.model_dump(mode="json")
            if request.immich_evidence
            else None
        ),
        "last_error": request.last_error,
        "created_at": utc_timestamp(request.created_at),
        "updated_at": utc_timestamp(request.updated_at),
    }
