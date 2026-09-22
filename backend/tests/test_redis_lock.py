import asyncio
from types import SimpleNamespace

import pytest
from redis.exceptions import LockError

from backend.services import redis_lock


class _FakeLock:
    def __init__(self, *, acquired=True, release_error=None):
        self.acquired = acquired
        self.release_error = release_error
        self.released = False

    async def acquire(self):
        return self.acquired

    async def release(self):
        self.released = True
        if self.release_error is not None:
            raise self.release_error


class _FakeClient:
    def __init__(self, lock):
        self._lock = lock
        self.closed = False
        self.lock_call = None

    def lock(self, key, *, timeout, blocking_timeout):
        self.lock_call = SimpleNamespace(
            key=key, timeout=timeout, blocking_timeout=blocking_timeout
        )
        return self._lock

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_distributed_lock_acquires_releases_and_closes(monkeypatch):
    lock = _FakeLock()
    client = _FakeClient(lock)
    monkeypatch.setattr(redis_lock, "create_async_redis", lambda **_kwargs: client)

    async with redis_lock.distributed_lock(
        "single-flight", timeout=42, blocking_timeout=7
    ):
        assert lock.released is False

    assert lock.released is True
    assert client.closed is True
    assert client.lock_call == SimpleNamespace(
        key="single-flight", timeout=42, blocking_timeout=7
    )


@pytest.mark.asyncio
async def test_distributed_lock_closes_client_when_claim_is_unavailable(monkeypatch):
    lock = _FakeLock(acquired=False)
    client = _FakeClient(lock)
    monkeypatch.setattr(redis_lock, "create_async_redis", lambda **_kwargs: client)

    with pytest.raises(redis_lock.LockUnavailable, match="single-flight"):
        async with redis_lock.distributed_lock("single-flight"):
            raise AssertionError("unreachable")

    assert lock.released is False
    assert client.closed is True


@pytest.mark.asyncio
async def test_expired_lock_does_not_mask_protected_result(monkeypatch):
    lock = _FakeLock(release_error=LockError("expired"))
    client = _FakeClient(lock)
    monkeypatch.setattr(redis_lock, "create_async_redis", lambda **_kwargs: client)

    async with redis_lock.distributed_lock("single-flight"):
        pass

    assert lock.released is True
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_lease", [False, True])
async def test_renewed_lease_preserves_ownership_or_stops_the_operation(
    monkeypatch, lose_lease
):
    lock = _FakeLock()
    client = _FakeClient(lock)
    monkeypatch.setattr(redis_lock, "create_async_redis", lambda **_: client)
    renewed = asyncio.Event()
    calls = []

    async def extend(seconds, *, replace_ttl):
        calls.append((seconds, replace_ttl))
        if lose_lease:
            raise LockError("Ownership lost")
        renewed.set()
        return True

    lock.extend = extend

    async def operation():
        async with redis_lock.distributed_lock("generation", timeout=0.03, renew=True):
            if lose_lease:
                await asyncio.Event().wait()
                raise AssertionError("Operation continued after losing ownership")
            await renewed.wait()

    if lose_lease:
        with pytest.raises(redis_lock.LockUnavailable, match="lease lost"):
            await asyncio.wait_for(operation(), 1)
    else:
        await asyncio.wait_for(operation(), 1)
    assert calls == [(0.03, True)]
    assert client.closed and lock.released
