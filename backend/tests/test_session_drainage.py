"""Tests for how a streaming session decides its work has settled.

The predicate behind ``get_streaming_status`` used to ask RQ "what work is
outstanding for this device?" — a question RQ has no index for, so it degenerated
into a scan of all job history, per session, per poll, synchronously on the event
loop. These tests pin the three properties of the replacement:

1. Drainage is *monotonic* and recorded, so a settled session is never re-derived.
2. Work is attributed to a *session*, not to a device that outlives its sessions.
3. A hash that never became a session is not reported as a live recording.
"""

import json
import time
from types import SimpleNamespace

import pytest
from fakeredis import aioredis as fake_aioredis

from backend.controllers import session_controller
from backend.controllers.queue_controller import PendingWork
from backend.controllers.session_controller import (
    SETTLED_SESSION_RETENTION_SECONDS,
    _is_uninitialized,
    _jobs_drained,
    _newest_session_per_client,
    get_streaming_status,
)
from backend.routers.modules.queue_routes import summarize_job_result
from backend.services.audio_stream.session_store import SessionStatus
from backend.services.audio_stream.session_store import (
    SessionStore as ProductionSessionStore,
)
from backend.services.audio_stream.session_store import SessionView

pytestmark = pytest.mark.unit


class SessionStore(ProductionSessionStore):
    """Ambient provenance fixture for drainage tests."""

    async def init_session(self, session_id: str, **kwargs) -> None:
        kwargs.update(
            capture_epoch=0,
            processing_profile="ambient",
            effects={
                "aec": {"reporting": "unreported"},
                "noise_suppression": {"reporting": "unreported"},
            },
            voice_session_id=None,
        )
        await super().init_session(session_id, **kwargs)


def _view(session_id, **kw):
    return SessionView(session_id=session_id, **kw)


def _nothing_pending():
    return PendingWork(frozenset(), frozenset())


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def test_session_with_its_own_pending_job_is_not_drained():
    view = _view("s1", client_id="dev-phone", started_at=100.0)
    pending = PendingWork(frozenset({"s1"}), frozenset({"dev-phone"}))

    assert _jobs_drained(view, pending, {"dev-phone": "s1"}) is False


def test_pending_job_does_not_pin_open_an_earlier_session_on_the_same_device():
    """The bug the device-keyed predicate had.

    A device outlives its sessions, so work belonging to the recording happening
    now was making every earlier session on that device read as unsettled — which
    for a phone with 14 sessions meant 13 finished ones could not settle until the
    14th did.
    """
    older = _view("s1", client_id="dev-phone", started_at=100.0)
    newest = _view("s2", client_id="dev-phone", started_at=200.0)
    # A chain job that knows only its device — the post-conversation chain has no
    # session_id to stamp.
    pending = PendingWork(frozenset(), frozenset({"dev-phone"}))
    newest_by_client = _newest_session_per_client([older, newest])

    assert _jobs_drained(older, pending, newest_by_client) is True
    assert _jobs_drained(newest, pending, newest_by_client) is False


def test_session_with_no_pending_work_is_drained():
    view = _view("s1", client_id="dev-phone", started_at=100.0)

    assert _jobs_drained(view, _nothing_pending(), {"dev-phone": "s1"}) is True


def test_recorded_drainage_wins_over_a_live_scan():
    """Monotonic: a settled session stays settled and is never re-derived."""
    view = _view("s1", client_id="dev-phone", started_at=100.0, jobs_drained_at=1.0)
    pending = PendingWork(frozenset({"s1"}), frozenset({"dev-phone"}))

    assert _jobs_drained(view, pending, {"dev-phone": "s1"}) is True


def test_newest_session_per_client_ignores_sessions_with_no_device():
    views = [
        _view("s1", client_id="dev-a", started_at=10.0),
        _view("s2", client_id="dev-a", started_at=30.0),
        _view("s3", client_id="dev-b", started_at=20.0),
        _view("s4", started_at=99.0),
    ]

    assert _newest_session_per_client(views) == {"dev-a": "s2", "dev-b": "s3"}


# --------------------------------------------------------------------------- #
# Uninitialized hashes
# --------------------------------------------------------------------------- #


def test_hash_that_never_became_a_session_is_recognised():
    assert _is_uninitialized(_view("durability-probe-abc")) is True


def test_real_session_is_not_mistaken_for_an_uninitialized_hash():
    assert _is_uninitialized(_view("s1", client_id="dev-phone")) is False
    assert _is_uninitialized(_view("s2", status=SessionStatus.ACTIVE)) is False
    assert _is_uninitialized(_view("s3", started_at=1.0)) is False


# --------------------------------------------------------------------------- #
# Durable flag
# --------------------------------------------------------------------------- #


async def test_mark_jobs_drained_persists_and_applies_retention():
    redis = fake_aioredis.FakeRedis()
    store = SessionStore(redis)
    await store.init_session(
        "s1", user_id="u1", client_id="dev-phone", stream_name="audio:stream:s1"
    )

    await store.mark_jobs_drained("s1", retention=SETTLED_SESSION_RETENTION_SECONDS)

    view = await store.read("s1")
    assert view.jobs_drained_at is not None
    ttl = await redis.ttl("audio:session:s1")
    assert 0 < ttl <= SETTLED_SESSION_RETENTION_SECONDS


async def test_mark_jobs_drained_keeps_the_first_observation():
    redis = fake_aioredis.FakeRedis()
    store = SessionStore(redis)
    await store.init_session(
        "s1", user_id="u1", client_id="dev-phone", stream_name="audio:stream:s1"
    )

    await store.mark_jobs_drained("s1", retention=60)
    first = (await store.read("s1")).jobs_drained_at
    time.sleep(0.01)
    await store.mark_jobs_drained("s1", retention=60)

    assert (await store.read("s1")).jobs_drained_at == first


# --------------------------------------------------------------------------- #
# The real endpoint
# --------------------------------------------------------------------------- #


class _FakeQueue:
    """Only the registry-size reads the response's `rq_queues` block makes."""

    count = 0
    started_job_registry: list = []
    finished_job_registry: list = []
    failed_job_registry: list = []
    canceled_job_registry: list = []
    deferred_job_registry: list = []


def _body(response):
    """The handler returns a plain dict on success and JSONResponse on error."""
    if isinstance(response, dict):
        return response
    return json.loads(response.body)


class _FakeRequest:
    def __init__(self, redis):
        self.app = type("app", (), {"state": type("state", (), {})()})()
        self.app.state.redis_audio_stream = redis


@pytest.fixture
def streaming_status_env(monkeypatch):
    """Run the real handler with only the RQ boundary faked."""
    redis = fake_aioredis.FakeRedis()
    for name in ("transcription_queue", "memory_queue", "default_queue"):
        monkeypatch.setattr(session_controller, name, _FakeQueue())
    calls = []

    def _record(pending):
        def _fake():
            calls.append(1)
            return pending

        return _fake

    return redis, monkeypatch, calls, _record


async def test_finished_session_settles_and_the_answer_is_recorded(
    streaming_status_env,
):
    redis, monkeypatch, calls, record = streaming_status_env
    store = SessionStore(redis)
    await store.init_session(
        "s1", user_id="u1", client_id="dev-phone", stream_name="audio:stream:s1"
    )
    await store.mark_complete("s1", "websocket_disconnect")
    monkeypatch.setattr(
        session_controller, "pending_work_owners", record(_nothing_pending())
    )

    body = _body(
        await get_streaming_status(
            _FakeRequest(redis),
            SimpleNamespace(user_id="test-admin", is_superuser=True),
        )
    )

    assert [s["session_id"] for s in body["completed_sessions"]] == ["s1"]
    assert body["active_sessions"] == []
    # The terminal fact is now on the hash, not re-derived next time.
    assert (await store.read("s1")).jobs_drained_at is not None
    assert len(calls) == 1


async def test_settled_sessions_are_never_rescanned(streaming_status_env):
    redis, monkeypatch, calls, record = streaming_status_env
    store = SessionStore(redis)
    await store.init_session(
        "s1", user_id="u1", client_id="dev-phone", stream_name="audio:stream:s1"
    )
    await store.mark_complete("s1", "websocket_disconnect")
    monkeypatch.setattr(
        session_controller, "pending_work_owners", record(_nothing_pending())
    )

    await get_streaming_status(
        _FakeRequest(redis), SimpleNamespace(user_id="test-admin", is_superuser=True)
    )
    await get_streaming_status(
        _FakeRequest(redis), SimpleNamespace(user_id="test-admin", is_superuser=True)
    )
    await get_streaming_status(
        _FakeRequest(redis), SimpleNamespace(user_id="test-admin", is_superuser=True)
    )

    # One scan, on the poll that settled it. The 50-day-old sessions that used to
    # cost a full job-history scan on every poll now cost nothing.
    assert len(calls) == 1


async def test_uninitialized_hash_is_not_reported_as_a_live_recording(
    streaming_status_env,
):
    redis, monkeypatch, calls, record = streaming_status_env
    await redis.hset("audio:session:durability-probe-abc", "stream_name", "x")
    monkeypatch.setattr(
        session_controller, "pending_work_owners", record(_nothing_pending())
    )

    body = _body(
        await get_streaming_status(
            _FakeRequest(redis),
            SimpleNamespace(user_id="test-admin", is_superuser=True),
        )
    )

    assert body["active_sessions"] == []
    assert body["completed_sessions"] == []


# --------------------------------------------------------------------------- #
# Job-result summarization
# --------------------------------------------------------------------------- #


def test_long_string_reports_its_true_length_not_the_truncated_one():
    transcript = "x" * 53283

    out = summarize_job_result({"transcript": transcript})

    assert out["transcript"]["truncated"] is True
    # A consumer reading `.length` must still get the real answer.
    assert out["transcript"]["length"] == 53283
    assert len(out["transcript"]["preview"]) == 200


def test_lists_keep_their_type_so_join_and_indexing_still_work():
    out = summarize_job_result({"identified_speakers": ["alex", "sam"]})

    assert out["identified_speakers"] == ["alex", "sam"]


def test_oversized_list_is_cut_but_stays_a_list():
    out = summarize_job_result({"words": [{"word": "w"} for _ in range(10223)]})

    assert isinstance(out["words"], list)
    assert len(out["words"]) == 20


def test_small_scalars_pass_through_untouched():
    result = {
        "success": True,
        "memories_created": 6,
        "processing_time_seconds": 193.968,
        "provider": "deepgram",
        "diarization_source": None,
    }

    assert summarize_job_result(result) == result


def test_a_result_of_none_stays_none():
    assert summarize_job_result(None) is None
