"""One source/time policy for capture processing and derived-content visibility.

Uncovered time on an activated source is pending, never implicitly allowed.
Overrides are separate from detector records and survive replay/model changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from bisect import bisect_left, bisect_right
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

import backend.database as database_module
import backend.services.timeline.dirty_ranges as dirty_ranges
from backend.redis_keys import timeline_publication_lock
from backend.services.redis_lock import distributed_lock

STATES = Literal["pending", "allowed", "excluded", "needs_review"]
_processing_owner = ContextVar("privacy_processing_owner", default=None)

# Retain every provenance field used by permits_record in metadata-only reads.
_RECORD_PROJECTION = {
    key: 1
    for key in (
        "conversation_id",
        "episode_id",
        "source_item_id",
        "user_id",
        "audio_ranges",
        "evidence_refs",
        "source_id",
        "client_id",
        "locator",
        "captured_at",
        "started_at",
        "created_at",
        "ended_at",
        "source_ids",
        "metadata.speaker_recognition.enabled",
        "metadata.speaker_recognition.privacy_gallery_receipt",
        "metadata.speaker_recognition.privacy_reference_receipt",
        "privacy_reference_receipt",
        "metadata.privacy_reference_receipt",
        "raw_response.privacy_reference_receipt",
        "transcript_versions.metadata.privacy_reference_receipt",
        "transcript_versions.metadata.speaker_recognition.enabled",
        "transcript_versions.metadata.speaker_recognition.privacy_gallery_receipt",
        "transcript_versions.metadata.speaker_recognition.privacy_reference_receipt",
    )
}


def processing_owner(default):
    """Staged vault paths are storage identities, never privacy identities."""
    return _processing_owner.get() or default


@asynccontextmanager
async def processing_scope(user_id, payload):
    snapshot = await guard_payload(user_id, payload)
    token = _processing_owner.set(str(user_id))
    try:
        yield snapshot
        await assert_current(user_id, snapshot)
    finally:
        _processing_owner.reset(token)


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return (
        value.replace(microsecond=value.microsecond // 1000 * 1000)
        if value.microsecond % 1000
        else value
    )


class PrivacyHeld(RuntimeError):
    def __init__(self):
        super().__init__("Private or unscreened evidence is held from processing")


async def held_response(_request, _error):
    return JSONResponse(
        {"detail": "Private or unscreened evidence is held from processing"},
        status_code=423,
    )


class PrivacySegment(BaseModel):
    started_at: datetime
    ended_at: datetime
    state: STATES
    coverage: Literal["sampled", "verified", "unverified"]
    reason: Literal["missing_frame", "screening_failed", "capture_gap"] | None = None

    @model_validator(mode="after")
    def failure_is_held(self):
        if self.coverage == "unverified" and self.state != "pending":
            raise ValueError("unverified coverage must remain pending")
        if self.reason and (self.state != "pending" or self.coverage != "unverified"):
            raise ValueError("screening failures must remain unverified and pending")
        return self


class ScreeningEvidence(BaseModel):
    frame_id: int
    captured_at: datetime
    state: STATES
    score: float | None = Field(default=None, ge=0, le=1)
    input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: Literal[
        "adult_site_or_search",
        "nsfw_content",
        "none",
        "missing_frame",
        "screening_failed",
    ] = "none"

    @model_validator(mode="after")
    def prediction_or_failure(self):
        failed = self.reason in {"missing_frame", "screening_failed"}
        if failed:
            if (
                self.state != "pending"
                or self.input_hash is not None
                or self.score is not None
            ):
                raise ValueError("failed screening cannot claim a prediction")
        elif self.input_hash is None or self.score is None:
            raise ValueError("screening predictions require input identity and score")
        return self


class ScreeningResult(BaseModel):
    interval_id: str = Field(min_length=1, max_length=256)
    track_id: str = Field(min_length=1, max_length=256)
    started_at: datetime
    ended_at: datetime
    model_version: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=64)
    segments: list[PrivacySegment] = Field(min_length=1, max_length=1001)
    evidence: list[ScreeningEvidence] = Field(min_length=1, max_length=1001)

    @model_validator(mode="after")
    def bounded_segments(self):
        self.started_at, self.ended_at = utc(self.started_at), utc(self.ended_at)
        cursor = self.started_at
        screened_duration = timedelta()
        for segment in self.segments:
            segment.started_at, segment.ended_at = (
                utc(segment.started_at),
                utc(segment.ended_at),
            )
            if segment.started_at != cursor or segment.ended_at <= cursor:
                raise ValueError(
                    "screening segments must cover the interval contiguously"
                )
            # Recorder outages can last days, sometimes with missing originals
            # at their endpoints. Unverified holds must remain deliverable even
            # then; they never authorize processing. Bound the actual screening
            # coverage, not the elapsed time in those holds. PrivacySegment
            # independently requires unverified coverage to remain pending.
            if segment.coverage != "unverified":
                screened_duration += segment.ended_at - segment.started_at
            cursor = segment.ended_at
        if cursor != self.ended_at or screened_duration > timedelta(hours=24):
            raise ValueError("invalid screening interval bounds")
        if self.ended_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("screening cannot cover future capture")
        for evidence in self.evidence:
            evidence.captured_at = utc(evidence.captured_at)
            if not self.started_at <= evidence.captured_at <= self.ended_at:
                raise ValueError("screening evidence must belong to its interval")
            if evidence.reason in {"missing_frame", "screening_failed"}:
                adjacent = [
                    segment
                    for segment in self.segments
                    if segment.started_at <= evidence.captured_at <= segment.ended_at
                ]
                if any(
                    segment.state == "allowed" or segment.coverage != "unverified"
                    for segment in adjacent
                ):
                    raise ValueError("failed frame coverage must remain held")
        return self


class PrivacyDisplaySet(BaseModel):
    observed_at: datetime
    transition_started_at: datetime
    track_ids: list[str] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_inventory(self):
        self.observed_at = utc(self.observed_at)
        self.transition_started_at = utc(self.transition_started_at)
        if self.transition_started_at > self.observed_at:
            raise ValueError("display transition starts after observation")
        if self.observed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("display inventory cannot describe future capture")
        if any(not track or len(track) > 256 for track in self.track_ids):
            raise ValueError("invalid display track identity")
        self.track_ids = sorted(set(self.track_ids))
        return self


class PrivacyRequiredRange(BaseModel):
    """Historical coverage obligation, independent of completed predictions.

    Every display observed anywhere in this range is conservatively required
    throughout it. An empty inventory cannot establish safety.
    """

    started_at: datetime
    ended_at: datetime
    track_ids: list[str] = Field(max_length=32)
    coverage: Literal["historical_observed_displays"] = "historical_observed_displays"
    policy_version: str = Field(
        default="screen-privacy-v1", min_length=1, max_length=64
    )

    @model_validator(mode="after")
    def bounded(self):
        self.started_at, self.ended_at = utc(self.started_at), utc(self.ended_at)
        if not timedelta(0) < self.ended_at - self.started_at <= timedelta(days=32):
            raise ValueError("historical coverage requires at most 32 days")
        if self.ended_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("historical coverage cannot describe future capture")
        if any(not track or len(track) > 256 for track in self.track_ids):
            raise ValueError("invalid display track identity")
        self.track_ids = sorted(set(self.track_ids))
        return self


def merged(spans):
    result = []
    for low, high in sorted(spans):
        if high <= low:
            continue
        if result and low <= result[-1][1]:
            result[-1] = (result[-1][0], max(high, result[-1][1]))
        else:
            result.append((low, high))
    return result


class _IntervalIndex:
    """Immutable overlap index; prefix ends preserve long enclosing intervals."""

    def __init__(self, rows, start="started_at", end="ended_at"):
        self.rows = sorted(rows, key=lambda row: row[start])
        self.starts = [row[start] for row in self.rows]
        self.ends = []
        self.end_key = end
        for row in self.rows:
            self.ends.append(max(self.ends[-1], row[end]) if self.ends else row[end])

    def overlapping(self, start, end):
        first = bisect_right(self.ends, start)
        last = bisect_left(self.starts, end)
        return [row for row in self.rows[first:last] if row[self.end_key] > start]


class PrivacySnapshot:
    def __init__(
        self,
        sources,
        intervals,
        overrides=(),
        display_sets=(),
        required_ranges=(),
        capture_holds=(),
        historical_inventories=(),
    ):
        self.owner = None
        self._read_scoped = False

        self.gallery_dependencies = None
        self._checked_identifiers = set()
        self._all_sources = False
        self._record_admitted = False
        self.sources = {
            s["source_id"]: s
            for s in sources
            if s.get("privacy_enabled_from")
            or s.get("privacy_revision", 0)
            or s.get("privacy_tracks")
        }
        self.display_sets = display_sets
        self.required_ranges = required_ranges
        self.held_capture_sessions = {
            row["capture_session_id"] for row in capture_holds
        }
        self.held_capture_chunks = {
            str(chunk) for row in capture_holds for chunk in row["chunk_ids"]
        }
        self.revisions = {
            k: s.get("privacy_revision", 0) for k, s in self.sources.items()
        }
        # Index immutable snapshot segments once. Audio claims and listings query
        # narrow spans repeatedly; scanning the entire backfill per claim blocks
        # the event loop. Prefix maxima retain long, overlapping exclusions.
        recorded_tracks = {}
        for inventory in display_sets:
            recorded_tracks.setdefault(inventory["source_id"], set()).update(
                inventory["track_ids"]
            )
        for inventory in historical_inventories:
            tracks = recorded_tracks.setdefault(inventory["source_id"], set())
            for observation in inventory["observations"]:
                tracks.update(observation["track_ids"])
        grouped = {}
        for row in intervals:
            recorded_track = row["track_id"] in recorded_tracks.get(
                row["source_id"], ()
            )
            capture_times = None
            for segment in row["segments"]:
                start, end = utc(segment["started_at"]), utc(segment["ended_at"])
                # Frame timestamps are needed only to establish an uncertain
                # capture gap, never for an already-screened segment.
                candidate_gap = (
                    recorded_track
                    and segment["state"] == "pending"
                    and segment.get("coverage") == "unverified"
                    and segment.get("reason")
                    in {"capture_gap", "missing_frame", "screening_failed"}
                    and end > start
                )
                capture_gap = False
                if candidate_gap:
                    if capture_times is None:
                        capture_times = sorted(
                            {utc(e["captured_at"]) for e in row.get("evidence", [])}
                        )
                    left = bisect_left(capture_times, start)
                    right = bisect_left(capture_times, end)
                    capture_gap = (
                        right == left + 1
                        and right < len(capture_times)
                        and capture_times[left] == start
                        and capture_times[right] == end
                    )
                grouped.setdefault(row["source_id"], []).append(
                    {
                        **segment,
                        "track_id": row["track_id"],
                        "started_at": start,
                        "ended_at": end,
                        "_capture_gap": capture_gap,
                    }
                )

        # Normalize once, rather than allocating datetimes for every record/cell.
        def normalized(rows, keys):
            return [
                dict(row, **{key: utc(row[key]) for key in keys}, _policy_order=i)
                for i, row in enumerate(rows)
            ]

        self.overrides = normalized(overrides, ("started_at", "ended_at"))
        self.required_ranges = normalized(required_ranges, ("started_at", "ended_at"))
        self.display_sets = normalized(
            display_sets, ("observed_at", "transition_started_at")
        )
        self._overrides_by_source = {}
        self._requirements_by_source = {}
        self._inventories_by_source = {}
        self._transitions_by_source = {}
        for source_id in self.sources:
            self._overrides_by_source[source_id] = _IntervalIndex(
                [r for r in self.overrides if r["source_id"] == source_id]
            )
            self._requirements_by_source[source_id] = _IntervalIndex(
                [r for r in self.required_ranges if r["source_id"] == source_id]
            )
            inventories = sorted(
                [r for r in self.display_sets if r["source_id"] == source_id],
                key=lambda r: (r["observed_at"], -r["_policy_order"]),
            )
            self._inventories_by_source[source_id] = (
                inventories,
                [r["observed_at"] for r in inventories],
            )
            self._transitions_by_source[source_id] = _IntervalIndex(
                inventories, "transition_started_at", "observed_at"
            )

        self._segment_index = {}
        for source_id, segments in grouped.items():
            segments.sort(key=lambda segment: segment["started_at"])
            maximum_ends = []
            for segment in segments:
                maximum_ends.append(
                    max(maximum_ends[-1], segment["ended_at"])
                    if maximum_ends
                    else segment["ended_at"]
                )
            self._segment_index[source_id] = (
                segments,
                [segment["started_at"] for segment in segments],
                maximum_ends,
            )

    def _segments_in(self, source_id, start, end):
        index = self._segment_index.get(source_id)
        if not index:
            return []
        segments, starts, maximum_ends = index
        first = bisect_right(maximum_ends, start)
        last = bisect_left(starts, end)
        return [
            segment for segment in segments[first:last] if segment["ended_at"] > start
        ]

    def source(self, identifier):
        if identifier:
            # Also remember currently unprotected identifiers: activation of
            # that source later must invalidate an already-running request.
            self._checked_identifiers.add(identifier)
        return next(
            (
                s
                for key, s in self.sources.items()
                if identifier == key or identifier.startswith(key + ":")
            ),
            None,
        )

    def watch_all_sources(self):
        """Use the whole-owner fence when reading a broad, mutable evidence set."""
        self._all_sources = True

    @staticmethod
    def _display_requirements(activated, inventories, requirements, low, high):
        historical = [
            row
            for row in requirements
            if utc(row["started_at"]) <= low and utc(row["ended_at"]) >= high
        ]
        live = activated is not None and high > activated
        if live and any(
            utc(row["transition_started_at"]) < high and utc(row["observed_at"]) > low
            for row in inventories
        ):
            return set(), False, None
        known = [row for row in inventories if utc(row["observed_at"]) <= low]
        latest = max(known, key=lambda row: utc(row["observed_at"])) if known else None
        tracks = set(latest["track_ids"] if live and latest else [])
        ready = bool(tracks) if live else True
        if live and latest is None:
            # Activation may predate live inventory reporting. A recorder-backed
            # historical inventory can establish displays for this bounded cell;
            # observed frames alone cannot. Existing live inventories (including
            # empty/uncertain ones) and overlapping requirements still constrain it.
            ready = any(
                row.get("coverage") == "historical_recorded_displays"
                and row.get("inventory_refinement")
                and row["track_ids"]
                for row in historical
            )
        recorded = bool(live and latest or historical)
        observed = [utc(latest["observed_at"])] if live and latest else []
        for row in historical:
            ready = ready and bool(row["track_ids"])
            tracks.update(row["track_ids"])
            recorded = recorded and (
                row.get("coverage") == "historical_recorded_displays"
                and bool(row.get("inventory_refinement"))
            )
            observed.append(utc(row["started_at"]))
        since = max(observed) if recorded and ready and tracks else None
        return tracks, ready, since

    @staticmethod
    def _applicable_segments(segments, tracks, inventory_since, low, high):
        return [
            segment
            for segment in segments
            if segment["started_at"] <= low
            and segment["ended_at"] >= high
            and not (
                inventory_since is not None
                and segment["_capture_gap"]
                and segment["track_id"] not in tracks
                # Inventory must establish absence after the last capture.
                # A frame contradicting that inventory remains held, even when
                # a caller asks for a narrower slice after the frame timestamp.
                and segment["started_at"] < inventory_since
            )
        ]

    def allowed_spans(self, identifier, start, end):
        start, end = utc(start), utc(end)
        source = self.source(identifier or "")
        if not source:
            return [(start, end)] if end > start else []
        activated = (
            utc(source["privacy_enabled_from"])
            if source.get("privacy_enabled_from")
            else None
        )
        if source.get("privacy_updating"):
            return []
        source_id = source["source_id"]
        inventory_rows, observed = self._inventories_by_source[source_id]
        predecessor = bisect_right(observed, start) - 1
        inventories = inventory_rows[max(0, predecessor) : bisect_left(observed, end)]
        transitions = self._transitions_by_source[source_id].overlapping(start, end)
        segments = self._segments_in(source_id, start, end)
        overrides = self._overrides_by_source[source_id].overlapping(start, end)
        requirements = self._requirements_by_source[source_id].overlapping(start, end)

        # Sweep only overlapping boundaries. End events precede start events at
        # a boundary, matching the half-open capture range contract.
        events = {}
        active = {
            kind: {}
            for kind in ("segments", "overrides", "requirements", "transitions")
        }
        for kind, rows, low_key, high_key in (
            ("segments", segments, "started_at", "ended_at"),
            ("overrides", overrides, "started_at", "ended_at"),
            ("requirements", requirements, "started_at", "ended_at"),
            ("transitions", transitions, "transition_started_at", "observed_at"),
        ):
            for i, row in enumerate(rows):
                low, high = max(start, row[low_key]), min(end, row[high_key])
                if high <= low:
                    continue
                events.setdefault(low, []).append((kind, i, row))
                events.setdefault(high, []).append((kind, i, None))
        points = {start, end, *events}
        points.update(
            row["observed_at"]
            for row in inventories
            if start < row["observed_at"] < end
        )
        if activated is not None and start < activated < end:
            points.add(activated)
        points = sorted(points)
        allowed = []
        for low, high in zip(points, points[1:]):
            for kind, i, row in events.get(low, ()):
                if row is None:
                    active[kind].pop(i, None)
                else:
                    active[kind][i] = row
            decisions = active["overrides"].values()
            if decisions:
                latest = max(
                    decisions, key=lambda r: (r.get("revision", 0), -r["_policy_order"])
                )
                if latest["override"] == "allowed":
                    allowed.append((low, high))
                continue
            position = bisect_right(observed, low) - 1
            current_inventory = [inventory_rows[position]] if position >= 0 else []
            tracks, safe, recorded = self._display_requirements(
                activated,
                [*current_inventory, *active["transitions"].values()],
                active["requirements"].values(),
                low,
                high,
            )
            applicable = self._applicable_segments(
                active["segments"].values(), tracks, recorded, low, high
            )
            if any(seg["state"] != "allowed" for seg in applicable):
                continue
            covered = {seg["track_id"] for seg in applicable}
            if safe and tracks.issubset(covered):
                allowed.append((low, high))
        return merged(allowed)

    def permits(self, source, start, end):
        start, end = utc(start), utc(end)
        if end <= start:
            end = start + timedelta(milliseconds=1)
        return self.allowed_spans(source, start, end) == [(start, end)]

    def permits_record(self, row):
        # Rejected candidates cannot influence a returned result. Keep only
        # successful admissions in the publication fence, including the
        # original evidence reached through recursive dependency receipts.
        before = self.admission_checkpoint()
        dependencies = self.gallery_dependencies
        dependency_before = (
            dependencies.admission_checkpoint() if dependencies else None
        )
        admitted = False
        try:
            admitted = self._permits_record(row)
            if admitted:
                self._record_admitted = True
            return admitted
        finally:
            if not admitted:
                self.restore_admission(before)
                if dependencies:
                    dependencies.restore_admission(dependency_before)

    def admission_checkpoint(self):
        return (
            self._checked_identifiers.copy(),
            self._record_admitted,
            self._all_sources,
        )

    def fresh_capture_admission(self):
        """Share this request's capture policy with independent admission state."""
        snapshot = copy(self)
        snapshot._checked_identifiers = set()
        snapshot._all_sources = False
        snapshot._record_admitted = False
        snapshot.gallery_dependencies = None
        return snapshot

    def restore_admission(self, checkpoint):
        self._checked_identifiers, self._record_admitted, self._all_sources = checkpoint

    @property
    def has_admissions(self):
        return (
            self._record_admitted
            or self._all_sources
            or bool(self._checked_identifiers)
        )

    def _permits_record(self, row):
        if not isinstance(row, dict):
            row = row.model_dump() if hasattr(row, "model_dump") else vars(row)
        # Defer this dependency to break the import cycle through
        # backend.services.gallery_dependencies -> backend.services.privacy.
        from backend.services.gallery_dependencies import (
            permits_without_dependency_state,
        )

        if self.gallery_dependencies is not None:
            if not self.gallery_dependencies.permits(row, self.owner):
                return False
        elif not permits_without_dependency_state(row):
            return False
        references = list(row.get("audio_ranges") or []) + list(
            row.get("evidence_refs") or []
        )
        for ref in references:
            ref = ref if isinstance(ref, dict) else ref.model_dump()
            if ref.get("capture_session_ids") or ref.get("chunk_ids"):
                # Holds keyed only by capture identity cannot use a source fence.
                self.watch_all_sources()
            if self.held_capture_sessions.intersection(
                ref.get("capture_session_ids") or []
            ) or self.held_capture_chunks.intersection(
                str(chunk) for chunk in ref.get("chunk_ids") or []
            ):
                return False
            source = (
                ref.get("capture_source_id")
                or ref.get("source_id")
                or (ref.get("locator") or {}).get("capture_source_id")
            )
            start = ref.get("started_at")
            end = ref.get("ended_at") or start
            if source and start and not self.permits(source, start, end):
                return False
        source = (
            row.get("source_id")
            or row.get("client_id")
            or (row.get("locator") or {}).get("capture_source_id")
        )
        start = row.get("captured_at") or row.get("started_at") or row.get("created_at")
        end = row.get("ended_at") or start
        if start and any(
            not self.permits(identifier, start, end)
            for identifier in row.get("source_ids", [])
        ):
            return False
        return not (source and start) or self.permits(source, start, end)


def database():

    return database_module.get_database()


async def load_snapshot(user_id, start=None, end=None):
    # Defer this dependency to break the import cycle through
    # backend.services.gallery_dependencies -> backend.services.privacy.
    from backend.services.gallery_dependencies import load_dependencies

    snapshot = await _load_capture_snapshot(user_id, start, end)
    snapshot.owner = str(user_id)
    full_policies = {snapshot.owner: snapshot} if start is None and end is None else {}
    snapshot.gallery_dependencies = await load_dependencies(user_id, full_policies)
    return snapshot


async def capture_screening_spans(user_id, source_id, start, end):
    """Find raw capture work without loading saved derivative dependencies.

    None means the source is not governed by screen privacy; an empty list means
    the entire window is held. This returns scheduling bounds only, never an
    admission object. Semantic consumers still load and validate their complete
    evidence policy before using or publishing a result.
    """
    snapshot = await _load_capture_snapshot(user_id, start, end)
    spans = (
        snapshot.allowed_spans(source_id, start, end)
        if snapshot.source(source_id)
        else None
    )
    await _assert_capture_current(user_id, snapshot)
    return spans


async def _load_capture_snapshot(user_id, start=None, end=None):
    db = database()
    source_query = {
        "user_id": str(user_id),
        "$or": [
            {"privacy_enabled_from": {"$ne": None}},
            {"privacy_revision": {"$gt": 0}},
            {"privacy_tracks.0": {"$exists": True}},
        ],
    }
    sources = await db.capture_sources.find(source_query).to_list(length=None)
    if not sources:
        return PrivacySnapshot([], [])
    query = {
        "user_id": str(user_id),
        "source_id": {"$in": [s["source_id"] for s in sources]},
    }
    if start is not None:
        query["ended_at"] = {"$gt": utc(start)}
    if end is not None:
        query["started_at"] = {"$lt": utc(end)}
    intervals = await db.privacy_screening.find(
        {**query, "superseded_by": {"$exists": False}},
        {"source_id": 1, "track_id": 1, "segments": 1, "evidence.captured_at": 1},
    ).to_list(length=None)
    overrides = await db.privacy_overrides.find(query).to_list(length=None)
    requirements = await db.privacy_required_ranges.find(
        {**query, "superseded_by": {"$exists": False}}
    ).to_list(length=None)
    historical_inventories = (
        await db.privacy_inventory_refinements.find(
            {"user_id": str(user_id), "source_id": query["source_id"]},
            {"source_id": 1, "observations.track_ids": 1},
        ).to_list(length=None)
        if any(row.get("inventory_refinement") for row in requirements)
        else []
    )
    capture_holds = await db.privacy_capture_holds.find(
        {"user_id": str(user_id), "source_id": query["source_id"]},
        {"capture_session_id": 1, "chunk_ids": 1},
    ).to_list(length=None)
    # Inventory history is small: one row per connection/configuration change,
    # not one per capture. The preceding row is needed even for bounded reads.
    inventories = await db.privacy_display_sets.find(
        {"user_id": str(user_id), "source_id": query["source_id"]}
    ).to_list(length=None)
    latest = await db.capture_sources.find(source_query).to_list(length=None)
    original = {s["source_id"]: s for s in sources}
    current = {s["source_id"]: s for s in latest}
    sources = []
    for identifier in dict.fromkeys([*original, *current]):
        before, after = original.get(identifier), current.get(identifier)
        changed = (
            before is None
            or after is None
            or before.get("privacy_revision", 0) != after.get("privacy_revision", 0)
            or bool(before.get("privacy_updating"))
            != bool(after.get("privacy_updating"))
        )
        # A crossed revision invalidates that source only. Retain a disappeared
        # source as held too; dropping it would treat its evidence as unprotected.
        source = after if after is not None else before
        sources.append(dict(source, privacy_updating=True) if changed else source)
    # Large historical policies require sorting and timestamp normalization.
    # Keep that work off the capture/API event loop; admission still checks
    # the original revisions after construction and before publication.
    return await asyncio.to_thread(
        PrivacySnapshot,
        sources,
        intervals,
        overrides,
        inventories,
        requirements,
        capture_holds,
        historical_inventories,
    )


async def begin_update(user_id, query, update):
    """Order policy changes against the existing per-user publication commit.

    Only the transition into the durable hold uses this lock. Invalidation later
    acquires the publication lock itself, so it must run after this block exits.
    """
    async with distributed_lock(
        timeline_publication_lock(str(user_id)),
        timeout=120,
        blocking_timeout=30,
        renew=True,
    ):
        return await database().capture_sources.update_one(query, update)


async def save_required_range(source, body: PrivacyRequiredRange):
    """Fence the whole historical period before its first job can be released."""

    db = database()
    owner, source_id = str(source.user_id), source.source_id
    data = body.model_dump()
    data.update(user_id=owner, source_id=source_id)
    identity = hashlib.sha256(
        json.dumps(data, default=lambda v: v.isoformat(), sort_keys=True).encode()
    ).hexdigest()
    query = {"user_id": owner, "source_id": source_id}
    existing = await db.privacy_required_ranges.find_one({"_id": identity})
    if not existing:
        acquired = await begin_update(
            owner,
            {
                **query,
                "$or": [{"privacy_operation": None}, {"privacy_operation": identity}],
            },
            {
                "$inc": {"privacy_revision": 1},
                "$set": {"privacy_operation": identity, "privacy_updating": True},
            },
        )
        if not acquired.matched_count:
            raise ValueError("privacy policy update in progress")
        await db.privacy_required_ranges.update_one(
            {"_id": identity}, {"$setOnInsert": data}, upsert=True
        )
    elif not await db.capture_sources.find_one(
        {**query, "privacy_operation": identity, "privacy_updating": True}
    ):
        return
    # Keep interrupted invalidation held until an idempotent retry completes it.
    await dirty_ranges.mark_evidence_dirty(
        owner,
        body.started_at,
        body.ended_at,
        identity,
        "privacy_historical_coverage",
        source_kind="privacy",
    )
    if body.track_ids:
        # An unavailable collector can establish a hold before its original
        # display inventory is accessible. Retire that unknown inventory only
        # after the collector supplies the exact period and invalidation succeeds.
        # Known display obligations remain cumulative; partial or other-source
        # inventories cannot release the unknown range. Preserve the old record
        # so delayed retries remain idempotent.
        await db.privacy_required_ranges.update_many(
            {
                **query,
                "started_at": body.started_at,
                "ended_at": body.ended_at,
                "coverage": body.coverage,
                "track_ids": [],
                "superseded_by": {"$exists": False},
            },
            {"$set": {"superseded_by": identity}},
        )
    await db.capture_sources.update_one(
        {**query, "privacy_operation": identity},
        {"$set": {"privacy_operation": None, "privacy_updating": False}},
    )


async def save_display_set(source, body: PrivacyDisplaySet):
    """Append display history; never rewrite the requirements for past capture."""
    db = database()
    owner, source_id = str(source.user_id), source.source_id
    data = body.model_dump()
    data.update(user_id=owner, source_id=source_id)
    identity = hashlib.sha256(
        f"{owner}:{source_id}:displays:{body.observed_at.isoformat()}".encode()
    ).hexdigest()
    existing = await db.privacy_display_sets.find_one({"_id": identity})
    if existing and (
        existing["track_ids"] != body.track_ids
        or utc(existing["transition_started_at"]) != body.transition_started_at
    ):
        raise ValueError("display inventory identity conflict")
    if not existing:
        acquired = await begin_update(
            owner,
            {
                "user_id": owner,
                "source_id": source_id,
                "$or": [{"privacy_operation": None}, {"privacy_operation": identity}],
            },
            {
                "$inc": {"privacy_revision": 1},
                "$set": {"privacy_operation": identity, "privacy_updating": True},
            },
        )
        if not acquired.matched_count:
            raise ValueError("privacy policy update in progress")
        await db.privacy_display_sets.update_one(
            {"_id": identity}, {"$setOnInsert": data}, upsert=True
        )
    await db.capture_sources.update_one(
        {"user_id": owner, "source_id": source_id, "privacy_operation": identity},
        {"$set": {"privacy_operation": None, "privacy_updating": False}},
    )


async def assert_current(user_id, snapshot):
    await _assert_capture_current(user_id, snapshot)
    if snapshot.gallery_dependencies is not None:
        await snapshot.gallery_dependencies.assert_current()


async def _capture_revision_snapshot(user_id):
    rows = (
        await database()
        .capture_sources.find(
            {"user_id": str(user_id)},
            {
                "source_id": 1,
                "privacy_enabled_from": 1,
                "privacy_revision": 1,
                "privacy_tracks": 1,
                "privacy_updating": 1,
            },
        )
        .to_list(length=None)
    )
    return PrivacySnapshot(rows, [])


async def capture_revisions(user_id):
    """Read a current policy fingerprint without loading or admitting evidence."""
    snapshot = await _capture_revision_snapshot(user_id)
    snapshot.watch_all_sources()
    await _assert_capture_current(user_id, snapshot)
    return snapshot.revisions


async def _assert_capture_current(user_id, snapshot):
    # A successfully inspected record with no capture identifiers contributes
    # no capture-policy dependency. Gallery/reference receipts have their own
    # fences. Bare, unexamined snapshots still retain the whole-policy check.
    if (
        snapshot._read_scoped
        and snapshot._record_admitted
        and not snapshot._all_sources
        and not snapshot._checked_identifiers
    ):
        return
    current = await _capture_revision_snapshot(user_id)
    if snapshot._all_sources or not snapshot._checked_identifiers:
        if current.revisions != snapshot.revisions or any(
            s.get("privacy_updating")
            for s in [*current.sources.values(), *snapshot.sources.values()]
        ):
            raise PrivacyHeld()
        return
    for identifier in tuple(snapshot._checked_identifiers):
        before = snapshot.source(identifier)
        after = current.source(identifier)
        before_identity = (
            (before["source_id"], before.get("privacy_revision", 0))
            if before is not None
            else None
        )
        after_identity = (
            (after["source_id"], after.get("privacy_revision", 0))
            if after is not None
            else None
        )
        if before_identity != after_identity or any(
            source and source.get("privacy_updating") for source in (before, after)
        ):
            raise PrivacyHeld()


async def require_record(row, user_id=None):
    data = row if isinstance(row, dict) else row.model_dump()
    owner = user_id or data.get("user_id")
    if not owner:
        raise PrivacyHeld()
    snapshot = await load_snapshot(owner)
    if not snapshot.permits_record(data):
        raise PrivacyHeld()
    identifiers = data.get("related_conversation_ids") or []
    if identifiers:
        references = (
            await database()
            .conversations.find(
                {"user_id": str(owner), "conversation_id": {"$in": identifiers}}
            )
            .to_list(length=None)
        )
        if any(not snapshot.permits_record(r) for r in references):
            raise PrivacyHeld()
    await assert_current(owner, snapshot)
    return snapshot


async def require_conversation(identifier):
    row = await database().conversations.find_one({"conversation_id": identifier})
    if row:
        return await require_record(row)
    return None


async def guard_export(metadata):
    """Check retained archive evidence under its owners, including admin exports."""
    creator = metadata.get("created_by")
    if not creator:
        raise PrivacyHeld()
    identifiers = {
        item.get("conversation_id") for item in metadata.get("conversations", [])
    }
    if None in identifiers:
        raise PrivacyHeld()
    rows = (
        await database()
        .conversations.find({"conversation_id": {"$in": list(identifiers)}})
        .to_list(length=None)
    )
    if {row["conversation_id"] for row in rows} != identifiers:
        # Retained text without canonical provenance cannot be cleared safely.
        raise PrivacyHeld()
    owners = {str(creator): {"metadata": metadata, "records": []}}
    for row in rows:
        owners.setdefault(str(row["user_id"]), {"records": []})["records"].append(row)
    snapshots = [
        (owner, await guard_payload(owner, payload))
        for owner, payload in owners.items()
    ]
    for owner, snapshot in snapshots:
        await assert_current(owner, snapshot)
    return snapshots


class _PrivacyResponseFence:
    """Fence publication without changing the wrapped response's range semantics."""

    def __init__(self, *args, privacy_snapshots, **kwargs):
        super().__init__(*args, **kwargs)
        self.privacy_snapshots = privacy_snapshots

    async def _check_privacy(self):
        for owner, snapshot in self.privacy_snapshots:
            await assert_current(owner, snapshot)

    async def __call__(self, scope, receive, send):
        try:
            await self._check_privacy()
        except PrivacyHeld:
            return await JSONResponse(
                {"error": "Private or unscreened evidence is held from processing"},
                status_code=423,
            )(scope, receive, send)

        async def guarded_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await self._check_privacy()
            await send(message)

        # A server-side path send would bypass our per-chunk publication fence.
        extensions = dict(scope.get("extensions", {}))
        extensions.pop("http.response.pathsend", None)
        await super().__call__(
            {**scope, "extensions": extensions}, receive, guarded_send
        )


class PrivacyFileResponse(_PrivacyResponseFence, FileResponse):
    pass


class PrivacyBytesResponse(_PrivacyResponseFence, Response):
    pass


class PrivacyStreamingResponse(_PrivacyResponseFence, StreamingResponse):
    pass


async def filter_records(rows, user_id):
    snapshot = await load_snapshot(user_id)
    allowed = [r for r in rows if snapshot.permits_record(r)]
    if allowed:
        await assert_current(user_id, snapshot)
    return allowed


async def filter_conversation_documents(rows, user_id):
    snapshot = await load_snapshot(user_id)
    identifiers = [r["conversation_id"] for r in rows]
    full = (
        await database()
        .conversations.find(
            {"user_id": str(user_id), "conversation_id": {"$in": identifiers}}
        )
        .to_list(length=None)
    )
    allowed = {r["conversation_id"] for r in full if snapshot.permits_record(r)}
    if allowed:
        await assert_current(user_id, snapshot)
    return [r for r in rows if r["conversation_id"] in allowed]


class ConversationPrivacyFilter:
    """Filter projected conversations and derivatives by their canonical owner.

    A review or export's creator need not own its evidence. Resolve the original
    conversation before applying policy, and retain snapshots across all listing
    queries so a final revision check also protects derived counts and facets.
    """

    def __init__(self):
        self.snapshots = {}
        self.originals = {}
        self._snapshot_lock = asyncio.Lock()

    async def _snapshot(self, owner):
        async with self._snapshot_lock:
            if owner not in self.snapshots:
                self.snapshots[owner] = await load_snapshot(owner)
            return self.snapshots[owner]

    async def filter(self, rows):
        identifiers = list(
            {r["conversation_id"] for r in rows if r.get("conversation_id")}
        )
        if not identifiers:
            return []
        full = (
            await database()
            .conversations.find(
                {"conversation_id": {"$in": identifiers}},
                _RECORD_PROJECTION,
            )
            .to_list(length=None)
        )
        allowed = set()
        for record in full:
            owner = record.get("user_id")
            if not owner:
                continue
            owner = str(owner)
            snapshot = await self._snapshot(owner)
            if snapshot.permits_record(record):
                allowed.add(record["conversation_id"])
                identifier = record["conversation_id"]
                previous = self.originals.get(identifier)
                if previous is not None and previous != record:
                    raise PrivacyHeld()
                self.originals[identifier] = record
        retained = []
        for row in rows:
            identifier = row.get("conversation_id")
            if identifier not in allowed:
                continue
            original = self.originals[identifier]
            owner = str(
                row.get("user_id") or row.get("requested_by") or original["user_id"]
            )
            snapshot = await self._snapshot(owner)
            if snapshot.permits_record(row):
                retained.append(row)
                # A cached reference may itself depend on older evidence. Carry
                # its immutable receipt forward rather than only its current CID.
                receipt = row.get("privacy_reference_receipt")
                if receipt:
                    self.originals[(owner, identifier, tuple(receipt))] = {
                        "user_id": owner,
                        "conversation_id": identifier,
                        "privacy_reference_receipt": receipt,
                    }
        if retained:
            await self.assert_current()
        return retained

    async def assert_current(self):
        for owner, snapshot in self.snapshots.items():
            if snapshot.has_admissions:
                await assert_current(owner, snapshot)

    async def filter_embeddings(self, rows):
        """Cached audio vectors require nonempty immutable capture evidence."""
        return await self.filter(
            [
                row
                for row in rows
                if isinstance(row.get("privacy_reference_receipt"), list)
                and row["privacy_reference_receipt"]
            ]
        )

    async def require_reference_receipt(self, user_id, receipt):
        if not isinstance(receipt, list):
            raise PrivacyHeld()
        snapshot = await self._snapshot(str(user_id))
        if not snapshot.permits_record({"privacy_reference_receipt": receipt}):
            raise PrivacyHeld()
        await self.assert_current()

    async def reference_receipt(self, user_id, *, conversation_ids=None):
        """Persist metadata captured at admission, never re-read a mutable claim."""
        # Defer this dependency to break the import cycle through
        # backend.services.reference_dependencies -> backend.services.privacy.
        from backend.services.reference_dependencies import seal

        await self.assert_current()
        records = list(self.originals.values())
        if conversation_ids is not None:
            identifiers = set(conversation_ids)
            records = [row for row in records if row["conversation_id"] in identifiers]
            if {row["conversation_id"] for row in records} != identifiers:
                raise PrivacyHeld()
        receipt = await seal(user_id, records)
        await self.assert_current()
        return receipt

    def revision_receipt(self):
        """Persist the policies used to derive a cache entry or reviewed result."""
        return {
            owner: dict(snapshot.revisions)
            for owner, snapshot in self.snapshots.items()
        }

    async def require_receipt(self, receipt):
        """Restore the original policy boundary; absent/stale receipts remain held."""
        if not isinstance(receipt, dict) or not receipt:
            raise PrivacyHeld()
        for owner, revisions in receipt.items():
            if owner not in self.snapshots:
                self.snapshots[owner] = await load_snapshot(owner)
            if self.snapshots[owner].revisions != revisions:
                raise PrivacyHeld()
        if set(self.snapshots) != set(receipt):
            raise PrivacyHeld()
        await self.assert_current()

    @asynccontextmanager
    async def publication(self):
        """Serialize a short database publication against every evidence owner's policy."""
        owners = set(self.snapshots)
        for snapshot in self.snapshots.values():
            dependencies = snapshot.gallery_dependencies
            if dependencies is not None:
                owners.update(dependencies.publication_owners)
        async with AsyncExitStack() as stack:
            for owner in sorted(owners):
                await stack.enter_async_context(
                    distributed_lock(
                        timeline_publication_lock(owner),
                        timeout=120,
                        blocking_timeout=30,
                        renew=True,
                    )
                )
            await self.assert_current()
            yield
            await self.assert_current()


async def save_screening(source, result: ScreeningResult):
    db = database()
    owner, source_id = source.user_id, source.source_id
    document = result.model_dump(exclude_none=True)
    document.update(user_id=owner, source_id=source_id)
    identifier = hashlib.sha256(
        f"{owner}:{source_id}:{result.interval_id}".encode()
    ).hexdigest()

    async def finish():

        await dirty_ranges.mark_evidence_dirty(
            owner,
            result.started_at,
            result.ended_at,
            identifier,
            "privacy_screening",
            source_kind="privacy",
        )
        await db.capture_sources.update_one(
            {"user_id": owner, "source_id": source_id, "privacy_operation": identifier},
            {"$set": {"privacy_updating": False, "privacy_operation": None}},
        )

    existing = await db.privacy_screening.find_one({"_id": identifier})
    if existing:
        if any(existing.get(k) != v for k, v in document.items()):
            # BSON datetimes come back naive from the underlying driver.
            import json

            from bson import json_util

            def canonical(doc):
                return json.dumps(
                    doc,
                    default=lambda x: (
                        utc(x).isoformat()
                        if isinstance(x, datetime)
                        else json_util.default(x)
                    ),
                    sort_keys=True,
                )

            if canonical({k: existing.get(k) for k in document}) != canonical(document):
                raise ValueError("screening identity conflict")
        interrupted = await db.capture_sources.find_one(
            {
                "user_id": owner,
                "source_id": source_id,
                "privacy_operation": identifier,
                "privacy_updating": True,
            }
        )
        if interrupted:
            await finish()
        return
    # Every mutation bumps the fence before data becomes eligible. Interrupted
    # writes remain visibly held; replay of this exact operation may complete it.
    restrictive = any(s.state != "allowed" for s in result.segments)
    current = await db.capture_sources.find_one(
        {"user_id": owner, "source_id": source_id}
    )
    revision_change = restrictive or result.track_id not in current.get(
        "privacy_tracks", []
    )
    acquired = await begin_update(
        owner,
        {
            "user_id": owner,
            "source_id": source_id,
            "$or": [{"privacy_operation": None}, {"privacy_operation": identifier}],
        },
        {
            "$inc": {"privacy_revision": int(revision_change)},
            "$set": {"privacy_updating": True, "privacy_operation": identifier},
            "$addToSet": {"privacy_tracks": result.track_id},
        },
    )
    if not acquired.matched_count:
        raise ValueError("privacy policy update in progress")
    await db.privacy_screening.update_one(
        {"_id": identifier}, {"$setOnInsert": document}, upsert=True
    )
    await finish()


async def require_audio_ranges(ranges, user_id=None):
    if not ranges:
        return []
    identifiers = {r.capture_source_id.split(":", 1)[0] for r in ranges}
    sources = (
        await database()
        .capture_sources.find(
            {
                "source_id": {"$in": list(identifiers)},
                "$or": [
                    {"privacy_enabled_from": {"$ne": None}},
                    {"privacy_revision": {"$gt": 0}},
                    {"privacy_tracks.0": {"$exists": True}},
                ],
                **({"user_id": str(user_id)} if user_id else {}),
            }
        )
        .to_list(length=None)
    )
    snapshots = []
    for owner in {s["user_id"] for s in sources}:
        snapshot = await load_snapshot(
            owner, min(r.started_at for r in ranges), max(r.ended_at for r in ranges)
        )
        if not snapshot.permits_record({"audio_ranges": ranges}):
            raise PrivacyHeld()
        snapshots.append((owner, snapshot))
    return snapshots


async def ensure_indexes():
    await database().privacy_reference_dependencies.create_index("user_id")
    for name in ("privacy_screening", "privacy_overrides", "privacy_required_ranges"):
        await database()[name].create_index(
            [("user_id", 1), ("source_id", 1), ("started_at", 1), ("ended_at", 1)]
        )
    await database().privacy_display_sets.create_index(
        [("user_id", 1), ("source_id", 1), ("observed_at", 1)]
    )
    await database().privacy_capture_holds.create_index(
        [("user_id", 1), ("source_id", 1), ("capture_session_id", 1)], unique=True
    )


class _PrivacyReadContext:
    """Request-owned policy and lazy vault scan, never a cross-request cache."""

    def __init__(self, snapshot, *, scoped=False):
        self.snapshot = snapshot
        snapshot._read_scoped = scoped
        self.paths = None
        self.quarantine_snapshot = None

    async def quarantined(self):
        if self.paths is None:
            # Separate admission state: a rejected chat must not leave a broad
            # owner fence or retained dependency admissions on accepted chats.
            self.quarantine_snapshot = _fresh_read_snapshot(self.snapshot)
            self.paths = await quarantined_vault_paths(
                self.snapshot.owner, snapshot=self.quarantine_snapshot
            )
        _merge_read_admission(self.snapshot, self.quarantine_snapshot)
        return self.paths


def _fresh_read_snapshot(snapshot):

    # Defer this dependency to break the import cycle through
    # backend.services.gallery_dependencies -> backend.services.privacy.
    from backend.services.gallery_dependencies import GalleryDependencies

    result = snapshot.fresh_capture_admission()
    dependencies = snapshot.gallery_dependencies
    if dependencies is not None:
        policies = {
            owner: policy.fresh_capture_admission()
            for owner, policy in dependencies.snapshots.items()
        }
        state = GalleryDependencies(
            dependencies.rows.values(), policies, dependencies.references.rows.values()
        )
        for policy in policies.values():
            policy.gallery_dependencies = weakref.proxy(state)
        result.gallery_dependencies = state
    return result


def _merge_read_admission(target, source):
    target._checked_identifiers.update(source._checked_identifiers)
    target._record_admitted |= source._record_admitted
    target._all_sources |= source._all_sources
    a, b = target.gallery_dependencies, source.gallery_dependencies
    if a is not None and b is not None:
        a._used.update(b._used)
        a._used_owners.update(b._used_owners)
        a.references.used.update(b.references.used)
        # Dependency snapshots refer back to their parent; merge capture state
        # without recursively following that back-reference.
        for owner, policy in b.snapshots.items():
            other = a.snapshots[owner]
            other._checked_identifiers.update(policy._checked_identifiers)
            other._record_admitted |= policy._record_admitted
            other._all_sources |= policy._all_sources


async def guard_payload(user_id, payload, *, snapshot=None, _context=None):
    """Resolve cited identities afresh before an agent consumes derived content."""
    snapshot = snapshot or await load_snapshot(user_id)
    conversations, episodes, sessions, paths, proposals = (
        set(),
        set(),
        set(),
        set(),
        set(),
    )

    def visit(value):
        # An inner recursive function closes over itself and the entire policy.
        # Use an explicit stack so request completion releases policy history by
        # reference count, instead of retaining it until a process-wide GC pause.
        pending = [value]
        while pending:
            value = pending.pop()
            if hasattr(value, "model_dump"):
                value = value.model_dump()
            elif is_dataclass(value) and not isinstance(value, type):
                value = asdict(value)
            if isinstance(value, dict):
                if isinstance(value.get("path"), str):
                    paths.add(value["path"].casefold())
                if isinstance(value.get("note_path"), str):
                    paths.add(value["note_path"].casefold())
                if value.get("kind") == "recording" and isinstance(
                    value.get("key"), str
                ):
                    conversations.add(value["key"])
                if value.get("kind") == "episode" and isinstance(value.get("key"), str):
                    episodes.add(value["key"])
                if value.get("kind") == "session" and isinstance(value.get("key"), str):
                    sessions.add(value["key"])
                if not snapshot.permits_record(value):
                    raise PrivacyHeld()
                for key, child in value.items():
                    if key in {"conversation_id", "recording_id"} and isinstance(
                        child, str
                    ):
                        conversations.add(child)
                    if key == "proposal_id" and isinstance(child, str):
                        proposals.add(child)
                    if key in {"note_paths", "accepted_note_paths"} and isinstance(
                        child, list
                    ):
                        paths.update(
                            path.casefold() for path in child if isinstance(path, str)
                        )
                    if key in {
                        "conversation_ids",
                        "related_conversation_ids",
                    } and isinstance(child, list):
                        conversations.update(x for x in child if isinstance(x, str))
                    if key in {"episode_id", "episode_key"} and isinstance(child, str):
                        episodes.add(child)
                    if key in {
                        "episode_ids",
                        "episode_keys",
                        "source_episode_keys",
                    } and isinstance(child, list):
                        episodes.update(x for x in child if isinstance(x, str))
                    pending.append(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    pending.append(child)

    visit(payload)
    if proposals:
        rows = (
            await database()
            .memory_review_proposals.find(
                {"user_id": str(user_id), "proposal_id": {"$in": list(proposals)}}
            )
            .to_list(length=None)
        )
        for row in rows:
            visit(row)
    if sessions:
        rows = (
            await database()
            .undated_sessions.find(
                {"user_id": str(user_id), "session_key": {"$in": list(sessions)}},
                {"recording_id": 1},
            )
            .to_list(length=None)
        )
        conversations.update(
            row["recording_id"] for row in rows if row.get("recording_id")
        )
        days = (
            await database()
            .timeline_days.find(
                {
                    "user_id": str(user_id),
                    "semantic_group_history.group_key": {"$in": list(sessions)},
                },
                {"semantic_group_history": 1},
            )
            .to_list(length=None)
        )
        for day in days:
            for group in day.get("semantic_group_history", []):
                if group["group_key"] in sessions:
                    episodes.update(group.get("episode_ids", []))
    if episodes:
        rows = (
            await database()
            .timeline_episodes.find(
                {
                    "user_id": str(user_id),
                    "$or": [
                        {"episode_id": {"$in": list(episodes)}},
                        {"episode_key": {"$in": list(episodes)}},
                    ],
                }
            )
            .to_list(length=None)
        )
        if any(not snapshot.permits_record(r) for r in rows):
            raise PrivacyHeld()
        for row in rows:
            conversations.update(row.get("related_conversation_ids", []))
    if conversations:
        rows = (
            await database()
            .conversations.find(
                {
                    "user_id": str(user_id),
                    "conversation_id": {"$in": list(conversations)},
                }
            )
            .to_list(length=None)
        )
        if any(not snapshot.permits_record(r) for r in rows):
            raise PrivacyHeld()
    if paths and paths.intersection(
        path.casefold()
        for path in (
            await _context.quarantined()
            if _context is not None
            else await quarantined_vault_paths(user_id, snapshot=snapshot)
        )
    ):
        raise PrivacyHeld()
    await assert_current(user_id, snapshot)
    return snapshot


async def filter_payloads(rows, user_id):
    """Filter aggregate projections through one request-owned evaluation."""
    snapshot = await load_snapshot(user_id)
    context = _PrivacyReadContext(snapshot)
    result = []
    for row in rows:
        before = snapshot.admission_checkpoint()
        dependencies = snapshot.gallery_dependencies
        dependency_before = (
            dependencies.admission_checkpoint() if dependencies else None
        )
        try:
            await guard_payload(user_id, row, snapshot=snapshot, _context=context)
        except PrivacyHeld:
            snapshot.restore_admission(before)
            if dependencies:
                dependencies.restore_admission(dependency_before)
            continue
        result.append(row)
    if result:
        await assert_current(user_id, snapshot)
    return result


def _has_policy_evidence(payload):
    """Conservative fast path for content with no privacy/provenance fields.

    Derive capture fields from the same projection used by the evaluator. All
    lineage selectors understood by guard_payload force full evaluation too.
    This is only for checks whose caller does not retain a policy snapshot.
    """
    fields = {part for key in _RECORD_PROJECTION for part in key.split(".")} - {
        "metadata"
    }
    fields.update(
        {
            "path",
            "note_path",
            "recording_id",
            "proposal_id",
            "note_paths",
            "accepted_note_paths",
            "conversation_ids",
            "related_conversation_ids",
            "episode_key",
            "episode_ids",
            "episode_keys",
            "source_episode_keys",
            "background_similarity",
            "configuration",
        }
    )

    def visit(value):
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, dict):
            if value.get("kind") in {"recording", "episode", "session"} and isinstance(
                value.get("key"), str
            ):
                return True
            return bool(fields.intersection(value)) or any(
                visit(v) for v in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(visit(v) for v in value)
        return False

    return visit(payload)


def _chat_needs_quarantine(messages):
    return any(
        message.get("role") == "assistant"
        and (
            not isinstance(message.get("metadata", {}).get("evidence"), dict)
            or message.get("memories_used")
        )
        for message in messages
    )


async def check_payload(user_id, payload):
    """Check a read-only projection; callers needing a fence use guard_payload."""
    if _has_policy_evidence(payload):
        await guard_payload(user_id, payload)


async def _chat_evidence(user_id, session_id):
    return (
        await database()
        .chat_messages.find(
            {"user_id": str(user_id), "session_id": session_id},
            {"metadata": 1, "memories_used": 1, "role": 1},
        )
        .to_list(length=None)
    )


async def check_chat(user_id, session_id, metadata):
    """Authorize a chat read without building policies for evidence-free text."""
    messages = await _chat_evidence(user_id, session_id)
    payload = [metadata, *[m.get("metadata", {}) for m in messages]]
    if _has_policy_evidence(payload) or _chat_needs_quarantine(messages):
        context = _PrivacyReadContext(await load_snapshot(user_id), scoped=True)
        await _guard_chat_evidence(user_id, metadata, messages, context)


async def _guard_chat_evidence(user_id, metadata, messages, context):
    snapshot = context.snapshot
    await guard_payload(
        user_id,
        [metadata, *[m.get("metadata", {}) for m in messages]],
        snapshot=snapshot,
        _context=context,
    )
    if _chat_needs_quarantine(messages) and await context.quarantined():
        raise PrivacyHeld()
    await assert_current(user_id, snapshot)


async def guard_chat(user_id, session_id, metadata):
    """Check retained chat evidence using one current request-owned policy."""
    context = _PrivacyReadContext(await load_snapshot(user_id))
    messages = await _chat_evidence(user_id, session_id)
    await _guard_chat_evidence(user_id, metadata, messages, context)
    return context.snapshot


async def filter_chat_sessions(user_id, sessions):
    """Batch a chat list without multiplying policy loads or vault scans."""
    if not sessions:
        return []
    context = _PrivacyReadContext(await load_snapshot(user_id), scoped=True)
    snapshot = context.snapshot
    messages = {session.session_id: [] for session in sessions}
    cursor = (
        database()
        .chat_messages.find(
            {"user_id": str(user_id), "session_id": {"$in": list(messages)}},
            {"session_id": 1, "metadata": 1, "memories_used": 1, "role": 1},
        )
        .batch_size(128)
    )
    count = 0
    try:
        async for row in cursor:
            messages[row["session_id"]].append(row)
            count += 1
            if count % 128 == 0:
                await asyncio.sleep(0)
    finally:
        await cursor.close()
    result = []
    for session in sessions:
        before = snapshot.admission_checkpoint()
        dependencies = snapshot.gallery_dependencies
        dependency_before = (
            dependencies.admission_checkpoint() if dependencies else None
        )
        try:
            rows = messages[session.session_id]
            await _guard_chat_evidence(user_id, session.metadata, rows, context)
        except PrivacyHeld:
            snapshot.restore_admission(before)
            if dependencies:
                dependencies.restore_admission(dependency_before)
        else:
            result.append((session, len(rows)))
        await asyncio.sleep(0)
    if result:
        await assert_current(user_id, snapshot)
    return result


async def vault_reference_receipt(user_id, paths, *, snapshot):
    """Seal the capture evidence linked to the notes used by a derived result.

    Include every historical contributor to a mixed note. Later edits to a
    Conversation or its note links cannot retarget this immutable receipt.
    """
    # Defer this dependency to break the import cycle through
    # backend.services.reference_dependencies -> backend.services.privacy.
    from backend.services.reference_dependencies import seal

    original_paths = set(paths)
    paths = {path.casefold() for path in original_paths}
    snapshot.watch_all_sources()
    await assert_current(user_id, snapshot)
    identifiers = {
        path[len("conversations/") : -3]
        for path in original_paths
        if path.casefold().startswith("conversations/") and path.endswith(".md")
    }
    audits = (
        await database()
        .memory_audit.find(
            {"user_id": str(user_id)},
            {"conversation_id": 1, "note_path": 1},
        )
        .to_list(None)
    )
    identifiers.update(
        row["conversation_id"]
        for row in audits
        if str(row.get("note_path") or "").casefold() in paths
        and row.get("conversation_id")
    )
    records = []
    for collection in ("conversations", "timeline_episodes", "device_input_items"):
        rows = (
            await database()[collection]
            .find(
                {"user_id": str(user_id)},
                {**_RECORD_PROJECTION, "vault_paths": 1, "related_conversation_ids": 1},
            )
            .to_list(None)
        )
        for row in rows:
            if not paths.intersection(
                str(path).casefold() for path in row.get("vault_paths") or []
            ):
                continue
            identifiers.update(row.get("related_conversation_ids") or [])
            records.append(row)
    originals = (
        await database()
        .conversations.find(
            {"user_id": str(user_id), "conversation_id": {"$in": sorted(identifiers)}},
            _RECORD_PROJECTION,
        )
        .to_list(None)
    )
    if {row["conversation_id"] for row in originals} != identifiers:
        raise PrivacyHeld()
    records.extend(originals)
    if any(not snapshot.permits_record(row) for row in records):
        raise PrivacyHeld()
    receipt = await seal(user_id, records)
    await assert_current(user_id, snapshot)
    return receipt


async def quarantined_vault_paths(user_id, *, snapshot=None):
    """Keep mixed historical notes out of agents until their evidence is cleared.

    This is a visibility decision, not an edit to approved vault content.
    """
    snapshot = snapshot or await load_snapshot(user_id)
    snapshot.watch_all_sources()
    paths = set()
    held_conversations = set()
    for collection in ("timeline_episodes", "conversations", "device_input_items"):
        query = {"user_id": str(user_id)}
        if collection != "conversations":
            query["vault_paths.0"] = {"$exists": True}
        cursor = (
            database()[collection]
            .find(query, {**_RECORD_PROJECTION, "vault_paths": 1})
            .batch_size(128)
        )
        count = 0
        try:
            async for row in cursor:
                if not snapshot.permits_record(row):
                    paths.update(row.get("vault_paths") or [])
                    if collection == "conversations" and row.get("conversation_id"):
                        paths.add(f"Conversations/{row['conversation_id']}.md")
                        held_conversations.add(row["conversation_id"])
                count += 1
                if count % 32 == 0:
                    await asyncio.sleep(0)
        finally:
            await cursor.close()
    if held_conversations:
        audits = (
            await database()
            .memory_audit.find(
                {
                    "user_id": str(user_id),
                    "conversation_id": {"$in": list(held_conversations)},
                },
                {"note_path": 1},
            )
            .to_list(length=None)
        )
        paths.update(row["note_path"] for row in audits if row.get("note_path"))
    return paths


async def list_intervals(user_id, start, end):
    snapshot = await load_snapshot(user_id, start, end)
    output = []
    labels = {
        "excluded": "Private activity · excluded",
        "needs_review": "Private activity · needs review",
        "pending": "Awaiting privacy screening",
    }
    for source_id, source in snapshot.sources.items():
        low = utc(start)
        high = min(utc(end), utc(datetime.now(timezone.utc)))
        if high <= low:
            continue
        segments = snapshot._segments_in(source_id, low, high)
        overrides = [row for row in snapshot.overrides if row["source_id"] == source_id]
        requirements = [
            row for row in snapshot.required_ranges if row["source_id"] == source_id
        ]
        inventories = [
            row for row in snapshot.display_sets if row["source_id"] == source_id
        ]
        activated = (
            utc(source["privacy_enabled_from"])
            if source.get("privacy_enabled_from")
            else None
        )
        edges = {low, high}
        if activated is not None:
            edges.add(max(low, min(high, activated)))
        for inventory in inventories:
            edges.update(
                max(low, min(high, utc(inventory[key])))
                for key in ("transition_started_at", "observed_at")
            )
        for row in segments + overrides + requirements:
            edges.update(
                max(low, min(high, utc(row[key]))) for key in ("started_at", "ended_at")
            )
        points = sorted(edges)
        for a, b in zip(points, points[1:]):
            if snapshot.permits(source_id, a, b):
                continue
            decisions = [
                row
                for row in overrides
                if utc(row["started_at"]) <= a and utc(row["ended_at"]) >= b
            ]
            tracks, _, recorded = snapshot._display_requirements(
                activated, inventories, requirements, a, b
            )
            applicable = snapshot._applicable_segments(
                snapshot._segments_in(source_id, a, b), tracks, recorded, a, b
            )
            states = {row["state"] for row in applicable}
            explicit = bool(
                decisions
                and max(decisions, key=lambda row: row.get("revision", 0))["override"]
                == "excluded"
            )
            state = (
                "excluded"
                if explicit or "excluded" in states
                else "needs_review" if "needs_review" in states else "pending"
            )
            row = {
                "source_id": source_id,
                "source_name": source.get("name", source_id),
                "started_at": a,
                "ended_at": b,
                "revision": source.get("privacy_revision", 0),
                "state": state,
                "label": labels[state],
            }
            health = source.get("health", {}).get("privacy_screening", {})
            row["reason"] = (
                "Kept excluded by your review decision."
                if explicit
                else (
                    "Screening detected private activity."
                    if state == "excluded"
                    else (
                        "Screening is uncertain; processing is held for review."
                        if state == "needs_review"
                        else (
                            "Privacy settings are being updated."
                            if source.get("privacy_updating")
                            else (
                                "Historical screening is incomplete; unresolved time remains held."
                                if any(
                                    utc(r["started_at"]) <= a
                                    and utc(r["ended_at"]) >= b
                                    for r in requirements
                                )
                                else (
                                    "Screen recorder or display inventory is unavailable."
                                    if health.get("last_failure")
                                    == "display_inventory_unavailable"
                                    and health.get("state") == "unavailable"
                                    else (
                                        "Some original frames are missing; waiting for screen coverage."
                                        if health.get("last_failure") == "missing_frame"
                                        else "Waiting for screen coverage. Audio without coverage is held."
                                    )
                                )
                            )
                        )
                    )
                )
            )
            if (
                state == "pending"
                and not source.get("privacy_updating")
                and any(
                    r.get("coverage") == "awaiting_screening_worker"
                    and utc(r["started_at"]) <= a
                    and utc(r["ended_at"]) >= b
                    for r in requirements
                )
            ):
                row["reason"] = (
                    "Local screening is not ready; screen and audio processing remain held."
                )
            failures = {segment.get("reason") for segment in applicable}
            if state == "pending" and not source.get("privacy_updating"):
                if "missing_frame" in failures:
                    row["reason"] = (
                        "An original frame is unavailable; this portion remains held and will be retried."
                    )
                elif "screening_failed" in failures:
                    row["reason"] = (
                        "Screening failed for this portion; processing remains held while it retries."
                    )
                elif "capture_gap" in failures:
                    row["reason"] = (
                        "Screen captures are too far apart to establish coverage; screen and audio processing remain held."
                    )
            if (
                output
                and output[-1]["source_id"] == source_id
                and output[-1]["state"] == state
                and output[-1]["reason"] == row["reason"]
                and output[-1]["ended_at"] == a
            ):
                output[-1]["ended_at"] = b
            else:
                output.append(row)
    return output
