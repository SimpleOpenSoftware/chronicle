"""Validation and executor selection for semantic timeline analysis."""

import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from backend.config_loader import load_config
from backend.services.job_progress import report_job_progress

from .codex_executor import CodexTimelineExecutor
from .contracts import (
    AgentEpisode,
    EvidenceBundle,
    InterpretationResult,
    InterpretedEpisode,
    Publish,
    ReconcileAction,
    RequestMoreContext,
    SeparatedEpisode,
    SeparationResult,
    TimelineEvidenceManifest,
    UnassignedInterval,
    ValidatedTimelineProjection,
)
from .pi_executor import PiTimelineExecutor
from .workspace import write_workspace

logger = logging.getLogger(__name__)

BOUNDARY_SUPPORT_TOLERANCE = timedelta(minutes=2)
ACCOUNTING_GAP_TOLERANCE = timedelta(minutes=1)
BSON_DATETIME_PRECISION = timedelta(milliseconds=1)


def _evidence_end(item) -> datetime:
    return item.ended_at or item.started_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bson_datetime(value: datetime) -> datetime:
    """Normalize a timestamp to MongoDB BSON's millisecond precision."""

    value = _utc(value)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _supports_boundary(item, boundary: datetime) -> bool:
    return (
        item.started_at - BOUNDARY_SUPPORT_TOLERANCE
        <= boundary
        <= _evidence_end(item) + BOUNDARY_SUPPORT_TOLERANCE
    )


def _unique_evidence_suffixes(evidence_ids) -> dict[str, str]:
    """Map a model-shortened ID back only when the suffix is unambiguous."""

    candidates: dict[str, str] = {}
    ambiguous: set[str] = set()
    for evidence_id in evidence_ids:
        suffix = evidence_id.rsplit(":", 1)[-1]
        previous = candidates.get(suffix)
        if previous is not None and previous != evidence_id:
            ambiguous.add(suffix)
        else:
            candidates[suffix] = evidence_id
    return {
        suffix: evidence_id
        for suffix, evidence_id in candidates.items()
        if suffix not in ambiguous
    }


def _canonicalize_evidence_ids(values, evidence, suffixes) -> tuple[list[str], int]:
    repaired = [
        value if value in evidence else suffixes.get(value, value) for value in values
    ]
    unique = list(dict.fromkeys(repaired))
    return unique, sum(
        original != fixed for original, fixed in zip(values, repaired, strict=True)
    )


def _accounted_intervals(
    result: ValidatedTimelineProjection,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Intervals the day is already explained by.

    Confirmed episodes carried over from an earlier generation are pinned: the agent is
    told not to re-segment them, so their evidence must not read as unexplained.
    """

    return sorted(
        [
            (interval.started_at, interval.ended_at)
            for interval in result.unassigned_intervals
        ]
        + [(episode.started_at, episode.ended_at) for episode in result.episodes]
        + list(pinned_intervals or []),
        key=lambda interval: interval[0],
    )


def _validate_evidence_accounting(
    result: ValidatedTimelineProjection,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> None:
    accounted = _accounted_intervals(result, pinned_intervals)
    for index, item in enumerate(manifest.evidence):
        start = max(item.started_at, manifest.started_at)
        end = min(_evidence_end(item), manifest.ended_at)
        if end <= start:
            if not any(
                low - ACCOUNTING_GAP_TOLERANCE
                <= start
                <= high + ACCOUNTING_GAP_TOLERANCE
                for low, high in accounted
            ):
                raise ValueError(f"unaccounted evidence interval at item {index}")
            continue
        cursor = start
        for low, high in accounted:
            if high <= cursor:
                continue
            if low > cursor:
                if low - cursor > ACCOUNTING_GAP_TOLERANCE:
                    break
                cursor = low
            cursor = max(cursor, min(high, end))
            if cursor >= end:
                break
        if end - cursor > ACCOUNTING_GAP_TOLERANCE:
            raise ValueError(f"unaccounted evidence interval at item {index}")


def _fill_unassigned_evidence(
    result: ValidatedTimelineProjection,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
) -> None:
    """Materialize evidence the semantic draft did not explain as explicit unknowns."""

    accounted = _accounted_intervals(result, pinned_intervals)
    missing: list[tuple[datetime, datetime]] = []
    point_width = timedelta(seconds=1)
    for item in manifest.evidence:
        start = max(item.started_at, manifest.started_at)
        end = min(_evidence_end(item), manifest.ended_at)
        if end <= start:
            if any(
                low - ACCOUNTING_GAP_TOLERANCE
                <= start
                <= high + ACCOUNTING_GAP_TOLERANCE
                for low, high in accounted
            ):
                continue
            low = max(manifest.started_at, start - point_width)
            high = min(manifest.ended_at, start + point_width)
            if high > low:
                missing.append((low, high))
            continue

        cursor = start
        for low, high in accounted:
            if high <= cursor:
                continue
            if low > cursor:
                gap_end = min(low, end)
                if gap_end - cursor > ACCOUNTING_GAP_TOLERANCE:
                    missing.append((cursor, gap_end))
            cursor = max(cursor, min(high, end))
            if cursor >= end:
                break
        if end - cursor > ACCOUNTING_GAP_TOLERANCE:
            missing.append((cursor, end))

    merged: list[tuple[datetime, datetime]] = []
    for low, high in sorted(missing):
        if merged and low <= merged[-1][1] + ACCOUNTING_GAP_TOLERANCE:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    result.unassigned_intervals.extend(
        UnassignedInterval(
            started_at=low,
            ended_at=high,
            reason="Evidence was not assigned by semantic analysis",
        )
        for low, high in merged
    )


def _classify_unassigned_intervals(
    result: ValidatedTimelineProjection, manifest: TimelineEvidenceManifest
) -> None:
    """Decide *why* each unassigned interval is unassigned, from the manifest.

    `capture_gap` items record absent capture, so they do not count as evidence that
    something happened. An interval overlapped by any other evidence kind was captured
    and simply not explained; an interval overlapped by nothing else had nothing to
    explain.
    """

    captured = [
        (
            max(item.started_at, manifest.started_at),
            min(_evidence_end(item), manifest.ended_at),
        )
        for item in manifest.evidence
        if item.kind != "capture_gap"
    ]
    for interval in result.unassigned_intervals:
        start, end = _utc(interval.started_at), _utc(interval.ended_at)
        interval.cause = (
            "unexplained"
            if any(_utc(low) < end and start < _utc(high) for low, high in captured)
            else "no_capture"
        )


class TimelineIncompleteSegmentation(RuntimeError):
    """The agent returned no account of a day that has evidence.

    Restores the check removed in "fix(timeline): bound incomplete Luna output", which
    made an empty result *acceptable* and silently materialized it as unassigned time.
    That turned a model failure into a successful-looking run that blanked the day. An
    empty result is a retryable failure, not an answer: even a wholly idle day should
    come back as an idle episode or an explicit unassigned interval.
    """

    def __init__(
        self,
        message: str,
        *,
        episode_index: int | None = None,
        episode_indices: list[int] | tuple[int, ...] | None = None,
    ):
        super().__init__(message)
        indices = tuple(episode_indices or ())
        if episode_index is not None and episode_index not in indices:
            indices = (*indices, episode_index)
        self.episode_indices = tuple(sorted(indices))
        self.episode_index = (
            self.episode_indices[0] if len(self.episode_indices) == 1 else None
        )


def validate_agent_result(
    result: ValidatedTimelineProjection,
    manifest: TimelineEvidenceManifest,
    pinned_intervals: list[tuple[datetime, datetime]] | None = None,
    *,
    salvage_gap_bridging_episodes: bool = False,
) -> None:
    # Checked before _fill_unassigned_evidence, which would otherwise manufacture the
    # very intervals whose absence proves the agent said nothing.
    #
    # Skipped when episodes are pinned: a day already accounted for by confirmed
    # episodes legitimately leaves the agent nothing to add. Partial coverage is still
    # caught — _validate_evidence_accounting rejects evidence no interval explains.
    if (
        manifest.evidence
        and not result.episodes
        and not result.unassigned_intervals
        and not pinned_intervals
    ):
        raise TimelineIncompleteSegmentation(
            f"agent produced no episodes and no unassigned intervals for "
            f"{len(manifest.evidence)} evidence items across {len(manifest.windows)} windows"
        )
    bounded_unassigned: list[UnassignedInterval] = []
    for interval in result.unassigned_intervals:
        interval.started_at = max(_utc(interval.started_at), manifest.started_at)
        interval.ended_at = min(_utc(interval.ended_at), manifest.ended_at)
        if interval.ended_at > interval.started_at:
            bounded_unassigned.append(interval)
    result.unassigned_intervals = bounded_unassigned
    for episode in result.episodes:
        episode.started_at = _utc(episode.started_at)
        episode.ended_at = _utc(episode.ended_at)

    for index, interval in enumerate(result.unassigned_intervals):
        if interval.ended_at <= interval.started_at:
            raise ValueError(f"unassigned interval {index} must have positive duration")

    evidence = {item.evidence_id: item for item in manifest.evidence}
    suffixes = _unique_evidence_suffixes(evidence)
    citation_repairs = 0
    for episode in result.episodes:
        episode.evidence_ids, repaired = _canonicalize_evidence_ids(
            episode.evidence_ids, evidence, suffixes
        )
        citation_repairs += repaired
        for assertion in episode.assertions:
            assertion.evidence_ids, repaired = _canonicalize_evidence_ids(
                assertion.evidence_ids, evidence, suffixes
            )
            citation_repairs += repaired
        if episode.representative_evidence_id not in (None, ""):
            canonical, repaired = _canonicalize_evidence_ids(
                [episode.representative_evidence_id], evidence, suffixes
            )
            episode.representative_evidence_id = canonical[0]
            citation_repairs += repaired
    if citation_repairs:
        logger.warning(
            "Restored evidence-kind prefixes on %d uniquely matched citation(s)",
            citation_repairs,
        )
    # One malformed episode must not discard a whole day of good ones. Under
    # --output-schema the agent's own planning narration is schema-valid, so a stray
    # `{"kind": "task"}` entry can appear beside nine real episodes; rejecting the run
    # threw all of them away. Bad episodes are dropped individually, and the day still
    # fails loudly if nothing survives.
    kept: list[int] = []
    dropped: list[str] = []
    removed_unknown_citations: list[str] = []
    structural_errors: list[str] = []
    structural_error_indices: set[int] = set()
    for index, episode in enumerate(result.episodes):
        if (
            episode.started_at < manifest.started_at
            or episode.ended_at > manifest.ended_at
        ):
            dropped.append(f"episode {index} lies outside the analyzed day range")
            continue
        unknown = set(episode.evidence_ids) - evidence.keys()
        if unknown:
            known_evidence_ids = [
                evidence_id
                for evidence_id in episode.evidence_ids
                if evidence_id in evidence
            ]
            if not known_evidence_ids:
                dropped.append(
                    f"episode {index} references unknown evidence: {sorted(unknown)}"
                )
                continue
            episode.evidence_ids = known_evidence_ids
            removed_unknown_citations.append(f"episode {index}: {sorted(unknown)}")
        overlapping_evidence_ids: list[str] = []
        for evidence_id in episode.evidence_ids:
            item = evidence[evidence_id]
            item_end = item.ended_at or item.started_at
            # Evidence at an exact episode boundary is still grounding. Screen
            # observations commonly have zero duration, and condensed output uses
            # one observation as the start marker and the next as the end marker.
            # A strict half-open comparison discarded those otherwise exact spans.
            if (
                item.started_at <= episode.ended_at + BSON_DATETIME_PRECISION
                and item_end >= episode.started_at - BSON_DATETIME_PRECISION
            ):
                overlapping_evidence_ids.append(evidence_id)
        if not overlapping_evidence_ids:
            dropped.append(f"episode {index} has no temporally overlapping evidence")
            continue
        episode.evidence_ids = overlapping_evidence_ids
        cited_evidence = [evidence[evidence_id] for evidence_id in episode.evidence_ids]
        boundary_errors: list[str] = []
        if not any(
            _supports_boundary(item, episode.started_at) for item in cited_evidence
        ):
            earliest = min(item.started_at for item in cited_evidence)
            boundary_errors.append(
                f"episode {index} start {episode.started_at.isoformat()} is unsupported; "
                f"earliest cited boundary is {earliest.isoformat()}. Cite evidence "
                "supporting the proposed start or move the start deliberately"
            )
        if not any(
            _supports_boundary(item, episode.ended_at) for item in cited_evidence
        ):
            latest = max(_evidence_end(item) for item in cited_evidence)
            boundary_errors.append(
                f"episode {index} end {episode.ended_at.isoformat()} is unsupported; "
                f"latest cited boundary is {latest.isoformat()}. Cite evidence "
                "supporting the proposed end or move the end deliberately"
            )
        if boundary_errors:
            structural_errors.extend(boundary_errors)
            structural_error_indices.add(index)
            continue
        if episode.ended_at <= episode.started_at:
            dropped.append(
                f"episode {index} cannot be bounded to a positive cited interval"
            )
            continue
        # BSON truncates datetimes to milliseconds. A genuinely positive point-event
        # draft such as .792974 -> .792975 passed Pydantic here but round-tripped from
        # Mongo as .792 -> .792, making the published TimelineEpisode unreadable.
        # Validate the persisted representation, extending only intervals that were
        # positive before truncation. One millisecond is the smallest truthful stored
        # span and remains inside the analyzed day.
        episode.started_at = _bson_datetime(episode.started_at)
        episode.ended_at = _bson_datetime(episode.ended_at)
        if episode.ended_at <= episode.started_at:
            extended_end = episode.started_at + BSON_DATETIME_PRECISION
            if extended_end > manifest.ended_at:
                dropped.append(f"episode {index} collapses at BSON datetime precision")
                continue
            episode.ended_at = extended_end
        bound_evidence = set(overlapping_evidence_ids)
        for assertion in episode.assertions:
            assertion.evidence_ids = [
                evidence_id
                for evidence_id in assertion.evidence_ids
                if evidence_id in bound_evidence
            ]
        episode.assertions = [
            assertion for assertion in episode.assertions if assertion.evidence_ids
        ]
        if episode.representative_evidence_id:
            representative = evidence.get(episode.representative_evidence_id)
            if (
                episode.representative_evidence_id not in episode.evidence_ids
                or representative is None
                or not representative.image_filename
            ):
                # A thumbnail is optional decoration. Drop a bad selection without
                # discarding otherwise valid semantic boundaries and citations.
                episode.representative_evidence_id = None
        if episode.parent_episode_index is not None and (
            episode.parent_episode_index >= len(result.episodes)
            or episode.parent_episode_index == index
        ):
            # Parenting is structure, not substance — unlink rather than lose the episode.
            episode.parent_episode_index = None
        kept.append(index)

    if structural_errors:
        raise TimelineIncompleteSegmentation(
            "\n".join(structural_errors),
            episode_indices=sorted(structural_error_indices),
        )

    if dropped:
        logger.warning(
            "🧹 Dropped %d malformed episode(s) of %d, keeping %d: %s",
            len(dropped),
            len(result.episodes),
            len(kept),
            "; ".join(dropped[:5]),
        )
    if removed_unknown_citations:
        logger.warning(
            "🧹 Removed unknown citations from %d otherwise valid episode(s): %s",
            len(removed_unknown_citations),
            "; ".join(removed_unknown_citations[:5]),
        )
    if result.episodes and not kept:
        raise TimelineIncompleteSegmentation(
            f"every one of {len(result.episodes)} drafted episodes was malformed: "
            + "; ".join(dropped[:3])
        )
    if dropped:
        remap = {old: new for new, old in enumerate(kept)}
        result.episodes = [result.episodes[index] for index in kept]
        for episode in result.episodes:
            if episode.parent_episode_index is not None:
                episode.parent_episode_index = remap.get(episode.parent_episode_index)

    _fill_unassigned_evidence(result, manifest, pinned_intervals)
    _classify_unassigned_intervals(result, manifest)
    _validate_evidence_accounting(result, manifest, pinned_intervals)


def settings_dict() -> dict[str, Any]:
    value = load_config().get("timeline", {})
    converted = (
        OmegaConf.to_container(value, resolve=True)
        if OmegaConf.is_config(value)
        else value
    )
    return converted if isinstance(converted, dict) else {}


def build_executor():
    settings = settings_dict()
    executor = str(settings.get("executor") or "pi")
    if executor == "codex":
        return CodexTimelineExecutor(settings.get("codex") or {})
    if executor == "pi":
        return PiTimelineExecutor(settings.get("pi") or {})
    raise ValueError(f"unsupported timeline executor: {executor}")


# ── Range mode: staged separation then interpretation ────────────────────────


def _revision_key(value: Any) -> tuple[str, int]:
    if hasattr(value, "episode_key"):
        return str(value.episode_key), int(value.revision)
    return str(value.get("episode_key") or ""), int(value.get("revision") or 0)


def _prior_revision_map(
    bundle: EvidenceBundle,
) -> dict[tuple[str, int], dict[str, Any]]:
    priors: dict[tuple[str, int], dict[str, Any]] = {}
    for value in [*bundle.existing_episodes, *bundle.pinned_episodes]:
        key = _revision_key(value)
        if key[0] and key[1] > 0:
            priors[key] = value
    return priors


def _validate_confirmed_structure(
    hypothesis: SeparatedEpisode,
    predecessors: list[tuple[str, int]],
    priors: dict[tuple[str, int], dict[str, Any]],
) -> None:
    for predecessor in predecessors:
        prior = priors[predecessor]
        confirmed = set(prior.get("confirmed_fields") or [])
        checks = {
            "started_at": _utc(hypothesis.started_at),
            "ended_at": _utc(hypothesis.ended_at),
            "evidence_ids": set(hypothesis.evidence_ids),
        }
        for field, actual in checks.items():
            if field not in confirmed:
                continue
            expected = prior.get(field)
            if field in {"started_at", "ended_at"}:
                if isinstance(expected, str):
                    expected = datetime.fromisoformat(expected)
                if isinstance(expected, datetime):
                    expected = _utc(expected)
            if field == "evidence_ids":
                expected = set(expected or [])
            if actual != expected:
                raise ValueError(
                    f"hypothesis {hypothesis.hypothesis_id!r} changes confirmed "
                    f"field {field!r} on predecessor {predecessor}"
                )


def _normalize_separation_result(
    result: SeparationResult,
    bundle: EvidenceBundle,
) -> None:
    """Repair lossless structural redundancies before enforcing stage barriers.

    Boundary timestamps and boundary-anchor IDs describe the same fact. Models can
    return several nearby candidate anchors or round a timestamp even when one of the
    selected anchors is exact. Keep the selected anchors that support the declared
    boundary; when none do, snap to the nearest selected anchor only within the same
    conservative tolerance used by day-mode validation.

    Evidence outside a hypothesis envelope cannot support that hypothesis. Preserve it
    as unassigned instead of discarding it, and derive any omissions the same way. This
    keeps the strict validator authoritative while avoiding a fragile requirement for
    the model to enumerate every condensed evidence ID in two complementary lists.
    """

    manifest = bundle.manifest
    evidence = {item.evidence_id: item for item in manifest.evidence}
    anchors = {anchor.anchor_id: anchor for anchor in manifest.anchors}
    priors = _prior_revision_map(bundle)
    removed_evidence_ids: list[str] = []
    repaired_boundaries = 0
    repaired_anchor_sets = 0
    added_anchor_evidence = 0

    for hypothesis in result.hypotheses:
        predecessors = [
            _revision_key(value) for value in hypothesis.lineage.predecessor_revisions
        ]
        confirmed_fields = {
            field
            for predecessor in predecessors
            for field in (priors.get(predecessor) or {}).get("confirmed_fields", [])
        }

        for anchor_field, boundary_field in (
            ("start_anchor_ids", "started_at"),
            ("end_anchor_ids", "ended_at"),
        ):
            anchor_ids = getattr(hypothesis, anchor_field)
            # Unknown anchors remain untouched so the validator reports the bad ID.
            if not anchor_ids or any(value not in anchors for value in anchor_ids):
                continue
            selected = [anchors[value] for value in anchor_ids]
            boundary = _utc(getattr(hypothesis, boundary_field))
            supporting = [
                anchor
                for anchor in selected
                if _utc(anchor.earliest_at) <= boundary <= _utc(anchor.latest_at)
            ]
            if not supporting and all(
                anchor.evidence_id in hypothesis.evidence_ids for anchor in selected
            ):
                # Time and cited evidence already specify this boundary. A known
                # but mismatched anchor ID is redundant; recover exact support
                # only inside that explicit evidence claim, never nearby sources.
                supporting = [
                    anchor
                    for anchor in anchors.values()
                    if anchor.evidence_id in hypothesis.evidence_ids
                    and _utc(anchor.earliest_at) <= boundary <= _utc(anchor.latest_at)
                ]
            if supporting:
                retained = supporting
            elif boundary_field not in confirmed_fields:
                candidates: list[tuple[timedelta, datetime, Any]] = []
                for anchor in selected:
                    low = _utc(anchor.earliest_at)
                    high = _utc(anchor.latest_at)
                    snapped = low if boundary < low else high
                    candidates.append((abs(snapped - boundary), snapped, anchor))
                distance, snapped, _ = min(candidates, key=lambda value: value[0])
                if distance > BOUNDARY_SUPPORT_TOLERANCE:
                    continue
                retained = [
                    anchor for _, value, anchor in candidates if value == snapped
                ]
                setattr(hypothesis, boundary_field, snapped)
                repaired_boundaries += 1
            else:
                continue

            retained_ids = [anchor.anchor_id for anchor in retained]
            if retained_ids != anchor_ids:
                setattr(hypothesis, anchor_field, retained_ids)
                repaired_anchor_sets += 1
            if "evidence_ids" not in confirmed_fields:
                for anchor in retained:
                    if anchor.evidence_id not in hypothesis.evidence_ids:
                        hypothesis.evidence_ids.append(anchor.evidence_id)
                        added_anchor_evidence += 1

        if "evidence_ids" in confirmed_fields:
            continue
        start = _utc(hypothesis.started_at)
        end = _utc(hypothesis.ended_at)
        overlapping_evidence_ids: list[str] = []
        rejected_evidence_ids: list[str] = []
        for evidence_id in hypothesis.evidence_ids:
            item = evidence.get(evidence_id)
            # Preserve unknown IDs so deterministic validation reports them.
            if item is None or (
                _utc(item.started_at) <= end and _utc(_evidence_end(item)) >= start
            ):
                overlapping_evidence_ids.append(evidence_id)
            else:
                rejected_evidence_ids.append(evidence_id)
        # Keep a wholly malformed hypothesis intact so it still fails loudly.
        if overlapping_evidence_ids:
            hypothesis.evidence_ids = overlapping_evidence_ids
            removed_evidence_ids.extend(rejected_evidence_ids)

    point_only_ids = set()
    for hypothesis in result.hypotheses:
        if (
            hypothesis.lineage.action == "new"
            and not hypothesis.lineage.predecessor_revisions
            and hypothesis.started_at == hypothesis.ended_at
            and hypothesis.evidence_ids
            and all(
                evidence_id in evidence
                and evidence[evidence_id].started_at == hypothesis.started_at
                and evidence[evidence_id].ended_at in (None, hypothesis.started_at)
                for evidence_id in hypothesis.evidence_ids
            )
        ):
            point_only_ids.add(id(hypothesis))
            removed_evidence_ids.extend(hypothesis.evidence_ids)
    if point_only_ids:
        result.hypotheses = [
            hypothesis
            for hypothesis in result.hypotheses
            if id(hypothesis) not in point_only_ids
        ]
        logger.warning(
            "Retained %d point-only hypotheses as unassigned evidence",
            len(point_only_ids),
        )

    assigned = {
        evidence_id
        for hypothesis in result.hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    result.unassigned_evidence_ids = [
        evidence_id
        for evidence_id in result.unassigned_evidence_ids
        if evidence_id not in assigned
    ]
    unassigned = set(result.unassigned_evidence_ids)
    for evidence_id in [*removed_evidence_ids, *evidence]:
        if evidence_id not in assigned and evidence_id not in unassigned:
            result.unassigned_evidence_ids.append(evidence_id)
            unassigned.add(evidence_id)

    if (
        repaired_boundaries
        or repaired_anchor_sets
        or added_anchor_evidence
        or removed_evidence_ids
    ):
        logger.warning(
            "Normalized Timeline separation structure: %d boundary timestamp(s), "
            "%d anchor set(s), %d implicit anchor citation(s), and %d "
            "out-of-envelope citation(s)",
            repaired_boundaries,
            repaired_anchor_sets,
            added_anchor_evidence,
            len(removed_evidence_ids),
        )


def validate_separation_result(
    result: SeparationResult,
    bundle: EvidenceBundle,
) -> None:
    """Validate structural claims before any semantic interpretation can run."""

    manifest = bundle.manifest
    evidence = {item.evidence_id: item for item in manifest.evidence}
    anchors = {anchor.anchor_id: anchor for anchor in manifest.anchors}
    priors = _prior_revision_map(bundle)
    hypothesis_ids: set[str] = set()
    predecessor_uses: dict[tuple[str, int], list[str]] = {}
    split_counts: dict[tuple[str, int], int] = {}

    def capture_only(item):
        return item.kind == "capture_gap" or (
            item.kind == "audio_span"
            and not item.excerpt
            and not any(item.metadata.get("acoustic_active_fraction") or [])
        )

    def validate_predecessor_scope(predecessor):
        prior = priors[predecessor]
        for field in ("started_at", "ended_at"):
            value = prior.get(field)
            if value is None:
                continue
            stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not manifest.started_at <= _utc(stamp) <= manifest.ended_at:
                raise ValueError(
                    f"predecessor {predecessor} extends outside the manifest; "
                    "leave it unchanged by omitting it from lineage and retirements, "
                    "or request bounded context before changing it. Do not truncate "
                    "its outside-range activity."
                )

    if (
        manifest.evidence
        and not result.hypotheses
        and not result.unassigned_evidence_ids
        and not result.unresolved_intervals
    ):
        raise TimelineIncompleteSegmentation(
            "separation produced no hypotheses, unassigned evidence, or unresolved intervals"
        )

    _normalize_separation_result(result, bundle)

    for hypothesis in result.hypotheses:
        if hypothesis.hypothesis_id in hypothesis_ids:
            raise ValueError(f"duplicate hypothesis_id: {hypothesis.hypothesis_id!r}")
        hypothesis_ids.add(hypothesis.hypothesis_id)
        start, end = _utc(hypothesis.started_at), _utc(hypothesis.ended_at)
        if end <= start:
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} must have positive duration: "
                "ended_at must be later than started_at. Attach point observations "
                "to a supported coherent interval or leave them unassigned."
            )
        if not (manifest.started_at <= start < end <= manifest.ended_at):
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} lies outside the manifest"
            )
        if len(hypothesis.evidence_ids) != len(set(hypothesis.evidence_ids)):
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} repeats evidence IDs"
            )
        unknown_evidence = set(hypothesis.evidence_ids) - evidence.keys()
        if unknown_evidence:
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} cites unknown evidence: "
                f"{sorted(unknown_evidence)}"
            )
        if all(
            capture_only(item)
            for item in (evidence[value] for value in hypothesis.evidence_ids)
        ):
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} has capture coverage alone, "
                "not positive activity evidence. Leave these evidence IDs unassigned. "
                "A transcribed status or conversation ID is a pointer, not transcript "
                "content; cite the actual transcript evidence for a speech activity. "
                "Retire editable unsupported predecessor revisions explicitly instead "
                "of carrying or relabeling them as idle. Preserve confirmed fields "
                "and activities with positive evidence on other devices."
            )
        if not all(
            _utc(item.started_at) <= end and _utc(_evidence_end(item)) >= start
            for item in (evidence[value] for value in hypothesis.evidence_ids)
        ):
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} cites evidence outside its envelope"
            )

        for field, boundary in (
            ("start_anchor_ids", start),
            ("end_anchor_ids", end),
        ):
            anchor_ids = getattr(hypothesis, field)
            if len(anchor_ids) != len(set(anchor_ids)):
                raise ValueError(
                    f"hypothesis {hypothesis.hypothesis_id!r} repeats {field}"
                )
            for anchor_id in anchor_ids:
                anchor = anchors.get(anchor_id)
                if anchor is None:
                    raise ValueError(
                        f"hypothesis {hypothesis.hypothesis_id!r} cites unknown anchor "
                        f"{anchor_id!r}"
                    )
                if anchor.evidence_id not in hypothesis.evidence_ids:
                    raise ValueError(
                        f"anchor {anchor_id!r} is not assigned to hypothesis "
                        f"{hypothesis.hypothesis_id!r}"
                    )
                if not _utc(anchor.earliest_at) <= boundary <= _utc(anchor.latest_at):
                    raise ValueError(
                        f"anchor {anchor_id!r} does not support the {field[:-4]} "
                        f"of hypothesis {hypothesis.hypothesis_id!r}"
                    )

        action = hypothesis.lineage.action
        predecessors = [
            _revision_key(value) for value in hypothesis.lineage.predecessor_revisions
        ]
        required = {
            "new": len(predecessors) == 0,
            "carry": len(predecessors) == 1,
            "split": len(predecessors) == 1,
            "merge": len(predecessors) >= 2,
        }[action]
        if not required:
            raise ValueError(
                f"invalid {action} predecessor cardinality for hypothesis "
                f"{hypothesis.hypothesis_id!r}"
            )
        if len(predecessors) != len(set(predecessors)):
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} repeats a predecessor"
            )
        unknown_predecessors = set(predecessors) - priors.keys()
        if unknown_predecessors:
            raise ValueError(
                f"hypothesis {hypothesis.hypothesis_id!r} cites unknown predecessor "
                f"revisions: {sorted(unknown_predecessors)}"
            )
        for predecessor in predecessors:
            validate_predecessor_scope(predecessor)
            predecessor_uses.setdefault(predecessor, []).append(action)
            if action == "split":
                split_counts[predecessor] = split_counts.get(predecessor, 0) + 1
        _validate_confirmed_structure(hypothesis, predecessors, priors)

    for predecessor, actions in predecessor_uses.items():
        families = set(actions)
        if len(families) > 1:
            raise ValueError(
                f"predecessor {predecessor} participates in incompatible lineage actions"
            )
        if actions[0] != "split" and len(actions) > 1:
            raise ValueError(f"predecessor {predecessor} is consumed more than once")
        if actions[0] == "split" and split_counts[predecessor] < 2:
            raise ValueError(
                f"split predecessor {predecessor} must produce at least two hypotheses"
            )

    retired: set[tuple[str, int]] = set()
    for retirement in result.retirements:
        predecessor = _revision_key(retirement.predecessor_revision)
        if predecessor not in priors:
            raise ValueError(f"retirement cites unknown predecessor {predecessor}")
        validate_predecessor_scope(predecessor)
        if predecessor in predecessor_uses:
            raise ValueError(
                f"predecessor {predecessor} cannot be both consumed and retired"
            )
        if predecessor in retired:
            raise ValueError(f"duplicate retirement for predecessor {predecessor}")
        retired.add(predecessor)

    for predecessor, prior in priors.items():
        keys = prior.get("evidence_ids") or []
        if (
            prior.get("kind") == "idle"
            and not prior.get("confirmed_fields")
            and keys
            and all(
                key in evidence
                and evidence[key].kind in {"audio_span", "capture_gap"}
                and not evidence[key].excerpt
                for key in keys
            )
            and predecessor not in retired
        ):
            # Only require retirement when the entire prior is editable here.
            bounds = [prior.get(field) for field in ("started_at", "ended_at")]
            stamps = [
                datetime.fromisoformat(v) if isinstance(v, str) else v for v in bounds
            ]
            if all(
                v is not None and manifest.started_at <= _utc(v) <= manifest.ended_at
                for v in stamps
            ):
                raise ValueError(
                    f"idle predecessor {predecessor} has capture coverage alone. "
                    "Audio energy or speech detection cannot establish device idle; "
                    "It must appear ONLY in retirements; omission preserves the false "
                    "idle episode. Do not carry, merge, or split it into a replacement."
                )

    if len(result.unassigned_evidence_ids) != len(set(result.unassigned_evidence_ids)):
        raise ValueError("separation repeats an unassigned evidence ID")
    unknown_unassigned = set(result.unassigned_evidence_ids) - evidence.keys()
    if unknown_unassigned:
        raise ValueError(
            f"unassigned list cites unknown evidence: {sorted(unknown_unassigned)}"
        )
    assigned = {
        evidence_id
        for hypothesis in result.hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    missing_evidence = evidence.keys() - assigned - set(result.unassigned_evidence_ids)
    if missing_evidence:
        raise ValueError(
            f"separation omits evidence from assignment: {sorted(missing_evidence)}"
        )
    for index, interval in enumerate(result.unresolved_intervals):
        start, end = _utc(interval.started_at), _utc(interval.ended_at)
        if not (manifest.started_at <= start < end <= manifest.ended_at):
            raise ValueError(f"unresolved interval {index} lies outside the manifest")


def _validate_confirmed_semantics(
    interpreted: InterpretedEpisode,
    hypothesis: SeparatedEpisode,
    bundle: EvidenceBundle,
) -> None:
    priors = _prior_revision_map(bundle)
    for value in hypothesis.lineage.predecessor_revisions:
        predecessor = _revision_key(value)
        prior = priors[predecessor]
        for field in set(prior.get("confirmed_fields") or []):
            if field in {"started_at", "ended_at", "evidence_ids"}:
                continue
            if not hasattr(interpreted, field):
                continue
            actual = getattr(interpreted, field)
            expected = prior.get(field)
            if field == "attributes" and isinstance(expected, dict):
                actual = {item.key: item.value for item in actual}
            if actual != expected:
                raise ValueError(
                    f"interpretation {interpreted.hypothesis_id!r} changes confirmed "
                    f"field {field!r} on predecessor {predecessor}"
                )


def validate_interpretation_result(
    result: InterpretationResult,
    separation: SeparationResult,
    bundle: EvidenceBundle,
) -> None:
    """Require one local outcome for every accepted structural hypothesis."""

    hypotheses = {item.hypothesis_id: item for item in separation.hypotheses}
    accepted: set[str] = set()
    rejected: set[str] = set()
    for item in result.accepted:
        if item.hypothesis_id not in hypotheses:
            raise ValueError(
                f"interpretation accepts unknown hypothesis {item.hypothesis_id!r}"
            )
        if item.hypothesis_id in accepted:
            raise ValueError(
                f"duplicate accepted interpretation {item.hypothesis_id!r}"
            )
        accepted.add(item.hypothesis_id)
        assigned = set(hypotheses[item.hypothesis_id].evidence_ids)
        for assertion in item.assertions:
            if not set(assertion.evidence_ids) <= assigned:
                raise ValueError(
                    f"interpretation {item.hypothesis_id!r} cites evidence outside "
                    "its structural hypothesis"
                )
        if item.representative_evidence_id not in (None, "") and (
            item.representative_evidence_id not in assigned
        ):
            raise ValueError(
                f"interpretation {item.hypothesis_id!r} chooses representative "
                "evidence outside its structural hypothesis"
            )
        _validate_confirmed_semantics(item, hypotheses[item.hypothesis_id], bundle)

    for item in result.rejected:
        if item.hypothesis_id not in hypotheses:
            raise ValueError(
                f"interpretation rejects unknown hypothesis {item.hypothesis_id!r}"
            )
        if item.hypothesis_id in rejected:
            raise ValueError(f"duplicate rejection {item.hypothesis_id!r}")
        rejected.add(item.hypothesis_id)
        assigned = set(hypotheses[item.hypothesis_id].evidence_ids)
        if not set(item.implicated_evidence_ids) <= assigned:
            raise ValueError(
                f"rejection {item.hypothesis_id!r} implicates evidence outside its "
                "structural hypothesis"
            )
        if item.reason_code == "redundant_activity":
            duplicate = hypotheses[item.hypothesis_id]
            covered = any(
                assigned <= set(hypotheses[key].evidence_ids)
                and _utc(hypotheses[key].started_at) <= _utc(duplicate.started_at)
                and _utc(duplicate.ended_at) <= _utc(hypotheses[key].ended_at)
                for key in accepted
                if key != item.hypothesis_id
            )
            if duplicate.lineage.action != "new" or not covered:
                raise ValueError(
                    "redundant_activity requires a new hypothesis fully covered "
                    "by one accepted hypothesis"
                )

    if accepted & rejected:
        raise ValueError(
            f"hypotheses cannot be both accepted and rejected: {sorted(accepted & rejected)}"
        )
    missing = hypotheses.keys() - accepted - rejected
    if missing:
        raise ValueError(f"interpretation omitted hypotheses: {sorted(missing)}")


def project_validated_stages(
    separation: SeparationResult,
    interpretation: InterpretationResult,
) -> ValidatedTimelineProjection:
    """Join semantic fields onto immutable structure after both barriers pass."""

    interpreted_by_id = {item.hypothesis_id: item for item in interpretation.accepted}
    episodes = []
    for hypothesis in separation.hypotheses:
        interpreted = interpreted_by_id.get(hypothesis.hypothesis_id)
        if interpreted is None:
            continue
        semantic = interpreted.model_dump(exclude={"hypothesis_id"})
        episodes.append(
            AgentEpisode(
                **semantic,
                started_at=hypothesis.started_at,
                ended_at=hypothesis.ended_at,
                evidence_ids=list(hypothesis.evidence_ids),
            )
        )
    return ValidatedTimelineProjection(
        episodes=episodes,
        unassigned_intervals=[
            value.model_copy(deep=True) for value in separation.unresolved_intervals
        ],
    )


class RangeReconcileExecutor:
    """Own the range's separation and interpretation validation barriers."""

    def __init__(self, executor: Any):
        self._executor = executor

    async def reconcile(
        self,
        bundle: EvidenceBundle,
        *,
        reasoning_effort: str | None = None,
        validation_feedback: str | None = None,
    ) -> ReconcileAction:
        await report_job_progress(
            "evidence",
            "Evidence prepared",
            completed=len(bundle.manifest.evidence),
            total=len(bundle.manifest.evidence),
            unit="items",
            state="completed",
        )

        def validate_context_fences(result):
            for request in result.context_requests:
                if request.base_manifest_hash != bundle.manifest.evidence_revision:
                    raise ValueError(
                        "context request must copy the supplied base_manifest_hash"
                    )
                if request.leased_evidence_revision != bundle.evidence_revision:
                    raise ValueError(
                        "context request must copy the supplied leased_evidence_revision"
                    )

        def validate_separation_for_cache(result: SeparationResult) -> None:
            validate_context_fences(result)
            if not result.context_requests:
                validate_separation_result(result, bundle)

        with tempfile.TemporaryDirectory(prefix="chronicle-reconcile-") as temp_dir:
            workspace = Path(temp_dir)
            write_workspace(workspace, bundle.manifest)
            (workspace / "anchors.json").write_text(
                json.dumps(
                    [
                        anchor.model_dump(mode="json")
                        for anchor in bundle.manifest.anchors
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            await report_job_progress("separation", "Preparing episode inference")
            separation = await self._executor.separate(
                workspace,
                bundle,
                reasoning_effort=reasoning_effort,
                validation_feedback=validation_feedback,
                validate_result=validate_separation_for_cache,
            )
            await report_job_progress(
                "separation",
                "Episode hypotheses formed",
                completed=len(separation.hypotheses),
                total=len(separation.hypotheses),
                unit="hypotheses",
                state="completed",
            )
            validate_context_fences(separation)
            if separation.context_requests:
                return RequestMoreContext(request=separation.context_requests[0])
            validate_separation_result(separation, bundle)

            def validate_interpretation_for_cache(
                result: InterpretationResult,
            ) -> None:
                validate_context_fences(result)
                if not result.context_requests:
                    validate_interpretation_result(result, separation, bundle)

            await report_job_progress(
                "interpretation", "Interpreting episode hypotheses"
            )
            interpretation = await self._executor.interpret(
                workspace,
                bundle,
                separation,
                reasoning_effort=reasoning_effort,
                validate_result=validate_interpretation_for_cache,
            )
            validate_context_fences(interpretation)
            if interpretation.context_requests:
                return RequestMoreContext(request=interpretation.context_requests[0])
            validate_interpretation_result(interpretation, separation, bundle)
        await report_job_progress(
            "interpretation",
            "Episode validation complete",
            completed=1,
            total=1,
            unit="pass",
            state="completed",
        )
        await report_job_progress("publication", "Publishing validated episodes")
        projection = project_validated_stages(separation, interpretation)
        if separation.inference_provenance is None:
            raise ValueError("separation stage is missing inference provenance")
        if interpretation.inference_provenance is None:
            raise ValueError("interpretation stage is missing inference provenance")
        return Publish(
            projection=projection,
            separation=separation,
            interpretation=interpretation,
            separation_inference=separation.inference_provenance,
            interpretation_inference=interpretation.inference_provenance,
        )


def build_range_executor() -> RangeReconcileExecutor:
    return RangeReconcileExecutor(build_executor())
