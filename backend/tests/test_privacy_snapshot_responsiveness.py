"""Privacy policy construction must leave the event loop available for captures."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from test_privacy_enrollment import START, allow, evidence  # noqa: F401

from backend.services import privacy


async def test_real_snapshot_admission_does_not_block_capture_heartbeat(
    evidence, monkeypatch
):
    await allow(evidence)
    original = privacy.PrivacySnapshot
    building = threading.Event()
    heartbeat = threading.Event()

    def slow_snapshot(*args, **kwargs):
        building.set()
        assert heartbeat.wait(
            timeout=1
        ), "Policy construction blocked the event loop heartbeat"
        return original(*args, **kwargs)

    monkeypatch.setattr(privacy, "PrivacySnapshot", slow_snapshot)

    async def tick():
        while not building.is_set():
            await asyncio.sleep(0)
        heartbeat.set()

    tick_task = asyncio.create_task(tick())
    try:
        snapshot = await privacy.require_record(evidence.row)
        assert snapshot.permits_record(evidence.row)
    finally:
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 1, 0, 0, 0, 123456),
        datetime(2026, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc),
        datetime(
            2026,
            1,
            1,
            5,
            30,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        ),
        "2026-01-01T00:00:00.123456Z",
    ],
)
def test_timestamp_normalization_keeps_bson_precision_and_absolute_time(value):
    assert privacy.utc(value) == datetime(
        2026, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc
    )
