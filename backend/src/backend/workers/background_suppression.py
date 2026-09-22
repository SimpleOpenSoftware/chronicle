"""Background-speech suppression: shared zone logic and the disclosure ledger.

Design: Docs/background-suppression-design.md. The speaker job already relabels
segments that clearly match a background exemplar; this module centralises the
thresholds, adds the *unsure* band (queued for review, never relabelled), and
records every decision in the ``background_suppressions`` ledger so the UI can
say "N segments were marked background — review if you want".

Kept separate from the cleanup machinery on purpose (experimental), but the
zone function and ledger are the pieces a future unified system reuses.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

import numpy as np

from backend.models.conversation import Conversation
from backend.services import privacy

logger = logging.getLogger(__name__)

# Tuned 2026-07-18 on the cluster-held-out benchmark (405 samples): precision
# stays 1.0 down to similarity 0.40 as long as the margin over the best
# foreground match is >= 0.20 — the margin carries the signal, the absolute
# floor is a backstop for sparse buckets. 0.45 sits mid-plateau rather than at
# the best-on-eval point.
CONFIDENT_SIMILARITY = 0.45
CONFIDENT_MARGIN = 0.20
# Unsure band = the region that would add recall but at precision < 1.0
# (0.40/+0.10 scored P 0.97 / R 0.82): worth a human look, not safe to act on.
UNSURE_SIMILARITY = 0.40
UNSURE_MARGIN = 0.10

CLUSTER_SIMILARITY = 0.72

Zone = Literal["confident_background", "unsure", "foreground"]

# Ledger statuses:
#   applied  — segment was relabelled by the live pipeline (confident zone)
#   shadow   — confident zone found by backfill; transcript NOT touched
#   queued   — unsure band, awaiting user review
#   restored — user said "this is important speech"; never re-marked
#   confirmed— user endorsed the removal; feeds exemplars
STICKY_STATUSES = {"restored", "confirmed"}


def zone_for(background_similarity: float, foreground_similarity: float) -> Zone:
    """Classify one segment's scores into the three-zone policy."""
    if (
        background_similarity >= CONFIDENT_SIMILARITY
        and background_similarity >= foreground_similarity + CONFIDENT_MARGIN
    ):
        return "confident_background"
    if (
        background_similarity >= UNSURE_SIMILARITY
        and background_similarity >= foreground_similarity + UNSURE_MARGIN
    ):
        return "unsure"
    return "foreground"


def _ledger_collection():
    return Conversation.get_pymongo_collection().database["background_suppressions"]


def _overrides_collection():
    return Conversation.get_pymongo_collection().database["media_role_overrides"]


def segment_key(start: float) -> float:
    return round(float(start), 3)


def assign_cluster_signatures(records: list[dict]) -> None:
    """Greedy-cluster record embeddings and stamp a stable cluster_signature.

    The signature hashes the member segments' content, so re-scoring the same
    conversation reproduces the same signature for the same material.
    """
    scored = [r for r in records if r.get("embedding") is not None]
    if not scored:
        return
    matrix = np.asarray([r["embedding"] for r in scored], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for index, vector in enumerate(matrix):
        scores = [float(vector @ centroid) for centroid in centroids]
        nearest = int(np.argmax(scores)) if scores else -1
        if nearest >= 0 and scores[nearest] >= CLUSTER_SIMILARITY:
            clusters[nearest].append(index)
            centroid = matrix[clusters[nearest]].mean(axis=0)
            centroids[nearest] = centroid / (np.linalg.norm(centroid) + 1e-9)
        else:
            clusters.append([index])
            centroids.append(vector.copy())
    for members in clusters:
        content = sorted(
            f"{segment_key(scored[i]['segment_start'])}:"
            f"{segment_key(scored[i]['segment_end'])}:"
            f"{' '.join(str(scored[i].get('text') or '').lower().split())}"
            for i in members
        )
        signature = hashlib.sha256("|".join(content).encode()).hexdigest()[:16]
        for i in members:
            scored[i]["cluster_signature"] = signature


async def get_subject_override(user_id: str, conversation_id: str) -> Optional[dict]:
    """A conversation-scoped 'media is the subject here' decision, if any.

    ``cluster_signature: None`` means the whole conversation is exempt from
    background marking (the rescue path writes these).
    """
    visibility = await require_recording(conversation_id)
    result = await _overrides_collection().find_one(
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "role": "subject",
            "cluster_signature": None,
        }
    )
    await visibility.assert_current()
    return result


async def require_recording(conversation_id, visibility=None):
    visibility = visibility or privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    return visibility


async def load_sticky_segments(user_id: str, conversation_id: str) -> dict[float, dict]:
    """Segment starts the user has already ruled on — never re-scored.

    Maps start key -> {status, bucket_type}. "restored" segments must never be
    re-marked background; "confirmed" segments must always BE background (the
    user endorsed the removal), regardless of what a fresh score would say.
    """
    visibility = await require_recording(conversation_id)
    docs = (
        await _ledger_collection()
        .find(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "status": {"$in": sorted(STICKY_STATUSES)},
            }
        )
        .to_list(None)
    )
    docs = await visibility.filter(docs)
    result = {
        doc["segment_start"]: {
            "status": doc["status"],
            "bucket_type": doc.get("bucket_type"),
        }
        for doc in docs
    }
    await visibility.assert_current()
    return result


async def record_conversation_suppressions(
    conversation_id: str,
    user_id: str,
    records: list[dict],
    source: str,
    prune: bool = True,
    *,
    privacy_visibility: privacy.ConversationPrivacyFilter | None = None,
) -> int:
    """Upsert ledger entries for one conversation's scored segments.

    ``records``: {segment_start, segment_end, text, background_similarity,
    foreground_similarity, bucket_type, zone, embedding?, previous_identified_as?,
    previous_confidence?}. Foreground-zone records are not stored. Entries whose
    status the user already settled (restored/confirmed) are left untouched.

    ``prune`` (pass False after a partial scan): a call represents a complete
    re-score of the conversation, so shadow/queued entries for segments no
    longer in any zone are stale — e.g. after a threshold retune — and are
    removed. "applied" entries survive pruning: they mirror a relabel that is
    actually in the transcript, and deleting them would orphan the restore path.
    """
    visibility = await require_recording(conversation_id, privacy_visibility)
    assign_cluster_signatures(records)
    reference_receipt = await visibility.reference_receipt(user_id)
    now = datetime.now(timezone.utc)
    written = 0
    kept_keys: list[float] = []
    for record in records:
        zone = record["zone"]
        if zone == "foreground":
            continue
        kept_keys.append(segment_key(record["segment_start"]))
        if zone == "confident_background":
            status = "applied" if source == "speaker_job" else "shadow"
        else:
            status = "queued"
        key = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "segment_start": segment_key(record["segment_start"]),
        }
        existing = await _ledger_collection().find_one(key, {"status": 1})
        await visibility.assert_current()
        if existing and existing.get("status") in STICKY_STATUSES:
            continue
        await visibility.assert_current()
        await _ledger_collection().update_one(
            key,
            {
                "$set": {
                    "privacy_reference_receipt": reference_receipt,
                    "segment_end": segment_key(record["segment_end"]),
                    "text": record.get("text"),
                    "cluster_signature": record.get("cluster_signature"),
                    "background_similarity": round(
                        float(record["background_similarity"]), 4
                    ),
                    "foreground_similarity": round(
                        float(record["foreground_similarity"]), 4
                    ),
                    "bucket_type": record.get("bucket_type"),
                    "zone": zone,
                    "status": status,
                    "source": source,
                    "previous_identified_as": record.get("previous_identified_as"),
                    "previous_confidence": record.get("previous_confidence"),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        await visibility.assert_current()
        written += 1
    if prune:
        await visibility.assert_current()
        result = await _ledger_collection().delete_many(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "segment_start": {"$nin": kept_keys},
                "status": {"$in": ["shadow", "queued"]},
            }
        )
        await visibility.assert_current()
        if result.deleted_count:
            logger.info(
                "Background suppression ledger: pruned %d stale entries for %s",
                result.deleted_count,
                conversation_id[:8],
            )
    if written:
        logger.info(
            "Background suppression ledger: %d entries for conversation %s (%s)",
            written,
            conversation_id[:8],
            source,
        )
    await visibility.assert_current()
    return written
