import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from chronicle_screenpipe.frames import read_frame
from chronicle_screenpipe.privacy_history import queue_history
from chronicle_screenpipe.screening import ScreeningStore


def test_frame_reader_uses_exact_video_offset_and_holds_missing_frame(
    tmp_path, monkeypatch
):
    video = tmp_path / "capture.mp4"
    video.write_bytes(b"synthetic container")
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        db.executescript("""
            CREATE TABLE frames(id INTEGER PRIMARY KEY, snapshot_path TEXT, name TEXT,
                                offset_index INTEGER, video_chunk_id INTEGER);
            CREATE TABLE video_chunks(id INTEGER PRIMARY KEY, file_path TEXT);
        """)
        db.execute("INSERT INTO video_chunks VALUES (1, ?)", (str(video),))
        db.execute("INSERT INTO frames VALUES (7, NULL, NULL, 42, 1)")
    commands = []

    def extract(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"synthetic requested frame")

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    assert read_frame(tmp_path, 7) == b"synthetic requested frame"
    assert read_frame(tmp_path, 7) == b"synthetic requested frame"
    assert "select='eq(n,42)'" in commands[0]
    with pytest.raises(FileNotFoundError):
        read_frame(tmp_path, 8)
    assert len(commands) == 1


@pytest.fixture
def video_frames(tmp_path):
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"safe")
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        db.executescript("""
            CREATE TABLE frames(id INTEGER PRIMARY KEY, snapshot_path TEXT, name TEXT,
                                offset_index INTEGER, video_chunk_id INTEGER);
            CREATE TABLE video_chunks(id INTEGER PRIMARY KEY, file_path TEXT);
        """)
        db.execute("INSERT INTO video_chunks VALUES (1, ?)", (str(video),))
        db.executemany(
            "INSERT INTO frames VALUES (?, NULL, NULL, ?, 1)",
            [(7, 42), (8, 43), (9, 44)],
        )
    return video


def test_video_cache_checks_content_when_size_and_mtime_are_unchanged(
    tmp_path, video_frames, monkeypatch
):
    import os

    calls = []

    def extract(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=video_frames.read_bytes())

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    assert read_frame(tmp_path, 7) == b"safe"
    before = video_frames.stat()
    video_frames.write_bytes(b"risk")
    os.utime(video_frames, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert read_frame(tmp_path, 7) == b"risk"
    video_frames.write_bytes(b"safe")
    assert read_frame(tmp_path, 7) == b"safe"
    assert len(calls) == 2
    video_frames.unlink()
    with pytest.raises(FileNotFoundError):
        read_frame(tmp_path, 7)


def test_video_cache_never_reuses_a_different_offset(
    tmp_path, video_frames, monkeypatch
):
    calls = []

    def extract(command, **kwargs):
        offset = command[command.index("-vf") + 1].encode()
        calls.append(offset)
        return SimpleNamespace(returncode=0, stdout=offset)

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    first = read_frame(tmp_path, 7)
    assert read_frame(tmp_path, 8) != first
    assert read_frame(tmp_path, 7) == first
    assert len(calls) == 2


def test_failed_or_changed_decode_does_not_enter_frame_cache(
    tmp_path, video_frames, monkeypatch
):
    calls = []

    def extract(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout=b"")
        if len(calls) == 2:
            video_frames.write_bytes(b"risk")
            return SimpleNamespace(returncode=0, stdout=b"safe")
        return SimpleNamespace(returncode=0, stdout=b"risk")

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    with pytest.raises(FileNotFoundError):
        read_frame(tmp_path, 7)
    with pytest.raises(RuntimeError, match="changed"):
        read_frame(tmp_path, 7)
    assert read_frame(tmp_path, 7) == b"risk"
    assert read_frame(tmp_path, 7) == b"risk"
    assert len(calls) == 3


def test_video_frame_cache_has_a_byte_budget(tmp_path, video_frames, monkeypatch):
    calls = []
    pixels = b"x" * (8 * 1024 * 1024)

    def extract(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=pixels)

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    for identifier in [7, 8, 9, 9, 7]:
        assert read_frame(tmp_path, identifier) == pixels
    # The newest frame was cached; adding the third evicted the first.
    assert len(calls) == 4


def test_identical_concurrent_frame_reads_share_one_decode(
    tmp_path, video_frames, monkeypatch
):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    calls = []
    barrier = Barrier(4)

    def extract(command, **kwargs):
        calls.append(command)
        time.sleep(0.02)
        return SimpleNamespace(returncode=0, stdout=b"same exact frame")

    def read(_):
        barrier.wait(timeout=5)
        return read_frame(tmp_path, 7)

    monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(4))) == [b"same exact frame"] * 4
    assert len(calls) == 1


def test_historical_queue_is_bounded_idempotent_and_keeps_live_cadence(
    tmp_path, monkeypatch
):
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    state = tmp_path / "state"
    with sqlite3.connect(recorder / "db.sqlite") as db:
        db.execute(
            "CREATE TABLE frames (id INTEGER PRIMARY KEY, timestamp TEXT, device_name TEXT, "
            "app_name TEXT, window_name TEXT, browser_url TEXT, full_text TEXT)"
        )
        db.executemany(
            "INSERT INTO frames VALUES (?, ?, 'screen', 'Editor', 'Document', '', '')",
            [(i, f"2026-09-16T10:00:{i:02d}Z") for i in range(1, 13)],
        )
    config = SimpleNamespace(
        screenpipe_dir=recorder, backend_url="http://backend", token="synthetic-token"
    )
    sent = []

    def submit(request):
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert request.url.path == "/api/device-input/screening/required-range"
        import json

        body = json.loads(request.content)
        assert body["track_ids"] == ["screen"]
        if not sent:
            assert ScreeningStore(state / "privacy.sqlite").jobs() == []
        sent.append(body)
        return httpx.Response(200)

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(submit)),
    )
    first = queue_history(config, state, "2026-09-16T10:00:00Z", "2026-09-16T10:00:12Z")
    again = queue_history(config, state, "2026-09-16T10:00:00Z", "2026-09-16T10:00:12Z")
    assert first == again
    assert sent[0] == sent[1]
    assert first["frames_visited"] == 11
    assert first["pending_jobs"] == 2
    live = ScreeningStore(state / "privacy.sqlite")
    live.observe(dict(id=12, timestamp="2026-09-16T10:00:12Z", device_name="screen"))
    assert live.jobs()[0]["frames"][0]["id"] == 12
    with pytest.raises(ValueError):
        queue_history(config, state, "2026-09-16T10:00:00", "2026-09-16T10:00:12")


def test_required_history_hold_survives_failed_delivery_and_restart(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    payload = dict(
        started_at="2026-09-01T00:00:00Z", ended_at="2026-09-02T00:00:00Z", track_ids=[]
    )
    store.require_range(payload)
    with httpx.Client(
        base_url="http://backend",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            store.deliver_required_ranges(client)
    store.close()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    sent = []

    def accept(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200)

    with httpx.Client(
        base_url="http://backend", transport=httpx.MockTransport(accept)
    ) as client:
        store.deliver_required_ranges(client)
        store.deliver_required_ranges(client)
    assert sent == [payload]
