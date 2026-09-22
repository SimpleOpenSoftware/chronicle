"""Durable local screen screening. No image or OCR body enters the result ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from .frames import read_frame
from .screen_model import FreepikModel

log = logging.getLogger(__name__)
POLICY = "screen-privacy-v3"
RETRY_REASONS = {"missing_frame", "screening_failed"}
ADULT_SITE = re.compile(
    r"\b(?:pornhub|xvideos|xnxx|xhamster|redtube|youporn|spankbang|eporner|beeg|brazzers|chaturbate|stripchat)\b",
    re.I,
)
ADULT_QUERY = re.compile(
    r"\b(?:porn(?:ography|ographic)?|xxx|hentai|nudes?|sex\s+videos?)\b", re.I
)


def adult_context(row):
    """Text policy is independent of visual nudity, including adult searches."""
    text = " ".join(
        str(row.get(key) or "") for key in ("browser_url", "window_name", "full_text")
    )
    url = urlparse(str(row.get("browser_url") or ""))
    if url.hostname and ADULT_SITE.search(url.hostname):
        return "excluded"
    query = " ".join(parse_qs(url.query).get("q", []))
    search_url = url.hostname and any(
        url.hostname == host or url.hostname.endswith("." + host)
        for host in ("google.com", "google.co.in", "bing.com", "duckduckgo.com")
    )
    if search_url and ADULT_QUERY.search(query):
        return "excluded"
    # OCR-only search results: require search-page structure, not an isolated
    # sexual term in a conversation, health article or programming document.
    if (
        re.search(r"\b(?:google|bing)\b", text, re.I)
        and re.search(r"\bimages\b", text, re.I)
        and re.search(r"\bvideos\b", text, re.I)
        and (ADULT_QUERY.search(text) or ADULT_SITE.search(text))
    ):
        return "excluded"
    # A site name in arbitrary text can be a discussion or a background tab.
    # Hold for review rather than assert that the site was being browsed.
    return "needs_review" if ADULT_SITE.search(text) else None


def seconds(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(
        timespec="milliseconds"
    )


def verdict(predictions):
    regions = {}
    for prediction in predictions:
        if prediction["class"] in {"medium", "high"}:
            region = tuple(prediction["region"])
            regions[region] = regions.get(region, 0) + float(prediction["score"])
    score = min(1.0, max(regions.values(), default=0))
    return (
        "excluded" if score >= 0.60 else "needs_review" if score >= 0.30 else "allowed"
    ), score


def read_display_tracks(config):
    """Use connected recorder displays, not absence of recent frame rows."""
    headers = (
        {"Authorization": f"Bearer {config.screenpipe_token}"}
        if config.screenpipe_token
        else {}
    )
    response = httpx.get(
        config.screenpipe_url.rstrip("/") + "/vision/list", headers=headers, timeout=10
    )
    response.raise_for_status()
    monitors = response.json()
    if not isinstance(monitors, list) or any(
        not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 0
        for row in monitors
    ):
        raise ValueError("Recorder display inventory is unavailable")
    return sorted({f"monitor_{row['id']}" for row in monitors})


class ScreeningStore:
    """SQLite serializes scheduling, cache and acknowledged outbox transactions."""

    def __init__(self, path: Path, cache_limit=100_000, namespace="live"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.cache_limit = cache_limit
        self.namespace = namespace
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS tracks(track TEXT PRIMARY KEY, cursor INTEGER, state TEXT);
          CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, track TEXT, frames TEXT,
              result TEXT, delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              retry_at REAL NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS predictions(image TEXT, model TEXT, predictions TEXT,
              perceptual TEXT, used REAL, PRIMARY KEY(image, model));
          CREATE INDEX IF NOT EXISTS prediction_lru ON predictions(used);
          CREATE INDEX IF NOT EXISTS prediction_similarity ON predictions(model, perceptual);
          CREATE TABLE IF NOT EXISTS display_inventory(id INTEGER PRIMARY KEY CHECK(id=1), tracks TEXT, checked_at TEXT);
          CREATE TABLE IF NOT EXISTS display_outbox(observed_at TEXT PRIMARY KEY, payload TEXT, delivered INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS required_range_outbox(identity TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS inventory_refinement_outbox(identity TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rechecks(previous_interval TEXT PRIMARY KEY, job_id INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS recheck_job ON rechecks(job_id);
          CREATE TABLE IF NOT EXISTS screening_progress(job_id INTEGER PRIMARY KEY, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS replacement_outbox(job_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0);
        """)

    def close(self):
        self.db.close()

    def queue_recheck(self, track, frames, previous_interval):
        """Add an exact-interval recheck without changing capture sampling cadence."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM rechecks WHERE previous_interval=?", (previous_interval,)
            ).fetchone():
                return False
            job = self.db.execute(
                "INSERT INTO jobs(track,frames,priority) VALUES (?,?,5)",
                (track, json.dumps(frames)),
            ).lastrowid
            self.db.execute(
                "INSERT INTO rechecks VALUES (?,?)", (previous_interval, job)
            )
        return True

    def replacement(self):
        row = self.db.execute(
            "SELECT * FROM replacement_outbox WHERE ready=1 ORDER BY job_id LIMIT 1"
        ).fetchone()
        return (row["job_id"], json.loads(row["payload"])) if row else None

    def acknowledge_replacement(self, job_id):
        with self.db:
            self.db.execute("DELETE FROM replacement_outbox WHERE job_id=?", (job_id,))

    def replacement_count(self):
        return self.db.execute("SELECT count(*) FROM replacement_outbox").fetchone()[0]

    def observe_displays(self, tracks, observed_at):
        tracks = sorted(set(tracks))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT tracks,checked_at FROM display_inventory WHERE id=1"
            ).fetchone()
            if previous is None or json.loads(previous["tracks"]) != tracks:
                payload = {
                    "observed_at": observed_at,
                    "transition_started_at": (
                        previous["checked_at"] if previous else observed_at
                    ),
                    "track_ids": tracks,
                }
                self.db.execute(
                    "INSERT INTO display_outbox(observed_at,payload) VALUES (?,?)",
                    (observed_at, json.dumps(payload)),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO display_inventory VALUES (1,?,?)",
                (json.dumps(tracks), observed_at),
            )

    def display_results(self):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM display_outbox WHERE delivered=0 ORDER BY observed_at"
            )
        ]

    def acknowledge_displays(self, observed_at):
        with self.db:
            self.db.execute(
                "DELETE FROM display_outbox WHERE observed_at=?", (observed_at,)
            )

    def require_range(self, payload):
        encoded = json.dumps(payload, sort_keys=True)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO required_range_outbox VALUES (?,?)",
                (identity, encoded),
            )

    def deliver_required_ranges(self, client):
        for identity, payload in self.db.execute(
            "SELECT identity,payload FROM required_range_outbox"
        ).fetchall():
            client.post(
                "/api/device-input/screening/required-range", json=json.loads(payload)
            ).raise_for_status()
            with self.db:
                self.db.execute(
                    "DELETE FROM required_range_outbox WHERE identity=?", (identity,)
                )
        for identity, payload in self.db.execute(
            "SELECT identity,payload FROM inventory_refinement_outbox"
        ).fetchall():
            client.post(
                "/api/device-input/screening/required-range/inventory",
                json=json.loads(payload),
            ).raise_for_status()
            with self.db:
                self.db.execute(
                    "DELETE FROM inventory_refinement_outbox WHERE identity=?",
                    (identity,),
                )

    def refine_inventory(self, payload):
        encoded = json.dumps(payload, sort_keys=True)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO inventory_refinement_outbox VALUES (?,?)",
                (identity, encoded),
            )

    def observe(self, row):
        self.observe_many([row])

    def observe_many(self, rows):
        """Commit capture cadence and its jobs together, including on backfill."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in rows:
                self._observe(row)

    def _observe(self, row):
        row = dict(row)
        track = str(row.get("device_name") or "unknown-display")
        key = f"{self.namespace}/{track}"
        found = self.db.execute("SELECT * FROM tracks WHERE track=?", (key,)).fetchone()
        if found and int(row["id"]) <= found["cursor"]:
            return
        state = (
            json.loads(found["state"])
            if found
            else {"count": 0, "frames": [], "last": None, "context": None}
        )
        context = [
            str(row.get(k) or "") for k in ("app_name", "window_name", "browser_url")
        ]
        moment = seconds(row["timestamp"])
        text_state = adult_context(row)
        suspicious = bool(text_state)
        context = hashlib.sha256(json.dumps(context).encode()).hexdigest()
        # Only opaque identifiers and a rule flag are durable; never copy OCR bodies.
        frame = {
            "id": int(row["id"]),
            "time": stamp(moment),
            "suspicious": suspicious,
            "text_state": text_state,
        }
        state["frames"].append(frame)
        state["count"] += 1
        due = (
            state["last"] is None
            or (state["count"] - 1) % 10 == 0
            or moment - state["last"] >= 30
            or context != state["context"]
            or suspicious
        )
        if due:
            priority = 0 if self.namespace == "live" else 5 if suspicious else 10
            self.db.execute(
                "INSERT INTO jobs(track,frames,priority) VALUES (?,?,?)",
                (track, json.dumps(state["frames"]), priority),
            )
            state["frames"] = [frame]
            state["last"] = moment
        state["context"] = context
        self.db.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?,?,?)",
            (key, frame["id"], json.dumps(state)),
        )

    def jobs(self, limit=100, historical_only=False):
        return [
            dict(
                id=r["id"],
                track=r["track"],
                frames=json.loads(r["frames"]),
                result=json.loads(r["result"]) if r["result"] else None,
                previous_result=(
                    json.loads(r["previous_result"]) if r["previous_result"] else None
                ),
            )
            for r in self.db.execute(
                "SELECT j.*, p.result AS previous_result FROM jobs j "
                "LEFT JOIN screening_progress p ON p.job_id=j.id "
                "WHERE j.delivered=0 AND j.retry_at<=? AND (?=0 OR j.priority>0) "
                "AND NOT EXISTS (SELECT 1 FROM replacement_outbox r WHERE r.job_id=j.id AND r.ready=1) "
                # Preserve live/candidate priority. Background batches share a
                # fairness class: deliver durable results, then inspect untouched
                # capture before rescreening already-held gaps. Recent-history
                # promotion must not let its retries starve untouched old history.
                "ORDER BY CASE WHEN j.priority<8 THEN j.priority ELSE 8 END,"
                "(j.result IS NULL),(j.attempts>0),j.priority,j.retry_at,j.id LIMIT ?",
                (time.time(), int(historical_only), limit),
            )
        ]

    def flush(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in self.db.execute(
                "SELECT * FROM tracks WHERE track LIKE ?", (self.namespace + "/%",)
            ).fetchall():
                state = json.loads(row["state"])
                if len(state["frames"]) > 1:
                    self.db.execute(
                        "INSERT INTO jobs(track,frames,priority) VALUES (?,?,10)",
                        (row["track"].split("/", 1)[1], json.dumps(state["frames"])),
                    )
                    state["frames"] = state["frames"][-1:]
                    self.db.execute(
                        "UPDATE tracks SET state=? WHERE track=?",
                        (json.dumps(state), row["track"]),
                    )

    def pending_count(self):
        return self.db.execute(
            "SELECT count(*) FROM jobs WHERE delivered=0"
        ).fetchone()[0]

    def failed_count(self):
        return self.db.execute(
            "SELECT count(*) FROM jobs WHERE delivered=0 AND attempts>0"
        ).fetchone()[0]

    def queue_health(self):
        """Report live queue age separately from history, including held retries."""
        row = self.db.execute(
            "SELECT count(*) AS pending, coalesce(sum(attempts>0),0) AS failed, "
            "coalesce(sum(priority=0),0) AS live, "
            "min(CASE WHEN priority=0 THEN json_extract(frames,'$[0].time') END) AS oldest "
            "FROM jobs WHERE delivered=0"
        ).fetchone()
        return {
            "pending_jobs": row["pending"],
            "failed_jobs": row["failed"],
            "pending_live_jobs": row["live"],
            "pending_background_jobs": row["pending"] - row["live"],
            "oldest_live_pending_seconds": (
                round(max(0.0, time.time() - seconds(row["oldest"])), 3)
                if row["oldest"]
                else None
            ),
        }

    def cached(self, image, model):
        row = self.db.execute(
            "SELECT predictions FROM predictions WHERE image=? AND model=?",
            (image, model),
        ).fetchone()
        if row:
            with self.db:
                self.db.execute(
                    "UPDATE predictions SET used=? WHERE image=? AND model=?",
                    (time.time(), image, model),
                )
            return json.loads(row[0])
        return None

    def cache(self, image, model, predictions, perceptual):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?)",
                (image, model, json.dumps(predictions), perceptual, time.time()),
            )
            self.db.execute(
                "DELETE FROM predictions WHERE rowid IN (SELECT rowid FROM predictions ORDER BY used DESC LIMIT -1 OFFSET ?)",
                (self.cache_limit,),
            )

    def similar(self, perceptual, model):
        # Candidate lookup only. Callers must still compare exact tensor identity.
        return [
            r[0]
            for r in self.db.execute(
                "SELECT image FROM predictions WHERE model=? AND perceptual=? LIMIT 16",
                (model, perceptual),
            )
        ]

    def complete(self, identifier, result):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET result=? WHERE id=?", (json.dumps(result), identifier)
            )
            progress = self.db.execute(
                "SELECT result FROM screening_progress WHERE job_id=?", (identifier,)
            ).fetchone()
            recheck = self.db.execute(
                "SELECT previous_interval FROM rechecks WHERE job_id=?", (identifier,)
            ).fetchone()
            previous = (
                json.loads(progress[0])["interval_id"]
                if progress
                else recheck[0] if recheck else None
            )
            if previous and previous != result["interval_id"]:
                payload = {
                    "replacement_interval_id": result["interval_id"],
                    "previous_interval_ids": [previous],
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO replacement_outbox(job_id,payload) VALUES (?,?)",
                    (identifier, json.dumps(payload)),
                )

    def acknowledge(self, identifier):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT result,attempts FROM jobs WHERE id=?", (identifier,)
            ).fetchone()
            result = json.loads(row["result"])
            unresolved = any(
                e.get("reason") in RETRY_REASONS for e in result.get("evidence", [])
            )
            delay = min(1800, 30 * (2 ** min(row["attempts"], 6))) if unresolved else 0
            self.db.execute(
                "INSERT OR REPLACE INTO screening_progress VALUES (?,?)",
                (identifier, row["result"]),
            )
            if unresolved:
                self.db.execute(
                    "UPDATE jobs SET result=NULL,attempts=attempts+1,retry_at=? WHERE id=?",
                    (time.time() + delay, identifier),
                )
            else:
                self.db.execute("UPDATE jobs SET delivered=1 WHERE id=?", (identifier,))
            self.db.execute(
                "UPDATE replacement_outbox SET ready=1 WHERE job_id=?", (identifier,)
            )
            self.db.execute(
                "DELETE FROM jobs WHERE delivered=1 AND id < ?", (identifier - 1000,)
            )
            self.db.execute(
                "DELETE FROM screening_progress WHERE job_id NOT IN (SELECT id FROM jobs)"
            )
        return delay

    def retry(self, identifier):
        with self.db:
            row = self.db.execute(
                "SELECT attempts FROM jobs WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                return 0
            # Persist the delay with the attempt count. Missing originals stay
            # held, but repeated failures must not monopolize capture screening.
            delay = min(1800, 30 * (2 ** min(row["attempts"], 6)))
            self.db.execute(
                "UPDATE jobs SET attempts=attempts+1,retry_at=? WHERE id=?",
                (time.time() + delay, identifier),
            )
        return delay


class ScreeningWorker:
    def __init__(self, config, state_dir, model_factory=None, inventory_reader=None):
        self.config, self.state_dir, self.model_factory = (
            config,
            state_dir,
            model_factory or (lambda: FreepikModel(device=config.privacy_device)),
        )
        self.inventory_reader = inventory_reader or read_display_tracks
        self.inventory_checked = 0.0
        self.inventory_available = False
        self.stop = threading.Event()
        self.started_at = time.monotonic()
        self.health = {
            "state": "starting",
            "cache_hits": 0,
            "inferences": 0,
            "errors": 0,
            "completed_jobs": 0,
            "retried_jobs": 0,
            "frame_read_total_ms": 0.0,
            "prepare_total_ms": 0.0,
            "delivery_total_ms": 0.0,
        }
        self.thread = threading.Thread(
            target=self.run, name="screen-privacy", daemon=True
        )

    def start(self):
        self.thread.start()

    def refresh_displays(self, store, client):
        if time.monotonic() - self.inventory_checked < 5:
            return self.inventory_available
        self.inventory_checked = time.monotonic()
        try:
            tracks = self.inventory_reader(self.config)
            observed_at = stamp(time.time())
            store.observe_displays(tracks, observed_at)
            for result in store.display_results():
                client.post(
                    "/api/device-input/screening/displays", json=result
                ).raise_for_status()
                store.acknowledge_displays(result["observed_at"])
        except Exception:
            self.inventory_available = False
            self.health.update(
                state="unavailable", last_failure="display_inventory_unavailable"
            )
            self.health["errors"] += 1
            return False
        self.inventory_available = True
        self.health.update(
            active_displays=len(tracks), inventory_checked_at=observed_at
        )
        return True

    def run(self):
        # The file-lock dependency is POSIX-only; other collector operations remain importable.
        import fcntl

        lock = (self.state_dir / "privacy-worker.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            self.health["state"] = "worker_already_running"
            return
        store = ScreeningStore(self.state_dir / "privacy.sqlite")
        model = None
        with httpx.Client(
            base_url=self.config.backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.config.token}"},
            timeout=30,
        ) as client:
            while not self.stop.is_set():
                try:
                    self.health.update(
                        **store.queue_health(),
                        pending_replacements=store.replacement_count(),
                    )
                    # Persist the historical hold before any queued result or
                    # model initialization can delay its delivery.
                    store.deliver_required_ranges(client)
                    self.deliver_replacement(store, client)
                    if model is None:
                        model = self.model_factory()
                        self.health["model_version"] = model.version
                    inventory_ready = self.refresh_displays(store, client)
                    self.health["state"] = (
                        "unavailable"
                        if not inventory_ready
                        else (
                            "needs_review"
                            if store.failed_count() or store.replacement_count()
                            else "ready"
                        )
                    )
                    for _ in range(100):
                        if self.stop.is_set():
                            break
                        inventory_ready = self.refresh_displays(store, client)
                        # Reconsider priority after each inference so a historical
                        # batch cannot delay newly arrived live captures.
                        # Stored frames can be screened while the recorder is off.
                        # Live eligibility still requires a current display inventory.
                        due = store.jobs(limit=1, historical_only=not inventory_ready)
                        if not due:
                            break
                        job = due[0]
                        job_started = time.monotonic()
                        try:
                            result = job["result"] or self.screen(job, store, model)
                            store.complete(job["id"], result)
                            delivery_started = time.monotonic()
                            try:
                                client.post(
                                    "/api/device-input/screening", json=result
                                ).raise_for_status()
                            finally:
                                self.record_duration("delivery", delivery_started)
                            delay = store.acknowledge(job["id"])
                            if delay:
                                self.health.update(
                                    state="needs_review",
                                    retry_delay_seconds=delay,
                                    last_failure=next(
                                        e["reason"]
                                        for e in result["evidence"]
                                        if e.get("reason") in RETRY_REASONS
                                    ),
                                )
                                self.health["retried_jobs"] += 1
                            self.deliver_replacement(store, client)
                            self.health["completed_jobs"] += 1
                            self.health["last_completed_at"] = result["ended_at"]
                        except Exception as exc:
                            # No URLs, response bodies, OCR, or pixels in logs.
                            self.health["errors"] += 1
                            self.health["state"] = (
                                "needs_review" if inventory_ready else "unavailable"
                            )
                            self.health["last_failure"] = (
                                "missing_frame"
                                if isinstance(exc, FileNotFoundError)
                                else "screening_or_delivery_failed"
                            )
                            self.health["retry_delay_seconds"] = store.retry(job["id"])
                            self.health["retried_jobs"] += 1
                        finally:
                            self.health["job_ms"] = round(
                                (time.monotonic() - job_started) * 1000, 2
                            )
                            self.health["uptime_seconds"] = round(
                                time.monotonic() - self.started_at, 2
                            )
                    self.health.update(store.queue_health())
                except Exception:
                    self.health["state"] = "unavailable"
                    self.health["errors"] += 1
                self.stop.wait(2 if model else 30)
        store.close()
        lock.close()

    def deliver_replacement(self, store, client):
        pending = store.replacement()
        if pending is None:
            return
        identifier, payload = pending
        try:
            client.post(
                "/api/device-input/screening/replace", json=payload
            ).raise_for_status()
            store.acknowledge_replacement(identifier)
        except Exception:
            # Leave the old hold and durable replacement request intact. An
            # unavailable replacement endpoint must not stop new capture checks.
            self.health["errors"] += 1
            self.health.update(
                state="needs_review", last_failure="replacement_delivery_failed"
            )
        self.health["pending_replacements"] = store.replacement_count()

    def record_duration(self, stage, started):
        elapsed = round((time.monotonic() - started) * 1000, 2)
        self.health[f"{stage}_ms"] = elapsed
        total = f"{stage}_total_ms"
        self.health[total] = round(self.health.get(total, 0.0) + elapsed, 2)

    def screen(self, job, store, model):
        frames = job["frames"]

        def predict(frame):
            started = time.monotonic()
            try:
                data = read_frame(self.config.screenpipe_dir, frame["id"])
            finally:
                self.record_duration("frame_read", started)
            started = time.monotonic()
            try:
                picture, exact, perceptual = model.prepare(data)
            finally:
                self.record_duration("prepare", started)
            prediction = store.cached(exact, model.version)
            if prediction is None:
                store.similar(
                    perceptual, model.version
                )  # approximate matches never clear content
                began = time.monotonic()
                prediction = model.predict(picture)
                self.health["inference_ms"] = round(
                    (time.monotonic() - began) * 1000, 2
                )
                store.cache(exact, model.version, prediction, perceptual)
                self.health["inferences"] += 1
            else:
                self.health["cache_hits"] += 1
            state, score = verdict(prediction)
            if frame["text_state"] == "excluded":
                state = "excluded"
            elif frame["text_state"] == "needs_review" and state == "allowed":
                state = "needs_review"
            return {
                "frame_id": frame["id"],
                "captured_at": frame["time"],
                "state": state,
                "score": score,
                "input_hash": exact,
                "reason": (
                    "adult_site_or_search"
                    if frame["suspicious"]
                    else "nsfw_content" if state != "allowed" else "none"
                ),
            }

        def classify(frame):
            try:
                return predict(frame)
            except Exception as exc:
                # Failure evidence has no invented model input or score. It is
                # delivered as a hold, never written into the prediction cache.
                self.health["errors"] += 1
                return {
                    "frame_id": frame["id"],
                    "captured_at": frame["time"],
                    "state": "pending",
                    "reason": (
                        "missing_frame"
                        if isinstance(exc, FileNotFoundError)
                        else "screening_failed"
                    ),
                }

        first = classify(frames[0])
        last = classify(frames[-1]) if len(frames) > 1 else first
        checked = [first, last] if len(frames) > 1 else [first]
        has_gap = any(
            seconds(b["time"]) - seconds(a["time"]) > 30
            for a, b in zip(frames, frames[1:])
        )
        if (
            first["state"] != "allowed"
            or last["state"] != "allowed"
            or has_gap
            or job.get("previous_result")
        ):
            checked = (
                [first]
                + [classify(f) for f in frames[1:-1]]
                + ([last] if len(frames) > 1 else [])
            )
        segments = []
        pairs = list(zip(checked, checked[1:])) or [(first, first)]
        for left, right in pairs:
            states = {left["state"], right["state"]}
            state = (
                "pending"
                if "pending" in states
                else (
                    "excluded"
                    if "excluded" in states
                    else "needs_review" if "needs_review" in states else "allowed"
                )
            )
            # The next capture after the 30-second deadline may put sampled
            # endpoints further apart even when intervening captures exist.
            # Only an actual gap in the capture sequence leaves time unverified.
            capture_gap = has_gap and (
                seconds(right["captured_at"]) - seconds(left["captured_at"]) > 30
            )
            if capture_gap:
                state = "pending"
            failures = {e.get("reason") for e in (left, right)} & RETRY_REASONS
            segments.append(
                {
                    "started_at": left["captured_at"],
                    "ended_at": stamp(
                        max(
                            seconds(right["captured_at"]),
                            seconds(left["captured_at"]) + 0.001,
                        )
                    ),
                    "state": state,
                    "coverage": (
                        "unverified"
                        if failures or capture_gap
                        else "verified" if len(checked) == len(frames) else "sampled"
                    ),
                    **(
                        {
                            "reason": (
                                "missing_frame"
                                if "missing_frame" in failures
                                else "screening_failed"
                            )
                        }
                        if failures
                        else {"reason": "capture_gap"} if capture_gap else {}
                    ),
                }
            )
        previous = job.get("previous_result")
        if (
            previous
            and previous["model_version"] == model.version
            and previous["policy_version"] == POLICY
            and previous["evidence"] == checked
            and previous["segments"] == segments
        ):
            return previous
        identity = hashlib.sha256(
            json.dumps(
                [
                    job["track"],
                    frames[0]["id"],
                    frames[-1]["id"],
                    model.version,
                    POLICY,
                    checked,
                    previous["interval_id"] if previous else None,
                ]
            ).encode()
        ).hexdigest()
        return {
            "interval_id": identity,
            "track_id": job["track"],
            "started_at": frames[0]["time"],
            "ended_at": segments[-1]["ended_at"],
            "model_version": model.version,
            "policy_version": POLICY,
            "segments": segments,
            "evidence": checked,
        }
