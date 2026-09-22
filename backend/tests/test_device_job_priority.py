"""Exercise authenticated job creation and the real collector claim entry point."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from beanie import init_beanie
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from backend.models.device_input import CaptureSource, DeviceInputJob
from backend.routers.modules.device_input_routes import JobRequest, create_job, next_job


@pytest.mark.asyncio
async def test_explicit_source_request_precedes_background_backlog_and_claims_atomically(
    mongo_service,
):
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    database = "test_device_job_priority_" + uuid4().hex
    try:
        await init_beanie(
            database=client[database], document_models=[CaptureSource, DeviceInputJob]
        )
        owner = str(ObjectId())
        source = CaptureSource(
            user_id=owner,
            source_id="capture",
            name="Laptop",
            provider="screenpipe",
            platform="linux",
            token_hash=uuid4().hex,
        )
        await source.insert()
        background = DeviceInputJob(
            user_id=owner,
            source_id=source.source_id,
            kind="screen_context",
            purpose="background",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        await background.insert()
        unrelated = DeviceInputJob(
            user_id=str(ObjectId()),
            source_id="other-capture",
            kind="source_media",
            purpose="other owner",
            priority=100,
        )
        await unrelated.insert()
        requested = await create_job(
            JobRequest(
                source_id=source.source_id,
                kind="source_media",
                purpose="source inspection",
                payload={"frame_id": 42},
            ),
            user=SimpleNamespace(user_id=owner),
        )
        job = await DeviceInputJob.get(requested["job_id"])
        assert job.priority == 100
        first = await next_job(source)
        assert first["job"]["id"] == str(job.id)
        concurrent = await asyncio.gather(next_job(source), next_job(source))
        claimed = [result["job"]["id"] for result in concurrent if result["job"]]
        assert claimed == [str(background.id)]
        assert (await DeviceInputJob.get(unrelated.id)).status == "pending"
    finally:
        await client.drop_database(database)
        client.close()
