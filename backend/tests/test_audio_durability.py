"""Durability invariants from Redis acceptance through Mongo persistence."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.protobuf import timestamp_pb2

from backend.audio_contract.v2 import audio_pb2
from backend.services.audio_stream.durability import (
    PersistenceOutcome,
    PersistenceRuntimeState,
    ReadPhase,
    SessionPhase,
)
from backend.services.audio_stream.producer import AudioStreamProducer, SessionBuffer
from backend.services.audio_stream.session_store import SessionStatus
from backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
)
from backend.workers import audio_jobs, transcription_jobs

pytestmark = pytest.mark.unit


class _QueryField:
    def __eq__(self, value):
        return value


class _DurabilityStore:
    def __init__(self, redis_client):
        self._statuses = iter(redis_client.statuses)

    async def get_audio_format(self, session_id):
        return 16000, 1, 2

    async def get_last_chunk_at(self, session_id):
        return time.time()

    async def is_websocket_connected(self, session_id):
        return True

    async def get_status(self, session_id):
        return next(self._statuses, SessionStatus.FINISHED)


class _DurabilityRedis:
    def __init__(self, *, statuses, pending_message=None, new_message=None):
        self.statuses = statuses
        self.pending_message = pending_message
        self.new_message = new_message
        self.pending_delivered = False
        self.new_delivered = False
        self.read_cursors = []
        self.acked = []
        self.delivered_count = 0
        self.group = "audio_persistence"
        self.stream = b"audio:stream:session-1"

    async def xgroup_create(self, stream, group, *args, **kwargs):
        self.group = group
        self.stream = stream.encode() if isinstance(stream, str) else stream
        return True

    async def xreadgroup(self, group, consumer, streams, **kwargs):
        cursor = next(iter(streams.values()))
        self.read_cursors.append(cursor)
        stream = self.stream

        if cursor != ">" and self.pending_message and not self.pending_delivered:
            self.pending_delivered = True
            delivered = (
                self.pending_message
                if isinstance(self.pending_message, list)
                else [self.pending_message]
            )
            self.delivered_count += len(delivered)
            return [(stream, delivered)]
        if cursor == ">" and self.new_message and not self.new_delivered:
            self.new_delivered = True
            delivered = (
                self.new_message
                if isinstance(self.new_message, list)
                else [self.new_message]
            )
            self.delivered_count += len(delivered)
            return [(stream, delivered)]
        # redis-py/Redis returns a truthy stream envelope with an empty entry list
        # for a pending cursor that has nothing owned by this consumer.
        return [(stream, [])]

    async def xack(self, stream, group, *message_ids):
        self.acked.extend(message_ids)
        return len(message_ids)

    async def execute_command(self, *args):
        pending = max(0, self.delivered_count - len(self.acked))
        return [
            [
                b"name",
                self.group.encode(),
                b"pending",
                pending,
                b"lag",
                0,
            ]
        ]

    async def delete(self, *keys):
        return len(keys)


class _PersistedCapture:
    capture_session_id = "session-1"
    user_id = "user-1"
    client_id = "client-1"
    status = "active"
    time_basis = "received"
    ended_at = None
    failure = None

    async def save(self):
        return None

    async def set(self, updates):
        for field, value in updates.items():
            setattr(self, field, value)


class _EmptyFind:
    def sort(self, *_args, **_kwargs):
        return self

    async def first_or_none(self):
        return None


def _audio_message(message_id=b"1-0"):
    return (
        message_id,
        {
            b"audio_data": b"\x00\x01" * 160000,
            b"chunk_id": b"00001",
            b"capture_session_id": b"session-1",
            b"captured_at": b"1770000000.0",
            b"time_basis": b"received",
        },
    )


def _v2_message(message_id=b"1-0"):
    captured_at = timestamp_pb2.Timestamp()
    captured_at.FromDatetime(datetime.fromtimestamp(1_770_000_000.0, tz=timezone.utc))
    event = audio_pb2.CaptureStreamEvent(
        frame=audio_pb2.CanonicalPcmFrame(
            binding=audio_pb2.CaptureBinding(
                capture_session_id=audio_pb2.CaptureSessionId(value="session-1")
            ),
            sequence=1,
            captured_at=captured_at,
            delivery_class=audio_pb2.DELIVERY_CLASS_RECOVERED,
            pcm_s16le=b"\x00\x01" * 160000,
        )
    )
    return message_id, {b"event": event.SerializeToString()}


def _patch_persistence_dependencies(monkeypatch, audio_chunk_model):
    persisted_capture = _PersistedCapture()

    class FakeCapture:
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=persisted_capture)

    monkeypatch.setattr(audio_jobs, "SessionStore", _DurabilityStore)
    monkeypatch.setattr(audio_jobs, "AudioChunkDocument", audio_chunk_model)
    monkeypatch.setattr(audio_jobs, "AudioCaptureSession", FakeCapture)
    monkeypatch.setattr(audio_jobs, "get_current_job", lambda: object())
    monkeypatch.setattr(audio_jobs, "check_job_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(
        audio_jobs, "encode_pcm_to_opus", AsyncMock(return_value=b"opus")
    )
    return persisted_capture


@pytest.mark.asyncio
async def test_mongo_failure_never_acks_redis_audio(monkeypatch):
    class FailingAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            raise RuntimeError("mongo unavailable")

    _patch_persistence_dependencies(monkeypatch, FailingAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=_audio_message(),
    )

    with pytest.raises(RuntimeError, match="mongo unavailable"):
        await audio_jobs.audio_streaming_persistence_job.__wrapped__(
            "session-1",
            "user-1",
            "client-1",
            redis_client=redis,
        )

    assert redis.acked == [], "audio must stay pending until Mongo commits it"


@pytest.mark.asyncio
async def test_restarted_worker_replays_pending_audio_before_new_entries(monkeypatch):
    inserted = AsyncMock()

    class SuccessfulAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            await inserted()

    _patch_persistence_dependencies(monkeypatch, SuccessfulAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        pending_message=_audio_message(b"pending-1"),
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert redis.read_cursors[0] != ">", "pending entries must be replayed first"
    inserted.assert_awaited_once()
    assert redis.acked == [b"pending-1"]


@pytest.mark.asyncio
async def test_v2_recovered_frame_commits_from_typed_durable_stream(monkeypatch):
    inserted = []

    class SuccessfulAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            inserted.append(self)

    capture = _patch_persistence_dependencies(monkeypatch, SuccessfulAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=_v2_message(),
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        contract_version=2,
        redis_client=redis,
    )

    assert len(inserted) == 1
    assert inserted[0].source_stream == "audio:v2:durable:session-1"
    assert inserted[0].captured_at.timestamp() == 1_770_000_000.0
    assert inserted[0].original_size == 320000
    assert redis.acked == [b"1-0"]
    assert capture.time_basis == "captured"


@pytest.mark.asyncio
async def test_wal_messages_stay_owned_by_the_capture_session(monkeypatch):
    inserted = []

    class OwnedAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            inserted.append(self)

    _patch_persistence_dependencies(monkeypatch, OwnedAudioChunk)
    first = _audio_message(b"1-0")
    second_id, second_fields = _audio_message(b"2-0")
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=[first, (second_id, second_fields)],
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert [chunk.capture_session_id for chunk in inserted] == [
        "session-1",
        "session-1",
    ]
    assert [chunk.sequence for chunk in inserted] == [0, 1]
    assert [chunk.source_message_ids for chunk in inserted] == [["1-0"], ["2-0"]]
    assert redis.acked == [b"1-0", b"2-0"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_captured_at", "expected_durations", "expected_source_ids"),
    [
        pytest.param(
            1_770_000_045.0,
            [1.0, 1.0],
            [["1-0"], ["2-0"]],
            id="playback-gap",
        ),
        pytest.param(
            1_770_000_001.2,
            [2.0],
            [["1-0", "2-0"]],
            id="sub-tolerance-jitter",
        ),
    ],
)
async def test_persistence_respects_recorded_capture_continuity(
    monkeypatch,
    second_captured_at,
    expected_durations,
    expected_source_ids,
):
    inserted = []

    class DiscontinuousAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=None)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            inserted.append(self)

    def one_second(message_id: bytes, captured_at: float):
        return (
            message_id,
            {
                b"audio_data": b"\x00\x01" * 16000,
                b"chunk_id": b"00001",
                b"capture_session_id": b"session-1",
                b"captured_at": str(captured_at).encode(),
                b"time_basis": b"recorded",
            },
        )

    capture = _patch_persistence_dependencies(monkeypatch, DiscontinuousAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        new_message=[
            one_second(b"1-0", 1_770_000_000.0),
            one_second(b"2-0", second_captured_at),
        ],
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    assert [chunk.duration for chunk in inserted] == expected_durations
    assert [chunk.source_message_ids for chunk in inserted] == expected_source_ids
    assert inserted[0].captured_at.timestamp() == 1_770_000_000.0
    if len(inserted) == 2:
        assert inserted[1].captured_at.timestamp() == second_captured_at
    assert redis.acked == [b"1-0", b"2-0"]
    assert capture.time_basis == "recorded"


class _ProducerPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending_xadd = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def watch(self, *keys):
        return None

    async def get(self, key):
        return self.redis.owner

    async def hget(self, key, field):
        return self.redis.status

    async def unwatch(self):
        return None

    def multi(self):
        return None

    def xadd(self, stream, fields):
        self.pending_xadd = (stream, fields)

    async def execute(self):
        stream, fields = self.pending_xadd
        return [await self.redis.xadd(stream, fields)]


class _ProducerRedis:
    def __init__(self, error=None, owner=b"conversation-1", status=b"active"):
        self.error = error
        self.owner = owner
        self.status = status
        self.calls = []

    def pipeline(self, transaction=True):
        return _ProducerPipeline(self)

    async def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields, kwargs))
        if self.error:
            raise self.error
        return b"1-0"


@pytest.mark.asyncio
async def test_xadd_failure_keeps_audio_in_producer_buffer():
    redis = _ProducerRedis(RuntimeError("redis unavailable"))
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000  # exactly one 250 ms producer chunk

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await producer.add_audio_chunk(
            audio,
            "session-1",
            "user-1",
            "client-1",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )

    buffer = producer.session_buffers["session-1"]
    assert buffer.buffer == audio
    assert buffer.chunk_count == 0


@pytest.mark.asyncio
async def test_audio_stream_publish_does_not_trim_unpersisted_entries():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )

    await producer.add_audio_chunk(
        b"\x00\x01" * 4000,
        "session-1",
        "user-1",
        "client-1",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    assert "maxlen" not in redis.calls[0][2]
    assert redis.calls[0][1][b"capture_session_id"] == b"session-1"
    assert b"conversation_id" not in redis.calls[0][1]


@pytest.mark.asyncio
async def test_explicit_capture_timestamp_marks_wal_audio_as_recorded():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )

    await producer.add_audio_chunk(
        b"\x00\x01" * 4000,
        "session-1",
        "user-1",
        "client-1",
        sample_rate=16000,
        channels=1,
        sample_width=2,
        captured_at=1_770_000_000.125,
    )

    fields = redis.calls[0][1]
    assert fields[b"captured_at"] == b"1770000000.125"
    assert fields[b"time_basis"] == b"recorded"


@pytest.mark.asyncio
async def test_interactive_native_timestamp_marks_wal_audio_as_captured():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
        time_basis="captured",
    )

    await producer.add_audio_chunk(
        b"\x00\x01" * 4000,
        "session-1",
        "user-1",
        "client-1",
        sample_rate=16000,
        channels=1,
        sample_width=2,
        captured_at=1_770_000_000.125,
        time_basis="captured",
    )

    fields = redis.calls[0][1]
    assert fields[b"captured_at"] == b"1770000000.125"
    assert fields[b"time_basis"] == b"captured"


@pytest.mark.asyncio
async def test_producer_flushes_partial_wal_chunk_before_capture_gap():
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    tenth_second = b"\x00\x01" * 1600

    await producer.add_audio_chunk(
        tenth_second,
        "session-1",
        "user-1",
        "client-1",
        captured_at=1_770_000_000.0,
    )
    await producer.add_audio_chunk(
        tenth_second,
        "session-1",
        "user-1",
        "client-1",
        captured_at=1_770_000_045.0,
    )

    assert len(redis.calls) == 1
    assert redis.calls[0][1][b"audio_data"] == tenth_second
    assert redis.calls[0][1][b"captured_at"] == b"1770000000.0"
    buffer = producer.session_buffers["session-1"]
    assert buffer.buffer == tenth_second
    assert buffer.captured_at == 1_770_000_045.0


@pytest.mark.asyncio
async def test_capture_accepts_audio_without_a_conversation_owner():
    redis = _ProducerRedis(owner=None)
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000

    await producer.add_audio_chunk(
        audio,
        "session-1",
        "user-1",
        "client-1",
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )

    assert producer.session_buffers["session-1"].buffer == b""
    assert redis.calls[0][1][b"capture_session_id"] == b"session-1"


@pytest.mark.asyncio
async def test_terminal_session_rejects_late_audio_without_xadd():
    redis = _ProducerRedis(status=b"finished")
    producer = AudioStreamProducer(redis)
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
    )
    audio = b"\x00\x01" * 4000

    with pytest.raises(RuntimeError, match="is not active"):
        await producer.add_audio_chunk(
            audio,
            "session-1",
            "user-1",
            "client-1",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )

    assert producer.session_buffers["session-1"].buffer == audio
    assert redis.calls == []


@pytest.mark.asyncio
async def test_batch_materialization_creates_detected_claim_after_speech(monkeypatch):
    semantic = SimpleNamespace(insert=AsyncMock())
    create = Mock(return_value=semantic)
    apply_claim = AsyncMock(return_value=semantic)

    class Conversations:
        segmentation_key = _QueryField()
        find_one = AsyncMock(return_value=None)

    first_range = SimpleNamespace(started_at=1, ended_at=2)
    second_range = SimpleNamespace(started_at=3, ended_at=4)
    monkeypatch.setattr(transcription_jobs, "Conversation", Conversations)
    monkeypatch.setattr(transcription_jobs, "create_conversation", create)
    monkeypatch.setattr(transcription_jobs, "apply_audio_ranges", apply_claim)

    resolved = await transcription_jobs.materialize_batch_conversation(
        "session-1", "user-1", "client-1", [first_range, second_range]
    )

    assert resolved is semantic
    create.assert_called_once_with(
        user_id="user-1",
        client_id="client-1",
        title=transcription_jobs.TITLE_NOT_GENERATED,
        summary="Processing audio with offline transcription...",
        origin="detected",
        started_at=1,
        ended_at=4,
        segmentation_key="batch-fallback:session-1:v1",
    )
    apply_claim.assert_awaited_once_with(
        semantic, [first_range, second_range], save=False
    )
    semantic.insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_fallback_entrypoint_materializes_all_capture_ranges(monkeypatch):
    from backend.services.transcription.context import TranscriptionContext

    first_range = SimpleNamespace(
        range_id="range-1", duration_seconds=20.0, chunk_ids=["chunk-1", "chunk-2"]
    )
    second_range = SimpleNamespace(
        range_id="range-2", duration_seconds=10.0, chunk_ids=["chunk-3"]
    )
    artifact = SimpleNamespace(artifact_id="artifact-1")
    conversation = SimpleNamespace(conversation_id="conversation-1")
    materialize = AsyncMock(return_value=conversation)
    process = AsyncMock(return_value={"success": True})
    post_jobs = {"speaker_recognition": "speaker-1"}

    monkeypatch.setattr(
        transcription_jobs.Conversation,
        "find_one",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        transcription_jobs, "is_transcription_available", lambda **_kwargs: True
    )
    monkeypatch.setattr(
        transcription_jobs,
        "claim_entire_capture",
        AsyncMock(return_value=[first_range, second_range]),
    )
    monkeypatch.setattr(
        transcription_jobs,
        "gather_transcription_context",
        AsyncMock(return_value=TranscriptionContext()),
    )
    monkeypatch.setattr(
        transcription_jobs,
        "transcribe_audio_range",
        AsyncMock(
            return_value={
                "text": "hello world",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello world"}],
                "words": [
                    {"start": 0.0, "end": 0.5, "word": "hello"},
                    {"start": 0.5, "end": 1.0, "word": "world"},
                ],
                "provider_name": "smallest",
                "provider_capabilities": {"word_timestamps": True},
                "wav_size": 100,
            }
        ),
    )
    monkeypatch.setattr(
        transcription_jobs,
        "validate_and_normalize_transcript_timing",
        lambda segments, words, **_kwargs: (segments, words),
    )
    monkeypatch.setattr(
        transcription_jobs,
        "persist_transcript_artifact",
        AsyncMock(return_value=artifact),
    )
    monkeypatch.setattr(
        transcription_jobs,
        "analyze_speech",
        lambda _data: {"has_speech": True},
    )
    monkeypatch.setattr(
        transcription_jobs, "materialize_batch_conversation", materialize
    )
    monkeypatch.setattr(transcription_jobs, "process_transcription_result", process)
    monkeypatch.setattr(
        transcription_jobs,
        "start_post_conversation_jobs",
        Mock(return_value=post_jobs),
    )

    result = await transcription_jobs.transcription_fallback_check_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=SimpleNamespace(),
    )

    assert result["status"] == "batch_fallback_completed"
    materialize.assert_awaited_once_with(
        "session-1", "user-1", "client-1", [first_range, second_range]
    )
    process.assert_awaited_once()
    assert process.await_args.kwargs["transcript_artifact"] is artifact


class _LaggingStreamRedis:
    def __init__(self):
        self.deleted = []

    async def exists(self, stream_name):
        return True

    async def hget(self, key, field):
        return b"finished"

    async def execute_command(self, *args):
        return [
            [
                b"name",
                b"streaming-transcription-v2",
                b"pending",
                0,
                b"lag",
                0,
            ],
            [b"name", b"wakeword-v2", b"pending", 0, b"lag", 4],
            [b"name", b"interactive-turn-v2", b"pending", 0, b"lag", 0],
        ]

    async def delete(self, stream_name):
        self.deleted.append(stream_name)


@pytest.mark.asyncio
async def test_realtime_stream_with_unconsumed_wake_lag_is_not_deleted():
    redis = _LaggingStreamRedis()
    consumer = object.__new__(StreamingTranscriptionConsumer)
    consumer.redis_client = redis
    consumer.group_name = "streaming-transcription-v2"

    await consumer._try_delete_finished_stream("audio:v2:realtime:client-1")

    assert redis.deleted == []


def test_persistence_runtime_state_has_only_explicit_forward_transitions():
    state = PersistenceRuntimeState()
    assert state.session is SessionPhase.ACTIVE
    assert state.reader is ReadPhase.RECOVERING_PENDING
    assert state.outcome is PersistenceOutcome.RUNNING

    with pytest.raises(RuntimeError, match="while the session is active"):
        state.complete()

    state.pending_recovered()
    state.begin_draining()
    state.complete()
    assert state.outcome is PersistenceOutcome.COMPLETE

    with pytest.raises(RuntimeError, match="already complete"):
        state.fail()


@pytest.mark.asyncio
async def test_replay_after_mongo_commit_before_xack_is_idempotent(monkeypatch):
    source_ids = ["pending-1"]
    existing = SimpleNamespace(
        capture_session_id="session-1",
        user_id="user-1",
        capture_source_id="client-1",
        sequence=0,
        original_size=320000,
        compressed_size=4,
        duration=10.0,
        source_message_ids=source_ids,
    )
    inserted = AsyncMock()

    class IdempotentAudioChunk:
        source_stream = _QueryField()
        source_first_message_id = _QueryField()
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=existing)
        find = staticmethod(lambda *_args, **_kwargs: _EmptyFind())

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def insert(self):
            await inserted()

    _patch_persistence_dependencies(monkeypatch, IdempotentAudioChunk)
    redis = _DurabilityRedis(
        statuses=[SessionStatus.ACTIVE, SessionStatus.FINISHED],
        pending_message=_audio_message(b"pending-1"),
    )

    await audio_jobs.audio_streaming_persistence_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        redis_client=redis,
    )

    inserted.assert_not_awaited()
    assert redis.acked == [b"pending-1"]


@pytest.mark.asyncio
async def test_finalization_flushes_residual_audio_before_terminal_state(monkeypatch):
    class Capture:
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=_PersistedCapture())

    monkeypatch.setattr(
        "backend.services.audio_stream.producer.AudioCaptureSession",
        Capture,
    )
    redis = _ProducerRedis()
    producer = AudioStreamProducer(redis)
    producer.store = SimpleNamespace(
        get_audio_format=AsyncMock(return_value=(16000, 1, 2)),
        mark_finalizing=AsyncMock(),
    )
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
        buffer=b"residual-pcm",
    )

    await producer.finalize_session("session-1", "websocket_disconnect")

    assert redis.calls[0][1][b"audio_data"] == b"residual-pcm"
    assert redis.calls[1][1][b"chunk_id"] == b"END"
    producer.store.mark_finalizing.assert_awaited_once()
    assert "session-1" not in producer.session_buffers


@pytest.mark.asyncio
async def test_finalization_append_error_does_not_advance_or_discard_buffer(
    monkeypatch,
):
    class Capture:
        capture_session_id = _QueryField()
        find_one = AsyncMock(return_value=_PersistedCapture())

    monkeypatch.setattr(
        "backend.services.audio_stream.producer.AudioCaptureSession",
        Capture,
    )
    redis = _ProducerRedis(RuntimeError("redis unavailable"))
    producer = AudioStreamProducer(redis)
    producer.store = SimpleNamespace(
        get_audio_format=AsyncMock(return_value=(16000, 1, 2)),
        mark_finalizing=AsyncMock(),
    )
    producer.update_session_chunk_count = AsyncMock()
    producer.session_buffers["session-1"] = SessionBuffer(
        user_id="user-1",
        client_id="client-1",
        stream_name="audio:stream:session-1",
        buffer=b"residual-pcm",
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await producer.finalize_session("session-1", "websocket_disconnect")

    producer.store.mark_finalizing.assert_not_awaited()
    assert producer.session_buffers["session-1"].buffer == b"residual-pcm"
