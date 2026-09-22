"""Deterministic evidence broker for one user's local day."""

import asyncio
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import backend.services.privacy as privacy
from backend.models.conversation import Conversation
from backend.models.device_input import DeviceInputItem
from backend.models.manual_memory import ManualMemory
from backend.models.timeline import (
    AudioEvidenceSpan,
    ImmichEvidenceSummary,
    ImmichEvidenceWindow,
    TimelineEpisode,
)
from backend.services.memory.visibility import (
    conversation_scope_filter,
    main_only_filter,
)
from backend.services.recording_purpose import personal_recording_filter
from backend.services.transcript_time import AnchorMap, load_anchor_map, place_segments

from . import activity_policy
from .contracts import (
    EvidenceAnchor,
    EvidenceBundle,
    EvidenceCoverage,
    EvidenceLocator,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)
from .source_ids import parse_screenpipe_segment_source_id, transcript_evidence_locator
from .timezone import canonical_timezone

SCREEN_EVIDENCE_CONTINUITY_GAP = timedelta(minutes=20)
TRANSCRIPT_BLOCK_MAX_DURATION = timedelta(minutes=5)
TRANSCRIPT_BLOCK_GAP = timedelta(seconds=90)
TRANSCRIPT_BLOCK_MAX_CHARS = 6000
AGENT_VISIBLE_TEMPORAL_SAMPLES = 12
_PROTECTED_CAPTURE_TRIGGERS = {
    "click",
    "typing_pause",
    "scroll_stop",
    "key_press",
    "clipboard",
    "visual_change",
    "manual",
    "idle",
    "locked",
    "blank",
    "drm_paused",
}


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def day_bounds(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone: {timezone_name}") from error
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _evidence_id(kind: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return f"{kind}:{hashlib.sha256(value.encode()).hexdigest()[:20]}"


def _overlaps(
    start: datetime, end: datetime | None, low: datetime, high: datetime
) -> bool:
    return utc(start) < high and utc(end or start) >= low


def adaptive_temporal_coverage(
    samples: list[dict[str, Any]], *, limit: int = AGENT_VISIBLE_TEMPORAL_SAMPLES
) -> list[dict[str, Any]]:
    """Thin chronologically without turning the tail into the whole activity.

    Boundary samples and explicit transitions/inactivity markers are structural
    evidence and therefore survive even when they exceed the ordinary agent budget.
    Remaining slots are spread across the full interval.
    """

    if limit <= 0:
        raise ValueError("temporal coverage limit must be positive")
    ordered = sorted(
        samples,
        key=lambda item: (
            _timestamp(item.get("captured_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("sample_id") or ""),
        ),
    )
    if len(ordered) <= limit:
        return ordered
    mandatory = {
        0,
        len(ordered) - 1,
        *(
            index
            for index, item in enumerate(ordered)
            if item.get("inactive")
            or item.get("capture_trigger") in _PROTECTED_CAPTURE_TRIGGERS
            or item.get("support_type") in {"transition", "capture_gap_edge"}
        ),
    }
    remaining = [index for index in range(len(ordered)) if index not in mandatory]
    slots = max(0, limit - len(mandatory))
    if slots and remaining:
        if slots >= len(remaining):
            mandatory.update(remaining)
        else:
            mandatory.update(
                (
                    remaining[round(position * (len(remaining) - 1) / (slots - 1))]
                    if slots > 1
                    else remaining[len(remaining) // 2]
                )
                for position in range(slots)
            )
    return [item for index, item in enumerate(ordered) if index in mandatory]


def _temporal_samples(row: DeviceInputItem) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index, sample in enumerate(row.samples):
        captured_at = _timestamp(sample.get("captured_at"))
        if captured_at is None:
            continue
        samples.append(
            {
                "sample_id": str(
                    sample.get("content_fingerprint")
                    or sample.get("frame_id")
                    or f"sample-{index}"
                ),
                "captured_at": captured_at.isoformat(),
                "support_type": "sample",
                "source_position": sample.get("frame_id") or index,
                "capture_trigger": sample.get("capture_trigger"),
                "inactive": bool(sample.get("inactive")),
                "liveness": bool(sample.get("liveness")),
                "text": str(sample.get("text") or "")[:2000],
                "content_fingerprint": sample.get("content_fingerprint"),
            }
        )
    for index, candidate in enumerate(row.frame_candidates):
        captured_at = _timestamp(candidate.get("captured_at"))
        if captured_at is None:
            continue
        samples.append(
            {
                "sample_id": f"frame:{candidate.get('frame_id', index)}",
                "captured_at": captured_at.isoformat(),
                "support_type": "frame",
                "source_position": candidate.get("frame_id"),
                "capture_trigger": candidate.get("capture_trigger"),
                "inactive": candidate.get("capture_trigger")
                in {"idle", "locked", "blank", "drm_paused"},
                "liveness": False,
                "text": "",
                "content_fingerprint": None,
            }
        )
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for sample in samples:
        key = (
            str(sample["captured_at"]),
            str(sample["support_type"]),
            str(sample["source_position"]),
        )
        deduplicated[key] = sample
    return sorted(
        deduplicated.values(),
        key=lambda item: (_timestamp(item["captured_at"]), item["sample_id"]),
    )


def _device_locator(row: DeviceInputItem) -> EvidenceLocator:
    return row.locator


def _audio_item(span: AudioEvidenceSpan) -> TimelineEvidenceItem:
    # Output describes a capture route, not proof that anything was playing.
    role = (
        "media_content"
        if span.direction == "output" and span.state == "transcribed"
        else "uncertain"
    )
    return TimelineEvidenceItem(
        evidence_id=f"audio_span:{span.id}",
        kind="audio_span",
        source_id=span.source_id,
        source_item_id=span.first_source_item_id,
        locator=span.locator,
        started_at=utc(span.started_at),
        ended_at=utc(span.ended_at),
        role=role,
        content_hash=span.source_range_hash,
        metadata={
            "direction": span.direction,
            "meeting_id": span.meeting_id,
            "state": span.state,
            "speech_seconds": span.speech_seconds,
            "acoustic_active_seconds": span.acoustic_active_seconds,
            "acoustic_quiet_seconds": span.acoustic_quiet_seconds,
            "covered_seconds": span.covered_seconds,
            "missing_seconds": span.missing_seconds,
            "bucket_seconds": span.bucket_seconds,
            "coverage_fraction": span.coverage_fraction,
            "speech_fraction": span.speech_fraction,
            "acoustic_active_fraction": span.acoustic_active_fraction,
            "rms_dbfs": span.rms_dbfs,
            "peak_dbfs": span.peak_dbfs,
            "longest_no_speech_seconds": span.longest_no_speech_seconds,
            "conversation_id": span.conversation_id,
        },
    )


def _device_item(row: DeviceInputItem) -> TimelineEvidenceItem:
    if row.kind == "immich_memory":
        kind, role = "immich", "user_action"
    elif row.metadata.get("meeting_id"):
        kind, role = "meeting", "application_state"
    else:
        kind, role = "observation", "application_state"
    text_parts = [
        str(row.metadata.get(key) or "")
        for key in (
            "app_name",
            "window_name",
            "caption",
            "description",
            "text",
            "summary",
        )
    ]
    if row.kind == "immich_memory":
        # Visual enrichment is deliberately text-only at reconciliation time. Keep
        # useful OCR beside the description, but bound it before the generic excerpt
        # cap so one photographed document cannot crowd out the surrounding window.
        text_parts.append(str(row.metadata.get("ocr_text") or "")[:2000])
        photo = row.metadata.get("photo_metadata") or {}
        if not row.metadata.get("description"):
            text_parts.append(
                "Photo metadata only; pixels not yet inspected. Do not infer visible contents."
            )
        text_parts.extend(str(photo.get(k) or "") for k in ("city", "country"))
        if photo.get("people"):
            text_parts.append(
                "Immich person associations: "
                + ", ".join(p["name"] for p in photo["people"] if p.get("name"))
            )
    temporal_samples = _temporal_samples(row)
    visible_samples = adaptive_temporal_coverage(temporal_samples)
    if visible_samples:
        text_parts.extend(
            str(sample.get("text") or "")
            for sample in visible_samples
            if sample.get("text")
        )
    excerpt = (
        " · ".join(part.strip() for part in text_parts if part.strip())[:6000] or None
    )
    # ``description`` is already in the excerpt and ``ocr_text`` can run to thousands of
    # characters, so neither belongs in the analysis payload a second time.
    metadata = {
        key: value
        for key, value in row.metadata.items()
        if key not in {"text", "summary", "description", "ocr_text"}
    }
    return TimelineEvidenceItem(
        evidence_id=f"{kind}:{row.id}",
        kind=kind,
        source_id=row.source_id,
        source_item_id=row.source_item_id,
        locator=_device_locator(row),
        started_at=utc(row.captured_at),
        ended_at=utc(row.ended_at) if row.ended_at else None,
        role=role,
        excerpt=excerpt,
        content_hash=row.content_hash or row.curation_revision,
        ephemeral=bool(row.media_data),
        metadata={
            **metadata,
            "source_kind": row.kind,
            "observation_scope": (
                "coarse_application_session" if row.kind == "observation" else None
            ),
            "sample_count": len(row.samples),
            "sample_fingerprints": [
                sample.get("content_fingerprint")
                for sample in visible_samples
                if sample.get("content_fingerprint")
            ],
            "frame_candidates": row.frame_candidates,
            "temporal_samples": temporal_samples,
            "agent_visible_samples": visible_samples,
            "curation": row.curation,
            "image_content_type": row.media_content_type if row.media_data else None,
        },
        image_filename=f"{kind}-{row.id}" if row.media_data else None,
        coverage=EvidenceCoverage(
            source_count=len(row.samples) + len(row.frame_candidates),
            retained_count=len(temporal_samples),
            agent_visible_count=len(visible_samples),
        ),
    )


def _manual_memory_item(memory: ManualMemory) -> TimelineEvidenceItem:
    descriptions = [item.description for item in memory.attachments if item.description]
    excerpt = memory.note or (descriptions[0] if descriptions else "Manual memory.")
    return TimelineEvidenceItem(
        evidence_id=f"manual_memory:{memory.memory_id}",
        kind="frame",
        source_id="manual",
        source_item_id=memory.memory_id,
        locator=EvidenceLocator(
            capture_source_id="manual", modality="photo", track_id=None
        ),
        started_at=utc(memory.shared_at),
        role="user_action",
        excerpt=excerpt[:6000],
        content_hash=(
            memory.attachments[0].content_hash if memory.attachments else None
        ),
        metadata={
            "source_kind": "manual_memory",
            "memory_id": memory.memory_id,
            "memory_at": memory.memory_at,
            "source_application": memory.source.get("application"),
            "attachment_count": len(memory.attachments),
        },
    )


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if not value:
        return None
    try:
        return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _device_items(row: DeviceInputItem) -> list[TimelineEvidenceItem]:
    """Materialize supported screen intervals from one durable device row.

    Observation state is intentionally sparse: initial/novel/liveness samples prove
    continuity without storing every captured frame. A collector interruption can leave
    one observation open across a long wall-clock gap, however. Split those unsupported
    gaps here so historical rows cannot claim activity while a source was offline.
    Meeting intervals have their own liveness/closure state machine and remain intact.
    """

    base = _device_item(row)
    if row.kind not in {"activity", "observation"} or base.kind == "meeting":
        return [base]

    start = utc(row.captured_at)
    provisional = row.ended_at is None
    if provisional:
        supported_markers = [
            marker
            for source in (*row.samples, *row.frame_candidates)
            if (marker := _timestamp(source.get("captured_at"))) is not None
            and marker >= start
        ]
        end = max(supported_markers, default=start)
        if end > start:
            base.ended_at = end
            base.metadata["provisional_end"] = True
            base.metadata["provisional_end_source"] = "latest_screen_marker"
    else:
        end = utc(row.ended_at)
    if end <= start:
        return [base]

    markers = {start, end}
    for sample in row.samples:
        marker = _timestamp(sample.get("captured_at"))
        if marker is not None and start <= marker <= end:
            markers.add(marker)
    for candidate in row.frame_candidates:
        marker = _timestamp(candidate.get("captured_at"))
        if marker is not None and start <= marker <= end:
            markers.add(marker)

    ordered = sorted(markers)
    groups: list[list[datetime]] = [[ordered[0]]]
    for marker in ordered[1:]:
        if marker - groups[-1][-1] > SCREEN_EVIDENCE_CONTINUITY_GAP:
            groups.append([marker])
        else:
            groups[-1].append(marker)
    if len(groups) == 1:
        return [base]

    image_candidate = next(
        (
            candidate
            for candidate in row.frame_candidates
            if candidate.get("frame_id") == row.metadata.get("preview_frame_id")
        ),
        row.frame_candidates[0] if row.frame_candidates else None,
    )
    image_captured_at = (
        _timestamp(image_candidate.get("captured_at")) if image_candidate else None
    )
    result: list[TimelineEvidenceItem] = []
    for index, group in enumerate(groups):
        item = base.model_copy(deep=True)
        item.evidence_id = f"{base.evidence_id}:segment:{index}"
        item.started_at = group[0]
        item.ended_at = group[-1] if group[-1] > group[0] else None
        segment_samples = [
            sample
            for sample in row.samples
            if (marker := _timestamp(sample.get("captured_at"))) is not None
            and group[0] <= marker <= group[-1]
        ]
        segment_candidates = [
            candidate
            for candidate in row.frame_candidates
            if (marker := _timestamp(candidate.get("captured_at"))) is not None
            and group[0] <= marker <= group[-1]
        ]
        segment_temporal_samples = [
            sample
            for sample in base.metadata.get("temporal_samples", [])
            if (marker := _timestamp(sample.get("captured_at"))) is not None
            and group[0] <= marker <= group[-1]
        ]
        visible_samples = adaptive_temporal_coverage(segment_temporal_samples)
        text_parts = [
            str(row.metadata.get(key) or "")
            for key in ("app_name", "window_name", "browser_url")
        ]
        if index == 0:
            text_parts.extend(
                str(row.metadata.get(key) or "") for key in ("text", "summary")
            )
        text_parts.extend(
            str(sample.get("text") or "")
            for sample in visible_samples
            if sample.get("text")
        )
        item.excerpt = (
            " · ".join(part.strip() for part in text_parts if part.strip())[:6000]
            or None
        )
        item.content_hash = hashlib.sha256(
            f"{base.content_hash}:{item.started_at.isoformat()}:"
            f"{item.ended_at.isoformat() if item.ended_at else ''}".encode()
        ).hexdigest()
        item.metadata["continuity_segment"] = index
        item.metadata["continuity_segment_count"] = len(groups)
        item.metadata["continuity_marker_count"] = len(group)
        item.metadata["sample_count"] = len(segment_samples)
        item.metadata["sample_fingerprints"] = [
            sample.get("content_fingerprint")
            for sample in visible_samples
            if sample.get("content_fingerprint")
        ]
        item.metadata["frame_candidates"] = segment_candidates
        item.metadata["temporal_samples"] = segment_temporal_samples
        item.metadata["agent_visible_samples"] = visible_samples
        item.coverage = EvidenceCoverage(
            source_count=len(segment_samples) + len(segment_candidates),
            retained_count=len(segment_temporal_samples),
            agent_visible_count=len(visible_samples),
        )
        segment_has_image = bool(item.image_filename) and (
            (image_captured_at is None and index == 0)
            or (
                image_captured_at is not None
                and group[0] <= image_captured_at <= group[-1]
            )
        )
        if segment_has_image:
            item.image_filename = f"{item.image_filename}-segment-{index}"
        else:
            item.image_filename = None
            item.ephemeral = False
            item.metadata["image_content_type"] = None
        result.append(item)
    return result


def _coalesce_application_evidence(
    items: list[TimelineEvidenceItem],
) -> list[TimelineEvidenceItem]:
    """Compact each capture source independently, then merge for the user.

    Mongo's user/timestamp index is descending, and multiple devices naturally
    interleave. Sorting by source stream before compaction prevents negative gaps and
    ensures one computer never changes another computer's screen boundaries.
    """

    ordered = sorted(
        items,
        key=lambda item: (
            item.locator.capture_source_id if item.locator else item.source_id or "",
            item.locator.track_id if item.locator and item.locator.track_id else "",
            str(item.metadata.get("source_kind") or ""),
            utc(item.started_at),
            item.evidence_id,
        ),
    )
    result: list[TimelineEvidenceItem] = []
    for item in ordered:
        source_kind = item.metadata.get("source_kind")
        app_key = (
            item.locator.capture_source_id if item.locator else item.source_id,
            item.locator.track_id if item.locator else None,
            source_kind,
            item.metadata.get("app_name"),
            item.metadata.get("window_name"),
            item.metadata.get("browser_url"),
        )
        previous = result[-1] if result else None
        previous_key = (
            (
                (
                    previous.locator.capture_source_id
                    if previous.locator
                    else previous.source_id
                ),
                previous.locator.track_id if previous.locator else None,
                previous.metadata.get("source_kind"),
                previous.metadata.get("app_name"),
                previous.metadata.get("window_name"),
                previous.metadata.get("browser_url"),
            )
            if previous
            else None
        )
        previous_end = (
            utc(previous.ended_at or previous.started_at) if previous else None
        )
        gap = utc(item.started_at) - previous_end if previous_end is not None else None
        if (
            previous
            and source_kind in {"activity", "screen_context"}
            and app_key == previous_key
            and gap is not None
            and timedelta(0) <= gap <= timedelta(seconds=60)
            and not previous.image_filename
            and not item.image_filename
        ):
            previous.ended_at = max(previous_end, utc(item.ended_at or item.started_at))
            excerpts = [value for value in (previous.excerpt, item.excerpt) if value]
            previous.excerpt = "\n".join(dict.fromkeys(excerpts))[-6000:] or None
            previous.metadata["coalesced_count"] = (
                int(previous.metadata.get("coalesced_count") or 1) + 1
            )
            temporal_samples = sorted(
                [
                    *(previous.metadata.get("temporal_samples") or []),
                    *(item.metadata.get("temporal_samples") or []),
                ],
                key=lambda sample: _timestamp(sample.get("captured_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            visible_samples = adaptive_temporal_coverage(temporal_samples)
            previous.metadata["temporal_samples"] = temporal_samples
            previous.metadata["agent_visible_samples"] = visible_samples
            previous.coverage = EvidenceCoverage(
                source_count=previous.coverage.source_count
                + item.coverage.source_count,
                retained_count=len(temporal_samples),
                agent_visible_count=len(visible_samples),
            )
            previous.content_hash = hashlib.sha256(
                f"{previous.content_hash}:{item.content_hash}:{item.evidence_id}".encode()
            ).hexdigest()
            continue
        result.append(item.model_copy(deep=True))
    return sorted(result, key=lambda item: (utc(item.started_at), item.evidence_id))


def _clip_evidence_to_range(
    items: list[TimelineEvidenceItem], low: datetime, high: datetime
) -> list[TimelineEvidenceItem]:
    """Clip every source to the user's requested local-day range."""

    clipped: list[TimelineEvidenceItem] = []
    for original in items:
        start = utc(original.started_at)
        end = utc(original.ended_at) if original.ended_at else None
        if end is None or end <= start:
            if low <= start < high:
                item = original.model_copy(deep=True)
                item.started_at = start
                item.ended_at = None
                clipped.append(item)
            continue
        bounded_start = max(start, low)
        bounded_end = min(end, high)
        if bounded_end <= bounded_start:
            continue
        item = original.model_copy(deep=True)
        item.started_at = bounded_start
        item.ended_at = bounded_end
        if bounded_start != start or bounded_end != end:
            item.metadata["clipped_to_day"] = True
        clipped.append(item)
    return clipped


def _anchor_id(
    evidence_id: str,
    support_type: str,
    source_position: str | int | None,
    earliest_at: datetime,
    latest_at: datetime,
) -> str:
    return _evidence_id(
        "anchor",
        evidence_id,
        support_type,
        source_position,
        utc(earliest_at).isoformat(),
        utc(latest_at).isoformat(),
    )


def build_evidence_anchors(
    evidence: list[TimelineEvidenceItem],
) -> list[EvidenceAnchor]:
    """Build the full manifest anchor table before any agent-facing thinning."""

    anchors: list[EvidenceAnchor] = []
    for item in evidence:
        locator = item.locator
        item.locator = locator
        item.anchor_ids = []
        if item.kind == "capture_gap" and item.metadata.get("within_span"):
            # The envelope locates the source span, not the missing seconds.
            # It cannot support exact start/end boundaries for a gap.
            continue
        boundary_type = (
            "transcript_edge"
            if item.kind == "transcript"
            else (
                "capture_gap_edge"
                if item.kind == "capture_gap"
                else (
                    "user_edit"
                    if item.metadata.get("source_kind") == "manual_memory"
                    else "transition" if item.kind == "observation" else "source_edge"
                )
            )
        )
        bounds = [("start", utc(item.started_at))]
        if item.ended_at is not None and utc(item.ended_at) != utc(item.started_at):
            bounds.append(("end", utc(item.ended_at)))
        for position, moment in bounds:
            anchor = EvidenceAnchor(
                anchor_id=_anchor_id(
                    item.evidence_id, boundary_type, position, moment, moment
                ),
                evidence_id=item.evidence_id,
                locator=locator,
                support_type=boundary_type,
                earliest_at=moment,
                latest_at=moment,
                source_position=position,
            )
            anchors.append(anchor)
            item.anchor_ids.append(anchor.anchor_id)

        item_start = utc(item.started_at)
        item_end = utc(item.ended_at or item.started_at)
        retained_samples = [
            sample
            for sample in item.metadata.get("temporal_samples", [])
            if (moment := _timestamp(sample.get("captured_at"))) is not None
            and item_start <= moment <= item_end
        ]
        visible_samples = adaptive_temporal_coverage(retained_samples)
        item.metadata["temporal_samples"] = retained_samples
        item.metadata["agent_visible_samples"] = visible_samples
        if retained_samples:
            item.coverage.retained_count = len(retained_samples)
            item.coverage.agent_visible_count = len(visible_samples)
        for sample in retained_samples:
            moment = _timestamp(sample.get("captured_at"))
            if moment is None:
                continue
            support_type = sample.get("support_type") or "sample"
            anchor = EvidenceAnchor(
                anchor_id=_anchor_id(
                    item.evidence_id,
                    support_type,
                    sample.get("source_position"),
                    moment,
                    moment,
                ),
                evidence_id=item.evidence_id,
                locator=locator,
                support_type=support_type,
                earliest_at=moment,
                latest_at=moment,
                source_position=sample.get("source_position"),
            )
            if anchor.anchor_id not in item.anchor_ids:
                anchors.append(anchor)
                item.anchor_ids.append(anchor.anchor_id)
    return anchors


async def _conversation_audio_bounds(
    user_id: str, day_start: datetime, range_end: datetime
) -> dict[str, tuple[datetime, datetime]]:
    """Absolute audio bounds per conversation with audio inside the day.

    Selection is by audio time rather than by source: continuous screen capture,
    a wearable, and a phone recording are all streamed audio and must reach the
    agent the same way. A conversation whose chunks carry no ``captured_at`` is
    absent from the result and therefore not placed on any day -- an upload's
    ``created_at`` is when the file arrived, which would file it on the wrong one.

    Conversation claims, not raw chunks, define which speech-bearing semantic objects
    belong on the day. Raw capture may continue through silence without producing a
    recording at all.
    """
    collection = Conversation.get_pymongo_collection()
    rows = await collection.find(
        {
            "$and": [conversation_scope_filter()],
            "user_id": user_id,
            "deleted": {"$ne": True},
            **personal_recording_filter(),
            "audio_ranges": {
                "$elemMatch": {
                    "started_at": {"$lt": range_end},
                    "ended_at": {"$gt": day_start},
                    "time_basis": {"$ne": "unknown"},
                }
            },
        },
        {"conversation_id": 1, "audio_ranges": 1},
    ).to_list(length=None)
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for row in rows:
        overlapping = [
            audio_range
            for audio_range in row.get("audio_ranges", [])
            if utc(audio_range["started_at"]) < range_end
            and utc(audio_range["ended_at"]) > day_start
            and audio_range.get("time_basis") != "unknown"
        ]
        conversation_id = str(row.get("conversation_id") or "")
        if conversation_id and overlapping:
            bounds[conversation_id] = (
                min(utc(item["started_at"]) for item in overlapping),
                max(utc(item["ended_at"]) for item in overlapping),
            )
    return bounds


def _transcript_item(
    conversation: Conversation,
    bounds: tuple[datetime, datetime],
    meeting_id: str | None = None,
) -> TimelineEvidenceItem | None:
    transcript = (conversation.transcript or "").strip()
    if not transcript:
        return None
    # Bounds come from the chunks' immutable ``captured_at``, never from
    # ``created_at``: for a re-bound child that is the operation time, and this
    # deployment holds recordings whose two differ by six days.
    started_at, ended_at = bounds
    direction = _transcript_direction(conversation)
    role = "media_content" if direction == "output" else "uncertain"
    version = conversation.active_transcript
    locator = _transcript_locator(conversation, direction)
    return TimelineEvidenceItem(
        evidence_id=f"transcript:{conversation.conversation_id}",
        kind="transcript",
        source_id=locator.capture_source_id,
        source_item_id=conversation.conversation_id,
        locator=locator,
        started_at=started_at,
        ended_at=ended_at,
        role=role,
        # Speaker-attributed, so the agent can name who spoke rather than inferring it.
        # This replaces a parallel `segments` blob that repeated the same text with
        # per-segment timestamps — 119KB for one conversation, and the largest single
        # contributor to a workspace too big for the agent to read.
        excerpt=_attributed_transcript(version, transcript),
        content_hash=(version.version_id if version else None),
        metadata={
            "direction": direction,
            "conversation_id": conversation.conversation_id,
            **({"meeting_id": meeting_id} if meeting_id else {}),
            "speakers": sorted(
                {
                    segment.speaker
                    for segment in (version.segments if version else [])
                    if segment.speaker
                }
            ),
        },
    )


def _transcript_items(
    conversation: Conversation,
    bounds: tuple[datetime, datetime],
    anchors: AnchorMap,
    meeting_id: str | None = None,
) -> list[TimelineEvidenceItem]:
    """Wall-clock transcript blocks small enough for semantic internal boundaries.

    A whole recording used to be one evidence item. The validator then snapped any
    proposed boundary back to that item's outer bounds, making a 73-minute recording
    effectively indivisible. Timestamped blocks preserve speaker text while offering
    the final agent real silence/topic cut points; it may still merge adjacent blocks.
    """

    started_at, ended_at = (utc(bounds[0]), utc(bounds[1]))
    placed = [
        segment
        for segment in place_segments(conversation, anchors)
        if segment.started_at < ended_at and segment.ended_at > started_at
    ]
    if not placed:
        fallback = _transcript_item(conversation, bounds, meeting_id)
        return [fallback] if fallback is not None else []

    groups: list[list[Any]] = []
    current: list[Any] = []
    current_chars = 0
    for segment in placed:
        line = f"{segment.label}: {segment.text}".strip()
        split = bool(current) and (
            segment.started_at - current[-1].ended_at > TRANSCRIPT_BLOCK_GAP
            or segment.ended_at - current[0].started_at > TRANSCRIPT_BLOCK_MAX_DURATION
            or current_chars + len(line) + 1 > TRANSCRIPT_BLOCK_MAX_CHARS
        )
        if split:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += len(line) + 1
    if current:
        groups.append(current)

    direction = _transcript_direction(conversation)
    role = "media_content" if direction == "output" else "uncertain"
    version = conversation.active_transcript
    version_id = version.version_id if version else "unversioned"
    locator = _transcript_locator(conversation, direction)
    result: list[TimelineEvidenceItem] = []
    for index, group in enumerate(groups):
        excerpt = "\n".join(
            f"{segment.label}: {segment.text}" for segment in group if segment.text
        )[:TRANSCRIPT_BLOCK_MAX_CHARS]
        block_start = max(started_at, group[0].started_at)
        block_end = min(ended_at, group[-1].ended_at)
        content_hash = hashlib.sha256(
            f"{version_id}:{block_start.isoformat()}:{block_end.isoformat()}:{excerpt}".encode()
        ).hexdigest()
        result.append(
            TimelineEvidenceItem(
                evidence_id=_evidence_id(
                    "transcript_block",
                    conversation.conversation_id,
                    version_id,
                    block_start.isoformat(),
                    block_end.isoformat(),
                ),
                kind="transcript",
                source_id=locator.capture_source_id,
                source_item_id=conversation.conversation_id,
                locator=locator,
                started_at=block_start,
                ended_at=block_end,
                role=role,
                excerpt=excerpt or None,
                content_hash=content_hash,
                metadata={
                    "direction": direction,
                    "conversation_id": conversation.conversation_id,
                    **({"meeting_id": meeting_id} if meeting_id else {}),
                    "speakers": sorted({segment.label for segment in group}),
                    "segment_count": len(group),
                    "transcript_block_index": index,
                    "transcript_block_count": len(groups),
                },
            )
        )
    return result


def _transcript_direction(conversation: Conversation) -> str:
    external = getattr(conversation, "external_source_id", None)
    parsed = parse_screenpipe_segment_source_id(external)
    if parsed is not None:
        return parsed.direction
    source_parts = str(external or "").split(":")
    return source_parts[2] if len(source_parts) >= 3 else "unknown"


def _transcript_locator(conversation: Conversation, direction: str) -> EvidenceLocator:
    return transcript_evidence_locator(
        getattr(conversation, "external_source_id", None),
        getattr(conversation, "client_id", None),
        conversation.conversation_id,
        direction,
    )


def _attributed_transcript(version: Any, fallback: str) -> str:
    """`speaker: text` lines, falling back to the plain transcript when undiarized."""

    segments = version.segments if version else []
    if not segments:
        return fallback[:30000]
    lines: list[str] = []
    budget = 30000
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        line = f"{segment.speaker or 'unknown'}: {text}"
        if len(line) > budget:
            lines.append(line[:budget])
            break
        lines.append(line)
        budget -= len(line) + 1
    return "\n".join(lines) or fallback[:30000]


def _window_items(
    started_at: datetime,
    ended_at: datetime,
    window_minutes: int,
    overlap_minutes: int,
    evidence: list[TimelineEvidenceItem],
) -> list[TimelineCoverageWindow]:
    width = timedelta(minutes=window_minutes)
    step = timedelta(minutes=window_minutes - overlap_minutes)
    windows: list[TimelineCoverageWindow] = []
    cursor = started_at
    while cursor < ended_at:
        window_end = min(ended_at, cursor + width)
        window_id = f"window:{cursor.isoformat()}:{window_end.isoformat()}"
        ids = [
            item.evidence_id
            for item in evidence
            if _overlaps(item.started_at, item.ended_at, cursor, window_end)
        ]
        if ids:
            windows.append(
                TimelineCoverageWindow(
                    window_id=window_id,
                    started_at=cursor,
                    ended_at=window_end,
                    evidence_ids=ids,
                )
            )
        cursor += step
    return windows


def summarize_immich_evidence(
    manifest: TimelineEvidenceManifest,
) -> ImmichEvidenceSummary:
    """Describe where useful Immich evidence actually entered this manifest."""

    immich = {
        item.evidence_id: item for item in manifest.evidence if item.kind == "immich"
    }
    helpful = {
        evidence_id
        for evidence_id, item in immich.items()
        if item.metadata.get("timeline_relevance") in {"high", "medium"}
    }
    windows: list[ImmichEvidenceWindow] = []
    for window in manifest.windows:
        asset_ids = [item for item in window.evidence_ids if item in immich]
        if not asset_ids:
            continue
        windows.append(
            ImmichEvidenceWindow(
                started_at=window.started_at,
                ended_at=window.ended_at,
                asset_count=len(asset_ids),
                helpful_asset_count=sum(item in helpful for item in asset_ids),
            )
        )
    return ImmichEvidenceSummary(
        evidence_count=len(immich),
        helpful_evidence_count=len(helpful),
        window_count=len(windows),
        windows=windows,
    )


def _parse_device_input_rows(rows: list[dict[str, Any]]) -> list[DeviceInputItem]:
    return [DeviceInputItem.model_validate(row) for row in rows]


def _build_application_evidence(
    rows: list[DeviceInputItem],
) -> tuple[list[TimelineEvidenceItem], dict[str, bytes]]:
    """Expand every device row into screen evidence, then compact it.

    Hydrating the rows off-loop is not enough on its own: expanding them is pure
    Python over thousands of nested samples, and lazy-model attribute access parses
    on first touch, so the cost simply moves here. Measured on this deployment at
    1.7-2.8s per run on the event loop. Nothing here awaits, so it belongs beside
    the hydration in a worker thread.
    """

    device_evidence: list[TimelineEvidenceItem] = []
    images: dict[str, bytes] = {}
    for row in rows:
        items = _device_items(row)
        device_evidence.extend(items)
        if row.media_data:
            images.update(
                {
                    item.evidence_id: row.media_data
                    for item in items
                    if item.image_filename
                }
            )
    return _coalesce_application_evidence(device_evidence), images


async def _device_input_rows(
    user_id: str, day_start: datetime, range_end: datetime
) -> list[DeviceInputItem]:
    """Fetch raw evidence asynchronously and move Pydantic hydration off-loop.

    A screen-heavy day contains thousands of nested samples. Beanie normally performs
    their CPU-heavy model construction inside ``Cursor.to_list`` on the backend event
    loop, which pauses live requests for seconds. BSON I/O stays asynchronous; only the
    pure model hydration moves to a worker thread.
    """

    collection = DeviceInputItem.get_pymongo_collection()
    raw_rows = await collection.find(
        {
            "user_id": user_id,
            "kind": {"$ne": "audio"},
            "captured_at": {"$lt": range_end},
            "$or": [{"ended_at": None}, {"ended_at": {"$gte": day_start}}],
        }
    ).to_list(length=None)

    raw_rows = await privacy.filter_records(raw_rows, user_id)
    return await asyncio.to_thread(_parse_device_input_rows, raw_rows)


def _day_transcript_items(
    conversations: list[Conversation],
    anchors: list[AnchorMap],
    audio_bounds: dict[str, tuple[datetime, datetime]],
    meeting_ids: dict[str, str],
    day_start: datetime,
    range_end: datetime,
) -> list[TimelineEvidenceItem]:
    """Slice each conversation's transcript into the day's evidence blocks.

    Transcripts are the largest documents a day cites, and blocking them out is pure
    Python per segment, so this runs off the loop like the screen evidence above. The
    anchor maps it needs are awaited by the caller first.
    """

    items: list[TimelineEvidenceItem] = []
    for conversation, anchor_map in zip(conversations, anchors, strict=True):
        for item in _transcript_items(
            conversation,
            audio_bounds[conversation.conversation_id],
            anchor_map,
            meeting_ids.get(conversation.conversation_id),
        ):
            if _overlaps(item.started_at, item.ended_at, day_start, range_end):
                items.append(item)
    return items


async def _assemble_range_manifest(
    user_id: str,
    local_date: date,
    timezone_name: str,
    day_start: datetime,
    range_end: datetime,
    window_minutes: int,
    overlap_minutes: int,
) -> tuple[TimelineEvidenceManifest, dict[str, bytes]]:
    """Assemble one manifest over an arbitrary absolute ``[day_start, range_end)``.

    Every query is bounded by the requested range. ``local_date`` and
    ``timezone_name`` are recorded on the manifest as projection hints only; they never
    select evidence.
    """

    spans = await AudioEvidenceSpan.find(
        AudioEvidenceSpan.user_id == user_id,
        AudioEvidenceSpan.started_at < range_end,
        AudioEvidenceSpan.ended_at >= day_start,
    ).to_list()
    rows = await _device_input_rows(user_id, day_start, range_end)
    manual_memories = await ManualMemory.find(
        ManualMemory.user_id == user_id,
        main_only_filter(),
        ManualMemory.shared_at >= day_start,
        ManualMemory.shared_at < range_end,
    ).to_list()
    audio_bounds = await _conversation_audio_bounds(user_id, day_start, range_end)
    conversations = await Conversation.find(
        Conversation.user_id == user_id,
        conversation_scope_filter(),
        {"conversation_id": {"$in": sorted(audio_bounds)}},
        # A deleted recording is not evidence. Without this an episode can cite —
        # and a promoted recording can point at — audio the user can no longer play,
        # which is what a duplicate sweep leaves behind.
        {"deleted": {"$ne": True}},
        # Mining/audit clips are dataset material, not something that happened to
        # the user on this day. They are also the one corpus with no capture time.
        personal_recording_filter(),
    ).to_list()

    spans = await privacy.filter_records(spans, user_id)
    conversations = await privacy.filter_records(conversations, user_id)
    application_evidence, images = await asyncio.to_thread(
        _build_application_evidence, rows
    )

    evidence = [_audio_item(span) for span in spans]
    evidence.extend(application_evidence)
    evidence.extend(_manual_memory_item(memory) for memory in manual_memories)
    meeting_id_sets: dict[str, set[str]] = {}
    for span in spans:
        if span.conversation_id and span.meeting_id:
            meeting_id_sets.setdefault(span.conversation_id, set()).add(span.meeting_id)
    meeting_ids = {
        conversation_id: next(iter(values))
        for conversation_id, values in meeting_id_sets.items()
        if len(values) == 1
    }
    transcript_anchors = await asyncio.gather(
        *(
            load_anchor_map(conversation.conversation_id)
            for conversation in conversations
        )
    )
    evidence.extend(
        await asyncio.to_thread(
            _day_transcript_items,
            conversations,
            transcript_anchors,
            audio_bounds,
            meeting_ids,
            day_start,
            range_end,
        )
    )

    for span in spans:
        if span.missing_seconds <= 0:
            continue
        evidence.append(
            TimelineEvidenceItem(
                evidence_id=_evidence_id("capture_gap", span.id, span.missing_seconds),
                kind="capture_gap",
                source_id=span.source_id,
                locator=EvidenceLocator(
                    capture_source_id=span.source_id,
                    modality="audio",
                    track_id=span.direction,
                ),
                started_at=utc(span.started_at),
                ended_at=utc(span.ended_at),
                role="uncertain",
                excerpt=(
                    f"{span.missing_seconds:g} seconds are not covered by source recording timestamps within this span. Exact gap positions are unavailable. Silence and paused playback are not capture gaps when recording continues."
                ),
                metadata={"missing_seconds": span.missing_seconds, "within_span": True},
            )
        )

    evidence = _clip_evidence_to_range(evidence, day_start, range_end)
    evidence.sort(key=lambda item: (item.started_at, item.evidence_id))
    evidence_anchors = build_evidence_anchors(evidence)
    evidence_ids = {item.evidence_id for item in evidence}
    images = {
        evidence_id: data
        for evidence_id, data in images.items()
        if evidence_id in evidence_ids
    }
    windows = _window_items(
        day_start, range_end, window_minutes, overlap_minutes, evidence
    )
    revision_payload = [
        {
            "id": item.evidence_id,
            "start": item.started_at.isoformat(),
            "end": item.ended_at.isoformat() if item.ended_at else None,
            "hash": item.content_hash,
            "excerpt": item.excerpt,
            "metadata": item.metadata,
            "locator": item.locator.model_dump(mode="json") if item.locator else None,
            "anchor_ids": item.anchor_ids,
            "coverage": item.coverage.model_dump(mode="json"),
        }
        for item in evidence
    ]
    revision_payload.append(
        {
            "anchors": [
                anchor.model_dump(mode="json")
                for anchor in sorted(
                    evidence_anchors, key=lambda value: value.anchor_id
                )
            ]
        }
    )
    revision = hashlib.sha256(
        json.dumps(
            revision_payload, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()
    manifest = TimelineEvidenceManifest(
        user_id=user_id,
        local_date=local_date,
        timezone=timezone_name,
        started_at=day_start,
        ended_at=range_end,
        evidence_revision=revision,
        windows=windows,
        evidence=evidence,
        anchors=evidence_anchors,
    )
    return manifest, images


async def assemble_day_evidence(
    user_id: str,
    local_date: date,
    timezone_name: str,
    *,
    window_minutes: int = 20,
    overlap_minutes: int = 3,
    now: datetime | None = None,
) -> tuple[TimelineEvidenceManifest, dict[str, bytes]]:
    timezone_name = canonical_timezone(timezone_name)
    day_start, day_end = day_bounds(local_date, timezone_name)
    checked_at = utc(now or datetime.now(timezone.utc))
    range_end = (
        min(day_end, checked_at) if day_start <= checked_at < day_end else day_end
    )
    if range_end <= day_start:
        range_end = day_end
    return await _assemble_range_manifest(
        user_id,
        local_date,
        timezone_name,
        day_start,
        range_end,
        window_minutes,
        overlap_minutes,
    )


def _existing_episode_payload(episodes: list[TimelineEpisode]) -> list[dict[str, Any]]:
    """Supply exact lineage and every human-owned field to both stage validators."""
    fields = {
        "episode_id",
        "episode_key",
        "revision",
        "started_at",
        "ended_at",
        "kind",
        "title",
        "summary",
        "confirmed_fields",
    }
    return [
        {
            **episode.model_dump(
                mode="json", include=fields | set(episode.confirmed_fields)
            ),
            "evidence_ids": [ref.evidence_id for ref in episode.evidence_refs],
        }
        for episode in episodes
    ]


def _range_episode_query(
    user_id: str, started_at: datetime, ended_at: datetime
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "started_at": {"$lt": ended_at},
        "ended_at": {"$gt": started_at},
    }


async def load_reconciliation_evidence(
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
    *,
    timezone_name: str,
    evidence_revision: int = 0,
    window_minutes: int = 20,
    overlap_minutes: int = 3,
    now: datetime | None = None,
) -> EvidenceBundle:
    """Bounded range evidence for one reconciliation run.

    ``started_at``/``ended_at`` are absolute UTC and need not align to a local day. The
    manifest's ``local_date``/``timezone`` are derived from the range start as
    projection hints; its string ``evidence_revision`` remains the content hash of the
    assembled evidence, which is a different thing from the integer dirty-range counter
    carried on the bundle and passed through here.
    """

    timezone_name = canonical_timezone(timezone_name)
    range_start = utc(started_at)
    range_end = utc(ended_at)
    checked_at = utc(now) if now is not None else None
    if checked_at is not None and range_start < checked_at < range_end:
        range_end = checked_at
    if range_end <= range_start:
        raise ValueError("reconciliation range must be positive")

    local_date = range_start.astimezone(ZoneInfo(timezone_name)).date()
    manifest, _images = await _assemble_range_manifest(
        user_id,
        local_date,
        timezone_name,
        range_start,
        range_end,
        window_minutes,
        overlap_minutes,
    )

    query = _range_episode_query(user_id, range_start, range_end)
    existing = await TimelineEpisode.find(
        {**query, "status": {"$ne": "superseded"}}
    ).to_list()
    pinned = await TimelineEpisode.find(
        {**query, "pinned": True, "status": {"$ne": "superseded"}}
    ).to_list()

    policy_days = await activity_policy.activity_policy_days(
        user_id, range_start, range_end, timezone_name
    )

    return EvidenceBundle(
        activity_rejections=[
            decision.after["rejected_activity"]
            for day in policy_days
            for decision in day.review_decisions
            if decision.action == "episode_not_activity"
        ],
        manifest=manifest,
        existing_episodes=_existing_episode_payload(
            sorted(existing, key=lambda row: (row.started_at, row.episode_id))
        ),
        pinned_episodes=_existing_episode_payload(
            sorted(pinned, key=lambda row: (row.started_at, row.episode_key))
        ),
        evidence_revision=evidence_revision,
    )
