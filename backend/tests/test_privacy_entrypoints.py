from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mongomock_motor import AsyncMongoMockClient

from backend.routers.modules import device_input_routes as routes
from backend.services import privacy

START = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient().privacy_test
    monkeypatch.setattr(privacy, "database", lambda: database)
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", AsyncMock())
    await database.capture_sources.insert_one(
        {
            "source_id": "screenpipe-test",
            "user_id": "owner",
            "privacy_enabled_from": START,
            "privacy_tracks": ["display"],
            "privacy_revision": 1,
        }
    )
    await database.privacy_display_sets.insert_one(
        {
            "source_id": "screenpipe-test",
            "user_id": "owner",
            "observed_at": START,
            "transition_started_at": START,
            "track_ids": ["display"],
        }
    )
    return database


async def test_model_exchange_route_blocks_before_reading_private_artifacts(
    db, monkeypatch
):
    from backend.routers.modules import timeline_routes

    await db.timeline_episodes.insert_one(
        {
            "user_id": "owner",
            "episode_key": "held-episode",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    proposal = SimpleNamespace(
        model_dump=lambda: {
            "proposal_id": "proposal",
            "selected_episodes": [{"episode_key": "held-episode"}],
        }
    )
    monkeypatch.setattr(
        timeline_routes.MemoryReviewProposal,
        "find_one",
        AsyncMock(return_value=proposal),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Private proposal must not be serialized")

    monkeypatch.setattr(timeline_routes, "_proposal_payload", forbidden)
    with pytest.raises(privacy.PrivacyHeld):
        await timeline_routes.get_memory_model_exchanges(
            "proposal", SimpleNamespace(id="owner")
        )


async def test_saved_organization_exchange_resolves_original_evidence(db, monkeypatch):
    from backend.models.session_memory import SessionPreparation
    from backend.routers.modules import timeline_routes
    from backend.services import inference_artifacts

    await db.timeline_episodes.insert_one(
        {
            "user_id": "owner",
            "episode_key": "held-episode",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    monkeypatch.setattr(
        SessionPreparation,
        "find_one",
        AsyncMock(return_value=SimpleNamespace(inference_artifacts=["artifact"])),
    )
    monkeypatch.setattr(
        inference_artifacts,
        "read_inference_artifact",
        lambda *args: {
            "request": {"source_map": [{"episode_key": "held-episode"}]},
            "stdout": "Synthetic private explanation",
        },
    )
    with pytest.raises(privacy.PrivacyHeld):
        await timeline_routes.get_session_organization_exchanges(
            START.date(), "Asia/Kolkata", SimpleNamespace(id="owner")
        )


async def test_projected_proposal_resolves_canonical_private_context(db):
    await db.memory_review_proposals.insert_one(
        {
            "user_id": "owner",
            "proposal_id": "held-proposal",
            "source_scope": [
                {
                    "source_id": "screenpipe-test",
                    "started_at": START,
                    "ended_at": START + timedelta(seconds=1),
                }
            ],
        }
    )
    rows = [
        {"proposal_id": "held-proposal", "summary": "Synthetic private summary"},
        {"summary": "Synthetic ordinary unrelated row"},
    ]
    assert await privacy.filter_payloads(rows, "owner") == rows[1:]


async def test_session_source_route_checks_privacy_before_returning_excerpts(
    db, monkeypatch
):
    from backend.routers.modules import timeline_routes
    from backend.services.timeline import sessions

    group = SimpleNamespace(group_key="session", revision=1)
    member = {
        "source_id": "screenpipe-test",
        "started_at": START,
        "ended_at": START + timedelta(seconds=1),
    }
    monkeypatch.setattr(sessions, "get_day", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(sessions, "snapshot_episodes", AsyncMock(return_value=[member]))
    monkeypatch.setattr(
        sessions, "resolved_sessions", AsyncMock(return_value=[(None, group, [member])])
    )
    with pytest.raises(privacy.PrivacyHeld):
        await timeline_routes.get_session_sources(
            START.date(), "session", "Asia/Kolkata", 1, SimpleNamespace(id="owner")
        )


async def test_privacy_holds_have_a_content_free_http_response():
    import httpx

    from backend.app_factory import create_app

    app = create_app()

    @app.get("/synthetic-private-hold")
    async def held():
        raise privacy.PrivacyHeld()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/synthetic-private-hold")
    assert response.status_code == 423
    assert response.json() == {
        "detail": "Private or unscreened evidence is held from processing"
    }


async def test_review_worker_recovery_cannot_publish_held_proposal(db, monkeypatch):
    from contextlib import asynccontextmanager

    from backend.services.timeline import review

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield

    proposal = SimpleNamespace(
        id="proposal", user_id="owner", state="applying", save=AsyncMock()
    )
    proposal.model_dump = lambda: {
        "user_id": "owner",
        "source_scope": [
            {
                "source_id": "screenpipe-test",
                "started_at": START,
                "ended_at": START + timedelta(seconds=1),
            }
        ],
    }
    monkeypatch.setattr(review, "distributed_lock", lock)
    monkeypatch.setattr(
        review.MemoryReviewProposal, "get", AsyncMock(return_value=proposal)
    )
    finish = AsyncMock(
        side_effect=AssertionError("Held changes must not reach the vault")
    )
    monkeypatch.setattr(review, "_finish_application", finish)
    assert await review.process_memory_review_decision(proposal) == "applying"
    assert "PrivacyHeld" in proposal.error
    finish.assert_not_awaited()


def result(state="excluded"):
    return privacy.ScreeningResult(
        interval_id="display:1:11",
        track_id="display",
        started_at=START,
        ended_at=START + timedelta(seconds=10),
        model_version="test-model",
        policy_version="screen-privacy-v1",
        segments=[
            dict(
                started_at=START,
                ended_at=START + timedelta(seconds=10),
                state=state,
                coverage="sampled",
            )
        ],
        evidence=[
            dict(
                frame_id=1,
                captured_at=START,
                state=state,
                score=0.9 if state == "excluded" else 0,
                input_hash="a" * 64,
            )
        ],
    )


async def test_collector_submission_is_idempotent_and_owner_scoped(db):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    await routes.submit_screening(result(), source)
    await routes.submit_screening(result(), source)
    assert await db.privacy_screening.count_documents({}) == 1
    assert not (await privacy.load_snapshot("owner")).permits(
        "screenpipe-test:input:mic", START, START + timedelta(seconds=10)
    )
    assert (await privacy.load_snapshot("someone-else")).permits(
        "screenpipe-test", START, START + timedelta(seconds=10)
    )


@pytest.mark.parametrize("version", ["screen-privacy-v1", "screen-privacy-v2"])
async def test_http_screening_accepts_versioned_durable_results(db, version):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._device_source] = lambda: source
    body = result().model_dump(mode="json")
    body["policy_version"] = version
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = routes.router.prefix + "/screening"
        for _ in range(2):
            response = await client.post(path, json=body)
            assert response.status_code == 200
        stored = await db.privacy_screening.find_one({})
        assert stored["policy_version"] == version
        assert await db.privacy_screening.count_documents({}) == 1
        changed = {**body, "policy_version": version + "-different"}
        assert (await client.post(path, json=changed)).status_code == 409
        for invalid in ("", "x" * 65):
            assert (
                await client.post(path, json={**body, "policy_version": invalid})
            ).status_code == 422


async def test_display_inventory_submission_is_idempotent_and_invalidates_running_jobs(
    db,
):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    before = await privacy.load_snapshot("owner")
    body = privacy.PrivacyDisplaySet(
        observed_at=START + timedelta(seconds=10),
        transition_started_at=START + timedelta(seconds=5),
        track_ids=["second", "display"],
    )
    await routes.submit_privacy_displays(body, source)
    await routes.submit_privacy_displays(body, source)
    assert await db.privacy_display_sets.count_documents({"user_id": "owner"}) == 2
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    conflict = body.model_copy(update={"track_ids": ["display"]})
    with pytest.raises(routes.HTTPException) as exc:
        await routes.submit_privacy_displays(conflict, source)
    assert exc.value.status_code == 409
    assert (await privacy.load_snapshot("another-owner")).sources == {}


async def test_history_registration_holds_gaps_before_activation_and_invalidates_jobs(
    db,
):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    before = await privacy.load_snapshot("owner")
    start, end = START - timedelta(days=1), START - timedelta(hours=23)
    body = privacy.PrivacyRequiredRange(
        started_at=start, ended_at=end, track_ids=["display", "second"]
    )
    assert before.permits(source.source_id, start, end)
    await routes.submit_privacy_required_range(body, source)
    await routes.submit_privacy_required_range(body, source)
    assert await db.privacy_required_ranges.count_documents({}) == 1
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)
    snapshot = await privacy.load_snapshot("owner", start, end)
    for suffix in ("", ":input:microphone", ":output:system"):
        assert not snapshot.permits(source.source_id + suffix, start, end)
    assert snapshot.permits("another-device", start, end)
    assert (await privacy.load_snapshot("another-owner")).sources == {}
    # A completed sample on one display cannot release the other display's audio.
    for track in ("display", "second"):
        await db.privacy_screening.insert_one(
            {
                "user_id": "owner",
                "source_id": source.source_id,
                "track_id": track,
                "started_at": start,
                "ended_at": end - timedelta(seconds=1),
                "segments": [
                    dict(
                        started_at=start,
                        ended_at=end - timedelta(seconds=1),
                        state="allowed",
                    )
                ],
            }
        )
        if track == "display":
            assert not (await privacy.load_snapshot("owner")).permits(
                source.source_id, start, end - timedelta(seconds=1)
            )
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.allowed_spans(source.source_id, start, end) == [
        (start, end - timedelta(seconds=1))
    ]
    await db.capture_sources.update_one(
        {"source_id": source.source_id},
        {
            "$set": {
                "health.privacy_screening": {
                    "state": "unavailable",
                    "last_failure": "display_inventory_unavailable",
                }
            }
        },
    )
    rows = await privacy.list_intervals(
        "owner", start - timedelta(seconds=1), end + timedelta(seconds=1)
    )
    assert [(r["started_at"], r["ended_at"], r["state"]) for r in rows] == [
        (end - timedelta(seconds=1), end, "pending")
    ]
    assert (
        rows[0]["reason"]
        == "Historical screening is incomplete; unresolved time remains held."
    )


async def test_history_invalidation_retry_keeps_hold_and_preserves_overrides(
    db, monkeypatch
):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    start, end = START - timedelta(days=1), START - timedelta(hours=23)
    body = privacy.PrivacyRequiredRange(started_at=start, ended_at=end, track_ids=[])
    dirty = AsyncMock(side_effect=RuntimeError("synthetic interrupted invalidation"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", dirty)
    with pytest.raises(RuntimeError):
        await routes.submit_privacy_required_range(body, source)
    assert (await db.capture_sources.find_one({"source_id": source.source_id}))[
        "privacy_updating"
    ]
    dirty.side_effect = None
    await routes.submit_privacy_required_range(body, source)
    current = await db.capture_sources.find_one({"source_id": source.source_id})
    assert not current["privacy_updating"]
    assert current["privacy_revision"] == 2
    # Even an apparently allowed frame cannot prove coverage of an empty inventory.
    await db.privacy_screening.insert_one(
        {
            "user_id": "owner",
            "source_id": source.source_id,
            "track_id": "display",
            "started_at": start,
            "ended_at": end,
            "segments": [dict(started_at=start, ended_at=end, state="allowed")],
        }
    )
    assert not (await privacy.load_snapshot("owner")).permits(
        source.source_id, start, end
    )
    await routes.override_privacy(
        routes.PrivacyOverride(
            source_id=source.source_id,
            started_at=start,
            ended_at=end,
            revision=current["privacy_revision"],
            decision="allowed",
        ),
        SimpleNamespace(id="owner", user_id="owner"),
    )
    await routes.submit_privacy_required_range(body, source)
    assert (await privacy.load_snapshot("owner")).permits(source.source_id, start, end)


async def test_override_rejects_stale_revision_and_survives_collector_replay(
    db, monkeypatch
):
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", AsyncMock())
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    user = SimpleNamespace(id="owner", user_id="owner")
    await routes.submit_screening(result(), source)
    body = routes.PrivacyOverride(
        source_id=source.source_id,
        started_at=START,
        ended_at=START + timedelta(seconds=10),
        revision=2,
        decision="allowed",
    )
    await routes.override_privacy(body, user)
    with pytest.raises(routes.HTTPException) as stale:
        await routes.override_privacy(body, user)
    assert stale.value.status_code == 409
    await routes.submit_screening(result(), source)
    assert (await privacy.load_snapshot("owner")).permits(
        source.source_id, START, START + timedelta(seconds=10)
    )


@pytest.mark.parametrize("historical", [False, True])
async def test_transcription_entrypoint_blocks_before_provider_lookup(
    db, monkeypatch, historical
):
    from backend.workers import transcription_jobs

    captured = START - timedelta(days=1) if historical else START
    if historical:
        await routes.submit_privacy_required_range(
            privacy.PrivacyRequiredRange(
                started_at=captured, ended_at=START, track_ids=["display"]
            ),
            SimpleNamespace(
                user_id="owner", source_id="screenpipe-test", provider="screenpipe"
            ),
        )
    await db.conversations.insert_one(
        {
            "conversation_id": "recording",
            "user_id": "owner",
            "audio_ranges": [
                dict(
                    capture_source_id="screenpipe-test:output:speakers",
                    started_at=captured,
                    ended_at=captured + timedelta(seconds=5),
                )
            ],
        }
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "No transcription provider may be loaded for held evidence"
        )

    monkeypatch.setattr(transcription_jobs, "get_transcription_provider", forbidden)
    with pytest.raises(privacy.PrivacyHeld):
        await transcription_jobs.transcribe_audio_range("recording")


async def test_raw_thumbnail_api_cannot_reveal_held_pixels(db, monkeypatch):
    item = SimpleNamespace(
        user_id="owner",
        source_id="screenpipe-test",
        captured_at=START,
        ended_at=START + timedelta(seconds=5),
        media_data=b"private pixels",
    )
    item.model_dump = lambda: dict(
        user_id=item.user_id,
        source_id=item.source_id,
        captured_at=item.captured_at,
        ended_at=item.ended_at,
    )
    monkeypatch.setattr(routes.DeviceInputItem, "get", AsyncMock(return_value=item))
    with pytest.raises(routes.HTTPException) as held:
        await routes.context_thumbnail(
            "item", SimpleNamespace(id="owner", user_id="owner")
        )
    assert held.value.status_code == 423


async def test_interrupted_screening_invalidation_stays_held_until_replay(
    db, monkeypatch
):
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    invalidate = AsyncMock(side_effect=RuntimeError("Synthetic queue failure"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", invalidate)
    with pytest.raises(RuntimeError):
        await routes.submit_screening(result("allowed"), source)
    snapshot = await privacy.load_snapshot("owner")
    assert not snapshot.permits(source.source_id, START, START + timedelta(seconds=10))
    invalidate.side_effect = None
    await routes.submit_screening(result("allowed"), source)
    assert (await privacy.load_snapshot("owner")).permits(
        source.source_id, START, START + timedelta(seconds=10)
    )
    assert await db.privacy_screening.count_documents({}) == 1


@pytest.mark.parametrize("kind", ["recording", "episode", "session"])
async def test_chat_source_entrypoint_resolves_private_lineage_before_reading(
    db, monkeypatch, kind
):
    from backend.services import chat_sources

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "audio_ranges": [
                dict(
                    capture_source_id="screenpipe-test",
                    started_at=START,
                    ended_at=START + timedelta(seconds=5),
                )
            ],
        }
    )
    await db.timeline_episodes.insert_one(
        {
            "user_id": "owner",
            "episode_id": "episode",
            "related_conversation_ids": ["recording"],
        }
    )
    await db.undated_sessions.insert_one(
        {"user_id": "owner", "session_key": "session", "recording_id": "recording"}
    )
    reader = AsyncMock(side_effect=AssertionError("Held source must not be read"))
    monkeypatch.setattr(chat_sources, "_resolve", reader)
    with pytest.raises(chat_sources.SourceUnavailable):
        await chat_sources.resolve_source(
            chat_sources.ChatSourceRef(kind=kind, key=kind), "owner"
        )
    reader.assert_not_awaited()


async def test_mixed_note_and_audit_diff_are_held_without_editing_note(db, monkeypatch):
    from backend.controllers import memory_controller

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    await db.memory_audit.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "note_path": "Topics/Example.md",
        }
    )
    assert await privacy.quarantined_vault_paths("owner") == {
        "Conversations/recording.md",
        "Topics/Example.md",
    }
    entry = SimpleNamespace(
        user_id="owner",
        memory_space_id=None,
        note_path="Topics/Example.md",
        after_text="Synthetic private note",
    )
    monkeypatch.setattr(
        memory_controller.MemoryAuditEntry, "get", AsyncMock(return_value=entry)
    )
    response = await memory_controller.get_memory_audit_diff(
        SimpleNamespace(user_id="owner", is_superuser=False), "a" * 24
    )
    assert response.status_code == 423
    assert b"Synthetic private note" not in response.body
    assert entry.after_text == "Synthetic private note"


async def test_restrictive_change_invalidates_running_job(db):
    before = await privacy.load_snapshot("owner")
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    await routes.submit_screening(result(), source)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", before)


async def test_timeline_pending_marker_starts_at_activation(db):
    rows = await routes.privacy_intervals(
        START - timedelta(seconds=10),
        START + timedelta(seconds=10),
        SimpleNamespace(id="owner", user_id="owner"),
    )
    assert len(rows["intervals"]) == 1
    assert rows["intervals"][0]["started_at"] == START
    assert rows["intervals"][0]["state"] == "pending"


@pytest.mark.parametrize(
    "failure",
    [
        "stale_heartbeat",
        "stale_inventory",
        "missing_inventory",
        "no_result",
        "model_changed",
        "update_in_progress",
        None,
    ],
)
async def test_activation_requires_live_screening_readiness(db, monkeypatch, failure):
    now = privacy.utc(datetime.now(timezone.utc))
    health = {
        "state": "ready",
        "model_version": "test-model",
        "inventory_checked_at": now.isoformat(),
    }
    last_seen = now
    if failure == "stale_heartbeat":
        last_seen -= timedelta(minutes=3)
    if failure == "stale_inventory":
        health["inventory_checked_at"] = (now - timedelta(minutes=3)).isoformat()
    if failure == "model_changed":
        health["model_version"] = "different-model"
    await db.capture_sources.update_one(
        {"source_id": "screenpipe-test"},
        {
            "$set": {
                "privacy_enabled_from": None,
                "provider": "screenpipe",
                "last_seen_at": last_seen,
                "health": {"privacy_screening": health},
                "privacy_updating": failure == "update_in_progress",
                "privacy_operation": (
                    "other-operation" if failure == "update_in_progress" else None
                ),
            }
        },
    )
    if failure == "missing_inventory":
        await db.privacy_display_sets.delete_many({})
    if failure != "no_result":
        sample = result("allowed").model_dump()
        sample.update(user_id="owner", source_id="screenpipe-test", ended_at=now)
        await db.privacy_screening.insert_one(sample)
    source = SimpleNamespace(
        **await db.capture_sources.find_one({"source_id": "screenpipe-test"})
    )
    monkeypatch.setattr(
        routes.CaptureSource, "find_one", AsyncMock(return_value=source)
    )
    user = SimpleNamespace(id="owner", user_id="owner")
    body = routes.PrivacyActivation(started_at=now - timedelta(seconds=30))
    if failure:
        with pytest.raises(routes.HTTPException) as exc:
            await routes.activate_privacy("screenpipe-test", body, user)
        assert exc.value.status_code == 409
        assert (await db.capture_sources.find_one({"source_id": "screenpipe-test"}))[
            "privacy_enabled_from"
        ] is None
    else:
        activated = await routes.activate_privacy("screenpipe-test", body, user)
        assert activated["active"]
        assert activated["started_at"] == body.started_at


async def test_activation_retry_finishes_invalidation_before_releasing_hold(
    db, monkeypatch
):
    import hashlib

    started_at = START
    operation = hashlib.sha256(
        f"owner:screenpipe-test:activate:{started_at.isoformat()}".encode()
    ).hexdigest()
    await db.capture_sources.update_one(
        {"source_id": "screenpipe-test"},
        {
            "$set": {
                "privacy_operation": operation,
                "privacy_updating": True,
            }
        },
    )
    source = SimpleNamespace(
        **await db.capture_sources.find_one({"source_id": "screenpipe-test"})
    )
    monkeypatch.setattr(
        routes.CaptureSource, "find_one", AsyncMock(return_value=source)
    )
    invalidation = AsyncMock(
        side_effect=[RuntimeError("Synthetic queue interruption"), None]
    )
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", invalidation)
    user = SimpleNamespace(id="owner", user_id="owner")
    body = routes.PrivacyActivation(started_at=started_at)
    with pytest.raises(RuntimeError):
        await routes.activate_privacy("screenpipe-test", body, user)
    assert (await db.capture_sources.find_one({"source_id": "screenpipe-test"}))[
        "privacy_updating"
    ]
    activated = await routes.activate_privacy("screenpipe-test", body, user)
    assert activated["active"]
    saved = await db.capture_sources.find_one({"source_id": "screenpipe-test"})
    assert not saved["privacy_updating"]
    assert saved["privacy_revision"] == 1
    await routes.activate_privacy("screenpipe-test", body, user)
    assert invalidation.await_count == 2


@pytest.mark.parametrize("retained", ["note_evidence", "unproven_historical_answer"])
async def test_chat_detail_and_run_routes_hold_retained_private_evidence(
    db, monkeypatch, retained
):
    from backend.chat_service import ChatService, ChatSession
    from backend.routers.modules import chat_routes

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
            "vault_paths": ["Topics/Synthetic.md"],
        }
    )
    session = ChatSession(session_id="chat", user_id="owner", title="Synthetic chat")
    await db.chat_sessions.insert_one(session.to_dict())
    evidence = {"evidence": {"vault_notes": [{"path": "Topics/Synthetic.md"}]}}
    await db.chat_messages.insert_one(
        {
            "user_id": "owner",
            "session_id": "chat",
            "role": "assistant",
            "metadata": evidence if retained == "note_evidence" else {},
        }
    )
    service = ChatService()
    service._initialized = True
    service.db = db
    service.sessions_collection = db.chat_sessions
    service.messages_collection = db.chat_messages
    monkeypatch.setattr(chat_routes, "get_chat_service", lambda: service)
    user = SimpleNamespace(id="owner", user_id="owner")
    with pytest.raises(chat_routes.HTTPException) as exc:
        await chat_routes.get_session_messages("chat", 100, 0, user)
    assert exc.value.status_code == 423
    with pytest.raises(chat_routes.HTTPException) as exc:
        await chat_routes.get_chat_runs("chat", user)
    assert exc.value.status_code == 423
    assert (await privacy.guard_chat("someone-else", "chat", {})).sources == {}


async def test_chat_model_output_is_discarded_when_privacy_changes(db, monkeypatch):
    from backend import chat_service

    service = chat_service.ChatService()
    service._initialized = True
    service.add_message = AsyncMock(return_value=True)
    service.get_session_messages = AsyncMock(return_value=[])
    service._get_tool_mode_system_prompt = AsyncMock(
        return_value="Synthetic instructions"
    )

    async def stream(*args, **kwargs):
        await db.capture_sources.update_one(
            {"user_id": "owner"}, {"$inc": {"privacy_revision": 1}}
        )
        yield {"type": "content", "text": "Synthetic stale answer"}

    monkeypatch.setattr(chat_service, "async_chat_with_tools_stream", stream)
    events = [
        event
        async for event in service._generate_response_tool_mode(
            "chat", "owner", "Synthetic question"
        )
    ]
    assert any(event["type"] == "error" for event in events)
    assert not any(
        event["type"] in {"token", "complete", "evidence"} for event in events
    )
    assert service.add_message.await_count == 1  # Only the user's input was persisted.


async def test_timeline_context_seed_omits_quarantined_notes(db, monkeypatch):
    from backend.services.inference_artifacts import canonical_hash
    from backend.services.timeline import accepted_context

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
            "vault_paths": ["Topics/Synthetic.md"],
        }
    )
    monkeypatch.setattr(
        accepted_context,
        "snapshot",
        lambda *_: {
            "Topics/Synthetic.md": "Synthetic private text",
            "Topics/Allowed.md": "Synthetic ordinary text",
        },
    )
    seeded = await accepted_context.for_sources("owner", [])
    assert seeded["scope_hash"] == canonical_hash(
        {"Topics/Allowed.md": "Synthetic ordinary text"}
    )
    assert await accepted_context.processing_snapshot("owner") == {
        "Topics/Allowed.md": "Synthetic ordinary text"
    }


async def test_staged_memory_agent_uses_real_privacy_owner(db, monkeypatch, tmp_path):
    from backend.services.memory.agent import memory_agent
    from backend.services.memory.agent.vault_tools import VaultToolError

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
            "vault_paths": ["Topics/Synthetic.md"],
        }
    )
    stage = tmp_path / "temporary-review-vault"
    (stage / "Topics").mkdir(parents=True)
    (stage / "Topics" / "Synthetic.md").write_text("Synthetic held note")
    agent = memory_agent.MemoryAgent(stage)
    monkeypatch.setattr(
        memory_agent, "_get_prompt", AsyncMock(return_value="Synthetic instructions")
    )

    async def model(*args, **kwargs):
        assert agent._privacy_owner == "owner"
        with pytest.raises(VaultToolError):
            agent.tools.read_note("Topics/Synthetic.md")
        raise RuntimeError("Synthetic model stop")

    monkeypatch.setattr(memory_agent, "async_chat_with_tools", model)
    with pytest.raises(RuntimeError, match="Synthetic model stop"):
        async with privacy.processing_scope("owner", {}):
            await agent.run("Synthetic allowed input", "allowed-recording")
    assert privacy.processing_owner("outside") == "outside"


async def test_write_reviewer_does_not_swallow_privacy_change(
    db, monkeypatch, tmp_path
):
    from backend.services.memory.agent import review_agent

    root = tmp_path / "owner"
    (root / "Topics").mkdir(parents=True)
    (root / "Topics" / "Allowed.md").write_text("Synthetic new note")

    async def model(*args, **kwargs):
        await db.capture_sources.update_one(
            {"user_id": "owner"}, {"$inc": {"privacy_revision": 1}}
        )
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(review_agent, "async_chat_with_tools", model)
    with pytest.raises(privacy.PrivacyHeld):
        await review_agent.review_vault_write(
            root,
            source="Synthetic input",
            before={},
            touched=["Topics/Allowed.md"],
            record="conversation",
        )


async def test_pi_gateway_holds_private_note_and_discards_changed_policy_result(
    db, monkeypatch, tmp_path
):
    import asyncio
    import json
    from pathlib import Path

    from test_pi_executor import _call_gateway, _jsonl, _runtime_config

    from backend.services.memory.agent import pi_agent
    from backend.services.memory.agent.vault_tools import VAULT_SEARCH_TOOL_SCHEMAS

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
            "vault_paths": ["Topics/Synthetic.md"],
        }
    )
    root = tmp_path / "owner"
    (root / "Topics").mkdir(parents=True)
    (root / "Topics" / "Synthetic.md").write_text("Synthetic private sentinel")
    checked = []

    async def spawn(*command, **kwargs):
        extension = Path(command[command.index("-e") + 1]).read_text()

        class Process:
            returncode = 0

            async def communicate(self, input_bytes=None):
                response = await asyncio.to_thread(
                    _call_gateway,
                    extension,
                    "read_note",
                    {"path": "Topics/Synthetic.md"},
                )
                assert "Synthetic private sentinel" not in json.dumps(response)
                assert "held evidence" in json.dumps(response)
                checked.append(True)
                await db.capture_sources.update_one(
                    {"user_id": "owner"}, {"$inc": {"privacy_revision": 1}}
                )
                return _jsonl({"type": "agent_start"}, {"type": "agent_end"}), b""

        return Process()

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(privacy.PrivacyHeld):
        await pi_agent._invoke_pi(
            root,
            prompt="Synthetic question",
            system_prompt="Synthetic instructions",
            schemas=VAULT_SEARCH_TOOL_SCHEMAS,
            config=_runtime_config(
                api_key="synthetic-key", base_url="http://model.invalid/v1"
            ),
            max_tool_rounds=2,
            max_tool_calls=2,
            user_id="owner",
        )
    assert checked == [True]


async def test_direct_memory_entrypoint_blocks_before_model_call(
    db, tmp_path, monkeypatch
):
    from backend.services.memory.agent import memory_agent

    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "recording",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    provider = AsyncMock(
        side_effect=AssertionError("Held transcript must not enter a model")
    )
    monkeypatch.setattr(memory_agent, "async_chat_with_tools", provider)
    with pytest.raises(privacy.PrivacyHeld):
        await memory_agent.MemoryAgent(tmp_path / "owner").run(
            "Synthetic private transcript", "recording"
        )
    provider.assert_not_awaited()


async def test_direct_search_discards_completion_when_privacy_changes(
    db, tmp_path, monkeypatch
):
    from backend.services.memory.agent import memory_agent

    monkeypatch.setattr(
        memory_agent, "_get_prompt", AsyncMock(return_value="Synthetic instructions")
    )

    async def model(*_args, **_kwargs):
        await db.capture_sources.update_one(
            {"user_id": "owner"}, {"$inc": {"privacy_revision": 1}}
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Stale answer", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(memory_agent, "async_chat_with_tools", model)
    with pytest.raises(privacy.PrivacyHeld):
        await memory_agent.search_vault(
            "Synthetic question", tmp_path / "owner", user_id="owner"
        )


async def test_audio_worker_cuts_private_samples_and_retries_without_reprocessing(
    db, monkeypatch
):
    import array
    import wave

    from backend.models.audio_capture import AudioRangeRef
    from backend.models.timeline import EvidenceLocator
    from backend.services import device_audio_ingest as ingest

    item = SimpleNamespace(
        user_id="owner",
        source_id="screenpipe-test",
        source_item_id="audio-1",
        captured_at=START,
        ended_at=START + timedelta(seconds=3),
        metadata={"direction": "input"},
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test", modality="audio", track_id="microphone"
        ),
        delete=AsyncMock(),
        media_data=b"synthetic immutable input bytes",
        media_filename="synthetic.wav",
    )

    class Query:
        def sort(self, *args):
            return self

        async def to_list(self):
            return [item]

    monkeypatch.setattr(
        ingest,
        "DeviceInputItem",
        SimpleNamespace(kind="kind", state="state", find=lambda *a: Query()),
    )
    monkeypatch.setattr(ingest, "PydanticObjectId", lambda value: value)
    monkeypatch.setattr(
        ingest,
        "User",
        SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(user_id="owner", id="owner"))
        ),
    )
    monkeypatch.setattr(ingest, "require_speech_for_transcription", lambda: False)
    monkeypatch.setattr(
        ingest, "profile_pcm_audio", lambda *a: SimpleNamespace(scored=True)
    )
    capture = AudioRangeRef(
        capture_source_id="screenpipe-test:input:microphone",
        time_basis="recorded",
        chunk_ids=["a" * 24],
        capture_session_ids=["capture"],
        started_at=START,
        ended_at=item.ended_at,
    )
    original = array.array("h", [11] * 16000 + [22] * 16000 + [33] * 16000).tobytes()

    async def mix(_items, _directory, output):
        ingest._write_wav(output, original, 16000, 1, 2)

    persisted = []

    async def persist(_user, _source, _direction, _session, path):
        from uuid import uuid4

        with wave.open(str(path), "rb") as audio:
            persisted.append(audio.readframes(audio.getnframes()))
        return SimpleNamespace(
            audio_range=capture.model_copy(update={"range_id": str(uuid4())})
        )

    async def claim(_capture, segment):
        return capture.model_copy(
            update={"started_at": segment.started_at, "ended_at": segment.ended_at}
        )

    submitted = []

    async def materialize(_user, _source, _direction, segment, reference):
        with wave.open(str(segment.path), "rb") as audio:
            submitted.append(
                (
                    reference.started_at,
                    reference.ended_at,
                    audio.readframes(audio.getnframes()),
                )
            )
        return "recording"

    monkeypatch.setattr(ingest, "_mix_session", mix)
    monkeypatch.setattr(ingest, "_persist_capture_window", persist)
    monkeypatch.setattr(ingest, "_segment_audio_range", claim)
    monkeypatch.setattr(ingest, "_ingest_segment", materialize)
    await db.privacy_screening.insert_one(
        {
            "user_id": "owner",
            "source_id": "screenpipe-test",
            "track_id": "display",
            "started_at": START,
            "ended_at": item.ended_at,
            "segments": [
                {
                    "started_at": START + timedelta(seconds=i),
                    "ended_at": START + timedelta(seconds=i + 1),
                    "state": "excluded" if i == 1 else "allowed",
                }
                for i in range(3)
            ],
        }
    )
    await ingest.process_device_audio()
    await ingest.process_device_audio()
    assert persisted == [original]
    assert [(s, e) for s, e, _ in submitted] == [
        (START, START + timedelta(seconds=1)),
        (START + timedelta(seconds=2), item.ended_at),
    ]
    assert [set(array.array("h", pcm)) for _, _, pcm in submitted] == [{11}, {33}]
    item.delete.assert_not_awaited()
    await db.privacy_overrides.insert_one(
        {
            "user_id": "owner",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": item.ended_at,
            "override": "allowed",
            "revision": 2,
        }
    )
    await ingest.process_device_audio()
    assert len(submitted) == 3
    assert set(array.array("h", submitted[-1][2])) == {22}
    item.delete.assert_awaited_once()


@pytest.mark.parametrize("cancel_commit", [False, True])
@pytest.mark.parametrize("review_kind", ["timeline", "chat"])
async def test_policy_update_waits_for_review_commit_even_when_worker_is_cancelled(
    db,
    monkeypatch,
    tmp_path,
    cancel_commit,
    review_kind,
):
    import asyncio
    import threading
    from contextlib import asynccontextmanager

    from backend.redis_keys import timeline_publication_lock
    from backend.services import chat_review
    from backend.services.timeline import accepted_context, review

    locks = {}
    waiting = asyncio.Event()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    @asynccontextmanager
    async def lock(key, **kwargs):
        if asyncio.current_task().get_name() == "policy-update":
            assert key == timeline_publication_lock("owner")
            waiting.set()
        async with locks.setdefault(key, asyncio.Lock()):
            yield

    monkeypatch.setattr(review, "distributed_lock", lock)
    monkeypatch.setattr(privacy, "distributed_lock", lock)
    monkeypatch.setattr(chat_review, "distributed_lock", lock)
    proposal = SimpleNamespace(
        id="proposal",
        user_id="owner",
        state="applying",
        save=AsyncMock(),
        changes=[],
        requested_change_ids=[],
        memory_space_id=None,
    )
    proposal.model_dump = lambda: {"user_id": "owner"}
    monkeypatch.setattr(
        review.MemoryReviewProposal, "get", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(review, "_proposal_root", lambda _: tmp_path)
    monkeypatch.setattr(review, "_audit_applied_changes", AsyncMock())
    monkeypatch.setattr(review, "_resolve_correction_predecessors", AsyncMock())
    monkeypatch.setattr(accepted_context, "queue_context_assessment", AsyncMock())

    def write(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test did not release the filesystem writer"
        (tmp_path / "synthetic-note.md").write_text("Synthetic reviewed note")
        return []

    monkeypatch.setattr(review, "_apply_review_sync", write)
    monkeypatch.setattr(chat_review, "apply_changes", write)
    if review_kind == "timeline":
        commit = review.process_memory_review_decision(proposal)
    else:
        commit = chat_review.apply(
            None,
            {
                "user_id": "owner",
                "session_id": "synthetic-chat",
                "changes": [],
                "requested_change_ids": [],
                "memory_space_id": None,
            },
            tmp_path,
            tmp_path,
        )
    task = asyncio.create_task(commit)
    update = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        if cancel_commit:
            task.cancel()
        source = SimpleNamespace(
            user_id="owner", source_id="screenpipe-test", provider="screenpipe"
        )
        update = asyncio.create_task(
            routes.submit_screening(result(), source), name="policy-update"
        )
        await asyncio.wait_for(waiting.wait(), 5)
        assert not update.done()
        current = await db.capture_sources.find_one({"source_id": source.source_id})
        assert current["privacy_revision"] == 1
        assert not current.get("privacy_updating")
        release.set()
        if cancel_commit:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            completed = await task
            assert (
                completed == "no_changes"
                if review_kind == "timeline"
                else completed["state"] == "applied"
            )
        assert (tmp_path / "synthetic-note.md").read_text() == "Synthetic reviewed note"
        assert await update == {"accepted": True}
        assert (await db.capture_sources.find_one({"source_id": source.source_id}))[
            "privacy_revision"
        ] == 2
    finally:
        release.set()
        await asyncio.gather(
            task, *([update] if update else []), return_exceptions=True
        )


@pytest.mark.parametrize("operation", ["screening", "override"])
async def test_policy_invalidation_failure_remains_held_until_exact_retry(
    db, monkeypatch, operation
):
    dirty = AsyncMock(side_effect=RuntimeError("Synthetic invalidation unavailable"))
    monkeypatch.setattr(routes.dirty_ranges, "mark_evidence_dirty", dirty)
    source = SimpleNamespace(
        user_id="owner", source_id="screenpipe-test", provider="screenpipe"
    )
    if operation == "screening":

        async def submit():
            return await routes.submit_screening(result(), source)

    else:
        body = routes.PrivacyOverride(
            source_id=source.source_id,
            started_at=START,
            ended_at=START + timedelta(seconds=10),
            revision=1,
            decision="excluded",
        )

        async def submit():
            return await routes.override_privacy(body, SimpleNamespace(user_id="owner"))

    with pytest.raises(RuntimeError, match="invalidation unavailable"):
        await submit()
    held = await db.capture_sources.find_one({"source_id": source.source_id})
    assert held["privacy_updating"] and held["privacy_revision"] == 2
    dirty.side_effect = None
    await submit()
    current = await db.capture_sources.find_one({"source_id": source.source_id})
    assert not current["privacy_updating"]
    assert current["privacy_revision"] == 2
    assert dirty.await_count == 2


@pytest.mark.parametrize(
    "change", ["unrelated", "overlap", "unfinished", "capture_hold", "during_ingest"]
)
async def test_audio_worker_revalidates_after_local_profile(db, monkeypatch, change):
    import array
    import asyncio
    import wave

    from backend.models.audio_capture import AudioRangeRef
    from backend.models.timeline import EvidenceLocator
    from backend.services import device_audio_ingest as ingest

    item = SimpleNamespace(
        user_id="owner",
        source_id="screenpipe-test",
        source_item_id="audio-1",
        captured_at=START,
        ended_at=START + timedelta(seconds=3),
        metadata={"direction": "input"},
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test", modality="audio", track_id="microphone"
        ),
        delete=AsyncMock(),
    )

    class Query:
        def sort(self, *args):
            return self

        async def to_list(self):
            return [item]

    monkeypatch.setattr(
        ingest,
        "DeviceInputItem",
        SimpleNamespace(kind="kind", state="state", find=lambda *a: Query()),
    )
    monkeypatch.setattr(ingest, "PydanticObjectId", lambda value: value)
    monkeypatch.setattr(
        ingest,
        "User",
        SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(user_id="owner", id="owner"))
        ),
    )
    monkeypatch.setattr(ingest, "require_speech_for_transcription", lambda: False)
    capture = AudioRangeRef(
        capture_source_id="screenpipe-test:input:microphone",
        time_basis="recorded",
        chunk_ids=["a" * 24],
        capture_session_ids=["capture"],
        started_at=START,
        ended_at=item.ended_at,
    )
    original = array.array("h", [11] * 16000 + [22] * 16000 + [33] * 16000).tobytes()

    async def mix(_items, _directory, output):
        ingest._write_wav(output, original, 16000, 1, 2)

    persisted = []

    async def persist(_user, _source, _direction, _session, path):
        from uuid import uuid4

        with wave.open(str(path), "rb") as audio:
            persisted.append(audio.readframes(audio.getnframes()))
        return SimpleNamespace(
            audio_range=capture.model_copy(update={"range_id": str(uuid4())})
        )

    async def claim(_capture, segment):
        return capture.model_copy(
            update={"started_at": segment.started_at, "ended_at": segment.ended_at}
        )

    submitted = []

    async def materialize(_user, _source, _direction, segment, reference):
        with wave.open(str(segment.path), "rb") as audio:
            submitted.append(
                (
                    reference.started_at,
                    reference.ended_at,
                    audio.readframes(audio.getnframes()),
                )
            )
        return "recording"

    monkeypatch.setattr(ingest, "_mix_session", mix)
    monkeypatch.setattr(ingest, "_persist_capture_window", persist)
    monkeypatch.setattr(ingest, "_segment_audio_range", claim)
    monkeypatch.setattr(ingest, "_ingest_segment", materialize)
    await db.privacy_screening.insert_one(
        {
            "user_id": "owner",
            "source_id": "screenpipe-test",
            "track_id": "display",
            "started_at": START,
            "ended_at": item.ended_at,
            "segments": [
                {
                    "started_at": START + timedelta(seconds=i),
                    "ended_at": START + timedelta(seconds=i + 1),
                    "state": "excluded" if i == 1 else "allowed",
                }
                for i in range(3)
            ],
        }
    )
    loop = asyncio.get_running_loop()
    changed = False

    async def change_policy():
        nonlocal changed
        if changed:
            return
        changed = True
        if change in {"overlap", "during_ingest"}:
            await db.privacy_overrides.insert_one(
                {
                    "user_id": "owner",
                    "source_id": "screenpipe-test",
                    "started_at": START,
                    "ended_at": item.ended_at,
                    "override": "excluded",
                    "revision": 2,
                }
            )
        if change == "capture_hold":
            await db.privacy_capture_holds.insert_one(
                {
                    "user_id": "owner",
                    "source_id": "screenpipe-test",
                    "capture_session_id": "capture",
                    "chunk_ids": ["a" * 24],
                }
            )
        if change == "unrelated":
            await db.privacy_screening.insert_one(
                {
                    "user_id": "owner",
                    "source_id": "screenpipe-test",
                    "track_id": "display",
                    "started_at": START + timedelta(days=1),
                    "ended_at": START + timedelta(days=1, seconds=1),
                    "segments": [
                        {
                            "started_at": START + timedelta(days=1),
                            "ended_at": START + timedelta(days=1, seconds=1),
                            "state": "excluded",
                        }
                    ],
                }
            )
        await db.capture_sources.update_one(
            {"source_id": "screenpipe-test"},
            {
                "$inc": {"privacy_revision": 1},
                "$set": {"privacy_updating": change == "unfinished"},
            },
        )

    def profile(*args):
        if change != "during_ingest":
            asyncio.run_coroutine_threadsafe(change_policy(), loop).result(timeout=5)
        return SimpleNamespace(scored=True)

    async def materialize_with_update(*args):
        value = await materialize(*args)
        if change == "during_ingest":
            await change_policy()
        return value

    monkeypatch.setattr(ingest, "profile_pcm_audio", profile)
    monkeypatch.setattr(ingest, "_profile_wav", profile)
    monkeypatch.setattr(ingest, "_ingest_segment", materialize_with_update)
    result = await ingest.process_device_audio()
    item.delete.assert_not_awaited()
    assert persisted == [original]
    progress = await db.privacy_audio_progress.find_one({})
    if change == "unrelated":
        assert result["processed_sessions"] == 2
        assert [(s, e) for s, e, _ in submitted] == [
            (START, START + timedelta(seconds=1)),
            (START + timedelta(seconds=2), item.ended_at),
        ]
        assert [set(array.array("h", pcm)) for _, _, pcm in submitted] == [{11}, {33}]
        assert len(progress["completed"]) == 2
        await ingest.process_device_audio()
        assert len(submitted) == 2
    else:
        assert len(submitted) == (1 if change == "during_ingest" else 0)
        assert progress is None or not progress.get("completed")
