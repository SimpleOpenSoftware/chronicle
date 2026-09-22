"""Corpus-wide discovery of identities hidden behind local unknown labels."""

import asyncio
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from rq import get_current_job

from backend.constants import is_unknown_speaker_label
from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import reconstruct_audio_segment
from backend.workers.speaker_discovery_jobs import _active_segments

MIN_SECONDS = 3.0
MAX_SECONDS = 30.0
EMBED_CONCURRENCY = 4
CLUSTER_SIMILARITY = 0.72


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _centroid(vectors: list[list[float]]) -> list[float]:
    mean = [sum(values) / len(vectors) for values in zip(*vectors)]
    norm = math.sqrt(sum(value * value for value in mean))
    if not norm:
        raise ValueError("Cannot form a centroid from zero vectors")
    return [value / norm for value in mean]


def cluster_local_identities(
    identities: list[dict], threshold: float = CLUSTER_SIMILARITY
) -> tuple[list[list[dict]], list[dict]]:
    """Deterministic average-link clustering with per-conversation cannot-link."""
    ordered = sorted(identities, key=lambda item: item["identity_key"])
    clusters = [[item] for item in ordered]

    def similarity(left: list[dict], right: list[dict]) -> float:
        return sum(
            _cosine(a["centroid"], b["centroid"]) for a in left for b in right
        ) / (len(left) * len(right))

    while True:
        best = None
        for left_index, left in enumerate(clusters):
            left_conversations = {item["conversation_id"] for item in left}
            for right_index in range(left_index + 1, len(clusters)):
                right = clusters[right_index]
                if left_conversations.intersection(
                    item["conversation_id"] for item in right
                ):
                    continue
                score = similarity(left, right)
                candidate = (score, left_index, right_index)
                if score >= threshold and (best is None or candidate > best):
                    best = candidate
        if best is None:
            break
        _, left_index, right_index = best
        clusters[left_index] = clusters[left_index] + clusters[right_index]
        del clusters[right_index]

    discovered = [cluster for cluster in clusters if len(cluster) >= 2]
    outliers = [cluster[0] for cluster in clusters if len(cluster) == 1]
    return discovered, outliers


async def _unknown_identities(
    user_id: str, *, visibility=None, stats=None
) -> list[dict]:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    query = {
        "user_id": user_id,
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
    identities: dict[str, dict] = {}
    async for doc in Conversation.get_pymongo_collection().find(query, projection):
        if not await visibility.filter([doc]):
            if stats is not None:
                stats["privacy_held_recordings"] += 1
            continue
        duration = float(doc.get("audio_total_duration") or 0.0)
        active_segments = _active_segments(doc)
        for index, segment in enumerate(active_segments):
            label = segment.get("identified_as") or segment.get("speaker")
            if not is_unknown_speaker_label(label):
                continue
            start = float(segment.get("start") or 0.0)
            end = min(float(segment.get("end") or 0.0), duration or float("inf"))
            end = min(end, start + MAX_SECONDS)
            if end - start < MIN_SECONDS:
                continue
            if any(
                other is not segment
                and other.get("segment_type") in (None, "speech")
                and other.get("speaker") != segment.get("speaker")
                and float(other.get("start") or 0.0) < end
                and start < float(other.get("end") or 0.0)
                for other in active_segments
            ):
                continue
            key = f'{doc["conversation_id"]}:{label}'
            identity = identities.setdefault(
                key,
                {
                    "identity_key": key,
                    "conversation_id": doc["conversation_id"],
                    "conversation_title": doc.get("title") or "",
                    "conversation_date": str(doc.get("created_at") or ""),
                    "local_label": label,
                    "segments": [],
                },
            )
            identity["segments"].append(
                {
                    "segment_index": index,
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "text": (segment.get("text") or "")[:300],
                }
            )
    await visibility.assert_current()
    return list(identities.values())


@async_job(redis=False, beanie=True, timeout=14400)
async def discover_unknown_speakers_job(requested_by: str) -> dict:
    visibility = privacy.ConversationPrivacyFilter()
    stats = {"privacy_held_recordings": 0}
    client = SpeakerRecognitionClient()
    info = await client.get_embedding_info()
    model = info.get("embedding_model")
    if not model:
        raise RuntimeError("Could not resolve speaker embedding model")
    identities = await _unknown_identities(
        requested_by, visibility=visibility, stats=stats
    )
    semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)

    async def embed(identity: dict) -> dict | None:
        vectors = []
        async with semaphore:
            for segment in identity["segments"]:
                await visibility.assert_current()
                wav = await reconstruct_audio_segment(
                    identity["conversation_id"], segment["start"], segment["end"]
                )
                await visibility.assert_current()
                result = await client.extract_speaker_embedding(wav)
                await visibility.assert_current()
                if result.get("embedding"):
                    vectors.append(result["embedding"])
        if not vectors:
            return None
        return {**identity, "centroid": _centroid(vectors)}

    results = await asyncio.gather(
        *(embed(i) for i in identities), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    embedded = [item for item in results if item]
    await visibility.assert_current()
    clusters, outliers = await asyncio.to_thread(cluster_local_identities, embedded)
    await visibility.assert_current()
    evidence_conversation_ids = sorted({item["conversation_id"] for item in embedded})
    now = datetime.now(timezone.utc)
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                model,
                visibility.revision_receipt(),
                [(item["identity_key"], item["segments"]) for item in embedded],
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    rows = []
    for index, members in enumerate(clusters):
        cluster_id = hashlib.sha256(
            "|".join(item["identity_key"] for item in members).encode()
        ).hexdigest()[:16]
        rows.append(
            {
                "requested_by": requested_by,
                "evidence_conversation_ids": evidence_conversation_ids,
                "privacy_revisions": visibility.revision_receipt(),
                "run_fingerprint": fingerprint,
                "cluster_id": cluster_id,
                "members": [
                    {k: v for k, v in item.items() if k != "centroid"}
                    for item in members
                ],
                "conversation_count": len(members),
                "segment_count": sum(len(item["segments"]) for item in members),
                "created_at": now,
                "status": "pending",
            }
        )
    collection = Conversation.get_pymongo_collection().database[
        "unknown_speaker_clusters"
    ]
    async with visibility.publication():
        await collection.delete_many(
            {"requested_by": requested_by, "status": "pending"}
        )
        if rows:
            await collection.insert_many(rows)
    job = get_current_job()
    return {
        **stats,
        "job_id": job.id if job else None,
        "run_fingerprint": fingerprint,
        "embedding_model": model,
        "local_identities": len(embedded),
        "clusters": len(rows),
        "outliers": len(outliers),
    }
