"""Exercise incremental delivery through the production Redis coordinator/facade."""

import asyncio

import opuslib
import pytest
from fakeredis.aioredis import FakeRedis

from backend.audio_contract.v2 import audio_pb2
from backend.models.audio_capabilities import VoiceCapabilities
from backend.redis_keys import ClientId, SessionId, device_downlink_channel
from backend.services import response_delivery
from backend.services.audio_stream.session_store import SessionStore
from backend.services.playback_audio import StreamingPlaybackEncoder
from backend.services.response_coordinator import (
    InvalidResponseTransition,
    ResponseCoordinator,
    StaleResponse,
)
from backend.services.voice_sessions import VoiceSessionCoordinator

pytestmark = pytest.mark.unit


@pytest.fixture
async def setup(request):
    redis = FakeRedis()
    voices = VoiceSessionCoordinator(redis)
    started = await voices.start(
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        advertised_protocol=2,
    )
    voice = await voices.ready(
        voice_session_id=started.session.voice_session_id,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        socket_id="socket-1",
        capabilities=VoiceCapabilities(
            mode="duplex_isolated",
            input_route="built_in_mic",
            output_route="headphones",
            native_sample_rate=48000,
            incremental_playback=getattr(request, "param", True),
            fallback_reason=None,
            aec={"requested": False, "available": False, "enabled": False},
            noise_suppression={
                "requested": False,
                "available": False,
                "enabled": False,
            },
        ),
    )
    await SessionStore(redis).init_session(
        "audio-1",
        user_id="user-1",
        client_id="client-1",
        connection_id="socket-1",
        stream_name="audio:stream:audio-1",
        capture_epoch=3,
        processing_profile="duplex_isolated",
        effects={},
        voice_session_id=voice.voice_session_id,
    )
    coordinator = ResponseCoordinator(redis, voices)
    generation = await coordinator.begin_turn("user-1", "client-1")
    ack = dict(
        generation=generation,
        user_id="user-1",
        client_id="client-1",
        audio_session_id="audio-1",
        voice_session_id=voice.voice_session_id,
        capture_epoch=3,
        socket_id="socket-1",
    )
    return redis, coordinator, ack


async def queue(coordinator, ack):
    return await coordinator.queue(
        **{key: value for key, value in ack.items() if key != "monotonic_timestamp_ms"},
        turn_id="turn-1",
        turn_revision=0,
        kind="speech",
        barge_in_allowed=True,
        trace_id="trace-1",
        causation_id="turn-1",
    )


async def subscribe(redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(
        str(device_downlink_channel(ClientId.from_value("client-1")))
    )
    await pubsub.get_message(timeout=1)
    return pubsub


async def next_event(pubsub):
    while True:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if msg:
            return audio_pb2.DeviceDownlinkEvent.FromString(msg["data"])
        await asyncio.sleep(0)


def test_encoder_retains_partial_frames_and_final_valid_length():
    pcm = bytes(range(256)) * 8 + b"\0\0"
    fragmented = StreamingPlaybackEncoder()
    packets = []
    for start in range(0, len(pcm), 173):
        packets.extend(fragmented.append(pcm[start : start + 173]))
    packets.extend(fragmented.finish())
    whole = StreamingPlaybackEncoder()
    assert tuple(packets) == whole.append(pcm) + whole.finish()
    assert fragmented.total_samples == len(pcm) // 2
    decoder = opuslib.Decoder(24000, 1)
    assert (
        sum(len(decoder.decode(packet, 480)) for packet in packets)
        == len(packets) * 960
    )


async def test_production_can_finish_while_playback_remains_in_progress(setup):
    redis, coordinator, ack = setup
    record = await queue(coordinator, ack)
    pubsub = await subscribe(redis)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    finished = await coordinator.finish_stream(record.response_id, total_samples=400)
    assert finished.state == "playing"
    assert finished.producer_finished
    events = [await next_event(pubsub) for _ in range(3)]
    assert events[0].playback_offer.incremental
    assert not events[1].playback.final_packet
    assert events[2].playback_finished.total_samples == 400
    done = await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="done",
        monotonic_timestamp_ms=2,
        rendered_samples=400,
        buffered_samples=0,
    )
    assert done.state == "done"
    await pubsub.aclose()


async def test_facade_plays_before_producer_completion_and_drains(setup):
    redis, coordinator, ack = setup
    pubsub = await subscribe(redis)
    release = asyncio.Event()
    producing_done = asyncio.Event()

    async def producer():
        yield b"\0" * 960
        await release.wait()
        yield b"\0" * 322  # The last valid frame is shorter than an Opus packet.
        producing_done.set()

    task = asyncio.create_task(
        response_delivery.deliver_pcm_response(
            redis,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            producer(),
            generation=ack["generation"],
            turn_id="turn-1",
        )
    )
    offer = (await asyncio.wait_for(next_event(pubsub), 2)).playback_offer
    packet = (await asyncio.wait_for(next_event(pubsub), 2)).playback
    assert packet.opus_payload and not producing_done.is_set()
    ack["response_id"] = offer.response_id.value
    await coordinator.playback(**ack, state="started", monotonic_timestamp_ms=1)
    await coordinator.playback(
        **ack,
        state="progress",
        monotonic_timestamp_ms=2,
        rendered_samples=480 - offer.pre_skip_samples,
    )
    release.set()
    assert (await next_event(pubsub)).WhichOneof("event") == "playback"
    finished = (await next_event(pubsub)).playback_finished
    assert finished.total_samples == 641
    assert not task.done()
    await coordinator.playback(
        **ack, state="done", monotonic_timestamp_ms=3, rendered_samples=641
    )
    result = await asyncio.wait_for(task, 2)
    assert result.state == "done" and result.rendered_samples == 641
    await pubsub.aclose()


@pytest.mark.parametrize(
    "rendered,buffered", [(-1, 0), (481, 0), (0, 48001), (400, 100)]
)
async def test_rejects_impossible_playback_progress(setup, rendered, buffered):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    with pytest.raises(InvalidResponseTransition):
        await coordinator.playback(
            **ack,
            response_id=record.response_id,
            state="progress",
            monotonic_timestamp_ms=2,
            rendered_samples=rendered,
            buffered_samples=buffered,
        )


async def test_cancellation_retains_final_heard_cursor_without_reviving_generation(
    setup,
):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.begin_turn("user-1", "client-1", reason="barge_in")
    stopped = await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="cancelled",
        monotonic_timestamp_ms=10,
        rendered_samples=120,
    )
    assert stopped.state == "cancelled" and stopped.rendered_samples == 120
    assert stopped.terminal_reason == "barge_in"
    with pytest.raises(StaleResponse):
        await coordinator.append_stream(record.response_id, b"late")
    with pytest.raises(StaleResponse):
        await coordinator.finish_stream(record.response_id, total_samples=480)


async def test_backpressure_waits_for_render_credit_before_publishing(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    for _ in range(100):
        await coordinator.append_stream(record.response_id, b"opus")
    append = asyncio.create_task(coordinator.append_stream(record.response_id, b"next"))
    await asyncio.sleep(0.03)
    assert not append.done()
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="progress",
        monotonic_timestamp_ms=2,
        rendered_samples=480,
    )
    await asyncio.wait_for(append, 1)
    assert (await coordinator.get(record.response_id)).sent_samples == 48480


async def test_missing_progress_and_final_drain_have_hard_deadlines(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    offered = await coordinator.append_stream(record.response_id, b"opus")
    failed = await coordinator.expire_stalled(
        record.response_id, now=offered.progress_at + 6
    )
    assert failed.state == "failed"
    assert failed.terminal_reason == "playback_progress_timeout"


async def test_facade_cancels_pending_producer_on_superseded_generation(setup):
    redis, coordinator, ack = setup
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def producer():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield b"\0" * 960
        finally:
            closed.set()

    task = asyncio.create_task(
        response_delivery.deliver_pcm_response(
            redis,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            producer(),
            generation=ack["generation"],
        )
    )
    await entered.wait()
    await coordinator.begin_turn("user-1", "client-1")
    with pytest.raises(StaleResponse):
        await asyncio.wait_for(task, 2)
    assert closed.is_set()


async def test_done_is_idempotent_and_does_not_remove_new_response(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    await coordinator.finish_stream(record.response_id, total_samples=480)
    await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="done",
        monotonic_timestamp_ms=2,
        rendered_samples=480,
    )
    next_ack = {**ack, "generation": await coordinator.begin_turn("user-1", "client-1")}
    newer = await queue(coordinator, next_ack)
    duplicate = await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="done",
        monotonic_timestamp_ms=2,
        rendered_samples=480,
    )
    assert duplicate.state == "done"
    await coordinator.assert_current(newer)


async def test_final_drain_deadline_cannot_be_extended_by_heartbeats(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    finished = await coordinator.finish_stream(record.response_id, total_samples=480)
    await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="progress",
        monotonic_timestamp_ms=2,
        rendered_samples=100,
    )
    failed = await coordinator.expire_stalled(
        record.response_id, now=finished.production_finished_at + 6
    )
    assert failed.terminal_reason == "playback_drain_timeout"


async def test_done_requires_producer_finish_and_rejects_padding_as_heard(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack, response_id=record.response_id, state="started", monotonic_timestamp_ms=1
    )
    with pytest.raises(InvalidResponseTransition):
        await coordinator.playback(
            **ack,
            response_id=record.response_id,
            state="done",
            monotonic_timestamp_ms=2,
            rendered_samples=400,
        )
    await coordinator.finish_stream(record.response_id, total_samples=400)
    with pytest.raises(InvalidResponseTransition):
        await coordinator.playback(
            **ack,
            response_id=record.response_id,
            state="done",
            monotonic_timestamp_ms=2,
            rendered_samples=480,
        )


async def test_cancelling_old_delivery_cannot_cancel_new_response(setup):
    redis, coordinator, ack = setup
    entered = asyncio.Event()

    async def producer():
        entered.set()
        await asyncio.Event().wait()
        yield b"\0" * 960

    task = asyncio.create_task(
        response_delivery.deliver_pcm_response(
            redis,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            producer(),
            generation=ack["generation"],
        )
    )
    await entered.wait()
    next_ack = {**ack, "generation": await coordinator.begin_turn("user-1", "client-1")}
    newer = await queue(coordinator, next_ack)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await coordinator.assert_current(newer)


async def test_first_audio_deadline_and_terminal_cursor_are_immutable(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id)
    failed = await coordinator.expire_stalled(
        record.response_id, now=record.created_at + 16
    )
    assert failed.terminal_reason == "first_audio_timeout"
    observed = await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="cancelled",
        monotonic_timestamp_ms=1,
    )
    assert observed.state == "failed"
    with pytest.raises(InvalidResponseTransition):
        await coordinator.playback(
            **ack,
            response_id=record.response_id,
            state="cancelled",
            monotonic_timestamp_ms=2,
        )


@pytest.mark.parametrize("setup", [False], indirect=True)
async def test_stream_cannot_start_for_finite_only_device(setup):
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    with pytest.raises(InvalidResponseTransition):
        await coordinator.open_stream(record.response_id)


@pytest.mark.parametrize("unrendered_playable", [0, 480])
async def test_phrase_pause_excludes_codec_preskip_and_held_tail(
    setup, unrendered_playable
):
    # Real browser capture: 75 packets, 156 Opus pre-skip samples and one
    # retained 480-sample final packet leave 35,364 samples playable.
    _, coordinator, ack = setup
    record = await queue(coordinator, ack)
    await coordinator.open_stream(record.response_id, pre_skip_samples=156)
    for _ in range(75):
        await coordinator.append_stream(record.response_id, b"opus")
    await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="started",
        monotonic_timestamp_ms=1,
    )
    progress = await coordinator.playback(
        **ack,
        response_id=record.response_id,
        state="progress",
        monotonic_timestamp_ms=2,
        rendered_samples=35364 - unrendered_playable,
        buffered_samples=unrendered_playable,
    )
    observed = await coordinator.expire_stalled(
        record.response_id,
        now=progress.progress_at + 6,
    )
    if unrendered_playable:
        assert observed.terminal_reason == "playback_progress_timeout"
    else:
        assert observed.state == "playing"
        assert not observed.producer_finished


async def test_ready_pcm_delivery_bounds_serial_redis_round_trips(
    setup, monkeypatch, record_property
):
    """A prepared phrase must not pay a fresh multi-read polling loop per20ms frame."""
    from redis.asyncio.client import Pipeline

    redis, coordinator, ack = setup
    pubsub = await subscribe(redis)
    exchanges = 0
    original_command = redis.execute_command
    original_immediate = Pipeline.immediate_execute_command
    original_execute = Pipeline.execute

    async def paced_command(*args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        await asyncio.sleep(0.001)
        return await original_command(*args, **kwargs)

    async def paced_immediate(self, *args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        await asyncio.sleep(0.001)
        return await original_immediate(self, *args, **kwargs)

    async def paced_execute(self, *args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        await asyncio.sleep(0.001)
        return await original_execute(self, *args, **kwargs)

    monkeypatch.setattr(redis, "execute_command", paced_command)
    monkeypatch.setattr(Pipeline, "immediate_execute_command", paced_immediate)
    monkeypatch.setattr(Pipeline, "execute", paced_execute)

    async def producer():
        for _ in range(80):
            yield bytes(960)

    task = asyncio.create_task(
        response_delivery.deliver_pcm_response(
            redis,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            producer(),
            generation=ack["generation"],
            turn_id="turn-1",
        )
    )
    sequences = []
    try:
        while True:
            event = await asyncio.wait_for(next_event(pubsub), 5)
            if event.HasField("playback_offer"):
                ack["response_id"] = event.playback_offer.response_id.value
                await coordinator.playback(
                    **ack, state="started", monotonic_timestamp_ms=1
                )
            elif event.HasField("playback"):
                sequences.append(event.playback.sequence)
            elif event.HasField("playback_finished"):
                await coordinator.playback(
                    **ack,
                    state="done",
                    monotonic_timestamp_ms=2,
                    rendered_samples=event.playback_finished.total_samples,
                )
                break
        record = await asyncio.wait_for(task, 2)
        assert record.state == "done" and record.rendered_samples == 38400
        assert sequences == list(range(81))  # Includes encoder lookahead flush.
        # Includes setup, initial ACK, final ACK and drain checks, not just append.
        record_property("redis_exchanges", exchanges)
        assert exchanges <= 81 * 11 + 100, exchanges
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pubsub.aclose()


@pytest.mark.parametrize("supersede_before_commit", [True, False])
async def test_packet_cas_fences_supersession_and_returns_its_own_commit(
    setup, monkeypatch, supersede_before_commit
):
    from redis.asyncio.client import Pipeline

    redis, coordinator, ack = setup
    record = await queue(coordinator, ack)
    pubsub = await subscribe(redis)
    await coordinator.open_stream(record.response_id)
    await next_event(pubsub)  # Offer.
    original_execute = Pipeline.execute
    superseded = False

    async def interleaved_execute(self, *args, **kwargs):
        nonlocal superseded
        publishing = any(
            str(command[0]).upper() == "PUBLISH" for command, _ in self.command_stack
        )
        if publishing and not superseded:
            superseded = True
            if supersede_before_commit:
                await coordinator.begin_turn("user-1", "client-1")
                return await original_execute(self, *args, **kwargs)
            result = await original_execute(self, *args, **kwargs)
            await coordinator.begin_turn("user-1", "client-1")
            return result
        return await original_execute(self, *args, **kwargs)

    monkeypatch.setattr(Pipeline, "execute", interleaved_execute)
    if supersede_before_commit:
        with pytest.raises(StaleResponse):
            await coordinator.append_stream(record.response_id, b"opus")
    else:
        committed = await coordinator.append_stream(record.response_id, b"opus")
        assert committed.packet_count == 1 and committed.state == "offered"
    assert superseded
    current = await coordinator.get(record.response_id)
    assert current.state == "cancelled"
    assert current.packet_count == (0 if supersede_before_commit else 1)
    with pytest.raises(StaleResponse):
        await coordinator.append_stream(record.response_id, b"later")
    await pubsub.aclose()


async def test_text_facade_uses_incremental_transport_for_native_capability(
    setup, monkeypatch
):
    """Wake/plugin text must reach the same native stream owner as engaged voice."""
    import io
    import wave
    from unittest.mock import AsyncMock

    redis, coordinator, ack = setup
    body = io.BytesIO()
    with wave.open(body, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\0" * 1282)
    monkeypatch.setattr(
        response_delivery, "synthesize_speech", AsyncMock(return_value=body.getvalue())
    )
    pubsub = await subscribe(redis)
    work = asyncio.create_task(
        response_delivery.deliver_text_response(
            redis,
            ClientId.from_value("client-1"),
            SessionId.from_value("audio-1"),
            "Which room?",
            generation=ack["generation"],
        )
    )
    try:
        offer = (await asyncio.wait_for(next_event(pubsub), 2)).playback_offer
        assert offer.incremental and offer.pre_skip_samples > 0
        ack["response_id"] = offer.response_id.value
        await coordinator.playback(**ack, state="started", monotonic_timestamp_ms=1)
        while True:
            event = await asyncio.wait_for(next_event(pubsub), 2)
            if event.WhichOneof("event") == "playback_finished":
                assert event.playback_finished.total_samples == 641
                break
            assert (
                event.WhichOneof("event") == "playback"
                and not event.playback.final_packet
            )
        await coordinator.playback(
            **ack, state="done", monotonic_timestamp_ms=2, rendered_samples=641
        )
        result = await asyncio.wait_for(work, 2)
        assert result.state == "done" and result.rendered_samples == 641
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await pubsub.aclose()
