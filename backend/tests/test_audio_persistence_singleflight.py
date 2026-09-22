"""Single-flight guarantee for the per-session audio-persistence job.

A WebSocket reconnect mid-session re-runs ``start_streaming_jobs``. The audio
persistence job MUST be single-flight per session: re-enqueuing while one is
already live (i.e. a reconnect) must reuse the live job, never start a second
consumer.

Why this matters (the bug this guards against): two persistence jobs for one
session share the SAME Redis consumer name (``persistence-{session_id[:8]}``),
so the audio stream gets SPLIT between them — each new message goes to only one
consumer. The speech-detected conversations created after the reconnect then
find no audio chunks under their id and get deleted as ``audio_chunks_not_ready``
while their transcripts are stranded on a different conversation document.
"""

import pytest
from fakeredis import FakeStrictRedis
from rq import Queue, Worker
from rq.job import Job, JobStatus

from backend.controllers import queue_controller as module

pytestmark = pytest.mark.unit


@pytest.fixture
def qc(monkeypatch):
    """queue_controller with its Redis + audio queue pointed at fakeredis."""
    fake = FakeStrictRedis()

    monkeypatch.setattr(module, "redis_conn", fake)
    monkeypatch.setattr(module, "audio_queue", Queue("audio", connection=fake))
    return module


def test_reconnect_reuses_live_persistence_job(qc):
    """A second enqueue for the same session (a reconnect) reuses the live job."""
    first = qc.enqueue_audio_persistence("sess-1", "user-1", "sess-1")
    second = qc.enqueue_audio_persistence("sess-1", "user-1", "sess-1")

    assert first == second, "reconnect must reuse the live persistence job id"
    assert qc.audio_queue.count == 1, "must not enqueue a second persistence consumer"


def test_distinct_sessions_get_distinct_jobs(qc):
    """Single-flight is per-session: different sessions each get their own job."""
    a = qc.enqueue_audio_persistence("sess-A", "user-1", "sess-A")
    b = qc.enqueue_audio_persistence("sess-B", "user-1", "sess-B")

    assert a != b
    assert qc.audio_queue.count == 2


@pytest.mark.parametrize("owner_state", ["running", "missing", "dead", "different_job"])
def test_started_job_requires_its_actual_worker_owner(qc, owner_state):
    identifier = qc.enqueue_audio_persistence("sess-1", "user-1", "sess-1")
    job = Job.fetch(identifier, connection=qc.redis_conn)
    worker = Worker([qc.audio_queue], connection=qc.redis_conn, name="test-owner")
    worker.register_birth()
    worker.prepare_job_execution(job)
    if owner_state == "missing":
        qc.redis_conn.delete(worker.key)
    elif owner_state == "dead":
        worker.register_death()
    elif owner_state == "different_job":
        worker.set_current_job_id("other-job")
    assert qc._job_is_live(identifier) is (owner_state == "running")


def test_ended_job_allows_a_fresh_enqueue(qc):
    """After the persistence job terminates, a new session may enqueue again.

    Single-flight must gate on LIVENESS, not merely on the id ever having existed —
    otherwise a clean session end would permanently block the next session.
    """

    first = qc.enqueue_audio_persistence("sess-1", "user-1", "sess-1")
    # Simulate the job terminating (worker finished/abandoned it).
    job = Job.fetch(first, connection=qc.redis_conn)
    job.set_status(JobStatus.FINISHED)

    second = qc.enqueue_audio_persistence("sess-1", "user-1", "sess-1")
    assert qc._job_is_live(
        second
    ), "a fresh persistence job must be live after re-enqueue"


def test_ensure_audio_persistence_rejects_non_live_job(qc, monkeypatch):
    """Startup must fail closed if enqueue returns an id with no live consumer."""
    monkeypatch.setattr(qc, "enqueue_audio_persistence", lambda *args, **kwargs: "dead")
    monkeypatch.setattr(qc, "_job_is_live", lambda job_id: False)

    with pytest.raises(RuntimeError, match="is not live"):
        qc.ensure_audio_persistence("sess-1", "user-1", "sess-1")


def test_annotation_stream_starts_persistence_without_speech_detection(qc, monkeypatch):
    monkeypatch.setattr(
        qc, "ensure_audio_persistence", lambda *args, **kwargs: "persist-1"
    )
    speech_calls = []
    monkeypatch.setattr(
        qc,
        "enqueue_speech_detection",
        lambda *args, **kwargs: speech_calls.append((args, kwargs)),
    )
    published = []
    monkeypatch.setattr(qc, "publish_sse_event", lambda *args: published.append(args))

    result = qc.start_streaming_jobs(
        "sess-1",
        "user-1",
        "client-1",
        speech_detection_enabled=False,
    )

    assert result == {"speech_detection": "", "audio_persistence": "persist-1"}
    assert speech_calls == []
    assert published[0][2]["jobs"] == ["audio_persistence"]
