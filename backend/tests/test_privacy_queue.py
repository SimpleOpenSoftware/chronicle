"""Real queue and SSE entry points, using synthetic capture evidence and fake RQ."""

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401

from backend.controllers import queue_controller as controller
from backend.routers.modules import queue_routes as routes
from backend.routers.modules import sse_routes as sse
from backend.services import privacy

SENTINEL = "SYNTHETIC_HELD_PAYLOAD"
ADMIN = NS(user_id="review-admin", is_superuser=True)
WORKER = "backend.workers.transcription_jobs.transcribe_full_audio_job"


def job(identifier="held-job", cid="synthetic-recording", **updates):
    fields = dict(
        id=identifier,
        func_name=WORKER,
        args=(cid,),
        kwargs={"user_id": "evidence-owner"},
        description=SENTINEL,
        result={"text": SENTINEL},
        meta={
            "client_id": "synthetic-client",
            "batch_progress": {"message": SENTINEL, "percent": 25},
        },
        exc_info=SENTINEL,
        created_at=START,
        started_at=START,
        ended_at=START,
        dependent_ids=[],
        retries_left=0,
    )
    fields.update(updates)
    return NS(**fields)


@pytest.fixture
async def queue_setup(evidence, monkeypatch):
    await evidence.db.conversations.insert_one(
        {
            **evidence.row,
            "conversation_id": "ordinary-recording",
            "client_id": "ordinary-device",
        }
    )
    jobs = {
        "held-job": job(),
        "allowed-job": job(
            "allowed-job",
            "ordinary-recording",
            description="Synthetic allowed description",
            result={"text": "Synthetic allowed result"},
            exc_info=None,
            meta={"client_id": "synthetic-client"},
        ),
    }
    empty = NS(get_job_ids=lambda: [])
    queue = NS(
        job_ids=list(jobs),
        started_job_registry=empty,
        finished_job_registry=empty,
        failed_job_registry=empty,
        deferred_job_registry=empty,
    )
    for module in (routes, controller):
        monkeypatch.setattr(module, "QUEUE_NAMES", ["synthetic-queue"])
        monkeypatch.setattr(module, "get_queue", lambda *a: queue)
        monkeypatch.setattr(
            module, "Job", NS(fetch=lambda identifier, **kw: jobs[identifier])
        )
    for registry in [
        "StartedJobRegistry",
        "FinishedJobRegistry",
        "FailedJobRegistry",
        "CanceledJobRegistry",
        "DeferredJobRegistry",
        "ScheduledJobRegistry",
    ]:
        monkeypatch.setattr(routes, registry, lambda **kw: empty)
    monkeypatch.setattr(routes, "get_job_status_from_rq", lambda j: "failed")
    monkeypatch.setattr(routes, "get_job_stats", lambda: {"total_jobs": len(jobs)})
    monkeypatch.setattr(
        routes.session_controller,
        "get_streaming_status",
        AsyncMock(return_value={"active_sessions": []}),
    )
    events = []
    monkeypatch.setattr(
        routes, "get_plugin_router", lambda: NS(get_recent_events=lambda **kw: events)
    )
    return NS(jobs=jobs, queue=queue, events=events)


def assert_held(row):
    assert row["privacy_held"] is True
    assert SENTINEL not in json.dumps(row, default=str)
    assert not any(k.startswith("_privacy_") for k in row)
    for name in [
        "args",
        "kwargs",
        "meta",
        "result",
        "error_message",
        "exc_info",
        "batch_progress",
    ]:
        assert not row.get(name)


async def list_route(user=ADMIN):
    return await routes.list_jobs(
        limit=100,
        offset=0,
        queue_name=None,
        job_type=None,
        client_id=None,
        current_user=user,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["detail", "status", "list", "client", "dashboard"])
async def test_real_queue_routes_hold_canonical_original_owner(
    evidence, queue_setup, entry
):
    if entry in ("detail", "status"):
        fn = routes.get_job if entry == "detail" else routes.get_job_status
        assert_held(await fn("held-job", ADMIN))
        assert not (await fn("allowed-job", ADMIN)).get("privacy_held")
    else:
        if entry == "list":
            rows = (await list_route())["jobs"]
        elif entry == "client":
            rows = (await routes.get_jobs_by_client("synthetic-client", ADMIN))["jobs"]
        else:
            result = await routes.get_dashboard_data(NS(), "synthetic-client", ADMIN)
            rows = result["jobs"]["queued"]
            assert_held(result["client_jobs"]["synthetic-client"][0])
        assert len(rows) == 2
        assert_held(next(r for r in rows if r["job_id"] == "held-job"))
        assert not next(r for r in rows if r["job_id"] == "allowed-job").get(
            "privacy_held"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["status", "client", "dashboard"])
async def test_private_reference_beyond_result_preview_is_still_checked(
    evidence, queue_setup, entry
):
    queue_setup.jobs["allowed-job"].result = [
        {"value": "Synthetic allowed result"}
    ] * 25 + [{"conversation_id": "synthetic-recording", "text": SENTINEL}]
    queue_setup.jobs["allowed-job"].exc_info = SENTINEL
    if entry == "status":
        rows = [await routes.get_job_status("allowed-job", ADMIN)]
    elif entry == "client":
        rows = (await routes.get_jobs_by_client("synthetic-client", ADMIN))["jobs"]
    else:
        rows = (await routes.get_dashboard_data(NS(), "synthetic-client", ADMIN))[
            "jobs"
        ]["queued"]
    assert_held(next(r for r in rows if r["job_id"] == "allowed-job"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"func_name": "backend.workers.unknown", "args": ("/synthetic/file.wav",)},
        {"args": (), "kwargs": {"conversation_id": "missing-recording"}},
        {"args": (), "kwargs": {"note_path": "Synthetic.md"}},
        {
            "args": (),
            "kwargs": {"source_id": "missing-source", "started_at": START.isoformat()},
        },
        {
            "args": (),
            "kwargs": {"capture_source_id": "screenpipe-test", "started_at": "invalid"},
        },
    ],
)
async def test_unknown_or_unverifiable_provenance_cannot_release_payload(
    evidence, queue_setup, payload
):
    queue_setup.jobs["held-job"] = job(**payload)
    assert_held(await routes.get_job("held-job", ADMIN))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["source_id", "capture_source_id", "source_ids"])
async def test_direct_source_uses_original_owner_and_override(
    evidence, queue_setup, key
):
    source = ["screenpipe-test"] if key == "source_ids" else "screenpipe-test"
    queue_setup.jobs["held-job"] = job(
        args=(),
        kwargs={
            "user_id": "review-admin",
            key: source,
            "started_at": evidence.row["created_at"],
            "ended_at": evidence.row["ended_at"],
        },
    )
    assert_held(await routes.get_job("held-job", ADMIN))
    await allow(evidence)
    assert not (await routes.get_job("held-job", ADMIN)).get("privacy_held")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["detail", "status", "list", "client", "dashboard"])
async def test_nonadmin_cannot_observe_another_users_jobs(evidence, queue_setup, entry):
    user = NS(user_id="unrelated-user", is_superuser=False)
    if entry in ("detail", "status"):
        with pytest.raises(HTTPException) as exc:
            await (routes.get_job if entry == "detail" else routes.get_job_status)(
                "held-job", user
            )
        assert exc.value.status_code == 403
    elif entry == "list":
        assert (await list_route(user))["jobs"] == []
    elif entry == "client":
        assert (await routes.get_jobs_by_client("synthetic-client", user))["jobs"] == []
    else:
        result = await routes.get_dashboard_data(NS(), "synthetic-client", user)
        assert not any(result["jobs"].values())
        assert result["client_jobs"]["synthetic-client"] == []


@pytest.mark.asyncio
async def test_revision_change_before_return_discards_prepared_content(
    evidence, queue_setup, monkeypatch
):
    await allow(evidence)
    original = privacy.ConversationPrivacyFilter.assert_current

    async def change(self):
        await revoke(evidence)
        await original(self)

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "assert_current", change)
    with pytest.raises(privacy.PrivacyHeld):
        await routes.get_job("held-job", ADMIN)


@pytest.mark.asyncio
async def test_plugin_event_payloads_and_dashboard_events_share_privacy(
    evidence, queue_setup
):
    queue_setup.events.extend(
        [
            {
                "event": "test.event",
                "metadata": {"conversation_id": "synthetic-recording"},
                "plugins_executed": [{"message": SENTINEL, "data": {"text": SENTINEL}}],
            },
            {
                "event": "test.event",
                "metadata": {"conversation_id": "ordinary-recording"},
                "plugins_executed": [{"message": "Synthetic allowed event"}],
            },
            {
                "event": "test.event",
                "metadata": {},
                "plugins_executed": [{"message": SENTINEL}],
            },
        ]
    )
    for result in [
        await routes.get_events(50, None, ADMIN),
        await routes.get_dashboard_data(NS(), "", ADMIN),
    ]:
        rows = result["events"]
        assert len(rows) == 3
        for index in (0, 2):
            assert_held(rows[index])
            assert rows[index]["plugins_executed"] == []
        assert rows[1]["plugins_executed"][0]["message"] == "Synthetic allowed event"


@pytest.mark.asyncio
async def test_sse_stream_holds_payload_then_continues_and_cleans_up(
    evidence, queue_setup, monkeypatch
):
    messages = [
        {
            "event": "conversation.updated",
            "data": {"conversation_id": "synthetic-recording", "text": SENTINEL},
        },
        {
            "event": "conversation.updated",
            "data": {
                "conversation_id": "ordinary-recording",
                "text": "Synthetic allowed event",
            },
        },
        {"event": "invalid\ndata: injected", "data": {"text": SENTINEL}},
    ]
    pubsub = NS(
        subscribe=AsyncMock(),
        unsubscribe=AsyncMock(),
        aclose=AsyncMock(),
        get_message=AsyncMock(
            side_effect=[{"type": "message", "data": json.dumps(m)} for m in messages]
        ),
    )
    redis = NS(pubsub=lambda: pubsub, aclose=AsyncMock())
    monkeypatch.setattr(sse, "create_async_redis", lambda **kw: redis)
    monkeypatch.setattr(sse, "shutdown_requested", lambda: False)
    monkeypatch.setattr(
        sse, "get_user_from_token_param", AsyncMock(return_value=NS(id="review-admin"))
    )
    response = await sse.event_stream("synthetic-token")
    stream = response.body_iterator
    assert "event: connected" in await anext(stream)
    held = await anext(stream)
    assert SENTINEL not in held and '"privacy_held": true' in held
    allowed = await anext(stream)
    assert "Synthetic allowed event" in allowed
    malformed = await anext(stream)
    assert malformed.startswith("event: message\n") and "injected" not in malformed
    await stream.aclose()
    pubsub.unsubscribe.assert_awaited_once_with("sse:review-admin")
    pubsub.aclose.assert_awaited_once()
    redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_error_does_not_log_or_return_exception_payload(
    evidence, queue_setup, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(routes, "get_job_status_from_rq", fail)
    with pytest.raises(HTTPException) as exc:
        await routes.get_job_status("held-job", ADMIN)
    assert SENTINEL not in exc.value.detail
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_sse_revision_race_holds_only_current_message(
    evidence, queue_setup, monkeypatch
):
    await allow(evidence)
    original = privacy.ConversationPrivacyFilter.assert_current
    calls = 0

    async def change(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            await revoke(evidence)
        await original(self)

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "assert_current", change)
    pubsub = NS(
        subscribe=AsyncMock(),
        unsubscribe=AsyncMock(),
        aclose=AsyncMock(),
        get_message=AsyncMock(
            side_effect=[
                {
                    "type": "message",
                    "data": json.dumps(
                        {
                            "event": "conversation.updated",
                            "data": {"conversation_id": cid, "text": text},
                        }
                    ),
                }
                for cid, text in [
                    ("synthetic-recording", SENTINEL),
                    ("ordinary-recording", "Synthetic next event"),
                ]
            ]
        ),
    )
    redis = NS(pubsub=lambda: pubsub, aclose=AsyncMock())
    monkeypatch.setattr(sse, "create_async_redis", lambda **kw: redis)
    monkeypatch.setattr(sse, "shutdown_requested", lambda: False)
    stream = sse._sse_generator("review-admin")
    await anext(stream)
    result = await anext(stream)
    assert SENTINEL not in result and '"privacy_held": true' in result
    assert "Synthetic next event" in await anext(stream)
    await stream.aclose()
    redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_held_metadata_can_return_during_policy_changes(
    evidence, queue_setup, monkeypatch
):
    monkeypatch.setattr(
        privacy.ConversationPrivacyFilter,
        "assert_current",
        AsyncMock(side_effect=AssertionError("Content-free result needs no receipt")),
    )
    assert_held(await routes.get_job("held-job", ADMIN))


@pytest.mark.asyncio
async def test_authenticated_http_queue_contract_and_content_free_hold(
    evidence, queue_setup, monkeypatch
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.current_active_user] = lambda: ADMIN
    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/queue/jobs/held-job")
        assert response.status_code == 200
        assert_held(response.json())
        assert (await client.get("/api/queue/jobs/allowed-job")).status_code == 200
        await allow(evidence)
        original = privacy.ConversationPrivacyFilter.assert_current

        async def change(self):
            await revoke(evidence)
            await original(self)

        monkeypatch.setattr(privacy.ConversationPrivacyFilter, "assert_current", change)
        response = await client.get("/api/queue/jobs/held-job")
        assert response.status_code == 423
        assert set(response.json()) <= {"detail", "error"}
