"""Refine a historical display obligation using original recorder inventories.

This changes required displays, never detector predictions or user overrides.
Uncertain topology transitions remain empty-inventory holds. Replacement is
durable, source-scoped and fenced against publication just like screening.
"""

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

import backend.services.timeline.dirty_ranges as dirty_ranges
from backend.services import privacy


class InventoryObservation(privacy.PrivacyDisplaySet):
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def explicit_timezones(cls, values):
        for key in ("observed_at", "transition_started_at"):
            value = values.get(key)
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError("historical inventory needs explicit timezones")
        return values


class InventoryRefinement(BaseModel):
    original: privacy.PrivacyRequiredRange
    observations: list[InventoryObservation] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def bounded_history(self):
        previous = None
        tracks = set()
        for row in self.observations:
            if row.observed_at > self.original.ended_at:
                raise ValueError(
                    "inventory observation is outside the historical range"
                )
            if previous is not None and (
                row.observed_at <= previous or row.transition_started_at < previous
            ):
                raise ValueError("inventory order or transition is ambiguous")
            previous = row.observed_at
            tracks.update(row.track_ids)
        if not set(self.original.track_ids).issubset(tracks):
            raise ValueError(
                "inventory must account for every originally observed display"
            )
        return self

    def periods(self):
        low, high = self.original.started_at, self.original.ended_at
        edges = {low, high}
        for row in self.observations:
            edges.update(
                max(low, min(high, t))
                for t in (
                    row.observed_at,
                    row.transition_started_at,
                )
            )
        points = sorted(edges)
        output = []
        for start, end in zip(points, points[1:]):
            known = [r for r in self.observations if r.observed_at <= start]
            uncertain = any(
                r.transition_started_at < end and r.observed_at > start
                for r in self.observations
            )
            tracks = known[-1].track_ids if known and not uncertain else []
            if output and output[-1]["track_ids"] == tracks:
                output[-1]["ended_at"] = end
            else:
                output.append(dict(started_at=start, ended_at=end, track_ids=tracks))
        return output


async def refine(source, body: InventoryRefinement):

    # Revalidate even when invoked directly by a job with a mutated model.
    body = InventoryRefinement.model_validate(body.model_dump())
    db = privacy.database()
    owner = str(source.user_id)
    scope = {"user_id": owner, "source_id": source.source_id}
    original = await db.privacy_required_ranges.find_one(
        {
            **scope,
            **body.original.model_dump(),
        }
    )
    if original is None:
        raise ValueError("original historical obligation was not found")
    operation = hashlib.sha256(
        (
            owner + ":" + source.source_id + ":inventory:" + body.model_dump_json()
        ).encode()
    ).hexdigest()
    if original.get("superseded_by") not in (None, operation):
        raise ValueError("historical inventory has already changed")
    pending = {**scope, "privacy_operation": operation, "privacy_updating": True}
    if original.get("superseded_by") == operation:
        if not await db.capture_sources.find_one(pending):
            return
    elif not await db.capture_sources.find_one(pending):
        acquired = await privacy.begin_update(
            owner,
            {
                **scope,
                "privacy_operation": None,
            },
            {
                "$inc": {"privacy_revision": 1},
                "$set": {
                    "privacy_operation": operation,
                    "privacy_updating": True,
                },
            },
        )
        if not acquired.matched_count:
            raise ValueError("privacy policy update in progress")
    # Another request can finish after our initial read but before acquisition.
    # Now that this operation owns the source hold, recheck the immutable target
    # before writing any children or retiring the original obligation.
    current = await db.privacy_required_ranges.find_one(
        {"_id": original["_id"], **scope}
    )
    if current is None:
        raise ValueError("original historical obligation is unavailable")
    if current.get("superseded_by"):
        await db.capture_sources.update_one(
            pending,
            {"$set": {"privacy_operation": None, "privacy_updating": False}},
        )
        if current["superseded_by"] == operation:
            return
        raise ValueError("historical inventory has already changed")
    await db.privacy_inventory_refinements.update_one(
        {"_id": operation},
        {
            "$setOnInsert": {
                **scope,
                "original_required_range": original["_id"],
                "observations": [r.model_dump() for r in body.observations],
                "started_at": body.original.started_at,
                "ended_at": body.original.ended_at,
            }
        },
        upsert=True,
    )
    for index, period in enumerate(body.periods()):
        identity = hashlib.sha256(f"{operation}:{index}".encode()).hexdigest()
        await db.privacy_required_ranges.update_one(
            {"_id": identity},
            {
                "$setOnInsert": {
                    **scope,
                    **period,
                    "coverage": "historical_recorded_displays",
                    "policy_version": body.original.policy_version,
                    "inventory_refinement": operation,
                }
            },
            upsert=True,
        )
    # Both the original and replacement obligations apply until invalidation
    # succeeds. The source stays durably held across interruption at any step.
    await dirty_ranges.mark_evidence_dirty(
        owner,
        body.original.started_at,
        body.original.ended_at,
        operation,
        "privacy_historical_inventory",
        source_kind="privacy",
    )
    await db.privacy_required_ranges.update_one(
        {"_id": original["_id"], **scope},
        {"$set": {"superseded_by": operation}},
    )
    await db.capture_sources.update_one(
        pending,
        {"$set": {"privacy_operation": None, "privacy_updating": False}},
    )
