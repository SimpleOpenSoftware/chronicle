"""Full-corpus embedding index for iterative background cluster review."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from rq import get_current_job

from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import reconstruct_audio_segment

MIN_SPEECH_SECONDS = 1.0
MAX_SPEECH_SECONDS = 15.0
MIN_GAP_SECONDS = 2.0
MAX_GAP_SECONDS = 8.0
EMBED_CONCURRENCY = 4
CORPUS_BATCH_SIZE = 64
SERVICE_READY_ATTEMPTS = 6
SERVICE_READY_DELAY_SECONDS = 2

logger = logging.getLogger(__name__)


def _active_segments(doc: dict) -> list[dict]:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else {},
    )
    return active.get("segments") or []


def _gap_windows(segments: list[dict], duration: float) -> list[tuple[float, float]]:
    occupied = sorted(
        (
            max(0.0, float(segment.get("start", 0))),
            min(duration, float(segment.get("end", 0))),
        )
        for segment in segments
        if float(segment.get("end", 0)) > float(segment.get("start", 0))
    )
    merged: list[tuple[float, float]] = []
    for start, end in occupied:
        if merged and start <= merged[-1][1] + 0.1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in [*merged, (duration, duration)]:
        while start - cursor >= MIN_GAP_SECONDS:
            window_end = min(start, cursor + MAX_GAP_SECONDS)
            windows.append((round(cursor, 3), round(window_end, 3)))
            cursor = window_end
        cursor = max(cursor, end)
    return windows


async def _corpus_batches(cursor):
    batch = []
    async for document in cursor:
        batch.append(document)
        if len(batch) == CORPUS_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


async def _admit_corpus_batch(documents):
    """Share enumeration work, retaining independent recovery on policy changes.

    This admission only enumerates candidates. Each recording still obtains a
    fresh filter before inference and retains its original claim through publication.
    """
    visibility = privacy.ConversationPrivacyFilter()
    try:
        allowed = await visibility.filter(documents)
        await visibility.assert_current()
    except privacy.PrivacyHeld:
        if len(documents) == 1:
            return [], 1
        # Discard the batch's admission entirely. An updating source must not
        # starve other devices, so retry each original document independently.
        admitted, deferred = [], 0
        for document in documents:
            rows, held = await _admit_corpus_batch([document])
            admitted.extend(rows)
            deferred += held
        return admitted, deferred
    return [
        (document, visibility.originals[document["conversation_id"]])
        for document in allowed
    ], 0


async def _corpus_candidates(
    requested_by: str,
) -> tuple[list[tuple[dict, list[dict]]], int]:
    query = {
        "user_id": requested_by,
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    projection = {
        "conversation_id": 1,
        "title": 1,
        "created_at": 1,
        "audio_total_duration": 1,
        "active_transcript_version": 1,
        "transcript_versions": 1,
    }
    recordings = []
    deferred = 0
    cursor = Conversation.get_pymongo_collection().find(query, projection)
    async for batch in _corpus_batches(cursor):
        admitted, held = await _admit_corpus_batch(batch)
        deferred += held
        for doc, original in admitted:
            conversation_id = doc["conversation_id"]
            candidates: list[dict] = []
            duration = float(doc.get("audio_total_duration") or 0.0)
            segments = _active_segments(doc)
            for segment_index, segment in enumerate(segments):
                if segment.get("segment_type", "speech") != "speech":
                    continue
                start = float(segment.get("start", 0))
                end = min(
                    float(segment.get("end", 0)), start + MAX_SPEECH_SECONDS, duration
                )
                if end - start < MIN_SPEECH_SECONDS:
                    continue
                candidates.append(
                    {
                        "clip_key": f"{conversation_id}:{start:.3f}:{end:.3f}:speech",
                        "conversation_id": conversation_id,
                        "conversation_title": doc.get("title") or conversation_id[:8],
                        "conversation_date": doc.get("created_at"),
                        "segment_index": segment_index,
                        "start": start,
                        "end": end,
                        "duration": end - start,
                        "text": (segment.get("text") or "")[:300],
                        "candidate_type": "background_speech",
                        "current_label": segment.get("identified_as")
                        or segment.get("speaker"),
                        "stored_confidence": segment.get("confidence"),
                    }
                )
            for start, end in _gap_windows(segments, duration):
                candidates.append(
                    {
                        "clip_key": f"{conversation_id}:{start:.3f}:{end:.3f}:noise",
                        "conversation_id": conversation_id,
                        "conversation_title": doc.get("title") or conversation_id[:8],
                        "conversation_date": doc.get("created_at"),
                        "segment_index": -1,
                        "start": start,
                        "end": end,
                        "duration": end - start,
                        "text": "",
                        "candidate_type": "noise",
                        "current_label": None,
                        "stored_confidence": None,
                    }
                )
            recordings.append((original, candidates))
    return recordings, deferred


def _progress(current: int, total: int, message: str) -> None:
    job = get_current_job()
    if not job:
        return
    job.meta["batch_progress"] = {
        "current": current,
        "done": current,
        "total": total,
        "percent": round(current * 100 / total) if total else 100,
        "message": message,
    }
    job.save_meta()


async def _wait_for_embedding_model(client: SpeakerRecognitionClient) -> str:
    """Tolerate the speaker service restarting while a corpus job is queued."""
    last_info: dict = {}
    for attempt in range(SERVICE_READY_ATTEMPTS):
        last_info = await client.get_embedding_info()
        model = last_info.get("embedding_model")
        if model:
            return model
        if attempt < SERVICE_READY_ATTEMPTS - 1:
            _progress(0, 0, "Waiting for speaker recognition…")
            await asyncio.sleep(SERVICE_READY_DELAY_SECONDS)
    raise RuntimeError("Could not resolve speaker embedding model")


@async_job(redis=False, beanie=True, timeout=14400)
async def index_background_corpus_job(requested_by: str, source_revision: str) -> dict:
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    database = Conversation.get_pymongo_collection().database
    cache = database["background_corpus_embeddings"]
    await cache.create_index(
        [("requested_by", 1), ("clip_key", 1), ("embedding_model", 1)], unique=True
    )
    client = SpeakerRecognitionClient()
    model = await _wait_for_embedding_model(client)

    phase_started = time.perf_counter()
    recordings, held_recordings = await _corpus_candidates(requested_by)
    timings["enumerate_corpus_s"] = time.perf_counter() - phase_started
    job = get_current_job()
    run_id = job.id if job else datetime.now(timezone.utc).isoformat()
    total = sum(len(candidates) for _, candidates in recordings)
    semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)
    progress_lock = asyncio.Lock()
    completed = cached_count = embedded_count = failures = 0

    async def index_recording(original: dict, candidates: list[dict]) -> None:
        nonlocal completed, cached_count, embedded_count, failures, held_recordings
        async with semaphore:
            # The first admission identifies the exact capture used to enumerate
            # candidate offsets. Re-reading cannot silently retarget those offsets.
            visibility = privacy.ConversationPrivacyFilter()
            cid = original["conversation_id"]
            visibility.originals[cid] = original
            try:
                if not await visibility.filter([{"conversation_id": cid}]):
                    raise privacy.PrivacyHeld()
                receipt = await visibility.reference_receipt(
                    requested_by, conversation_ids=[cid]
                )
                cached_rows = await visibility.filter_embeddings(
                    [
                        row
                        async for row in cache.find(
                            {
                                "requested_by": requested_by,
                                "conversation_id": cid,
                                "embedding_model": model,
                                "embedding": {"$exists": True, "$ne": None},
                            }
                        )
                    ]
                )
                cached = {row["clip_key"]: row for row in cached_rows}
                await visibility.assert_current()
                for candidate in candidates:
                    try:
                        # Also detect a rewritten capture claim before reconstructing
                        # or attaching fresh metadata to a previously cached vector.
                        if not await visibility.filter([{"conversation_id": cid}]):
                            raise privacy.PrivacyHeld()
                        await visibility.assert_current()
                        hit = cached.get(candidate["clip_key"])
                        if hit:
                            values = {
                                "run_id": run_id,
                                **candidate,
                                "privacy_reference_receipt": sorted(
                                    set(receipt) | set(hit["privacy_reference_receipt"])
                                ),
                            }
                            query = {"_id": hit["_id"]}
                        else:
                            wav = await reconstruct_audio_segment(
                                cid, candidate["start"], candidate["end"]
                            )
                            if not await visibility.filter([{"conversation_id": cid}]):
                                raise privacy.PrivacyHeld()
                            await visibility.assert_current()
                            result = await client.extract_speaker_embedding(wav)
                            await visibility.assert_current()
                            if (
                                result.get("error")
                                or not result.get("embedding")
                                or result.get("embedding_model") != model
                            ):
                                raise RuntimeError("Speaker embedding unavailable")
                            values = {
                                **candidate,
                                **result,
                                "privacy_reference_receipt": receipt,
                                "requested_by": requested_by,
                                "run_id": run_id,
                                "indexed_at": datetime.now(timezone.utc),
                            }
                            query = {
                                "requested_by": requested_by,
                                "clip_key": candidate["clip_key"],
                                "embedding_model": model,
                            }
                        async with visibility.publication():
                            # Reject capture rewrites during inference as well as
                            # policy changes. Publication owns the original owners' locks.
                            if not await visibility.filter([{"conversation_id": cid}]):
                                raise privacy.PrivacyHeld()
                            await cache.update_one(
                                query, {"$set": values}, upsert=not bool(hit)
                            )
                        if hit:
                            cached_count += 1
                        else:
                            embedded_count += 1
                    except privacy.PrivacyHeld:
                        raise
                    except Exception:
                        failures += 1
            except privacy.PrivacyHeld:
                held_recordings += 1
            finally:
                async with progress_lock:
                    completed += len(candidates)
                    _progress(
                        completed,
                        total,
                        f"Indexed {cached_count + embedded_count}/{total} vectors; "
                        f"{held_recordings} recordings held",
                    )

    phase_started = time.perf_counter()
    # Drain every independent recording. A hold discards that recording's pending
    # results without starving recordings whose original evidence remains allowed.
    outcomes = await asyncio.gather(
        *(index_recording(original, candidates) for original, candidates in recordings),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    timings["embed_missing_s"] = time.perf_counter() - phase_started
    if held_recordings:
        # Successful per-recording cache entries are durable and can be resumed.
        # Do not stamp the full corpus current or delete untouched cache entries.
        raise privacy.PrivacyHeld()
    phase_started = time.perf_counter()
    if not failures:
        await cache.delete_many(
            {
                "requested_by": requested_by,
                "embedding_model": model,
                "run_id": {"$ne": run_id},
            }
        )
    counts = {
        kind: await cache.count_documents(
            {
                "requested_by": requested_by,
                "embedding_model": model,
                "candidate_type": kind,
            }
        )
        for kind in ("noise", "background_speech")
    }
    await database["background_cluster_cache"].delete_many(
        {"requested_by": requested_by}
    )
    if not failures:
        await database["background_index_runs"].update_one(
            {"requested_by": requested_by},
            {
                "$set": {
                    "source_revision": source_revision,
                    "indexed_at": datetime.now(timezone.utc),
                }
            },
        )
    timings["finalize_s"] = time.perf_counter() - phase_started
    timings["total_s"] = time.perf_counter() - started_at
    timings = {name: round(seconds, 3) for name, seconds in timings.items()}
    logger.info(
        "Background corpus index timings (%d vectors, %d cached): %s",
        total,
        cached_count,
        timings,
    )
    return {
        "embedding_model": model,
        "candidates": counts,
        "total": sum(counts.values()),
        "cached": cached_count,
        "embedded": embedded_count,
        "failures": failures,
        "timings": timings,
    }
