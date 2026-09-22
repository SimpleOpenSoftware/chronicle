"""
Health check routes for Chronicle backend.

This module provides health check endpoints for monitoring the application's status.
"""

import asyncio
import logging
import os
import time

import aiohttp
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient

import backend.service_deployment as service_deployment
from backend import __version__ as package_version
from backend.client_manager import get_client_manager
from backend.controllers.queue_controller import get_queue_health, redis_conn
from backend.heartbeat import FLEET_HEALTH_KEY, evaluate_fleet_health
from backend.llm_client import (
    async_health_check,
    async_health_check_fallback,
    async_health_check_fast,
)
from backend.model_registry import get_models_registry
from backend.services.memory import get_memory_service
from backend.services.transcription import get_transcription_provider

# Create router
router = APIRouter(tags=["health"])

# Logging setup
logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")

# MongoDB Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
mongo_client = AsyncIOMotorClient(MONGODB_URI)

EXPECTED_WAKEWORD_CONSUMER_GROUP = "wakeword-v2"
EXPECTED_WAKEWORD_STREAM_PATTERN = "audio:v2:realtime:*"


def evaluate_wakeword_health(body: dict, url: str) -> dict:
    """Validate the deployed wake worker, including its cross-service contract."""
    contract_matches = (
        body.get("status") == "ok"
        and body.get("consumer_alive") is True
        and body.get("consumer_group") == EXPECTED_WAKEWORD_CONSUMER_GROUP
        and body.get("stream_pattern") == EXPECTED_WAKEWORD_STREAM_PATTERN
    )
    return {
        "status": "Connected" if contract_matches else "Audio V2 contract mismatch",
        "healthy": contract_matches,
        "url": url,
        "consumer_group": body.get("consumer_group"),
        "stream_pattern": body.get("stream_pattern"),
        "expected_consumer_group": EXPECTED_WAKEWORD_CONSUMER_GROUP,
        "expected_stream_pattern": EXPECTED_WAKEWORD_STREAM_PATTERN,
        "critical": False,
    }


@router.get("/auth/health")
async def auth_health_check():
    """Pre-flight health check for authentication service connectivity."""
    try:
        # Test database connectivity
        await mongo_client.admin.command("ping")

        # Test memory service if available (creation itself can fail when the
        # LLM defaults are unconfigured, so it counts as unavailable, not a 500)
        try:
            memory_service = get_memory_service()
        except Exception as e:
            logger.warning(f"Memory service unavailable: {e}")
            memory_service = None
        if memory_service:
            try:
                await asyncio.wait_for(memory_service.test_connection(), timeout=2.0)
                memory_status = "ok"
            except Exception as e:
                logger.warning(f"Memory service health check failed: {e}")
                memory_status = "degraded"
        else:
            memory_status = "unavailable"

        return {
            "status": "ok",
            "database": "ok",
            "memory_service": memory_status,
            "timestamp": int(time.time()),
        }
    except Exception as e:
        logger.error(f"Auth health check failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "detail": "Service connectivity check failed",
                "error_type": "connection_failure",
                "timestamp": int(time.time()),
            },
        )


@router.get("/health")
async def health_check():
    """Comprehensive health check for all services."""
    # Load model config once for display fields
    _llm_def = None
    _llm_provider = "openai"
    _llm_model = None
    _llm_base_url = None
    _stt_name = None
    registry = None
    transcription_provider = get_transcription_provider()

    try:
        registry = get_models_registry()
        if registry:
            _defaults = registry.defaults
            _llm_name = _defaults.get("llm")
            _stt_name = _defaults.get("stt")
            _llm_def = registry.models.get(_llm_name)
            _llm_provider = _llm_def.model_provider if _llm_def else "openai"
            _llm_model = _llm_def.model_name if _llm_def else None
            _llm_base_url = _llm_def.resolved_url() if _llm_def else None
    except Exception as e:
        logger.warning(f"Failed to load model config for health check: {e}")
    health_status = {
        "status": "healthy",
        "version": os.getenv("CHRONICLE_BUILD_VERSION", "dev"),
        "timestamp": int(time.time()),
        "services": {},
        "config": {
            "mongodb_uri": MONGODB_URI,
            "transcription_service": (
                f"Speech to Text ({transcription_provider.name})"
                if transcription_provider
                else "Speech to Text (Not Configured)"
            ),
            "asr_uri": (
                f"{transcription_provider.mode.upper()} ({transcription_provider.name})"
                if transcription_provider
                else "Not configured"
            ),
            "transcription_provider": (
                registry.get_default("stt").name
                if registry and registry.get_default("stt")
                else "not configured"
            ),
            "provider_type": (
                transcription_provider.mode if transcription_provider else "none"
            ),
            "chunk_dir": str(os.getenv("CHUNK_DIR", "./audio_chunks")),
            "active_clients": get_client_manager().get_client_count(),
            "new_conversation_timeout_minutes": float(
                os.getenv("NEW_CONVERSATION_TIMEOUT_MINUTES", "1.5")
            ),
            "llm_provider": (_llm_def.model_provider if _llm_def else None),
            "llm_model": (_llm_def.model_name if _llm_def else None),
            "llm_base_url": (_llm_def.resolved_url() if _llm_def else None),
        },
    }

    overall_healthy = True
    critical_services_healthy = True

    # Get configuration once at the start
    # Memory provider (registry-based)
    mem_settings = registry.memory if registry else {}
    memory_provider = (mem_settings.get("provider") or "chronicle").lower()

    speaker_service_url = os.getenv("SPEAKER_SERVICE_URL")
    speaker_gateway_headers = {}
    speaker_registry = get_models_registry()
    speaker_config = speaker_registry.speaker_recognition if speaker_registry else {}
    if speaker_config.get("deployment"):

        speaker_service_url = service_deployment.gateway_url(
            speaker_config["deployment"], "speaker"
        )
        speaker_gateway_headers = {
            "X-Chronicle-Service-Token": service_deployment.gateway_token()
        }
    wakeword_service_url = os.getenv("WAKEWORD_SERVICE_URL")

    # Check MongoDB (critical service)
    try:
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=5.0)
        health_status["services"]["mongodb"] = {
            "status": "✅ Connected",
            "healthy": True,
            "critical": True,
        }
    except asyncio.TimeoutError:
        health_status["services"]["mongodb"] = {
            "status": "❌ Connection Timeout (5s)",
            "healthy": False,
            "critical": True,
        }
        overall_healthy = False
        critical_services_healthy = False
    except Exception as e:
        health_status["services"]["mongodb"] = {
            "status": f"❌ Connection Failed: {str(e)}",
            "healthy": False,
            "critical": True,
        }
        overall_healthy = False
        critical_services_healthy = False

    # Check Redis and RQ Workers (critical for queue processing)
    try:
        # Get queue health (includes Redis connection test and worker count)
        queue_health = await asyncio.wait_for(
            asyncio.to_thread(get_queue_health), timeout=5.0
        )

        # Check if Redis is healthy
        redis_healthy = queue_health.get("redis_connection") == "healthy"
        worker_count = queue_health.get("total_workers", 0)
        active_workers = queue_health.get("active_workers", 0)
        idle_workers = queue_health.get("idle_workers", 0)
        worker_fleet = queue_health.get("worker_fleet", {})

        if redis_healthy:
            health_status["services"]["redis"] = {
                "status": "✅ Connected",
                "healthy": True,
                "critical": True,
                "worker_count": worker_count,
                "active_workers": active_workers,
                "idle_workers": idle_workers,
                "queues": queue_health.get("queues", {}),
            }

            fleet_healthy = worker_fleet.get("healthy", False)
            health_status["services"]["workers"] = {
                **worker_fleet,
                "status": (
                    "✅ Worker fleet healthy"
                    if fleet_healthy
                    else f"❌ {worker_fleet.get('detail', 'Worker fleet unavailable')}"
                ),
                "fleet_status": worker_fleet.get("status", "unknown"),
                "healthy": fleet_healthy,
                "critical": True,
            }
            if not fleet_healthy:
                overall_healthy = False
                critical_services_healthy = False
        else:
            health_status["services"]["redis"] = {
                "status": f"❌ Connection Failed: {queue_health.get('redis_connection')}",
                "healthy": False,
                "critical": True,
                "worker_count": 0,
            }
            overall_healthy = False
            critical_services_healthy = False

    except asyncio.TimeoutError:
        health_status["services"]["redis"] = {
            "status": "❌ Connection Timeout (5s)",
            "healthy": False,
            "critical": True,
            "worker_count": 0,
        }
        overall_healthy = False
        critical_services_healthy = False
    except Exception as e:
        health_status["services"]["redis"] = {
            "status": f"❌ Connection Failed: {str(e)}",
            "healthy": False,
            "critical": True,
            "worker_count": 0,
        }
        overall_healthy = False
        critical_services_healthy = False

    # Check LLM service (non-critical service - may not be running)
    try:
        llm_health = await asyncio.wait_for(async_health_check(), timeout=8.0)
        is_healthy = llm_health.get("healthy", False)
        health_status["services"]["llm"] = {
            "status": llm_health.get("status", "❌ Unknown"),
            "healthy": is_healthy,
            "base_url": llm_health.get("base_url", ""),
            "model": llm_health.get("default_model", ""),
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
        if not is_healthy:
            overall_healthy = False
    except asyncio.TimeoutError:
        health_status["services"]["llm"] = {
            "status": "⚠️ Connection Timeout (8s) - Service may not be running",
            "healthy": False,
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
        overall_healthy = False
    except Exception as e:
        health_status["services"]["llm"] = {
            "status": f"⚠️ Connection Failed: {str(e)} - Service may not be running",
            "healthy": False,
            "provider": (_llm_def.model_provider if _llm_def else "unknown"),
            "critical": False,
        }
        overall_healthy = False

    # Check separate fast LLM, only when one is configured (defaults.fast_llm set
    # and distinct from defaults.llm). Reuses the main LLM otherwise.
    try:
        fast_health = await asyncio.wait_for(async_health_check_fast(), timeout=8.0)
        if fast_health is not None:
            is_healthy = fast_health.get("healthy", False)
            health_status["services"]["fast_llm"] = {
                "status": fast_health.get("status", "❌ Unknown"),
                "healthy": is_healthy,
                "base_url": fast_health.get("base_url", ""),
                "model": fast_health.get("default_model", ""),
                "critical": False,
            }
            if not is_healthy:
                overall_healthy = False
    except Exception as e:  # noqa: BLE001 - fast LLM is optional/non-critical
        health_status["services"]["fast_llm"] = {
            "status": f"⚠️ Connection Failed: {str(e)} - Service may not be running",
            "healthy": False,
            "critical": False,
        }
        overall_healthy = False

    # Check separate fallback LLM, only when one is configured
    # (defaults.fallback_llm set and distinct from defaults.llm).
    try:
        fb_health = await asyncio.wait_for(async_health_check_fallback(), timeout=8.0)
        if fb_health is not None:
            is_healthy = fb_health.get("healthy", False)
            health_status["services"]["fallback_llm"] = {
                "status": fb_health.get("status", "❌ Unknown"),
                "healthy": is_healthy,
                "base_url": fb_health.get("base_url", ""),
                "model": fb_health.get("default_model", ""),
                "critical": False,
            }
            if not is_healthy:
                overall_healthy = False
    except Exception as e:  # noqa: BLE001 - fallback LLM is optional/non-critical
        health_status["services"]["fallback_llm"] = {
            "status": f"⚠️ Connection Failed: {str(e)} - Service may not be running",
            "healthy": False,
            "critical": False,
        }
        overall_healthy = False

    # Check memory service (Chronicle agentic vault). Created here rather than at
    # module load — creation raises when LLM defaults are unconfigured, and an
    # import-time failure would take down every route in this package.
    try:
        memory_service = get_memory_service()
        test_success = await asyncio.wait_for(
            memory_service.test_connection(), timeout=8.0
        )
        if test_success:
            health_status["services"]["memory_service"] = {
                "status": "✅ Chronicle Memory Connected",
                "healthy": True,
                "provider": memory_provider,
                "critical": False,
            }
        else:
            health_status["services"]["memory_service"] = {
                "status": "⚠️ Chronicle Memory Test Failed",
                "healthy": False,
                "provider": memory_provider,
                "critical": False,
            }
            overall_healthy = False
    except Exception as e:
        health_status["services"]["memory_service"] = {
            "status": f"⚠️ Chronicle Memory Failed: {str(e)}",
            "healthy": False,
            "provider": memory_provider,
            "critical": False,
        }
        overall_healthy = False

    # Check Speech to Text services — both batch (stored transcripts) and
    # streaming (live, real-time). Each is reported separately so the status
    # page shows what's configured for each mode, or flags it as unconfigured.
    async def _check_stt(provider, label):
        if provider is None:
            return {
                "status": "⚠️ Not configured",
                "healthy": False,
                "type": label,
                "provider": "None",
                "configured": False,
                "critical": False,
            }
        try:
            h = await asyncio.wait_for(provider.health_check(), timeout=8.0)
            return {
                "status": h.get("status", "❌ Unknown"),
                "healthy": h.get("healthy", False),
                "type": label,
                "provider": provider.name,
                "configured": True,
                "critical": False,
            }
        except asyncio.TimeoutError:
            return {
                "status": "⚠️ Connection Timeout (8s)",
                "healthy": False,
                "type": label,
                "provider": provider.name,
                "configured": True,
                "critical": False,
            }
        except Exception as e:
            return {
                "status": f"⚠️ Provider Error: {str(e)}",
                "healthy": False,
                "type": label,
                "provider": provider.name,
                "configured": True,
                "critical": False,
            }

    batch_provider = get_transcription_provider(mode="batch")
    streaming_provider = get_transcription_provider(mode="streaming")

    health_status["services"]["speech_to_text"] = await _check_stt(
        batch_provider, "Batch"
    )
    health_status["services"]["speech_to_text_streaming"] = await _check_stt(
        streaming_provider, "Streaming"
    )

    # Batch STT is required for stored transcripts; missing → degraded overall.
    # Streaming is optional (live preview only), so it stays a non-fatal warning.
    if batch_provider is None:
        health_status["services"]["speech_to_text"][
            "status"
        ] = "❌ No batch STT configured (set defaults.stt)"
        overall_healthy = False
    if streaming_provider is None:
        health_status["services"]["speech_to_text_streaming"][
            "status"
        ] = "⚠️ No streaming STT configured (set defaults.stt_stream)"

    # Check Speaker Recognition service (non-critical - optional feature)
    if speaker_service_url:
        try:
            # Make a health check request to the speaker service
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{speaker_service_url}/health",
                    headers=speaker_gateway_headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status == 200:
                        health_status["services"]["speaker_recognition"] = {
                            "status": "✅ Connected",
                            "healthy": True,
                            "url": speaker_service_url,
                            "critical": False,
                        }
                    else:
                        health_status["services"]["speaker_recognition"] = {
                            "status": f"⚠️ Unhealthy: HTTP {response.status}",
                            "healthy": False,
                            "url": speaker_service_url,
                            "critical": False,
                        }
                        overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["speaker_recognition"] = {
                "status": "⚠️ Connection Timeout (5s)",
                "healthy": False,
                "url": speaker_service_url,
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["speaker_recognition"] = {
                "status": f"⚠️ Connection Failed: {str(e)}",
                "healthy": False,
                "url": speaker_service_url,
                "critical": False,
            }
            overall_healthy = False

    # Check Wake-word service (non-critical - optional feature). Only probed when
    # WAKEWORD_SERVICE_URL is configured, mirroring the speaker_recognition gate.
    if wakeword_service_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{wakeword_service_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status == 200:
                        body = await response.json()
                        wakeword_health = evaluate_wakeword_health(
                            body, wakeword_service_url
                        )
                        health_status["services"]["wakeword"] = wakeword_health
                        if not wakeword_health["healthy"]:
                            overall_healthy = False
                    else:
                        health_status["services"]["wakeword"] = {
                            "status": f"⚠️ Unhealthy: HTTP {response.status}",
                            "healthy": False,
                            "url": wakeword_service_url,
                            "critical": False,
                        }
                        overall_healthy = False
        except asyncio.TimeoutError:
            health_status["services"]["wakeword"] = {
                "status": "⚠️ Connection Timeout (5s)",
                "healthy": False,
                "url": wakeword_service_url,
                "critical": False,
            }
            overall_healthy = False
        except Exception as e:
            health_status["services"]["wakeword"] = {
                "status": f"⚠️ Connection Failed: {str(e)}",
                "healthy": False,
                "url": wakeword_service_url,
                "critical": False,
            }
            overall_healthy = False

    # Set overall status
    health_status["overall_healthy"] = overall_healthy
    health_status["critical_services_healthy"] = critical_services_healthy

    if not critical_services_healthy:
        health_status["status"] = "critical"
    elif not overall_healthy:
        health_status["status"] = "degraded"
    else:
        health_status["status"] = "healthy"

    # Add helpful messages
    if not overall_healthy:
        messages = []
        if not critical_services_healthy:
            messages.append(
                "Critical services (MongoDB) are unavailable - core functionality will not work"
            )

        unhealthy_optional = [
            name
            for name, service in health_status["services"].items()
            if not service["healthy"] and not service.get("critical", True)
        ]
        if unhealthy_optional:
            messages.append(
                f"Optional services unavailable: {', '.join(unhealthy_optional)}"
            )

        health_status["message"] = "; ".join(messages)

    return JSONResponse(content=health_status, status_code=200)


@router.get("/version")
async def version_check():
    """Report the running backend build version. Unauthenticated, like /health.

    ``version`` is the baked-in git describe / release tag (CHRONICLE_BUILD_VERSION,
    defaults to "dev"); ``package_version`` is the Python package version.
    """
    return {
        "version": os.getenv("CHRONICLE_BUILD_VERSION", "dev"),
        "package_version": package_version,
        "timestamp": int(time.time()),
    }


@router.get("/readiness")
async def readiness_check():
    """Simple readiness check for container orchestration."""
    # Use debug level for health check to reduce log spam
    logger.debug("Readiness check requested")

    # Only check critical services for readiness
    try:
        # MongoDB and the worker fleet are both required for accepting audio. A
        # backend with no workers can receive recordings but cannot persist or
        # transcribe them, so reporting ready would permit silent data loss.
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=2.0)
        await asyncio.wait_for(asyncio.to_thread(redis_conn.ping), timeout=2.0)
        fleet = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: evaluate_fleet_health(redis_conn.get(FLEET_HEALTH_KEY))
            ),
            timeout=2.0,
        )
        if not fleet["healthy"]:
            raise RuntimeError(fleet.get("detail") or "Worker fleet unavailable")
        memory_service = get_memory_service()
        memory_ready = await asyncio.wait_for(
            memory_service.test_connection(), timeout=2.0
        )
        if not memory_ready:
            raise RuntimeError("Memory service configuration is unavailable")
        return JSONResponse(
            content={"status": "ready", "timestamp": int(time.time())}, status_code=200
        )
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return JSONResponse(
            content={
                "status": "not_ready",
                "error": str(e),
                "timestamp": int(time.time()),
            },
            status_code=503,
        )
