"""
Data-audit routes for Chronicle API.

Endpoints backing the Data Audit dashboard: batch VAD audio analysis,
filtered listing with speech metrics + speaker labels, audio archival (hard
delete of audio bytes, metadata kept), silence-gap detection, and
conversation split/merge.
"""

import logging
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import backend.controllers.conversation_controller as conversation_controller
from backend.auth import (
    current_active_user,
    current_active_user_optional,
    current_superuser,
    get_user_from_token_param,
)
from backend.controllers import (
    background_bucket_controller,
    background_suppression_controller,
    data_audit_controller,
    guided_enrollment_controller,
)
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.users import User
from backend.workers.background_suppression_jobs import backfill_all_suppressions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-audit", tags=["data-audit"])


class AnalyzeRequest(BaseModel):
    conversation_ids: Optional[List[str]] = Field(
        None, description="Subset to analyze; omit for all eligible conversations"
    )
    force: bool = Field(False, description="Re-analyze even if cached results exist")


class ArchiveRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1)
    reason: str = Field(
        "manual_cleanup",
        description="Archive reason: near_silent, bad_speaker, manual_cleanup, etc.",
    )


class SplitRequest(BaseModel):
    split_points: List[float] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Split points in seconds from conversation start",
    )


class MergeRequest(BaseModel):
    conversation_ids: List[str] = Field(
        ..., min_length=2, description="Adjacent conversations to merge, any order"
    )


class UnknownClusterClip(BaseModel):
    identity_key: str
    segment_index: int


class UnknownClusterDecision(BaseModel):
    run_fingerprint: str
    action: Literal["confirm", "dismiss"]
    speaker_name: Optional[str] = None
    accepted_identity_keys: List[str] = Field(default_factory=list)
    enrollment_clips: List[UnknownClusterClip] = Field(default_factory=list)


class ScreenRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1, max_length=200)
    policy: Optional[str] = Field(
        None,
        description="Shareability policy describing what's too sensitive to send "
        "to an annotator. Omit to use the configured default.",
    )


class ExportRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1, max_length=200)
    mode: str = Field(
        "clips",
        pattern="^(clips|full)$",
        description="clips = one WAV per VAD speech region (silence cropped); "
        "full = one WAV per conversation, untouched",
    )
    pad_seconds: float = Field(
        1.0,
        ge=0.0,
        le=10.0,
        description="Padding added on both sides of each speech region",
    )
    speech_threshold: float = Field(
        0.5, ge=0.0, le=1.0, description="VAD frame probability counted as speech"
    )
    merge_gap_seconds: float = Field(
        3.0,
        ge=0.0,
        le=60.0,
        description="Speech regions closer than this become one clip",
    )
    excluded_ranges: Dict[str, List[List[float]]] = Field(
        default_factory=dict,
        description="conversation_id → withheld [start, end] time ranges (seconds) "
        "from the privacy screen; carved out of the exported audio + transcript",
    )
    dropped_ranges: Dict[str, List[List[float]]] = Field(
        default_factory=dict,
        description="conversation_id → [start, end] ranges of clips the user "
        "unticked in the export preview; removed from the export (accounted "
        "separately from privacy withholdings)",
    )
    sensitivity_policy: Optional[str] = Field(
        None, description="Policy used for the screen (recorded in export metadata)"
    )


class ExportPreviewRequest(BaseModel):
    conversation_ids: List[str] = Field(..., min_length=1, max_length=200)
    mode: str = Field("clips", pattern="^(clips|full)$")
    pad_seconds: float = Field(1.0, ge=0.0, le=10.0)
    speech_threshold: float = Field(0.5, ge=0.0, le=1.0)
    merge_gap_seconds: float = Field(3.0, ge=0.0, le=60.0)
    excluded_ranges: Dict[str, List[List[float]]] = Field(
        default_factory=dict,
        description="Privacy-screen withholdings to apply to the preview, so the "
        "clips shown match what the export would produce",
    )


@router.post("/analyze")
async def analyze(
    body: AnalyzeRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue batch VAD analysis. Poll job status via /api/queue/jobs/{id}/status."""
    return await data_audit_controller.enqueue_analysis(
        current_user, body.conversation_ids, body.force
    )


@router.get("/conversations")
async def list_conversations(
    speech_threshold: float = Query(
        0.5,
        ge=0.0,
        le=1.0,
        description="VAD frame probability at/above which audio counts as speech",
    ),
    min_speech_fraction: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description="Minimum speech fraction to include (0-1; 0 disables the filter)",
    ),
    max_speech_fraction: float = Query(
        1.0,
        ge=0.0,
        le=1.0,
        description="Maximum speech fraction to include (0-1; 1 disables the filter)",
    ),
    min_duration: float = Query(0.0, ge=0.0, description="Minimum duration in seconds"),
    max_duration: float = Query(
        0.0, ge=0.0, description="Maximum duration in seconds (0 disables the filter)"
    ),
    created_after: Optional[datetime] = Query(
        None, description="Only conversations created at/after this time (ISO 8601)"
    ),
    created_before: Optional[datetime] = Query(
        None, description="Only conversations created at/before this time (ISO 8601)"
    ),
    include_speakers: Optional[str] = Query(
        None,
        description="Comma-separated speakers a conversation must contain at least one of",
    ),
    exclude_speakers: Optional[str] = Query(
        None, description="Comma-separated speakers a conversation must contain none of"
    ),
    unknown_speakers: Optional[Literal["include", "exclude"]] = Query(
        None,
        description="Include conversations containing any conversation-local unknown "
        "speaker, or exclude every such conversation",
    ),
    dataset_id: Optional[str] = Query(
        None,
        max_length=200,
        description="Only conversations imported from this annotation dataset",
    ),
    exported: Optional[str] = Query(
        None,
        pattern="^(never|exported)$",
        description="Filter on annotation-export history: never = not in any "
        "export, exported = shipped by a previous export",
    ),
    archived_only: bool = Query(
        False,
        description="List archived metadata stubs instead of active conversations",
    ),
    hide_failed: bool = Query(
        False,
        description="Exclude conversations the pipeline marked processing_status='failed'",
    ),
    hide_reviewed: bool = Query(
        False,
        description="Exclude conversations with nothing left to review (no unidentified speech segments)",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(current_active_user),
):
    """List conversations with VAD speech metrics + latest speaker labels, filtered."""

    def _csv(v: Optional[str]) -> Optional[list]:
        return [s.strip() for s in v.split(",") if s.strip()] if v else None

    return await data_audit_controller.list_for_audit(
        current_user,
        speech_threshold=speech_threshold,
        min_speech_fraction=min_speech_fraction,
        max_speech_fraction=max_speech_fraction,
        min_duration=min_duration,
        max_duration=max_duration,
        created_after=created_after,
        created_before=created_before,
        include_speakers=_csv(include_speakers),
        exclude_speakers=_csv(exclude_speakers),
        unknown_speakers=unknown_speakers,
        dataset_id=dataset_id,
        exported=exported,
        archived_only=archived_only,
        hide_failed=hide_failed,
        hide_reviewed=hide_reviewed,
        limit=limit,
        offset=offset,
    )


@router.post("/archive")
async def archive(
    body: ArchiveRequest,
    current_user: User = Depends(current_active_user),
):
    """Archive selected conversations: hard-delete audio bytes, keep metadata stub."""
    return await data_audit_controller.archive_audio_many(
        current_user, body.conversation_ids, body.reason
    )


@router.get("/conversations/{conversation_id}/silence-gaps")
async def silence_gaps(
    conversation_id: str,
    speech_threshold: float = Query(
        0.5,
        ge=0.0,
        le=1.0,
        description="Chunk max VAD probability below which the chunk counts as silent",
    ),
    min_gap_seconds: float = Query(
        900.0, gt=0.0, description="Minimum silence-gap length to report"
    ),
    current_user: User = Depends(current_active_user),
):
    """Detected long silence gaps (candidate split points) from cached chunk VAD scores."""
    return await data_audit_controller.get_silence_gaps(
        current_user,
        conversation_id,
        speech_threshold=speech_threshold,
        min_gap_seconds=min_gap_seconds,
    )


@router.get("/conversations/{conversation_id}/speech-regions")
async def speech_regions(
    conversation_id: str,
    speakers: Optional[str] = Query(
        None,
        description="Comma-separated speaker labels; regions are limited to "
        "VAD speech overlapping those speakers' transcript segments",
    ),
    current_user: User = Depends(current_active_user),
):
    """Merged speech intervals (for speech-skip playback), derived from cached VAD scores."""
    speaker_list = (
        [s.strip() for s in speakers.split(",") if s.strip()] if speakers else None
    )
    return await data_audit_controller.get_speech_regions(
        current_user, conversation_id, speakers=speaker_list
    )


@router.get("/conversations/{conversation_id}/segments")
async def list_segments(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Active-version transcript segments (with speaker-recognition confidence)
    for the speaker-triage panel."""
    return await data_audit_controller.get_segments(current_user, conversation_id)


class IdentifySegmentRequest(BaseModel):
    start: float = Field(..., ge=0.0, description="Segment start time in seconds")
    end: float = Field(..., gt=0.0, description="Segment end time in seconds")


class SpeakerLabelReviewDecision(BaseModel):
    conversation_id: str
    segment_index: int = Field(..., ge=0)
    segment_start_time: float = Field(..., ge=0.0)
    claimed_speaker: str
    confidence: Optional[float] = None
    selection_lane: Optional[Literal["boundary", "control"]] = None
    verdict: Literal[
        "correct", "relabel", "unknown", "background", "mixed", "bad_audio"
    ]
    corrected_speaker: Optional[str] = None


class SpeakerLabelReviewRequest(BaseModel):
    decisions: List[SpeakerLabelReviewDecision] = Field(
        ..., min_length=1, max_length=20
    )


@router.post("/conversations/{conversation_id}/segments/identify")
async def identify_segment(
    conversation_id: str,
    body: IdentifySegmentRequest,
    current_user: User = Depends(current_active_user),
):
    """Live speaker suggestion for one segment: returns the closest enrolled
    speaker + cosine for the clip (even below the match threshold)."""
    return await data_audit_controller.identify_segment_clip(
        current_user, conversation_id, body.start, body.end
    )


@router.get("/speaker-label-reviews/next")
async def next_speaker_label_reviews(
    batch_size: int = Query(5, ge=1, le=20),
    current_user: User = Depends(current_active_user),
):
    """Next corpus-wide batch of speaker identity claims for human review."""
    return await data_audit_controller.next_speaker_label_reviews(
        current_user, batch_size
    )


@router.get("/speaker-label-reviews/metrics")
async def speaker_label_review_metrics(
    current_user: User = Depends(current_active_user),
):
    """Measured identity precision from completed human reviews."""
    return await data_audit_controller.speaker_label_review_metrics(current_user)


@router.post("/speaker-label-reviews/decide")
async def decide_speaker_label_reviews(
    body: SpeakerLabelReviewRequest,
    current_user: User = Depends(current_active_user),
):
    """Record verdicts; relabels become ordinary pending diarization annotations."""
    return await data_audit_controller.decide_speaker_label_reviews(
        current_user, [decision.model_dump() for decision in body.decisions]
    )


@router.get("/speakers/confidence")
async def speakers_confidence(current_user: User = Depends(current_active_user)):
    """Per-speaker identification-confidence stats across the corpus: distribution
    histogram, marginal-match fraction, per-speaker baselines (mean/median/
    %marginal), threshold survival counts, and a data-driven recommended
    similarity threshold. Computed from stored confidence — no re-embedding."""
    return await data_audit_controller.speaker_confidence_overview(current_user)


class GuidedSuggestRequest(BaseModel):
    speaker_name: str = Field(..., description="Enrolled speaker to improve")
    batch_size: int = Field(5, ge=3, le=5, description="Maximum clips per review batch")
    max_scan: int = Field(
        24, ge=4, le=48, description="Shortlist size scored per request"
    )
    include_deleted: bool = Field(
        False,
        description="Discovery only: also index speech from soft-deleted "
        "conversations whose audio chunks are still present",
    )
    order: str = Field(
        "informative",
        pattern="^(informative|confidence)$",
        description="Batch ranking: 'informative' (novelty + boundary "
        "uncertainty) or 'confidence' (highest similarity first)",
    )


class GuidedDecisionClip(BaseModel):
    conversation_id: str
    start: float = Field(..., ge=0.0)
    end: float = Field(..., gt=0.0)
    original_start: float = Field(..., ge=0.0)
    original_end: float = Field(..., gt=0.0)
    decision: str = Field(
        ..., pattern="^(accept|reject|skip|bad_clip|multiple_speakers|another_speaker)$"
    )
    actual_speaker: Optional[str] = None
    scores: Optional[Dict] = Field(
        None, description="Scores shown at review time, kept for provenance"
    )


class GuidedDecideRequest(BaseModel):
    speaker_name: str
    decisions: List[GuidedDecisionClip] = Field(..., min_length=1, max_length=16)


@router.post("/enrollment/guided/suggest")
async def guided_enrollment_suggest(
    body: GuidedSuggestRequest,
    current_user: User = Depends(current_active_user),
):
    """Next batch of highest-information candidate clips for one enrolled speaker:
    corpus scan → embedding scores vs the speaker's gallery → ranked by novelty +
    boundary uncertainty + duration, max 2 clips per conversation."""
    return await guided_enrollment_controller.suggest_clips(
        current_user, body.speaker_name, body.batch_size, body.max_scan, body.order
    )


@router.post("/enrollment/guided/decide")
async def guided_enrollment_decide(
    body: GuidedDecideRequest,
    current_user: User = Depends(current_active_user),
):
    """Record review decisions; confirmed clips are appended to the selected
    speaker's voiceprint, and reviewed clips are not re-suggested."""
    return await guided_enrollment_controller.decide_clips(
        current_user,
        body.speaker_name,
        [d.model_dump() for d in body.decisions],
    )


@router.post("/enrollment/guided/discover")
async def guided_enrollment_discover(
    body: GuidedSuggestRequest,
    current_user: User = Depends(current_active_user),
):
    """Queue reusable corpus-speech indexing and selected-gallery matching."""
    return await guided_enrollment_controller.enqueue_corpus_discovery(
        current_user, body.speaker_name, body.include_deleted
    )


@router.post("/enrollment/unknown/discover")
async def unknown_speaker_discover(
    current_user: User = Depends(current_active_user),
):
    return await guided_enrollment_controller.enqueue_unknown_discovery(current_user)


@router.get("/enrollment/unknown/clusters")
async def unknown_speaker_clusters(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(current_active_user),
):
    return await guided_enrollment_controller.list_unknown_clusters(current_user, limit)


@router.post("/enrollment/unknown/clusters/{cluster_id}/decide")
async def unknown_speaker_cluster_decide(
    cluster_id: str,
    body: UnknownClusterDecision,
    current_user: User = Depends(current_active_user),
):
    return await guided_enrollment_controller.decide_unknown_cluster(
        current_user,
        cluster_id,
        body.run_fingerprint,
        body.action,
        body.speaker_name,
        body.accepted_identity_keys,
        [clip.model_dump() for clip in body.enrollment_clips],
    )


@router.post("/enrollment/guided/mine")
async def guided_enrollment_mine(
    speaker_name: str = Form(..., description="Enrolled speaker to mine for"),
    files: List[UploadFile] = File(..., description="Unlabelled audio corpus"),
    current_user: User = Depends(current_superuser),
):
    """Upload an unlabelled audio corpus and mine it for one speaker's voice.

    Files are ingested as annotation-only conversations (no memory extraction)
    and corpus discovery is chained behind their transcription jobs, so mined
    clips surface in guided enrollment automatically."""
    return await guided_enrollment_controller.mine_uploaded_files(
        current_user, speaker_name, files
    )


class GuidedMineLocalRequest(BaseModel):
    speaker_name: str = Field(..., description="Enrolled speaker to mine for")
    paths: List[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Absolute paths under the backend data directory (/app/data)",
    )


@router.post("/enrollment/guided/mine-local")
async def guided_enrollment_mine_local(
    body: GuidedMineLocalRequest,
    current_user: User = Depends(current_superuser),
):
    """Queue server-side corpus mining over files already on the data volume
    (e.g. backup WAVs of purged conversations). Admin only."""
    return await guided_enrollment_controller.enqueue_local_mining(
        current_user, body.speaker_name, body.paths
    )


@router.get("/enrollment/guided/discover")
async def guided_enrollment_discovery_state(
    speaker_name: str,
    current_user: User = Depends(current_active_user),
):
    """Return the persisted corpus-discovery job so refreshed pages can reattach."""
    return await guided_enrollment_controller.corpus_discovery_state(
        current_user, speaker_name
    )


@router.get("/enrollment/guided/history")
async def guided_enrollment_history(
    speaker_name: str,
    limit: int = 50,
    current_user: User = Depends(current_active_user),
):
    """Dated before/after gallery-health snapshots for enrollment sessions."""
    return await guided_enrollment_controller.enrollment_history(
        current_user, speaker_name, limit
    )


@router.get("/enrollment/guided/gallery")
async def guided_enrollment_gallery(
    speaker_name: str,
    current_user: User = Depends(current_active_user),
):
    """A speaker's enrolled clips with per-clip contamination flags
    (self-similarity, closest other speaker, mislabel/junk/weak)."""
    return await guided_enrollment_controller.gallery_clips(current_user, speaker_name)


class GalleryClipDeleteRequest(BaseModel):
    speaker_name: str = Field(..., description="Speaker the clip must belong to")
    hard: bool = Field(
        False, description="Permanently delete the audio instead of quarantining"
    )


@router.post("/enrollment/guided/gallery/segments/{segment_id}/delete")
async def guided_enrollment_gallery_delete(
    segment_id: int,
    body: GalleryClipDeleteRequest,
    current_user: User = Depends(current_active_user),
):
    """Remove one enrolled clip from the speaker's voiceprint; the speaker
    service recomputes the centroid. Quarantined (recoverable) by default."""
    return await guided_enrollment_controller.delete_gallery_clip(
        current_user, body.speaker_name, segment_id, body.hard
    )


@router.get("/enrollment/guided/gallery/segments/{segment_id}/audio")
async def guided_enrollment_gallery_audio(
    segment_id: int,
    token: Optional[str] = Query(
        default=None, description="JWT token for audio element access"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """Stream one enrolled clip for playback in the gallery panel."""
    if not current_user and token:
        current_user = await get_user_from_token_param(token)
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    audio = await SpeakerRecognitionClient().get_enrollment_segment_audio(segment_id)
    if audio is None:
        raise HTTPException(status_code=404, detail="Clip audio not available")
    return Response(content=audio, media_type="audio/wav")


class GuidedResetRequest(BaseModel):
    speaker_name: str = Field(..., description="Speaker profile to clean")
    purge_gallery: bool = Field(
        False,
        description="Also delete the speaker's voiceprint and enrollment audio "
        "from the speaker service",
    )


@router.post("/enrollment/guided/reset")
async def guided_enrollment_reset(
    body: GuidedResetRequest,
    current_user: User = Depends(current_active_user),
):
    """Forget all guided-enrollment state recorded under a speaker name
    (review decisions, session history, corpus-discovery matches) so clips
    become suggestible again after a delete/re-enroll. Optionally also purges
    the voiceprint gallery on the speaker service."""
    return await guided_enrollment_controller.reset_speaker_state(
        current_user, body.speaker_name, body.purge_gallery
    )


@router.post("/enrollment/benchmark")
async def run_enrollment_benchmark(
    current_user: User = Depends(current_active_user),
):
    """Queue five-fold conversation-grouped evaluation over human-labeled clips."""
    return await guided_enrollment_controller.enqueue_benchmark(current_user)


@router.get("/enrollment/benchmark/latest")
async def latest_enrollment_benchmark(
    current_user: User = Depends(current_active_user),
):
    return await guided_enrollment_controller.latest_benchmark(current_user)


@router.get("/enrollment/baseline")
async def enrollment_baseline(
    current_user: User = Depends(current_active_user),
):
    """Reconstruct all speaker galleries immediately before guided review began."""
    return await guided_enrollment_controller.reconstructed_baseline(current_user)


@router.get("/triage/pending")
async def triage_pending(current_user: User = Depends(current_active_user)):
    """Count of unapplied speaker-triage decisions and conversations they span."""
    return await data_audit_controller.get_triage_pending(current_user)


@router.post("/triage/apply")
async def apply_triage(current_user: User = Depends(current_active_user)):
    """Bulk-apply all pending speaker-triage decisions across every conversation:
    new transcript versions with corrected labels, voiceprint enrollment, and
    chained memory reprocessing (noise decisions skip enrollment)."""
    return await data_audit_controller.apply_triage(current_user)


class BackgroundAddRequest(BaseModel):
    conversation_id: str
    start: float
    end: float
    bucket_type: Literal["noise", "background_speech"]
    source: str = "manual"


class BackgroundClusterDecisionRequest(BaseModel):
    cluster_id: str
    member_keys: List[str] = Field(..., min_length=1)
    review_sample_keys: List[str] = Field(default_factory=list, max_length=5)
    decision: Literal[
        "noise", "background_speech", "not_background", "mixed", "dismissed"
    ]
    sample_decisions: dict[
        str, Literal["noise", "background_speech", "not_background"]
    ] = Field(default_factory=dict)


class BackgroundCleanupApplyRequest(BaseModel):
    report_id: str


@router.post("/background/seed")
async def background_seed(current_user: User = Depends(current_active_user)):
    """Rebuild both background buckets from accepted Noise and Background Speech
    annotations. Idempotent; safe to run repeatedly as new ones are labeled."""
    return await background_bucket_controller.seed_from_annotations(current_user)


@router.post("/background/add")
async def background_add(
    body: BackgroundAddRequest,
    current_user: User = Depends(current_active_user),
):
    """Add one confirmed background clip to the bucket (the accumulation loop)."""
    doc = await background_bucket_controller.add_background_clip(
        body.conversation_id,
        body.start,
        body.end,
        bucket_type=body.bucket_type,
        source=body.source,
        user=current_user,
    )
    if doc is None:
        return JSONResponse(status_code=422, content={"error": "Could not embed clip"})
    return {
        "added": True,
        "conversation_id": body.conversation_id,
        "start": doc["segment_start"],
    }


@router.get("/background/suggest")
async def background_suggest(
    conversation_id: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(current_active_user),
):
    """Rank a conversation's unknown segments as 'potentially background' by
    max-similarity to the background bucket plus low SNR (channel condition)."""
    return await background_bucket_controller.suggest_background_candidates(
        current_user, conversation_id, limit
    )


@router.get("/background/scan")
async def background_scan(
    limit: int = Query(40, ge=1, le=200),
    max_conversations: int = Query(8, ge=1, le=50),
    current_user: User = Depends(current_active_user),
):
    """Corpus-wide 'potentially background' feed: scans recent unknown-heavy
    conversations and returns one merged ranked list of candidate clips."""
    return await background_bucket_controller.scan_background_candidates(
        current_user, limit, max_conversations
    )


@router.post("/background/index")
async def background_index(current_user: User = Depends(current_active_user)):
    """Sample and embed the user's complete corpus as a visible background job."""
    return await background_bucket_controller.enqueue_background_index(current_user)


@router.get("/background/index")
async def background_index_status(current_user: User = Depends(current_active_user)):
    """Current full-corpus background index state, including a resumable job id."""
    return await background_bucket_controller.background_index_state(current_user)


@router.get("/background/clusters")
async def background_clusters(
    limit: int = Query(6, ge=1, le=20),
    samples_per_cluster: int = Query(5, ge=3, le=5),
    lane: Optional[Literal["harvest", "novel", "similar"]] = Query(None),
    surface: Literal["less", "default", "more"] = Query("default"),
    current_user: User = Depends(current_active_user),
):
    """Large within-conversation clusters represented by 3–5 central clips.

    ``surface`` widens/narrows the review dial (lane thresholds + minimum
    cluster size); production suppression thresholds are unaffected.
    """
    return await background_bucket_controller.get_background_clusters(
        current_user, limit, samples_per_cluster, lane, surface
    )


@router.post("/background/clusters/decide")
async def background_cluster_decide(
    body: BackgroundClusterDecisionRequest,
    current_user: User = Depends(current_active_user),
):
    """Classify, individually label, or dismiss a review cluster."""
    return await background_bucket_controller.decide_background_cluster(
        current_user, body.model_dump(), body.decision
    )


@router.get("/background/clusters/latest-decision")
async def background_cluster_latest_decision(
    current_user: User = Depends(current_active_user),
):
    """Latest reversible background review decision for this user."""
    return await background_bucket_controller.latest_background_decision(current_user)


@router.get("/background/clusters/decisions")
async def background_cluster_decisions(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(current_active_user),
):
    """Stored background annotation history, newest first."""
    return await background_bucket_controller.list_background_decisions(
        current_user, limit
    )


@router.delete("/background/clusters/decisions/{review_id}")
async def background_cluster_undo_decision(
    review_id: str,
    current_user: User = Depends(current_active_user),
):
    """Undo a review, remove its references, and restore its clips to the queue."""
    return await background_bucket_controller.undo_background_decision(
        current_user, review_id
    )


@router.get("/background/cleanup/report")
async def background_cleanup_report(
    current_user: User = Depends(current_active_user),
):
    """Preview high-confidence and ambiguous changes from reviewed references."""
    return await background_bucket_controller.background_cleanup_report(current_user)


@router.get("/background/accuracy/report")
async def background_accuracy_report(
    current_user: User = Depends(current_active_user),
):
    """Baseline versus adapted foreground/background F1 over reviewed clusters."""
    return await background_bucket_controller.background_accuracy_report(current_user)


class SuppressionDecisionRequest(BaseModel):
    conversation_id: str
    cluster_signature: str
    decision: Literal["restore", "confirm"]


@router.get("/background/suppressions/{conversation_id}")
async def background_suppressions(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Suppression ledger for one conversation, grouped by cluster."""
    return await background_suppression_controller.get_conversation_suppressions(
        current_user, conversation_id
    )


@router.post("/background/suppressions/decide")
async def background_suppressions_decide(
    body: SuppressionDecisionRequest,
    current_user: User = Depends(current_active_user),
):
    """Restore ('important speech') or confirm ('yes, background') a cluster."""
    return await background_suppression_controller.decide_suppression_cluster(
        current_user, body.conversation_id, body.cluster_signature, body.decision
    )


class EditDecisionRequest(BaseModel):
    decision: Literal["noise", "background_speech", "not_background", "dismissed"]


@router.post("/background/clusters/decisions/{review_id}/edit")
async def background_cluster_edit_decision(
    review_id: str,
    body: EditDecisionRequest,
    current_user: User = Depends(current_active_user),
):
    """Change a past review's verdict in place (annotation-history edit)."""
    return await background_bucket_controller.edit_background_decision(
        current_user, review_id, body.decision
    )


@router.post("/background/suppressions/backfill")
async def background_suppressions_backfill(
    current_user: User = Depends(current_active_user),
):
    """Score all corpus-indexed conversations into the ledger (shadow mode)."""

    return await backfill_all_suppressions(str(current_user.user_id))


@router.post("/background/cleanup/apply")
async def background_cleanup_apply(
    body: BackgroundCleanupApplyRequest,
    current_user: User = Depends(current_active_user),
):
    """Create new transcript versions for high-confidence report changes only."""
    return await background_bucket_controller.enqueue_background_cleanup(
        current_user, body.report_id
    )


@router.post("/conversations/{conversation_id}/split")
async def split(
    conversation_id: str,
    body: SplitRequest,
    current_user: User = Depends(current_active_user),
):
    """Split a conversation into children at the given time points.

    Audio chunks are reassigned (no re-encode), the transcript is sliced by
    time range, the parent is soft-deleted with lineage metadata, and memory +
    title jobs are enqueued for each child with a transcript.
    """
    return await data_audit_controller.split_conversation(
        current_user, conversation_id, body.split_points
    )


@router.get("/sensitivity-policy")
async def sensitivity_policy(current_user: User = Depends(current_active_user)):
    """The configured default shareability policy (prefill for the screen UI)."""
    return await data_audit_controller.get_default_sensitivity_policy()


@router.post("/export/screen")
async def screen_export(
    body: ScreenRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue a privacy screen over the selected conversations.

    Applies the shareability policy to each transcript and flags segments too
    personal to share with an annotator. Poll job status via
    /api/queue/jobs/{id}/status; the result lists flagged segments (with time
    ranges) to review, then pass the confirmed ranges to /export as
    ``excluded_ranges``.
    """
    return await data_audit_controller.start_screening(
        current_user, body.conversation_ids, body.policy
    )


@router.post("/export/preview")
async def preview_export(
    body: ExportPreviewRequest,
    current_user: User = Depends(current_active_user),
):
    """Dry-run of the export: the exact clips (boundaries, durations, sliced
    transcripts) the current settings would produce, computed synchronously
    without writing any audio. Unanalyzed conversations are reported skipped
    (``not analyzed``) — run /analyze first. Untick clips here and pass their
    ranges to /export as ``dropped_ranges``.
    """
    return await data_audit_controller.preview_export(
        current_user,
        body.conversation_ids,
        mode=body.mode,
        pad_seconds=body.pad_seconds,
        speech_threshold=body.speech_threshold,
        merge_gap_seconds=body.merge_gap_seconds,
        excluded_ranges=body.excluded_ranges,
    )


@router.post("/export")
async def start_export(
    body: ExportRequest,
    current_user: User = Depends(current_active_user),
):
    """Enqueue an annotation-dataset export: WAV audio + transcript manifest,
    zipped for download. Mode ``clips`` cuts one padded WAV per VAD speech
    region (silence cropped); mode ``full`` exports each conversation as a
    single untouched WAV. ``excluded_ranges`` from the privacy screen and
    ``dropped_ranges`` from the export preview are carved out of the exported
    audio + transcript.

    Poll job status via /api/queue/jobs/{id}/status, then download from
    /api/data-audit/exports/{export_id}/download.
    """
    return await data_audit_controller.start_export(
        current_user,
        body.conversation_ids,
        mode=body.mode,
        pad_seconds=body.pad_seconds,
        speech_threshold=body.speech_threshold,
        merge_gap_seconds=body.merge_gap_seconds,
        excluded_ranges=body.excluded_ranges,
        dropped_ranges=body.dropped_ranges,
        sensitivity_policy=body.sensitivity_policy,
    )


@router.post("/import")
async def import_dataset(
    dataset: UploadFile = File(..., description="Chronicle annotation dataset ZIP"),
    current_user: User = Depends(current_active_user),
):
    """Import WAV clips and existing transcripts from an annotation dataset ZIP.

    Imported clips are immediately available in the transcript editor and are
    permanently excluded from memory processing.
    """
    filename = dataset.filename or ""
    if not filename.lower().endswith(".zip"):
        return JSONResponse(
            status_code=422,
            content={"error": "Annotation dataset must be a .zip file"},
        )
    return await data_audit_controller.import_annotation_dataset(
        current_user, await dataset.read()
    )


@router.get("/exports")
async def list_exports(current_user: User = Depends(current_active_user)):
    """List completed annotation exports with their summaries."""
    return await data_audit_controller.list_exports(current_user)


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: str,
    token: Optional[str] = Query(
        default=None, description="JWT token for direct browser download links"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """Download an export's dataset.zip (header auth or ?token= for <a href>)."""
    if not current_user and token:
        current_user = await get_user_from_token_param(token)
    if not current_user:
        return JSONResponse(
            status_code=401, content={"error": "Authentication required"}
        )
    return await data_audit_controller.download_export(current_user, export_id)


@router.delete("/exports/{export_id}")
async def delete_export(
    export_id: str,
    current_user: User = Depends(current_active_user),
):
    """Delete an export (zip + metadata) from disk."""
    return await data_audit_controller.delete_export(current_user, export_id)


@router.post("/merge")
async def merge(
    body: MergeRequest,
    current_user: User = Depends(current_active_user),
):
    """Merge adjacent conversations (same client, consecutive in time) into a new one.

    Audio chunks are reassigned with time offsets, transcripts are concatenated
    with a seam note where wall-clock gaps are elided, sources are soft-deleted
    with lineage metadata, and memory + title jobs are enqueued.
    """
    return await data_audit_controller.merge_conversations(
        current_user, body.conversation_ids
    )


@router.get("/recordings/{conversation_id}")
async def dataset_recording_detail(
    conversation_id: str, user: User = Depends(current_active_user)
):
    """Explicit corpus inspection; personal activity and memory actions live elsewhere."""

    return await conversation_controller.get_conversation(
        conversation_id, user, dataset=True
    )
