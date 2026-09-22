"""SQLAlchemy models for speaker recognition system."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from . import Base


class User(Base):
    """One tenant of the speaker gallery, identified by its Chronicle user id.

    The id is Chronicle's own ``user_id`` (a MongoDB ObjectId string), not a local
    autoincrementing integer. The service previously minted its own integer identity,
    which Chronicle could not supply — so ``speaker_recognition_client`` hardcoded
    ``user_id="1"`` on every call and every Chronicle user landed in one shared
    gallery. One opaque identity across both services removes the mapping that was
    never built, and with it the chance of getting the mapping wrong.
    """

    __tablename__ = "users"

    id = Column(String(64), primary_key=True)
    username = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    speakers = relationship(
        "Speaker", back_populates="user", cascade="all, delete-orphan"
    )
    annotations = relationship(
        "Annotation", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"


class Speaker(Base):
    """Speaker profile model."""

    __tablename__ = "speakers"

    id = Column(String(100), primary_key=True)  # User-defined speaker ID
    name = Column(String(200), nullable=False)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    embedding_version = Column(Integer, default=1)
    embedding_config = Column(Text)  # JSON: {"method": "mean", "params": {...}}
    embedding_data = Column(Text)  # JSON: serialized embedding vector
    audio_segments_metadata = Column(Text)  # JSON: references to audio segments
    notes = Column(Text)  # Optional notes about the speaker
    audio_sample_count = Column(
        Integer, default=0
    )  # Number of audio segments used for enrollment
    total_audio_duration = Column(
        Float, default=0.0
    )  # Total duration of all audio segments in seconds

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="unique_user_speaker_name"),
    )

    # Relationships
    user = relationship("User", back_populates="speakers")
    enrollment_sessions = relationship(
        "EnrollmentSession", back_populates="speaker", cascade="all, delete-orphan"
    )
    annotations = relationship("Annotation", back_populates="speaker")
    audio_segments = relationship(
        "SpeakerAudioSegment", back_populates="speaker", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Speaker(id='{self.id}', name='{self.name}', user_id={self.user_id})>"


class EnrollmentSession(Base):
    """Enrollment session tracking model."""

    __tablename__ = "enrollment_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    speaker_id = Column(String(100), ForeignKey("speakers.id"), nullable=False)
    audio_file_path = Column(String(500), nullable=False)
    duration_seconds = Column(Float)  # Total audio duration
    speech_duration_seconds = Column(Float)  # Actual speech duration
    quality_score = Column(Float)  # Overall quality score (0.0 to 1.0)
    snr_db = Column(Float)  # Signal-to-noise ratio in dB
    created_at = Column(DateTime, default=datetime.utcnow)
    enrollment_method = Column(String(50))  # 'live_recording' or 'file_upload'

    # Relationships
    speaker = relationship("Speaker", back_populates="enrollment_sessions")

    def __repr__(self):
        return f"<EnrollmentSession(id={self.id}, speaker_id='{self.speaker_id}', quality={self.quality_score})>"


class SpeakerCatalogIdentity(Base):
    """Stable identity of this writable database, preserved across restarts."""

    __tablename__ = "speaker_catalog_identity"
    id = Column(Integer, primary_key=True)
    catalog_id = Column(String(32), nullable=False, unique=True)


class EnrollmentOperation(Base):
    """Durable evidence binding and revocation tombstone for staged enrollment.

    Staged audio is retained separately from the active gallery. A tombstone may
    arrive before prepare, so only the operation identity and tenant are required.
    Segment identity is deliberately not a foreign key: deletion must not erase
    the evidence ledger or permit a delayed activation.
    """

    __tablename__ = "enrollment_operations"

    id = Column(String(32), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    state = Column(String(20), nullable=False)
    binding = Column(Text)
    audio_sha256 = Column(String(64))
    audio_file_path = Column(Text)
    embedding = Column(Text)
    duration_seconds = Column(Float)
    speaker_id = Column(String(100), index=True)
    segment_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SpeakerPrivacyHold(Base):
    """An inseparable gallery stays held across rebuilds and ordinary edits."""

    __tablename__ = "speaker_privacy_holds"
    speaker_id = Column(String(100), primary_key=True)
    user_id = Column(String(64), nullable=False)
    operation_id = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SpeakerAudioSegment(Base):
    """Individual audio segments for speakers."""

    __tablename__ = "speaker_audio_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    speaker_id = Column(String(100), ForeignKey("speakers.id"), nullable=False)
    audio_file_path = Column(String(500), nullable=False)  # Path to segment file
    original_file_path = Column(String(500))  # Original source file
    start_time = Column(Float, nullable=False)  # Start time in original file
    end_time = Column(Float, nullable=False)  # End time in original file
    duration_seconds = Column(Float, nullable=False)
    quality_score = Column(Float)  # Segment quality (0.0 to 1.0)
    embedding = Column(Text)  # JSON: Individual segment embedding
    transcription = Column(Text)  # Transcribed text
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    speaker = relationship("Speaker", back_populates="audio_segments")
    audit_decision = relationship(
        "EnrollmentAuditDecision",
        back_populates="segment",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def __repr__(self):
        return f"<SpeakerAudioSegment(id={self.id}, speaker_id='{self.speaker_id}', duration={self.duration_seconds})>"


class EnrollmentAuditDecision(Base):
    """A human decision that overrides a heuristic enrollment-health flag."""

    __tablename__ = "enrollment_audit_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    segment_id = Column(
        Integer,
        ForeignKey("speaker_audio_segments.id"),
        nullable=False,
        unique=True,
    )
    decision = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    segment = relationship("SpeakerAudioSegment", back_populates="audit_decision")


class Annotation(Base):
    """Audio segment annotation model."""

    __tablename__ = "annotations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    audio_file_path = Column(String(500), nullable=False)
    audio_file_hash = Column(
        String(32)
    )  # MD5 hash of audio file for consistent identification
    audio_file_name = Column(String(255))  # Original filename for display
    start_time = Column(Float, nullable=False)  # Start time in seconds
    end_time = Column(Float, nullable=False)  # End time in seconds
    speaker_id = Column(
        String(100), ForeignKey("speakers.id"), nullable=True
    )  # Can be NULL for unknown speakers
    speaker_label = Column(
        String(100)
    )  # For unknown speakers: "unknown_1", "unknown_2", etc.
    deepgram_speaker_label = Column(
        String(50)
    )  # Original Deepgram label: "speaker_0", "speaker_1", etc.
    label = Column(String(20), nullable=False)  # 'CORRECT', 'INCORRECT', 'UNCERTAIN'
    confidence = Column(Float)  # Model confidence in the annotation (0.0 to 1.0)
    transcription = Column(Text)  # Text content of the segment
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)  # Optional annotation notes

    # Relationships
    speaker = relationship("Speaker", back_populates="annotations")
    user = relationship("User", back_populates="annotations")

    def __repr__(self):
        return f"<Annotation(id={self.id}, speaker_id='{self.speaker_id}', label='{self.label}')>"


class ProcessingJob(Base):
    """Background processing job tracking."""

    __tablename__ = "processing_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(
        String(50), nullable=False
    )  # 'enrollment', 'export', 'annotation', etc.
    status = Column(
        String(20), nullable=False, default="pending"
    )  # 'pending', 'running', 'completed', 'failed'
    input_data = Column(Text)  # JSON string of input parameters
    output_data = Column(Text)  # JSON string of results
    progress = Column(Float, default=0.0)  # Progress percentage (0.0 to 100.0)
    error_message = Column(Text)  # Error details if status is 'failed'
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    def __repr__(self):
        return f"<ProcessingJob(id={self.id}, type='{self.job_type}', status='{self.status}')>"


class ExportHistory(Base):
    """Export operation history."""

    __tablename__ = "export_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    export_type = Column(
        String(50), nullable=False
    )  # 'single_speaker', 'bulk', 'annotations'
    format_type = Column(
        String(20), nullable=False
    )  # 'concatenated', 'segments', 'metadata'
    file_path = Column(String(500))  # Path to generated export file
    file_size_bytes = Column(Integer)
    speaker_ids = Column(Text)  # JSON array of exported speaker IDs
    created_at = Column(DateTime, default=datetime.utcnow)
    downloaded_at = Column(DateTime)  # When the file was downloaded
    cleaned_up = Column(Boolean, default=False)  # Whether temp files were cleaned up

    # Relationships
    user = relationship("User")

    def __repr__(self):
        return f"<ExportHistory(id={self.id}, type='{self.export_type}', user_id={self.user_id})>"
