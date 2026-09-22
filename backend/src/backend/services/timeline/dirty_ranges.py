"""Dirty-range scheduling for rolling reconciliation.

Every evidence producer — recording closure, transcript revision, speaker revision,
silence trim, evidence spans, manual memories — calls :func:`mark_evidence_dirty` with
the absolute interval its change touched. Nearby ``pending``/``waiting`` rows coalesce
into one, so a burst of revisions over the same minutes costs one reconciliation.

Two clocks live on the row. ``not_before`` is the debounce: each new trigger pushes it
out, so reconciliation waits for evidence to stop moving. ``force_after`` is the
liveness bound, and merging keeps the *earliest* one, so continuous media can never
postpone a first look indefinitely.

A ``leased`` row is never coalesced into. The lease snapshots ``evidence_revision``
into ``leased_evidence_revision``; the run reconciles that snapshot and fences its
publish on it, while a trigger arriving mid-run opens a fresh ``pending`` row over the
same interval. That is what keeps continuous evidence from livelocking the fence:
forced progress guarantees a run starts, the snapshot guarantees it can finish, and the
fresh row guarantees nothing is lost.

See ``docs/backend/rolling-reconciliation.md`` → "Dirty-range scheduling".
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.models.conversation import Conversation
from backend.models.device_input import DeviceInputJob
from backend.models.timeline import (
    DirtyEvidenceRange,
    DirtyEvidenceRangeResolution,
    TimelineContextRequestState,
    TimelineReconciliationRequest,
    utcnow,
)
from backend.redis_factory import create_async_redis
from backend.redis_keys import timeline_evidence_revision, timeline_publication_lock
from backend.services.inference_artifacts import canonical_hash
from backend.services.redis_lock import distributed_lock
from backend.services.timeline.contracts import StageContextRequest

logger = logging.getLogger(__name__)

# Overridable at module level so tests can compress the schedule without patching
# every call site. See the module docstring for what each clock means.
DEBOUNCE_MINUTES = 5
FORCE_AFTER_MINUTES = 15
COALESCE_GAP_MINUTES = 5
LEASE_MINUTES = 30
MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 60
# One scan enqueues at most this many ranges, so a large backlog cannot turn a cron
# tick on the API event loop into a long Mongo/Redis burst.
SCAN_BATCH = 50
MAX_CONTEXT_ITEMS = 100
MAX_CONTEXT_REQUEST_MINUTES = 30

_COALESCABLE_STATES = ("pending",)


class DirtyRangeDismissalError(ValueError):
    """The requested range cannot make the failed-to-dismissed transition."""


class DirtyRangeLeaseLost(RuntimeError):
    """A worker no longer owns the exact lease attempt it loaded."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _next_evidence_revision(user_id: str) -> int:
    """Bump the per-user monotonic evidence counter."""
    client = create_async_redis(decode_responses=True)
    try:
        return int(await client.incr(timeline_evidence_revision(user_id)))
    finally:
        await client.aclose()


async def _current_evidence_revision(user_id: str, *, fallback: int) -> int:
    """Read the current monotonic counter without manufacturing a revision."""

    client = create_async_redis(decode_responses=True)
    try:
        value = await client.get(timeline_evidence_revision(user_id))
        return max(fallback, int(value or 0))
    finally:
        await client.aclose()


def explicit_dirty_range_id(
    user_id: str,
    reconciliation_request_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> str:
    """Stable identity for one request's exact authorized interval."""

    return canonical_hash(
        {
            "kind": "timeline-explicit-dirty-range-v1",
            "user_id": user_id,
            "reconciliation_request_id": reconciliation_request_id,
            "started_at": _as_utc(started_at).isoformat(),
            "ended_at": _as_utc(ended_at).isoformat(),
        }
    )


def context_successor_id(parent_dirty_range_id: str, context_request_id: str) -> str:
    return canonical_hash(
        {
            "kind": "timeline-context-successor-v1",
            "parent_dirty_range_id": parent_dirty_range_id,
            "context_request_id": context_request_id,
        }
    )


def bind_context_request(
    dirty_range: DirtyEvidenceRange, request: StageContextRequest
) -> StageContextRequest:
    """Install the canonical identity owned by this durable inference attempt."""

    context_request_id = canonical_hash(
        {
            "kind": "timeline-stage-context-request-v1",
            "dirty_range_id": dirty_range.dirty_range_id,
            "inference_attempt": dirty_range.attempts,
            "hypothesis_id": request.hypothesis_id,
            "stage": request.stage,
            "locator": request.locator.model_dump(mode="json"),
            "started_at": _as_utc(request.started_at).isoformat(),
            "ended_at": _as_utc(request.ended_at).isoformat(),
            "base_manifest_hash": request.base_manifest_hash,
            "leased_evidence_revision": request.leased_evidence_revision,
            "target_resolution": request.target_resolution,
            "max_items": request.max_items,
        }
    )
    return request.model_copy(update={"context_request_id": context_request_id})


def _merge_ordered(existing: Sequence[str], additions: Sequence[str]) -> list[str]:
    """Union two sequences preserving first-seen order."""
    merged = list(existing)
    seen = set(merged)
    for item in additions:
        if item and item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


async def authorize_explicit_range(
    *,
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
    reconciliation_request_id: str,
    reason: str,
    source_kind: str = "manual",
    authorized_at: Optional[datetime] = None,
) -> DirtyEvidenceRange:
    """Idempotently authorize one exact, immutable reconciliation interval.

    Authorization is represented by the row's deterministic identity and
    ``authorized_pending`` state. It never passes through the ordinary coalescing
    path, so ingestion cannot widen, merge, delete, or accidentally inherit it.
    """

    started_at = _as_utc(started_at)
    ended_at = _as_utc(ended_at)
    if ended_at <= started_at:
        raise ValueError("dirty evidence range must have positive duration")
    if not reconciliation_request_id:
        raise ValueError("explicit reconciliation request id is required")

    dirty_range_id = explicit_dirty_range_id(
        user_id, reconciliation_request_id, started_at, ended_at
    )
    existing = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )
    if existing is not None:
        if (
            existing.user_id != user_id
            or _as_utc(existing.started_at) != started_at
            or _as_utc(existing.ended_at) != ended_at
            or existing.reconciliation_request_id != reconciliation_request_id
        ):
            raise RuntimeError("deterministic dirty-range identity collision")
        return existing

    now = _as_utc(authorized_at) if authorized_at else utcnow()
    revision = await _current_evidence_revision(user_id, fallback=0)
    row = DirtyEvidenceRange(
        dirty_range_id=dirty_range_id,
        user_id=user_id,
        started_at=started_at,
        ended_at=ended_at,
        authorized_started_at=started_at,
        authorized_ended_at=ended_at,
        evidence_revision=revision,
        source_revisions={source_kind: [reconciliation_request_id]},
        trigger_reasons=[reason],
        not_before=now,
        force_after=now,
        state="authorized_pending",
        dispatch_authorized_at=now,
        reconciliation_request_id=reconciliation_request_id,
        created_at=now,
        updated_at=now,
    )
    try:
        await row.insert()
    except DuplicateKeyError:
        existing = await DirtyEvidenceRange.find_one(
            DirtyEvidenceRange.dirty_range_id == dirty_range_id
        )
        if existing is None:
            raise
        return existing
    return row


def _context_state(
    parent: DirtyEvidenceRange, context_request_id: str
) -> TimelineContextRequestState:
    request = next(
        (
            item
            for item in parent.context_requests
            if item.context_request_id == context_request_id
        ),
        None,
    )
    if request is None:
        raise ValueError(f"unknown context request {context_request_id!r}")
    return request


async def _context_parent(context_request_id: str) -> DirtyEvidenceRange:
    parent = await DirtyEvidenceRange.find_one(
        {
            "context_requests.context_request_id": context_request_id,
            "state": {"$in": ["awaiting_context", "superseded"]},
        }
    )
    if parent is None:
        raise ValueError(f"no dirty range owns context request {context_request_id!r}")
    return parent


async def _mark_context_evidence_dirty(
    *,
    user_id: str,
    source_revision: str,
    reason: str,
    source_kind: str,
    context_request_id: str,
    revision: int,
) -> DirtyEvidenceRange:
    """Fold context evidence only into its deterministic unauthorized successor."""

    parent = await _context_parent(context_request_id)
    if parent.user_id != user_id:
        raise ValueError("context request does not belong to evidence owner")
    request = _context_state(parent, context_request_id)
    authorized_start = _as_utc(parent.authorized_started_at or parent.started_at)
    authorized_end = _as_utc(parent.authorized_ended_at or parent.ended_at)
    started_at = min(authorized_start, _as_utc(request.started_at))
    ended_at = max(authorized_end, _as_utc(request.ended_at))
    dirty_range_id = context_successor_id(parent.dirty_range_id, context_request_id)
    now = utcnow()
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )
    if successor is None:
        successor = DirtyEvidenceRange(
            dirty_range_id=dirty_range_id,
            user_id=user_id,
            started_at=started_at,
            ended_at=ended_at,
            evidence_revision=revision,
            source_revisions=(
                {source_kind: [source_revision]} if source_revision else {}
            ),
            trigger_reasons=[reason],
            not_before=now,
            force_after=now,
            state="context_pending",
            parent_dirty_range_id=parent.dirty_range_id,
            context_request_id=context_request_id,
            context_requests=[request.model_copy(deep=True)],
            created_at=now,
            updated_at=now,
        )
        try:
            await successor.insert()
            return successor
        except DuplicateKeyError:
            successor = await DirtyEvidenceRange.find_one(
                DirtyEvidenceRange.dirty_range_id == dirty_range_id
            )
            if successor is None:
                raise

    if (
        successor.user_id != user_id
        or successor.parent_dirty_range_id != parent.dirty_range_id
        or _as_utc(successor.started_at) != started_at
        or _as_utc(successor.ended_at) != ended_at
    ):
        raise RuntimeError("context successor identity or bounds changed")
    successor.evidence_revision = max(successor.evidence_revision, revision)
    if source_revision:
        successor.source_revisions[source_kind] = _merge_ordered(
            successor.source_revisions.get(source_kind, []), [source_revision]
        )
    successor.trigger_reasons = _merge_ordered(successor.trigger_reasons, [reason])
    successor.updated_at = now
    await successor.save()
    return successor


async def _mark_evidence_dirty_locked(
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
    source_revision: str,
    reason: str,
    *,
    source_kind: str = "generic",
    not_before: Optional[datetime] = None,
    coalesce: bool = True,
    context_request_id: Optional[str] = None,
) -> DirtyEvidenceRange:
    """Record that ``[started_at, ended_at)`` needs reconciliation. Idempotent.

    Overlapping or nearby (within ``COALESCE_GAP_MINUTES``) ``pending``/``waiting``
    rows are folded into one row spanning min-start/max-end, carrying the union of
    source revisions and trigger reasons. The debounce restarts; the earliest existing
    ``force_after`` is preserved. A ``waiting`` row overlapped by a trigger wakes back
    to ``pending``.

    ``not_before`` overrides the debounce — that is how an explicit reconcile
    request asks for the range to be looked at now. ``coalesce=False`` preserves
    that request's exact authorization boundary; ordinary evidence ingestion should
    keep the default and merge adjacent dirty ranges.
    """

    started_at = _as_utc(started_at)
    ended_at = _as_utc(ended_at)
    if ended_at <= started_at:
        raise ValueError("dirty evidence range must have positive duration")

    now = utcnow()
    revision = await _next_evidence_revision(user_id)
    if context_request_id is not None:
        if not coalesce:
            raise ValueError("context evidence has its own deterministic coalescing")
        return await _mark_context_evidence_dirty(
            user_id=user_id,
            source_revision=source_revision,
            reason=reason,
            source_kind=source_kind,
            context_request_id=context_request_id,
            revision=revision,
        )
    gap = timedelta(minutes=COALESCE_GAP_MINUTES)

    neighbours = []
    if coalesce:
        neighbours = (
            await DirtyEvidenceRange.find(
                {
                    "user_id": user_id,
                    "state": {"$in": list(_COALESCABLE_STATES)},
                    "started_at": {"$lte": ended_at + gap},
                    "ended_at": {"$gte": started_at - gap},
                }
            )
            .sort("+created_at")
            .to_list()
        )

    debounce = (
        _as_utc(not_before) if not_before else now + timedelta(minutes=DEBOUNCE_MINUTES)
    )

    if not neighbours:
        row = DirtyEvidenceRange(
            user_id=user_id,
            started_at=started_at,
            ended_at=ended_at,
            evidence_revision=revision,
            source_revisions=(
                {source_kind: [source_revision]} if source_revision else {}
            ),
            trigger_reasons=[reason],
            not_before=debounce,
            force_after=now + timedelta(minutes=FORCE_AFTER_MINUTES),
        )
        await row.insert()
        logger.debug(
            "🩹 Dirty range opened %s for %s (%s)", row.dirty_range_id, user_id, reason
        )
        return row

    survivor, *duplicates = neighbours
    previous_revision = survivor.evidence_revision
    survivor.started_at = min(
        [started_at] + [_as_utc(row.started_at) for row in neighbours]
    )
    survivor.ended_at = max([ended_at] + [_as_utc(row.ended_at) for row in neighbours])

    source_revisions: dict[str, list[str]] = {}
    reasons: list[str] = []
    for row in neighbours:
        for kind, values in (row.source_revisions or {}).items():
            source_revisions[kind] = _merge_ordered(
                source_revisions.get(kind, []), values
            )
        reasons = _merge_ordered(reasons, row.trigger_reasons or [])
    if source_revision:
        source_revisions[source_kind] = _merge_ordered(
            source_revisions.get(source_kind, []), [source_revision]
        )
    survivor.source_revisions = source_revisions
    survivor.trigger_reasons = _merge_ordered(reasons, [reason])

    survivor.evidence_revision = revision
    survivor.not_before = debounce
    # Earliest existing deadline wins: coalescing must never extend how long a range
    # can go unlooked-at.
    survivor.force_after = min(_as_utc(row.force_after) for row in neighbours)
    survivor.state = "pending"
    survivor.lease_owner = None
    survivor.lease_expires_at = None
    survivor.updated_at = now
    if duplicates or not source_kind or any(char in source_kind for char in ".$\x00"):
        # Combining distinct rows requires the complete union. Arbitrary producer
        # keys must also remain literal BSON keys rather than update paths.
        await survivor.save()
    else:
        # The common single-row case must not rewrite its accumulated revision
        # history on every capture. Keep that history intact and append only the
        # new reference, under the same publication lock and revision fence.
        update = {
            "$set": {
                field: getattr(survivor, field)
                for field in (
                    "started_at",
                    "ended_at",
                    "evidence_revision",
                    "trigger_reasons",
                    "not_before",
                    "force_after",
                    "state",
                    "lease_owner",
                    "lease_expires_at",
                    "updated_at",
                )
            }
        }
        if source_revision:
            update["$addToSet"] = {f"source_revisions.{source_kind}": source_revision}
        changed = await DirtyEvidenceRange.get_pymongo_collection().update_one(
            {
                "_id": survivor.id,
                "state": {"$in": list(_COALESCABLE_STATES)},
                "evidence_revision": previous_revision,
            },
            update,
        )
        if changed.matched_count != 1:
            raise RuntimeError("Dirty evidence range changed during coalescing")

    for row in duplicates:
        await row.delete()

    logger.debug(
        "🩹 Dirty range %s coalesced %d row(s) for %s (%s)",
        survivor.dirty_range_id,
        len(duplicates),
        user_id,
        reason,
    )
    return survivor


async def mark_evidence_dirty(
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
    source_revision: str,
    reason: str,
    *,
    source_kind: str = "generic",
    not_before: Optional[datetime] = None,
    coalesce: bool = True,
    context_request_id: Optional[str] = None,
) -> DirtyEvidenceRange:
    """Record evidence while excluding the user's publication commit boundary."""

    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=300, blocking_timeout=300
    ):
        return await _mark_evidence_dirty_locked(
            user_id,
            started_at,
            ended_at,
            source_revision,
            reason,
            source_kind=source_kind,
            not_before=not_before,
            coalesce=coalesce,
            context_request_id=context_request_id,
        )


def _due_or_reclaimable(now: datetime) -> dict[str, Any]:
    return {
        "dispatch_authorized_at": {"$ne": None},
        "$or": [
            {
                "state": "authorized_pending",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "$or": [{"not_before": {"$lte": now}}, {"force_after": {"$lte": now}}],
            },
            {
                "state": "leased",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "lease_expires_at": {"$lt": now},
            },
        ],
    }


async def _fail_exhausted(now: datetime) -> int:
    """Terminate ranges that have burned every attempt."""
    collection = DirtyEvidenceRange.get_pymongo_collection()
    result = await collection.update_many(
        {
            "dispatch_authorized_at": {"$ne": None},
            "attempts": {"$gte": MAX_ATTEMPTS},
            "$or": [
                {"state": "authorized_pending"},
                {
                    "state": "leased",
                    "lease_expires_at": {"$lt": now},
                },
            ],
        },
        {
            "$set": {
                "state": "failed",
                "last_error": f"exhausted {MAX_ATTEMPTS} reconciliation attempts",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    return int(result.modified_count)


async def lease_due_range(
    owner: str, now: Optional[datetime] = None
) -> Optional[DirtyEvidenceRange]:
    """Compare-and-swap claim of one due range, oldest deadline first.

    Also reclaims a ``leased`` row whose lease expired — a worker that died holding a
    range must not strand it. A range that has already used ``MAX_ATTEMPTS`` leases is
    marked ``failed`` rather than leased again.
    """

    now = _as_utc(now) if now else utcnow()
    await _fail_exhausted(now)

    collection = DirtyEvidenceRange.get_pymongo_collection()
    candidates = (
        await collection.find(_due_or_reclaimable(now))
        .sort("force_after", 1)
        .limit(SCAN_BATCH)
        .to_list(length=SCAN_BATCH)
    )
    for candidate in candidates:
        leased = await lease_authorized_range_by_id(
            candidate["dirty_range_id"], owner, now
        )
        if leased is not None:
            return leased
    return None


async def lease_authorized_range_by_id(
    dirty_range_id: str, owner: str, now: Optional[datetime] = None
) -> Optional[DirtyEvidenceRange]:
    """CAS-claim one named authorized range, including safe lease re-adoption."""

    now = _as_utc(now) if now else utcnow()
    candidate = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )
    if candidate is None:
        return None
    revision = await _current_evidence_revision(
        candidate.user_id, fallback=candidate.evidence_revision
    )
    collection = DirtyEvidenceRange.get_pymongo_collection()
    document = await collection.find_one_and_update(
        {
            "dirty_range_id": dirty_range_id,
            "dispatch_authorized_at": {"$ne": None},
            "attempts": {"$lt": MAX_ATTEMPTS},
            "$or": [
                {"state": "authorized_pending"},
                {"state": "leased", "lease_owner": owner},
                {"state": "leased", "lease_expires_at": {"$lt": now}},
            ],
        },
        [
            {
                "$set": {
                    "state": "leased",
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(minutes=LEASE_MINUTES),
                    "attempts": {"$add": ["$attempts", 1]},
                    "evidence_revision": revision,
                    "leased_evidence_revision": revision,
                    "last_error": None,
                    "updated_at": now,
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id
    )


def _exact_lease_filter(dirty_range: DirtyEvidenceRange) -> dict[str, Any]:
    """Identity of the one worker attempt allowed to mutate a leased range."""

    if (
        dirty_range.id is None
        or dirty_range.state != "leased"
        or not dirty_range.lease_owner
        or dirty_range.leased_evidence_revision is None
        or dirty_range.attempts < 1
    ):
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} has no active lease identity"
        )
    return {
        "_id": dirty_range.id,
        "dirty_range_id": dirty_range.dirty_range_id,
        "user_id": dirty_range.user_id,
        "state": "leased",
        "lease_owner": dirty_range.lease_owner,
        "attempts": int(dirty_range.attempts),
        "evidence_revision": int(dirty_range.evidence_revision),
        "leased_evidence_revision": int(dirty_range.leased_evidence_revision),
    }


async def claim_range_publication_fence(
    dirty_range: DirtyEvidenceRange,
) -> DirtyEvidenceRange:
    """CAS-renew the exact lease attempt immediately before publication."""

    stamp = utcnow()
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {
            "$set": {
                "lease_expires_at": stamp + timedelta(minutes=LEASE_MINUTES),
                "updated_at": stamp,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before publication"
        )
    fenced = DirtyEvidenceRange.model_validate(document)
    dirty_range.lease_expires_at = fenced.lease_expires_at
    dirty_range.updated_at = fenced.updated_at
    return fenced


async def update_leased_range_fields(
    dirty_range: DirtyEvidenceRange,
    fields: dict[str, Any],
    *,
    action: str,
) -> DirtyEvidenceRange:
    """Persist worker-owned fields without allowing a stale attempt to overwrite."""

    stamp = utcnow()
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {"$set": {**fields, "updated_at": stamp}},
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before {action}"
        )
    updated = DirtyEvidenceRange.model_validate(document)
    dirty_range.updated_at = updated.updated_at
    for field in fields:
        setattr(dirty_range, field, getattr(updated, field))
    return updated


def _context_job_key(user_id: str, request: StageContextRequest) -> str:
    return canonical_hash(
        {
            "kind": "timeline-context-device-job-v1",
            "user_id": user_id,
            "context_request_id": request.context_request_id,
            "source_id": request.locator.capture_source_id,
        }
    )


async def _ensure_context_job(
    parent: DirtyEvidenceRange, request: StageContextRequest
) -> DeviceInputJob:
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.user_id == parent.user_id,
        DeviceInputJob.context_request_id == request.context_request_id,
        DeviceInputJob.source_id == request.locator.capture_source_id,
    )
    if existing is not None:
        return existing
    job = DeviceInputJob(
        user_id=parent.user_id,
        source_id=request.locator.capture_source_id,
        kind="screen_context",
        start_at=request.started_at,
        end_at=request.ended_at,
        purpose="timeline_context_acquisition",
        context_request_id=request.context_request_id,
        idempotency_key=_context_job_key(parent.user_id, request),
        payload={
            "parent_dirty_range_id": parent.dirty_range_id,
            "context_request_id": request.context_request_id,
            "hypothesis_id": request.hypothesis_id,
            "stage": request.stage,
            "locator": request.locator.model_dump(mode="json"),
            "target_resolution": request.target_resolution,
            "max_items": request.max_items,
        },
    )
    try:
        await job.insert()
    except DuplicateKeyError:
        existing = await DeviceInputJob.find_one(
            DeviceInputJob.idempotency_key == job.idempotency_key
        )
        if existing is None:
            raise
        return existing
    return job


async def park_for_context(
    dirty_range: DirtyEvidenceRange, request: StageContextRequest
) -> DirtyEvidenceRange:
    """Park a leased attempt and durably enqueue its bounded context acquisition."""

    request = bind_context_request(dirty_range, request)
    if dirty_range.state != "leased":
        raise ValueError("only a leased range can request context")
    if dirty_range.dispatch_authorized_at is None:
        raise ValueError("context acquisition requires explicit authorization")
    if request.locator.modality != "screen":
        raise ValueError("device-input context acquisition requires a screen locator")
    if request.max_items > MAX_CONTEXT_ITEMS:
        raise ValueError("context request item budget exceeds the configured maximum")
    request_start = _as_utc(request.started_at)
    request_end = _as_utc(request.ended_at)
    if request_end <= request_start:
        raise ValueError("context request interval must have positive duration")
    if request_end - request_start > timedelta(minutes=MAX_CONTEXT_REQUEST_MINUTES):
        raise ValueError("context request interval exceeds the configured maximum")
    authorized_start = _as_utc(
        dirty_range.authorized_started_at or dirty_range.started_at
    )
    authorized_end = _as_utc(dirty_range.authorized_ended_at or dirty_range.ended_at)
    if request_end < authorized_start or request_start > authorized_end:
        raise ValueError(
            "context request must overlap or adjoin the authorized interval"
        )
    if request.leased_evidence_revision != dirty_range.leased_evidence_revision:
        raise ValueError("context request evidence fence does not match the lease")
    if (
        dirty_range.base_manifest_hash is not None
        and dirty_range.base_manifest_hash != request.base_manifest_hash
    ):
        raise ValueError("context request manifest fence does not match the attempt")

    existing = next(
        (
            item
            for item in dirty_range.context_requests
            if item.context_request_id == request.context_request_id
        ),
        None,
    )
    request_payload = request.model_dump()
    if existing is None:
        existing = TimelineContextRequestState(
            **request_payload,
            status="queued",
            attempt_count=1,
        )
        dirty_range.context_requests.append(existing)
    else:
        durable_payload = existing.model_dump(
            exclude={
                "status",
                "device_input_job_ids",
                "result_evidence_ids",
                "attempt_count",
                "newest_evidence_revision",
                "last_error",
                "created_at",
                "updated_at",
            }
        )
        if durable_payload != request_payload:
            raise ValueError("context request id was reused with different bounds")

    stamp = utcnow()
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {
            "$set": {
                "base_manifest_hash": request.base_manifest_hash,
                "context_requests": [
                    item.model_dump(mode="python")
                    for item in dirty_range.context_requests
                ],
                "state": "awaiting_context",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": stamp,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before context parking"
        )
    dirty_range = DirtyEvidenceRange.model_validate(document)

    existing = _context_state(dirty_range, request.context_request_id)
    job = await _ensure_context_job(dirty_range, request)
    if str(job.id) not in existing.device_input_job_ids:
        existing.device_input_job_ids.append(str(job.id))
    existing.status = (
        "ready_to_refence" if job.status in {"complete", "failed"} else "awaiting"
    )
    existing.updated_at = utcnow()
    await dirty_range.save()
    if existing.status == "ready_to_refence":
        return await _handoff_ready_context(dirty_range, existing)
    return dirty_range


async def _handoff_ready_context(
    parent: DirtyEvidenceRange, request: TimelineContextRequestState
) -> DirtyEvidenceRange:
    """Idempotently supersede a parked parent and authorize its refenced successor."""

    revision = await _current_evidence_revision(
        parent.user_id,
        fallback=max(
            parent.evidence_revision,
            request.newest_evidence_revision or 0,
        ),
    )
    successor = await _mark_context_evidence_dirty(
        user_id=parent.user_id,
        source_revision="",
        reason=f"context_ready:{request.context_request_id}",
        source_kind="context",
        context_request_id=request.context_request_id,
        revision=revision,
    )
    terminal_request = request.model_copy(deep=True)
    terminal_request.status = "failed" if request.last_error else "complete"
    terminal_request.newest_evidence_revision = revision
    terminal_request.updated_at = utcnow()
    successor.context_requests = [terminal_request]
    successor.evidence_revision = revision

    # Authorize the fully refenced successor before retiring its parent. A crash after
    # this save leaves a harmless duplicate authority that recovery can roll forward;
    # the inverse order could strand the only authorized range as ``context_pending``.
    successor.authorized_started_at = successor.started_at
    successor.authorized_ended_at = successor.ended_at
    successor.dispatch_authorized_at = parent.dispatch_authorized_at
    successor.reconciliation_request_id = parent.reconciliation_request_id
    successor.state = "authorized_pending"
    successor.not_before = utcnow()
    successor.force_after = successor.not_before
    successor.updated_at = utcnow()
    await successor.save()

    parent.state = "superseded"
    parent.superseded_by_dirty_range_id = successor.dirty_range_id
    parent_request = request.model_copy(
        update={
            "status": "superseded",
            "newest_evidence_revision": revision,
            "updated_at": utcnow(),
        }
    )
    parent.context_requests = [
        (
            parent_request
            if item.context_request_id == request.context_request_id
            else item
        )
        for item in parent.context_requests
    ]
    parent.updated_at = utcnow()
    await parent.save()
    request_row = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == parent.reconciliation_request_id
    )
    if (
        request_row is not None
        and request_row.dirty_range_id != successor.dirty_range_id
    ):
        request_row.dirty_range_id = successor.dirty_range_id
        request_row.updated_at = utcnow()
        await request_row.save()
    return successor


async def notify_context_job_terminal(
    *,
    context_request_id: str,
    job_id: str,
    result_evidence_ids: Sequence[str] = (),
) -> DirtyEvidenceRange:
    """Consume a persisted device-job result and advance the context state machine."""

    parent = await _context_parent(context_request_id)
    request = _context_state(parent, context_request_id)
    if job_id not in request.device_input_job_ids:
        raise ValueError("device-input job does not belong to context request")
    jobs = await DeviceInputJob.find(
        DeviceInputJob.user_id == parent.user_id,
        DeviceInputJob.context_request_id == context_request_id,
    ).to_list()
    jobs_by_id = {
        str(job.id): job for job in jobs if str(job.id) in request.device_input_job_ids
    }
    if job_id not in jobs_by_id:
        raise ValueError("context device-input job is missing")
    request.result_evidence_ids = _merge_ordered(
        request.result_evidence_ids, result_evidence_ids
    )
    jobs = list(jobs_by_id.values())
    failed = [job for job in jobs if job.status == "failed"]
    if failed:
        request.last_error = "; ".join(
            str(job.error or "context acquisition failed") for job in failed
        )
    request.updated_at = utcnow()
    if len(jobs) != len(request.device_input_job_ids) or any(
        job.status not in {"complete", "failed"} for job in jobs
    ):
        request.status = "awaiting"
        await parent.save()
        return parent
    request.status = "ready_to_refence"
    request.newest_evidence_revision = await _current_evidence_revision(
        parent.user_id, fallback=parent.evidence_revision
    )
    await parent.save()
    return await _handoff_ready_context(parent, request)


async def recover_context_requests() -> int:
    """Repair missed enqueue/completion callbacks for parked context requests."""

    parents = await DirtyEvidenceRange.find(
        {"state": {"$in": ["awaiting_context", "superseded"]}}
    ).to_list()
    recovered = 0
    for parent in parents:
        if parent.state == "superseded" and parent.superseded_by_dirty_range_id:
            successor = await DirtyEvidenceRange.find_one(
                DirtyEvidenceRange.dirty_range_id == parent.superseded_by_dirty_range_id
            )
            if successor is not None and successor.state == "context_pending":
                successor.authorized_started_at = successor.started_at
                successor.authorized_ended_at = successor.ended_at
                successor.dispatch_authorized_at = parent.dispatch_authorized_at
                successor.reconciliation_request_id = parent.reconciliation_request_id
                successor.state = "authorized_pending"
                successor.not_before = utcnow()
                successor.force_after = successor.not_before
                successor.updated_at = utcnow()
                await successor.save()
                request_row = await TimelineReconciliationRequest.find_one(
                    TimelineReconciliationRequest.request_id
                    == parent.reconciliation_request_id
                )
                if (
                    request_row is not None
                    and request_row.dirty_range_id != successor.dirty_range_id
                ):
                    request_row.dirty_range_id = successor.dirty_range_id
                    request_row.updated_at = utcnow()
                    await request_row.save()
                recovered += 1
        for state in list(parent.context_requests):
            if state.status in {"complete", "failed", "superseded"}:
                continue
            request = StageContextRequest.model_validate(
                state.model_dump(
                    include={
                        "context_request_id",
                        "hypothesis_id",
                        "stage",
                        "locator",
                        "started_at",
                        "ended_at",
                        "base_manifest_hash",
                        "leased_evidence_revision",
                        "target_resolution",
                        "max_items",
                        "reason",
                    }
                )
            )
            job = await _ensure_context_job(parent, request)
            if str(job.id) not in state.device_input_job_ids:
                state.device_input_job_ids.append(str(job.id))
                state.status = "awaiting"
                state.updated_at = utcnow()
                await parent.save()
                recovered += 1
            if job.status in {"complete", "failed"}:
                await notify_context_job_terminal(
                    context_request_id=state.context_request_id,
                    job_id=str(job.id),
                    result_evidence_ids=job.payload.get("result_evidence_ids") or [],
                )
                recovered += 1
    return recovered


async def resolve_completed_pending_ranges(
    dirty_range_id: str, *, user_id: str
) -> None:
    """Subtract a published checkpoint from older producer ranges, replayably.

    Insert deterministic remainder rows before superseding their parent: interruption
    can leave redundant pending coverage, but can never lose outstanding evidence.
    The publication lock excludes producer coalescing while the split is installed.
    Newer revisions and authorized/in-flight ranges are never consumed here.
    """
    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=300, blocking_timeout=300
    ):
        completed = await DirtyEvidenceRange.find_one(
            {"dirty_range_id": dirty_range_id, "user_id": user_id, "state": "completed"}
        )
        if completed is None or completed.leased_evidence_revision is None:
            raise ValueError("pending coverage requires a completed fenced range")
        collection = DirtyEvidenceRange.get_pymongo_collection()
        candidates = await DirtyEvidenceRange.find(
            {
                "user_id": user_id,
                "state": "pending",
                "dispatch_authorized_at": None,
                "started_at": {"$lt": completed.ended_at},
                "ended_at": {"$gt": completed.started_at},
                "evidence_revision": {"$lte": completed.leased_evidence_revision},
                "updated_at": {"$lte": completed.created_at},
            }
        ).to_list()
        for parent in candidates:
            start, end = _as_utc(parent.started_at), _as_utc(parent.ended_at)
            covered_start, covered_end = _as_utc(completed.started_at), _as_utc(
                completed.ended_at
            )
            for left, right in (
                (start, min(end, covered_start)),
                (max(start, covered_end), end),
            ):
                if right <= left:
                    continue
                remainder = parent.model_copy(
                    update={
                        "id": None,
                        "dirty_range_id": canonical_hash(
                            {
                                "kind": "timeline-pending-remainder-v1",
                                "parent": parent.dirty_range_id,
                                "completed": completed.dirty_range_id,
                                "started_at": left.isoformat(),
                                "ended_at": right.isoformat(),
                            }
                        ),
                        "started_at": left,
                        "ended_at": right,
                        "parent_dirty_range_id": parent.dirty_range_id,
                    }
                )
                # Preserve evidence timestamps and revisions; this is bookkeeping,
                # not a new capture. Never overwrite a remainder changed on retry.
                await collection.update_one(
                    {"dirty_range_id": remainder.dirty_range_id},
                    {
                        "$setOnInsert": remainder.model_dump(
                            mode="python", exclude={"id"}
                        )
                    },
                    upsert=True,
                )
            await collection.update_one(
                {
                    "dirty_range_id": parent.dirty_range_id,
                    "state": "pending",
                    "evidence_revision": parent.evidence_revision,
                    "updated_at": parent.updated_at,
                },
                {
                    "$set": {
                        "state": "superseded",
                        "superseded_by_dirty_range_id": completed.dirty_range_id,
                        "updated_at": utcnow(),
                    }
                },
            )


async def complete_range(
    dirty_range: DirtyEvidenceRange, *, error: Optional[str] = None
) -> DirtyEvidenceRange:
    """Terminate a leased range as ``completed`` or, with ``error``, ``failed``."""

    stamp = utcnow()
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {
            "$set": {
                "state": "failed" if error else "completed",
                "last_error": error,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": stamp,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before completion"
        )
    if error is None:
        # A validated, fenced publication resolves older failed attempts only
        # when it covers their entire interval and evidence revision. Preserve
        # their diagnostic fields and link the successful replacement for audit.
        await DirtyEvidenceRange.get_pymongo_collection().update_many(
            {
                "user_id": dirty_range.user_id,
                "state": "failed",
                "started_at": {"$gte": dirty_range.started_at},
                "ended_at": {"$lte": dirty_range.ended_at},
                "evidence_revision": {"$lte": dirty_range.leased_evidence_revision},
                "updated_at": {"$lte": dirty_range.created_at},
            },
            {
                "$set": {
                    "state": "superseded",
                    "superseded_by_dirty_range_id": dirty_range.dirty_range_id,
                    "updated_at": stamp,
                }
            },
        )
    if error is None:
        await resolve_completed_pending_ranges(
            dirty_range.dirty_range_id, user_id=dirty_range.user_id
        )
    return DirtyEvidenceRange.model_validate(document)


async def dismiss_failed_range(
    dirty_range_id: str, *, user_id: str, reason: str
) -> DirtyEvidenceRange:
    """Audit and dismiss one owner-scoped terminal failure with a single CAS.

    Rejection lineage and diagnostics remain on the row. Only its scheduling/review
    state changes, so a person can explicitly accept the unresolved interval without
    erasing why inference could not resolve it.
    """

    reason = reason.strip()
    if not reason:
        raise DirtyRangeDismissalError("dismissal reason is required")
    resolution = DirtyEvidenceRangeResolution(
        actor_user_id=user_id,
        reason=reason,
    )
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        {
            "dirty_range_id": dirty_range_id,
            "user_id": user_id,
            "state": "failed",
        },
        {
            "$set": {"state": "dismissed", "updated_at": resolution.created_at},
            "$push": {"resolution_history": resolution.model_dump(mode="python")},
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is not None:
        return DirtyEvidenceRange.model_validate(document)
    existing = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range_id,
        DirtyEvidenceRange.user_id == user_id,
    )
    if existing is None:
        raise LookupError("failed reconciliation range not found")
    raise DirtyRangeDismissalError(
        f"only a terminal failed range can be dismissed (state={existing.state})"
    )


async def release_range_for_retry(
    dirty_range: DirtyEvidenceRange, error: str
) -> DirtyEvidenceRange:
    """Release a failed authorized attempt without publishing terminal failure early.

    RQ retries and the recovery cron are two parts of the same durable attempt chain.
    Until the range has exhausted that chain, its request must remain queued; otherwise
    a caller can advance to a later day while recovery silently restarts this one.
    """

    if dirty_range.attempts >= MAX_ATTEMPTS:
        return await complete_range(dirty_range, error=error)

    retry_at = utcnow() + timedelta(seconds=RETRY_DELAY_SECONDS)
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {
            "$set": {
                "state": "authorized_pending",
                "last_error": error,
                "not_before": retry_at,
                "force_after": retry_at,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before retry"
        )
    return DirtyEvidenceRange.model_validate(document)


async def park_waiting(
    dirty_range: DirtyEvidenceRange, reason: str
) -> DirtyEvidenceRange:
    """Park a range that needs future evidence before it can be reconciled.

    It stays schedulable: ``not_before`` becomes a fallback wake so a range whose
    awaited evidence never arrives is still looked at again, and an overlapping
    trigger wakes it immediately via :func:`mark_evidence_dirty`.
    """

    now = utcnow()
    trigger_reasons = _merge_ordered(
        dirty_range.trigger_reasons or [], [f"waiting:{reason}"]
    )
    document = await DirtyEvidenceRange.get_pymongo_collection().find_one_and_update(
        _exact_lease_filter(dirty_range),
        {
            "$set": {
                "state": "waiting",
                "not_before": now + timedelta(minutes=FORCE_AFTER_MINUTES),
                "trigger_reasons": trigger_reasons,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} lease changed before parking"
        )
    return DirtyEvidenceRange.model_validate(document)


async def reap_expired_leases(now: Optional[datetime] = None) -> int:
    """Return expired leases to ``authorized_pending``."""

    now = _as_utc(now) if now else utcnow()
    collection = DirtyEvidenceRange.get_pymongo_collection()
    result = await collection.update_many(
        {
            "state": "leased",
            "dispatch_authorized_at": {"$ne": None},
            "lease_expires_at": {"$lt": now},
        },
        {
            "$set": {
                "state": "authorized_pending",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    reclaimed = int(result.modified_count)
    if reclaimed:
        logger.info("🩹 Reclaimed %d expired dirty-range lease(s)", reclaimed)
    return reclaimed


async def due_ranges(
    now: Optional[datetime] = None, limit: int = SCAN_BATCH
) -> list[DirtyEvidenceRange]:
    """Pending ranges whose debounce elapsed or whose forced deadline passed."""

    now = _as_utc(now) if now else utcnow()
    return (
        await DirtyEvidenceRange.find(
            {
                "state": "authorized_pending",
                "dispatch_authorized_at": {"$ne": None},
                "attempts": {"$lt": MAX_ATTEMPTS},
                "$or": [
                    {"not_before": {"$lte": now}},
                    {"force_after": {"$lte": now}},
                ],
            }
        )
        .sort("+force_after")
        .limit(limit)
        .to_list()
    )


async def reconcile_dirty_ranges() -> dict[str, int]:
    """Cron entry point: recover explicitly authorized reconciliation requests.

    Deliberately cheap — it runs on the API event loop, so it does Mongo queries and
    RQ enqueues. Ordinary producer ranges never match ``due_ranges``; all agent/model
    work remains behind the durable explicit-request entrypoint.
    """

    # Imported here to avoid a circular import with the controllers package.
    from backend.controllers.queue_controller import (
        enqueue_explicit_timeline_reconciliation,
    )

    now = utcnow()
    context_recovered = await recover_context_requests()
    reclaimed = await reap_expired_leases(now)
    await _fail_exhausted(now)
    ranges = await due_ranges(now)

    enqueued = 0
    for dirty_range in ranges:
        if not dirty_range.reconciliation_request_id:
            logger.error(
                "Authorized dirty range %s has no reconciliation request",
                dirty_range.dirty_range_id,
            )
            continue
        job_id = await asyncio.to_thread(
            enqueue_explicit_timeline_reconciliation,
            dirty_range.reconciliation_request_id,
        )
        if job_id:
            enqueued += 1

    if ranges or reclaimed:
        logger.info(
            "🩹 Rolling reconciliation scan: %d due, %d enqueued, %d lease(s) reclaimed",
            len(ranges),
            enqueued,
            reclaimed,
        )
    return {
        "due": len(ranges),
        "enqueued": enqueued,
        "reclaimed": reclaimed,
        "context_recovered": context_recovered,
    }


# ── Producer-side trigger helpers ────────────────────────────────────────────
#
# A producer must never fail because scheduling failed: the recovery scan and the
# next trigger both re-open the range, while a raised exception would break audio
# processing. These wrappers log and swallow.


async def note_evidence_dirty(
    user_id: str,
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
    source_revision: str,
    reason: str,
    *,
    source_kind: str = "generic",
) -> Optional[DirtyEvidenceRange]:
    """Best-effort :func:`mark_evidence_dirty` for evidence producers."""

    if started_at is None or ended_at is None:
        logger.debug("🩹 Skipping dirty mark (%s): no absolute bounds", reason)
        return None
    try:
        return await mark_evidence_dirty(
            user_id,
            started_at,
            ended_at,
            source_revision,
            reason,
            source_kind=source_kind,
        )
    except Exception:
        logger.warning("🩹 Failed to mark evidence dirty (%s)", reason, exc_info=True)
        return None


async def note_conversation_dirty(
    conversation_id: str,
    reason: str,
    *,
    source_revision: Optional[str] = None,
    source_kind: str = "conversation",
) -> Optional[DirtyEvidenceRange]:
    """Mark a conversation's absolute audio interval dirty, best effort.

    The bounds come from the conversation's audio range claims — the authoritative
    wall-clock interval its evidence occupies — falling back to the conversation's own
    semantic bounds when it holds no claim yet.
    """

    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if conversation is None:
            return None
        if conversation.memory_space_id and conversation.published_to_main_at is None:
            return None
        ranges = conversation.audio_ranges or []
        if ranges:
            started_at = min(_as_utc(item.started_at) for item in ranges)
            ended_at = max(_as_utc(item.ended_at) for item in ranges)
        else:
            started_at = conversation.started_at
            ended_at = conversation.ended_at
        return await note_evidence_dirty(
            conversation.user_id,
            started_at,
            ended_at,
            source_revision or conversation_id,
            reason,
            source_kind=source_kind,
        )
    except Exception:
        logger.warning(
            "🩹 Failed to mark conversation %s dirty (%s)",
            conversation_id,
            reason,
            exc_info=True,
        )
        return None


# Strong references to fire-and-forget marks, so the event loop cannot drop a task
# that no one awaits.
_pending_marks: set[asyncio.Task] = set()


def schedule_conversation_dirty(conversation_id: str, reason: str) -> None:
    """Schedule :func:`note_conversation_dirty` from synchronous code.

    Every caller of ``start_post_conversation_jobs`` runs inside an event loop (a
    FastAPI handler or an ``@async_job`` worker), so the mark rides that loop. Without
    one there is nothing safe to do — Beanie's client is bound to the loop that
    created it — so this logs and leaves the range to the recovery scan.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "🩹 No running loop for dirty mark (%s) of %s", reason, conversation_id
        )
        return
    task = loop.create_task(note_conversation_dirty(conversation_id, reason))
    _pending_marks.add(task)
    task.add_done_callback(_pending_marks.discard)
