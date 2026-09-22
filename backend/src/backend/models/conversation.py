"""
Conversation models for Chronicle backend.

This module contains Beanie Document and Pydantic models for conversations
and transcript versions.
"""

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from beanie import (
    Delete,
    Document,
    Indexed,
    Insert,
    Replace,
    Save,
    SaveChanges,
    after_event,
)
from pydantic import BaseModel, Field, computed_field
from pymongo import IndexModel

from backend.models.audio_capture import AudioRangeRef


class Conversation(Document):
    """Complete conversation model with versioned processing."""

    # Nested Enums - Note: TranscriptProvider accepts any string value for flexibility

    class ConversationStatus(str, Enum):
        """Conversation processing status."""

        ACTIVE = "active"  # Has running jobs or open websocket
        COMPLETED = "completed"  # All jobs succeeded
        FAILED = "failed"  # One or more jobs failed

    class EndReason(str, Enum):
        """Why the recorded conversation ended.

        Strictly about the recording's own ending. Why the *pipeline* is running over
        it — an upload, a reprocess, a re-bound — is a separate question answered by
        ``ProcessingTrigger``. Mixing the two meant reprocessing invented a new end
        reason for a conversation that had already ended for a real reason, and the
        invented values were not members here, so they were silently stored as
        ``UNKNOWN`` while the emitted event still reported the raw string.
        """

        USER_STOPPED = "user_stopped"  # User manually stopped recording
        INACTIVITY_TIMEOUT = (
            "inactivity_timeout"  # No speech detected for threshold period
        )
        WEBSOCKET_DISCONNECT = (
            "websocket_disconnect"  # Connection lost (Bluetooth, network, etc.)
        )
        MAX_DURATION = "max_duration"  # Hit maximum conversation duration
        CLOSE_REQUESTED = (
            "close_requested"  # External close signal (API, plugin, button)
        )
        ERROR = "error"  # Processing error forced conversation end
        SPLIT = "split"  # Created by splitting a longer conversation
        MERGE = "merge"  # Created by merging adjacent conversations
        UNKNOWN = "unknown"  # Unknown or legacy reason

    class ProcessingTrigger(str, Enum):
        """Why the post-conversation pipeline is running over a conversation.

        Orthogonal to ``EndReason``: a reprocess of a conversation that ended on
        ``websocket_disconnect`` is still a ``websocket_disconnect`` recording. An
        uploaded file never ended for an operational reason at all, so its
        ``end_reason`` stays unset rather than borrowing this vocabulary.
        """

        LIVE_SESSION = "live_session"  # A live capture session just closed
        FILE_UPLOAD = "file_upload"  # Audio arrived as an uploaded file
        REPROCESS_ORPHAN = "reprocess_orphan"  # Re-run over audio with no transcript
        REPROCESS_TRANSCRIPT = "reprocess_transcript"  # Operator asked for a re-run
        REBOUND = "rebound"  # Recording bounds were recomputed and re-split

    class MemoryContextFrame(BaseModel):
        """One user-reviewable ScreenPipe frame staged for note extraction."""

        source_id: str
        frame_id: int
        captured_at: Optional[datetime] = None
        content_type: str = "image/jpeg"
        data: bytes

    # Nested Models
    class Word(BaseModel):
        """Individual word with timestamp in a transcript."""

        word: str = Field(description="Word text")
        start: float = Field(description="Start time in seconds")
        end: float = Field(description="End time in seconds")
        confidence: Optional[float] = Field(None, description="Confidence score (0-1)")
        speaker: Optional[int] = Field(None, description="Speaker ID from diarization")
        speaker_confidence: Optional[float] = Field(
            None, description="Speaker diarization confidence"
        )

    class SegmentType(str, Enum):
        """Type of transcript segment."""

        SPEECH = "speech"
        EVENT = "event"  # Non-speech: [laughter], [music], etc.
        NOTE = "note"  # User-inserted annotation/tag

    class SpeakerSegment(BaseModel):
        """Individual speaker segment in a transcript."""

        start: float = Field(description="Start time in seconds")
        end: float = Field(description="End time in seconds")
        text: str = Field(description="Transcript text for this segment")
        speaker: str = Field(description="Speaker identifier")
        segment_type: "Conversation.SegmentType" = Field(
            default_factory=lambda: Conversation.SegmentType.SPEECH,
            description="Type: speech, event (non-speech from ASR), or note (user-inserted)",
        )
        identified_as: Optional[str] = Field(
            None,
            description="Speaker name from speaker recognition (None if not identified)",
        )
        confidence: Optional[float] = Field(None, description="Confidence score (0-1)")
        words: List["Conversation.Word"] = Field(
            default_factory=list, description="Word-level timestamps for this segment"
        )

    class TranscriptVersion(BaseModel):
        """Version of a transcript with processing metadata."""

        version_id: str = Field(description="Unique version identifier")
        transcript: Optional[str] = Field(None, description="Full transcript text")
        words: List["Conversation.Word"] = Field(
            default_factory=list,
            description="Word-level timestamps for entire transcript",
        )
        segments: List["Conversation.SpeakerSegment"] = Field(
            default_factory=list,
            description="Speaker segments (filled by speaker recognition)",
        )
        provider: Optional[str] = Field(
            None,
            description="Transcription provider used (deepgram, parakeet, vibevoice, etc.)",
        )
        model: Optional[str] = Field(
            None, description="Model used (e.g., nova-3, parakeet)"
        )
        created_at: datetime = Field(description="When this version was created")
        processing_time_seconds: Optional[float] = Field(
            None, description="Time taken to process"
        )
        diarization_source: Optional[str] = Field(
            None,
            description="Source of speaker diarization: 'provider' (transcription service), 'pyannote' (speaker recognition), or None",
        )
        metadata: Dict[str, Any] = Field(
            default_factory=dict, description="Additional provider-specific metadata"
        )

    class VadAnalysis(BaseModel):
        """Cached VAD summary for a conversation's audio (data-audit feature).

        Frame-level speech probabilities live on the audio chunk documents
        (``AudioChunkDocument.vad_scores``); this stores a threshold-independent
        histogram of those probabilities so the UI can derive the speech
        fraction for any chosen threshold without touching the chunks.
        """

        provider: str = Field(description="VAD provider that produced the scores")
        frame_hop_ms: float = Field(
            description="Milliseconds of audio per VAD frame/score"
        )
        frame_count: int = Field(description="Number of frames scored")
        histogram_bin_width: float = Field(
            description="Width of each probability histogram bin (bins span [0, 1])"
        )
        histogram: List[int] = Field(
            default_factory=list,
            description="Frame counts per speech-probability bin (low→high)",
        )
        chunk_duration_seconds: float = Field(
            description="Nominal audio chunk duration used during analysis"
        )
        speech_regions: Optional[List[List[float]]] = Field(
            None,
            description="Merged [start, end] speech intervals in seconds, for speech-skip playback",
        )
        analyzed_at: datetime = Field(description="When this analysis was computed")

        def speech_fraction(self, threshold: float = 0.5) -> float:
            """Fraction of frames with speech probability >= ``threshold`` (0.0-1.0)."""
            if self.frame_count <= 0 or not self.histogram:
                return 0.0
            speech = 0
            for i, count in enumerate(self.histogram):
                bin_lower = i * self.histogram_bin_width
                if bin_lower >= threshold:
                    speech += count
            return speech / self.frame_count

    class DerivedOperation(str, Enum):
        """Operation that produced a derived conversation.

        Every operation that moves audio into a new conversation belongs here.
        ``silence_trim`` was missing while ``maybe_trim_silence`` passed it, so the
        remnant's lineage record raised at construction and trimming crashed outright.
        """

        SPLIT = "split"
        MERGE = "merge"
        SILENCE_TRIM = "silence_trim"
        REBOUND = "rebound"

    class DerivedFrom(BaseModel):
        """Lineage record for conversations produced by split/merge operations."""

        operation: "Conversation.DerivedOperation" = Field(
            description="Operation that produced this conversation"
        )
        source_conversation_ids: List[str] = Field(
            description="Conversations this one was derived from"
        )
        time_range: Optional[List[float]] = Field(
            None,
            description="For split children: [start, end] seconds in the parent timeline",
        )
        performed_at: datetime = Field(description="When the operation ran")
        performed_by: str = Field(description="User who performed the operation")

    # Core identifiers
    conversation_id: Indexed(str, unique=True) = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique conversation identifier",
    )
    user_id: Indexed(str) = Field(description="User who owns this conversation")
    client_id: Indexed(str) = Field(description="Client device identifier")
    audio_ranges: List[AudioRangeRef] = Field(
        default_factory=list,
        description="Ordered absolute-time claims over immutable audio documents",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Absolute start of the semantic conversation claim",
    )
    ended_at: Optional[datetime] = Field(
        None, description="Absolute end of the semantic conversation claim"
    )
    origin: Literal["deliberate", "detected"] = Field(
        default="deliberate",
        description="Whether a user action or speech segmentation created the claim",
    )
    segmentation_key: Optional[str] = Field(
        None, description="Deterministic idempotency key for detected materialization"
    )

    # External file tracking (for deduplication of imported files)
    external_source_id: Optional[str] = Field(
        None,
        description="External file identifier (e.g., Google Drive file_id) for deduplication",
    )
    external_source_type: Optional[str] = Field(
        None, description="Type of external source (gdrive, dropbox, s3, etc.)"
    )
    data_purpose: Optional[str] = Field(
        None,
        description="Operational purpose for this conversation, e.g. annotation or normal_capture",
    )
    memory_excluded: bool = Field(
        False,
        description="When true, this conversation must not create or reprocess memories",
    )
    memory_exclusion_reason: Optional[str] = Field(
        None,
        description="Why memory processing is disabled for this conversation",
    )
    memory_space_id: Optional[str] = Field(
        None,
        description="Isolated memory space that owns this semantic claim; null means Main",
    )
    published_to_main_at: Optional[datetime] = Field(
        None,
        description="When a space-owned conversation became visible to Main consumers",
    )
    published_by_merge_proposal_id: Optional[str] = Field(
        None,
        description="Space merge proposal that published this conversation to Main",
    )
    memory_review_state: Literal[
        "automatic",
        "awaiting_context",
        "context_requested",
        "ready",
        "extracting",
        "extracted",
        "failed",
    ] = Field(
        default="automatic",
        description=(
            "Space-only checkpoint between transcript completion and note extraction; "
            "Main conversations remain automatic"
        ),
    )
    memory_context_frames: List["Conversation.MemoryContextFrame"] = Field(
        default_factory=list,
        description="Bounded ScreenPipe contact sheet offered for explicit selection",
    )
    selected_memory_context_frame_keys: List[str] = Field(
        default_factory=list,
        description="User-selected source_id:frame_id keys admitted to extraction",
    )
    memory_context_description: Optional[str] = Field(
        None, description="Vision-grounded description of the selected screen evidence"
    )
    memory_review_error: Optional[str] = Field(
        None, description="Last context-review or user-triggered extraction error"
    )

    # MongoDB chunk-based audio storage (new system)
    audio_chunks_count: Optional[int] = Field(
        None, description="Total number of 10-second audio chunks stored in MongoDB"
    )
    audio_total_duration: Optional[float] = Field(
        None, description="Total audio duration in seconds (sum of all chunks)"
    )
    audio_compression_ratio: Optional[float] = Field(
        None,
        description="Compression ratio (compressed_size / original_size), typically ~0.047 for Opus",
    )

    # Cached VAD speech analysis (data-audit feature)
    vad_analysis: Optional["Conversation.VadAnalysis"] = Field(
        None,
        description="Cached VAD speech-probability summary computed from audio chunks",
    )
    audio_integrity_error: Optional[str] = Field(
        None,
        description="Set when the conversation's audio is internally inconsistent "
        "(e.g. reconnect-duplication: stored duration/chunk-count disagree with the "
        "actual chunks). Such conversations are excluded from the data-audit list and "
        "surfaced on the System Errors page instead of being repeatedly re-analyzed.",
    )
    transcript_integrity_error: Optional[str] = Field(
        None,
        description="Set when a transcription response cannot describe the current "
        "conversation audio timeline. The exact paid-provider response remains in the "
        "content-hash cache, but it is not promoted to an active transcript.",
    )

    # Split/merge lineage (data-audit feature)
    derived_from: Optional["Conversation.DerivedFrom"] = Field(
        None, description="Set on conversations created by a split/merge operation"
    )
    derived_into: List[str] = Field(
        default_factory=list,
        description="Conversation IDs this conversation was split/merged into (set on soft-deleted sources)",
    )

    # Audio archival (data-audit feature): audio bytes hard-deleted, metadata kept
    audio_archived: bool = Field(
        False,
        description="Whether the audio bytes were permanently deleted while keeping this metadata stub",
    )
    audio_archived_at: Optional[datetime] = Field(
        None, description="When the audio was archived (hard-deleted)"
    )
    archive_reason: Optional[str] = Field(
        None,
        description="Why audio was archived (near_silent, bad_speaker, manual_cleanup, etc.)",
    )

    # Markers (e.g., button events) captured during the session
    markers: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Markers captured during audio session (button events, bookmarks, etc.)",
    )

    # Creation metadata
    created_at: Indexed(datetime) = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the conversation was created",
    )

    # Processing status tracking
    deleted: bool = Field(
        False,
        description="Whether this conversation was deleted due to processing failure",
    )
    deletion_reason: Optional[str] = Field(
        None,
        description="Reason for deletion (no_meaningful_speech, audio_file_not_ready, etc.)",
    )
    deleted_at: Optional[datetime] = Field(
        None, description="When the conversation was marked as deleted"
    )

    # Processing status.
    # Canonical values are the ConversationStatus enum: "active" | "completed" |
    # "failed". This field is OWNED by apply_status() — derived from facts (does an
    # active transcript exist?) at terminal points and by the reconciler. Individual
    # jobs must NOT hand-stamp it; they do their work and let the finalizer reconcile.
    processing_status: Optional[str] = Field(
        None,
        description="Processing status (ConversationStatus): active, completed, failed",
    )
    # When processing_status == "failed", which pipeline stage failed
    # (e.g. "transcription", "summarization"). Replaces the old overloaded
    # "transcription_failed" string that conflated distinct failure causes.
    failure_stage: Optional[str] = Field(
        None, description="Stage that failed when processing_status == 'failed'"
    )
    processing_enqueued_at: Optional[datetime] = Field(
        None,
        description="When the initial transcript/post-processing chain was durably enqueued",
    )
    # Conversation completion tracking
    end_reason: Optional["Conversation.EndReason"] = Field(
        None, description="Reason why the conversation ended"
    )
    completed_at: Optional[datetime] = Field(
        None, description="When the conversation was completed/closed"
    )

    # Star/favorite
    starred: bool = Field(
        False, description="Whether this conversation is starred/favorited"
    )
    starred_at: Optional[datetime] = Field(
        None, description="When the conversation was starred"
    )

    # Summary fields (auto-generated from transcript)
    title: Optional[str] = Field(None, description="Auto-generated conversation title")
    summary: Optional[str] = Field(
        None, description="Auto-generated short summary (1-2 sentences)"
    )
    detailed_summary: Optional[str] = Field(
        None,
        description="Auto-generated detailed summary (comprehensive, corrected content)",
    )

    # Versioned processing
    transcript_versions: List["Conversation.TranscriptVersion"] = Field(
        default_factory=list,
        description=(
            "Bounded embedded read models: provider sources plus the active derived "
            "projection; immutable processing history lives in standalone revisions"
        ),
    )

    # Active version pointer
    active_transcript_version: Optional[str] = Field(
        None, description="Version ID of currently active transcript"
    )
    active_transcript_revision_id: Optional[str] = Field(
        None,
        description="Standalone derived transcript/diarization revision used for display",
    )

    # Legacy fields removed - use transcript_versions[active_transcript_version].
    # Frontend should access: conversation.active_transcript.segments, conversation.active_transcript.transcript.
    # Memory is no longer versioned (the vault is the system of record); changes are
    # recorded in the memory_audit ledger (see models/memory_audit.py).

    def get_transcript_version(
        self, version_id: str
    ) -> Optional["Conversation.TranscriptVersion"]:
        """Find a transcript version by id, or None if it doesn't exist."""
        for version in self.transcript_versions:
            if version.version_id == version_id:
                return version
        return None

    @computed_field
    @property
    def active_transcript(self) -> Optional["Conversation.TranscriptVersion"]:
        """Get the currently active transcript version."""
        if not self.active_transcript_version:
            return None
        return self.get_transcript_version(self.active_transcript_version)

    # Convenience properties that return data from active transcript version
    @computed_field
    @property
    def transcript(self) -> Optional[str]:
        """Get transcript text from active transcript version."""
        return self.active_transcript.transcript if self.active_transcript else None

    @computed_field
    @property
    def segments(self) -> List["Conversation.SpeakerSegment"]:
        """Get segments from active transcript version."""
        return self.active_transcript.segments if self.active_transcript else []

    @computed_field
    @property
    def segment_count(self) -> int:
        """Get segment count from active transcript version."""
        return len(self.segments) if self.segments else 0

    @computed_field
    @property
    def transcript_version_count(self) -> int:
        """Get count of transcript versions."""
        return len(self.transcript_versions)

    @computed_field
    @property
    def active_transcript_version_number(self) -> Optional[int]:
        """Get 1-based version number of the active transcript version."""
        if not self.active_transcript_version:
            return None
        for i, version in enumerate(self.transcript_versions):
            if version.version_id == self.active_transcript_version:
                return i + 1
        return None

    def add_transcript_version(
        self,
        version_id: str,
        transcript: str,
        words: Optional[List["Conversation.Word"]] = None,
        segments: Optional[List["Conversation.SpeakerSegment"]] = None,
        provider: Optional[
            str
        ] = None,  # Provider name from config.yml (deepgram, parakeet, etc.)
        model: Optional[str] = None,
        processing_time_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        set_as_active: bool = True,
    ) -> "Conversation.TranscriptVersion":
        """Add a new transcript version and optionally set it as active."""
        new_version = Conversation.TranscriptVersion(
            version_id=version_id,
            transcript=transcript,
            words=words or [],
            segments=segments or [],
            provider=provider,
            model=model,
            created_at=datetime.now(),
            processing_time_seconds=processing_time_seconds,
            metadata=metadata or {},
        )

        self.transcript_versions.append(new_version)

        if set_as_active:
            self.active_transcript_version = version_id

        return new_version

    def set_active_transcript_version(self, version_id: str) -> bool:
        """Set a specific transcript version as active."""
        for version in self.transcript_versions:
            if version.version_id == version_id:
                self.active_transcript_version = version_id
                return True
        return False

    @property
    def has_meaningful_transcript(self) -> bool:
        """True when the active transcript version has non-empty text."""
        av = self.active_transcript
        return bool(av and (av.transcript or "").strip())

    @classmethod
    def derive_status(
        cls,
        *,
        has_transcript: bool,
        settled: bool,
        failure_stage: str = "transcription",
    ) -> tuple[str, Optional[str]]:
        """Decide ``(processing_status, failure_stage)`` from facts. The SINGLE owner.

        The transcript is the conversation's core deliverable, so its presence is the
        source of truth for success — independent of which jobs ran or what order they
        ran in. This makes ``failed`` non-absorbing (a later recovery flips it to
        ``completed``) and removes the need for jobs to hand-stamp the status.

        - has a meaningful transcript            -> COMPLETED
        - ``settled`` and no transcript          -> FAILED (failure_stage)
        - otherwise (still in flight)            -> ACTIVE

        ``settled`` means the caller knows the pipeline has reached a terminal point
        (the finalizer job, a fallback dead-end, or the reconciler's staleness check),
        so "no transcript" is final rather than "not yet".

        Split out from :meth:`apply_status` so a caller that cannot afford to load the
        document — the reconciler scans every conversation, and their transcripts are
        by far the largest field — can decide from a projection without a second copy
        of these rules existing anywhere.
        """
        if has_transcript:
            return cls.ConversationStatus.COMPLETED.value, None
        if settled:
            return cls.ConversationStatus.FAILED.value, failure_stage
        return cls.ConversationStatus.ACTIVE.value, None

    def apply_status(
        self, *, settled: bool, failure_stage: str = "transcription"
    ) -> bool:
        """Set processing_status from facts. Returns True if anything changed."""
        prev = (self.processing_status, self.failure_stage)
        self.processing_status, self.failure_stage = self.derive_status(
            has_transcript=self.has_meaningful_transcript,
            settled=settled,
            failure_stage=failure_stage,
        )
        return (self.processing_status, self.failure_stage) != prev

    @after_event(Insert, Replace, Save, SaveChanges, Delete)
    async def update_search_projection(self):

        # Defer this dependency to break the import cycle through backend.services.source_search
        # -> backend.models.conversation.
        from backend.services.source_search import index_recording

        try:
            await index_recording(self.conversation_id)
        except Exception:
            logging.getLogger(__name__).warning(
                "Search update deferred to recovery", exc_info=True
            )

    class Settings:
        name = "conversations"
        indexes = [
            "conversation_id",
            "user_id",
            "created_at",
            [
                ("user_id", 1),
                ("deleted", 1),
                ("created_at", -1),
            ],  # Compound index for paginated list queries
            IndexModel(
                [("external_source_id", 1)], sparse=True
            ),  # Sparse index for deduplication
            IndexModel(
                [("segmentation_key", 1)],
                unique=True,
                partialFilterExpression={"segmentation_key": {"$type": "string"}},
                name="unique_detected_segmentation",
            ),
            IndexModel(
                [
                    ("title", "text"),
                    ("summary", "text"),
                    ("detailed_summary", "text"),
                    ("transcript_versions.transcript", "text"),
                ],
                weights={
                    "title": 10,
                    "summary": 5,
                    "detailed_summary": 3,
                    "transcript_versions.transcript": 1,
                },
                name="conversation_text_search",
            ),
        ]


# Factory function for creating conversations
def create_conversation(
    user_id: str,
    client_id: str,
    conversation_id: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    transcript: Optional[str] = None,
    segments: Optional[List["Conversation.SpeakerSegment"]] = None,
    external_source_id: Optional[str] = None,
    external_source_type: Optional[str] = None,
    data_purpose: Optional[str] = None,
    memory_excluded: bool = False,
    memory_exclusion_reason: Optional[str] = None,
    audio_ranges: Optional[List[AudioRangeRef]] = None,
    origin: Literal["deliberate", "detected"] = "deliberate",
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    segmentation_key: Optional[str] = None,
    memory_space_id: Optional[str] = None,
) -> Conversation:
    """
    Factory function to create a new conversation.

    Args:
        user_id: User who owns this conversation
        client_id: Client device identifier
        conversation_id: Optional unique conversation identifier (auto-generated if not provided)
        title: Optional conversation title
        summary: Optional conversation summary
        transcript: Optional transcript text
        segments: Optional speaker segments
        external_source_id: Optional external file ID for deduplication (e.g., Google Drive file_id)
        external_source_type: Optional external source type (gdrive, dropbox, etc.)

    Returns:
        Conversation instance
    """
    # Build the conversation data
    created_at = started_at or datetime.now(timezone.utc)
    conv_data = {
        "user_id": user_id,
        "client_id": client_id,
        "created_at": created_at,
        "started_at": created_at,
        "ended_at": ended_at,
        "origin": origin,
        "audio_ranges": audio_ranges or [],
        "segmentation_key": segmentation_key,
        "memory_space_id": memory_space_id,
        "memory_review_state": ("awaiting_context" if memory_space_id else "automatic"),
        "title": title,
        "summary": summary,
        "transcript_versions": [],
        "active_transcript_version": None,
        "external_source_id": external_source_id,
        "external_source_type": external_source_type,
        "data_purpose": data_purpose,
        "memory_excluded": memory_excluded,
        "memory_exclusion_reason": memory_exclusion_reason,
    }

    # Only set conversation_id if provided, otherwise let the model auto-generate it
    if conversation_id is not None:
        conv_data["conversation_id"] = conversation_id

    return Conversation(**conv_data)
