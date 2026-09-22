"""The ingest entry point recovers admitted, terminal privacy failures."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from rq.job import JobStatus
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import queue_controller
from backend.services import device_audio_ingest as ingest
from backend.services import privacy


@pytest.fixture
async def recovery(evidence, monkeypatch):
    from rq.job import Job
    from rq.registry import FailedJobRegistry

    await evidence.db.capture_sources.update_one(
        {}, {"$set": {"provider": "screenpipe"}}
    )
    await evidence.db.conversations.update_one(
        {}, {"$set": {"external_source_type": "screenpipe"}}
    )
    job = NS(
        id="transcribe_synthetic-re",
        func_name="backend.workers.transcription_jobs.transcribe_full_audio_job",
        args=("synthetic-recording", "synthetic-version", "batch"),
        exc_info="backend.services.privacy.PrivacyHeld: Private or unscreened evidence is held from processing",
        get_status=Mock(return_value=JobStatus.FAILED),
    )
    requeue = Mock(
        side_effect=lambda _: job.get_status.configure_mock(
            return_value=JobStatus.QUEUED
        )
    )
    monkeypatch.setattr(
        queue_controller, "transcription_queue", NS(connection=object())
    )
    monkeypatch.setattr(FailedJobRegistry, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(
        FailedJobRegistry, "get_job_ids", lambda *args, **kwargs: [job.id]
    )
    monkeypatch.setattr(
        FailedJobRegistry, "requeue", lambda self, candidate: requeue(candidate)
    )
    monkeypatch.setattr(Job, "fetch", lambda *args, **kwargs: job)

    class EmptyInputs:
        def sort(self, *args):
            return self

        async def to_list(self):
            return []

    monkeypatch.setattr(
        ingest,
        "DeviceInputItem",
        NS(kind="kind", state="state", find=lambda *args: EmptyInputs()),
    )
    return NS(evidence=evidence, job=job, requeue=requeue)


async def test_ingest_recovers_terminal_privacy_failure_after_inputs_are_consumed(
    recovery,
):
    await allow(recovery.evidence)
    await ingest.process_device_audio()
    recovery.requeue.assert_called_once_with(recovery.job)
    # A subsequent cron pass must respect the now-live job.
    await ingest.process_device_audio()
    recovery.requeue.assert_called_once()


async def test_ingest_keeps_private_failure_held_until_override(recovery):
    await ingest.process_device_audio()
    recovery.requeue.assert_not_called()
    await allow(recovery.evidence)
    await ingest.process_device_audio()
    recovery.requeue.assert_called_once()


@pytest.mark.parametrize(
    "state",
    [
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.SCHEDULED,
        JobStatus.DEFERRED,
        JobStatus.FINISHED,
        JobStatus.CANCELED,
    ],
)
async def test_recovery_never_restarts_nonfailed_handle(recovery, state):
    await allow(recovery.evidence)
    recovery.job.get_status.return_value = state
    await ingest.process_device_audio()
    recovery.requeue.assert_not_called()


@pytest.mark.parametrize(
    "change", ["other_failure", "other_function", "other_source", "missing_record"]
)
async def test_recovery_is_limited_to_screenpipe_privacy_failures(recovery, change):
    await allow(recovery.evidence)
    if change == "other_failure":
        recovery.job.exc_info = "RuntimeError: provider unavailable"
    elif change == "other_function":
        recovery.job.func_name = "backend.workers.other_job"
    elif change == "other_source":
        await recovery.evidence.db.conversations.update_one(
            {}, {"$set": {"external_source_type": "upload"}}
        )
    else:
        await recovery.evidence.db.conversations.delete_many({})
    await ingest.process_device_audio()
    recovery.requeue.assert_not_called()


async def test_recovery_rechecks_revision_before_enqueue(recovery, monkeypatch):
    await allow(recovery.evidence)
    original = privacy.require_record

    async def crossed(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        await revoke(recovery.evidence)
        return snapshot

    monkeypatch.setattr(privacy, "require_record", crossed)
    await ingest.process_device_audio()
    recovery.requeue.assert_not_called()
