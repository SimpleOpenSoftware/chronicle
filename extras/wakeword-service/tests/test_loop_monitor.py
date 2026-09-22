import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_monitor import LoopMonitor


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, **kwargs):
        self.values[key] = (json.loads(value), kwargs)


@pytest.mark.asyncio
async def test_loop_monitor_measures_and_publishes_scheduling_delay():
    redis = FakeRedis()
    monitor = LoopMonitor(
        "wakeword-service",
        redis,
        tick_seconds=0.01,
        snapshot_interval_seconds=0.01,
        stall_seconds=0.02,
    )
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.sleep(0.015)
        # This is the production failure class: synchronous work monopolizes the
        # loop while the service process and its consumer tasks remain alive.
        import time

        time.sleep(0.05)
        # The watchdog samples on a separate thread; wait for its report to be
        # drained and published rather than assuming it beats a fixed sleep.
        async with asyncio.timeout(1):
            while not redis.values.get("system:loopmon:wakeword-service", ({},))[0].get(
                "stalls"
            ):
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    stats = monitor.stats()
    assert stats["process"] == "wakeword-service"
    assert stats["lag_max_ms"] >= 35
    assert stats["stalls"] >= 1
    assert stats["recent_stalls"]
    published, options = redis.values["system:loopmon:wakeword-service"]
    assert published["lag_max_ms"] >= 35
    assert options["ex"] > 0
