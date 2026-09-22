"""Production control/router/outbox entrypoints with only provider I/O faked."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.models.audio_capabilities import VoiceCapabilities
from backend.services.audio_stream.session_store import SessionStore
from backend.services.interaction_modes.committed_turns import (
    CommittedAudioTurn,
    CommittedTurnRouter,
)
from backend.services.interaction_modes.contracts import AudioInterval
from backend.services.interaction_modes.registry import InteractionRegistry
from backend.services.interaction_modes.store import VOICE_EFFECT_STREAM
from backend.services.interaction_modes.voice.engine_types import (
    VoiceAudio,
    VoiceToolCall,
)
from backend.services.interaction_modes.voice.runtime import (
    VoiceConversationRuntime,
    _binding,
    _id,
)
from backend.services.interaction_modes.voice.settings import VoiceSettings
from backend.services.interaction_modes.voice.tools import VoiceTools
from backend.services.interaction_modes.voice.worker import (
    GROUP,
    VoiceEffectWorker,
    release_lease,
)
from backend.services.response_coordinator import StaleResponse
from backend.services.wakeword.activations import WakeActivation, WakeActivationStore


@pytest.fixture
async def setup():
    redis = FakeRedis(decode_responses=False)
    transcriber = AsyncMock(return_value="Hello Chronicle")
    runtime = VoiceConversationRuntime(
        redis,
        dialogue=SimpleNamespace(
            open=AsyncMock(return_value=("thread", [])),
            close=AsyncMock(),
            pending_output=AsyncMock(return_value=None),
            guard=AsyncMock(return_value=AsyncMock()),
            input=AsyncMock(return_value=None),
            output=AsyncMock(return_value=None),
            heartbeat=AsyncMock(),
            hints=AsyncMock(return_value=None),
            tool=AsyncMock(),
        ),
        settings=VoiceSettings(),
        tools=VoiceTools(),
        transcript_assembler=SimpleNamespace(exact_transcriber=transcriber),
    )
    voice = await runtime.voices.start(
        user_id="user",
        client_id="client",
        audio_session_id="audio",
        capture_epoch=1,
        socket_id="socket",
        advertised_protocol=2,
    )
    voice = await runtime.voices.ready(
        voice_session_id=voice.session.voice_session_id,
        user_id="user",
        client_id="client",
        audio_session_id="audio",
        capture_epoch=1,
        socket_id="socket",
        capabilities=VoiceCapabilities(
            mode="duplex_isolated",
            input_route="unknown",
            output_route="headphones",
            native_sample_rate=48000,
            aec={"requested": False, "available": False, "enabled": False},
            noise_suppression={
                "requested": False,
                "available": False,
                "enabled": False,
            },
            incremental_playback=True,
            fallback_reason=None,
        ),
    )
    await SessionStore(redis).init_session(
        "audio",
        user_id="user",
        client_id="client",
        stream_name="capture",
        connection_id="socket",
        capture_epoch=1,
        processing_profile="source_native",
        effects={
            "aec": {"reporting": "unreported"},
            "noise_suppression": {"reporting": "unreported"},
        },
        voice_session_id=voice.voice_session_id,
        memory_space_id="space",
    )
    yield redis, runtime, voice, transcriber
    await runtime.tools.aclose()
    await redis.aclose()


async def command(runtime, voice, action=pb.CONVERSATION_ACTION_START, **kwargs):
    if (
        action in {pb.CONVERSATION_ACTION_END, pb.CONVERSATION_ACTION_CANCEL_TASK}
        and "interaction_id" not in kwargs
    ):
        active = await runtime.store.get_active("user", "client")
        kwargs["interaction_id"] = active.interaction_id if active else ""
    return await runtime.control(
        pb.ConversationCommand(binding=_binding(voice), action=action, **kwargs),
        user_id="user",
        client_id="client",
        socket_id="socket",
        event_id=str(uuid.uuid4()),
    )


def turn(voice, identity="turn", start=1000):
    return CommittedAudioTurn(
        interval=AudioInterval(
            audio_session_id="audio",
            capture_epoch=1,
            start_ms=start,
            end_ms=start + 1000,
            voice_session_id=voice.voice_session_id,
            turn_id=identity,
            turn_revision=0,
        ),
        start_sequence=0,
        end_sequence=49,
        pcm=b"\x01\x00" * 16000,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )


async def effects(redis, kind):
    out = []
    for identity, fields in await redis.xrange(VOICE_EFFECT_STREAM):
        effect = pb.VoiceEffect.FromString(fields[b"effect"])
        if effect.kind == kind:
            out.append((identity, fields, effect))
    return out


async def test_control_single_owner_binding_and_end_keeps_capture(setup):
    redis, runtime, voice, _ = setup
    first = await command(runtime, voice)
    again = await command(runtime, voice)
    assert first.interaction_id == again.interaction_id
    with pytest.raises(ValueError, match="end this conversation"):
        await command(runtime, voice, engine=pb.SPEECH_ENGINE_REALTIME)
    stale = pb.ConversationCommand(
        binding=_binding(voice), action=pb.CONVERSATION_ACTION_END
    )
    stale.binding.capture_epoch += 1
    with pytest.raises(Exception, match="current incremental"):
        await runtime.control(
            stale,
            user_id="user",
            client_id="client",
            socket_id="socket",
            event_id="stale",
        )
    ended = await command(runtime, voice, pb.CONVERSATION_ACTION_END)
    assert ended.phase == pb.CONVERSATION_PHASE_ENDED
    assert await runtime.store.get_active("user", "client") is None
    assert (await SessionStore(redis).read("audio")).connection_id == "socket"


async def test_router_durably_enqueues_before_stt_and_retry_is_single_owner(setup):
    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    router = CommittedTurnRouter(redis, InteractionRegistry(), voice_runtime=runtime)
    audio = turn(voice)
    fields = {
        "turn_id": "turn",
        "turn_revision": "0",
        "voice_session_id": voice.voice_session_id,
        "audio_session_id": "audio",
        "capture_epoch": "1",
        "start_sequence": "0",
        "end_sequence": "49",
        "started_at_ms": "1000",
        "ended_at_ms": "2000",
        "sample_rate": "16000",
        "channels": "1",
        "sample_width": "2",
        "pcm": audio.pcm,
    }
    first = await router.route(fields)
    second = await router.route(fields)
    assert first.accepted and second.consumed and not second.accepted
    transcriber.assert_not_awaited()
    pending = await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE)
    assert len(pending) == 1
    saved = await runtime.store.get(first.interaction_id)
    assert (
        saved.plugin_state["turns"][pending[0][2].task_id]["audio_interval"]["start_ms"]
        == 1000
    )
    assert (
        await redis.hget("interaction:voice:turn:" + pending[0][2].task_id, "pcm")
        == audio.pcm
    )


async def test_unaddressed_audio_never_starts_but_owned_wake_retry_recovers(setup):
    redis, runtime, voice, _ = setup
    audio = turn(voice)
    assert (await runtime.enqueue_committed(audio, voice)).reason == "not_addressed"
    activation = WakeActivation(
        wake_trace_id=str(uuid.uuid4()),
        user_id="user",
        client_id="client",
        audio_session_id="audio",
        capture_epoch=1,
        wakeword="hermes",
        armed_at=1,
        end_of_turn_at=2,
        command_start_ms=1100,
        command_end_ms=1900,
    )
    store = WakeActivationStore(redis)
    await store.register(activation)
    work_id = _id("voice-turn", "user", "client", "audio", 1, "turn", 0)
    assert await store.claim(audio.interval, owner_id=work_id) == activation
    assert (await runtime.enqueue_committed(audio, voice)).accepted
    assert len(await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE)) == 1


async def test_effect_entrypoint_delivers_and_records_only_heard_phrase(setup):
    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)

    class Engine:
        async def generate(self, **kwargs):
            assert kwargs["text"] == "Hello Chronicle"
            yield VoiceAudio(b"\x00\x00" * 10, 0, "Heard.", 0, True)
            yield VoiceAudio(b"\x00\x00" * 10, 1, "Unheard.", 10, True)

    runtime.engine = Engine()
    records = {}

    async def deliver(redis, client, session, producer, **kwargs):
        response = SimpleNamespace(response_id="own-response")
        await kwargs["on_queued"](response)
        assert len([chunk async for chunk in producer]) == 2
        records["own-response"] = SimpleNamespace(
            rendered_samples=10, terminal_ack_state="cancelled"
        )

    runtime.deliver = deliver
    runtime.responses.get = AsyncMock(
        side_effect=lambda identity: records.get(identity)
    )
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    await worker.handle(identity, fields)
    transcriber.assert_awaited_once()
    session = await runtime.store.get(effect.interaction_id)
    assert session.plugin_state["history"][-1]["content"] == "Heard."
    assert session.plugin_state["response"]["rendered_samples"] == 10
    await worker.handle(identity, fields)
    assert transcriber.await_count == 1

    # A new turn can fail before begin() replaces the previous response state.
    transcriber.side_effect = TimeoutError("transcription deadline exceeded")
    assert (
        await runtime.enqueue_committed(
            turn(voice, identity="next-turn", start=3000), voice
        )
    ).accepted
    next_identity, next_fields, next_effect = (
        await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE)
    )[-1]
    await worker.handle(next_identity, next_fields)
    failed = await runtime.store.get(next_effect.interaction_id)
    assert failed.plugin_state["response"]["response_id"] == ""
    assert failed.plugin_state["response"]["rendered_samples"] == 0
    assert records["own-response"].rendered_samples == 10
    assert failed.plugin_state["turns"][effect.task_id]["heard_text"] == "Heard."


@pytest.mark.parametrize("failure", ["rate_limit", "timeout", "engine_setup"])
async def test_provider_setup_failure_settles_turn_without_repeating_paid_stt(
    setup, failure
):
    import httpx

    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    if failure == "rate_limit":
        request = httpx.Request("POST", "https://api.smallest.ai/test")
        response = httpx.Response(429, request=request)
        transcriber.side_effect = httpx.HTTPStatusError(
            "rate limit", request=request, response=response
        )
    elif failure == "timeout":
        transcriber.side_effect = TimeoutError("provider deadline exceeded")
    else:
        runtime.engine_factory = lambda _: (_ for _ in ()).throw(
            ValueError("bad configuration")
        )

    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    await worker.handle(identity, fields)
    session = await runtime.store.get(effect.interaction_id)
    assert session.phase == "listening"
    assert session.plugin_state["turns"][effect.task_id]["status"] == "failed"
    assert session.plugin_state["detail"].startswith(
        "engine_setup_failed:" if failure == "engine_setup" else "transcription_failed:"
    )
    assert await redis.exists("interaction:voice:effect-done:" + effect.effect_id)
    assert await SessionStore(redis).read("audio") is not None
    await worker.handle(identity, fields)
    transcriber.assert_awaited_once()


async def test_transcription_generation_store_failure_remains_recoverable(setup):
    from redis.exceptions import ConnectionError

    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    transcriber.side_effect = ConnectionError("generation store unavailable")
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    await worker.handle(identity, fields)
    assert not await redis.exists("interaction:voice:effect-done:" + effect.effect_id)
    assert await redis.xrange(VOICE_EFFECT_STREAM, min=identity, max=identity)
    assert (await runtime.store.get(effect.interaction_id)).plugin_state["turns"][
        effect.task_id
    ]["status"] == "queued"


@pytest.mark.parametrize(
    "failure", ["missing", "bad_json", "missing_pcm", "wrong_interval", "redis"]
)
async def test_invalid_durable_input_settles_without_stt_but_transport_is_recoverable(
    setup, failure, monkeypatch
):
    from redis.exceptions import ConnectionError

    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    key = f"interaction:voice:turn:{effect.task_id}"
    if failure == "missing":
        await redis.delete(key)
    elif failure == "bad_json":
        await redis.hset(key, "metadata", "{broken")
    elif failure == "missing_pcm":
        await redis.hdel(key, "pcm")
    elif failure == "wrong_interval":
        metadata = json.loads(await redis.hget(key, "metadata"))
        metadata["interval"]["audio_session_id"] = "another-capture"
        await redis.hset(key, "metadata", json.dumps(metadata))
    else:
        monkeypatch.setattr(
            redis,
            "hgetall",
            AsyncMock(side_effect=ConnectionError("Redis unavailable")),
        )

    await worker.handle(identity, fields)
    if failure == "redis":
        monkeypatch.undo()
    session = await runtime.store.get(effect.interaction_id)
    transcriber.assert_not_awaited()
    assert await SessionStore(redis).read("audio") is not None
    if failure == "redis":
        assert session.phase == "thinking"
        assert session.plugin_state["turns"][effect.task_id]["status"] == "queued"
        assert not await redis.exists(
            "interaction:voice:effect-done:" + effect.effect_id
        )
        assert await redis.xrange(VOICE_EFFECT_STREAM, min=identity, max=identity)
    else:
        assert session.phase == "listening"
        assert session.plugin_state["turns"][effect.task_id]["status"] == "failed"
        assert session.plugin_state["detail"].startswith("committed_input_invalid:")
        assert await redis.exists("interaction:voice:effect-done:" + effect.effect_id)
        assert not await redis.xrange(VOICE_EFFECT_STREAM, min=identity, max=identity)
        await worker.handle(identity, fields)
        transcriber.assert_not_awaited()


@pytest.mark.parametrize("owns_response", [True, False])
async def test_recovery_uses_only_response_owned_by_interrupted_effect(
    setup, owns_response
):
    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]

    def interrupted(session):
        session.plugin_state["response_effects"] = {effect.effect_id: "generating"}
        session.plugin_state["turns"][effect.task_id]["status"] = "generating"
        session.plugin_state["response"] = {
            "effect_id": effect.effect_id if owns_response else "previous-effect",
            "response_id": "stored-response",
        }
        return []

    await runtime.store.transition(effect.interaction_id, interrupted)
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(
            rendered_samples=480, terminal_ack_state="cancelled"
        )
    )
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    await worker.handle(identity, fields)
    session = await runtime.store.get(effect.interaction_id)
    response = session.plugin_state["response"]
    assert response["response_id"] == ("stored-response" if owns_response else "")
    assert response["rendered_samples"] == (480 if owns_response else 0)
    assert response["text"] == ""
    assert session.plugin_state["detail"] == "worker_interrupted"
    transcriber.assert_not_awaited()
    if owns_response:
        runtime.responses.get.assert_awaited_once_with("stored-response")
    else:
        runtime.responses.get.assert_not_awaited()


@pytest.mark.parametrize("crash", [False, True])
async def test_phrase_checkpoint_precedes_audio_and_retains_conservative_interruption(
    setup, crash
):
    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    checkpoint = None

    class Engine:
        async def generate(self, **kwargs):
            if crash:
                yield VoiceAudio(b"\x00\x00" * 480, 0, "Confirmed phrase.", 0, True)
            yield VoiceAudio(
                b"\x00\x00" * 480,
                1 if crash else 0,
                "Never claim these whole words were heard.",
                480 if crash else 0,
                False,
            )

    async def deliver(redis_client, client, capture, producer, **kwargs):
        nonlocal checkpoint
        await kwargs["on_queued"](SimpleNamespace(response_id="interrupted-response"))
        async for _ in producer:
            checkpoint = await runtime.store.get(effect.interaction_id)
            phrase = checkpoint.plugin_state["response"]["phrases"][-1]
            assert phrase["text"]  # Durable before any yielded audio is publishable.
            if phrase["end_sample"] is None:
                raise StaleResponse("playback interrupted within phrase")

    runtime.engine = Engine()
    runtime.deliver = deliver
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(
            rendered_samples=720 if crash else 240, terminal_ack_state="cancelled"
        )
    )
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    await worker.handle(identity, fields)
    if crash:
        # Restore exactly the last durable pre-finish checkpoint, as after a
        # process crash. Its local phrase list no longer exists on recovery.
        def restore(session):
            session.plugin_state = checkpoint.plugin_state
            return []

        await runtime.store.transition(effect.interaction_id, restore)
        await redis.delete("interaction:voice:effect-done:" + effect.effect_id)
        runtime.engine = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("must not replay provider"))
        )
        await worker.handle(identity, fields)
    final = await runtime.store.get(effect.interaction_id)
    work = final.plugin_state["turns"][effect.task_id]
    assert work["heard_text"] == ("Confirmed phrase." if crash else "")
    assert work["unconfirmed_partial"] is True
    assistant = final.plugin_state["history"][-1]
    assert assistant["interrupted"] is True
    assert "no additional words are confirmed as heard" in assistant["content"]
    assert "Never claim these whole words" not in assistant["content"]
    if crash:
        assert assistant["content"].startswith("Confirmed phrase.")


async def test_phrase_metadata_limit_stops_audio_before_unbounded_checkpoint(
    setup, monkeypatch
):
    redis, runtime, voice, _ = setup
    monkeypatch.setattr(
        "backend.services.interaction_modes.voice.runtime.MAX_RESPONSE_PHRASES", 2
    )
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    identity, fields, effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0]
    published = []

    class Engine:
        async def generate(self, **kwargs):
            for index in range(3):
                yield VoiceAudio(b"\0\0", index, f"Phrase {index}.", index, True)

    async def deliver(redis_client, client, capture, producer, **kwargs):
        await kwargs["on_queued"](SimpleNamespace(response_id="bounded-response"))
        async for chunk in producer:
            published.append(chunk)

    runtime.engine = Engine()
    runtime.deliver = deliver
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(rendered_samples=2, terminal_ack_state="cancelled")
    )
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    await worker.handle(identity, fields)
    final = await runtime.store.get(effect.interaction_id)
    assert len(published) == 2
    assert len(final.plugin_state["response"]["phrases"]) == 2
    assert final.plugin_state["turns"][effect.task_id]["status"] == "failed"
    assert (
        final.plugin_state["turns"][effect.task_id]["heard_text"]
        == "Phrase 0. Phrase 1."
    )
    assert await redis.exists("interaction:voice:effect-done:" + effect.effect_id)


@pytest.mark.parametrize("new_engagement", [True, False])
async def test_late_task_capacity_check_cannot_interrupt_newer_generation(
    setup, new_engagement
):
    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    old = await runtime.store.get_active("user", "client")

    def full(session):
        session.plugin_state["tasks"] = {
            str(i): {
                "name": "search_vault",
                "arguments": {"query": "test"},
                "status": "completed",
                "state": {},
                "detail": "",
            }
            for i in range(8)
        }
        return []

    old, _ = await runtime.store.transition(old.interaction_id, full)
    effect = pb.VoiceEffect(effect_id="late", generation=old.response_generation)
    if new_engagement:
        await command(runtime, voice, pb.CONVERSATION_ACTION_END)
        await command(runtime, voice)
    else:
        await runtime.enqueue_committed(turn(voice, identity="new-turn"), voice)
    current = await runtime.store.get_active("user", "client")
    await runtime._queue_tools(
        old,
        effect,
        [
            SimpleNamespace(
                call_id="late", name="search_vault", arguments={"query": "test"}
            )
        ],
    )
    await runtime.responses.assert_generation(
        "user", "client", current.response_generation
    )
    active = await runtime.store.get_active("user", "client")
    assert active.interaction_id == current.interaction_id
    assert active.status == "active"


async def test_onset_supersedes_before_queued_effect_or_transcription(setup):
    redis, runtime, voice, transcriber = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[0][2]
    event = {
        "kind": "opened",
        "voice_session_id": voice.voice_session_id,
        "audio_session_id": "audio",
        "capture_epoch": 1,
    }
    await runtime.speech_onset(event, event_id="onset")
    await runtime.execute_effect(effect)
    transcriber.assert_not_awaited()
    session = await runtime.store.get(effect.interaction_id)
    assert session.plugin_state["input_open"] and session.phase == "listening"
    assert session.plugin_state["turns"][effect.task_id]["status"] == "interrupted"
    generation = session.response_generation
    await runtime.speech_onset(event, event_id="onset")
    assert (
        await runtime.store.get(effect.interaction_id)
    ).response_generation == generation


async def queue_tool(redis, runtime, voice):
    state = await command(runtime, voice)
    session = await runtime.store.get(state.interaction_id)
    source = pb.VoiceEffect(effect_id="source", generation=session.response_generation)
    await runtime._queue_tools(
        session,
        source,
        [VoiceToolCall("call", "delegate_to_hermes", {"request": "Research"})],
    )
    return (await effects(redis, pb.VOICE_EFFECT_KIND_TASK))[-1][2]


async def test_task_continues_after_end_and_cannot_restart_speech(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    started, complete = asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        await complete.wait()
        return {"status": "completed", "answer": "Done"}

    runtime.tools.execute = execute
    task = asyncio.create_task(runtime.execute_effect(effect))
    await started.wait()
    await command(runtime, voice, pb.CONVERSATION_ACTION_END)
    complete.set()
    await task
    session = await runtime.store.get(effect.interaction_id)
    assert session.status == "ended"
    assert session.plugin_state["tasks"][effect.task_id]["status"] == "completed"
    assert await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE) == []


async def test_cancel_before_queued_execution_never_submits(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    runtime.tools.execute = AsyncMock()
    await command(
        runtime, voice, pb.CONVERSATION_ACTION_CANCEL_TASK, task_id=effect.task_id
    )
    await runtime.execute_effect(effect)
    cancel = (await effects(redis, pb.VOICE_EFFECT_KIND_CANCEL_TASK))[-1][2]
    await runtime.execute_effect(cancel)
    runtime.tools.execute.assert_not_awaited()
    assert (await runtime.store.get(effect.interaction_id)).plugin_state["tasks"][
        effect.task_id
    ]["status"] == "cancelled"


async def test_cancel_during_remote_submission_waits_then_stops_known_run(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    submitted, identified, done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        await kwargs["checkpoint"]({"submission_started": True})
        submitted.set()
        await identified.wait()
        await kwargs["checkpoint"]({"remote_run_id": "remote"})
        await done.wait()
        return {"status": "cancelled", "answer": "Remote cancelled"}

    runtime.tools.execute = execute
    runtime.tools.cancel = AsyncMock(
        return_value={"status": "cancel_requested", "answer": "Stop requested"}
    )
    running = asyncio.create_task(runtime.execute_effect(effect))
    await submitted.wait()
    await command(
        runtime, voice, pb.CONVERSATION_ACTION_CANCEL_TASK, task_id=effect.task_id
    )
    first_cancel = (await effects(redis, pb.VOICE_EFFECT_KIND_CANCEL_TASK))[-1][2]
    await runtime.execute_effect(first_cancel)
    runtime.tools.cancel.assert_not_awaited()
    identified.set()
    for _ in range(100):
        pending = await effects(redis, pb.VOICE_EFFECT_KIND_CANCEL_TASK)
        if len(pending) == 2:
            break
        await asyncio.sleep(0.001)
    assert len(pending) == 2
    await runtime.execute_effect(pending[-1][2])
    assert (
        runtime.tools.cancel.call_args.kwargs["context"].state["remote_run_id"]
        == "remote"
    )
    done.set()
    await running
    assert (await runtime.store.get(effect.interaction_id)).plugin_state["tasks"][
        effect.task_id
    ]["status"] == "cancelled"


async def test_lease_release_never_deletes_successor(setup):
    redis, _, _, _ = setup
    await redis.set("lease", "new-owner")
    assert not await release_lease(redis, "lease", "old-owner")
    assert await redis.get("lease") == b"new-owner"


async def test_stale_stt_is_cancelled_and_next_turn_is_not_blocked(setup):
    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def stt(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.transcripts.exact_transcriber = stt
    pending = asyncio.create_task(runtime.execute_effect(effect))
    await started.wait()
    await runtime.speech_onset(
        {
            "kind": "opened",
            "voice_session_id": voice.voice_session_id,
            "audio_session_id": "audio",
            "capture_epoch": 1,
        },
        event_id="cut-stt",
    )
    with pytest.raises(StaleResponse):
        await asyncio.wait_for(pending, 0.2)
    assert cancelled.is_set()


async def test_native_live_input_subscribes_without_consuming_durable_or_other_groups(
    setup,
):
    redis, runtime, voice, _ = setup
    from backend.services.audio_stream.v2_streams import realtime_stream

    class Engine:
        def __init__(self):
            self.chunks = []
            self.clears = 0

        async def open(self, **kwargs):
            pass

        async def clear_input(self):
            self.chunks = []
            self.clears += 1

        async def append_audio(self, pcm):
            self.chunks.append(pcm)

    engine = Engine()
    runtime.engine_factory = lambda choice: engine
    state = await command(runtime, voice, engine=pb.SPEECH_ENGINE_REALTIME)
    session = await runtime.store.get(state.interaction_id)
    stream = realtime_stream("audio")
    pcm = b"\x01\x00" * 320
    for sequence in range(2):
        event = pb.CaptureStreamEvent(
            frame=pb.CanonicalPcmFrame(
                binding=_binding(voice),
                sequence=sequence,
                delivery_class=pb.DELIVERY_CLASS_LIVE,
                pcm_s16le=pcm,
            )
        )
        await redis.xadd(stream, {"event": event.SerializeToString()})
    await runtime._start_native_input(session, {"start_sequence": 0, "turn_id": "live"})
    for _ in range(100):
        if len(engine.chunks) == 2:
            break
        await asyncio.sleep(0.001)
    assert engine.chunks == [pcm, pcm]
    audio = CommittedAudioTurn(
        interval=AudioInterval(
            audio_session_id="audio",
            capture_epoch=1,
            start_ms=0,
            end_ms=40,
            voice_session_id=voice.voice_session_id,
            turn_id="live",
            turn_revision=0,
        ),
        start_sequence=0,
        end_sequence=1,
        pcm=pcm * 2,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )
    await runtime._commit_native_input(session, audio, engine)
    assert engine.clears == 1  # Exact live prefix needs no duplicate append.
    assert await redis.xlen(stream) == 2
    assert await redis.xinfo_groups(stream) == []


async def test_native_overshot_input_reconciles_exact_canonical_boundary(setup):
    redis, runtime, voice, _ = setup
    from backend.services.audio_stream.v2_streams import realtime_stream

    engine = SimpleNamespace(
        open=AsyncMock(), clear_input=AsyncMock(), append_audio=AsyncMock()
    )
    runtime.engine_factory = lambda choice: engine
    state = await command(runtime, voice, engine=pb.SPEECH_ENGINE_REALTIME)
    session = await runtime.store.get(state.interaction_id)
    pcm = b"\x01\x00" * 320
    for sequence in range(3):
        event = pb.CaptureStreamEvent(
            frame=pb.CanonicalPcmFrame(
                binding=_binding(voice),
                sequence=sequence,
                delivery_class=pb.DELIVERY_CLASS_LIVE,
                pcm_s16le=pcm,
            )
        )
        await redis.xadd(realtime_stream("audio"), {"event": event.SerializeToString()})
    await runtime._start_native_input(session, {"start_sequence": 0, "turn_id": "live"})
    for _ in range(100):
        if engine.append_audio.await_count == 3:
            break
        await asyncio.sleep(0.001)
    audio = CommittedAudioTurn(
        interval=AudioInterval(
            audio_session_id="audio",
            capture_epoch=1,
            start_ms=0,
            end_ms=40,
            voice_session_id=voice.voice_session_id,
            turn_id="live",
            turn_revision=0,
        ),
        start_sequence=0,
        end_sequence=1,
        pcm=pcm * 2,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )
    await runtime._commit_native_input(session, audio, engine)
    assert engine.clear_input.await_count == 2
    assert engine.append_audio.call_args.args == (pcm * 2,)


async def test_failed_effect_keeps_pending_until_owned_retry_finishes(setup):
    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    worker = VoiceEffectWorker(runtime)
    await worker.setup()
    delivered = await redis.xreadgroup(
        GROUP, worker.consumer, {VOICE_EFFECT_STREAM: ">"}, count=20
    )
    identity, fields = next(
        (identity, fields)
        for _, batch in delivered
        for identity, fields in batch
        if pb.VoiceEffect.FromString(fields[b"effect"]).kind
        == pb.VOICE_EFFECT_KIND_RESPONSE
    )
    effect = pb.VoiceEffect.FromString(fields[b"effect"])
    runtime.execute_effect = AsyncMock(
        side_effect=[RuntimeError("provider unavailable"), None]
    )
    await worker.handle(identity, fields)
    assert not await redis.exists("interaction:voice:effect-done:" + effect.effect_id)
    assert any(
        item["message_id"] == identity
        for item in await redis.xpending_range(VOICE_EFFECT_STREAM, GROUP, "-", "+", 20)
    )
    await worker.handle(identity, fields)
    assert await redis.exists("interaction:voice:effect-done:" + effect.effect_id)
    assert all(
        item["message_id"] != identity
        for item in await redis.xpending_range(VOICE_EFFECT_STREAM, GROUP, "-", "+", 20)
    )


async def test_old_response_uses_own_late_ack_without_overwriting_new_response(setup):
    redis, runtime, voice, _ = setup
    state = await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    old = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    session = await runtime.store.get(state.interaction_id)
    await runtime.enqueue_committed(turn(voice, "next", 3000), voice)

    def newer(current):
        current.plugin_state["response"] = {
            "response_id": "new-response",
            "text": "new text",
        }
        return []

    await runtime.store.transition(session.interaction_id, newer)
    runtime.responses.get = AsyncMock(
        side_effect=[
            SimpleNamespace(rendered_samples=4, terminal_ack_state=None),
            SimpleNamespace(rendered_samples=10, terminal_ack_state="cancelled"),
        ]
    )
    await runtime._response_finished(
        session,
        old,
        [{"text": "Heard", "end_sample": 10}, {"text": "Unheard", "end_sample": 20}],
        "interrupted",
        response_id="old-response",
    )
    updated = await runtime.store.get(session.interaction_id)
    assert updated.plugin_state["response"] == {
        "response_id": "new-response",
        "text": "new text",
    }
    assert updated.plugin_state["turns"][old.task_id]["heard_text"] == "Heard"
    assert all(
        call.args == ("old-response",) for call in runtime.responses.get.call_args_list
    )


async def test_main_worker_monitors_projector_and_cleans_children(setup, monkeypatch):
    from backend.plugins.router import PluginRouter
    from backend.workers.interaction_mode_worker import InteractionModeWorker

    redis, runtime, _, _ = setup
    started = asyncio.Event()

    async def running():
        started.set()
        await asyncio.Event().wait()

    runtime.run = running
    runtime.stop = AsyncMock()
    projector = SimpleNamespace(
        run=AsyncMock(side_effect=RuntimeError("journal failed")), stop=AsyncMock()
    )
    worker = InteractionModeWorker(
        redis, PluginRouter(), voice_runtime=runtime, journal_projector=projector
    )
    worker.turn_router.run = running
    worker.turn_router.stop = AsyncMock()
    monkeypatch.setattr("backend.workers.interaction_mode_worker.beat", AsyncMock())
    # Do not block fakeredis socket cancellation; the orchestration remains real.
    redis.xreadgroup = AsyncMock(return_value=[])

    async def recover():
        await asyncio.sleep(0.001)

    worker._recover_pending = recover
    with pytest.raises(RuntimeError, match="journal failed"):
        await asyncio.wait_for(worker.run(), 0.2)
    runtime.stop.assert_awaited()
    projector.stop.assert_awaited()


async def test_ended_task_can_be_cancelled_without_ending_new_engagement(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    await command(runtime, voice, pb.CONVERSATION_ACTION_END)
    new = await command(runtime, voice)
    ended = await command(
        runtime,
        voice,
        pb.CONVERSATION_ACTION_CANCEL_TASK,
        interaction_id=effect.interaction_id,
        task_id=effect.task_id,
    )
    assert ended.phase == pb.CONVERSATION_PHASE_ENDED
    assert (
        await runtime.store.get_active("user", "client")
    ).interaction_id == new.interaction_id
    with pytest.raises(ValueError, match="different interaction"):
        await command(
            runtime,
            voice,
            pb.CONVERSATION_ACTION_END,
            interaction_id=effect.interaction_id,
        )
    assert (
        await runtime.store.get_active("user", "client")
    ).interaction_id == new.interaction_id


async def test_remote_task_saturation_leaves_control_lane_available(setup):
    redis, runtime, voice, _ = setup
    from backend.services.interaction_modes.voice.worker import VoiceEffectWorker

    task_lane = VoiceEffectWorker(
        runtime, group="test-tasks", kinds={pb.VOICE_EFFECT_KIND_TASK}, limit=1
    )
    control_lane = VoiceEffectWorker(
        runtime, group="test-control", kinds={pb.VOICE_EFFECT_KIND_CANCEL_TASK}, limit=1
    )
    await task_lane.setup()
    await control_lane.setup()
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(effect):
        if effect.kind == pb.VOICE_EFFECT_KIND_TASK:
            started.set()
            await release.wait()
        else:
            cancelled.set()

    runtime.execute_effect = execute
    entries = []
    for kind in [pb.VOICE_EFFECT_KIND_TASK, pb.VOICE_EFFECT_KIND_CANCEL_TASK]:
        effect = pb.VoiceEffect(effect_id=str(uuid.uuid4()), kind=kind)
        fields = {b"effect": effect.SerializeToString()}
        entries.append((await redis.xadd(VOICE_EFFECT_STREAM, fields), fields))
    await task_lane._schedule(entries)
    await started.wait()
    await control_lane._schedule(entries)
    await asyncio.wait_for(cancelled.wait(), 0.1)
    assert not release.is_set()
    release.set()
    await asyncio.gather(*task_lane.tasks, *control_lane.tasks)


async def test_capacity_ends_conversation_but_preserves_capture_and_existing_tasks(
    setup,
):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)

    def full(current):
        current.plugin_state["turns"] = {
            str(index): {"status": "completed"} for index in range(100)
        }
        return []

    await runtime.store.transition(effect.interaction_id, full)
    result = await runtime.enqueue_committed(turn(voice), voice)
    assert result.reason == "conversation_capacity_reached"
    assert await runtime.store.get_active("user", "client") is None
    assert (await SessionStore(redis).read("audio")).connection_id == "socket"
    runtime.tools.execute = AsyncMock(
        return_value={"status": "completed", "answer": "Still completed"}
    )
    await runtime.execute_effect(effect)
    assert (await runtime.store.get(effect.interaction_id)).plugin_state["tasks"][
        effect.task_id
    ]["status"] == "completed"


async def test_repeated_unchanged_task_progress_does_not_duplicate_snapshots(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    revisions = []

    async def execute(*args, **kwargs):
        await kwargs["on_progress"]("working")
        revisions.append((await runtime.store.get(effect.interaction_id)).revision)
        await kwargs["on_progress"]("working")
        revisions.append((await runtime.store.get(effect.interaction_id)).revision)
        return {"status": "completed", "answer": "Done"}

    runtime.tools.execute = execute
    await runtime.execute_effect(effect)
    assert revisions[0] == revisions[1]


async def test_cancelled_input_releases_pending_task_result(setup):
    redis, runtime, voice, _ = setup
    effect = await queue_tool(redis, runtime, voice)
    event = {
        "kind": "opened",
        "voice_session_id": voice.voice_session_id,
        "audio_session_id": "audio",
        "capture_epoch": 1,
    }
    await runtime.speech_onset(event, event_id="opened")
    runtime.tools.execute = AsyncMock(
        return_value={"status": "completed", "answer": "Ready"}
    )
    await runtime.execute_effect(effect)
    assert await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE) == []
    await runtime.speech_onset({**event, "kind": "cancelled"}, event_id="abandoned")
    assert len(await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE)) == 1
    session = await runtime.store.get(effect.interaction_id)
    assert not session.plugin_state["input_open"]
    assert session.phase == "thinking"


async def test_short_first_phrase_does_not_deadlock_browser_prebuffer(setup):
    from backend.services.interaction_modes.voice.engine_types import VoiceText

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)

    class Engine:
        async def generate(self, **kwargs):
            yield VoiceAudio(
                b"\0\0" * 1920, 0, "Yes.", 0, True
            )  # 80ms: browser cannot start120ms prebuffer.
            yield VoiceText("Here is more.", 1)
            yield VoiceAudio(b"\0\0" * 2400, 1, "Here is more.", 1920, True)

    runtime.engine = Engine()
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(rendered_samples=0, terminal_ack_state="done")
    )
    delivered = []

    async def deliver(redis, client, session, producer, **kwargs):
        await kwargs["on_queued"](SimpleNamespace(response_id="short"))
        delivered.extend([pcm async for pcm in producer])

    runtime.deliver = deliver
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    await asyncio.wait_for(runtime.execute_effect(effect), 0.2)
    assert len(delivered) == 2


async def test_next_phrase_synthesizes_while_previous_audio_is_playing(setup):
    """The real runtime must not add a near-full phrase drain before slow TTS.

    Replays the measured pattern: 1.5 seconds of 'Hey!' and a 1.9-second
    synthesis request for the next phrase. The provider itself is slower than
    that first phrase, so at most about 0.4 seconds of starvation is unavoidable.
    """
    import io
    import time
    import wave

    from backend.services.interaction_modes.voice.engine import ModularVoiceEngine

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    synthesis_started = {}
    first_playback = None
    first_second_phrase = None
    sent = 0
    maximum_network_samples = 0
    rendered = 0.0
    rendered_at = time.monotonic()

    async def llm(**kwargs):
        yield {"type": "content", "text": "Hey! I do not know."}
        yield {"type": "done", "finish_reason": "stop"}

    async def synthesize(text):
        synthesis_started[text] = time.monotonic()
        if text != "Hey!":
            await asyncio.sleep(1.9)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x01\x00" * (36000 if text == "Hey!" else 63000))
        return buffer.getvalue()

    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=synthesize)

    def response(_identity):
        nonlocal rendered, rendered_at
        now = time.monotonic()
        rendered = min(sent, rendered + (now - rendered_at) * 24000)
        rendered_at = now
        return SimpleNamespace(
            rendered_samples=int(rendered), terminal_ack_state="done"
        )

    runtime.responses.get = AsyncMock(side_effect=response)

    async def deliver(redis, client, session, producer, **kwargs):
        nonlocal first_playback, first_second_phrase, sent, maximum_network_samples
        await kwargs["on_queued"](SimpleNamespace(response_id="phrase-continuity"))
        async for pcm in producer:
            if first_playback is None:
                first_playback = time.monotonic()
            if sent == 36000:
                first_second_phrase = time.monotonic()
            # Model the coordinator's unchanged two-second transport reservoir.
            while sent - response(None).rendered_samples + len(pcm) // 2 > 48000:
                await asyncio.sleep(0.005)
            sent += len(pcm) // 2
            maximum_network_samples = max(
                maximum_network_samples, sent - response(None).rendered_samples
            )
            # Publication is asynchronous even before the reservoir is full.
            # A pull-only engine must not wait for every packet's Redis I/O
            # before it starts synthesizing the following phrase.
            await asyncio.sleep(0.012)

    runtime.deliver = deliver
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    await asyncio.wait_for(runtime.execute_effect(effect), 6)
    assert sent == 99000
    assert maximum_network_samples <= 48000
    assert synthesis_started["I do not know."] - first_playback < 0.25
    assert first_second_phrase - first_playback - 1.5 < 0.65


async def test_native_completed_transcript_is_heard_only_after_full_playback(setup):
    from backend.services.interaction_modes.voice.engine_types import VoiceCompleted

    redis, runtime, voice, stt = setup
    state = await command(runtime, voice, engine=pb.SPEECH_ENGINE_REALTIME)
    await runtime.enqueue_committed(turn(voice), voice)

    class Native:
        open = AsyncMock()
        clear_input = AsyncMock()
        append_audio = AsyncMock()

        async def generate(self, **kwargs):
            yield VoiceAudio(b"\0\0" * 480, 0, "", 0, False)
            yield VoiceCompleted("Native answer", "stop", 480)

    runtime.engine_factory = lambda choice: Native()
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(rendered_samples=480, terminal_ack_state="done")
    )

    async def deliver(redis, client, session, producer, **kwargs):
        await kwargs["on_queued"](SimpleNamespace(response_id="native"))
        assert len([pcm async for pcm in producer]) == 1

    runtime.deliver = deliver
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    await runtime.execute_effect(effect)
    stt.assert_not_awaited()
    session = await runtime.store.get(state.interaction_id)
    assert session.plugin_state["history"][-1]["content"] == "Native answer"
    assert session.plugin_state["response"]["rendered_samples"] == 480


async def test_processing_entrypoint_reports_exact_stt_and_overlapping_llm_tts(setup):
    from test_voice_engine import _wav
    from test_voice_processing_progress import next_update

    from backend.redis_keys import ClientId, device_downlink_channel
    from backend.services.interaction_modes.voice.engine import ModularVoiceEngine

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    subscription = redis.pubsub()
    await subscription.subscribe(
        str(device_downlink_channel(ClientId.from_value("client")))
    )
    stt_release, llm_release, tts_release, pcm_ready = (
        asyncio.Event() for _ in range(4)
    )

    async def transcribe(*args):
        await stt_release.wait()
        return "Hello Chronicle"

    async def llm(**kwargs):
        yield {"type": "content", "text": "Hello. "}
        await llm_release.wait()
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        await tts_release.wait()
        return _wav(480)

    async def deliver(redis, client, session, producer, **kwargs):
        await kwargs["on_queued"](SimpleNamespace(response_id="processing-response"))
        async for _ in producer:
            pcm_ready.set()

    runtime.transcripts.exact_transcriber = transcribe
    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=tts)
    runtime.deliver = deliver
    runtime.responses.get = AsyncMock(
        return_value=SimpleNamespace(rendered_samples=480, terminal_ack_state="done")
    )
    pending = asyncio.create_task(runtime.execute_effect(effect))
    try:
        stt = await next_update(subscription, lambda update: update.transcribing)
        assert stt.generation == effect.generation
        assert not stt.generating_text and not stt.synthesizing_speech
        stt_release.set()
        overlap = await next_update(
            subscription,
            lambda update: update.generating_text and update.synthesizing_speech,
        )
        assert not overlap.transcribing
        before = (await runtime.store.get(effect.interaction_id)).revision
        await asyncio.sleep(0.15)
        assert (await runtime.store.get(effect.interaction_id)).revision == before
        tts_release.set()
        await asyncio.wait_for(pcm_ready.wait(), 0.3)
        assert not llm_release.is_set()  # audio progresses while model work continues
        llm_release.set()
        await asyncio.wait_for(pending, 1)
        final = await next_update(subscription, lambda update: update.finished)
        assert final.sequence > overlap.sequence
        assert not final.generating_text and not final.synthesizing_speech
        assert (
            runtime.state(
                await runtime.store.get(effect.interaction_id)
            ).response_generation
            == effect.generation
        )
    finally:
        stt_release.set()
        tts_release.set()
        llm_release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await subscription.aclose()


async def test_slow_processing_publication_does_not_block_runtime_pcm(
    setup, monkeypatch
):
    from test_voice_engine import _wav

    from backend.services.interaction_modes.voice.engine import ModularVoiceEngine
    from backend.services.interaction_modes.voice.progress import (
        VoiceProcessingPublisher,
    )

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    publication_started, audio_delivered = asyncio.Event(), asyncio.Event()

    async def publish(self):
        publication_started.set()
        await asyncio.Event().wait()

    async def llm(**kwargs):
        await publication_started.wait()
        yield {"type": "content", "text": "Hi."}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        return _wav(480)

    async def deliver(redis, client, session, producer, **kwargs):
        async for _ in producer:
            audio_delivered.set()

    monkeypatch.setattr(VoiceProcessingPublisher, "_publish", publish)
    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=tts)
    runtime.deliver = deliver
    pending = asyncio.create_task(runtime.execute_effect(effect))
    try:
        await asyncio.wait_for(audio_delivered.wait(), 0.2)
        await asyncio.wait_for(pending, 0.5)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "voice-processing-publisher" and not task.done()
        ]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_superseded_processing_cancels_providers_and_cannot_relight(setup):
    from test_voice_engine import _wav
    from test_voice_processing_progress import next_update

    from backend.redis_keys import ClientId, device_downlink_channel
    from backend.services.interaction_modes.voice.engine import ModularVoiceEngine

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    subscription = redis.pubsub()
    await subscription.subscribe(
        str(device_downlink_channel(ClientId.from_value("client")))
    )
    llm_closed, tts_closed = asyncio.Event(), asyncio.Event()

    async def llm(**kwargs):
        try:
            yield {"type": "content", "text": "Hello. "}
            await asyncio.Event().wait()
        finally:
            llm_closed.set()

    async def tts(text):
        try:
            await asyncio.Event().wait()
            return _wav(480)
        finally:
            tts_closed.set()

    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=tts)
    pending = asyncio.create_task(runtime.execute_effect(effect))
    try:
        await next_update(
            subscription,
            lambda update: update.generating_text and update.synthesizing_speech,
        )
        await runtime.speech_onset(
            {
                "kind": "opened",
                "voice_session_id": voice.voice_session_id,
                "audio_session_id": "audio",
                "capture_epoch": 1,
            },
            event_id="processing-barge",
        )
        await asyncio.wait_for(pending, 0.4)
        assert llm_closed.is_set() and tts_closed.is_set()
        state = runtime.state(await runtime.store.get(effect.interaction_id))
        assert state.response_generation > effect.generation
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "voice-processing-publisher" and not task.done()
        ]
        # Any update already queued before interruption is fenced by state generation;
        # the obsolete publisher must emit nothing further after cleanup.
        while await subscription.get_message(
            ignore_subscribe_messages=True, timeout=0.01
        ):
            pass
        assert (
            await subscription.get_message(ignore_subscribe_messages=True, timeout=0.03)
            is None
        )
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await subscription.aclose()


async def test_tool_continuation_admits_new_effect_with_same_generation(
    setup, monkeypatch
):
    from test_voice_engine import _wav
    from test_voice_processing_progress import next_update

    from backend.redis_keys import (
        ClientId,
        device_downlink_channel,
        voice_processing_owner,
    )
    from backend.services.interaction_modes.voice.engine import ModularVoiceEngine
    from backend.services.interaction_modes.voice.progress import (
        VoiceProcessingPublisher,
    )

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    first = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    admitted = await runtime.store.get(first.interaction_id)
    old_publisher = VoiceProcessingPublisher(
        redis,
        session=admitted,
        generation=first.generation,
        binding=_binding(admitted),
        effect_id=first.effect_id,
        state_revision=admitted.plugin_state["processing_effect_revision"],
    )
    old_publisher.observe("generating_text", True)
    subscription = redis.pubsub()
    await subscription.subscribe(
        str(device_downlink_channel(ClientId.from_value("client")))
    )
    release = asyncio.Event()
    calls = 0

    async def llm(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "type": "done",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "tool-call",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            }
        else:
            await release.wait()
            yield {"type": "content", "text": "Found it."}
            yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        return _wav(480)

    async def deliver(redis, client, session, producer, **kwargs):
        async for _ in producer:
            pass

    monkeypatch.setattr(
        runtime.tools,
        "schemas",
        lambda: [{"type": "function", "function": {"name": "lookup"}}],
    )
    monkeypatch.setattr(
        runtime.tools,
        "execute",
        AsyncMock(return_value={"status": "completed", "answer": "Found it"}),
    )
    runtime.engine = ModularVoiceEngine(llm_stream=llm, synthesize=tts)
    runtime.deliver = deliver
    await runtime.execute_effect(first)
    old_terminal = await next_update(subscription, lambda update: update.finished)
    assert old_terminal.effect_id == first.effect_id
    task_effect = (await effects(redis, pb.VOICE_EFFECT_KIND_TASK))[-1][2]
    await runtime.execute_effect(task_effect)
    continuation = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    assert continuation.generation == first.generation
    assert continuation.effect_id != first.effect_id
    state = runtime.state(await runtime.store.get(first.interaction_id))
    assert state.response_effect_id == continuation.effect_id
    assert (
        await redis.get(voice_processing_owner(first.interaction_id))
        == continuation.effect_id.encode()
    )
    assert not await old_publisher._publish()
    old_publisher.snapshot.finished = True
    assert not await old_publisher._publish()
    pending = asyncio.create_task(runtime.execute_effect(continuation))
    try:
        active = await next_update(subscription, lambda update: update.generating_text)
        assert active.effect_id == continuation.effect_id
        assert active.state_revision == state.revision
        assert active.state_revision > old_terminal.state_revision
        release.set()
        await asyncio.wait_for(pending, 1)
        final = await next_update(subscription, lambda update: update.finished)
        assert final.effect_id == continuation.effect_id
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await subscription.aclose()


async def test_native_processing_reports_one_provider_activity_not_tts(setup):
    from test_voice_processing_progress import next_update

    from backend.redis_keys import ClientId, device_downlink_channel
    from backend.services.interaction_modes.voice.engine_types import VoiceCompleted

    redis, runtime, voice, stt = setup
    await command(runtime, voice, engine=pb.SPEECH_ENGINE_REALTIME)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    subscription = redis.pubsub()
    await subscription.subscribe(
        str(device_downlink_channel(ClientId.from_value("client")))
    )
    release = asyncio.Event()

    class Native:
        open = AsyncMock()
        clear_input = AsyncMock()
        append_audio = AsyncMock()

        async def generate(self, **kwargs):
            assert "activity" not in kwargs
            await release.wait()
            yield VoiceCompleted("", "stop", 0)

    runtime.engine_factory = lambda choice: Native()
    pending = asyncio.create_task(runtime.execute_effect(effect))
    try:
        update = await next_update(
            subscription, lambda update: update.generating_response
        )
        assert not any(
            (update.generating_text, update.synthesizing_speech, update.transcribing)
        )
        stt.assert_not_awaited()
        release.set()
        await asyncio.wait_for(pending, 1)
        final = await next_update(subscription, lambda update: update.finished)
        assert not final.generating_response
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await subscription.aclose()


async def test_processing_owner_is_unchanged_by_checkpoint_and_deleted_on_end(setup):
    from backend.redis_keys import voice_processing_owner

    redis, runtime, voice, _ = setup
    await command(runtime, voice)
    await runtime.enqueue_committed(turn(voice), voice)
    effect = (await effects(redis, pb.VOICE_EFFECT_KIND_RESPONSE))[-1][2]
    owner_key = voice_processing_owner(effect.interaction_id)
    admitted = await runtime.store.get(effect.interaction_id)
    admission_revision = admitted.plugin_state["processing_effect_revision"]
    async with redis.pipeline(transaction=True) as pipe:
        await pipe.watch(owner_key)
        assert await pipe.get(owner_key) == effect.effect_id.encode()

        def checkpoint(session):
            session.plugin_state["test_checkpoint"] = "saved"
            return []

        state, changed = await runtime.store.transition(
            effect.interaction_id, checkpoint
        )
        assert changed and state.revision > admission_revision
        assert state.plugin_state["processing_effect_revision"] == admission_revision
        pipe.multi()
        pipe.get(owner_key)
        # Rewriting even the identical pointer would invalidate this WATCH.
        assert await pipe.execute() == [effect.effect_id.encode()]
    await command(runtime, voice, pb.CONVERSATION_ACTION_END)
    assert await redis.get(owner_key) is None
