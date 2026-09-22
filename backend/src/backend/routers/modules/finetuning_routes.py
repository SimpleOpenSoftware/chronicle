"""
Fine-tuning routes for Chronicle API.

Handles sending annotation corrections to speaker recognition service for training
and cron job management for automated tasks.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.auth import current_active_user
from backend.constants import is_non_enrollable_speaker
from backend.cron_scheduler import get_scheduler
from backend.models.annotation import Annotation, AnnotationSource, AnnotationType
from backend.models.conversation import Conversation
from backend.services import privacy
from backend.services.observability.system_events import record_event
from backend.services.speaker_enrollment import capture_evidence
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.users import User
from backend.utils.audio_chunk_utils import reconstruct_audio_segment
from backend.workers.finetuning_jobs import run_asr_finetuning_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/finetuning", tags=["finetuning"])

# Segments whose start is within this many seconds of the annotation's recorded
# start are treated as the same segment when keying by time.
_SEGMENT_START_TOLERANCE = 0.25


async def _existing_conversation_ids(conversation_ids: set[str]) -> set[str]:
    """Which of these conversations still exist. Ids only — never the documents.

    Orphan detection needs nothing but the ids, and a Conversation carries its whole
    transcript. Loading these as models pulled **71.7 MB into roughly 354,000 Word and
    SpeakerSegment instances** on a single status call here, for 31 ids. Allocation on
    that scale reliably triggers a generation-2 collection, which holds the GIL, and
    the event loop was measured stalling ~3 s on it.
    """
    if not conversation_ids:
        return set()
    return set(
        await Conversation.get_pymongo_collection().distinct(
            "conversation_id", {"conversation_id": {"$in": list(conversation_ids)}}
        )
    )


def _resolve_annotated_segment(segments, segment_index, segment_start_time):
    """Find the segment an annotation refers to.

    Prefers matching by ``segment_start_time`` (stable across re-ordering) and
    falls back to the stored index. Returns the segment or ``None`` if neither
    locates a valid segment.
    """
    if segment_start_time is not None:
        best = None
        best_delta = _SEGMENT_START_TOLERANCE
        for seg in segments:
            delta = abs(seg.start - segment_start_time)
            if delta <= best_delta:
                best = seg
                best_delta = delta
        if best is not None:
            return best
    if segment_index is not None and 0 <= segment_index < len(segments):
        return segments[segment_index]
    return None


async def _record_training_failure(annotation: Annotation, reason: str) -> None:
    """Persist a training failure on the annotation so it can be surfaced/cleared.

    Without this the annotation stays in the "applied, not trained" bucket and
    re-fails on every run with no visible record — the Fine-tuning page appears
    stuck. Recording the attempt + reason lets the UI show the failure and offer
    retry/discard.
    """
    annotation.training_attempts = (annotation.training_attempts or 0) + 1
    annotation.training_error = reason
    annotation.updated_at = datetime.now(timezone.utc)
    await annotation.save()


# ---------------------------------------------------------------------------
# Curated enrollment: build a quality-gated candidate set from the ACTIVE
# transcript version (not annotation replay), let the user review/promote, then
# enroll only the selected clips. This is the safe path that replaces the blunt
# "process every applied annotation" enrollment which mismatched audio↔label and
# enrolled cross-talk/short scraps.
# ---------------------------------------------------------------------------

# Minimum clip duration to allow into enrollment (drops short, low-information
# scraps that blur a single centroid). User-chosen default.
ENROLL_MIN_DURATION = 3.0
# Default-select at most this many (longest) clips per speaker; the rest stay
# available but unticked so the user can add them deliberately.
ENROLL_DEFAULT_PER_SPEAKER = 5
# Two clips whose start AND end match within this are treated as the same span.
ENROLL_DEDUP_TOLERANCE = 0.20


def _overlaps_other_person(segments, idx: int) -> bool:
    """True if segment ``idx`` time-overlaps a DIFFERENT enrollable person's segment.

    Cross-talk (two real speakers at once) is poison for single-speaker
    enrollment, so it's excluded. Overlap with non-enrollable background
    (``Unknown Speaker N`` / noise / TV) is ignored — a clean solo clip spoken
    over background is still good enrollment audio.
    """
    a = segments[idx]
    for j, b in enumerate(segments):
        if j == idx:
            continue
        if is_non_enrollable_speaker(b.speaker) or b.speaker == a.speaker:
            continue
        if b.start < a.end and a.start < b.end:
            return True
    return False


def _resolve_relabeled_segment(segs, ann) -> Optional[int]:
    """Locate the active-version segment index a user diarization annotation now
    points at, or None if the relabel no longer survives there.

    Prefer the annotation's recorded index (if it still carries the corrected
    label at the right time); else match by start time + corrected label. The
    label must still equal ``corrected_speaker`` so a segment re-diarized away or
    overwritten by auto-identification is NOT treated as user-labelled.
    """
    target = ann.corrected_speaker
    st = ann.segment_start_time
    idx = ann.segment_index
    if idx is not None and 0 <= idx < len(segs):
        s = segs[idx]
        if s.speaker == target and (st is None or abs(s.start - st) <= 0.5):
            return idx
    if st is not None:
        best, best_d = None, 0.5
        for i, s in enumerate(segs):
            if s.speaker == target:
                d = abs(s.start - st)
                if d <= best_d:
                    best, best_d = i, d
        return best
    return None


def _resolve_inserted_segment(segs, ann) -> Optional[int]:
    """Locate the active-version segment created by a speech INSERT annotation.

    Apply may sort segments chronologically, so ``insert_after_index`` is not a
    stable active-version index. The explicit waveform span, speaker, and text
    are the durable identity of an inserted speech segment.
    """
    if (
        ann.insert_segment_type != "speech"
        or not ann.insert_speaker
        or ann.insert_start is None
        or ann.insert_end is None
    ):
        return None
    for i, segment in enumerate(segs):
        if (
            segment.speaker == ann.insert_speaker
            and abs(segment.start - ann.insert_start) <= ENROLL_DEDUP_TOLERANCE
            and abs(segment.end - ann.insert_end) <= ENROLL_DEDUP_TOLERANCE
            and (segment.text or "") == (ann.insert_text or "")
        ):
            return i
    return None


@router.get("/enrollment-candidates")
async def get_enrollment_candidates(
    current_user: User = Depends(current_active_user),
    min_duration: float = Query(ENROLL_MIN_DURATION, ge=0.0, le=30.0),
    include_identified: bool = Query(
        False,
        description="Also surface segments auto-labelled by speaker "
        "identification (not relabelled by you). Off by default — enrolling "
        "auto-matched clips reinforces weak/marginal matches.",
    ),
):
    """Per-speaker enrollment candidate clips from explicit human speaker labels.

    A candidate is a segment in a conversation's active transcript whose speaker
    was set by a processed USER diarization annotation or a processed USER speech
    INSERT annotation. The annotation must still resolve to the labelled segment
    in the active transcript.
    Segments auto-labelled by the identification service are NOT candidates
    unless ``include_identified`` is set (and even then they are flagged
    ``auto_identified`` and never pre-ticked). Preview-only — nothing is enrolled.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin only")

    annotations = await Annotation.find(
        {
            "annotation_type": {
                "$in": [AnnotationType.DIARIZATION.value, AnnotationType.INSERT.value]
            }
        },
        Annotation.processed == True,
        # Human-authored labels only — never enrol a voiceprint from an
        # unreviewed AI (model_suggestion) label.
        Annotation.source == AnnotationSource.USER,
    ).to_list()
    # conv_id -> untrained human speaker-label annotations
    labels_by_conv: dict[str, list] = {}
    for a in annotations:
        if a.processed_by and "training" in a.processed_by:
            continue
        speaker = (
            a.corrected_speaker
            if a.annotation_type == AnnotationType.DIARIZATION
            else a.insert_speaker
        )
        if not a.conversation_id or not speaker:
            continue
        if (
            a.annotation_type == AnnotationType.INSERT
            and a.insert_segment_type != "speech"
        ):
            continue
        if is_non_enrollable_speaker(speaker):
            continue
        labels_by_conv.setdefault(a.conversation_id, []).append(a)

    if not labels_by_conv:
        return JSONResponse(
            content={
                "candidates": [],
                "min_duration": min_duration,
                "default_per_speaker": ENROLL_DEFAULT_PER_SPEAKER,
                "conversation_count": 0,
                "include_identified": include_identified,
            }
        )

    conversations = await Conversation.find(
        {"conversation_id": {"$in": list(labels_by_conv.keys())}}
    ).to_list()

    privacy_filter = privacy.ConversationPrivacyFilter()
    permitted = await privacy_filter.filter(
        [{"conversation_id": conv.conversation_id} for conv in conversations]
    )
    allowed_ids = {row["conversation_id"] for row in permitted}
    conversations = [
        conv for conv in conversations if conv.conversation_id in allowed_ids
    ]

    def _clip(conv, segs, i, auto):
        s = segs[i]
        dur = round(s.end - s.start, 2)
        audio_dur = conv.audio_total_duration or 0.0
        reasons = []
        if not (s.end > s.start >= 0) or (audio_dur and s.start >= audio_dur):
            reasons.append("invalid times")
        if dur < min_duration:
            reasons.append(f"short ({dur:.1f}s < {min_duration:.0f}s)")
        if _overlaps_other_person(segs, i):
            reasons.append("overlaps another speaker")
        return {
            "conversation_id": conv.conversation_id,
            "conversation_title": conv.title or "",
            "segment_index": i,
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "duration": dur,
            "text": (s.text or "")[:120],
            "gated_in": len(reasons) == 0,
            "reasons": reasons,
            "auto_identified": auto,
        }

    # speaker -> list of candidate clip dicts
    by_speaker: dict[str, list] = {}
    for conv in conversations:
        tr = conv.active_transcript
        if not tr or not tr.segments:
            continue
        segs = tr.segments
        # Segments the user explicitly labelled (resolved into the active version).
        resolved: dict[int, str] = {}
        for a in labels_by_conv.get(conv.conversation_id, []):
            if a.annotation_type == AnnotationType.DIARIZATION:
                idx = _resolve_relabeled_segment(segs, a)
                speaker = a.corrected_speaker
            else:
                idx = _resolve_inserted_segment(segs, a)
                speaker = a.insert_speaker
            if idx is not None:
                resolved[idx] = speaker
        for i, speaker in resolved.items():
            by_speaker.setdefault(speaker, []).append(_clip(conv, segs, i, False))
        # Opt-in: segments auto-labelled by identification (never pre-ticked).
        if include_identified:
            for i, s in enumerate(segs):
                if i in resolved or is_non_enrollable_speaker(s.speaker):
                    continue
                if getattr(s, "identified_as", None):
                    by_speaker.setdefault(s.speaker, []).append(
                        _clip(conv, segs, i, True)
                    )

    # Per speaker: default-select the longest N clean, deduped — but NEVER an
    # auto-identified clip (those stay unticked for deliberate review).
    candidates = []
    for speaker, clips in sorted(by_speaker.items()):
        clean = [c for c in clips if c["gated_in"] and not c["auto_identified"]]
        deduped = []
        for c in sorted(clean, key=lambda x: -x["duration"]):
            if any(
                abs(c["start"] - d["start"]) <= ENROLL_DEDUP_TOLERANCE
                and abs(c["end"] - d["end"]) <= ENROLL_DEDUP_TOLERANCE
                for d in deduped
            ):
                continue
            deduped.append(c)
        chosen = {
            (c["conversation_id"], c["segment_index"])
            for c in deduped[:ENROLL_DEFAULT_PER_SPEAKER]
        }
        for c in clips:
            c["default_selected"] = (
                c["conversation_id"],
                c["segment_index"],
            ) in chosen
        clips.sort(
            key=lambda x: (x["auto_identified"], not x["gated_in"], -x["duration"])
        )
        candidates.append(
            {
                "speaker": speaker,
                "clips": clips,
                "selected_count": len(chosen),
            }
        )

    await privacy_filter.assert_current()
    return JSONResponse(
        content={
            "candidates": candidates,
            "min_duration": min_duration,
            "default_per_speaker": ENROLL_DEFAULT_PER_SPEAKER,
            "conversation_count": len(conversations),
            "include_identified": include_identified,
        }
    )


class SelectedClip(BaseModel):
    conversation_id: str
    segment_index: int
    start: float
    end: float
    speaker: str


class EnrollSelectedRequest(BaseModel):
    clips: list[SelectedClip]


@router.post("/enroll-selected")
async def enroll_selected_clips(
    body: EnrollSelectedRequest,
    current_user: User = Depends(current_active_user),
):
    """Enroll ONLY the explicitly selected clips, carved from the active version.

    This takes an explicit list of spans the user promoted — no annotation
    replay, no start-time/index guessing. Each clip is
    re-validated against the conversation's current active version (guards a
    version change between review and submit), then enrolled/appended by speaker
    name. Conversations we enrolled at least one clip from have their
    applied-but-untrained diarization annotations marked trained (cleared from
    the queue).
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin only")

    if not body.clips:
        return JSONResponse(content={"message": "No clips selected", "enrolled": 0})

    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503,
            content={"message": "Speaker recognition service not enabled"},
        )

    enrolled = 0
    appended = 0
    failed = 0
    skipped = 0
    errors: list[str] = []
    touched_conv_ids: set[str] = set()
    policies = {}
    held = 0

    # Cache conversations to avoid refetching per clip
    conv_cache = {}

    for clip in body.clips:
        try:
            if is_non_enrollable_speaker(clip.speaker):
                skipped += 1
                continue

            conv = conv_cache.get(clip.conversation_id)
            if conv is None:
                conv = await Conversation.find_one(
                    Conversation.conversation_id == clip.conversation_id
                )
                conv_cache[clip.conversation_id] = conv
            if not conv or not conv.active_transcript:
                failed += 1
                errors.append(f"{clip.conversation_id[:8]}: conversation not found")
                continue

            policy = await privacy.require_record(conv)
            policies[clip.conversation_id] = (str(conv.user_id), policy)
            visibility = privacy.ConversationPrivacyFilter()
            visibility.snapshots[str(conv.user_id)] = policy

            # Re-validate the span against the current active version (guards a
            # version change between candidate-fetch and submit).
            segs = conv.active_transcript.segments
            seg = None
            if 0 <= clip.segment_index < len(segs):
                cand = segs[clip.segment_index]
                if (
                    abs(cand.start - clip.start) <= ENROLL_DEDUP_TOLERANCE
                    and abs(cand.end - clip.end) <= ENROLL_DEDUP_TOLERANCE
                    and cand.speaker == clip.speaker
                ):
                    seg = cand
            if seg is None:
                skipped += 1
                errors.append(
                    f"{clip.conversation_id[:8]} seg {clip.segment_index}: "
                    f"segment changed since review; skipped"
                )
                continue

            evidence_records = await capture_evidence(
                visibility, [clip.conversation_id]
            )
            wav_bytes = await reconstruct_audio_segment(
                conversation_id=clip.conversation_id,
                start_time=seg.start,
                end_time=seg.end,
            )
            if not wav_bytes:
                failed += 1
                errors.append(
                    f"{clip.conversation_id[:8]} seg {clip.segment_index}: no audio"
                )
                continue

            await privacy.assert_current(str(conv.user_id), policy)
            existing = await speaker_client.get_speaker_by_name(
                speaker_name=clip.speaker, user_id=str(current_user.user_id)
            )
            await privacy.assert_current(str(conv.user_id), policy)
            if existing:
                result = await speaker_client.append_to_speaker(
                    speaker_id=existing["id"],
                    audio_data=wav_bytes,
                    user_id=str(current_user.user_id),
                    speaker_name=clip.speaker,
                    conversation_ids=[clip.conversation_id],
                    visibility=visibility,
                    evidence_records=evidence_records,
                )
                await privacy.assert_current(str(conv.user_id), policy)
                if "error" in result:
                    failed += 1
                    errors.append(
                        f"{clip.speaker}: append failed ({result.get('error')})"
                    )
                    continue
                if result.get("status") == "already_enrolled":
                    skipped += 1
                else:
                    appended += 1
            else:
                result = await speaker_client.enroll_new_speaker(
                    speaker_name=clip.speaker,
                    audio_data=wav_bytes,
                    user_id=str(current_user.user_id),
                    conversation_ids=[clip.conversation_id],
                    visibility=visibility,
                    evidence_records=evidence_records,
                )
                await privacy.assert_current(str(conv.user_id), policy)
                if "error" in result:
                    failed += 1
                    errors.append(
                        f"{clip.speaker}: enroll failed ({result.get('error')})"
                    )
                    continue
                if result.get("status") == "already_enrolled":
                    skipped += 1
                else:
                    enrolled += 1

            touched_conv_ids.add(clip.conversation_id)

        except privacy.PrivacyHeld:
            held += 1
            continue
        except Exception as e:
            failed += 1
            errors.append(
                f"{clip.conversation_id[:8]} seg {clip.segment_index}: {str(e)[:50]}"
            )
            logger.error(
                f"enroll_selected: error on {clip.conversation_id} seg {clip.segment_index}: {e}",
                exc_info=True,
            )

    # Mark applied-but-untrained human speaker labels from enrolled conversations
    # as trained so both relabel and inserted-speech candidates leave the queue.
    marked = 0
    if touched_conv_ids:
        anns = await Annotation.find(
            {
                "annotation_type": {
                    "$in": [
                        AnnotationType.DIARIZATION.value,
                        AnnotationType.INSERT.value,
                    ]
                }
            },
            Annotation.processed == True,
            {"conversation_id": {"$in": list(touched_conv_ids)}},
        ).to_list()
        for a in anns:
            owner, policy = policies[a.conversation_id]
            await privacy.assert_current(owner, policy)
            if a.processed_by and "training" in a.processed_by:
                continue
            a.processed_by = (
                f"{a.processed_by},training" if a.processed_by else "training"
            )
            a.training_error = None
            a.updated_at = datetime.now(timezone.utc)
            await a.save()
            marked += 1

    total = enrolled + appended
    logger.info(
        f"enroll_selected complete: {total} enrolled ({enrolled} new, {appended} appended), "
        f"{failed} failed, {skipped} skipped, {marked} annotations marked trained"
    )
    return JSONResponse(
        content={
            "message": "Enrollment complete",
            "enrolled_new": enrolled,
            "appended": appended,
            "total_enrolled": total,
            "privacy_held": held,
            "failed": failed,
            "skipped": skipped,
            "annotations_marked_trained": marked,
            "errors": errors[:10],
            "status": "success" if total > 0 else "partial_failure",
        }
    )


@router.post("/export-asr-dataset")
async def export_asr_dataset(
    current_user: User = Depends(current_active_user),
):
    """
    Manually trigger ASR fine-tuning data export.

    Finds applied transcript/diarization annotations not yet consumed by ASR training,
    reconstructs audio, builds VibeVoice training labels, and POSTs to the ASR service.

    Returns:
        Export job results with counts of conversations exported and annotations consumed.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Only administrators can trigger ASR dataset export"
        )

    try:
        result = await run_asr_finetuning_job()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"ASR dataset export failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"ASR dataset export failed: {str(e)}"
        )


@router.get("/status")
async def get_finetuning_status(
    current_user: User = Depends(current_active_user),
):
    """
    Get fine-tuning status and statistics.

    Returns:
        - pending_annotation_count: Annotations not yet applied
        - applied_annotation_count: Annotations applied but not trained
        - trained_annotation_count: Annotations sent to training
        - last_training_run: Timestamp of last training job
        - cron_status: Cron job schedule and last run info
    """
    try:
        # ------------------------------------------------------------------
        # Per-type annotation counts (with orphan detection)
        # ------------------------------------------------------------------
        annotation_counts: dict[str, dict] = {}
        trained_diarization_list: list = []
        failed_diarization_errors: list[str] = []

        # Collect all annotations to batch-check for orphans
        all_annotations_by_type: dict[AnnotationType, list] = {}
        for ann_type in AnnotationType:
            all_annotations_by_type[ann_type] = await Annotation.find(
                Annotation.annotation_type == ann_type,
            ).to_list()

        # Batch-check which conversation_ids still exist
        conv_annotation_types = {
            AnnotationType.DIARIZATION,
            AnnotationType.TRANSCRIPT,
            AnnotationType.SPEECH_SUGGESTION_CORRECTION,
        }
        all_conv_ids: set[str] = set()
        for ann_type in conv_annotation_types:
            for a in all_annotations_by_type.get(ann_type, []):
                if a.conversation_id:
                    all_conv_ids.add(a.conversation_id)

        existing_conv_ids = await _existing_conversation_ids(all_conv_ids)

        orphaned_conv_ids = all_conv_ids - existing_conv_ids

        total_orphaned = 0
        for ann_type in AnnotationType:
            annotations = all_annotations_by_type[ann_type]

            # Identify orphaned annotations for conversation-based types
            if ann_type in conv_annotation_types:
                orphaned = [
                    a for a in annotations if a.conversation_id in orphaned_conv_ids
                ]
                non_orphaned = [
                    a for a in annotations if a.conversation_id not in orphaned_conv_ids
                ]
            else:
                # Memory/entity orphan detection is placeholder for now
                orphaned = []
                non_orphaned = annotations

            pending = [a for a in non_orphaned if not a.processed]
            processed = [a for a in non_orphaned if a.processed]
            trained = [
                a for a in processed if a.processed_by and "training" in a.processed_by
            ]
            applied_not_trained = [
                a
                for a in processed
                if not a.processed_by or "training" not in a.processed_by
            ]
            # "Failed" = applied annotations that hit a training error and are still
            # stuck (not yet trained). Surfaced so an admin can retry or discard them.
            failed = [a for a in applied_not_trained if (a.training_attempts or 0) > 0]

            orphan_count = len(orphaned)
            total_orphaned += orphan_count

            annotation_counts[ann_type.value] = {
                "total": len(non_orphaned),
                "pending": len(pending),
                "applied": len(applied_not_trained),
                "trained": len(trained),
                "orphaned": orphan_count,
                "failed": len(failed),
            }

            if ann_type == AnnotationType.DIARIZATION and failed:
                seen_errors: set[str] = set()
                for a in failed:
                    if a.training_error and a.training_error not in seen_errors:
                        seen_errors.add(a.training_error)
                        failed_diarization_errors.append(a.training_error)

            if ann_type == AnnotationType.DIARIZATION:
                trained_diarization_list = trained

        # ------------------------------------------------------------------
        # Diarization-specific fields (backward compat)
        # ------------------------------------------------------------------
        diarization = annotation_counts.get("diarization", {})
        pending_count = diarization.get("pending", 0)
        applied_count = diarization.get("applied", 0)
        trained_count = diarization.get("trained", 0)
        failed_count = diarization.get("failed", 0)

        # Get last training run timestamp from diarization annotations
        last_training_run = None
        if trained_diarization_list:
            latest_trained = max(
                trained_diarization_list,
                key=lambda a: (
                    a.updated_at
                    if a.updated_at
                    else datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            last_training_run = (
                latest_trained.updated_at.isoformat()
                if latest_trained.updated_at
                else None
            )

        # Get cron job status from scheduler
        try:
            scheduler = get_scheduler()
            all_jobs = await scheduler.get_all_jobs_status()
            # Find speaker finetuning job for backward compat
            speaker_job = next(
                (j for j in all_jobs if j["job_id"] == "speaker_finetuning"), None
            )
            cron_status = {
                "enabled": speaker_job["enabled"] if speaker_job else False,
                "schedule": speaker_job["schedule"] if speaker_job else "0 2 * * *",
                "last_run": speaker_job["last_run"] if speaker_job else None,
                "next_run": speaker_job["next_run"] if speaker_job else None,
            }
        except Exception:
            cron_status = {
                "enabled": False,
                "schedule": "0 2 * * *",
                "last_run": None,
                "next_run": None,
            }

        return JSONResponse(
            content={
                "pending_annotation_count": pending_count,
                "applied_annotation_count": applied_count,
                "trained_annotation_count": trained_count,
                "failed_annotation_count": failed_count,
                "failed_annotation_errors": failed_diarization_errors[:10],
                "last_training_run": last_training_run,
                "cron_status": cron_status,
                "annotation_counts": annotation_counts,
                "orphaned_annotation_count": total_orphaned,
            }
        )

    except Exception as e:
        logger.error(f"Error fetching fine-tuning status: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch fine-tuning status: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Orphaned Annotation Management Endpoints
# ---------------------------------------------------------------------------


@router.delete("/orphaned-annotations")
async def delete_orphaned_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (e.g. 'diarization')"
    ),
):
    """
    Find and delete orphaned annotations whose referenced conversation no longer exists.

    Only handles conversation-based annotation types (diarization, transcript).
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    conv_annotation_types = {AnnotationType.DIARIZATION, AnnotationType.TRANSCRIPT}

    # Filter to requested type if specified
    if annotation_type:
        try:
            requested_type = AnnotationType(annotation_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unknown annotation type: {annotation_type}"
            )
        if requested_type not in conv_annotation_types:
            return JSONResponse(
                content={
                    "deleted_count": 0,
                    "by_type": {},
                    "message": "Orphan detection not supported for this type",
                }
            )
        types_to_check = {requested_type}
    else:
        types_to_check = conv_annotation_types

    # Collect all conversation_ids referenced by these annotation types
    all_conv_ids: set[str] = set()
    annotations_by_type: dict[AnnotationType, list] = {}
    for ann_type in types_to_check:
        annotations = await Annotation.find(
            Annotation.annotation_type == ann_type,
        ).to_list()
        annotations_by_type[ann_type] = annotations
        for a in annotations:
            if a.conversation_id:
                all_conv_ids.add(a.conversation_id)

    if not all_conv_ids:
        return JSONResponse(content={"deleted_count": 0, "by_type": {}})

    # Batch-check which conversations still exist
    existing_conv_ids = await _existing_conversation_ids(all_conv_ids)
    orphaned_conv_ids = all_conv_ids - existing_conv_ids

    if not orphaned_conv_ids:
        return JSONResponse(content={"deleted_count": 0, "by_type": {}})

    # Delete orphaned annotations
    deleted_by_type: dict[str, int] = {}
    total_deleted = 0
    for ann_type, annotations in annotations_by_type.items():
        orphaned = [a for a in annotations if a.conversation_id in orphaned_conv_ids]
        for a in orphaned:
            await a.delete()
        if orphaned:
            deleted_by_type[ann_type.value] = len(orphaned)
            total_deleted += len(orphaned)

    logger.info(f"Deleted {total_deleted} orphaned annotations: {deleted_by_type}")
    return JSONResponse(
        content={
            "deleted_count": total_deleted,
            "by_type": deleted_by_type,
        }
    )


@router.post("/orphaned-annotations/reattach")
async def reattach_orphaned_annotations(
    current_user: User = Depends(current_active_user),
):
    """Placeholder for reattaching orphaned annotations to a different conversation."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    raise HTTPException(status_code=501, detail="Reattach functionality coming soon")


# ---------------------------------------------------------------------------
# Failed Annotation Management Endpoints
# ---------------------------------------------------------------------------


async def _find_failed_annotations(annotation_type: Optional[str]) -> list[Annotation]:
    """Return applied-but-stuck annotations that have a recorded training failure.

    These are ``processed=True`` and not yet trained, with ``training_attempts > 0``.
    """
    if annotation_type:
        try:
            ann_type = AnnotationType(annotation_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unknown annotation type: {annotation_type}"
            )
        candidates = await Annotation.find(
            Annotation.annotation_type == ann_type,
            Annotation.processed == True,
        ).to_list()
    else:
        candidates = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.processed == True,
        ).to_list()

    return [
        a
        for a in candidates
        if (a.training_attempts or 0) > 0
        and (not a.processed_by or "training" not in a.processed_by)
    ]


@router.post("/failed-annotations/retry")
async def retry_failed_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (default: diarization)"
    ),
):
    """Clear the recorded failure on stuck annotations so they can be retried.

    Resets ``training_attempts``/``training_error`` (without deleting). The next
    training run re-attempts them; if the underlying cause is fixed they will
    succeed, otherwise they re-appear as failed.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    failed = await _find_failed_annotations(annotation_type)
    for a in failed:
        a.training_attempts = 0
        a.training_error = None
        a.updated_at = datetime.now(timezone.utc)
        await a.save()

    logger.info(f"Reset {len(failed)} failed annotations for retry")
    return JSONResponse(content={"reset_count": len(failed)})


@router.delete("/failed-annotations")
async def delete_failed_annotations(
    current_user: User = Depends(current_active_user),
    annotation_type: Optional[str] = Query(
        None, description="Filter by annotation type (default: diarization)"
    ),
):
    """Discard stuck annotations that keep failing to train (corrupt/unusable)."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    failed = await _find_failed_annotations(annotation_type)
    for a in failed:
        await a.delete()

    logger.info(f"Deleted {len(failed)} failed annotations")
    return JSONResponse(content={"deleted_count": len(failed)})


# ---------------------------------------------------------------------------
# Cron Job Management Endpoints
# ---------------------------------------------------------------------------


class CronJobUpdate(BaseModel):
    enabled: Optional[bool] = None
    schedule: Optional[str] = None


@router.get("/cron-jobs")
async def get_cron_jobs(current_user: User = Depends(current_active_user)):
    """List all cron jobs with status, schedule, last/next run."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    return await scheduler.get_all_jobs_status()


@router.put("/cron-jobs/{job_id}")
async def update_cron_job(
    job_id: str,
    body: CronJobUpdate,
    current_user: User = Depends(current_active_user),
):
    """Update a cron job's schedule or enabled state."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    try:
        await scheduler.update_job(job_id, enabled=body.enabled, schedule=body.schedule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": f"Job '{job_id}' updated", "job_id": job_id}


@router.post("/cron-jobs/{job_id}/run")
async def run_cron_job_now(
    job_id: str,
    current_user: User = Depends(current_active_user),
):
    """Manually trigger a cron job."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    scheduler = get_scheduler()
    try:
        result = await scheduler.run_job_now(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result
