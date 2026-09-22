"""Durable evidence and revisioned semantic timeline models."""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel

from backend.models.audio_capture import AudioRangeRef


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


EvidenceKind = Literal[
    "audio_span",
    "transcript",
    "observation",
    "frame",
    "meeting",
    "immich",
    "capture_gap",
]
EvidenceRole = Literal[
    "user_action",
    "user_statement",
    "third_party",
    "application_state",
    "media_content",
    "assistant_generated",
    "ambient",
    "uncertain",
]


class EvidenceLocator(BaseModel):
    """Stable identity of an independently evolving evidence track."""

    capture_source_id: str = Field(min_length=1)
    modality: Literal["screen", "audio", "transcript", "photo", "context"]
    track_id: Optional[str] = None


class EvidenceAnchor(BaseModel):
    """A resolvable source position that can support an episode boundary."""

    anchor_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    locator: EvidenceLocator
    support_type: Literal[
        "sample",
        "frame",
        "transcript_edge",
        "source_edge",
        "transition",
        "capture_gap_edge",
        "user_edit",
    ]
    earliest_at: datetime
    latest_at: datetime
    source_position: Optional[str | int] = None

    @model_validator(mode="after")
    def validate_window(self) -> "EvidenceAnchor":
        if _utc(self.latest_at) < _utc(self.earliest_at):
            raise ValueError("evidence anchor latest_at must not precede earliest_at")
        return self


class ResolvedBoundarySupport(BaseModel):
    """Publication-time proof that one boundary anchor still resolves."""

    anchor_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    locator: EvidenceLocator
    support_type: Literal[
        "sample",
        "frame",
        "transcript_edge",
        "source_edge",
        "transition",
        "capture_gap_edge",
        "user_edit",
    ]
    resolved_source_position: Optional[str | int] = None
    earliest_at: datetime
    latest_at: datetime
    resolved_at: datetime
    uncertainty_seconds: float = Field(default=0, ge=0)
    separation_artifact_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_resolution(self) -> "ResolvedBoundarySupport":
        if _utc(self.latest_at) < _utc(self.earliest_at):
            raise ValueError("boundary support latest_at must not precede earliest_at")
        if not (
            _utc(self.earliest_at) <= _utc(self.resolved_at) <= _utc(self.latest_at)
        ):
            raise ValueError("resolved_at must fall inside the boundary support window")
        return self


class TimelineEvidenceRef(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    source_id: Optional[str] = None
    source_item_id: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    role: EvidenceRole
    excerpt: Optional[str] = None
    content_hash: Optional[str] = None
    ephemeral: bool = False
    locator: EvidenceLocator
    start_boundary_support: list[ResolvedBoundarySupport] = Field(default_factory=list)
    end_boundary_support: list[ResolvedBoundarySupport] = Field(default_factory=list)
    # Assembly-time metadata (``conversation_id``, image content type, app/window
    # identity). Persisted so an episode can link back to the artifact it cites.
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_boundary_support(self) -> "TimelineEvidenceRef":
        for support in self.start_boundary_support + self.end_boundary_support:
            if support.evidence_id != self.evidence_id:
                raise ValueError("boundary support must name its containing evidence")
            if support.locator != self.locator:
                raise ValueError("boundary support locator must match its evidence")
        return self


class TimelineAssertion(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    role: EvidenceRole
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class TimelineAudioRange(AudioRangeRef):
    """Immutable audio-document references supporting one episode interval.

    Chunk ids and absolute bounds remain valid when an operational conversation is
    split, merged, or silence-trimmed. ``conversation_ids`` is lineage/debug context;
    consumers must not use it as the range's identity.
    """

    source_stream: Optional[str] = None
    conversation_ids: list[str] = Field(default_factory=list)


def _utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; compare everything in UTC."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def clip_audio_ranges(
    ranges: Sequence[TimelineAudioRange],
    start: datetime,
    end: datetime,
    chunk_spans: Mapping[str, Optional[tuple[datetime, float]]],
    *,
    keep_unplaceable: bool,
) -> list[TimelineAudioRange]:
    """The audio claim of ``ranges`` restricted to ``[start, end)``.

    An episode's ranges are its claim over real audio, so cutting the episode has to
    cut the claim too. Splitting used to deep-copy every range onto both halves, which
    made each half claim the whole original recording — both sides played the same
    audio, and each reported the other's recordings as its own.

    ``chunk_spans`` maps a chunk id to its ``(captured_at, duration)``; the caller
    supplies it so this stays a pure function. A chunk whose span is unknown — 3% of
    this deployment's chunks predate ``captured_at`` — cannot be placed on either side
    of the cut, so ``keep_unplaceable`` decides where it goes. Callers give it to the
    surviving head only: dropping it would lose a reference that a later
    ``captured_at`` backfill could still resolve, and putting it on both sides would
    reinstate exactly the double-claim being fixed.
    """

    start, end = _utc(start), _utc(end)
    clipped: list[TimelineAudioRange] = []

    for audio_range in ranges:
        range_start = max(_utc(audio_range.started_at), start)
        range_end = min(_utc(audio_range.ended_at), end)
        if range_end <= range_start:
            continue

        chunk_ids = []
        for chunk_id in audio_range.chunk_ids:
            span = chunk_spans.get(chunk_id)
            if span is None:
                if keep_unplaceable:
                    chunk_ids.append(chunk_id)
                continue
            captured_at, duration = span
            captured_at = _utc(captured_at)
            if (
                captured_at < range_end
                and captured_at + timedelta(seconds=duration) > range_start
            ):
                chunk_ids.append(chunk_id)

        # ``chunk_ids`` is min_length=1: a window covering none of this range's audio
        # is not a claim at all, so the range is dropped rather than emptied.
        if not chunk_ids:
            continue

        clipped.append(
            audio_range.model_copy(
                update={
                    "chunk_ids": chunk_ids,
                    "started_at": range_start,
                    "ended_at": range_end,
                }
            )
        )

    return clipped


def merge_audio_ranges(
    groups: Iterable[Sequence[TimelineAudioRange]],
) -> list[TimelineAudioRange]:
    """The union of several episodes' audio claims, in wall-clock order.

    Merging used to union evidence, entities and conversation ids while leaving
    ``audio_ranges`` untouched, so the survivor kept only its own audio and every
    absorbed episode's went to the grave with its document — a merged episode covering
    three recordings could play one.

    Ranges are identified by ``range_id`` and are immutable, so a union deduplicated on
    that id needs no interval arithmetic: two episodes citing the same range cite the
    same audio.
    """

    merged: dict[str, TimelineAudioRange] = {}
    for group in groups:
        for audio_range in group:
            merged.setdefault(audio_range.range_id, audio_range)
    return sorted(merged.values(), key=lambda item: _utc(item.started_at))


class AudioEvidenceSpan(Document):
    """Compact profile for one assembled ScreenPipe compute span.

    Thirty-second source files remain transport units. This document is the durable
    compute unit and stores parallel 10-second series so downstream windows can slice
    the profile without duplicating per-bucket documents.
    """

    user_id: str
    source_id: str
    locator: EvidenceLocator
    source_item_ids: list[str]
    first_source_item_id: str
    last_source_item_id: str
    source_range_hash: str
    started_at: datetime
    ended_at: datetime
    direction: Literal["input", "output", "unknown"] = "unknown"
    meeting_id: Optional[str] = None
    conversation_id: Optional[str] = None
    audio_ranges: list[AudioRangeRef] = Field(default_factory=list)
    state: Literal["transcribed", "no_speech", "unscored", "failed", "abandoned"]
    # Ingest attempts for this range. A failed upload leaves the staged chunks in
    # place so the next tick can retry; without a bound that retry is forever, and
    # each pass leaks another Conversation holding a full copy of the audio.
    attempts: int = Field(default=0, ge=0)
    covered_seconds: float = Field(ge=0)
    missing_seconds: float = Field(ge=0)
    bucket_seconds: float = Field(default=10.0, gt=0)
    coverage_fraction: list[float] = Field(default_factory=list)
    speech_fraction: list[Optional[float]] = Field(default_factory=list)
    acoustic_active_fraction: list[float] = Field(default_factory=list)
    rms_dbfs: list[Optional[float]] = Field(default_factory=list)
    peak_dbfs: list[Optional[float]] = Field(default_factory=list)
    speech_seconds: Optional[float] = Field(default=None, ge=0)
    longest_no_speech_seconds: Optional[float] = Field(default=None, ge=0)
    acoustic_active_seconds: float = Field(default=0, ge=0)
    acoustic_quiet_seconds: float = Field(default=0, ge=0)
    word_count: Optional[int] = Field(default=None, ge=0)
    analysis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_series(self) -> "AudioEvidenceSpan":
        if self.locator.capture_source_id != self.source_id:
            raise ValueError("audio evidence locator must match its capture source")
        if self.locator.modality != "audio":
            raise ValueError("audio evidence locator modality must be audio")
        if not self.locator.track_id:
            raise ValueError("audio evidence locator requires a track id")
        if self.ended_at <= self.started_at:
            raise ValueError("audio evidence span must have positive duration")
        lengths = {
            len(self.coverage_fraction),
            len(self.speech_fraction),
            len(self.acoustic_active_fraction),
            len(self.rms_dbfs),
            len(self.peak_dbfs),
        }
        if len(lengths) != 1:
            raise ValueError("audio evidence bucket series must have equal lengths")
        return self

    class Settings:
        name = "audio_evidence_spans"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("source_id", ASCENDING),
                    ("direction", ASCENDING),
                    ("locator.track_id", ASCENDING),
                    ("first_source_item_id", ASCENDING),
                    ("last_source_item_id", ASCENDING),
                    ("started_at", ASCENDING),
                    ("ended_at", ASCENDING),
                ],
                unique=True,
                name="audio_evidence_source_time_range",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("started_at", ASCENDING),
                    ("ended_at", ASCENDING),
                ]
            ),
        ]


class TimelineAnalysisRun(Document):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    local_date: date
    timezone: str
    day_started_at: datetime
    day_ended_at: datetime
    evidence_revision: str
    processed_evidence_revision: Optional[str] = None
    prompt_version: str
    executor: str
    state: Literal[
        "pending",
        "preparing",
        "awaiting_evidence",
        "running",
        "validating",
        "complete",
        "failed",
        "quota_deferred",
    ] = "pending"
    claimed_at: Optional[datetime] = None
    retry_after: Optional[datetime] = None
    attempts: int = 0
    coverage_window_ids: list[str] = Field(default_factory=list)
    output_episode_ids: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None

    class Settings:
        name = "timeline_analysis_runs"
        indexes = [
            IndexModel([("run_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", ASCENDING),
                    ("timezone", ASCENDING),
                    ("evidence_revision", ASCENDING),
                    ("prompt_version", ASCENDING),
                ],
                unique=True,
                name="timeline_analysis_revision",
            ),
            IndexModel([("state", ASCENDING), ("created_at", ASCENDING)]),
        ]


class EpisodeRevisionRef(BaseModel):
    """Exact active revision of one durable episode identity."""

    episode_key: str = Field(min_length=1)
    revision: int = Field(ge=0)


class TimelineEpisode(Document):
    episode_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Durable identity across analysis runs. ``episode_id`` names this row; a pinned
    # episode keeps its ``episode_key`` when it is carried into the next generation.
    episode_key: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    user_id: str
    local_date: date
    timezone: str
    started_at: datetime
    ended_at: datetime
    kind: str
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=1200)
    detailed_summary: Optional[str] = None
    detailed_summary_scope_hash: Optional[str] = None
    detailed_summary_revision: Optional[int] = Field(default=None, ge=0)
    detailed_summary_generated_at: Optional[datetime] = None
    # People actually talked with each other here. Promotes the cited capture-evidence
    # recordings back into the user-facing Recordings list and search — see
    # services/timeline/dispatch.py. Factual; ``salience`` carries importance.
    conversational: bool = False
    # Lifecycle and human ownership are orthogonal: ``confirmed_fields`` owns fields;
    # it never creates a separate lifecycle status.
    status: Literal["open", "provisional", "settled", "superseded"] = "provisional"
    # Monotonic per-``episode_key`` revision counter and the evidence revision the
    # staged rolling publication reconciled.
    revision: int = Field(default=0, ge=0)
    evidence_revision: Optional[int] = None
    # Exact content-addressed stage interactions plus deterministic result digests.
    # Artifact hashes resolve under their operation namespaces; result hashes remain
    # distinct because they describe validated semantic content, not an inference run.
    separation_inference_operation: Optional[str] = None
    separation_request_hash: Optional[str] = None
    separation_artifact_hash: Optional[str] = None
    separation_result_hash: Optional[str] = None
    interpretation_inference_operation: Optional[str] = None
    interpretation_request_hash: Optional[str] = None
    interpretation_artifact_hash: Optional[str] = None
    interpretation_result_hash: Optional[str] = None
    # Lineage across split/merge: the keys this episode was derived from and the keys
    # that replaced it. A superseded key resolves through ``successor_keys``.
    predecessor_keys: list[str] = Field(default_factory=list)
    # Exact direct inputs for identity-changing operations. Key-only ancestry is kept
    # for stable-key navigation, but cannot identify which immutable revision was
    # split or merged.
    predecessor_revisions: list[EpisodeRevisionRef] = Field(default_factory=list)
    successor_keys: list[str] = Field(default_factory=list)
    # Human pinning, orthogonal to settlement. ``confirmed_fields`` lists the pinned
    # fields for both pipelines.
    pinned: bool = False
    # Vault semantics are independent from Timeline visibility. ``auto`` keeps ordinary
    # episodes eligible for semantic memory but treats ``media`` kinds as reference-only;
    # ``remember`` is the person's explicit opt-in for media worth retaining.
    memory_policy: Literal["auto", "reference", "remember"] = "auto"
    salience: Literal["background", "routine", "notable", "highlight"] = "routine"
    confidence: float = Field(ge=0, le=1)
    activity_mode: Literal["foreground", "background", "ambient", "idle"]
    entities: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    assertions: list[TimelineAssertion] = Field(default_factory=list)
    evidence_refs: list[TimelineEvidenceRef] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    related_episode_ids: list[str] = Field(default_factory=list)
    related_conversation_ids: list[str] = Field(default_factory=list)
    # Authoritative playable audio. Unlike related_conversation_ids, these references
    # survive container replacement because Mongo chunk ids and captured_at are stable.
    audio_ranges: list[TimelineAudioRange] = Field(default_factory=list)
    parent_episode_id: Optional[str] = None
    representative_image: Optional[bytes] = None
    representative_image_type: Optional[str] = None
    # Frames sampled across this episode's own interval, fetched from the node's full
    # ScreenPipe store rather than inherited from whatever an observation happened to
    # shortlist. Transient: the picker keeps one as `representative_image` and clears
    # the rest. `thumbnail_state` drives the pass — "" not yet requested, "requested"
    # awaiting the node, "chosen"/"unavailable" terminal.
    frame_shortlist: list[dict[str, Any]] = Field(default_factory=list)
    thumbnail_state: Literal["", "requested", "chosen", "unavailable"] = ""
    # Human confirmation. A pinned episode is an anchor: later runs carry it forward
    # verbatim instead of regenerating its interval. ``confirmed_fields`` records which
    # fields the person owns, so agent-derived fields stay free to refresh.
    confirmed_at: Optional[datetime] = None
    confirmed_fields: list[str] = Field(default_factory=list)
    # Projection metadata only. Exact selection generations and per-change decisions
    # in MemoryReviewProposal are the authority for memory progress.
    memory_state: Literal["", "written", "partial", "skipped"] = ""
    vault_paths: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    revised_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_episode(self) -> "TimelineEpisode":
        if self.ended_at <= self.started_at:
            raise ValueError("timeline episode must have positive duration")
        known_ids = {ref.evidence_id for ref in self.evidence_refs}
        for assertion in self.assertions:
            if not set(assertion.evidence_ids).issubset(known_ids):
                raise ValueError("timeline assertion references unknown evidence")
        predecessor_refs = [
            (item.episode_key, item.revision) for item in self.predecessor_revisions
        ]
        if len(predecessor_refs) != len(set(predecessor_refs)):
            raise ValueError("timeline predecessor revisions must be unique")
        return self

    class Settings:
        name = "timeline_episodes"
        indexes = [
            IndexModel([("episode_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("local_date", DESCENDING)]),
            IndexModel([("run_id", ASCENDING), ("started_at", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("episode_key", ASCENDING)]),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("episode_key", ASCENDING),
                    ("revision", ASCENDING),
                ],
                unique=True,
                name="timeline_episode_revision_identity",
            ),
        ]


class TimelineInterpretationRejectionState(BaseModel):
    """Durable linkage from a rejected hypothesis to its bounded retry range."""

    hypothesis_id: str = Field(min_length=1, max_length=160)
    reason_code: Literal["incoherent", "mixed_activities", "insufficient_context"]
    explanation: str = Field(min_length=1, max_length=1000)
    implicated_evidence_ids: list[str] = Field(default_factory=list)
    retry_depth: int = Field(ge=1)
    successor_dirty_range_id: str = Field(min_length=1)
    status: Literal["retry_scheduled", "exhausted"]
    interpretation_result_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class DirtyEvidenceRangeResolution(BaseModel):
    """Append-only human resolution of one terminal reconciliation range."""

    resolution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: Literal["dismissed"] = "dismissed"
    actor_user_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    prior_state: Literal["failed"] = "failed"
    created_at: datetime = Field(default_factory=utcnow)


class DirtyEvidenceRange(Document):
    """An absolute evidence interval awaiting rolling reconciliation.

    Producers call ``services/timeline/dirty_ranges.mark_evidence_dirty``; overlapping
    or nearby ``pending``/``waiting`` rows coalesce. A ``leased`` row is never
    coalesced into: the lease snapshots ``evidence_revision`` into
    ``leased_evidence_revision`` and the run's publish fences on that snapshot, while
    new triggers open a fresh pending row over the same interval. Scheduling clocks
    live on the row: ``not_before`` is the debounce, ``force_after`` bounds how long
    continuous evidence can postpone a first look.
    """

    dirty_range_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    started_at: datetime
    ended_at: datetime
    # Per-user monotonic counter (Redis INCR, see redis_keys.timeline_evidence_revision)
    # recording the newest evidence change folded into this range.
    evidence_revision: int
    leased_evidence_revision: Optional[int] = None
    base_manifest_hash: Optional[str] = None
    # Producer revision ids folded in, keyed by source kind — observability + fencing.
    source_revisions: dict[str, list[str]] = Field(default_factory=dict)
    trigger_reasons: list[str] = Field(default_factory=list)
    not_before: datetime
    force_after: datetime
    state: Literal[
        "pending",
        "authorized_pending",
        "leased",
        "waiting",
        "awaiting_context",
        "context_pending",
        "completed",
        "failed",
        "dismissed",
        "superseded",
    ] = "pending"
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    attempts: int = Field(default=0, ge=0)
    last_error: Optional[str] = None
    # Only an explicit, Immich-ready day request may set this. The recovery scan
    # ignores ordinary producer-created ranges until a person authorizes them.
    dispatch_authorized_at: Optional[datetime] = None
    reconciliation_request_id: Optional[str] = None
    authorized_started_at: Optional[datetime] = None
    authorized_ended_at: Optional[datetime] = None
    parent_dirty_range_id: Optional[str] = None
    superseded_by_dirty_range_id: Optional[str] = None
    context_request_id: Optional[str] = None
    context_requests: list["TimelineContextRequestState"] = Field(
        default_factory=list, max_length=8
    )
    rejection_retry_depth: int = Field(default=0, ge=0)
    rejection_hypothesis_id: Optional[str] = None
    rejection_reason_code: Optional[
        Literal["incoherent", "mixed_activities", "insufficient_context"]
    ] = None
    rejection_evidence_ids: list[str] = Field(default_factory=list)
    interpretation_rejections: list[TimelineInterpretationRejectionState] = Field(
        default_factory=list, max_length=16
    )
    separation_result_hash: Optional[str] = None
    interpretation_result_hash: Optional[str] = None
    separation_inference_operation: Optional[str] = None
    separation_request_hash: Optional[str] = None
    separation_artifact_hash: Optional[str] = None
    interpretation_inference_operation: Optional[str] = None
    interpretation_request_hash: Optional[str] = None
    interpretation_artifact_hash: Optional[str] = None
    published_snapshot_ids: list[str] = Field(default_factory=list)
    resolution_history: list[DirtyEvidenceRangeResolution] = Field(
        default_factory=list, max_length=16
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_range(self) -> "DirtyEvidenceRange":
        if self.ended_at <= self.started_at:
            raise ValueError("dirty evidence range must have positive duration")
        authorized = self.dispatch_authorized_at is not None
        if authorized:
            if not self.reconciliation_request_id:
                raise ValueError(
                    "authorized dirty range requires a reconciliation request"
                )
            if self.authorized_started_at is None or self.authorized_ended_at is None:
                raise ValueError("authorized dirty range requires immutable bounds")
            if self.authorized_ended_at <= self.authorized_started_at:
                raise ValueError("authorized dirty bounds must have positive duration")
            if self.started_at != self.authorized_started_at:
                raise ValueError("authorized dirty range start is immutable")
            if self.ended_at != self.authorized_ended_at:
                raise ValueError("authorized dirty range end is immutable")
        elif (
            self.authorized_started_at is not None
            or self.authorized_ended_at is not None
        ):
            raise ValueError("unauthorized dirty range cannot carry authorized bounds")
        if self.state == "context_pending" and not self.context_request_id:
            raise ValueError("context-pending range requires a context request")
        if self.state == "dismissed" and not self.resolution_history:
            raise ValueError("dismissed range requires an audited resolution")
        return self

    class Settings:
        name = "dirty_evidence_ranges"
        indexes = [
            IndexModel([("dirty_range_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("state", ASCENDING),
                    ("not_before", ASCENDING),
                ],
                name="dirty_range_schedule",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("started_at", ASCENDING),
                    ("ended_at", ASCENDING),
                ],
                name="dirty_range_overlap",
            ),
            IndexModel(
                [("state", ASCENDING), ("lease_expires_at", ASCENDING)],
                name="dirty_range_lease_recovery",
            ),
            IndexModel(
                [
                    ("state", ASCENDING),
                    ("dispatch_authorized_at", ASCENDING),
                    ("not_before", ASCENDING),
                ],
                name="dirty_range_explicit_dispatch",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("context_request_id", ASCENDING)],
                unique=True,
                partialFilterExpression={"context_request_id": {"$type": "string"}},
                name="dirty_range_context_successor",
            ),
        ]


class TimelineContextRequestState(BaseModel):
    """Durable state for one bounded evidence-acquisition request."""

    context_request_id: str = Field(min_length=1)
    hypothesis_id: Optional[str] = None
    stage: Literal["separation", "interpretation"]
    locator: EvidenceLocator
    started_at: datetime
    ended_at: datetime
    base_manifest_hash: str = Field(min_length=1)
    leased_evidence_revision: int = Field(ge=0)
    target_resolution: str = Field(min_length=1)
    max_items: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    status: Literal[
        "queued",
        "awaiting",
        "ready_to_refence",
        "complete",
        "failed",
        "superseded",
    ] = "queued"
    device_input_job_ids: list[str] = Field(default_factory=list, max_length=16)
    result_evidence_ids: list[str] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
    newest_evidence_revision: Optional[int] = Field(default=None, ge=0)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_request(self) -> "TimelineContextRequestState":
        if self.ended_at <= self.started_at:
            raise ValueError("context request must have positive duration")
        return self


DirtyEvidenceRange.model_rebuild()


class ImmichVisualPreparationStatus(BaseModel):
    """What Chronicle could understand from the ready day's selected photos."""

    state: Literal["pending", "running", "not_needed", "complete", "partial", "failed"]
    candidate_count: int = Field(default=0, ge=0)
    analyzed_count: int = Field(default=0, ge=0)
    newly_analyzed_count: int = Field(default=0, ge=0)
    helpful_count: int = Field(default=0, ge=0)
    unhelpful_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    uninspected_count: int = Field(default=0, ge=0)
    exploration_rounds: int = Field(default=0, ge=0)
    stop_reason: str = ""
    artifact_id: str = ""


class ImmichEvidenceWindow(BaseModel):
    started_at: datetime
    ended_at: datetime
    asset_count: int = Field(default=0, ge=0)
    helpful_asset_count: int = Field(default=0, ge=0)


class ImmichEvidenceSummary(BaseModel):
    """Actual Immich evidence present in the manifest inspected by Timeline."""

    evidence_count: int = Field(default=0, ge=0)
    helpful_evidence_count: int = Field(default=0, ge=0)
    window_count: int = Field(default=0, ge=0)
    windows: list[ImmichEvidenceWindow] = Field(default_factory=list)


class TimelineReconciliationRequest(Document):
    """Durable status for one explicit local-day reconciliation request."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    local_date: date
    timezone: str
    state: Literal["blocked", "queued", "running", "completed", "failed"]
    reason: Literal[
        "assets_on_day",
        "later_asset_watermark",
        "no_immich_evidence",
        "immich_unconfigured",
        "immich_unreachable",
        "user_bypassed_immich",
    ]
    target_asset_count: int = Field(default=0, ge=0)
    latest_asset_local_date: Optional[date] = None
    checked_at: datetime = Field(default_factory=utcnow)
    notification_id: Optional[str] = None
    notification_status: Optional[str] = None
    job_id: Optional[str] = None
    dirty_range_id: Optional[str] = None
    run_id: Optional[str] = None
    immich_visual: Optional[ImmichVisualPreparationStatus] = None
    immich_evidence: Optional[ImmichEvidenceSummary] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "timeline_reconciliation_requests"
        indexes = [
            IndexModel([("request_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", DESCENDING),
                    ("created_at", DESCENDING),
                ],
                name="timeline_reconciliation_day_history",
            ),
        ]


class EpisodeDispatchLatch(Document):
    """Claim for one classified Timeline side effect.

    Inline completion events retain the row after success. Detailed-summary rows are
    renewable enqueue leases and are removed by the exact worker attempt after it
    finishes; the materialized episode scope hash, not this row, proves completion.
    """

    user_id: str
    episode_key: str
    event_type: str
    episode_id: str
    revision: int
    claim_token: str = Field(min_length=1)
    dispatched_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "episode_dispatch_latches"
        indexes = [
            IndexModel(
                [("episode_key", ASCENDING), ("event_type", ASCENDING)],
                unique=True,
                name="episode_dispatch_identity",
            ),
            IndexModel([("user_id", ASCENDING), ("dispatched_at", DESCENDING)]),
        ]


class PotentialMemoryChange(BaseModel):
    """One vault mutation proposed by a reviewed Timeline day.

    The full before/after text is durable review evidence. ``before_hash`` is also the
    apply-time fence: a proposal is never merged into a note that changed after the
    memory agent read it.
    """

    change_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    note_path: str = Field(min_length=1)
    operation: Literal["create", "update", "delete"]
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None
    before_text: Optional[str] = None
    after_text: Optional[str] = None
    summary: str = ""
    source_episode_keys: list[str] = Field(default_factory=list)
    source_evidence_keys: list[str] = Field(default_factory=list)
    source_session_keys: list[str] = Field(default_factory=list)


class MemoryFreshnessResult(BaseModel):
    verdict: Literal["unaffected", "affected", "uncertain"]
    reason: str = Field(min_length=1, max_length=4000)
    relevant_paths: list[str] = Field(default_factory=list)


class MemoryReviewProposal(Document):
    """One immutable diff generation for a human-authorized episode selection.

    request_id survives regeneration; proposal_id identifies the exact diff accepted.
    Terminal generations remain audit evidence. Explicit draft feedback can reuse
    a prior account only while its source scope still matches and is revalidated.
    """

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str
    generation: int = Field(default=1, ge=1)
    user_id: str
    local_date: Optional[date]
    memory_space_id: Optional[str] = None
    source_kind: Literal["timeline", "undated"] = "timeline"
    recording_id: Optional[str] = None
    accepted_context: dict[str, Any] = Field(default_factory=dict)
    refresh_assessment: Optional[dict[str, Any]] = None
    timezone: str
    snapshot_id: str
    selected_episodes: list[EpisodeRevisionRef] = Field(default_factory=list)
    selection_hash: str
    selected_tokens: list[str] = Field(min_length=1)
    active: bool = True
    source_digest: str = ""
    group_revisions: list["TimelineSemanticGroupRevision"] = Field(default_factory=list)
    source_scope: list[dict[str, Any]] = Field(default_factory=list)
    excluded_source_keys: list[str] = Field(default_factory=list)
    session_key: Optional[str] = None
    session_revision: Optional[int] = None
    session_owner_date: Optional[date] = None
    source_scope_hash: str = ""
    source_checked_at: Optional[datetime] = None
    priority: int = 0
    job_id: Optional[str] = None
    attempts: int = 0
    stage: str = "queued"
    completed_sources: int = 0
    total_sources: int = 0
    investigation_activity: dict[str, Any] = Field(default_factory=dict)
    failure_kind: Optional[str] = None
    questions: list[str] = Field(default_factory=list)
    inference_runs: list[dict[str, Any]] = Field(default_factory=list)
    writer_inference_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    account: Optional[dict[str, Any]] = None
    revision_feedback: Optional[str] = Field(
        default=None, min_length=1, max_length=4000
    )
    supersedes_proposal_id: Optional[str] = None
    replacement_proposal_id: Optional[str] = None
    replacement_feedback: Optional[str] = Field(
        default=None, min_length=1, max_length=4000
    )
    corrected_by_proposal_id: Optional[str] = None
    correction_of: list[str] = Field(default_factory=list)
    withdrawn: bool = False
    correction_episode_keys: list[str] = Field(default_factory=list)
    vault_base_hash: Optional[str] = None
    checked_vault_hash: Optional[str] = None
    freshness: Optional[MemoryFreshnessResult] = None
    freshness_checked_at: Optional[datetime] = None
    freshness_vault_hash: Optional[str] = None
    state: Literal[
        "queued",
        "generating",
        "pending",
        "checking",
        "applying",
        "applied",
        "rejected",
        "no_changes",
        "excluded",
        "stale",
        "failed",
        "regenerating",
        "correction_required",
        "corrected",
        "needs_attention",
        "deferred",
        "paused",
    ] = "queued"
    changes: list[PotentialMemoryChange] = Field(default_factory=list)
    accepted_change_ids: list[str] = Field(default_factory=list)
    rejected_change_ids: list[str] = Field(default_factory=list)
    requested_change_ids: list[str] = Field(default_factory=list)
    applied_change_ids: list[str] = Field(default_factory=list)
    audited_change_ids: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    generated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_source_selection(self):
        if self.source_kind == "undated":
            if (
                self.local_date is not None
                or self.selected_episodes
                or not self.recording_id
                or not self.session_key
                or not self.session_revision
            ):
                raise ValueError(
                    "Undated selections require a recording and session revision, without a fabricated day or episode"
                )
        elif self.local_date is None or not self.selected_episodes:
            raise ValueError("Timeline selections require a date and episode revisions")
        return self

    class Settings:
        name = "memory_review_proposals"
        indexes = [
            IndexModel([("proposal_id", ASCENDING)], unique=True),
            IndexModel(
                [("request_id", ASCENDING), ("generation", ASCENDING)], unique=True
            ),
            IndexModel(
                [("user_id", ASCENDING), ("selected_tokens", ASCENDING)],
                unique=True,
                partialFilterExpression={"active": True},
                name="memory_review_active_episode_selection",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("state", ASCENDING),
                    ("created_at", ASCENDING),
                ]
            ),
        ]


class TimelineReviewDecision(BaseModel):
    """Append-only training evidence from one human Timeline action."""

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    action: Literal[
        "grouping_accept",
        "grouping_reject",
        "grouping_remove",
        "episode_update",
        "episode_structure_confirm",
        "episode_split",
        "episode_merge",
        "episode_delete",
        "episode_not_activity",
        "episode_coverage_only",
        "session_organized",
    ]
    episode_ids: list[str] = Field(default_factory=list)
    suggestion_id: Optional[str] = None
    model: Optional[str] = None
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class GroupRevisionRef(BaseModel):
    """Exact accepted semantic-group revision owned by a local day."""

    owner_local_date: date
    group_key: str = Field(min_length=1)
    revision: int = Field(ge=1)


class TimelineSemanticGroupRevision(BaseModel):
    """Immutable, snapshot-scoped ``same_activity`` relationship claim."""

    group_key: str = Field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = Field(default=1, ge=1)
    relation_type: Literal["same_activity"] = "same_activity"
    member_revisions: list[EpisodeRevisionRef] = Field(min_length=1)
    # Episode ids are display locators only. Exact membership is the tuple above.
    episode_ids: list[str] = Field(min_length=1)
    origin: Literal["human", "automatic"] = "human"
    questions: list[str] = Field(default_factory=list)
    source_snapshot_id: str = Field(min_length=64, max_length=64)
    predecessor_revisions: list[GroupRevisionRef] = Field(default_factory=list)
    status: Literal["active", "tombstone"] = "active"
    title: str = Field(default="", max_length=160)
    summary: str = Field(default="", max_length=1200)
    started_at: datetime
    ended_at: datetime
    suggestion_id: Optional[str] = None
    reason: str = Field(default="", max_length=500)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    model: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_revision(self) -> "TimelineSemanticGroupRevision":
        members = [(item.episode_key, item.revision) for item in self.member_revisions]
        if len(members) != len(set(members)):
            raise ValueError("semantic group member revisions must be unique")
        if len(self.episode_ids) != len(self.member_revisions):
            raise ValueError("semantic group display ids must match exact members")
        if self.ended_at < self.started_at:
            raise ValueError("semantic group cannot end before it starts")
        if self.status == "active" and (not self.title or not self.summary):
            raise ValueError("an active semantic group requires an account")
        return self


class TimelineDaySnapshot(BaseModel):
    """Content-addressed database/UI projection for one rolling local day."""

    schema_version: Literal["timeline-day-snapshot-v1"] = "timeline-day-snapshot-v1"
    snapshot_id: str = Field(min_length=64, max_length=64)
    episode_revisions: list[EpisodeRevisionRef] = Field(default_factory=list)
    semantic_group_revisions: list[GroupRevisionRef] = Field(default_factory=list)
    evidence_state_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_revision_identities(self) -> "TimelineDaySnapshot":
        episode_keys = [item.episode_key for item in self.episode_revisions]
        if len(episode_keys) != len(set(episode_keys)):
            raise ValueError("day snapshot episode keys must be unique")
        group_keys = [
            (item.owner_local_date, item.group_key)
            for item in self.semantic_group_revisions
        ]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError("day snapshot semantic group keys must be unique")
        return self


class TimelinePublicationDayPlan(BaseModel):
    """One day compare-and-swap carried by a publication journal."""

    local_date: date
    timezone: str
    base_snapshot_id: Optional[str] = None
    resulting_snapshot: TimelineDaySnapshot
    review_decision: Optional[TimelineReviewDecision] = None


class TimelinePublicationOperation(BaseModel):
    """One idempotent graph mutation in journal execution order."""

    operation_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    kind: Literal[
        "insert_episode_revision",
        "supersede_episode_revision",
        "insert_group_revision",
        "tombstone_group_revision",
        "upsert_rejected_reconciliation_retry",
    ]
    expected_revision: Optional[int] = Field(default=None, ge=0)
    payload: dict[str, Any]
    state: Literal["pending", "applied"] = "pending"
    applied_at: Optional[datetime] = None


class TimelinePublicationEvidenceFence(BaseModel):
    """Durable evidence generation that an agent publication is allowed to commit."""

    dirty_range_id: str = Field(min_length=1)
    lease_owner: str = Field(min_length=1)
    lease_attempt: int = Field(ge=1)
    leased_evidence_revision: int = Field(ge=0)
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def validate_bounds(self) -> "TimelinePublicationEvidenceFence":
        if self.ended_at <= self.started_at:
            raise ValueError("publication evidence fence must have positive duration")
        return self


class TimelinePublicationJournal(Document):
    """Durable roll-forward intent for one cross-document Timeline publication."""

    schema_version: Literal["timeline-publication-v1"] = "timeline-publication-v1"
    publication_id: str = Field(min_length=64, max_length=64)
    intent_hash: str = Field(min_length=64, max_length=64)
    user_id: str
    operation_source: Literal["agent", "manual", "semantic_group", "projection"]
    evidence_fence: Optional[TimelinePublicationEvidenceFence] = None
    affected_days: list[TimelinePublicationDayPlan] = Field(default_factory=list)
    operations: list[TimelinePublicationOperation] = Field(default_factory=list)
    status: Literal[
        "prepared",
        "days_dirty",
        "applying",
        "snapshots_installed",
        "committed",
        "conflict",
    ] = "prepared"
    errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    committed_at: Optional[datetime] = None
    dispatch_pending: bool = True
    dispatch_completed_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_plan(self) -> "TimelinePublicationJournal":
        day_keys = [(item.local_date, item.timezone) for item in self.affected_days]
        if len(day_keys) != len(set(day_keys)):
            raise ValueError("publication affected days must be unique")
        sequences = [item.sequence for item in self.operations]
        if sequences != list(range(len(sequences))):
            raise ValueError("publication operation sequences must be contiguous")
        operation_ids = [item.operation_id for item in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("publication operation ids must be unique")
        phase = {
            "insert_episode_revision": 0,
            "insert_group_revision": 0,
            "supersede_episode_revision": 1,
            "tombstone_group_revision": 1,
            "upsert_rejected_reconciliation_retry": 2,
        }
        phases = [phase[item.kind] for item in self.operations]
        if phases != sorted(phases):
            raise ValueError("publication inserts must precede supersessions")
        return self

    class Settings:
        name = "timeline_publication_journals"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("publication_id", ASCENDING)],
                unique=True,
                name="timeline_publication_identity",
            ),
            IndexModel(
                [("status", ASCENDING), ("updated_at", ASCENDING)],
                name="timeline_publication_recovery",
            ),
            IndexModel(
                [
                    ("status", ASCENDING),
                    ("dispatch_pending", ASCENDING),
                    ("committed_at", ASCENDING),
                ],
                name="timeline_publication_dispatch_recovery",
            ),
        ]


class TimelineDay(Document):
    user_id: str
    local_date: date
    timezone: str
    coverage: dict[str, Any] = Field(default_factory=dict)
    current_snapshot: Optional[TimelineDaySnapshot] = None
    current_snapshot_id: Optional[str] = None
    reviewed_snapshot_id: Optional[str] = None
    applied_snapshot_id: Optional[str] = None
    snapshot_state: Literal[
        "dirty", "ready", "reviewed", "applied", "correction_required"
    ] = "dirty"
    pending_publication_id: Optional[str] = None
    # Whole-day structural review is independent from selective memory requests.
    # MemoryReviewProposal owns exact reviewed selections and their immutable decisions;
    # none of these day-level projection fields authorizes a vault mutation.
    review_state: Literal[
        "episodes_pending",
        "memory_queued",
        "memory_generating",
        "memory_pending",
        "memory_applying",
        "finalized",
        "failed",
    ] = "episodes_pending"
    review_snapshot_id: Optional[str] = None
    memory_review_proposal_id: Optional[str] = None
    episodes_reviewed_at: Optional[datetime] = None
    review_resolved_at: Optional[datetime] = None
    review_outcome: Optional[Literal["applied", "rejected", "no_changes"]] = None
    review_error: Optional[str] = None
    # Disposable, run-fenced second opinion over the published episode set.
    consolidation_state: Literal[
        "", "queued", "generating", "ready", "resolved", "failed"
    ] = ""
    consolidation_snapshot_id: Optional[str] = None
    consolidation_model: Optional[str] = None
    consolidation_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    consolidation_error: Optional[str] = None
    consolidation_started_at: Optional[datetime] = None
    consolidation_generated_at: Optional[datetime] = None
    consolidation_resolved_at: Optional[datetime] = None
    # Append-only relationship store. Current membership is resolved
    # exclusively through ``current_snapshot.semantic_group_revisions``.
    semantic_group_history: list[TimelineSemanticGroupRevision] = Field(
        default_factory=list
    )
    review_decisions: list[TimelineReviewDecision] = Field(default_factory=list)
    revised_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_semantic_groups(cls, values: Any) -> Any:
        if isinstance(values, Mapping) and "semantic_groups" in values:
            raise ValueError(
                "TimelineDay.semantic_groups was removed; use immutable group "
                "history and current snapshot revision refs"
            )
        return values

    @model_validator(mode="after")
    def validate_snapshot_state(self) -> "TimelineDay":
        if (self.current_snapshot is None) != (self.current_snapshot_id is None):
            raise ValueError("current snapshot and pointer must be present together")
        if (
            self.current_snapshot is not None
            and self.current_snapshot_id != self.current_snapshot.snapshot_id
        ):
            raise ValueError("current snapshot pointer must match embedded snapshot")
        if self.pending_publication_id is not None and self.snapshot_state != "dirty":
            raise ValueError("a pending publication requires a dirty day")
        if (
            self.reviewed_snapshot_id is not None
            and self.snapshot_state != "dirty"
            and self.reviewed_snapshot_id != self.current_snapshot_id
        ):
            raise ValueError("reviewed snapshot must be the current snapshot")
        return self

    class Settings:
        name = "timeline_days"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("semantic_group_history.episode_ids", ASCENDING),
                ]
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", ASCENDING),
                    ("timezone", ASCENDING),
                ],
                unique=True,
                name="timeline_day_identity",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("review_state", ASCENDING),
                    ("local_date", ASCENDING),
                ],
                name="timeline_day_review_queue",
            ),
        ]
