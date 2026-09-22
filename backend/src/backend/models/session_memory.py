"""Durable user dispositions and preparation leases for session memory."""

from datetime import date, datetime
from typing import Any, Literal

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from .timeline import utcnow


class UndatedSession(Document):
    """An explicit activity selection whose event date is unknown, not upload time."""

    user_id: str
    memory_space_id: str | None = None
    session_key: str
    revision: int
    recording_id: str
    recording_revision: str
    source_hash: str
    title: str
    sources: list[dict[str, Any]]
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "undated_sessions"
        indexes = [
            IndexModel([("session_key", 1), ("revision", 1)], unique=True),
            IndexModel([("user_id", 1), ("recording_id", 1)]),
        ]


class SessionProposalView(BaseModel):
    """Polling projection excludes source text, note bodies and inference records."""

    proposal_id: str
    session_key: str | None = None
    selected_tokens: list[str]
    source_scope_hash: str = ""
    state: str
    accepted_change_ids: list[str] = Field(default_factory=list)
    corrected_by_proposal_id: str | None = None
    account: dict[str, Any] | None = None
    questions: list[str] = Field(default_factory=list)
    change_count: int
    stage: str
    completed_sources: int
    total_sources: int
    investigation_activity: dict[str, Any] = Field(default_factory=dict)
    failure_kind: str | None = None
    error: str | None = None
    refresh_assessment: dict[str, Any] | None = None

    class Settings:
        projection = {
            key: 1
            for key in (
                "proposal_id",
                "session_key",
                "selected_tokens",
                "source_scope_hash",
                "state",
                "accepted_change_ids",
                "corrected_by_proposal_id",
                "account",
                "questions",
                "stage",
                "completed_sources",
                "total_sources",
                "investigation_activity",
                "failure_kind",
                "error",
                "refresh_assessment",
            )
        }
        projection["change_count"] = {"$size": "$changes"}


class MemorySourceDecision(Document):
    user_id: str
    memory_space_id: str | None = None
    session_key: str
    action: Literal["exclude", "defer", "resume", "include", "attribute", "clarify"]
    clarification: str | None = Field(default=None, max_length=2000)
    role: Literal["user_statement", "third_party", "media_content"] | None = None
    sources: list[dict[str, Any]]
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "memory_source_decisions"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("sources.evidence_id", ASCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
            IndexModel(
                [("user_id", ASCENDING), ("sources.capture_chunk_ids", ASCENDING)]
            ),
            IndexModel(
                [("user_id", ASCENDING), ("sources.parent_evidence_ids", ASCENDING)]
            ),
        ]


class SessionPreparation(Document):
    user_id: str
    local_date: date
    timezone: str
    snapshot_id: str
    result_snapshot_id: str | None = None
    priority: int = 0
    organize_only: bool = False
    state: Literal["queued", "running", "waiting", "complete", "failed", "stale"] = (
        "queued"
    )
    attempts: int = 0
    requested_revision: int = 0
    completed_revision: int = 0
    job_id: str | None = None
    error: str | None = None
    inference_artifacts: list[str] = Field(default_factory=list)
    waiting_sessions: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "timeline_session_preparations"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", ASCENDING),
                    ("timezone", ASCENDING),
                    ("snapshot_id", ASCENDING),
                ],
                unique=True,
            )
        ]
