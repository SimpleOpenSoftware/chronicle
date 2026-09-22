"""Real generation-fenced Redis publisher, with bounded provider/status I/O."""

import asyncio
from types import SimpleNamespace

import pytest
from fakeredis.aioredis import FakeRedis

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.redis_keys import response_generation, voice_processing_owner
from backend.services.interaction_modes.voice.progress import VoiceProcessingPublisher


async def next_update(subscription, predicate=lambda update: True):
    async with asyncio.timeout(1):
        while True:
            message = await subscription.get_message(
                ignore_subscribe_messages=True, timeout=0.02
            )
            if message and message["type"] == "message":
                event = pb.DeviceDownlinkEvent.FromString(message["data"])
                if event.WhichOneof("event") == "voice_processing_update":
                    if predicate(event.voice_processing_update):
                        return event.voice_processing_update
            await asyncio.sleep(0)


@pytest.fixture
async def publisher_setup():
    redis = FakeRedis(decode_responses=False)
    session = SimpleNamespace(
        user_id="user", client_id="client", interaction_id="interaction"
    )
    await redis.set(response_generation("user", "client"), 7)
    publisher = VoiceProcessingPublisher(
        redis,
        session=session,
        generation=7,
        binding=pb.CaptureBinding(),
        effect_id="effect",
        state_revision=2,
    )
    await redis.set(voice_processing_owner("interaction"), "effect")
    subscription = redis.pubsub()
    await subscription.subscribe(publisher.channel)
    yield redis, publisher, subscription
    await subscription.aclose()
    await redis.aclose()


async def test_overlapping_snapshot_and_terminal_clear_without_response_record(
    publisher_setup,
):
    redis, publisher, subscription = publisher_setup
    async with publisher:
        publisher.observe("generating_text", True)
        publisher.observe("synthesizing_speech", True)
        update = await next_update(subscription)
        assert update.generating_text and update.synthesizing_speech
        assert not update.generating_response
        # No ResponseRecord/current-response pointer is necessary to clear work.
        publisher.set_response("finished-response")
    final = await next_update(subscription, lambda update: update.finished)
    assert final.sequence > update.sequence
    assert final.response_id.value == "finished-response"
    assert not any(
        (
            final.transcribing,
            final.generating_text,
            final.synthesizing_speech,
            final.generating_response,
        )
    )
    assert publisher.task.done()
    publisher.observe("generating_text", True)
    assert not publisher.snapshot.generating_text


@pytest.mark.parametrize("changed", ["generation", "effect"])
async def test_ownership_change_between_read_and_publish_is_atomic(
    publisher_setup, monkeypatch, changed
):
    redis, publisher, subscription = publisher_setup
    original_pipeline = redis.pipeline
    raced = False

    def pipeline(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        original_mget = pipe.mget

        async def mget(*keys):
            nonlocal raced
            value = await original_mget(*keys)
            if not raced:
                raced = True
                if changed == "generation":
                    await redis.incr(publisher.generation_key)
                else:
                    await redis.set(publisher.owner_key, "replacement-effect")
            return value

        pipe.mget = mget
        return pipe

    monkeypatch.setattr(redis, "pipeline", pipeline)
    publisher.observe("generating_text", True)
    assert not await publisher._publish()
    # The watched generation changes after GET; EXEC must publish nothing.
    assert raced
    assert (
        await subscription.get_message(ignore_subscribe_messages=True, timeout=0.03)
        is None
    )


async def test_slow_publish_has_bounded_cleanup_and_single_pending_snapshot(
    publisher_setup, monkeypatch
):
    redis, publisher, subscription = publisher_setup
    started = asyncio.Event()
    active = 0
    max_active = 0

    async def stalled():
        nonlocal active, max_active
        active += 1
        max_active = max(active, max_active)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(publisher, "_publish", stalled)
    async with asyncio.timeout(0.5):
        async with publisher:
            publisher.observe("generating_text", True)
            await started.wait()
            for index in range(10000):
                publisher.observe("synthesizing_speech", bool(index % 2))
    assert max_active == 1
    assert active == 0
    assert publisher.task.done()
    assert publisher.snapshot.finished
