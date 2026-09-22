"""Recover bounded historical display requirements from original recorder rows.

The recorder stores changes, not every successful inventory poll. Therefore a
changed set is uncertain all the way back to the preceding recorded observation.
No polling duration, missing frame, or log timestamp establishes safety here.
"""

import bisect
import hashlib
import json
from datetime import datetime, timedelta, timezone

from .collector import open_screenpipe_db
from .screening import ScreeningStore, seconds, stamp


def inventory_payload(connection, start, end, original_policy_version):
    low, high = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end))
    if low.tzinfo is None or high.tzinfo is None:
        raise ValueError("Historical inventory requires explicit timezones")
    low, high = low.astimezone(timezone.utc), high.astimezone(timezone.utc)
    if not timedelta(0) < high - low <= timedelta(days=32):
        raise ValueError("Historical inventory requires at most 32 days")
    if high > datetime.now(timezone.utc):
        raise ValueError("Historical inventory cannot cover future capture")
    rows = connection.execute(
        "SELECT id,timestamp,layout_json FROM display_layout "
        "WHERE julianday(timestamp)<=julianday(?) ORDER BY julianday(timestamp),id",
        (high.isoformat(),),
    ).fetchall()
    observations = []
    for identifier, timestamp, raw in rows:
        observed = stamp(seconds(timestamp))
        layout = json.loads(raw)
        if not isinstance(layout, list) or len(layout) > 32:
            raise ValueError("Original display inventory is invalid")
        if any(
            not isinstance(display, dict)
            or type(display.get("id")) is not int
            or display["id"] < 0
            for display in layout
        ):
            raise ValueError("Original display identity is invalid")
        tracks = sorted({f"monitor_{d['id']}" for d in layout})
        if any(d.get("width", 0) <= 0 or d.get("height", 0) <= 0 for d in layout):
            tracks = []
        previous = observations[-1] if observations else None
        if previous and seconds(observed) <= seconds(previous["observed_at"]):
            raise ValueError("Original inventory timestamps are ambiguous")
        observations.append(
            {
                "observed_at": observed,
                "transition_started_at": (
                    previous["observed_at"]
                    if previous and previous["track_ids"] != tracks
                    else observed
                ),
                "track_ids": tracks,
                "evidence_sha256": hashlib.sha256(
                    json.dumps(
                        [identifier, timestamp, raw],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        )
    if not observations:
        raise ValueError("Original display inventory is unavailable")
    times = [seconds(r["observed_at"]) for r in observations]
    tracks = set()
    frames = connection.execute(
        "SELECT device_name,timestamp FROM frames "
        "WHERE julianday(timestamp)>=julianday(?) AND julianday(timestamp)<julianday(?)",
        (low.isoformat(), high.isoformat()),
    )
    for track, timestamp in frames:
        track = track or "unknown-display"
        tracks.add(track)
        captured = seconds(timestamp)
        index = bisect.bisect_right(times, captured) - 1
        matches_observed = index >= 0 and track in observations[index]["track_ids"]
        following = observations[index + 1] if index + 1 < len(observations) else None
        # A newly attached display can produce a frame before the recorder's
        # next topology observation. That entire transition is already held by
        # the backend. Retain the original observation and uncertainty bounds;
        # this frame cannot establish an earlier safe display inventory.
        matches_held_transition = (
            index >= 0
            and following is not None
            and seconds(following["transition_started_at"])
            <= captured
            < seconds(following["observed_at"])
            and track in following["track_ids"]
        )
        if not matches_observed and not matches_held_transition:
            raise ValueError("Original frames disagree with recorded display inventory")
    # Retain the observation before the range; later transitions may begin there.
    index = max(0, bisect.bisect_right(times, low.timestamp()) - 1)
    observations = observations[index:]
    if not tracks.issubset({t for row in observations for t in row["track_ids"]}):
        raise ValueError("Historical displays are not fully represented")
    return {
        "original": {
            "started_at": low.isoformat(),
            "ended_at": high.isoformat(),
            "track_ids": sorted(tracks),
            "coverage": "historical_observed_displays",
            "policy_version": original_policy_version,
        },
        "observations": observations,
    }


def queue_inventory(config, state_dir, start, end, original_policy_version):
    with open_screenpipe_db(config.screenpipe_dir / "db.sqlite") as connection:
        payload = inventory_payload(connection, start, end, original_policy_version)
    store = ScreeningStore(state_dir / "privacy.sqlite")
    try:
        store.refine_inventory(payload)
    finally:
        store.close()
    return {"queued": True, "inventory_observations": len(payload["observations"])}
