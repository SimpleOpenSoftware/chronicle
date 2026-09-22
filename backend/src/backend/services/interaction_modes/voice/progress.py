"""Bounded, ephemeral observations of provider work, independent of playback."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

from redis.exceptions import WatchError

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.redis_keys import (
    ClientId,
    device_downlink_channel,
    response_generation,
    voice_processing_owner,
)

LOGGER = logging.getLogger(__name__)
COALESCE_SECONDS = 0.1
PUBLISH_TIMEOUT_SECONDS = 0.3
_STAGES = frozenset(
    {"transcribing", "generating_text", "synthesizing_speech", "generating_response"}
)


class VoiceProcessingPublisher:
    """One response effect owns one snapshot and one cancellable publisher task.

    The synchronous observer never performs I/O. A Redis generation fence and
    PUBLISH share one transaction, so superseded work cannot publish activity.
    Nothing is added to the interaction journal or retained after this scope.
    """

    def __init__(
        self, redis, *, session, generation, binding, effect_id, state_revision
    ):
        self.redis = redis
        self.generation = generation
        self.generation_key = response_generation(session.user_id, session.client_id)
        self.owner_key = voice_processing_owner(session.interaction_id)
        self.channel = str(
            device_downlink_channel(ClientId.from_value(session.client_id))
        )
        self.snapshot = pb.VoiceProcessingUpdate(
            binding=binding,
            interaction_id=session.interaction_id,
            generation=generation,
            effect_id=effect_id,
            state_revision=state_revision,
        )
        self.dirty = asyncio.Event()
        self.task = None
        self.closed = False
        self.sequence = 0
        self.reported_unavailable = False

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run(), name="voice-processing-publisher")
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for stage in _STAGES:
            setattr(self.snapshot, stage, False)
        self.snapshot.finished = True
        # Cleanup is bounded even when the diagnostic transport is unavailable.
        await self._try_publish()

    def observe(self, stage: str, active: bool) -> None:
        if stage not in _STAGES:
            raise ValueError(f"unknown voice processing stage: {stage}")
        if not self.closed and getattr(self.snapshot, stage) != active:
            setattr(self.snapshot, stage, active)
            self.dirty.set()

    @contextmanager
    def activity(self, stage):
        self.observe(stage, True)
        try:
            yield
        finally:
            self.observe(stage, False)

    def set_response(self, response_id):
        if not self.closed:
            self.snapshot.response_id.value = response_id
            self.dirty.set()

    async def _run(self):
        while True:
            await self.dirty.wait()
            self.dirty.clear()
            if not await self._try_publish():
                return
            await asyncio.sleep(COALESCE_SECONDS)

    async def _try_publish(self):
        try:
            async with asyncio.timeout(PUBLISH_TIMEOUT_SECONDS):
                return await self._publish()
        except Exception:
            # UI observations cannot fail or stall response production.
            if not self.reported_unavailable:
                LOGGER.warning("Voice processing update unavailable", exc_info=True)
                self.reported_unavailable = True
            return True

    async def _publish(self):
        self.sequence += 1
        update = pb.VoiceProcessingUpdate()
        update.CopyFrom(self.snapshot)
        update.sequence = self.sequence
        payload = pb.DeviceDownlinkEvent(
            voice_processing_update=update
        ).SerializeToString()
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self.generation_key, self.owner_key)
                generation, owner = await pipe.mget(self.generation_key, self.owner_key)
                owner = owner.decode() if isinstance(owner, bytes) else owner
                if int(generation or 0) != self.generation or owner != update.effect_id:
                    return False
                pipe.multi()
                pipe.publish(self.channel, payload)
                await pipe.execute()
                return True
            except WatchError:
                # A generation change invalidates this publisher permanently.
                return False
