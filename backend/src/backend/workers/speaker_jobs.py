"""
Speaker recognition related RQ job functions.

This module contains all jobs related to speaker identification and recognition.
"""

import asyncio
import logging
import math
import time
import traceback
from bisect import bisect_right
from typing import Any, Dict

import numpy as np

from backend.auth import generate_jwt_for_user
from backend.config import get_diarization_settings, get_misc_settings
from backend.constants import (
    BACKGROUND_SPEECH_LABEL,
    NOISE_LABEL,
    UNKNOWN_SPEAKER_PREFIX,
)
from backend.controllers import background_bucket_controller
from backend.controllers.drift_controller import compute_cluster_centroids
from backend.models.annotation import (
    Annotation,
    AnnotationSource,
    AnnotationStatus,
    AnnotationType,
)
from backend.models.audio_capture import ConversationTranscriptRevision
from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.services import privacy
from backend.services.audio_claims import range_duration, resolve_conversation_audio
from backend.services.audio_stream import TranscriptionResultsAggregator
from backend.services.forced_alignment import (
    estimate_words_from_segment_timing,
    synthesize_words_via_alignment,
)
from backend.services.observability import record_event_sync
from backend.services.processing_artifacts import (
    persist_conversation_revision,
    persist_diarization_artifact,
    persist_timing_normalized_revision,
    persist_word_timed_revision,
    resolve_transcript_artifact_ids,
)
from backend.services.speaker_gallery_privacy import protect_result_logs, result_scope
from backend.services.timeline.dirty_ranges import note_conversation_dirty
from backend.services.transcript_integrity import (
    TranscriptTimingError,
    load_transcript_audio_ranges,
    validate_and_normalize_transcript_timing,
)
from backend.speaker_recognition_client import (
    SPEAKER_IDENTIFY_CONCURRENCY,
    SpeakerRecognitionClient,
)
from backend.users import get_user_by_id
from backend.utils.audio_chunk_utils import reconstruct_resolved_audio_ranges
from backend.utils.job_utils import update_job_meta
from backend.utils.segment_utils import classify_segment_text
from backend.workers import background_suppression

logger = logging.getLogger(__name__)


PROPAGATION_MIN_VOTES = 2
HUMAN_LABEL_START_TOLERANCE_SECONDS = 0.75
AUDIO_CONTINUITY_TOLERANCE_SECONDS = 0.25
BACKGROUND_AUDIO_BATCH_SIZE = 100
SPEAKER_BOUNDARY_CLIP_TOLERANCE_SECONDS = 0.05


def _audio_ranges_cover_continuously(
    ranges: list[tuple[float, float]], duration: float
) -> bool:
    """Whether full-conversation audio can be reconstructed without inventing time."""
    if not ranges or duration <= 0:
        return False
    ordered = sorted((float(start), float(end)) for start, end in ranges)
    if ordered[0][0] > AUDIO_CONTINUITY_TOLERANCE_SECONDS:
        return False
    covered_until = ordered[0][1]
    for start, end in ordered[1:]:
        if start - covered_until > AUDIO_CONTINUITY_TOLERANCE_SECONDS:
            return False
        covered_until = max(covered_until, end)
    return covered_until + AUDIO_CONTINUITY_TOLERANCE_SECONDS >= duration


def _normalize_speaker_segment_bounds(
    segments: list[dict[str, Any]], *, duration: float
) -> list[dict[str, Any]]:
    """Clip model-frame rounding to the exact immutable audio-claim boundary."""

    if duration <= 0 or not math.isfinite(duration):
        raise SpeakerDataIntegrityError(
            "Cannot normalize speaker turns without a positive audio duration"
        )
    normalized: list[dict[str, Any]] = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise SpeakerDataIntegrityError(
                f"Speaker service returned invalid turn bounds {start}-{end}"
            )
        if (
            start < -SPEAKER_BOUNDARY_CLIP_TOLERANCE_SECONDS
            or end > duration + SPEAKER_BOUNDARY_CLIP_TOLERANCE_SECONDS
        ):
            raise SpeakerDataIntegrityError(
                f"Speaker service turn {start}-{end} lies outside {duration:.3f}s audio"
            )
        clipped_start = min(max(start, 0.0), duration)
        clipped_end = min(max(end, 0.0), duration)
        if clipped_end <= clipped_start:
            raise SpeakerDataIntegrityError(
                "Speaker service turn collapsed while clipping to the audio claim: "
                f"{start}-{end} against {duration:.3f}s"
            )
        item = dict(segment)
        item["start"] = clipped_start
        item["end"] = clipped_end
        if "duration" in item:
            item["duration"] = clipped_end - clipped_start
        normalized.append(item)
    return normalized


def _project_source_words_onto_speaker_turns(
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assign every immutable ASR word to exactly one neural speaker turn.

    Pyannote intentionally leaves non-speech gaps. Provider-side midpoint matching can
    consequently omit words near a neural boundary or duplicate them when timelines
    overlap. The exclusive turns remain the speaker evidence; this projection rebuilds
    their text from the immutable word clock and chooses one deterministic nearest turn
    for each word. Returned provider text/word lists are never trusted as ownership.
    """

    ordered_segments = sorted(
        (dict(segment) for segment in segments),
        key=lambda segment: (
            float(segment.get("start", 0.0)),
            float(segment.get("end", 0.0)),
            str(segment.get("speaker", "")),
        ),
    )
    if words and not ordered_segments:
        raise SpeakerDataIntegrityError(
            "Cannot project timed transcript words without speaker turns"
        )

    segment_starts = [float(segment["start"]) for segment in ordered_segments]
    previous_end = 0.0
    for segment in ordered_segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if start < previous_end - 1e-6:
            raise SpeakerDataIntegrityError(
                "Cannot project words onto overlapping neural speaker turns"
            )
        previous_end = max(previous_end, end)

    assigned: list[list[dict[str, Any]]] = [[] for _ in ordered_segments]
    for word in words:
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
        text = str(word.get("word", ""))
        if (
            not text.strip()
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
        ):
            raise SpeakerDataIntegrityError(
                f"Cannot project invalid source word {text!r} at {start}-{end}"
            )
        midpoint = (start + end) / 2.0

        # Exclusive turns are sorted, so ownership needs only the turn immediately
        # before the word midpoint and the following turn. This is O(log T) per word,
        # not a W-by-T scan on long recordings.
        previous_index = bisect_right(segment_starts, midpoint) - 1
        if previous_index < 0:
            owner = 0
        elif midpoint <= float(ordered_segments[previous_index]["end"]):
            owner = previous_index
        elif previous_index + 1 >= len(ordered_segments):
            owner = previous_index
        else:
            next_index = previous_index + 1
            previous_distance = midpoint - float(
                ordered_segments[previous_index]["end"]
            )
            next_distance = float(ordered_segments[next_index]["start"]) - midpoint
            owner = previous_index if previous_distance <= next_distance else next_index
        assigned[owner].append(dict(word))

    projected: list[dict[str, Any]] = []
    for segment, segment_words in zip(ordered_segments, assigned, strict=True):
        item = dict(segment)
        item["words"] = segment_words
        item["text"] = " ".join(
            str(word["word"]).strip() for word in segment_words
        ).strip()
        projected.append(item)
    return projected


def _strip_reprojected_words_from_events(
    segments: list[Conversation.SpeakerSegment],
    projected_words: list[dict[str, Any]],
) -> list[Conversation.SpeakerSegment]:
    """Keep event boundaries without duplicating words owned by neural turns."""

    projected_keys = {
        (
            float(word.get("start", 0.0)),
            float(word.get("end", word.get("start", 0.0))),
            str(word.get("word", "")),
        )
        for word in projected_words
    }
    stripped: list[Conversation.SpeakerSegment] = []
    for segment in segments:
        item = segment.model_copy(deep=True)
        item.words = [
            word
            for word in item.words
            if (float(word.start), float(word.end), str(word.word))
            not in projected_keys
        ]
        stripped.append(item)
    return stripped


def _is_speech_segment(segment: Any) -> bool:
    """Recover ordinary speech when an imported provider mislabeled it as an event."""
    return (
        getattr(segment, "segment_type", "speech") == "speech"
        or classify_segment_text(getattr(segment, "text", "")) == "speech"
    )


def _retained_non_speech_segments(segments: list[Any]) -> list[Any]:
    """Keep meaningful events, not empty provider timing placeholders.

    Raw audio remains durably claimed for every interval. A provider row with no text
    and no words contributes no transcript evidence and must not be overlaid on the
    exclusive speaker timeline. Named events and event rows carrying words remain
    first-class boundaries for display consolidation.
    """
    return [
        segment
        for segment in segments
        if not _is_speech_segment(segment)
        and (
            bool((getattr(segment, "text", "") or "").strip())
            or bool(getattr(segment, "words", None))
        )
    ]


def _word_timeline_fallback_segments(
    words: list[dict[str, Any]],
    *,
    duration: float,
    max_gap: float = 2.0,
) -> list[dict[str, Any]]:
    """Build provider-independent speech spans after an empty pyannote result.

    Pyannote can occasionally return no neural speaker turns for a short clip even
    though the immutable ASR evidence contains timed words. Reusing the provider's
    utterance rows in that case preserves exactly the fragmentation this reprocessing
    pass exists to replace. Instead, group the source words only by real silence gaps,
    give every group one neutral label for downstream voice identification, and retain
    every source word exactly once.
    """

    if duration <= 0:
        raise SpeakerDataIntegrityError(
            "Cannot construct word-timeline fallback without positive audio duration"
        )
    if max_gap < 0:
        raise ValueError("max_gap must be non-negative")

    normalized: list[dict[str, Any]] = []
    for word in words:
        text = str(word.get("word", ""))
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
        if not text.strip():
            raise SpeakerDataIntegrityError(
                "Cannot construct word-timeline fallback from an empty word"
            )
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
        ):
            raise SpeakerDataIntegrityError(
                f"Cannot construct word-timeline fallback from invalid word bounds "
                f"{start}-{end}"
            )
        if end > duration + 1e-3:
            raise SpeakerDataIntegrityError(
                f"Cannot construct word-timeline fallback: word ends at {end:.3f}s "
                f"outside {duration:.3f}s audio"
            )
        normalized.append({**word, "start": start, "end": min(end, duration)})

    normalized.sort(key=lambda item: (item["start"], item["end"]))
    groups: list[list[dict[str, Any]]] = []
    for word in normalized:
        if groups and word["start"] - groups[-1][-1]["end"] <= max_gap:
            groups[-1].append(word)
        else:
            groups.append([word])

    segments: list[dict[str, Any]] = []
    for group in groups:
        start = float(group[0]["start"])
        end = max(float(word["end"]) for word in group)
        if end <= start:
            end = min(duration, start + 1e-3)
            if end <= start:
                start = max(0.0, end - 1e-3)
        segments.append(
            {
                "start": start,
                "end": end,
                "text": " ".join(str(word["word"]).strip() for word in group),
                "speaker": "WORD_TIMELINE_FALLBACK",
                "words": group,
            }
        )
    return segments


def _compose_exclusive_projection(
    segments: list[Conversation.SpeakerSegment],
    events: list[Conversation.SpeakerSegment],
) -> list[Conversation.SpeakerSegment]:
    """Overlay meaningful provider events without overlapping displayed speech.

    A zero-duration event is a point boundary (not an audio interval), so split the
    containing word-aligned speech turn around it. A positive-duration event only
    occupies parts not already owned by speech; speech text wins when providers report
    a concurrent sound tag. The raw provider revision and audio remain unchanged.
    """

    projected = [segment.model_copy(deep=True) for segment in segments]
    retained: list[Conversation.SpeakerSegment] = []
    tolerance = 1e-6

    for event in sorted(events, key=lambda item: (item.start, item.end)):
        event_start = float(event.start)
        event_end = float(event.end)
        if event_end < event_start - tolerance:
            raise ValueError(
                f"Non-speech event ends before it starts: {event_start}-{event_end}"
            )

        if event_end <= event_start + tolerance:
            containing = [
                segment
                for segment in projected
                if float(segment.start) + tolerance
                < event_start
                < float(segment.end) - tolerance
            ]
            if any(segment.segment_type != "speech" for segment in containing):
                continue

            split_projection: list[Conversation.SpeakerSegment] = []
            marker_is_representable = True
            for segment in projected:
                if segment not in containing:
                    split_projection.append(segment)
                    continue
                if not segment.words:
                    marker_is_representable = False
                    split_projection.append(segment)
                    continue

                left_words = [
                    word
                    for word in segment.words
                    if (float(word.start) + float(word.end)) / 2 < event_start
                ]
                right_words = [
                    word
                    for word in segment.words
                    if (float(word.start) + float(word.end)) / 2 >= event_start
                ]
                if left_words:
                    left = segment.model_copy(deep=True)
                    left.end = event_start
                    left.words = left_words
                    left.text = " ".join(word.word for word in left_words).strip()
                    split_projection.append(left)
                if right_words:
                    right = segment.model_copy(deep=True)
                    right.start = event_start
                    right.words = right_words
                    right.text = " ".join(word.word for word in right_words).strip()
                    split_projection.append(right)

            if marker_is_representable:
                projected = split_projection
                retained.append(event.model_copy(deep=True))
            continue

        uncovered = [(event_start, event_end)]
        occupied = [
            segment
            for segment in projected + retained
            if float(segment.end) > float(segment.start) + tolerance
        ]
        for segment in occupied:
            occupied_start = float(segment.start)
            occupied_end = float(segment.end)
            next_uncovered: list[tuple[float, float]] = []
            for start, end in uncovered:
                if (
                    occupied_end <= start + tolerance
                    or occupied_start >= end - tolerance
                ):
                    next_uncovered.append((start, end))
                    continue
                if occupied_start > start + tolerance:
                    next_uncovered.append((start, min(occupied_start, end)))
                if occupied_end < end - tolerance:
                    next_uncovered.append((max(occupied_end, start), end))
            uncovered = next_uncovered
            if not uncovered:
                break

        for start, end in uncovered:
            if end <= start + tolerance:
                continue
            fragment = event.model_copy(deep=True)
            fragment.start = start
            fragment.end = end
            retained.append(fragment)

    return sorted(projected + retained, key=lambda item: (item.start, item.end))


def _pool_returned_segment_embeddings(
    segments: list[dict], unknown_label_map: dict[str, str]
) -> dict[str, list[float]]:
    """Pool identification embeddings by the final label without re-decoding audio."""
    grouped: dict[str, list[np.ndarray]] = {}
    for segment in segments:
        values = segment.get("_evaluation_embedding")
        if not values:
            continue
        embedding = np.asarray(values, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(embedding))
        if not np.all(np.isfinite(embedding)) or norm == 0.0:
            continue
        label = segment.get("identified_as") or unknown_label_map.get(
            segment.get("speaker", "Unknown"), UNKNOWN_SPEAKER_PREFIX
        )
        grouped.setdefault(label, []).append(embedding / norm)

    pooled = {}
    for label, embeddings in grouped.items():
        centroid = np.mean(np.stack(embeddings), axis=0)
        norm = float(np.linalg.norm(centroid))
        if np.all(np.isfinite(centroid)) and norm > 0.0:
            pooled[label] = (centroid / norm).tolist()
    return pooled


def _rekey_cluster_centroids(
    raw_centroids: dict[str, list[float]],
    segments: list[dict],
    unknown_label_map: dict[str, str],
) -> dict[str, list[float]]:
    """Key raw neural-cluster centroids by their stable automatic identity.

    Background suppression can relabel a few turns in an otherwise human-owned
    neural cluster as ``Noise``/``Background Speech``. A last-write-wins mapping
    therefore loses the person's centroid whenever a background turn happens to be
    last. Prefer the cluster's sole person identity; use its deterministic unknown
    label when it has no person identity; and omit genuinely mixed-person clusters
    because one pooled vector cannot truthfully represent either person.
    """

    identities_by_cluster: dict[str, set[str]] = {}
    for segment in segments:
        cluster = segment.get("speaker")
        identified = segment.get("identified_as")
        if (
            cluster
            and identified
            and identified not in (NOISE_LABEL, BACKGROUND_SPEECH_LABEL)
        ):
            identities_by_cluster.setdefault(cluster, set()).add(identified)

    rekeyed: dict[str, list[float]] = {}
    blocked_labels: set[str] = set()
    for cluster, centroid in raw_centroids.items():
        identities = identities_by_cluster.get(cluster, set())
        if len(identities) > 1:
            logger.warning(
                "Not storing drift centroid for mixed-identity cluster %s: %s",
                cluster,
                sorted(identities),
            )
            continue
        label = next(iter(identities), None) or unknown_label_map.get(cluster)
        if not label or label in blocked_labels:
            continue
        if label in rekeyed:
            # Exclusivity should prevent this. If it does happen, neither pooled
            # centroid is a valid representative of the shared final label.
            logger.warning(
                "Not storing colliding drift centroids for final label %s", label
            )
            rekeyed.pop(label, None)
            blocked_labels.add(label)
            continue
        rekeyed[label] = centroid
    return rekeyed


def _apply_human_speaker_overlays(segments: list, annotations: list) -> list[dict]:
    """Keep accepted human speaker labels authoritative after reprocessing.

    Reprocessing is still allowed to compute a fresh model label. When it disagrees,
    preserve that attempted result in the returned audit records, then overlay the
    human label onto the output segment. Matching is deliberately conservative: an
    annotation must land on a segment start within 750 ms. Provider reprocessing keeps
    exact boundaries; the tolerance only absorbs small timestamp movement.
    """
    failures = []
    claimed: set[int] = set()
    for annotation in sorted(
        annotations,
        key=lambda item: float(item.segment_start_time or 0.0),
    ):
        if annotation.segment_start_time is None or not annotation.corrected_speaker:
            continue
        candidates = [
            (abs(float(segment.start) - float(annotation.segment_start_time)), index)
            for index, segment in enumerate(segments)
            if index not in claimed
            and getattr(segment, "segment_type", "speech") == "speech"
        ]
        if not candidates:
            continue
        distance, index = min(candidates)
        if distance > HUMAN_LABEL_START_TOLERANCE_SECONDS:
            continue
        segment = segments[index]
        claimed.add(index)
        model_speaker = segment.identified_as
        model_confidence = segment.confidence
        if model_speaker != annotation.corrected_speaker:
            failures.append(
                {
                    "annotation_id": str(annotation.id),
                    "segment_start": float(segment.start),
                    "human_speaker": annotation.corrected_speaker,
                    "model_speaker": model_speaker,
                    "model_confidence": model_confidence,
                }
            )
        segment.speaker = annotation.corrected_speaker
        segment.identified_as = None
        segment.confidence = 0.0
    return failures


def _merge_adjacent_projected_speech(
    segments: list[Conversation.SpeakerSegment], max_gap: float = 2.0
) -> list[Conversation.SpeakerSegment]:
    """Remove compute-window seams after empty diarization turns are filtered.

    The speaker service merges adjacent turns before text alignment. A textless
    overlapping turn can later be filtered out, making two pieces of the same final
    speaker adjacent again—most visibly at a 20-minute ownership boundary. Merge that
    pair using the same two-second collar, while an event or another speaker remains a
    real boundary.
    """

    if not segments:
        return []
    ordered = sorted(segments, key=lambda segment: (segment.start, segment.end))
    merged = [ordered[0].model_copy(deep=True)]
    for segment in ordered[1:]:
        current = merged[-1]
        gap = float(segment.start) - float(current.end)
        can_merge = (
            current.segment_type == "speech"
            and segment.segment_type == "speech"
            and current.speaker == segment.speaker
            and current.identified_as == segment.identified_as
            and gap <= max_gap
        )
        if not can_merge:
            merged.append(segment.model_copy(deep=True))
            continue

        current_duration = max(0.0, float(current.end) - float(current.start))
        next_duration = max(0.0, float(segment.end) - float(segment.start))
        confidences = [
            (float(value), duration)
            for value, duration in (
                (current.confidence, current_duration),
                (segment.confidence, next_duration),
            )
            if value is not None
        ]
        current.end = max(float(current.end), float(segment.end))
        current.text = " ".join(
            value for value in (current.text.strip(), segment.text.strip()) if value
        )
        current.words.extend(segment.words)
        if confidences:
            weight = sum(duration for _, duration in confidences)
            current.confidence = (
                sum(value * duration for value, duration in confidences) / weight
                if weight > 0
                else sum(value for value, _ in confidences) / len(confidences)
            )
    return merged


def _speaker_identification_mode(
    *,
    ran_pyannote_diarization: bool,
    used_word_timeline_fallback: bool,
    use_per_segment: bool,
) -> str:
    """Describe the identity decision that actually produced the projection."""

    if used_word_timeline_fallback:
        return "none"
    if ran_pyannote_diarization:
        return "cluster_centroid"
    return "per_segment" if use_per_segment else "majority_vote"


async def _compact_embedded_speaker_history(
    conversation: Conversation,
    *,
    keep_version_id: str,
) -> int:
    """Keep one speaker read model embedded after verifying history is archived.

    Full immutable history belongs to ``conversation_transcript_revisions``. Repeatedly
    embedding every word and turn eventually breaches MongoDB's 16 MB document limit
    on long recordings. Verify each displaced speaker projection has a standalone
    revision before removing only that denormalized copy.
    """

    versions_by_id = {
        version.version_id: version for version in conversation.transcript_versions
    }
    keep = versions_by_id.get(keep_version_id)
    if keep is None:
        raise SpeakerDataIntegrityError(
            f"Cannot compact speaker history: active version {keep_version_id} is missing"
        )

    # Older callers sometimes projected from the active speaker projection rather
    # than its provider/annotation source. Collapse that chain before removing the
    # intermediate embedded copies so the bounded read model never has a dangling
    # source pointer.
    source_id = (keep.metadata or {}).get("source_version_id")
    visited: set[str] = set()
    while source_id:
        if source_id in visited:
            raise SpeakerDataIntegrityError(
                f"Cannot compact cyclic speaker source chain at {source_id}"
            )
        visited.add(source_id)
        source = versions_by_id.get(source_id)
        if source is None:
            raise SpeakerDataIntegrityError(
                "Cannot compact speaker history: source version "
                f"{source_id} is missing"
            )
        if (source.metadata or {}).get("reprocessing_type") != "speaker_diarization":
            break
        source_id = (source.metadata or {}).get("source_version_id")
    if source_id:
        keep.metadata["source_version_id"] = source_id

    displaced = [
        version
        for version in conversation.transcript_versions
        if version.version_id != keep_version_id
        and (version.metadata or {}).get("reprocessing_type") == "speaker_diarization"
    ]
    for version in displaced:
        revision = await ConversationTranscriptRevision.find_one(
            {
                "$or": [
                    {
                        "retry_key": (
                            f"speaker-projection:{conversation.conversation_id}:"
                            f"{version.version_id}"
                        )
                    },
                    {
                        "conversation_id": conversation.conversation_id,
                        "metadata.source_version_id": version.version_id,
                    },
                ]
            }
        )
        if revision is None:
            raise SpeakerDataIntegrityError(
                "Cannot compact embedded speaker version "
                f"{version.version_id}: standalone revision is missing"
            )

    displaced_ids = {version.version_id for version in displaced}
    conversation.transcript_versions = [
        version
        for version in conversation.transcript_versions
        if version.version_id not in displaced_ids
    ]
    return len(displaced)


async def _human_speaker_annotations(conversation_id: str) -> list[Annotation]:
    annotations = (
        await Annotation.find(
            Annotation.conversation_id == conversation_id,
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.source == AnnotationSource.USER,
            Annotation.status == AnnotationStatus.ACCEPTED,
            Annotation.processed == True,
        )
        .sort("updated_at")
        .to_list()
    )
    # Later corrections at the same source timestamp supersede earlier ones.
    latest = {}
    for annotation in annotations:
        if annotation.segment_start_time is not None:
            latest[round(float(annotation.segment_start_time), 3)] = annotation
    return list(latest.values())


def _propagate_cluster_identities(
    segments: list[dict], excluded_starts: set[float]
) -> int:
    """Let each diarization cluster inherit its members' confident IDs.

    Per-segment identification names only the clear utterances; short or partly
    overlapped ones stay unknown even though pyannote already grouped them with
    a confidently-identified voice. When every confident vote inside a cluster
    agrees on one person (and there are at least PROPAGATION_MIN_VOTES votes),
    the remaining unidentified members inherit that name.

    ``excluded_starts``: segments the background scorer put in a non-foreground
    zone — media that diarization folded into a human's cluster must not inherit
    the human's name.
    """
    votes: dict[str, list[float]] = {}
    names_by_cluster: dict[str, set[str]] = {}
    for seg in segments:
        identified = seg.get("identified_as")
        if not identified or identified in (NOISE_LABEL, BACKGROUND_SPEECH_LABEL):
            continue
        cluster = seg.get("speaker") or ""
        if not cluster:
            continue
        names_by_cluster.setdefault(cluster, set()).add(identified)
        votes.setdefault(cluster, []).append(float(seg.get("confidence") or 0.0))
    propagated = 0
    for seg in segments:
        if seg.get("identified_as"):
            continue
        cluster = seg.get("speaker") or ""
        names = names_by_cluster.get(cluster)
        if not names or len(names) != 1:
            continue
        if len(votes[cluster]) < PROPAGATION_MIN_VOTES:
            continue
        start_key = background_suppression.segment_key(seg.get("start") or 0.0)
        if start_key in excluded_starts:
            continue
        seg["identified_as"] = next(iter(names))
        seg["confidence"] = round(sum(votes[cluster]) / len(votes[cluster]), 4)
        propagated += 1
    return propagated


async def _apply_background_references(
    conversation_id: str,
    segments: list[dict],
    user,
    speaker_client: SpeakerRecognitionClient,
    *,
    privacy_visibility: privacy.ConversationPrivacyFilter | None = None,
) -> set[float]:
    """Mark segments that match a background exemplar, and disclose it.

    Confident zone relabels the segment (existing behaviour); the unsure band is
    only recorded. Every non-foreground verdict lands in the suppression ledger
    so the conversation page can show what was marked and let the user restore
    or confirm it. Segments the user already ruled on are never re-marked, and a
    conversation-level "media is the subject" override skips marking entirely.

    Returns the start keys of segments in a non-foreground zone (minus ones the
    user restored as important speech) — the set cluster propagation must not
    write names onto.
    """
    visibility = privacy_visibility or privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": conversation_id}]):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    user_id = str(user.user_id)
    if await background_suppression.get_subject_override(user_id, conversation_id):
        logger.info(
            "Background marking skipped for %s: user override says media is the subject",
            conversation_id[:8],
        )
        return set()
    sticky = await background_suppression.load_sticky_segments(user_id, conversation_id)
    # Confirmed segments are background by the user's own verdict — relabel them
    # up front, before scoring, so a fresh score can never un-mark them (and so
    # cluster propagation never writes a name onto them).
    for segment in segments:
        ruling = sticky.get(background_suppression.segment_key(segment["start"]))
        if ruling and ruling["status"] == "confirmed":
            segment["identified_as"] = (
                NOISE_LABEL
                if ruling.get("bucket_type") == "noise"
                else BACKGROUND_SPEECH_LABEL
            )
            segment["status"] = "background_reference"
    semaphore = asyncio.Semaphore(SPEAKER_IDENTIFY_CONCURRENCY)
    ledger_records: list[dict] = []

    async def classify(segment: dict, wav: bytes | None = None) -> None:
        async with semaphore:
            if segment.get("_evaluation_embedding"):
                embedded = {
                    "embedding": segment["_evaluation_embedding"],
                    "embedding_model": segment.get("_embedding_model"),
                }
            else:
                if wav is None:
                    raise ValueError("background-reference segment has no audio")
                await visibility.assert_current()
                embedded = await speaker_client.extract_speaker_embedding(wav)
                await visibility.assert_current()
            if embedded.get("error") or "embedding" not in embedded:
                return
            scores = {}
            for bucket_type in background_bucket_controller.BUCKET_TYPES:
                matched = await background_bucket_controller.match_embeddings(
                    user,
                    [embedded["embedding"]],
                    bucket_type,
                    embedded.get("embedding_model"),
                    visibility=visibility,
                )
                results = matched.get("results") or []
                scores[bucket_type] = (
                    float(results[0]["bucket_similarity"]) if results else 0.0
                )
            best_type, best_score = max(scores.items(), key=lambda pair: pair[1])
            foreground_score = float(segment.get("confidence") or 0.0)
            zone = background_suppression.zone_for(best_score, foreground_score)
            if zone == "foreground":
                return
            start_key = background_suppression.segment_key(segment["start"])
            record = {
                "segment_start": segment["start"],
                "segment_end": segment["end"],
                "text": segment.get("text"),
                "background_similarity": best_score,
                "foreground_similarity": foreground_score,
                "bucket_type": best_type,
                "zone": zone,
                "embedding": embedded["embedding"],
                "previous_identified_as": segment.get("identified_as"),
                "previous_confidence": segment.get("confidence"),
            }
            ledger_records.append(record)
            if zone == "confident_background" and start_key not in sticky:
                segment["identified_as"] = (
                    NOISE_LABEL if best_type == "noise" else BACKGROUND_SPEECH_LABEL
                )
                segment["confidence"] = best_score
                segment["status"] = "background_reference"
                segment["background_scores"] = scores

    eligible = [
        segment
        for segment in segments
        if 1.0 <= float(segment.get("end", 0)) - float(segment.get("start", 0)) <= 15.0
    ]
    cached = [segment for segment in eligible if segment.get("_evaluation_embedding")]
    needs_audio = [
        segment for segment in eligible if not segment.get("_evaluation_embedding")
    ]
    results = list(
        await asyncio.gather(
            *(classify(segment) for segment in cached), return_exceptions=True
        )
    )
    if needs_audio:
        try:
            resolved_audio = await resolve_conversation_audio(conversation_id)
        except privacy.PrivacyHeld:
            raise
        except Exception as error:  # keep the suppression ledger explicitly incomplete
            logger.warning(
                "Background-reference audio claim failed for %s: %s",
                conversation_id[:8],
                error,
            )
            results.extend([error] * len(needs_audio))
        else:
            for offset in range(0, len(needs_audio), BACKGROUND_AUDIO_BATCH_SIZE):
                batch = needs_audio[offset : offset + BACKGROUND_AUDIO_BATCH_SIZE]
                ranges = [
                    (float(segment["start"]), float(segment["end"]))
                    for segment in batch
                ]
                try:
                    wavs = await reconstruct_resolved_audio_ranges(
                        resolved_audio,
                        ranges,
                        conversation_id=conversation_id,
                    )
                except privacy.PrivacyHeld:
                    raise
                except Exception as error:  # one bad batch must not hide later verdicts
                    logger.warning(
                        "Background-reference audio batch failed for %s at %d: %s",
                        conversation_id[:8],
                        offset,
                        error,
                    )
                    results.extend([error] * len(batch))
                    continue
                results.extend(
                    await asyncio.gather(
                        *(classify(segment, wav) for segment, wav in zip(batch, wavs)),
                        return_exceptions=True,
                    )
                )
    for result in results:
        if isinstance(result, privacy.PrivacyHeld):
            raise result
    await visibility.assert_current()
    failures = sum(isinstance(result, Exception) for result in results)
    if failures:
        logger.warning(
            "Background-reference comparison failed for %d segments", failures
        )
    try:
        await background_suppression.record_conversation_suppressions(
            conversation_id,
            user_id,
            ledger_records,
            source="speaker_job",
            # A scan with failed segments is incomplete — don't treat missing
            # records as "no longer in zone".
            prune=failures == 0,
            privacy_visibility=visibility,
        )
    except privacy.PrivacyHeld:
        raise
    except Exception:
        logger.exception(
            "Failed to write background suppression ledger for %s",
            conversation_id[:8],
        )
    await visibility.assert_current()
    excluded = set()
    for record in ledger_records:
        start_key = background_suppression.segment_key(record["segment_start"])
        ruling = sticky.get(start_key)
        if not ruling or ruling["status"] != "restored":
            excluded.add(start_key)
    return excluded


class SpeakerServiceError(Exception):
    """The speaker service was reached but failed to process the request.

    Raised (rather than returned as ``{success: False}``) so the post-conversation
    chain's failure machinery engages honestly: RQ marks the job FAILED instead of
    "OK", ``Retry`` gets a shot at a transient blip, ``on_chain_job_failure`` records a
    conversation-linked system event, and ``allow_failure`` dependencies still let the
    rest of the pipeline (memory, title, events) run. Returning success here is what
    made a hard HTTP 500 look "OK" all down the chain.
    """


class SpeakerReprocessFailed(SpeakerServiceError):
    """A manual speaker-reprocess produced no usable result.

    Subclasses :class:`SpeakerServiceError` so it propagates through the same failure
    machinery (RQ-failed + conversation-linked system event). Raised in *create* mode —
    where the new transcript version is only created once we have a usable result, the
    way ``transcribe_full_audio_job`` does it. So a failed reprocess creates NO version,
    leaves the conversation exactly as it was, and surfaces an error — instead of silently
    leaving a new version with the old (unimproved) labels and reporting success.
    """


class SpeakerDataIntegrityError(Exception):
    """Speaker recognition was blocked by invalid local transcript/audio data."""


@async_job(redis=True, beanie=True)
async def check_enrolled_speakers_job(
    session_id: str, user_id: str, client_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """
    Check if any enrolled speakers are present in the current audio stream.

    This job is used during speech detection to filter conversations by enrolled speakers.

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with enrolled_present, identified_speakers, and speaker_result
    """

    logger.info(f"🎤 Starting enrolled speaker check for session {session_id[:12]}")

    start_time = time.time()

    # Get aggregated transcription results
    aggregator = TranscriptionResultsAggregator(redis_client)
    raw_results = await aggregator.get_session_results(session_id)

    # Check for enrolled speakers
    speaker_client = SpeakerRecognitionClient()
    enrolled_present, speaker_result = (
        await speaker_client.check_if_enrolled_speaker_present(
            redis_client=redis_client,
            client_id=client_id,
            session_id=session_id,
            user_id=user_id,
            transcription_results=raw_results,
        )
    )

    # Check for errors from speaker service
    if speaker_result and speaker_result.get("error"):
        error_type = speaker_result.get("error")
        error_message = speaker_result.get("message", "Unknown error")
        logger.error(
            f"🎤 [SPEAKER CHECK] Speaker service error: {error_type} - {error_message}"
        )

        # For connection failures, assume no enrolled speakers but allow conversation to proceed
        # Speaker filtering is optional - if service is down, conversation should still be created
        if error_type in ("connection_failed", "timeout", "client_error"):
            logger.warning(
                f"⚠️ Speaker service unavailable ({error_type}), assuming no enrolled speakers. "
                f"Conversation will proceed normally."
            )
            return {
                "success": True,
                "session_id": session_id,
                "speaker_service_unavailable": True,
                "enrolled_present": False,
                "identified_speakers": [],
                "skip_reason": f"Speaker service unavailable: {error_type}",
                "processing_time_seconds": time.time() - start_time,
            }

        # For other processing errors, also assume no enrolled speakers
        return {
            "success": False,
            "session_id": session_id,
            "error": f"Speaker recognition failed: {error_type}",
            "error_details": error_message,
            "enrolled_present": False,
            "identified_speakers": [],
            "processing_time_seconds": time.time() - start_time,
        }

    # Extract identified speakers
    identified_speakers = []
    if speaker_result and "segments" in speaker_result:
        for seg in speaker_result["segments"]:
            identified_as = seg.get("identified_as")
            if (
                identified_as
                and identified_as != "Unknown"
                and identified_as not in identified_speakers
            ):
                identified_speakers.append(identified_as)

    processing_time = time.time() - start_time

    if enrolled_present:
        logger.info(
            f"✅ Enrolled speaker(s) found: {', '.join(identified_speakers)} ({processing_time:.2f}s)"
        )
    else:
        logger.info(f"⏭️ No enrolled speakers found ({processing_time:.2f}s)")

    # Update job metadata for timeline tracking
    update_job_meta(
        session_id=session_id,
        client_id=client_id,
        enrolled_present=enrolled_present,
        identified_speakers=identified_speakers,
        speaker_count=len(identified_speakers),
        processing_time=processing_time,
    )

    return {
        "success": True,
        "session_id": session_id,
        "enrolled_present": enrolled_present,
        "identified_speakers": identified_speakers,
        "speaker_result": speaker_result,
        "processing_time_seconds": processing_time,
    }


@async_job(redis=True, beanie=True)
@protect_result_logs
async def recognise_speakers_job(
    conversation_id: str,
    version_id: str,
    transcript_text: str = "",
    words: list | None = None,
    source_version_id: str | None = None,
    diarization_source_override: str | None = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    RQ job function for identifying speakers in a transcribed conversation.

    This job adapts based on provider capabilities:
    1. If provider has diarization (e.g., VibeVoice) → skip pyannote, do identification only
    2. If provider has word timestamps (e.g., Parakeet) → full pyannote diarization + identification
    3. If no word timestamps → cannot run diarization, keep existing segments

    If pyannote re-diarization finds no speaker turns despite timed ASR words, it builds
    provider-independent spans from the word clock and identifies those — so a neural
    miss does not restore fragmented provider utterance boundaries.

    Speaker identification always runs if enrolled speakers exist, mapping
    generic labels ("Speaker 0") to enrolled speaker names ("Alice").

    Two write modes:
    - **In-place** (initial post-conversation chain): ``version_id`` refers to an
      existing version this job refines in place.
    - **Create-on-success** (manual reprocess, when ``source_version_id`` is given and
      ``version_id`` does not exist yet): read from the source version and create
      ``version_id`` ONLY once a usable result exists — the way
      ``transcribe_full_audio_job`` does it. A failed/empty reprocess then creates no
      version and raises, instead of leaving a degraded no-op version active.

    Args:
        conversation_id: Conversation ID
        version_id: Transcript version ID to write (existing in-place, or to create)
        transcript_text: Transcript text from transcription job (optional, reads from DB if empty)
        words: Word-level timing data from transcription job (optional, reads from DB if empty)
        source_version_id: When set (and version_id doesn't exist), read from this
            version and create version_id on success (manual-reprocess create mode)
        diarization_source_override: Per-job engine choice (provider or pyannote).
            When omitted, use the configured default.
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """

    logger.info(
        f"🎤 RQ: Starting speaker recognition for conversation {conversation_id}"
    )

    start_time = time.time()

    # Get the conversation
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    # Get user_id from conversation
    user_id = conversation.user_id

    target_visibility = privacy.ConversationPrivacyFilter()
    target_snapshot = await privacy.load_snapshot(str(user_id))
    if not target_snapshot.permits_record(conversation):
        raise privacy.PrivacyHeld()
    target_visibility.snapshots[str(user_id)] = target_snapshot

    # Resolve the SOURCE version (what we read from) and the write mode.
    #
    # In-place mode (initial post-conversation chain): version_id is an existing version
    # this job refines in place; source == target.
    #
    # Create mode (manual reprocess): the controller pre-allocated version_id but did NOT
    # create it (mirrors transcribe_full_audio_job). Read from source_version_id and only
    # create version_id once we have a usable result — so a failed/empty reprocess creates
    # no version and surfaces an error instead of leaving a degraded no-op version active.
    target_version = conversation.get_transcript_version(version_id)
    create_mode = target_version is None and source_version_id is not None
    if create_mode:
        source_version = conversation.get_transcript_version(source_version_id)
        if not source_version:
            logger.error(
                f"Source transcript version {source_version_id} not found for reprocess"
            )
            raise SpeakerReprocessFailed(
                f"source transcript version {source_version_id} not found"
            )
        logger.info(
            f"🎤 Create mode: reading source {source_version_id}, will create "
            f"version {version_id} on success"
        )
    else:
        source_version = target_version
        if not source_version:
            # Upstream declined to create the handed-in version (e.g. an empty batch
            # re-transcription kept the existing transcript) — refine whatever is active.
            source_version = conversation.active_transcript
            if source_version:
                logger.warning(
                    f"Transcript version {version_id} not found; falling back to active "
                    f"version {source_version.version_id} (upstream likely kept the "
                    f"existing transcript)"
                )
                version_id = source_version.version_id
            else:
                logger.error(
                    f"Transcript version {version_id} not found and no active version exists"
                )
                return {"success": False, "error": "Transcript version not found"}

    # All reads below use the source version.
    transcript_version = source_version

    # Reject stale split/trim clocks before constructing audio or touching the remote
    # service. Missing chunk coverage is allowed here: the speaker service receives the
    # coverage ranges and pads real gaps with silence so the original transcript clock is
    # preserved.
    preflight_segments = [
        segment.model_dump(mode="python")
        for segment in (transcript_version.segments or [])
    ]
    preflight_words = [
        word.model_dump(mode="python") for word in (transcript_version.words or [])
    ]
    try:
        audio_ranges = await load_transcript_audio_ranges(conversation_id)
        normalized_segments, normalized_words = (
            validate_and_normalize_transcript_timing(
                preflight_segments,
                preflight_words,
                audio_duration=conversation.audio_total_duration or 0.0,
                audio_ranges=audio_ranges,
            )
        )
    except TranscriptTimingError as error:
        reason = f"{error.code}: {error}"
        conversation.transcript_integrity_error = reason
        async with target_visibility.publication():
            await conversation.save()
        record_event_sync(
            severity="error",
            category="data_integrity",
            source="speaker_recognition",
            title="Speaker recognition blocked by transcript timing",
            detail=reason,
            user_id=str(user_id) if user_id else None,
            client_id=conversation.client_id,
            conversation_id=conversation_id,
            metadata={**error.details, "version_id": transcript_version.version_id},
            incident_key=f"transcript-integrity:{conversation_id}",
        )
        raise SpeakerDataIntegrityError(reason) from error

    if normalized_segments != preflight_segments or normalized_words != preflight_words:
        async with target_visibility.publication():
            transcript_version = await persist_timing_normalized_revision(
                conversation,
                transcript_version,
                segments=normalized_segments,
                words=normalized_words,
                audio_duration=conversation.audio_total_duration or 0.0,
            )
            source_version = transcript_version
            await conversation.save()
        logger.info(
            "Normalized harmless transcript edge timing for %s into derived "
            "version %s",
            conversation_id,
            transcript_version.version_id,
        )

    # Check if speaker recognition is enabled
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        logger.info(f"🎤 Speaker recognition disabled, skipping")
        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": False,
            "processing_time_seconds": 0,
        }

    if transcript_version.segments and not any(
        _is_speech_segment(segment) for segment in transcript_version.segments
    ):
        logger.info(
            "🎤 Transcript contains only non-speech events; no speaker work needed"
        )
        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": True,
            "identified_speakers": [],
            "skip_reason": "No speech segments to identify",
            "processing_time_seconds": time.time() - start_time,
        }

    # Get provider capabilities from metadata
    provider_capabilities = transcript_version.metadata.get("provider_capabilities", {})
    provider_has_diarization = provider_capabilities.get("diarization", False)
    provider_has_word_timestamps = provider_capabilities.get("word_timestamps", False)

    # Check if provider already did diarization (set by transcription job)
    diarization_source = transcript_version.diarization_source
    provider_diarized = provider_has_diarization or diarization_source == "provider"

    # Diarization policy from settings:
    # - "provider": trust provider diarization when available, fall back to pyannote
    # - "pyannote": always re-diarize with pyannote (when word timestamps allow it)
    diarization_settings = get_diarization_settings()
    preferred_source = diarization_source_override or diarization_settings.get(
        "diarization_source", "pyannote"
    )
    use_provider_diarization = provider_diarized and preferred_source == "provider"

    if use_provider_diarization:
        # Provider already did diarization (e.g., VibeVoice, Deepgram batch)
        # Skip pyannote diarization, go straight to speaker identification
        logger.info(
            f"🎤 Provider already diarized (diarization_source={diarization_source}), "
            f"skipping pyannote diarization - will run speaker identification only"
        )

        # If we have existing segments from provider, proceed to identification
        if transcript_version.segments:
            logger.info(
                f"🎤 Using {len(transcript_version.segments)} segments from provider"
            )
            # Continue to speaker identification below (after this block)
        else:
            logger.warning(f"🎤 Provider claimed diarization but no segments found")
            # Still continue - identification may work with audio analysis

    # Read transcript text and words from the transcript version
    # (Parameters may be empty if called via job dependency)
    actual_transcript_text = transcript_text or transcript_version.transcript or ""
    actual_words = words if words else []
    word_timing_method = (
        "provided_job_words" if actual_words and not transcript_version.words else None
    )

    # If words not provided as parameter, read from version.words field (standardized location)
    if not actual_words and transcript_version.words:
        # Convert Word objects to dicts for speaker service API
        actual_words = [
            {"word": w.word, "start": w.start, "end": w.end, "confidence": w.confidence}
            for w in transcript_version.words
        ]
        logger.info(
            f"🔤 Loaded {len(actual_words)} words from transcript version.words field"
        )
    # Backward compatibility: Fall back to metadata if words field is empty (old data)
    elif not actual_words and transcript_version.metadata.get("words"):
        actual_words = transcript_version.metadata.get("words", [])
        word_timing_method = "legacy_metadata_words"
        logger.info(
            f"🔤 Loaded {len(actual_words)} words from transcript version metadata (legacy)"
        )
    # Backward compatibility: Extract from segments if that's all we have (old streaming data)
    elif not actual_words and transcript_version.segments:
        for segment in transcript_version.segments:
            if segment.words:
                for w in segment.words:
                    actual_words.append(
                        {
                            "word": w.word,
                            "start": w.start,
                            "end": w.end,
                            "confidence": w.confidence,
                        }
                    )
        if actual_words:
            word_timing_method = "embedded_segment_words"
            logger.info(
                f"🔤 Extracted {len(actual_words)} words from segments (legacy)"
            )

    if not actual_transcript_text and actual_words:
        actual_transcript_text = " ".join(
            str(word.get("word", "")).strip() for word in actual_words
        ).strip()
        if actual_transcript_text:
            logger.info("🔤 Recovered transcript text from stored timed words")

    if not actual_transcript_text:
        logger.warning(f"🎤 No transcript text found in version {version_id}")
        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": "No transcript text available",
            "processing_time_seconds": 0,
        }

    # If the provider gave segment text but no word timestamps (e.g. VibeVoice),
    # synthesize word timings via forced alignment so we can run the full pyannote
    # re-diarization path (Path A) instead of per-segment identification on the
    # provider's segments. This unifies word-less conversations onto the better path.
    if (
        not actual_words
        and not use_provider_diarization
        and transcript_version.segments
    ):
        speech_for_align = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in transcript_version.segments
            if _is_speech_segment(s) and (s.text or "").strip()
        ]
        total_dur = max((s.end for s in transcript_version.segments), default=0.0)
        if speech_for_align and total_dur > 0:
            logger.info(
                f"🔤 No word timestamps; forced-aligning {len(speech_for_align)} "
                f"segments to recover word timing for re-diarization"
            )
            actual_words = await synthesize_words_via_alignment(
                conversation_id, speech_for_align, total_dur
            )
            if actual_words:
                word_timing_method = "forced_alignment"
            else:
                actual_words = estimate_words_from_segment_timing(speech_for_align)
                if actual_words:
                    word_timing_method = "segment_clock_estimate"
                    logger.warning(
                        "🔤 Forced alignment returned no words; using segment-clock "
                        "word estimates for Pyannote text matching"
                    )

    if word_timing_method and actual_words:
        async with target_visibility.publication():
            transcript_version = await persist_word_timed_revision(
                conversation,
                transcript_version,
                words=actual_words,
                method=word_timing_method,
                audio_duration=float(conversation.audio_total_duration or 0.0),
            )
            source_version = transcript_version
            actual_words = [
                word.model_dump(mode="python") for word in transcript_version.words
            ]
            await conversation.save()
        logger.info(
            "🔤 Persisted %d %s word clocks as derived source version %s",
            len(actual_words),
            word_timing_method,
            transcript_version.version_id,
        )

    # Check if we can run pyannote diarization
    # Pyannote requires word timestamps to align speaker segments with text
    continuous_audio = _audio_ranges_cover_continuously(
        audio_ranges,
        float(conversation.audio_total_duration or 0.0),
    )
    can_run_pyannote = (
        bool(actual_words) and not use_provider_diarization and bool(audio_ranges)
    )
    if actual_words and not use_provider_diarization and not continuous_audio:
        logger.info(
            "🎤 Audio has chunk gaps; Pyannote will preserve the original clock by "
            "padding missing ranges with silence"
        )

    if not actual_words and not provider_diarized:
        if not transcript_version.segments:
            # No words, no provider diarization, no existing segments - nothing we can do
            logger.warning(
                f"🎤 No word timestamps available, provider didn't diarize, "
                f"and no existing segments to identify."
            )
            return {
                "success": False,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "error": "No word timestamps and no segments available",
                "processing_time_seconds": time.time() - start_time,
            }
        # Has existing segments - fall through to run identification on them
        logger.info(
            f"🎤 No word timestamps for pyannote re-diarization, but "
            f"{len(transcript_version.segments)} existing segments found. "
            f"Running speaker identification on existing segments."
        )

    # Per-segment identification mode comes solely from settings
    misc_config = get_misc_settings()
    use_per_segment = misc_config.get("per_segment_speaker_id", False)
    if use_per_segment:
        logger.info("🎤 Per-segment identification mode active (config toggle enabled)")

    try:
        ran_pyannote_diarization = False
        used_word_timeline_fallback = False
        if transcript_version.segments and not can_run_pyannote:
            # Have existing segments and can't/shouldn't run pyannote - do identification only
            # Covers: provider already diarized, no word timestamps but segments exist, etc.
            # Only send speech segments for identification; skip event/note segments
            speech_segments = [
                s for s in transcript_version.segments if _is_speech_segment(s)
            ]
            logger.info(
                f"🎤 Using segment-level speaker identification on {len(speech_segments)} speech segments "
                f"(skipped {len(transcript_version.segments) - len(speech_segments)} non-speech)"
            )
            segments_data = [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                for s in speech_segments
            ]
            await target_visibility.assert_current()
            speaker_result = await speaker_client.identify_provider_segments(
                conversation_id=conversation_id,
                segments=segments_data,
                user_id=user_id,
                per_segment=use_per_segment,
                min_segment_duration=0.5 if use_per_segment else 1.5,
            )
        else:
            # Standard path: full diarization + identification via speaker service
            ran_pyannote_diarization = True
            transcript_data = {"text": actual_transcript_text, "words": actual_words}

            # Generate backend token for speaker service to fetch audio
            try:
                user = await get_user_by_id(user_id)
                if not user:
                    logger.error(f"User {user_id} not found for token generation")
                    return {
                        "success": False,
                        "conversation_id": conversation_id,
                        "version_id": version_id,
                        "error": "User not found",
                        "processing_time_seconds": time.time() - start_time,
                    }

                backend_token = generate_jwt_for_user(user_id, user.email)
                logger.info(f"🔐 Generated backend token for speaker service")

            except Exception as token_error:
                logger.error(
                    f"Failed to generate backend token: {token_error}", exc_info=True
                )
                return {
                    "success": False,
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "error": f"Token generation failed: {token_error}",
                    "processing_time_seconds": time.time() - start_time,
                }

            logger.info(
                f"🎤 Calling speaker recognition service with conversation_id..."
            )
            await target_visibility.assert_current()
            speaker_result = await speaker_client.diarize_identify_match(
                conversation_id=conversation_id,
                backend_token=backend_token,
                transcript_data=transcript_data,
                user_id=user_id,
                audio_ranges=audio_ranges if not continuous_audio else None,
            )

        # Check for errors from speaker service
        if speaker_result.get("error"):
            error_type = speaker_result.get("error")
            error_message = speaker_result.get("message", "Unknown error")
            logger.error(
                f"🎤 Speaker recognition service error: {error_type} - {error_message}"
            )

            if error_type == "transcript_data_error":
                conversation.transcript_integrity_error = error_message
                async with target_visibility.publication():
                    await conversation.save()
                record_event_sync(
                    severity="error",
                    category="data_integrity",
                    source="speaker_recognition",
                    title="Speaker recognition blocked by transcript timing",
                    detail=error_message,
                    user_id=str(user_id) if user_id else None,
                    client_id=conversation.client_id,
                    conversation_id=conversation_id,
                    metadata={"version_id": transcript_version.version_id},
                    incident_key=f"transcript-integrity:{conversation_id}",
                )
                raise SpeakerDataIntegrityError(error_message)

            # Connection/timeout errors → skip gracefully (existing behavior).
            # Exception: in create mode (manual reprocess) we have nothing to write, so
            # surface the failure instead of creating an empty/no-op version.
            if error_type in ("connection_failed", "timeout", "client_error"):
                if create_mode:
                    raise SpeakerReprocessFailed(
                        f"speaker service unavailable ({error_type}): {error_message}"
                    )
                logger.warning(
                    f"⚠️ Speaker service unavailable ({error_type}), skipping speaker recognition. "
                    f"Downstream jobs (memory, title/summary, events) will proceed normally."
                )
                return {
                    "success": True,  # Allow pipeline to continue
                    "conversation_id": conversation_id,
                    "version_id": version_id,
                    "speaker_recognition_enabled": True,
                    "speaker_service_unavailable": True,
                    "identified_speakers": [],
                    "skip_reason": f"Speaker service unavailable: {error_type}",
                    "error_type": error_type,
                    "processing_time_seconds": time.time() - start_time,
                }

            # The service was reached but failed to process (HTTP 500 server_error,
            # validation_error, resource_error, or anything unknown). RAISE so the
            # failure is honest: RQ marks the job FAILED (not "OK"), Retry(max=2) gets a
            # shot at a transient blip, on_chain_job_failure records a conversation-linked
            # system event, and allow_failure deps still let memory/title/events run.
            else:
                logger.error(
                    f"❌ Speaker service {error_type}: {error_message} "
                    f"(conversation {conversation_id})"
                )
                raise SpeakerServiceError(f"{error_type}: {error_message}")

        # Pyannote re-diarization can occasionally find no speaker turns even on clearly
        # audible short speech. Do not reuse provider utterance boundaries here: that
        # would preserve exactly the Smallest.ai fragmentation this pass is replacing.
        # Build neutral spans from the immutable word clock, then run ordinary voice
        # identification over those spans. Provenance remains explicit below.
        if ran_pyannote_diarization and not (speaker_result or {}).get("segments"):
            fallback_segments = _word_timeline_fallback_segments(
                actual_words,
                duration=float(conversation.audio_total_duration or 0.0),
                max_gap=float(diarization_settings.get("collar", 2.0)),
            )
            if fallback_segments:
                logger.warning(
                    "🎤 Re-diarization returned 0 segments; using %d "
                    "provider-independent word-timeline span(s)",
                    len(fallback_segments),
                )
                await target_visibility.assert_current()
                speaker_result = await speaker_client.identify_provider_segments(
                    conversation_id=conversation_id,
                    segments=fallback_segments,
                    user_id=user_id,
                    per_segment=use_per_segment,
                    min_segment_duration=0.5 if use_per_segment else 1.5,
                )
                result_segments = speaker_result.get("segments") or []
                if len(result_segments) != len(fallback_segments):
                    raise SpeakerDataIntegrityError(
                        "Word-timeline identification changed segment cardinality: "
                        f"{len(fallback_segments)} in, {len(result_segments)} out"
                    )
                for result_segment, fallback_segment in zip(
                    result_segments, fallback_segments, strict=True
                ):
                    result_segment["words"] = fallback_segment["words"]
                used_word_timeline_fallback = True

        # Service worked but found no segments (legitimate empty result, and the fallback
        # above — if any — also found nothing).
        if (
            not speaker_result
            or "segments" not in speaker_result
            or not speaker_result["segments"]
        ):
            logger.warning(f"🎤 Speaker recognition returned no segments")
            if create_mode:
                # Nothing usable to put in a new version — don't create one; surface it.
                raise SpeakerReprocessFailed(
                    "speaker reprocess produced no segments (diarization and "
                    "segment-identification fallback both empty)"
                )
            return {
                "success": True,
                "conversation_id": conversation_id,
                "version_id": version_id,
                "speaker_recognition_enabled": True,
                "identified_speakers": [],
                "processing_time_seconds": time.time() - start_time,
            }

        gallery_scope = result_scope(speaker_result)
        await gallery_scope.assert_current()
        await target_visibility.assert_current()

        claim_duration = (
            range_duration(conversation.audio_ranges)
            if conversation.audio_ranges
            else float(conversation.audio_total_duration or 0.0)
        )
        speaker_segments = _normalize_speaker_segment_bounds(
            speaker_result["segments"],
            duration=claim_duration,
        )
        if ran_pyannote_diarization and actual_words:
            speaker_segments = _project_source_words_onto_speaker_turns(
                speaker_segments,
                actual_words,
            )
        speaker_result["segments"] = speaker_segments
        logger.info(f"🎤 Speaker recognition returned {len(speaker_segments)} segments")

        background_user = await get_user_by_id(user_id)
        background_starts: set[float] = set()
        if background_user:
            background_starts = await _apply_background_references(
                conversation_id,
                speaker_segments,
                background_user,
                speaker_client,
                privacy_visibility=target_visibility,
            )

        # Per-segment mode names only the clear utterances; propagate agreeing
        # confident IDs across each diarization cluster so short clips inherit,
        # while background-flagged segments stay out of the inheritance.
        propagated_segments = 0
        if use_per_segment:
            propagated_segments = _propagate_cluster_identities(
                speaker_segments, background_starts
            )
            if propagated_segments:
                logger.info(
                    "🎤 Cluster propagation named %d unidentified segments",
                    propagated_segments,
                )

        # Build mapping for unknown speakers: diarization_label -> "Unknown Speaker N"
        unknown_label_map = {}
        unknown_counter = 1
        for seg in speaker_segments:
            identified_as = seg.get("identified_as")
            if not identified_as:
                label = seg.get("speaker", "Unknown")
                if label not in unknown_label_map:
                    unknown_label_map[label] = (
                        f"{UNKNOWN_SPEAKER_PREFIX} {unknown_counter}"
                    )
                    unknown_counter += 1

        if unknown_label_map:
            logger.info(f"🎤 Unknown speaker mapping: {unknown_label_map}")

        # Update the transcript version segments with identified speakers
        # Filter out empty segments (diarization sometimes creates segments with no text)
        updated_segments = []
        empty_segment_count = 0
        for seg in speaker_segments:
            words_data = seg.get("words", []) or []
            text = str(seg.get("text", "") or "").strip()
            if not text and words_data:
                text = " ".join(
                    str(word.get("word", "")).strip()
                    for word in words_data
                    if str(word.get("word", "")).strip()
                )

            # A one-character word is still transcript evidence. Only a truly empty
            # turn may be discarded; dropping short text loses words such as "I".
            if not text:
                empty_segment_count += 1
                logger.debug("Filtered speaker turn with no text or words")
                continue

            # Skip segments with invalid structure
            if not isinstance(seg.get("start"), (int, float)) or not isinstance(
                seg.get("end"), (int, float)
            ):
                empty_segment_count += 1
                logger.debug(f"Filtered segment with invalid timing: {seg}")
                continue

            speaker_name = seg.get("identified_as") or unknown_label_map.get(
                seg.get("speaker", "Unknown"), UNKNOWN_SPEAKER_PREFIX
            )

            # Words were already matched to this neural turn by the speaker service.
            segment_words = [
                Conversation.Word(
                    word=w.get("word", ""),
                    start=w.get("start", 0.0),
                    end=w.get("end", 0.0),
                    confidence=w.get("confidence"),
                )
                for w in words_data
            ]

            # Noise is non-speech; background speech remains speech but is not a person.
            if speaker_name == NOISE_LABEL:
                seg_type = "event"
            else:
                seg_classification = classify_segment_text(text)
                seg_type = "event" if seg_classification == "event" else "speech"

            updated_segments.append(
                Conversation.SpeakerSegment(
                    start=seg.get("start", 0),
                    end=seg.get("end", 0),
                    text=text,
                    speaker="" if seg_type == "event" else speaker_name,
                    segment_type=seg_type,
                    identified_as=seg.get("identified_as"),
                    confidence=seg.get("confidence"),
                    words=segment_words,  # Use words from speaker service
                )
            )

        if empty_segment_count > 0:
            logger.info(
                f"🔇 Filtered out {empty_segment_count} empty segments from speaker recognition"
            )

        human_label_failures = _apply_human_speaker_overlays(
            updated_segments,
            await _human_speaker_annotations(conversation_id),
        )
        if human_label_failures:
            logger.info(
                "🎤 Preserved %d human speaker labels missed by reprocessing",
                len(human_label_failures),
            )
            # A human overlay changes who said what, which can move an episode's
            # participants — reconcile the range even if the model output is unchanged.
            await note_conversation_dirty(
                conversation_id, "speaker_overlay", source_kind="speaker"
            )

        # Compose meaningful event/note boundaries into the exclusive speech timeline.
        non_speech_segments = _retained_non_speech_segments(transcript_version.segments)
        if ran_pyannote_diarization and actual_words:
            non_speech_segments = _strip_reprojected_words_from_events(
                non_speech_segments,
                actual_words,
            )
        if non_speech_segments:
            updated_segments = _compose_exclusive_projection(
                updated_segments, non_speech_segments
            )
            logger.info(
                "🎤 Composed %d meaningful non-speech boundaries into an exclusive projection",
                len(non_speech_segments),
            )

        before_merge_count = len(updated_segments)
        updated_segments = _merge_adjacent_projected_speech(
            updated_segments,
            max_gap=float(diarization_settings.get("collar", 2.0)),
        )
        if len(updated_segments) != before_merge_count:
            logger.info(
                "🎤 Merged %d adjacent same-speaker projection seams",
                before_merge_count - len(updated_segments),
            )

        # Extract unique identified speakers for metadata
        identified_speakers = set()
        for seg in speaker_segments:
            identified_as = seg.get("identified_as")
            if identified_as and identified_as != "Unknown":
                identified_speakers.add(identified_as)

        reference_receipt = await target_visibility.reference_receipt(user_id)
        sr_metadata = {
            "enabled": True,
            "privacy_reference_receipt": reference_receipt,
            "privacy_gallery_receipt": gallery_scope.receipt(),
            "identification_mode": _speaker_identification_mode(
                ran_pyannote_diarization=ran_pyannote_diarization,
                used_word_timeline_fallback=used_word_timeline_fallback,
                use_per_segment=use_per_segment,
            ),
            "identified_speakers": list(identified_speakers),
            "speaker_count": len(identified_speakers),
            "total_segments": len(speaker_segments),
            "propagated_segments": propagated_segments,
            "processing_time_seconds": time.time() - start_time,
        }
        if speaker_result.get("partial_errors"):
            sr_metadata["partial_errors"] = speaker_result["partial_errors"]
        if speaker_result.get("identification_evidence"):
            sr_metadata["identification_evidence"] = speaker_result[
                "identification_evidence"
            ]
        if human_label_failures:
            sr_metadata["human_label_recognition_failures"] = human_label_failures

        # Which engine produced these segments. An empty neural result is represented as
        # Chronicle's deterministic word-timeline fallback, never mislabeled as pyannote
        # and never inherited from provider utterance boundaries.
        diarization_source_value = (
            "word_timeline_fallback"
            if used_word_timeline_fallback
            else (
                "pyannote"
                if ran_pyannote_diarization
                else source_version.diarization_source
            )
        )

        # Per-diarized-speaker pooled centroids used for identification, so a later
        # "reprocess impact"/drift check can re-identify against the updated gallery with
        # pure vector math (no GPU, no re-diarization).
        # Re-key from raw diar labels (SPEAKER_00) to the FINAL segment labels (name or
        # "Unknown Speaker N") so each centroid maps 1:1 to its segments' `speaker`.
        raw_centroids = speaker_result.get("cluster_centroids") or {}
        if raw_centroids:
            centroids_map = _rekey_cluster_centroids(
                raw_centroids,
                speaker_segments,
                unknown_label_map,
            )
        else:
            # The provider/segment-identification path (identify_provider_segments)
            # can return the embeddings it already computed. Pool those directly so
            # reprocessing does not reconstruct and embed the whole conversation again.
            centroids_map = _pool_returned_segment_embeddings(
                speaker_segments, unknown_label_map
            )
            if not centroids_map:
                # Pyannote and older callers do not return per-segment embeddings.
                centroids_map, _ = await compute_cluster_centroids(
                    conversation_id, updated_segments, speaker_client
                )

        if create_mode:
            # Build the new version only now that we have a usable result. Carry the
            # source's text/words/provider; segments + speaker metadata are the new work.
            new_metadata = {
                "reprocessing_type": "speaker_diarization",
                "source_version_id": source_version.version_id,
                "trigger": "manual_reprocess",
                "requested_diarization_source": preferred_source,
                "provider_capabilities": provider_capabilities,
                "speaker_recognition": sr_metadata,
            }
            if centroids_map:
                new_metadata["cluster_centroids"] = centroids_map
            if used_word_timeline_fallback:
                new_metadata["diarization_fallback"] = {
                    "mode": "word_timeline",
                    "reason": "pyannote_empty",
                }
            new_version = conversation.add_transcript_version(
                version_id=version_id,
                transcript=source_version.transcript,
                words=source_version.words,
                segments=updated_segments,
                provider=source_version.provider,
                model=source_version.model,
                metadata=new_metadata,
                set_as_active=True,
            )
            new_version.diarization_source = diarization_source_value
            conversation.apply_status(settled=True)
            logger.info(
                f"🎤 Created reprocess version {version_id} from source "
                f"{source_version.version_id} with {len(updated_segments)} segments"
            )
        else:
            # In-place: refine the existing version.
            transcript_version.segments = updated_segments
            if not transcript_version.metadata:
                transcript_version.metadata = {}
            transcript_version.metadata["speaker_recognition"] = sr_metadata
            if ran_pyannote_diarization:
                transcript_version.diarization_source = "pyannote"
            if centroids_map:
                transcript_version.metadata["cluster_centroids"] = centroids_map

        async with gallery_scope.publication(target_visibility):
            projected_version = new_version if create_mode else transcript_version
            if create_mode:
                compacted_versions = await _compact_embedded_speaker_history(
                    conversation,
                    keep_version_id=projected_version.version_id,
                )
                if compacted_versions:
                    logger.info(
                        "🎤 Compacted %d archived speaker projection(s) from the "
                        "Conversation read model",
                        compacted_versions,
                    )
            diarization_artifact = await persist_diarization_artifact(
                user_id=user_id,
                audio_ranges=conversation.audio_ranges,
                retry_key=f"speaker-diarization:{conversation_id}:{version_id}",
                provider=(
                    "word_timeline_fallback"
                    if used_word_timeline_fallback
                    else ("pyannote" if ran_pyannote_diarization else "provider")
                ),
                model=speaker_result.get("diarization_model"),
                segments=speaker_segments,
                configuration={
                    "privacy_reference_receipt": reference_receipt,
                    "privacy_gallery_receipt": gallery_scope.receipt(),
                    **dict(diarization_settings),
                    "requested_source": preferred_source,
                    "ran_pyannote_segmentation": ran_pyannote_diarization,
                    "pyannote_returned_turns": not used_word_timeline_fallback,
                    "fallback_mode": (
                        "word_timeline" if used_word_timeline_fallback else None
                    ),
                    "neural_window_ceiling_seconds": 1200,
                },
            )
            projected_version.metadata["diarization_artifact_id"] = (
                diarization_artifact.artifact_id
            )
            transcript_artifact_ids = await resolve_transcript_artifact_ids(
                conversation_id,
                source_version,
            )
            if transcript_artifact_ids:
                projected_version.metadata["transcript_artifact_ids"] = (
                    transcript_artifact_ids
                )
            revision = await persist_conversation_revision(
                conversation,
                projected_version,
                retry_key=f"speaker-projection:{conversation_id}:{version_id}",
                transcript_artifact_ids=transcript_artifact_ids,
                diarization_artifact_ids=[diarization_artifact.artifact_id],
            )

            await conversation.save()

        await note_conversation_dirty(
            conversation_id,
            "speaker_revision",
            source_revision=revision.revision_id,
            source_kind="speaker",
        )

        processing_time = time.time() - start_time
        logger.info(
            f"✅ Speaker recognition completed for {conversation_id} in {processing_time:.2f}s"
        )

        return {
            "success": True,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "speaker_recognition_enabled": True,
            "identified_speakers": list(identified_speakers),
            "segment_count": len(updated_segments),
            "diarization_artifact_id": diarization_artifact.artifact_id,
            "transcript_revision_id": revision.revision_id,
            "processing_time_seconds": processing_time,
        }

    except privacy.PrivacyHeld:
        # A hold is a policy outcome. Do not serialize recognition names into
        # error strings, traces, job metadata or a successful result.
        raise

    except asyncio.TimeoutError as e:
        logger.error(f"❌ Speaker recognition timeout: {e}")

        # Add timeout metadata to job
        update_job_meta(
            error_type="timeout",
            audio_duration=conversation.audio_total_duration if conversation else None,
            timeout_occurred_at=time.time(),
        )

        # In create mode no version was created — surface the failure rather than
        # reporting a quiet success:False (which RQ treats as "OK", hiding it).
        if create_mode:
            raise SpeakerReprocessFailed("speaker reprocess timed out") from e

        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": "Speaker recognition timeout",
            "error_type": "timeout",
            "audio_duration": (
                conversation.audio_total_duration if conversation else None
            ),
            "processing_time_seconds": time.time() - start_time,
        }

    except SpeakerServiceError:
        # A genuine service failure — let it propagate so RQ fails the job and the
        # chain's failure machinery (retry, conversation-linked system event,
        # allow_failure deps) engages. Don't bury it as a success:False dict.
        raise

    except Exception as speaker_error:
        logger.error(f"❌ Speaker recognition failed: {speaker_error}")
        logger.debug(traceback.format_exc())

        # Create mode created no version — surface the failure so it isn't a silent
        # no-op (RQ treats success:False as "OK"). In-place keeps the existing transcript.
        if create_mode:
            raise SpeakerReprocessFailed(str(speaker_error)) from speaker_error

        return {
            "success": False,
            "conversation_id": conversation_id,
            "version_id": version_id,
            "error": str(speaker_error),
            "processing_time_seconds": time.time() - start_time,
        }
