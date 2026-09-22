"""Semantic organization keeps supplied activity units intact.

These profiles locate related activities. The resulting account still resolves all
original evidence through the common source-scope validator before memory writing.
"""

import json
from collections import defaultdict
from types import SimpleNamespace

from pydantic import BaseModel, Field

import backend.services.timeline.pi_tasks as pi_tasks
from backend.services.inference_artifacts import canonical_hash

from .memory_sources import evidence_sources, utc

VERSION = "session-organization-v1"


class Group(BaseModel):
    units: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class Organization(BaseModel):
    groups: list[Group]


def profile(unit):
    sources = evidence_sources(unit.members)
    tracks = defaultdict(list)
    for source in sources:
        tracks[
            (
                json.dumps(source["locator"], sort_keys=True),
                source["role"],
                source["direction"],
            )
        ].append(source)
    return {
        "unit": unit.key,
        "title": unit.title,
        "account": unit.summary[:1600],
        "started_at": min(utc(e.started_at) for e in unit.members).isoformat(),
        "ended_at": max(utc(e.ended_at) for e in unit.members).isoformat(),
        "members": [
            {"episode_key": e.episode_key, "revision": e.revision} for e in unit.members
        ],
        "source_manifest_hash": canonical_hash(sources),
        "tracks": [
            {
                "locator": rows[0]["locator"],
                "role": role,
                "direction": direction,
                "evidence_count": len(rows),
                "meeting_ids": sorted(
                    {
                        str(s["metadata"]["meeting_id"])
                        for s in rows
                        if s["metadata"].get("meeting_id")
                    }
                ),
                "examples": [
                    {
                        "source_key": s["key"],
                        "text": s["excerpt"][:500],
                        "started_at": s["started_at"],
                    }
                    for s in (rows[:1] + rows[-1:] if len(rows) > 1 else rows)
                ],
            }
            for (_, role, direction), rows in tracks.items()
        ],
    }


async def organize_batch(units, record):

    def validate(result):
        assigned = [key for group in result.groups for key in group.units]
        if len(assigned) != len(set(assigned)) or set(assigned) != {
            u.key for u in units
        }:
            raise ValueError(
                "Session organization omitted, duplicated or invented an activity"
            )

    async def retain(run):
        await record(run["artifact_hash"])

    members = [e for unit in units for e in unit.members]
    outcome = await pi_tasks.run_task(
        stage="session_organization",
        artifact_operation=VERSION,
        instruction="Organize the supplied units into meaningful sessions, grounding membership in evidence. Existing units are indivisible; return each exactly once. Preserve unresolved distinctions.",
        payload={"units": [profile(unit) for unit in units]},
        sources=evidence_sources(members),
        result_type=Organization,
        validate=validate,
        user_id=getattr(members[0], "user_id", None),
        record=retain,
    )
    result = outcome.result
    by_key = {u.key: u for u in units}
    return [
        SimpleNamespace(
            key=canonical_hash(
                sorted(e.episode_id for key in group.units for e in by_key[key].members)
            ),
            title=group.title,
            summary=group.reason,
            members=[e for key in group.units for e in by_key[key].members],
        )
        for group in result.groups
    ]


async def organize_sessions(episodes, *, record):
    units = [
        SimpleNamespace(
            key=e.episode_id, title=e.title, summary=e.summary or e.title, members=[e]
        )
        for e in episodes
    ]
    # Pagination bounds tool context; a capacity boundary does not constrain membership.
    return await organize_batch(units, record) if len(units) > 1 else units
