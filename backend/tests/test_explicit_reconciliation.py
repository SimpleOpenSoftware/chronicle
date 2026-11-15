import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.controllers import queue_controller
from backend.models.timeline import (
    DirtyEvidenceRange,
    ImmichEvidenceSummary,
    ImmichVisualPreparationStatus,
    TimelineReconciliationRequest,
)
from backend.services.immich_discovery import ImmichDayReadiness
from backend.services.timeline import explicit_reconciliation
from backend.services.timeline.immich_visual_evidence import ImmichVisualPreparation
from backend.workers import timeline_jobs


@pytest.fixture
async def requests_db(mongo_service, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_explicit_reconciliation_db"]
    await init_beanie(
        database=database,
        document_models=[TimelineReconciliationRequest, DirtyEvidenceRange],
    )
    await TimelineReconciliationRequest.delete_all()
    monkeypatch.setattr(
        explicit_reconciliation,
        "resolve_immich_user_id",
        lambda: _async_value("user"),
    )

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", unlocked)
    yield
    await client.drop_database("test_explicit_reconciliation_db")
    client.close()


def readiness(*, ready: bool, reason: str, assets=None):
    return ImmichDayReadiness(
        ready=ready,
        reason=reason,
        target_asset_count=len(assets or []),
        latest_asset_local_date=None,
        checked_at=datetime.now(timezone.utc),
        target_assets=assets or [],
    )


def test_explicit_enqueue_replaces_finished_job(monkeypatch):
    deleted = []
    existing = SimpleNamespace(
        id="timeline-explicit_request-one",
        ended_at=datetime.now(timezone.utc),
        delete=lambda: deleted.append(True),
    )
    monkeypatch.setattr(
        queue_controller.Job,
        "fetch",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        queue_controller, "get_job_status_from_rq", lambda _job: "finished"
    )
    enqueued = []

    def enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(queue_controller.default_queue, "enqueue", enqueue)

    job_id = queue_controller.enqueue_explicit_timeline_reconciliation("request-one")

    assert deleted == [True]
    assert job_id == "timeline-explicit_request-one"
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_blocked_request_never_imports_or_queues_work(requests_db, monkeypatch):
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=False, reason="immich_unreachable")
        ),
    )
    imported = queued = 0

    async def import_candidates(*_args):
        nonlocal imported
        imported += 1

    def queue_request(*_args):
        nonlocal queued
        queued += 1

    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", queue_request)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert created is True
    assert request.state == "blocked"
    assert request.reason == "immich_unreachable"
    assert imported == 0
    assert queued == 0


@pytest.mark.asyncio
async def test_unconfigured_immich_queues_any_user_without_import(
    requests_db, monkeypatch
):
    monkeypatch.setattr(explicit_reconciliation, "immich_configured", lambda: False)
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="immich_unconfigured")
        ),
    )
    imported = 0

    async def import_candidates(*_args):
        nonlocal imported
        imported += 1

    async def not_called():
        raise AssertionError("Immich ownership does not apply without Immich")

    async def authorize(**_kwargs):
        return SimpleNamespace(dirty_range_id="range-one")

    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "resolve_immich_user_id", not_called)
    monkeypatch.setattr(explicit_reconciliation, "authorize_explicit_range", authorize)
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda *_args: "job")

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="not-the-admin"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert created is True
    assert request.state == "queued"
    assert request.reason == "immich_unconfigured"
    assert request.job_id == "job"
    assert imported == 0


@pytest.mark.asyncio
async def test_user_can_explicitly_reconcile_without_immich(requests_db, monkeypatch):
    checked = imported = 0

    async def check(*_args):
        nonlocal checked
        checked += 1

    async def import_candidates(*_args):
        nonlocal imported
        imported += 1

    captured = {}

    async def authorize(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        async def save():
            return None

        return SimpleNamespace(dirty_range_id="bypassed-day", save=save)

    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)
    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "authorize_explicit_range", authorize)
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda _id: "job")

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
        skip_immich=True,
    )

    assert created is True
    assert request.state == "queued"
    assert request.reason == "user_bypassed_immich"
    assert request.target_asset_count == 0
    assert request.immich_visual.state == "not_needed"
    assert request.dirty_range_id == "bypassed-day"
    assert request.job_id == "job"
    assert captured["kwargs"]["reconciliation_request_id"] == request.request_id
    assert captured["kwargs"]["started_at"] < captured["kwargs"]["ended_at"]
    assert checked == 0
    assert imported == 0


@pytest.mark.asyncio
async def test_ready_request_imports_before_queueing(requests_db, monkeypatch):
    events = []
    assets = [{"id": "photo"}]
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="assets_on_day", assets=assets)
        ),
    )

    async def import_candidates(user_id, items):
        events.append(("import", user_id, items))
        return 1

    def queue_request(request_id):
        events.append(("queue", request_id))
        return "job-one"

    monkeypatch.setattr(
        explicit_reconciliation, "import_immich_day_candidates", import_candidates
    )
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", queue_request)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert created is True
    assert request.state == "queued"
    assert request.job_id == "job-one"
    assert [event[0] for event in events] == ["import", "queue"]


@pytest.mark.asyncio
async def test_queued_request_is_single_flight(requests_db, monkeypatch):
    existing = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="queued",
        reason="assets_on_day",
    )
    await existing.insert()
    checks = 0

    async def check(*_args):
        nonlocal checks
        checks += 1

    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert request.request_id == existing.request_id
    assert created is False
    assert checks == 0


@pytest.mark.asyncio
async def test_concurrent_requests_are_single_flight(requests_db, monkeypatch):
    lock = asyncio.Lock()
    lock_options = []

    @asynccontextmanager
    async def local_lock(*_args, **kwargs):
        lock_options.append(kwargs)
        async with lock:
            yield

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", local_lock)
    monkeypatch.setattr(
        explicit_reconciliation,
        "import_immich_day_candidates",
        lambda *_args: _async_value(0),
    )
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="later_asset_watermark")
        ),
    )
    queued = []
    monkeypatch.setattr(
        explicit_reconciliation,
        "_queue_request",
        lambda request_id: queued.append(request_id) or "job-one",
    )

    results = await asyncio.gather(
        *(
            explicit_reconciliation.request_explicit_reconciliation(
                user=SimpleNamespace(id="user"),
                local_date=date(2026, 8, 15),
                timezone_name="Asia/Kolkata",
            )
            for _ in range(2)
        )
    )

    assert [created for _request, created in results] == [True, False]
    assert results[0][0].request_id == results[1][0].request_id
    assert len(queued) == 1
    assert (
        lock_options
        == [
            {
                "timeout": explicit_reconciliation.EXPLICIT_REQUEST_LOCK_SECONDS,
                "blocking_timeout": explicit_reconciliation.EXPLICIT_REQUEST_LOCK_SECONDS,
            }
        ]
        * 2
    )


@pytest.mark.asyncio
async def test_concurrent_blocked_requests_reuse_one_readiness_snapshot(
    requests_db, monkeypatch
):
    lock = asyncio.Lock()
    checks = 0

    @asynccontextmanager
    async def local_lock(*_args, **_kwargs):
        async with lock:
            yield

    async def check(*_args):
        nonlocal checks
        checks += 1
        return readiness(ready=False, reason="immich_unreachable")

    monkeypatch.setattr(explicit_reconciliation, "distributed_lock", local_lock)
    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    results = await asyncio.gather(
        *(
            explicit_reconciliation.request_explicit_reconciliation(
                user=SimpleNamespace(id="user"),
                local_date=date(2026, 8, 15),
                timezone_name="Asia/Kolkata",
            )
            for _ in range(2)
        )
    )

    assert checks == 1
    assert len({request.request_id for request, _created in results}) == 1
    assert [created for _request, created in results] == [True, False]


@pytest.mark.asyncio
async def test_non_owner_cannot_query_or_import_immich(requests_db, monkeypatch):
    monkeypatch.setattr(explicit_reconciliation, "immich_configured", lambda: True)
    monkeypatch.setattr(
        explicit_reconciliation,
        "resolve_immich_user_id",
        lambda: _async_value("configured-owner"),
    )
    checked = False

    async def check(*_args):
        nonlocal checked
        checked = True

    monkeypatch.setattr(explicit_reconciliation, "check_immich_day_readiness", check)

    with pytest.raises(PermissionError, match="configured Chronicle owner"):
        await explicit_reconciliation.request_explicit_reconciliation(
            user=SimpleNamespace(id="other-user"),
            local_date=date(2026, 8, 15),
            timezone_name="Asia/Kolkata",
        )

    assert checked is False


@pytest.mark.asyncio
async def test_rolling_authorization_creates_an_exact_noncoalescing_day_range(
    requests_db, monkeypatch
):
    monkeypatch.setattr(
        explicit_reconciliation,
        "check_immich_day_readiness",
        lambda *_args: _async_value(
            readiness(ready=True, reason="later_asset_watermark")
        ),
    )
    monkeypatch.setattr(
        explicit_reconciliation,
        "import_immich_day_candidates",
        lambda *_args: _async_value(0),
    )
    captured = {}

    async def authorize(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        async def save():
            return None

        return SimpleNamespace(dirty_range_id="exact-day", save=save)

    monkeypatch.setattr(explicit_reconciliation, "authorize_explicit_range", authorize)
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda _id: "job")

    request, _ = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 8, 15),
        timezone_name="Asia/Kolkata",
    )

    assert request.dirty_range_id == "exact-day"
    assert captured["kwargs"]["reconciliation_request_id"] == request.request_id
    assert captured["kwargs"]["ended_at"] - captured["kwargs"][
        "started_at"
    ] == timedelta(days=1)


@pytest.mark.asyncio
async def test_real_explicit_worker_entrypoint_updates_durable_status(
    requests_db, monkeypatch
):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="queued",
        reason="assets_on_day",
        target_asset_count=1,
        immich_visual=ImmichVisualPreparationStatus(state="pending"),
    )
    now = datetime.now(timezone.utc)
    dirty_range = DirtyEvidenceRange(
        user_id="user",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        authorized_started_at=now - timedelta(hours=1),
        authorized_ended_at=now,
        evidence_revision=1,
        not_before=now - timedelta(minutes=1),
        force_after=now - timedelta(minutes=1),
        state="authorized_pending",
        dispatch_authorized_at=now,
        reconciliation_request_id=request.request_id,
    )
    await dirty_range.insert()
    request.dirty_range_id = dirty_range.dirty_range_id
    await request.insert()
    events = []
    progress = []

    async def report(stage, message, **kwargs):
        progress.append((stage, kwargs))

    monkeypatch.setattr(timeline_jobs, "report_job_progress", report)

    async def prepare(*_args, **_kwargs):
        events.append("visual")
        return ImmichVisualPreparation("complete", 1, 1, 1, 1, 0, 0)

    async def reconcile(_range, *, accounting):
        events.append("reconcile")
        accounting.append(
            {"immich_evidence": ImmichEvidenceSummary(window_count=1).model_dump()}
        )
        return SimpleNamespace(episode_ids=["episode-one"])

    monkeypatch.setattr(timeline_jobs, "prepare_immich_visual_evidence", prepare)
    monkeypatch.setattr(
        timeline_jobs,
        "lease_authorized_range_by_id",
        lambda *_args: _async_value(dirty_range),
    )
    monkeypatch.setattr(timeline_jobs, "reconcile_range", reconcile)
    monkeypatch.setattr(
        timeline_jobs, "finish_range", lambda *_args: _async_value("completed")
    )

    result = await timeline_jobs.explicit_reconciliation_job.__wrapped__(
        request.request_id
    )
    assert [stage for stage, _ in progress] == [
        "photos",
        "photos",
        "evidence",
        "publication",
    ]
    assert progress[-1][1]["state"] == "completed"
    stored = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )

    assert result["state"] == "completed"
    assert stored.state == "completed"
    assert stored.immich_visual.helpful_count == 1
    assert stored.immich_evidence.window_count == 1
    assert events == ["visual", "reconcile"]


@pytest.mark.asyncio
async def test_real_worker_does_not_reconcile_when_all_visual_analysis_fails(
    requests_db, monkeypatch
):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="queued",
        reason="assets_on_day",
        target_asset_count=1,
        immich_visual=ImmichVisualPreparationStatus(state="pending"),
    )
    await request.insert()
    monkeypatch.setattr(
        timeline_jobs,
        "prepare_immich_visual_evidence",
        lambda *_args, **_kwargs: _async_value(
            ImmichVisualPreparation("failed", 1, 0, 0, 0, 0, 1)
        ),
    )
    timeline_started = False

    async def reconcile(*_args, **_kwargs):
        nonlocal timeline_started
        timeline_started = True

    monkeypatch.setattr(timeline_jobs, "reconcile_range", reconcile)

    with pytest.raises(RuntimeError, match="all selected Immich photos"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    assert stored.state == "failed"
    assert stored.immich_visual.state == "failed"
    assert timeline_started is False


@pytest.mark.asyncio
async def test_recoverable_rolling_failure_stays_queued_and_releases_range(
    requests_db, monkeypatch
):
    now = datetime.now(timezone.utc)
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="queued",
        reason="later_asset_watermark",
    )
    await request.insert()
    dirty_range = DirtyEvidenceRange(
        user_id="user",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        authorized_started_at=now - timedelta(hours=1),
        authorized_ended_at=now,
        evidence_revision=1,
        not_before=now - timedelta(minutes=1),
        force_after=now - timedelta(minutes=1),
        state="authorized_pending",
        dispatch_authorized_at=now,
        reconciliation_request_id=request.request_id,
    )
    await dirty_range.insert()
    request.dirty_range_id = dirty_range.dirty_range_id
    await request.save()

    async def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("invalid TimelineAgentResult JSON")

    monkeypatch.setattr(timeline_jobs, "reconcile_range", fail_reconciliation)

    with pytest.raises(RuntimeError, match="invalid TimelineAgentResult JSON"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored_request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    stored_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range.dirty_range_id
    )
    assert stored_request.state == "queued"
    assert "invalid TimelineAgentResult JSON" in (stored_request.last_error or "")
    assert stored_range.state == "authorized_pending"
    assert stored_range.lease_owner is None
    assert "invalid TimelineAgentResult JSON" in (stored_range.last_error or "")


@pytest.mark.asyncio
async def test_exhausted_rolling_failure_is_terminal(requests_db, monkeypatch):
    now = datetime.now(timezone.utc)
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="queued",
        reason="later_asset_watermark",
    )
    await request.insert()
    dirty_range = DirtyEvidenceRange(
        user_id="user",
        started_at=now - timedelta(hours=1),
        ended_at=now,
        authorized_started_at=now - timedelta(hours=1),
        authorized_ended_at=now,
        evidence_revision=1,
        not_before=now - timedelta(minutes=1),
        force_after=now - timedelta(minutes=1),
        attempts=4,
        state="authorized_pending",
        dispatch_authorized_at=now,
        reconciliation_request_id=request.request_id,
    )
    await dirty_range.insert()
    request.dirty_range_id = dirty_range.dirty_range_id
    await request.save()

    async def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("still invalid")

    monkeypatch.setattr(timeline_jobs, "reconcile_range", fail_reconciliation)

    with pytest.raises(RuntimeError, match="still invalid"):
        await timeline_jobs.explicit_reconciliation_job.__wrapped__(request.request_id)

    stored_request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    stored_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == dirty_range.dirty_range_id
    )
    assert stored_request.state == "failed"
    assert stored_range.state == "failed"
    assert stored_range.attempts == 5


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_ongoing_day_authorization_and_payload_keep_fixed_cutoff(
    requests_db, monkeypatch
):
    checkpoint = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(explicit_reconciliation, "utcnow", lambda: checkpoint)
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda _: "job")
    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=SimpleNamespace(id="user"),
        local_date=date(2026, 9, 5),
        timezone_name="Asia/Kolkata",
        skip_immich=True,
    )
    assert created
    dirty = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == request.dirty_range_id
    )
    assert dirty.ended_at.replace(tzinfo=timezone.utc) == checkpoint
    stored = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request.request_id
    )
    payload = explicit_reconciliation.reconciliation_request_payload(stored)
    for key in ("checked_at", "created_at", "updated_at"):
        assert payload[key].utcoffset() == timedelta(0)
    assert payload["checked_at"] == checkpoint
    monkeypatch.setattr(
        explicit_reconciliation, "utcnow", lambda: checkpoint + timedelta(hours=2)
    )
    assert (
        explicit_reconciliation.reconciliation_request_payload(request)[
            "evidence_cutoff"
        ]
        == checkpoint
    )


@pytest.mark.asyncio
async def test_terminal_request_retry_preserves_original_error(
    requests_db, monkeypatch
):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 9, 4),
        timezone="Asia/Kolkata",
        state="failed",
        reason="assets_on_day",
        last_error="original coverage error",
    )
    await request.insert()

    async def must_not_run(*args):
        raise AssertionError("terminal request must not run")

    monkeypatch.setattr(timeline_jobs, "_run_explicit_reconciliation", must_not_run)
    result = await timeline_jobs.explicit_reconciliation_job.__wrapped__(
        request.request_id
    )
    assert result["state"] == "failed"
    stored = await TimelineReconciliationRequest.get(request.id)
    assert stored.last_error == "original coverage error"


@pytest.mark.parametrize("status", ["queued", "started", "deferred", "scheduled"])
def test_enqueue_keeps_running_retry_with_previous_ended_at(monkeypatch, status):
    existing = SimpleNamespace(id="live-retry", ended_at=datetime.now(timezone.utc))
    monkeypatch.setattr(queue_controller.Job, "fetch", lambda *a, **k: existing)
    monkeypatch.setattr(queue_controller, "get_job_status_from_rq", lambda _: status)
    assert (
        queue_controller.enqueue_explicit_timeline_reconciliation("request")
        == "live-retry"
    )


@pytest.mark.asyncio
async def test_cancel_stops_job_dismisses_range_and_allows_a_new_request(
    requests_db, monkeypatch
):
    stopped = []
    monkeypatch.setattr(explicit_reconciliation, "_queue_request", lambda _id: "job")
    monkeypatch.setattr(explicit_reconciliation, "_stop_job", stopped.append)
    user = SimpleNamespace(id="user")
    day = date(2026, 8, 15)

    first, _ = await explicit_reconciliation.request_explicit_reconciliation(
        user=user, local_date=day, timezone_name="Asia/Kolkata", skip_immich=True
    )
    cancelled = await explicit_reconciliation.cancel_explicit_reconciliation(
        user=user, request_id=first.request_id
    )

    assert cancelled.state == "cancelled"
    assert stopped == ["job"]
    dirty_range = await DirtyEvidenceRange.find_one(
        DirtyEvidenceRange.dirty_range_id == first.dirty_range_id
    )
    assert dirty_range.state == "dismissed"
    assert dirty_range.lease_owner is None
    assert dirty_range.resolution_history[-1].action == "cancelled"
    assert dirty_range.resolution_history[-1].prior_state == "authorized_pending"

    # The cancelled request no longer pins the day: asking again starts fresh.
    second, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=user, local_date=day, timezone_name="Asia/Kolkata", skip_immich=True
    )
    assert created is True
    assert second.request_id != first.request_id

    # The worker entrypoint refuses to run a cancelled request.
    result = await timeline_jobs.explicit_reconciliation_job.__wrapped__(
        first.request_id
    )
    assert result == {"request_id": first.request_id, "state": "cancelled"}


@pytest.mark.asyncio
async def test_finished_request_cannot_be_cancelled(requests_db):
    request = TimelineReconciliationRequest(
        user_id="user",
        local_date=date(2026, 8, 15),
        timezone="Asia/Kolkata",
        state="completed",
        reason="user_bypassed_immich",
    )
    await request.insert()

    with pytest.raises(explicit_reconciliation.ReconciliationNotCancellable):
        await explicit_reconciliation.cancel_explicit_reconciliation(
            user=SimpleNamespace(id="user"), request_id=request.request_id
        )
    with pytest.raises(LookupError):
        await explicit_reconciliation.cancel_explicit_reconciliation(
            user=SimpleNamespace(id="someone-else"), request_id=request.request_id
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [("started", "stopped"), ("queued", "cancelled"), ("scheduled", "cancelled")],
)
def test_stop_job_kills_running_and_cancels_pending(monkeypatch, status, expected):
    calls = []
    job = SimpleNamespace(
        id="timeline-explicit_x", cancel=lambda: calls.append("cancel")
    )
    monkeypatch.setattr(queue_controller.Job, "fetch", lambda *_a, **_k: job)
    monkeypatch.setattr(queue_controller, "get_job_status_from_rq", lambda _j: status)
    monkeypatch.setattr(
        queue_controller,
        "send_stop_job_command",
        lambda _conn, job_id: calls.append(("stop", job_id)),
    )

    assert queue_controller.stop_explicit_timeline_reconciliation(job.id) == expected
    assert calls == ([("stop", job.id)] if status == "started" else ["cancel"])
