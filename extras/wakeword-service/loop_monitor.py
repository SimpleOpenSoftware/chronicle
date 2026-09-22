"""In-process event-loop scheduling telemetry for the wake-word service.

The main backend has its own richer monitor.  This standalone service cannot import
the backend package, but it publishes the same ``system:loopmon:*`` snapshot shape so
the admin event-loop endpoint includes it alongside the backend and stream workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import Counter, deque
from dataclasses import dataclass

import redis.asyncio as redis

logger = logging.getLogger(__name__)

SNAPSHOT_KEY_PREFIX = "system:loopmon:"
DEFAULT_TICK_SECONDS = 0.25
DEFAULT_STALL_SECONDS = 1.0
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 15.0
SNAPSHOT_TTL_SECONDS = 60
WINDOW_SAMPLES = 1200
RECENT_STALLS = 10
STACK_SAMPLE_SECONDS = 0.05
MAX_STACK_SAMPLES = 40
STACK_DEPTH = 18


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1))))
    return round(ordered[index] * 1000, 1)


@dataclass(frozen=True)
class Stall:
    started_at: float
    duration: float
    samples: int
    stack: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "duration_ms": round(self.duration * 1000, 1),
            "stack_samples": self.samples,
            "stack": list(self.stack),
        }


class LoopMonitor:
    """Measure loop scheduling delay and sample the blocking callback's stack."""

    def __init__(
        self,
        process: str,
        redis_client=None,
        *,
        redis_url: str | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        snapshot_interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
    ):
        self.process = process
        self.redis_client = redis_client
        self.redis_url = redis_url
        self.tick_seconds = tick_seconds
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.stall_seconds = stall_seconds
        self._owns_redis = redis_client is None
        self._lags: deque[float] = deque(maxlen=WINDOW_SAMPLES)
        self._recent: deque[Stall] = deque(maxlen=RECENT_STALLS)
        self._pending: queue.SimpleQueue[Stall] = queue.SimpleQueue()
        self._stall_count = 0
        self._started_at = time.time()
        self._last_tick = time.monotonic()
        self._loop_thread_id: int | None = None
        self._stop = threading.Event()

    def _watch(self) -> None:
        poll = max(self.tick_seconds, self.stall_seconds / 4)
        while not self._stop.wait(poll):
            tick = self._last_tick
            if time.monotonic() - tick < self.stall_seconds:
                continue
            samples: list[tuple[str, ...]] = []
            while not self._stop.is_set() and self._last_tick == tick:
                frame = sys._current_frames().get(self._loop_thread_id)
                if frame is not None and len(samples) < MAX_STACK_SAMPLES:
                    summary = traceback.extract_stack(frame)[-STACK_DEPTH:]
                    samples.append(
                        tuple(
                            f"{item.filename}:{item.lineno} in {item.name}"
                            for item in summary
                        )
                    )
                self._stop.wait(STACK_SAMPLE_SECONDS)
            duration = time.monotonic() - tick
            if duration < self.stall_seconds:
                continue
            stack = Counter(samples).most_common(1)[0][0] if samples else ()
            self._pending.put(
                Stall(
                    started_at=time.time() - duration,
                    duration=duration,
                    samples=len(samples),
                    stack=stack,
                )
            )

    def stats(self) -> dict:
        values = list(self._lags)
        return {
            "process": self.process,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "samples": len(values),
            "window_seconds": round(len(values) * self.tick_seconds, 1),
            "stacks_enabled": True,
            "stall_threshold_ms": round(self.stall_seconds * 1000),
            "lag_p50_ms": _percentile(values, 0.50),
            "lag_p95_ms": _percentile(values, 0.95),
            "lag_p99_ms": _percentile(values, 0.99),
            "lag_max_ms": _percentile(values, 1.0),
            "stalls": self._stall_count,
            "recent_stalls": [stall.as_dict() for stall in reversed(self._recent)],
        }

    async def _publish(self) -> None:
        if self.redis_client is None:
            return
        try:
            await self.redis_client.set(
                f"{SNAPSHOT_KEY_PREFIX}{self.process}",
                json.dumps(self.stats()),
                ex=SNAPSHOT_TTL_SECONDS,
            )
        except Exception as error:  # diagnostics must never break wake detection
            logger.warning("Could not publish event-loop telemetry: %s", error)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        if self.redis_client is None and self.redis_url:
            self.redis_client = redis.from_url(self.redis_url)
        watchdog = threading.Thread(
            target=self._watch,
            name=f"loop-monitor-{self.process}",
            daemon=True,
        )
        watchdog.start()
        logger.info(
            "Event-loop monitor active for %s (stall>%ss)",
            self.process,
            self.stall_seconds,
        )
        last_snapshot = 0.0
        try:
            while True:
                expected = loop.time() + self.tick_seconds
                await asyncio.sleep(self.tick_seconds)
                self._last_tick = time.monotonic()
                lag = max(0.0, loop.time() - expected)
                self._lags.append(lag)
                while True:
                    try:
                        stall = self._pending.get_nowait()
                    except queue.Empty:
                        break
                    self._stall_count += 1
                    self._recent.append(stall)
                    where = stall.stack[-1] if stall.stack else "unattributed"
                    logger.warning(
                        "Event loop stalled %.2fs in %s (%s samples, innermost: %s)",
                        stall.duration,
                        self.process,
                        stall.samples,
                        where,
                    )
                now = time.monotonic()
                if now - last_snapshot >= self.snapshot_interval_seconds:
                    last_snapshot = now
                    await self._publish()
        except asyncio.CancelledError:
            raise
        finally:
            self._stop.set()
            watchdog.join(timeout=max(0.1, self.tick_seconds * 2))
            if self._owns_redis and self.redis_client is not None:
                await self.redis_client.aclose()
