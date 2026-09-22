"""
Application factory for Chronicle backend.

Creates and configures the FastAPI application with all routers, middleware,
and service initializations.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from beanie import init_beanie
from fastapi import FastAPI

import backend.services.privacy as privacy
from backend.app_config import get_app_config
from backend.auth import (
    bearer_backend,
    cookie_backend,
    create_admin_user_if_needed,
    current_superuser,
    fastapi_users,
    websocket_auth,
)
from backend.browser_sessions import BrowserSession
from backend.browser_sessions import router as browser_session_router
from backend.client_manager import (
    get_client_manager,
    initialize_redis_for_client_manager,
)
from backend.config_loader import load_config
from backend.controllers.capture_lifecycle import cleanup_client_state
from backend.controllers.data_audit_controller import run_auto_clean_cron
from backend.controllers.queue_controller import redis_conn
from backend.cron_scheduler import get_scheduler, register_cron_job
from backend.llm_client import get_llm_client
from backend.middleware.app_middleware import setup_middleware
from backend.model_routes import (
    effective_model_routes,
    effective_operation_routes,
    format_model_routes,
)
from backend.models.annotation import Annotation
from backend.models.api_key import ApiKey
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
from backend.models.system_event import SystemEvent
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
from backend.models.waveform import WaveformData
from backend.observability.otel_setup import init_otel
from backend.prompt_defaults import register_all_defaults
from backend.prompt_registry import get_prompt_registry
from backend.redis_factory import create_async_redis
from backend.routers.api_router import router as api_router
from backend.routers.modules.health_routes import router as health_router
from backend.routers.modules.websocket_routes import router as websocket_router
from backend.services.audio_service import get_audio_stream_service
from backend.services.audio_stream import AudioStreamProducer
from backend.services.audio_stream.reclaim import reclaim_settled_audio_streams
from backend.services.chat_review import process_chat_review_queue
from backend.services.device_audio_ingest import process_device_audio
from backend.services.device_context import purge_screen_context
from backend.services.immich_discovery import scan_immich_memories
from backend.services.manual_memories.image import process_manual_memory_images
from backend.services.manual_memories.visual_index import (
    process_manual_memory_visual_index,
)
from backend.services.memory import get_memory_service, shutdown_memory_service
from backend.services.memory.agent.operating_memory_optimizer import (
    run_operating_memory_daily_job,
    run_operating_memory_threshold_job,
)
from backend.services.memory.syncthing_audit import start_syncthing_audit_listener
from backend.services.notifications import queue_due_notifications, queue_receipt_check
from backend.services.observability import run_event_ingest_drain
from backend.services.observability.health_poller import run_health_poller
from backend.services.observability.loop_monitor import start_loop_monitor
from backend.services.person_photos import sync_person_photos
from backend.services.plugin_service import (
    cleanup_plugin_router,
    init_plugin_router,
    initialize_plugins,
    run_plugin_recovery,
    set_plugin_router,
)
from backend.services.reaper import run_reaper
from backend.services.source_search import recover_search_index
from backend.services.speaker_enrollment import recover_speaker_enrollments
from backend.services.status_reconciler import reconcile_conversation_statuses
from backend.services.timeline.accepted_context import recover_context_assessments
from backend.services.timeline.consolidation import prefetch_consolidation_horizon
from backend.services.timeline.dirty_ranges import reconcile_dirty_ranges
from backend.services.timeline.dispatch import dispatch_ready_episodes
from backend.services.timeline.publication import recover_timeline_publications
from backend.services.timeline.review import process_memory_review_queue
from backend.services.timeline.sessions import prepare_recent_sessions
from backend.services.timeline.thumbnails import process_episode_thumbnails
from backend.task_manager import get_task_manager, init_task_manager
from backend.users import User, UserRead, UserUpdate, register_client_to_user
from backend.workers.annotation_jobs import surface_error_suggestions
from backend.workers.finetuning_jobs import (
    run_asr_finetuning_job,
    run_asr_jargon_extraction_job,
    run_speaker_finetuning_job,
)
from backend.workers.prompt_optimization_jobs import run_prompt_optimization_job

logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")


def register_application_cron_jobs() -> None:
    """Register the production cron entrypoints in one directly testable seam."""

    register_cron_job("speaker_finetuning", run_speaker_finetuning_job)
    register_cron_job("speaker_enrollment_recovery", recover_speaker_enrollments)
    register_cron_job("asr_finetuning", run_asr_finetuning_job)
    register_cron_job("asr_jargon_extraction", run_asr_jargon_extraction_job)
    register_cron_job("prompt_optimization", run_prompt_optimization_job)
    register_cron_job("annotation_suggestions", surface_error_suggestions)
    register_cron_job("auto_clean", run_auto_clean_cron)
    register_cron_job("immich_memories", scan_immich_memories)
    register_cron_job("person_photos", sync_person_photos)
    register_cron_job("device_audio_ingest", process_device_audio)
    register_cron_job("screen_context_retention", purge_screen_context)
    register_cron_job("timeline_publication_recovery", recover_timeline_publications)
    register_cron_job("timeline_episode_dispatch_recovery", dispatch_ready_episodes)
    register_cron_job("timeline_consolidation_prefetch", prefetch_consolidation_horizon)
    register_cron_job("rolling_reconciliation_scan", reconcile_dirty_ranges)
    register_cron_job("notification_dispatch", queue_due_notifications)
    register_cron_job("notification_receipts", queue_receipt_check)
    register_cron_job("episode_thumbnails", process_episode_thumbnails)
    register_cron_job("manual_memory_image_enrichment", process_manual_memory_images)
    register_cron_job("manual_memory_visual_index", process_manual_memory_visual_index)
    register_cron_job("episode_memory_review", process_memory_review_queue)
    register_cron_job("chat_note_review", process_chat_review_queue)
    register_cron_job("session_memory_prepare", prepare_recent_sessions)
    register_cron_job("source_search_index", recover_search_index)
    register_cron_job("vault_context_assessment", recover_context_assessments)
    register_cron_job("audio_stream_reclaim", reclaim_settled_audio_streams)
    register_cron_job(
        "pi_operating_memory_threshold", run_operating_memory_threshold_job
    )
    register_cron_job("pi_operating_memory_daily", run_operating_memory_daily_job)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan events."""
    config = get_app_config()
    startup_start = time.monotonic()

    # Startup
    application_logger.info("Starting application...")
    # Model selection is intentionally granular. Print the selected high-level routes
    # together so an accidental Codex/cloud island is visible without reading YAML.
    selected_model_routes = effective_model_routes(load_config())
    application_logger.info(
        "Effective model routes:\n%s", format_model_routes(selected_model_routes)
    )
    external_selected_routes = [
        route for route in selected_model_routes if route["location"] == "external"
    ]
    if external_selected_routes:
        application_logger.warning(
            "External selected high-level model routes:\n%s",
            format_model_routes(external_selected_routes),
        )
    operation_routes = effective_operation_routes()
    external_operations = [
        route for route in operation_routes if route["location"] == "external"
    ]
    if external_operations:
        application_logger.warning(
            "External named LLM operations:\n%s",
            format_model_routes(external_operations),
        )
    else:
        application_logger.info(
            "Named LLM operation audit: all %d routes are self-hosted",
            len(operation_routes),
        )

    # ── Phase 1 (sequential — dependencies) ──────────────────────────
    phase_start = time.monotonic()

    # Refuse an implicit conversion of durable human review decisions.
    from .services.timeline.review_storage import assert_memory_review_storage_ready

    await assert_memory_review_storage_ready(config.db)
    # Initialize Beanie for all document models
    try:
        await init_beanie(
            database=config.db,
            document_models=[
                User,
                ApiKey,
                BrowserSession,
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
                SystemEvent,
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
        application_logger.info("Beanie initialized for all document models")
    except Exception as e:
        application_logger.error(f"Failed to initialize Beanie: {e}")
        raise

    # Create admin user if needed (requires Beanie)
    try:
        await create_admin_user_if_needed()
    except Exception as e:
        application_logger.error(f"Failed to create admin user: {e}")

    application_logger.info(
        f"Phase 1 (Beanie + admin) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 2 (parallel — all independent) ─────────────────────────
    phase_start = time.monotonic()

    async def _init_redis_rq():
        try:

            redis_conn.ping()
            application_logger.info("Redis connection established for RQ")
        except Exception as e:
            application_logger.error(f"Failed to connect to Redis for RQ: {e}")
            application_logger.warning(
                "RQ queue system will not be available - check Redis connection"
            )

    async def _init_task_manager():
        try:
            tm = init_task_manager()
            await tm.start()
            application_logger.info("BackgroundTaskManager initialized and started")
        except Exception as e:
            application_logger.error(f"Failed to initialize task manager: {e}")
            raise  # Task manager is essential

    async def _init_client_manager():
        get_client_manager()
        application_logger.info("ClientManager initialized")

    async def _init_otel():
        try:

            init_otel()
        except Exception as e:
            application_logger.warning(f"OTEL initialization skipped: {e}")

    async def _init_prompt_registry():
        try:

            registry = get_prompt_registry()
            register_all_defaults(registry)
            application_logger.info(
                f"Prompt registry initialized with {len(registry._defaults)} defaults"
            )
        except Exception as e:
            application_logger.warning(f"Prompt registry initialization failed: {e}")

    await asyncio.gather(
        _init_redis_rq(),
        _init_task_manager(),
        _init_client_manager(),
        _init_otel(),
        _init_prompt_registry(),
    )

    application_logger.info(
        f"Phase 2 (Redis/TaskMgr/ClientMgr/OTEL/Prompts) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 3 (parallel — OTEL done, safe for LLM patching) ────────
    phase_start = time.monotonic()

    async def _init_llm_client():
        try:

            get_llm_client()
            application_logger.info("LLM client initialized from config.yml")
        except Exception as e:
            application_logger.warning(f"LLM client initialization deferred: {e}")

    async def _init_audio_stream_service():
        try:
            audio_service = get_audio_stream_service()
            await audio_service.connect()
            application_logger.info("Audio stream service connected to Redis Streams")
        except Exception as e:
            application_logger.error(f"Failed to connect audio stream service: {e}")
            application_logger.warning(
                "Redis Streams audio processing will not be available"
            )

    async def _init_redis_audio_producer():
        try:
            app.state.redis_audio_stream = create_async_redis(decode_responses=False)

            app.state.audio_stream_producer = AudioStreamProducer(
                app.state.redis_audio_stream
            )
            application_logger.info(
                "Redis client for audio streaming producer initialized"
            )

            initialize_redis_for_client_manager()
        except Exception as e:
            application_logger.error(
                f"Failed to initialize Redis client for audio streaming: {e}",
                exc_info=True,
            )
            application_logger.warning("Audio streaming producer will not be available")

    async def _deferred_prompt_seed():
        """Seed prompts into Langfuse with retry backoff."""
        try:

            registry = get_prompt_registry()
        except Exception:
            return

        backoff_delays = [0, 2, 4, 8, 16, 32]
        for delay in backoff_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                await registry.seed_prompts()
                application_logger.info("Prompt seeding to Langfuse completed")
                return
            except Exception as e:
                application_logger.debug(
                    f"Prompt seeding attempt failed (next retry in {delay}s): {e}"
                )
        application_logger.warning(
            "Prompt seeding to Langfuse failed after all retries"
        )

    await asyncio.gather(
        _init_llm_client(),
        _init_audio_stream_service(),
        _init_redis_audio_producer(),
    )

    # Launch deferred prompt seeding as a fire-and-forget background task
    asyncio.create_task(_deferred_prompt_seed())

    application_logger.info(
        f"Phase 3 (LLM/AudioStream/RedisProducer) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # ── Phase 4 (parallel — all independent) ─────────────────────────
    phase_start = time.monotonic()

    application_logger.info(
        "Memory service will be initialized on first use (lazy loading)"
    )

    async def _init_cron_scheduler():
        try:
            register_application_cron_jobs()

            scheduler = get_scheduler()
            await scheduler.start()
            application_logger.info("Cron scheduler started")
        except Exception as e:
            application_logger.warning(f"Cron scheduler failed to start: {e}")

    async def _init_plugins():
        try:

            plugin_router = init_plugin_router()

            if plugin_router:
                await initialize_plugins(plugin_router)

                health = plugin_router.get_health_summary()
                application_logger.info(
                    f"Plugins initialized: {health['initialized']}/{health['total']} active"
                    + (f", {health['degraded']} degraded" if health["degraded"] else "")
                    + (f", {health['failed']} failed" if health["failed"] else "")
                )

                app.state.plugin_router = plugin_router
                set_plugin_router(plugin_router)
                # Background recovery: retries degraded/failed plugins with backoff
                # (e.g. Home Assistant on a server that's off at boot) and demotes
                # initialized plugins whose health_check starts failing.
                app.state.plugin_recovery_task = asyncio.create_task(
                    run_plugin_recovery(plugin_router)
                )
            else:
                application_logger.info("No plugins configured")
                app.state.plugin_router = None
                app.state.plugin_recovery_task = None

        except Exception as e:
            application_logger.error(
                f"Failed to initialize plugin system: {e}", exc_info=True
            )
            app.state.plugin_router = None
            app.state.plugin_recovery_task = None

    await asyncio.gather(
        _init_cron_scheduler(),
        _init_plugins(),
    )

    application_logger.info(
        f"Phase 4 (Cron/Plugins) completed in {time.monotonic() - phase_start:.2f}s"
    )

    # Inbound vault edits (human edits in Obsidian, delivered by Syncthing) are
    # recorded into the memory audit ledger by a background listener. No-ops when
    # vault sync isn't configured.
    try:

        app.state.syncthing_audit_task = start_syncthing_audit_listener()
    except Exception as e:
        application_logger.warning(f"Syncthing memory-audit listener not started: {e}")
        app.state.syncthing_audit_task = None

    # Backstop reaper: one periodic loop that force-cleans stale clients (zombie
    # "connected" devices), orphaned audio streams the idle-timeout path missed, and
    # orphaned deferred RQ jobs whose dependency was deleted (never promotable).
    try:

        app.state.reaper_task = asyncio.create_task(run_reaper())
    except Exception as e:
        application_logger.warning(f"Reaper not started: {e}")
        app.state.reaper_task = None

    # Observability: drain the system-event ingest list (filled by RQ workers and the
    # catch-all log handler) into Mongo + SSE, and poll service health to record
    # crash-loop / down / recovered transitions.
    try:

        app.state.system_event_drain_task = asyncio.create_task(
            run_event_ingest_drain()
        )
        app.state.health_poller_task = asyncio.create_task(run_health_poller(app))
        # Measures this loop's own scheduling delay. A blocked loop fails nothing
        # that a container probe can see — it just makes everything slow at once.
        app.state.loop_monitor_task = start_loop_monitor("backend")
    except Exception as e:
        application_logger.warning(f"Observability tasks not started: {e}")
        app.state.system_event_drain_task = None
        app.state.health_poller_task = None
        app.state.loop_monitor_task = None

    # One-shot startup reconcile: recompute processing_status from facts once, so any
    # drift left before this version (or by a failure callback that itself died) is
    # healed at boot. Steady-state recovery is now event-driven — the post-conversation
    # chain uses Retry + Dependency(allow_failure=True) + an on_failure callback, so a
    # crashed/abandoned job recovers and surfaces a system event on its own without a
    # periodic poll. This boot sweep + the admin endpoint
    # (/api/admin/conversations/reconcile-status) are the remaining backstops. Run as a
    # background task so the (full-collection) scan doesn't block startup.
    try:

        app.state.status_reconciler_task = asyncio.create_task(
            reconcile_conversation_statuses()
        )
    except Exception as e:
        application_logger.warning(f"Startup status reconcile not started: {e}")
        app.state.status_reconciler_task = None

    total_startup = time.monotonic() - startup_start
    application_logger.info(
        f"Application ready in {total_startup:.2f}s - using application-level processing architecture."
    )

    logger.info("App ready")
    try:
        yield
    finally:
        # Shutdown
        application_logger.info("Shutting down application...")

        # Clean up all active clients
        client_manager = get_client_manager()
        for client_id in client_manager.get_all_client_ids():
            try:

                await cleanup_client_state(client_id)
            except Exception as e:
                application_logger.error(f"Error cleaning up client {client_id}: {e}")

        # Stop the Syncthing memory-audit listener
        try:
            audit_task = getattr(app.state, "syncthing_audit_task", None)
            if audit_task is not None:
                audit_task.cancel()
                application_logger.info("Syncthing memory-audit listener stopped")
        except Exception as e:
            application_logger.error(f"Error stopping memory-audit listener: {e}")

        # Stop the backstop reaper
        try:
            reaper_task = getattr(app.state, "reaper_task", None)
            if reaper_task is not None:
                reaper_task.cancel()
                application_logger.info("Reaper stopped")
        except Exception as e:
            application_logger.error(f"Error stopping reaper: {e}")

        # Stop the plugin recovery loop
        try:
            recovery_task = getattr(app.state, "plugin_recovery_task", None)
            if recovery_task is not None:
                recovery_task.cancel()
                application_logger.info("Plugin recovery loop stopped")
        except Exception as e:
            application_logger.error(f"Error stopping plugin recovery loop: {e}")

        # Stop the observability tasks (event drain + health poller)
        for _attr, _label in (
            ("system_event_drain_task", "System-event drain"),
            ("health_poller_task", "Health poller"),
            ("loop_monitor_task", "Event-loop monitor"),
        ):
            try:
                _task = getattr(app.state, _attr, None)
                if _task is not None:
                    _task.cancel()
                    application_logger.info(f"{_label} stopped")
            except Exception as e:
                application_logger.error(f"Error stopping {_label}: {e}")

        # Shutdown BackgroundTaskManager
        try:
            task_mgr = get_task_manager()
            await task_mgr.shutdown()
            application_logger.info("BackgroundTaskManager shut down")
        except RuntimeError:
            pass  # Never initialized
        except Exception as e:
            application_logger.error(f"Error shutting down task manager: {e}")

        # RQ workers shut down automatically when process ends
        # No special cleanup needed for Redis connections

        # Shutdown audio stream service
        try:
            audio_service = get_audio_stream_service()
            await audio_service.disconnect()
            application_logger.info("Audio stream service disconnected")
        except Exception as e:
            application_logger.error(f"Error disconnecting audio stream service: {e}")

        # Close Redis client for audio streaming producer
        try:
            if (
                hasattr(app.state, "redis_audio_stream")
                and app.state.redis_audio_stream
            ):
                await app.state.redis_audio_stream.close()
                application_logger.info(
                    "Redis client for audio streaming producer closed"
                )
        except Exception as e:
            application_logger.error(f"Error closing Redis audio streaming client: {e}")

        # Stop metrics collection and save final report
        application_logger.info("Metrics collection stopped")

        # Shutdown plugins
        try:

            await cleanup_plugin_router()
            application_logger.info("Plugins shut down")
        except Exception as e:
            application_logger.error(f"Error shutting down plugins: {e}")

        # Shutdown cron scheduler
        try:

            scheduler = get_scheduler()
            await scheduler.stop()
            application_logger.info("Cron scheduler stopped")
        except Exception as e:
            application_logger.error(f"Error stopping cron scheduler: {e}")

        # Shutdown memory service and speaker service
        shutdown_memory_service()
        application_logger.info("Memory and speaker services shut down.")

        application_logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Create FastAPI application with lifespan management
    app = FastAPI(lifespan=lifespan)

    # Set up middleware (CORS, exception handlers)
    setup_middleware(
        app,
        disable_request_logging=os.getenv("DISABLE_REQUEST_LOGGING", "").lower()
        == "true",
    )

    app.add_exception_handler(privacy.PrivacyHeld, privacy.held_response)

    # Include all routers
    app.include_router(api_router)

    # Add health check router at root level (not under /api prefix)
    app.include_router(health_router)

    # Add WebSocket router at root level (not under /api prefix)
    app.include_router(websocket_router)

    app.include_router(browser_session_router)

    # Add authentication routers
    app.include_router(
        fastapi_users.get_auth_router(cookie_backend),
        prefix="/auth/cookie",
        tags=["auth"],
    )
    app.include_router(
        fastapi_users.get_auth_router(bearer_backend),
        prefix="/auth/jwt",
        tags=["auth"],
    )

    # Add users router for /users/me and other user endpoints
    app.include_router(
        fastapi_users.get_users_router(UserRead, UserUpdate),
        prefix="/users",
        tags=["users"],
    )

    logger.info(
        "FastAPI application created with all routers and middleware configured"
    )

    return app
