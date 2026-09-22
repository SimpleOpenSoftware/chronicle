"""Real journal/client/caller entry points; network and model execution are fake."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, invoke, revoke  # noqa: F401
from test_privacy_guided_enrollment import guided_setup  # noqa: F401

from backend.services import privacy
from backend.services import speaker_enrollment as journal
from backend.speaker_recognition_client import SpeakerRecognitionClient


@pytest.fixture
async def enrollment_setup(evidence, monkeypatch):
    monkeypatch.setattr(journal, "distributed_lock", privacy.distributed_lock)
    calls = []
    provider = {}

    async def operation(action, identifier, binding, **kwargs):
        # The Chronicle journal must exist before ANY provider write.
        row = await evidence.db.speaker_enrollment_operations.find_one(
            {"_id": identifier}
        )
        assert row
        assert kwargs["catalog_id"] == "d" * 32
        assert kwargs["service_url"] == "http://synthetic-speaker.invalid"
        calls.append(action)
        state = {
            "prepare": "prepared",
            "activate": "active",
            "quarantine": "quarantined",
        }[action]
        if provider.get(identifier) == "quarantined" and action != "quarantine":
            raise journal.EnrollmentUnavailable()
        provider[identifier] = state
        return {
            "operation_id": identifier,
            "state": state,
            "speaker_id": binding["speaker_id"],
            "segment_id": 7 if state == "active" else None,
        }

    client = object.__new__(SpeakerRecognitionClient)
    client.enabled = True
    client.service_url = "http://synthetic-speaker.invalid"
    client.enrollment_catalog = AsyncMock(return_value={"catalog_id": "d" * 32})
    client.enrollment_operation = AsyncMock(side_effect=operation)
    client.get_speaker_by_name = AsyncMock(return_value=None)
    return SimpleNamespace(
        client=client, calls=calls, provider=provider, operation=operation
    )


async def enroll(setup):
    visibility = privacy.ConversationPrivacyFilter()
    records = await journal.capture_evidence(visibility, ["synthetic-recording"])
    return await setup.client.enroll_new_speaker(
        "Synthetic speaker",
        b"synthetic audio",
        "review-admin",
        conversation_ids=["synthetic-recording"],
        visibility=visibility,
        evidence_records=records,
    )


@pytest.mark.asyncio
async def test_journal_precedes_write_and_identical_delivery_is_idempotent(
    evidence, enrollment_setup
):
    await allow(evidence)
    results = await asyncio.gather(enroll(enrollment_setup), enroll(enrollment_setup))
    assert sorted(r["status"] for r in results) == ["already_enrolled", "enrolled"]
    assert enrollment_setup.calls == ["prepare", "activate", "activate"]
    rows = await evidence.db.speaker_enrollment_operations.find({}).to_list(None)
    assert len(rows) == 1
    assert rows[0]["state"] == "active"
    assert rows[0]["binding"]["evidence"]["privacy_revisions"] == {
        "evidence-owner": {"screenpipe-test": 1}
    }
    assert rows[0]["evidence_owner_ids"] == ["evidence-owner"]


@pytest.mark.asyncio
async def test_held_canonical_owner_stops_before_provider_or_journal(
    evidence, enrollment_setup
):
    with pytest.raises(privacy.PrivacyHeld):
        await enroll(enrollment_setup)
    enrollment_setup.client.enrollment_catalog.assert_not_awaited()
    assert await evidence.db.speaker_enrollment_operations.count_documents({}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "activate"])
async def test_revision_change_quarantines_external_write_before_success(
    evidence, enrollment_setup, stage
):
    await allow(evidence)

    async def changed(action, *args, **kwargs):
        reply = await enrollment_setup.operation(action, *args, **kwargs)
        if action == stage:
            await revoke(evidence)
        return reply

    enrollment_setup.client.enrollment_operation.side_effect = changed
    with pytest.raises(privacy.PrivacyHeld):
        await enroll(enrollment_setup)
    assert enrollment_setup.calls[-1] == "quarantine"
    assert set(enrollment_setup.provider.values()) == {"quarantined"}
    assert (await evidence.db.speaker_enrollment_operations.find_one({}))[
        "state"
    ] == "quarantined"


@pytest.mark.asyncio
async def test_lost_activation_and_failed_compensation_remain_durable_until_recovery(
    evidence, enrollment_setup, monkeypatch
):
    await allow(evidence)

    async def failed(action, *args, **kwargs):
        if action == "quarantine":
            raise journal.EnrollmentUnavailable()
        result = await enrollment_setup.operation(action, *args, **kwargs)
        if action == "activate":
            raise journal.EnrollmentUnavailable()
        return result

    enrollment_setup.client.enrollment_operation.side_effect = failed
    with pytest.raises(journal.EnrollmentUnavailable):
        await enroll(enrollment_setup)
    assert (await evidence.db.speaker_enrollment_operations.find_one({}))[
        "state"
    ] == "quarantine_pending"
    assert set(enrollment_setup.provider.values()) == {"active"}
    enrollment_setup.client.enrollment_operation.side_effect = (
        enrollment_setup.operation
    )
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    result = await journal.recover_speaker_enrollments()
    assert result == {"checked": 1, "quarantined": 1, "pending": 0}
    assert set(enrollment_setup.provider.values()) == {"quarantined"}


@pytest.mark.asyncio
async def test_canceled_prepare_is_quarantined(evidence, enrollment_setup):
    await allow(evidence)
    entered = asyncio.Event()

    async def paused(action, *args, **kwargs):
        if action == "prepare":
            await enrollment_setup.operation(action, *args, **kwargs)
            entered.set()
            await asyncio.Event().wait()
        return await enrollment_setup.operation(action, *args, **kwargs)

    enrollment_setup.client.enrollment_operation.side_effect = paused
    task = asyncio.create_task(enroll(enrollment_setup))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(enrollment_setup.provider.values()) == {"quarantined"}


@pytest.mark.asyncio
async def test_recovery_preserves_completed_assets_after_source_exclusion(
    evidence, enrollment_setup, monkeypatch
):
    await allow(evidence)
    await enroll(enrollment_setup)
    row = await evidence.db.speaker_enrollment_operations.find_one({})
    await evidence.db.conversations.insert_one(
        {
            "conversation_id": "ordinary-recording",
            "user_id": "ordinary-owner",
            "client_id": "ordinary-device",
        }
    )
    await evidence.db.speaker_enrollment_operations.insert_one(
        {
            **row,
            "_id": "e" * 32,
            "conversation_ids": ["ordinary-recording"],
            "evidence_records": [
                {
                    "conversation_id": "ordinary-recording",
                    "user_id": "ordinary-owner",
                    "client_id": "ordinary-device",
                }
            ],
        }
    )
    await revoke(evidence)
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    result = await journal.recover_speaker_enrollments()
    assert result == {"checked": 2, "quarantined": 0, "pending": 0}
    assert (
        await evidence.db.speaker_enrollment_operations.find_one({"_id": "e" * 32})
    )["state"] == "active"


@pytest.mark.asyncio
async def test_recovery_waits_for_live_lease_and_recovers_abandoned_preparation(
    evidence, enrollment_setup, monkeypatch
):
    await allow(evidence)
    await enroll(enrollment_setup)
    await evidence.db.speaker_enrollment_operations.update_one(
        {},
        {
            "$set": {
                "state": "preparing",
                "lease_until": journal._now() + timedelta(minutes=5),
            }
        },
    )
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    assert (await journal.recover_speaker_enrollments())["checked"] == 0
    await evidence.db.speaker_enrollment_operations.update_one(
        {}, {"$set": {"lease_until": journal._now() - timedelta(seconds=1)}}
    )
    assert (await journal.recover_speaker_enrollments())["quarantined"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["manual", "scheduled"])
async def test_real_enrollment_callers_write_through_journal(
    evidence, enrollment_setup, monkeypatch, entry
):
    from backend.routers.modules import finetuning_routes
    from backend.workers import finetuning_jobs

    await allow(evidence)
    for module in (finetuning_routes, finetuning_jobs):
        monkeypatch.setattr(
            module, "SpeakerRecognitionClient", lambda: enrollment_setup.client
        )
    await invoke(entry)
    assert enrollment_setup.calls == ["prepare", "activate"]
    assert (await evidence.db.speaker_enrollment_operations.find_one({}))[
        "state"
    ] == "active"


@pytest.mark.asyncio
async def test_registered_recovery_and_default_schedule_are_wired(
    evidence, enrollment_setup, monkeypatch
):
    from pathlib import Path

    from backend import app_factory, cron_scheduler
    from backend.config_loader import load_config

    registered = {}
    monkeypatch.setattr(
        app_factory, "register_cron_job", lambda key, fn: registered.setdefault(key, fn)
    )
    app_factory.register_application_cron_jobs()
    assert (
        registered["speaker_enrollment_recovery"] is journal.recover_speaker_enrollments
    )
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    assert (await registered["speaker_enrollment_recovery"]())["checked"] == 0
    monkeypatch.setenv("CONFIG_DIR", str(Path(__file__).parents[2] / "config"))
    monkeypatch.setenv("CONFIG_FILE", "missing-test-overrides.yml")
    monkeypatch.setattr(
        cron_scheduler, "load_config", lambda: load_config(force_reload=True)
    )
    scheduler = cron_scheduler.CronScheduler()
    scheduler._load_jobs_from_config()
    job = scheduler.jobs["speaker_enrollment_recovery"]
    assert job.enabled and job.schedule == "* * * * *"


@pytest.mark.asyncio
async def test_completed_clip_is_independent_of_rebuilt_source(
    evidence, enrollment_setup, monkeypatch
):
    await allow(evidence)
    await enroll(enrollment_setup)
    await revoke(evidence)
    await evidence.db.conversations.update_one(
        {}, {"$set": {"client_id": "ordinary-device"}}
    )
    monkeypatch.setattr(
        "backend.speaker_recognition_client.SpeakerRecognitionClient",
        lambda: enrollment_setup.client,
    )
    assert (await journal.recover_speaker_enrollments())["quarantined"] == 0


@pytest.mark.asyncio
async def test_changed_claim_between_decode_and_enrollment_is_held(
    evidence, enrollment_setup
):
    await allow(evidence)
    visibility = privacy.ConversationPrivacyFilter()
    records = await journal.capture_evidence(visibility, ["synthetic-recording"])
    await evidence.db.conversations.update_one(
        {}, {"$set": {"created_at": evidence.row["created_at"] + timedelta(seconds=1)}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await enrollment_setup.client.enroll_new_speaker(
            "Synthetic speaker",
            b"synthetic audio",
            "review-admin",
            conversation_ids=["synthetic-recording"],
            visibility=visibility,
            evidence_records=records,
        )
    assert enrollment_setup.calls == []


@pytest.mark.asyncio
async def test_guided_decision_uses_the_real_journal(
    evidence, enrollment_setup, guided_setup, monkeypatch
):
    from test_privacy_guided_enrollment import clip

    from backend.controllers import guided_enrollment_controller as guided

    await allow(evidence)
    monkeypatch.setattr(
        guided, "SpeakerRecognitionClient", lambda: enrollment_setup.client
    )
    await guided.decide_clips(guided_setup.user, "Synthetic speaker", [clip()])
    assert enrollment_setup.calls == ["prepare", "activate"]
    assert (await evidence.db.speaker_enrollment_operations.find_one({}))[
        "state"
    ] == "active"


@pytest.mark.asyncio
async def test_real_http_transport_binds_catalog_and_never_resends_audio_on_replay(
    evidence, monkeypatch, unused_tcp_port
):
    from aiohttp import web

    await allow(evidence)
    monkeypatch.setattr(journal, "distributed_lock", privacy.distributed_lock)
    requests = []

    async def catalog(request):
        return web.json_response({"catalog_id": "d" * 32})

    async def operation(request):
        assert request.headers["X-Speaker-Catalog"] == "d" * 32
        assert request.headers["X-Chronicle-Service-Token"] == "synthetic-token"
        action = request.match_info["action"]
        requests.append(action)
        identifier = request.match_info["identifier"]
        row = await evidence.db.speaker_enrollment_operations.find_one(
            {"_id": identifier}
        )
        assert row
        if action == "prepare":
            import json

            body = await request.post()
            bound = json.loads(body["binding"])
            assert body["file"].file.read() == b"synthetic audio"
        else:
            bound = await request.json()
        assert bound == row["binding"]
        return web.json_response(
            {
                "operation_id": identifier,
                "state": "prepared" if action == "prepare" else "active",
                "speaker_id": bound["speaker_id"],
                "segment_id": 7 if action == "activate" else None,
            }
        )

    app = web.Application()
    app.router.add_get("/enrollment/operations/catalog", catalog)
    app.router.add_post("/enrollment/operations/{identifier}/{action}", operation)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    client = object.__new__(SpeakerRecognitionClient)
    client.enabled = True
    client.service_url = f"http://127.0.0.1:{unused_tcp_port}"
    client._gateway_headers = {"X-Chronicle-Service-Token": "synthetic-token"}
    try:
        setup = SimpleNamespace(client=client)
        assert (await enroll(setup))["status"] == "enrolled"
        assert (await enroll(setup))["status"] == "already_enrolled"
        assert requests == ["prepare", "activate", "activate"]
        row = await evidence.db.speaker_enrollment_operations.find_one({})
        client.service_url = "http://different-catalog.invalid"
        with pytest.raises(journal.EnrollmentUnavailable):
            await client.enrollment_operation(
                "quarantine",
                row["_id"],
                row["binding"],
                catalog_id=row["catalog_id"],
                service_url=row["service_url"],
            )
        assert requests == ["prepare", "activate", "activate"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_allowed_exact_audio_reuses_contribution_after_policy_revision(
    evidence, enrollment_setup
):
    await allow(evidence)
    first = await enroll(enrollment_setup)
    await evidence.db.capture_sources.update_one({}, {"$inc": {"privacy_revision": 1}})
    second = await enroll(enrollment_setup)
    assert second["status"] == "already_enrolled"
    assert second["operation_id"] == first["operation_id"]
    assert enrollment_setup.calls == ["prepare", "activate", "activate"]
    assert await evidence.db.speaker_enrollment_operations.count_documents({}) == 1
