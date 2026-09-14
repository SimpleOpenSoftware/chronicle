import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from backend.workers import wakeword_dispatch_worker as worker


@pytest.mark.asyncio
async def test_worker_main_wires_detection_and_response_fact_consumers(monkeypatch):
    events = []

    class Redis:
        async def aclose(self):
            events.append("redis_closed")

    class Collection:
        async def create_index(self, keys, unique=False, **kwargs):
            events.append(("index", tuple(keys), unique))

    collection = Collection()

    class Database:
        def __getitem__(self, name):
            assert name in {"wake_interaction_facts", "voice_interaction_events"}
            return collection

    class Mongo:
        def __getitem__(self, name):
            return Database()

    class Dispatcher:
        def __init__(self, *, redis_client, plugin_router, interaction_ledger):
            events.append("dispatcher_created")

        async def run(self):
            events.append("dispatcher_run")

        async def stop(self):
            pass

    class LifecycleConsumer:
        def __init__(self, redis_client, ledger, timing_ledger):
            assert timing_ledger.collection is collection
            events.append("lifecycle_created")

        async def run(self):
            events.append("lifecycle_run")

        async def stop(self):
            pass

    async def recovery(_router):
        await asyncio.Future()

    monkeypatch.setattr(worker, "create_async_redis", lambda **kwargs: Redis())
    monkeypatch.setattr(worker, "initialize_redis_for_client_manager", lambda: None)
    monkeypatch.setattr(worker, "AsyncIOMotorClient", lambda _uri: Mongo())
    monkeypatch.setattr(worker, "init_beanie", AsyncMock())
    monkeypatch.setattr(worker, "init_plugin_router", lambda: Mock(plugins={}))
    monkeypatch.setattr(worker, "initialize_plugins", AsyncMock())
    monkeypatch.setattr(worker, "run_plugin_recovery", recovery)
    monkeypatch.setattr(worker, "WakeWordDispatcher", Dispatcher)
    monkeypatch.setattr(worker, "WakeInteractionEventConsumer", LifecycleConsumer)
    monkeypatch.setattr(worker, "start_loop_monitor", lambda _name: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)

    await worker.main()

    assert "dispatcher_run" in events
    assert "lifecycle_run" in events
    assert (
        "index",
        (("wake_trace_id", 1), ("stage", 1), ("ordinal", 1)),
        True,
    ) in events
    assert events[-1] == "redis_closed"
