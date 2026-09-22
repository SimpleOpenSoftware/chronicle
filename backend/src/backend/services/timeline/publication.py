"""Crash-safe dirty-first publication of Timeline revisions and day snapshots.

The journal stores the complete intent before any day or graph row changes.  Operation
appliers are supplied by the owning episode/group writer and must be idempotent: after a
crash they prove that an already-landed operation is complete instead of replaying a
blind mutation.  This module owns ordering, day fences, snapshot installation, and
roll-forward recovery; it does not become a second reconciliation orchestrator.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import backend.services.privacy as privacy
from backend.models.timeline import (
    DirtyEvidenceRange,
    TimelineDay,
    TimelineEpisode,
    TimelineInterpretationRejectionState,
    TimelinePublicationDayPlan,
    TimelinePublicationEvidenceFence,
    TimelinePublicationJournal,
    TimelinePublicationOperation,
    TimelineReviewDecision,
    TimelineSemanticGroupRevision,
    utcnow,
)
from backend.redis_keys import timeline_publication_lock
from backend.services.inference_artifacts import canonical_hash
from backend.services.redis_lock import distributed_lock

from . import dirty_ranges
from .snapshots import build_day_snapshot, verify_day_snapshot

logger = logging.getLogger(__name__)

PUBLICATION_SCHEMA_VERSION = "timeline-publication-v1"
OperationOutcome = Literal["applied", "already_applied", "conflict"]
OperationApplier = Callable[[TimelinePublicationOperation], Awaitable[OperationOutcome]]
ConflictNotifier = Callable[[TimelinePublicationJournal, str], Awaitable[None]]
PublicationGuard = Callable[[], Awaitable[None]]
PublicationAction = Callable[[], Awaitable[object]]


class PublicationConflict(RuntimeError):
    """A journal cannot be rolled forward without overwriting a newer revision."""


class IncompletePublication(RuntimeError):
    """A recoverable operation failed after its intent was made durable."""


def _stable_hash_value(value):
    """Normalize BSON date round-trips before hashing durable journal intent."""

    if isinstance(value, datetime):
        aware = (
            value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        )
        return aware.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _stable_hash_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable_hash_value(item) for item in value]
    return value


def _bson_stable_value(value):
    """Normalize values that Mongo stores at millisecond datetime precision."""

    if isinstance(value, datetime):
        aware = (
            value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        )
        aware = aware.astimezone(timezone.utc)
        return aware.replace(microsecond=(aware.microsecond // 1000) * 1000).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _bson_stable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bson_stable_value(item) for item in value]
    return value


@dataclass
class PublicationRecoveryReport:
    committed_publication_ids: list[str] = field(default_factory=list)
    conflicted_publication_ids: list[str] = field(default_factory=list)
    failed_publication_ids: list[str] = field(default_factory=list)
    orphaned_dirty_days: list[tuple[str, str, str | None]] = field(default_factory=list)


def build_publication_operation(
    *,
    sequence: int,
    kind: Literal[
        "insert_episode_revision",
        "supersede_episode_revision",
        "insert_group_revision",
        "tombstone_group_revision",
        "upsert_rejected_reconciliation_retry",
    ],
    payload: dict,
    expected_revision: int | None = None,
) -> TimelinePublicationOperation:
    """Build an operation whose identity is stable across caller retries."""

    stable_payload = _stable_hash_value(payload)
    operation_id = canonical_hash(
        {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "sequence": sequence,
            "kind": kind,
            "expected_revision": expected_revision,
            "payload": stable_payload,
        }
    )
    return TimelinePublicationOperation(
        operation_id=operation_id,
        sequence=sequence,
        kind=kind,
        expected_revision=expected_revision,
        payload=stable_payload,
    )


def _verify_operation_identity(operation: TimelinePublicationOperation) -> None:
    expected = build_publication_operation(
        sequence=operation.sequence,
        kind=operation.kind,
        payload=operation.payload,
        expected_revision=operation.expected_revision,
    ).operation_id
    if operation.operation_id != expected:
        raise ValueError(
            f"publication operation hash mismatch for {operation.operation_id}"
        )


def publication_intent_payload(
    *,
    user_id: str,
    operation_source: str,
    affected_days: Sequence[TimelinePublicationDayPlan],
    operations: Sequence[TimelinePublicationOperation],
    evidence_fence: TimelinePublicationEvidenceFence | None = None,
) -> dict:
    """Canonical immutable journal content, excluding progress and timestamps."""

    days = sorted(affected_days, key=lambda item: (item.local_date, item.timezone))
    ordered_operations = [
        item.model_copy(update={"state": "pending", "applied_at": None})
        for item in sorted(operations, key=lambda item: item.sequence)
    ]
    day_payloads = []
    for item in days:
        snapshot = item.resulting_snapshot
        day_payloads.append(
            {
                "local_date": item.local_date.isoformat(),
                "timezone": item.timezone,
                "base_snapshot_id": item.base_snapshot_id,
                "resulting_snapshot": {
                    "schema_version": snapshot.schema_version,
                    "snapshot_id": snapshot.snapshot_id,
                    "episode_revisions": [
                        ref.model_dump(mode="json")
                        for ref in snapshot.episode_revisions
                    ],
                    "semantic_group_revisions": [
                        ref.model_dump(mode="json")
                        for ref in snapshot.semantic_group_revisions
                    ],
                    "evidence_state_hash": snapshot.evidence_state_hash,
                },
                "review_decision": (
                    item.review_decision.model_dump(mode="python")
                    if item.review_decision is not None
                    else None
                ),
            }
        )
    return {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "user_id": user_id,
        "operation_source": operation_source,
        "evidence_fence": (
            _bson_stable_value(evidence_fence.model_dump(mode="python"))
            if evidence_fence is not None
            else None
        ),
        "affected_days": day_payloads,
        "operations": [
            {
                "operation_id": item.operation_id,
                "sequence": item.sequence,
                "kind": item.kind,
                "expected_revision": item.expected_revision,
                "payload": item.payload,
            }
            for item in ordered_operations
        ],
    }


def publication_identity(
    *,
    user_id: str,
    operation_source: str,
    affected_days: Sequence[TimelinePublicationDayPlan],
    operations: Sequence[TimelinePublicationOperation],
    evidence_fence: TimelinePublicationEvidenceFence | None = None,
) -> tuple[str, str]:
    """Return ``(publication_id, intent_hash)`` for a complete intended mutation."""

    payload = publication_intent_payload(
        user_id=user_id,
        operation_source=operation_source,
        affected_days=affected_days,
        operations=operations,
        evidence_fence=evidence_fence,
    )
    intent_hash = canonical_hash(_stable_hash_value(payload))
    publication_id = canonical_hash(
        {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "user_id": user_id,
            "intent_hash": intent_hash,
        }
    )
    return publication_id, intent_hash


async def _load_day(
    plan: TimelinePublicationDayPlan, user_id: str
) -> TimelineDay | None:
    return await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == plan.local_date,
        TimelineDay.timezone == plan.timezone,
    )


async def _filter_stale_semantic_groups(
    user_id: str,
    affected_days: Sequence[TimelinePublicationDayPlan],
    operations: Sequence[TimelinePublicationOperation],
) -> list[TimelinePublicationDayPlan]:
    """Keep only group revisions whose exact members survive each successor snapshot.

    Existing groups live in their owner day's immutable history. Group revisions being
    created by this publication live in its operation intent and must also pass the
    same membership check. Centralizing this here keeps every episode publisher from
    having to reproduce the snapshot/group invariant.
    """

    operation_groups: dict[tuple[date, str, int], TimelineSemanticGroupRevision] = {}
    for operation in operations:
        if operation.kind not in {"insert_group_revision", "tombstone_group_revision"}:
            continue
        raw_revision = operation.payload.get("revision")
        raw_local_date = operation.payload.get("local_date")
        if raw_revision is None or raw_local_date is None:
            continue
        revision = TimelineSemanticGroupRevision.model_validate(raw_revision)
        owner_local_date = (
            date.fromisoformat(raw_local_date)
            if isinstance(raw_local_date, str)
            else raw_local_date
        )
        operation_groups[(owner_local_date, revision.group_key, revision.revision)] = (
            revision
        )

    owner_day_cache: dict[tuple[date, str], TimelineDay | None] = {}
    normalized: list[TimelinePublicationDayPlan] = []
    for plan in affected_days:
        snapshot = plan.resulting_snapshot
        exact_episode_refs = {
            (item.episode_key, item.revision) for item in snapshot.episode_revisions
        }
        kept_group_refs = []
        for group_ref in snapshot.semantic_group_revisions:
            key = (
                group_ref.owner_local_date,
                group_ref.group_key,
                group_ref.revision,
            )
            revision = operation_groups.get(key)
            if revision is None:
                owner_key = (group_ref.owner_local_date, plan.timezone)
                if owner_key not in owner_day_cache:
                    owner_day_cache[owner_key] = await TimelineDay.find_one(
                        TimelineDay.user_id == user_id,
                        TimelineDay.local_date == group_ref.owner_local_date,
                        TimelineDay.timezone == plan.timezone,
                    )
                owner_day = owner_day_cache[owner_key]
                revision = next(
                    (
                        item
                        for item in (
                            owner_day.semantic_group_history if owner_day else []
                        )
                        if item.group_key == group_ref.group_key
                        and item.revision == group_ref.revision
                    ),
                    None,
                )
            if revision is None:
                continue
            members = {
                (item.episode_key, item.revision) for item in revision.member_revisions
            }
            if revision.status == "active" and not members <= exact_episode_refs:
                continue
            kept_group_refs.append(group_ref)

        if kept_group_refs == snapshot.semantic_group_revisions:
            normalized.append(plan)
            continue
        resulting_snapshot = build_day_snapshot(
            user_id=user_id,
            local_date=plan.local_date,
            timezone_name=plan.timezone,
            evidence_state_hash=snapshot.evidence_state_hash,
            episode_revisions=snapshot.episode_revisions,
            semantic_group_revisions=kept_group_refs,
            created_at=snapshot.created_at,
        )
        review_decision = (
            plan.review_decision.model_copy(
                update={"run_id": resulting_snapshot.snapshot_id}
            )
            if plan.review_decision is not None
            else None
        )
        normalized.append(
            plan.model_copy(
                update={
                    "resulting_snapshot": resulting_snapshot,
                    "review_decision": review_decision,
                }
            )
        )
    return normalized


async def _validate_bases(
    user_id: str, affected_days: Sequence[TimelinePublicationDayPlan]
) -> None:
    for plan in affected_days:
        verify_day_snapshot(
            plan.resulting_snapshot,
            user_id=user_id,
            local_date=plan.local_date,
            timezone_name=plan.timezone,
        )
        day = await _load_day(plan, user_id)
        current_id = day.current_snapshot_id if day else None
        if day is not None and day.pending_publication_id:
            raise PublicationConflict(
                f"day {plan.local_date} already belongs to publication "
                f"{day.pending_publication_id}"
            )
        if current_id != plan.base_snapshot_id:
            raise PublicationConflict(
                f"day {plan.local_date} moved from base {plan.base_snapshot_id!r} "
                f"to {current_id!r}"
            )


async def _validate_evidence_fence(
    journal: TimelinePublicationJournal, *, renew_expired_lease: bool = False
) -> None:
    """Reject roll-forward when its lease or evidence generation is no longer current."""

    fence = journal.evidence_fence
    if fence is None:
        return
    collection = DirtyEvidenceRange.get_pymongo_collection()
    source = await collection.find_one(
        {
            "user_id": journal.user_id,
            "dirty_range_id": fence.dirty_range_id,
        },
        {
            "_id": 1,
            "state": 1,
            "lease_owner": 1,
            "attempts": 1,
            "evidence_revision": 1,
            "leased_evidence_revision": 1,
            "lease_expires_at": 1,
        },
    )
    if source is None:
        raise PublicationConflict(
            f"publication {journal.publication_id} lost its evidence range"
        )
    lease_expires_at = source.get("lease_expires_at")
    evidence_revision = source.get("evidence_revision")
    leased_evidence_revision = source.get("leased_evidence_revision")
    if lease_expires_at is not None and lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
    if (
        source.get("state") != "leased"
        or source.get("lease_owner") != fence.lease_owner
        or int(source.get("attempts") or 0) != fence.lease_attempt
        or evidence_revision is None
        or int(evidence_revision) != fence.leased_evidence_revision
        or leased_evidence_revision is None
        or int(leased_evidence_revision) != fence.leased_evidence_revision
        or lease_expires_at is None
    ):
        raise PublicationConflict(
            f"publication {journal.publication_id} lost its exact evidence lease"
        )
    if lease_expires_at <= utcnow():
        if not renew_expired_lease:
            raise PublicationConflict(
                f"publication {journal.publication_id} lost its exact evidence lease"
            )
        renewed_until = utcnow() + timedelta(minutes=dirty_ranges.LEASE_MINUTES)
        renewed = await collection.update_one(
            {
                "user_id": journal.user_id,
                "dirty_range_id": fence.dirty_range_id,
                "state": "leased",
                "lease_owner": fence.lease_owner,
                "attempts": fence.lease_attempt,
                "evidence_revision": fence.leased_evidence_revision,
                "leased_evidence_revision": fence.leased_evidence_revision,
            },
            {"$set": {"lease_expires_at": renewed_until, "updated_at": utcnow()}},
        )
        if renewed.matched_count != 1:
            raise PublicationConflict(
                f"publication {journal.publication_id} lost its exact evidence lease"
            )
    newer = await collection.find_one(
        {
            "user_id": journal.user_id,
            "dirty_range_id": {"$ne": fence.dirty_range_id},
            "evidence_revision": {"$gt": fence.leased_evidence_revision},
            "started_at": {"$lt": fence.ended_at},
            "ended_at": {"$gt": fence.started_at},
            "state": {"$ne": "superseded"},
        },
        {"_id": 1},
    )
    if newer is not None:
        raise PublicationConflict(
            f"publication {journal.publication_id} was invalidated by newer evidence"
        )


async def prepare_publication(
    *,
    user_id: str,
    operation_source: Literal["agent", "manual", "semantic_group", "projection"],
    affected_days: Sequence[TimelinePublicationDayPlan],
    operations: Sequence[TimelinePublicationOperation],
    evidence_fence: TimelinePublicationEvidenceFence | None = None,
) -> TimelinePublicationJournal:
    """Validate bases and durably record the complete intent before mutation."""

    if not affected_days and not operations:
        raise ValueError(
            "a timeline publication must affect a day or carry an operation"
        )
    ordered_operations = [
        item.model_copy(update={"state": "pending", "applied_at": None})
        for item in sorted(operations, key=lambda item: item.sequence)
    ]
    if [item.sequence for item in ordered_operations] != list(
        range(len(ordered_operations))
    ):
        raise ValueError("publication operation sequences must be contiguous")
    for operation in ordered_operations:
        _verify_operation_identity(operation)
    affected_days = await _filter_stale_semantic_groups(
        user_id, affected_days, ordered_operations
    )
    publication_id, intent_hash = publication_identity(
        user_id=user_id,
        operation_source=operation_source,
        affected_days=affected_days,
        operations=ordered_operations,
        evidence_fence=evidence_fence,
    )
    existing = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.user_id == user_id,
        TimelinePublicationJournal.publication_id == publication_id,
    )
    if existing is not None:
        if existing.intent_hash != intent_hash:
            raise PublicationConflict(
                f"publication {publication_id} exists with a different intent"
            )
        return existing
    await _validate_bases(user_id, affected_days)
    journal = TimelinePublicationJournal(
        publication_id=publication_id,
        intent_hash=intent_hash,
        user_id=user_id,
        operation_source=operation_source,
        evidence_fence=evidence_fence,
        affected_days=sorted(
            affected_days, key=lambda item: (item.local_date, item.timezone)
        ),
        operations=ordered_operations,
    )
    await journal.insert()
    return journal


async def _set_journal_status(
    journal: TimelinePublicationJournal,
    status: Literal[
        "prepared",
        "days_dirty",
        "applying",
        "snapshots_installed",
        "committed",
        "conflict",
    ],
    *,
    error: str | None = None,
) -> None:
    stamp = utcnow()
    update: dict = {"$set": {"status": status, "updated_at": stamp}}
    if status == "committed":
        update["$set"]["committed_at"] = stamp
    if error:
        update["$push"] = {"errors": error}
    await TimelinePublicationJournal.get_pymongo_collection().update_one(
        {"user_id": journal.user_id, "publication_id": journal.publication_id},
        update,
    )
    journal.status = status
    journal.updated_at = stamp
    if status == "committed":
        journal.committed_at = stamp
    if error:
        journal.errors.append(error)


async def _default_conflict_notifier(
    journal: TimelinePublicationJournal, detail: str
) -> None:
    try:
        # Keep observability best-effort so a ledger outage cannot block recovery.
        from backend.services.observability.system_events import record_event

        await record_event(
            severity="error",
            category="pipeline",
            source="timeline.publication",
            title="Timeline publication requires review",
            detail=detail,
            user_id=journal.user_id,
            metadata={"publication_id": journal.publication_id},
            incident_key=f"timeline-publication:{journal.user_id}:{journal.publication_id}",
        )
    except Exception:  # pragma: no cover - the journal remains authoritative
        logger.exception("Could not record Timeline publication conflict system event")


async def _mark_conflict(
    journal: TimelinePublicationJournal,
    detail: str,
    notifier: ConflictNotifier | None,
) -> None:
    await _set_journal_status(journal, "conflict", error=detail)
    await (notifier or _default_conflict_notifier)(journal, detail)


async def _mark_days_dirty(journal: TimelinePublicationJournal) -> None:
    collection = TimelineDay.get_pymongo_collection()
    for plan in journal.affected_days:
        day = await _load_day(plan, journal.user_id)
        if day is None:
            if plan.base_snapshot_id is not None:
                raise PublicationConflict(
                    f"day {plan.local_date} disappeared before dirtying"
                )
            await TimelineDay(
                user_id=journal.user_id,
                local_date=plan.local_date,
                timezone=plan.timezone,
                snapshot_state="dirty",
                pending_publication_id=journal.publication_id,
            ).insert()
            continue
        if (
            day.pending_publication_id == journal.publication_id
            and day.snapshot_state == "dirty"
        ):
            continue
        if day.pending_publication_id is not None:
            raise PublicationConflict(
                f"day {plan.local_date} is dirty for {day.pending_publication_id}"
            )
        if day.current_snapshot_id != plan.base_snapshot_id:
            raise PublicationConflict(
                f"day {plan.local_date} no longer matches base "
                f"{plan.base_snapshot_id!r}"
            )
        result = await collection.update_one(
            {
                "_id": day.id,
                "current_snapshot_id": plan.base_snapshot_id,
                "pending_publication_id": None,
            },
            {
                "$set": {
                    "snapshot_state": "dirty",
                    "pending_publication_id": journal.publication_id,
                    "reviewed_snapshot_id": None,
                    "revised_at": utcnow(),
                }
            },
        )
        if result.modified_count != 1:
            raise PublicationConflict(f"day {plan.local_date} changed while dirtying")
    await _set_journal_status(journal, "days_dirty")


async def _default_operation_applier(
    journal: TimelinePublicationJournal, operation: TimelinePublicationOperation
) -> OperationOutcome:
    """Apply persisted operation payloads so recovery needs no original caller."""

    payload = operation.payload
    if operation.kind == "insert_episode_revision":
        encoded = payload.get("episode", payload)
        successor = TimelineEpisode.model_validate(encoded)

        await privacy.require_record(successor, journal.user_id)
        existing = await TimelineEpisode.find_one(
            TimelineEpisode.user_id == journal.user_id,
            TimelineEpisode.episode_key == successor.episode_key,
            TimelineEpisode.revision == successor.revision,
        )
        if existing is not None:
            return (
                "already_applied"
                if existing.episode_id == successor.episode_id
                else "conflict"
            )
        await successor.insert()
        return "applied"

    if operation.kind == "supersede_episode_revision":
        query = {
            "user_id": journal.user_id,
            "episode_key": payload["episode_key"],
            "revision": int(payload["revision"]),
        }
        if payload.get("episode_id"):
            query["episode_id"] = payload["episode_id"]
        existing = await TimelineEpisode.find_one(query)
        if existing is None:
            return "conflict"
        desired = sorted(set(payload.get("successor_keys") or []))
        if existing.status == "superseded":
            return (
                "already_applied"
                if sorted(existing.successor_keys) == desired
                else "conflict"
            )
        result = await TimelineEpisode.get_pymongo_collection().update_one(
            {**query, "status": existing.status},
            {
                "$set": {
                    "status": "superseded",
                    "successor_keys": desired,
                    "revised_at": (
                        datetime.fromisoformat(payload["revised_at"])
                        if isinstance(payload.get("revised_at"), str)
                        else payload.get("revised_at") or utcnow()
                    ),
                }
            },
        )
        return "applied" if result.modified_count == 1 else "conflict"

    if operation.kind == "upsert_rejected_reconciliation_retry":
        return await apply_rejected_retry_operation(journal.user_id, operation)

    return await apply_group_revision(
        journal.user_id, journal.publication_id, operation
    )


async def apply_group_revision(
    user_id: str, publication_id: str, operation: TimelinePublicationOperation
) -> OperationOutcome:
    """Apply a group's immutable revision during publication or recovery."""
    payload = operation.payload
    revision = TimelineSemanticGroupRevision.model_validate(payload["revision"])
    local_date = date.fromisoformat(payload["local_date"])
    day = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == payload["timezone"],
    )
    if day is None or day.pending_publication_id != publication_id:
        return "conflict"
    history = day.semantic_group_history
    existing_revision = next(
        (
            item
            for item in history
            if item.group_key == revision.group_key
            and item.revision == revision.revision
        ),
        None,
    )
    if existing_revision is not None:
        existing_value = _bson_stable_value(existing_revision.model_dump(mode="python"))
        intended_value = _bson_stable_value(revision.model_dump(mode="python"))
        return "already_applied" if existing_value == intended_value else "conflict"
    highest = max(
        (item.revision for item in history if item.group_key == revision.group_key),
        default=0,
    )
    if highest != operation.expected_revision:
        return "conflict"
    update: dict = {
        "$push": {"semantic_group_history": revision.model_dump(mode="json")},
        "$set": {"revised_at": utcnow()},
    }
    if payload.get("decision"):
        update["$push"]["review_decisions"] = TimelineReviewDecision.model_validate(
            payload["decision"]
        ).model_dump(mode="json")
    result = await TimelineDay.get_pymongo_collection().update_one(
        {
            "_id": day.id,
            "pending_publication_id": publication_id,
            "semantic_group_history": {
                "$not": {
                    "$elemMatch": {
                        "group_key": revision.group_key,
                        "revision": revision.revision,
                    }
                }
            },
        },
        update,
    )
    return "applied" if result.modified_count == 1 else "conflict"


async def apply_rejected_retry_operation(
    user_id: str, operation: TimelinePublicationOperation
) -> OperationOutcome:
    """Idempotently materialize one rejected-hypothesis retry intent."""

    payload = operation.payload
    if operation.kind != "upsert_rejected_reconciliation_retry":
        raise ValueError("operation is not a rejected reconciliation retry")
    try:
        successor = DirtyEvidenceRange.model_validate(payload["successor"])
        if successor.user_id != user_id:
            return "conflict"
        existing = await DirtyEvidenceRange.find_one(
            DirtyEvidenceRange.dirty_range_id == successor.dirty_range_id
        )
        applied = False
        if existing is None:
            await successor.insert()
            applied = True
        elif any(
            _bson_stable_value(getattr(existing, field))
            != _bson_stable_value(getattr(successor, field))
            for field in (
                "user_id",
                "started_at",
                "ended_at",
                "authorized_started_at",
                "authorized_ended_at",
                "dispatch_authorized_at",
                "reconciliation_request_id",
                "parent_dirty_range_id",
                "rejection_retry_depth",
                "rejection_hypothesis_id",
                "rejection_reason_code",
                "rejection_evidence_ids",
            )
        ):
            return "conflict"

        rejection = payload["rejection"]
        parent = await DirtyEvidenceRange.find_one(
            DirtyEvidenceRange.dirty_range_id == payload["parent_dirty_range_id"]
        )
        if parent is None or parent.user_id != user_id:
            return "conflict"
        known = next(
            (
                item
                for item in parent.interpretation_rejections
                if item.successor_dirty_range_id == successor.dirty_range_id
            ),
            None,
        )
        expected_rejection = TimelineInterpretationRejectionState.model_validate(
            rejection
        )
        if known is not None:
            if _bson_stable_value(
                known.model_dump(mode="python")
            ) != _bson_stable_value(expected_rejection.model_dump(mode="python")):
                return "conflict"
        else:
            result = await DirtyEvidenceRange.get_pymongo_collection().update_one(
                {
                    "dirty_range_id": parent.dirty_range_id,
                    "interpretation_rejections.successor_dirty_range_id": {
                        "$ne": successor.dirty_range_id
                    },
                },
                {
                    "$push": {
                        "interpretation_rejections": expected_rejection.model_dump(
                            mode="python"
                        )
                    },
                    "$set": {"updated_at": utcnow()},
                },
            )
            if result.modified_count != 1:
                return "conflict"
            applied = True
        return "applied" if applied else "already_applied"
    except (KeyError, TypeError, ValueError):
        return "conflict"


async def _apply_operations(
    journal: TimelinePublicationJournal, apply_operation: OperationApplier | None
) -> None:
    await _set_journal_status(journal, "applying")
    collection = TimelinePublicationJournal.get_pymongo_collection()
    for index, operation in enumerate(journal.operations):
        if operation.state == "applied":
            continue
        outcome = await (
            apply_operation(operation)
            if apply_operation is not None
            else _default_operation_applier(journal, operation)
        )
        if outcome == "conflict":
            raise PublicationConflict(
                f"operation {operation.operation_id} ({operation.kind}) conflicted"
            )
        if outcome not in {"applied", "already_applied"}:
            raise ValueError(
                f"operation applier returned invalid outcome {outcome!r} for "
                f"{operation.operation_id}"
            )
        stamp = utcnow()
        result = await collection.update_one(
            {
                "user_id": journal.user_id,
                "publication_id": journal.publication_id,
                f"operations.{index}.operation_id": operation.operation_id,
            },
            {
                "$set": {
                    f"operations.{index}.state": "applied",
                    f"operations.{index}.applied_at": stamp,
                    "updated_at": stamp,
                }
            },
        )
        if result.matched_count != 1:
            raise IncompletePublication(
                f"could not persist progress for operation {operation.operation_id}"
            )
        operation.state = "applied"
        operation.applied_at = stamp


def _installed_state(day: TimelineDay, snapshot_id: str) -> str:
    # Memory acceptance belongs to exact selections, not this whole-day projection.
    # Selection recovery independently reopens only accounts whose evidence changed.
    return "reviewed" if day.reviewed_snapshot_id == snapshot_id else "ready"


async def _install_snapshots(journal: TimelinePublicationJournal) -> None:
    collection = TimelineDay.get_pymongo_collection()
    for plan in journal.affected_days:
        day = await _load_day(plan, journal.user_id)
        if day is None:
            raise PublicationConflict(f"day {plan.local_date} vanished before install")
        snapshot_id = plan.resulting_snapshot.snapshot_id
        if (
            day.current_snapshot_id == snapshot_id
            and day.pending_publication_id is None
        ):
            continue
        if day.pending_publication_id != journal.publication_id:
            raise PublicationConflict(
                f"day {plan.local_date} lost pending publication ownership"
            )
        set_fields = {
            # Raw PyMongo does not apply Beanie's ``date`` encoder. JSON-mode
            # values are BSON-safe and Pydantic restores the typed embedded
            # snapshot (including group owner dates) on read.
            "current_snapshot": plan.resulting_snapshot.model_dump(mode="json"),
            "current_snapshot_id": snapshot_id,
            "snapshot_state": _installed_state(day, snapshot_id),
            "revised_at": utcnow(),
        }
        update: dict = {
            "$set": set_fields,
            "$unset": {"pending_publication_id": ""},
        }
        if plan.review_decision is not None:
            set_fields.update(
                {
                    "review_state": "episodes_pending",
                    "review_snapshot_id": None,
                    "memory_review_proposal_id": None,
                    "episodes_reviewed_at": None,
                    "review_resolved_at": None,
                    "review_outcome": None,
                    "review_error": None,
                    "consolidation_state": "",
                    "consolidation_snapshot_id": None,
                    "consolidation_suggestions": [],
                }
            )
            update["$push"] = {
                "review_decisions": plan.review_decision.model_dump(mode="python")
            }
        result = await collection.update_one(
            {
                "_id": day.id,
                "current_snapshot_id": plan.base_snapshot_id,
                "pending_publication_id": journal.publication_id,
                "snapshot_state": "dirty",
            },
            update,
        )
        if result.modified_count != 1:
            raise PublicationConflict(f"day {plan.local_date} changed during install")
    await _set_journal_status(journal, "snapshots_installed")


async def _roll_forward_publication_locked(
    journal: TimelinePublicationJournal,
    *,
    apply_operation: OperationApplier | None = None,
    conflict_notifier: ConflictNotifier | None = None,
    renew_expired_evidence_lease: bool = False,
) -> TimelinePublicationJournal:
    """Idempotently finish one prepared journal while its user lock is held."""

    if journal.status == "committed":
        return journal
    if journal.status == "conflict":
        raise PublicationConflict(
            f"publication {journal.publication_id} is in conflict"
        )
    try:
        expected_publication_id, expected_intent_hash = publication_identity(
            user_id=journal.user_id,
            operation_source=journal.operation_source,
            affected_days=journal.affected_days,
            operations=journal.operations,
            evidence_fence=journal.evidence_fence,
        )
        if (
            journal.publication_id != expected_publication_id
            or journal.intent_hash != expected_intent_hash
        ):
            raise PublicationConflict(
                f"publication {journal.publication_id} no longer matches its intent"
            )
        for operation in journal.operations:
            _verify_operation_identity(operation)
        await _validate_evidence_fence(
            journal, renew_expired_lease=renew_expired_evidence_lease
        )
        if journal.status == "prepared":
            await _mark_days_dirty(journal)
        if journal.status in {"days_dirty", "applying"}:
            await _apply_operations(journal, apply_operation)
        if journal.status == "applying":
            await _install_snapshots(journal)
        if journal.status == "snapshots_installed":
            await _set_journal_status(journal, "committed")
        return journal
    except PublicationConflict as exc:
        await _mark_conflict(journal, str(exc), conflict_notifier)
        raise
    except Exception as exc:
        await _set_journal_status(journal, journal.status, error=str(exc))
        raise IncompletePublication(
            f"publication {journal.publication_id} remains recoverable: {exc}"
        ) from exc


async def publish_timeline_revision(
    *,
    user_id: str,
    operation_source: Literal["agent", "manual", "semantic_group", "projection"],
    affected_days: Sequence[TimelinePublicationDayPlan],
    operations: Sequence[TimelinePublicationOperation] = (),
    apply_operation: OperationApplier | None = None,
    conflict_notifier: ConflictNotifier | None = None,
    publication_guard: PublicationGuard | None = None,
    evidence_fence: TimelinePublicationEvidenceFence | None = None,
) -> TimelinePublicationJournal:
    """Prepare and roll forward one publication under the existing per-user lock."""

    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=300, blocking_timeout=300
    ):
        if publication_guard is not None:
            await publication_guard()
        journal = await prepare_publication(
            user_id=user_id,
            operation_source=operation_source,
            affected_days=affected_days,
            operations=operations,
            evidence_fence=evidence_fence,
        )
        completed = await _roll_forward_publication_locked(
            journal,
            apply_operation=apply_operation,
            conflict_notifier=conflict_notifier,
        )
    try:
        # Defer this dependency to break the import cycle through
        # backend.services.timeline.sessions -> backend.services.timeline.consolidation ->
        # backend.services.timeline.publication.
        from .sessions import queue_published_sessions

        await queue_published_sessions(completed)
        # Defer this dependency to break the import cycle through backend.services.source_search
        # -> backend.services.timeline.consolidation -> backend.services.timeline.publication.
        from backend.services.source_search import index_day

        for plan in completed.affected_days:
            day = await TimelineDay.find_one(
                TimelineDay.user_id == user_id,
                TimelineDay.local_date == plan.local_date,
                TimelineDay.timezone == plan.timezone,
            )
            if day:
                await index_day(day)
    except Exception:
        logger.warning(
            "Session preparation enqueue deferred to recovery", exc_info=True
        )
    return completed


async def run_guarded_publication_action(
    user_id: str,
    *,
    publication_guard: PublicationGuard,
    action: PublicationAction,
) -> object:
    """Run a non-journaled atomic mutation behind the same evidence boundary."""

    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=300, blocking_timeout=300
    ):
        await publication_guard()
        return await action()


async def recover_timeline_publications(
    *,
    apply_operation: OperationApplier | None = None,
    conflict_notifier: ConflictNotifier | None = None,
    user_id: str | None = None,
) -> PublicationRecoveryReport:
    """Scan journals and dirty days, then roll every recoverable intent forward."""

    query: dict = {"status": {"$ne": "committed"}}
    if user_id is not None:
        query["user_id"] = user_id
    journals = await TimelinePublicationJournal.find(query).sort("created_at").to_list()
    report = PublicationRecoveryReport()
    for journal in journals:
        if journal.status == "conflict":
            report.conflicted_publication_ids.append(journal.publication_id)
            continue
        try:
            async with distributed_lock(
                timeline_publication_lock(journal.user_id),
                timeout=300,
                blocking_timeout=300,
            ):
                fresh = await TimelinePublicationJournal.find_one(
                    TimelinePublicationJournal.user_id == journal.user_id,
                    TimelinePublicationJournal.publication_id == journal.publication_id,
                )
                if fresh is None:
                    report.failed_publication_ids.append(journal.publication_id)
                    continue
                await _roll_forward_publication_locked(
                    fresh,
                    apply_operation=apply_operation,
                    conflict_notifier=conflict_notifier,
                    renew_expired_evidence_lease=True,
                )
                report.committed_publication_ids.append(journal.publication_id)
        except PublicationConflict:
            report.conflicted_publication_ids.append(journal.publication_id)
        except IncompletePublication:
            report.failed_publication_ids.append(journal.publication_id)

    dirty_query: dict = {"snapshot_state": "dirty"}
    if user_id is not None:
        dirty_query["user_id"] = user_id
    dirty_days = await TimelineDay.find(dirty_query).to_list()
    pending_ids = {
        item.pending_publication_id
        for item in dirty_days
        if item.pending_publication_id is not None
    }
    known = (
        await TimelinePublicationJournal.find(
            {"publication_id": {"$in": list(pending_ids)}}
        ).to_list()
        if pending_ids
        else []
    )
    known_ids = {item.publication_id for item in known}
    for day in dirty_days:
        if day.pending_publication_id not in known_ids:
            report.orphaned_dirty_days.append(
                (day.user_id, day.local_date.isoformat(), day.pending_publication_id)
            )
    return report
