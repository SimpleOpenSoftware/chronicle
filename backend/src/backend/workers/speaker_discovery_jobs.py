"""Reusable speech-segment embedding index for speaker corpus discovery."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from rq import get_current_job

from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import reconstruct_audio_segment

MIN_SECONDS = 3.0
MAX_SECONDS = 30.0
EMBED_CONCURRENCY = 4
SCORE_BATCH_SIZE = 500
PROGRESS_INTERVAL = 100

logger = logging.getLogger(__name__)


def _active_segments(doc: dict) -> list:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    version = next(
        (item for item in versions if item.get("version_id") == active_id), None
    )
    return (version or (versions[-1] if versions else {})).get("segments") or []


async def _speech_clips(
    user_id: str,
    include_all_users: bool,
    include_deleted: bool = False,
    *,
    visibility=None,
    stats=None,
) -> list[dict]:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    query: dict[str, Any] = {
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    if not include_deleted:
        # Soft-deleted conversations keep their audio chunks; opt in to mine them.
        query["deleted"] = {"$ne": True}
    if not include_all_users:
        query["user_id"] = user_id
    database = Conversation.get_pymongo_collection().database
    human_labels: dict[str, str] = {}
    review_query = {} if include_all_users else {"reviewed_by": user_id}
    async for review in database["enrollment_reviews"].find(
        review_query,
        {"conversation_id": 1, "segment_start": 1, "actual_speaker": 1, "enrolled": 1},
    ):
        if not await visibility.filter([review]):
            continue
        if review.get("enrolled") and review.get("actual_speaker"):
            key = f'{review["conversation_id"]}:{round(float(review["segment_start"]), 2)}'
            human_labels[key] = review["actual_speaker"]
    annotation_query: dict[str, Any] = {
        "annotation_type": "diarization",
        "status": "accepted",
        "corrected_speaker": {"$nin": [None, "", "Noise", "Unknown Speaker"]},
        "segment_start_time": {"$ne": None},
    }
    if not include_all_users:
        annotation_query["user_id"] = user_id
    async for annotation in database["annotations"].find(
        annotation_query,
        {"conversation_id": 1, "segment_start_time": 1, "corrected_speaker": 1},
    ):
        if not await visibility.filter([annotation]):
            continue
        key = (
            f'{annotation["conversation_id"]}:'
            f'{round(float(annotation["segment_start_time"]), 2)}'
        )
        human_labels[key] = annotation["corrected_speaker"]

    projection = {
        "conversation_id": 1,
        "user_id": 1,
        "title": 1,
        "created_at": 1,
        "audio_total_duration": 1,
        "active_transcript_version": 1,
        "transcript_versions": 1,
    }
    clips = []
    async for doc in Conversation.get_pymongo_collection().find(query, projection):
        if not await visibility.filter([doc]):
            if stats is not None:
                stats["privacy_held_recordings"] += 1
            continue
        audio_duration = float(doc.get("audio_total_duration") or 0)
        for index, segment in enumerate(_active_segments(doc)):
            if segment.get("segment_type") not in (None, "speech"):
                continue
            start = float(segment.get("start") or 0)
            end = min(float(segment.get("end") or 0), audio_duration or float("inf"))
            end = min(end, start + MAX_SECONDS)
            if end - start < MIN_SECONDS:
                continue
            clip_key = f'{doc["conversation_id"]}:{start:.3f}:{end:.3f}'
            review_key = f'{doc["conversation_id"]}:{round(start, 2)}'
            clips.append(
                {
                    "clip_key": clip_key,
                    "review_key": review_key,
                    "conversation_id": doc["conversation_id"],
                    "corpus_user_id": doc.get("user_id"),
                    "conversation_title": doc.get("title"),
                    "conversation_date": doc.get("created_at"),
                    "conversation_duration": audio_duration,
                    "segment_index": index,
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "text": (segment.get("text") or "")[:300],
                    "current_label": segment.get("identified_as"),
                    "human_label": human_labels.get(review_key),
                    "stored_confidence": segment.get("confidence"),
                }
            )
    await visibility.assert_current()
    return clips


def _progress(current: int, total: int, message: str) -> None:
    job = get_current_job()
    if not job:
        return
    job.meta["batch_progress"] = {
        "current": current,
        "total": total,
        "percent": round(current * 100 / total) if total else 100,
        "message": message,
    }
    job.save_meta()


@async_job(redis=False, beanie=True, timeout=14400)
async def discover_speaker_candidates_job(
    requested_by: str,
    speaker_id: str,
    speaker_name: str,
    include_all_users: bool = False,
    include_deleted: bool = False,
) -> dict:
    visibility = privacy.ConversationPrivacyFilter()
    stats = {"privacy_held_recordings": 0}
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    db = Conversation.get_pymongo_collection().database
    cache = db["speaker_corpus_embeddings"]
    matches = db["speaker_corpus_matches"]
    await cache.create_index([("clip_key", 1), ("embedding_model", 1)], unique=True)
    await matches.create_index(
        [("requested_by", 1), ("speaker_id", 1), ("review_key", 1)]
    )
    client = SpeakerRecognitionClient()
    info = await client.get_embedding_info()
    model = info.get("embedding_model")
    if not model:
        raise RuntimeError("Could not resolve speaker embedding model")

    phase_started = time.perf_counter()
    clips = await _speech_clips(
        requested_by,
        include_all_users,
        include_deleted,
        visibility=visibility,
        stats=stats,
    )
    timings["enumerate_corpus_s"] = time.perf_counter() - phase_started
    clip_keys = [clip["clip_key"] for clip in clips]
    phase_started = time.perf_counter()
    cached_embeddings = {
        row["clip_key"]: row["embedding"]
        async for row in cache.find(
            {"clip_key": {"$in": clip_keys}, "embedding_model": model},
            {"clip_key": 1, "embedding": 1},
        )
        if row.get("embedding")
    }
    await visibility.assert_current()
    timings["load_cache_s"] = time.perf_counter() - phase_started
    sem = asyncio.Semaphore(EMBED_CONCURRENCY)
    embedded: list[tuple[dict, list[float]]] = [
        (clip, cached_embeddings[clip["clip_key"]])
        for clip in clips
        if clip["clip_key"] in cached_embeddings
    ]
    missing = [clip for clip in clips if clip["clip_key"] not in cached_embeddings]
    failures = 0
    completed = len(embedded)
    progress_lock = asyncio.Lock()

    _progress(
        completed,
        len(clips),
        f"Loaded {completed}/{len(clips)} cached corpus vectors",
    )

    async def embed(clip: dict) -> None:
        nonlocal completed, failures
        async with sem:
            try:
                await visibility.assert_current()
                wav = await reconstruct_audio_segment(
                    clip["conversation_id"], clip["start"], clip["end"]
                )
                await visibility.assert_current()
                result = await client.extract_speaker_embedding(wav)
                await visibility.assert_current()
                if result.get("error") or not result.get("embedding"):
                    raise RuntimeError("Speaker embedding failed")
                async with visibility.publication():
                    await cache.update_one(
                        {"clip_key": clip["clip_key"], "embedding_model": model},
                        {
                            "$set": {
                                **clip,
                                **result,
                                "indexed_at": datetime.now(timezone.utc),
                            }
                        },
                        upsert=True,
                    )
                embedded.append((clip, result["embedding"]))
            except privacy.PrivacyHeld:
                raise
            except Exception:
                failures += 1
            finally:
                async with progress_lock:
                    completed += 1
                    if completed == len(clips) or completed % PROGRESS_INTERVAL == 0:
                        _progress(
                            completed,
                            len(clips),
                            f"Embedding new corpus speech {completed}/{len(clips)}",
                        )

    phase_started = time.perf_counter()
    results = await asyncio.gather(
        *(embed(clip) for clip in missing), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    await visibility.assert_current()
    timings["embed_missing_s"] = time.perf_counter() - phase_started
    embedded.sort(key=lambda item: item[0]["clip_key"])

    phase_started = time.perf_counter()
    async with visibility.publication():
        await matches.delete_many(
            {"requested_by": requested_by, "speaker_id": speaker_id}
        )
    scored_count = 0
    for offset in range(0, len(embedded), SCORE_BATCH_SIZE):
        batch = embedded[offset : offset + SCORE_BATCH_SIZE]
        await visibility.assert_current()
        response = await client.score_cached_embeddings(
            speaker_id, [embedding for _clip, embedding in batch]
        )
        await visibility.assert_current()
        scores = response.get("scores")
        if not isinstance(scores, list) or len(scores) != len(batch):
            raise RuntimeError("Speaker-service batch scoring failed")
        now = datetime.now(timezone.utc)
        documents = []
        for (clip, _embedding), score in zip(batch, scores):
            if score.get("sim_centroid") is None:
                continue
            documents.append(
                {
                    **clip,
                    "requested_by": requested_by,
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "embedding_model": model,
                    "scores": score,
                    "scored_at": now,
                }
            )
        if documents:
            async with visibility.publication():
                await matches.insert_many(documents, ordered=False)
            scored_count += len(documents)
        _progress(
            min(offset + len(batch), len(embedded)),
            len(embedded),
            "Comparing the selected speaker with indexed speech",
        )
    timings["score_and_store_s"] = time.perf_counter() - phase_started
    timings["total_s"] = time.perf_counter() - started_at
    timings = {name: round(seconds, 3) for name, seconds in timings.items()}
    logger.info(
        "Speaker corpus discovery timings (%d vectors, %d cached): %s",
        len(clips),
        len(cached_embeddings),
        timings,
    )

    await visibility.assert_current()
    return {
        **stats,
        "speaker_name": speaker_name,
        "speech_clips": len(clips),
        "embedded": len(embedded),
        "scored": scored_count,
        "failures": failures,
        "embedding_model": model,
        "timings": timings,
    }
