"""Partial failures retain exact holds and recover through durable replacements."""

import json

import httpx
import pytest
from chronicle_screenpipe.screening import ScreeningStore
from test_screening import FakeModel, worker
from test_screening_rechecks import recheck_frames


def test_partial_failure_restarts_retries_idempotently_and_recovers(
    tmp_path, monkeypatch
):
    path = tmp_path / "privacy.sqlite"
    store = ScreeningStore(path)
    store.queue_recheck("display", recheck_frames(), "old")
    identifier = store.jobs()[0]["id"]
    submissions, replacements = [], []
    missing = {1}
    original_client = httpx.Client
    current = [None]
    fail_replacement = [True]

    def read(_root, frame_id):
        if frame_id in missing:
            raise FileNotFoundError("Synthetic missing frame")
        return b"ordinary"

    def send(request):
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        body = json.loads(request.content)
        if request.url.path.endswith("/replace"):
            replacements.append(body)
            return httpx.Response(503 if fail_replacement[0] else 200)
        submissions.append(body)
        current[0].stop.set()
        return httpx.Response(200)

    monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", read)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(send)),
    )

    def run():
        current[0] = worker(tmp_path)
        current[0].run()

    run()
    partial = submissions[-1]
    assert [s["state"] for s in partial["segments"]] == [
        "pending",
        "allowed",
        "allowed",
        "allowed",
    ]
    assert partial["segments"][0]["coverage"] == "unverified"
    assert partial["evidence"][0]["reason"] == "missing_frame"
    assert "input_hash" not in partial["evidence"][0]
    assert store.pending_count() == 1 and store.failed_count() == 1
    assert store.replacement_count() == 1
    assert store.db.execute("select count(*) from predictions").fetchone()[0] == 1
    store.db.execute("update jobs set retry_at=0")
    store.db.commit()
    # Even a due retry cannot overtake an undelivered replacement.
    assert store.jobs() == []
    store.close()
    store = ScreeningStore(path)
    fail_replacement[0] = False
    run()
    assert submissions[-1] == partial
    assert replacements[0] == replacements[1]
    assert store.replacement_count() == 0 and store.pending_count() == 1
    # Recovery changes identity and explicitly replaces the last acknowledged hold.
    missing.clear()
    store.db.execute("update jobs set retry_at=0")
    store.db.commit()
    run()
    final = submissions[-1]
    assert final["interval_id"] != partial["interval_id"]
    assert all(
        s["state"] == "allowed" and s["coverage"] == "verified"
        for s in final["segments"]
    )
    assert replacements[-1] == {
        "previous_interval_ids": [partial["interval_id"]],
        "replacement_interval_id": final["interval_id"],
    }
    assert store.pending_count() == 0 and store.replacement_count() == 0
    assert (
        final["started_at"] == partial["started_at"]
        and final["ended_at"] == partial["ended_at"]
    )


def test_failure_changes_never_reuse_a_superseded_identity(tmp_path, monkeypatch):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.queue_recheck("display", recheck_frames(), "old")
    identifier = store.jobs()[0]["id"]
    instance = worker(tmp_path)
    results = []
    for missing in [1, 2, 1]:

        def read(_root, frame_id):
            if frame_id == missing:
                raise FileNotFoundError("Synthetic missing frame")
            return b"ordinary"

        monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", read)
        store.db.execute("update jobs set retry_at=0")
        store.db.commit()
        job = store.jobs()[0]
        result = instance.screen(job, store, FakeModel())
        store.complete(identifier, result)
        store.acknowledge(identifier)
        store.acknowledge_replacement(identifier)
        results.append(result)
    assert results[0]["evidence"] == results[2]["evidence"]
    assert len({r["interval_id"] for r in results}) == 3


@pytest.mark.parametrize("error", [FileNotFoundError, RuntimeError])
def test_partial_result_delivery_retry_does_not_rerun_inference(
    tmp_path, monkeypatch, error
):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.queue_recheck("display", recheck_frames(), "old")
    identifier = store.jobs()[0]["id"]

    def read(_root, frame_id):
        if frame_id == 1:
            raise error("Synthetic failure")
        return b"ordinary"

    monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", read)
    result = worker(tmp_path).screen(store.jobs()[0], store, FakeModel())
    store.complete(identifier, result)
    store.retry(identifier)
    store.close()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.db.execute("update jobs set retry_at=0")
    store.db.commit()
    instance = worker(tmp_path)
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame",
        lambda *_: pytest.fail("Delivery retry must use persisted result"),
    )
    original = httpx.Client
    sent = []

    def send(request):
        if request.url.path.endswith("/screening"):
            sent.append(json.loads(request.content))
            instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(**kw, transport=httpx.MockTransport(send)),
    )
    instance.run()
    assert sent == [result] and store.pending_count() == 1
