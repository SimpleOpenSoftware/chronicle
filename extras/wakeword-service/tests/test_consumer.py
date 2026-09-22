import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Response
from google.protobuf import timestamp_pb2
from redis import exceptions as redis_exceptions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as wake_app
from audio_contract.v2 import audio_pb2
from consumer import GROUP_NAME, WakeWordConsumer
from detector import WakeEvent, WakeEventInterval
from identities import (
    AudioChunkRef,
    AudioSessionRef,
    AudioStreamName,
    ClientId,
    SessionId,
    device_downlink_channel,
)


class FakeRedis:
    def __init__(self):
        self.read_count = 0
        self.published = []

    async def hmget(self, key, *fields):
        assert key == "audio:session:session-uuid"
        assert fields == ("client_id", "capture_epoch", "started_at")
        return [b"a421c9-elato", b"4", b"1770000000.0"]

    async def xgroup_create(self, *args, **kwargs):
        return None

    async def xreadgroup(self, group, consumer, streams, count, block):
        assert group == GROUP_NAME
        assert streams == {"audio:v2:realtime:session-uuid": ">"}
        self.read_count += 1
        if self.read_count == 1:
            captured_at = timestamp_pb2.Timestamp()
            captured_at.FromMilliseconds(1_770_000_001_250)
            event = audio_pb2.CaptureStreamEvent(
                frame=audio_pb2.CanonicalPcmFrame(
                    binding=audio_pb2.CaptureBinding(
                        capture_session_id=audio_pb2.CaptureSessionId(
                            value="session-uuid"
                        )
                    ),
                    captured_at=captured_at,
                    delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
                    pcm_s16le=b"\x00\x00",
                )
            )
            return [
                (
                    b"audio:v2:realtime:session-uuid",
                    [
                        (
                            b"1-0",
                            {b"event": event.SerializeToString()},
                        )
                    ],
                )
            ]
        ended = audio_pb2.CaptureStreamEvent(
            ended=audio_pb2.CaptureStreamEnded(
                binding=audio_pb2.CaptureBinding(
                    capture_session_id=audio_pb2.CaptureSessionId(value="session-uuid")
                )
            )
        )
        return [
            (
                b"audio:v2:realtime:session-uuid",
                [(b"2-0", {b"event": ended.SerializeToString()})],
            )
        ]

    async def xack(self, *args):
        return 1

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1


class FakeDetector:
    def __init__(self):
        self.calls = []

    def new_client_state(self):
        return SimpleNamespace(armed=False, priming=False)

    async def process_frame(self, state, session_ref, chunk_ref, pcm):
        assert isinstance(chunk_ref, AudioChunkRef)
        self.calls.append(
            (
                str(session_ref.client_id),
                str(session_ref.session_id),
                session_ref.capture_epoch,
                session_ref.started_at,
                chunk_ref.captured_at,
                chunk_ref.time_basis,
            )
        )
        return WakeEvent(
            client_id=session_ref.client_id,
            session_id=session_ref.session_id,
            wakeword="hey_hermes",
            audio=pcm,
            arm_time=1.0,
            eot_time=2.0,
            score=0.99,
            reason="test",
        )

    def flush(self, state, session_ref):
        return None


@pytest.mark.asyncio
async def test_process_stream_keeps_client_and_session_id_distinct():
    detector = FakeDetector()
    consumer = WakeWordConsumer(detector, "redis://unused", SimpleNamespace())
    consumer.redis_client = FakeRedis()
    consumer.running = True
    events = []

    async def capture_event(event):
        events.append(event)

    consumer._handle_event = capture_event

    await consumer._process_stream(
        AudioStreamName.from_value("audio:v2:realtime:session-uuid"),
        SessionId.from_value("session-uuid"),
    )

    assert detector.calls == [
        (
            "a421c9-elato",
            "session-uuid",
            4,
            1770000000.0,
            1770000001.25,
            "captured",
        )
    ]
    assert [(str(event.client_id), str(event.session_id)) for event in events] == [
        ("a421c9-elato", "session-uuid")
    ]


@pytest.mark.asyncio
async def test_reclaimed_drained_stream_ends_without_task_failure():
    class ReclaimedRedis(FakeRedis):
        async def xreadgroup(self, *args, **kwargs):
            raise redis_exceptions.ResponseError(
                "NOGROUP No such key 'audio:v2:realtime:session-uuid' "
                "or consumer group 'wakeword-v2'"
            )

    consumer = WakeWordConsumer(FakeDetector(), "redis://unused", SimpleNamespace())
    consumer.redis_client = ReclaimedRedis()
    consumer.running = True

    await consumer._process_stream(
        AudioStreamName.from_value("audio:v2:realtime:session-uuid"),
        SessionId.from_value("session-uuid"),
    )


def test_device_downlink_channel_requires_client_identity():
    assert str(device_downlink_channel(ClientId.from_value("a421c9-elato"))) == (
        "device:downlink:a421c9-elato"
    )

    with pytest.raises(TypeError, match="ClientId"):
        device_downlink_channel(SessionId.from_value("session-uuid"))


def test_wake_consumer_health_flags_alive_but_lagging_stream():
    consumer = WakeWordConsumer(FakeDetector(), "redis://unused", SimpleNamespace())
    consumer.running = True
    session_id = SessionId.from_value("session-uuid")
    consumer._stream_tasks[session_id] = SimpleNamespace(done=lambda: False)

    consumer._record_stream_progress(
        session_id,
        AudioStreamName.from_value("audio:v2:realtime:session-uuid"),
        "95000-0",
        processed_at_ms=100_000.0,
    )

    health = consumer.health()

    assert health["healthy"] is False
    assert health["lagging_streams"] == 1
    assert health["delivery_lag_max_ms"] == 5_000.0
    assert health["streams"][0]["last_consumed_id"] == "95000-0"


def test_wake_consumer_health_accepts_fresh_stream_progress():
    consumer = WakeWordConsumer(FakeDetector(), "redis://unused", SimpleNamespace())
    consumer.running = True
    session_id = SessionId.from_value("session-uuid")
    consumer._stream_tasks[session_id] = SimpleNamespace(done=lambda: False)

    consumer._record_stream_progress(
        session_id,
        AudioStreamName.from_value("audio:v2:realtime:session-uuid"),
        "99500-0",
        processed_at_ms=100_000.0,
    )

    health = consumer.health()

    assert health["healthy"] is True
    assert health["lagging_streams"] == 0
    assert health["delivery_lag_max_ms"] == 500.0


@pytest.mark.asyncio
async def test_http_health_is_unhealthy_when_wake_consumer_is_falling_behind(
    monkeypatch,
):
    wake_consumer = SimpleNamespace(
        running=True,
        health=lambda: {
            "healthy": False,
            "active_streams": 1,
            "lagging_streams": 1,
            "delivery_lag_max_ms": 5_000.0,
        },
    )
    active_consumer = SimpleNamespace(
        running=True,
        health=lambda: {"healthy": True, "running": True, "active_streams": 1},
    )
    loop_monitor = SimpleNamespace(stats=lambda: {"lag_p95_ms": 1.0})
    live_task = SimpleNamespace(done=lambda: False)
    monkeypatch.setattr(wake_app.app.state, "consumer", wake_consumer, raising=False)
    monkeypatch.setattr(wake_app.app.state, "consumer_task", live_task, raising=False)
    monkeypatch.setattr(
        wake_app.app.state,
        "active_turn_consumer",
        active_consumer,
        raising=False,
    )
    monkeypatch.setattr(
        wake_app.app.state, "active_turn_task", live_task, raising=False
    )
    monkeypatch.setattr(wake_app.app.state, "loop_monitor", loop_monitor, raising=False)
    monkeypatch.setattr(
        wake_app.app.state, "loop_monitor_task", live_task, raising=False
    )

    response = Response()
    payload = await wake_app.health(response)

    assert response.status_code == 503
    assert payload["status"] == "unhealthy"
    assert payload["wake_consumer"]["lagging_streams"] == 1


@pytest.mark.asyncio
async def test_detection_payload_preserves_trace_and_both_audio_clocks():
    class PublishRedis:
        def __init__(self):
            self.events = []

        async def hget(self, key, field):
            assert field == "user_id"
            return b"user-1"

        async def publish(self, *args):
            return 1

        async def xadd(self, stream, fields, **kwargs):
            self.events.append((stream, fields, kwargs))
            return b"1-0"

    redis = PublishRedis()
    consumer = WakeWordConsumer(FakeDetector(), "redis://unused", SimpleNamespace())
    consumer.redis_client = redis
    event = WakeEvent(
        client_id=ClientId.from_value("a421c9-elato"),
        session_id=SessionId.from_value("session-uuid"),
        wakeword="hermes",
        audio=b"\x00\x00" * 1600,
        arm_time=10.0,
        eot_time=10.1,
        score=0.94,
        reason="smart_turn",
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        capture_epoch=4,
        armed_at=1_770_000_001.33,
        end_of_turn_at=1_770_000_001.43,
        trigger_interval=WakeEventInterval(
            start_ms=0,
            end_ms=1330,
            started_at=1_770_000_000.0,
            ended_at=1_770_000_001.33,
        ),
        command_interval=WakeEventInterval(
            start_ms=1330,
            end_ms=1430,
            started_at=1_770_000_001.33,
            ended_at=1_770_000_001.43,
        ),
    )

    await consumer._publish_detection(event)

    _, fields, kwargs = redis.events[-1]
    payload = __import__("json").loads(fields[b"event"])
    assert kwargs == {}
    assert payload["wake_trace_id"] == event.wake_trace_id
    assert payload["capture_epoch"] == 4
    assert payload["armed_at"] == 1_770_000_001.33
    assert payload["end_of_turn_at"] == 1_770_000_001.43
    assert payload["trigger_interval"]["end_ms"] == 1330
    assert payload["command_interval"]["start_ms"] == 1330
    assert "detected_at" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "armed_word, expected_status",
    [("hermes", "detected"), ("hey_hermes", "wrong_word")],
)
async def test_session_probe_consumes_real_arm_without_dispatch_side_effects(
    armed_word, expected_status
):
    state = SimpleNamespace(
        armed=False,
        priming=False,
        armed_wakeword=None,
        arm_score=0.0,
        arm_verifier_passed=None,
        arm_verifier_score=None,
        arm_occurred_at=None,
        arm_offset_ms=None,
        arm_capture_epoch=None,
        wake_trace_id=None,
    )

    class ProbeDetector:
        wakewords = ["hey_hermes", "hermes"]
        disabled = set()
        collect_only = {"hermes"}

        async def process_frame(
            self, state, session_ref, chunk_ref, pcm, *, probe_wakeword=None
        ):
            assert probe_wakeword == "hermes"
            state.armed = True
            state.armed_wakeword = armed_word
            state.arm_score = 0.97
            state.arm_verifier_passed = True
            state.arm_verifier_score = 0.91
            state.arm_occurred_at = chunk_ref.captured_at + 0.04
            state.arm_offset_ms = 1040.0
            state.arm_capture_epoch = session_ref.capture_epoch
            state.wake_trace_id = "probe-trace"
            return None

        def reset_armed_state(self, state):
            state.armed = False
            state.armed_wakeword = None

    consumer = WakeWordConsumer(ProbeDetector(), "redis://unused", SimpleNamespace())
    session_id = SessionId.from_value("session-uuid")
    client_id = ClientId.from_value("a421c9-elato")
    consumer._states[session_id] = state
    consumer._client_ids[session_id] = client_id
    consumer._stream_tasks[session_id] = SimpleNamespace(done=lambda: False)
    consumer._on_armed = AsyncMock()
    consumer._handle_event = AsyncMock()
    probe = consumer.start_probe(
        str(client_id), str(session_id), "hermes", timeout_seconds=15
    )

    await consumer._process_detector_frame(
        state,
        AudioSessionRef(
            session_id=session_id,
            client_id=client_id,
            capture_epoch=4,
            started_at=1770000000.0,
        ),
        AudioChunkRef(
            captured_at=1770000001.0,
            time_basis="captured",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        ),
        b"\x00\x00" * 640,
    )

    result = consumer.get_probe(probe["probe_id"])
    assert result["status"] == expected_status
    assert result["detection"]["wakeword"] == armed_word
    assert result["detection"]["verifier_passed"] is True
    assert result["detection"]["verifier_score"] == 0.91
    assert state.armed is False
    consumer._on_armed.assert_not_awaited()
    consumer._handle_event.assert_not_awaited()


def test_session_probe_cancel_timeout_and_conflict_are_bounded():
    detector = SimpleNamespace(wakewords=["hermes"], disabled=set(), collect_only=set())
    consumer = WakeWordConsumer(detector, "redis://unused", SimpleNamespace())
    session_id = SessionId.from_value("session-uuid")
    client_id = ClientId.from_value("a421c9-elato")
    consumer._states[session_id] = SimpleNamespace(armed=False)
    consumer._client_ids[session_id] = client_id
    consumer._stream_tasks[session_id] = SimpleNamespace(done=lambda: False)

    first = consumer.start_probe(
        str(client_id), str(session_id), "hermes", timeout_seconds=15
    )
    with pytest.raises(ValueError, match="already active"):
        consumer.start_probe(
            str(client_id), str(session_id), "hermes", timeout_seconds=15
        )
    assert consumer.cancel_probe(first["probe_id"])["status"] == "cancelled"

    now = [100.0]
    consumer._monotonic = lambda: now[0]
    second = consumer.start_probe(
        str(client_id), str(session_id), "hermes", timeout_seconds=1
    )
    now[0] = 101.1
    assert consumer.get_probe(second["probe_id"])["status"] == "timed_out"


@pytest.mark.asyncio
async def test_probe_http_interface_exposes_start_status_and_cancel():
    class ProbeConsumer:
        def start_probe(
            self, client_id, audio_session_id, wakeword, *, timeout_seconds
        ):
            assert (client_id, audio_session_id, wakeword, timeout_seconds) == (
                "a421c9-webui-recorder",
                "audio-current",
                "hermes",
                15,
            )
            return {
                "probe_id": "probe-1",
                "client_id": client_id,
                "wakeword": wakeword,
                "status": "listening",
            }

        def get_probe(self, probe_id):
            assert probe_id == "probe-1"
            return {"probe_id": probe_id, "status": "detected"}

        def cancel_probe(self, probe_id):
            assert probe_id == "probe-1"
            return {"probe_id": probe_id, "status": "cancelled"}

    wake_app.app.state.consumer = ProbeConsumer()
    route_app = FastAPI()
    route_app.add_api_route(
        "/probes", wake_app.start_probe, methods=["POST"], status_code=201
    )
    route_app.add_api_route("/probes/{probe_id}", wake_app.get_probe, methods=["GET"])
    route_app.add_api_route(
        "/probes/{probe_id}", wake_app.cancel_probe, methods=["DELETE"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=route_app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/probes",
            json={
                "client_id": "a421c9-webui-recorder",
                "audio_session_id": "audio-current",
                "wakeword": "hermes",
            },
        )
        assert started.status_code == 201
        assert started.json()["status"] == "listening"
        assert (await client.get("/probes/probe-1")).json()["status"] == "detected"
        assert (await client.delete("/probes/probe-1")).json()["status"] == "cancelled"
