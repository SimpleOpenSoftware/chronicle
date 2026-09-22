"""Small async interface for cross-process single-flight claims."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.exceptions import LockError

from backend.redis_factory import create_async_redis


class LockUnavailable(RuntimeError):
    pass


@asynccontextmanager
async def distributed_lock(
    key: str, *, timeout: int = 120, blocking_timeout: int = 30, renew: bool = False
) -> AsyncIterator[None]:
    """Hold one Redis-backed lock without leaking a client across event loops."""

    client = create_async_redis(decode_responses=True)
    lock = client.lock(key, timeout=timeout, blocking_timeout=blocking_timeout)
    acquired = False
    renewal = None
    lease_error = None
    owner = asyncio.current_task()

    async def keep_lease():
        nonlocal lease_error
        try:
            while True:
                await asyncio.sleep(timeout / 3)
                await lock.extend(timeout, replace_ttl=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lease_error = exc
            owner.cancel()

    try:
        acquired = bool(await lock.acquire())
        if not acquired:
            raise LockUnavailable(f"could not acquire single-flight claim: {key}")
        if renew:
            renewal = asyncio.create_task(keep_lease(), name=f"renew:{key}")
        try:
            yield
            if lease_error is not None:
                raise LockUnavailable(
                    f"single-flight lease lost: {key}"
                ) from lease_error
        except asyncio.CancelledError:
            if lease_error is not None:
                raise LockUnavailable(
                    f"single-flight lease lost: {key}"
                ) from lease_error
            raise
    finally:
        if renewal is not None:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        if acquired:
            try:
                await lock.release()
            except LockError:
                # The protected operation already completed or raised. An expired
                # lease must not hide that result behind a cleanup-only exception.
                pass
        await client.aclose()
