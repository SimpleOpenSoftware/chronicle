"""Queue a bounded historical range through the collector's regular worker."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx

from .collector import open_screenpipe_db
from .screen_model import MODEL_REVISION
from .screening import POLICY, ScreeningStore, adult_context, seconds, stamp


def queue_history(config, state_dir, start, end):
    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Historical bounds require an explicit timezone")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if not timedelta(0) < end - start <= timedelta(days=32):
        raise ValueError("Historical screening requires a range of at most 32 days")
    identity = hashlib.sha256(
        f"{start.isoformat()}:{end.isoformat()}:{MODEL_REVISION}:{POLICY}:pad-tiles60-v1".encode()
    ).hexdigest()[:24]
    store = ScreeningStore(
        state_dir / "privacy.sqlite", namespace=f"history-{identity}"
    )
    count = 0
    try:
        with open_screenpipe_db(config.screenpipe_dir / "db.sqlite") as connection:
            tracks = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT COALESCE(NULLIF(device_name,''),'unknown-display') "
                    "FROM frames WHERE julianday(timestamp)>=julianday(?) "
                    "AND julianday(timestamp)<julianday(?) ORDER BY 1",
                    (start.isoformat(), end.isoformat()),
                )
            ]
            if len(tracks) > 32:
                raise ValueError(
                    "Historical range contains too many display identities"
                )
            store.require_range(
                {
                    "started_at": start.isoformat(),
                    "ended_at": end.isoformat(),
                    "track_ids": tracks,
                    "coverage": "historical_observed_displays",
                    "policy_version": POLICY,
                }
            )
            with httpx.Client(
                base_url=config.backend_url.rstrip("/"),
                headers={"Authorization": f"Bearer {config.token}"},
                timeout=30,
            ) as client:
                store.deliver_required_ranges(client)
            rows = connection.execute(
                "SELECT id,timestamp,device_name,app_name,window_name,browser_url,full_text "
                "FROM frames WHERE julianday(timestamp)>=julianday(?) "
                "AND julianday(timestamp)<julianday(?) ORDER BY id",
                (start.isoformat(), end.isoformat()),
            )
            while batch := rows.fetchmany(100):
                store.observe_many(batch)
                count += len(batch)
        store.flush()
        return {
            "frames_visited": count,
            "pending_jobs": store.pending_count(),
            "range_id": identity,
        }
    finally:
        store.close()


def queue_gap_rechecks(config, state_dir, start, end):
    """Recheck v1 sampling-gap holds using original frames and normal inference.

    Actual gaps, unavailable capture identities, and positive/uncertain evidence
    keep their existing decisions. Merely finding a candidate never releases it.
    """
    if any(
        datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None
        for value in (start, end)
    ):
        raise ValueError("Recheck bounds require an explicit timezone")
    low, high = seconds(start), seconds(end)
    if not 0 < high - low <= 32 * 86400:
        raise ValueError("Rechecks require a range of at most 32 days")
    store = ScreeningStore(state_dir / "privacy.sqlite")
    stats = dict(examined=0, queued=0, already_queued=0, genuine_gaps=0, unavailable=0)
    after = ""
    try:
        with open_screenpipe_db(
            config.screenpipe_dir / "db.sqlite"
        ) as connection, httpx.Client(
            base_url=config.backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=30,
        ) as client:
            while True:
                response = client.get(
                    "/api/device-input/screening/results",
                    params={
                        "start_at": start,
                        "end_at": end,
                        "policy_version": "screen-privacy-v1",
                        "after": after,
                    },
                )
                response.raise_for_status()
                page = response.json()
                for result in page["results"]:
                    stats["examined"] += 1
                    if (
                        not any(s["state"] == "pending" for s in result["segments"])
                        or not result.get("evidence")
                        or any(e["state"] != "allowed" for e in result["evidence"])
                    ):
                        continue
                    first, last = result["evidence"][0], result["evidence"][-1]
                    rows = connection.execute(
                        "SELECT id,timestamp,device_name,app_name,window_name,browser_url,full_text "
                        "FROM frames WHERE id>=? AND id<=? "
                        "AND COALESCE(NULLIF(device_name,''),'unknown-display')=? ORDER BY id",
                        (first["frame_id"], last["frame_id"], result["track_id"]),
                    ).fetchall()
                    if (
                        len(rows) < 2
                        or rows[0]["id"] != first["frame_id"]
                        or rows[-1]["id"] != last["frame_id"]
                        or stamp(seconds(rows[0]["timestamp"]))
                        != stamp(seconds(result["started_at"]))
                        or stamp(seconds(rows[-1]["timestamp"]))
                        != stamp(seconds(result["ended_at"]))
                    ):
                        stats["unavailable"] += 1
                        continue
                    gaps = [
                        seconds(b["timestamp"]) - seconds(a["timestamp"])
                        for a, b in zip(rows, rows[1:])
                    ]
                    if min(gaps) <= 0 or max(gaps) > 30:
                        stats["genuine_gaps"] += 1
                        continue
                    frames = []
                    for row in rows:
                        state = adult_context(dict(row))
                        frames.append(
                            {
                                "id": row["id"],
                                "time": stamp(seconds(row["timestamp"])),
                                "suspicious": bool(state),
                                "text_state": state,
                            }
                        )
                    queued = store.queue_recheck(
                        result["track_id"], frames, result["interval_id"]
                    )
                    stats["queued" if queued else "already_queued"] += 1
                cursor = page.get("next_cursor")
                if cursor is None:
                    break
                if cursor <= after:
                    raise ValueError("Screening result cursor did not advance")
                after = cursor
        return stats
    finally:
        store.close()
