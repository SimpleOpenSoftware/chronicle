"""
Job models and base classes for RQ queue system.

This module provides:
- JobPriority enum for job priority levels
- BaseRQJob abstract class for common job setup and teardown
- async_job decorator for simplified job creation
"""

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, Optional

import redis.asyncio as redis_async

from backend.observability.otel_setup import force_flush_otel
from backend.prompt_defaults import register_all_defaults
from backend.prompt_registry import get_prompt_registry
from backend.redis_factory import create_async_redis
from backend.services.timeline.review_storage import assert_memory_review_storage_ready

logger = logging.getLogger(__name__)

# Global flag to track if Beanie is initialized in this process
_beanie_initialized = False
_beanie_init_lock = asyncio.Lock()


async def _ensure_beanie_initialized():
    """Ensure Beanie is initialized in the current process (for RQ workers)."""
    global _beanie_initialized
    async with _beanie_init_lock:
        if _beanie_initialized:
            return
        try:
            # Lazy import: Beanie + Mongo drivers + document models are only pulled in
            # when an RQ worker process first needs them, so importing this module
            # (e.g. for BaseRQJob/async_job) doesn't drag in the DB stack.
            from beanie import init_beanie
            from motor.motor_asyncio import AsyncIOMotorClient
            from pymongo.errors import ConfigurationError

            from backend.models.annotation import Annotation
            from backend.models.audio_capture import (
                AudioCaptureSession,
                ConversationTranscriptRevision,
                DiarizationArtifact,
                TranscriptArtifact,
            )
            from backend.models.audio_chunk import AudioChunkDocument
            from backend.models.conversation import Conversation
            from backend.models.device_input import (
                CaptureSource,
                DeviceInputItem,
                DeviceInputJob,
                PairingCode,
            )
            from backend.models.manual_memory import ManualMemory
            from backend.models.memory_audit import MemoryAuditEntry
            from backend.models.memory_space import (
                DeferredSpaceEvent,
                MemorySpace,
                SpaceMergeProposal,
            )
            from backend.models.notification import (
                NotificationDelivery,
                NotificationIntent,
                PushDevice,
            )
            from backend.models.session_memory import (
                MemorySourceDecision,
                SessionPreparation,
                UndatedSession,
            )
            from backend.models.timeline import (
                AudioEvidenceSpan,
                DirtyEvidenceRange,
                EpisodeDispatchLatch,
                MemoryReviewProposal,
                TimelineAnalysisRun,
                TimelineDay,
                TimelineEpisode,
                TimelinePublicationJournal,
                TimelineReconciliationRequest,
            )
            from backend.models.user import User
            from backend.models.waveform import WaveformData

            # Get MongoDB URI from environment
            mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")

            # Create MongoDB client
            mongodb_database = os.getenv("MONGODB_DATABASE", "chronicle")
            # RQ ACKs Redis source messages after Beanie writes return. Require the
            # return to mean journaled majority commit, not merely accepted into the
            # mongod process cache.
            client = AsyncIOMotorClient(mongodb_uri, w="majority", journal=True)
            try:
                database = client.get_default_database(mongodb_database)
            except ConfigurationError:
                database = client[mongodb_database]
                raise
            await assert_memory_review_storage_ready(database)
            # Initialize Beanie
            await init_beanie(
                database=database,
                document_models=[
                    User,
                    Conversation,
                    AudioCaptureSession,
                    AudioChunkDocument,
                    TranscriptArtifact,
                    DiarizationArtifact,
                    ConversationTranscriptRevision,
                    WaveformData,
                    Annotation,
                    MemoryAuditEntry,
                    ManualMemory,
                    MemorySpace,
                    SpaceMergeProposal,
                    DeferredSpaceEvent,
                    CaptureSource,
                    PairingCode,
                    DeviceInputItem,
                    DeviceInputJob,
                    AudioEvidenceSpan,
                    TimelineAnalysisRun,
                    TimelineEpisode,
                    TimelineDay,
                    TimelinePublicationJournal,
                    TimelineReconciliationRequest,
                    DirtyEvidenceRange,
                    EpisodeDispatchLatch,
                    MemoryReviewProposal,
                    MemorySourceDecision,
                    SessionPreparation,
                    UndatedSession,
                    PushDevice,
                    NotificationIntent,
                    NotificationDelivery,
                ],
            )

            _beanie_initialized = True
            logger.info("✅ Beanie initialized in RQ worker process")

            # Register prompt defaults (needed for title/summary generation etc.)
            prompt_registry = get_prompt_registry()
            register_all_defaults(prompt_registry)
            logger.info("✅ Prompt registry initialized in RQ worker process")

        except Exception as e:
            _beanie_initialized = False
            logger.error(f"❌ Failed to initialize Beanie in RQ worker: {e}")
            raise


class JobPriority(str, Enum):
    """Priority levels for RQ job processing.

    Used to map priority to RQ job timeout values:
    - URGENT: 10 minutes timeout
    - HIGH: 8 minutes timeout
    - NORMAL: 5 minutes timeout (default)
    - LOW: 3 minutes timeout
    """

    URGENT = "urgent"  # 1 - Process immediately
    HIGH = "high"  # 2 - Process before normal
    NORMAL = "normal"  # 3 - Default priority
    LOW = "low"  # 4 - Process when idle


class BaseRQJob(ABC):
    """
    Base class for RQ job implementations.

    Handles common setup and teardown:
    - Event loop management
    - Beanie (MongoDB ODM) initialization
    - Redis client creation (optional)
    - Exception handling and logging

    Subclasses must implement the `execute()` method with job-specific logic.

    Example:
        class MyJob(BaseRQJob):
            async def execute(self) -> Dict[str, Any]:
                # Job-specific async logic here
                result = await some_async_operation()
                return {"success": True, "result": result}

        # RQ job function wrapper
        def my_job_function(arg1, arg2, redis_url=None):
            job = MyJob(redis_url=redis_url)
            return job.run(arg1=arg1, arg2=arg2)
    """

    def __init__(self, redis_url: Optional[str] = None, initialize_beanie: bool = True):
        """
        Initialize base job with common dependencies.

        Args:
            redis_url: Redis connection URL (optional, creates client if provided)
            initialize_beanie: Whether to initialize Beanie ODM (default True)
        """
        self.redis_url = redis_url
        self.initialize_beanie = initialize_beanie
        self.redis_client: Optional[redis_async.Redis] = None
        self.job_start_time = time.time()

    async def _setup(self):
        """Setup common dependencies before job execution."""
        # Initialize Beanie for MongoDB access
        if self.initialize_beanie:
            await _ensure_beanie_initialized()
            logger.debug("Beanie initialized")

        # Create Redis client if URL provided
        if self.redis_url:
            self.redis_client = redis_async.from_url(self.redis_url)
            logger.debug(f"Redis client created: {self.redis_url}")

    async def _teardown(self):
        """Cleanup resources after job execution."""
        if self.redis_client:
            await self.redis_client.close()
            logger.debug("Redis client closed")

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Execute job-specific logic.

        This method must be implemented by subclasses.

        Args:
            **kwargs: Job-specific parameters passed from RQ

        Returns:
            Dict with job results
        """
        pass

    def run(self, **kwargs) -> Dict[str, Any]:
        """
        Run the job with common setup and teardown.

        This method:
        1. Creates a new event loop
        2. Calls _setup() for dependencies
        3. Calls execute() with job-specific logic
        4. Calls _teardown() for cleanup
        5. Handles exceptions and logging

        Args:
            **kwargs: Job-specific parameters to pass to execute()

        Returns:
            Dict with job results
        """
        job_name = self.__class__.__name__
        logger.info(f"🚀 Starting {job_name}")

        try:
            # Create new event loop for this job
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:

                async def process():
                    await self._setup()
                    try:
                        result = await self.execute(**kwargs)
                        return result
                    finally:
                        await self._teardown()

                result = loop.run_until_complete(process())

                elapsed = time.time() - self.job_start_time
                logger.info(f"✅ {job_name} completed in {elapsed:.2f}s")
                return result

            finally:
                loop.close()

        except Exception as e:
            elapsed = time.time() - self.job_start_time
            logger.error(
                f"❌ {job_name} failed after {elapsed:.2f}s: {e}", exc_info=True
            )
            raise


def async_job(
    redis: bool = True, beanie: bool = True, timeout: int = 300, result_ttl: int = 3600
):
    """
    Decorator to convert async functions into RQ-compatible job functions.

    Handles common setup/teardown:
    - Event loop management
    - Beanie (MongoDB ODM) initialization
    - Redis client creation (optional)
    - Exception handling and logging
    - Default job configuration (timeout, result_ttl)

    Args:
        redis: If True, creates Redis client and passes as 'redis_client' kwarg
        beanie: If True, initializes Beanie ODM (default True)
        timeout: Default job timeout in seconds (default 300 = 5 minutes)
        result_ttl: Default result TTL in seconds (default 3600 = 1 hour)

    Example:
        @async_job(redis=True, beanie=True, timeout=600)
        async def my_job(arg1, arg2, redis_client=None):
            # Job logic with redis_client available
            result = await some_async_operation()
            return {"success": True, "result": result}

        # Enqueue with defaults or override
        queue.enqueue(my_job, arg1_value, arg2_value)  # Uses timeout=600
        queue.enqueue(my_job, arg1_value, arg2_value, job_timeout=1200)  # Override
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Dict[str, Any]:
            job_name = func.__name__
            start_time = time.time()
            logger.info(f"🚀 Starting {job_name}")

            redis_client = None

            try:
                # Create new event loop for this job
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:

                    async def process():
                        nonlocal redis_client

                        # Initialize Beanie for MongoDB access
                        if beanie:
                            await _ensure_beanie_initialized()
                            logger.debug("Beanie initialized")

                        # Create Redis client if requested
                        if redis:
                            redis_client = create_async_redis()
                            kwargs["redis_client"] = redis_client
                            logger.debug(f"Redis client created")

                        try:
                            # Call the actual job function
                            result = await func(*args, **kwargs)
                            return result
                        finally:
                            # Cleanup Redis client
                            if redis_client:
                                await redis_client.close()
                                logger.debug("Redis client closed")

                    result = loop.run_until_complete(process())

                    elapsed = time.time() - start_time
                    logger.info(f"✅ {job_name} completed in {elapsed:.2f}s")
                    return result

                finally:
                    loop.close()

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"❌ {job_name} failed after {elapsed:.2f}s: {e}", exc_info=True
                )
                raise
            finally:
                # RQ work-horses exit with os._exit(), bypassing the OpenTelemetry
                # batch exporter's atexit hook. Flush after the complete job tree has
                # ended so late spans (notably the day-memory model call) are not lost.
                force_flush_otel()

        # Store default job configuration as attributes for RQ introspection
        wrapper.job_timeout = timeout
        wrapper.result_ttl = result_ttl

        return wrapper

    return decorator
