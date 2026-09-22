"""
Data-audit controller.

Backs the Data Audit dashboard page: surfaces per-conversation VAD speech
metrics + latest speaker labels, filters by a compound predicate (speech
fraction AND speaker include/exclude), enqueues batch audio analysis,
archives (hard-deletes) audio, and splits/merges conversations.
"""

import hashlib
import json
import logging
import re
import shutil
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import JSONResponse

import backend.services.privacy as privacy_module
import backend.services.recording_purpose as recording_purpose
from backend.client_manager import synthetic_client_id
from backend.config import get_diarization_settings
from backend.constants import is_non_enrollable_speaker, is_unknown_speaker_label
from backend.controllers.conversation_controller import archive_conversation_audio
from backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    start_post_conversation_jobs,
)
from backend.models.annotation import Annotation, AnnotationType
from backend.models.conversation import Conversation, create_conversation
from backend.services import privacy
from backend.services.audio_claims import (
    apply_audio_ranges,
    merge_audio_ranges,
    partition_audio_ranges,
    resolve_audio_ranges,
    resolve_conversation_audio,
)
from backend.services.memory import get_memory_service
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.users import User
from backend.utils.annotation_export import (
    EXPORTS_DIR,
    META_NAME,
    ZIP_NAME,
    active_segments,
    export_dir,
    new_export_id,
    validate_export_id,
)
from backend.utils.annotation_import import (
    AnnotationDatasetError,
    parse_annotation_dataset,
)
from backend.utils.audio_chunk_utils import (
    audio_cache_duration_matches,
    convert_audio_to_chunks,
    reconstruct_audio_segment,
)
from backend.utils.audio_utils import AudioValidationError, validate_and_prepare_audio
from backend.utils.export_planning import export_eligibility, plan_conversation_clips
from backend.utils.transcript_slicing import (
    build_transcript_text,
    shift_segments,
    shift_words,
    slice_segments,
    slice_words,
)
from backend.utils.vad_analysis import (
    detect_silence_gaps,
    frame_speech_intervals,
    intersect_intervals,
    merge_speech_regions,
    speech_fraction_from_histogram,
)
from backend.workers.data_audit_jobs import (
    analyze_audio_batch_job,
    auto_clean_job,
    export_annotation_dataset_job,
    get_sensitivity_policy,
    screen_conversations_job,
)

logger = logging.getLogger(__name__)

# Upper bound on how many conversations a single scan inspects in memory.
# Speaker/silence predicates are applied in Python, so we cap the working set
# and report when it was hit rather than silently truncating.
MAX_SCAN = 2000

SPEAKER_REVIEW_CONTEXT_SEGMENTS = 1

# Projection: lightweight metadata + cached VAD analysis + speaker labels
# from the active transcript version's segments (no transcript text / words).
_SCAN_PROJECTION = {
    "conversation_id": 1,
    "user_id": 1,
    "client_id": 1,
    "title": 1,
    "created_at": 1,
    "audio_total_duration": 1,
    "audio_chunks_count": 1,
    "audio_archived": 1,
    "audio_archived_at": 1,
    "archive_reason": 1,
    "processing_status": 1,
    "failure_stage": 1,
    "external_source_id": 1,
    "external_source_type": 1,
    "vad_analysis": 1,
    "derived_from": 1,
    "active_transcript_version": 1,
    "transcript_versions.version_id": 1,
    "transcript_versions.segments.speaker": 1,
    "transcript_versions.segments.identified_as": 1,
    "transcript_versions.segments.confidence": 1,
    "transcript_versions.segments.segment_type": 1,
}


def _audit_segments(doc: dict) -> list:
    """Segments to audit for this conversation.

    Prefers the active transcript version's segments. Falls back to the version
    with the most identified segments when the active pointer is missing or
    points at an empty version — a known reprocess failure mode leaves a
    dangling ``active_transcript_version`` (a UUID absent from the versions
    dict), which would otherwise hide a conversation's segments entirely.
    """
    active = doc.get("active_transcript_version")
    best: list = []
    best_score = -1
    for tv in doc.get("transcript_versions") or []:
        segs = tv.get("segments") or []
        if tv.get("version_id") == active and segs:
            return segs
        ident = sum(1 for s in segs if s.get("identified_as"))
        score = ident * 100000 + len(segs)
        if score > best_score:
            best_score = score
            best = segs
    return best


def _speaker_review_key(conversation_id: str, segment_start: float) -> str:
    """Stable key shared by queue selection and submitted review decisions."""
    return f"{conversation_id}:{round(float(segment_start), 3):.3f}"


def _speaker_review_candidates(doc: dict, reviewed_keys: set[str]) -> List[dict]:
    """Return every reviewable identity claim in transcript order.

    This deliberately performs no anomaly scoring: an identified speech segment is
    an identity claim and therefore eligible for human review.  Context is adjacent
    transcript evidence only, not another model's foreground/background estimate.
    """
    conversation_id = doc.get("conversation_id")
    if not conversation_id:
        return []
    segments = _audit_segments(doc)
    candidates = []
    for index, segment in enumerate(segments):
        if (segment.get("segment_type") or "speech") != "speech":
            continue
        claimed_speaker = segment.get("identified_as")
        start = segment.get("start")
        end = segment.get("end")
        if not claimed_speaker or start is None or end is None or end <= start:
            continue
        review_key = _speaker_review_key(conversation_id, start)
        if review_key in reviewed_keys:
            continue

        context = []
        context_start = max(0, index - SPEAKER_REVIEW_CONTEXT_SEGMENTS)
        context_end = min(len(segments), index + SPEAKER_REVIEW_CONTEXT_SEGMENTS + 1)
        for context_index in range(context_start, context_end):
            item = segments[context_index]
            context.append(
                {
                    "position": (
                        "current"
                        if context_index == index
                        else "before" if context_index < index else "after"
                    ),
                    "speaker": item.get("identified_as") or item.get("speaker"),
                    "text": item.get("text") or "",
                    "start": item.get("start"),
                    "end": item.get("end"),
                }
            )

        candidates.append(
            {
                "review_key": review_key,
                "conversation_id": conversation_id,
                "conversation_title": doc.get("title"),
                "conversation_date": str(doc.get("created_at") or ""),
                "segment_index": index,
                "segment_start_time": float(start),
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "text": segment.get("text") or "",
                "claimed_speaker": claimed_speaker,
                "raw_speaker": segment.get("speaker"),
                "confidence": segment.get("confidence"),
                "context": context,
            }
        )
    return candidates


def _select_speaker_review_batch(
    candidates: List[dict],
    batch_size: int,
    threshold: float,
    conversation_review_counts: Dict[str, int],
    speaker_review_counts: Dict[str, int],
) -> List[dict]:
    """Select an information-bearing, corpus-diverse review batch.

    Three of every five slots target the decision boundary; two are deterministic
    controls from the rest of the score range.  Conversation and speaker coverage
    precede uncertainty so one long recording or frequent person cannot monopolize
    review.  The control lane is essential: boundary-only review cannot estimate
    false accepts made with high model confidence.
    """
    if not candidates:
        return []

    def coverage(candidate: dict) -> tuple:
        return (
            conversation_review_counts.get(candidate["conversation_id"], 0),
            speaker_review_counts.get(candidate["claimed_speaker"], 0),
        )

    def boundary_key(candidate: dict) -> tuple:
        confidence = candidate.get("confidence")
        distance = abs(float(confidence) - threshold) if confidence is not None else 2.0
        return (*coverage(candidate), distance, candidate["review_key"])

    def control_key(candidate: dict) -> tuple:
        stable = hashlib.sha256(candidate["review_key"].encode()).hexdigest()
        return (*coverage(candidate), stable)

    boundary = sorted(candidates, key=boundary_key)
    control_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("confidence") is not None
        and abs(float(candidate["confidence"]) - threshold) >= 0.1
    ]
    control = sorted(control_candidates or candidates, key=control_key)
    lane_pattern = ["boundary", "boundary", "control", "boundary", "control"]
    picked: List[dict] = []
    picked_keys: set[str] = set()
    picked_conversations: set[str] = set()
    picked_speakers: set[str] = set()

    def take(pool: List[dict], require_new_speaker: bool) -> Optional[dict]:
        for candidate in pool:
            if candidate["review_key"] in picked_keys:
                continue
            if candidate["conversation_id"] in picked_conversations:
                continue
            if require_new_speaker and candidate["claimed_speaker"] in picked_speakers:
                continue
            return candidate
        return None

    for slot in range(batch_size):
        pool = (
            boundary
            if lane_pattern[slot % len(lane_pattern)] == "boundary"
            else control
        )
        candidate = take(pool, require_new_speaker=True)
        if candidate is None:
            candidate = take(pool, require_new_speaker=False)
        if candidate is None:
            other = control if pool is boundary else boundary
            candidate = take(other, require_new_speaker=False)
        if candidate is None:
            break
        candidate = {
            **candidate,
            "selection_lane": lane_pattern[slot % len(lane_pattern)],
        }
        picked.append(candidate)
        picked_keys.add(candidate["review_key"])
        picked_conversations.add(candidate["conversation_id"])
        picked_speakers.add(candidate["claimed_speaker"])
    return picked


def _speakers_for_doc(doc: dict) -> List[str]:
    """Distinct speaker labels from the audited transcript version's segments.

    Prefers ``identified_as`` (recognized name) over the raw ``speaker`` label.
    """
    speakers: set = set()
    for seg in _audit_segments(doc):
        label = seg.get("identified_as") or seg.get("speaker")
        if label:
            speakers.add(label)
    return sorted(speakers)


def _speaker_facets_for_doc(doc: dict) -> tuple[List[str], bool]:
    """Return global identities separately from conversation-local unknowns.

    ``Unknown Speaker N`` is numbered independently inside each conversation.
    It is a display description, not a corpus-wide identity, so it must never
    enter a global list of named speakers.
    """
    labels = _speakers_for_doc(doc)
    return (
        [label for label in labels if not is_unknown_speaker_label(label)],
        any(is_unknown_speaker_label(label) for label in labels),
    )


def _unknown_speech_count(doc: dict) -> int:
    """Speech segments in the audited version not matched to an enrolled speaker.

    The reliable "needs triage" signal: a speech segment with no ``identified_as``
    is either an unenrolled person or background noise. Drives the table's
    "N to review" hint so you can see which files still need triage.
    """
    count = 0
    for seg in _audit_segments(doc):
        if (seg.get("segment_type") or "speech") != "speech":
            continue
        if not seg.get("identified_as"):
            count += 1
    return count


def _marginal_identified_count(doc: dict, threshold: float, margin: float) -> int:
    """Speech segments identified as an enrolled speaker but at a confidence
    below ``threshold + margin`` — weak matches that likely shouldn't carry a
    name (e.g. background noise force-labeled as the nearest speaker). This is
    the cheap, stored-data review signal: confidence is already on each segment,
    so no audio re-embedding is needed.
    """
    cutoff = threshold + margin
    count = 0
    for seg in _audit_segments(doc):
        if (seg.get("segment_type") or "speech") != "speech":
            continue
        if not seg.get("identified_as"):
            continue
        conf = seg.get("confidence")
        if conf is not None and conf < cutoff:
            count += 1
    return count


def _vad_stale(va: Optional[dict], duration: float) -> bool:
    """True if a cached ``vad_analysis`` no longer describes the conversation's
    current audio. The analysis is derived from the chunk set; if the chunks
    changed in place its implied duration (frame_count * frame_hop) drifts from
    ``audio_total_duration`` and the speech metrics are stale — the Analyze
    button should re-run it. Returns False when there is no cached analysis
    (that's "unanalyzed", surfaced separately)."""
    if not va:
        return False
    cached = (va.get("frame_count") or 0) * (va.get("frame_hop_ms") or 0) / 1000.0
    return not audio_cache_duration_matches(cached, duration)


def _latest_exports_by_conversation(user: User) -> Dict[str, dict]:
    """conversation_id → the most recent export that actually shipped it.

    Read from the on-disk export metadata (the audit trail the export job
    already writes) rather than a new Mongo field, so deleting an export
    directory naturally un-marks its conversations. Conversations that were
    selected but skipped don't count as exported. Scoped by the same
    ownership rule as ``list_exports``.
    """
    latest: Dict[str, dict] = {}
    if not EXPORTS_DIR.is_dir():
        return latest
    for meta_path in EXPORTS_DIR.glob(f"*/{META_NAME}"):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if not user.is_superuser and meta.get("created_by") != str(user.user_id):
            continue
        created_at = meta.get("created_at") or ""
        for conv in meta.get("conversations", []):
            if conv.get("skipped_reason"):
                continue
            cid = conv.get("conversation_id")
            if cid and created_at > (latest.get(cid, {}).get("created_at") or ""):
                latest[cid] = {
                    "export_id": meta.get("export_id"),
                    "created_at": created_at,
                }
    return latest


async def list_for_audit(
    user: User,
    speech_threshold: float = 0.5,
    min_speech_fraction: float = 0.0,
    max_speech_fraction: float = 1.0,
    min_duration: float = 0.0,
    max_duration: float = 0.0,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    include_speakers: Optional[List[str]] = None,
    exclude_speakers: Optional[List[str]] = None,
    unknown_speakers: Optional[str] = None,
    dataset_id: Optional[str] = None,
    exported: Optional[str] = None,
    archived_only: bool = False,
    hide_failed: bool = False,
    hide_reviewed: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    """List conversations with VAD speech metrics + speakers, filtered by the
    compound predicate. Returns derived ``speech_fraction`` at the requested
    probability threshold so the UI threshold control is just a re-fetch.

    Speaker filtering is per-speaker tri-state: a conversation is kept only if
    it contains at least one ``include_speakers`` (when any are set) AND none of
    the ``exclude_speakers``. Speech bounds exclude unanalyzed conversations;
    ``max_speech_fraction=1`` / ``min_speech_fraction=0`` / ``max_duration=0``
    disable the respective bound.

    ``exported`` filters on annotation-export history (from the on-disk export
    metadata): ``never`` keeps only conversations no export has shipped,
    ``exported`` only those a previous export contains. Each row carries
    ``last_export`` either way, so curation sessions can skip audio already
    sent to annotators.
    """
    try:
        visibility = privacy.ConversationPrivacyFilter()
        base: dict = {} if user.is_superuser else {"user_id": str(user.user_id)}

        if archived_only:
            base["audio_archived"] = True
        else:
            # audio_archived is a newer field; absent == not archived
            base["audio_archived"] = {"$ne": True}
            base["deleted"] = {"$ne": True}
            base["audio_chunks_count"] = {"$gt": 0}
            # Exclude conversations flagged with corrupt audio metadata — they're
            # surfaced on the System Errors page instead (a None match also covers
            # the field being absent on normal conversations).
            base["audio_integrity_error"] = None
            # Opt-in: drop conversations the pipeline marked failed (keeps null
            # legacy status and in-progress 'active' rows visible — the latter
            # are chipped as 'Processing…' so a stuck one is still apparent).
            if hide_failed:
                base["processing_status"] = {
                    "$ne": Conversation.ConversationStatus.FAILED.value
                }

        if dataset_id:
            base["external_source_type"] = "annotation_dataset"
            base["external_source_id"] = {"$regex": f"^{re.escape(dataset_id)}:"}

        # Date range goes into the Mongo query (not the Python predicate) so
        # it narrows the MAX_SCAN working set instead of competing with it.
        if created_after or created_before:
            created: dict = {}
            if created_after:
                created["$gte"] = created_after
            if created_before:
                created["$lte"] = created_before
            base["created_at"] = created

        collection = Conversation.get_pymongo_collection()
        cursor = (
            collection.find(base, _SCAN_PROJECTION)
            .sort("created_at", -1)
            .limit(MAX_SCAN)
        )
        raw_docs = await cursor.to_list(length=MAX_SCAN)
        scan_capped = len(raw_docs) >= MAX_SCAN
        raw_docs = await visibility.filter(raw_docs)

        dataset_base: dict = {} if user.is_superuser else {"user_id": str(user.user_id)}
        dataset_base.update(
            {
                "external_source_type": "annotation_dataset",
                "audio_archived": {"$ne": True},
                "deleted": {"$ne": True},
                "audio_chunks_count": {"$gt": 0},
            }
        )
        dataset_docs = await (
            collection.find(
                dataset_base, {"external_source_id": 1, "conversation_id": 1}
            )
            .sort("created_at", -1)
            .limit(MAX_SCAN)
        ).to_list(length=MAX_SCAN)
        dataset_docs = await visibility.filter(dataset_docs)
        available_datasets = list(
            dict.fromkeys(
                source_id.rsplit(":", 1)[0]
                for doc in dataset_docs
                if (source_id := doc.get("external_source_id")) and ":" in source_id
            )
        )

        # Match threshold the pipeline used + a small comfort margin: an
        # identification within this band of the cutoff is a weak/suspect match
        # (the "low-confidence" review signal), computed from stored confidence.
        similarity_threshold = float(
            get_diarization_settings().get("similarity_threshold", 0.5)
        )
        marginal_margin = 0.05

        include_set = set(include_speakers or [])
        exclude_set = set(exclude_speakers or [])
        export_history = _latest_exports_by_conversation(user)
        matched: List[dict] = []
        # Speakers present anywhere in the scanned working set (before the
        # compound predicate), so the filter UI offers exactly the labels that
        # exist in this view — and isn't narrowed by its own selection.
        available_speakers: set = set()
        has_unknown_speakers = False

        for doc in raw_docs:
            duration = doc.get("audio_total_duration") or 0.0
            va = doc.get("vad_analysis")
            all_doc_speakers = _speakers_for_doc(doc)
            doc_speakers, doc_has_unknown = _speaker_facets_for_doc(doc)
            available_speakers.update(doc_speakers)
            has_unknown_speakers = has_unknown_speakers or doc_has_unknown
            unknown_count = _unknown_speech_count(doc)

            speech_fraction = None
            if va:
                speech_fraction = speech_fraction_from_histogram(
                    histogram=va.get("histogram") or [],
                    frame_count=va.get("frame_count") or 0,
                    histogram_bin_width=va.get("histogram_bin_width") or 0.05,
                    threshold=speech_threshold,
                )

            # --- Compound filter (skip filters for the archived audit view) ---
            if not archived_only:
                if duration < min_duration:
                    continue
                if 0.0 < max_duration < duration:
                    continue
                if max_speech_fraction < 1.0 or min_speech_fraction > 0.0:
                    # Unanalyzed conversations can't satisfy a speech filter
                    if speech_fraction is None:
                        continue
                    if speech_fraction > max_speech_fraction:
                        continue
                    if speech_fraction < min_speech_fraction:
                        continue
                include_matches = bool(include_set.intersection(doc_speakers))
                if (include_set or unknown_speakers == "include") and not (
                    include_matches
                    or (unknown_speakers == "include" and doc_has_unknown)
                ):
                    continue
                if exclude_set and exclude_set.intersection(doc_speakers):
                    continue
                if unknown_speakers == "exclude" and doc_has_unknown:
                    continue
                # Opt-in: drop conversations with nothing left to triage (every
                # speech segment already has an identified_as).
                if hide_reviewed and unknown_count == 0:
                    continue
                in_export = doc.get("conversation_id") in export_history
                if exported == "never" and in_export:
                    continue
                if exported == "exported" and not in_export:
                    continue

            created_at = doc.get("created_at")
            archived_at = doc.get("audio_archived_at")
            derived_from = doc.get("derived_from")
            matched.append(
                {
                    "conversation_id": doc.get("conversation_id"),
                    "title": doc.get("title"),
                    "client_id": doc.get("client_id"),
                    "created_at": created_at.isoformat() if created_at else None,
                    "duration_seconds": duration,
                    "speakers": all_doc_speakers,
                    "unknown_speech_segments": unknown_count,
                    "marginal_identified_segments": _marginal_identified_count(
                        doc, similarity_threshold, marginal_margin
                    ),
                    "processing_status": doc.get("processing_status"),
                    "failure_stage": doc.get("failure_stage"),
                    "analyzed": va is not None,
                    "speech_fraction": (
                        round(speech_fraction, 4)
                        if speech_fraction is not None
                        else None
                    ),
                    "derived_operation": (
                        derived_from.get("operation") if derived_from else None
                    ),
                    "last_export": export_history.get(doc.get("conversation_id")),
                    "audio_archived": doc.get("audio_archived", False),
                    "audio_archived_at": (
                        archived_at.isoformat() if archived_at else None
                    ),
                    "archive_reason": doc.get("archive_reason"),
                }
            )

        total = len(matched)
        page = matched[offset : offset + limit]

        # How many conversations the Analyze button would actually process:
        # the user's own live audio without cached VAD analysis (the batch job
        # is scoped to the requesting user, so the count matches even for
        # superusers viewing everyone's rows). Lets the UI disable the button
        # when there is nothing left to analyze.
        # Conversations the Analyze button should process: those with no VAD
        # analysis OR whose cached analysis went stale (audio changed in place
        # since it was computed). Staleness needs the frame-count/duration
        # comparison, so count it in Python over the projected set rather than a
        # Mongo predicate. Folding stale into this count means a stale cache
        # surfaces as a non-zero Analyze count and the existing button re-runs
        # it — no separate UI affordance for what is a rare contingency.
        analyze_docs = await collection.find(
            {
                "user_id": str(user.user_id),
                "audio_archived": {"$ne": True},
                "deleted": {"$ne": True},
                "audio_chunks_count": {"$gt": 0},
                "audio_integrity_error": None,
            },
            {
                "conversation_id": 1,
                "vad_analysis.frame_count": 1,
                "vad_analysis.frame_hop_ms": 1,
                "audio_total_duration": 1,
            },
        ).to_list(length=MAX_SCAN)
        analyze_docs = await visibility.filter(analyze_docs)
        unanalyzed_count = sum(
            1
            for d in analyze_docs
            if d.get("vad_analysis") is None
            or _vad_stale(d.get("vad_analysis"), d.get("audio_total_duration") or 0.0)
        )

        await visibility.assert_current()
        return {
            "conversations": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "scan_capped": scan_capped,
            "speech_threshold": speech_threshold,
            "similarity_threshold": similarity_threshold,
            "marginal_margin": marginal_margin,
            "unanalyzed_count": unanalyzed_count,
            "speakers": sorted(available_speakers),
            "has_unknown_speakers": has_unknown_speakers,
            "datasets": available_datasets,
        }

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.exception(f"Error listing conversations for cleaning: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error listing conversations"}
        )


def _recommend_threshold(values: List[float]) -> Optional[float]:
    """Otsu-style split: the cutoff in [0.36, 0.60] maximizing between-class
    variance of the confidence distribution. With the bimodal noise/real
    structure this lands in the valley between the two humps — a data-driven
    similarity-threshold suggestion. Needs enough points to be meaningful.
    """
    if len(values) < 30:
        return None
    best_t: Optional[float] = None
    best_var = -1.0
    t = 0.36
    while t <= 0.60001:
        below = [v for v in values if v < t]
        above = [v for v in values if v >= t]
        if below and above:
            wb = len(below) / len(values)
            wa = len(above) / len(values)
            var = wb * wa * (statistics.mean(above) - statistics.mean(below)) ** 2
            if var > best_var:
                best_var = var
                best_t = round(t, 2)
        t += 0.01
    return best_t


async def speaker_confidence_overview(user: User):
    """Per-speaker identification-confidence statistics across the corpus.

    Reads stored per-segment confidence (no audio re-embedding) and reports the
    global distribution histogram, the marginal-match fraction, per-speaker
    baselines (mean/median/min/max + %marginal + survival at the live
    threshold), survival counts at candidate thresholds, and a data-driven
    recommended threshold. This is the strategic view: it surfaces which
    enrolled speakers are "noise magnets" (matches clustered at the floor) and
    what threshold cleanly separates real matches from noise.
    """
    try:
        threshold = float(get_diarization_settings().get("similarity_threshold", 0.5))
        margin = 0.05

        base: dict = {"deleted": {"$ne": True}}
        if not user.is_superuser:
            base["user_id"] = str(user.user_id)

        collection = Conversation.get_pymongo_collection()
        cursor = collection.find(base, _SCAN_PROJECTION).limit(MAX_SCAN)
        docs = await cursor.to_list(length=MAX_SCAN)

        scan_capped = len(docs) >= MAX_SCAN
        visibility = privacy.ConversationPrivacyFilter()
        docs = await visibility.filter(docs)

        per_speaker: Dict[str, List[float]] = {}
        per_speaker_convs: Dict[str, set] = {}
        all_conf: List[float] = []
        convs_with_ids = 0

        for doc in docs:
            cid = doc.get("conversation_id")
            had = False
            for seg in _audit_segments(doc):
                if (seg.get("segment_type") or "speech") != "speech":
                    continue
                name = seg.get("identified_as")
                conf = seg.get("confidence")
                if not name or conf is None:
                    continue
                # Reserved category labels ("Background Speech", "Noise") are
                # written into identified_as but are NOT speaker identities — their
                # stored confidence is a background-bucket similarity, not an
                # identification score. Exclude them so they don't appear as bogus
                # "speaker" rows (and skew the global histogram/marginal stats).
                if is_non_enrollable_speaker(name):
                    continue
                had = True
                all_conf.append(conf)
                per_speaker.setdefault(name, []).append(conf)
                per_speaker_convs.setdefault(name, set()).add(cid)
            if had:
                convs_with_ids += 1

        cutoff = threshold + margin
        # Histogram: 0.30..1.00 in 0.05 bins (14 bins).
        bin_width = 0.05
        hist_start = 0.30
        n_bins = 14
        counts = [0] * n_bins
        for v in all_conf:
            idx = int((v - hist_start) / bin_width)
            idx = max(0, min(n_bins - 1, idx))
            counts[idx] += 1

        total = len(all_conf)
        marginal = sum(1 for v in all_conf if v < cutoff)
        survival = [
            {
                "threshold": t,
                "keep": sum(1 for v in all_conf if v >= t),
                "drop": sum(1 for v in all_conf if v < t),
            }
            for t in (0.40, 0.45, 0.50, 0.55)
        ]

        speakers = []
        for name, vals in per_speaker.items():
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            speakers.append(
                {
                    "name": name,
                    "nseg": n,
                    "nconv": len(per_speaker_convs[name]),
                    "mean": round(statistics.mean(vals_sorted), 3),
                    "median": round(statistics.median(vals_sorted), 3),
                    "min": round(vals_sorted[0], 3),
                    "max": round(vals_sorted[-1], 3),
                    "marginal_pct": round(
                        100.0 * sum(1 for v in vals_sorted if v < cutoff) / n, 1
                    ),
                    "keep_pct": round(
                        100.0 * sum(1 for v in vals_sorted if v >= threshold) / n, 1
                    ),
                }
            )
        speakers.sort(key=lambda s: (-s["marginal_pct"], -s["nseg"]))

        # Enrolled speakers with NO stored identifications yet (e.g. just enrolled)
        # would otherwise be invisible here — list them explicitly so a fresh
        # enrollment shows up immediately instead of silently missing until some
        # conversation is (re)processed and matches them.
        try:
            enrolled = await SpeakerRecognitionClient().get_enrolled_speakers()
            for sp in enrolled.get("speakers", []):
                sp_name = sp.get("name")
                if not sp_name or sp_name in per_speaker:
                    continue
                speakers.append(
                    {
                        "name": sp_name,
                        "nseg": 0,
                        "nconv": 0,
                        "mean": None,
                        "median": None,
                        "min": None,
                        "max": None,
                        "marginal_pct": None,
                        "keep_pct": None,
                        "never_identified": True,
                    }
                )
        except Exception:
            logger.warning("Could not list enrolled speakers for confidence overview")

        await visibility.assert_current()
        return {
            "threshold": threshold,
            "margin": margin,
            "total_identified": total,
            "conversations_with_ids": convs_with_ids,
            "conversations_scanned": len(docs),
            "scan_capped": scan_capped,
            "marginal_count": marginal,
            "marginal_fraction": round(marginal / total, 4) if total else 0.0,
            "histogram": {
                "start": hist_start,
                "bin_width": bin_width,
                "counts": counts,
            },
            "survival": survival,
            "recommended_threshold": _recommend_threshold(all_conf),
            "speakers": speakers,
        }
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.exception(f"Error computing speaker confidence overview: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "Error computing speaker confidence overview"},
        )


async def enqueue_analysis(
    user: User, conversation_ids: Optional[List[str]] = None, force: bool = False
):
    """Enqueue the batch VAD-analysis job for the user's conversations."""
    try:
        # Pass as kwargs so the job's user_id is recorded in job.kwargs — the
        # queue status endpoint authorizes non-admins by job.kwargs["user_id"].
        job = default_queue.enqueue(
            analyze_audio_batch_job,
            user_id=str(user.user_id),
            conversation_ids=conversation_ids,
            force=force,
            job_timeout=3600,
            result_ttl=JOB_RESULT_TTL,
            description="Analyze conversation audio (VAD)",
        )
        logger.info(f"Enqueued VAD analysis job {job.id} for user {user.user_id}")
        return {"job_id": job.id, "status": "queued"}

    except Exception as e:
        logger.exception(f"Error enqueueing VAD analysis: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing analysis"}
        )


async def run_auto_clean_cron() -> dict:
    """Cron entrypoint for auto-clean.

    Runs in the FastAPI process (invoked by the cron scheduler). It does NOT do
    the work itself — it enqueues the ``auto_clean_job`` RQ job so the sweep is
    visible on the Queue/Jobs page (and runs in the worker). Returns the
    enqueued job id, which the cron-jobs page records as the run result.
    """
    job = default_queue.enqueue(
        auto_clean_job,
        job_timeout=3600,
        result_ttl=JOB_RESULT_TTL,
        description="Auto-clean: archive speech-free conversations",
    )
    logger.info(f"Auto-clean cron enqueued job {job.id}")
    return {"enqueued_job_id": job.id, "queue": "default"}


async def archive_audio_many(user: User, conversation_ids: List[str], reason: str):
    """Archive (hard-delete) audio for multiple conversations."""
    results = []
    for cid in conversation_ids:
        response = await archive_conversation_audio(cid, user, reason)
        try:
            body = json.loads(bytes(response.body).decode())
        except Exception:
            body = {}
        results.append(
            {
                "conversation_id": cid,
                "status_code": response.status_code,
                "ok": response.status_code == 200,
                "deleted_chunks": body.get("deleted_chunks"),
                "error": body.get("error"),
            }
        )

    archived = sum(1 for r in results if r["ok"])
    return {"archived": archived, "total": len(conversation_ids), "results": results}


# ---------------------------------------------------------------------------
# Split / merge
# ---------------------------------------------------------------------------


async def _load_operable_conversation(
    user: User, conversation_id: str
) -> Tuple[Optional[Conversation], Optional[JSONResponse]]:
    """Fetch a conversation and validate it can be split/merged.

    Returns (conversation, None) or (None, error_response).
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        return None, JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    if not user.is_superuser and conversation.user_id != str(user.user_id):
        return None, JSONResponse(
            status_code=403, content={"error": "Access forbidden"}
        )

    try:
        await privacy_module.require_record(conversation)
    except privacy_module.PrivacyHeld:
        return None, JSONResponse(
            status_code=423,
            content={"error": "Private or unscreened evidence is held from processing"},
        )
    if conversation.deleted:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation is deleted"}
        )
    if conversation.audio_archived:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation audio is archived"}
        )
    if not conversation.audio_chunks_count:
        return None, JSONResponse(
            status_code=409, content={"error": "Conversation has no audio"}
        )
    if conversation.derived_into:
        return None, JSONResponse(
            status_code=409,
            content={"error": "Conversation was already split/merged"},
        )
    return conversation, None


async def _chunk_timeline(conversation_id: str) -> List[dict]:
    """Virtual presentation timeline derived from immutable audio claims."""
    claimed = await resolve_conversation_audio(conversation_id)
    return [
        {
            "chunk_index": index,
            "start_time": item.conversation_start_seconds,
            "end_time": item.conversation_start_seconds + item.duration_seconds,
            "duration": item.duration_seconds,
            "vad": item.chunk.vad.model_dump() if item.chunk.vad else None,
        }
        for index, item in enumerate(claimed)
    ]


def _active_transcript_version(
    conversation: Conversation,
) -> Optional["Conversation.TranscriptVersion"]:
    for version in conversation.transcript_versions:
        if version.version_id == conversation.active_transcript_version:
            return version
    return None


async def _delete_source_memories(user_id: str, conversation_id: str) -> None:
    """Best-effort removal of memories sourced from a replaced conversation."""
    try:
        memory_service = get_memory_service()
        deleted = await memory_service.delete_memories_by_source(
            user_id, conversation_id
        )
        if deleted:
            logger.info(
                f"Deleted {deleted} memories sourced from {conversation_id[:12]}"
            )
    except Exception as e:
        logger.warning(
            f"Memory cleanup failed for {conversation_id[:12]} (non-fatal): {e}"
        )


async def get_silence_gaps(
    user: User,
    conversation_id: str,
    speech_threshold: float = 0.5,
    min_gap_seconds: float = 900.0,
):
    """Silence gaps (candidate split points) from cached chunk VAD scores."""
    conversation, error = await _load_operable_conversation(user, conversation_id)
    if error:
        return error

    policy = await privacy.require_record(conversation)
    chunks = await _chunk_timeline(conversation_id)
    if not chunks:
        return JSONResponse(
            status_code=409, content={"error": "Conversation has no audio chunks"}
        )

    # Only "needs analysis" when *no* chunk is scored. detect_silence_gaps
    # already treats individual unscored chunks as speech (so it never suggests a
    # split through unscored audio), which keeps a partially-scored conversation
    # — e.g. one with leftover unscored chunks from the reconnect-duplicate bug —
    # usable for splitting instead of falsely blocked.
    needs_analysis = all((c.get("vad") or {}).get("max_score") is None for c in chunks)
    duration = float(chunks[-1]["end_time"])

    gaps = (
        []
        if needs_analysis
        else detect_silence_gaps(
            chunks,
            speech_threshold=speech_threshold,
            min_gap_seconds=min_gap_seconds,
        )
    )

    await privacy.assert_current(conversation.user_id, policy)
    return {
        "analyzed": not needs_analysis,
        "needs_analysis": needs_analysis,
        "duration_seconds": round(duration, 2),
        "chunk_duration_seconds": float(chunks[0].get("duration") or 10.0),
        "speech_threshold": speech_threshold,
        "min_gap_seconds": min_gap_seconds,
        "gaps": gaps,
    }


async def get_speech_regions(
    user: User, conversation_id: str, speakers: Optional[List[str]] = None
):
    """Merged speech intervals for speech-skip playback.

    Without ``speakers``: served from the cached ``vad_analysis.speech_regions``
    when present; otherwise derived from the chunk-level frame scores (no audio
    decode) and cached back onto the conversation.

    With ``speakers``: the raw frame-level speech intervals are intersected
    with the selected speakers' transcript segments *before* merging, so the
    regions cover only time where the VAD heard voice while one of those
    speakers was tagged. Speaker labels match ``identified_as`` (recognized
    name) falling back to the raw ``speaker`` label, same as the audit
    listing. Filtered results are never cached (they depend on the selection).
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        return JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    if not user.is_superuser and conversation.user_id != str(user.user_id):
        return JSONResponse(status_code=403, content={"error": "Access forbidden"})
    policy = await privacy.require_record(conversation)
    if conversation.audio_archived or not conversation.audio_chunks_count:
        return JSONResponse(
            status_code=409, content={"error": "Conversation has no audio"}
        )

    wanted = {s for s in (speakers or []) if s}
    duration = conversation.audio_total_duration or 0.0
    va = conversation.vad_analysis

    # Trust the cached summary only if it still describes the current chunk set.
    # vad_analysis is derived from the chunks' frame scores; if the chunks changed
    # in place (e.g. the reconnect-duplicate dedup) the cache's implied duration
    # (frame_count * frame_hop) no longer matches audio_total_duration — fall back
    # to deriving from current chunks rather than serving stale regions.
    va_fresh = va is not None and audio_cache_duration_matches(
        va.frame_count * va.frame_hop_ms / 1000.0, duration
    )

    if not wanted and va_fresh and va.speech_regions is not None:
        regions = va.speech_regions
    else:
        # Derive regions from the chunks that have scores; chunks missing them
        # contribute no speech intervals (treated as non-speech for speech-only
        # playback — full-audio mode still plays everything). Only report
        # needs_analysis when *no* chunk is scored, i.e. the conversation was
        # genuinely never analyzed. A partially-scored conversation — e.g. one
        # damaged by the reconnect-duplicate bug, where some overlapping chunks
        # never got scored — still gets a usable speech preview instead of
        # falsely reading as "needs analysis" while its cached summary says it is
        # analyzed (which is the contradiction the audit listing would show).
        raw_intervals: List[List[float]] = []
        scored_any = False
        last_end = 0.0
        for chunk in await _chunk_timeline(conversation_id):
            vad = chunk.get("vad")
            last_end = max(last_end, float(chunk["end_time"]))
            if not vad or vad.get("scores") is None:
                continue
            scored_any = True
            raw_intervals.extend(
                frame_speech_intervals(
                    vad["scores"],
                    float(vad["frame_hop_ms"]) / 1000.0,
                    float(chunk["start_time"]),
                )
            )

        if not scored_any:
            await privacy.assert_current(conversation.user_id, policy)
            return {
                "analyzed": False,
                "needs_analysis": True,
                "duration_seconds": round(duration, 2),
                "speech_seconds": 0,
                "regions": [],
            }

        duration = duration or last_end
        if wanted:
            version = _active_transcript_version(conversation)
            tagged = [
                [segment.start, segment.end]
                for segment in (version.segments if version else [])
                if segment.segment_type == Conversation.SegmentType.SPEECH
                and (segment.identified_as or segment.speaker) in wanted
            ]
            raw_intervals = intersect_intervals(raw_intervals, tagged)
        regions = merge_speech_regions(raw_intervals, duration)
        if not wanted and va is not None:
            va.speech_regions = regions
            await privacy.assert_current(conversation.user_id, policy)
            await conversation.save()

    speech_seconds = sum(end - start for start, end in regions)
    await privacy.assert_current(conversation.user_id, policy)
    return {
        "analyzed": True,
        "needs_analysis": False,
        "duration_seconds": round(duration, 2),
        "speech_seconds": round(speech_seconds, 2),
        "speakers": sorted(wanted),
        "regions": [{"start": start, "end": end} for start, end in regions],
    }


async def get_segments(user: User, conversation_id: str):
    """Active-version transcript segments for the speaker-triage panel.

    Returns every segment (the frontend filters to speech / needs-review) with
    the speaker-recognition fields the panel needs — including ``confidence``,
    which the listing projection strips. ``index`` is the position in the active
    version's segment list (so it matches the ``segment_index`` an annotation
    stores), and ``segment_start_time`` is sent so the frontend records the
    drift-stable time key on each annotation it creates.
    """
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        return JSONResponse(
            status_code=404, content={"error": "Conversation not found"}
        )
    if not user.is_superuser and conversation.user_id != str(user.user_id):
        return JSONResponse(status_code=403, content={"error": "Access forbidden"})
    policy = await privacy.require_record(conversation)

    version = _active_transcript_version(conversation)
    segments = []
    for index, seg in enumerate(version.segments if version else []):
        segments.append(
            {
                "index": index,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "segment_start_time": seg.start,
                "text": seg.text,
                "speaker": seg.speaker,
                "identified_as": seg.identified_as,
                "confidence": seg.confidence,
                "segment_type": seg.segment_type,
            }
        )

    await privacy.assert_current(conversation.user_id, policy)
    return {
        "conversation_id": conversation_id,
        "duration_seconds": round(conversation.audio_total_duration or 0.0, 2),
        "audio_available": bool(
            conversation.audio_chunks_count and not conversation.audio_archived
        ),
        "segments": segments,
    }


async def identify_segment_clip(
    user: User, conversation_id: str, start: float, end: float
):
    """Live speaker suggestion for one segment.

    Reconstructs the segment's audio and asks the speaker service for its
    closest enrolled match. The service returns the best name + cosine even
    below the match threshold, which is the only real signal available on an
    unknown segment (stored confidence is 0.0 there).
    """
    _conversation, error = await _load_operable_conversation(user, conversation_id)
    if error:
        return error
    if end <= start:
        return JSONResponse(
            status_code=400, content={"error": "end must be greater than start"}
        )

    policy = await privacy.require_record(_conversation)
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )

    try:
        wav_bytes = await reconstruct_audio_segment(conversation_id, start, end)
    except Exception as e:
        logger.warning(f"Segment audio reconstruction failed for identify: {e}")
        return JSONResponse(
            status_code=409, content={"error": "Could not reconstruct segment audio"}
        )

    # Pass a near-zero threshold so the service always returns the *closest*
    # enrolled speaker + its true cosine, even for a below-threshold (unknown)
    # segment — that name+score is the whole point of a triage suggestion. The
    # caller surfaces the cosine (color-coded) so a weak match reads as weak;
    # `found` reflects whether it would clear the real operating threshold.
    await privacy.assert_current(_conversation.user_id, policy)
    suggest = await speaker_client.identify_segment(
        wav_bytes, user_id=str(user.user_id), similarity_threshold=0.0
    )
    confidence = suggest.get("confidence")
    threshold = get_diarization_settings().get("similarity_threshold", 0.5)
    await privacy.assert_current(_conversation.user_id, policy)
    return {
        "found": confidence is not None and confidence >= threshold,
        "speaker_id": suggest.get("speaker_id"),
        "speaker_name": suggest.get("speaker_name"),
        "confidence": confidence,
        "threshold": threshold,
        "status": suggest.get("status"),
    }


def _speaker_label_reviews_collection():
    return Conversation.get_pymongo_collection().database["speaker_label_reviews"]


async def next_speaker_label_reviews(user: User, batch_size: int = 5):
    """Serve a diverse active-learning batch plus calibration controls."""
    visibility = privacy.ConversationPrivacyFilter()
    batch_size = max(1, min(batch_size, 20))
    scope = {} if user.is_superuser else {"user_id": str(user.user_id)}
    review_rows = (
        await _speaker_label_reviews_collection()
        .find(
            scope,
            {"review_key": 1, "conversation_id": 1, "claimed_speaker": 1},
        )
        .to_list()
    )
    review_rows = await visibility.filter(review_rows)
    reviewed_keys = {row["review_key"] for row in review_rows}
    conversation_review_counts: Dict[str, int] = {}
    speaker_review_counts: Dict[str, int] = {}
    for row in review_rows:
        conversation_id = row.get("conversation_id")
        speaker = row.get("claimed_speaker")
        if conversation_id:
            conversation_review_counts[conversation_id] = (
                conversation_review_counts.get(conversation_id, 0) + 1
            )
        if speaker:
            speaker_review_counts[speaker] = speaker_review_counts.get(speaker, 0) + 1

    pending_annotation_keys = {
        _speaker_review_key(row["conversation_id"], row["segment_start_time"])
        async for row in Annotation.get_pymongo_collection().find(
            {
                **scope,
                "annotation_type": AnnotationType.DIARIZATION.value,
                "segment_start_time": {"$ne": None},
            },
            {"conversation_id": 1, "segment_start_time": 1},
        )
        if row.get("conversation_id") and row.get("segment_start_time") is not None
    }
    excluded_keys = reviewed_keys | pending_annotation_keys

    query = {
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    if not user.is_superuser:
        query["user_id"] = str(user.user_id)
    projection = {
        "conversation_id": 1,
        "title": 1,
        "created_at": 1,
        "active_transcript_version": 1,
        "transcript_versions.version_id": 1,
        "transcript_versions.segments.start": 1,
        "transcript_versions.segments.end": 1,
        "transcript_versions.segments.text": 1,
        "transcript_versions.segments.speaker": 1,
        "transcript_versions.segments.identified_as": 1,
        "transcript_versions.segments.confidence": 1,
        "transcript_versions.segments.segment_type": 1,
    }

    candidates = []
    conversations_scanned = 0
    cursor = (
        Conversation.get_pymongo_collection()
        .find(query, projection)
        .sort("created_at", -1)
        .limit(MAX_SCAN)
    )
    docs = await visibility.filter(await cursor.to_list(length=MAX_SCAN))
    for doc in docs:
        conversations_scanned += 1
        candidates.extend(_speaker_review_candidates(doc, excluded_keys))
    threshold = float(get_diarization_settings().get("similarity_threshold", 0.5))
    batch = _select_speaker_review_batch(
        candidates,
        batch_size,
        threshold,
        conversation_review_counts,
        speaker_review_counts,
    )
    await visibility.assert_current()
    return {
        "batch": batch,
        "reviewed_total": len(reviewed_keys),
        "conversations_scanned": conversations_scanned,
        "candidate_claims": len(candidates),
        "threshold": threshold,
    }


async def speaker_label_review_metrics(user: User):
    """Human-measured assignment precision, globally and per claimed identity."""
    scope = {} if user.is_superuser else {"user_id": str(user.user_id)}
    rows = await _speaker_label_reviews_collection().find(scope, {"_id": 0}).to_list()
    visibility = privacy.ConversationPrivacyFilter()
    rows = await visibility.filter(rows)
    evaluable_verdicts = {"correct", "relabel", "unknown", "background"}

    def summarize(items: List[dict]) -> dict:
        evaluable = [row for row in items if row.get("verdict") in evaluable_verdicts]
        correct = sum(row.get("verdict") == "correct" for row in evaluable)
        errors: Dict[str, int] = {}
        for row in evaluable:
            if row.get("verdict") != "correct":
                label = row.get("corrected_speaker") or row.get("verdict")
                errors[label] = errors.get(label, 0) + 1
        return {
            "reviewed": len(items),
            "evaluable": len(evaluable),
            "correct": correct,
            "precision": round(correct / len(evaluable), 4) if evaluable else None,
            "excluded": len(items) - len(evaluable),
            "errors": errors,
        }

    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("claimed_speaker") or "(unknown)", []).append(row)
    speakers = [
        {"speaker": speaker, **summarize(items)} for speaker, items in grouped.items()
    ]
    speakers.sort(key=lambda item: (-item["evaluable"], item["speaker"]))
    await visibility.assert_current()
    return {
        "overall": summarize(rows),
        "boundary": summarize(
            [row for row in rows if row.get("selection_lane") == "boundary"]
        ),
        "control": summarize(
            [row for row in rows if row.get("selection_lane") == "control"]
        ),
        "speakers": speakers,
    }


async def decide_speaker_label_reviews(user: User, decisions: List[dict]):
    """Persist verdicts and create ordinary pending diarization corrections."""
    reviews = _speaker_label_reviews_collection()
    recorded = corrected = 0
    errors = []
    now = datetime.now(timezone.utc)

    for decision in decisions:
        conversation_id = decision.get("conversation_id")
        segment_index = decision.get("segment_index")
        segment_start = decision.get("segment_start_time")
        verdict = decision.get("verdict")
        corrected_speaker = decision.get("corrected_speaker")
        if (
            not conversation_id
            or segment_index is None
            or segment_start is None
            or verdict
            not in {"correct", "relabel", "unknown", "background", "mixed", "bad_audio"}
        ):
            errors.append({"decision": decision, "error": "invalid decision"})
            continue

        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if not conversation or (
            not user.is_superuser and conversation.user_id != str(user.user_id)
        ):
            errors.append({"decision": decision, "error": "conversation not found"})
            continue
        version = _active_transcript_version(conversation)
        if not version or segment_index >= len(version.segments):
            errors.append({"decision": decision, "error": "segment no longer exists"})
            continue
        segment = version.segments[segment_index]
        if abs(float(segment.start) - float(segment_start)) > 0.01:
            errors.append(
                {"decision": decision, "error": "segment changed since review"}
            )
            continue

        target = corrected_speaker
        if verdict == "unknown":
            target = "Unknown Speaker"
        elif verdict == "background":
            target = "Background Speech"
        elif verdict == "relabel" and not target:
            errors.append({"decision": decision, "error": "corrected speaker required"})
            continue
        elif verdict not in {"relabel", "unknown", "background"}:
            target = None

        annotation_id = None
        if target and target != (segment.identified_as or segment.speaker):
            annotation = await Annotation.find_one(
                Annotation.user_id == str(user.user_id),
                Annotation.annotation_type == AnnotationType.DIARIZATION,
                Annotation.conversation_id == conversation_id,
                Annotation.segment_start_time == float(segment.start),
                Annotation.processed == False,  # noqa: E712 - Beanie query expression
            )
            if annotation is None:
                annotation = Annotation(
                    annotation_type=AnnotationType.DIARIZATION,
                    user_id=str(user.user_id),
                    conversation_id=conversation_id,
                    segment_index=segment_index,
                    original_speaker=segment.identified_as or segment.speaker or "",
                    corrected_speaker=target,
                    segment_start_time=float(segment.start),
                    status="accepted",
                    processed=False,
                )
            else:
                annotation.segment_index = segment_index
                annotation.corrected_speaker = target
                annotation.updated_at = now
            await annotation.save()
            annotation_id = annotation.id
            corrected += 1

        review_key = _speaker_review_key(conversation_id, segment.start)
        await reviews.update_one(
            {"user_id": str(user.user_id), "review_key": review_key},
            {
                "$set": {
                    "user_id": str(user.user_id),
                    "review_key": review_key,
                    "conversation_id": conversation_id,
                    "segment_index": segment_index,
                    "segment_start_time": float(segment.start),
                    "segment_end_time": float(segment.end),
                    "claimed_speaker": segment.identified_as or segment.speaker,
                    "model_confidence": segment.confidence,
                    "selection_lane": decision.get("selection_lane"),
                    "verdict": verdict,
                    "corrected_speaker": target,
                    "annotation_id": annotation_id,
                    "reviewed_at": now,
                }
            },
            upsert=True,
        )
        recorded += 1

    return {
        "status": "ok" if not errors else "partial",
        "recorded": recorded,
        "corrections_pending": corrected,
        "errors": errors,
    }


async def get_triage_pending(user: User):
    """Count of unapplied speaker-triage decisions (pending diarization
    annotations) and how many conversations they span — drives the toolbar's
    'Apply all' control."""
    base = {
        "annotation_type": AnnotationType.DIARIZATION,
        "processed": False,
    }
    if not user.is_superuser:
        base["user_id"] = user.user_id
    pending = await Annotation.find(base).to_list()
    conversation_ids = {a.conversation_id for a in pending if a.conversation_id}
    return {
        "pending_count": len(pending),
        "conversation_count": len(conversation_ids),
    }


async def apply_triage(user: User):
    """Bulk-apply all pending speaker-triage decisions across every conversation.

    Each triage decision was persisted as a diarization annotation. This applies them
    (new transcript version + chained memory reprocess) in one pass over every
    conversation the user triaged. It does NOT enroll voiceprints — that's a deliberate
    action reserved for the finetuning / Enrollment pages. Noise decisions ride along:
    apply reclassifies them to non-speech.
    """
    # Lazy import: circular dependency — the routers.modules package __init__
    # imports data_audit_routes, which imports back into this controller, so a
    # top-level import here would re-enter this module mid-import.
    from backend.routers.modules.annotation_routes import apply_diarization_annotations

    base = {
        "annotation_type": AnnotationType.DIARIZATION,
        "processed": False,
    }
    if not user.is_superuser:
        base["user_id"] = user.user_id
    pending = await Annotation.find(base).to_list()

    conversation_ids = sorted({a.conversation_id for a in pending if a.conversation_id})
    if not conversation_ids:
        return {"applied_count": 0, "conversation_count": 0, "enrolled": None}

    applied_count = 0
    apply_errors: List[str] = []
    for cid in conversation_ids:
        try:
            await apply_diarization_annotations(cid, current_user=user)
            applied_count += 1
        except Exception as e:
            logger.warning(f"Triage apply failed for {cid[:8]}: {e}")
            apply_errors.append(cid)

    # NOTE: triage only ANNOTATES (fixes the conversation transcript + memory). It does
    # NOT enroll/train voiceprints — voiceprint training is a deliberate action done only
    # from the finetuning page and the speaker-recognition Enrollment page, so noisy
    # conversational corrections (e.g. a one-word "yeah") never drift someone's voiceprint.
    return {
        "applied_count": applied_count,
        "conversation_count": len(conversation_ids),
        "apply_errors": apply_errors,
        "enrolled": None,
    }


def _snap_split_points(
    split_points: List[float], chunks: List[dict]
) -> Tuple[Optional[List[int]], Optional[str]]:
    """Snap time points to chunk boundaries.

    Returns (sorted chunk indices that begin each new child, None) or
    (None, error message). Each child must contain at least one chunk, so
    snapped indices must be unique and within (0, n_chunks).
    """
    n_chunks = len(chunks)
    duration = float(chunks[-1]["end_time"])

    indices = []
    for point in sorted(set(split_points)):
        if not (0 < point < duration):
            return None, f"Split point {point}s is outside (0, {duration:.0f}s)"
        # The chunk containing the point begins the next child.
        snapped = None
        for chunk in chunks:
            if chunk["start_time"] <= point < chunk["end_time"]:
                snapped = int(chunk["chunk_index"])
                break
        if snapped is None:
            snapped = n_chunks - 1
        if snapped == 0:
            return None, f"Split point {point}s leaves an empty first part"
        indices.append(snapped)

    if len(set(indices)) != len(indices):
        return None, "Split points collapse onto the same chunk boundary"
    return indices, None


def _asr_source_version(
    conversation: Conversation,
) -> Optional["Conversation.TranscriptVersion"]:
    """Walk a conversation's active version back to its underlying ASR transcript.

    Same rule the memory rebuild uses: a version tagged
    ``reprocessing_type == "speaker_diarization"`` is a derivative, so follow its
    ``source_version_id``. A conversation that was never diarized returns its
    active version, which *is* the ASR layer.
    """
    versions = {
        version.version_id: version
        for version in (conversation.transcript_versions or [])
    }
    current = versions.get(conversation.active_transcript_version or "")
    seen: set[str] = set()
    while current is not None and current.version_id not in seen:
        seen.add(current.version_id)
        metadata = current.metadata or {}
        if metadata.get("reprocessing_type") != "speaker_diarization":
            return current
        source = versions.get(metadata.get("source_version_id") or "")
        if source is None:
            return current
        current = source
    return current


def _avoid_speech_segments(
    split_points: List[float], segments: List["Conversation.SpeakerSegment"]
) -> Tuple[List[float], List[dict]]:
    """Move each cut out of any speech segment it would land inside.

    Slicing sends a segment wholly to the side holding its midpoint and keeps
    only the words starting on that side, so a cut through a segment drops the
    words beyond it from both children. ``transcript_slicing`` states this as an
    assumption -- "split points sit inside long silence gaps, so segments never
    straddle them" -- and nothing enforced it; two conversations here lost 4 and
    5 words that way.

    Only ``speech`` blocks a cut. An ``event`` or ``note`` segment ([laughter],
    a tag) carries no words to lose and may be cut through.
    """
    speech = sorted(
        (
            segment
            for segment in segments
            if segment.segment_type == Conversation.SegmentType.SPEECH
            and segment.end > segment.start
        ),
        key=lambda segment: segment.start,
    )
    moved: List[dict] = []
    adjusted: List[float] = []
    for point in split_points:
        straddled = next(
            (segment for segment in speech if segment.start < point < segment.end),
            None,
        )
        if straddled is None:
            adjusted.append(point)
            continue
        # Nearest edge, so the cut moves as little as possible.
        target = (
            straddled.start
            if point - straddled.start <= straddled.end - point
            else straddled.end
        )
        moved.append(
            {
                "requested": round(point, 3),
                "moved_to": round(target, 3),
                "segment": [round(straddled.start, 3), round(straddled.end, 3)],
            }
        )
        adjusted.append(target)
    return adjusted, moved


def _slice_versions_onto_child(
    child: Conversation,
    parent: Conversation,
    t0: float,
    t1: float,
) -> Optional[str]:
    """Give the child a time slice of *every* one of the parent's versions.

    Slicing only the active version strands the rest on a soft-deleted parent
    nobody reads, which breaks the one thing that needs them: rebuild walks a
    child's own versions back to the underlying ASR transcript by following
    ``reprocessing_type == "speaker_diarization"``. A lone slice is not tagged
    that way, so the walk stops on it and a speaker-labelled slice gets treated
    as clean ASR -- diarization layered on diarized text.

    Version ids are minted fresh per child, so ``source_version_id`` is remapped
    to the child's copy of that source; a link whose source sliced to nothing
    keeps pointing at the parent's id, with ``source_conversation_id`` naming
    where to find it. Returns the child's active version id.
    """
    id_map: dict[str, str] = {}
    active_version_id: Optional[str] = None
    for version in parent.transcript_versions or []:
        segments = slice_segments(version.segments or [], t0, t1)
        words = slice_words(version.words or [], t0, t1)
        if not segments and not words:
            continue
        new_id = str(uuid.uuid4())
        metadata = dict(version.metadata or {})
        source_id = metadata.get("source_version_id")
        metadata.update(
            {
                "derived": "split",
                "source_conversation_id": parent.conversation_id,
                "source_version_id": version.version_id,
                "time_range": [t0, t1],
            }
        )
        if source_id:
            # Keep the chain inside the child where both links survived the cut.
            metadata["source_version_id"] = id_map.get(source_id, source_id)
            if source_id in id_map:
                metadata["source_conversation_id"] = child.conversation_id
            metadata["origin_version_id"] = version.version_id
        child.add_transcript_version(
            version_id=new_id,
            transcript=build_transcript_text(segments),
            words=words,
            segments=segments,
            provider=version.provider,
            model=version.model,
            metadata=metadata,
            set_as_active=False,
        )
        id_map[version.version_id] = new_id
        if version.version_id == parent.active_transcript_version:
            active_version_id = new_id

    if active_version_id is None:
        # The parent's active version sliced to nothing here; the newest slice
        # that did survive is the best available answer for this child.
        active_version_id = next(iter(reversed(id_map.values())), None)
    if active_version_id:
        child.set_active_transcript_version(active_version_id)
    return active_version_id


async def split_conversation(
    user: User, conversation_id: str, split_points: List[float]
):
    """Partition semantic range claims without rewriting capture chunks."""
    conversation, error = await _load_operable_conversation(user, conversation_id)
    if error:
        return error

    children: List[Conversation] = []
    try:
        duration = float(conversation.audio_total_duration or 0.0)
        if duration <= 0 or not conversation.audio_ranges:
            return JSONResponse(
                status_code=409, content={"error": "Conversation has no audio chunks"}
            )

        # Move cuts off speech before snapping to chunks, so a boundary never
        # lands mid-utterance and drops its far-side words.
        active_version = _active_transcript_version(conversation)
        requested_points = sorted(set(split_points))
        adjusted_points, moved_cuts = _avoid_speech_segments(
            requested_points,
            list(active_version.segments or []) if active_version else [],
        )
        if moved_cuts:
            logger.info(
                "Split %s: moved %d cut(s) out of speech segments: %s",
                conversation_id[:12],
                len(moved_cuts),
                moved_cuts,
            )

        if any(point <= 0 or point >= duration for point in adjusted_points):
            return JSONResponse(
                status_code=422,
                content={"error": f"Split points must lie inside (0, {duration})"},
            )
        if len(set(adjusted_points)) != len(adjusted_points):
            return JSONResponse(
                status_code=422, content={"error": "Split points must be unique"}
            )

        claim_groups = await partition_audio_ranges(
            conversation.audio_ranges, adjusted_points
        )
        edges = [0.0, *adjusted_points, duration]
        now = datetime.now(timezone.utc)

        child_specs: List[dict] = []
        for t0, t1, ranges in zip(edges[:-1], edges[1:], claim_groups):
            child = create_conversation(
                user_id=conversation.user_id,
                client_id=conversation.client_id,
                data_purpose=conversation.data_purpose,
                memory_excluded=conversation.memory_excluded,
                memory_exclusion_reason=conversation.memory_exclusion_reason,
                origin=conversation.origin,
                audio_ranges=ranges,
            )
            child.derived_from = Conversation.DerivedFrom(
                operation="split",
                source_conversation_ids=[conversation_id],
                time_range=[t0, t1],
                performed_at=now,
                performed_by=str(user.user_id),
            )
            child.external_source_type = conversation.external_source_type
            child.external_source_id = conversation.external_source_id
            child.end_reason = conversation.end_reason
            await apply_audio_ranges(child, ranges, save=False)

            version_id = _slice_versions_onto_child(child, conversation, t0, t1)

            part = len(children) + 1
            total = len(edges) - 1
            base_title = conversation.title or conversation_id[:8]
            child.title = f"Part {part}/{total} — {base_title}"

            await child.insert()
            children.append(child)
            child_specs.append({"t0": t0, "t1": t1, "version_id": version_id})

        # Re-validate just before mutating chunks (no lock; admin tool).
        current = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if current is None or current.deleted or current.derived_into:
            for child in children:
                await child.delete()
            return JSONResponse(
                status_code=409,
                content={"error": "Conversation changed during split; aborted"},
            )

        # Retire only the old semantic claim. Its ranges remain as lineage/evidence.
        conversation.deleted = True
        conversation.deletion_reason = "split"
        conversation.deleted_at = now
        conversation.derived_into = [c.conversation_id for c in children]
        await conversation.save()

        await _delete_source_memories(conversation.user_id, conversation_id)

        results = []
        for child, spec in zip(children, child_specs):
            jobs = None
            if spec["version_id"]:
                jobs = start_post_conversation_jobs(
                    child.conversation_id,
                    conversation.user_id,
                    transcript_version_id=spec["version_id"],
                    client_id=conversation.client_id,
                    end_reason="split",
                    skip_speaker_recognition=True,
                    memory_space_id=child.memory_space_id,
                )
            results.append(
                {
                    "conversation_id": child.conversation_id,
                    "start_seconds": spec["t0"],
                    "end_seconds": spec["t1"],
                    "duration_seconds": child.audio_total_duration,
                    "chunk_count": child.audio_chunks_count,
                    "has_transcript": spec["version_id"] is not None,
                    "jobs": jobs,
                }
            )

        logger.info(
            f"Split conversation {conversation_id[:12]} into {len(children)} parts"
        )
        return {
            "parent_conversation_id": conversation_id,
            "children": results,
            "cuts_moved_off_speech": moved_cuts,
        }

    except Exception as e:
        for child in children:
            try:
                await child.delete()
            except Exception as cleanup_error:
                logger.warning(
                    "Could not remove partial split child %s: %s",
                    child.conversation_id,
                    cleanup_error,
                )
        logger.exception(f"Error splitting conversation {conversation_id}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error splitting conversation"}
        )


async def merge_conversations(user: User, conversation_ids: List[str]):
    """Merge adjacent conversations into a new conversation.

    Creates a fresh conversation (symmetric with split: sources stay intact
    and individually recoverable), reassigns chunks with cumulative time
    offsets, concatenates transcripts with a seam note where the wall-clock
    gap between recordings is elided, soft-deletes the sources, and enqueues
    memory + title jobs. Crash-safe ordering: the merged conversation is
    created before any source is mutated.
    """
    if len(set(conversation_ids)) != len(conversation_ids):
        return JSONResponse(
            status_code=422, content={"error": "Duplicate conversation ids"}
        )

    try:
        sources: List[Conversation] = []
        for cid in conversation_ids:
            conversation, error = await _load_operable_conversation(user, cid)
            if error:
                return error
            sources.append(conversation)

        if (
            len({recording_purpose.is_personal_recording(source) for source in sources})
            > 1
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "error": "Training-only and personal recordings cannot be merged"
                },
            )

        client_ids = {s.client_id for s in sources}
        if len(client_ids) != 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations belong to different devices"},
            )
        user_ids = {s.user_id for s in sources}
        if len(user_ids) != 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations belong to different users"},
            )

        sources.sort(key=lambda s: s.created_at)
        first, last = sources[0], sources[-1]

        # Adjacency: no other live conversation of this device may sit between
        # the earliest and latest selected (server-authoritative check).
        conv_collection = Conversation.get_pymongo_collection()
        between = await conv_collection.count_documents(
            {
                "client_id": first.client_id,
                "deleted": {"$ne": True},
                "conversation_id": {"$nin": conversation_ids},
                "created_at": {"$gt": first.created_at, "$lt": last.created_at},
            }
        )
        if between:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "Conversations are not adjacent: "
                    f"{between} other conversation(s) lie between them"
                },
            )

        resolved_by_source = {
            source.conversation_id: await resolve_audio_ranges(source.audio_ranges)
            for source in sources
        }
        sample_rates = {
            item.chunk.sample_rate
            for claimed in resolved_by_source.values()
            for item in claimed
        }
        channel_counts = {
            item.chunk.channels
            for claimed in resolved_by_source.values()
            for item in claimed
        }
        if len(sample_rates) > 1 or len(channel_counts) > 1:
            return JSONResponse(
                status_code=422,
                content={"error": "Conversations have mixed audio formats"},
            )

        stats = {
            source.conversation_id: {
                "duration": sum(
                    item.duration_seconds
                    for item in resolved_by_source[source.conversation_id]
                ),
                "count": len(
                    {
                        str(item.chunk.id)
                        for item in resolved_by_source[source.conversation_id]
                    }
                ),
            }
            for source in sources
        }
        missing = [
            source.conversation_id
            for source in sources
            if not resolved_by_source[source.conversation_id]
        ]
        if missing:
            return JSONResponse(
                status_code=409,
                content={"error": f"No audio chunks for {missing[0]}"},
            )

        now = datetime.now(timezone.utc)
        # Fencing survives a merge, and the merged span is only memory-eligible if
        # every source was. One promoted part means the segmentation agent judged that
        # stretch conversational, so the merge follows it rather than the fence.
        fenced = [s for s in sources if s.memory_excluded]
        merged = create_conversation(
            user_id=first.user_id,
            client_id=first.client_id,
            data_purpose=(
                first.data_purpose
                if len(fenced) == len(sources)
                else next(
                    (s.data_purpose for s in sources if not s.memory_excluded), None
                )
            ),
            memory_excluded=len(fenced) == len(sources),
            memory_exclusion_reason=(
                first.memory_exclusion_reason if len(fenced) == len(sources) else None
            ),
            audio_ranges=merge_audio_ranges(source.audio_ranges for source in sources),
        )
        # Where the audio came from survives a merge too. The timeline selects its
        # audio evidence by external_source_type and reads the capture direction out
        # of external_source_id, so a merged recording that drops them disappears from
        # the timeline and loses its media/speech role.
        merged.external_source_type = first.external_source_type
        merged.external_source_id = first.external_source_id
        merged.derived_from = Conversation.DerivedFrom(
            operation="merge",
            source_conversation_ids=[s.conversation_id for s in sources],
            performed_at=now,
            performed_by=str(user.user_id),
        )
        merged.end_reason = Conversation.EndReason.MERGE
        await apply_audio_ranges(merged, merged.audio_ranges, save=False)

        def _concatenate(pick) -> tuple:
            """Concatenate one layer across the sources, with cumulative offsets.

            Called once per layer so the merged conversation keeps a real
            ``asr -> speaker`` chain instead of collapsing to a single version
            whose sources are stranded on the soft-deleted originals.
            """
            merged_segments: List[Conversation.SpeakerSegment] = []
            merged_words: List[Conversation.Word] = []
            provider = None
            model = None
            offset = 0.0
            prev = None
            for source in sources:
                version = pick(source)
                if prev is not None:
                    gap_seconds = max(
                        0.0,
                        (source.created_at - prev.created_at).total_seconds()
                        - float(stats[prev.conversation_id]["duration"]),
                    )
                    merged_segments.append(
                        Conversation.SpeakerSegment(
                            start=round(offset, 3),
                            end=round(offset, 3),
                            text=(
                                f"[merged: {max(1, round(gap_seconds / 60))} min gap "
                                "between recordings elided]"
                            ),
                            speaker="system",
                            segment_type=Conversation.SegmentType.NOTE,
                        )
                    )
                if version:
                    merged_segments.extend(
                        shift_segments(version.segments or [], offset)
                    )
                    merged_words.extend(shift_words(version.words or [], offset))
                    provider = provider or version.provider
                    model = model or version.model
                offset += float(stats[source.conversation_id]["duration"])
                prev = source
            return merged_segments, merged_words, provider, model

        def _has_content(segments, words) -> bool:
            return any(
                seg.segment_type == Conversation.SegmentType.SPEECH for seg in segments
            ) or bool(words)

        source_ids = [s.conversation_id for s in sources]
        # An ASR layer distinct from the active one only exists if some source
        # was diarized; without that the two concatenations are the same text.
        diarized = any(
            _asr_source_version(source) is not _active_transcript_version(source)
            for source in sources
        )
        asr_version_id = None
        if diarized:
            segments, words, provider, model = _concatenate(_asr_source_version)
            if _has_content(segments, words):
                asr_version_id = str(uuid.uuid4())
                merged.add_transcript_version(
                    version_id=asr_version_id,
                    transcript=build_transcript_text(segments),
                    words=words,
                    segments=segments,
                    provider=provider,
                    model=model,
                    metadata={
                        "derived": "merge",
                        "layer": "asr",
                        "source_conversation_ids": source_ids,
                    },
                    set_as_active=False,
                )

        merged_segments, merged_words, provider, model = _concatenate(
            _active_transcript_version
        )
        version_id = None
        if _has_content(merged_segments, merged_words):
            version_id = str(uuid.uuid4())
            metadata = {
                "derived": "merge",
                "source_conversation_ids": source_ids,
            }
            if asr_version_id:
                # Tagged so the rebuild's walk-back finds the merged ASR layer
                # instead of stopping on speaker-labelled text.
                metadata["reprocessing_type"] = "speaker_diarization"
                metadata["source_version_id"] = asr_version_id
            merged.add_transcript_version(
                version_id=version_id,
                transcript=build_transcript_text(merged_segments),
                words=merged_words,
                segments=merged_segments,
                provider=provider,
                model=model,
                metadata=metadata,
                set_as_active=True,
            )

        merged.title = first.title or f"Merged conversation ({len(sources)} parts)"
        await merged.insert()

        # Soft-delete source semantic claims with lineage. Capture chunks are shared
        # immutable evidence and remain untouched.
        for source in sources:
            source.deleted = True
            source.deletion_reason = "merged"
            source.deleted_at = now
            source.derived_into = [merged.conversation_id]
            await source.save()
            await _delete_source_memories(source.user_id, source.conversation_id)

        jobs = None
        if version_id:
            jobs = start_post_conversation_jobs(
                merged.conversation_id,
                merged.user_id,
                transcript_version_id=version_id,
                client_id=merged.client_id,
                end_reason="merge",
                skip_speaker_recognition=True,
                memory_space_id=merged.memory_space_id,
            )

        logger.info(
            f"Merged {len(sources)} conversations into {merged.conversation_id[:12]}"
        )
        return {
            "merged_conversation_id": merged.conversation_id,
            "source_conversation_ids": [s.conversation_id for s in sources],
            "duration_seconds": merged.audio_total_duration,
            "chunk_count": merged.audio_chunks_count,
            "has_transcript": version_id is not None,
            "jobs": jobs,
        }

    except Exception as e:
        logger.exception(f"Error merging conversations {conversation_ids}: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error merging conversations"}
        )


# ---------------------------------------------------------------------------
# Annotation dataset import / export
# ---------------------------------------------------------------------------


async def import_annotation_dataset(user: User, archive_bytes: bytes):
    """Import an export-compatible ZIP as isolated, editor-ready conversations."""
    try:
        dataset = parse_annotation_dataset(archive_bytes)
    except AnnotationDatasetError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    client_id = synthetic_client_id(user, "annotation-import")
    results = []
    for clip in dataset.clips:
        external_source_id = f"{dataset.dataset_id}:{clip.clip_id}"
        existing = await Conversation.find_one(
            Conversation.user_id == user.user_id,
            Conversation.external_source_type == "annotation_dataset",
            Conversation.external_source_id == external_source_id,
        )
        if existing:
            results.append(
                {
                    "clip_id": clip.clip_id,
                    "status": "skipped",
                    "reason": "already_imported",
                    "conversation_id": existing.conversation_id,
                }
            )
            continue

        conversation = None
        try:
            audio_data, sample_rate, sample_width, channels, duration = (
                await validate_and_prepare_audio(
                    audio_data=clip.audio_bytes,
                    expected_sample_rate=16000,
                    convert_to_mono=True,
                    auto_resample=True,
                )
            )
            segments = [
                Conversation.SpeakerSegment(**segment) for segment in clip.segments
            ]
            conversation = create_conversation(
                user_id=user.user_id,
                client_id=client_id,
                title=clip.conversation_title,
                summary="Imported annotation dataset; excluded from user memory.",
                external_source_id=external_source_id,
                external_source_type="annotation_dataset",
                data_purpose="annotation",
                memory_excluded=True,
                memory_exclusion_reason="annotation_dataset_import",
            )
            version_id = str(uuid.uuid4())
            conversation.add_transcript_version(
                version_id=version_id,
                transcript=clip.transcript,
                segments=segments,
                provider="annotation-import",
                model=f"chronicle-dataset-v{dataset.schema_version}",
                metadata={
                    "dataset_id": dataset.dataset_id,
                    "clip_id": clip.clip_id,
                    "source_conversation_id": clip.source_conversation_id,
                    "source_client_id": clip.source_client_id,
                    "source_audio_path": clip.audio_path,
                    "annotation_notes": clip.notes,
                },
                set_as_active=True,
            )
            conversation.apply_status(settled=bool(clip.transcript.strip()))
            await conversation.insert()

            ingest = await convert_audio_to_chunks(
                user_id=user.user_id,
                capture_source_id=client_id,
                audio_data=audio_data,
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
                origin="import",
                external_source_id=external_source_id,
                data_purpose="annotation",
            )
            await apply_audio_ranges(conversation, [ingest.audio_range])
            results.append(
                {
                    "clip_id": clip.clip_id,
                    "status": "imported",
                    "conversation_id": conversation.conversation_id,
                    "duration_seconds": round(duration, 2),
                    "chunk_count": ingest.chunk_count,
                    "transcript_source": clip.transcript_source,
                }
            )
        except (AudioValidationError, ValueError) as exc:
            logger.warning(f"Could not import annotation clip {clip.clip_id}: {exc}")
            if conversation and conversation.id:
                await conversation.delete()
            results.append(
                {"clip_id": clip.clip_id, "status": "error", "error": str(exc)}
            )
        except Exception as exc:
            logger.exception(f"Could not import annotation clip {clip.clip_id}")
            if conversation and conversation.id:
                await conversation.delete()
            results.append(
                {"clip_id": clip.clip_id, "status": "error", "error": str(exc)}
            )

    imported = sum(result["status"] == "imported" for result in results)
    skipped = sum(result["status"] == "skipped" for result in results)
    failed = sum(result["status"] == "error" for result in results)
    response = {
        "dataset_id": dataset.dataset_id,
        "schema_version": dataset.schema_version,
        "message": f"Imported {imported} annotation clip(s)",
        "results": results,
        "summary": {
            "total": len(results),
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
        },
    }
    if failed == len(results):
        return JSONResponse(status_code=400, content=response)
    if failed:
        return JSONResponse(status_code=207, content=response)
    return response


async def start_screening(
    user: User,
    conversation_ids: List[str],
    policy: Optional[str] = None,
):
    """Enqueue the privacy-screen job for selected conversations.

    The job applies the shareability ``policy`` (or the configured default) to
    each conversation's transcript and returns the flagged segments for the
    user to review before exporting.
    """
    try:
        job = default_queue.enqueue(
            screen_conversations_job,
            user_id=str(user.user_id),
            conversation_ids=conversation_ids,
            policy=policy,
            job_timeout=1800,
            result_ttl=JOB_RESULT_TTL,
            description=f"Privacy-screen {len(conversation_ids)} conversations",
        )
        logger.info(
            f"Enqueued sensitivity screen (job {job.id}) for user {user.user_id}"
        )
        return {"job_id": job.id, "status": "queued"}
    except Exception as e:
        logger.exception(f"Error enqueueing sensitivity screen: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing screen"}
        )


async def get_default_sensitivity_policy():
    """Return the configured default shareability policy (UI prefill)."""
    return {"policy": get_sensitivity_policy()}


async def preview_export(
    user: User,
    conversation_ids: List[str],
    mode: str = "clips",
    pad_seconds: float = 1.0,
    speech_threshold: float = 0.5,
    merge_gap_seconds: float = 3.0,
    excluded_ranges: Optional[Dict[str, List[List[float]]]] = None,
):
    """Dry-run of the export: the exact clips it would produce, without
    writing any audio.

    Runs the same plan computation as the export job
    (``utils/export_planning.plan_conversation_clips``), so the boundaries,
    durations, and sliced transcripts returned here are byte-for-byte what
    the manifest would contain. Unanalyzed conversations are reported as
    skipped (``not analyzed``) rather than analyzed inline — VAD over a long
    recording is too slow for a synchronous endpoint; the UI points at the
    Analyze button.
    """

    fences = []
    excluded_ranges = excluded_ranges or {}
    user_id = str(user.user_id)
    conversations: List[dict] = []
    totals = {"clip_count": 0, "total_clip_seconds": 0.0, "excluded_seconds": 0.0}

    try:
        for cid in dict.fromkeys(conversation_ids):
            conv = await Conversation.find_one(Conversation.conversation_id == cid)
            entry: Dict[str, Any] = {"conversation_id": cid}
            skipped = export_eligibility(conv, user_id, user.is_superuser)
            if skipped:
                entry["skipped_reason"] = skipped
                conversations.append(entry)
                continue
            try:
                snapshot = await privacy_module.require_record(conv)
            except privacy_module.PrivacyHeld:
                entry["skipped_reason"] = (
                    "Private or unscreened evidence is held from processing"
                )
                conversations.append(entry)
                continue
            entry.update(
                {
                    "title": conv.title if conv else None,
                    "client_id": conv.client_id if conv else None,
                    "created_at": (
                        conv.created_at.isoformat()
                        if conv and conv.created_at
                        else None
                    ),
                }
            )
            if not skipped:
                plan = await plan_conversation_clips(
                    conv,
                    mode,
                    pad_seconds,
                    speech_threshold,
                    merge_gap_seconds,
                    excluded_ranges.get(cid),
                )
                skipped = plan.skipped_reason
            try:
                await privacy_module.assert_current(str(conv.user_id), snapshot)
            except privacy_module.PrivacyHeld:
                conversations.append(
                    {
                        "conversation_id": cid,
                        "skipped_reason": "Privacy changed; reload the export preview",
                    }
                )
                continue
            fences.append((str(conv.user_id), snapshot))
            if skipped:
                entry["skipped_reason"] = skipped
                conversations.append(entry)
                continue

            segments = active_segments(conv)
            clips = []
            for clip in plan.clips:
                sliced = slice_segments(segments, clip.start, clip.end)
                clips.append(
                    {
                        "clip_index": clip.clip_index,
                        "clip_id": f"{cid}_{clip.clip_index:03d}",
                        "start": round(clip.start, 2),
                        "end": round(clip.end, 2),
                        "duration_seconds": round(clip.duration, 2),
                        "text": build_transcript_text(sliced),
                        "segment_count": len(sliced),
                    }
                )
            entry["clips"] = clips
            entry["sample_rate"] = plan.sample_rate
            entry["clip_seconds"] = round(plan.clip_seconds, 2)
            entry["excluded_seconds"] = plan.excluded_seconds
            totals["clip_count"] += len(clips)
            totals["total_clip_seconds"] += plan.clip_seconds
            totals["excluded_seconds"] += plan.excluded_seconds
            conversations.append(entry)

        totals["total_clip_seconds"] = round(totals["total_clip_seconds"], 2)
        totals["excluded_seconds"] = round(totals["excluded_seconds"], 2)
        totals["conversation_count"] = len(conversations)
        totals["exported_conversations"] = sum(
            1 for c in conversations if "skipped_reason" not in c
        )
        for owner, snapshot in fences:
            await privacy_module.assert_current(owner, snapshot)
        return {"conversations": conversations, "totals": totals}
    except privacy_module.PrivacyHeld:
        return JSONResponse(
            status_code=423,
            content={"error": "Privacy changed; reload the export preview"},
        )
    except Exception as e:
        logger.exception(f"Error previewing annotation export: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error previewing export"}
        )


async def start_export(
    user: User,
    conversation_ids: List[str],
    mode: str = "clips",
    pad_seconds: float = 1.0,
    speech_threshold: float = 0.5,
    merge_gap_seconds: float = 3.0,
    excluded_ranges: Optional[Dict[str, List[List[float]]]] = None,
    dropped_ranges: Optional[Dict[str, List[List[float]]]] = None,
    sensitivity_policy: Optional[str] = None,
):
    """Enqueue the annotation-dataset export job for selected conversations.

    ``excluded_ranges`` maps conversation_id → withheld ``[start, end]`` ranges
    confirmed from the privacy screen; ``dropped_ranges`` → clips the user
    unticked in the export preview. Both are carved out of the export.
    """

    for identifier in conversation_ids:
        await privacy_module.require_conversation(identifier)
    try:
        export_id = new_export_id()
        job = default_queue.enqueue(
            export_annotation_dataset_job,
            user_id=str(user.user_id),
            export_id=export_id,
            conversation_ids=conversation_ids,
            mode=mode,
            pad_seconds=pad_seconds,
            speech_threshold=speech_threshold,
            merge_gap_seconds=merge_gap_seconds,
            excluded_ranges=excluded_ranges,
            dropped_ranges=dropped_ranges,
            sensitivity_policy=sensitivity_policy,
            job_timeout=3600,
            result_ttl=JOB_RESULT_TTL,
            description=f"Export annotation dataset ({len(conversation_ids)} conversations, {mode})",
        )
        logger.info(
            f"Enqueued annotation export {export_id} (job {job.id}) "
            f"for user {user.user_id}"
        )
        return {"job_id": job.id, "export_id": export_id, "status": "queued"}
    except Exception as e:
        logger.exception(f"Error enqueueing annotation export: {e}")
        return JSONResponse(
            status_code=500, content={"error": "Error enqueueing export"}
        )


def _read_export_meta(user: User, export_id: str):
    """Load an export's metadata, enforcing id validity + ownership.

    Returns (meta_dict, None) or (None, error_response).
    """
    if not validate_export_id(export_id):
        return None, JSONResponse(
            status_code=422, content={"error": "Invalid export id"}
        )
    meta_path = export_dir(export_id) / META_NAME
    if not meta_path.is_file():
        return None, JSONResponse(
            status_code=404, content={"error": "Export not found"}
        )
    meta = json.loads(meta_path.read_text())
    if not user.is_superuser and meta.get("created_by") != str(user.user_id):
        return None, JSONResponse(
            status_code=403, content={"error": "Access forbidden"}
        )
    return meta, None


async def list_exports(user: User):
    """List completed exports (superusers see all, others their own)."""

    exports = []
    if EXPORTS_DIR.is_dir():
        for meta_path in EXPORTS_DIR.glob(f"*/{META_NAME}"):
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                logger.warning(f"Unreadable export metadata: {meta_path}")
                continue
            if not user.is_superuser and meta.get("created_by") != str(user.user_id):
                continue
            try:
                await privacy_module.guard_export(meta)
            except privacy_module.PrivacyHeld:
                continue
            meta["zip_ready"] = (meta_path.parent / ZIP_NAME).is_file()
            exports.append(meta)
    exports.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return {"exports": exports}


async def download_export(user: User, export_id: str):
    """Stream an export's dataset.zip as an attachment."""
    meta, error = _read_export_meta(user, export_id)
    if error:
        return error

    try:
        snapshots = await privacy_module.guard_export(meta)
    except privacy_module.PrivacyHeld:
        return JSONResponse(
            status_code=423,
            content={"error": "Private or unscreened evidence is held from processing"},
        )
    zip_path = export_dir(export_id) / ZIP_NAME
    if not zip_path.is_file():
        return JSONResponse(
            status_code=404, content={"error": "Export zip not found (job failed?)"}
        )
    return privacy_module.PrivacyFileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{export_id}.zip",
        privacy_snapshots=snapshots,
    )


async def delete_export(user: User, export_id: str):
    """Delete an export directory (zip + metadata)."""
    meta, error = _read_export_meta(user, export_id)
    if error:
        return error
    shutil.rmtree(export_dir(export_id))
    logger.info(f"Deleted annotation export {export_id}")
    return {"deleted": True, "export_id": export_id}
