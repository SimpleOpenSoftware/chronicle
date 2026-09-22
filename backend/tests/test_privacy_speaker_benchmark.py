"""Benchmark workers and saved reports must retain their original evidence policy."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401
from test_privacy_guided_enrollment import guided_setup  # noqa: F401

from backend.controllers import guided_enrollment_controller as guided
from backend.models import job as job_model
from backend.services import privacy
from backend.workers import speaker_benchmark_jobs as benchmark


@pytest.fixture
async def benchmarking(evidence, guided_setup, monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "Conversation",
        NS(get_pymongo_collection=lambda: evidence.db.conversations),
    )
    monkeypatch.setattr(benchmark, "get_current_job", lambda: None)
    monkeypatch.setattr(job_model, "_ensure_beanie_initialized", AsyncMock())
    monkeypatch.setattr(benchmark, "get_diarization_settings", lambda: {})
    client = NS(
        get_embedding_info=AsyncMock(
            return_value={"embedding_model": "synthetic-model"}
        ),
        extract_speaker_embedding=AsyncMock(return_value={"embedding": [1.0, 0.0]}),
    )
    monkeypatch.setattr(benchmark, "SpeakerRecognitionClient", lambda: client)
    monkeypatch.setattr(benchmark, "reconstruct_audio_segment", evidence.reconstruct)
    for cid in ["synthetic-recording", "ordinary-recording"]:
        await evidence.db.enrollment_reviews.insert_one(
            {
                "reviewed_by": "review-admin",
                "conversation_id": cid,
                "decision": "accept",
                "actual_speaker": "Synthetic speaker",
                "selected_start": 0,
                "selected_end": 10,
            }
        )
    return NS(client=client, user=NS(user_id="review-admin"))


async def run():
    return await asyncio.to_thread(benchmark.run_speaker_benchmark_job, "review-admin")


@pytest.mark.asyncio
async def test_registered_worker_omits_private_review_and_keeps_queue_result_content_free(
    evidence, benchmarking
):
    result = await run()
    assert set(result) == {"status", "report_id"} and result["status"] == "complete"
    evidence.reconstruct.assert_awaited_once_with("ordinary-recording", 0.0, 10.0)
    saved = await evidence.db.speaker_benchmark_runs.find_one({})
    assert saved["dataset"]["exclusions"]["privacy_held"] == 1
    assert saved["evidence_conversation_ids"] == ["ordinary-recording"]
    assert "evidence-owner" in saved["privacy_revisions"]
    response = await guided.latest_benchmark(benchmarking.user)
    assert response["report"]["dataset"]["embedded_clips"] == 1
    assert "privacy_revisions" not in response["report"]
    assert "evidence_conversation_ids" not in response["report"]


@pytest.mark.asyncio
async def test_private_annotation_is_filtered_before_transcript_access(
    evidence, benchmarking, monkeypatch
):
    await evidence.db.enrollment_reviews.delete_many({})
    await evidence.db.annotations.insert_one(
        {
            "user_id": "review-admin",
            "annotation_type": "diarization",
            "status": "accepted",
            "conversation_id": "synthetic-recording",
            "segment_index": 0,
            "corrected_speaker": "Synthetic speaker",
        }
    )

    def forbidden(_):
        raise AssertionError("Private transcript reached benchmark")

    monkeypatch.setattr(benchmark, "_active_segments", forbidden)
    await run()
    evidence.reconstruct.assert_not_awaited()
    saved = await evidence.db.speaker_benchmark_runs.find_one({})
    assert saved["dataset"]["exclusions"]["privacy_held"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["decode", "embedding", "evaluate"])
async def test_revision_change_stops_worker_publication(
    evidence, benchmarking, monkeypatch, stage
):
    await allow(evidence)
    await evidence.db.enrollment_reviews.delete_one(
        {"conversation_id": "ordinary-recording"}
    )

    async def changed(*a, **kw):
        await revoke(evidence)
        return b"synthetic audio" if stage == "decode" else {"embedding": [1.0, 0.0]}

    if stage == "decode":
        evidence.reconstruct.side_effect = changed
    elif stage == "embedding":
        benchmarking.client.extract_speaker_embedding.side_effect = changed
    else:
        original = benchmark._evaluate

        def evaluate(*a, **kw):
            asyncio.run(revoke(evidence))
            return original(*a, **kw)

        monkeypatch.setattr(benchmark, "_evaluate", evaluate)
    with pytest.raises(privacy.PrivacyHeld):
        await run()
    assert await evidence.db.speaker_benchmark_runs.count_documents({}) == 0
    assert benchmarking.client.extract_speaker_embedding.await_count == (
        stage != "decode"
    )


@pytest.mark.asyncio
async def test_allowed_embedding_cache_reused_and_changed_model_recomputed(
    evidence, benchmarking
):
    await run()
    evidence.reconstruct.reset_mock()
    benchmarking.client.extract_speaker_embedding.reset_mock()
    await run()
    evidence.reconstruct.assert_not_awaited()
    benchmarking.client.extract_speaker_embedding.assert_not_awaited()
    benchmarking.client.get_embedding_info.return_value = {
        "embedding_model": "synthetic-model-v2"
    }
    await run()
    evidence.reconstruct.assert_awaited_once()
    assert await evidence.db.speaker_evaluation_embeddings.count_documents({}) == 2


@pytest.mark.asyncio
async def test_cached_private_vector_never_reaches_evaluation(
    evidence, benchmarking, monkeypatch
):
    await evidence.db.speaker_evaluation_embeddings.insert_one(
        {
            "user_id": "review-admin",
            "clip_key": "synthetic-recording:0.000:10.000:Synthetic speaker",
            "embedding_model": "synthetic-model",
            "embedding": [0.0, 1.0],
        }
    )
    original = benchmark._evaluate

    def evaluate(rows, *a):
        assert [r["conversation_id"] for r in rows] == ["ordinary-recording"]
        return original(rows, *a)

    monkeypatch.setattr(benchmark, "_evaluate", evaluate)
    await run()


@pytest.mark.asyncio
async def test_saved_report_becomes_held_after_source_exclusion(evidence, benchmarking):
    await allow(evidence)
    await run()
    assert (await guided.latest_benchmark(benchmarking.user))["report"]
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await guided.latest_benchmark(benchmarking.user)


@pytest.mark.asyncio
async def test_unproven_saved_report_is_held(evidence, benchmarking):
    await evidence.db.speaker_benchmark_runs.insert_one(
        {
            "user_id": "review-admin",
            "created_at": evidence.row["created_at"],
            "fold_groups": {"1": ["synthetic-recording"]},
        }
    )
    with pytest.raises(privacy.PrivacyHeld):
        await guided.latest_benchmark(benchmarking.user)


@pytest.mark.asyncio
async def test_http_report_route_returns_content_free_hold(evidence, benchmarking):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.routers.modules import data_audit_routes

    await evidence.db.speaker_benchmark_runs.insert_one(
        {"user_id": "review-admin", "created_at": evidence.row["created_at"]}
    )
    app = FastAPI()
    app.include_router(data_audit_routes.router, prefix="/api")
    app.dependency_overrides[data_audit_routes.current_active_user] = (
        lambda: benchmarking.user
    )
    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/data-audit/enrollment/benchmark/latest")
    assert response.status_code == 423 and set(response.json()) <= {"detail", "error"}


@pytest.mark.asyncio
async def test_provider_failure_details_are_not_retained(evidence, benchmarking):
    benchmarking.client.extract_speaker_embedding.side_effect = RuntimeError(
        "Synthetic sensitive diagnostic"
    )
    await run()
    row = await evidence.db.speaker_benchmark_runs.find_one({})
    assert row["failures"][0]["error"] == "RuntimeError"
    assert "Synthetic sensitive diagnostic" not in str(row)
