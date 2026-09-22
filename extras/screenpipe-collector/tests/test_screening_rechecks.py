import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from chronicle_screenpipe.privacy_history import queue_gap_rechecks
from chronicle_screenpipe.screening import ScreeningStore
from test_screening import FakeModel, frame, worker


def recheck_frames():
    return [
        {
            "id": i + 1,
            "time": f"2026-09-16T10:00:{t:02d}+00:00",
            "suspicious": False,
            "text_state": None,
        }
        for i, t in enumerate([0, 8, 16, 24, 32])
    ]


def test_recheck_coalesces_and_preserves_capture_cadence(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe(frame(1))
    before = tuple(store.db.execute("select * from tracks").fetchone())
    assert store.queue_recheck("display", recheck_frames(), "old")
    store.close()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    assert not store.queue_recheck("display", recheck_frames(), "old")
    assert tuple(store.db.execute("select * from tracks").fetchone()) == before
    assert len(store.jobs()) == 2
    assert len(store.jobs()[0]["frames"]) == 1  # live capture still has priority


def test_worker_restarts_between_result_delivery_and_replacement(tmp_path, monkeypatch):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.queue_recheck("display", recheck_frames(), "old")
    instance = worker(tmp_path)
    original_client = httpx.Client
    submissions, replacements = [], []

    def send(request):
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        body = json.loads(request.content)
        if request.url.path.endswith("/replace"):
            replacements.append(body)
            instance.stop.set()
            return httpx.Response(503 if len(replacements) == 1 else 200)
        submissions.append(body)
        assert store.replacement() is None  # never replace an unacknowledged result
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert len(submissions) == 1 and store.pending_count() == 0
    assert store.replacement_count() == 1
    assert submissions[0]["segments"][0]["state"] == "allowed"
    store.close()
    instance = worker(tmp_path)
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame",
        lambda *_: pytest.fail("Must reuse the completed result"),
    )
    instance.run()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    assert store.replacement_count() == 0
    assert len(submissions) == 1
    assert (
        replacements[0]
        == replacements[1]
        == {
            "previous_interval_ids": ["old"],
            "replacement_interval_id": submissions[0]["interval_id"],
        }
    )


def test_failed_replacement_does_not_stop_new_live_screening(tmp_path, monkeypatch):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.queue_recheck("display", recheck_frames(), "old")
    identifier = store.jobs()[0]["id"]
    store.complete(identifier, {"interval_id": "completed-recheck"})
    store.acknowledge(identifier)
    store.observe(frame(7, track="live-display"))
    instance = worker(tmp_path)
    submitted = []
    original_client = httpx.Client

    def send(request):
        if request.url.path.endswith("/replace"):
            return httpx.Response(503)
        if request.url.path.endswith("/screening"):
            submitted.append(json.loads(request.content))
            instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert [row["track_id"] for row in submitted] == ["live-display"]
    assert store.replacement_count() == 1


def test_queue_rechecks_uses_original_frame_ids_and_skips_real_gaps_and_positives(
    tmp_path, monkeypatch, capsys
):
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    db = sqlite3.connect(recorder / "db.sqlite")
    db.execute(
        "create table frames(id integer, timestamp text, device_name text, app_name text, window_name text, browser_url text, full_text text)"
    )
    for i, t in enumerate([0, 8, 16, 24, 32]):
        db.execute(
            "insert into frames values(?,?,?,?,?,?,?)",
            (
                i + 1,
                f"2026-09-16T10:00:{t:02d}Z",
                "display",
                "Browser",
                "Synthetic",
                "",
                "",
            ),
        )
    for i, t in [(10, 0), (11, 40)]:
        db.execute(
            "insert into frames values(?,?,?,?,?,?,?)",
            (
                i,
                f"2026-09-16T10:01:{t:02d}Z",
                "display",
                "Browser",
                "Synthetic",
                "",
                "",
            ),
        )
    db.commit()
    db.close()

    def row(identifier, first=1, last=5, state="allowed", minute="00", end="32"):
        return {
            "interval_id": identifier,
            "track_id": "display",
            "started_at": f"2026-09-16T10:{minute}:00+00:00",
            "ended_at": f"2026-09-16T10:{minute}:{end}+00:00",
            "segments": [{"state": "pending"}],
            "evidence": [
                {"frame_id": first, "state": state},
                {"frame_id": last, "state": state},
            ],
        }

    requested = []
    original_client = httpx.Client

    def serve(request):
        assert request.headers["Authorization"] == "Bearer synthetic"
        requested.append(str(request.url))
        if request.url.params["after"]:
            return httpx.Response(
                200,
                json={
                    "results": [row("real-gap", 10, 11, minute="01", end="40")],
                    "next_cursor": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    row("fixable"),
                    row("positive", state="excluded"),
                    row("missing", last=8),
                ],
                "next_cursor": "a" * 64,
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(serve)),
    )
    config = SimpleNamespace(
        screenpipe_dir=recorder, backend_url="http://backend", token="synthetic"
    )
    args = (config, tmp_path / "state", "2026-09-16T10:00:00Z", "2026-09-16T11:00:00Z")
    result = queue_gap_rechecks(*args)
    assert result == dict(
        examined=4, queued=1, already_queued=0, genuine_gaps=1, unavailable=1
    )
    assert len(requested) == 2
    from chronicle_screenpipe import main

    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main, "state_dir", lambda: args[1])
    monkeypatch.setattr(
        "sys.argv",
        [
            "chronicle-screenpipe",
            "recheck-screen-gaps",
            "--start",
            args[2],
            "--end",
            args[3],
        ],
    )
    main.main()
    assert json.loads(capsys.readouterr().out)["already_queued"] == 1
    store = ScreeningStore(tmp_path / "state/privacy.sqlite")
    assert len(store.jobs()) == 1
    assert [f["id"] for f in store.jobs()[0]["frames"]] == [1, 2, 3, 4, 5]
    assert store.db.execute("select count(*) from tracks").fetchone()[0] == 0
