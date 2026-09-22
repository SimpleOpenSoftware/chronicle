"""Bounded voice effects and an independent low-latency speech-onset consumer."""

import asyncio
import json
import logging
import time
import uuid

from redis.exceptions import ResponseError, WatchError

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.services.response_coordinator import StaleResponse

from ..store import VOICE_EFFECT_STREAM

LOGGER = logging.getLogger(__name__)
GROUP = "voice-effects"
LEASE_SECONDS = 90
ONSET_STREAM = "voice:turns:events"


async def _lease_change(redis, key, token, *, renew):
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                value = await pipe.get(key)
                if isinstance(value, bytes):
                    value = value.decode()
                if value != token:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                if renew:
                    pipe.expire(key, LEASE_SECONDS)
                else:
                    pipe.delete(key)
                await pipe.execute()
                return True
            except WatchError:
                continue


async def release_lease(redis, key, token):
    return await _lease_change(redis, key, token, renew=False)


class VoiceEffectWorker:
    def __init__(self, runtime, *, group=GROUP, kinds=None, limit=8):
        self.runtime = runtime
        self.group = group
        self.kinds = kinds
        self.limit = limit
        self.lanes = []
        self.redis = runtime.redis
        self.consumer = "voice-" + str(uuid.uuid4())
        self.running = False
        self.tasks = set()

    async def setup(self):
        try:
            await self.redis.xgroup_create(
                VOICE_EFFECT_STREAM, self.group, "0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def handle(self, message_id, fields):
        raw = fields.get(b"effect") or fields.get("effect")
        if not isinstance(raw, bytes):
            LOGGER.error("Invalid generated voice effect at %s", message_id)
            await self.redis.xack(VOICE_EFFECT_STREAM, self.group, message_id)
            return
        effect = pb.VoiceEffect()
        try:
            effect.ParseFromString(raw)
        except Exception:
            LOGGER.exception("Invalid voice effect protobuf at %s", message_id)
            await self.redis.xack(VOICE_EFFECT_STREAM, self.group, message_id)
            return
        done_key = "interaction:voice:effect-done:" + effect.effect_id
        if await self.redis.exists(done_key):
            await self.redis.xack(VOICE_EFFECT_STREAM, self.group, message_id)
            return
        key = "interaction:voice:effect-lease:" + effect.effect_id
        token = str(uuid.uuid4())
        if not await self.redis.set(key, token, nx=True, ex=LEASE_SECONDS):
            return
        task = asyncio.create_task(self.runtime.execute_effect(effect))

        async def renew():
            while True:
                await asyncio.sleep(15)
                if not await _lease_change(self.redis, key, token, renew=True):
                    raise RuntimeError("voice effect ownership expired")

        renewal = asyncio.create_task(renew())
        try:
            await asyncio.wait((task, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal.done():
                renewal.result()
                raise RuntimeError("voice effect lease monitor exited")
            try:
                task.result()
            except StaleResponse:
                pass  # Superseded audio is terminal, never replayed.
            while True:
                async with self.redis.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        owner = await pipe.get(key)
                        if owner not in {token, token.encode()}:
                            raise RuntimeError(
                                "voice effect owner changed before completion"
                            )
                        pipe.multi()
                        pipe.set(done_key, "1", ex=86400)
                        pipe.xack(VOICE_EFFECT_STREAM, self.group, message_id)
                        pipe.xdel(VOICE_EFFECT_STREAM, message_id)
                        await pipe.execute()
                        break
                    except WatchError:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "Voice effect %s failed; retained for recovery", effect.effect_id
            )
        finally:
            task.cancel()
            renewal.cancel()
            await asyncio.gather(task, renewal, return_exceptions=True)
            await release_lease(self.redis, key, token)

    async def _schedule(self, entries):
        for message_id, fields in entries:
            if self.kinds is not None:
                try:
                    raw = fields.get(b"effect") or fields.get("effect")
                    effect = pb.VoiceEffect.FromString(raw)
                except Exception:
                    await self.redis.xack(VOICE_EFFECT_STREAM, self.group, message_id)
                    continue
                if effect.kind not in self.kinds:
                    await self.redis.xack(VOICE_EFFECT_STREAM, self.group, message_id)
                    continue
            while len(self.tasks) >= self.limit:
                await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(self.handle(message_id, fields))
            self.tasks.add(task)

            def settled(done):
                self.tasks.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    LOGGER.error("Voice worker task failed", exc_info=done.exception())

            task.add_done_callback(settled)

    async def recover_pending(self):
        cursor = "0-0"
        for _ in range(100):
            result = await self.redis.xautoclaim(
                VOICE_EFFECT_STREAM,
                self.group,
                self.consumer,
                LEASE_SECONDS * 1000,
                start_id=cursor,
                count=8,
            )
            cursor, entries = result[:2]
            await self._schedule(entries)
            if cursor in {"0-0", b"0-0"}:
                break

    async def _onsets(self):
        recent = await self.redis.xrevrange(ONSET_STREAM, count=1)
        cursor = recent[0][0] if recent else "0-0"
        while self.running:
            for _, entries in await self.redis.xread(
                {ONSET_STREAM: cursor}, count=20, block=500
            ):
                for identity, fields in entries:
                    cursor = identity
                    try:
                        raw = fields.get(b"event") or fields.get("event")
                        await self.runtime.speech_onset(
                            json.loads(raw), event_id=str(identity)
                        )
                    except (ValueError, TypeError):
                        LOGGER.exception("Invalid voice onset event")

    async def _consume(self):
        await self.setup()
        self.running = True
        recovery_at = 0
        try:
            while self.running:
                if time.monotonic() >= recovery_at:
                    await self.recover_pending()
                    recovery_at = time.monotonic() + 15
                streams = await self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {VOICE_EFFECT_STREAM: ">"},
                    count=8,
                    block=500,
                )
                for _, entries in streams:
                    await self._schedule(entries)
        finally:
            for task in tuple(self.tasks):
                task.cancel()
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def _expire(self):
        while self.running:
            await self.runtime.expire_due()
            await asyncio.sleep(0.5)

    async def run(self):
        # Separate durable delivery groups reserve capacity for cancellation and
        # speech even when every remote-task slot is occupied by slow Hermes runs.
        self.running = True
        specs = [
            ("responses", {pb.VOICE_EFFECT_KIND_RESPONSE}, 4),
            ("tasks", {pb.VOICE_EFFECT_KIND_TASK}, 4),
            (
                "control",
                {pb.VOICE_EFFECT_KIND_PUBLISH_STATE, pb.VOICE_EFFECT_KIND_CANCEL_TASK},
                4,
            ),
        ]
        self.lanes = [
            VoiceEffectWorker(
                self.runtime, group=GROUP + ":" + name, kinds=kinds, limit=limit
            )
            for name, kinds, limit in specs
        ]
        children = [asyncio.create_task(lane._consume()) for lane in self.lanes]
        children.extend(
            (asyncio.create_task(self._onsets()), asyncio.create_task(self._expire()))
        )
        try:
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if self.running:
                raise RuntimeError("voice worker consumer exited unexpectedly")
        finally:
            for lane in self.lanes:
                await lane.stop()
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            await self.runtime.close_engines()
            await self.runtime.tools.aclose()

    async def stop(self):
        self.running = False
        for lane in self.lanes:
            await lane.stop()
