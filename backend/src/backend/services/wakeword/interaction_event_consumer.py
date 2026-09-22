"""Persist cross-process wake response lifecycle events into the Mongo ledger."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from redis import exceptions as redis_exceptions

from backend.audio_contract.v2 import audio_pb2
from backend.heartbeat import beat
from backend.services.response_coordinator import WAKE_INTERACTION_EVENTS_STREAM
from backend.services.voice_latency import VoiceTimingLedger
from backend.services.wakeword.interaction_ledger import (
    WakeInteractionFact,
    WakeInteractionLedger,
)

logger = logging.getLogger(__name__)

GROUP_NAME = "wake-interaction-ledger"
_PENDING_MIN_IDLE_MS = 30_000
_ORDINALS = {
    "command_resolved": 2,
    "dispatched": 3,
    "acted": 4,
    "response_queued": 5,
    "response_ready": 6,
    "response_offered": 7,
    "response_playing": 8,
    "response_done": 9,
    "followup_opened": 10,
}


class WakeInteractionEventConsumer:
    """Consume response facts atomically emitted with Redis response transitions."""

    def __init__(
        self,
        redis_client,
        ledger: WakeInteractionLedger,
        timing_ledger: VoiceTimingLedger | None = None,
    ):
        self.redis_client = redis_client
        self.ledger = ledger
        self.timing_ledger = timing_ledger
        self.consumer_name = "wake-interaction-ledger-worker"
        self.running = False

    async def stop(self) -> None:
        self.running = False

    async def _setup_group(self) -> None:
        try:
            await self.redis_client.xgroup_create(
                WAKE_INTERACTION_EVENTS_STREAM, GROUP_NAME, "0", mkstream=True
            )
        except redis_exceptions.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def run(self) -> None:
        await self._setup_group()
        self.running = True
        await self._recover_pending_once()
        last_recovery = time.monotonic()
        while self.running:
            if time.monotonic() - last_recovery >= 15:
                await self._recover_pending_once()
                last_recovery = time.monotonic()
            await beat(self.redis_client, "wake-interaction-ledger")
            messages = await self.redis_client.xreadgroup(
                GROUP_NAME,
                self.consumer_name,
                {WAKE_INTERACTION_EVENTS_STREAM: ">"},
                count=20,
                block=1000,
            )
            for _stream, rows in messages:
                for message_id, fields in rows:
                    try:
                        await self._handle(fields)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # keep failed fact pending
                        logger.error(
                            "Failed to persist wake interaction lifecycle event %s: %s",
                            message_id,
                            error,
                            exc_info=True,
                        )
                        continue
                    await self.redis_client.xack(
                        WAKE_INTERACTION_EVENTS_STREAM, GROUP_NAME, message_id
                    )

    async def _recover_pending_once(self) -> None:
        """Claim lifecycle facts abandoned by a prior worker instance."""
        claimed = await self.redis_client.xautoclaim(
            WAKE_INTERACTION_EVENTS_STREAM,
            GROUP_NAME,
            self.consumer_name,
            min_idle_time=_PENDING_MIN_IDLE_MS,
            start_id="0-0",
            count=100,
        )
        rows = claimed[1] if claimed else []
        for message_id, fields in rows:
            try:
                await self._handle(fields)
            except Exception as error:
                logger.error(
                    "Failed to recover wake lifecycle event %s: %s",
                    message_id,
                    error,
                    exc_info=True,
                )
                continue
            await self.redis_client.xack(
                WAKE_INTERACTION_EVENTS_STREAM, GROUP_NAME, message_id
            )

    async def _handle(self, fields: dict) -> None:
        timing = fields.get(b"timing") or fields.get("timing")
        if timing is not None:
            if self.timing_ledger is None:
                raise RuntimeError("voice timing ledger is not initialized")
            event = audio_pb2.InteractionTimingEvent()
            event.ParseFromString(timing)
            await self.timing_ledger.append(event)
            return
        raw = fields.get(b"event") or fields.get("event")
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            raise ValueError("wake interaction lifecycle event missing event")
        payload = json.loads(raw)
        stage = payload.get("stage")
        if stage not in _ORDINALS:
            raise ValueError(f"unsupported wake interaction lifecycle stage: {stage}")
        occurred_at = float(payload["occurred_at"])
        fact_payload = dict(payload.get("payload") or {})
        if stage.startswith("response_"):
            fact_payload.update(
                generation=int(payload["generation"]),
                response_state=str(payload["response_state"]),
            )
        await self.ledger.append(
            WakeInteractionFact(
                wake_trace_id=str(payload["wake_trace_id"]),
                stage=stage,
                ordinal=_ORDINALS[stage],
                occurred_at=datetime.fromtimestamp(occurred_at, tz=timezone.utc),
                user_id=str(payload["user_id"]),
                client_id=str(payload["client_id"]),
                audio_session_id=str(payload["audio_session_id"]),
                capture_epoch=int(payload["capture_epoch"]),
                wakeword=payload.get("wakeword") or None,
                voice_session_id=payload.get("voice_session_id") or None,
                turn_id=payload.get("turn_id") or None,
                turn_revision=int(payload.get("turn_revision", 0)),
                response_id=payload.get("response_id") or None,
                payload=fact_payload,
            )
        )
