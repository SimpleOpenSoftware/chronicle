"""Conversation-grouped cross-validation for human-labeled speaker clips."""

import asyncio
import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
from rq import get_current_job

from backend.config import get_diarization_settings
from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import reconstruct_audio_segment

FOLDS = 5
FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)
MIN_SECONDS = 1.5


def _unit(values: list) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if not vector.size or not np.isfinite(vector).all() or norm == 0:
        raise ValueError("invalid embedding")
    return vector / norm


def _stable(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _active_segments(doc: dict) -> list:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    version = next(
        (item for item in versions if item.get("version_id") == active_id), None
    )
    return (version or (versions[-1] if versions else {})).get("segments") or []


async def _labeled_clips(
    user_id: str, *, visibility=None
) -> tuple[List[dict], Dict[str, int]]:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    db = Conversation.get_pymongo_collection().database
    clips: List[dict] = []
    exclusions = {
        "bad_or_mixed": 0,
        "missing_bounds": 0,
        "too_short": 0,
        "duplicate": 0,
        "privacy_held": 0,
    }

    async for review in db["enrollment_reviews"].find(
        {"reviewed_by": user_id},
        {
            "conversation_id": 1,
            "decision": 1,
            "actual_speaker": 1,
            "selected_start": 1,
            "selected_end": 1,
            "segment_start": 1,
            "segment_end": 1,
        },
    ):
        if not await visibility.filter([review]):
            exclusions["privacy_held"] += 1
            continue
        if review.get("decision") not in ("accept", "another_speaker"):
            exclusions["bad_or_mixed"] += 1
            continue
        start = review.get("selected_start", review.get("segment_start"))
        end = review.get("selected_end", review.get("segment_end"))
        if start is None or end is None:
            exclusions["missing_bounds"] += 1
            continue
        clips.append(
            {
                "conversation_id": review["conversation_id"],
                "start": float(start),
                "end": float(end),
                "speaker": review.get("actual_speaker"),
                "source": "guided_enrollment",
            }
        )

    async for annotation in db["annotations"].find(
        {
            "user_id": user_id,
            "annotation_type": "diarization",
            "status": "accepted",
            "corrected_speaker": {"$nin": [None, "", "Noise", "Unknown Speaker"]},
        },
        {
            "conversation_id": 1,
            "segment_index": 1,
            "segment_start_time": 1,
            "corrected_speaker": 1,
        },
    ):
        if not await visibility.filter([annotation]):
            exclusions["privacy_held"] += 1
            continue
        doc = await Conversation.get_pymongo_collection().find_one(
            {"conversation_id": annotation.get("conversation_id")},
            {"active_transcript_version": 1, "transcript_versions": 1},
        )
        segments = _active_segments(doc or {})
        index = annotation.get("segment_index")
        segment = (
            segments[index]
            if isinstance(index, int) and 0 <= index < len(segments)
            else None
        )
        if segment is None and annotation.get("segment_start_time") is not None:
            target = float(annotation["segment_start_time"])
            segment = min(
                segments,
                key=lambda item: abs(float(item.get("start", 0)) - target),
                default=None,
            )
        if not segment:
            exclusions["missing_bounds"] += 1
            continue
        clips.append(
            {
                "conversation_id": annotation["conversation_id"],
                "start": float(segment.get("start", 0)),
                "end": float(segment.get("end", 0)),
                "speaker": annotation["corrected_speaker"],
                "source": "diarization_annotation",
            }
        )

    unique = {}
    for clip in clips:
        if not clip.get("speaker") or clip["end"] - clip["start"] < MIN_SECONDS:
            exclusions["too_short"] += 1
            continue
        key = f'{clip["conversation_id"]}:{clip["start"]:.3f}:{clip["end"]:.3f}:{clip["speaker"]}'
        if key in unique:
            exclusions["duplicate"] += 1
            continue
        clip["key"] = key
        unique[key] = clip
    await visibility.assert_current()
    return list(unique.values()), exclusions


async def _embed_clips(
    clips: List[dict], user_id: str, embedding_model: str, *, visibility=None
) -> tuple[List[dict], int, List[dict]]:
    visibility = visibility or privacy.ConversationPrivacyFilter()
    if len(await visibility.filter(clips)) != len(clips):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    db = Conversation.get_pymongo_collection().database
    cache = db["speaker_evaluation_embeddings"]
    client = SpeakerRecognitionClient()
    embedded = []
    failures = []
    cache_hits = 0
    job = get_current_job()
    for index, clip in enumerate(clips):
        await visibility.assert_current()
        cached = await cache.find_one(
            {
                "user_id": user_id,
                "clip_key": clip["key"],
                "embedding_model": embedding_model,
            }
        )
        await visibility.assert_current()
        if cached:
            result = cached
            cache_hits += 1
        else:
            try:
                wav = await reconstruct_audio_segment(
                    clip["conversation_id"], clip["start"], clip["end"]
                )
                await visibility.assert_current()
                result = await client.extract_speaker_embedding(wav)
                await visibility.assert_current()
                if result.get("error"):
                    raise RuntimeError("Speaker embedding failed")
                async with visibility.publication():
                    await cache.update_one(
                        {
                            "user_id": user_id,
                            "clip_key": clip["key"],
                            "embedding_model": embedding_model,
                        },
                        {
                            "$set": {
                                **clip,
                                **result,
                                "user_id": user_id,
                                "embedding_model": embedding_model,
                                "created_at": datetime.now(timezone.utc),
                            }
                        },
                        upsert=True,
                    )
            except privacy.PrivacyHeld:
                raise
            except Exception as exc:
                failures.append({"clip_key": clip["key"], "error": type(exc).__name__})
                continue
        try:
            embedded.append({**clip, "embedding": _unit(result["embedding"])})
        except Exception as exc:
            failures.append({"clip_key": clip["key"], "error": type(exc).__name__})
        if job and (index % 5 == 0 or index + 1 == len(clips)):
            job.meta["batch_progress"] = {
                "current": index + 1,
                "total": len(clips),
                "message": f"Embedding labeled clips {index + 1}/{len(clips)}",
            }
            job.save_meta()
    await visibility.assert_current()
    return embedded, cache_hits, failures


def _centroids(samples: List[dict]) -> Dict[str, np.ndarray]:
    grouped: Dict[str, list] = {}
    for sample in samples:
        grouped.setdefault(sample["speaker"], []).append(sample["embedding"])
    return {
        speaker: _unit(np.mean(vectors, axis=0).tolist())
        for speaker, vectors in grouped.items()
    }


def _metrics(train: List[dict], test: List[dict], threshold: float) -> dict:
    centroids = _centroids(train)
    evaluable = [sample for sample in test if sample["speaker"] in centroids]
    if not evaluable or len(centroids) < 2:
        return {
            "test_clips": len(evaluable),
            "top1_accuracy": None,
            "macro_recall": None,
            "false_accept_rate": None,
        }
    correct = 0
    by_speaker: Dict[str, list] = {}
    confusion: Dict[str, Dict[str, int]] = {}
    target_scores = []
    impostor_scores = []
    wrong_trials = wrong_accepts = 0
    for sample in evaluable:
        scores = {
            speaker: float(sample["embedding"] @ centroid)
            for speaker, centroid in centroids.items()
        }
        predicted = max(scores, key=scores.get)
        hit = predicted == sample["speaker"]
        correct += int(hit)
        by_speaker.setdefault(sample["speaker"], []).append(int(hit))
        confusion.setdefault(sample["speaker"], {})[predicted] = (
            confusion.setdefault(sample["speaker"], {}).get(predicted, 0) + 1
        )
        target_scores.append(scores[sample["speaker"]])
        for speaker, score in scores.items():
            if speaker != sample["speaker"]:
                impostor_scores.append(score)
                wrong_trials += 1
                wrong_accepts += int(score >= threshold)
    recalls = [sum(values) / len(values) for values in by_speaker.values()]
    thresholds = sorted(set(target_scores + impostor_scores))
    eer = None
    if target_scores and impostor_scores and thresholds:
        candidates = []
        for candidate in thresholds:
            false_reject = sum(score < candidate for score in target_scores) / len(
                target_scores
            )
            false_accept = sum(score >= candidate for score in impostor_scores) / len(
                impostor_scores
            )
            candidates.append(
                (abs(false_reject - false_accept), (false_reject + false_accept) / 2)
            )
        eer = min(candidates, key=lambda item: item[0])[1]
    return {
        "test_clips": len(evaluable),
        "speakers": len(centroids),
        "top1_accuracy": round(correct / len(evaluable), 4),
        "macro_recall": round(sum(recalls) / len(recalls), 4),
        "false_accept_rate": (
            round(wrong_accepts / wrong_trials, 4) if wrong_trials else None
        ),
        "eer": round(eer, 4) if eer is not None else None,
        "per_speaker_recall": {
            speaker: round(sum(values) / len(values), 4)
            for speaker, values in sorted(by_speaker.items())
        },
        "confusion": confusion,
    }


def _evaluate(samples: List[dict], threshold: float) -> dict:
    groups = sorted({sample["conversation_id"] for sample in samples}, key=_stable)
    fold_by_group = {group: index % FOLDS for index, group in enumerate(groups)}
    curves = []
    fold_results = []
    for fraction in FRACTIONS:
        metrics_for_fraction = []
        for fold in range(FOLDS):
            test = [
                sample
                for sample in samples
                if fold_by_group[sample["conversation_id"]] == fold
            ]
            available = [
                sample
                for sample in samples
                if fold_by_group[sample["conversation_id"]] != fold
            ]
            train = []
            for speaker in sorted({sample["speaker"] for sample in available}):
                speaker_samples = sorted(
                    (sample for sample in available if sample["speaker"] == speaker),
                    key=lambda sample: _stable(sample["key"]),
                )
                take = max(1, math.ceil(len(speaker_samples) * fraction))
                train.extend(speaker_samples[:take])
            metrics = _metrics(train, test, threshold)
            metrics["fold"] = fold + 1
            metrics["train_clips"] = len(train)
            metrics_for_fraction.append(metrics)
            if fraction == 1.0:
                fold_results.append(metrics)
        valid = [
            item for item in metrics_for_fraction if item["top1_accuracy"] is not None
        ]
        valid_far = [item for item in valid if item["false_accept_rate"] is not None]
        valid_eer = [item for item in valid if item.get("eer") is not None]
        curves.append(
            {
                "fraction": fraction,
                "train_clips_mean": round(
                    sum(item["train_clips"] for item in metrics_for_fraction) / FOLDS, 1
                ),
                "top1_accuracy_mean": (
                    round(sum(item["top1_accuracy"] for item in valid) / len(valid), 4)
                    if valid
                    else None
                ),
                "macro_recall_mean": (
                    round(sum(item["macro_recall"] for item in valid) / len(valid), 4)
                    if valid
                    else None
                ),
                "false_accept_rate_mean": (
                    round(
                        sum(item["false_accept_rate"] for item in valid_far)
                        / len(valid_far),
                        4,
                    )
                    if valid_far
                    else None
                ),
                "eer_mean": (
                    round(sum(item["eer"] for item in valid_eer) / len(valid_eer), 4)
                    if valid_eer
                    else None
                ),
            }
        )
    return {
        "learning_curve": curves,
        "folds": fold_results,
        "conversation_groups": len(groups),
        "fold_groups": {
            str(fold + 1): sorted(
                group for group, assigned in fold_by_group.items() if assigned == fold
            )
            for fold in range(FOLDS)
        },
    }


@async_job(redis=False, beanie=True, timeout=7200)
async def run_speaker_benchmark_job(user_id: str) -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    visibility = privacy.ConversationPrivacyFilter()
    # Bind even an empty report to the requesting user's policy.
    visibility.snapshots[user_id] = await privacy.load_snapshot(user_id)
    clips, exclusions = await _labeled_clips(user_id, visibility=visibility)
    client = SpeakerRecognitionClient()
    embedding_info = await client.get_embedding_info()
    if embedding_info.get("error") or not embedding_info.get("embedding_model"):
        raise RuntimeError("Cannot determine embedding model")
    embedding_model = embedding_info["embedding_model"]
    embedded, cache_hits, failures = await _embed_clips(
        clips, user_id, embedding_model, visibility=visibility
    )
    threshold = float(get_diarization_settings().get("similarity_threshold", 0.5))
    await visibility.assert_current()
    evaluation = await asyncio.to_thread(_evaluate, embedded, threshold)
    await visibility.assert_current()
    report = {
        "evidence_conversation_ids": sorted(
            {clip["conversation_id"] for clip in clips}
        ),
        "privacy_revisions": visibility.revision_receipt(),
        "user_id": user_id,
        "created_at": started_at,
        "protocol": "5-fold conversation-grouped cross-validation",
        "fractions": list(FRACTIONS),
        "threshold": threshold,
        "embedding_model": embedding_model,
        "dataset": {
            "labeled_clips": len(clips),
            "embedded_clips": len(embedded),
            "speakers": len({sample["speaker"] for sample in embedded}),
            "cache_hits": cache_hits,
            "embedding_failures": len(failures),
            "exclusions": exclusions,
        },
        **evaluation,
        "failures": failures[:20],
    }
    db = Conversation.get_pymongo_collection().database
    async with visibility.publication():
        saved = await db["speaker_benchmark_runs"].insert_one(report)
    # Queue results outlive policy changes. The report route resolves current
    # evidence/privacy; Redis stores only an opaque reference and completion state.
    return {"status": "complete", "report_id": str(saved.inserted_id)}
