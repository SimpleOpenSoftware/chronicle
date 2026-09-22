"""Atomic audio-interval ownership for every interactive routing source."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass

import redis.asyncio as redis
from redis.exceptions import WatchError

from .contracts import AudioInterval, InteractionSource

CLAIM_RETENTION_SECONDS = 24 * 60 * 60
MIN_DUPLICATE_OVERLAP_RATIO = 0.6


@dataclass(frozen=True)
class AudioEpisodeClaim:
    claim_id: str
    source: InteractionSource
    interval: AudioInterval
    claimed_at: float
    owner_id: str | None = None


@dataclass(frozen=True)
class AudioEpisodeClaimResult:
    accepted: bool
    claim: AudioEpisodeClaim


def _claims_key(
    user_id: str, client_id: str, audio_session_id: str, capture_epoch: int
) -> str:
    return f"interaction:episode-claims:{user_id}:{client_id}:{audio_session_id}:{capture_epoch}"


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _overlap_ratio(left: AudioInterval, right: AudioInterval) -> float:
    intersection = max(
        0.0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)
    )
    shortest = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return intersection / shortest if shortest > 0 else 0.0


class AudioEpisodeArbiter:
    """Claim one route by interval with an optimistic Redis transaction."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def claim(
        self,
        *,
        user_id: str,
        client_id: str,
        interval: AudioInterval,
        source: InteractionSource,
        now: float | None = None,
        owner_id: str | None = None,
    ) -> AudioEpisodeClaimResult:
        key = _claims_key(
            user_id,
            client_id,
            interval.audio_session_id,
            interval.capture_epoch,
        )
        claimed_at = now if now is not None else time.time()
        candidate = AudioEpisodeClaim(
            claim_id=str(uuid.uuid4()),
            source=source,
            interval=interval,
            claimed_at=claimed_at,
            owner_id=owner_id,
        )
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw_candidates = await pipe.zrangebyscore(
                        key, "-inf", interval.end_ms
                    )
                    for raw in raw_candidates:
                        value = json.loads(_decode(raw))
                        existing = AudioEpisodeClaim(
                            claim_id=value["claim_id"],
                            source=value["source"],
                            interval=AudioInterval(**value["interval"]),
                            claimed_at=float(value["claimed_at"]),
                            owner_id=value.get("owner_id"),
                        )
                        if (
                            existing.interval.end_ms >= interval.start_ms
                            and _overlap_ratio(existing.interval, interval)
                            >= MIN_DUPLICATE_OVERLAP_RATIO
                        ):
                            await pipe.unwatch()
                            return AudioEpisodeClaimResult(
                                owner_id is not None and existing.owner_id == owner_id,
                                existing,
                            )

                    member = json.dumps(
                        {
                            **asdict(candidate),
                            "interval": asdict(interval),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    pipe.multi()
                    pipe.zadd(key, {member: interval.start_ms})
                    pipe.expire(key, CLAIM_RETENTION_SECONDS)
                    await pipe.execute()
                    return AudioEpisodeClaimResult(True, candidate)
                except WatchError:
                    continue
