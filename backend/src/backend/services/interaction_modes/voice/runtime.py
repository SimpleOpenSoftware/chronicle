"""Engaged voice on the existing interaction store, with asynchronous effects.

Capture is never owned here. Durable utterance payloads precede state/outbox
commit; effects own STT, models, playback, and tools outside state transitions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import asdict

from redis.exceptions import RedisError

import backend.services.audio_stream.v2_streams as v2_streams
import backend.services.dialogue.voice as voice_module
import backend.services.interaction_modes.committed_turns as committed_turns
import backend.services.interaction_modes.voice.realtime as realtime
import backend.services.interaction_modes.voice.worker as worker
from backend.audio_contract.v2 import audio_pb2 as pb
from backend.redis_keys import ClientId, SessionId, device_downlink_channel
from backend.services.audio_stream.session_store import SessionStore
from backend.services.response_coordinator import ResponseCoordinator, StaleResponse
from backend.services.response_delivery import deliver_pcm_response
from backend.services.voice_diagnostics import (
    VoiceCadenceRecorder,
    cadence_span,
    current_recorder,
)
from backend.services.voice_sessions import StaleVoiceBinding, VoiceSessionCoordinator
from backend.services.wakeword.activations import WakeActivationStore

from ..contracts import AudioInterval, InteractionSession
from ..episode_claims import AudioEpisodeArbiter
from ..ingress import InteractionIngressResult
from ..store import InteractionStore
from .engine import ModularVoiceEngine
from .engine_types import VoiceAudio, VoiceCompleted, VoiceText, VoiceToolCall
from .progress import VoiceProcessingPublisher
from .settings import VoiceSettings
from .tools import VoiceToolContext, VoiceTools

MODE = "voice_conversation"
LOGGER = logging.getLogger(__name__)
_TERMINAL_TASKS = {"completed", "failed", "unknown", "cancelled"}
MAX_ENGAGEMENT_TURNS = 100
MAX_ENGAGEMENT_TASKS = 8
MAX_HISTORY_BYTES = 512 * 1024
MAX_TRANSCRIPT_CHARS = 8192
MAX_RESPONSE_PHRASES = 512
MAX_RESPONSE_TEXT_CHARS = 8192


def _id(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(str(part) for part in parts)))


def _binding(session) -> pb.CaptureBinding:
    return pb.CaptureBinding(
        capture_session_id=pb.CaptureSessionId(value=session.audio_session_id),
        voice_session_id=pb.VoiceSessionId(value=session.voice_session_id or ""),
        capture_epoch=session.capture_epoch,
    )


def _matches(session, binding) -> bool:
    return _binding(session) == binding


def _effect(session, kind, *, task_id="", generation=None, suffix=""):
    return pb.VoiceEffect(
        effect_id=_id(
            session.interaction_id, session.revision + 1, kind, task_id, suffix
        ),
        interaction_id=session.interaction_id,
        kind=kind,
        generation=session.response_generation if generation is None else generation,
        task_id=task_id,
    )


def _response_effect(session, *, task_id="", suffix=""):
    effect = _effect(
        session, pb.VOICE_EFFECT_KIND_RESPONSE, task_id=task_id, suffix=suffix
    )
    session.plugin_state["processing_effect_id"] = effect.effect_id
    session.plugin_state["processing_effect_revision"] = session.revision + 1
    return effect


def _publish(session):
    return _effect(session, pb.VOICE_EFFECT_KIND_PUBLISH_STATE)


class VoiceConversationRuntime:
    def __init__(
        self,
        redis_client,
        *,
        engine=None,
        engine_factory=None,
        transcript_assembler=None,
        tools=None,
        settings=None,
        deliver=None,
        dialogue=None,
    ):

        self.dialogue = dialogue or voice_module.VoiceDialogue()
        self._dialogue_heartbeat_at = 0.0
        self._dialogue_poll_at = 0.0
        self.redis = redis_client
        self.store = InteractionStore(redis_client)
        self.voices = VoiceSessionCoordinator(redis_client)
        self.responses = ResponseCoordinator(redis_client, self.voices)
        self.engine = engine or ModularVoiceEngine()
        self.engine_factory = engine_factory
        self.transcripts = transcript_assembler
        self.tools = tools or VoiceTools.from_config()
        self.settings = settings or VoiceSettings.load()
        self.deliver = deliver or deliver_pcm_response
        self._worker = None
        self._engines = {}
        self._engine_locks = {}
        self._native_inputs = {}

    @asynccontextmanager
    async def _serial(self, user_id, client_id):
        """Short per-client control critical section; never holds provider I/O."""
        key = f"interaction:voice:control:{user_id}:{client_id}"
        token = str(uuid.uuid4())
        for _ in range(50):
            if await self.redis.set(key, token, nx=True, ex=10):
                break
            await asyncio.sleep(0.01)
        else:
            raise ValueError("voice control is busy; retry this command")
        try:
            yield
        finally:

            await worker.release_lease(self.redis, key, token)

    async def _validate(self, binding, user_id, client_id, socket_id):
        voice = await self.voices.get(binding.voice_session_id.value)
        view = await SessionStore(self.redis).read(binding.capture_session_id.value)
        if (
            voice is None
            or view is None
            or not _matches(voice, binding)
            or voice.user_id != user_id
            or voice.client_id != client_id
            or voice.socket_id != socket_id
            or view.connection_id != socket_id
            or view.user_id != user_id
            or view.client_id != client_id
            or view.voice_session_id != voice.voice_session_id
            or view.capture_epoch != voice.capture_epoch
            or voice.state not in {"ready_full", "ready_isolated"}
            or not (voice.capabilities or {}).get("incremental_playback")
        ):
            raise StaleVoiceBinding(
                "conversation requires the current incremental voice binding"
            )
        return voice, view

    async def control(
        self,
        command: pb.ConversationCommand,
        *,
        user_id: str,
        client_id: str,
        socket_id: str,
        event_id: str,
    ) -> pb.ConversationState:
        if not self.settings.enabled:
            raise ValueError("conversational voice is disabled")
        voice, view = await self._validate(
            command.binding, user_id, client_id, socket_id
        )
        marker = _id("voice-control", user_id, client_id, event_id)
        async with self._serial(user_id, client_id):
            session = await self.store.get_active(user_id, client_id)
            if command.action == pb.CONVERSATION_ACTION_CANCEL_TASK:
                if not command.interaction_id:
                    raise ValueError("task cancellation requires interaction identity")
                session = await self.store.get(command.interaction_id)
                if (
                    session is None
                    or session.user_id != user_id
                    or session.client_id != client_id
                ):
                    raise ValueError("task does not belong to this conversation")
            if session and (
                session.mode_id != MODE or not _matches(session, command.binding)
            ):
                raise ValueError("another interaction owns this device")
            if command.action == pb.CONVERSATION_ACTION_START:
                engine = command.engine or (
                    pb.SPEECH_ENGINE_MODULAR
                    if self.settings.default_engine == "modular"
                    else pb.SPEECH_ENGINE_REALTIME
                )
                if engine not in {pb.SPEECH_ENGINE_MODULAR, pb.SPEECH_ENGINE_REALTIME}:
                    raise ValueError("unsupported speech engine")
                if session is not None:
                    if (
                        command.thread_id
                        and session.plugin_state["thread_id"] != command.thread_id
                    ):
                        raise ValueError(
                            "End this voice engagement before switching threads"
                        )
                    if session.plugin_state["engine"] != engine:
                        raise ValueError(
                            "end this conversation before selecting another engine"
                        )
                    return self.state(session)
                if await self.store.is_processed(marker):
                    return self.state(None, binding=command.binding)
                session = await self._start(
                    voice, view, engine, marker, command.thread_id or None
                )
            elif command.action == pb.CONVERSATION_ACTION_END:
                if not command.interaction_id:
                    raise ValueError("end requires interaction identity")
                if session and session.interaction_id != command.interaction_id:
                    raise ValueError("end targets a different interaction")
                if session:
                    session = await self._end(session, "user_ended", marker)
            elif command.thread_id and command.action in {
                pb.CONVERSATION_ACTION_CANCEL_TASK,
                pb.CONVERSATION_ACTION_PAUSE_TASK,
                pb.CONVERSATION_ACTION_RESUME_TASK,
            }:
                if (
                    session is None
                    or session.plugin_state["thread_id"] != command.thread_id
                ):
                    raise ValueError("Task control must target this voice thread")
                await self.dialogue.control(session, command, marker)
            elif command.action == pb.CONVERSATION_ACTION_CANCEL_TASK:
                if (
                    session is None
                    or command.task_id not in session.plugin_state["tasks"]
                ):
                    raise ValueError("task does not belong to this conversation")

                def cancel(current):
                    task = current.plugin_state["tasks"][command.task_id]
                    if task["status"] in _TERMINAL_TASKS:
                        return []
                    task["status"] = "cancel_requested"
                    return [
                        _publish(current),
                        _effect(
                            current,
                            pb.VOICE_EFFECT_KIND_CANCEL_TASK,
                            task_id=command.task_id,
                            suffix="cancel",
                        ),
                    ]

                session, _ = await self.store.transition(
                    session.interaction_id, cancel, input_id=marker
                )
            elif command.action != pb.CONVERSATION_ACTION_SNAPSHOT:
                raise ValueError("unsupported conversation action")
        return self.state(session, binding=command.binding)

    async def _start(self, voice, view, engine, marker, thread_id=None):
        now = time.time()
        thread_id, history = await self.dialogue.open(
            voice.user_id,
            view.memory_space_id or None,
            voice.client_id,
            _id("engagement", marker),
            thread_id,
        )
        generation = await self.responses.begin_turn(voice.user_id, voice.client_id)
        session = InteractionSession(
            interaction_id=_id("engagement", marker),
            mode_id=MODE,
            owner_plugin_id="chronicle_voice",
            user_id=voice.user_id,
            client_id=voice.client_id,
            audio_session_id=voice.audio_session_id,
            capture_epoch=voice.capture_epoch,
            voice_session_id=voice.voice_session_id,
            response_generation=generation,
            response_turn_id=marker,
            response_turn_revision=0,
            phase="listening",
            plugin_state={
                "engine": engine,
                "socket_id": voice.socket_id,
                "memory_space_id": view.memory_space_id,
                "thread_id": thread_id,
                "history": history,
                "tasks": {},
                "turns": {},
                "input_open": False,
                "response": {},
                "pending_results": False,
            },
            started_at=now,
            last_activity_at=now,
            idle_timeout_seconds=self.settings.idle_timeout_seconds,
            max_duration_seconds=self.settings.max_duration_seconds,
        )
        if not await self.store.create(session):
            raise RuntimeError("interaction activation raced with another owner")
        session, _ = await self.store.transition(
            session.interaction_id, lambda s: [_publish(s)], input_id=marker
        )
        return session

    async def _end(self, session, reason, marker=None):
        generation = await self.responses.begin_turn(
            session.user_id, session.client_id, reason=reason
        )

        def end(current):
            current.status = "ended"
            current.phase = "ended"
            current.ended_at = time.time()
            current.end_reason = reason
            current.response_generation = generation
            return [_publish(current)]

        session, _ = await self.store.transition(
            session.interaction_id, end, input_id=marker
        )
        await self.dialogue.close(session)
        return session

    async def end_for_capture(self, *, user_id, client_id, binding, reason):
        async with self._serial(user_id, client_id):
            session = await self.store.get_active(user_id, client_id)
            if session and session.mode_id == MODE and _matches(session, binding):
                await self._end(session, reason)

    async def _end_response_if_current(self, session, effect, reason):
        """Late provider work cannot cancel another engagement or newer turn."""
        async with self._serial(session.user_id, session.client_id):
            current = await self.store.get_active(session.user_id, session.client_id)
            if (
                current is not None
                and current.interaction_id == session.interaction_id
                and current.response_generation == effect.generation
            ):
                await self._end(current, reason)

    async def enqueue_committed(self, turn, voice):
        if not (voice.capabilities or {}).get("incremental_playback"):
            return None
        if not self.settings.enabled:
            return InteractionIngressResult(consumed=True, reason="voice_disabled")
        work_id = _id(
            "voice-turn",
            voice.user_id,
            voice.client_id,
            turn.interval.audio_session_id,
            turn.interval.capture_epoch,
            turn.interval.turn_id,
            turn.interval.turn_revision,
        )
        async with self._serial(voice.user_id, voice.client_id):
            session = await self.store.get_active(voice.user_id, voice.client_id)
            if session and session.mode_id != MODE:
                return None
            if await self.store.is_processed(work_id):
                return InteractionIngressResult(
                    consumed=True, reason="already_enqueued"
                )
            if session is None:
                activation = await WakeActivationStore(self.redis).claim(
                    turn.interval, owner_id=work_id
                )
                if activation is None:
                    return InteractionIngressResult(
                        consumed=True, reason="not_addressed"
                    )
                if (
                    activation.user_id != voice.user_id
                    or activation.client_id != voice.client_id
                ):
                    raise ValueError("wake activation does not match voice owner")
                view = await SessionStore(self.redis).read(voice.audio_session_id)
                if view is None:
                    raise StaleVoiceBinding("capture ended before activation")
                engine = (
                    pb.SPEECH_ENGINE_MODULAR
                    if self.settings.default_engine == "modular"
                    else pb.SPEECH_ENGINE_REALTIME
                )
                session = await self._start(voice, view, engine, _id("wake", work_id))
            if not _matches(session, _binding(voice)):
                raise StaleVoiceBinding("committed turn cannot rebind an engagement")
            if (
                len(session.plugin_state["turns"]) >= MAX_ENGAGEMENT_TURNS
                or len(json.dumps(session.plugin_state["history"]).encode())
                >= MAX_HISTORY_BYTES
            ):
                await self._end(session, "conversation_capacity_reached")
                return InteractionIngressResult(
                    consumed=True, reason="conversation_capacity_reached"
                )
            claim = await AudioEpisodeArbiter(self.redis).claim(
                user_id=voice.user_id,
                client_id=voice.client_id,
                interval=turn.interval,
                source="committed",
                owner_id=work_id,
            )
            if not claim.accepted:
                return InteractionIngressResult(
                    consumed=True, reason="episode_already_claimed"
                )
            # Immutable PCM payload exists before the transaction references it.
            payload = {
                "metadata": json.dumps(
                    {key: value for key, value in asdict(turn).items() if key != "pcm"}
                ),
                "pcm": turn.pcm,
            }
            key = f"interaction:voice:turn:{work_id}"
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=payload)
                pipe.expire(key, 86400)
                await pipe.execute()
            generation = await self.responses.begin_turn(voice.user_id, voice.client_id)

            def enqueue(current):
                if current.status != "active":
                    return []
                current.response_generation = generation
                current.response_turn_id = turn.interval.turn_id or work_id
                current.response_turn_revision = turn.interval.turn_revision
                current.phase = "thinking"
                current.last_activity_at = time.time()
                current.plugin_state["input_open"] = False
                current.plugin_state["turns"][work_id] = {
                    "status": "queued",
                    "generation": generation,
                    "audio_interval": asdict(turn.interval),
                }
                return [
                    _publish(current),
                    _response_effect(current, task_id=work_id),
                ]

            session, applied = await self.store.transition(
                session.interaction_id, enqueue, input_id=work_id
            )
            return InteractionIngressResult(
                consumed=True,
                accepted=applied,
                interaction_id=session.interaction_id,
                mode_id=MODE,
            )

    async def speech_onset(self, event, *, event_id):
        if event.get("kind") not in {"opened", "reopened", "cancelled"}:
            return
        voice = await self.voices.get(event.get("voice_session_id", ""))
        if voice is None:
            return
        async with self._serial(voice.user_id, voice.client_id):
            session = await self.store.get_active(voice.user_id, voice.client_id)
            if (
                session is None
                or session.mode_id != MODE
                or event.get("audio_session_id") != session.audio_session_id
                or int(event.get("capture_epoch", -1)) != session.capture_epoch
                or voice.socket_id != session.plugin_state["socket_id"]
            ):
                return
            marker = _id("voice-onset", event_id)
            if await self.store.is_processed(marker):
                return
            if event.get("kind") == "cancelled":

                def abandon(current):
                    current.plugin_state["input_open"] = False
                    current.phase = "listening"
                    current.last_activity_at = time.time()
                    effects = [_publish(current)]
                    if current.plugin_state["pending_results"]:
                        current.phase = "thinking"
                        effects.append(_response_effect(current, suffix="task_result"))
                    return effects

                await self.store.transition(
                    session.interaction_id, abandon, input_id=marker
                )
                pending = self._native_inputs.get(session.interaction_id)
                if pending:
                    pending["task"].cancel()
                return
            generation = await self.responses.begin_turn(
                session.user_id, session.client_id, reason="speech_onset"
            )

            def interrupt(current):
                current.response_generation = generation
                current.phase = "listening"
                current.plugin_state["input_open"] = True
                current.last_activity_at = time.time()
                return [_publish(current)]

            session, _ = await self.store.transition(
                session.interaction_id, interrupt, input_id=marker
            )
        if session.plugin_state["engine"] == pb.SPEECH_ENGINE_REALTIME:
            await self._start_native_input(session, event)

    async def _engine_for(self, session):
        identity = session.interaction_id
        if identity not in self._engines:
            if self.engine_factory:
                engine = self.engine_factory(session.plugin_state["engine"])
            elif session.plugin_state["engine"] == pb.SPEECH_ENGINE_REALTIME:

                engine = realtime.OpenAIRealtimeEngine()
            else:
                engine = self.engine
            self._engines[identity] = engine
        return self._engines[identity]

    async def _start_native_input(self, session, event):
        """Own a bounded live subscription; capture itself remains elsewhere."""
        identity = session.interaction_id
        existing = self._native_inputs.get(identity)
        if existing and existing["turn_id"] == event.get("turn_id"):
            return  # Reopening extends the same provider input buffer.
        if existing:
            existing["task"].cancel()
            await asyncio.gather(existing["task"], return_exceptions=True)
        start = int(event.get("start_sequence", -1))
        if start < 0:
            return
        state = {
            "turn_id": event.get("turn_id"),
            "start": start,
            "end": start - 1,
            "samples": 0,
            "digest": hashlib.sha256(),
            "complete": False,
            "inflight": False,
        }
        state["task"] = asyncio.create_task(self._feed_native(session, state))
        self._native_inputs[identity] = state

    async def _feed_native(self, session, state):

        verify_privacy = await self.dialogue.guard(session)
        engine = await self._engine_for(session)
        # Seed text only once in the adapter; subsequent opens are idempotent.
        history = [
            {
                k: v
                for k, v in m.items()
                if not k.startswith("_voice_") and k != "interrupted"
            }
            for m in session.plugin_state["history"]
        ]
        lock = self._engine_locks.setdefault(session.interaction_id, asyncio.Lock())
        async with lock:
            await engine.open(history=history, tool_schemas=self.tools.schemas())
            await engine.clear_input()
        stream = v2_streams.realtime_stream(session.audio_session_id)
        # A turn is capped at60s. Bound replay lookup and reject a missing prefix.
        recent = await self.redis.xrevrange(stream, count=3100)
        cursor = recent[-1][0] if recent else "0-0"
        pending = list(reversed(recent))
        deadline = asyncio.get_running_loop().time() + 65
        try:
            while asyncio.get_running_loop().time() < deadline:
                await verify_privacy()
                current = await self.store.get(session.interaction_id)
                if current is None or current.status != "active":
                    return
                for identity, fields in pending:
                    cursor = identity
                    event = pb.CaptureStreamEvent.FromString(
                        fields.get(b"event") or fields.get("event")
                    )
                    if event.WhichOneof("event") != "frame":
                        continue
                    frame = event.frame
                    if (
                        frame.binding != _binding(session)
                        or frame.delivery_class != pb.DELIVERY_CLASS_LIVE
                    ):
                        raise StaleVoiceBinding("native feed capture binding changed")
                    if frame.sequence < state["start"]:
                        continue
                    if frame.sequence != state["end"] + 1:
                        raise ValueError("native live input has a sequence gap")
                    if state["samples"] + len(frame.pcm_s16le) // 2 > 16000 * 60:
                        raise ValueError("native live input exceeded utterance bound")
                    state["inflight"] = True
                    await engine.append_audio(frame.pcm_s16le)
                    state["inflight"] = False
                    state["digest"].update(frame.pcm_s16le)
                    state["samples"] += len(frame.pcm_s16le) // 2
                    state["end"] = frame.sequence
                pending = []
                for _, entries in await self.redis.xread({stream: cursor}, count=50):
                    pending.extend(entries)
                if not pending:
                    await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "Native live input unavailable; exact commit reconciliation required"
            )
        finally:
            state["complete"] = True

    async def _commit_native_input(self, session, turn, engine):
        state = self._native_inputs.pop(session.interaction_id, None)
        if state:
            state["task"].cancel()
            await asyncio.gather(state["task"], return_exceptions=True)
        # Independent ingress/turn streams can race at the trailing boundary.
        # Only an exact canonical buffer is ever committed to the provider.
        exact = (
            state
            and not state["inflight"]
            and state["start"] == turn.start_sequence
            and state["end"] == turn.end_sequence
        )
        exact = exact and state["digest"].digest() == hashlib.sha256(turn.pcm).digest()
        if not exact:
            await engine.clear_input()
            for offset in range(0, len(turn.pcm), 3200):
                await engine.append_audio(turn.pcm[offset : offset + 3200])

    def state(self, session, *, binding=None):
        if session is None:
            return pb.ConversationState(
                binding=binding, phase=pb.CONVERSATION_PHASE_ENDED
            )
        state = session.plugin_state
        result = pb.ConversationState(
            binding=_binding(session),
            interaction_id=session.interaction_id,
            thread_id=state["thread_id"],
            revision=session.revision,
            response_generation=session.response_generation,
            response_effect_id=state.get("processing_effect_id", ""),
            engine=state["engine"],
            phase={
                "listening": pb.CONVERSATION_PHASE_LISTENING,
                "thinking": pb.CONVERSATION_PHASE_THINKING,
                "speaking": pb.CONVERSATION_PHASE_SPEAKING,
                "ended": pb.CONVERSATION_PHASE_ENDED,
            }[session.phase],
            detail=session.end_reason or state.get("detail", ""),
            transcript=state.get("transcript", ""),
            response_text=state.get("response", {}).get("text", ""),
            vault_retrieval_enabled=self.settings.vault_retrieval_enabled,
        )
        statuses = {
            name: getattr(pb, "VOICE_TASK_STATUS_" + name.upper())
            for name in (
                "queued",
                "running",
                "completed",
                "failed",
                "cancel_requested",
                "cancelled",
                "unknown",
            )
        }
        for task_id, task in state["tasks"].items():
            result.tasks.add(
                task_id=task_id,
                tool_name=task["name"],
                status=statuses[task["status"]],
                detail=task.get("detail", ""),
            )
        return result

    async def publish_state(self, session):
        await self.redis.publish(
            str(device_downlink_channel(ClientId.from_value(session.client_id))),
            pb.DeviceDownlinkEvent(
                conversation_state=self.state(session)
            ).SerializeToString(),
        )

    async def execute_effect(self, effect):
        session = await self.store.get(effect.interaction_id)
        if session is None or session.mode_id != MODE:
            return
        if effect.kind == pb.VOICE_EFFECT_KIND_PUBLISH_STATE:
            await self.publish_state(session)
        elif effect.kind == pb.VOICE_EFFECT_KIND_RESPONSE:
            lock = self._engine_locks.setdefault(session.interaction_id, asyncio.Lock())
            async with lock:
                session = await self.store.get(effect.interaction_id)
                if session is None:
                    return
                if (
                    session.status != "active"
                    or session.response_generation != effect.generation
                    or session.plugin_state.get("processing_effect_id")
                    != effect.effect_id
                ):
                    await self._mark_obsolete(effect)
                    return
                diagnostic = VoiceCadenceRecorder(
                    user_id=session.user_id,
                    client_id=session.client_id,
                    capture_session_id=session.audio_session_id,
                    voice_session_id=session.voice_session_id,
                    capture_epoch=session.capture_epoch,
                    turn_id=session.response_turn_id,
                    generation=effect.generation,
                    interaction_id=session.interaction_id,
                    effect_id=effect.effect_id,
                )
                outcome = "returned"
                try:
                    with diagnostic.bind(), cadence_span("response_effect"):
                        async with VoiceProcessingPublisher(
                            self.redis,
                            session=session,
                            generation=effect.generation,
                            binding=_binding(session),
                            effect_id=effect.effect_id,
                            state_revision=session.plugin_state[
                                "processing_effect_revision"
                            ],
                        ) as processing:
                            await self._respond(session, effect, processing)
                except StaleResponse:
                    outcome = "StaleResponse"
                    await self._mark_obsolete(effect)
                    raise
                except BaseException as error:
                    outcome = type(error).__name__
                    raise
                finally:
                    await diagnostic.flush(outcome)
        elif effect.kind == pb.VOICE_EFFECT_KIND_TASK:
            await self._task(session, effect.task_id)
        elif effect.kind == pb.VOICE_EFFECT_KIND_CANCEL_TASK:
            await self._task(session, effect.task_id, cancelling=True)

    async def _mark_obsolete(self, effect):
        def obsolete(current):
            work = current.plugin_state["turns"].get(effect.task_id)
            if work is not None and work["status"] == "queued":
                work["status"] = "interrupted"
                work["interrupted"] = True
                current.plugin_state.setdefault("response_effects", {})[
                    effect.effect_id
                ] = "failed"
            return []

        await self.store.transition(effect.interaction_id, obsolete)

    async def _while_current(self, awaitable, session, generation, *, timeout):
        """Stop waiting on an obsolete provider before final STT is available."""
        task = asyncio.ensure_future(awaitable)
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            verify_privacy = await self.dialogue.guard(session)
            while not task.done():
                await verify_privacy()
                await self.responses.assert_generation(
                    session.user_id, session.client_id, generation
                )
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("voice provider deadline exceeded")
                await asyncio.wait({task}, timeout=0.02)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _respond(self, session, effect, processing):

        previous = session.plugin_state.get("response_effects", {}).get(
            effect.effect_id
        )
        stored_response = session.plugin_state.get("response", {})
        recovery_response_id = (
            stored_response.get("response_id")
            if stored_response.get("effect_id") == effect.effect_id
            else None
        )
        recovery_phrases = (
            stored_response.get("phrases", []) if recovery_response_id else []
        )
        if previous in {"completed", "failed"}:
            return
        if previous == "generating":
            await self._response_finished(
                session,
                effect,
                recovery_phrases,
                "worker_interrupted",
                response_id=recovery_response_id,
                generated_text=stored_response.get("generated_text", ""),
            )
            return
        work = session.plugin_state["turns"].get(effect.task_id)
        if work is not None and work["status"] in {
            "completed",
            "failed",
            "interrupted",
        }:
            return
        if work is not None and work["status"] == "generating":
            # A previously claimed provider generation cannot safely be replayed.
            await self._response_finished(
                session,
                effect,
                recovery_phrases,
                "worker_interrupted",
                response_id=recovery_response_id,
                generated_text=stored_response.get("generated_text", ""),
            )
            return
        text = None
        dialogue_continuation = None
        native_transcript_task = None
        turn = None
        native = session.plugin_state["engine"] == pb.SPEECH_ENGINE_REALTIME
        committed_utterance = None
        if effect.task_id.startswith("utterance:"):
            committed_utterance = await self.dialogue.delivery(
                session, effect.task_id.removeprefix("utterance:")
            )
            if committed_utterance is None:
                return
            native = False
        if work is not None:
            # Transport failures remain recoverable; invalid stored input does not.
            raw = await self.redis.hgetall(f"interaction:voice:turn:{effect.task_id}")
            try:
                metadata = json.loads(
                    raw.get(b"metadata") or raw.get("metadata") or "{}"
                )
                if not isinstance(metadata, dict) or not metadata:
                    raise ValueError(
                        "durable voice turn metadata is missing or invalid"
                    )
                if metadata.get("interval") != work["audio_interval"]:
                    raise ValueError(
                        "durable voice turn identity does not match admission"
                    )
                metadata["interval"] = AudioInterval(**metadata["interval"])
                turn = committed_turns.CommittedAudioTurn(
                    **metadata, pcm=raw.get(b"pcm") or raw.get("pcm")
                )
                if (
                    not isinstance(turn.pcm, bytes)
                    or not turn.pcm
                    or len(turn.pcm) % 2
                    or (turn.sample_rate, turn.channels, turn.sample_width)
                    != (16000, 1, 2)
                    or type(turn.start_sequence) is not int
                    or type(turn.end_sequence) is not int
                    or turn.start_sequence < 0
                    or turn.end_sequence < turn.start_sequence
                ):
                    raise ValueError("durable voice turn is not valid canonical PCM")
            except (ValueError, TypeError, KeyError) as exc:
                LOGGER.exception(
                    "Invalid durable voice input for %s", session.interaction_id
                )
                await self._response_finished(
                    session, effect, [], "committed_input_invalid:" + type(exc).__name__
                )
                return
            if not native:
                try:
                    assembler = (
                        self.transcripts
                        or committed_turns.CommittedTranscriptAssembler(
                            self.redis, allow_provider_fallback=False
                        )
                    )
                    hints = await self.dialogue.hints(session)
                    transcription_options = {"context_info": hints} if hints else {}
                    # Exact dynamic utterance; no final-word watermark wait.
                    with processing.activity("transcribing"):
                        text = (
                            await self._while_current(
                                assembler.exact_transcriber(
                                    turn.pcm,
                                    turn.sample_rate,
                                    turn.channels,
                                    turn.sample_width,
                                    **transcription_options,
                                ),
                                session,
                                effect.generation,
                                timeout=60,
                            )
                        ).strip()
                except (StaleResponse, RedisError):
                    raise
                except Exception as exc:
                    LOGGER.exception(
                        "Voice transcription failed for %s", session.interaction_id
                    )
                    await self._response_finished(
                        session,
                        effect,
                        [],
                        "transcription_failed:" + type(exc).__name__,
                    )
                    return
        if text is not None and len(text) > MAX_TRANSCRIPT_CHARS:
            await self._end_response_if_current(
                session, effect, "conversation_capacity_reached"
            )
            return
        await self.responses.assert_generation(
            session.user_id, session.client_id, effect.generation
        )

        def begin(current):
            if (
                current.status != "active"
                or current.response_generation != effect.generation
            ):
                raise StaleResponse("voice response superseded during transcription")
            current.plugin_state.setdefault("response_effects", {})[
                effect.effect_id
            ] = "generating"
            if text:
                current.plugin_state["history"].append(
                    {
                        "role": "user",
                        "content": text,
                        "_voice_generation": effect.generation,
                    }
                )
                current.plugin_state["transcript"] = text
            if work is not None:
                current.plugin_state["turns"][effect.task_id]["status"] = "generating"
            current.plugin_state["response"] = {
                "effect_id": effect.effect_id,
                "text": "",
                "phrases": [],
                "response_id": "",
            }
            current.plugin_state["pending_results"] = False
            return [_publish(current)]

        if text and turn is not None:
            dialogue_continuation = await self.dialogue.input(
                session, effect, text, turn.interval
            )
        session, _ = await self.store.transition(session.interaction_id, begin)
        history = (
            session.plugin_state["history"][:-1]
            if text
            else session.plugin_state["history"]
        )
        history = [
            {
                key: value
                for key, value in message.items()
                if not key.startswith("_voice_") and key != "interrupted"
            }
            for message in history
        ]
        if work is not None and not text and not native:
            await self._response_finished(session, effect, [], None)
            return
        try:
            engine = await self._engine_for(session)
        except RedisError:
            raise
        except Exception as exc:
            LOGGER.exception("Voice engine setup failed for %s", session.interaction_id)
            await self._response_finished(
                session, effect, [], "engine_setup_failed:" + type(exc).__name__
            )
            return
        cancellation = asyncio.Event()
        verify_privacy = await self.dialogue.guard(session)
        privacy_error = None

        async def watch_generation():
            nonlocal privacy_error
            last_policy_check = 0
            while True:
                try:
                    if time.monotonic() - last_policy_check >= 0.25:
                        await verify_privacy()
                        last_policy_check = time.monotonic()
                    await self.responses.assert_generation(
                        session.user_id, session.client_id, effect.generation
                    )
                except StaleResponse:
                    cancellation.set()
                    return
                except Exception as exc:
                    privacy_error = exc
                    cancellation.set()
                    await self.responses.begin_turn(session.user_id, session.client_id)
                    return
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(watch_generation())
        phrases = []
        generated_text = []
        tool_calls = []
        error = None
        response_id = None
        active_phrase_index = None
        checkpointed_phrases = []

        async def checkpoint_phrases():
            nonlocal checkpointed_phrases
            if (
                len(phrases) > MAX_RESPONSE_PHRASES
                or sum(len(phrase["text"]) for phrase in phrases)
                > MAX_RESPONSE_TEXT_CHARS
            ):
                phrases[:] = [dict(phrase) for phrase in checkpointed_phrases]
                raise ValueError("voice response phrase metadata exceeded its bound")
            snapshot = [dict(phrase) for phrase in phrases]

            def checkpoint(current):
                response = current.plugin_state["response"]
                if (
                    current.status != "active"
                    or current.response_generation != effect.generation
                    or response.get("effect_id") != effect.effect_id
                ):
                    raise StaleResponse("phrase checkpoint superseded")
                response["phrases"] = snapshot
                response["generated_text"] = "".join(generated_text)
                return []

            await self.store.transition(session.interaction_id, checkpoint)
            checkpointed_phrases = snapshot

        async def record_queued(response):
            nonlocal response_id
            response_id = response.response_id
            processing.set_response(response_id)

            def record(current):
                if current.response_generation != effect.generation:
                    raise StaleResponse("response queued after interruption")
                current.plugin_state["response"]["response_id"] = response.response_id
                current.phase = "speaking"
                return [_publish(current)]

            await self.store.transition(session.interaction_id, record)

        async def observed_native(events):
            while True:
                try:
                    with processing.activity("generating_response"):
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                yield event

        async def pcm_events(events):
            nonlocal active_phrase_index
            async for event in events:
                if isinstance(event, VoiceAudio):
                    first_chunk = active_phrase_index != event.phrase_index
                    if first_chunk:
                        active_phrase_index = event.phrase_index
                        phrases.append(
                            {
                                "text": event.phrase_text,
                                "start_sample": event.start_sample,
                                "end_sample": None,
                            }
                        )
                    if event.phrase_final:
                        phrases[-1]["end_sample"] = event.end_sample
                    # At most two writes per phrase, before its first/last audio
                    # becomes publishable. No per-packet snapshot journal growth.
                    if first_chunk or event.phrase_final:
                        await checkpoint_phrases()
                    yield event.pcm
                elif isinstance(event, VoiceToolCall):
                    tool_calls.append(event)
                elif isinstance(event, VoiceText):
                    if native:
                        generated_text.append(event.text)
                        await checkpoint_phrases()
                    # Synthesize the next bounded phrase while the coordinator
                    # plays its existing two-second reservoir. Waiting for the
                    # previous phrase to drain adds the full synthesis latency
                    # as silence. The engine retains its current phrase and one
                    # bounded prefetch; coordinator backpressure still bounds
                    # transport delivery to two seconds.
                    continue
                elif isinstance(event, VoiceCompleted):
                    generated_text[:] = [event.text]
                    if native and event.text:
                        phrases[:] = [
                            {
                                "text": event.text,
                                "start_sample": 0,
                                "end_sample": event.total_samples,
                            }
                        ]
                        await checkpoint_phrases()
                else:
                    raise ValueError("unsupported voice engine event")

        try:
            options = (
                {}
                if native
                else {
                    "activity": processing.observe,
                    "generated": generated_text.append,
                }
            )
            if native:
                await engine.open(history=history, tool_schemas=self.tools.schemas())
                if turn is not None:
                    if (turn.sample_rate, turn.channels, turn.sample_width) != (
                        16000,
                        1,
                        2,
                    ):
                        raise ValueError(
                            "native voice requires canonical PCM16 mono16k"
                        )
                    await self._commit_native_input(session, turn, engine)
                else:
                    for task in session.plugin_state["tasks"].values():
                        if "result" in task and not task.get("native_submitted"):
                            await engine.submit_tool_result(
                                task["call_id"], json.dumps(task["result"])
                            )

                            def submitted(current, call_id=task["call_id"]):
                                for current_task in current.plugin_state[
                                    "tasks"
                                ].values():
                                    if current_task["call_id"] == call_id:
                                        current_task["native_submitted"] = True
                                return []

                            await self.store.transition(
                                session.interaction_id, submitted
                            )
                options["commit_audio"] = turn is not None
                if turn is not None:

                    def record_native_input(transcript):
                        nonlocal native_transcript_task
                        if transcript.strip():
                            native_transcript_task = asyncio.create_task(
                                self.dialogue.input(
                                    session, effect, transcript.strip(), turn.interval
                                )
                            )

                    options["input_transcript"] = record_native_input
            async with aclosing(
                self.engine.speak(committed_utterance.text, cancellation=cancellation)
                if committed_utterance
                else engine.generate(
                    text=text,
                    history=history,
                    tool_schemas=self.tools.schemas(),
                    cancellation=cancellation,
                    **options,
                )
            ) as events:
                async with AsyncExitStack() as cleanup:
                    if native:
                        events = await cleanup.enter_async_context(
                            aclosing(observed_native(events))
                        )
                    pcm = await cleanup.enter_async_context(
                        aclosing(pcm_events(events))
                    )
                    first = await asyncio.wait_for(anext(pcm, None), 60)
                    if first is not None:

                        async def prefetched():
                            yield first
                            async for chunk in pcm:
                                yield chunk

                        await self.deliver(
                            self.redis,
                            ClientId.from_value(session.client_id),
                            SessionId.from_value(session.audio_session_id),
                            prefetched(),
                            generation=effect.generation,
                            turn_id=session.response_turn_id,
                            turn_revision=session.response_turn_revision,
                            on_queued=record_queued,
                        )
            if tool_calls:
                if native_transcript_task:
                    dialogue_continuation = await native_transcript_task
                shared_calls = [
                    call
                    for call in tool_calls
                    if call.name in {"ask_user", "start_task", "delegate_to_hermes"}
                ]
                for call in shared_calls:
                    result = await self.dialogue.tool(
                        session, effect, call, dialogue_continuation
                    )
                    if native:
                        await engine.submit_tool_result(
                            call.call_id, json.dumps(result)
                        )

                    def shared_result(current, call=call, result=result):
                        current.plugin_state["history"].extend(
                            [
                                {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": call.call_id,
                                            "type": "function",
                                            "function": {
                                                "name": call.name,
                                                "arguments": json.dumps(call.arguments),
                                            },
                                        }
                                    ],
                                },
                                {
                                    "role": "tool",
                                    "tool_call_id": call.call_id,
                                    "content": json.dumps(result),
                                },
                            ]
                        )
                        current.plugin_state["pending_results"] = not result.get(
                            "utterance_id"
                        )
                        return []

                    await self.store.transition(session.interaction_id, shared_result)
                remaining = [call for call in tool_calls if call not in shared_calls]
                if remaining:
                    await self._queue_tools(session, effect, remaining)
        except (StaleResponse, asyncio.CancelledError):
            error = "interrupted"
            if not cancellation.is_set():
                raise
        except Exception as exc:
            error = type(exc).__name__
            LOGGER.exception("Voice response failed for %s", session.interaction_id)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if privacy_error is not None:
                error = "privacy_changed"
            if native_transcript_task:
                try:
                    dialogue_continuation = await asyncio.wait_for(
                        asyncio.shield(native_transcript_task), 60
                    )
                except Exception:
                    native_transcript_task.cancel()
                    await asyncio.gather(native_transcript_task, return_exceptions=True)
                    LOGGER.exception("Native dialogue transcript recording failed")
            await self._response_finished(
                session,
                effect,
                phrases,
                error,
                response_id=response_id,
                dialogue_continuation=dialogue_continuation,
                generated_text="".join(generated_text),
            )
            if native and error:
                record = await self.responses.get(response_id) if response_id else None
                try:
                    await engine.interrupt(record.rendered_samples if record else 0)
                except Exception:
                    LOGGER.exception("Native interrupted-context reconciliation failed")
                    await engine.close()
                    self._engines.pop(session.interaction_id, None)

    async def _response_finished(
        self,
        session,
        effect,
        phrases,
        error,
        *,
        response_id=None,
        dialogue_continuation=None,
        generated_text="",
    ):
        diagnostic = current_recorder()
        if diagnostic is not None:
            diagnostic.terminal_outcome = error or "completed"
        current = await self.store.get(session.interaction_id)
        if current is None:
            return
        record = await self.responses.get(response_id) if response_id else None
        if error and record is not None and not record.terminal_ack_state:
            deadline = asyncio.get_running_loop().time() + 0.3
            while (
                not record.terminal_ack_state
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
                record = await self.responses.get(response_id)
                if record is None:
                    break
        heard_samples = record.rendered_samples if record else 0
        heard = " ".join(
            phrase["text"]
            for phrase in phrases
            if phrase["end_sample"] is not None
            and phrase["end_sample"] <= heard_samples
        )
        unconfirmed = bool(
            response_id
            and phrases
            and any(
                phrase["end_sample"] is None or phrase["end_sample"] > heard_samples
                for phrase in phrases
            )
            and error
        )
        history_content = heard
        if unconfirmed:
            history_content = (heard + "\n" if heard else "") + (
                "[Assistant audio was interrupted; no additional words are confirmed as heard.]"
            )

        # Commit communication before publishing a terminal transport checkpoint.
        # Recovery can repeat this stable upsert without losing or duplicating text.
        from backend.services.privacy import PrivacyHeld

        utterance_id = None
        try:
            utterance_id = await self.dialogue.output(
                session,
                effect,
                generated_text,
                heard,
                heard_samples,
                response_id,
                error,
                dialogue_continuation,
            )
        except PrivacyHeld:
            error = "privacy_changed"

        def finish(state):
            if utterance_id and generated_text:
                state.plugin_state["utterance_ids"] = list(
                    dict.fromkeys(
                        [*state.plugin_state.get("utterance_ids", []), utterance_id]
                    )
                )[-200:]
            state.plugin_state.setdefault("response_effects", {})[effect.effect_id] = (
                "failed" if error else "completed"
            )
            work = state.plugin_state["turns"].get(effect.task_id)
            if work:
                work["status"] = (
                    "interrupted"
                    if error == "interrupted"
                    else "failed" if error else "completed"
                )
                work["heard_samples"] = heard_samples
                work["heard_text"] = heard
                work["interrupted"] = bool(error)
                work["unconfirmed_partial"] = unconfirmed
            if history_content:
                messages = state.plugin_state["history"]
                index = next(
                    (
                        index + 1
                        for index, message in enumerate(messages)
                        if message.get("_voice_generation") == effect.generation
                        and message.get("role") == "user"
                    ),
                    len(messages),
                )
                messages.insert(
                    index,
                    {
                        "role": "assistant",
                        "content": history_content,
                        "_voice_generation": effect.generation,
                        "interrupted": bool(error) or unconfirmed,
                    },
                )
            if (
                state.status != "active"
                or state.response_generation != effect.generation
            ):
                return []
            state.phase = "listening"
            state.last_activity_at = time.time()
            state.plugin_state["response"]["text"] = heard
            state.plugin_state["response"]["effect_id"] = effect.effect_id
            state.plugin_state["response"]["response_id"] = response_id or ""
            state.plugin_state["response"]["rendered_samples"] = heard_samples
            state.plugin_state["response"]["phrases"] = phrases
            state.plugin_state["response"]["interrupted"] = bool(error)
            state.plugin_state["response"]["unconfirmed_partial"] = unconfirmed
            state.plugin_state["detail"] = error or ""
            effects = [_publish(state)]
            if (
                state.plugin_state["pending_results"]
                and not state.plugin_state["input_open"]
            ):
                effects.append(_response_effect(state, suffix="task_result"))
                state.phase = "thinking"
            return effects

        await self.store.transition(session.interaction_id, finish)

    async def _queue_tools(self, session, effect, calls):
        latest = await self.store.get(session.interaction_id)
        if (
            latest
            and len(latest.plugin_state["tasks"]) + len(calls) > MAX_ENGAGEMENT_TASKS
        ):
            await self._end_response_if_current(
                latest, effect, "conversation_task_limit_reached"
            )
            return

        def queue(current):
            if (
                current.status != "active"
                or current.response_generation != effect.generation
            ):
                return []
            effects = []
            for call in calls:
                task_id = _id(session.interaction_id, effect.effect_id, call.call_id)
                if task_id in current.plugin_state["tasks"]:
                    continue
                current.plugin_state["tasks"][task_id] = {
                    "name": call.name,
                    "arguments": call.arguments,
                    "call_id": call.call_id,
                    "status": "queued",
                    "state": {},
                    "detail": "",
                }
                effects.append(
                    _effect(current, pb.VOICE_EFFECT_KIND_TASK, task_id=task_id)
                )
            return effects + [_publish(current)]

        await self.store.transition(session.interaction_id, queue)

    async def _task(self, session, task_id, *, cancelling=False):
        task = session.plugin_state["tasks"].get(task_id)
        if task is None or task["status"] in _TERMINAL_TASKS:
            return

        def context(state):
            current_task = state.plugin_state["tasks"][task_id]
            return VoiceToolContext(
                user_id=state.user_id,
                memory_space_id=state.plugin_state["memory_space_id"] or None,
                interaction_id=state.interaction_id,
                task_id=task_id,
                history=tuple(state.plugin_state["history"][-12:]),
                state=current_task["state"],
            )

        async def checkpoint(patch):
            def update(current):
                item = current.plugin_state["tasks"][task_id]
                if patch.get("submission_started") and item["status"] != "running":
                    raise StaleResponse("task cancelled before remote submission")
                item["state"].update(patch)
                if patch.get("remote_run_id") and item["status"] == "cancel_requested":
                    return [
                        _effect(
                            current,
                            pb.VOICE_EFFECT_KIND_CANCEL_TASK,
                            task_id=task_id,
                            suffix="remote_identified",
                        )
                    ]
                return []

            await self.store.transition(session.interaction_id, update)

        async def progress(detail):
            latest = await self.store.get(session.interaction_id)
            if (
                latest is None
                or latest.plugin_state["tasks"][task_id].get("detail") == detail[:500]
            ):
                return

            def update(current):
                current.plugin_state["tasks"][task_id]["detail"] = detail[:500]
                return [_publish(current)]

            await self.store.transition(session.interaction_id, update)

        if cancelling:
            if task["status"] != "cancel_requested":
                return
            if not task["state"].get("submission_started") and not task["state"].get(
                "remote_run_id"
            ):
                result = {
                    "status": "cancelled",
                    "answer": "Task cancelled before submission.",
                }
            elif task["name"] == "delegate_to_hermes" and not task["state"].get(
                "remote_run_id"
            ):
                result = {
                    "status": "cancel_requested",
                    "answer": "Waiting for the submitted Hermes run identity before requesting stop.",
                }
            else:
                result = await self.tools.cancel(task["name"], context=context(session))
        else:

            def running(current):
                item = current.plugin_state["tasks"][task_id]
                if (
                    item["status"] in _TERMINAL_TASKS
                    or item["status"] == "cancel_requested"
                ):
                    return []
                item["status"] = "running"
                return [_publish(current)]

            session, _ = await self.store.transition(session.interaction_id, running)
            observed = session.plugin_state["tasks"][task_id]
            if observed["status"] != "running" and not (
                observed["status"] == "cancel_requested"
                and observed["state"].get("remote_run_id")
            ):
                return
            result = await self.tools.execute(
                task["name"],
                task["arguments"],
                context=context(session),
                checkpoint=checkpoint,
                on_progress=progress,
            )

        def completed(current):
            item = current.plugin_state["tasks"][task_id]
            if item["status"] in _TERMINAL_TASKS:
                return []
            if (
                cancelling
                and result.get("status") == "unknown"
                and item["state"].get("remote_run_id")
            ):
                item["detail"] = result.get("answer", "")
                return [_publish(current)]
            item["result"] = result
            item["status"] = result.get("status", "failed")
            if cancelling and item["status"] == "cancel_requested":
                item["detail"] = result.get("answer", "")
                return [_publish(current)]
            current.plugin_state["history"].extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": item["call_id"],
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": json.dumps(item["arguments"]),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": json.dumps(result),
                    },
                ]
            )
            current.plugin_state["pending_results"] = True
            effects = [_publish(current)]
            if (
                current.status == "active"
                and current.phase == "listening"
                and not current.plugin_state["input_open"]
            ):
                current.phase = "thinking"
                effects.append(_response_effect(current, suffix="task_result"))
            return effects

        await self.store.transition(session.interaction_id, completed)

    async def expire_due(self):
        if time.monotonic() - self._dialogue_poll_at >= 1:
            for identity in await self.store.active_interaction_ids():
                active = await self.store.get(identity)
                if (
                    not active
                    or active.mode_id != MODE
                    or active.status != "active"
                    or active.phase != "listening"
                    or active.plugin_state.get("input_open")
                ):
                    continue
                try:
                    message = await self.dialogue.pending_output(active)
                    if message:

                        def deliver_message(current):
                            if (
                                current.status != "active"
                                or current.phase != "listening"
                                or current.plugin_state.get("input_open")
                            ):
                                return []
                            current.phase = "thinking"
                            return [
                                _response_effect(
                                    current,
                                    task_id="utterance:" + message["message_id"],
                                )
                            ]

                        await self.store.transition(identity, deliver_message)
                except Exception:
                    LOGGER.exception(
                        "Dialogue voice delivery unavailable for %s", identity
                    )
            self._dialogue_poll_at = time.monotonic()
        if time.monotonic() - self._dialogue_heartbeat_at >= 15:
            for identity in await self.store.active_interaction_ids():
                active = await self.store.get(identity)
                if active and active.mode_id == MODE and active.status == "active":
                    try:
                        await self.dialogue.heartbeat(active)
                    except Exception:
                        LOGGER.exception(
                            "Voice dialogue ownership lost for %s", identity
                        )
                        await self._end(active, "dialogue_unavailable")
            self._dialogue_heartbeat_at = time.monotonic()
        # Finished engagements release provider sockets independently of task work.
        for identity in tuple(self._engines):
            session = await self.store.get(identity)
            lock = self._engine_locks.get(identity)
            if (session is None or session.status == "ended") and not (
                lock and lock.locked()
            ):
                pending = self._native_inputs.pop(identity, None)
                if pending:
                    pending["task"].cancel()
                    await asyncio.gather(pending["task"], return_exceptions=True)
                engine = self._engines.pop(identity)
                close = getattr(engine, "close", None)
                if close is not None:
                    await close()
                self._engine_locks.pop(identity, None)
        for identity in await self.store.due_interaction_ids():
            session = await self.store.get(identity)
            if session is None or session.mode_id != MODE or session.status != "active":
                continue
            async with self._serial(session.user_id, session.client_id):
                session = await self.store.get(identity)
                if session is None or session.status != "active":
                    continue
                now = time.time()
                busy = session.phase in {"thinking", "speaking"} or any(
                    task["status"] not in _TERMINAL_TASKS
                    for task in session.plugin_state["tasks"].values()
                )
                if now >= session.hard_deadline or (
                    not busy and now >= session.idle_deadline
                ):
                    await self._end(
                        session,
                        (
                            "max_duration"
                            if now >= session.hard_deadline
                            else "idle_timeout"
                        ),
                    )

    async def run(self):

        self._worker = worker.VoiceEffectWorker(self)
        await self._worker.run()

    async def stop(self):
        if self._worker is not None:
            await self._worker.stop()

    async def close_engines(self):
        for state in self._native_inputs.values():
            state["task"].cancel()
        await asyncio.gather(
            *(state["task"] for state in self._native_inputs.values()),
            return_exceptions=True,
        )
        self._native_inputs.clear()
        for engine in tuple(self._engines.values()):
            close = getattr(engine, "close", None)
            if close is not None:
                await close()
        self._engines.clear()
