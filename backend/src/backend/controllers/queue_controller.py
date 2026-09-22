"""
Queue Controller - RQ queue configuration, management and monitoring.

This module provides:
- Queue setup and configuration
- Job statistics and monitoring
- Queue health checks
- Beanie initialization for workers
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse
from rq import Queue, Retry, Worker
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job, JobStatus
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry

from backend.config_loader import get_service_config
from backend.heartbeat import (
    FLEET_HEALTH_KEY,
    evaluate_fleet_health,
    is_rq_worker_fresh,
)
from backend.models.conversation import Conversation
from backend.redis_factory import create_sync_redis
from backend.redis_keys import dirty_range_enqueue_lock
from backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
    parse_consumer_groups,
)
from backend.services.audio_stream.session_store import SessionStatus, SessionStore
from backend.services.memory.audit import MemoryCause, UpdateStrategy
from backend.services.sse_publisher import publish_sse_event
from backend.services.timeline.dirty_ranges import schedule_conversation_dirty

logger = logging.getLogger(__name__)

# Shared sync Redis connection for RQ (decode_responses=False, as RQ requires).
redis_conn = create_sync_redis()


def _as_allow_failure_dependency(depends_on):
    """Wrap a job (or list of jobs) in a ``Dependency`` with ``allow_failure=True``.

    A plain ``depends_on`` leaves a dependent ``deferred`` forever when an upstream
    job ends up FAILED (e.g. abandoned + retries exhausted). ``allow_failure=True``
    makes RQ *promote* the dependent on upstream failure instead — so the chain
    always drains to the finalizer, which reconciles the real status. Returns
    ``None`` for an empty/all-None input (enqueue with no dependency).
    """
    if depends_on is None:
        return None
    if isinstance(depends_on, Dependency):
        return depends_on
    jobs = list(depends_on) if isinstance(depends_on, (list, tuple)) else [depends_on]
    jobs = [j for j in jobs if j is not None]
    if not jobs:
        return None
    return Dependency(jobs=jobs, allow_failure=True)


def post_conv_enqueue_kwargs(stage: str, meta: dict, depends_on=None) -> dict:
    """Shared enqueue kwargs for every post-conversation chain job.

    Centralises the three things each chain job needs so the three enqueue sites
    (this module, conversation_controller reprocess, enqueue_memory_processing)
    can't drift:

    - ``retry``: bounded immediate re-enqueue, so a transient crash / worker death
      doesn't permanently strand the job (``interval=0`` needs no rq-scheduler).
    - ``on_failure``: emits a visible ``system_event`` + diagnostic breadcrumb when
      the job fails or is abandoned (instead of failing silently).
    - ``meta.failure_stage``: tells that callback which stage this job is.
    - ``depends_on`` wrapped with ``allow_failure=True`` (see helper above).
    """
    # Lazy import: job_callbacks lives in the `workers` package, whose __init__
    # imports back from this module — importing it at module top would create a
    # circular import. By call time (enqueue) all modules are fully loaded.
    from backend.workers.job_callbacks import on_chain_job_failure

    kwargs: dict = {
        "retry": Retry(max=2, interval=0),
        "on_failure": on_chain_job_failure,
        "meta": {**meta, "failure_stage": stage},
    }
    dep = _as_allow_failure_dependency(depends_on)
    if dep is not None:
        kwargs["depends_on"] = dep
    return kwargs


def get_job_status_from_rq(job: Job) -> str:
    """
    Get job status using RQ's native method.

    Uses job.get_status() which is the Redis Queue standard approach.
    Returns RQ's standard status names.

    Returns one of: queued, started, finished, failed, deferred, scheduled, canceled, stopped

    Raises:
        RuntimeError: If job status is unexpected (should never happen with RQ's method)
    """
    rq_status = job.get_status()

    # RQ returns status as JobStatus enum or string
    # Convert to string if it's an enum
    if isinstance(rq_status, JobStatus):
        status_str = rq_status.value
    else:
        status_str = str(rq_status)

    # Validate it's a known RQ status
    valid_statuses = {
        JobStatus.QUEUED.value,
        JobStatus.STARTED.value,
        JobStatus.FINISHED.value,
        JobStatus.FAILED.value,
        JobStatus.DEFERRED.value,
        JobStatus.SCHEDULED.value,
        JobStatus.CANCELED.value,
        JobStatus.STOPPED.value,
    }

    if status_str not in valid_statuses:
        logger.error(
            f"Job {job.id} has unexpected RQ status: {status_str}. "
            f"This indicates RQ library added a new status we don't know about."
        )
        raise RuntimeError(
            f"Job {job.id} has unknown RQ status: {status_str}. "
            f"Please update get_job_status_from_rq() to handle this new status."
        )

    return status_str


# Queue name constants
TRANSCRIPTION_QUEUE = "transcription"
MEMORY_QUEUE = "memory"
AUDIO_QUEUE = "audio"
DEFAULT_QUEUE = "default"
SUMMARY_QUEUE = "summary"

# Centralized list of all queue names
QUEUE_NAMES = [
    DEFAULT_QUEUE,
    TRANSCRIPTION_QUEUE,
    MEMORY_QUEUE,
    SUMMARY_QUEUE,
    AUDIO_QUEUE,
]

# Job retention configuration
JOB_RESULT_TTL = int(os.getenv("RQ_RESULT_TTL", 86400))  # 24 hour default

# Create queues with custom result TTL
transcription_queue = Queue(
    TRANSCRIPTION_QUEUE, connection=redis_conn, default_timeout=86400
)  # 24 hours for streaming jobs
memory_queue = Queue(MEMORY_QUEUE, connection=redis_conn, default_timeout=300)
audio_queue = Queue(
    AUDIO_QUEUE, connection=redis_conn, default_timeout=86400
)  # 24 hours for all-day sessions
default_queue = Queue(DEFAULT_QUEUE, connection=redis_conn, default_timeout=300)
summary_queue = Queue(SUMMARY_QUEUE, connection=redis_conn, default_timeout=300)

TITLE_JOB_TIMEOUT_SECONDS = 30
SHORT_SUMMARY_JOB_TIMEOUT_SECONDS = 60
DETAILED_SUMMARY_JOB_TIMEOUT_SECONDS = 300


def enqueue_summary_job_bundle(
    conversation_id: str,
    *,
    depends_on=None,
    meta: Optional[dict] = None,
    include_memory_context: bool = True,
) -> Dict[str, Job]:
    """Enqueue title, short summary, and detailed summary as one ordered bundle.

    Each output has its own worker execution and timeout. Failure-tolerant
    dependencies ensure every stage gets an attempt even when an earlier stage
    exhausts its retries. Ordering also prevents three whole-document saves from
    racing with one another.

    RQ starts each timeout only after a worker dequeues and begins that specific job;
    time spent queued or deferred behind the preceding bundle stage is not charged.
    """
    # Lazy import avoids the workers package's queue-controller import cycle.
    from backend.workers.conversation_jobs import (
        generate_detailed_summary_job,
        generate_short_summary_job,
        generate_title_job,
    )

    suffix = conversation_id[:12]
    bundle_meta = {
        **(meta or {}),
        "conversation_id": conversation_id,
        "summary_bundle_id": f"summary_bundle_{suffix}",
    }
    title_job = summary_queue.enqueue(
        generate_title_job,
        conversation_id,
        job_timeout=TITLE_JOB_TIMEOUT_SECONDS,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"title_{suffix}",
        description=f"Generate title for conversation {conversation_id[:8]}",
        **post_conv_enqueue_kwargs("title", bundle_meta, depends_on=depends_on),
    )
    short_summary_job = summary_queue.enqueue(
        generate_short_summary_job,
        conversation_id,
        job_timeout=SHORT_SUMMARY_JOB_TIMEOUT_SECONDS,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"short_summary_{suffix}",
        description=f"Generate short summary for conversation {conversation_id[:8]}",
        **post_conv_enqueue_kwargs("short_summary", bundle_meta, depends_on=title_job),
    )
    detailed_summary_job = summary_queue.enqueue(
        generate_detailed_summary_job,
        conversation_id,
        include_memory_context=include_memory_context,
        job_timeout=DETAILED_SUMMARY_JOB_TIMEOUT_SECONDS,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"detailed_summary_{suffix}",
        description=f"Generate detailed summary for conversation {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "detailed_summary", bundle_meta, depends_on=short_summary_job
        ),
    )
    return {
        "title": title_job,
        "short_summary": short_summary_job,
        "detailed_summary": detailed_summary_job,
    }


def get_queue(queue_name: str = DEFAULT_QUEUE) -> Queue:
    """Get an RQ queue by name."""
    queues = {
        TRANSCRIPTION_QUEUE: transcription_queue,
        MEMORY_QUEUE: memory_queue,
        AUDIO_QUEUE: audio_queue,
        SUMMARY_QUEUE: summary_queue,
        DEFAULT_QUEUE: default_queue,
    }
    return queues.get(queue_name, default_queue)


def get_job_stats() -> Dict[str, Any]:
    """Get statistics about jobs in all queues using RQ standard status names."""
    total_jobs = 0
    queued_jobs = 0
    started_jobs = 0  # RQ standard: "started" not "processing"
    finished_jobs = 0  # RQ standard: "finished" not "completed"
    failed_jobs = 0
    canceled_jobs = 0  # RQ standard: "canceled" not "cancelled"
    deferred_jobs = 0  # Jobs waiting for dependencies (depends_on)

    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)

        queued_jobs += len(queue)
        started_jobs += len(queue.started_job_registry)
        finished_jobs += len(queue.finished_job_registry)
        failed_jobs += len(queue.failed_job_registry)
        canceled_jobs += len(queue.canceled_job_registry)
        deferred_jobs += len(queue.deferred_job_registry)

    total_jobs = (
        queued_jobs
        + started_jobs
        + finished_jobs
        + failed_jobs
        + canceled_jobs
        + deferred_jobs
    )

    return {
        "total_jobs": total_jobs,
        "queued_jobs": queued_jobs,
        "started_jobs": started_jobs,
        "finished_jobs": finished_jobs,
        "failed_jobs": failed_jobs,
        "canceled_jobs": canceled_jobs,
        "deferred_jobs": deferred_jobs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def get_jobs(
    limit: int = 20,
    offset: int = 0,
    queue_name: Optional[str] = None,
    job_type: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get jobs from a specific queue or all queues with optional filtering.

    Args:
        limit: Maximum number of jobs to return
        offset: Number of jobs to skip
        queue_name: Specific queue name or None for all queues
        job_type: Filter by job type (matches func_name, e.g., "speech_detection")
        client_id: Filter by client_id in job meta (partial match)

    Returns:
        Dict with jobs list and pagination metadata matching frontend expectations
    """
    logger.info(
        f"🔍 DEBUG get_jobs: Filtering - queue_name={queue_name}, job_type={job_type}, client_id={client_id}"
    )
    all_jobs = []
    seen_job_ids = (
        set()
    )  # Track which job IDs we've already processed to avoid duplicates

    queues_to_check = [queue_name] if queue_name else QUEUE_NAMES
    logger.info(f"🔍 DEBUG get_jobs: Checking queues: {queues_to_check}")

    for qname in queues_to_check:
        queue = get_queue(qname)

        # Collect jobs from all registries (using RQ standard status names)
        registries = [
            (queue.job_ids, "queued"),
            (
                queue.started_job_registry.get_job_ids(),
                "started",
            ),  # RQ standard, not "processing"
            (
                queue.finished_job_registry.get_job_ids(),
                "finished",
            ),  # RQ standard, not "completed"
            (queue.failed_job_registry.get_job_ids(), "failed"),
            (
                queue.deferred_job_registry.get_job_ids(),
                "deferred",
            ),  # Jobs waiting for dependencies
        ]

        for job_ids, status in registries:
            for job_id in job_ids:
                # Skip if we've already processed this job_id (prevents duplicates across registries)
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)

                try:
                    job = Job.fetch(job_id, connection=redis_conn)

                    # Extract user_id from kwargs if present
                    user_id = job.kwargs.get("user_id", "") if job.kwargs else ""

                    # Extract just the function name (e.g., "listen_for_speech_job" from "module.listen_for_speech_job")
                    func_name = (
                        job.func_name.split(".")[-1] if job.func_name else "unknown"
                    )

                    # Debug: Log job details before filtering
                    logger.debug(
                        f"🔍 DEBUG get_jobs: Job {job_id} - func_name={func_name}, full_func_name={job.func_name}, meta_client_id={job.meta.get('client_id', '') if job.meta else ''}, status={status}"
                    )

                    # Apply job_type filter
                    if job_type and job_type not in func_name:
                        logger.debug(
                            f"🔍 DEBUG get_jobs: Filtered out {job_id} - job_type '{job_type}' not in func_name '{func_name}'"
                        )
                        continue

                    # Apply client_id filter (partial match in meta)
                    if client_id:
                        job_client_id = (
                            job.meta.get("client_id", "") if job.meta else ""
                        )
                        if client_id not in job_client_id:
                            logger.debug(
                                f"🔍 DEBUG get_jobs: Filtered out {job_id} - client_id '{client_id}' not in job_client_id '{job_client_id}'"
                            )
                            continue

                    logger.debug(
                        f"🔍 DEBUG get_jobs: Including job {job_id} in results"
                    )

                    all_jobs.append(
                        {
                            "job_id": job.id,
                            "kwargs": job.kwargs or {},
                            "_privacy_payload": {
                                "func_name": job.func_name,
                                "args": job.args,
                                "kwargs": job.kwargs or {},
                                "meta": job.meta or {},
                            },
                            "job_type": func_name,
                            "user_id": user_id,
                            "status": status,
                            "priority": "normal",  # RQ doesn't track priority in metadata
                            "data": {
                                "description": job.description or "",
                                "queue": qname,
                            },
                            "result": job.result if hasattr(job, "result") else None,
                            "meta": (
                                job.meta if job.meta else {}
                            ),  # Include job metadata
                            "error_message": (
                                str(job.exc_info) if job.exc_info else None
                            ),
                            "created_at": (
                                job.created_at.isoformat() if job.created_at else None
                            ),
                            "started_at": (
                                job.started_at.isoformat() if job.started_at else None
                            ),
                            "completed_at": (
                                job.ended_at.isoformat() if job.ended_at else None
                            ),
                            "retry_count": (
                                job.retries_left if hasattr(job, "retries_left") else 0
                            ),
                            "max_retries": 3,  # Default max retries
                            "progress_percent": (job.meta or {})
                            .get("batch_progress", {})
                            .get("percent", 0),
                            "progress_message": (job.meta or {})
                            .get("batch_progress", {})
                            .get("message", ""),
                        }
                    )
                except Exception as e:
                    logger.error(f"Error fetching job {job_id}: {type(e).__name__}")

    # Sort by created_at (most recent first)
    all_jobs.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # Paginate
    total_jobs = len(all_jobs)
    paginated_jobs = all_jobs[offset : offset + limit]
    has_more = (offset + limit) < total_jobs

    logger.info(
        f"🔍 DEBUG get_jobs: Found {total_jobs} matching jobs (returning {len(paginated_jobs)} after pagination)"
    )

    return {
        "jobs": paginated_jobs,
        "pagination": {
            "total": total_jobs,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        },
    }


@dataclass(frozen=True)
class PendingWork:
    """Who owns the non-terminal jobs, as stamped on the jobs themselves.

    ``session_ids`` is the precise answer and ``client_ids`` the fallback for jobs
    that only know their device — see :func:`pending_work_owners`.
    """

    session_ids: frozenset
    client_ids: frozenset


def pending_work_owners() -> PendingWork:
    """Owners of every job that is not yet in a terminal state.

    Two properties matter, and the old per-client scan had neither.

    **It is proportional to work in flight, not to history.** Only the four
    registries that can hold a non-terminal job are read. Finished, failed, and
    canceled are terminal *by definition* — the completeness test is
    ``is_finished or is_failed or is_canceled``, which every member of them
    satisfies — so reading them could only confirm what their membership already
    states. They are also where the jobs are: this deployment held 2,535 of its
    2,536 jobs there, and the previous version re-fetched all of them from Redis
    on every call, once per session.

    **It is asked once.** The registries do not vary by owner, so a caller
    classifying N sessions scans once and does N membership tests, rather than
    running N identical scans.

    Attribution prefers ``meta["session_id"]`` and falls back to
    ``meta["client_id"]``. A job carrying neither — the deferred tail of a
    post-conversation chain, say — inherits from the job it is waiting on, walking
    up ``dependency_ids``. That replaces the old downward ``dependent_ids`` walk,
    which needed the terminal registries as its starting points.
    """
    session_ids: set = set()
    client_ids: set = set()
    all_queues = [transcription_queue, memory_queue, audio_queue, default_queue]

    for queue in all_queues:
        pending_job_ids: list = []
        for job_ids in (
            queue.job_ids,
            queue.started_job_registry.get_job_ids(),
            DeferredJobRegistry(queue=queue).get_job_ids(),
            ScheduledJobRegistry(queue=queue).get_job_ids(),
        ):
            pending_job_ids.extend(job_ids)

        for job_id in pending_job_ids:
            try:
                job = Job.fetch(job_id, connection=redis_conn)
            except Exception as e:
                logger.debug(f"Error checking job {job_id}: {type(e).__name__}")
                continue

            session_id, client_id = _owner_of_job(job)
            if session_id:
                session_ids.add(session_id)
            if client_id:
                client_ids.add(client_id)

    return PendingWork(frozenset(session_ids), frozenset(client_ids))


def _owner_of_job(job, _depth: int = 0) -> tuple:
    """Resolve ``(session_id, client_id)`` for a job, following dependencies up."""
    meta = job.meta or {}
    session_id = meta.get("session_id")
    client_id = meta.get("client_id")
    if session_id or client_id:
        return session_id, client_id
    # A chain is a handful of jobs deep (speakers → memory → title → dispatch);
    # the bound only stops a cycle from becoming an infinite walk.
    if _depth >= 8:
        return None, None
    for dependency_id in job.dependency_ids or []:
        try:
            parent = Job.fetch(dependency_id, connection=redis_conn)
        except Exception as e:
            logger.debug(
                f"Error fetching dependency {dependency_id}: {type(e).__name__}"
            )
            continue
        resolved = _owner_of_job(parent, _depth + 1)
        if resolved[0] or resolved[1]:
            return resolved
    return None, None


# Job statuses that mean a speech-detection job is still live, so re-enqueuing
# would create a duplicate detector for the same session.
_LIVE_JOB_STATUSES = {"queued", "started", "deferred", "scheduled"}


def _job_is_live(job_id: str) -> bool:
    """A started job is live only while its registered worker still owns it."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        status = job.get_status(refresh=True)
        if status == JobStatus.STARTED:
            if not job.worker_name:
                return False
            worker = Worker.find_by_key(
                Worker.redis_worker_namespace_prefix + job.worker_name,
                connection=redis_conn,
            )
            return bool(
                worker is not None
                and worker.death_date is None
                and worker.get_current_job_id() == job.id
            )
        return status in _LIVE_JOB_STATUSES
    except NoSuchJobError:
        return False
    except Exception:
        return False


def _speech_detection_job_is_live(job_id: str) -> bool:
    """True if the given speech-detection job exists and hasn't terminated."""
    return _job_is_live(job_id)


def enqueue_audio_persistence(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    contract_version: int = 1,
) -> str:
    """Single-flight enqueue of the per-session audio-persistence job.

    The persistence job is scoped to one immutable recording session and its
    ``audio:stream:{session_id}`` WAL. Repeated liveness checks for that session
    must not enqueue a second consumer.

    The job id is deterministic per session, so liveness is checked by fetching it
    directly; a short Redis mutex collapses a simultaneous reconnect burst into one
    winner. Returns the live or newly-enqueued job id.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from backend.workers.audio_jobs import audio_streaming_persistence_job

    if contract_version not in {1, 2}:
        raise ValueError(f"unsupported audio contract version {contract_version}")
    job_id = f"audio-persist-v{contract_version}_{session_id}"
    lock_key = f"audio_persistence_enqueue_lock:{session_id}"

    if _job_is_live(job_id):
        logger.info(
            f"⏭️ Audio persistence already live for session {session_id[:12]} "
            f"({job_id}) — skipping duplicate enqueue (reconnect)"
        )
        return job_id

    if not redis_conn.set(lock_key, "1", nx=True, ex=15):
        logger.info(
            f"⏭️ Concurrent audio-persistence enqueue in progress for session "
            f"{session_id[:12]} — skipping"
        )
        return job_id
    try:
        # Re-check liveness under the mutex (another caller may have just enqueued).
        if _job_is_live(job_id):
            return job_id

        audio_job = audio_queue.enqueue(
            audio_streaming_persistence_job,
            session_id,
            user_id,
            client_id,
            contract_version=contract_version,
            job_timeout=86400,  # 24 hours for all-day sessions
            ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
            result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
            failure_ttl=604800,
            retry=Retry(max=1000, interval=[1, 5, 15, 30, 60, 300]),
            job_id=job_id,
            description=f"Audio persistence for session {session_id}",
            # session_id is what makes this job attributable to one recording
            # rather than to the device: a device outlives its sessions, so
            # attributing by client_id alone lets a new session's work keep every
            # earlier session on the same device looking unsettled.
            meta={
                "session_id": session_id,
                "client_id": client_id,
                "session_level": True,
            },
        )
        logger.info(
            f"📥 RQ: Enqueued audio persistence job {audio_job.id} on audio queue "
            f"(session {session_id[:12]})"
        )
        return audio_job.id
    finally:
        redis_conn.delete(lock_key)


def ensure_audio_persistence(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    contract_version: int = 1,
) -> str:
    """Return a verified-live persistence job for an audio session.

    This is the data-integrity boundary used both at session startup and by the
    WebSocket watchdog. Merely returning a deterministic RQ job id is insufficient:
    a prior recording may have left a finished job with that id behind.
    """
    job_id = enqueue_audio_persistence(
        session_id,
        user_id,
        client_id,
        contract_version=contract_version,
    )
    if not _job_is_live(job_id):
        raise RuntimeError(
            f"Audio persistence job {job_id} is not live for session {session_id}"
        )
    return job_id


def enqueue_speech_detection(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    reason: str = "restart",
    replaces_current: bool = False,
) -> Optional[str]:
    """Single-flight enqueue of the per-session speech-detection job.

    At most ONE speech-detection job may be live per session. Re-enqueuing while
    one is already listening (a WebSocket reconnect, or several conversation-end
    handlers firing at once) previously spawned a swarm of duplicate detectors that
    raced over the same capture session. This guards every restart path against that.

    The current live job (if any) is tracked in ``speech_detection_job:{session_id}``;
    a short Redis mutex collapses a simultaneous burst of callers into one winner.

    Args:
        replaces_current: set by the off-mode rotation path, where the caller IS
            the current tracked job and is deliberately handing off to a successor
            (so the liveness check would otherwise see itself and skip).

    Returns the live or newly-enqueued job id, or None if it could not enqueue.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from backend.workers.transcription_jobs import stream_speech_detection_job

    job_key = f"speech_detection_job:{session_id}"
    lock_key = f"speech_detection_enqueue_lock:{session_id}"

    def _tracked_id() -> Optional[str]:
        val = redis_conn.get(job_key)
        if isinstance(val, bytes):
            return val.decode()
        if isinstance(val, str):
            return val
        return None

    if not replaces_current:
        existing_id = _tracked_id()
        if existing_id and _speech_detection_job_is_live(existing_id):
            logger.info(
                f"⏭️ Speech detection already live for session {session_id[:12]} "
                f"({existing_id}) — skipping duplicate enqueue (reason={reason})"
            )
            return existing_id

    # Collapse a concurrent-enqueue burst (several end handlers firing together)
    # into a single winner. The loser skips; the winner records the new job in
    # job_key so any later caller sees it live and also skips.
    if not redis_conn.set(lock_key, "1", nx=True, ex=15):
        logger.info(
            f"⏭️ Concurrent speech-detection enqueue in progress for session "
            f"{session_id[:12]} — skipping (reason={reason})"
        )
        return _tracked_id()
    try:
        if not replaces_current:
            # Re-check liveness under the mutex (another caller may have just enqueued).
            existing_id = _tracked_id()
            if existing_id and _speech_detection_job_is_live(existing_id):
                logger.info(
                    f"⏭️ Speech detection became live for session {session_id[:12]} "
                    f"while acquiring lock — skipping (reason={reason})"
                )
                return existing_id

        speech_job = transcription_queue.enqueue(
            stream_speech_detection_job,
            session_id,
            user_id,
            client_id,
            job_timeout=86400,  # 24 hours for all-day sessions
            ttl=None,  # No pre-run expiry (job can wait indefinitely in queue)
            result_ttl=JOB_RESULT_TTL,  # Cleanup AFTER completion
            failure_ttl=86400,  # Cleanup failed jobs after 24h
            job_id=f"speech-detect_{session_id}_{uuid.uuid4().hex[:8]}",
            description="Listening for speech...",
            meta={
                "session_id": session_id,
                "client_id": client_id,
                "session_level": True,
            },
        )
        # Track the live job for both single-flight and WebSocket cleanup.
        redis_conn.set(job_key, speech_job.id, ex=86400)
        logger.info(
            f"📥 RQ: Enqueued speech detection job {speech_job.id} for session "
            f"{session_id[:12]} (reason={reason})"
        )
        return speech_job.id
    finally:
        redis_conn.delete(lock_key)


def enqueue_dirty_range_reconciliation(dirty_range_id: str) -> Optional[str]:
    """Single-flight enqueue of the rolling reconciliation job for one dirty range.

    The recovery scan runs every few minutes and a range stays ``pending`` until its
    job leases it, so two consecutive scans would otherwise queue the same range
    twice. The short Redis lock collapses that; the job itself is lease-guarded, so a
    duplicate that slips through still cannot run twice.
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from backend.workers.timeline_jobs import reconcile_range_job

    lock_key = dirty_range_enqueue_lock(dirty_range_id)
    if not redis_conn.set(lock_key, "1", nx=True, ex=300):
        return None
    try:
        job = default_queue.enqueue(
            reconcile_range_job,
            dirty_range_id,
            job_timeout=1800,
            result_ttl=JOB_RESULT_TTL,
            failure_ttl=86400,
            job_id=f"reconcile-range_{dirty_range_id}",
            description="Reconciling dirty evidence range",
            meta={"dirty_range_id": dirty_range_id},
        )
        return job.id
    except Exception:
        # Leave nothing latched: the next scan must be able to retry immediately.
        redis_conn.delete(lock_key)
        logger.warning(
            "Failed to enqueue reconciliation for dirty range %s",
            dirty_range_id,
            exc_info=True,
        )
        return None


def enqueue_notification_dispatch(notification_id: str) -> Optional[str]:
    """Single-flight enqueue for one durable notification intent."""

    # Lazy because worker modules import the shared job/bootstrap layer.
    from backend.workers.notification_jobs import dispatch_notification_job

    job_id = f"notification-dispatch_{notification_id}"
    try:
        existing = Job.fetch(job_id, connection=redis_conn)
        if existing.ended_at is None and get_job_status_from_rq(existing) in {
            "queued",
            "started",
            "deferred",
            "scheduled",
        }:
            return existing.id
        existing.delete()
    except NoSuchJobError:
        pass
    job = default_queue.enqueue(
        dispatch_notification_job,
        notification_id,
        job_timeout=120,
        result_ttl=JOB_RESULT_TTL,
        failure_ttl=86400,
        retry=Retry(max=5, interval=[10, 30, 120, 300, 900]),
        job_id=job_id,
        description="Sending push notification",
        meta={"notification_id": notification_id},
    )
    return job.id


def enqueue_notification_receipts() -> Optional[str]:
    """Single-flight provider-receipt reconciliation job."""

    # Lazy because worker modules import the shared job/bootstrap layer.
    from backend.workers.notification_jobs import check_notification_receipts_job

    job_id = "notification-receipts"
    try:
        existing = Job.fetch(job_id, connection=redis_conn)
        if existing.ended_at is None and get_job_status_from_rq(existing) in {
            "queued",
            "started",
            "deferred",
            "scheduled",
        }:
            return existing.id
        existing.delete()
    except NoSuchJobError:
        pass
    job = default_queue.enqueue(
        check_notification_receipts_job,
        job_timeout=120,
        result_ttl=JOB_RESULT_TTL,
        failure_ttl=86400,
        retry=Retry(max=5, interval=[30, 120, 300, 900, 1800]),
        job_id=job_id,
        description="Checking push provider receipts",
    )
    return job.id


def enqueue_explicit_timeline_reconciliation(request_id: str) -> Optional[str]:
    """Queue the sole authorized Timeline write entrypoint for one local day."""

    # Lazy because worker modules import the shared job/bootstrap layer.
    from backend.workers.timeline_jobs import explicit_reconciliation_job

    job_id = f"timeline-explicit_{request_id}"
    try:
        existing = Job.fetch(job_id, connection=redis_conn)
        # RQ retains ended_at from the preceding attempt while a retry is
        # scheduled or running. Status, not that old timestamp, owns single-flight.
        status = get_job_status_from_rq(existing)
        if status in {"queued", "started", "deferred", "scheduled"}:
            return existing.id
        existing.delete()
    except NoSuchJobError:
        pass
    job = default_queue.enqueue(
        explicit_reconciliation_job,
        request_id,
        # One bounded Immich vision pass (up to 10 minutes) now precedes the existing
        # Timeline pass (up to 30 minutes). Keep one timeout around the authorized
        # end-to-end request rather than spawning an untracked side job.
        job_timeout=2700,
        result_ttl=JOB_RESULT_TTL,
        failure_ttl=86400,
        retry=Retry(max=2, interval=[30, 300]),
        job_id=job_id,
        description="Explicit Immich-gated Timeline reconciliation",
        meta={"reconciliation_request_id": request_id},
    )
    return job.id


def start_streaming_jobs(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    speech_detection_enabled: bool = True,
    contract_version: int = 1,
) -> Dict[str, str]:
    """
    Enqueue jobs for streaming audio session (initial session setup).

    This starts the parallel job processing for a NEW streaming session:
    1. Speech detection job - monitors transcription results for speech
    2. Audio persistence job - writes audio chunks to WAV file (file rotation per conversation)

    Args:
        session_id: Stream/audio session ID
        user_id: User identifier
        client_id: Connected device identifier

    Returns:
        Dict with job IDs: {'speech_detection': job_id, 'audio_persistence': job_id}

    Note:
        - user_email is fetched from the database when needed.
        - raw audio always receives a durable owner before this function is called.
    """
    # Enqueue speech detection job (single-flight: skips if one is already live
    # for this session, e.g. on a WebSocket reconnect mid-session).
    speech_job_id = ""
    if speech_detection_enabled:
        speech_job_id = (
            enqueue_speech_detection(
                session_id, user_id, client_id, reason="session_start"
            )
            or ""
        )

    # Enqueue audio persistence job on dedicated audio queue (single-flight: a
    # reconnect mid-session reuses the live job instead of starting a second
    # consumer that would split the audio stream). This job handles file rotation
    # for multiple conversations and runs for the entire session.
    audio_job_id = ensure_audio_persistence(
        session_id,
        user_id,
        client_id,
        contract_version=contract_version,
    )

    # Notify frontend that streaming jobs are queued
    jobs = ["audio_persistence"]
    if speech_detection_enabled:
        jobs.insert(0, "speech_detection")
    publish_sse_event(
        user_id,
        "jobs.queued",
        {
            "client_id": client_id,
            "session_id": session_id,
            "jobs": jobs,
        },
    )

    return {"speech_detection": speech_job_id, "audio_persistence": audio_job_id}


def _clear_post_conversation_chain(conversation_id: str) -> list:
    """Delete any existing post-conversation chain jobs for a conversation.

    The post-conversation jobs (speaker → memory → summary bundle → event) use
    deterministic job_ids keyed on the conversation. When the chain is
    re-triggered (e.g. a transcript reprocess) while a previous chain is still
    ``deferred``, re-enqueuing the same job_id with a *new* ``depends_on`` makes
    RQ **accumulate** dependencies on the existing deferred job rather than
    replacing it. If one upstream dependency then finishes and is evicted from
    Redis before the others resolve, RQ never promotes the dependents and the
    whole chain stays ``deferred`` forever (orphaned).

    Deleting the stale chain first guarantees each re-enqueue starts fresh with
    a single, correct dependency. Jobs that are currently ``started`` are left
    alone so we never yank work out from under a running worker.

    Returns the list of job_ids that were actually deleted (for logging).
    """
    suffix = conversation_id[:12]
    job_ids = [
        f"speaker_{suffix}",
        f"memory_{suffix}",
        f"title_{suffix}",
        f"short_summary_{suffix}",
        f"detailed_summary_{suffix}",
        f"event_complete_{suffix}",
    ]
    cleared = []
    for job_id in job_ids:
        try:
            job = Job.fetch(job_id, connection=redis_conn)
        except NoSuchJobError:
            continue
        if get_job_status_from_rq(job) == JobStatus.STARTED.value:
            logger.warning(
                f"⚠️  Not clearing post-conversation job {job_id} for "
                f"{conversation_id[:8]}: currently running"
            )
            continue
        try:
            job.delete(remove_from_queue=True)
            cleared.append(job_id)
        except Exception as e:
            logger.error(
                f"Failed to delete stale chain job {job_id}: {type(e).__name__}"
            )
    if cleared:
        logger.info(
            f"🧹 Cleared {len(cleared)} stale post-conversation job(s) for "
            f"{conversation_id[:8]}: {cleared}"
        )
    return cleared


# Statuses that mean a job is still occupying the chain (not yet terminal).
_IN_FLIGHT_JOB_STATUSES = frozenset(
    {
        JobStatus.QUEUED.value,
        JobStatus.STARTED.value,
        JobStatus.DEFERRED.value,
        JobStatus.SCHEDULED.value,
    }
)


def conversation_edit_chain_in_flight(conversation_id: str) -> Optional[str]:
    """Return an in-flight edit-chain job_id for this conversation, else None.

    Several endpoints edit a conversation by creating a new transcript version and
    enqueuing follow-up work under deterministic job_ids keyed on the conversation:

    - ``reprocess_speakers`` → reprocess_speaker → memory → summary bundle
    - annotation apply (``/diarization/{id}/apply``, ``/{id}/apply``) → memory

    Firing any of these again while a previous one is still running spawns overlapping
    work that races on the conversation's full-document ``save()`` — a stale writer can
    clobber a newer version's segments/metadata (e.g. lost speaker labels, an orphaned
    stale ``active`` version). Callers use this as a single-flight guard: if an edit
    chain is already live, don't create a new transcript version or enqueue more work.
    """
    suffix = conversation_id[:12]
    job_ids = [
        f"reprocess_speaker_{suffix}",
        f"memory_{suffix}",
        f"title_{suffix}",
        f"short_summary_{suffix}",
        f"detailed_summary_{suffix}",
    ]
    for job_id in job_ids:
        try:
            job = Job.fetch(job_id, connection=redis_conn)
        except NoSuchJobError:
            continue
        if get_job_status_from_rq(job) in _IN_FLIGHT_JOB_STATUSES:
            return job_id
    return None


def start_post_conversation_jobs(
    conversation_id: str,
    user_id: str,
    transcript_version_id: Optional[str] = None,
    depends_on_job=None,
    client_id: Optional[str] = None,
    end_reason: Optional[str] = None,
    trigger: str = Conversation.ProcessingTrigger.FILE_UPLOAD.value,
    skip_speaker_recognition: bool = False,
    skip_memory_extraction: bool = False,
    skip_title_summary: bool = False,
    memory_cause: MemoryCause = MemoryCause.AUTO_EXTRACTION,
    memory_strategy: UpdateStrategy = UpdateStrategy.FULL,
    memory_space_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Start post-conversation processing jobs after conversation is created.

    This creates the standard processing chain after a conversation is created:
    1. Speaker recognition job - Identifies speakers in audio segments
    2. Memory extraction job - Extracts memories from conversation
    3. Summary bundle - Generates title, short summary, and detailed summary
    4. Event dispatch job - Triggers conversation.complete plugins

    Note: Batch transcription removed - streaming conversations use streaming transcript.
    For file uploads, batch transcription must be enqueued separately before calling this function.

    Args:
        conversation_id: Conversation identifier
        user_id: User identifier
        transcript_version_id: Transcript version ID (auto-generated if None)
        depends_on_job: Optional job dependency for first job (e.g., transcription for file uploads)
        client_id: Client ID for UI tracking
        end_reason: Why the recording ended, a ``Conversation.EndReason`` value, or
            None to leave the stored reason alone. A reprocess passes None: the
            conversation already ended for a real reason and re-running the pipeline
            does not change it.
        trigger: Why the pipeline is running, a ``Conversation.ProcessingTrigger``
        skip_speaker_recognition: Skip the speaker step even when enabled — used
            by split/merge, whose transcripts already carry speaker labels
        skip_memory_extraction: Skip memory extraction even when globally enabled —
            used for annotation/training datasets that should not enter user memory
        skip_title_summary: Skip LLM title/summary generation for callers whose
            conversations are not user-visible recordings
        memory_space_id: Semantic destination of the Conversation. Isolated spaces
            settle memory and terminal events directly because they do not enter the
            Main rolling Timeline before publication.

    The terminal event-dispatch job is never skippable: it owns ``end_reason``,
    ``completed_at``, and the reconciled ``processing_status``, so a conversation whose
    chain omits it never settles.

    Returns:
        Dict with job IDs for speaker_recognition, memory, each summary stage, and event_dispatch
    """
    # Lazy import: circular dependency with the `workers` package (its __init__
    # imports back from this module).
    from backend.services.timeline.dispatch import finalize_conversation_close_job
    from backend.workers.conversation_jobs import (
        dispatch_conversation_complete_event_job,
    )
    from backend.workers.memory_jobs import process_memory_job
    from backend.workers.speaker_jobs import recognise_speakers_job

    # Re-triggering the chain (e.g. transcript reprocess) must not stack new
    # dependencies onto a previously-deferred chain — that orphans it forever.
    # Clear any stale chain jobs first so each enqueue below starts fresh.
    _clear_post_conversation_chain(conversation_id)

    # Rolling reconciliation: a recording closing is a scheduling signal, not an
    # episode boundary. Mark the conversation's absolute audio interval dirty and let
    # the debounce gather the transcript/speaker revisions this chain is about to
    # produce. Best-effort — the recovery scan and later triggers re-open the range.
    schedule_conversation_dirty(conversation_id, "conversation_closed")

    # Raw capture Conversations keep evidence producers such as transcription,
    # speaker recognition, and VAD, but do not receive semantic summaries or memory.
    # Stable Episode revisions own bounded summaries and a reviewed day snapshot owns
    # the single vault proposal. The plugin event waits for Episode settlement.
    defer_to_timeline = not memory_space_id
    if defer_to_timeline:
        skip_memory_extraction = True
        # Capture fragments are evidence, not semantic conversations. Timeline may
        # join several fragments or use only a bounded slice of one.
        skip_title_summary = True
        logger.info(
            "⏭️  Summaries and user-facing dispatch for "
            f"recording {conversation_id[:8]} deferred to Timeline classification"
        )
    if memory_space_id:
        # A space is a deliberate notebook, not an automatic sink. Finish speaker,
        # summary, and terminal settlement now; the user admits zero or more screen
        # frames and starts the memory write from the scoped review endpoint.
        skip_memory_extraction = True
        logger.info(
            "⏸️  Memory-space extraction for conversation %s is awaiting review",
            conversation_id[:8],
        )

    version_id = transcript_version_id or str(uuid.uuid4())

    # Build job metadata (include client_id if provided for UI tracking)
    job_meta = {"conversation_id": conversation_id}
    if client_id:
        job_meta["client_id"] = client_id

    # Check if speaker recognition is enabled
    speaker_config = get_service_config("speaker_recognition")
    speaker_enabled = speaker_config.get(
        "enabled", True
    )  # Default to True for backward compatibility

    # Step 1: Speaker recognition job (conditional - only if enabled)
    speaker_dependency = (
        depends_on_job  # Start with upstream dependency (transcription if file upload)
    )
    speaker_job = None

    if speaker_enabled and skip_speaker_recognition:
        logger.info(
            f"⏭️  Speaker recognition skipped by caller for conversation {conversation_id[:8]}"
        )
        speaker_enabled = False

    if speaker_enabled:
        speaker_job_id = f"speaker_{conversation_id[:12]}"
        logger.info(
            f"🔍 DEBUG: Creating speaker job with job_id={speaker_job_id}, conversation_id={conversation_id[:12]}"
        )

        speaker_job = transcription_queue.enqueue(
            recognise_speakers_job,
            conversation_id,
            version_id,
            job_timeout=1200,  # 20 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=speaker_job_id,
            description=f"Speaker recognition for conversation {conversation_id[:8]}",
            **post_conv_enqueue_kwargs(
                "speaker", job_meta, depends_on=speaker_dependency
            ),
        )
        speaker_dependency = speaker_job  # Chain for next jobs
        if depends_on_job:
            logger.info(
                f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (depends on {depends_on_job.id})"
            )
        else:
            logger.info(
                f"📥 RQ: Enqueued speaker recognition job {speaker_job.id}, meta={speaker_job.meta} (no dependencies, starts immediately)"
            )
    else:
        logger.info(
            f"⏭️  Speaker recognition disabled, skipping speaker job for conversation {conversation_id[:8]}"
        )

    # Step 2: Memory extraction job (conditional - only if enabled)
    # Check if memory extraction is enabled
    memory_config = get_service_config("memory.extraction")
    memory_enabled = memory_config.get(
        "enabled", True
    )  # Default to True for backward compatibility
    if memory_enabled and skip_memory_extraction:
        logger.info(
            f"⏭️  Memory extraction skipped by caller for conversation {conversation_id[:8]}"
        )
        memory_enabled = False

    memory_job = None
    if memory_enabled:
        # Depends on speaker job if it was created, otherwise depends on upstream (transcription or nothing)
        memory_job_id = f"memory_{conversation_id[:12]}"
        logger.info(
            f"🔍 DEBUG: Creating memory job with job_id={memory_job_id}, conversation_id={conversation_id[:12]}"
        )

        # Memory job carries provenance (cause/strategy) on top of the shared meta.
        memory_meta = {
            **job_meta,
            "cause": memory_cause.value,
            "strategy": memory_strategy.value,
        }
        memory_job = memory_queue.enqueue(
            process_memory_job,
            conversation_id,
            job_timeout=900,  # 15 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=memory_job_id,
            description=f"Memory extraction for conversation {conversation_id[:8]}",
            # Either speaker_job or upstream dependency
            **post_conv_enqueue_kwargs(
                "memory", memory_meta, depends_on=speaker_dependency
            ),
        )
        if speaker_job:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on speaker job {speaker_job.id})"
            )
        elif depends_on_job:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (depends on {depends_on_job.id})"
            )
        else:
            logger.info(
                f"📥 RQ: Enqueued memory extraction job {memory_job.id}, meta={memory_job.meta} (no dependencies, starts immediately)"
            )
    else:
        logger.info(
            f"⏭️  Memory extraction disabled, skipping memory job for conversation {conversation_id[:8]}"
        )

    # Step 3: Ordered title/summary bundle. It follows memory both to avoid
    # whole-document save races and to make fresh memory available to the detailed
    # summary. Each output owns its own timeout and retry lifecycle.
    summary_dependency = memory_job if memory_job else speaker_dependency
    summary_jobs = None
    if skip_title_summary:
        logger.info(
            f"⏭️  Title/summary skipped by caller for conversation {conversation_id[:8]}"
        )
    else:
        summary_jobs = enqueue_summary_job_bundle(
            conversation_id,
            depends_on=summary_dependency,
            meta=job_meta,
        )
        upstream = memory_job or speaker_job or depends_on_job
        logger.info(
            f"📥 RQ: Enqueued summary bundle "
            f"{[job.id for job in summary_jobs.values()]} "
            + (
                f"(depends on {upstream.id})"
                if upstream
                else "(no dependencies, starts immediately)"
            )
        )

    # Step 5: Dispatch conversation.complete event (runs last, after the whole chain)
    # This ensures plugins receive the event after all processing is done
    event_job_id = f"event_complete_{conversation_id[:12]}"
    logger.info(
        f"🔍 DEBUG: Creating conversation complete event job with job_id={event_job_id}, conversation_id={conversation_id[:12]}"
    )

    # Depend on the LAST link actually enqueued, not on a fixed pair. Every job here
    # is skippable, and each is already chained behind the one after it, so naming
    # only memory/title left the finalizer dependency-free whenever a caller skipped
    # both — continuous capture does. It then settled the conversation as
    # failed/"transcription" in ~100ms, while the transcription it was meant to wait
    # for was still tens of seconds from writing its version.
    terminal_job = (
        summary_jobs["detailed_summary"]
        if summary_jobs
        else memory_job or speaker_job or depends_on_job
    )
    event_dependencies = [terminal_job] if terminal_job else []

    # Enqueue event dispatch job (may have no dependencies if all jobs were skipped)
    # The terminal job is never skippable: it owns end_reason, completed_at, and the
    # reconciled processing_status. For Timeline capture it is the finalize-only variant,
    # which does that same work and dispatches nothing.
    event_dispatch_job = default_queue.enqueue(
        (
            finalize_conversation_close_job
            if defer_to_timeline
            else dispatch_conversation_complete_event_job
        ),
        conversation_id,
        client_id or "",
        user_id,
        end_reason,
        trigger,
        job_timeout=120,  # 2 minutes
        result_ttl=JOB_RESULT_TTL,
        job_id=event_job_id,
        description=f"Dispatch conversation complete event ({trigger}) for {conversation_id[:8]}",
        # Wait for whichever upstream jobs were enqueued; allow_failure so this
        # finalizer still runs (and reconciles status) even if one failed.
        **post_conv_enqueue_kwargs(
            "event_complete", job_meta, depends_on=event_dependencies or None
        ),
    )

    # Log event dispatch dependencies
    if event_dependencies:
        dep_ids = [job.id for job in event_dependencies]
        logger.info(
            f"📥 RQ: Enqueued conversation complete event job {event_dispatch_job.id}, meta={event_dispatch_job.meta} (depends on {', '.join(dep_ids)})"
        )
    else:
        logger.info(
            f"📥 RQ: Enqueued conversation complete event job {event_dispatch_job.id}, meta={event_dispatch_job.meta} (no dependencies, starts immediately)"
        )

    # Notify frontend that post-conversation pipeline is queued
    publish_sse_event(
        user_id,
        "jobs.queued",
        {
            "conversation_id": conversation_id,
            "client_id": client_id or "",
            "jobs": [
                j
                for j in [
                    "speaker_recognition" if speaker_job else None,
                    "memory_extraction" if memory_job else None,
                    "title" if summary_jobs else None,
                    "short_summary" if summary_jobs else None,
                    "detailed_summary" if summary_jobs else None,
                    "event_dispatch",
                ]
                if j
            ],
        },
    )

    return {
        "speaker_recognition": speaker_job.id if speaker_job else None,
        "memory": memory_job.id if memory_job else None,
        "title": summary_jobs["title"].id if summary_jobs else None,
        "short_summary": summary_jobs["short_summary"].id if summary_jobs else None,
        "detailed_summary": (
            summary_jobs["detailed_summary"].id if summary_jobs else None
        ),
        "event_dispatch": event_dispatch_job.id,
    }


def get_queue_health() -> Dict[str, Any]:
    """Get health status of all queues and workers."""
    health = {
        "queues": {},
        "workers": [],
        "redis_connection": "unknown",
        "total_workers": 0,
        "active_workers": 0,
        "idle_workers": 0,
        "worker_fleet": {
            "healthy": False,
            "status": "unknown",
            "detail": "Worker fleet health has not been checked",
        },
    }

    # Check Redis connection
    try:
        redis_conn.ping()
        health["redis_connection"] = "healthy"
        health["worker_fleet"] = evaluate_fleet_health(redis_conn.get(FLEET_HEALTH_KEY))
    except Exception as e:
        health["redis_connection"] = f"unhealthy: {type(e).__name__}"
        return health

    # Check each queue. Registry sizes are read with ZCARD rather than len(registry):
    # RQ's ``BaseRegistry.__len__`` runs ``cleanup()`` first, and StartedJobRegistry's
    # cleanup moves expired jobs to the failed registry inside a UnixSignalDeathPenalty.
    # signal.signal() only works on the main thread, and /health calls this through
    # asyncio.to_thread — so as soon as any job was in the started registry the whole
    # probe raised and the endpoint reported "Redis Connection Failed: signal only works
    # in main thread of the main interpreter". Observing health must not mutate state
    # anyway; reaping expired jobs belongs to the workers.
    for queue_name in QUEUE_NAMES:
        queue = get_queue(queue_name)
        health["queues"][queue_name] = {
            "count": len(queue),
            "failed_count": redis_conn.zcard(queue.failed_job_registry.key),
            "finished_count": redis_conn.zcard(queue.finished_job_registry.key),
            "started_count": redis_conn.zcard(queue.started_job_registry.key),
        }

    # Check workers
    workers = [
        worker
        for worker in Worker.all(connection=redis_conn)
        if is_rq_worker_fresh(worker)
    ]
    health["total_workers"] = len(workers)

    for worker in workers:
        state = worker.get_state()
        current_job = worker.get_current_job_id()

        # Count active vs idle workers
        if current_job or state == "busy":
            health["active_workers"] += 1
        else:
            health["idle_workers"] += 1

        health["workers"].append(
            {
                "name": worker.name,
                "state": state,
                "queues": [q.name for q in worker.queues],
                "current_job": current_job,
            }
        )

    return health
