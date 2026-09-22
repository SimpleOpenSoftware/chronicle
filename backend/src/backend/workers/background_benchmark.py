"""Leakage-safe evaluation of foreground/background review decisions."""

import hashlib
from collections import Counter

import numpy as np

from backend.models.conversation import Conversation
from backend.services import privacy
from backend.workers.background_suppression import (
    CONFIDENT_MARGIN,
    CONFIDENT_SIMILARITY,
)

BACKGROUND_DECISIONS = {"noise", "background_speech"}
FOREGROUND_DECISIONS = {"not_background", "skip"}
SAMPLES_PER_REVIEW = 5


def _normalise(matrix: np.ndarray) -> np.ndarray:
    matrix = matrix.astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    return matrix


def _signature(row: dict) -> str:
    text = " ".join(str(row.get("text") or "").lower().split())
    start = round(float(row.get("start") or 0), 2)
    end = round(float(row.get("end") or 0), 2)
    material = f"{row.get('candidate_type')}|{start}|{end}|{text}"
    if not text:
        material += "|" + ",".join(f"{float(value):.3f}" for value in row["embedding"])
    return hashlib.sha256(material.encode()).hexdigest()


def _central_samples(rows: list[dict], limit: int = SAMPLES_PER_REVIEW) -> list[dict]:
    if len(rows) <= limit:
        return rows
    matrix = _normalise(np.asarray([row["embedding"] for row in rows]))
    centroid = matrix.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    indices = np.argsort(-(matrix @ centroid))[:limit]
    return [rows[int(index)] for index in indices]


def _baseline_prediction(example: dict) -> bool:
    """Before learning: transcript gaps are background; speech is foreground."""
    return example.get("candidate_type") == "noise"


def _adapted_prediction(example: dict, training: list[dict]) -> tuple[bool, dict]:
    if not training:
        prediction = _baseline_prediction(example)
        return prediction, {"background_similarity": 0.0, "foreground_similarity": 0.0}
    query = _normalise(np.asarray([example["embedding"]]))[0]
    background = [item["embedding"] for item in training if item["is_background"]]
    foreground = [item["embedding"] for item in training if not item["is_background"]]
    background_similarity = (
        float((_normalise(np.asarray(background)) @ query).max()) if background else 0.0
    )
    foreground_similarity = (
        float((_normalise(np.asarray(foreground)) @ query).max()) if foreground else 0.0
    )
    prediction = (
        background_similarity >= CONFIDENT_SIMILARITY
        and background_similarity >= foreground_similarity + CONFIDENT_MARGIN
    )
    return prediction, {
        "background_similarity": round(background_similarity, 4),
        "foreground_similarity": round(foreground_similarity, 4),
    }


def _metrics(truth: list[bool], predictions: list[bool]) -> dict:
    tp = sum(actual and predicted for actual, predicted in zip(truth, predictions))
    fp = sum(not actual and predicted for actual, predicted in zip(truth, predictions))
    fn = sum(actual and not predicted for actual, predicted in zip(truth, predictions))
    tn = sum(
        not actual and not predicted for actual, predicted in zip(truth, predictions)
    )
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / len(truth) if truth else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(accuracy, 4),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "samples": len(truth),
    }


def evaluate_reviews(reviews: list[dict]) -> dict:
    examples = [example for review in reviews for example in review["examples"]]
    truth = [example["is_background"] for example in examples]
    baseline_predictions = [_baseline_prediction(example) for example in examples]

    adapted_predictions = []
    errors = []
    for review in reviews:
        test_signatures = {
            example["content_signature"] for example in review["examples"]
        }
        training = [
            example
            for other in reviews
            if other["cluster_id"] != review["cluster_id"]
            for example in other["examples"]
            if example["content_signature"] not in test_signatures
        ]
        for example in review["examples"]:
            prediction, scores = _adapted_prediction(example, training)
            adapted_predictions.append(prediction)
            if prediction != example["is_background"]:
                errors.append(
                    {
                        key: example.get(key)
                        for key in (
                            "clip_key",
                            "conversation_id",
                            "conversation_title",
                            "start",
                            "end",
                            "text",
                            "decision",
                        )
                    }
                    | {
                        "predicted": "background" if prediction else "foreground",
                        **scores,
                    }
                )

    learning_curve = []
    for count in range(len(reviews)):
        training_reviews = reviews[:count]
        evaluation_reviews = reviews[count:]
        training = [
            example for review in training_reviews for example in review["examples"]
        ]
        curve_truth, curve_predictions = [], []
        for review in evaluation_reviews:
            test_signatures = {
                example["content_signature"] for example in review["examples"]
            }
            safe_training = [
                example
                for example in training
                if example["content_signature"] not in test_signatures
            ]
            for example in review["examples"]:
                prediction, _ = _adapted_prediction(example, safe_training)
                curve_truth.append(example["is_background"])
                curve_predictions.append(prediction)
        metric = _metrics(curve_truth, curve_predictions)
        learning_curve.append(
            {"annotations": count, "f1": metric["f1"], "samples": metric["samples"]}
        )

    baseline = _metrics(truth, baseline_predictions)
    adapted = _metrics(truth, adapted_predictions)
    return {
        "baseline": baseline,
        "adapted": adapted,
        "f1_change": round(adapted["f1"] - baseline["f1"], 4),
        "learning_curve": learning_curve,
        "errors": errors[:10],
        "reviewed_samples": len(examples),
    }


async def build_background_benchmark(requested_by: str) -> dict:
    visibility = privacy.ConversationPrivacyFilter()
    database = Conversation.get_pymongo_collection().database
    corpus_rows = [
        row
        async for row in database["background_corpus_embeddings"].find(
            {"requested_by": requested_by}, {"_id": 0}
        )
    ]
    corpus_rows = await visibility.filter_embeddings(corpus_rows)
    await visibility.assert_current()
    by_key = {row["clip_key"]: row for row in corpus_rows}
    review_docs = [
        row
        async for row in database["background_cluster_reviews"]
        .find({"requested_by": requested_by}, {"_id": 0})
        .sort("reviewed_at", 1)
    ]
    reviews = []
    reconstructed_review_samples = False
    await visibility.assert_current()
    for review in review_docs:
        if not review.get("member_keys") or not set(review["member_keys"]) <= set(
            by_key
        ):
            continue
        decision = review.get("decision")
        if decision not in {"background_speech", "mixed"} | FOREGROUND_DECISIONS:
            continue
        members = [
            by_key[key] for key in review.get("member_keys", []) if key in by_key
        ]
        members = [
            member
            for member in members
            if member.get("candidate_type") == "background_speech"
        ]
        stored_sample_keys = review.get("review_sample_keys") or []
        if decision == "mixed":
            samples = members
        elif stored_sample_keys:
            member_by_key = {member["clip_key"]: member for member in members}
            samples = [
                member_by_key[key] for key in stored_sample_keys if key in member_by_key
            ]
        else:
            samples = _central_samples(members)
            reconstructed_review_samples = True
        examples = []
        for sample in samples:
            sample_decision = (
                (review.get("sample_decisions") or {}).get(sample["clip_key"])
                if decision == "mixed"
                else decision
            )
            if sample_decision not in {"background_speech"} | FOREGROUND_DECISIONS:
                continue
            examples.append(
                {
                    **sample,
                    "decision": sample_decision,
                    "is_background": sample_decision in BACKGROUND_DECISIONS,
                    "content_signature": _signature(sample),
                }
            )
        if examples:
            reviews.append(
                {
                    "cluster_id": review["cluster_id"],
                    "decision": decision,
                    "reviewed_at": review.get("reviewed_at"),
                    "examples": examples,
                }
            )
    decisions = Counter(
        example["decision"] for review in reviews for example in review["examples"]
    )
    positives = decisions.get("background_speech", 0)
    negatives = sum(decisions.get(decision, 0) for decision in FOREGROUND_DECISIONS)
    if not positives or not negatives:
        return {
            "ready": False,
            "reason": (
                "Hard-case F1 is not measurable yet. Review at least one Background "
                "Speech cluster and one foreground speech cluster."
            ),
            "reviewed_clusters": len(reviews),
            "decision_counts": dict(decisions),
            "background_speech_samples": positives,
            "foreground_speech_samples": negatives,
        }
    evaluated = evaluate_reviews(reviews)
    return {
        "ready": True,
        "method": "cluster-held-out cross-validation",
        "reconstructed_review_samples": reconstructed_review_samples,
        "reviewed_clusters": len(reviews),
        "decision_counts": dict(decisions),
        "background_speech_samples": sum(
            example["decision"] == "background_speech"
            for review in reviews
            for example in review["examples"]
        ),
        **evaluated,
    }
