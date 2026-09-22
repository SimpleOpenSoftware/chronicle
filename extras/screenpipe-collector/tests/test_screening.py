import hashlib
from types import SimpleNamespace

import httpx
import pytest
from chronicle_screenpipe.screening import ScreeningStore, ScreeningWorker


def frame(i, track="display", **values):
    return dict(
        dict(
            id=i,
            timestamp=f"2026-09-16T10:00:{i:02d}Z",
            device_name=track,
            app_name="Browser",
            window_name="Tab",
            browser_url="",
            full_text="",
        ),
        **values,
    )


def test_capture_cadence_survives_restart_and_separates_displays(tmp_path):
    path = tmp_path / "privacy.sqlite"
    store = ScreeningStore(path)
    for i in range(1, 7):
        store.observe(frame(i))
    store.close()
    store = ScreeningStore(path)
    for i in range(7, 12):
        store.observe(frame(i))
    store.observe(frame(12, track="second"))
    jobs = store.jobs()
    assert [(j["track"], [f["id"] for f in j["frames"]]) for j in jobs] == [
        ("display", [1]),
        ("display", list(range(1, 12))),
        ("second", [12]),
    ]
    store.observe(frame(11))
    assert len(store.jobs()) == 3


def test_failed_capture_batch_rolls_back_cadence_and_pending_jobs(tmp_path):
    path = tmp_path / "privacy.sqlite"
    store = ScreeningStore(path, namespace="history-test")
    with pytest.raises(ValueError):
        store.observe_many([frame(1), frame(2, timestamp="invalid")])
    store.close()
    store = ScreeningStore(path, namespace="history-test")
    assert store.jobs() == []
    store.observe_many([frame(i) for i in range(1, 12)])
    assert [[f["id"] for f in job["frames"]] for job in store.jobs()] == [
        [1],
        list(range(1, 12)),
    ]
    store.close()
    store = ScreeningStore(path, namespace="history-test")
    store.observe_many([frame(i) for i in range(1, 12)])
    assert len(store.jobs()) == 2


def test_cached_predictions_require_identical_input_and_model(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite", cache_limit=2)
    assert store.cached("image-a", "model-a") is None
    prediction = [{"class": "high", "score": 0.8, "region": [1, 2, 3, 4]}]
    store.cache("image-a", "model-a", prediction, "00")
    assert store.cached("image-a", "model-a") == prediction
    assert store.cached("image-b", "model-a") is None
    assert store.cached("image-a", "model-b") is None


def test_display_inventory_remembers_transition_boundary_across_restart(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe_displays(["one", "two"], "2026-09-16T10:00:00+00:00")
    store.observe_displays(["two", "one"], "2026-09-16T10:00:05+00:00")
    assert len(store.display_results()) == 1
    store.close()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe_displays(["one"], "2026-09-16T10:00:10+00:00")
    assert store.display_results()[1] == {
        "observed_at": "2026-09-16T10:00:10+00:00",
        "transition_started_at": "2026-09-16T10:00:05+00:00",
        "track_ids": ["one"],
    }


def test_worker_retries_authenticated_display_inventory_before_screening(
    tmp_path, monkeypatch
):
    from chronicle_screenpipe.collector import Config

    config = Config(
        backend_url="http://backend",
        source_id="synthetic-source",
        token="backend-token",
        screenpipe_dir=tmp_path,
        screenpipe_token="recorder-token",
    )
    inventory_calls = []

    def get(url, **kwargs):
        inventory_calls.append((url, kwargs["headers"]))
        return httpx.Response(
            200, json=[{"id": 7}, {"id": 9}], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)
    sent = []
    instance = ScreeningWorker(config, tmp_path, FakeModel)

    def send(request):
        assert request.url.path.endswith("/screening/displays")
        assert request.headers["Authorization"] == "Bearer backend-token"
        sent.append(request.content)
        instance.stop.set()
        return httpx.Response(503 if len(sent) == 1 else 200)

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    instance.run()
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    assert store.display_results()[0]["track_ids"] == ["monitor_7", "monitor_9"]
    assert instance.health["state"] == "unavailable"
    instance = ScreeningWorker(config, tmp_path, FakeModel)
    instance.run()
    assert sent[0] == sent[1]
    assert store.display_results() == []
    assert inventory_calls[0] == (
        "http://127.0.0.1:3030/vision/list",
        {"Authorization": "Bearer recorder-token"},
    )


def test_lru_bounds_storage_and_preserves_recently_used_predictions(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite", cache_limit=2)
    for image in ("a", "b"):
        store.cache(image, "model", [], "00")
    store.cached("a", "model")
    store.cache("c", "model", [], "00")
    assert store.cached("b", "model") is None
    assert store.cached("a", "model") == []
    assert set(store.similar("00", "model")) == {"a", "c"}


def test_additional_checks_do_not_reset_one_in_ten_cadence(tmp_path):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    for i in range(1, 12):
        store.observe(frame(i, window_name="New tab" if i >= 4 else "Tab"))
    assert [job["frames"][-1]["id"] for job in store.jobs()] == [1, 4, 11]
    store.observe(frame(12, timestamp="2026-09-16T10:00:41Z", window_name="New tab"))
    assert store.jobs()[-1]["frames"][-1]["id"] == 12


class FakeModel:
    version = "fake-v1"

    def prepare(self, data):
        return data, hashlib.sha256(data).hexdigest(), "same-perceptual-hash"

    def predict(self, data):
        return [
            {
                "class": "high",
                "score": 0.9 if data == b"positive" else 0,
                "region": [0, 0, 10, 10],
            }
        ]


def worker(tmp_path, factory=FakeModel):
    config = SimpleNamespace(
        backend_url="http://backend", token="synthetic-token", screenpipe_dir=tmp_path
    )
    return ScreeningWorker(
        config, tmp_path, factory, inventory_reader=lambda _: ["display"]
    )


def test_positive_sample_checks_intervening_frames_and_recovery(tmp_path, monkeypatch):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    for i in range(1, 22):
        store.observe(frame(i))
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame",
        lambda _root, identifier: (
            b"positive" if 8 <= identifier <= 13 else str(identifier).encode()
        ),
    )
    instance = worker(tmp_path)
    jobs = store.jobs()
    before = instance.screen(jobs[1], store, FakeModel())
    after = instance.screen(jobs[2], store, FakeModel())
    assert len(before["evidence"]) == len(after["evidence"]) == 11
    assert all(
        row["coverage"] == "verified" for row in before["segments"] + after["segments"]
    )
    assert before["segments"][0]["state"] == "allowed"
    assert before["segments"][-1]["state"] == "excluded"
    assert after["segments"][0]["state"] == "excluded"
    assert after["segments"][-1]["state"] == "allowed"
    # Similar desktops containing different video frames never share a verdict.
    assert instance.health["inferences"] > 2


@pytest.mark.parametrize(
    "times,expected_states,coverage",
    [
        ([0, 8, 16, 24, 32], ["allowed"], ["sampled"]),
        ([0, 8, 40], ["allowed", "pending"], ["verified", "unverified"]),
    ],
)
def test_worker_distinguishes_sampling_delay_from_capture_gap(
    tmp_path, monkeypatch, times, expected_states, coverage
):
    import json

    store = ScreeningStore(tmp_path / "privacy.sqlite")
    for identifier, second in enumerate(times, 1):
        store.observe(frame(identifier, timestamp=f"2026-09-16T10:00:{second:02d}Z"))
    instance = worker(tmp_path)
    sent = []
    original_client = httpx.Client

    def send(request):
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        sent.append(json.loads(request.content))
        if len(sent) == 2:
            instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert [segment["state"] for segment in sent[-1]["segments"]] == expected_states
    assert [segment["coverage"] for segment in sent[-1]["segments"]] == coverage
    for segment in sent[-1]["segments"]:
        if segment["state"] == "pending":
            assert segment["reason"] == "capture_gap"
    assert store.pending_count() == 0


@pytest.mark.parametrize("failure", [FileNotFoundError, RuntimeError])
def test_failed_screening_never_enters_allowed_cache_or_outbox(
    tmp_path, monkeypatch, failure
):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe(frame(1))
    instance = worker(tmp_path)

    def fail(*_args):
        instance.stop.set()
        raise failure("Synthetic failure")

    monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", fail)

    def forbidden(*_args, **_kwargs):
        if _args[1].endswith("/displays"):
            return httpx.Response(
                200, request=httpx.Request("POST", "http://backend/displays")
            )
        result = _kwargs["json"]
        assert all(segment["state"] == "pending" for segment in result["segments"])
        assert all("input_hash" not in evidence for evidence in result["evidence"])
        return httpx.Response(
            200, request=httpx.Request("POST", "http://backend/screening")
        )

    monkeypatch.setattr(httpx.Client, "post", forbidden)
    instance.run()
    assert store.failed_count() == 1
    assert store.db.execute("SELECT count(*) FROM predictions").fetchone()[0] == 0
    assert store.db.execute("SELECT result FROM jobs").fetchone()[0] is None
    assert instance.health["state"] == "needs_review"


def test_delivery_restart_reuses_persisted_result(tmp_path, monkeypatch):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe(frame(1))
    instance = worker(tmp_path)
    original_client = httpx.Client
    requests = []

    def send(request):
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        requests.append(request.content)
        instance.stop.set()
        return httpx.Response(503 if len(requests) == 1 else 200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert store.failed_count() == 1
    store.db.execute("UPDATE jobs SET retry_at=0")
    store.db.commit()

    def forbidden(*_args):
        raise AssertionError("Delivery retry must use its durable result")

    monkeypatch.setattr("chronicle_screenpipe.screening.read_frame", forbidden)
    instance = worker(tmp_path)
    instance.run()
    assert requests[0] == requests[1]
    assert store.pending_count() == 0


@pytest.mark.parametrize("suspicious", [False, True])
def test_new_live_capture_takes_priority_over_remaining_history(
    tmp_path, monkeypatch, suspicious
):
    import json

    historical = ScreeningStore(tmp_path / "privacy.sqlite", namespace="history-test")
    for i in range(1, 22):
        historical.observe(
            frame(
                i,
                browser_url=(
                    "https://www.google.com/search?q=porn" if suspicious else ""
                ),
            )
        )
    live = ScreeningStore(tmp_path / "privacy.sqlite")
    instance = worker(tmp_path)
    requests = []

    def send(request):
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            live.observe(frame(22, track="live-display"))
        else:
            instance.stop.set()
        return httpx.Response(200)

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert [row["track_id"] for row in requests] == ["display", "live-display"]
    assert historical.pending_count() == (20 if suspicious else 2)


def test_recorder_outage_holds_live_jobs_but_allows_stored_history(
    tmp_path, monkeypatch
):
    import json

    history = ScreeningStore(tmp_path / "privacy.sqlite", namespace="history-test")
    history.observe(frame(1, track="historical-display"))
    live = ScreeningStore(tmp_path / "privacy.sqlite")
    live.observe(frame(2, track="live-display"))
    instance = worker(tmp_path)

    def unavailable(_):
        raise ConnectionError("Synthetic recorder outage")

    instance.inventory_reader = unavailable
    sent = []
    original_client = httpx.Client

    def send(request):
        sent.append(json.loads(request.content))
        instance.stop.set()
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(
        "chronicle_screenpipe.screening.read_frame", lambda *_: b"ordinary"
    )
    instance.run()
    assert [row["track_id"] for row in sent] == ["historical-display"]
    assert [row["track"] for row in live.jobs()] == ["live-display"]
    assert instance.health["state"] == "unavailable"
    assert instance.health["last_failure"] == "display_inventory_unavailable"


def test_worker_delivers_historical_hold_even_when_model_cannot_start(
    tmp_path, monkeypatch
):
    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.require_range(
        dict(
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-02T00:00:00Z",
            track_ids=[],
        )
    )
    instance = worker(tmp_path)
    calls = []

    def unavailable():
        assert calls == ["/api/device-input/screening/required-range"]
        instance.stop.set()
        raise RuntimeError("Synthetic unavailable model")

    instance.model_factory = unavailable

    def send(request):
        calls.append(request.url.path)
        return httpx.Response(200)

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(send)),
    )
    instance.run()
    assert (
        store.db.execute("SELECT count(*) FROM required_range_outbox").fetchone()[0]
        == 0
    )
    assert instance.health["state"] == "unavailable"


@pytest.mark.parametrize("device", ["cuda", "mps"])
def test_configured_gpu_failure_keeps_jobs_pending_and_reports_queue(
    tmp_path, monkeypatch, device
):
    from chronicle_screenpipe.collector import Config

    store = ScreeningStore(tmp_path / "privacy.sqlite")
    store.observe(frame(1))
    config = Config(
        backend_url="http://backend",
        source_id="synthetic",
        token="synthetic-token",
        screenpipe_dir=tmp_path,
        privacy_device=device,
    )
    instance = ScreeningWorker(config, tmp_path)
    calls = []

    def unavailable(*, device):
        calls.append(device)
        instance.stop.set()
        raise RuntimeError("Synthetic unavailable GPU")

    monkeypatch.setattr("chronicle_screenpipe.screening.FreepikModel", unavailable)
    instance.run()
    assert calls == [device]
    assert store.pending_count() == 1
    assert instance.health["pending_jobs"] == 1
    assert instance.health["pending_live_jobs"] == 1
    assert instance.health["pending_background_jobs"] == 0
    assert instance.health["oldest_live_pending_seconds"] > 0
    assert instance.health["state"] == "unavailable"


@pytest.mark.parametrize("real_frame_reader", [False, True])
def test_worker_delivers_durable_results_without_reclassifying_duplicates(
    tmp_path, monkeypatch, real_frame_reader
):
    import hashlib
    import json
    from types import SimpleNamespace

    import httpx
    from chronicle_screenpipe.screening import ScreeningWorker

    store = ScreeningStore(tmp_path / "privacy.sqlite")
    for i in range(1, 12):
        store.observe(frame(i))
    store.close()

    class Model:
        version = "fake-v1"

        def prepare(self, data):
            return data, hashlib.sha256(data).hexdigest(), "00"

        def predict(self, picture):
            return []

    config = SimpleNamespace(
        backend_url="http://backend", token="test", screenpipe_dir=tmp_path
    )
    worker = ScreeningWorker(
        config, tmp_path, Model, inventory_reader=lambda _: ["display"]
    )
    delivered = []

    def send(request):
        if request.url.path.endswith("/displays"):
            return httpx.Response(200)
        delivered.append(json.loads(request.content))
        if len(delivered) == 2:
            worker.stop.set()
        return httpx.Response(200, json={"accepted": True})

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(send)),
    )
    decodes = []
    if real_frame_reader:
        import sqlite3

        video = tmp_path / "synthetic.mp4"
        video.write_bytes(b"synthetic video")
        with sqlite3.connect(tmp_path / "db.sqlite") as db:
            db.executescript("""
                CREATE TABLE frames(id INTEGER PRIMARY KEY, snapshot_path TEXT,
                    name TEXT, offset_index INTEGER, video_chunk_id INTEGER);
                CREATE TABLE video_chunks(id INTEGER PRIMARY KEY, file_path TEXT);
            """)
            db.execute("INSERT INTO video_chunks VALUES (1, ?)", (str(video),))
            db.executemany(
                "INSERT INTO frames VALUES (?, NULL, NULL, ?, 1)", [(1, 0), (11, 10)]
            )

        def extract(command, **kwargs):
            decodes.append(command)
            return SimpleNamespace(returncode=0, stdout=b"unchanged pixels")

        monkeypatch.setattr("chronicle_screenpipe.frames.subprocess.run", extract)
    else:
        monkeypatch.setattr(
            "chronicle_screenpipe.screening.read_frame", lambda *a: b"unchanged pixels"
        )
    worker.run()
    assert worker.health["inferences"] == 1
    assert worker.health["cache_hits"] == 2
    assert worker.health["completed_jobs"] == 2
    assert worker.health["retried_jobs"] == 0
    assert worker.health["uptime_seconds"] >= 0
    for stage in ("frame_read", "prepare", "delivery"):
        assert worker.health[f"{stage}_total_ms"] >= worker.health[f"{stage}_ms"] >= 0
    assert delivered[1]["segments"] == [
        {
            "started_at": "2026-09-16T10:00:01.000+00:00",
            "ended_at": "2026-09-16T10:00:11.000+00:00",
            "state": "allowed",
            "coverage": "sampled",
        }
    ]
    assert ScreeningStore(tmp_path / "privacy.sqlite").jobs() == []
    if real_frame_reader:
        assert len(decodes) == 2


def test_classifier_policy_sums_medium_and_high_within_each_crop():
    from chronicle_screenpipe.screening import verdict

    assert verdict([{"class": "low", "score": 0.99, "region": [0, 0, 10, 10]}]) == (
        "allowed",
        0,
    )
    assert verdict(
        [
            {"class": "medium", "score": 0.4, "region": [0, 0, 10, 10]},
            {"class": "high", "score": 0.3, "region": [0, 0, 10, 10]},
        ]
    ) == ("excluded", 0.7)
    assert verdict(
        [
            {"class": "medium", "score": 0.4, "region": [0, 0, 10, 10]},
            {"class": "high", "score": 0.3, "region": [5, 5, 10, 10]},
        ]
    ) == ("needs_review", 0.4)


def test_text_policy_distinguishes_searches_from_mentions():
    from chronicle_screenpipe.screening import adult_context

    assert (
        adult_context({"browser_url": "https://www.google.com/search?q=porn+videos"})
        == "excluded"
    )
    assert adult_context({"browser_url": "https://www.pornhub.com/"}) == "excluded"
    assert adult_context({"full_text": "Google All Images Videos porn"}) == "excluded"
    assert (
        adult_context({"full_text": "A discussion mentioning Pornhub"})
        == "needs_review"
    )
    assert adult_context({"full_text": "Implement a porn detection model"}) is None
    assert (
        adult_context({"browser_url": "https://www.google.com/search?q=weather"})
        is None
    )


def test_failed_frames_back_off_durably_without_blocking_new_capture(
    tmp_path, monkeypatch
):
    moment = [1000.0]
    monkeypatch.setattr("chronicle_screenpipe.screening.time.time", lambda: moment[0])
    path = tmp_path / "privacy.sqlite"
    store = ScreeningStore(path)
    store.observe(frame(1))
    identifier = store.jobs()[0]["id"]
    for expected in [30, 60, 120, 240, 480, 960, 1800, 1800]:
        assert store.retry(identifier) == expected
        store.close()
        store = ScreeningStore(path)
        assert store.jobs() == []
        moment[0] += expected - 1
        assert store.jobs() == []
        moment[0] += 1
        assert store.jobs()[0]["id"] == identifier
    assert store.failed_count() == 1
    store.retry(identifier)
    store.observe(frame(2, track="another-display"))
    jobs = store.jobs()
    assert len(jobs) == 1 and jobs[0]["track"] == "another-display"
    assert store.pending_count() == 2
    assert store.cached("unverified", "fake-model") is None
    assert (
        store.db.execute(
            "SELECT result FROM jobs WHERE id=?", (identifier,)
        ).fetchone()[0]
        is None
    )
    store.close()
