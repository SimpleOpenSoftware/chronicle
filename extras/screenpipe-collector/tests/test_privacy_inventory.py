"""Only original display inventories can refine historical coverage."""

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from chronicle_screenpipe.privacy_inventory import inventory_payload, queue_inventory
from chronicle_screenpipe.screening import ScreeningStore
from test_screening import worker

LOW = "2026-01-01T00:00:00+00:00"
HIGH = "2026-01-01T00:00:20+00:00"
CHANGE = "2026-01-01T00:00:10+00:00"


def database(path):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE display_layout(id INTEGER,timestamp TEXT,layout_json TEXT); CREATE TABLE frames(device_name TEXT,timestamp TEXT);"
    )
    for i, t in [(1, LOW), (2, CHANGE)]:
        db.execute(
            "INSERT INTO display_layout VALUES(?,?,?)",
            (i, t, json.dumps([dict(id=i, width=100, height=100)])),
        )
        db.execute("INSERT INTO frames VALUES(?,?)", (f"monitor_{i}", t))
    db.commit()
    return db


def test_changed_layout_holds_entire_uncertain_transition(tmp_path):
    db = database(tmp_path / "db.sqlite")
    payload = inventory_payload(db, LOW, HIGH, "screen-privacy-test")
    assert payload["original"]["track_ids"] == ["monitor_1", "monitor_2"]
    assert datetime.fromisoformat(
        payload["observations"][1]["transition_started_at"]
    ) == datetime.fromisoformat(LOW)
    assert all(len(r["evidence_sha256"]) == 64 for r in payload["observations"])
    assert "layout_json" not in json.dumps(payload)
    db.close()


@pytest.mark.parametrize(
    "change",
    [
        "contradiction",
        "missing_layout",
        "duplicate_time",
        "invalid_geometry",
        "naive_bounds",
        "too_long",
    ],
)
def test_ambiguous_capture_never_queues_refinement(tmp_path, change):
    db = database(tmp_path / "db.sqlite")
    low, high = LOW, HIGH
    if change == "contradiction":
        db.execute("INSERT INTO frames VALUES('unknown-display',?)", (LOW,))
    elif change == "missing_layout":
        db.execute("DELETE FROM display_layout")
    elif change == "duplicate_time":
        db.execute("UPDATE display_layout SET timestamp=?", (LOW,))
    elif change == "invalid_geometry":
        db.execute(
            "UPDATE display_layout SET layout_json=? WHERE id=1",
            (json.dumps([dict(id=1, width=0, height=0)]),),
        )
    elif change == "naive_bounds":
        low = "2026-01-01T00:00:00"
    else:
        high = "2026-03-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        inventory_payload(db, low, high, "screen-privacy-test")
    db.close()


def test_original_observation_before_window_is_retained(tmp_path):
    db = database(tmp_path / "db.sqlite")
    payload = inventory_payload(
        db, "2026-01-01T00:00:05+00:00", HIGH, "screen-privacy-test"
    )
    assert payload["observations"][0]["observed_at"] == "2026-01-01T00:00:00.000+00:00"
    assert (
        payload["observations"][1]["transition_started_at"]
        == payload["observations"][0]["observed_at"]
    )
    db.close()


def test_new_display_frame_during_held_transition_queues_original_inventory(tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir()
    db = database(capture / "db.sqlite")
    db.execute(
        "INSERT INTO frames VALUES('monitor_2',?)",
        ("2026-01-01T00:00:08.500000+00:00",),
    )
    db.commit()
    payload = inventory_payload(db, LOW, HIGH, "screen-privacy-test")
    db.close()
    # The original observation times are unchanged. The backend holds this
    # entire transition; the earlier frame does not establish display safety.
    assert payload["observations"][1]["observed_at"] == "2026-01-01T00:00:10.000+00:00"
    assert (
        payload["observations"][1]["transition_started_at"]
        == "2026-01-01T00:00:00.000+00:00"
    )
    assert queue_inventory(
        SimpleNamespace(screenpipe_dir=capture),
        tmp_path,
        LOW,
        HIGH,
        "screen-privacy-test",
    ) == {"queued": True, "inventory_observations": 2}


@pytest.mark.parametrize(
    "timestamp", ["2025-12-31T23:59:59+00:00", "2026-01-01T00:00:10.001+00:00"]
)
def test_contradiction_outside_transition_still_rejected(tmp_path, timestamp):
    db = database(tmp_path / "db.sqlite")
    track = "monitor_2" if timestamp < LOW else "monitor_1"
    db.execute("INSERT INTO frames VALUES(?,?)", (track, timestamp))
    with pytest.raises(ValueError, match="disagree"):
        inventory_payload(db, min(LOW, timestamp), HIGH, "screen-privacy-test")
    db.close()


def test_future_display_cannot_explain_frame_in_unchanged_inventory(tmp_path):
    db = database(tmp_path / "db.sqlite")
    db.execute(
        "INSERT INTO display_layout VALUES(?,?,?)",
        (
            3,
            "2026-01-01T00:00:05+00:00",
            json.dumps([dict(id=1, width=100, height=100)]),
        ),
    )
    db.execute(
        "INSERT INTO frames VALUES('monitor_2',?)", ("2026-01-01T00:00:03+00:00",)
    )
    with pytest.raises(ValueError, match="disagree"):
        inventory_payload(db, LOW, HIGH, "screen-privacy-test")
    db.close()


def test_queued_inventory_survives_restart_and_worker_delivers_after_obligation(
    tmp_path, monkeypatch
):
    capture = tmp_path / "capture"
    capture.mkdir()
    database(capture / "db.sqlite").close()
    result = queue_inventory(
        SimpleNamespace(screenpipe_dir=capture),
        tmp_path,
        LOW,
        HIGH,
        "screen-privacy-test",
    )
    assert result == dict(queued=True, inventory_observations=2)
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.require_range({"synthetic": "original-obligation"})
    # A failed request is retained, never acknowledged as coverage.
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        base_url="http://synthetic",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            store.deliver_required_ranges(client)
    store.close()
    instance = worker(tmp_path)
    seen = []
    original = httpx.Client

    def send(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/inventory"):
            instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(send)),
    )
    instance.run()
    assert seen[:2] == [
        "/api/device-input/screening/required-range",
        "/api/device-input/screening/required-range/inventory",
    ]
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    assert (
        store.db.execute("SELECT count(*) FROM inventory_refinement_outbox").fetchone()[
            0
        ]
        == 0
    )
    store.close()


def test_cli_queues_the_exact_original_policy_version(tmp_path, monkeypatch, capsys):
    import sys

    from chronicle_screenpipe import main

    capture = tmp_path / "capture"
    capture.mkdir()
    database(capture / "db.sqlite").close()
    monkeypatch.setattr(
        main, "load_config", lambda: SimpleNamespace(screenpipe_dir=capture)
    )
    monkeypatch.setattr(main, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chronicle-screenpipe",
            "refine-screen-inventory",
            "--start",
            LOW,
            "--end",
            HIGH,
            "--original-policy-version",
            "original-test-policy",
        ],
    )
    main.main()
    assert json.loads(capsys.readouterr().out)["queued"]
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    payload = json.loads(
        store.db.execute("SELECT payload FROM inventory_refinement_outbox").fetchone()[
            0
        ]
    )
    assert payload["original"]["policy_version"] == "original-test-policy"
    store.close()
