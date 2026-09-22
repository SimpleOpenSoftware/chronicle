"""Redis persistence for interaction sessions."""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

import redis.asyncio as redis
from redis.exceptions import WatchError

from backend.audio_contract.v2 import audio_pb2
from backend.redis_keys import voice_processing_owner

from .contracts import InteractionSession

SESSION_RETENTION_SECONDS = 24 * 60 * 60
PROCESSED_RETENTION_SECONDS = 24 * 60 * 60
DEADLINES_KEY = "interaction:deadlines"
VOICE_EFFECT_STREAM = "interaction:voice:effects"


def _active_key(user_id: str, client_id: str) -> str:
    return f"interaction:active:{user_id}:{client_id}"


def _session_key(interaction_id: str) -> str:
    return f"interaction:session:{interaction_id}"


def _processed_key(input_id: str) -> str:
    return f"interaction:processed:{input_id}"


def interaction_lock_key(interaction_id: str) -> str:
    return f"interaction:lock:{interaction_id}"


class InteractionStore:
    """Owns active pointers, session JSON, and timeout deadlines."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    @staticmethod
    def _decode(raw):
        return raw.decode() if isinstance(raw, bytes) else raw

    async def get(self, interaction_id: str) -> Optional[InteractionSession]:
        raw = await self.redis.get(_session_key(interaction_id))
        if raw is None:
            return None
        raw = self._decode(raw)
        return InteractionSession.from_dict(json.loads(raw))

    async def get_active(
        self, user_id: str, client_id: str, *, now: Optional[float] = None
    ) -> Optional[InteractionSession]:
        key = _active_key(user_id, client_id)
        raw_id = await self.redis.get(key)
        if raw_id is None:
            return None
        interaction_id = self._decode(raw_id)
        session = await self.get(interaction_id)
        if session is None or session.status != "active":
            await self.delete_pointer_if_owned(user_id, client_id, interaction_id)
            return None
        # Do not finalize expiry in this low-level lookup. The processor owns the
        # plugin end callback and user-facing timeout reply. Returning an overdue
        # active session lets either the deadline sweep or the next queued turn
        # perform that complete transition instead of silently deleting it here.
        return session

    async def create(self, session: InteractionSession) -> bool:
        """Claim the user/device active slot and persist ``session``.

        Returns False when another producer won the activation race.
        """
        ttl = max(1, int(session.hard_deadline - time.time()))
        key = _active_key(session.user_id, session.client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    if await pipe.get(key) is not None:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.set(key, session.interaction_id, ex=ttl)
                    pipe.set(
                        _session_key(session.interaction_id),
                        json.dumps(session.to_dict(), separators=(",", ":")),
                        ex=SESSION_RETENTION_SECONDS,
                    )
                    pipe.zadd(
                        DEADLINES_KEY, {session.interaction_id: session.next_deadline}
                    )
                    if session.mode_id == "voice_conversation":
                        # Defer this dependency to break the import cycle through
                        # backend.services.interaction_modes -> backend.services.interaction_modes.store.
                        from .voice.runtime_journal import STREAM, journal_entry

                        pipe.xadd(
                            STREAM,
                            {"entry": journal_entry(session).SerializeToString()},
                        )
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def transition(
        self,
        interaction_id: str,
        update: Callable[[InteractionSession], list[audio_pb2.VoiceEffect]],
        *,
        input_id: str | None = None,
    ) -> tuple[InteractionSession | None, bool]:
        """CAS one short pure transition with input marker and typed effect outbox.

        ``update`` may be replayed on conflict; it MUST NOT perform external I/O.
        Ended sessions may receive task results, but cannot reacquire an active slot.
        """
        key = _session_key(interaction_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        await pipe.unwatch()
                        return None, False
                    session = InteractionSession.from_dict(
                        json.loads(self._decode(raw))
                    )
                    if input_id:
                        await pipe.watch(_processed_key(input_id))
                        if await pipe.exists(_processed_key(input_id)):
                            await pipe.unwatch()
                            return session, False
                    active_key = _active_key(session.user_id, session.client_id)
                    await pipe.watch(active_key)
                    active_id = self._decode(await pipe.get(active_key))
                    previous_effect = (
                        session.plugin_state.get("processing_effect_id")
                        if session.status == "active"
                        else None
                    )
                    effects = update(session)
                    session.revision += 1
                    pipe.multi()
                    pipe.set(
                        key,
                        json.dumps(session.to_dict(), separators=(",", ":")),
                        ex=SESSION_RETENTION_SECONDS,
                    )
                    if session.status == "active" and active_id == interaction_id:
                        pipe.expire(
                            active_key, max(1, int(session.hard_deadline - time.time()))
                        )
                        pipe.zadd(
                            DEADLINES_KEY, {interaction_id: session.next_deadline}
                        )
                    else:
                        pipe.zrem(DEADLINES_KEY, interaction_id)
                        if active_id == interaction_id:
                            pipe.delete(active_key)
                    if input_id:
                        pipe.set(
                            _processed_key(input_id),
                            "1",
                            ex=PROCESSED_RETENTION_SECONDS,
                        )
                    if session.mode_id == "voice_conversation":
                        admitted_effect = (
                            session.plugin_state.get("processing_effect_id")
                            if session.status == "active"
                            else None
                        )
                        # Admission is atomic with state/outbox. Per-phrase
                        # checkpoints never rewrite this tiny observation fence.
                        if admitted_effect != previous_effect:
                            owner_key = voice_processing_owner(interaction_id)
                            if admitted_effect:
                                pipe.set(
                                    owner_key,
                                    admitted_effect,
                                    ex=SESSION_RETENTION_SECONDS,
                                )
                            else:
                                pipe.delete(owner_key)
                        # Defer this dependency to break the import cycle through
                        # backend.services.interaction_modes -> backend.services.interaction_modes.store.
                        from .voice.runtime_journal import STREAM, journal_entry

                        pipe.xadd(
                            STREAM,
                            {"entry": journal_entry(session).SerializeToString()},
                        )
                    for effect in effects:
                        if not isinstance(effect, audio_pb2.VoiceEffect):
                            raise TypeError(
                                "voice outbox requires generated VoiceEffect"
                            )
                        pipe.xadd(
                            VOICE_EFFECT_STREAM, {"effect": effect.SerializeToString()}
                        )
                    await pipe.execute()
                    return session, True
                except WatchError:
                    continue

    async def delete_pointer_if_owned(
        self, user_id: str, client_id: str, interaction_id: str
    ) -> None:
        key = _active_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    if self._decode(await pipe.get(key)) != interaction_id:
                        await pipe.unwatch()
                        return
                    pipe.multi()
                    pipe.delete(key)
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def is_processed(self, input_id: str) -> bool:
        return bool(await self.redis.exists(_processed_key(input_id)))

    async def mark_processed(self, input_id: str) -> None:
        await self.redis.set(
            _processed_key(input_id), "1", ex=PROCESSED_RETENTION_SECONDS
        )

    async def save(
        self,
        session: InteractionSession,
        *,
        processed_input_id: Optional[str] = None,
    ) -> None:
        """Persist one transition and its input marker in one Redis transaction."""
        pipe = self.redis.pipeline(transaction=True)
        pipe.set(
            _session_key(session.interaction_id),
            json.dumps(session.to_dict(), separators=(",", ":")),
            ex=SESSION_RETENTION_SECONDS,
        )
        if session.status == "active":
            remaining = max(1, int(session.hard_deadline - time.time()))
            pipe.expire(_active_key(session.user_id, session.client_id), remaining)
            pipe.zadd(DEADLINES_KEY, {session.interaction_id: session.next_deadline})
        else:
            pipe.zrem(DEADLINES_KEY, session.interaction_id)
        if processed_input_id:
            pipe.set(
                _processed_key(processed_input_id),
                "1",
                ex=PROCESSED_RETENTION_SECONDS,
            )
        await pipe.execute()

    async def end(
        self,
        session: InteractionSession,
        *,
        reason: str,
        now: Optional[float] = None,
        processed_input_id: Optional[str] = None,
    ) -> InteractionSession:
        ended_at = now if now is not None else time.time()
        session.status = "ended"
        session.ended_at = ended_at
        session.end_reason = reason
        await self.save(session, processed_input_id=processed_input_id)
        await self.delete_pointer_if_owned(
            session.user_id, session.client_id, session.interaction_id
        )
        return session

    async def active_interaction_ids(self) -> list[str]:
        return [
            self._decode(value)
            for value in await self.redis.zrange(DEADLINES_KEY, 0, -1)
        ]

    async def due_interaction_ids(
        self, *, now: Optional[float] = None, limit: int = 100
    ) -> list[str]:
        deadline = now if now is not None else time.time()
        raw_ids = await self.redis.zrangebyscore(
            DEADLINES_KEY, min="-inf", max=deadline, start=0, num=limit
        )
        return [self._decode(value) for value in raw_ids]
