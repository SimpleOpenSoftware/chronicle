"""Dirty-range scheduling: coalescing algebra, leasing, and the recovery scan."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.device_input import DeviceInputJob
from backend.models.timeline import (
    DirtyEvidenceRange,
    EvidenceLocator,
    TimelineInterpretationRejectionState,
    TimelineReconciliationRequest,
)
from backend.redis_keys import timeline_evidence_revision
from backend.services.timeline import dirty_ranges
from backend.services.timeline.contracts import StageContextRequest
from backend.services.timeline.dirty_ranges import _as_utc
from backend.workers import timeline_jobs

START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _RedisCounterFake:
    """Stands in for the per-user Redis INCR counter."""

    def __init__(self):
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str):
        return self.values.get(key)

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def dirty_range_documents(mongo_service, monkeypatch):
    """Real documents so the model's validators run, with Redis stubbed."""

    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_dirty_ranges_db"]
    await init_beanie(
        database=database,
        document_models=[
            DirtyEvidenceRange,
            DeviceInputJob,
            TimelineReconciliationRequest,
        ],
    )
    await DirtyEvidenceRange.delete_all()
    counter = _RedisCounterFake()
    monkeypatch.setattr(dirty_ranges, "create_async_redis", lambda **_: counter)

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(dirty_ranges, "distributed_lock", unlocked)
    yield counter
    await client.drop_database("test_dirty_ranges_db")
    client.close()


async def _mark(offset_minutes: float, minutes: float, reason: str, **kwargs):
    authorized = kwargs.pop("authorized", False)
    if authorized:
        return await dirty_ranges.authorize_explicit_range(
            user_id="user",
            started_at=START + timedelta(minutes=offset_minutes),
            ended_at=START + timedelta(minutes=offset_minutes + minutes),
            reconciliation_request_id=f"request-{reason}-{offset_minutes}",
            reason=reason,
        )
    row = await dirty_ranges.mark_evidence_dirty(
        "user",
        START + timedelta(minutes=offset_minutes),
        START + timedelta(minutes=offset_minutes + minutes),
        f"rev-{reason}-{offset_minutes}",
        reason,
        **kwargs,
    )
    return row


@pytest.mark.asyncio
async def test_morning_completion_preserves_afternoon_dirty_evidence(
    dirty_range_documents,
):
    """Completing one checkpoint must not complete later captured evidence."""
    morning = await _mark(0, 60, "morning_checkpoint", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        morning.dirty_range_id, "worker"
    )
    afternoon = await _mark(120, 72, "transcript_revision")
    await dirty_ranges.complete_range(leased)

    completed = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == morning.dirty_range_id
    )
    remaining = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == afternoon.dirty_range_id
    )
    assert completed.state == "completed"
    assert _as_utc(completed.ended_at) == START + timedelta(minutes=60)
    assert remaining.state == "pending"
    assert _as_utc(remaining.started_at) == START + timedelta(minutes=120)
    assert _as_utc(remaining.ended_at) == START + timedelta(minutes=192)
    assert remaining.dispatch_authorized_at is None
    assert await dirty_ranges.due_ranges() == []


@pytest.mark.asyncio
async def test_overlapping_triggers_coalesce_into_one_row(dirty_range_documents):
    first = await _mark(0, 10, "conversation_closed", source_kind="conversation")
    second = await _mark(5, 10, "transcript_revision", source_kind="transcript")

    assert second.dirty_range_id == first.dirty_range_id
    assert await DirtyEvidenceRange.find_all().count() == 1
    # MongoDB returns naive UTC, so compare on the normalized instant.
    assert _as_utc(second.started_at) == START
    assert _as_utc(second.ended_at) == START + timedelta(minutes=15)
    assert second.trigger_reasons == ["conversation_closed", "transcript_revision"]
    assert set(second.source_revisions) == {"conversation", "transcript"}
    assert second.evidence_revision == 2


@pytest.mark.asyncio
async def test_repeated_screening_appends_without_rewriting_revision_history(
    dirty_range_documents, monkeypatch
):
    """Historical screening must not resend a growing multi-megabyte work row."""
    from bson import BSON

    first = await _mark(0, 10, "privacy_screening", source_kind="privacy")
    history = [f"synthetic-screening-{index:064d}" for index in range(5000)]
    first.source_revisions = {"privacy": history, "transcript": ["retained-transcript"]}
    await first.save()
    original_deadline = first.force_after
    collection = DirtyEvidenceRange.get_pymongo_collection()
    write_sizes = []
    for method_name in ("update_one", "find_one_and_update"):
        original = getattr(collection, method_name)

        async def measured(*args, _original=original, **kwargs):
            update = args[1] if len(args) > 1 else kwargs["update"]
            write_sizes.append(len(BSON.encode(update)))
            return await _original(*args, **kwargs)

        monkeypatch.setattr(collection, method_name, measured)

    second = await _mark(5, 10, "privacy_screening", source_kind="privacy")
    repeated = await _mark(5, 10, "privacy_screening", source_kind="privacy")
    stored = await DirtyEvidenceRange.get(first.id)
    expected = {
        "privacy": history + ["rev-privacy_screening-5"],
        "transcript": ["retained-transcript"],
    }
    assert (
        second.source_revisions
        == repeated.source_revisions
        == stored.source_revisions
        == expected
    )
    assert stored.evidence_revision == 3
    assert _as_utc(stored.started_at) == START
    assert _as_utc(stored.ended_at) == START + timedelta(minutes=15)
    assert _as_utc(stored.force_after) == _as_utc(original_deadline)
    assert stored.state == "pending" and stored.dispatch_authorized_at is None
    assert write_sizes and max(write_sizes) < 16384


@pytest.mark.asyncio
async def test_coalescing_cannot_overwrite_a_new_authorization(
    dirty_range_documents, monkeypatch
):
    first = await _mark(0, 10, "privacy_screening", source_kind="privacy")
    collection = DirtyEvidenceRange.get_pymongo_collection()
    original = collection.update_one

    async def authorize_before_update(*args, **kwargs):
        await original({"_id": first.id}, {"$set": {"state": "authorized_pending"}})
        return await original(*args, **kwargs)

    monkeypatch.setattr(collection, "update_one", authorize_before_update)
    with pytest.raises(RuntimeError, match="changed during coalescing"):
        await _mark(5, 10, "privacy_screening", source_kind="privacy")
    stored = await DirtyEvidenceRange.get(first.id)
    assert stored.state == "authorized_pending"
    assert stored.evidence_revision == first.evidence_revision
    assert stored.source_revisions == first.source_revisions
    assert _as_utc(stored.ended_at) == START + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_nearby_ranges_within_the_gap_merge(dirty_range_documents):
    first = await _mark(0, 5, "conversation_closed")
    # Starts 3 minutes after the first ends — inside COALESCE_GAP_MINUTES.
    second = await _mark(8, 5, "evidence_span")

    assert second.dirty_range_id == first.dirty_range_id
    assert _as_utc(second.ended_at) == START + timedelta(minutes=13)


@pytest.mark.asyncio
async def test_distant_ranges_stay_separate(dirty_range_documents):
    first = await _mark(0, 5, "conversation_closed")
    second = await _mark(60, 5, "conversation_closed")

    assert second.dirty_range_id != first.dirty_range_id
    assert await DirtyEvidenceRange.find_all().count() == 2


@pytest.mark.asyncio
async def test_debounce_resets_but_forced_deadline_is_preserved(dirty_range_documents):
    first = await _mark(0, 10, "conversation_closed")
    original_force_after = first.force_after
    original_not_before = first.not_before

    second = await _mark(1, 10, "transcript_revision")

    assert _as_utc(second.not_before) >= _as_utc(original_not_before)
    # Late evidence extends the debounce; it must not postpone forced progress.
    drift = abs(_as_utc(second.force_after) - _as_utc(original_force_after))
    assert drift < timedelta(milliseconds=2)  # BSON truncates to milliseconds


@pytest.mark.asyncio
async def test_force_override_schedules_immediately(dirty_range_documents):
    now = datetime.now(timezone.utc)
    row = await _mark(0, 10, "manual_request", authorized=True)
    assert _as_utc(row.not_before) <= now + timedelta(seconds=1)

    due = await dirty_ranges.due_ranges()
    assert [item.dirty_range_id for item in due] == [row.dirty_range_id]


@pytest.mark.asyncio
async def test_unauthorized_dirty_range_never_becomes_due(dirty_range_documents):
    row = await _mark(
        0,
        10,
        "producer_update",
        not_before=datetime.now(timezone.utc),
    )

    assert row.dispatch_authorized_at is None
    assert await dirty_ranges.due_ranges() == []
    assert await dirty_ranges.lease_due_range("worker") is None


@pytest.mark.asyncio
async def test_trigger_never_coalesces_into_a_leased_range(dirty_range_documents):
    leased_row = await _mark(0, 10, "conversation_closed", authorized=True)
    leased = await dirty_ranges.lease_due_range("worker-1")
    assert leased is not None and leased.dirty_range_id == leased_row.dirty_range_id
    snapshot = leased.leased_evidence_revision

    fresh = await _mark(2, 10, "transcript_revision")

    assert fresh.dirty_range_id != leased.dirty_range_id
    assert fresh.state == "pending"
    reloaded = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == leased.dirty_range_id
    )
    # The run keeps reconciling its own snapshot; the fresh row re-reconciles after.
    assert reloaded.state == "leased"
    assert reloaded.leased_evidence_revision == snapshot
    assert reloaded.lease_owner == "worker-1"


@pytest.mark.asyncio
async def test_ordinary_trigger_cannot_inherit_a_waiting_authorization(
    dirty_range_documents,
):
    row = await _mark(0, 10, "conversation_closed", authorized=True)
    leased = await dirty_ranges.lease_due_range("worker-1")
    await dirty_ranges.park_waiting(leased, "needs future evidence")

    parked = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == row.dirty_range_id
    )
    assert parked.state == "waiting"
    assert "waiting:needs future evidence" in parked.trigger_reasons

    woken = await _mark(1, 5, "speaker_revision")
    assert woken.dirty_range_id != row.dirty_range_id
    assert woken.state == "pending"
    assert woken.dispatch_authorized_at is None


@pytest.mark.asyncio
async def test_lease_is_exclusive_and_expired_leases_are_reclaimed(
    dirty_range_documents,
):
    await _mark(0, 10, "conversation_closed", authorized=True)

    first = await dirty_ranges.lease_due_range("worker-1")
    assert first is not None
    assert await dirty_ranges.lease_due_range("worker-2") is None

    reclaimed = await dirty_ranges.reap_expired_leases(
        datetime.now(timezone.utc) + timedelta(minutes=dirty_ranges.LEASE_MINUTES + 1)
    )
    assert reclaimed == 1

    second = await dirty_ranges.lease_due_range("worker-2")
    assert second is not None
    assert second.dirty_range_id == first.dirty_range_id
    assert second.lease_owner == "worker-2"
    assert second.attempts == 2


@pytest.mark.asyncio
async def test_claim_snapshots_the_current_user_evidence_counter(
    dirty_range_documents,
):
    row = await _mark(0, 10, "conversation_closed", authorized=True)
    dirty_range_documents.values[timeline_evidence_revision("user")] = 9

    leased = await dirty_ranges.lease_authorized_range_by_id(
        row.dirty_range_id, "worker"
    )

    assert leased.evidence_revision == 9
    assert leased.leased_evidence_revision == 9


@pytest.mark.asyncio
async def test_a_range_that_burns_every_attempt_fails(dirty_range_documents):
    await _mark(0, 10, "conversation_closed", authorized=True)

    for attempt in range(dirty_ranges.MAX_ATTEMPTS):
        leased = await dirty_ranges.lease_due_range(f"worker-{attempt}")
        assert leased is not None
        # A crashed worker leaves the lease behind; reclaim it and try again.
        await dirty_ranges.reap_expired_leases(
            datetime.now(timezone.utc)
            + timedelta(minutes=dirty_ranges.LEASE_MINUTES + 1)
        )

    assert await dirty_ranges.lease_due_range("worker-last") is None
    row = await DirtyEvidenceRange.find_one({})
    assert row.state == "failed"
    assert "exhausted" in (row.last_error or "")


@pytest.mark.asyncio
async def test_exhaustion_scan_does_not_revoke_an_active_final_attempt(
    dirty_range_documents,
):
    row = await _mark(0, 10, "conversation_closed", authorized=True)
    leased = await dirty_ranges.lease_due_range("final-worker")
    future_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"_id": leased.id},
        {
            "$set": {
                "attempts": dirty_ranges.MAX_ATTEMPTS,
                "lease_expires_at": future_expiry,
            }
        },
    )

    assert await dirty_ranges.lease_due_range("recovery-worker") is None
    active = await DirtyEvidenceRange.get(row.id)
    assert active.state == "leased"
    assert active.lease_owner == "final-worker"
    assert active.attempts == dirty_ranges.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_complete_range_terminates_and_records_error(dirty_range_documents):
    await _mark(0, 10, "conversation_closed", authorized=True)
    leased = await dirty_ranges.lease_due_range("worker-1")

    await dirty_ranges.complete_range(leased, error="boom")
    row = await DirtyEvidenceRange.find_one({})
    assert row.state == "failed"
    assert row.last_error == "boom"
    assert row.lease_owner is None


@pytest.mark.asyncio
async def test_authorized_range_is_due_immediately(dirty_range_documents):
    await _mark(0, 10, "conversation_closed", authorized=True)
    assert len(await dirty_ranges.due_ranges()) == 1
    assert await dirty_ranges.lease_due_range("worker-1") is not None


@pytest.mark.asyncio
async def test_reconcile_dirty_ranges_scan_enqueues_due_work(
    dirty_range_documents, monkeypatch
):
    """The registered cron entry point itself, with only the enqueue faked."""

    # Lease the stale range first, while it is the only due row, so the reclaim below
    # is unambiguous.
    stale = await _mark(240, 10, "evidence_span", authorized=True)
    leased = await dirty_ranges.lease_due_range("dead-worker")
    assert leased.dirty_range_id == stale.dirty_range_id

    row = await _mark(0, 10, "conversation_closed", authorized=True)
    await _mark(120, 10, "conversation_closed")  # still debounced
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    await DirtyEvidenceRange.get_pymongo_collection().update_one(
        {"dirty_range_id": stale.dirty_range_id},
        {"$set": {"lease_expires_at": expired}},
    )

    enqueued: list[str] = []

    def _fake_enqueue(request_id: str):
        enqueued.append(request_id)
        return f"job-{request_id}"

    monkeypatch.setattr(
        "backend.controllers.queue_controller."
        "enqueue_explicit_timeline_reconciliation",
        _fake_enqueue,
    )

    outcome = await dirty_ranges.reconcile_dirty_ranges()

    assert outcome["reclaimed"] == 1
    assert outcome["due"] == 2
    assert outcome["enqueued"] == 2
    assert set(enqueued) == {
        row.reconciliation_request_id,
        stale.reconciliation_request_id,
    }


@pytest.mark.asyncio
async def test_explicit_authorization_is_deterministic_exact_and_noncoalescing(
    dirty_range_documents,
):
    ordinary = await _mark(0, 20, "ordinary")
    first = await dirty_ranges.authorize_explicit_range(
        user_id="user",
        started_at=START + timedelta(minutes=5),
        ended_at=START + timedelta(minutes=15),
        reconciliation_request_id="explicit-one",
        reason="manual",
    )
    second = await dirty_ranges.authorize_explicit_range(
        user_id="user",
        started_at=START + timedelta(minutes=5),
        ended_at=START + timedelta(minutes=15),
        reconciliation_request_id="explicit-one",
        reason="manual",
    )

    assert first.dirty_range_id == second.dirty_range_id
    assert first.dirty_range_id != ordinary.dirty_range_id
    assert first.state == "authorized_pending"
    assert _as_utc(first.started_at) == START + timedelta(minutes=5)
    assert _as_utc(first.ended_at) == START + timedelta(minutes=15)
    assert await DirtyEvidenceRange.find_all().count() == 2


def _context_request(leased: DirtyEvidenceRange) -> StageContextRequest:
    request = StageContextRequest(
        context_request_id="context-one",
        hypothesis_id="hypothesis-one",
        stage="separation",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-source",
            modality="screen",
            track_id="display-1",
        ),
        started_at=START - timedelta(minutes=2),
        ended_at=START + timedelta(minutes=12),
        base_manifest_hash="manifest-one",
        leased_evidence_revision=leased.leased_evidence_revision,
        target_resolution="one_frame_per_10_seconds",
        max_items=12,
        reason="uncertain screen transition",
    )
    return dirty_ranges.bind_context_request(leased, request)


@pytest.mark.asyncio
async def test_context_completion_refences_before_authorizing_successor(
    dirty_range_documents,
):
    parent = await _mark(0, 10, "explicit", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        parent.dirty_range_id, "worker"
    )
    request = _context_request(leased)

    parked = await dirty_ranges.park_for_context(leased, request)
    job = await DeviceInputJob.find_one(
        DeviceInputJob.context_request_id == request.context_request_id
    )
    assert parked.state == "awaiting_context"
    assert job is not None

    context_pending = await dirty_ranges.mark_evidence_dirty(
        "user",
        request.started_at,
        request.ended_at,
        "device-result-one",
        "timeline_context_acquired",
        source_kind="device_input",
        context_request_id=request.context_request_id,
    )
    assert context_pending.state == "context_pending"
    assert context_pending.dispatch_authorized_at is None
    assert await dirty_ranges.lease_due_range("other-worker") is None

    job.status = "complete"
    job.payload = {**job.payload, "result_evidence_ids": ["observation:item-one"]}
    job.completed_at = datetime.now(timezone.utc)
    await job.save()
    successor = await dirty_ranges.notify_context_job_terminal(
        context_request_id=request.context_request_id,
        job_id=str(job.id),
        result_evidence_ids=["observation:item-one"],
    )

    reloaded_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == parent.dirty_range_id
    )
    assert reloaded_parent.state == "superseded"
    assert reloaded_parent.superseded_by_dirty_range_id == successor.dirty_range_id
    assert successor.state == "authorized_pending"
    assert successor.parent_dirty_range_id == parent.dirty_range_id
    assert successor.reconciliation_request_id == parent.reconciliation_request_id
    assert _as_utc(successor.started_at) == request.started_at
    assert _as_utc(successor.ended_at) == request.ended_at
    assert successor.context_requests[0].result_evidence_ids == ["observation:item-one"]


@pytest.mark.asyncio
async def test_context_request_cannot_widen_authority_across_a_distant_gap(
    dirty_range_documents,
):
    parent = await _mark(0, 10, "explicit", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        parent.dirty_range_id, "worker"
    )
    request = _context_request(leased).model_copy(
        update={
            "started_at": START + timedelta(days=365),
            "ended_at": START + timedelta(days=365, minutes=10),
        }
    )
    request = dirty_ranges.bind_context_request(leased, request)

    with pytest.raises(ValueError, match="overlap or adjoin"):
        await dirty_ranges.park_for_context(leased, request)


@pytest.mark.asyncio
async def test_context_recovery_authorizes_a_successor_stranded_by_old_save_order(
    dirty_range_documents,
):
    parent = await _mark(0, 10, "explicit", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        parent.dirty_range_id, "worker"
    )
    request = _context_request(leased)
    await dirty_ranges.park_for_context(leased, request)
    successor = await dirty_ranges.mark_evidence_dirty(
        "user",
        request.started_at,
        request.ended_at,
        "device-result-one",
        "timeline_context_acquired",
        source_kind="device_input",
        context_request_id=request.context_request_id,
    )
    parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == parent.dirty_range_id
    )
    parent.state = "superseded"
    parent.superseded_by_dirty_range_id = successor.dirty_range_id
    parent.context_requests[0].status = "superseded"
    await parent.save()

    assert successor.state == "context_pending"
    assert await dirty_ranges.recover_context_requests() == 1
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == successor.dirty_range_id
    )
    assert successor.state == "authorized_pending"
    assert successor.dispatch_authorized_at is not None
    assert successor.reconciliation_request_id == parent.reconciliation_request_id


@pytest.mark.asyncio
async def test_context_recovery_repairs_a_missed_terminal_callback(
    dirty_range_documents,
):
    parent = await _mark(0, 10, "explicit", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        parent.dirty_range_id, "worker"
    )
    request = _context_request(leased)
    await dirty_ranges.park_for_context(leased, request)
    job = await DeviceInputJob.find_one(
        DeviceInputJob.context_request_id == request.context_request_id
    )
    job.status = "complete"
    job.payload = {**job.payload, "result_evidence_ids": []}
    job.completed_at = datetime.now(timezone.utc)
    await job.save()

    assert await dirty_ranges.recover_context_requests() == 1
    reloaded_parent = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == parent.dirty_range_id
    )
    successor = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id
        == reloaded_parent.superseded_by_dirty_range_id
    )
    assert reloaded_parent.state == "superseded"
    assert successor.state == "authorized_pending"
    assert successor.evidence_revision >= parent.evidence_revision
    assert await dirty_ranges.recover_context_requests() == 0


@pytest.mark.asyncio
async def test_dismiss_failed_range_preserves_rejection_history_and_diagnostics(
    dirty_range_documents,
):
    row = await _mark(0, 10, "explicit", authorized=True)
    rejection = TimelineInterpretationRejectionState(
        hypothesis_id="hypothesis-1",
        reason_code="insufficient_context",
        explanation="The evidence never resolves the boundary.",
        implicated_evidence_ids=["observation:1"],
        retry_depth=3,
        successor_dirty_range_id=row.dirty_range_id,
        status="exhausted",
    )
    row.state = "failed"
    row.last_error = "exhausted interpretation retries"
    row.interpretation_rejections = [rejection]
    row.rejection_hypothesis_id = rejection.hypothesis_id
    row.rejection_reason_code = rejection.reason_code
    row.rejection_evidence_ids = list(rejection.implicated_evidence_ids)
    await row.save()

    dismissed = await dirty_ranges.dismiss_failed_range(
        row.dirty_range_id,
        user_id="user",
        reason="I reviewed the source evidence and accept this unresolved interval.",
    )

    assert dismissed.state == "dismissed"
    assert dismissed.last_error == "exhausted interpretation retries"
    assert len(dismissed.interpretation_rejections) == 1
    preserved = dismissed.interpretation_rejections[0]
    assert preserved.hypothesis_id == rejection.hypothesis_id
    assert preserved.reason_code == rejection.reason_code
    assert preserved.explanation == rejection.explanation
    assert preserved.implicated_evidence_ids == rejection.implicated_evidence_ids
    assert dismissed.rejection_hypothesis_id == "hypothesis-1"
    assert dismissed.resolution_history[0].action == "dismissed"
    assert dismissed.resolution_history[0].actor_user_id == "user"


@pytest.mark.asyncio
async def test_dismiss_failed_range_is_owner_scoped(dirty_range_documents):
    row = await _mark(0, 10, "explicit", authorized=True)
    row.state = "failed"
    await row.save()

    with pytest.raises(LookupError, match="not found"):
        await dirty_ranges.dismiss_failed_range(
            row.dirty_range_id, user_id="other-user", reason="Dismiss"
        )

    stored = await DirtyEvidenceRange.get(row.id)
    assert stored.state == "failed"
    assert stored.resolution_history == []


@pytest.mark.asyncio
async def test_dismiss_failed_range_rejects_nonfailed_state(dirty_range_documents):
    row = await _mark(0, 10, "explicit", authorized=True)

    with pytest.raises(
        dirty_ranges.DirtyRangeDismissalError, match="state=authorized_pending"
    ):
        await dirty_ranges.dismiss_failed_range(
            row.dirty_range_id, user_id="user", reason="Dismiss"
        )

    stored = await DirtyEvidenceRange.get(row.id)
    assert stored.state == "authorized_pending"
    assert stored.resolution_history == []


@pytest.mark.asyncio
async def test_dismiss_failed_range_rejects_blank_reason_and_repeat_dismissal(
    dirty_range_documents,
):
    row = await _mark(0, 10, "explicit", authorized=True)
    row.state = "failed"
    await row.save()

    with pytest.raises(
        dirty_ranges.DirtyRangeDismissalError, match="reason is required"
    ):
        await dirty_ranges.dismiss_failed_range(
            row.dirty_range_id, user_id="user", reason="   "
        )

    dismissed = await dirty_ranges.dismiss_failed_range(
        row.dirty_range_id, user_id="user", reason="Reviewed and accepted"
    )
    with pytest.raises(dirty_ranges.DirtyRangeDismissalError, match="state=dismissed"):
        await dirty_ranges.dismiss_failed_range(
            row.dirty_range_id, user_id="user", reason="Try again"
        )

    stored = await DirtyEvidenceRange.get(row.id)
    assert stored.state == "dismissed"
    assert len(stored.resolution_history) == 1
    assert stored.resolution_history == dismissed.resolution_history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset, duration, expected",
    [
        (-60, 180, [(-60, 0), (60, 120)]),
        (-60, 90, [(-60, 0)]),
        (30, 90, [(60, 120)]),
        (10, 20, []),
    ],
)
async def test_completion_subtracts_covered_pending_time(
    dirty_range_documents, offset, duration, expected
):
    pending = await _mark(offset, duration, "transcript")
    authorized = await _mark(0, 60, "day", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        authorized.dirty_range_id, "worker"
    )
    await dirty_ranges.complete_range(leased)
    rows = (
        await DirtyEvidenceRange.find({"state": "pending"})
        .sort("+started_at")
        .to_list()
    )
    assert [(_as_utc(r.started_at), _as_utc(r.ended_at)) for r in rows] == [
        (START + timedelta(minutes=a), START + timedelta(minutes=b))
        for a, b in expected
    ]
    assert all(r.evidence_revision == pending.evidence_revision for r in rows)
    assert all(r.dispatch_authorized_at is None for r in rows)
    assert all(r.source_revisions == pending.source_revisions for r in rows)
    # Completion recovery is idempotent; it must not recreate already split tails.
    await dirty_ranges.resolve_completed_pending_ranges(
        authorized.dirty_range_id, user_id="user"
    )
    assert len(await DirtyEvidenceRange.find({"state": "pending"}).to_list()) == len(
        expected
    )


@pytest.mark.asyncio
async def test_completion_preserves_newer_overlapping_pending(dirty_range_documents):
    await _mark(-60, 180, "old")
    authorized = await _mark(0, 60, "day", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        authorized.dirty_range_id, "worker"
    )
    newer = await _mark(10, 5, "new")
    await dirty_ranges.complete_range(leased)
    stored = await DirtyEvidenceRange.find_one({"dirty_range_id": newer.dirty_range_id})
    assert stored.state == "pending"
    assert _as_utc(stored.started_at) == START - timedelta(minutes=60)
    assert _as_utc(stored.ended_at) == START + timedelta(minutes=120)


@pytest.mark.asyncio
async def test_registered_range_job_recovers_pending_split(
    dirty_range_documents, monkeypatch
):
    await _mark(-60, 180, "transcript")
    authorized = await _mark(0, 60, "day", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        authorized.dirty_range_id, "worker"
    )
    collection = DirtyEvidenceRange.get_pymongo_collection()
    await collection.update_one(
        {"dirty_range_id": leased.dirty_range_id},
        {"$set": {"state": "completed", "lease_owner": None, "lease_expires_at": None}},
    )

    async def no_inference(*args, **kwargs):
        raise AssertionError("completed publication must not rerun inference")

    monkeypatch.setattr(timeline_jobs, "reconcile_range", no_inference)
    result = await timeline_jobs.reconcile_range_job.__wrapped__(leased.dirty_range_id)
    assert result["state"] == "completed"
    assert (
        await DirtyEvidenceRange.find(
            {
                "state": "pending",
                "started_at": {"$lt": START + timedelta(minutes=60)},
                "ended_at": {"$gt": START},
            }
        ).count()
        == 0
    )
    assert await DirtyEvidenceRange.find({"state": "pending"}).count() == 2


@pytest.mark.asyncio
async def test_interrupted_pending_split_preserves_coverage_and_recovers(
    dirty_range_documents, monkeypatch
):
    parent = await _mark(-60, 180, "transcript")
    authorized = await _mark(0, 60, "day", authorized=True)
    leased = await dirty_ranges.lease_authorized_range_by_id(
        authorized.dirty_range_id, "worker"
    )
    collection = DirtyEvidenceRange.get_pymongo_collection()
    update = collection.update_one

    async def interrupt_parent(query, *args, **kwargs):
        if query.get("dirty_range_id") == parent.dirty_range_id:
            raise RuntimeError("interrupted split")
        return await update(query, *args, **kwargs)

    monkeypatch.setattr(collection, "update_one", interrupt_parent)
    with pytest.raises(RuntimeError, match="interrupted split"):
        await dirty_ranges.complete_range(leased)
    original = await DirtyEvidenceRange.find_one(
        {"dirty_range_id": parent.dirty_range_id}
    )
    assert original.state == "pending"
    assert await DirtyEvidenceRange.find({"state": "pending"}).count() == 3
    monkeypatch.setattr(collection, "update_one", update)
    await dirty_ranges.resolve_completed_pending_ranges(
        leased.dirty_range_id, user_id="user"
    )
    assert await DirtyEvidenceRange.find({"state": "pending"}).count() == 2
