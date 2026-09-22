"""Backfill the background-suppression ledger for existing conversations.

Reuses embeddings cached in ``background_corpus_embeddings`` (built by the
corpus index job), so no speaker-service traffic. Backfilled confident-zone
entries get status ``shadow`` — transcripts are never touched retroactively;
the conversation page just shows what the current bucket *would* mark.

Deliberate media uploads are skipped: their media is the subject (see the
media-dominant guard in Docs/background-suppression-design.md — until that
ships, upload conversations are simply exempt).
"""

import logging

import numpy as np

from backend.models.conversation import Conversation
from backend.services import privacy
from backend.workers import background_suppression

logger = logging.getLogger(__name__)

UPLOAD_CLIENT_MARKER = "speaker-mining"


def _database():
    return Conversation.get_pymongo_collection().database


def _normalise(rows: list[list[float]]) -> np.ndarray:
    matrix = np.asarray(rows, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    return matrix


async def _exemplar_matrices(
    user_id: str, *, visibility: privacy.ConversationPrivacyFilter | None = None
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    database = _database()
    by_type: dict[str, list[list[float]]] = {}
    references = [
        doc
        async for doc in database["background_clips"].find(
            {"user_id": user_id, "embedding": {"$ne": None}},
            {
                "embedding": 1,
                "bucket_type": 1,
                "conversation_id": 1,
                "user_id": 1,
                "privacy_reference_receipt": 1,
            },
        )
    ]
    for doc in await visibility.filter_embeddings(references):
        bucket_type = doc.get("bucket_type")
        if bucket_type:
            by_type.setdefault(bucket_type, []).append(doc["embedding"])
    foreground_rows = [
        doc
        async for doc in database["background_foreground_clips"].find(
            {"requested_by": user_id, "embedding": {"$ne": None}},
            {
                "embedding": 1,
                "conversation_id": 1,
                "requested_by": 1,
                "privacy_reference_receipt": 1,
            },
        )
    ]
    foreground = [
        doc["embedding"] for doc in await visibility.filter_embeddings(foreground_rows)
    ]
    await visibility.assert_current()
    return (
        {kind: _normalise(rows) for kind, rows in by_type.items()},
        _normalise(foreground) if foreground else np.zeros((0, 1), dtype=np.float32),
    )


async def _is_upload_conversation(conversation_id: str) -> bool:
    doc = await _database()["conversations"].find_one(
        {"conversation_id": conversation_id}, {"client_id": 1, "title": 1}
    )
    if not doc:
        return False
    return UPLOAD_CLIENT_MARKER in (doc.get("client_id") or "") or "upload" in (
        doc.get("title") or ""
    )


async def backfill_conversation_suppressions(
    user_id: str, conversation_id: str
) -> dict:
    """Score one conversation's indexed clips against the current buckets."""
    visibility = privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    if await _is_upload_conversation(conversation_id):
        return {"conversation_id": conversation_id, "skipped": "upload"}
    if await background_suppression.get_subject_override(user_id, conversation_id):
        return {"conversation_id": conversation_id, "skipped": "subject_override"}

    rows = [
        row
        async for row in _database()["background_corpus_embeddings"].find(
            {
                "requested_by": user_id,
                "conversation_id": conversation_id,
                "candidate_type": "background_speech",
                "embedding": {"$ne": None},
            },
            {"_id": 0},
        )
    ]
    rows = await visibility.filter_embeddings(rows)
    if not rows:
        return {"conversation_id": conversation_id, "skipped": "no_indexed_clips"}

    buckets, foreground = await _exemplar_matrices(user_id, visibility=visibility)
    await visibility.assert_current()
    if not buckets:
        return {"conversation_id": conversation_id, "skipped": "no_exemplars"}

    query = _normalise([row["embedding"] for row in rows])
    similarity_by_type = {
        kind: (query @ matrix.T).max(axis=1) for kind, matrix in buckets.items()
    }
    foreground_similarity = (
        (query @ foreground.T).max(axis=1)
        if foreground.size
        else np.zeros(len(rows), dtype=np.float32)
    )

    records = []
    for index, row in enumerate(rows):
        scores = {
            kind: float(values[index]) for kind, values in similarity_by_type.items()
        }
        best_type, best_score = max(scores.items(), key=lambda pair: pair[1])
        fg_score = max(
            float(foreground_similarity[index]),
            float(row.get("stored_confidence") or 0.0),
        )
        zone = background_suppression.zone_for(best_score, fg_score)
        if zone == "foreground":
            continue
        records.append(
            {
                "segment_start": row["start"],
                "segment_end": row["end"],
                "text": row.get("text"),
                "background_similarity": best_score,
                "foreground_similarity": fg_score,
                "bucket_type": best_type,
                "zone": zone,
                "embedding": row["embedding"],
            }
        )
    written = await background_suppression.record_conversation_suppressions(
        conversation_id,
        user_id,
        records,
        source="backfill",
        privacy_visibility=visibility,
    )
    await visibility.assert_current()
    return {"conversation_id": conversation_id, "written": written}


async def backfill_all_suppressions(user_id: str, limit: int = 500) -> dict:
    """Backfill every conversation present in the corpus index."""
    conversation_ids = await _database()["background_corpus_embeddings"].distinct(
        "conversation_id", {"requested_by": user_id}
    )
    results = {"conversations": 0, "written": 0, "skipped": 0, "privacy_held": 0}
    for conversation_id in conversation_ids[:limit]:
        try:
            outcome = await backfill_conversation_suppressions(user_id, conversation_id)
        except privacy.PrivacyHeld:
            results["privacy_held"] += 1
            continue
        results["conversations"] += 1
        if "written" in outcome:
            results["written"] += outcome["written"]
        else:
            results["skipped"] += 1
    logger.info("Background suppression backfill: %s", results)
    return results
