"""Guided enrollment entry points retain canonical privacy through external work."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.controllers import guided_enrollment_controller as guided
from backend.services import privacy


@pytest.fixture
async def guided_setup(evidence, monkeypatch):
    fields = dict(
        title="Synthetic title",
        audio_chunks_count=1,
        audio_total_duration=10,
        active_transcript_version="synthetic",
        transcript_versions=[
            dict(
                version_id="synthetic",
                segments=[
                    dict(
                        start=0,
                        end=10,
                        text="Synthetic transcript",
                        speaker="Synthetic speaker",
                    )
                ],
            )
        ],
    )
    await evidence.db.conversations.update_one({}, {"$set": fields})
    ordinary = {
        **evidence.row,
        **fields,
        "conversation_id": "ordinary-recording",
        "client_id": "ordinary-device",
    }
    await evidence.db.conversations.insert_one(ordinary)
    monkeypatch.setattr(
        guided,
        "Conversation",
        NS(get_pymongo_collection=lambda: evidence.db.conversations),
    )
    client = NS(
        enabled=True,
        score_enrollment_candidate=AsyncMock(
            return_value={
                "sim_centroid": 0.8,
                "max_clip_sim": 0.7,
                "best_other": {"score": 0.1},
            }
        ),
        append_to_speaker=AsyncMock(return_value={"status": "enrolled"}),
    )
    monkeypatch.setattr(guided, "SpeakerRecognitionClient", lambda: client)
    monkeypatch.setattr(guided, "reconstruct_audio_segment", evidence.reconstruct)
    gallery = dict(speaker_id="synthetic-gallery", speaker_name="Synthetic speaker")
    monkeypatch.setattr(guided, "_gallery_stats", AsyncMock(return_value=gallery))
    monkeypatch.setattr(guided, "_gallery_health", AsyncMock(return_value={}))
    monkeypatch.setattr(guided, "get_diarization_settings", lambda: {})
    queue = Mock(return_value=NS(id="synthetic-job"))
    monkeypatch.setattr(guided, "default_queue", NS(enqueue=queue))
    monkeypatch.setattr(
        guided,
        "enqueue_corpus_discovery",
        AsyncMock(return_value={"job_id": "synthetic-job"}),
    )
    return NS(
        user=NS(user_id="review-admin", is_superuser=True), client=client, queue=queue
    )


def clip(cid="synthetic-recording"):
    return dict(
        conversation_id=cid,
        start=0,
        end=10,
        duration=10,
        original_start=0,
        original_end=10,
        decision="accept",
        current_label="Synthetic speaker",
        manually_labeled=True,
        stored_confidence=0.8,
        scores={"sim_centroid": 0.8, "max_clip_sim": 0.7, "best_other": {"score": 0.1}},
    )


@pytest.mark.asyncio
async def test_candidate_pool_filters_original_owner_before_transcript(
    evidence, guided_setup, monkeypatch
):
    original = guided._active_segments

    def segments(doc):
        assert doc["conversation_id"] != "synthetic-recording"
        return original(doc)

    monkeypatch.setattr(guided, "_active_segments", segments)
    pool = await guided._candidate_pool(guided_setup.user, "Synthetic speaker", set())
    assert [r["conversation_id"] for r in pool] == ["ordinary-recording"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["entry", "decode", "provider"])
async def test_score_rechecks_before_and_after_provider(evidence, guided_setup, stage):
    if stage != "entry":
        await allow(evidence)

        async def changed(*a, **kw):
            await revoke(evidence)
            return b"synthetic audio" if stage == "decode" else {"sim_centroid": 0.8}

        target = (
            evidence.reconstruct
            if stage == "decode"
            else guided_setup.client.score_enrollment_candidate
        )
        target.side_effect = changed
    with pytest.raises(privacy.PrivacyHeld):
        await guided._score_clip(
            guided_setup.client, asyncio.Semaphore(1), clip(), "synthetic-gallery"
        )
    assert evidence.reconstruct.await_count == (stage != "entry")
    assert guided_setup.client.score_enrollment_candidate.await_count == (
        stage == "provider"
    )


@pytest.mark.asyncio
async def test_suggestion_filters_cached_discovery_before_ranking(
    evidence, guided_setup, monkeypatch
):
    monkeypatch.setattr(guided, "_candidate_pool", AsyncMock(return_value=[]))
    for cid in ["synthetic-recording", "ordinary-recording"]:
        await evidence.db.speaker_corpus_matches.insert_one(
            {
                **clip(cid),
                "requested_by": "review-admin",
                "speaker_id": "synthetic-gallery",
                "review_key": cid,
                "human_label": None,
            }
        )
    original = guided._information_score

    def rank(row, threshold):
        assert row["conversation_id"] == "ordinary-recording"
        return original(row, threshold)

    monkeypatch.setattr(guided, "_information_score", rank)
    result = await guided.suggest_clips(guided_setup.user, "Synthetic speaker")
    assert [r["conversation_id"] for r in result["batch"]] == ["ordinary-recording"]
    assert result["discovery_candidates"] == 1
    guided_setup.client.score_enrollment_candidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accept", "reject", "another_speaker", "skip"])
async def test_held_decision_never_enrolls_or_saves(evidence, guided_setup, decision):
    with pytest.raises(privacy.PrivacyHeld):
        await guided.decide_clips(
            guided_setup.user, "Synthetic speaker", [{**clip(), "decision": decision}]
        )
    evidence.reconstruct.assert_not_awaited()
    guided_setup.client.append_to_speaker.assert_not_awaited()
    assert await evidence.db.enrollment_reviews.count_documents({}) == 0
    assert await evidence.db.enrollment_batches.count_documents({}) == 0
    guided_setup.queue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["decode", "provider", "health"])
async def test_decision_discards_results_when_policy_changes(
    evidence, guided_setup, monkeypatch, stage
):
    await allow(evidence)

    async def changed(*a, **kw):
        await revoke(evidence)
        return b"synthetic audio" if stage == "decode" else {}

    if stage == "decode":
        evidence.reconstruct.side_effect = changed
    elif stage == "provider":
        guided_setup.client.append_to_speaker.side_effect = changed
    else:
        monkeypatch.setattr(guided, "_gallery_health", changed)
    with pytest.raises(privacy.PrivacyHeld):
        await guided.decide_clips(guided_setup.user, "Synthetic speaker", [clip()])
    assert guided_setup.client.append_to_speaker.await_count == (stage == "provider")
    assert await evidence.db.enrollment_reviews.count_documents({}) == 0
    assert await evidence.db.enrollment_batches.count_documents({}) == 0
    guided_setup.queue.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_enrollment_records_provenance_and_later_history_is_held(
    evidence, guided_setup
):
    await allow(evidence)
    result = await guided.decide_clips(guided_setup.user, "Synthetic speaker", [clip()])
    assert result["enrolled"] == 1
    guided_setup.client.append_to_speaker.assert_awaited_once()
    batch = await evidence.db.enrollment_batches.find_one({})
    assert batch["evidence_conversation_ids"] == ["synthetic-recording"]
    assert set(batch["privacy_revisions"]) == {"evidence-owner"}
    result = await guided.enrollment_history(guided_setup.user, "Synthetic speaker")
    assert len(result["sessions"]) == 1
    assert "privacy_revisions" not in result["sessions"][0]
    await revoke(evidence)
    assert (await guided.enrollment_history(guided_setup.user, "Synthetic speaker"))[
        "sessions"
    ] == []


@pytest.mark.asyncio
async def test_history_without_provenance_is_held(evidence, guided_setup):
    await evidence.db.enrollment_batches.insert_one(
        {"speaker_name": "Synthetic speaker", "created_at": evidence.row["created_at"]}
    )
    assert (await guided.enrollment_history(guided_setup.user, "Synthetic speaker"))[
        "sessions"
    ] == []


@pytest.mark.asyncio
async def test_unknown_clusters_omit_mixed_and_missing_evidence(evidence, guided_setup):
    for name, members in [
        ("mixed", ["synthetic-recording", "ordinary-recording"]),
        ("allowed", ["ordinary-recording"]),
        ("missing", ["missing-recording"]),
    ]:
        await evidence.db.unknown_speaker_clusters.insert_one(
            {
                "cluster_id": name,
                "status": "pending",
                "requested_by": "review-admin",
                "members": [{"conversation_id": c} for c in members],
                "evidence_conversation_ids": members,
                "privacy_revisions": {"evidence-owner": {"screenpipe-test": 1}},
            }
        )
    rows = (await guided.list_unknown_clusters(guided_setup.user))["clusters"]
    assert [r["cluster_id"] for r in rows] == ["allowed"]


@pytest.mark.asyncio
async def test_unknown_decision_checks_unselected_cluster_members(
    evidence, guided_setup
):
    await evidence.db.unknown_speaker_clusters.insert_one(
        {
            "cluster_id": "synthetic-cluster",
            "run_fingerprint": "synthetic-run",
            "status": "pending",
            "requested_by": "review-admin",
            "members": [
                dict(conversation_id=c, identity_key=c)
                for c in ["synthetic-recording", "ordinary-recording"]
            ],
        }
    )
    with pytest.raises(privacy.PrivacyHeld):
        await guided.decide_unknown_cluster(
            guided_setup.user,
            "synthetic-cluster",
            "synthetic-run",
            "confirm",
            "Synthetic speaker",
            ["ordinary-recording"],
            [{"identity_key": "ordinary-recording"}],
        )
    evidence.reconstruct.assert_not_awaited()
    assert await evidence.db.annotations.count_documents({}) == 0


@pytest.mark.asyncio
async def test_discovery_count_omits_private_cached_matches(evidence, guided_setup):
    for c in ["ordinary-recording", "synthetic-recording"]:
        await evidence.db.speaker_corpus_matches.insert_one(
            {
                "requested_by": "review-admin",
                "speaker_id": "synthetic-gallery",
                "conversation_id": c,
            }
        )
    result = await guided.corpus_discovery_state(guided_setup.user, "Synthetic speaker")
    assert result["matched_segments"] == 1


@pytest.mark.asyncio
async def test_suggestions_score_only_allowed_pool_clips(evidence, guided_setup):
    result = await guided.suggest_clips(guided_setup.user, "Synthetic speaker")
    assert [r["conversation_id"] for r in result["batch"]] == ["ordinary-recording"]
    evidence.reconstruct.assert_awaited_once_with("ordinary-recording", 0.0, 10.0)
    guided_setup.client.score_enrollment_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_suggestion_drains_scoring_siblings_before_raising_hold(
    evidence, guided_setup, monkeypatch
):
    await allow(evidence)
    monkeypatch.setattr(
        guided,
        "_candidate_pool",
        AsyncMock(return_value=[clip(), clip("ordinary-recording")]),
    )
    ready = asyncio.Event()
    completed = []

    async def score(_client, _sem, row, _speaker, *, visibility):
        if row["conversation_id"] == "synthetic-recording":
            await ready.wait()
            raise privacy.PrivacyHeld()
        ready.set()
        await asyncio.sleep(0.01)
        completed.append(True)
        return row

    monkeypatch.setattr(guided, "_score_clip", score)
    with pytest.raises(privacy.PrivacyHeld):
        await guided.suggest_clips(guided_setup.user, "Synthetic speaker")
    assert completed == [True]


@pytest.mark.asyncio
async def test_cluster_listing_discards_changed_policy(
    evidence, guided_setup, monkeypatch
):
    await allow(evidence)
    await evidence.db.unknown_speaker_clusters.insert_one(
        {
            "cluster_id": "synthetic",
            "requested_by": "review-admin",
            "status": "pending",
            "members": [clip()],
            "evidence_conversation_ids": ["synthetic-recording"],
            "privacy_revisions": {"evidence-owner": {"screenpipe-test": 1}},
        }
    )
    original = privacy.ConversationPrivacyFilter.filter

    async def change(self, rows):
        result = await original(self, rows)
        await revoke(evidence)
        return result

    monkeypatch.setattr(privacy.ConversationPrivacyFilter, "filter", change)
    with pytest.raises(privacy.PrivacyHeld):
        await guided.list_unknown_clusters(guided_setup.user)


@pytest.mark.asyncio
async def test_guided_decision_http_entry_returns_content_free_hold(
    evidence, guided_setup
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.routers.modules import data_audit_routes

    app = FastAPI()
    app.include_router(data_audit_routes.router, prefix="/api")
    app.dependency_overrides[data_audit_routes.current_active_user] = (
        lambda: guided_setup.user
    )
    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/data-audit/enrollment/guided/decide",
            json={"speaker_name": "Synthetic speaker", "decisions": [clip()]},
        )
    assert response.status_code == 423
    assert set(response.json()) <= {"detail", "error"}
    evidence.reconstruct.assert_not_awaited()
