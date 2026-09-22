"""
Main API router for Chronicle backend.

This module aggregates all the functional router modules and provides
a single entry point for the API endpoints.
"""

import logging
import os

from fastapi import APIRouter

from .modules import (
    admin_router,
    annotation_router,
    api_key_router,
    audio_router,
    chat_router,
    client_diagnostic_router,
    client_router,
    conversation_router,
    data_audit_router,
    device_input_router,
    finetuning_router,
    manual_memory_router,
    memory_router,
    memory_space_router,
    notification_router,
    openai_compat_router,
    queue_router,
    sse_router,
    system_events_router,
    system_router,
    timeline_router,
    user_router,
    vault_sync_router,
    wakeword_router,
)
from .modules.health_routes import router as health_router
from .modules.source_search_routes import router as source_search_router

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

# Create main API router
router = APIRouter(prefix="/api", tags=["api"])

# Include all sub-routers
router.include_router(admin_router)
router.include_router(annotation_router)
router.include_router(api_key_router)
router.include_router(audio_router)
router.include_router(user_router)
router.include_router(chat_router)
router.include_router(client_diagnostic_router)
router.include_router(client_router)
router.include_router(conversation_router)
router.include_router(data_audit_router)
router.include_router(device_input_router)
router.include_router(finetuning_router)
router.include_router(memory_router)
router.include_router(memory_space_router)
router.include_router(manual_memory_router)
router.include_router(notification_router)
router.include_router(openai_compat_router)
router.include_router(sse_router)
router.include_router(system_events_router)
router.include_router(system_router)
router.include_router(timeline_router)
router.include_router(source_search_router)
router.include_router(queue_router)
router.include_router(vault_sync_router)
router.include_router(wakeword_router)
router.include_router(
    health_router
)  # Also include under /api for frontend compatibility

# Conditionally include test routes (only in test environments)
if os.getenv("DEBUG_DIR"):
    try:
        from .modules.test_routes import router as test_router

        router.include_router(test_router)
        logger.info("✅ Test routes loaded (test environment detected)")
    except Exception as e:
        logger.error(f"Error loading test routes: {e}", exc_info=True)

logger.info("API router initialized with all sub-modules")
