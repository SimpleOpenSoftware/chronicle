"""Evaluate and safely apply reviewed background references to existing transcripts."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone

import numpy as np
from rq import get_current_job

from backend.constants import BACKGROUND_SPEECH_LABEL, NOISE_LABEL
from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy

HIGH_THRESHOLD = 0.62
MARGIN = 0.05
AMBIGUOUS_THRESHOLD = 0.52
AMBIGUOUS_BAND = 0.12
REPORT_VERSION = 3


def _normalise(values: list[list[float]]) -> np.ndarray:
    if not values:
        return np.empty((0, 0), dtype=np.float32)
    matrix = np.asarray(values, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    return matrix


def _max_scores(query: np.ndarray, references: np.ndarray) -> np.ndarray:
    if references.size == 0:
        return np.zeros(query.shape[0], dtype=np.float32)
    return (query @ references.T).max(axis=1)


def _score_rows(
    rows: list[dict], buckets: dict[str, list[dict]], foreground: list[dict]
) -> list[dict]:
    speech_rows = [
        row for row in rows if row.get("candidate_type") == "background_speech"
    ]
    if not speech_rows:
        return []
    query = _normalise([row["embedding"] for row in speech_rows])
    speech_scores = _max_scores(
        query, _normalise([row["embedding"] for row in buckets["background_speech"]])
    )
    foreground_scores = _max_scores(
        query, _normalise([row["embedding"] for row in foreground])
    )
    results = []
    for index, row in enumerate(speech_rows):
        background_speech_score = float(speech_scores[index])
        bucket_type, background_score, proposed_label = (
            "background_speech",
            background_speech_score,
            BACKGROUND_SPEECH_LABEL,
        )
        stored_foreground = float(row.get("stored_confidence") or 0.0)
        foreground_score = max(float(foreground_scores[index]), stored_foreground)
        margin = background_score - foreground_score
        if background_score >= HIGH_THRESHOLD and margin >= MARGIN:
            tier = "high"
        elif background_score >= AMBIGUOUS_THRESHOLD and abs(margin) <= AMBIGUOUS_BAND:
            tier = "ambiguous"
        else:
            continue
        results.append(
            {
                "clip_key": row["clip_key"],
                "conversation_id": row["conversation_id"],
                "conversation_title": row.get("conversation_title"),
                "segment_index": row.get("segment_index"),
                "start": row["start"],
                "end": row["end"],
                "text": row.get("text") or "",
                "current_label": row.get("current_label"),
                "proposed_label": proposed_label,
                "bucket_type": bucket_type,
                "background_score": round(background_score, 4),
                "foreground_score": round(foreground_score, 4),
                "margin": round(margin, 4),
                "tier": tier,
            }
        )
    return results


async def build_background_cleanup_report(requested_by: str) -> dict:
    visibility = privacy.ConversationPrivacyFilter()
    visibility.snapshots[requested_by] = await privacy.load_snapshot(requested_by)
    database = Conversation.get_pymongo_collection().database
    corpus = database["background_corpus_embeddings"]
    latest = await corpus.find_one(
        {"requested_by": requested_by}, sort=[("indexed_at", -1)]
    )
    if not latest:
        return {"ready": False, "reason": "Corpus has not been sampled"}
    model = latest["embedding_model"]
    rows = [
        row
        async for row in corpus.find(
            {"requested_by": requested_by, "embedding_model": model}, {"_id": 0}
        )
    ]
    rows = await visibility.filter_embeddings(rows)
    bucket_docs = [
        row
        async for row in database["background_clips"].find(
            {"user_id": requested_by, "embedding_model": model}, {"_id": 0}
        )
    ]
    bucket_docs = await visibility.filter_embeddings(bucket_docs)
    buckets = {
        kind: [row for row in bucket_docs if row.get("bucket_type") == kind]
        for kind in ("noise", "background_speech")
    }
    foreground = [
        row
        async for row in database["background_foreground_clips"].find(
            {"requested_by": requested_by, "embedding_model": model}, {"_id": 0}
        )
    ]
    foreground = await visibility.filter_embeddings(foreground)
    await visibility.assert_current()
    if not buckets["noise"] and not buckets["background_speech"]:
        return {"ready": False, "reason": "No confirmed background samples yet"}
    scored = _score_rows(rows, buckets, foreground)
    high = [item for item in scored if item["tier"] == "high"]
    ambiguous = [item for item in scored if item["tier"] == "ambiguous"]
    high.sort(key=lambda item: item["margin"], reverse=True)
    ambiguous.sort(key=lambda item: abs(item["margin"]))
    material = "|".join(
        [model, json.dumps(visibility.revision_receipt(), sort_keys=True)]
        + sorted(
            f"{row.get('conversation_id')}:{row.get('segment_start')}:{row.get('bucket_type')}"
            for row in bucket_docs
        )
        + sorted(row.get("clip_key", "") for row in foreground)
        + sorted(item["clip_key"] for item in high)
    )
    report_id = hashlib.sha256(material.encode()).hexdigest()[:20]
    report = {
        "ready": True,
        "report_version": REPORT_VERSION,
        "report_id": report_id,
        "embedding_model": model,
        "generated_at": datetime.now(timezone.utc),
        "reference_counts": {
            "noise": len(buckets["noise"]),
            "background_speech": len(buckets["background_speech"]),
            "foreground": len(foreground),
        },
        "high_confidence": len(high),
        "ambiguous": len(ambiguous),
        "conversations_affected": len({item["conversation_id"] for item in high}),
        "proposed_counts": {
            "noise": sum(item["bucket_type"] == "noise" for item in high),
            "background_speech": sum(
                item["bucket_type"] == "background_speech" for item in high
            ),
        },
        "high_samples": high[:10],
        "ambiguous_samples": ambiguous[:10],
        "recommendation": (
            "No transcript cleanup is recommended because no Background Speech "
            "references were confirmed. Noise references describe transcript gaps "
            "and are never used to relabel speech."
            if not buckets["background_speech"]
            else "Spot-check the proposed changes before applying them."
        ),
    }
    await visibility.assert_current()
    await database["background_cleanup_reports"].update_one(
        {"requested_by": requested_by, "report_id": report_id},
        {
            "$set": {
                **report,
                "privacy_revisions": visibility.revision_receipt(),
                "privacy_reference_receipt": await visibility.reference_receipt(
                    requested_by
                ),
                "evidence_conversation_ids": sorted(
                    {row["conversation_id"] for row in rows + bucket_docs + foreground}
                ),
                "high_changes": [
                    {
                        key: item[key]
                        for key in (
                            "conversation_id",
                            "segment_index",
                            "start",
                            "proposed_label",
                            "background_score",
                        )
                    }
                    for item in high
                ],
            }
        },
        upsert=True,
    )
    await visibility.assert_current()
    return report


async def require_cleanup_report(report, requested_by):
    """A proposal keeps the policy and all reference evidence used to score it."""
    if report.get("report_version") != REPORT_VERSION:
        raise privacy.PrivacyHeld()
    visibility = privacy.ConversationPrivacyFilter()
    visibility.snapshots[str(requested_by)] = await privacy.load_snapshot(requested_by)
    await visibility.require_receipt(report.get("privacy_revisions"))
    await visibility.require_reference_receipt(
        requested_by, report.get("privacy_reference_receipt")
    )
    identifiers = report.get("evidence_conversation_ids")
    if not isinstance(identifiers, list) or not identifiers:
        raise privacy.PrivacyHeld()
    records = [{"conversation_id": identifier} for identifier in identifiers]
    if len(await visibility.filter(records)) != len(records):
        raise privacy.PrivacyHeld()
    await visibility.require_receipt(report.get("privacy_revisions"))
    return visibility


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


@async_job(redis=False, beanie=True, timeout=3600)
async def apply_background_cleanup_job(requested_by: str, report_id: str) -> dict:
    database = Conversation.get_pymongo_collection().database
    report = await database["background_cleanup_reports"].find_one(
        {"requested_by": requested_by, "report_id": report_id}
    )
    if not report:
        raise ValueError("Background cleanup report not found")
    visibility = await require_cleanup_report(report, requested_by)
    grouped: dict[str, list[dict]] = {}
    for change in report.get("high_changes") or []:
        grouped.setdefault(change["conversation_id"], []).append(change)
    updated = skipped = segments_changed = 0
    for current, (conversation_id, changes) in enumerate(grouped.items(), start=1):
        doc = await Conversation.get_pymongo_collection().find_one(
            {"conversation_id": conversation_id, "user_id": requested_by}
        )
        if not doc:
            skipped += 1
            continue
        if not await visibility.filter([doc]):
            raise privacy.PrivacyHeld()
        await visibility.assert_current()
        versions = doc.get("transcript_versions") or []
        active_id = doc.get("active_transcript_version")
        active = next(
            (version for version in versions if version.get("version_id") == active_id),
            None,
        )
        if (
            not active
            or active.get("metadata", {}).get("background_cleanup_report") == report_id
        ):
            skipped += 1
            continue
        new_version = copy.deepcopy(active)
        new_version_id = str(uuid.uuid4())
        new_version["version_id"] = new_version_id
        new_version["created_at"] = datetime.now(timezone.utc)
        new_version.setdefault("metadata", {}).update(
            {
                "background_cleanup_report": report_id,
                "background_cleanup_source_version": active_id,
            }
        )
        changed_here = 0
        for change in changes:
            index = int(change["segment_index"])
            segments = new_version.get("segments") or []
            if index < 0 or index >= len(segments):
                continue
            segment = segments[index]
            if abs(float(segment.get("start", -1)) - float(change["start"])) > 0.05:
                continue
            label = change["proposed_label"]
            segment["speaker"] = label
            segment["identified_as"] = label
            segment["confidence"] = change["background_score"]
            segment["segment_type"] = "event" if label == NOISE_LABEL else "speech"
            changed_here += 1
        if not changed_here:
            skipped += 1
            continue
        async with visibility.publication():
            await visibility.assert_current()
            result = await Conversation.get_pymongo_collection().update_one(
                {"_id": doc["_id"], "active_transcript_version": active_id},
                {
                    "$push": {"transcript_versions": new_version},
                    "$set": {"active_transcript_version": new_version_id},
                },
            )
            await visibility.assert_current()
        if not result.matched_count:
            skipped += 1
            continue
        updated += 1
        segments_changed += changed_here
        _progress(current, len(grouped), f"Applying cleanup {current}/{len(grouped)}")
    await visibility.assert_current()
    return {
        "report_id": report_id,
        "conversations_updated": updated,
        "conversations_skipped": skipped,
        "segments_changed": segments_changed,
        "memory_reprocessed": False,
    }
