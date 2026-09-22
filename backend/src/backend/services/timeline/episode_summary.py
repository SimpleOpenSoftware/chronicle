"""Transcript projection for one reconciled semantic episode."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

from backend.models.timeline import (
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationJournal,
)
from backend.services.inference_artifacts import canonical_hash


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def bounded_episode_transcript(episode: TimelineEpisode) -> str:
    """Render immutable transcript evidence carried by this exact revision.

    Publication has already selected bounded transcript blocks and persisted their
    excerpts/content hashes on the episode. Reading mutable Conversation projections
    here would reopen that accepted evidence claim and create a cross-document race at
    summary materialization.
    """

    episode_start, episode_end = _utc(episode.started_at), _utc(episode.ended_at)
    sources: dict[str, tuple[datetime, list[str]]] = {}
    for ref in sorted(episode.evidence_refs, key=lambda item: _utc(item.started_at)):
        text = (ref.excerpt or "").strip()
        if ref.kind != "transcript" or not text:
            continue
        ref_start = _utc(ref.started_at)
        ref_end = _utc(ref.ended_at or ref.started_at)
        if ref_start == ref_end:
            overlaps = episode_start <= ref_start < episode_end
        else:
            overlaps = ref_start < episode_end and ref_end > episode_start
        if not overlaps:
            continue
        source = str(
            ref.metadata.get("conversation_id") or ref.source_item_id or ref.evidence_id
        )
        first, lines = sources.setdefault(source, (ref_start, []))
        attributed = json.dumps(
            {
                "evidence_id": ref.evidence_id,
                "content_hash": ref.content_hash,
                "locator": ref.locator.model_dump(mode="json"),
                "role": ref.role,
                "direction": ref.metadata.get("direction", "unknown"),
                "started_at": ref_start.isoformat(),
                "ended_at": ref_end.isoformat(),
                "text": text,
            },
            ensure_ascii=False,
        )
        sources[source] = (min(first, ref_start), [*lines, attributed])

    blocks = sorted(
        (
            started_at,
            f"SOURCE RECORDING {source}\n" + "\n".join(lines),
        )
        for source, (started_at, lines) in sources.items()
    )
    if not blocks:
        return ""
    return (
        "The blocks below are capture sources for ONE episode. They may overlap or "
        "record the same speech from input and output streams. Reconcile duplicates; "
        "do not describe repeated capture as repeated conversation. Summarize only "
        "content inside the selected episode bounds.\n\n"
        "Retain attribution: media, application output and assistant content are not "
        "the user's speech or life. Uncertain speech remains uncertain.\n\n"
        + "\n\n".join(block for _, block in blocks)
    )


def episode_summary_scope_hash(
    episode: TimelineEpisode,
    *,
    transcript: str | None = None,
) -> str:
    """Hash only inputs that can change one episode's bounded long account.

    The day snapshot is intentionally absent. A sibling episode or semantic-group edit
    must not invalidate a summary whose exact revision and bounded source projection did
    not change. Conversely, a bounds, evidence-claim, or transcript change necessarily
    changes this payload and prevents a stale worker result from landing.
    """

    rendered = (
        transcript if transcript is not None else bounded_episode_transcript(episode)
    )
    return canonical_hash(
        {
            "schema": "timeline-episode-summary-scope-v1",
            "episode_key": episode.episode_key,
            "revision": int(episode.revision),
            "started_at": _utc(episode.started_at).isoformat(),
            "ended_at": _utc(episode.ended_at).isoformat(),
            "evidence": sorted(
                (
                    ref.evidence_id,
                    _utc(ref.started_at).isoformat(),
                    _utc(ref.ended_at).isoformat() if ref.ended_at else None,
                    ref.content_hash,
                )
                for ref in episode.evidence_refs
            ),
            "sources": sorted(
                {
                    str(
                        ref.metadata.get("conversation_id")
                        or ref.source_item_id
                        or ref.evidence_id
                    )
                    for ref in episode.evidence_refs
                    if ref.kind == "transcript"
                }
            ),
            "bounded_transcript": rendered,
        }
    )


class EpisodeSummaryEligibility(NamedTuple):
    eligible: bool
    scope_hash: str | None
    reason: str


STRUCTURAL_CONFIRMATION_FIELDS = frozenset(
    {
        "started_at",
        "ended_at",
        "evidence_refs",
    }
)
SUMMARY_DAY_STATES = frozenset({"ready", "reviewed", "applied"})


def episode_structure_is_stable(episode: TimelineEpisode) -> bool:
    """Whether this exact revision may acquire a derived long account."""

    if episode.status == "settled":
        return True
    return STRUCTURAL_CONFIRMATION_FIELDS.issubset(set(episode.confirmed_fields))


def _episode_local_dates(episode: TimelineEpisode) -> list[date]:
    zone = ZoneInfo(episode.timezone)
    start = _utc(episode.started_at).astimezone(zone).date()
    # Episode intervals are half-open. Subtracting one microsecond prevents an episode
    # ending exactly at midnight from claiming the following local day.
    end = (_utc(episode.ended_at) - timedelta(microseconds=1)).astimezone(zone).date()
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


async def episode_revision_is_published(episode: TimelineEpisode) -> bool:
    """Require every current day projection to come from committed publication.

    Episode operations are applied before snapshots are installed and before their
    publication journal commits. Checking the episode collection alone would therefore
    expose a crash orphan to irreversible downstream work. The current snapshot on each
    affected local day and the committed journal that installed it must both contain the
    exact episode revision.
    """

    required_dates = _episode_local_dates(episode)
    days = await TimelineDay.find(
        TimelineDay.user_id == episode.user_id,
        {"local_date": {"$in": required_dates}},
        TimelineDay.timezone == episode.timezone,
    ).to_list()
    by_date = {day.local_date: day for day in days}
    exact_ref = (episode.episode_key, int(episode.revision))
    current_snapshot_ids: dict[date, str] = {}
    for local_date in required_dates:
        day = by_date.get(local_date)
        if (
            day is None
            or day.snapshot_state not in SUMMARY_DAY_STATES
            or day.pending_publication_id is not None
            or day.current_snapshot is None
            or day.current_snapshot_id is None
        ):
            return False
        members = {
            (item.episode_key, int(item.revision))
            for item in day.current_snapshot.episode_revisions
        }
        if exact_ref not in members:
            return False
        current_snapshot_ids[local_date] = day.current_snapshot_id

    journals = await TimelinePublicationJournal.find(
        TimelinePublicationJournal.user_id == episode.user_id,
        TimelinePublicationJournal.status == "committed",
        {
            "affected_days.resulting_snapshot.snapshot_id": {
                "$in": sorted(set(current_snapshot_ids.values()))
            }
        },
    ).to_list()
    committed_dates: set[date] = set()
    for journal in journals:
        for plan in journal.affected_days:
            if (
                plan.timezone != episode.timezone
                or current_snapshot_ids.get(plan.local_date)
                != plan.resulting_snapshot.snapshot_id
            ):
                continue
            members = {
                (item.episode_key, int(item.revision))
                for item in plan.resulting_snapshot.episode_revisions
            }
            if exact_ref in members:
                committed_dates.add(plan.local_date)
    return committed_dates == set(required_dates)


async def episode_summary_eligibility(
    episode: TimelineEpisode,
) -> EpisodeSummaryEligibility:
    """Check exact-revision snapshot membership before enqueueing derived work."""

    if not episode.conversational:
        return EpisodeSummaryEligibility(False, None, "not_conversational")
    if episode.status == "superseded":
        return EpisodeSummaryEligibility(False, None, "superseded")
    if not episode_structure_is_stable(episode):
        return EpisodeSummaryEligibility(False, None, "structure_not_stable")

    if not await episode_revision_is_published(episode):
        return EpisodeSummaryEligibility(
            False,
            None,
            "revision_not_in_committed_current_snapshots",
        )
    transcript = bounded_episode_transcript(episode)
    if not transcript:
        return EpisodeSummaryEligibility(False, None, "no_bounded_transcript")
    return EpisodeSummaryEligibility(
        True,
        episode_summary_scope_hash(episode, transcript=transcript),
        "eligible",
    )
