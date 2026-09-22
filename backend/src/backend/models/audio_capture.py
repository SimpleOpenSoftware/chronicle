"""Capture identity, semantic audio claims, and processing artifacts.

Raw audio belongs to a technical capture session.  Conversations, timeline
episodes, transcripts, and diarization outputs reference the same immutable
audio documents through :class:`AudioRangeRef`; none of those semantic objects
owns or moves the audio.
"""

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document, Indexed
from pydantic import BaseModel, Field, model_validator
from pymongo import IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def bson_datetime(value: datetime) -> datetime:
    """Return the timestamp MongoDB will actually persist.

    BSON datetimes have millisecond precision. Normalizing before validation keeps an
    interval that passed in memory from becoming invalid after a database round trip.
    """

    value = as_utc(value)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


CaptureOrigin = Literal["streaming", "upload", "batch", "screenpipe", "import"]
CaptureStatus = Literal["active", "finalizing", "complete", "failed"]
CaptureTimeBasis = Literal["captured", "recorded", "received", "unknown"]
ArtifactStatus = Literal["pending", "complete", "failed"]
CaptureProcessingProfile = Literal[
    "ambient",
    "imported",
    "source_native",
    "duplex_aec",
    "duplex_isolated",
    "half_duplex",
]
CaptureDataPurpose = Literal["normal_capture", "annotation"]
EffectReporting = Literal["reported", "unreported", "not_applicable"]

# Packet clocks can jitter slightly around their nominal audio duration. A larger
# deviation is a real capture discontinuity and must become an audio-document seam.
CAPTURE_CONTINUITY_TOLERANCE_SECONDS = 0.25


class CaptureEffectStatus(BaseModel):
    """What one capture engine actually reported about a platform effect."""

    reporting: EffectReporting
    requested: Optional[bool] = None
    available: Optional[bool] = None
    enabled: Optional[bool] = None

    @model_validator(mode="after")
    def validate_reporting(self) -> "CaptureEffectStatus":
        values = (self.requested, self.available, self.enabled)
        if self.reporting == "reported":
            if any(value is None for value in values):
                raise ValueError(
                    "reported effects require requested, available, and enabled"
                )
            if self.enabled and (not self.requested or not self.available):
                raise ValueError(
                    "an enabled effect must have been requested and available"
                )
        elif any(value is not None for value in values):
            raise ValueError(
                "unreported and not-applicable effects cannot carry boolean claims"
            )
        return self


class CaptureEffects(BaseModel):
    """Capture-time effect provenance, persisted once on the owning session."""

    aec: CaptureEffectStatus
    noise_suppression: CaptureEffectStatus

    @classmethod
    def unreported(cls) -> "CaptureEffects":
        return cls(
            aec=CaptureEffectStatus(reporting="unreported"),
            noise_suppression=CaptureEffectStatus(reporting="unreported"),
        )

    @classmethod
    def not_applicable(cls) -> "CaptureEffects":
        return cls(
            aec=CaptureEffectStatus(reporting="not_applicable"),
            noise_suppression=CaptureEffectStatus(reporting="not_applicable"),
        )

    @property
    def is_reported(self) -> bool:
        return (
            self.aec.reporting == "reported"
            and self.noise_suppression.reporting == "reported"
        )


class CaptureStartProvenance(BaseModel):
    """Application-level capture claims supplied when a stream is opened.

    Transport adapters construct this type explicitly. The capture lifecycle never
    infers provenance from a codec/options dictionary.
    """

    protocol: int | None = None
    capture_epoch: int = Field(ge=0)
    processing_profile: CaptureProcessingProfile
    effects: CaptureEffects
    voice_session_id: str | None = None
    data_purpose: CaptureDataPurpose = "normal_capture"
    memory_space_id: str | None = None


class AudioRangeRef(BaseModel):
    """An ordered claim over immutable audio chunks and absolute UTC bounds."""

    range_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    capture_source_id: str
    time_basis: CaptureTimeBasis
    chunk_ids: list[str] = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    capture_session_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range(self) -> "AudioRangeRef":
        self.started_at = bson_datetime(self.started_at)
        self.ended_at = bson_datetime(self.ended_at)
        if self.ended_at <= self.started_at:
            raise ValueError("audio range must have positive duration")
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("audio range chunk_ids must be unique and ordered")
        return self

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class AudioCaptureSession(Document):
    """One technical ingest/recovery attempt, never a semantic recording."""

    capture_session_id: Indexed(str, unique=True) = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    user_id: Indexed(str)
    capture_source_id: Indexed(str)
    client_id: str
    origin: CaptureOrigin
    time_basis: CaptureTimeBasis
    capture_epoch: int = Field(ge=0)
    processing_profile: CaptureProcessingProfile
    effects: CaptureEffects
    voice_session_id: Optional[str]
    status: CaptureStatus = "active"
    source_stream: Optional[str] = None
    external_source_id: Optional[str] = None
    content_sha256: Optional[str] = Field(
        default=None,
        description="Digest of finite imported PCM; absent for an open live stream",
    )
    data_purpose: str = Field(
        default="normal_capture",
        description="Operational use of the capture (annotation data is not lived timeline evidence)",
    )
    memory_space_id: Optional[str] = Field(
        default=None,
        description="Semantic memory space selected when the capture started; null means Main",
    )
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
    sample_rate: int = Field(default=16000, gt=0)
    channels: int = Field(default=1, gt=0)
    sample_width: int = Field(default=2, gt=0)
    created_at: datetime = Field(default_factory=utcnow)
    failure: Optional[str] = None

    @model_validator(mode="after")
    def validate_times(self) -> "AudioCaptureSession":
        self.started_at = as_utc(self.started_at)
        if self.ended_at is not None:
            self.ended_at = as_utc(self.ended_at)
            if self.ended_at < self.started_at:
                raise ValueError("capture session cannot end before it starts")
        interactive = self.processing_profile in {
            "duplex_aec",
            "duplex_isolated",
            "half_duplex",
        }
        if interactive and not self.voice_session_id:
            raise ValueError("interactive capture profiles require a voice_session_id")
        if not interactive and self.voice_session_id is not None:
            raise ValueError(
                "non-interactive capture profiles cannot bind a voice session"
            )
        if interactive and not self.effects.is_reported:
            raise ValueError("interactive capture profiles require reported effects")
        if self.processing_profile == "imported" and (
            self.capture_epoch != 0 or self.effects != CaptureEffects.not_applicable()
        ):
            raise ValueError(
                "imported capture requires epoch zero and not-applicable effects"
            )
        if self.processing_profile == "source_native" and self.capture_epoch != 0:
            raise ValueError("source-native capture requires epoch zero")
        return self

    class Settings:
        name = "audio_capture_sessions"
        indexes = [
            [("user_id", 1), ("capture_source_id", 1), ("started_at", 1)],
            IndexModel(
                [("source_stream", 1)],
                unique=True,
                partialFilterExpression={"source_stream": {"$type": "string"}},
                name="unique_capture_source_stream",
            ),
            IndexModel(
                [("external_source_id", 1)],
                sparse=True,
                name="capture_external_source",
            ),
            IndexModel(
                [("user_id", 1), ("content_sha256", 1)],
                unique=True,
                partialFilterExpression={
                    "content_sha256": {"$type": "string"},
                    "processing_profile": "imported",
                },
                name="unique_user_imported_pcm_content",
            ),
        ]


class ArtifactAudioSpan(BaseModel):
    """One physical absolute-time piece of presentation-time evidence.

    A provider interval may cross from one ordered ``AudioRangeRef`` into another.
    Those ranges can be discontinuous or overlap in wall-clock time, so one pair of
    absolute timestamps cannot represent the interval honestly. ``audio_spans`` keep
    one piece per claimed range while the owner retains its presentation offsets.

    Point spans are valid for zero-duration STT evidence. Neural diarization turns
    themselves remain strictly positive in presentation time.
    """

    audio_range_id: str
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def validate_span(self) -> "ArtifactAudioSpan":
        self.started_at = bson_datetime(self.started_at)
        self.ended_at = bson_datetime(self.ended_at)
        if self.ended_at < self.started_at:
            raise ValueError("artifact audio span cannot run backward")
        return self


def _validate_presentation_offsets(
    start_seconds: float, end_seconds: float, *, require_positive: bool
) -> None:
    if not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
        raise ValueError("artifact presentation offsets must be finite")
    if start_seconds < 0:
        raise ValueError("artifact presentation offsets cannot be negative")
    if end_seconds < start_seconds or (
        require_positive and end_seconds <= start_seconds
    ):
        qualifier = "positive" if require_positive else "non-negative"
        raise ValueError(f"artifact presentation interval must be {qualifier}")


class AbsoluteWord(BaseModel):
    text: str
    start_seconds: float
    end_seconds: float
    audio_spans: list[ArtifactAudioSpan] = Field(min_length=1)
    confidence: Optional[float] = None
    provider_speaker: Optional[str] = None

    @model_validator(mode="after")
    def validate_offsets(self) -> "AbsoluteWord":
        _validate_presentation_offsets(
            self.start_seconds, self.end_seconds, require_positive=False
        )
        return self


class TranscriptUtterance(BaseModel):
    text: str
    start_seconds: float
    end_seconds: float
    audio_spans: list[ArtifactAudioSpan] = Field(min_length=1)
    words: list[AbsoluteWord] = Field(default_factory=list)
    provider_speaker: Optional[str] = None
    confidence: Optional[float] = None

    @model_validator(mode="after")
    def validate_offsets(self) -> "TranscriptUtterance":
        _validate_presentation_offsets(
            self.start_seconds, self.end_seconds, require_positive=False
        )
        return self


class TranscriptArtifact(Document):
    """Immutable STT evidence over audio; it exists before any Conversation."""

    artifact_id: Indexed(str, unique=True) = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    retry_key: Indexed(str, unique=True)
    user_id: Indexed(str)
    capture_source_ids: list[str] = Field(min_length=1)
    audio_ranges: list[AudioRangeRef] = Field(min_length=1)
    provider: str
    model: Optional[str] = None
    status: ArtifactStatus = "complete"
    transcript: str = ""
    words: list[AbsoluteWord] = Field(default_factory=list)
    utterances: list[TranscriptUtterance] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    failure: Optional[str] = None

    @model_validator(mode="after")
    def validate_evidence_spans(self) -> "TranscriptArtifact":
        ranges = {
            audio_range.range_id: audio_range for audio_range in self.audio_ranges
        }
        if len(ranges) != len(self.audio_ranges):
            raise ValueError("transcript artifact audio range IDs must be unique")
        for word in self.words:
            _validate_spans_against_ranges(word.audio_spans, ranges)
        for utterance in self.utterances:
            _validate_spans_against_ranges(utterance.audio_spans, ranges)
            for word in utterance.words:
                _validate_spans_against_ranges(word.audio_spans, ranges)
        return self

    class Settings:
        name = "transcript_artifacts"
        indexes = [
            [("user_id", 1), ("audio_ranges.started_at", 1)],
            "capture_source_ids",
        ]


class DiarizationTurn(BaseModel):
    start_seconds: float
    end_seconds: float
    audio_spans: list[ArtifactAudioSpan] = Field(min_length=1)
    speaker: str
    identified_as: Optional[str] = None
    confidence: Optional[float] = None
    embedding: Optional[list[float]] = None

    @model_validator(mode="after")
    def validate_offsets(self) -> "DiarizationTurn":
        _validate_presentation_offsets(
            self.start_seconds, self.end_seconds, require_positive=True
        )
        return self


def _validate_spans_against_ranges(
    spans: list[ArtifactAudioSpan], ranges: dict[str, AudioRangeRef]
) -> None:
    for span in spans:
        audio_range = ranges.get(span.audio_range_id)
        if audio_range is None:
            raise ValueError(
                f"artifact span references unknown audio range {span.audio_range_id}"
            )
        if (
            span.started_at < audio_range.started_at
            or span.ended_at > audio_range.ended_at
        ):
            raise ValueError(
                f"artifact span lies outside audio range {span.audio_range_id}"
            )


class DiarizationArtifact(Document):
    """Immutable neural speaker-segmentation output over an audio range."""

    artifact_id: Indexed(str, unique=True) = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    retry_key: Indexed(str, unique=True)
    user_id: Indexed(str)
    capture_source_ids: list[str] = Field(min_length=1)
    audio_ranges: list[AudioRangeRef] = Field(min_length=1)
    provider: str = "pyannote"
    model: Optional[str] = None
    status: ArtifactStatus = "complete"
    turns: list[DiarizationTurn] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    failure: Optional[str] = None

    @model_validator(mode="after")
    def validate_turn_spans(self) -> "DiarizationArtifact":
        ranges = {
            audio_range.range_id: audio_range for audio_range in self.audio_ranges
        }
        if len(ranges) != len(self.audio_ranges):
            raise ValueError("diarization artifact audio range IDs must be unique")
        for turn in self.turns:
            _validate_spans_against_ranges(turn.audio_spans, ranges)
        return self

    class Settings:
        name = "diarization_artifacts"
        indexes = [
            [("user_id", 1), ("audio_ranges.started_at", 1)],
            "capture_source_ids",
        ]


class ConversationTranscriptRevision(Document):
    """Derived, user-facing fusion of transcript and diarization evidence."""

    revision_id: Indexed(str, unique=True) = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    retry_key: Indexed(str, unique=True)
    conversation_id: Indexed(str)
    transcript_artifact_ids: list[str] = Field(default_factory=list)
    diarization_artifact_ids: list[str] = Field(default_factory=list)
    transcript: str = ""
    words: list[dict[str, Any]] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    provider: Optional[str] = None
    model: Optional[str] = None
    diarization_source: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "conversation_transcript_revisions"
        indexes = [[("conversation_id", 1), ("created_at", 1)]]
