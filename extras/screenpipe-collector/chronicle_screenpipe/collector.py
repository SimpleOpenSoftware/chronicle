from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import sqlite3
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import chronicle_screenpipe.screening as screening
import httpx

from .meeting import MeetingTracker, RecorderMeetingLog, pipewire_capture_apps
from .observations import ObservationTracker, display_track_id, timestamp_seconds

logger = logging.getLogger(__name__)


def error_summary(error: Exception) -> str:
    """Describe a failure without echoing capture paths, URLs or response text."""
    kind = type(error).__name__
    if isinstance(error, httpx.HTTPStatusError):
        return f"{kind} (HTTP {error.response.status_code})"
    return kind


# SQLite's default compiled limit on host parameters in one statement is 999.
_SQLITE_PARAMETER_LIMIT = 500
_OBSERVATION_STATE_SCHEMA = 2


@dataclass(frozen=True)
class Config:
    backend_url: str
    source_id: str
    token: str
    screenpipe_dir: Path
    screenpipe_url: str = "http://127.0.0.1:3030"
    screenpipe_token: str | None = None
    forward_audio: str = "both"
    # Word-overlap threshold handed to ScreenPipe's `/search?dedupe=`, which
    # collapses consecutive near-identical frames before they cross the wire.
    # Matches the backend's own default so a recorder that predates the
    # parameter — it is ignored there — filters to the same result later.
    # Set to None to receive every frame.
    search_dedupe: float | None = 0.85
    poll_seconds: float = 5.0
    activity_debounce_seconds: float = 10.0
    sample_cooldown_seconds: float = 120.0
    liveness_seconds: float = 900.0
    # Detect active calls from the PipeWire graph and tag forwarded audio, so
    # the backend can bound sessions on real meeting intervals.
    meeting_detection: bool = True
    privacy_screening: bool = False
    privacy_device: str = "cpu"


class Checkpoints:
    def __init__(self, path: Path):
        self.path = path
        self.values: dict[str, int] = {}
        if path.exists():
            self.values = json.loads(path.read_text(encoding="utf-8"))

    def get(self, stream: str) -> int:
        return int(self.values.get(stream, 0))

    def set(self, stream: str, value: int) -> None:
        self.values[stream] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.values, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


@contextmanager
def open_screenpipe_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the live WAL database without ever requesting a write lock.

    A context manager rather than a bare connection because `sqlite3.Connection`
    is *itself* one — and its `__exit__` only ends a transaction, it does not
    close. Handing the raw connection to `with` therefore leaks two descriptors
    (the database and its WAL) per call, which the polling loop repeats until
    the process hits its descriptor limit and every subsequent operation,
    including the HTTP upload, fails with `Too many open files`.
    """
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        yield connection
    finally:
        connection.close()


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def infer_audio_direction(path: str) -> str:
    value = path.lower()
    if "(input)" in value or "input" in Path(value).name:
        return "input"
    if "(output)" in value or "output" in Path(value).name:
        return "output"
    return "unknown"


def infer_audio_track_id(path: str) -> str:
    """Return ScreenPipe's stable, provider-local audio device identity.

    Audio chunk filenames append a per-chunk suffix to the device name. The
    parenthesized input/output marker terminates the stable device portion, so it
    can distinguish two microphones without turning every chunk into a new lane.
    """

    stem = Path(path).stem
    match = re.match(r"^(.+?\((?:input|output)\))(?:[_-].*)?$", stem, re.IGNORECASE)
    if match:
        return match.group(1)
    return re.sub(r"_(?:\d+|\d{4}-\d{2}-\d{2}.*)$", "", stem) or stem


def iso_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value)
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return f"{text}Z"


def timestamp_seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, OSError, ZeroDivisionError):
        return 30.0


class Collector:
    def __init__(self, config: Config, state_dir: Path):
        self.config = config
        self.checkpoints = Checkpoints(state_dir / "checkpoints.json")
        self.observations_path = state_dir / "observations.json"
        self.meetings_path = state_dir / "meetings.json"
        self.rejections_path = state_dir / "rejections.jsonl"
        self._meeting_tracker: MeetingTracker | None = None
        self._recorder_meetings: RecorderMeetingLog | None = None
        self.metrics = {
            "observation_opens": 0,
            "observation_closes": 0,
            "observation_samples": 0,
        }
        self.client = httpx.Client(
            base_url=config.backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=60,
        )
        self.screening = None
        self.screening_store = None
        if config.privacy_screening:

            self.screening_store = screening.ScreeningStore(
                state_dir / "privacy.sqlite"
            )
            self.screening = screening.ScreeningWorker(config, state_dir)
            self.screening.start()

    @property
    def database_path(self) -> Path:
        return self.config.screenpipe_dir / "db.sqlite"

    def heartbeat(self, error: str | None = None) -> None:
        health = {
            "screenpipe_db": str(self.database_path),
            "audio_cursor": self.checkpoints.get("audio"),
            "frame_cursor": self.checkpoints.get("frames"),
            "active_meeting": (
                self._recorder_meetings.active_platform()
                if self._recorder_meetings
                else None
            )
            or (
                self._meeting_tracker.active_platform()
                if self._meeting_tracker
                else None
            ),
            **self.metrics,
        }
        if error:
            health["error"] = error
        if getattr(self, "screening", None):
            health["privacy_screening"] = dict(self.screening.health)
        response = self.client.post(
            "/api/device-input/heartbeat",
            json={"status": "error" if error else "online", "health": health},
        )
        response.raise_for_status()

    def collect_audio(self, connection: sqlite3.Connection) -> int:
        columns = table_columns(connection, "audio_chunks")
        required = {"id", "file_path", "timestamp"}
        if not required <= columns:
            # ScreenPipe may expose a migration placeholder briefly before the
            # first audio segment initializes the final schema.
            count = connection.execute("SELECT COUNT(*) FROM audio_chunks").fetchone()[
                0
            ]
            if count == 0:
                return 0
            raise RuntimeError(
                f"unsupported ScreenPipe audio_chunks schema; missing {sorted(required - columns)}"
            )
        cursor = self.checkpoints.get("audio")
        rows = connection.execute(
            "SELECT id, file_path, timestamp FROM audio_chunks WHERE id > ? AND timestamp IS NOT NULL ORDER BY id LIMIT 100",
            (cursor,),
        ).fetchall()
        sent = 0
        for row in rows:
            path = Path(row["file_path"])
            direction = infer_audio_direction(str(path))
            if self.config.forward_audio == "none" or (
                self.config.forward_audio != "both"
                and direction != self.config.forward_audio
            ):
                self.checkpoints.set("audio", row["id"])
                continue
            if not path.is_file():
                captured = timestamp_seconds(iso_timestamp(row["timestamp"]))
                if time.time() - captured < 120:
                    logger.warning("audio chunk %s is not available yet", row["id"])
                    break
                self.rejections_path.parent.mkdir(parents=True, exist_ok=True)
                with self.rejections_path.open("a", encoding="utf-8") as rejected:
                    rejected.write(
                        json.dumps(
                            {
                                "stream": "audio",
                                "source_item_id": row["id"],
                                "detail": "source media missing",
                            }
                        )
                        + "\n"
                    )
                self.checkpoints.set("audio", row["id"])
                continue
            before = path.stat()
            time.sleep(0.05)
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                break
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            content_type = mimetypes.guess_type(path.name)[0] or "audio/wav"
            captured_at = iso_timestamp(row["timestamp"])
            duration = audio_duration(path)
            data = {
                "source_item_id": str(row["id"]),
                "captured_at": captured_at,
                "duration_seconds": str(duration),
                "device_name": path.stem,
                "track_id": infer_audio_track_id(str(path)),
                "direction": direction,
                "content_hash": digest,
            }
            chunk_end = datetime.fromtimestamp(
                timestamp_seconds(captured_at) + duration, tz=timezone.utc
            ).isoformat()
            meeting_id = self._meeting_for(captured_at, chunk_end)
            if meeting_id:
                data["meeting_id"] = meeting_id
            with path.open("rb") as handle:
                response = self.client.post(
                    "/api/device-input/audio",
                    data=data,
                    files={"file": (path.name, handle, content_type)},
                )
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code >= 400:
                # Validation responses can echo submitted capture metadata. Keep
                # diagnostics content-free even before screening has completed.
                logger.error(
                    "audio chunk %s rejected with HTTP %s",
                    row["id"],
                    response.status_code,
                )
                self.rejections_path.parent.mkdir(parents=True, exist_ok=True)
                with self.rejections_path.open("a", encoding="utf-8") as rejected:
                    rejected.write(
                        json.dumps(
                            {
                                "stream": "audio",
                                "source_item_id": row["id"],
                                "status": response.status_code,
                                "detail": "backend rejected audio; checkpoint retained",
                            }
                        )
                        + "\n"
                    )
                # A 4xx can mean the collector and backend disagree about their
                # ingestion contract (for example, a newly required locator field).
                # Advancing here silently loses every rejected row because ScreenPipe
                # remains authoritative but this cursor will never visit it again.
                # Keep the checkpoint in place and surface an error heartbeat; an
                # operator can repair or explicitly quarantine a genuine poison row.
                raise RuntimeError(
                    f"audio chunk {row['id']} rejected with HTTP "
                    f"{response.status_code}; checkpoint retained"
                )
            self.checkpoints.set("audio", row["id"])
            sent += 1
        return sent

    def _load_meeting_state(self) -> tuple[MeetingTracker, RecorderMeetingLog]:
        state: dict[str, Any] = {}
        if self.meetings_path.exists():
            state = json.loads(self.meetings_path.read_text(encoding="utf-8"))
        return (
            MeetingTracker(
                state.get("tracker"), capture_source_id=self.config.source_id
            ),
            RecorderMeetingLog(
                state.get("recorder"), capture_source_id=self.config.source_id
            ),
        )

    def _save_meeting_state(
        self, tracker: MeetingTracker, recorder: RecorderMeetingLog
    ) -> None:
        self.meetings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.meetings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"tracker": tracker.state, "recorder": recorder.state}, sort_keys=True
            ),
            encoding="utf-8",
        )
        temporary.replace(self.meetings_path)

    def _current_context(self) -> dict[str, Any] | None:
        """The freshest observed context, for attributing a browser's mic."""
        observations = [
            observation
            for tracker in self._load_observation_trackers().values()
            for observation in (tracker.candidate, tracker.active)
            if observation is not None
        ]
        if not observations:
            return None
        return max(
            observations,
            key=lambda observation: timestamp_seconds(
                observation.get("ended_at") or observation["captured_at"]
            ),
        )

    def _meeting_for(self, start: str, end: str) -> str | None:
        """Recorder-written meetings take precedence over the live tracker."""
        for source in (self._recorder_meetings, self._meeting_tracker):
            if source is not None:
                meeting_id = source.meeting_for(start, end)
                if meeting_id:
                    return meeting_id
        return None

    def _recorder_meeting_rows(self, connection: sqlite3.Connection, horizon: str):
        """Recent rows of the recorder's own meetings table, if it has one."""
        columns = table_columns(connection, "meetings")
        if not {"id", "meeting_start", "meeting_end", "meeting_app"} <= columns:
            return []

        def optional(name: str) -> str:
            return name if name in columns else f"NULL AS {name}"

        return connection.execute(
            f"SELECT id, meeting_start, meeting_end, meeting_app, {optional('title')} "
            "FROM meetings WHERE meeting_start >= ? ORDER BY id",
            (horizon,),
        ).fetchall()

    def collect_meetings(self, connection: sqlite3.Connection) -> int:
        """Advance meeting detection one poll; emit boundaries as observations.

        The recorder's persisted meetings (macOS/Windows watcher) are the
        preferred source — they survive collector downtime, so backfill keeps
        its bounds. The PipeWire tracker only runs where the recorder is not
        writing meetings (Linux, or detector disabled).
        """
        if not self.config.meeting_detection:
            return 0
        tracker, recorder = self._load_meeting_state()
        now = datetime.now(timezone.utc).isoformat()
        horizon = datetime.fromtimestamp(
            timestamp_seconds(now) - recorder.retain_seconds, tz=timezone.utc
        ).isoformat()
        events = recorder.sync(self._recorder_meeting_rows(connection, horizon), now)
        if recorder.owns_detection(now):
            events.extend(tracker.retire())
        else:
            events.extend(
                tracker.tick(pipewire_capture_apps(), self._current_context(), now)
            )
        sent = self._send_observation_events(events)
        self._save_meeting_state(tracker, recorder)
        self._meeting_tracker = tracker
        self._recorder_meetings = recorder
        return sent

    def _load_observation_trackers(self) -> dict[str, ObservationTracker]:
        state: dict[str, Any] = {
            "schema": _OBSERVATION_STATE_SCHEMA,
            "tracks": {},
        }
        if self.observations_path.exists():
            state = json.loads(self.observations_path.read_text(encoding="utf-8"))
        if state.get("schema") != _OBSERVATION_STATE_SCHEMA:
            raise ValueError("unsupported display-local observation state schema")
        return {
            track_id: ObservationTracker(
                track_state,
                stability_seconds=self.config.activity_debounce_seconds,
                sample_cooldown_seconds=self.config.sample_cooldown_seconds,
                liveness_seconds=self.config.liveness_seconds,
                capture_source_id=self.config.source_id,
                track_id=track_id,
            )
            for track_id, track_state in state.get("tracks", {}).items()
        }

    def _new_observation_tracker(self, track_id: str) -> ObservationTracker:
        return ObservationTracker(
            stability_seconds=self.config.activity_debounce_seconds,
            sample_cooldown_seconds=self.config.sample_cooldown_seconds,
            liveness_seconds=self.config.liveness_seconds,
            capture_source_id=self.config.source_id,
            track_id=track_id,
        )

    def _save_observation_trackers(
        self, trackers: dict[str, ObservationTracker]
    ) -> None:
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.observations_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": _OBSERVATION_STATE_SCHEMA,
                    "tracks": {
                        track_id: tracker.state
                        for track_id, tracker in sorted(trackers.items())
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.observations_path)

    def _send_observation_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        response = self.client.post(
            "/api/device-input/observations", json={"events": events}
        )
        response.raise_for_status()
        for event in events:
            key = {
                "open": "observation_opens",
                "close": "observation_closes",
                "sample": "observation_samples",
            }[event["event"]]
            self.metrics[key] += 1
        return len(events)

    def collect_observations(self, connection: sqlite3.Connection) -> int:
        columns = table_columns(connection, "frames")
        required = {"id", "timestamp", "app_name", "window_name"}
        if not required <= columns:
            raise RuntimeError(
                f"unsupported ScreenPipe frames schema; missing {sorted(required - columns)}"
            )

        def optional(name: str) -> str:
            return name if name in columns else f"NULL AS {name}"

        cursor = self.checkpoints.get("frames")
        rows = connection.execute(
            f"SELECT id, timestamp, app_name, window_name, {optional('browser_url')}, "
            f"{optional('capture_trigger')}, {optional('full_text')}, {optional('text_source')}, "
            f"{optional('device_name')} "
            "FROM frames WHERE id > ? ORDER BY id LIMIT 1000",
            (cursor,),
        ).fetchall()
        trackers = self._load_observation_trackers()
        now = datetime.now(timezone.utc).isoformat()
        events: list[dict[str, Any]] = []
        for row in rows:
            if getattr(self, "screening_store", None):
                self.screening_store.observe(row)
            track_id = display_track_id(row)
            tracker = trackers.setdefault(
                track_id, self._new_observation_tracker(track_id)
            )
            events.extend(tracker.process_rows([row], iso_timestamp(row["timestamp"])))
        for tracker in trackers.values():
            events.extend(tracker.process_rows([], now))
        sent = self._send_observation_events(events)
        self._save_observation_trackers(trackers)
        if rows:
            self.checkpoints.set("frames", rows[-1]["id"])
        return sent

    def close_observation(self) -> int:
        trackers = self._load_observation_trackers()
        now = datetime.now(timezone.utc).isoformat()
        events = [
            event for tracker in trackers.values() for event in tracker.close(now)
        ]
        sent = self._send_observation_events(events)
        self._save_observation_trackers(trackers)
        return sent

    def _attach_capture_triggers(self, items: list[dict[str, Any]]) -> None:
        """Add ScreenPipe's own reason for capturing each frame.

        `/search` does not expose `capture_trigger`, so it comes from the local
        database instead. It tells Chronicle which frames were explicit capture
        events — a manual grab or a window focus — rather than incidental
        samples, so those are never folded into a neighbouring frame. Best
        effort: a frame with no trigger is simply left unlabelled.
        """
        by_frame = {
            item["metadata"]["frame_id"]: item
            for item in items
            if item["metadata"].get("frame_id") is not None
        }
        if not by_frame:
            return
        frame_ids = list(by_frame)
        try:
            with open_screenpipe_db(self.database_path) as connection:
                for start in range(0, len(frame_ids), _SQLITE_PARAMETER_LIMIT):
                    batch = frame_ids[start : start + _SQLITE_PARAMETER_LIMIT]
                    placeholders = ",".join("?" * len(batch))
                    rows = connection.execute(
                        f"SELECT id, capture_trigger FROM frames WHERE id IN ({placeholders})",
                        batch,
                    ).fetchall()
                    for row in rows:
                        if row["capture_trigger"]:
                            by_frame[row["id"]]["metadata"]["capture_trigger"] = row[
                                "capture_trigger"
                            ]
        except sqlite3.Error as exc:
            logger.warning("could not read capture triggers: %s", error_summary(exc))

    def _frames_across_interval(self, start: str, end: str, count: int) -> list[int]:
        """Pick `count` frame ids spread evenly across [start, end).

        ScreenPipe holds every frame, so an interval can be sampled directly instead of
        being limited to whatever was shortlisted per observation while it was open. One
        narrow query per slice keeps this bounded — a 30-minute gaming session holds
        ~500 frames, and enumerating them to pick six would be pure waste.
        """

        # Job timestamps arrive naive (Mongo stores UTC and drops the suffix), and a
        # naive string read by `timestamp_seconds` would be interpreted in the node's
        # local zone — silently shifting every slice by the UTC offset and querying a
        # stretch of the day the episode never covered.
        begin = timestamp_seconds(iso_timestamp(start))
        finish = timestamp_seconds(iso_timestamp(end))
        if finish <= begin:
            return []
        width = (finish - begin) / count
        headers = (
            {"Authorization": f"Bearer {self.config.screenpipe_token}"}
            if self.config.screenpipe_token
            else None
        )
        frame_ids: list[int] = []
        for index in range(count):
            slice_start = datetime.fromtimestamp(begin + index * width, tz=timezone.utc)
            slice_end = datetime.fromtimestamp(
                begin + (index + 1) * width, tz=timezone.utc
            )
            try:
                found = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/search",
                    params={
                        "content_type": "ocr",
                        "start_time": slice_start.isoformat().replace("+00:00", "Z"),
                        "end_time": slice_end.isoformat().replace("+00:00", "Z"),
                        "limit": 1,
                    },
                    headers=headers,
                    timeout=30,
                )
                found.raise_for_status()
                rows = found.json().get("data") or []
            except (httpx.HTTPError, ValueError):
                logger.info("no frame resolvable for slice %s", index)
                continue
            for row in rows:
                frame_id = (row.get("content") or {}).get("frame_id")
                # A slice that captured nothing simply contributes no frame; the
                # shortlist is allowed to be shorter than asked for.
                if isinstance(frame_id, int) and frame_id not in frame_ids:
                    frame_ids.append(frame_id)
        return frame_ids

    def _serve_preview_batch(self, job: dict) -> bool:
        """Fetch every shortlisted frame of one observation and upload them together.

        Frames are served best-effort: ScreenPipe prunes its store, so a missing frame
        is expected and must not fail the batch. Chronicle only needs a "good enough"
        spread to choose from, and the backend records which ids could not be served.
        """

        payload = job.get("payload", {})
        width = int(payload.get("width") or 640)
        headers = (
            {"Authorization": f"Bearer {self.config.screenpipe_token}"}
            if self.config.screenpipe_token
            else None
        )
        # Explicit ids name an observation's own frames; an interval means "sample this
        # stretch of the day", which only the node can resolve.
        frame_ids = payload.get("frame_ids") or self._frames_across_interval(
            job["start_at"], job["end_at"], int(payload.get("count") or 6)
        )
        files = []
        for frame_id in frame_ids:
            try:
                frame = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/frames/{frame_id}/thumbnail",
                    params={"width": width, "quality": 75},
                    headers=headers,
                    timeout=30,
                )
                frame.raise_for_status()
            except httpx.HTTPError:
                logger.info("frame %s is no longer available", frame_id)
                continue
            files.append(
                (
                    "files",
                    (
                        f"frame-{frame_id}.jpg",
                        frame.content,
                        frame.headers.get("content-type", "image/jpeg"),
                    ),
                )
            )
        if not files:
            raise RuntimeError("no shortlisted frame could be served")
        done = self.client.post(
            f"/api/device-input/jobs/{job['id']}/previews", files=files
        )
        done.raise_for_status()
        return True

    def process_job(self) -> bool:
        response = self.client.get("/api/device-input/jobs/next")
        response.raise_for_status()
        job = response.json().get("job")
        if not job:
            return False
        try:
            if job["kind"] in {"thumbnail", "source_media"}:
                payload = job.get("payload", {})
                if payload.get("frame_ids") or payload.get("count"):
                    return self._serve_preview_batch(job)
                frame_id = payload.get("frame_id")
                if frame_id is None:
                    raise RuntimeError("thumbnail job is missing frame_id")
                headers = (
                    {"Authorization": f"Bearer {self.config.screenpipe_token}"}
                    if self.config.screenpipe_token
                    else None
                )
                width = int(job.get("payload", {}).get("width") or 640)
                quality = 85 if width > 640 else 75
                thumbnail = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/frames/{frame_id}/thumbnail",
                    params={"width": width, "quality": quality},
                    headers=headers,
                    timeout=30,
                )
                thumbnail.raise_for_status()
                done = self.client.post(
                    f"/api/device-input/jobs/{job['id']}/thumbnail",
                    files={
                        "file": (
                            f"screenpipe-frame-{frame_id}.jpg",
                            thumbnail.content,
                            thumbnail.headers.get("content-type", "image/jpeg"),
                        )
                    },
                )
                done.raise_for_status()
                return True
            raw_items = []
            offset = 0
            page_size = 500
            collapsed = 0
            while True:
                params = {
                    "content_type": "ocr",
                    "start_time": (
                        iso_timestamp(job["start_at"]) if job.get("start_at") else None
                    ),
                    "end_time": (
                        iso_timestamp(job["end_at"]) if job.get("end_at") else None
                    ),
                    "limit": page_size,
                    "offset": offset,
                }
                if self.config.search_dedupe:
                    params["dedupe"] = self.config.search_dedupe
                headers = (
                    {"Authorization": f"Bearer {self.config.screenpipe_token}"}
                    if self.config.screenpipe_token
                    else None
                )
                local = httpx.get(
                    f"{self.config.screenpipe_url.rstrip('/')}/search",
                    params=params,
                    headers=headers,
                    timeout=60,
                )
                local.raise_for_status()
                body = local.json()
                page = body.get("data", [])
                # `limit` bounds rows scanned, not returned, so a deduplicated
                # page is short of `page_size` while more data remains. Page on
                # rows *scanned* instead. A recorder without the parameter
                # reports no `deduped`, which reduces this to the plain
                # short-page test.
                deduped = int(body.get("deduped") or 0)
                collapsed += deduped
                scanned = len(page) + deduped
                raw_items.extend(page)
                if scanned < page_size:
                    break
                offset += scanned
            if collapsed:
                logger.info(
                    "screenpipe collapsed %d near-duplicate frames for job %s",
                    collapsed,
                    job["id"],
                )
            items = []
            requested_locator = job.get("payload", {}).get("locator")
            for raw in raw_items:
                content = raw.get("content", raw)
                frame_id = content.get("frame_id")
                if frame_id is None:
                    continue
                track_id = (
                    (requested_locator or {}).get("track_id")
                    or content.get("device_name")
                    or content.get("display_id")
                )
                if not track_id:
                    logger.warning(
                        "screenpipe frame %s has no display track; omitting it from job %s",
                        frame_id,
                        job["id"],
                    )
                    continue
                items.append(
                    {
                        "source_item_id": f"frame:{frame_id}",
                        "locator": {
                            "capture_source_id": self.config.source_id,
                            "modality": "screen",
                            "track_id": str(track_id),
                        },
                        "captured_at": content.get("timestamp"),
                        "metadata": {
                            "frame_id": frame_id,
                            "app_name": content.get("app_name"),
                            "window_name": content.get("window_name"),
                            "browser_url": content.get("browser_url"),
                            "text": content.get("text"),
                            # How ScreenPipe read this frame. Chronicle needs it to
                            # tell a dense accessibility-tree read from the OCR
                            # fallback used where a window exposes no tree.
                            "text_source": content.get("text_source"),
                        },
                    }
                )
            self._attach_capture_triggers(items)
            result = {"success": True, "items": items}
        except Exception as exc:
            result = {"success": False, "items": [], "error": error_summary(exc)}
        done = self.client.post(
            f"/api/device-input/jobs/{job['id']}/complete", json=result
        )
        done.raise_for_status()
        return True

    def run(self) -> None:
        last_heartbeat = 0.0
        while True:
            try:
                with open_screenpipe_db(self.database_path) as connection:
                    self.collect_meetings(connection)
                    self.collect_observations(connection)
                    self.collect_audio(connection)
                self.process_job()
                if time.monotonic() - last_heartbeat >= 30:
                    self.heartbeat()
                    last_heartbeat = time.monotonic()
            except KeyboardInterrupt:
                try:
                    self.close_observation()
                except Exception as exc:
                    logger.error(
                        "failed to close observation during shutdown: %s",
                        error_summary(exc),
                    )
                return
            except Exception as exc:
                logger.error("collector pass failed: %s", error_summary(exc))
                try:
                    self.heartbeat(error_summary(exc))
                except Exception as heartbeat_error:
                    logger.error("heartbeat failed: %s", error_summary(heartbeat_error))
            try:
                time.sleep(self.config.poll_seconds)
            except KeyboardInterrupt:
                try:
                    self.close_observation()
                except Exception as exc:
                    logger.error(
                        "failed to close observation during shutdown: %s",
                        error_summary(exc),
                    )
                return
