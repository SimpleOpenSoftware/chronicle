"""Durable records for external multimodal capture sources."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel

from backend.models.timeline import EvidenceLocator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# How many frames of one observation are shortlisted for the curation agent to choose
# between. Mirrors the collector's own cap; the backend re-applies it when merging
# shortlists so a long-lived observation cannot accumulate an unbounded list.
MAX_FRAME_CANDIDATES = 6


class CaptureSource(Document):
    user_id: str
    source_id: str
    name: str
    provider: Literal["screenpipe", "immich", "mobile"]
    platform: str
    token_hash: str
    capabilities: list[str] = Field(default_factory=list)
    status: Literal["pairing", "online", "offline", "error"] = "pairing"
    health: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None
    privacy_enabled_from: Optional[datetime] = None
    privacy_revision: int = 0
    privacy_tracks: list[str] = Field(default_factory=list)
    privacy_operation: Optional[str] = None
    privacy_updating: bool = False
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "capture_sources"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("source_id", ASCENDING)], unique=True),
            IndexModel([("token_hash", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("last_seen_at", DESCENDING)]),
        ]


class PairingCode(Document):
    user_id: str
    code_hash: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "device_input_pairing_codes"
        indexes = [
            IndexModel([("code_hash", ASCENDING)], unique=True),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ]


class DeviceInputItem(Document):
    user_id: str
    source_id: str
    kind: Literal[
        "audio",
        "activity",
        "observation",
        "screen_context",
        "immich_memory",
    ]
    source_item_id: str
    locator: EvidenceLocator
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    media_data: Optional[bytes] = None
    media_filename: Optional[str] = None
    media_content_type: Optional[str] = None
    content_hash: Optional[str] = None
    conversation_id: Optional[str] = None
    promoted_path: Optional[str] = None
    state: Literal["received", "linked", "promoted", "rejected"] = "received"
    lifecycle: Optional[Literal["open", "closed"]] = None
    curation: Optional[
        Literal[
            "pending",
            "curating",
            "discarded",
            "duplicate",
            "linked",
            "promoted",
            "failed",
        ]
    ] = None
    samples: list[dict[str, Any]] = Field(default_factory=list)
    frame_candidates: list[dict[str, Any]] = Field(default_factory=list)
    # Fetched previews the curation agent looks at and chooses between; the one it
    # picks is copied into ``media_data`` above. Distinct concepts: this is the
    # shortlist, that is the selection. Entries hold ``frame_id``, ``data``,
    # ``content_type``, and ``captured_at``.
    media_previews: list[dict[str, Any]] = Field(default_factory=list)
    related_conversation_ids: list[str] = Field(default_factory=list)
    duplicate_of: Optional[str] = None
    curation_revision: Optional[str] = None
    curated_at: Optional[datetime] = None
    agent_reason: Optional[str] = None
    vault_paths: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_locator_source(self) -> "DeviceInputItem":
        """A typed track identity must belong to the authenticated capture source."""

        if self.locator.capture_source_id != self.source_id:
            raise ValueError("device-input locator does not match authenticated source")
        return self

    class Settings:
        name = "device_input_items"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("source_id", ASCENDING),
                    ("kind", ASCENDING),
                    ("source_item_id", ASCENDING),
                ],
                unique=True,
            ),
            IndexModel([("user_id", ASCENDING), ("captured_at", DESCENDING)]),
            IndexModel([("conversation_id", ASCENDING), ("captured_at", ASCENDING)]),
        ]


class DeviceInputJob(Document):
    user_id: str
    source_id: str
    kind: Literal["screen_context", "thumbnail", "source_media"]
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    purpose: str
    priority: int = Field(default=0, ge=0, le=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    context_request_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    status: Literal["pending", "claimed", "complete", "failed"] = "pending"
    claimed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "device_input_jobs"
        indexes = [
            IndexModel(
                [
                    ("source_id", ASCENDING),
                    ("status", ASCENDING),
                    ("priority", DESCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("context_request_id", ASCENDING),
                    ("source_id", ASCENDING),
                ],
                unique=True,
                partialFilterExpression={"context_request_id": {"$type": "string"}},
                name="device_input_context_request_source",
            ),
            IndexModel(
                [("idempotency_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"idempotency_key": {"$type": "string"}},
                name="device_input_job_idempotency",
            ),
        ]
