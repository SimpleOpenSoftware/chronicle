"""Focused tests for the Conversation-independent interaction runtime."""

import asyncio
import base64
import itertools
import json
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fakeredis import aioredis as fake_aioredis

from backend.plugins import BasePlugin
from backend.plugins.router import PluginRouter
from backend.redis_keys import ClientId, SessionId
from backend.services.audio_stream.session_store import (
    SessionStore as ProductionSessionStore,
)
from backend.services.interaction_modes import (
    AudioInterval,
    InteractionIngress,
    InteractionModeDefinition,
    InteractionRegistry,
    InteractionResult,
    InteractionStore,
)
from backend.services.interaction_modes import processor as processor_module
from backend.services.interaction_modes.contracts import InteractionInput
from backend.services.interaction_modes.ingress import INPUT_STREAM
from backend.services.interaction_modes.processor import InteractionProcessor
from backend.services.response_coordinator import ResponseCoordinator
from backend.services.transcription.streaming_consumer import (
    StreamingTranscriptionConsumer,
)
from backend.services.voice_sessions import VoiceSessionCoordinator
from backend.services.wakeword.activations import WakeActivation, WakeActivationStore
from backend.services.wakeword.dispatcher import WakeWordDispatcher
from backend.services.wakeword.executor import execute_voice_command
from backend.workers import interaction_mode_worker
from backend.workers.interaction_mode_worker import InteractionModeWorker


class SessionStore(ProductionSessionStore):
    """Ambient provenance fixture for interaction tests."""

    async def init_session(self, session_id: str, **kwargs) -> None:
        kwargs.setdefault("capture_epoch", 0)
        kwargs.setdefault("processing_profile", "ambient")
        kwargs.setdefault(
            "effects",
            {
                "aec": {"reporting": "unreported"},
                "noise_suppression": {"reporting": "unreported"},
            },
        )
        kwargs.setdefault("voice_session_id", None)
        await super().init_session(session_id, **kwargs)


class _ModePlugin(BasePlugin):
    INTERACTION_MODES = (
        InteractionModeDefinition(
            mode_id="swiggy_order",
            activation_phrases=("order swiggy",),
            idle_timeout_seconds=600,
            max_duration_seconds=1800,
        ),
    )

    def __init__(self):
        super().__init__({"enabled": True, "modes": ["swiggy_order"]})
        self.end_reasons = []

    async def initialize(self):
        return None

    async def on_interaction_start(self, context):
        return InteractionResult(
            reply="Started",
            phase="shopping",
            plugin_state={"first_request": context.input.text},
        )

    async def on_interaction_turn(self, context):
        if context.input.text == "complete order":
            return InteractionResult(
                reply="Closed", end=True, end_reason="user_cancelled"
            )
        return InteractionResult(
            reply="Updated",
            phase="shopping",
            plugin_state={"last_request": context.input.text},
        )

    async def on_interaction_end(self, context):
        self.end_reasons.append(context.end_reason)


_interval_slots = itertools.count(1)


def _interval(
    audio_session_id: str = "audio-1", *, slot: int | None = None
) -> AudioInterval:
    slot = next(_interval_slots) if slot is None else slot
    return AudioInterval(
        audio_session_id=audio_session_id,
        capture_epoch=0,
        start_ms=slot * 2_000,
        end_ms=slot * 2_000 + 1_000,
    )


@pytest.fixture
def registry():
    value = InteractionRegistry()
    value.register("fixture_shopping", _ModePlugin.INTERACTION_MODES[0])
    return value


async def _read_input(redis_client) -> InteractionInput:
    entries = await redis_client.xrange(INPUT_STREAM)
    raw = entries[-1][1]["input"]
    return InteractionInput.from_dict(json.loads(raw))


def test_registry_accepts_keyword_and_optional_hermes_prefix(registry):
    plain = registry.match("Order Swiggy")
    acoustic = registry.match("Hey, Hermes, order Swiggy add two litres of milk")

    assert plain is not None
    assert plain.definition.mode_id == "swiggy_order"
    assert plain.remainder == ""
    assert acoustic is not None
    assert acoustic.remainder == "add two litres of milk"


def test_registry_rejects_ambiguous_prefixes(registry):
    with pytest.raises(ValueError, match="collides"):
        registry.register(
            "other",
            InteractionModeDefinition(
                mode_id="other_order", activation_phrases=("order swiggy groceries",)
            ),
        )


def test_router_only_registers_configured_modes_and_exposes_asr_hint():
    router = PluginRouter()
    plugin = _ModePlugin()

    router.register_plugin("fixture_shopping", plugin)

    assert "swiggy_order" in router.interaction_registry.modes
    assert "order swiggy" in router.get_asr_keywords()


def test_router_rejects_unknown_configured_mode():
    plugin = _ModePlugin()
    plugin.config["modes"] = ["typo_mode"]

    with pytest.raises(ValueError, match="undeclared interaction mode"):
        PluginRouter().register_plugin("fixture_shopping", plugin)


async def test_ingress_activates_claims_interval_and_exclusively_consumes(registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    ingress = InteractionIngress(redis_client, registry)

    started = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(slot=1),
        text="Hermes, order Swiggy add milk",
        source="streaming",
    )
    duplicate = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(slot=1),
        text="order Swiggy add milk",
        source="wake",
    )
    unrelated_turn = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(slot=2),
        text="make that two packets",
        source="streaming",
    )

    assert started.consumed and started.accepted and started.reason == "start"
    assert duplicate.consumed and not duplicate.accepted
    assert duplicate.reason == "episode_already_claimed"
    assert unrelated_turn.consumed and unrelated_turn.accepted
    assert unrelated_turn.interaction_id == started.interaction_id
    assert await redis_client.xlen(INPUT_STREAM) == 2


async def test_ingress_allows_a_repeated_turn_from_the_same_source(registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    ingress = InteractionIngress(redis_client, registry)
    await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy",
        source="streaming",
    )

    first = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="one",
        source="streaming",
    )
    repeated = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="one",
        source="streaming",
    )

    assert first.accepted and repeated.accepted
    assert await redis_client.xlen(INPUT_STREAM) == 3


async def test_streaming_fragments_do_not_enter_interaction_modes():
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    router.register_plugin("fixture_shopping", _ModePlugin())
    normal_dispatch = AsyncMock()
    router.dispatch_event = normal_dispatch
    consumer = StreamingTranscriptionConsumer.__new__(StreamingTranscriptionConsumer)
    consumer.redis_client = redis_client
    consumer.plugin_router = router
    consumer.store = SessionStore(redis_client)
    await consumer.store.init_session(
        "audio-1",
        user_id="user-1",
        client_id="device-1",
        stream_name="audio:stream:audio-1",
    )

    await consumer.trigger_plugins(
        "audio-1",
        {
            "text": "order Swiggy",
            "words": [
                {"word": "order", "start": 0.1, "end": 0.3},
                {"word": "Swiggy", "start": 0.3, "end": 0.6},
            ],
        },
    )

    assert await redis_client.xlen(INPUT_STREAM) == 0
    normal_dispatch.assert_awaited_once()


async def test_streaming_fragment_is_inert_for_bound_voice_session():
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    normal_dispatch = AsyncMock()
    router.dispatch_event = normal_dispatch
    consumer = StreamingTranscriptionConsumer.__new__(StreamingTranscriptionConsumer)
    consumer.redis_client = redis_client
    consumer.plugin_router = router
    consumer.store = SessionStore(redis_client)
    await consumer.store.init_session(
        "audio-1",
        user_id="user-1",
        client_id="device-1",
        stream_name="audio:stream:audio-1",
        voice_session_id="voice-1",
    )

    await consumer.trigger_plugins(
        "audio-1",
        {"text": "turn on the lights", "words": []},
    )

    normal_dispatch.assert_not_awaited()


async def test_acoustic_wake_does_not_bypass_committed_turn_router(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    router.register_plugin("fixture_shopping", _ModePlugin())
    dispatcher = WakeWordDispatcher(redis_client, router)
    dispatcher._check_speaker_gate = AsyncMock(
        return_value={"allowed": True, "reason": "gate_off", "identified": None}
    )
    dispatcher._resolve_command = AsyncMock(
        return_value=("order Swiggy", "transcribed")
    )
    command_dispatch = AsyncMock()
    monkeypatch.setattr(
        "backend.services.wakeword.dispatcher.execute_voice_command",
        command_dispatch,
    )
    await SessionStore(redis_client).init_session(
        "audio-1",
        user_id="user-1",
        client_id="device-1",
        stream_name="audio:stream:audio-1",
    )
    payload = {
        "session_id": "audio-1",
        "client_id": "device-1",
        "user_id": "user-1",
        "audio_b64": base64.b64encode(b"\x00\x00").decode(),
        "sample_rate": 16000,
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "capture_epoch": 0,
        "armed_at": time.time() - 0.001,
        "end_of_turn_at": time.time(),
        "trigger_interval": {
            "start_ms": 1,
            "end_ms": 2,
            "started_at": time.time() - 1,
            "ended_at": time.time() - 0.5,
        },
        "command_interval": {
            "start_ms": 2,
            "end_ms": 3,
            "started_at": time.time() - 0.5,
            "ended_at": time.time(),
        },
        "has_speech": True,
        "wakeword": "hermes",
    }

    await dispatcher._handle_message({"event": json.dumps(payload)})

    assert await redis_client.xlen(INPUT_STREAM) == 0
    command_dispatch.assert_awaited_once()


async def test_acoustic_wake_is_inert_while_committed_turn_owns_audio(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    dispatcher = WakeWordDispatcher(redis_client, router)
    dispatcher._check_speaker_gate = AsyncMock(
        return_value={"allowed": True, "reason": "gate_off", "identified": None}
    )
    dispatcher._resolve_command = AsyncMock(
        return_value=("turn on the lights", "transcribed")
    )
    command_dispatch = AsyncMock()
    monkeypatch.setattr(
        "backend.services.wakeword.dispatcher.execute_voice_command",
        command_dispatch,
    )
    await SessionStore(redis_client).init_session(
        "audio-1",
        user_id="user-1",
        client_id="device-1",
        stream_name="audio:stream:audio-1",
        voice_session_id="voice-1",
    )
    payload = {
        "session_id": "audio-1",
        "client_id": "device-1",
        "user_id": "user-1",
        "audio_b64": base64.b64encode(b"\x00\x00").decode(),
        "sample_rate": 16000,
        "wake_trace_id": "965f573d-b050-4c25-9b59-1bc564806bd4",
        "capture_epoch": 0,
        "armed_at": time.time() - 0.001,
        "end_of_turn_at": time.time(),
        "trigger_interval": {
            "start_ms": 1,
            "end_ms": 2,
            "started_at": time.time() - 1,
            "ended_at": time.time() - 0.5,
        },
        "command_interval": {
            "start_ms": 2,
            "end_ms": 3,
            "started_at": time.time() - 0.5,
            "ended_at": time.time(),
        },
        "has_speech": True,
        "wakeword": "hermes",
    }

    await dispatcher._handle_message({"event": json.dumps(payload)})

    command_dispatch.assert_not_awaited()
    dispatcher._resolve_command.assert_not_awaited()
    activation = await WakeActivationStore(redis_client).claim(
        AudioInterval(
            audio_session_id="audio-1",
            capture_epoch=0,
            start_ms=0,
            end_ms=10,
            voice_session_id="voice-1",
            turn_id="turn-1",
        )
    )
    assert activation is not None
    assert activation.wake_trace_id == payload["wake_trace_id"]


async def test_wake_tone_request_enters_response_coordinator_facade(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    dispatcher = WakeWordDispatcher(redis_client, PluginRouter())
    play_tone = AsyncMock()
    monkeypatch.setattr(
        "backend.services.wakeword.dispatcher.play_tone_on_device",
        play_tone,
    )

    await dispatcher._handle_message(
        {
            "event": json.dumps(
                {
                    "kind": "tone",
                    "client_id": "device-1",
                    "session_id": "audio-1",
                    "tone": "armed",
                }
            )
        }
    )

    play_tone.assert_awaited_once()
    assert str(play_tone.await_args.args[1]) == "device-1"
    assert str(play_tone.await_args.args[2]) == "audio-1"
    assert play_tone.await_args.args[3] == "armed"


async def test_wake_stream_ack_happens_only_after_handler_success(monkeypatch):
    redis_client = AsyncMock()
    dispatcher = WakeWordDispatcher(redis_client, PluginRouter())
    play_tone = AsyncMock()
    monkeypatch.setattr(
        "backend.services.wakeword.dispatcher.play_tone_on_device",
        play_tone,
    )

    await dispatcher._handle_message_safe(
        {
            "event": json.dumps(
                {
                    "kind": "tone",
                    "client_id": "device-1",
                    "session_id": "audio-1",
                    "tone": "armed",
                }
            )
        },
        "42-0",
    )

    redis_client.xack.assert_awaited_once_with(
        "wakeword:detections", "wakeword-dispatch", "42-0"
    )


async def test_wake_stream_failure_remains_pending_for_recovery():
    redis_client = AsyncMock()
    dispatcher = WakeWordDispatcher(redis_client, PluginRouter())

    await dispatcher._handle_message_safe(
        {
            "event": json.dumps(
                {
                    "kind": "tone",
                    "client_id": "device-1",
                    "session_id": "audio-1",
                    "tone": "invalid",
                }
            )
        },
        "43-0",
    )

    redis_client.xack.assert_not_awaited()


async def test_wake_dispatcher_reclaims_stale_pending_detection():
    redis_client = AsyncMock()
    redis_client.xautoclaim.return_value = (
        b"0-0",
        [(b"42-0", {b"event": b"{}"})],
        [],
    )
    dispatcher = WakeWordDispatcher(redis_client, PluginRouter())
    dispatcher._handle_message_safe = AsyncMock()

    await dispatcher._recover_pending_once()

    dispatcher._handle_message_safe.assert_awaited_once_with({b"event": b"{}"}, "42-0")


async def test_acoustic_entry_writes_armed_and_end_of_turn_facts(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    ledger = Mock(append=AsyncMock())
    dispatcher = WakeWordDispatcher(redis_client, PluginRouter(), ledger)
    dispatcher._check_speaker_gate = AsyncMock(
        return_value={"allowed": False, "reason": "test", "identified": None}
    )
    monkeypatch.setattr(
        "backend.services.wakeword.dispatcher.set_device_led", AsyncMock()
    )
    payload = {
        "session_id": "audio-1",
        "client_id": "device-1",
        "user_id": "user-1",
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "capture_epoch": 4,
        "armed_at": 1_770_000_001.0,
        "end_of_turn_at": 1_770_000_002.0,
        "trigger_interval": {
            "start_ms": 100,
            "end_ms": 1100,
            "started_at": 1_770_000_000.0,
            "ended_at": 1_770_000_001.0,
        },
        "command_interval": {
            "start_ms": 1100,
            "end_ms": 2100,
            "started_at": 1_770_000_001.0,
            "ended_at": 1_770_000_002.0,
        },
        "wakeword": "hermes",
    }

    await dispatcher._handle_message({"event": json.dumps(payload)})

    facts = [call.args[0] for call in ledger.append.await_args_list]
    assert [(fact.stage, fact.ordinal) for fact in facts] == [
        ("armed", 0),
        ("end_of_turn", 1),
    ]
    assert facts[0].audio_interval.end_ms == 1100
    assert facts[1].audio_interval.end_ms == 2100


async def test_voice_executor_reports_dispatch_action_and_followup_stages():
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = Mock()
    router.dispatch_event = AsyncMock(
        return_value=[Mock(success=True, message="", should_continue=False)]
    )
    record_stage = AsyncMock()

    await execute_voice_command(
        redis_client,
        router,
        user_id="user-1",
        session_id=SessionId.from_value("audio-1"),
        client_id=ClientId.from_value("device-1"),
        command="turn on the lights",
        quiet=True,
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        interaction_stage_callback=record_stage,
    )

    assert [call.args[0] for call in record_stage.await_args_list] == [
        "dispatched",
        "acted",
        "followup_opened",
    ]


async def test_distinct_audio_interval_is_not_suppressed_by_assistant_text(registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    ingress = InteractionIngress(redis_client, registry)
    await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy",
        source="streaming",
    )

    result = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="the assistant's own spoken reply",
        source="streaming",
    )

    assert result.consumed and result.accepted
    assert result.reason == "turn"
    assert await redis_client.xlen(INPUT_STREAM) == 2


async def test_processor_applies_full_state_and_ends_session(registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    plugin = _ModePlugin()
    router = PluginRouter()
    router.register_plugin("fixture_shopping", plugin)
    ingress = InteractionIngress(redis_client, router.interaction_registry)
    processor = InteractionProcessor(redis_client, router)

    started = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy add milk",
        source="streaming",
    )
    first_input = await _read_input(redis_client)
    first_dispatch = await processor.process(first_input)
    first_session = await InteractionStore(redis_client).get(started.interaction_id)

    assert first_dispatch.lifecycle == "started"
    assert first_session.phase == "shopping"
    assert first_session.plugin_state == {"first_request": "add milk"}
    assert await InteractionStore(redis_client).is_processed(first_input.input_id)
    assert await processor.process(first_input) is None

    await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval("audio-2"),
        text="complete order",
        source="streaming",
    )
    second_dispatch = await processor.process(await _read_input(redis_client))
    ended = await InteractionStore(redis_client).get(started.interaction_id)

    assert second_dispatch.lifecycle == "ended"
    assert ended.status == "ended"
    assert ended.audio_session_id == "audio-2"
    assert plugin.end_reasons == ["user_cancelled"]
    assert await InteractionStore(redis_client).get_active("user-1", "device-1") is None


async def test_processor_emits_privacy_safe_langfuse_trace(monkeypatch, registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    router.register_plugin("fixture_shopping", _ModePlugin())
    ingress = InteractionIngress(redis_client, router.interaction_registry)
    spans = []
    span_io = []
    span_attributes = []
    fake_span = object()

    @contextmanager
    def capture_span(name, **kwargs):
        spans.append((name, kwargs))
        yield fake_span

    monkeypatch.setattr(processor_module, "chronicle_span", capture_span)
    monkeypatch.setattr(
        processor_module,
        "set_span_io",
        lambda span, **kwargs: span_io.append((span, kwargs)),
    )
    monkeypatch.setattr(
        processor_module,
        "set_span_attributes",
        lambda span, attributes: span_attributes.append((span, attributes)),
    )

    started = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy add milk",
        source="streaming",
    )
    dispatch = await InteractionProcessor(redis_client, router).process(
        await _read_input(redis_client)
    )

    assert dispatch.lifecycle == "started"
    assert spans == [
        (
            "interaction.mode.input",
            {
                "tracer_name": "chronicle.interactions",
                "attributes": {
                    "chronicle.interaction.id": started.interaction_id,
                    "chronicle.interaction.mode_id": "swiggy_order",
                    "chronicle.interaction.plugin_id": "fixture_shopping",
                    "chronicle.interaction.input_kind": "start",
                    "chronicle.interaction.source": "streaming",
                    "chronicle.client_id": "device-1",
                    "langfuse.session.id": started.interaction_id,
                    "langfuse.user.id": "user-1",
                },
            },
        )
    ]
    assert span_io[0] == (
        fake_span,
        {
            "input": {
                "kind": "start",
                "source": "streaming",
                "text_chars": len("add milk"),
                "activation_phrase": "order swiggy",
            }
        },
    )
    assert span_io[-1][1]["output"] == {
        "handled": True,
        "lifecycle": "started",
        "phase": "shopping",
        "status": "active",
        "end_reason": None,
        "reply_chars": len("Started"),
        "event_keys": [],
    }
    assert span_attributes[-1][1]["chronicle.interaction.success"] is True
    captured = json.dumps(
        {"spans": spans, "io": span_io, "attributes": span_attributes},
        default=str,
    )
    assert "add milk" not in captured
    assert "Started" not in captured


async def test_processor_expires_idle_session_and_notifies_plugin(registry):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    plugin = _ModePlugin()
    router = PluginRouter()
    router.register_plugin("fixture_shopping", plugin)
    now = time.time()
    session = await _activate_at(redis_client, router, now)
    processor = InteractionProcessor(redis_client, router)

    dispatches = await processor.expire_due(now=now + 601)

    assert len(dispatches) == 1
    assert dispatches[0].session.status == "ended"
    assert dispatches[0].session.end_reason == "idle_timeout"
    assert plugin.end_reasons == ["idle_timeout"]
    assert await InteractionStore(redis_client).get_active("user-1", "device-1") is None


async def test_first_turn_after_idle_deadline_runs_complete_expiry_transition():
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    plugin = _ModePlugin()
    router = PluginRouter()
    router.register_plugin("fixture_shopping", plugin)
    session = await _activate_at(redis_client, router, time.time() - 601)
    ingress = InteractionIngress(redis_client, router.interaction_registry)

    accepted = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval("audio-2"),
        text="add milk",
        source="streaming",
    )
    dispatch = await InteractionProcessor(redis_client, router).process(
        await _read_input(redis_client)
    )

    assert accepted.interaction_id == session.interaction_id
    assert dispatch.lifecycle == "ended"
    assert dispatch.session.end_reason == "idle_timeout"
    assert plugin.end_reasons == ["idle_timeout"]


async def test_worker_recovers_an_input_stranded_in_another_consumer(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    plugin = _ModePlugin()
    router = PluginRouter()
    router.register_plugin("fixture_shopping", plugin)
    await redis_client.xgroup_create(
        INPUT_STREAM,
        interaction_mode_worker.GROUP_NAME,
        "0",
        mkstream=True,
    )
    await InteractionIngress(redis_client, router.interaction_registry).submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy add milk",
        source="streaming",
    )
    await redis_client.xreadgroup(
        interaction_mode_worker.GROUP_NAME,
        "dead-worker",
        {INPUT_STREAM: ">"},
        count=1,
    )
    worker = InteractionModeWorker(redis_client, router)
    worker._safe_deliver = AsyncMock()
    monkeypatch.setattr(interaction_mode_worker, "PENDING_CLAIM_MIN_IDLE_MS", 0)

    recovered = await worker._recover_pending()

    assert recovered == 1
    session = await InteractionStore(redis_client).get_active("user-1", "device-1")
    assert session.phase == "shopping"
    assert session.plugin_state == {"first_request": "add milk"}


async def test_worker_delivers_reply_with_committed_generation_binding(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    router.register_plugin("fixture_shopping", _ModePlugin())
    ingress = InteractionIngress(redis_client, router.interaction_registry)
    await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy add milk",
        source="streaming",
    )
    dispatch = await InteractionProcessor(redis_client, router).process(
        await _read_input(redis_client)
    )
    speech = AsyncMock()
    monkeypatch.setattr(interaction_mode_worker, "speak_on_device", speech)
    monkeypatch.setattr(interaction_mode_worker, "publish_sse", AsyncMock())

    await InteractionModeWorker(redis_client, router)._deliver(dispatch)

    speech.assert_awaited_once_with(
        redis_client,
        interaction_mode_worker.ClientId.from_value("device-1"),
        interaction_mode_worker.SessionId.from_value("audio-1"),
        "Started",
        generation=dispatch.session.response_generation,
        turn_id=dispatch.session.response_turn_id,
        turn_revision=dispatch.session.response_turn_revision,
    )


async def test_worker_routes_ordinary_committed_turn_through_voice_executor(
    monkeypatch,
):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    execute = AsyncMock(return_value="done")
    monkeypatch.setattr(interaction_mode_worker, "execute_voice_command", execute)
    turn = interaction_mode_worker.CommittedAudioTurn(
        interval=AudioInterval(
            audio_session_id="audio-1",
            capture_epoch=3,
            start_ms=100,
            end_ms=900,
            voice_session_id="voice-1",
            turn_id="turn-1",
        ),
        start_sequence=2,
        end_sequence=10,
        pcm=b"\x00\x00" * 6_400,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    )
    activation = WakeActivation(
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        user_id="user-1",
        client_id="device-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        wakeword="hey_hermes",
        armed_at=1_770_000_001.0,
        end_of_turn_at=1_770_000_001.8,
        command_start_ms=100,
        command_end_ms=900,
    )

    await InteractionModeWorker(redis_client, router)._dispatch_committed_command(
        turn,
        "turn on the lights",
        "user-1",
        "device-1",
        4,
        activation,
    )

    execute.assert_awaited_once_with(
        redis_client,
        router,
        user_id="user-1",
        session_id=interaction_mode_worker.SessionId.from_value("audio-1"),
        client_id=interaction_mode_worker.ClientId.from_value("device-1"),
        command="turn on the lights",
        source="committed",
        asr_status="committed_exact",
        capture_secs=0.8,
        response_generation=4,
        response_turn_id="turn-1",
        response_turn_revision=0,
        wakeword="hey_hermes",
        wake_trace_id=activation.wake_trace_id,
        interaction_stage_callback=execute.await_args.kwargs[
            "interaction_stage_callback"
        ],
    )
    record_stage = execute.await_args.kwargs["interaction_stage_callback"]
    await record_stage(
        "dispatched",
        {
            "dispatch_ms": 4250.0,
            "plugins": [{"plugin_id": "hermes", "duration_ms": 4200.0}],
        },
    )
    rows = await redis_client.xrange("wakeword:interaction-events")
    event = json.loads(rows[0][1]["event"])
    assert event["stage"] == "command_resolved"
    assert event["occurred_at"] >= activation.end_of_turn_at
    event = json.loads(rows[1][1]["event"])
    assert event["stage"] == "dispatched"
    assert event["wake_trace_id"] == activation.wake_trace_id
    assert event["payload"]["dispatch_ms"] == 4250.0


async def test_effect_fence_finishes_mutation_but_suppresses_stale_speech():
    entered = asyncio.Event()
    release = asyncio.Event()

    class _FencedPlugin(_ModePlugin):
        async def on_interaction_start(self, context):
            context.session.plugin_state = {"intent": "checkout"}
            await context.checkpoint()
            entered.set()
            await release.wait()
            return InteractionResult(
                reply="Order placed",
                phase="finished",
                plugin_state={"order": "committed"},
                end=True,
                end_reason="completed",
            )

    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    router = PluginRouter()
    router.register_plugin("fixture_shopping", _FencedPlugin())
    await InteractionIngress(redis_client, router.interaction_registry).submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy checkout",
        source="streaming",
    )
    item = await _read_input(redis_client)
    task = asyncio.create_task(InteractionProcessor(redis_client, router).process(item))
    await entered.wait()
    await ResponseCoordinator(
        redis_client, VoiceSessionCoordinator(redis_client)
    ).begin_turn("user-1", "device-1")
    release.set()

    dispatch = await task
    stored = await InteractionStore(redis_client).get(item.interaction_id)

    assert dispatch.reply is None
    assert dispatch.event_data["response_suppressed"] is True
    assert stored.plugin_state == {"order": "committed"}
    assert stored.status == "ended"


async def test_worker_main_initializes_otel_before_plugins(monkeypatch):
    events = []
    redis_options = []

    class _Redis:
        async def aclose(self):
            events.append("redis_closed")

    class _Worker:
        def __init__(self, redis_client, router, *, journal_projector):
            assert journal_projector is not None
            events.append("worker_created")

        async def run(self):
            events.append("worker_run")
            await asyncio.sleep(0)

    async def initialize_plugins(_router):
        events.append("plugins_initialized")

    async def recovery(_router):
        events.append("recovery_started")

    monkeypatch.setattr(
        interaction_mode_worker, "init_otel", lambda: events.append("otel_initialized")
    )

    def create_redis(**kwargs):
        redis_options.append(kwargs)
        return _Redis()

    monkeypatch.setattr(interaction_mode_worker, "create_async_redis", create_redis)
    monkeypatch.setattr(
        interaction_mode_worker, "initialize_redis_for_client_manager", Mock()
    )
    monkeypatch.setattr(interaction_mode_worker, "init_plugin_router", lambda: object())
    monkeypatch.setattr(
        interaction_mode_worker, "initialize_plugins", initialize_plugins
    )
    monkeypatch.setattr(interaction_mode_worker, "run_plugin_recovery", recovery)
    monkeypatch.setattr(interaction_mode_worker, "InteractionModeWorker", _Worker)
    monkeypatch.setattr(interaction_mode_worker, "start_loop_monitor", Mock())
    monkeypatch.setattr(interaction_mode_worker.signal, "signal", Mock())

    await interaction_mode_worker.main()

    assert events.index("otel_initialized") < events.index("plugins_initialized")
    assert redis_options == [{"decode_responses": False}]
    assert "worker_run" in events
    assert events[-1] == "redis_closed"


async def _activate_at(redis_client, router, now):
    ingress = InteractionIngress(redis_client, router.interaction_registry)
    result = await ingress.submit(
        user_id="user-1",
        client_id="device-1",
        audio_interval=_interval(),
        text="order swiggy",
        source="streaming",
        now=now,
    )
    return await InteractionStore(redis_client).get(result.interaction_id)
