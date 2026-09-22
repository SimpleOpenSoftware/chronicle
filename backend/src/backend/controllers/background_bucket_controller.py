"""Background clip bucket — a growing set of user-confirmed background exemplars.

Background (TV, media, room noise, ambient chatter of no known speaker) is not
one voiceprint and has no single centroid: it is heterogeneous. So instead of a
gallery we keep a *bucket* of exemplar embeddings and classify a new clip by its
MAX cosine similarity to any exemplar (non-parametric / k-NN). The bucket starts
from confirmed ``Noise`` and ``Background Speech`` decisions and grows each time a user
confirms another background clip in Data Audit — so separation improves over time
as coverage of background *types* fills in.

Matching a lone bucket is weak until it is dense, so a candidate's
"background likelihood" also folds in two orthogonal signals validated on
annotated data: a low SNR (distant/quiet bed vs close foreground) and the
absence of any enrolled-speaker match. See ``background_bucket_controller`` docs
and the two-axis (speaker × channel-condition) design.
"""

import asyncio
import hashlib
import io
import json
import logging
import re
import wave
from datetime import datetime, timezone
from typing import Literal, Optional

import numpy as np
from bson import ObjectId
from fastapi.responses import JSONResponse
from rq.exceptions import NoSuchJobError
from rq.job import Job

from backend.constants import BACKGROUND_SPEECH_LABEL, NOISE_LABEL
from backend.controllers.queue_controller import JOB_RESULT_TTL, default_queue
from backend.models.conversation import Conversation
from backend.services import privacy
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.users import User
from backend.utils.audio_chunk_utils import reconstruct_audio_segment
from backend.workers.background_benchmark import build_background_benchmark
from backend.workers.background_cleanup_jobs import (
    apply_background_cleanup_job,
    build_background_cleanup_report,
    require_cleanup_report,
)
from backend.workers.background_index_jobs import index_background_corpus_job
from backend.workers.background_suppression import zone_for

logger = logging.getLogger(__name__)


async def _current_cache_privacy(cached, visibility):
    try:
        await visibility.require_receipt(cached.get("privacy_revisions"))
        await visibility.require_reference_receipt(
            cached["requested_by"], cached.get("privacy_reference_receipt")
        )
    except privacy.PrivacyHeld:
        return False
    return True


MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 15.0
SCAN_LIMIT = 40  # max unknown segments embedded per suggest call
BucketType = Literal["noise", "background_speech"]
BUCKET_TYPES: tuple[BucketType, ...] = ("noise", "background_speech")
# background-likelihood blend: how much each orthogonal signal contributes
W_BUCKET, W_SNR = 0.6, 0.4
SNR_FLOOR_DB = 15.0  # SNR at/below which a clip looks fully like a background bed
CLUSTER_SIMILARITY = 0.72
FOREGROUND_SIMILARITY = 0.72
HARD_CASE_TARGET_CONFIDENCE = 0.62
# harvest lane: unreviewed clips this close to a confirmed background exemplar
# are queued as one batch-confirmable group per conversation
HARVEST_SIMILARITY = 0.55
HARVEST_MARGIN = 0.25
# novelty lane: clips unlike every labelled reference (background OR foreground)
# — surfaces new background types k-NN mining cannot rank (cold start)
NOVELTY_FOREGROUND_MAX = 0.22
NOVELTY_BACKGROUND_MAX = 0.45
NOVELTY_MIN_CLIPS = 4
MIN_CLUSTER_SIZE = 3

# Review-queue "show less / default / show more" dial. Only tunes what the
# REVIEW surfaces — production suppression thresholds live in
# workers/background_suppression.py and are not affected.
SURFACE_PROFILES: dict[str, dict[str, float | int]] = {
    "less": {
        "harvest_similarity": 0.62,
        "harvest_margin": 0.30,
        "novelty_foreground_max": 0.18,
        "novelty_background_max": 0.40,
        "novelty_min_clips": 5,
        "min_cluster_size": 4,
    },
    "default": {
        "harvest_similarity": HARVEST_SIMILARITY,
        "harvest_margin": HARVEST_MARGIN,
        "novelty_foreground_max": NOVELTY_FOREGROUND_MAX,
        "novelty_background_max": NOVELTY_BACKGROUND_MAX,
        "novelty_min_clips": NOVELTY_MIN_CLIPS,
        "min_cluster_size": MIN_CLUSTER_SIZE,
    },
    "more": {
        "harvest_similarity": 0.45,
        "harvest_margin": 0.15,
        "novelty_foreground_max": 0.30,
        "novelty_background_max": 0.55,
        "novelty_min_clips": 3,
        "min_cluster_size": 2,
    },
}


def _bucket_collection():
    return Conversation.get_pymongo_collection().database["background_clips"]


def _annotations_collection():
    return Conversation.get_pymongo_collection().database["annotations"]


def _corpus_collection():
    return Conversation.get_pymongo_collection().database[
        "background_corpus_embeddings"
    ]


def _index_runs_collection():
    return Conversation.get_pymongo_collection().database["background_index_runs"]


def _cluster_reviews_collection():
    return Conversation.get_pymongo_collection().database["background_cluster_reviews"]


def _cluster_cache_collection():
    return Conversation.get_pymongo_collection().database["background_cluster_cache"]


def _foreground_collection():
    return Conversation.get_pymongo_collection().database["background_foreground_clips"]


def _job_status(job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    try:
        status = Job.fetch(job_id, connection=default_queue.connection).get_status(
            refresh=True
        )
        return status.value if hasattr(status, "value") else str(status)
    except NoSuchJobError:
        return "expired"


def _is_known_foreground(row: dict) -> bool:
    """Known people never enter a bulk background decision."""
    label = str(row.get("current_label") or "").strip()
    if not label or label in {NOISE_LABEL, BACKGROUND_SPEECH_LABEL}:
        return False
    return re.fullmatch(r"(?:unknown\s+)?speaker\s*\d+", label, re.IGNORECASE) is None


def _content_signature(row: dict) -> str:
    """Stable identity shared by duplicate imports of the same logical clip."""
    kind = row.get("candidate_type") or ""
    start = round(float(row.get("start") or 0), 2)
    end = round(float(row.get("end") or 0), 2)
    text = " ".join(str(row.get("text") or "").lower().split())
    if text:
        material = f"{kind}|{start}|{end}|{text}"
    else:
        embedding = ",".join(
            f"{float(value):.3f}" for value in row.get("embedding", [])
        )
        material = f"{kind}|{end - start:.2f}|{embedding}"
    return hashlib.sha256(material.encode()).hexdigest()


def _clip_key(conversation_id: str, start: float) -> str:
    return f"{conversation_id}:{round(start, 3)}"


def _wav_snr_db(wav_bytes: bytes) -> Optional[float]:
    """Crude blind SNR: spread between loud (90th pct) and quiet (10th pct) frame
    levels. High = a close foreground talker over quiet; low = a steady bed."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            n, sr = w.getnframes(), w.getframerate()
            raw = w.readframes(n)
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if y.size < sr // 20:  # <50ms
            return None
        frame = max(1, sr // 100)  # 10ms frames
        trimmed = y[: (y.size // frame) * frame].reshape(-1, frame)
        rms = np.sqrt((trimmed**2).mean(axis=1) + 1e-12)
        lvl = 20 * np.log10(rms + 1e-9)
        return float(np.percentile(lvl, 90) - np.percentile(lvl, 10))
    except Exception as e:  # noqa: BLE001 - degrade gracefully, SNR is optional
        logger.warning("background bucket: SNR computation failed: %s", e)
        return None


async def _embed_clip(
    speaker_client: SpeakerRecognitionClient,
    conversation_id: str,
    start: float,
    end: float,
    *,
    visibility: privacy.ConversationPrivacyFilter | None = None,
) -> Optional[dict]:
    """Reconstruct a clip, embed it, and measure its SNR. None if unusable."""
    visibility = visibility or privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    end = max(end, start + MIN_CLIP_SECONDS)
    end = min(end, start + MAX_CLIP_SECONDS)
    try:
        wav = await reconstruct_audio_segment(conversation_id, start, end)
    except privacy.PrivacyHeld:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "background bucket: reconstruct failed (%s)",
            type(e).__name__,
        )
        return None
    await visibility.assert_current()
    if not wav:
        return None
    result = await speaker_client.extract_speaker_embedding(wav)
    await visibility.assert_current()
    if result.get("error") or "embedding" not in result:
        logger.warning(
            "background bucket: embedding unavailable",
        )
        return None
    return {
        "embedding": result["embedding"],
        "embedding_model": result.get("embedding_model"),
        "snr_db": _wav_snr_db(wav),
        "duration": result.get("duration"),
    }


async def add_background_clip(
    conversation_id: str,
    start: float,
    end: float,
    bucket_type: BucketType,
    source: str = "manual",
    user: User | None = None,
) -> Optional[dict]:
    """Embed one background clip and upsert it into the bucket (dedup by conv+start)."""
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return None
    visibility = privacy.ConversationPrivacyFilter()
    measured = await _embed_clip(
        speaker_client, conversation_id, start, end, visibility=visibility
    )
    if measured is None:
        return None
    doc = {
        "conversation_id": conversation_id,
        "segment_start": round(start, 3),
        "segment_end": round(end, 3),
        "bucket_type": bucket_type,
        "user_id": str(user.user_id) if user else None,
        "privacy_reference_receipt": await visibility.reference_receipt(
            (
                str(user.user_id)
                if user
                else visibility.originals[conversation_id]["user_id"]
            ),
            conversation_ids=[conversation_id],
        ),
        "embedding": measured["embedding"],
        "embedding_model": measured["embedding_model"],
        "snr_db": measured["snr_db"],
        "source": source,
        "added_by": str(user.id) if user else None,
        "created_at": datetime.now(timezone.utc),
    }
    await visibility.assert_current()
    await _bucket_collection().update_one(
        {
            "conversation_id": conversation_id,
            "segment_start": round(start, 3),
            "bucket_type": bucket_type,
            "user_id": str(user.user_id) if user else None,
        },
        {"$set": doc},
        upsert=True,
    )
    await visibility.assert_current()
    return doc


async def seed_from_annotations(user: Optional[User] = None) -> dict:
    """Populate both buckets from accepted background annotations."""
    query = {
        "annotation_type": "diarization",
        "corrected_speaker": {"$in": [NOISE_LABEL, BACKGROUND_SPEECH_LABEL]},
        "status": "accepted",
        "user_id": user.user_id if user else None,
    }
    existing = {
        _clip_key(d["conversation_id"], d["segment_start"])
        async for d in _bucket_collection().find(
            {"user_id": str(user.user_id) if user else None},
            {"conversation_id": 1, "segment_start": 1, "bucket_type": 1},
        )
    }
    added, skipped, failed, privacy_held = 0, 0, 0, 0
    async for a in _annotations_collection().find(query):
        cid, t = a.get("conversation_id"), a.get("segment_start_time")
        if cid is None or t is None:
            failed += 1
            continue
        bucket_type: BucketType = (
            "background_speech"
            if a.get("corrected_speaker") == BACKGROUND_SPEECH_LABEL
            else "noise"
        )
        if _clip_key(cid, t) in existing:
            skipped += 1
            continue
        # use the annotated segment's real end if we can find it, else a short window
        try:
            end = await _segment_end_for(cid, t)
            doc = await add_background_clip(
                cid,
                float(t),
                end,
                bucket_type=bucket_type,
                source="annotation",
                user=user,
            )
        except privacy.PrivacyHeld:
            privacy_held += 1
            continue
        if doc is None:
            failed += 1
        else:
            added += 1
    user_filter = {"user_id": str(user.user_id) if user else None}
    sizes = {
        kind: await _bucket_collection().count_documents(
            {**user_filter, "bucket_type": kind}
        )
        for kind in BUCKET_TYPES
    }
    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "privacy_held": privacy_held,
        "bucket_sizes": sizes,
    }


async def _segment_end_for(
    conversation_id: str, start: float, default_len: float = 4.0
) -> float:
    visibility = privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    doc = await Conversation.get_pymongo_collection().find_one(
        {"conversation_id": conversation_id},
        {"transcript_versions": 1, "active_transcript_version": 1},
    )
    await visibility.assert_current()
    if not doc:
        return start + default_len
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next(
        (v for v in versions if v.get("version_id") == active_id),
        versions[-1] if versions else {},
    )
    for s in active.get("segments") or []:
        if abs(float(s.get("start", -1)) - start) <= 0.05:
            return float(s.get("end", start + default_len))
    return start + default_len


async def _bucket_matrix(
    user: User,
    bucket_type: BucketType,
    embedding_model: Optional[str],
    *,
    visibility: privacy.ConversationPrivacyFilter | None = None,
) -> tuple[np.ndarray, list[dict]]:
    """Load bucket exemplars for the given model as a unit-normalized matrix + meta."""
    query: dict = {"user_id": str(user.user_id), "bucket_type": bucket_type}
    if embedding_model:
        query["embedding_model"] = embedding_model
    rows = [d async for d in _bucket_collection().find(query)]
    visibility = visibility or privacy.ConversationPrivacyFilter()
    rows = await visibility.filter_embeddings(rows)
    await visibility.assert_current()
    if not rows:
        return np.empty((0, 0)), []
    M = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
    meta = [
        {
            "conversation_id": r["conversation_id"],
            "segment_start": r["segment_start"],
            "source": r.get("source"),
        }
        for r in rows
    ]
    return M, meta


def _max_similarity(
    query_embs: np.ndarray, bucket: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return (max cosine to any bucket exemplar, index of that exemplar) per query."""
    if bucket.shape[0] == 0:
        return np.zeros(query_embs.shape[0]), np.full(query_embs.shape[0], -1)
    q = query_embs / (np.linalg.norm(query_embs, axis=1, keepdims=True) + 1e-9)
    sims = q @ bucket.T
    return sims.max(axis=1), sims.argmax(axis=1)


async def match_embeddings(
    user: User,
    embeddings: list[list[float]],
    bucket_type: BucketType,
    embedding_model: Optional[str] = None,
    *,
    visibility: privacy.ConversationPrivacyFilter | None = None,
) -> dict:
    """Score query embeddings against the background bucket by max similarity."""
    visibility = visibility or privacy.ConversationPrivacyFilter()
    bucket, meta = await _bucket_matrix(
        user, bucket_type, embedding_model, visibility=visibility
    )
    if not embeddings:
        return {"bucket_size": len(meta), "results": []}
    q = np.asarray(embeddings, dtype=np.float32)
    max_sim, arg = _max_similarity(q, bucket)
    results = []
    for i in range(len(embeddings)):
        j = int(arg[i])
        results.append(
            {
                "bucket_similarity": round(float(max_sim[i]), 4),
                "nearest_exemplar": meta[j] if j >= 0 else None,
            }
        )
    await visibility.assert_current()
    return {"bucket_size": len(meta), "results": results}


def _background_likelihood(bucket_sim: float, snr_db: Optional[float]) -> float:
    snr_bg = (
        0.0
        if snr_db is None
        else max(0.0, min(1.0, (SNR_FLOOR_DB - snr_db) / SNR_FLOOR_DB))
    )
    return round(W_BUCKET * max(0.0, bucket_sim) + W_SNR * snr_bg, 4)


def _gap_windows(segments: list[dict], duration: float) -> list[tuple[float, float]]:
    """Return 2–8 second windows where the transcript has no segment at all."""
    occupied = sorted(
        (max(0.0, float(s.get("start", 0))), min(duration, float(s.get("end", 0))))
        for s in segments
        if float(s.get("end", 0)) > float(s.get("start", 0))
    )
    merged: list[tuple[float, float]] = []
    for start, end in occupied:
        if merged and start <= merged[-1][1] + 0.1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in [*merged, (duration, duration)]:
        while start - cursor >= 2.0:
            window_end = min(start, cursor + 8.0)
            gaps.append((round(cursor, 3), round(window_end, 3)))
            cursor = window_end
        cursor = max(cursor, end)
    return gaps


async def _score_candidates(
    user: User,
    collected: list[dict],
    model_id: str | None,
    *,
    visibility: privacy.ConversationPrivacyFilter | None = None,
) -> dict:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    collected = await visibility.filter(collected)
    matrices = {
        kind: await _bucket_matrix(user, kind, model_id, visibility=visibility)
        for kind in BUCKET_TYPES
    }
    await visibility.assert_current()
    sizes = {kind: len(meta) for kind, (_, meta) in matrices.items()}
    if not collected:
        return {"bucket_sizes": sizes, "candidates": []}
    q = np.asarray([c["embedding"] for c in collected], dtype=np.float32)
    similarities = {
        kind: _max_similarity(q, matrix)[0] for kind, (matrix, _) in matrices.items()
    }
    scored: dict[BucketType, list[tuple[float, dict]]] = {
        kind: [] for kind in BUCKET_TYPES
    }
    for i, candidate in enumerate(collected):
        kind: BucketType = candidate["candidate_type"]
        sim = float(similarities[kind][i])
        likelihood = _background_likelihood(sim, candidate["snr_db"])
        result = {
            key: value
            for key, value in candidate.items()
            if key not in {"embedding", "embedding_model"}
        }
        result.update(
            {
                "bucket_similarity": round(sim, 4),
                "bucket_similarities": {
                    bucket_kind: round(float(values[i]), 4)
                    for bucket_kind, values in similarities.items()
                },
                "background_likelihood": likelihood,
            }
        )
        scored[kind].append((likelihood, result))
    for values in scored.values():
        values.sort(key=lambda pair: pair[0], reverse=True)
    return {"bucket_sizes": sizes, "scored": scored}


async def suggest_background_candidates(
    user: User, conversation_id: str, limit: int = 10
) -> JSONResponse | dict:
    """Rank a conversation's unknown segments by how likely they are background.

    Signal 1: max similarity to the background bucket (grows as users confirm more).
    Signal 2: low SNR (channel condition — a distant/quiet bed, not close speech).
    Only unidentified speech segments (no enrolled-speaker match) are considered.
    """
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    doc = await Conversation.get_pymongo_collection().find_one(
        {"conversation_id": conversation_id, "user_id": user.user_id},
        {"transcript_versions": 1, "active_transcript_version": 1},
    )
    if not doc:
        return JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    visibility = privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next(
        (v for v in versions if v.get("version_id") == active_id),
        versions[-1] if versions else {},
    )
    already = {
        _clip_key(d["conversation_id"], d["segment_start"])
        async for d in _bucket_collection().find(
            {"conversation_id": conversation_id, "user_id": str(user.user_id)},
            {"conversation_id": 1, "segment_start": 1},
        )
    }
    unknown = [
        (idx, s)
        for idx, s in enumerate(active.get("segments") or [])
        if not s.get("identified_as")
        and s.get("segment_type", "speech") == "speech"
        and MIN_CLIP_SECONDS
        <= (float(s.get("end", 0)) - float(s.get("start", 0)))
        <= MAX_CLIP_SECONDS
        and _clip_key(conversation_id, float(s.get("start", 0))) not in already
    ][:SCAN_LIMIT]

    embedded = []
    model_id = None
    for idx, s in unknown:
        m = await _embed_clip(
            speaker_client,
            conversation_id,
            float(s["start"]),
            float(s["end"]),
            visibility=visibility,
        )
        if m is None:
            continue
        model_id = model_id or m["embedding_model"]
        embedded.append((idx, s, m))
    if not embedded:
        return {"conversation_id": conversation_id, "bucket_size": 0, "candidates": []}

    bucket, meta = await _bucket_matrix(
        user, "background_speech", model_id, visibility=visibility
    )
    await visibility.assert_current()
    q = np.asarray([m["embedding"] for _, _, m in embedded], dtype=np.float32)
    max_sim, _ = _max_similarity(q, bucket)
    scored: list[tuple[float, dict]] = []
    for i, (idx, s, m) in enumerate(embedded):
        likelihood = _background_likelihood(float(max_sim[i]), m["snr_db"])
        scored.append(
            (
                likelihood,
                {
                    "conversation_id": conversation_id,
                    "segment_index": idx,
                    "segment_start_time": round(float(s["start"]), 3),
                    "start": round(float(s["start"]), 3),
                    "end": round(float(s["end"]), 3),
                    "text": (s.get("text") or "")[:80],
                    "bucket_similarity": round(float(max_sim[i]), 4),
                    "snr_db": None if m["snr_db"] is None else round(m["snr_db"], 1),
                    "background_likelihood": likelihood,
                    "candidate_type": "background_speech",
                    "bucket_similarities": {
                        "background_speech": round(float(max_sim[i]), 4)
                    },
                },
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "conversation_id": conversation_id,
        "bucket_size": len(meta),
        "candidates": [c for _, c in scored[:limit]],
    }


async def scan_background_candidates(
    user: User, limit: int = 40, max_conversations: int = 8, per_conversation: int = 12
) -> JSONResponse | dict:
    """Corpus-wide scan: rank 'potentially background' clips ACROSS conversations.

    Walks recent conversations, embeds a bounded number of unidentified clips from
    each (cost cap: max_conversations × per_conversation), scores them all against
    the background bucket, and returns one merged ranked list. This is the top-level
    "Potentially background" feed; per-clip confirmation still flows through the
    appropriate background annotation (which grows one of the two buckets).
    """
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )

    user_bucket_filter = {"user_id": str(user.user_id)}
    already = {
        _clip_key(d["conversation_id"], d["segment_start"])
        async for d in _bucket_collection().find(
            user_bucket_filter, {"conversation_id": 1, "segment_start": 1}
        )
    }

    visibility = privacy.ConversationPrivacyFilter()
    planned: list[dict] = []
    scanned = 0
    cursor = (
        Conversation.get_pymongo_collection()
        .find(
            {
                "user_id": user.user_id,
                "deleted": {"$ne": True},
                "audio_archived": {"$ne": True},
                "audio_chunks_count": {"$gt": 0},
            },
            {
                "conversation_id": 1,
                "title": 1,
                "transcript_versions": 1,
                "active_transcript_version": 1,
                "audio_total_duration": 1,
            },
        )
        .sort("created_at", -1)
        .limit(
            60
        )  # examine at most 60 recent conversations to find max_conversations with unknowns
    )
    async for doc in cursor:
        if scanned >= max_conversations:
            break
        cid = doc.get("conversation_id")
        if not await visibility.filter([{"conversation_id": cid}]):
            continue
        await visibility.assert_current()
        versions = doc.get("transcript_versions") or []
        active_id = doc.get("active_transcript_version")
        active = next(
            (v for v in versions if v.get("version_id") == active_id),
            versions[-1] if versions else {},
        )
        segments = active.get("segments") or []
        background_speech = [
            (idx, s)
            for idx, s in enumerate(active.get("segments") or [])
            if not s.get("identified_as")
            and s.get("segment_type", "speech") == "speech"
            and MIN_CLIP_SECONDS
            <= (float(s.get("end", 0)) - float(s.get("start", 0)))
            <= MAX_CLIP_SECONDS
            and _clip_key(cid, float(s.get("start", 0))) not in already
        ]
        noise_windows = [
            window
            for window in _gap_windows(
                segments, float(doc.get("audio_total_duration") or 0.0)
            )
            if _clip_key(cid, window[0]) not in already
        ]
        if not background_speech and not noise_windows:
            continue
        scanned += 1
        speech_quota = max(1, per_conversation // 2)
        noise_quota = max(1, per_conversation - speech_quota)
        for idx, s in background_speech[:speech_quota]:
            planned.append(
                {
                    "conversation_id": cid,
                    "title": doc.get("title") or cid[:8],
                    "segment_index": idx,
                    "segment_start_time": round(float(s["start"]), 3),
                    "start": round(float(s["start"]), 3),
                    "end": round(float(s["end"]), 3),
                    "text": (s.get("text") or "")[:80],
                    "candidate_type": "background_speech",
                }
            )
        for start, end in noise_windows[:noise_quota]:
            planned.append(
                {
                    "conversation_id": cid,
                    "title": doc.get("title") or cid[:8],
                    "segment_index": -1,
                    "segment_start_time": start,
                    "start": start,
                    "end": end,
                    "text": "",
                    "candidate_type": "noise",
                }
            )

    semaphore = asyncio.Semaphore(4)

    async def measure(candidate: dict) -> dict | None:
        async with semaphore:
            result = await _embed_clip(
                speaker_client,
                candidate["conversation_id"],
                candidate["start"],
                candidate["end"],
                visibility=visibility,
            )
        if result is None:
            return None
        return {
            **candidate,
            "embedding": result["embedding"],
            "embedding_model": result["embedding_model"],
            "snr_db": result["snr_db"],
        }

    measured = await asyncio.gather(*(measure(candidate) for candidate in planned))
    collected = [candidate for candidate in measured if candidate is not None]
    model_id = next(
        (
            candidate["embedding_model"]
            for candidate in collected
            if candidate["embedding_model"]
        ),
        None,
    )

    scored_result = await _score_candidates(
        user, collected, model_id, visibility=visibility
    )
    await visibility.assert_current()
    scored = scored_result.get("scored", {kind: [] for kind in BUCKET_TYPES})
    per_bucket_limit = max(1, limit // 2)
    candidates = [
        item for kind in BUCKET_TYPES for _, item in scored[kind][:per_bucket_limit]
    ]
    return {
        "bucket_sizes": scored_result["bucket_sizes"],
        "scanned_conversations": scanned,
        "candidates": candidates,
    }


async def enqueue_background_index(user: User) -> dict:
    """Index every usable corpus clip, reusing cached embeddings on later runs."""
    key = {"requested_by": str(user.user_id)}
    revision = await _corpus_revision(str(user.user_id))
    existing = await _index_runs_collection().find_one(key)
    status = _job_status(existing.get("job_id") if existing else None)
    if status in {"queued", "started", "deferred", "scheduled"}:
        return {"job_id": existing["job_id"], "status": status, "reused": True}
    if (
        existing
        and existing.get("source_revision") == revision
        and await _corpus_collection().count_documents(key) > 0
    ):
        return {"job_id": existing.get("job_id"), "status": "finished", "reused": True}
    job = default_queue.enqueue(
        index_background_corpus_job,
        requested_by=str(user.user_id),
        source_revision=revision,
        job_timeout=14400,
        result_ttl=JOB_RESULT_TTL,
        description="Index full corpus for background review",
    )
    await _index_runs_collection().update_one(
        key,
        {
            "$set": {"job_id": job.id, "queued_at": datetime.now(timezone.utc)},
            "$unset": {"source_revision": ""},
        },
        upsert=True,
    )
    return {"job_id": job.id, "status": "queued", "reused": False}


async def _corpus_revision(user_id: str) -> str:
    """Cheap stable fingerprint of inputs that affect background candidates."""
    parts = []
    cursor = Conversation.get_pymongo_collection().find(
        {
            "user_id": user_id,
            "deleted": {"$ne": True},
            "audio_archived": {"$ne": True},
            "audio_chunks_count": {"$gt": 0},
        },
        {
            "conversation_id": 1,
            "active_transcript_version": 1,
            "audio_chunks_count": 1,
            "audio_total_duration": 1,
        },
    )
    async for doc in cursor:
        parts.append(
            f"{doc['conversation_id']}|{doc.get('active_transcript_version')}|"
            f"{doc.get('audio_chunks_count')}|{doc.get('audio_total_duration')}"
        )
    revisions = await privacy.capture_revisions(user_id)
    return hashlib.sha256(
        json.dumps(
            {
                "corpus": sorted(parts),
                "privacy": revisions,
                "index_version": "capture-references-v2",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


async def background_index_state(user: User) -> dict:
    key = {"requested_by": str(user.user_id)}
    run = await _index_runs_collection().find_one(key, {"_id": 0})
    job_id = run.get("job_id") if run else None
    latest = await _corpus_collection().find_one(key, sort=[("indexed_at", -1)])
    model = latest.get("embedding_model") if latest else None
    indexed = await _corpus_collection().count_documents(
        {**key, **({"embedding_model": model} if model else {})}
    )
    return {"job_id": job_id, "status": _job_status(job_id), "indexed": indexed}


def _cluster_rows(
    rows: list[dict], similarity: float = CLUSTER_SIMILARITY
) -> list[list[int]]:
    """Greedy cosine clustering; input vectors are speaker embeddings."""
    if not rows:
        return []
    matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for index, vector in enumerate(matrix):
        scores = [float(vector @ centroid) for centroid in centroids]
        nearest = int(np.argmax(scores)) if scores else -1
        if nearest >= 0 and scores[nearest] >= similarity:
            clusters[nearest].append(index)
            centroid = matrix[clusters[nearest]].mean(axis=0)
            centroids[nearest] = centroid / (np.linalg.norm(centroid) + 1e-9)
        else:
            clusters.append([index])
            centroids.append(vector.copy())
    return clusters


def _representatives(
    rows: list[dict], member_indices: list[int], limit: int
) -> list[dict]:
    matrix = np.asarray(
        [rows[index]["embedding"] for index in member_indices], dtype=np.float32
    )
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    centroid = matrix.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    ranked = np.argsort(-(matrix @ centroid))[:limit]
    return [rows[member_indices[int(index)]] for index in ranked]


def _review_samples(
    rows: list[dict], member_indices: list[int], limit: int
) -> list[tuple[dict, str]]:
    """Show central examples plus the weakest cluster members where we may be wrong."""
    matrix = np.asarray(
        [rows[index]["embedding"] for index in member_indices], dtype=np.float32
    )
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    centroid = matrix.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    ranked = np.argsort(-(matrix @ centroid)).tolist()
    edge_count = min(2, max(1, limit - 3)) if len(ranked) > 3 else 0
    typical_count = min(len(ranked) - edge_count, limit - edge_count)
    selected = [(index, "typical") for index in ranked[:typical_count]]
    selected.extend((index, "edge") for index in ranked[-edge_count:] if edge_count)
    return [(rows[member_indices[int(index)]], role) for index, role in selected]


def _harvest_groups(
    rows: list[dict],
    background_scores: dict[str, float],
    foreground_scores: dict[str, float],
    profile: Optional[dict] = None,
) -> list[list[dict]]:
    """Per-conversation groups of clips near a confirmed background exemplar."""
    profile = profile or SURFACE_PROFILES["default"]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("candidate_type") != "background_speech":
            continue
        key = row["clip_key"]
        margin = background_scores[key] - foreground_scores[key]
        if (
            background_scores[key] >= profile["harvest_similarity"]
            and margin >= profile["harvest_margin"]
        ):
            groups.setdefault(row["conversation_id"], []).append(row)
    return list(groups.values())


def _novelty_groups(
    rows: list[dict],
    background_scores: dict[str, float],
    foreground_scores: dict[str, float],
    consumed: set[str],
    profile: Optional[dict] = None,
) -> list[list[dict]]:
    """Per-conversation groups of clips far from every labelled reference."""
    profile = profile or SURFACE_PROFILES["default"]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = row["clip_key"]
        if key in consumed or row.get("candidate_type") != "background_speech":
            continue
        if (
            foreground_scores[key] <= profile["novelty_foreground_max"]
            and background_scores[key] <= profile["novelty_background_max"]
        ):
            groups.setdefault(row["conversation_id"], []).append(row)
    return [
        group for group in groups.values() if len(group) >= profile["novelty_min_clips"]
    ]


def _foreground_matches(
    rows: list[dict], exemplars: list[dict], threshold: float = FOREGROUND_SIMILARITY
) -> set[str]:
    """Clip keys acoustically covered by confirmed not-background examples."""
    speech = [row for row in rows if row.get("candidate_type") == "background_speech"]
    if not speech or not exemplars:
        return set()
    query = np.asarray([row["embedding"] for row in speech], dtype=np.float32)
    references = np.asarray([row["embedding"] for row in exemplars], dtype=np.float32)
    query /= np.linalg.norm(query, axis=1, keepdims=True) + 1e-9
    references /= np.linalg.norm(references, axis=1, keepdims=True) + 1e-9
    similarities = (query @ references.T).max(axis=1)
    return {
        row["clip_key"]
        for row, similarity in zip(speech, similarities)
        if float(similarity) >= threshold
    }


def _reference_scores(rows: list[dict], exemplars: list[dict]) -> dict[str, float]:
    """Maximum acoustic similarity to a labelled reference corpus."""
    if not rows or not exemplars:
        return {row["clip_key"]: 0.0 for row in rows}
    query = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
    references = np.asarray([row["embedding"] for row in exemplars], dtype=np.float32)
    query /= np.linalg.norm(query, axis=1, keepdims=True) + 1e-9
    references /= np.linalg.norm(references, axis=1, keepdims=True) + 1e-9
    similarities = (query @ references.T).max(axis=1)
    return {
        row["clip_key"]: float(similarity)
        for row, similarity in zip(rows, similarities)
    }


def _lane_of(cluster: dict) -> str:
    return cluster.get("mined") or "similar"


def _queue_summary(clusters: list[dict]) -> dict:
    """Split the queue into what the system already believes vs genuine unknowns.

    "quick confirms" are clusters production suppression would already call
    background (harvest lane, or mean scores in the confident zone) — reviewing
    them is a sign-off, not a judgment call. "uncertain" is the number that
    should shrink as the system learns.
    """
    quick = sum(
        1
        for cluster in clusters
        if cluster.get("mined") == "harvest"
        or zone_for(
            cluster.get("mean_background_similarity") or 0.0,
            cluster.get("mean_foreground_similarity") or 0.0,
        )
        == "confident_background"
    )
    return {
        "unreviewed": len(clusters),
        "quick_confirms": quick,
        "uncertain": len(clusters) - quick,
    }


def _serve_clusters(payload: dict, lane: Optional[str], limit: int) -> dict:
    """Slice the cached full cluster list down to one lane and the limit."""
    clusters = payload.get("clusters") or []
    lane_counts: dict[str, int] = {}
    for cluster in clusters:
        lane_counts[_lane_of(cluster)] = lane_counts.get(_lane_of(cluster), 0) + 1
    queue_summary = _queue_summary(clusters)
    if lane:
        clusters = [c for c in clusters if _lane_of(c) == lane]
    return {
        **payload,
        "clusters": clusters[:limit],
        "lane": lane,
        "lane_counts": lane_counts,
        "queue_summary": queue_summary,
    }


async def get_background_clusters(
    user: User,
    limit: int = 6,
    samples_per_cluster: int = 5,
    lane: Optional[str] = None,
    surface: str = "default",
) -> dict:
    """Return high-impact, within-conversation clusters with 3–5 review clips.

    ``lane`` filters the queue: "harvest" (near a confirmed background exemplar,
    batch-confirmable), "novel" (unlike every labelled reference — new sources),
    "similar" (regular within-conversation clusters). None keeps the default
    order (harvest first, then the focus-mode ranking).

    ``surface`` is the review dial ("less" / "default" / "more"): it widens or
    narrows the lane thresholds and minimum cluster size, changing only what is
    surfaced for review — production suppression is untouched.
    """
    user_id = str(user.user_id)
    visibility = privacy.ConversationPrivacyFilter()
    visibility.snapshots[user_id] = await privacy.load_snapshot(user_id)
    profile = SURFACE_PROFILES.get(surface) or SURFACE_PROFILES["default"]
    latest = await _corpus_collection().find_one(
        {"requested_by": user_id}, sort=[("indexed_at", -1)]
    )
    if not latest:
        return {"clusters": [], "indexed": 0, "remaining": 0, "bucket_sizes": {}}
    model = latest["embedding_model"]
    cached = await _cluster_cache_collection().find_one(
        {"requested_by": user_id, "embedding_model": model, "surface": surface},
        {
            "_id": 0,
            "payload": 1,
            "privacy_revisions": 1,
            "requested_by": 1,
            "privacy_reference_receipt": 1,
        },
    )
    if cached and await _current_cache_privacy(cached, visibility):
        return _serve_clusters(cached["payload"], lane, limit)
    all_rows = await visibility.filter_embeddings(
        [
            doc
            async for doc in _corpus_collection().find(
                {"requested_by": user_id, "embedding_model": model}, {"_id": 0}
            )
        ]
    )
    allowed_keys = {row["clip_key"] for row in all_rows}
    reviews = [
        doc
        async for doc in _cluster_reviews_collection().find(
            {"requested_by": user_id, "embedding_model": model}
        )
        if doc.get("member_keys") and set(doc["member_keys"]) <= allowed_keys
    ]
    reviewed = {key for doc in reviews for key in doc["member_keys"]}
    bucket_rows = await visibility.filter_embeddings(
        [doc async for doc in _bucket_collection().find({"user_id": user_id})]
    )
    confirmed = {
        f"{doc['conversation_id']}:{float(doc['segment_start']):.3f}"
        for doc in bucket_rows
        if doc.get("embedding_model") == model
    }
    foreground_rows = await visibility.filter_embeddings(
        [
            doc
            async for doc in _foreground_collection().find(
                {"requested_by": user_id, "embedding_model": model}
            )
        ]
    )
    foreground_signatures = {doc["content_signature"] for doc in foreground_rows}
    foreground_exemplars = [doc for doc in foreground_rows if doc.get("embedding")]
    background_exemplars = [
        doc
        for doc in bucket_rows
        if doc.get("embedding")
        and doc.get("embedding_model") == model
        and doc.get("bucket_type") == "background_speech"
    ]
    await visibility.assert_current()
    reviewed_signatures = {
        _content_signature(row) for row in all_rows if row["clip_key"] in reviewed
    }
    foreground_matches = _foreground_matches(all_rows, foreground_exemplars)
    foreground_scores = _reference_scores(all_rows, foreground_exemplars)
    background_scores = _reference_scores(all_rows, background_exemplars)
    noise_reference_count = sum(
        doc.get("bucket_type") == "noise" for doc in bucket_rows
    )
    foreground_review_count = sum(
        doc.get("decision") == "not_background"
        or (
            doc.get("decision") == "mixed"
            and "not_background" in (doc.get("sample_decisions") or {}).values()
        )
        for doc in reviews
    )
    focus_hard_speech = noise_reference_count >= 10
    adaptive_discovery = focus_hard_speech and foreground_review_count >= 10
    rows = [
        row
        for row in all_rows
        if row["clip_key"] not in reviewed
        and (not focus_hard_speech or row.get("candidate_type") == "background_speech")
        and (focus_hard_speech or row["clip_key"] not in foreground_matches)
        and _content_signature(row) not in reviewed_signatures
        and f"{row['conversation_id']}:{float(row['start']):.3f}" not in confirmed
        and _content_signature(row) not in foreground_signatures
    ]
    rows.sort(
        key=lambda row: (row["conversation_id"], row["candidate_type"], row["clip_key"])
    )

    def build_cluster(
        group: list[dict], members: list[int], mined: Optional[str] = None
    ) -> dict:
        reps = _review_samples(group, members, min(samples_per_cluster, len(members)))
        member_keys = [group[index]["clip_key"] for index in members]
        signatures = sorted({_content_signature(group[index]) for index in members})
        cluster_id = hashlib.sha256("|".join(signatures).encode()).hexdigest()[:16]
        mean_foreground_similarity = sum(
            foreground_scores[group[index]["clip_key"]] for index in members
        ) / len(members)
        mean_background_similarity = sum(
            background_scores[group[index]["clip_key"]] for index in members
        ) / len(members)
        known_speaker_fraction = sum(
            _is_known_foreground(group[index]) for index in members
        ) / len(members)
        suggestion_score = (
            mean_background_similarity - mean_foreground_similarity
            if background_exemplars
            else 1.0 - mean_foreground_similarity
        ) - 0.1 * known_speaker_fraction
        return {
            "cluster_id": cluster_id,
            "candidate_type": group[members[0]]["candidate_type"],
            "conversation_id": group[members[0]]["conversation_id"],
            "conversation_title": group[members[0]].get("conversation_title"),
            "size": len(members),
            "mined": mined,
            "known_speaker_fraction": round(known_speaker_fraction, 3),
            "known_speaker_count": len(
                {
                    str(group[index].get("current_label"))
                    for index in members
                    if _is_known_foreground(group[index])
                }
            ),
            "mean_foreground_confidence": round(
                sum(
                    float(group[index].get("stored_confidence") or 0)
                    for index in members
                )
                / len(members),
                3,
            ),
            "mean_foreground_similarity": round(mean_foreground_similarity, 3),
            "mean_background_similarity": round(mean_background_similarity, 3),
            "suggestion_score": round(suggestion_score, 3),
            "member_keys": member_keys,
            "samples": [
                {
                    "review_role": role,
                    **{
                        key: row.get(key)
                        for key in (
                            "clip_key",
                            "conversation_id",
                            "conversation_title",
                            "segment_index",
                            "start",
                            "end",
                            "text",
                            "candidate_type",
                            "current_label",
                        )
                    },
                }
                for row, role in reps
            ],
        }

    clusters = []
    consumed: set[str] = set()

    # Harvest lane: unreviewed clips near a confirmed background exemplar —
    # one batch-confirmable group per conversation grows the bucket fastest.
    if background_exemplars:
        for group in _harvest_groups(
            rows, background_scores, foreground_scores, profile
        ):
            clusters.append(build_cluster(group, list(range(len(group))), "harvest"))
            consumed.update(row["clip_key"] for row in group)

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["clip_key"] in consumed:
            continue
        grouped.setdefault((row["candidate_type"], row["conversation_id"]), []).append(
            row
        )
    for group in grouped.values():
        for members in _cluster_rows(group):
            if len(members) < profile["min_cluster_size"]:
                continue
            clusters.append(build_cluster(group, members))
            consumed.update(group[index]["clip_key"] for index in members)

    # Novelty lane: clips far from every labelled reference. k-NN mining can
    # only find more of what is already labelled; new background types (a TV
    # channel never reviewed) surface here for a first human verdict.
    if background_exemplars and foreground_exemplars:
        for group in _novelty_groups(
            rows, background_scores, foreground_scores, consumed, profile
        ):
            clusters.append(build_cluster(group, list(range(len(group))), "novel"))

    # Duplicate uploads produce identical content signatures and therefore
    # identical cluster ids — reviewing one covers all, so show only one.
    unique_clusters: dict[str, dict] = {}
    for cluster in clusters:
        unique_clusters.setdefault(cluster["cluster_id"], cluster)
    clusters = list(unique_clusters.values())

    if adaptive_discovery:
        clusters.sort(
            key=lambda item: (item["suggestion_score"], item["size"]), reverse=True
        )
    elif focus_hard_speech:
        clusters.sort(
            key=lambda item: (
                item["known_speaker_count"],
                item["known_speaker_fraction"],
                -abs(item["mean_foreground_confidence"] - HARD_CASE_TARGET_CONFIDENCE),
                item["size"],
            ),
            reverse=True,
        )
    else:
        clusters.sort(key=lambda item: item["size"], reverse=True)
    # Harvest groups are near-certain positives — always review those first.
    clusters.sort(key=lambda item: item.get("mined") != "harvest")
    bucket_sizes = {
        kind: sum(doc.get("bucket_type") == kind for doc in bucket_rows)
        for kind in BUCKET_TYPES
    }
    # Cache the FULL sorted list; lane filter and limit are applied at serve
    # time so one expensive build answers every lane.
    payload = {
        "clusters": clusters,
        "indexed": len(rows) + len(reviewed),
        "remaining": len(rows),
        "bucket_sizes": bucket_sizes,
        "surface": surface,
        "review_focus": (
            "discovery"
            if adaptive_discovery
            else "hard_speech" if focus_hard_speech else "bootstrap"
        ),
    }
    await visibility.assert_current()
    await _cluster_cache_collection().update_one(
        {"requested_by": user_id, "embedding_model": model, "surface": surface},
        {
            "$set": {
                "payload": payload,
                "privacy_revisions": visibility.revision_receipt(),
                "privacy_reference_receipt": await visibility.reference_receipt(
                    user_id
                ),
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    await visibility.assert_current()
    return _serve_clusters(payload, lane, limit)


async def decide_background_cluster(
    user: User,
    cluster: dict,
    decision: Literal[
        "noise", "background_speech", "not_background", "mixed", "dismissed"
    ],
) -> dict:
    """Apply a whole-cluster decision, clip-level mixed labels, or dismiss it."""
    user_id = str(user.user_id)
    member_keys = list(dict.fromkeys(cluster.get("member_keys") or []))
    review_sample_keys = list(dict.fromkeys(cluster.get("review_sample_keys") or []))[
        :5
    ]
    sample_decisions = cluster.get("sample_decisions") or {}
    if decision == "mixed":
        allowed_keys = set(review_sample_keys)
        sample_decisions = {
            key: value
            for key, value in sample_decisions.items()
            if key in allowed_keys
            and value in {"noise", "background_speech", "not_background"}
        }
        if not allowed_keys or set(sample_decisions) != allowed_keys:
            return JSONResponse(
                status_code=422,
                content={"error": "Label every reviewed clip in a mixed cluster"},
            )
        # Only explicitly labelled samples leave the queue. Unseen members are
        # deliberately left available to be reclustered around the new references.
        member_keys = list(sample_decisions)
    docs = [
        doc
        async for doc in _corpus_collection().find(
            {"requested_by": user_id, "clip_key": {"$in": member_keys}}, {"_id": 0}
        )
    ]
    if not docs:
        return JSONResponse(
            status_code=404, content={"error": "Cluster clips not found"}
        )
    visibility = privacy.ConversationPrivacyFilter()
    if {doc["clip_key"] for doc in docs} != set(member_keys) or len(
        await visibility.filter_embeddings(docs)
    ) != len(docs):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    model = docs[0].get("embedding_model")
    review_object_id = ObjectId()
    review_id = str(review_object_id)
    if decision not in {"mixed", "dismissed"}:
        signatures = {_content_signature(doc) for doc in docs}
        allowed_duplicates = await visibility.filter_embeddings(
            [
                doc
                async for doc in _corpus_collection().find(
                    {"requested_by": user_id, "embedding_model": model}, {"_id": 0}
                )
            ]
        )
        await visibility.assert_current()
        duplicate_docs = [
            doc for doc in allowed_duplicates if _content_signature(doc) in signatures
        ]
        member_keys = list(dict.fromkeys(doc["clip_key"] for doc in duplicate_docs))
    added = 0
    exemplar_keys: list[str] = []
    foreground_signatures: list[str] = []
    if decision == "mixed":
        for doc in docs:
            clip_decision = sample_decisions[doc["clip_key"]]
            if clip_decision in {"noise", "background_speech"}:
                await visibility.assert_current()
                await _bucket_collection().update_one(
                    {
                        "user_id": user_id,
                        "conversation_id": doc["conversation_id"],
                        "segment_start": round(float(doc["start"]), 3),
                        "bucket_type": clip_decision,
                    },
                    {
                        "$set": {
                            "segment_end": round(float(doc["end"]), 3),
                            "privacy_reference_receipt": doc[
                                "privacy_reference_receipt"
                            ],
                            "embedding": doc["embedding"],
                            "embedding_model": model,
                            "source": "mixed_cluster_review",
                            "review_id": review_id,
                            "added_by": user_id,
                            "created_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )
                await visibility.assert_current()
                exemplar_keys.append(doc["clip_key"])
                added += 1
            else:
                signature = _content_signature(doc)
                await visibility.assert_current()
                await _foreground_collection().update_one(
                    {
                        "requested_by": user_id,
                        "content_signature": signature,
                        "embedding_model": model,
                    },
                    {
                        "$set": {
                            "privacy_reference_receipt": doc[
                                "privacy_reference_receipt"
                            ],
                            "embedding": doc["embedding"],
                            "conversation_id": doc["conversation_id"],
                            "clip_key": doc["clip_key"],
                            "review_id": review_id,
                            "created_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )
                await visibility.assert_current()
                foreground_signatures.append(signature)
    elif decision in {"noise", "background_speech"}:
        representatives = _representatives(
            docs, list(range(len(docs))), min(5, len(docs))
        )
        for doc in representatives:
            await visibility.assert_current()
            await _bucket_collection().update_one(
                {
                    "user_id": user_id,
                    "conversation_id": doc["conversation_id"],
                    "segment_start": round(float(doc["start"]), 3),
                    "bucket_type": decision,
                },
                {
                    "$set": {
                        "segment_end": round(float(doc["end"]), 3),
                        "privacy_reference_receipt": doc["privacy_reference_receipt"],
                        "embedding": doc["embedding"],
                        "embedding_model": model,
                        "source": "cluster_review",
                        "review_id": review_id,
                        "added_by": user_id,
                        "created_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
            await visibility.assert_current()
            added += 1
            exemplar_keys.append(doc["clip_key"])
    elif decision == "not_background":
        for doc in _representatives(docs, list(range(len(docs))), min(5, len(docs))):
            signature = _content_signature(doc)
            await visibility.assert_current()
            await _foreground_collection().update_one(
                {
                    "requested_by": user_id,
                    "content_signature": signature,
                    "embedding_model": model,
                },
                {
                    "$set": {
                        "privacy_reference_receipt": doc["privacy_reference_receipt"],
                        "embedding": doc["embedding"],
                        "conversation_id": doc["conversation_id"],
                        "clip_key": doc["clip_key"],
                        "review_id": review_id,
                        "created_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
            await visibility.assert_current()
            foreground_signatures.append(signature)
    await visibility.assert_current()
    inserted = await _cluster_reviews_collection().insert_one(
        {
            "_id": review_object_id,
            "requested_by": user_id,
            "embedding_model": model,
            "cluster_id": cluster.get("cluster_id"),
            "member_keys": member_keys,
            "review_sample_keys": review_sample_keys,
            "decision": decision,
            "sample_decisions": sample_decisions,
            "exemplar_keys": exemplar_keys,
            "foreground_signatures": foreground_signatures,
            "reviewed_at": datetime.now(timezone.utc),
        }
    )
    await visibility.assert_current()
    await visibility.assert_current()
    await _cluster_cache_collection().delete_many({"requested_by": user_id})
    await visibility.assert_current()
    await visibility.assert_current()
    await Conversation.get_pymongo_collection().database[
        "background_cleanup_reports"
    ].delete_many({"requested_by": user_id})
    await visibility.assert_current()
    return {
        "review_id": str(inserted.inserted_id),
        "reviewed": len(member_keys),
        "exemplars_added": added,
        "duplicates_covered": max(
            0, len(member_keys) - len(cluster.get("member_keys") or [])
        ),
        "decision": decision,
    }


async def latest_background_decision(user: User) -> dict:
    review = await _cluster_reviews_collection().find_one(
        {"requested_by": str(user.user_id)}, sort=[("reviewed_at", -1)]
    )
    if not review:
        return {"decision": None}
    return {
        "review_id": str(review["_id"]),
        "decision": review.get("decision"),
        "reviewed": len(review.get("member_keys") or []),
        "reviewed_at": review.get("reviewed_at"),
    }


async def list_background_decisions(user: User, limit: int = 50) -> dict:
    visibility = privacy.ConversationPrivacyFilter()
    decisions = []
    user_id = str(user.user_id)
    cursor = (
        _cluster_reviews_collection()
        .find({"requested_by": user_id})
        .sort("reviewed_at", -1)
        .limit(limit)
    )
    async for review in cursor:
        member_keys = review.get("member_keys") or []
        docs = [
            doc
            async for doc in _corpus_collection().find(
                {"requested_by": user_id, "clip_key": {"$in": member_keys}},
                {"_id": 0},
            )
        ]
        allowed_docs = await visibility.filter_embeddings(docs)
        if len(allowed_docs) != len(docs) or {doc["clip_key"] for doc in docs} != set(
            member_keys
        ):
            continue
        await visibility.assert_current()
        by_key = {doc["clip_key"]: doc for doc in docs}
        stored_sample_keys = review.get("review_sample_keys") or []
        if stored_sample_keys:
            samples = [by_key[key] for key in stored_sample_keys if key in by_key]
            reconstructed = False
        else:
            samples = _representatives(docs, list(range(len(docs))), min(5, len(docs)))
            reconstructed = True
        decisions.append(
            {
                "review_id": str(review["_id"]),
                "cluster_id": review.get("cluster_id"),
                "decision": review.get("decision"),
                "reviewed": len(review.get("review_sample_keys") or [])
                or len(review.get("exemplar_keys") or [])
                or min(5, len(review.get("member_keys") or [])),
                "clips_affected": len(review.get("member_keys") or []),
                "reviewed_at": review.get("reviewed_at"),
                "samples_reconstructed": reconstructed,
                "samples": [
                    {
                        "clip_key": sample["clip_key"],
                        "conversation_id": sample["conversation_id"],
                        "conversation_title": sample.get("conversation_title"),
                        "segment_index": sample.get("segment_index"),
                        "start": sample["start"],
                        "end": sample["end"],
                        "text": sample.get("text") or "",
                        "current_label": sample.get("current_label"),
                        "candidate_type": sample.get("candidate_type"),
                        "decision": (review.get("sample_decisions") or {}).get(
                            sample["clip_key"]
                        ),
                    }
                    for sample in samples
                ],
            }
        )
    await visibility.assert_current()
    return {"decisions": decisions}


async def undo_background_decision(user: User, review_id: str) -> dict:
    """Reverse one review and every training reference it introduced."""
    try:
        object_id = ObjectId(review_id)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid review id"})
    user_id = str(user.user_id)
    review = await _cluster_reviews_collection().find_one(
        {"_id": object_id, "requested_by": user_id}
    )
    if not review:
        return JSONResponse(status_code=404, content={"error": "Review not found"})
    docs = [
        doc
        async for doc in _corpus_collection().find(
            {
                "requested_by": user_id,
                "clip_key": {"$in": review.get("member_keys") or []},
            },
            {"_id": 0},
        )
    ]
    representatives = _representatives(docs, list(range(len(docs))), min(5, len(docs)))
    exemplar_keys = review.get("exemplar_keys") or [
        doc["clip_key"] for doc in representatives
    ]
    foreground_signatures = review.get("foreground_signatures") or [
        _content_signature(doc) for doc in representatives
    ]
    exemplar_docs = [doc for doc in docs if doc["clip_key"] in exemplar_keys]
    has_tracked_references = (
        "exemplar_keys" in review or "foreground_signatures" in review
    )
    bucket_deleted = 0
    for doc in exemplar_docs:
        bucket_query = {
            "user_id": user_id,
            "conversation_id": doc["conversation_id"],
            "segment_start": round(float(doc["start"]), 3),
            "bucket_type": review.get("decision"),
            "source": "cluster_review",
        }
        if has_tracked_references:
            bucket_query["review_id"] = review_id
        result = await _bucket_collection().delete_many(bucket_query)
        bucket_deleted += result.deleted_count
    foreground_deleted = (
        await _foreground_collection().delete_many(
            {
                "requested_by": user_id,
                "embedding_model": review.get("embedding_model"),
                "content_signature": {"$in": foreground_signatures},
                **({"review_id": review_id} if has_tracked_references else {}),
            }
        )
    ).deleted_count
    await _cluster_reviews_collection().delete_one({"_id": object_id})
    await _cluster_cache_collection().delete_many({"requested_by": user_id})
    await Conversation.get_pymongo_collection().database[
        "background_cleanup_reports"
    ].delete_many({"requested_by": user_id})
    return {
        "undone": True,
        "review_id": review_id,
        "decision": review.get("decision"),
        "clips_restored": len(review.get("member_keys") or []),
        "references_removed": bucket_deleted + foreground_deleted,
    }


async def edit_background_decision(
    user: User,
    review_id: str,
    decision: Literal["noise", "background_speech", "not_background", "dismissed"],
) -> dict:
    """Change a past review's verdict in place (annotation-history edit).

    Reverses every training reference the original decision introduced (via the
    undo path), re-applies the same cluster under the new decision, and stamps
    the replacement review with where it came from. This is the durable
    mechanism for fixing source mislabels — e.g. a "real people" verdict that
    was actually content — without a separate re-check queue.
    """
    try:
        object_id = ObjectId(review_id)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid review id"})
    review = await _cluster_reviews_collection().find_one(
        {"_id": object_id, "requested_by": str(user.user_id)}
    )
    if not review:
        return JSONResponse(status_code=404, content={"error": "Review not found"})
    previous_decision = review.get("decision")
    if previous_decision == decision:
        return {"edited": False, "review_id": review_id, "decision": decision}
    if previous_decision == "mixed":
        return JSONResponse(
            status_code=422,
            content={
                "error": "Mixed reviews carry per-clip labels — remove and "
                "re-review the cluster instead"
            },
        )
    cluster = {
        "cluster_id": review.get("cluster_id"),
        "member_keys": review.get("member_keys") or [],
        "review_sample_keys": review.get("review_sample_keys") or [],
    }
    visibility = privacy.ConversationPrivacyFilter()
    members = [
        doc
        async for doc in _corpus_collection().find(
            {
                "requested_by": str(user.user_id),
                "clip_key": {"$in": cluster["member_keys"]},
            }
        )
    ]
    if {doc["clip_key"] for doc in members} != set(cluster["member_keys"]) or len(
        await visibility.filter_embeddings(members)
    ) != len(members):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    undone = await undo_background_decision(user, review_id)
    await visibility.assert_current()
    if isinstance(undone, JSONResponse):
        return undone
    result = await decide_background_cluster(user, cluster, decision)
    if isinstance(result, JSONResponse):
        return result
    await _cluster_reviews_collection().update_one(
        {"_id": ObjectId(result["review_id"])},
        {"$set": {"edited_from": review_id, "previous_decision": previous_decision}},
    )
    return {"edited": True, "previous_decision": previous_decision, **result}


async def background_cleanup_report(user: User) -> dict:
    """Evaluate reviewed references against cached corpus embeddings."""
    return await build_background_cleanup_report(str(user.user_id))


async def background_accuracy_report(user: User) -> dict:
    """Measure baseline versus learned foreground/background differentiation."""
    return await build_background_benchmark(str(user.user_id))


async def enqueue_background_cleanup(user: User, report_id: str) -> dict:
    report = (
        await Conversation.get_pymongo_collection()
        .database["background_cleanup_reports"]
        .find_one({"requested_by": str(user.user_id), "report_id": report_id})
    )
    if not report:
        return JSONResponse(status_code=404, content={"error": "Report not found"})
    visibility = await require_cleanup_report(report, str(user.user_id))
    await visibility.assert_current()
    job = default_queue.enqueue(
        apply_background_cleanup_job,
        requested_by=str(user.user_id),
        report_id=report_id,
        job_timeout=3600,
        result_ttl=JOB_RESULT_TTL,
        description=f"Apply background cleanup {report_id}",
    )
    return {"job_id": job.id, "status": "queued", "report_id": report_id}
