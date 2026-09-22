"""Session observability checks use real stores, controllers, and registered routes."""

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401
from test_session_drainage import SessionStore, _FakeQueue, _FakeRequest

from backend.controllers import session_controller as controller
from backend.routers.modules import queue_routes, system_routes
from backend.services import privacy

SENTINEL = "Synthetic held session diagnostic"
ADMIN = NS(user_id="review-admin", is_superuser=True)


@pytest.fixture
async def sessions(evidence, monkeypatch):
    redis = FakeRedis()
    store = SessionStore(redis)
    await evidence.db.conversations.insert_one(
        {
            **evidence.row,
            "conversation_id": "ordinary-recording",
            "client_id": "ordinary-device",
        }
    )
    for sid, owner, device in [
        ("held", "evidence-owner", "screenpipe-test"),
        ("allowed", "evidence-owner", "ordinary-device"),
        ("other", "other-owner", "other-device"),
    ]:
        await store.init_session(
            sid, user_id=owner, client_id=device, stream_name="audio:stream:" + sid
        )
        await redis.hset(
            "audio:session:" + sid,
            mapping={
                "started_at": START.timestamp(),
                "last_chunk_at": START.timestamp() + 10,
                "identified_speakers": (
                    SENTINEL if sid == "held" else "Synthetic allowed speaker"
                ),
                "last_event": SENTINEL if sid == "held" else "Synthetic allowed event",
                "completion_reason": (
                    SENTINEL if sid == "held" else "Synthetic allowed reason"
                ),
            },
        )
        await redis.xadd("audio:stream:" + sid, {"audio": SENTINEL})
    original = redis.xinfo_stream
    inspected = []

    async def inspect_stream(name):
        inspected.append(name)
        return await original(name)

    monkeypatch.setattr(redis, "xinfo_stream", inspect_stream)
    monkeypatch.setattr(controller, "time", NS(time=lambda: START.timestamp() + 10))
    monkeypatch.setattr(
        controller,
        "pending_work_owners",
        lambda: controller.PendingWork(frozenset(), frozenset()),
    )
    for name in ["transcription_queue", "memory_queue", "default_queue"]:
        monkeypatch.setattr(controller, name, _FakeQueue())
    monkeypatch.setattr(queue_routes, "create_async_redis", lambda: redis)
    return NS(
        redis=redis, store=store, request=_FakeRequest(redis), inspected=inspected
    )


def held(row):
    assert row["privacy_held"]
    assert (
        row["identified_speakers"]
        == row["last_event"]
        == row["completion_reason"]
        == ""
    )
    assert SENTINEL not in json.dumps(row)
    assert row["client_id"] == "screenpipe-test"
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_registered_status_route_holds_original_owner_and_keeps_operational_state(
    evidence, sessions
):
    result = await system_routes.get_streaming_status(sessions.request, ADMIN)
    rows = {r["session_id"]: r for r in result["active_sessions"]}
    held(rows["held"])
    assert rows["allowed"]["identified_speakers"] == "Synthetic allowed speaker"
    assert SENTINEL not in json.dumps(result)
    assert set(result["active_streams"]) == {
        "audio:stream:held",
        "audio:stream:allowed",
        "audio:stream:other",
    }


@pytest.mark.asyncio
async def test_override_can_allow_session_diagnostics(evidence, sessions):
    await allow(evidence)
    rows = (await controller.get_streaming_status(sessions.request, ADMIN))[
        "active_sessions"
    ]
    assert (
        next(r for r in rows if r["session_id"] == "held")["identified_speakers"]
        == SENTINEL
    )


@pytest.mark.asyncio
async def test_nonadmin_only_sees_own_sessions_and_streams(evidence, sessions):
    user = NS(user_id="other-owner", is_superuser=False)
    result = await controller.get_streaming_status(sessions.request, user)
    assert [r["session_id"] for r in result["active_sessions"]] == ["other"]
    assert sessions.inspected == ["audio:stream:other"]
    result = await queue_routes.get_redis_sessions(100, user)
    assert [r["session_id"] for r in result["sessions"]] == ["other"]


@pytest.mark.asyncio
async def test_current_recording_provenance_can_hold_otherwise_allowed_device(
    evidence, sessions
):
    await sessions.redis.hset(
        "audio:session:allowed", "active_conversation_id", "synthetic-recording"
    )
    result = await controller.get_streaming_status(sessions.request, ADMIN)
    row = next(r for r in result["active_sessions"] if r["session_id"] == "allowed")
    assert row["privacy_held"] and row["identified_speakers"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("user_id", ""), ("started_at", "0"), ("started_at", "nan")]
)
async def test_incomplete_session_identity_holds_diagnostics(
    evidence, sessions, field, value
):
    await sessions.redis.hset("audio:session:held", field, value)
    rows = (await controller.get_streaming_status(sessions.request, ADMIN))[
        "active_sessions"
    ]
    held(next(r for r in rows if r["session_id"] == "held"))
    json.dumps(rows, allow_nan=False)


@pytest.mark.asyncio
async def test_revision_change_during_stream_reads_returns_content_free_http_hold(
    evidence, sessions, monkeypatch
):
    await allow(evidence)
    original = sessions.redis.xinfo_stream

    async def changed(*args, **kwargs):
        await revoke(evidence)
        return await original(*args, **kwargs)

    monkeypatch.setattr(sessions.redis, "xinfo_stream", changed)
    app = FastAPI()
    app.include_router(system_routes.router, prefix="/api")
    app.state.redis_audio_stream = sessions.redis
    app.dependency_overrides[system_routes.current_superuser] = lambda: ADMIN
    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/streaming/status")
    assert response.status_code == 423
    assert set(response.json()) <= {"detail", "error"}


@pytest.mark.asyncio
async def test_session_errors_do_not_log_payload(
    evidence, sessions, monkeypatch, caplog
):
    def failure(_):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(controller, "SessionStore", failure)
    result = await controller.get_streaming_status(sessions.request, ADMIN)
    assert result.status_code == 500
    assert SENTINEL not in result.body.decode() and SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_stream_stats_use_real_redis_shapes_and_omit_message_payloads(
    evidence, sessions, monkeypatch
):
    await sessions.redis.xgroup_create("audio:stream:held", "synthetic-group", id="0")
    await sessions.redis.xreadgroup(
        "synthetic-group", "synthetic-consumer", {"audio:stream:held": ">"}, count=1
    )
    monkeypatch.setattr(
        queue_routes,
        "get_audio_stream_service",
        lambda: NS(redis=sessions.redis, audio_stream_prefix="audio:"),
    )
    result = await queue_routes.get_stream_stats(100, ADMIN)
    assert result["total_streams"] == 3
    assert SENTINEL not in json.dumps(result)
    row = next(r for r in result["streams"] if r["stream_name"] == "audio:stream:held")
    assert row["length"] == 1 and row["groups"][0]["pending"] == 1
    assert row["groups"][0]["consumer_details"][0]["name"] == "synthetic-consumer"
    result = await queue_routes.get_stream_stats(
        100, NS(user_id="other-owner", is_superuser=False)
    )
    assert [r["stream_name"] for r in result["streams"]] == ["audio:stream:other"]


@pytest.mark.asyncio
async def test_queue_dashboard_retains_session_revision_until_final_response(
    evidence, sessions, monkeypatch
):
    await allow(evidence)
    monkeypatch.setattr(queue_routes, "QUEUE_NAMES", [])
    monkeypatch.setattr(queue_routes, "get_plugin_router", lambda: None)
    monkeypatch.setattr(queue_routes, "get_job_stats", lambda: {})
    original = queue_routes.QueuePrivacyFilter.project
    changed = False

    async def project(self, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            await revoke(evidence)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(queue_routes.QueuePrivacyFilter, "project", project)
    with pytest.raises(privacy.PrivacyHeld):
        await queue_routes.get_dashboard_data(sessions.request, "", ADMIN)
