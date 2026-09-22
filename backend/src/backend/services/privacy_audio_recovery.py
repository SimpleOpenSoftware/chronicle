"""Resume terminal ScreenPipe privacy holds without replaying captured inputs."""

import asyncio
import logging

from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry

from backend.controllers import queue_controller
from backend.services import privacy

logger = logging.getLogger(__name__)
_TRANSCRIPTION = "backend.workers.transcription_jobs.transcribe_full_audio_job"
_PRIVACY_ERROR = "backend.services.privacy.PrivacyHeld:"


def _failed_privacy_job(identifier, connection):
    job = Job.fetch(identifier, connection=connection)
    if (
        job.get_status(refresh=True) != JobStatus.FAILED
        or job.func_name != _TRANSCRIPTION
        or not job.args
        or not isinstance(job.args[0], str)
    ):
        return None
    # Inspect only the terminal exception, never a caught earlier exception.
    lines = (job.exc_info or "").strip().splitlines()
    return job if lines and lines[-1].startswith(_PRIVACY_ERROR) else None


async def resume_privacy_held_transcriptions():
    """Recover exhausted privacy retries; live RQ handles retain ownership.

    RQ's failed registry is the durable retry ledger. Requeue preserves the job ID
    and its dependent jobs. Admission here avoids futile retries; the worker still
    independently checks permission before external calls and publication.
    """
    stats = {"requeued": 0, "held": 0, "errors": 0}
    db = privacy.database()
    if not await db.capture_sources.find_one(
        {"provider": "screenpipe", "privacy_enabled_from": {"$ne": None}},
        {"_id": 1},
    ):
        return stats
    queue = queue_controller.transcription_queue
    registry = FailedJobRegistry(queue=queue)
    try:
        identifiers = await asyncio.to_thread(registry.get_job_ids, cleanup=False)
    except Exception as error:
        logger.warning(
            "Privacy transcription recovery unavailable: %s", type(error).__name__
        )
        stats["errors"] += 1
        return stats
    for identifier in identifiers:
        try:
            job = await asyncio.to_thread(
                _failed_privacy_job, identifier, queue.connection
            )
            if job is None:
                continue
            row = await db.conversations.find_one(
                {"conversation_id": job.args[0], "external_source_type": "screenpipe"},
                privacy._RECORD_PROJECTION,
            )
            if row is None:
                continue
            snapshot = await privacy.require_record(row)
            await privacy.assert_current(row["user_id"], snapshot)
            # Registry removal is atomic: a concurrent recovery cannot enqueue twice.
            await asyncio.to_thread(registry.requeue, job)
            stats["requeued"] += 1
        except privacy.PrivacyHeld:
            stats["held"] += 1
        except (NoSuchJobError, InvalidJobOperation):
            # Another consumer recovered or removed this job after our read.
            continue
        except Exception as error:
            logger.warning(
                "Privacy transcription recovery deferred: %s", type(error).__name__
            )
            stats["errors"] += 1
    return stats
