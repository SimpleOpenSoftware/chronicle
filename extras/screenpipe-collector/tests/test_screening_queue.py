"""Already-held missing frames must not starve untouched historical capture."""

import json

import httpx
import pytest
from chronicle_screenpipe.screening import ScreeningStore
from test_screening import frame, worker


def test_queue_health_separates_live_wait_from_history_and_survives_restart(
    tmp_path, monkeypatch
):
    from chronicle_screenpipe.screening import seconds

    path = tmp_path / "privacy.sqlite"
    history = ScreeningStore(path, namespace="history-test")
    history.observe(frame(1, timestamp="2026-08-17T10:00:00Z"))
    history.close()
    live = ScreeningStore(path)
    live.observe(frame(2))
    live.retry(live.jobs()[0]["id"])
    live.close()
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.time.time",
        lambda: seconds("2026-09-16T10:01:32Z"),
    )
    reopened = ScreeningStore(path)
    assert reopened.queue_health() == {
        "pending_jobs": 2,
        "failed_jobs": 1,
        "pending_live_jobs": 1,
        "pending_background_jobs": 1,
        "oldest_live_pending_seconds": 90.0,
    }
    with reopened.db:
        reopened.db.execute("UPDATE jobs SET delivered=1 WHERE priority=0")
    health = reopened.queue_health()
    assert health["pending_jobs"] == health["pending_background_jobs"] == 1
    assert health["failed_jobs"] == health["pending_live_jobs"] == 0
    assert health["oldest_live_pending_seconds"] is None
    reopened.close()


@pytest.mark.parametrize("retry_priority", [10, 8])
def test_worker_checks_fresh_history_before_rescreening_due_failure_after_restart(
    tmp_path, monkeypatch, retry_priority
):
    path = tmp_path / "privacy.sqlite"
    history = ScreeningStore(path, namespace="history-test")
    history.observe(frame(1, track="missing-display"))
    retry_id = history.jobs()[0]["id"]
    history.retry(retry_id)
    # A completed recent-history first pass must not keep its failed originals
    # ahead of untouched older captures merely because the batch was promoted.
    with history.db:
        history.db.execute(
            "UPDATE jobs SET priority=? WHERE id=?", (retry_priority, retry_id)
        )
    history.observe(frame(2, track="fresh-display"))
    history.db.execute("UPDATE jobs SET retry_at=0")
    history.db.commit()
    history.close()
    # The actual worker opens its durable queue again after restart.
    instance = worker(tmp_path)
    calls = []

    def read(_root, identifier):
        if identifier == 1:
            raise FileNotFoundError("Synthetic missing frame")
        return b"ordinary"

    original = httpx.Client

    def send(request):
        if request.url.path.endswith("/screening"):
            calls.append(json.loads(request.content))
            if len(calls) == 2:
                instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", read)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(send)),
    )
    instance.run()
    assert [r["track_id"] for r in calls] == ["fresh-display", "missing-display"]
    assert calls[1]["segments"][0]["state"] == "pending"
    assert calls[1]["evidence"][0]["reason"] == "missing_frame"
    reopened = ScreeningStore(path)
    assert reopened.pending_count() == 1 and reopened.failed_count() == 1
    assert (
        reopened.db.execute(
            "SELECT attempts FROM jobs WHERE id=?", (retry_id,)
        ).fetchone()[0]
        == 2
    )
    reopened.close()


def test_live_jobs_and_ready_deliveries_precede_fresh_history(tmp_path):
    path = tmp_path / "privacy.sqlite"
    history = ScreeningStore(path, namespace="history-test")
    history.observe(frame(1, track="missing-display"))
    missing_id = history.jobs()[0]["id"]
    history.retry(missing_id)
    history.observe(frame(2, track="delivery-display"))
    delivery_id = next(
        j["id"] for j in history.jobs() if j["track"] == "delivery-display"
    )
    history.complete(delivery_id, {"synthetic": "saved-result"})
    history.retry(delivery_id)
    history.observe(frame(3, track="fresh-display"))
    live = ScreeningStore(path)
    live.observe(frame(4, track="live-display"))
    history.db.execute("UPDATE jobs SET retry_at=0")
    history.db.commit()
    assert [j["track"] for j in history.jobs()] == [
        "live-display",
        "delivery-display",
        "fresh-display",
        "missing-display",
    ]
    assert history.jobs()[1]["result"] == {"synthetic": "saved-result"}
    assert [j["track"] for j in history.jobs(historical_only=True)] == [
        "delivery-display",
        "fresh-display",
        "missing-display",
    ]
    live.close()
    history.close()


def test_background_fairness_preserves_live_candidates_delivery_and_recent_order(
    tmp_path,
):
    store = ScreeningStore(tmp_path / "privacy.sqlite", namespace="history-test")
    specs = [
        ("recent-retry", 8, 1, None),
        ("old-fresh", 10, 0, None),
        ("recent-fresh", 8, 0, None),
        ("old-delivery", 10, 1, json.dumps({"synthetic": "saved-result"})),
        ("candidate-retry", 5, 1, None),
        ("live-retry", 0, 1, None),
        ("old-retry", 10, 1, None),
    ]
    for number, (track, priority, attempts, result) in enumerate(specs, 1):
        store.observe(frame(number, track=track))
        with store.db:
            store.db.execute(
                "UPDATE jobs SET priority=?,attempts=?,result=?,retry_at=0 WHERE track=?",
                (priority, attempts, result, track),
            )
    expected = [
        "live-retry",
        "candidate-retry",
        "old-delivery",
        "recent-fresh",
        "old-fresh",
        "recent-retry",
        "old-retry",
    ]
    assert [job["track"] for job in store.jobs()] == expected
    assert [job["track"] for job in store.jobs(historical_only=True)] == expected[1:]
    store.close()
