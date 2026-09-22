#!/usr/bin/env python3
"""Dedicated Redis-stream worker for long-lived interaction modes."""

import asyncio
import json
import logging
import signal
import sys
import time

from redis import exceptions as redis_exceptions

import backend.database as database
import backend.services.dialogue.worker as worker_module
import backend.services.interaction_modes.voice.runtime_journal as runtime_journal
from backend.client_manager import initialize_redis_for_client_manager
from backend.heartbeat import beat
from backend.observability.otel_setup import force_flush_otel, init_otel
from backend.redis_factory import REDIS_URL, create_async_redis
from backend.redis_keys import ClientId, SessionId
from backend.services.interaction_modes.committed_turns import (
    CommittedAudioTurn,
    CommittedTurnRouter,
)
from backend.services.interaction_modes.contracts import InteractionInput
from backend.services.interaction_modes.ingress import INPUT_STREAM
from backend.services.interaction_modes.processor import (
    InteractionBusyError,
    InteractionDispatch,
    InteractionProcessor,
)
from backend.services.interaction_modes.voice.runtime import VoiceConversationRuntime
from backend.services.interaction_modes.voice.tools import VoiceTools
from backend.services.observability.loop_monitor import start_loop_monitor
from backend.services.plugin_service import (
    init_plugin_router,
    initialize_plugins,
    run_plugin_recovery,
)
from backend.services.response_coordinator import WAKE_INTERACTION_EVENTS_STREAM
from backend.services.wakeword.activations import WakeActivation
from backend.services.wakeword.executor import (
    execute_voice_command,
    publish_sse,
    speak_on_device,
)

GROUP_NAME = "interaction-mode"
CONSUMER_NAME = "interaction-mode-worker"
PENDING_CLAIM_MIN_IDLE_MS = 130_000
PENDING_RECOVERY_INTERVAL_SECONDS = 15

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


class InteractionModeWorker:
    def __init__(
        self, redis_client, plugin_router, *, voice_runtime=None, journal_projector=None
    ):
        self.redis = redis_client
        self.plugin_router = plugin_router
        self.processor = InteractionProcessor(redis_client, plugin_router)
        self.journal_projector = journal_projector
        self.voice_runtime = voice_runtime or VoiceConversationRuntime(
            redis_client, tools=VoiceTools.from_config(plugin_router)
        )
        self.turn_router = CommittedTurnRouter(
            redis_client,
            plugin_router.interaction_registry,
            command_dispatcher=self._dispatch_committed_command,
            voice_runtime=self.voice_runtime,
        )
        self.running = False

    async def _dispatch_committed_command(
        self,
        turn: CommittedAudioTurn,
        text: str,
        user_id: str,
        client_id: str,
        generation: int,
        activation: WakeActivation,
    ) -> None:
        base_event = {
            "wake_trace_id": activation.wake_trace_id,
            "user_id": user_id,
            "client_id": client_id,
            "audio_session_id": turn.interval.audio_session_id,
            "capture_epoch": turn.interval.capture_epoch,
            "voice_session_id": turn.interval.voice_session_id,
            "turn_id": turn.interval.turn_id or str(turn.start_sequence),
            "turn_revision": turn.interval.turn_revision,
            "wakeword": activation.wakeword,
        }

        async def publish_stage(
            stage: str, payload: dict, *, occurred_at: float | None = None
        ) -> None:
            event = {
                **base_event,
                "stage": stage,
                "occurred_at": occurred_at or time.time(),
                "payload": payload,
            }
            await self.redis.xadd(
                WAKE_INTERACTION_EVENTS_STREAM,
                {"event": json.dumps(event, separators=(",", ":"), sort_keys=True)},
            )

        await publish_stage(
            "command_resolved",
            {"source": "committed", "character_count": len(text)},
        )
        await execute_voice_command(
            self.redis,
            self.plugin_router,
            user_id=user_id,
            session_id=SessionId.from_value(turn.interval.audio_session_id),
            client_id=ClientId.from_value(client_id),
            command=text,
            source="committed",
            asr_status="committed_exact",
            capture_secs=(turn.interval.end_ms - turn.interval.start_ms) / 1000,
            response_generation=generation,
            response_turn_id=turn.interval.turn_id or str(turn.start_sequence),
            response_turn_revision=turn.interval.turn_revision,
            wakeword=activation.wakeword,
            wake_trace_id=activation.wake_trace_id,
            interaction_stage_callback=publish_stage,
        )

    async def stop(self) -> None:
        self.running = False
        await self.turn_router.stop()
        await self.voice_runtime.stop()
        if self.journal_projector is not None:
            await self.journal_projector.stop()

    async def _setup_group(self) -> None:
        try:
            await self.redis.xgroup_create(INPUT_STREAM, GROUP_NAME, "0", mkstream=True)
        except redis_exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self) -> None:
        await self._setup_group()
        self.running = True
        turn_router_task = asyncio.create_task(self.turn_router.run())
        voice_task = asyncio.create_task(self.voice_runtime.run())

        dialogue_task = asyncio.create_task(worker_module.run_dialogue_worker())
        journal_task = (
            asyncio.create_task(self.journal_projector.run())
            if self.journal_projector is not None
            else None
        )
        last_pending_recovery = 0.0
        logger.info("InteractionModeWorker listening on %s", INPUT_STREAM)
        try:
            while self.running:
                if turn_router_task.done():
                    turn_router_task.result()
                    raise RuntimeError("committed-turn router exited unexpectedly")
                if voice_task.done():
                    voice_task.result()
                    raise RuntimeError("voice conversation runtime exited unexpectedly")
                if dialogue_task.done():
                    dialogue_task.result()
                    raise RuntimeError("shared dialogue worker exited unexpectedly")
                if journal_task is not None and journal_task.done():
                    journal_task.result()
                    raise RuntimeError("voice journal projector exited unexpectedly")
                await beat(self.redis, "interaction-mode")
                if (
                    time.monotonic() - last_pending_recovery
                    >= PENDING_RECOVERY_INTERVAL_SECONDS
                ):
                    try:
                        await self._recover_pending()
                    except Exception as exc:  # noqa: BLE001 - retry on the next sweep
                        logger.error(
                            "Interaction pending-input recovery failed: %s",
                            exc,
                            exc_info=True,
                        )
                    last_pending_recovery = time.monotonic()
                try:
                    for dispatch in await self.processor.expire_due():
                        await self._safe_deliver(dispatch)
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    logger.error(
                        "Interaction expiry sweep failed: %s", exc, exc_info=True
                    )

                try:
                    messages = await self.redis.xreadgroup(
                        GROUP_NAME,
                        CONSUMER_NAME,
                        {INPUT_STREAM: ">"},
                        count=10,
                        block=1000,
                    )
                except Exception as exc:  # noqa: BLE001 - tolerate Redis blips
                    logger.error(
                        "Interaction input read failed: %s", exc, exc_info=True
                    )
                    await asyncio.sleep(1)
                    continue

                for _stream, entries in messages or []:
                    for message_id, fields in entries:
                        await self._handle(message_id, fields)
        finally:
            dialogue_task.cancel()
            await asyncio.gather(dialogue_task, return_exceptions=True)
            await self.turn_router.stop()
            await self.voice_runtime.stop()
            if journal_task is not None:
                await self.journal_projector.stop()
                journal_task.cancel()
                await asyncio.gather(journal_task, return_exceptions=True)
            voice_task.cancel()
            turn_router_task.cancel()
            try:
                await turn_router_task
            except asyncio.CancelledError:
                pass
            await asyncio.gather(voice_task, return_exceptions=True)

    async def _recover_pending(self) -> int:
        """Claim inputs stranded by a dead worker after its safety lock expires."""
        cursor = "0-0"
        recovered = 0
        for _ in range(100):
            response = await self.redis.xautoclaim(
                INPUT_STREAM,
                GROUP_NAME,
                CONSUMER_NAME,
                PENDING_CLAIM_MIN_IDLE_MS,
                start_id=cursor,
                count=10,
            )
            cursor, entries = response[0], response[1]
            if not entries:
                break
            for message_id, fields in entries:
                recovered += 1
                await self._handle(message_id, fields)
            if cursor in {"0-0", b"0-0"}:
                break
        if recovered:
            logger.warning("Recovered %s pending interaction input(s)", recovered)
        return recovered

    async def _handle(self, message_id, fields: dict) -> None:
        raw = fields.get("input") or fields.get(b"input")
        if isinstance(raw, bytes):
            raw = raw.decode()
        item = None
        acknowledge = True
        try:
            if not raw:
                raise ValueError("interaction stream entry has no input field")
            item = InteractionInput.from_dict(json.loads(raw))
            dispatch = await self.processor.process(item)
        except InteractionBusyError as exc:
            # Leave this entry pending. Recovery reclaims it only after the
            # current owner's safety lock has expired or been released.
            acknowledge = False
            logger.info("Interaction input deferred: %s", exc)
        except Exception as exc:  # noqa: BLE001 - isolate one malformed/plugin turn
            logger.error("Interaction input processing failed: %s", exc, exc_info=True)
            if item is not None:
                dispatch = await self.processor.fail(item.interaction_id)
                if dispatch:
                    dispatch.reply = "The interaction hit an error and has been closed."
                    await self._safe_deliver(dispatch)
        else:
            if dispatch:
                await self._safe_deliver(dispatch)
        finally:
            if acknowledge:
                await self.redis.xack(INPUT_STREAM, GROUP_NAME, message_id)

    async def _safe_deliver(self, dispatch: InteractionDispatch) -> None:
        """Keep a TTS/SSE outage from corrupting an already-committed transition."""
        try:
            await self._deliver(dispatch)
        except Exception as exc:  # noqa: BLE001 - delivery is best effort
            logger.error(
                "Interaction reply delivery failed for %s: %s",
                dispatch.session.interaction_id,
                exc,
                exc_info=True,
            )

    async def _deliver(self, dispatch: InteractionDispatch) -> None:
        session = dispatch.session
        payload = {
            "interaction_id": session.interaction_id,
            "mode_id": session.mode_id,
            "phase": session.phase,
            "status": session.status,
            "end_reason": session.end_reason,
            "reply": dispatch.reply,
            **dispatch.event_data,
        }
        await publish_sse(
            self.redis,
            session.user_id,
            f"interaction.{dispatch.lifecycle}",
            payload,
        )
        if dispatch.reply and session.audio_session_id:
            await speak_on_device(
                self.redis,
                ClientId.from_value(session.client_id),
                SessionId.from_value(session.audio_session_id),
                dispatch.reply,
                generation=session.response_generation,
                turn_id=session.response_turn_id,
                turn_revision=session.response_turn_revision,
            )


async def main() -> None:
    logger.info("Starting interaction-mode worker")
    init_otel()
    # This worker consumes committed voice turns containing raw PCM. A text-decoding
    # Redis client tries to UTF-8 decode that binary field before the router can
    # inspect it, killing the whole worker on ordinary audio bytes.
    redis_client = create_async_redis(decode_responses=False)
    logger.info("Connected to Redis: %s", REDIS_URL)
    initialize_redis_for_client_manager()

    plugin_router = init_plugin_router()
    if plugin_router is None:
        logger.error("No plugin router available for interaction modes")
        await redis_client.aclose()
        sys.exit(1)
    await initialize_plugins(plugin_router)
    recovery_task = asyncio.create_task(run_plugin_recovery(plugin_router))

    worker = InteractionModeWorker(
        redis_client,
        plugin_router,
        journal_projector=runtime_journal.VoiceJournalProjector(
            redis_client, database.get_database()["voice_conversation_journal"]
        ),
    )

    def signal_handler(signum, frame):
        logger.info("Received signal %s, shutting down", signum)
        asyncio.create_task(worker.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        start_loop_monitor("interaction-mode")
        await worker.run()
    finally:
        recovery_task.cancel()
        force_flush_otel()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
