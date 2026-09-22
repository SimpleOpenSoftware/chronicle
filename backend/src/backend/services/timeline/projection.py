"""Deterministic database projections rendered from active timeline episodes.

The day view is a projection, not a second interpretation pass. Whenever a material
episode revision is published, this module recomputes the desired ``## Episodes``
snapshot for every local day the episode touches and writes it only when the content
actually differs — a reconciliation that changed nothing writes nothing. Provisional
projection never mutates the Markdown vault; reviewed memory publication owns that.

Two invariants shape the queries here:

- **Cross-midnight.** An episode belongs to every local day its absolute interval
  intersects, so days are selected by UTC-interval overlap against
  :func:`evidence.day_bounds`, never by ``local_date`` equality.
- **Canonical revisions.** Rolling reconciliation is the sole writer. A projection
  renders every non-superseded revision intersecting the day; the installed snapshot
  is the fence that identifies the exact reviewable set.

Reviewed vault writes reuse the deterministic renderers in ``vault_day_index``. They
are re-exported here for the existing memory-provider seam, but this projection path
does not read or write the filesystem.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import backend.services.privacy as privacy
from backend.models.timeline import (
    EpisodeRevisionRef,
    TimelineDay,
    TimelineEpisode,
    TimelinePublicationDayPlan,
)

from .publication import publish_timeline_revision
from .snapshots import build_day_snapshot, evidence_state_hash_for_episodes
from .timezone import canonical_timezone
from .vault_day_index import (
    ensure_day_episode_index,
    render_day_episode_index,
    replace_h2_section,
)

__all__ = [
    "affected_local_dates",
    "active_day_episodes",
    "ensure_day_episode_index",
    "refresh_day_projection",
    "refresh_projections",
    "render_day_episode_index",
    "replace_h2_section",
]

logger = logging.getLogger(__name__)

# Rows a projection must never render: superseded revisions are history.
ACTIVE_EPISODE_QUERY = {"status": {"$ne": "superseded"}}


def _utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; compare everything in UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def affected_local_dates(
    started_at: datetime, ended_at: datetime, timezone_name: str
) -> list[date]:
    """Every local date the absolute interval ``[started_at, ended_at)`` intersects.

    An episode running past midnight belongs to both days, so a single ``local_date``
    field cannot answer this. Candidate dates are enumerated from the local calendar
    and then tested against :func:`day_bounds`, which is what makes this correct on a
    DST transition: a local day there is 23 or 25 hours long, and the offset used to
    derive the calendar date is not the offset that bounds it.
    """

    # Lazy because evidence imports the memory package, whose Chronicle provider
    # reuses the deterministic index renderers in this module.
    from .evidence import day_bounds

    timezone_name = canonical_timezone(timezone_name)
    zone = ZoneInfo(timezone_name)
    start, end = _utc(started_at), _utc(ended_at)
    if end < start:
        start, end = end, start

    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    dates: list[date] = []
    # One day of slack on either side: a UTC instant can fall in a neighbouring local
    # date under an offset the naive conversion above did not use.
    candidate = first - timedelta(days=1)
    while candidate <= last + timedelta(days=1):
        day_start, day_end = day_bounds(candidate, timezone_name)
        # Half-open on both sides, except for a zero-length interval, which belongs to
        # the day containing its instant.
        if day_start <= start < day_end or (start < day_end and end > day_start):
            dates.append(candidate)
        candidate += timedelta(days=1)
    return dates


async def active_day_episodes(
    user_id: str,
    local_date: date,
    timezone_name: str,
) -> list[TimelineEpisode]:
    """Active rolling revisions intersecting this local day."""

    # See the circular-import boundary documented in ``affected_local_dates``.
    from .evidence import day_bounds

    timezone_name = canonical_timezone(timezone_name)
    day_start, day_end = day_bounds(local_date, timezone_name)
    query: dict = {
        "user_id": user_id,
        "started_at": {"$lt": day_end},
        "ended_at": {"$gt": day_start},
        **ACTIVE_EPISODE_QUERY,
    }

    episodes = await TimelineEpisode.find(query).to_list()

    episodes = await privacy.filter_records(episodes, user_id)
    episodes.sort(key=lambda item: (_utc(item.started_at), _utc(item.ended_at)))
    return episodes


async def refresh_day_projection(
    user_id: str, local_date: date, timezone_name: str
) -> bool:
    """Install this local day's canonical database snapshot if content changed."""

    timezone_name = canonical_timezone(timezone_name)
    plan, episode_count = await _projection_plan(user_id, local_date, timezone_name)
    if plan is None:
        return False
    await publish_timeline_revision(
        user_id=user_id,
        operation_source="projection",
        affected_days=[plan],
    )
    logger.info(
        "Day snapshot updated for %s %s (%d episode(s), snapshot=%s)",
        user_id,
        local_date,
        episode_count,
        plan.resulting_snapshot.snapshot_id,
    )
    return True


async def _projection_plan(
    user_id: str, local_date: date, timezone_name: str
) -> tuple[TimelinePublicationDayPlan | None, int]:
    """Compute one day plan without mutating its publication or review state."""

    await _ensure_day_row(user_id, local_date, timezone_name)
    episodes = await active_day_episodes(user_id, local_date, timezone_name)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None:  # pragma: no cover - the unique insert above must be visible
        raise RuntimeError("timeline day disappeared while projecting")
    episode_refs = [
        EpisodeRevisionRef(episode_key=item.episode_key, revision=int(item.revision))
        for item in episodes
    ]
    episode_revision_ids = {
        (item.episode_key, int(item.revision)) for item in episode_refs
    }
    group_history = {
        (group.group_key, int(group.revision)): group
        for group in day.semantic_group_history
    }
    group_refs = []
    if day.current_snapshot is not None:
        for ref in day.current_snapshot.semantic_group_revisions:
            if ref.owner_local_date != local_date:
                continue
            group = group_history.get((ref.group_key, int(ref.revision)))
            if group is None or group.status != "active":
                continue
            members = {
                (item.episode_key, int(item.revision))
                for item in group.member_revisions
            }
            if members <= episode_revision_ids:
                group_refs.append(ref)
    snapshot = build_day_snapshot(
        user_id=user_id,
        local_date=local_date,
        timezone_name=timezone_name,
        evidence_state_hash=evidence_state_hash_for_episodes(episodes),
        episode_revisions=episode_refs,
        semantic_group_revisions=group_refs,
    )
    if day.current_snapshot_id == snapshot.snapshot_id:
        return None, len(episodes)
    return (
        TimelinePublicationDayPlan(
            local_date=local_date,
            timezone=timezone_name,
            base_snapshot_id=day.current_snapshot_id,
            resulting_snapshot=snapshot,
        ),
        len(episodes),
    )


async def _ensure_day_row(user_id: str, local_date: date, timezone_name: str) -> None:
    existing = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if existing is not None:
        return
    await TimelineDay(
        user_id=user_id, local_date=local_date, timezone=timezone_name
    ).insert()


async def refresh_projections(
    user_id: str,
    dates: Optional[list[date]] = None,
    *,
    episode: Optional[TimelineEpisode] = None,
    timezone_name: Optional[str] = None,
) -> list[date]:
    """Post-publish hook: refresh the days an episode revision affects.

    Returns the dates whose projection content actually changed, so a caller can
    report real work rather than the number of days it looked at.
    """

    if timezone_name is None and episode is not None:
        timezone_name = episode.timezone
    if timezone_name is None:
        raise ValueError("refresh_projections needs a timezone or an episode")
    timezone_name = canonical_timezone(timezone_name)

    if dates is None:
        if episode is None:
            raise ValueError("refresh_projections needs dates or an episode")
        dates = affected_local_dates(
            episode.started_at, episode.ended_at, timezone_name
        )

    plans: list[TimelinePublicationDayPlan] = []
    for local_date in sorted(set(dates)):
        plan, _ = await _projection_plan(user_id, local_date, timezone_name)
        if plan is not None:
            plans.append(plan)
    if not plans:
        return []
    # A cross-midnight episode changes both day projections under one journal, so a
    # crash cannot expose one side as ready while the other remains stale.
    await publish_timeline_revision(
        user_id=user_id,
        operation_source="projection",
        affected_days=plans,
    )
    return [item.local_date for item in plans]
