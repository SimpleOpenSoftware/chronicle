"""Short-lived acoustic wake claims consumed by complete voice turns."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from redis.exceptions import WatchError

from backend.services.interaction_modes.contracts import AudioInterval

RETENTION_SECONDS = 5 * 60


@dataclass(frozen=True)
class WakeActivation:
    """An acoustic wake interval waiting for its canonical committed turn."""

    wake_trace_id: str
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    wakeword: str
    armed_at: float
    end_of_turn_at: float
    command_start_ms: float
    command_end_ms: float

    def __post_init__(self) -> None:
        UUID(self.wake_trace_id)
        if not self.user_id or not self.client_id or not self.audio_session_id:
            raise ValueError("wake activation identity is required")
        if self.capture_epoch < 0:
            raise ValueError("wake activation capture_epoch must be non-negative")
        if self.end_of_turn_at < self.armed_at:
            raise ValueError("wake activation end_of_turn_at precedes armed_at")
        if self.command_start_ms < 0 or self.command_end_ms <= self.command_start_ms:
            raise ValueError("wake activation command interval is invalid")

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, value: str | bytes) -> "WakeActivation":
        if isinstance(value, bytes):
            value = value.decode()
        return cls(**json.loads(value))


class WakeActivationStore:
    """Match one acoustic wake claim to one complete turn by capture coordinates."""

    def __init__(self, redis_client):
        self.redis = redis_client

    @staticmethod
    def _key(audio_session_id: str, capture_epoch: int) -> str:
        return f"voice:wake-activations:{audio_session_id}:{capture_epoch}"

    async def register(self, activation: WakeActivation) -> None:
        key = self._key(activation.audio_session_id, activation.capture_epoch)
        await self.redis.zadd(key, {activation.encode(): activation.command_start_ms})
        await self.redis.expire(key, RETENTION_SECONDS)

    async def claim(
        self, interval: AudioInterval, *, owner_id: str | None = None
    ) -> WakeActivation | None:
        key = self._key(interval.audio_session_id, interval.capture_epoch)
        if owner_id is not None:
            return await self._claim_owned(interval, key, owner_id)
        candidates = await self.redis.zrangebyscore(key, "-inf", interval.end_ms)
        for encoded in candidates:
            activation = WakeActivation.decode(encoded)
            if activation.command_end_ms < interval.start_ms:
                await self.redis.zrem(key, encoded)
                continue
            if (
                activation.command_start_ms >= interval.start_ms
                and activation.command_end_ms <= interval.end_ms
                and await self.redis.zrem(key, encoded) == 1
            ):
                return activation
        return None

    async def _claim_owned(self, interval, key, owner_id):
        """Reserve and journal an acoustic activation atomically for retried work."""
        owner_key = f"{key}:owner:{owner_id}"
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, owner_key)
                    cached = await pipe.get(owner_key)
                    if cached is not None:
                        await pipe.unwatch()
                        return WakeActivation.decode(cached)
                    candidates = await pipe.zrangebyscore(key, "-inf", interval.end_ms)
                    for encoded in candidates:
                        activation = WakeActivation.decode(encoded)
                        if (
                            activation.command_start_ms >= interval.start_ms
                            and activation.command_end_ms <= interval.end_ms
                        ):
                            pipe.multi()
                            pipe.zrem(key, encoded)
                            pipe.set(owner_key, encoded, ex=RETENTION_SECONDS)
                            await pipe.execute()
                            return activation
                    await pipe.unwatch()
                    return None
                except WatchError:
                    continue
