"""Registered Timeline jobs for staged rolling reconciliation and review."""

import logging
import uuid
from datetime import date

from backend.models.job import async_job
from backend.models.timeline import (
    DirtyEvidenceRange,
    ImmichEvidenceSummary,
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
    utcnow,
)
from backend.services.job_progress import report_job_progress
from backend.services.timeline.consolidation import generate_day_consolidation
from backend.services.timeline.dirty_ranges import (
    DirtyRangeLeaseLost,
    authorize_explicit_range,
    complete_range,
    lease_authorized_range_by_id,
    release_range_for_retry,
    resolve_completed_pending_ranges,
)
from backend.services.timeline.evidence import (
    assemble_day_evidence,
    summarize_immich_evidence,
)
from backend.services.timeline.executor import settings_dict
from backend.services.timeline.explicit_reconciliation import (
    day_bounds,
    evidence_cutoff,
)
from backend.services.timeline.immich_visual_evidence import (
    prepare_immich_visual_evidence,
)
from backend.services.timeline.publication import recover_timeline_publications
from backend.services.timeline.reconciliation import finish_range, reconcile_range

logger = logging.getLogger(__name__)


def _lease_lost_result(
    dirty_range_id: str, error: DirtyRangeLeaseLost, *, request_id: str | None = None
) -> dict:
    logger.warning(
        "🩹 Stale Timeline worker lost lease for %s: %s", dirty_range_id, error
    )
    result = {
        "dirty_range_id": dirty_range_id,
        "state": "lease_lost",
        "error": str(error),
    }
    if request_id is not None:
        result["request_id"] = request_id
    return result


@async_job(redis=True, beanie=True)
async def recover_timeline_publications_job(*, redis_client=None) -> dict:
    """Roll every durable, incomplete Timeline publication forward.

    The journal contains the full operation payload, so this registered production
    entry point does not depend on the process or closure that began publication.
    """

    report = await recover_timeline_publications()
    return {
        "committed_publication_ids": report.committed_publication_ids,
        "conflicted_publication_ids": report.conflicted_publication_ids,
        "failed_publication_ids": report.failed_publication_ids,
        "orphaned_dirty_days": [list(item) for item in report.orphaned_dirty_days],
    }


def _last_immich_evidence(accounting: list[dict]) -> ImmichEvidenceSummary:
    for iteration in reversed(accounting):
        if iteration.get("immich_evidence") is not None:
            return ImmichEvidenceSummary.model_validate(iteration["immich_evidence"])
    return ImmichEvidenceSummary()


@async_job(redis=True, beanie=True)
async def explicit_reconciliation_job(request_id: str, *, redis_client=None) -> dict:
    """Real registered entrypoint for the sole explicit Timeline write path."""

    request = await TimelineReconciliationRequest.find_one({"request_id": request_id})
    if request is None:
        return {"request_id": request_id, "state": "missing"}
    if request.state in {"completed", "failed", "blocked", "cancelled"}:
        return {"request_id": request_id, "state": request.state}
    return await _run_explicit_reconciliation(request)


async def _run_explicit_reconciliation(
    request: TimelineReconciliationRequest,
) -> dict:
    """Run one already-authorized request through the staged reconciler."""

    request_id = request.request_id
    request.state = "running"
    request.last_error = None
    request.updated_at = utcnow()
    await request.save()
    leased = None
    await report_job_progress(
        "photos", "Checking available photos", user_id=request.user_id, reset=True
    )
    try:
        if request.target_asset_count > 0:
            request.immich_visual = ImmichVisualPreparationStatus(state="running")
            request.updated_at = utcnow()
            await request.save()
            visual = await prepare_immich_visual_evidence(
                request.user_id,
                request.local_date,
                request.timezone,
                cutoff=evidence_cutoff(request),
            )
            request.immich_visual = ImmichVisualPreparationStatus.model_validate(
                visual.payload()
            )
            request.updated_at = utcnow()
            await request.save()
            if visual.state == "failed":
                raise RuntimeError(
                    "all selected Immich photos failed visual preparation"
                )
        elif request.immich_visual is None:
            request.immich_visual = ImmichVisualPreparationStatus(state="not_needed")

        photo_count = (
            request.immich_visual.analyzed_count if request.immich_visual else 0
        )
        await report_job_progress(
            "photos",
            "Photo inspection complete",
            completed=photo_count,
            total=request.target_asset_count,
            unit="photos",
            state="completed",
        )
        await report_job_progress("evidence", "Loading captured evidence")
        if not request.dirty_range_id:
            raise RuntimeError("explicit reconciliation request has no dirty range")
        dirty_range = await DirtyEvidenceRange.find_one(
            DirtyEvidenceRange.dirty_range_id == request.dirty_range_id
        )
        if dirty_range is None:
            raise RuntimeError("authorized dirty range is missing")
        if dirty_range.state == "completed":
            # Recover a crash after publication/completion but before bookkeeping
            # and the request's terminal status were saved. Do not rerun inference.
            await resolve_completed_pending_ranges(
                dirty_range.dirty_range_id, user_id=dirty_range.user_id
            )
            result = {
                "request_id": request_id,
                "dirty_range_id": dirty_range.dirty_range_id,
                "recovered": True,
            }
        else:
            owner = f"explicit_reconciliation:{request_id}"
            leased = await lease_authorized_range_by_id(
                dirty_range.dirty_range_id, owner
            )
            if leased is None:
                raise RuntimeError("authorized dirty range could not be leased")
            accounting: list[dict] = []
            outcome = await reconcile_range(leased, accounting=accounting)
            request.immich_evidence = _last_immich_evidence(accounting)
            request.updated_at = utcnow()
            await request.save()
            range_state = await finish_range(leased, outcome)
            if range_state != "completed":
                raise RuntimeError(f"reconciliation ended in {range_state}")
            result = {
                "request_id": request_id,
                "dirty_range_id": leased.dirty_range_id,
                "published": list(outcome.episode_ids) if outcome else [],
                "iterations": accounting,
            }
    except DirtyRangeLeaseLost as error:
        return _lease_lost_result(
            (
                leased.dirty_range_id
                if leased is not None
                else request.dirty_range_id or ""
            ),
            error,
            request_id=request_id,
        )
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        if leased is not None:
            try:
                released = await release_range_for_retry(leased, error_text)
            except DirtyRangeLeaseLost as lease_error:
                return _lease_lost_result(
                    leased.dirty_range_id, lease_error, request_id=request_id
                )
            request.state = "failed" if released.state == "failed" else "queued"
        else:
            request.state = "failed"
        await report_job_progress(
            None,
            (
                "Attempt failed; waiting for retry"
                if request.state == "queued"
                else "Reconciliation failed"
            ),
            state=request.state,
        )
        request.last_error = error_text
        request.updated_at = utcnow()
        await request.save()
        raise
    await report_job_progress(
        "publication",
        "Timeline published",
        completed=1,
        total=1,
        unit="timeline",
        state="completed",
    )
    request.state = "completed"
    request.updated_at = utcnow()
    await request.save()
    return {**result, "state": "completed"}


@async_job(redis=True, beanie=True)
async def generate_timeline_consolidation_job(
    user_id: str,
    local_date: str,
    timezone_name: str,
    snapshot_id: str,
    *,
    redis_client=None,
) -> dict:
    """Pre-generate a snapshot-fenced grouping proposal outside publication."""

    return await generate_day_consolidation(
        user_id, date.fromisoformat(local_date), timezone_name, snapshot_id
    )


@async_job(redis=True, beanie=True)
async def rebuild_timeline_day_job(
    user_id: str,
    local_date: str,
    timezone_name: str,
    rebuild_run_id: str,
    *,
    redis_client=None,
) -> dict:
    """Reconcile one local day through the sole staged rolling product path."""

    day = date.fromisoformat(local_date)
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"chronicle:timeline-rebuild:{rebuild_run_id}:{user_id}:{local_date}:{timezone_name}",
        )
    )
    request = await TimelineReconciliationRequest.find_one({"request_id": request_id})
    if request is not None and request.state == "completed":
        return {
            "local_date": local_date,
            "request_id": request_id,
            "state": "completed",
        }
    if request is None:
        request = TimelineReconciliationRequest(
            request_id=request_id,
            user_id=user_id,
            local_date=day,
            timezone=timezone_name,
            state="queued",
            reason="user_bypassed_immich",
        )
        await request.insert()
    started_at, ended_at = day_bounds(day, timezone_name)
    dirty_range = await authorize_explicit_range(
        user_id=user_id,
        started_at=started_at,
        ended_at=ended_at,
        reconciliation_request_id=request_id,
        reason="timeline_rebuild",
        source_kind="rebuild",
    )
    request.dirty_range_id = dirty_range.dirty_range_id
    request.state = "queued"
    request.updated_at = utcnow()
    await request.save()
    result = await _run_explicit_reconciliation(request)
    logger.info("🗓️ Rebuild reconciled %s for user %s", local_date, user_id)
    return {"local_date": local_date, "memory": "pending_review", **result}


@async_job(redis=True, beanie=True)
async def reconcile_range_job(dirty_range_id: str, redis_client=None) -> dict:
    """Reconcile one dirty evidence range: lease it, run it, terminate it.

    The lease is claimed *by id* rather than through ``lease_due_range``, which takes
    the globally oldest due range and would have this job silently reconcile someone
    else's interval. A run that parks or fences the range leaves it schedulable; only a
    completed publish or no-op closes it.
    """

    dirty_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )
    if dirty_range is None:
        return {"dirty_range_id": dirty_range_id, "state": "missing"}
    if dirty_range.state == "completed":
        await resolve_completed_pending_ranges(
            dirty_range.dirty_range_id, user_id=dirty_range.user_id
        )
    if dirty_range.state in ("completed", "failed", "dismissed"):
        # Another worker already finished it; the enqueue lock only collapses bursts.
        return {"dirty_range_id": dirty_range_id, "state": dirty_range.state}

    owner = f"reconcile_range_job:{dirty_range_id}"
    leased = await lease_authorized_range_by_id(dirty_range_id, owner)
    if leased is None:
        return {"dirty_range_id": dirty_range_id, "state": "not_leased"}

    logger.info(
        "🩹 Reconciling dirty range %s (%s → %s, revision %s)",
        leased.dirty_range_id,
        leased.started_at.isoformat(),
        leased.ended_at.isoformat(),
        leased.leased_evidence_revision,
    )
    accounting: list[dict] = []
    try:
        outcome = await reconcile_range(leased, accounting=accounting)
    except DirtyRangeLeaseLost as error:
        return _lease_lost_result(dirty_range_id, error)
    except Exception as error:
        logger.exception("🩹 Reconciliation of %s failed", dirty_range_id)
        try:
            await complete_range(leased, error=f"{type(error).__name__}: {error}")
        except DirtyRangeLeaseLost as lease_error:
            return _lease_lost_result(dirty_range_id, lease_error)
        return {
            "dirty_range_id": dirty_range_id,
            "state": "failed",
            "error": str(error),
            "iterations": accounting,
        }

    try:
        state = await finish_range(leased, outcome)
    except DirtyRangeLeaseLost as error:
        return _lease_lost_result(dirty_range_id, error)
    return {
        "dirty_range_id": dirty_range_id,
        "state": state,
        "evidence_revision": leased.leased_evidence_revision,
        "iterations": accounting,
        "published": list(outcome.episode_ids) if outcome else [],
        "fenced": bool(outcome.fenced) if outcome else None,
        "material_change": bool(outcome.material_change) if outcome else False,
        "affected_local_dates": (
            [value.isoformat() for value in outcome.affected_local_dates]
            if outcome
            else []
        ),
    }
