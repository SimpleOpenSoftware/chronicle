import json
import sqlite3
import time
import wave
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from chronicle_screenpipe import collector as collector_module
from chronicle_screenpipe.collector import (
    Checkpoints,
    Collector,
    Config,
    audio_duration,
    infer_audio_direction,
    infer_audio_track_id,
)
from chronicle_screenpipe.meeting import CaptureApp, MeetingTracker, RecorderMeetingLog


def test_run_diagnostics_do_not_forward_capture_exception_text(
    tmp_path, monkeypatch, caplog
):
    collector = Collector(
        Config(
            backend_url="http://backend",
            source_id="synthetic-source",
            token="synthetic-token",
            screenpipe_dir=tmp_path,
            privacy_screening=False,
            poll_seconds=0,
        ),
        tmp_path / "state",
    )
    posted = []

    def heartbeat_post(_url, json):
        posted.append(json)
        raise RuntimeError("PRIVATE_HEARTBEAT_SENTINEL")

    collector.client.close()
    collector.client = SimpleNamespace(post=heartbeat_post)
    monkeypatch.setattr(
        collector_module, "open_screenpipe_db", lambda _: nullcontext(object())
    )
    monkeypatch.setattr(collector, "collect_meetings", lambda _: None)
    monkeypatch.setattr(collector, "collect_observations", lambda _: None)

    def fail_capture(_connection):
        raise PermissionError(13, "denied", "/synthetic/PRIVATE_CAPTURE_SENTINEL.wav")

    def fail_shutdown():
        raise ValueError("PRIVATE_SHUTDOWN_SENTINEL")

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(collector, "collect_audio", fail_capture)
    monkeypatch.setattr(collector, "close_observation", fail_shutdown)
    monkeypatch.setattr(collector_module.time, "sleep", stop)
    collector.run()

    assert len(posted) == 1
    assert posted[0]["status"] == "error"
    diagnostics = caplog.text + json.dumps(posted)
    assert "PRIVATE_" not in diagnostics
    assert posted[0]["health"]["error"] == "PermissionError"
    assert "RuntimeError" in caplog.text
    assert "ValueError" in caplog.text


def test_preview_job_failure_does_not_forward_exception_message_or_url(
    tmp_path, monkeypatch
):
    collector = Collector(
        Config(
            backend_url="http://backend",
            source_id="synthetic-source",
            token="synthetic-token",
            screenpipe_dir=tmp_path,
            privacy_screening=False,
        ),
        tmp_path / "state",
    )
    job = {"id": "synthetic-job", "kind": "source_media", "payload": {"frame_ids": [1]}}
    posted = []
    collector.client.close()
    collector.client = SimpleNamespace(
        get=lambda _: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"job": job}
        ),
        post=lambda _url, json: (
            posted.append(json) or SimpleNamespace(raise_for_status=lambda: None)
        ),
    )

    def fail_preview(_job):
        request = httpx.Request(
            "GET", "http://recorder.invalid/?query=PRIVATE_URL_SENTINEL"
        )
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError(
            "PRIVATE_BODY_SENTINEL", request=request, response=response
        )

    monkeypatch.setattr(collector, "_serve_preview_batch", fail_preview)
    assert collector.process_job()
    assert posted == [
        {"success": False, "items": [], "error": "HTTPStatusError (HTTP 503)"}
    ]


def test_checkpoints_are_atomic(tmp_path: Path):
    checkpoints = Checkpoints(tmp_path / "state.json")
    checkpoints.set("audio", 42)
    assert Checkpoints(tmp_path / "state.json").get("audio") == 42


def test_audio_direction_from_screenpipe_filename():
    assert infer_audio_direction("device (input)_2026.wav") == "input"
    assert infer_audio_direction("device (output)_2026.wav") == "output"


def test_audio_track_id_keeps_device_identity_without_chunk_suffix():
    assert infer_audio_track_id("Desk Mic (input)_2026-09-03_10-00.wav") == (
        "Desk Mic (input)"
    )
    assert infer_audio_track_id("Desk Mic (input)_2026-09-03_10-01.wav") == (
        "Desk Mic (input)"
    )
    assert infer_audio_track_id("Headset Mic (input)_42.wav") == ("Headset Mic (input)")


def test_wav_duration_is_read_from_media(tmp_path: Path):
    target = tmp_path / "sample.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 8000)
    assert audio_duration(target) == 0.5


def test_collect_audio_accepts_screenpipe_startup_schema():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (placeholder TEXT)")
    collector = object.__new__(Collector)
    assert collector.collect_audio(db) == 0


def test_collect_audio_checkpoints_sources_excluded_from_forwarding(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (id INTEGER, file_path TEXT, timestamp TEXT)")
    db.execute(
        "INSERT INTO audio_chunks VALUES (1, ?, ?)",
        (str(tmp_path / "Microphone (input)_1.wav"), "2026-07-22T10:00:00Z"),
    )
    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://chronicle",
        source_id="rainbow",
        token="token",
        screenpipe_dir=tmp_path,
        forward_audio="output",
    )
    collector.checkpoints = Checkpoints(tmp_path / "state.json")

    assert collector.collect_audio(db) == 0
    assert collector.checkpoints.get("audio") == 1


def test_collect_audio_tags_chunks_with_the_active_meeting(tmp_path: Path):
    chunk = tmp_path / "Microphone (input)_1.wav"
    with wave.open(str(chunk), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (id INTEGER, file_path TEXT, timestamp TEXT)")
    db.execute(
        "INSERT INTO audio_chunks VALUES (1, ?, ?)",
        (str(chunk), "2026-07-22T10:00:30Z"),
    )

    tracker = MeetingTracker()
    zoom = [CaptureApp(name="Zoom", binary="zoom")]
    tracker.tick(zoom, None, "2026-07-22T10:00:00+00:00")
    tracker.tick(zoom, None, "2026-07-22T10:00:05+00:00")

    posted: dict = {}

    class FakeClient:
        def post(self, url, data=None, files=None):
            posted.update(data or {})
            return SimpleNamespace(status_code=200)

    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=tmp_path,
    )
    collector.checkpoints = Checkpoints(tmp_path / "state.json")
    collector._meeting_tracker = tracker
    collector._recorder_meetings = None
    collector.client = FakeClient()

    assert collector.collect_audio(db) == 1
    assert posted["meeting_id"] == tracker.meeting["meeting_id"]
    assert posted["track_id"] == "Microphone (input)"


def test_collect_audio_keeps_checkpoint_when_backend_rejects_contract(
    tmp_path: Path, caplog
):
    chunk = tmp_path / "Microphone (input)_1.wav"
    with wave.open(str(chunk), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (id INTEGER, file_path TEXT, timestamp TEXT)")
    db.execute(
        "INSERT INTO audio_chunks VALUES (1, ?, ?)",
        (str(chunk), "2026-07-22T10:00:30Z"),
    )

    class RejectingClient:
        def post(self, _url, data=None, files=None):
            return SimpleNamespace(
                status_code=422,
                text='{"detail": "rejected input: PRIVATE_CAPTURE_SENTINEL"}',
            )

    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=tmp_path,
    )
    collector.checkpoints = Checkpoints(tmp_path / "state.json")
    collector.rejections_path = tmp_path / "rejections.jsonl"
    collector._meeting_tracker = None
    collector._recorder_meetings = None
    collector.client = RejectingClient()

    with pytest.raises(RuntimeError, match="checkpoint retained") as failure:
        collector.collect_audio(db)

    assert collector.checkpoints.get("audio") == 0
    rejection = json.loads(collector.rejections_path.read_text().splitlines()[0])
    assert rejection["source_item_id"] == 1
    assert rejection["status"] == 422
    assert rejection["detail"] == "backend rejected audio; checkpoint retained"
    assert "HTTP 422" in caplog.text
    assert "PRIVATE_CAPTURE_SENTINEL" not in (
        caplog.text + collector.rejections_path.read_text() + str(failure.value)
    )


def test_collect_meetings_reads_the_recorders_meetings_table(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, meeting_start TEXT, "
        "meeting_end TEXT, meeting_app TEXT, title TEXT)"
    )
    start = datetime.now(timezone.utc) - timedelta(minutes=45)
    end = start + timedelta(minutes=40)
    db.execute(
        "INSERT INTO meetings VALUES (5, ?, ?, 'Google Meet', 'Standup')",
        (start.isoformat(), end.isoformat()),
    )

    sent_events = []

    class FakeClient:
        def post(self, url, json=None, **kwargs):
            sent_events.extend(json["events"])

            class Done:
                def raise_for_status(self):
                    return None

            return Done()

    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=tmp_path,
    )
    collector.meetings_path = tmp_path / "meetings.json"
    collector.observations_path = tmp_path / "observations.json"
    collector.metrics = {
        "observation_opens": 0,
        "observation_closes": 0,
        "observation_samples": 0,
    }
    collector._meeting_tracker = None
    collector._recorder_meetings = None
    collector.client = FakeClient()

    assert collector.collect_meetings(db) == 2
    assert [event["event"] for event in sent_events] == ["open", "close"]
    assert {event["locator"]["capture_source_id"] for event in sent_events} == {
        "source-1"
    }
    assert {event["locator"]["modality"] for event in sent_events} == {"context"}
    assert {event["locator"]["track_id"] for event in sent_events} == {
        "meeting-detector"
    }
    assert all(
        event["metadata"]["locator"] == event["locator"] for event in sent_events
    )
    assert sent_events[0]["metadata"]["title"] == "Standup"
    # The recorder owns detection now, so no PipeWire sensing is attempted
    # and a second pass re-sends nothing.
    assert collector.collect_meetings(db) == 0
    chunk = start + timedelta(minutes=5)
    assert (
        collector._meeting_for(
            chunk.isoformat(), (chunk + timedelta(seconds=30)).isoformat()
        )
        == "meeting:recorder:5"
    )


def test_collect_meetings_uses_freshest_display_context_for_pipewire(
    tmp_path: Path, monkeypatch
):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    sent_events = []

    class FakeClient:
        def post(self, _url, json=None):
            sent_events.extend(json["events"])
            return _Response({})

    old_context = {
        "captured_at": "2026-07-22T10:00:00Z",
        "ended_at": "2026-07-22T10:00:01Z",
        "browser_url": "https://example.com",
    }
    fresh_context = {
        "captured_at": "2026-07-22T10:00:02Z",
        "ended_at": "2026-07-22T10:00:03Z",
        "browser_url": "https://meet.google.com/abc-defg-hij",
    }
    (tmp_path / "observations.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "tracks": {
                    "Display 1": {
                        "schema": 1,
                        "active": old_context,
                        "candidate": None,
                    },
                    "Display 2": {
                        "schema": 1,
                        "active": fresh_context,
                        "candidate": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        collector_module,
        "pipewire_capture_apps",
        lambda: [CaptureApp(name="Chromium", binary="chromium")],
    )
    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=tmp_path,
    )
    collector.meetings_path = tmp_path / "meetings.json"
    collector.observations_path = tmp_path / "observations.json"
    collector.metrics = {
        "observation_opens": 0,
        "observation_closes": 0,
        "observation_samples": 0,
    }
    collector._meeting_tracker = None
    collector._recorder_meetings = None
    collector.client = FakeClient()

    assert collector.collect_meetings(db) == 0
    assert collector.collect_meetings(db) == 1
    assert sent_events[0]["metadata"]["platform"] == "google-meet"


def test_recorder_meetings_take_precedence_over_the_live_tracker():
    tracker = MeetingTracker()
    zoom = [CaptureApp(name="Zoom", binary="zoom")]
    tracker.tick(zoom, None, "2026-07-22T10:00:00+00:00")
    tracker.tick(zoom, None, "2026-07-22T10:00:05+00:00")
    recorder = RecorderMeetingLog()
    recorder.sync(
        [
            {
                "id": 4,
                "meeting_start": "2026-07-22T10:00:00Z",
                "meeting_end": None,
                "meeting_app": "Zoom",
                "title": None,
            }
        ],
        "2026-07-22T10:00:05+00:00",
    )

    collector = object.__new__(Collector)
    collector._meeting_tracker = tracker
    collector._recorder_meetings = recorder

    assert (
        collector._meeting_for("2026-07-22T10:01:00+00:00", "2026-07-22T10:01:30+00:00")
        == "meeting:recorder:4"
    )


def _collector_over(database: Path) -> Collector:
    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=database.parent,
    )
    return collector


def _frames_database(tmp_path: Path, rows: list[tuple[int, str | None]]) -> Path:
    database = tmp_path / "db.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE frames (id INTEGER PRIMARY KEY, capture_trigger TEXT)"
    )
    connection.executemany(
        "INSERT INTO frames (id, capture_trigger) VALUES (?, ?)", rows
    )
    connection.commit()
    connection.close()
    return database


def test_capture_triggers_are_attached_from_the_local_database(tmp_path: Path):
    """`/search` omits capture_trigger, so it is read from ScreenPipe's own DB."""
    database = _frames_database(tmp_path, [(7, "manual"), (8, "visual_change")])
    items = [
        {"source_item_id": "frame:7", "metadata": {"frame_id": 7}},
        {"source_item_id": "frame:8", "metadata": {"frame_id": 8}},
    ]

    _collector_over(database)._attach_capture_triggers(items)

    assert items[0]["metadata"]["capture_trigger"] == "manual"
    assert items[1]["metadata"]["capture_trigger"] == "visual_change"


def test_frames_without_a_trigger_are_left_unlabelled(tmp_path: Path):
    database = _frames_database(tmp_path, [(7, None)])
    items = [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]

    _collector_over(database)._attach_capture_triggers(items)

    assert "capture_trigger" not in items[0]["metadata"]


def test_an_unreadable_database_does_not_fail_the_job(tmp_path: Path):
    """Triggers are an enrichment; losing them must not lose the context items."""
    items = [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]

    _collector_over(tmp_path / "missing.sqlite")._attach_capture_triggers(items)

    assert items == [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_paging_counts_rows_scanned_not_rows_returned(monkeypatch):
    """A deduplicated page is short of the limit while data remains.

    `limit` bounds rows ScreenPipe *scans*, so paging on `len(page)` would stop
    at the first page the recorder collapsed anything out of.
    """

    pages = [
        {"data": [{"content": {"frame_id": i}} for i in range(300)], "deduped": 200},
        {"data": [{"content": {"frame_id": 900}}], "deduped": 0},
    ]
    seen_offsets = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_offsets.append(params["offset"])
        return _Response(pages[len(seen_offsets) - 1])

    monkeypatch.setattr(collector_module.httpx, "get", fake_get)

    collected = []
    offset = 0
    page_size = 500
    while True:
        body = fake_get("", params={"offset": offset})._payload
        page = body["data"]
        scanned = len(page) + int(body.get("deduped") or 0)
        collected.extend(page)
        if scanned < page_size:
            break
        offset += scanned

    assert seen_offsets == [0, 500]
    assert len(collected) == 301


def test_dedupe_is_requested_by_default():
    config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=Path("/tmp"),
    )

    assert config.search_dedupe == 0.85


def test_dedupe_can_be_turned_off():
    config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=Path("/tmp"),
        search_dedupe=None,
    )

    assert config.search_dedupe is None


def test_interval_frames_are_sampled_evenly_across_the_span(monkeypatch):
    """An episode samples its own interval, not an observation's shortlist.

    ScreenPipe holds every frame — ~500 for a half-hour session — so one narrow query
    per slice returns a spread without enumerating them all.
    """

    queries = []

    class _Response:
        def __init__(self, frame_id):
            self._frame_id = frame_id

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"content": {"frame_id": self._frame_id}}]}

    def fake_get(url, params=None, headers=None, timeout=None):
        queries.append((params["start_time"], params["end_time"]))
        assert params["limit"] == 1
        return _Response(1000 + len(queries))

    monkeypatch.setattr(collector_module.httpx, "get", fake_get)
    collector = Collector.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="screenpipe-1",
        token="t",
        screenpipe_dir=Path("/tmp"),
    )

    frames = collector._frames_across_interval(
        "2026-08-06T17:00:00Z", "2026-08-06T18:00:00Z", 6
    )

    assert frames == [1001, 1002, 1003, 1004, 1005, 1006]
    assert len(queries) == 6
    # Contiguous 10-minute slices covering the whole hour, in order.
    assert queries[0][0].startswith("2026-08-06T17:00")
    assert queries[-1][1].startswith("2026-08-06T18:00")
    assert all(queries[i][1] == queries[i + 1][0] for i in range(5))


def test_a_naive_job_interval_is_read_as_utc_not_node_local_time(monkeypatch):
    """Jobs deliver naive timestamps, and the node's own zone must not shift them.

    Mongo stores UTC and drops the suffix, so `start_at` arrives as
    "2026-08-06T17:00:00". Parsing that without normalizing makes `.timestamp()`
    interpret it in the node's local zone — on an IST node every slice moved 5h30m
    earlier and queried a stretch of the day the episode never covered, so a
    55-minute gaming session resolved zero frames.
    """

    queries = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        queries.append(params["start_time"])
        return _Response()

    monkeypatch.setattr(collector_module.httpx, "get", fake_get)
    # A zone far from UTC, so a local-time reading cannot coincidentally pass.
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        collector = Collector.__new__(Collector)
        collector.config = Config(
            backend_url="http://backend",
            source_id="screenpipe-1",
            token="t",
            screenpipe_dir=Path("/tmp"),
        )

        collector._frames_across_interval(
            "2026-08-06T17:00:00", "2026-08-06T18:00:00", 6
        )
    finally:
        monkeypatch.undo()
        time.tzset()

    assert queries[0].startswith("2026-08-06T17:00")


def test_an_interval_with_no_frames_yields_an_empty_shortlist(monkeypatch):
    """A stretch the recorder never covered contributes nothing, and must not raise."""

    def fake_get(url, params=None, headers=None, timeout=None):
        raise collector_module.httpx.HTTPError("no rows")

    monkeypatch.setattr(collector_module.httpx, "get", fake_get)
    collector = Collector.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="screenpipe-1",
        token="t",
        screenpipe_dir=Path("/tmp"),
    )

    assert (
        collector._frames_across_interval(
            "2026-08-06T17:00:00Z", "2026-08-06T18:00:00Z", 6
        )
        == []
    )


@pytest.mark.parametrize("screening", [False, True])
def test_collect_observations_keeps_interleaved_displays_independent(
    tmp_path: Path, screening
):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE frames (id INTEGER PRIMARY KEY, timestamp TEXT, app_name TEXT, "
        "window_name TEXT, browser_url TEXT, capture_trigger TEXT, full_text TEXT, "
        "text_source TEXT, device_name TEXT)"
    )
    db.executemany(
        "INSERT INTO frames VALUES (?, ?, ?, ?, '', 'click', ?, 'accessibility', ?)",
        [
            (1, "2026-07-23T10:00:00Z", "Code", "collector.py", "code", "Display 1"),
            (2, "2026-07-23T10:00:01Z", "Video", "Movie", "movie", "Display 2"),
        ],
    )
    batches: list[list[dict]] = []

    class FakeClient:
        def post(self, _url, json=None):
            batches.append(json["events"])
            return _Response({})

    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="screenpipe-rainbow",
        token="token",
        screenpipe_dir=tmp_path,
    )
    collector.checkpoints = Checkpoints(tmp_path / "checkpoints.json")
    collector.observations_path = tmp_path / "observations.json"
    collector.metrics = {
        "observation_opens": 0,
        "observation_closes": 0,
        "observation_samples": 0,
    }
    collector.client = FakeClient()

    if screening:
        from chronicle_screenpipe.screening import ScreeningStore

        collector.screening_store = ScreeningStore(tmp_path / "privacy.sqlite")

    assert collector.collect_observations(db) == 2
    if screening:
        assert {job["track"] for job in collector.screening_store.jobs()} == {
            "Display 1",
            "Display 2",
        }
    state = json.loads(collector.observations_path.read_text())
    assert state["schema"] == 2
    assert set(state["tracks"]) == {"Display 1", "Display 2"}
    assert state["tracks"]["Display 1"]["active"]["app_name"] == "Code"
    assert state["tracks"]["Display 2"]["active"]["app_name"] == "Video"
    assert {event["locator"]["track_id"] for event in batches[0]} == {
        "Display 1",
        "Display 2",
    }

    db.execute(
        "INSERT INTO frames VALUES (3, '2026-07-23T10:00:03Z', 'Browser', "
        "'Docs', '', 'click', 'docs', 'accessibility', 'Display 1')"
    )
    assert collector.collect_observations(db) == 2
    state = json.loads(collector.observations_path.read_text())
    assert state["tracks"]["Display 1"]["active"]["app_name"] == "Browser"
    assert state["tracks"]["Display 2"]["active"]["app_name"] == "Video"
