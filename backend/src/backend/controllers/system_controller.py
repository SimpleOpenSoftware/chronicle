"""
System controller for handling system-related business logic.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import signal
import time
import warnings
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import set_key as dotenv_set_key
from fastapi import HTTPException
from ruamel.yaml import YAML

from backend.chat_service import reset_chat_service
from backend.client_manager import get_client_manager
from backend.config import CleanupSettings, get_cleanup_settings
from backend.config import get_diarization_settings as load_diarization_settings
from backend.config import get_misc_settings as load_misc_settings
from backend.config import (
    save_cleanup_settings,
    save_diarization_settings,
    save_misc_settings,
)
from backend.config_loader import (
    get_backend_config,
    get_config_load_warnings,
    get_plugins_yml_path,
    get_raw_models,
    load_config,
    save_config_section,
    save_models_list,
)
from backend.controllers import client_controller
from backend.model_registry import (
    ModelDef,
    _find_config_path,
    get_models_registry,
    load_models_config,
)
from backend.model_routes import effective_model_routes, effective_operation_routes
from backend.models.user import User
from backend.observability.otel_setup import is_langfuse_enabled
from backend.openai_factory import create_openai_client
from backend.services.memory import get_memory_service, reset_memory_service
from backend.services.plugin_service import (
    _get_plugins_dir,
    discover_plugins,
    expand_env_vars,
    get_plugin_metadata,
    load_plugin_env,
    reload_plugins,
    save_plugin_env,
    signal_worker_restart,
)
from backend.speaker_recognition_client import SpeakerRecognitionClient

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


async def get_network_discovery(app, current_user=None):
    """Return Tailscale status and discovered minidisc services.

    The *app* parameter is the FastAPI application instance (kept for API
    compatibility but no longer used — the node agent handles advertising).
    """

    result = {
        "tailscale_available": False,
        "advertising": [],
        "discovered_services": [],
    }

    try:
        # Lazy import: optional external module (edge/discovery, resolved via a
        # sys.path arrangement) that may be absent; guarded by the ImportError below.
        from discovery import is_tailscale_available, list_all_services
    except ImportError:
        result["error"] = "discovery module not available"
        return result

    result["tailscale_available"] = is_tailscale_available()

    # Read advertised services written by the node agent (edge/service_manager.py).
    # The file is at config/advertised-services.json (volume-mounted from repo root).
    _advertised_path = Path("/app/config/advertised-services.json")
    if _advertised_path.exists():
        try:
            result["advertising"] = json.loads(_advertised_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read advertised-services.json: %s", exc)

    if not result["tailscale_available"]:
        return result

    # Discover all chronicle-* services on the Tailnet via list_all_services()
    loop = asyncio.get_running_loop()
    all_services = await loop.run_in_executor(None, list_all_services)

    async def _health_check(svc: dict):
        name = svc["name"]
        address = svc.get("address", "")
        port = svc.get("port", 0)
        labels = svc.get("labels", {})
        host = labels.get("host", address)

        url = f"http://{address}:{port}" if address and port else None
        reachable = False
        if url:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{url}/health")
                    reachable = resp.status_code < 500
            except Exception:
                pass
        return {
            "name": name,
            "url": url,
            "reachable": reachable,
            "labels": labels,
            "host": host,
        }

    if all_services:
        discovered = await asyncio.gather(*[_health_check(svc) for svc in all_services])
        result["discovered_services"] = list(discovered)
    else:
        result["discovered_services"] = []

    # Connected WebSocket clients (phones, relays, etc.)
    # Devices are the user's *remembered* devices (the registry) joined with live
    # connection state — so a known device shows whether it's online now or when it was
    # last seen, with its editable friendly name. "connected" is derived from real
    # activity (the live ClientState's last_activity), never a persisted flag.
    mgr = get_client_manager()
    if current_user is not None:
        devices = (await client_controller.list_devices(current_user, mgr))["devices"]
    else:
        devices = []
    result["connected_devices"] = devices

    return result


_yaml = YAML()
_yaml.preserve_quotes = True


def _is_self_hosted_model(model) -> bool:
    """Whether a model entry points at a self-hosted service (no API key needed).

    Cloud providers (Deepgram, OpenAI, smallest.ai, ...) live on public domains;
    self-hosted services are reached via localhost, docker hostnames, private/
    tailnet IPs, or tailnet DNS names.
    """
    host = urlparse(str(getattr(model, "model_url", "") or "")).hostname or ""
    if not host:
        return False
    if host in ("localhost", "host.docker.internal"):
        return True
    if re.match(r"^(127\.|10\.|172\.|192\.168\.|100\.)", host):
        return True
    # Tailnet DNS names, or bare docker-compose service names (no dots)
    return host.endswith(".ts.net") or "." not in host


async def get_config_diagnostics():
    """
    Get comprehensive configuration diagnostics.

    Returns warnings, errors, and status for all configuration components.
    """
    diagnostics = {
        "timestamp": datetime.now(UTC).isoformat(),
        "overall_status": "healthy",
        "issues": [],
        "warnings": [],
        "info": [],
        "components": {},
    }

    # Test OmegaConf configuration loading
    try:
        config_warnings = await asyncio.to_thread(get_config_load_warnings)

        # Check for OmegaConf warnings
        for warning_msg in config_warnings:
            if "some elements are missing" in warning_msg.lower():
                # Extract the variable name from warning
                if "variable '" in warning_msg.lower():
                    var_name = warning_msg.split("'")[1]
                    diagnostics["warnings"].append(
                        {
                            "component": "OmegaConf",
                            "severity": "warning",
                            "message": f"Environment variable '{var_name}' not set (using empty default)",
                            "resolution": f"Set {var_name} in .env file if needed",
                        }
                    )

        diagnostics["components"]["omegaconf"] = {
            "status": "healthy",
            "message": "Configuration loaded successfully",
        }
    except Exception as e:
        diagnostics["overall_status"] = "unhealthy"
        diagnostics["issues"].append(
            {
                "component": "OmegaConf",
                "severity": "error",
                "message": f"Failed to load configuration: {str(e)}",
                "resolution": "Check config/defaults.yml and config/config.yml syntax",
            }
        )
        diagnostics["components"]["omegaconf"] = {
            "status": "unhealthy",
            "message": str(e),
        }

    # Test model registry
    registry = None
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry = get_models_registry()

            # Capture model loading warnings
            for warning in w:
                warning_msg = str(warning.message)
                diagnostics["warnings"].append(
                    {
                        "component": "Model Registry",
                        "severity": "warning",
                        "message": warning_msg,
                        "resolution": "Check model definitions in config/defaults.yml",
                    }
                )

        if registry:
            diagnostics["components"]["model_registry"] = {
                "status": "healthy",
                "message": f"Loaded {len(registry.models)} models",
                "details": {
                    "total_models": len(registry.models),
                    "defaults": dict(registry.defaults) if registry.defaults else {},
                },
            }

            # Check critical models
            stt = registry.get_default("stt")
            stt_stream = registry.get_default("stt_stream")
            llm = registry.get_default("llm")

            # STT check
            if stt:
                if stt.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "STT (Batch)",
                            "message": f"Configured: {stt.name} ({stt.model_provider}) - API key present",
                        }
                    )
                elif _is_self_hosted_model(stt):
                    diagnostics["info"].append(
                        {
                            "component": "STT (Batch)",
                            "message": f"Configured: {stt.name} ({stt.model_provider}) - local service, no API key required",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "STT (Batch)",
                            "severity": "warning",
                            "message": f"{stt.name} ({stt.model_provider}) - No API key configured",
                            "resolution": "Transcription can fail without API key",
                        }
                    )
            else:
                diagnostics["issues"].append(
                    {
                        "component": "STT (Batch)",
                        "severity": "error",
                        "message": "No batch STT model configured",
                        "resolution": "Set defaults.stt in config.yml",
                    }
                )
                diagnostics["overall_status"] = "partial"

            # Streaming STT check
            if stt_stream:
                if stt_stream.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "STT (Streaming)",
                            "message": f"Configured: {stt_stream.name} ({stt_stream.model_provider}) - API key present",
                        }
                    )
                elif _is_self_hosted_model(stt_stream):
                    diagnostics["info"].append(
                        {
                            "component": "STT (Streaming)",
                            "message": f"Configured: {stt_stream.name} ({stt_stream.model_provider}) - local service, no API key required",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "STT (Streaming)",
                            "severity": "warning",
                            "message": f"{stt_stream.name} ({stt_stream.model_provider}) - No API key configured",
                            "resolution": "Real-time transcription can fail without API key",
                        }
                    )
            else:
                diagnostics["warnings"].append(
                    {
                        "component": "STT (Streaming)",
                        "severity": "warning",
                        "message": "No streaming STT model configured - streaming worker disabled",
                        "resolution": "Set defaults.stt_stream in config.yml for WebSocket transcription",
                    }
                )

            # LLM check
            if llm:
                if llm.api_key:
                    diagnostics["info"].append(
                        {
                            "component": "LLM",
                            "message": f"Configured: {llm.name} ({llm.model_provider}) - API key present",
                        }
                    )
                elif _is_self_hosted_model(llm):
                    diagnostics["info"].append(
                        {
                            "component": "LLM",
                            "message": f"Configured: {llm.name} ({llm.model_provider}) - local service, no API key required",
                        }
                    )
                else:
                    diagnostics["warnings"].append(
                        {
                            "component": "LLM",
                            "severity": "warning",
                            "message": f"{llm.name} ({llm.model_provider}) - No API key configured",
                            "resolution": "Memory extraction can fail without API key",
                        }
                    )

        else:
            diagnostics["overall_status"] = "unhealthy"
            diagnostics["issues"].append(
                {
                    "component": "Model Registry",
                    "severity": "error",
                    "message": "Failed to load model registry",
                    "resolution": "Check config/defaults.yml for syntax errors",
                }
            )
            diagnostics["components"]["model_registry"] = {
                "status": "unhealthy",
                "message": "Registry failed to load",
            }
    except Exception as e:
        diagnostics["overall_status"] = "partial"
        diagnostics["issues"].append(
            {
                "component": "Model Registry",
                "severity": "error",
                "message": f"Error loading registry: {str(e)}",
                "resolution": "Check logs for detailed error information",
            }
        )
        diagnostics["components"]["model_registry"] = {
            "status": "unhealthy",
            "message": str(e),
        }

    # Check environment variables (only warn about keys relevant to configured providers)
    env_checks = [
        ("AUTH_SECRET_KEY", "Required for authentication"),
        ("ADMIN_EMAIL", "Required for admin user login"),
        ("ADMIN_PASSWORD", "Required for admin user login"),
    ]

    if registry:
        # Add LLM API key check based on active provider
        llm_model = registry.get_default("llm")
        if llm_model and llm_model.model_provider == "openai":
            env_checks.append(
                ("OPENAI_API_KEY", "Required for OpenAI LLM and embeddings")
            )
        elif llm_model and llm_model.model_provider == "groq":
            env_checks.append(("GROQ_API_KEY", "Required for Groq LLM"))

        # Add transcription API key check based on active STT provider
        stt_model = registry.get_default("stt")
        if stt_model:
            provider = stt_model.model_provider
            if provider == "deepgram":
                env_checks.append(
                    ("DEEPGRAM_API_KEY", "Required for Deepgram transcription")
                )
            elif provider == "smallest":
                env_checks.append(
                    ("SMALLEST_API_KEY", "Required for Smallest.ai Pulse transcription")
                )

    for env_var, description in env_checks:
        value = os.getenv(env_var)
        if not value or value == "":
            diagnostics["warnings"].append(
                {
                    "component": "Environment Variables",
                    "severity": "warning",
                    "message": f"{env_var} not set - {description}",
                    "resolution": f"Set {env_var} in .env file",
                }
            )

    return diagnostics


async def get_current_metrics():
    """Get current system metrics."""
    try:
        # Get memory provider configuration
        memory_provider = (await get_memory_provider())["current_provider"]

        # Get basic system metrics
        metrics = {
            "timestamp": int(time.time()),
            "memory_provider": memory_provider,
            "memory_provider_supports_threshold": memory_provider == "chronicle",
        }

        return metrics

    except Exception as e:
        audio_logger.exception("Error fetching metrics")
        raise e


async def get_auth_config():
    """Get authentication configuration for frontend."""
    return {
        "auth_method": "email",
        "registration_enabled": False,  # Only admin can create users
        "features": {
            "email_login": True,
            "user_id_login": False,  # Deprecated
            "registration": False,
        },
    }


async def get_observability_config():
    """Get observability configuration for frontend (Langfuse deep-links).

    Returns non-secret data only (enabled status and browser URL).
    """
    enabled = is_langfuse_enabled()
    session_base_url = None

    if enabled:
        cfg = load_config()
        public_url = (
            cfg.get("observability", {}).get("langfuse", {}).get("public_url", "")
        )
        if public_url:
            # Strip trailing slash and build session URL
            session_base_url = f"{public_url.rstrip('/')}/project/chronicle/sessions"

    return {
        "langfuse": {
            "enabled": enabled,
            "session_base_url": session_base_url,
        }
    }


# Audio file processing functions moved to audio_controller.py


# Configuration functions moved to config.py to avoid circular imports


async def get_diarization_settings():
    """Get current diarization settings."""
    try:
        # Get settings using OmegaConf
        settings = load_diarization_settings()
        return {"settings": settings, "status": "success"}
    except Exception as e:
        logger.exception("Error getting diarization settings")
        raise e


async def save_diarization_settings_controller(settings: dict):
    """Save diarization settings."""
    try:
        # Validate settings
        valid_keys = {
            "diarization_source",
            "similarity_threshold",
            "min_duration",
            "collar",
            "min_duration_off",
            "min_speakers",
            "max_speakers",
        }

        # Filter to only valid keys (allow round-trip GET→POST)
        filtered_settings = {}
        for key, value in settings.items():
            if key not in valid_keys:
                continue  # Skip unknown keys instead of rejecting

            # Type validation for known keys only
            if key in ["min_speakers", "max_speakers"]:
                if value is not None and (
                    not isinstance(value, int) or value < 1 or value > 20
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Invalid value for {key}: must be null (automatic) "
                            "or an integer 1-20"
                        ),
                    )
            elif key == "diarization_source":
                if not isinstance(value, str) or value not in ["pyannote", "provider"]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be 'pyannote' or 'provider'",
                    )
            else:
                if not isinstance(value, (int, float)) or value < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be positive number",
                    )

            filtered_settings[key] = value

        # Reject if NO valid keys provided (completely invalid request)
        if not filtered_settings:
            raise HTTPException(
                status_code=400, detail="No valid diarization settings provided"
            )

        # Get current settings and merge with new values
        current_settings = load_diarization_settings()
        current_settings.update(filtered_settings)

        # Save using OmegaConf
        if save_diarization_settings(current_settings):
            logger.info(f"Updated and saved diarization settings: {filtered_settings}")

            return {
                "message": "Diarization settings saved successfully",
                "settings": current_settings,
                "status": "success",
            }
        else:
            logger.warning("Settings save failed")
            return {
                "message": "Settings save failed",
                "settings": current_settings,
                "status": "error",
            }

    except Exception as e:
        logger.exception("Error saving diarization settings")
        raise e


# ---------------------------------------------------------------------------
# ASR context / hint-mechanism settings
#
# Each STT provider consumes recognition hints in exactly one way (see
# ModelDef.capabilities): "keyword_boosting" (acoustic hot-word boost, never
# echoed) or "context_prompt" (LLM context that informs but must not be echoed).
# context_prompt providers (e.g. Gemma 4) are NOT given the wake-word boost list;
# instead the user authors a free-form context string, stored per-model under
# backend.asr.context.<model_name> in config.yml.
# ---------------------------------------------------------------------------


def _asr_hint_type(capabilities) -> str:
    caps = set(capabilities or [])
    if "context_prompt" in caps:
        return "context_prompt"
    if "keyword_boosting" in caps:
        return "keyword_boosting"
    return "none"


def _asr_model_info(model) -> Optional[dict]:
    """Summarise an STT model's hint mechanism + resolved context for the UI."""
    if not model:
        return None
    asr_cfg = get_backend_config("asr") or {}
    ctx_map = asr_cfg.get("context", {}) or {}
    override = ctx_map.get(model.name)
    inline = getattr(model, "asr_context", None)
    context = override if override is not None else (inline or "")
    return {
        "name": model.name,
        "provider": model.model_provider,
        "description": model.description,
        "capabilities": list(model.capabilities or []),
        "hint_type": _asr_hint_type(model.capabilities),
        "context": context or "",
    }


async def get_asr_context_config():
    """Return the active batch + streaming STT provider hint mechanisms."""
    registry = get_models_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Model registry unavailable")
    return {
        "batch": _asr_model_info(registry.get_default("stt")),
        "stream": _asr_model_info(registry.get_default("stt_stream")),
        "status": "success",
    }


async def save_asr_context_controller(payload: dict):
    """Persist a context string for a context_prompt STT provider."""
    model_name = (payload.get("model_name") or "").strip()
    context = payload.get("context", "")
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")
    if not isinstance(context, str):
        raise HTTPException(status_code=400, detail="context must be a string")

    registry = get_models_registry()
    model = registry.get_by_name(model_name) if registry else None
    if not model:
        raise HTTPException(status_code=404, detail=f"Unknown model '{model_name}'")
    if "context_prompt" not in set(model.capabilities or []):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_name}' does not use a context prompt; ASR context "
                "only applies to context_prompt providers."
            ),
        )

    if not save_config_section("backend.asr.context", {model_name: context.strip()}):
        raise HTTPException(status_code=500, detail="Failed to save ASR context")

    # Refresh the in-process registry and signal workers so the new context is
    # picked up on the next transcription (same pattern as a provider switch).
    load_models_config(force_reload=True)
    try:
        signal_worker_restart()
    except Exception as e:
        logger.warning(f"Could not signal worker restart after ASR context save: {e}")

    return {
        "status": "success",
        "model_name": model_name,
        "context": context.strip(),
    }


async def get_misc_settings():
    """Get current miscellaneous settings."""
    try:
        # Get settings using OmegaConf
        settings = load_misc_settings()
        return {"settings": settings, "status": "success"}
    except Exception as e:
        logger.exception("Error getting misc settings")
        raise e


async def get_timeline_grouping_settings():
    cfg = load_config().get("timeline", {}).get("consolidation", {})
    values = dict(cfg) if cfg else {}
    return {
        "status": "success",
        "settings": {
            "pregenerate": bool(values.get("pregenerate", True)),
            "prefetch_days": int(values.get("prefetch_days", 5)),
        },
    }


async def save_timeline_grouping_settings(settings: dict):
    if set(settings) - {"pregenerate", "prefetch_days"}:
        raise HTTPException(status_code=400, detail="Unknown Timeline grouping setting")
    pregenerate = settings.get("pregenerate")
    days = settings.get("prefetch_days")
    if not isinstance(pregenerate, bool):
        raise HTTPException(status_code=400, detail="pregenerate must be a boolean")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise HTTPException(
            status_code=400, detail="prefetch_days must be between 1 and 30"
        )
    if not save_config_section(
        "timeline.consolidation", {"pregenerate": pregenerate, "prefetch_days": days}
    ):
        raise HTTPException(
            status_code=500, detail="Could not save Timeline grouping settings"
        )
    return {"status": "success", "settings": settings}


async def save_misc_settings_controller(settings: dict):
    """Save miscellaneous settings."""
    try:
        # Validate settings
        boolean_keys = {
            "per_segment_speaker_id",
            "always_batch_retranscribe",
            "audio_filtering_require_speech",
        }
        integer_keys = {
            "streaming_fallback_timeout_seconds",
            "max_conversation_duration_seconds",
        }
        # Live-transcription mode selector (top-level defaults.live_segmentation).
        # "windowed_batch" = pseudo-streaming via batch preview; "off" disables the
        # live preview; "streaming_stt" uses a real streaming ASR provider.
        enum_keys = {
            "live_segmentation": {"streaming_stt", "windowed_batch", "off"},
        }
        valid_keys = boolean_keys | integer_keys | set(enum_keys)

        # Filter to only valid keys
        filtered_settings = {}
        for key, value in settings.items():
            if key not in valid_keys:
                continue  # Skip unknown keys

            # Type validation
            if key in boolean_keys:
                if not isinstance(value, bool):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be boolean",
                    )
            elif key in enum_keys:
                if value not in enum_keys[key]:
                    allowed = ", ".join(sorted(enum_keys[key]))
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be one of {allowed}",
                    )
            elif key == "streaming_fallback_timeout_seconds":
                if not isinstance(value, int) or value < 60 or value > 7200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be integer between 60 and 7200",
                    )
            elif key == "max_conversation_duration_seconds":
                if not isinstance(value, int) or value < 600 or value > 86400:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid value for {key}: must be integer between 600 and 86400",
                    )

            filtered_settings[key] = value

        # Reject if NO valid keys provided
        if not filtered_settings:
            raise HTTPException(
                status_code=400, detail="No valid misc settings provided"
            )

        # Save using OmegaConf
        if save_misc_settings(filtered_settings):
            # The batch-transcription speech gate and the windowed-batch
            # consumer read this toggle in the workers container, which only
            # reloads config on restart.
            requires_worker_restart = (
                "audio_filtering_require_speech" in filtered_settings
            )
            if requires_worker_restart:
                signal_worker_restart()

            # Get updated settings
            updated_settings = load_misc_settings()
            logger.info(f"Updated and saved misc settings: {filtered_settings}")

            return {
                "message": "Miscellaneous settings saved successfully",
                "settings": updated_settings,
                "status": "success",
                "requires_worker_restart": requires_worker_restart,
            }
        else:
            logger.warning("Settings save failed")
            return {
                "message": "Settings save failed",
                "settings": load_misc_settings(),
                "status": "error",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error saving misc settings")
        raise e


async def get_cleanup_settings_controller(user: User) -> dict:
    """
    Get current cleanup settings (admin only).

    Args:
        user: Authenticated admin user

    Returns:
        Dict with cleanup settings
    """
    return get_cleanup_settings()


async def save_cleanup_settings_controller(
    auto_cleanup_enabled: bool, retention_days: int, user: User
) -> dict:
    """
    Save cleanup settings (admin only).

    Args:
        auto_cleanup_enabled: Enable/disable automatic cleanup
        retention_days: Number of days to retain soft-deleted conversations
        user: Authenticated admin user

    Returns:
        Updated cleanup settings

    Raises:
        ValueError: If validation fails
    """
    # Validation
    if not isinstance(auto_cleanup_enabled, bool):
        raise ValueError("auto_cleanup_enabled must be a boolean")

    if not isinstance(retention_days, int):
        raise ValueError("retention_days must be an integer")

    if retention_days < 1 or retention_days > 365:
        raise ValueError("retention_days must be between 1 and 365")

    # Create settings object
    settings = CleanupSettings(
        auto_cleanup_enabled=auto_cleanup_enabled, retention_days=retention_days
    )

    # Save using OmegaConf
    save_cleanup_settings(settings)

    logger.info(
        f"Admin {user.email} updated cleanup settings: auto_cleanup={auto_cleanup_enabled}, retention={retention_days}d"
    )

    return {
        "auto_cleanup_enabled": settings.auto_cleanup_enabled,
        "retention_days": settings.retention_days,
        "message": "Cleanup settings saved successfully",
    }


async def get_speaker_configuration(user: User):
    """Get current user's primary speakers configuration."""
    try:
        return {
            "primary_speakers": user.primary_speakers,
            "user_id": user.user_id,
            "status": "success",
        }
    except Exception as e:
        logger.exception(f"Error getting speaker configuration for user {user.user_id}")
        raise e


async def update_speaker_configuration(user: User, primary_speakers: list[dict]):
    """Update current user's primary speakers configuration."""
    try:
        # Validate speaker data format
        for speaker in primary_speakers:
            if not isinstance(speaker, dict):
                raise ValueError("Each speaker must be a dictionary")

            required_fields = ["speaker_id", "name", "user_id"]
            for field in required_fields:
                if field not in speaker:
                    raise ValueError(f"Missing required field: {field}")

        # Enforce server-side user_id and add timestamp to each speaker
        for speaker in primary_speakers:
            speaker["user_id"] = user.user_id  # Override client-supplied user_id
            speaker["selected_at"] = datetime.now(UTC).isoformat()

        # Update user model
        user.primary_speakers = primary_speakers
        await user.save()

        logger.info(
            f"Updated primary speakers configuration for user {user.user_id}: {len(primary_speakers)} speakers"
        )

        return {
            "message": "Primary speakers configuration updated successfully",
            "primary_speakers": primary_speakers,
            "count": len(primary_speakers),
            "status": "success",
        }

    except Exception as e:
        logger.exception(
            f"Error updating speaker configuration for user {user.user_id}"
        )
        raise e


async def get_wakeword_speaker_gate(user: User):
    """Get current user's wake-word speaker gate configuration."""
    try:
        return {
            "enabled": user.wakeword_gate_enabled,
            "speakers": user.wakeword_allowed_speakers,
            "user_id": user.user_id,
            "status": "success",
        }
    except Exception:
        logger.exception(
            f"Error getting wake-word speaker gate for user {user.user_id}"
        )
        raise


async def update_wakeword_speaker_gate(user: User, enabled: bool, speakers: list[dict]):
    """Update current user's wake-word speaker gate configuration.

    When ``enabled`` and at least one speaker is selected, an acoustic wake word
    only dispatches a command if one of these speakers is recognized in the
    captured turn (see the wake-word dispatcher's speaker gate).
    """
    try:
        # Keep only the fields we rely on for matching, mirroring primary_speakers.
        for speaker in speakers:
            if not isinstance(speaker, dict):
                raise ValueError("Each speaker must be a dictionary")
            if "speaker_id" not in speaker or "name" not in speaker:
                raise ValueError("Each speaker needs 'speaker_id' and 'name'")

        cleaned = [{"speaker_id": s["speaker_id"], "name": s["name"]} for s in speakers]
        user.wakeword_gate_enabled = bool(enabled)
        user.wakeword_allowed_speakers = cleaned
        await user.save()

        logger.info(
            f"Updated wake-word speaker gate for user {user.user_id}: "
            f"enabled={enabled}, speakers={len(cleaned)}"
        )

        return {
            "message": "Wake-word speaker gate updated successfully",
            "enabled": user.wakeword_gate_enabled,
            "speakers": cleaned,
            "count": len(cleaned),
            "status": "success",
        }
    except Exception:
        logger.exception(
            f"Error updating wake-word speaker gate for user {user.user_id}"
        )
        raise


async def get_enrolled_speakers(user: User):
    """Get enrolled speakers from speaker recognition service."""
    try:
        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()

        if not speaker_client.enabled:
            return {
                "speakers": [],
                "service_available": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "success",
            }

        speakers = await speaker_client.get_enrolled_speakers(user_id=str(user.user_id))

        return {
            "speakers": speakers.get("speakers", []) if speakers else [],
            "service_available": True,
            "message": "Successfully retrieved enrolled speakers",
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error getting enrolled speakers for user {user.user_id}")
        raise e


async def get_speaker_service_status():
    """Check speaker recognition service health status."""
    try:
        # Initialize speaker recognition client
        speaker_client = SpeakerRecognitionClient()

        if not speaker_client.enabled:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not configured or disabled",
                "status": "disabled",
            }

        # Perform health check
        health_result = await speaker_client.health_check()

        if health_result:
            return {
                "service_available": True,
                "healthy": True,
                "message": "Speaker recognition service is healthy",
                "service_url": speaker_client.service_url,
                "status": "healthy",
            }
        else:
            return {
                "service_available": False,
                "healthy": False,
                "message": "Speaker recognition service is not responding",
                "service_url": speaker_client.service_url,
                "status": "unhealthy",
            }

    except Exception as e:
        logger.exception("Error checking speaker service status")
        raise e


# Memory Configuration Management Functions

_MEMORY_WRITE_BACKENDS = {"direct", "codex", "pi"}
_MEMORY_SEARCH_BACKENDS = {"direct", "pi"}
_OBSOLETE_MEMORY_ROOT_KEYS = {"agent_executor", "codex", "pi"}
_CODEX_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
_CODEX_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
_PI_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_PI_COMPAT_BOOLEAN_FIELDS = {
    "supportsDeveloperRole",
    "supportsReasoningEffort",
    "supportsStore",
    "supportsStrictMode",
    "supportsUsageInStreaming",
}
_PI_COMPAT_FIELDS = _PI_COMPAT_BOOLEAN_FIELDS | {
    "maxTokensField",
    "thinkingFormat",
}
_PI_THINKING_FORMATS = {
    "ant-ling",
    "chat-template",
    "deepseek",
    "openai",
    "openrouter",
    "qwen",
    "qwen-chat-template",
    "string-thinking",
    "together",
    "zai",
}


def _positive_memory_int(value, *, field: str, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _validate_memory_mapping(memory_section: dict) -> None:
    """Validate selectors plus the model-facing Pi contract before saving YAML."""
    obsolete_keys = sorted(set(memory_section) & _OBSOLETE_MEMORY_ROOT_KEYS)
    if obsolete_keys:
        raise ValueError(
            "Obsolete root memory key(s): "
            + ", ".join(obsolete_keys)
            + ". Run the setup wizard or configure memory.agents and "
            "memory.backends."
        )

    provider = str(memory_section.get("provider") or "chronicle").lower()
    if provider != "chronicle":
        raise ValueError(f"Unsupported memory provider: {provider}")

    agents = memory_section.get("agents")
    backends = memory_section.get("backends")
    if agents is None:
        agents = {}
    if backends is None:
        backends = {}
    if not isinstance(agents, dict):
        raise ValueError("memory.agents must be a mapping")
    if not isinstance(backends, dict):
        raise ValueError("memory.backends must be a mapping")
    write = agents.get("write")
    search = agents.get("search")
    if write is None:
        write = {}
    if search is None:
        search = {}
    if not isinstance(write, dict) or not isinstance(search, dict):
        raise ValueError("memory.agents.write/search must be mappings")

    write_backend = str(write.get("backend") or "direct").lower()
    raw_recovery = write.get("recovery_backend", "direct")
    recovery_backend = (
        str(raw_recovery).lower() if raw_recovery not in (None, "") else None
    )
    search_backend = str(search.get("backend") or "direct").lower()
    if write_backend not in _MEMORY_WRITE_BACKENDS:
        raise ValueError(f"Unsupported memory write backend: {write_backend}")
    if recovery_backend is not None and recovery_backend not in _MEMORY_WRITE_BACKENDS:
        raise ValueError(
            f"Unsupported memory write recovery backend: {recovery_backend}"
        )
    if search_backend not in _MEMORY_SEARCH_BACKENDS:
        raise ValueError(f"Unsupported memory search backend: {search_backend}")
    if write.get("max_consecutive_identical_tool_calls") not in (None, ""):
        _positive_memory_int(
            write.get("max_consecutive_identical_tool_calls"),
            field="memory.agents.write.max_consecutive_identical_tool_calls",
            default=2,
        )

    if "codex" in {write_backend, recovery_backend}:
        codex = backends.get("codex")
        if codex is None:
            codex = {}
        if not isinstance(codex, dict):
            raise ValueError("memory.backends.codex must be a mapping")
        _positive_memory_int(
            codex.get("timeout_seconds"),
            field="memory.backends.codex.timeout_seconds",
            default=900,
        )
        sandbox = codex.get("sandbox_mode")
        if sandbox in (None, ""):
            sandbox = "workspace-write"
        if not isinstance(sandbox, str) or sandbox not in _CODEX_SANDBOX_MODES:
            raise ValueError(
                "memory.backends.codex.sandbox_mode must be one of "
                + ", ".join(sorted(_CODEX_SANDBOX_MODES))
            )
        model = codex.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("memory.backends.codex.model must be a string")
        reasoning = codex.get("reasoning_effort")
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise ValueError(
                    "memory.backends.codex.reasoning_effort must be a string"
                )
            reasoning = reasoning.strip().lower()
            if reasoning and reasoning not in _CODEX_REASONING_EFFORTS:
                raise ValueError(
                    "memory.backends.codex.reasoning_effort must be one of "
                    + ", ".join(sorted(_CODEX_REASONING_EFFORTS))
                )
        threshold = codex.get("max_used_percent")
        if threshold not in (None, ""):
            if isinstance(threshold, bool) or (
                isinstance(threshold, float) and not threshold.is_integer()
            ):
                raise ValueError(
                    "memory.backends.codex.max_used_percent must be an integer "
                    "between 0 and 100"
                )
            try:
                threshold = int(threshold)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "memory.backends.codex.max_used_percent must be an integer "
                    "between 0 and 100"
                ) from exc
            if not 0 <= threshold <= 100:
                raise ValueError(
                    "memory.backends.codex.max_used_percent must be an integer "
                    "between 0 and 100"
                )
        limit_id = codex.get("limit_id")
        if limit_id is not None and not isinstance(limit_id, str):
            raise ValueError("memory.backends.codex.limit_id must be a string")

    if "pi" not in {write_backend, recovery_backend, search_backend}:
        return
    pi = backends.get("pi") or {}
    if not isinstance(pi, dict):
        raise ValueError("memory.backends.pi must be a mapping")
    if pi.get("model") not in (None, ""):
        raise ValueError(
            "memory.backends.pi.model is obsolete; select models with defaults or "
            "llm_operations"
        )
    registry = get_models_registry()
    if registry is None:
        raise ValueError("Chronicle model registry is unavailable")
    operation_names = set()
    if "pi" in {write_backend, recovery_backend}:
        operation_names.add("memory_write")
    if search_backend == "pi":
        operation_names.add("memory_search")
    context_defaults = []
    for operation_name in sorted(operation_names):
        operation = registry.get_llm_operation(operation_name)
        model = operation.model_def
        model_name = model.name
        if model.model_type != "llm" or str(model.api_family).lower() != "openai":
            raise ValueError(
                f"{operation_name} resolves to {model_name!r}, which must be an "
                "OpenAI-compatible LLM for Pi"
            )
        if not model.resolved_url():
            raise ValueError(
                f"{operation_name} resolves to {model_name!r}, which has no "
                "resolvable URL"
            )
        model_params = model.model_params or {}
        context_default = getattr(model, "context_window", None)
        if context_default in (None, ""):
            context_default = model_params.get("context_window")
        context_defaults.append(
            _positive_memory_int(
                context_default,
                field=f"models.{model_name}.context_window",
                default=32768,
            )
        )
    context_default = min(context_defaults) if context_defaults else 32768
    context_window = _positive_memory_int(
        pi.get("context_window"),
        field="memory.backends.pi.context_window",
        default=context_default,
    )
    max_tokens = _positive_memory_int(
        pi.get("max_tokens"),
        field="memory.backends.pi.max_tokens",
        default=4096,
    )
    if max_tokens > context_window - 1024:
        raise ValueError(
            "memory.backends.pi.max_tokens must leave at least 1024 tokens of context"
        )
    raw_thinking = pi.get("thinking", "off")
    if isinstance(raw_thinking, bool):
        thinking = "low" if raw_thinking else "off"
    else:
        thinking = str(raw_thinking or "off").strip().lower()
    if thinking not in _PI_THINKING_LEVELS:
        raise ValueError(
            "memory.backends.pi.thinking must be one of "
            + ", ".join(sorted(_PI_THINKING_LEVELS))
        )

    _positive_memory_int(
        pi.get("timeout_seconds"),
        field="memory.backends.pi.timeout_seconds",
        default=900,
    )
    compat = pi.get("compat")
    if compat is not None:
        if not isinstance(compat, dict):
            raise ValueError("memory.backends.pi.compat must be a mapping")
        unknown = sorted(set(compat) - _PI_COMPAT_FIELDS)
        if unknown:
            raise ValueError(
                "memory.backends.pi.compat contains unsupported field(s): "
                + ", ".join(unknown)
            )
        for name in _PI_COMPAT_BOOLEAN_FIELDS:
            if name in compat and not isinstance(compat[name], bool):
                raise ValueError(f"memory.backends.pi.compat.{name} must be a boolean")
        if compat.get("maxTokensField") not in (
            None,
            "max_tokens",
            "max_completion_tokens",
        ):
            raise ValueError(
                "memory.backends.pi.compat.maxTokensField must be max_tokens or "
                "max_completion_tokens"
            )
        thinking_format = compat.get("thinkingFormat")
        if thinking_format is not None and thinking_format not in _PI_THINKING_FORMATS:
            raise ValueError(
                "memory.backends.pi.compat.thinkingFormat must be one of "
                + ", ".join(sorted(_PI_THINKING_FORMATS))
            )


async def get_memory_config_raw():
    """Get current memory configuration (memory section of config.yml) as YAML."""
    try:
        cfg_path = _find_config_path()
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"Config file not found: {cfg_path}")

        with open(cfg_path, "r") as f:
            data = _yaml.load(f) or {}
        memory_section = data.get("memory", {})
        stream = StringIO()
        _yaml.dump(dict(memory_section) if memory_section else {}, stream)
        config_yaml = stream.getvalue()

        return {
            "config_yaml": config_yaml,
            "config_path": str(cfg_path),
            "section": "memory",
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error reading memory config")
        raise e


async def update_memory_config_raw(config_yaml: str):
    """Update memory configuration and restart processes that cache it."""
    try:
        # Validate YAML
        try:
            new_mem = _yaml.load(config_yaml) or {}
        except Exception as e:
            raise ValueError(f"Invalid YAML syntax: {str(e)}")
        if not isinstance(new_mem, dict):
            raise HTTPException(
                status_code=400, detail="Configuration must be a YAML object"
            )
        try:
            _validate_memory_mapping(new_mem)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cfg_path = _find_config_path()
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"Config file not found: {cfg_path}")

        # Backup
        backup_path = f"{cfg_path}.bak"
        shutil.copy2(cfg_path, backup_path)

        # Update memory section and write file
        with open(cfg_path, "r") as f:
            data = _yaml.load(f) or {}
        data["memory"] = new_mem
        with open(cfg_path, "w") as f:
            _yaml.dump(data, f)

        # The API and RQ workers each cache both the registry and memory-service
        # singleton. Reset this process immediately and ask the worker orchestrator
        # to restart its processes so the saved backend selection actually applies.
        load_models_config(force_reload=True)
        reset_memory_service()
        reset_chat_service()
        signal_worker_restart()

        return {
            "message": "Memory configuration updated; worker restart requested",
            "config_path": str(cfg_path),
            "backup_created": os.path.exists(backup_path),
            "requires_worker_restart": True,
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error updating memory config")
        raise e


async def validate_memory_config(config_yaml: str):
    """Validate memory configuration YAML syntax (memory section)."""
    try:
        try:
            parsed = _yaml.load(config_yaml)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid YAML syntax: {str(e)}"
            )
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=400, detail="Configuration must be a YAML object"
            )
        try:
            _validate_memory_mapping(parsed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"message": "Configuration is valid", "status": "success"}
    except HTTPException:
        # Re-raise HTTPExceptions without wrapping
        raise
    except Exception as e:
        logger.exception("Error validating memory config")
        raise HTTPException(
            status_code=500, detail=f"Error validating memory config: {str(e)}"
        )


async def reload_memory_config():
    """Reload config.yml and rebuild memory services in API and worker processes."""
    try:
        cfg_path = _find_config_path()
        load_models_config(force_reload=True)
        reset_memory_service()
        reset_chat_service()
        signal_worker_restart()
        return {
            "message": "Configuration reloaded; worker restart requested",
            "config_path": str(cfg_path),
            "requires_worker_restart": True,
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error reloading config")
        raise e


async def delete_all_user_memories(user: User):
    """Delete all memories for the current user."""
    try:
        memory_service = get_memory_service()

        # Delete all memories for the user
        deleted_count = await memory_service.delete_all_user_memories(user.user_id)

        logger.info(f"Deleted {deleted_count} memories for user {user.user_id}")

        return {
            "message": f"Successfully deleted {deleted_count} memories",
            "deleted_count": deleted_count,
            "user_id": user.user_id,
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error deleting all memories for user {user.user_id}")
        raise e


# Memory Provider Configuration Functions


async def get_memory_provider():
    """Get current memory provider configuration."""
    try:
        current_provider = os.getenv("MEMORY_PROVIDER", "chronicle").lower()

        # Chronicle (agentic vault) is currently the only provider.
        available_providers = ["chronicle"]

        return {
            "current_provider": current_provider,
            "available_providers": available_providers,
            "status": "success",
        }

    except Exception as e:
        logger.exception("Error getting memory provider")
        raise e


async def set_memory_provider(provider: str):
    """Set memory provider and update .env file."""
    try:
        # Validate provider. Chronicle (agentic vault) is currently the only provider.
        provider = provider.lower().strip()
        valid_providers = ["chronicle"]

        if provider not in valid_providers:
            raise ValueError(
                f"Invalid provider '{provider}'. Valid providers: {', '.join(valid_providers)}"
            )

        # Path to .env file (assuming we're running from backend/)
        env_path = os.path.join(os.getcwd(), ".env")

        if not os.path.exists(env_path):
            raise FileNotFoundError(f".env file not found at {env_path}")

        # Create backup
        backup_path = f"{env_path}.bak"
        shutil.copy2(env_path, backup_path)
        logger.info(f"Created .env backup at {backup_path}")

        # Update key using python-dotenv (handles add-or-update automatically)
        dotenv_set_key(env_path, "MEMORY_PROVIDER", provider, quote_mode="never")

        # Update environment variable for current process
        os.environ["MEMORY_PROVIDER"] = provider

        logger.info(f"Updated MEMORY_PROVIDER to '{provider}' in .env file")

        return {
            "message": f"Memory provider updated to '{provider}'. Please restart the backend service for changes to take effect.",
            "provider": provider,
            "env_path": env_path,
            "backup_created": True,
            "requires_restart": True,
            "status": "success",
        }

    except Exception as e:
        logger.exception("Error setting memory provider")
        raise e


# LLM Operations Configuration Functions


async def get_llm_operations():
    """Get LLM operation configurations and available models."""
    try:
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        # Serialize each LLMOperationConfig to dict
        operations = {}
        operation_routes = effective_operation_routes(registry)
        effective_routing = {
            route["operation"]: {
                key: value
                for key, value in route.items()
                if key not in {"workload", "adapter", "operation"}
            }
            for route in operation_routes
        }
        for op_name, op_config in registry.llm_operations.items():
            operations[op_name] = {
                "model": op_config.model,
                "temperature": op_config.temperature,
                "max_tokens": op_config.max_tokens,
                "response_format": op_config.response_format,
                "reasoning_effort": op_config.reasoning_effort,
            }

        # Collect available LLM models
        available_models = [
            {"name": m.name, "description": m.description, "provider": m.model_provider}
            for m in registry.get_all_by_type("llm")
        ]

        default_llm = registry.defaults.get("llm")

        return {
            "operations": operations,
            "available_models": available_models,
            "default_llm": default_llm,
            "effective_routing": effective_routing,
            "operation_route_audit": {
                "total": len(operation_routes),
                "self_hosted": sum(
                    route["location"] == "self-hosted" for route in operation_routes
                ),
                "external": sum(
                    route["location"] == "external" for route in operation_routes
                ),
                "unknown": sum(
                    route["location"] == "unknown" for route in operation_routes
                ),
            },
            "runtime_routes": effective_model_routes(load_config(), registry),
            "status": "success",
        }
    except Exception as e:
        logger.exception("Error getting LLM operations")
        raise e


async def save_llm_operations(operations: dict):
    """Save LLM operation configurations to config.yml and hot-reload."""
    try:
        if "memory_agent" in operations:
            raise HTTPException(
                status_code=400,
                detail=(
                    "llm_operations.memory_agent is obsolete; configure "
                    "llm_operations.memory_write and llm_operations.memory_search"
                ),
            )

        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        valid_keys = {
            "model",
            "temperature",
            "max_tokens",
            "response_format",
            "reasoning_effort",
        }

        for op_name, op_value in operations.items():
            if not isinstance(op_value, dict):
                raise HTTPException(
                    status_code=400, detail=f"Operation '{op_name}' must be a dict"
                )

            extra_keys = set(op_value.keys()) - valid_keys
            if extra_keys:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid keys for '{op_name}': {extra_keys}",
                )

            if "temperature" in op_value and op_value["temperature"] is not None:
                t = op_value["temperature"]
                if not isinstance(t, (int, float)) or t < 0 or t > 2:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid temperature for '{op_name}': must be 0-2",
                    )

            if "max_tokens" in op_value and op_value["max_tokens"] is not None:
                mt = op_value["max_tokens"]
                if not isinstance(mt, int) or mt <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid max_tokens for '{op_name}': must be positive int",
                    )

            if "model" in op_value and op_value["model"] is not None:
                if not registry.get_by_name(op_value["model"]):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Model '{op_value['model']}' not found in registry",
                    )

            if (
                "reasoning_effort" in op_value
                and op_value["reasoning_effort"] is not None
                and not isinstance(op_value["reasoning_effort"], str)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"reasoning_effort for '{op_name}' must be a string or null",
                )

            if (
                "response_format" in op_value
                and op_value["response_format"] is not None
            ):
                if op_value["response_format"] != "json":
                    raise HTTPException(
                        status_code=400,
                        detail=f"response_format must be 'json' or null",
                    )

        if save_config_section("llm_operations", operations):
            load_models_config(force_reload=True)
            memory_operations_changed = bool(
                {"memory_write", "memory_search"}.intersection(operations)
            )
            if memory_operations_changed:
                # Both API and RQ worker processes cache resolved operations and
                # memory/chat singletons. Apply the same reset contract as a raw
                # memory configuration update so a saved model/budget takes effect.
                reset_memory_service()
                reset_chat_service()
                signal_worker_restart()
            logger.info(f"Updated LLM operations config: {list(operations.keys())}")
            return {
                "message": "LLM operations saved successfully",
                "requires_worker_restart": memory_operations_changed,
                "status": "success",
            }
        else:
            return {
                "message": "Failed to save LLM operations",
                "status": "error",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error saving LLM operations")
        raise e


async def test_llm_model(model_name: Optional[str]):
    """Test an LLM model connection with a trivial prompt."""
    try:
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not loaded")

        if model_name:
            model_def = registry.get_by_name(model_name)
            if not model_def:
                return {
                    "success": False,
                    "model_name": model_name,
                    "error": f"Model '{model_name}' not found",
                    "status": "error",
                }
        else:
            model_def = registry.get_default("llm")
            if not model_def:
                return {
                    "success": False,
                    "model_name": None,
                    "error": "No default LLM configured",
                    "status": "error",
                }

        operation = registry.get_llm_operation(
            "model_test", model_override=model_def.name
        ).model_copy(update={"temperature": 0.0, "max_tokens": 64})
        client = operation.get_client(is_async=True)
        start = time.time()
        response = await client.chat.completions.create(
            **operation.to_api_params(),
            messages=operation.prepare_messages(
                [{"role": "user", "content": "Say hello in one word."}]
            ),
        )
        latency_ms = int((time.time() - start) * 1000)

        return {
            "success": True,
            "model_name": model_def.name,
            "model_provider": model_def.model_provider,
            "response": response.choices[0].message.content.strip(),
            "latency_ms": latency_ms,
            "status": "success",
        }
    except Exception as e:
        return {
            "success": False,
            "model_name": model_name or "(default)",
            "error": str(e),
            "status": "error",
        }


# Model Registry Management Functions

# Default-pointer keys exposed for editing → the model_type a chosen model must
# have. ``llm`` and ``fast_llm`` both point at LLMs. ``live_segmentation`` is a
# mode string (owned by misc-settings), not a model, so it's intentionally absent.
_DEFAULT_KEY_TO_MODEL_TYPE = {
    "llm": "llm",
    "fast_llm": "llm",
    "fallback_llm": "llm",
    "embedding": "embedding",
    "stt": "stt",
    "stt_stream": "stt_stream",
    "tts": "tts",
}

# Model types editable from the registry UI (must be routable by the pipeline).
_EDITABLE_MODEL_TYPES = ["llm", "embedding", "stt", "stt_stream", "tts"]

# Sentinel sent to the browser instead of an inline secret, and recognised on the
# way back in as "keep the stored value".
_API_KEY_MASK = "••••••••"

# Inline api_key values that aren't real secrets (placeholders) — shown verbatim.
_NON_SECRET_API_KEYS = {"", "no-key", "dummy", "none", "null"}


def _is_env_ref(value) -> bool:
    """True if the value is an OmegaConf interpolation like ``${oc.env:VAR}``."""
    return isinstance(value, str) and value.strip().startswith("${")


def _mask_api_key(raw_value):
    """Mask an inline secret for display; pass through refs/placeholders/None."""
    if raw_value is None:
        return ""
    if _is_env_ref(raw_value):
        return raw_value
    if isinstance(raw_value, str) and raw_value.strip().lower() in _NON_SECRET_API_KEYS:
        return raw_value
    return _API_KEY_MASK


def _model_view(model_def: ModelDef, raw_by_name: dict, default_names: set) -> dict:
    """Build the UI-facing model dict, zipping registry (resolved/derived) with
    raw config.yml (source + unmasked secret reference detection)."""
    raw = raw_by_name.get(model_def.name)
    raw_api_key = raw.get("api_key") if raw else model_def.api_key
    return {
        "name": model_def.name,
        "model_type": model_def.model_type,
        "model_provider": model_def.model_provider,
        "model_name": model_def.model_name,
        "model_url": model_def.model_url,
        "deployment": model_def.deployment,
        "deployment_endpoint": model_def.deployment_endpoint,
        "api_family": model_def.api_family,
        "api_key": _mask_api_key(raw_api_key),
        "api_key_is_set": bool(raw_api_key)
        and not (
            isinstance(raw_api_key, str)
            and raw_api_key.strip().lower() in _NON_SECRET_API_KEYS
        ),
        "api_key_is_ref": _is_env_ref(raw_api_key),
        "description": model_def.description,
        "model_params": dict(model_def.model_params or {}),
        "capabilities": list(model_def.capabilities or []),
        "embedding_dimensions": model_def.embedding_dimensions,
        "model_output": model_def.model_output,
        "thinking": model_def.thinking,
        # 'config' = defined in config.yml (editable/deletable);
        # 'default' = built-in template from defaults.yml (read-only baseline).
        "source": "config" if model_def.name in raw_by_name else "default",
        "is_default": model_def.name in default_names,
    }


async def get_models():
    """Return all registry models grouped by type plus the active defaults.

    Inline API keys are masked; ``${oc.env:...}`` references are shown verbatim so
    the operator can see which env var backs a model without leaking the secret.
    """
    registry = get_models_registry()
    if not registry:
        raise RuntimeError("Model registry not loaded")

    raw_by_name = {
        m.get("name"): m
        for m in get_raw_models()
        if isinstance(m, dict) and m.get("name")
    }
    default_names = set(registry.defaults.values())

    grouped = {t: [] for t in _EDITABLE_MODEL_TYPES}
    for model_def in registry.models.values():
        if model_def.model_type in grouped:
            grouped[model_def.model_type].append(
                _model_view(model_def, raw_by_name, default_names)
            )
    for t in grouped:
        grouped[t].sort(key=lambda v: v["name"])

    defaults = {
        key: registry.defaults.get(key)
        for key in (
            "llm",
            "fast_llm",
            "fallback_llm",
            "embedding",
            "stt",
            "stt_stream",
            "tts",
            "live_segmentation",
        )
    }

    return {"defaults": defaults, "models": grouped, "status": "success"}


async def set_active_defaults(body: dict):
    """Repoint one or more active-model defaults (llm/fast_llm/embedding/stt/
    stt_stream/tts). Validates the target exists and its model_type matches the
    key, then hot-reloads the registry and signals workers."""
    registry = get_models_registry()
    if not registry:
        raise RuntimeError("Model registry not loaded")

    updates: dict = {}
    for key, model_name in (body or {}).items():
        if key not in _DEFAULT_KEY_TO_MODEL_TYPE:
            raise HTTPException(status_code=400, detail=f"Unknown default key '{key}'")
        if not model_name:
            continue
        model_def = registry.get_by_name(model_name)
        if not model_def:
            raise HTTPException(
                status_code=400, detail=f"Model '{model_name}' not found in registry"
            )
        expected = _DEFAULT_KEY_TO_MODEL_TYPE[key]
        if model_def.model_type != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model '{model_name}' is a {model_def.model_type} model; "
                    f"default '{key}' requires a {expected} model"
                ),
            )
        updates[key] = model_name

    if not updates:
        raise HTTPException(status_code=400, detail="No valid defaults to update")

    if not save_config_section("defaults", updates):
        return {"status": "error", "message": "Failed to save defaults"}

    load_models_config(force_reload=True)
    signal_worker_restart()
    logger.info("Updated active defaults: %s", updates)
    return {
        "status": "success",
        "defaults": updates,
        "requires_worker_restart": True,
    }


async def upsert_model(body: dict):
    """Add or update a single model definition in config.yml.

    Validates the def via the ModelDef schema. An incoming api_key equal to the
    mask sentinel preserves the stored secret. Editing a default-only (defaults.yml)
    model creates a config.yml override. Hot-reloads the registry afterwards.
    """
    if not isinstance(body, dict) or not body.get("name"):
        raise HTTPException(status_code=400, detail="Model 'name' is required")

    model_type = body.get("model_type")
    if model_type not in _EDITABLE_MODEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"model_type must be one of {_EDITABLE_MODEL_TYPES}",
        )

    raw_models = get_raw_models()
    existing = next((m for m in raw_models if m.get("name") == body["name"]), None)

    # Preserve the stored secret when the form sends back the mask sentinel.
    incoming_key = body.get("api_key")
    if incoming_key == _API_KEY_MASK:
        body["api_key"] = existing.get("api_key") if existing else None

    # Validate shape via the single source of truth (raises on bad def).
    try:
        ModelDef(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid model definition: {e}")

    # Drop None/empty optional keys so we don't litter config.yml with nulls.
    clean = {k: v for k, v in body.items() if v is not None}

    new_models = []
    replaced = False
    for m in raw_models:
        if m.get("name") == body["name"]:
            new_models.append(clean)
            replaced = True
        else:
            new_models.append(m)
    if not replaced:
        new_models.append(clean)

    if not save_models_list(new_models):
        return {"status": "error", "message": "Failed to save model"}

    load_models_config(force_reload=True)
    signal_worker_restart()
    logger.info("%s model '%s'", "Updated" if replaced else "Added", body["name"])

    registry = get_models_registry()
    raw_by_name = {
        m.get("name"): m
        for m in get_raw_models()
        if isinstance(m, dict) and m.get("name")
    }
    model_def = registry.get_by_name(body["name"]) if registry else None
    view = (
        _model_view(model_def, raw_by_name, set(registry.defaults.values()))
        if model_def
        else None
    )
    return {"status": "success", "model": view, "requires_worker_restart": True}


async def delete_model(name: str):
    """Delete a config.yml model. Refuses if it's an active default or a built-in
    (defaults.yml-only) template."""
    registry = get_models_registry()
    if not registry:
        raise RuntimeError("Model registry not loaded")

    for key, default_name in registry.defaults.items():
        if default_name == name:
            raise HTTPException(
                status_code=409,
                detail=f"Model '{name}' is the active '{key}'; repoint that default first",
            )

    raw_models = get_raw_models()
    if not any(m.get("name") == name for m in raw_models):
        raise HTTPException(
            status_code=409,
            detail=f"Model '{name}' is a built-in template (defaults.yml) and cannot be deleted",
        )

    new_models = [m for m in raw_models if m.get("name") != name]
    if not save_models_list(new_models):
        return {"status": "error", "message": "Failed to delete model"}

    load_models_config(force_reload=True)
    signal_worker_restart()
    logger.info("Deleted model '%s'", name)
    return {"status": "success", "deleted": name}


async def test_model(model_name: Optional[str]):
    """Connectivity test for a registry model. LLMs do a trivial chat round-trip;
    embedding models do a 1-token embeddings call; STT/TTS have no automated test."""
    registry = get_models_registry()
    if not registry:
        raise RuntimeError("Model registry not loaded")

    if not model_name:
        return await test_llm_model(None)

    model_def = registry.get_by_name(model_name)
    if not model_def:
        return {
            "success": False,
            "model_name": model_name,
            "error": f"Model '{model_name}' not found",
            "status": "error",
        }

    if model_def.model_type == "llm":
        return await test_llm_model(model_name)

    if model_def.model_type == "embedding":
        try:
            client = create_openai_client(
                api_key=model_def.api_key or "",
                base_url=model_def.resolved_url(),
                is_async=True,
            )
            start = time.time()
            await client.embeddings.create(model=model_def.model_name, input="ping")
            latency_ms = int((time.time() - start) * 1000)
            return {
                "success": True,
                "model_name": model_def.name,
                "model_provider": model_def.model_provider,
                "latency_ms": latency_ms,
                "status": "success",
            }
        except Exception as e:
            return {
                "success": False,
                "model_name": model_name,
                "error": str(e),
                "status": "error",
            }

    return {
        "success": False,
        "model_name": model_name,
        "error": f"No automated test for {model_def.model_type} models",
        "status": "unsupported",
    }


# Plugin Configuration Management Functions


async def get_plugins_config_yaml() -> str:
    """Get plugins configuration as YAML text."""
    try:
        plugins_yml_path = get_plugins_yml_path()

        # Default empty plugins config
        default_config = """plugins:
  # No plugins configured yet
  # Example plugin configuration:
  # homeassistant:
  #   enabled: true
  #   access_level: transcript
  #   trigger:
  #     type: wake_word
  #     wake_word: hermes
  #   ha_url: http://localhost:8123
  #   ha_token: YOUR_TOKEN_HERE
"""

        if not plugins_yml_path.exists():
            return default_config

        with open(plugins_yml_path, "r") as f:
            yaml_content = f.read()

        return yaml_content

    except Exception as e:
        logger.error(f"Error loading plugins config: {e}")
        raise


async def save_plugins_config_yaml(yaml_content: str) -> dict:
    """Save plugins configuration from YAML text."""
    try:
        plugins_yml_path = get_plugins_yml_path()

        # Validate YAML can be parsed
        try:
            parsed_config = _yaml.load(yaml_content)
            if not isinstance(parsed_config, dict):
                raise ValueError("Configuration must be a YAML dictionary")

            # Validate has 'plugins' key
            if "plugins" not in parsed_config:
                raise ValueError("Configuration must contain 'plugins' key")

        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid YAML syntax: {e}")

        # Create config directory if it doesn't exist
        plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing config
        if plugins_yml_path.exists():
            backup_path = str(plugins_yml_path) + ".backup"
            shutil.copy2(plugins_yml_path, backup_path)
            logger.info(f"Created plugins config backup at {backup_path}")

        # Save new config
        with open(plugins_yml_path, "w") as f:
            f.write(yaml_content)

        # Hot-reload plugins and signal worker restart
        reload_result = None
        try:
            reload_result, _ = await _reload_and_signal()
            logger.info("Plugins reloaded and worker restart signaled")
        except Exception as reload_err:
            logger.warning(f"Auto-reload failed, manual restart needed: {reload_err}")

        logger.info("Plugins configuration updated successfully")

        message = "Plugins configuration updated and reloaded successfully."
        if reload_result is None:
            message = "Plugins configuration updated. Restart backend for changes to take effect."

        return {
            "success": True,
            "message": message,
            "reload": reload_result,
        }

    except Exception as e:
        logger.error(f"Error saving plugins config: {e}")
        raise


async def validate_plugins_config_yaml(yaml_content: str) -> dict:
    """Validate plugins configuration YAML."""
    try:
        # Parse YAML
        try:
            parsed_config = _yaml.load(yaml_content)
        except Exception as e:
            return {"valid": False, "error": f"Invalid YAML syntax: {e}"}

        # Check structure
        if not isinstance(parsed_config, dict):
            return {"valid": False, "error": "Configuration must be a YAML dictionary"}

        if "plugins" not in parsed_config:
            return {"valid": False, "error": "Configuration must contain 'plugins' key"}

        plugins = parsed_config["plugins"]
        if not isinstance(plugins, dict):
            return {"valid": False, "error": "'plugins' must be a dictionary"}

        # Validate each plugin
        valid_access_levels = ["transcript", "conversation", "memory"]
        valid_trigger_types = ["wake_word", "always", "conditional"]

        for plugin_id, plugin_config in plugins.items():
            if not isinstance(plugin_config, dict):
                return {
                    "valid": False,
                    "error": f"Plugin '{plugin_id}' config must be a dictionary",
                }

            # Check required fields
            if "enabled" in plugin_config and not isinstance(
                plugin_config["enabled"], bool
            ):
                return {
                    "valid": False,
                    "error": f"Plugin '{plugin_id}': 'enabled' must be boolean",
                }

            if (
                "access_level" in plugin_config
                and plugin_config["access_level"] not in valid_access_levels
            ):
                return {
                    "valid": False,
                    "error": f"Plugin '{plugin_id}': invalid access_level (must be one of {valid_access_levels})",
                }

            if "trigger" in plugin_config:
                trigger = plugin_config["trigger"]
                if not isinstance(trigger, dict):
                    return {
                        "valid": False,
                        "error": f"Plugin '{plugin_id}': 'trigger' must be a dictionary",
                    }

                if "type" in trigger and trigger["type"] not in valid_trigger_types:
                    return {
                        "valid": False,
                        "error": f"Plugin '{plugin_id}': invalid trigger type (must be one of {valid_trigger_types})",
                    }

        return {"valid": True, "message": "Configuration is valid"}

    except Exception as e:
        logger.error(f"Error validating plugins config: {e}")
        return {"valid": False, "error": f"Validation error: {str(e)}"}


async def _reload_and_signal(app=None) -> tuple[dict, bool]:
    """Reload plugins and signal worker restart.

    Returns:
        (reload_result, worker_signal_sent) tuple.
    """
    reload_result = await reload_plugins(app=app)

    worker_signal_sent = False
    try:
        signal_worker_restart()
        worker_signal_sent = True
    except Exception as e:
        logger.error(f"Failed to signal worker restart: {e}")

    return reload_result, worker_signal_sent


async def restart_workers() -> dict:
    """Signal all RQ workers to gracefully restart via Redis.

    Workers finish their current job before restarting.
    Uses the existing plugin-reload worker restart mechanism.
    """
    try:
        signal_worker_restart()
        logger.info("Worker restart signaled via Redis")
        return {
            "message": "Worker restart signal sent. Workers will restart after finishing current jobs.",
            "status": "accepted",
        }
    except Exception as e:
        logger.exception("Failed to signal worker restart")
        raise e


async def restart_backend() -> dict:
    """Schedule a SIGTERM to the current process after a short delay.

    The delay allows the HTTP response to be sent before the process dies.
    Docker (or the process supervisor) will automatically restart the container.
    """

    async def _delayed_kill():
        await asyncio.sleep(1.5)
        logger.info("Sending SIGTERM to self (PID %d) for backend restart", os.getpid())
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_kill())
    logger.info("Backend restart scheduled in 1.5s")
    return {
        "message": "Backend restart scheduled. The service will be briefly unavailable.",
        "status": "accepted",
    }


async def reload_plugins_controller(app=None) -> dict:
    """Reload all plugins and signal workers to restart.

    Args:
        app: Optional FastAPI app instance for updating app.state.plugin_router

    Returns:
        Combined result with backend reload details and worker signal status
    """
    reload_result, worker_signal_sent = await _reload_and_signal(app=app)

    return {
        "success": reload_result.get("success", False),
        "message": (
            "Plugins reloaded and worker restart signaled"
            if worker_signal_sent
            else "Plugins reloaded but worker restart signal failed"
        ),
        "reload": reload_result,
        "worker_signal_sent": worker_signal_sent,
    }


# Structured Plugin Configuration Management Functions (Form-based UI)


async def get_plugins_metadata() -> dict:
    """Get plugin metadata for form-based configuration UI.

    Returns complete metadata for all discovered plugins including:
    - Plugin information (name, description, enabled status)
    - Auto-generated schemas from config.yml (or explicit schema.yml)
    - Current configuration with masked secrets
    - Orchestration settings (events, conditions)

    Returns:
        Dict with plugins list containing metadata for each plugin
    """
    try:
        # Discover all available plugins
        discovered_plugins = discover_plugins()

        # Load orchestration config from plugins.yml
        plugins_yml_path = get_plugins_yml_path()
        orchestration_configs = {}

        if plugins_yml_path.exists():
            with open(plugins_yml_path, "r") as f:
                plugins_data = _yaml.load(f) or {}
                orchestration_configs = plugins_data.get("plugins", {})

        # Build metadata for each plugin
        plugins_metadata = []
        for plugin_id, plugin_class in discovered_plugins.items():
            # Get orchestration config (or empty dict if not configured)
            orchestration_config = orchestration_configs.get(
                plugin_id,
                {"enabled": False, "events": [], "condition": {"type": "always"}},
            )

            # Get complete metadata including schema
            metadata = get_plugin_metadata(
                plugin_id, plugin_class, orchestration_config
            )
            plugins_metadata.append(metadata)

        logger.info(f"Retrieved metadata for {len(plugins_metadata)} plugins")

        return {"plugins": plugins_metadata, "status": "success"}

    except Exception as e:
        logger.exception("Error getting plugins metadata")
        raise e


async def update_plugin_config_structured(plugin_id: str, config: dict) -> dict:
    """Update plugin configuration from structured JSON (form data).

    Updates the three-file plugin architecture:
    1. config/plugins.yml - Orchestration (enabled, events, condition, priority, modes)
    2. plugins/{plugin_id}/config.yml - Settings with ${ENV_VAR} references
    3. backend/.env - Actual secret values

    Args:
        plugin_id: Plugin identifier
        config: Structured configuration with 'orchestration', 'settings', 'env_vars' sections

    Returns:
        Success message with list of updated files
    """
    try:
        # Validate plugin exists
        discovered_plugins = discover_plugins()
        if plugin_id not in discovered_plugins:
            raise ValueError(f"Plugin '{plugin_id}' not found")

        updated_files = []

        # 1. Update config/plugins.yml (orchestration)
        if "orchestration" in config:
            plugins_yml_path = get_plugins_yml_path()

            # Load current plugins.yml
            if plugins_yml_path.exists():
                with open(plugins_yml_path, "r") as f:
                    plugins_data = _yaml.load(f) or {}
            else:
                plugins_data = {}

            if "plugins" not in plugins_data:
                plugins_data["plugins"] = {}

            # Update orchestration config
            orchestration = config["orchestration"]
            existing_orchestration = plugins_data["plugins"].get(plugin_id) or {}
            updated_orchestration = {
                "enabled": orchestration.get("enabled", False),
                "events": orchestration.get("events", []),
                "condition": orchestration.get("condition", {"type": "always"}),
            }
            # The current admin form may not render these fields yet. Preserve
            # existing values unless the caller explicitly sends replacements,
            # otherwise toggling a plugin would silently destroy its route order
            # or disable its interaction worker declaration.
            for key in ("priority", "modes"):
                if key in orchestration:
                    updated_orchestration[key] = orchestration[key]
                elif key in existing_orchestration:
                    updated_orchestration[key] = existing_orchestration[key]
            plugins_data["plugins"][plugin_id] = updated_orchestration

            # Create backup
            if plugins_yml_path.exists():
                backup_path = str(plugins_yml_path) + ".backup"
                shutil.copy2(plugins_yml_path, backup_path)

            # Create config directory if needed
            plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

            # Write updated plugins.yml
            with open(plugins_yml_path, "w") as f:
                _yaml.dump(plugins_data, f)

            updated_files.append(str(plugins_yml_path))
            logger.info(
                f"Updated orchestration config for '{plugin_id}' in {plugins_yml_path}"
            )

        # 2. Update plugins/{plugin_id}/config.yml (settings with env var references)
        if "settings" in config:
            plugins_dir = _get_plugins_dir()
            plugin_config_path = plugins_dir / plugin_id / "config.yml"

            # Load current config.yml
            if plugin_config_path.exists():
                with open(plugin_config_path, "r") as f:
                    plugin_config_data = _yaml.load(f) or {}
            else:
                plugin_config_data = {}

            # Update settings (preserve ${ENV_VAR} references)
            settings = config["settings"]
            plugin_config_data.update(settings)

            # Create backup
            if plugin_config_path.exists():
                backup_path = str(plugin_config_path) + ".backup"
                shutil.copy2(plugin_config_path, backup_path)

            # Write updated config.yml
            with open(plugin_config_path, "w") as f:
                _yaml.dump(plugin_config_data, f)

            updated_files.append(str(plugin_config_path))
            logger.info(f"Updated settings for '{plugin_id}' in {plugin_config_path}")

        # 3. Update per-plugin .env (only changed env vars)
        if "env_vars" in config and config["env_vars"]:
            # Filter out masked values (unchanged secrets)
            changed_vars = {
                k: v for k, v in config["env_vars"].items() if v != "••••••••••••"
            }

            if changed_vars:
                env_path = save_plugin_env(plugin_id, changed_vars)
                updated_files.append(str(env_path))
                logger.info(
                    f"Saved {len(changed_vars)} env var(s) to per-plugin .env for '{plugin_id}'"
                )

                # Update os.environ so hot-reload picks up changes immediately
                for k, v in changed_vars.items():
                    os.environ[k] = v

        # Hot-reload plugins and signal worker restart
        reload_result = None
        try:
            reload_result, _ = await _reload_and_signal()
        except Exception as reload_err:
            logger.warning(f"Auto-reload failed, manual restart needed: {reload_err}")

        message = (
            f"Plugin '{plugin_id}' configuration updated and reloaded successfully."
        )
        if reload_result is None:
            message = f"Plugin '{plugin_id}' configuration updated. Restart backend for changes to take effect."

        return {
            "success": True,
            "message": message,
            "updated_files": updated_files,
            "reload": reload_result,
            "status": "success",
        }

    except Exception as e:
        logger.exception(f"Error updating structured config for plugin '{plugin_id}'")
        raise e


async def test_plugin_connection(plugin_id: str, config: dict) -> dict:
    """Test plugin connection/configuration without saving.

    Calls the plugin's test_connection method if available to validate
    configuration (e.g., SMTP connection, Home Assistant API).

    Args:
        plugin_id: Plugin identifier
        config: Configuration to test (same structure as update_plugin_config_structured)

    Returns:
        Test result with success status and details
    """
    try:
        # Validate plugin exists
        discovered_plugins = discover_plugins()
        if plugin_id not in discovered_plugins:
            raise ValueError(f"Plugin '{plugin_id}' not found")

        plugin_class = discovered_plugins[plugin_id]

        # Check if plugin supports testing
        if not hasattr(plugin_class, "test_connection"):
            return {
                "success": False,
                "message": f"Plugin '{plugin_id}' does not support connection testing",
                "status": "unsupported",
            }

        # Build complete config from provided data
        test_config = {}

        # Merge settings
        if "settings" in config:
            test_config.update(config["settings"])

        # Load per-plugin env for resolving masked values
        plugin_env = load_plugin_env(plugin_id)

        # Add env vars (expand any ${ENV_VAR} references with test values)
        if "env_vars" in config:
            for key, value in config["env_vars"].items():
                # For masked values, resolve from per-plugin .env then os.environ
                if value == "••••••••••••":
                    value = plugin_env.get(key) or os.getenv(key, "")
                test_config[key.lower()] = value

        # Expand any remaining env var references
        test_config = expand_env_vars(test_config)

        # Call plugin's test_connection static method
        result = await plugin_class.test_connection(test_config)

        logger.info(
            f"Test connection for '{plugin_id}': {result.get('message', 'No message')}"
        )

        return result

    except Exception as e:
        logger.exception(f"Error testing connection for plugin '{plugin_id}'")
        return {
            "success": False,
            "message": f"Connection test failed: {str(e)}",
            "status": "error",
        }


# Plugin Lifecycle Management Functions (create / write-code / delete)


def _snake_to_pascal(snake_str: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in snake_str.split("_"))


def _extract_class_name(code: str) -> Optional[str]:
    """Extract the BasePlugin subclass name from plugin code."""
    match = re.search(r"class\s+(\w+)\s*\(.*BasePlugin.*\)", code)
    return match.group(1) if match else None


async def create_plugin(
    plugin_name: str,
    description: str,
    events: list[str],
    plugin_code: Optional[str] = None,
) -> dict:
    """Create a new plugin directory with boilerplate or LLM-generated code.

    Args:
        plugin_name: snake_case plugin identifier
        description: Human-readable description
        events: List of event strings the plugin subscribes to
        plugin_code: Optional full plugin.py source (LLM-generated)

    Returns:
        Success dict with plugin_id and created_files list
    """
    # Validate name
    if not plugin_name.replace("_", "").isalnum():
        return {
            "success": False,
            "error": "Plugin name must be alphanumeric with underscores only",
        }

    if not re.match(r"^[a-z][a-z0-9_]*$", plugin_name):
        return {
            "success": False,
            "error": "Plugin name must be lowercase snake_case starting with a letter",
        }

    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_name

    # Collision check
    if plugin_dir.exists():
        return {
            "success": False,
            "error": f"Plugin '{plugin_name}' already exists at {plugin_dir}",
        }

    discovered = discover_plugins()
    if plugin_name in discovered:
        return {
            "success": False,
            "error": f"Plugin '{plugin_name}' is already registered",
        }

    class_name = _snake_to_pascal(plugin_name) + "Plugin"
    created_files: list[str] = []

    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # plugin.py
        if plugin_code:
            # Use LLM-generated code; extract real class name from it
            extracted = _extract_class_name(plugin_code)
            if extracted:
                class_name = extracted
            (plugin_dir / "plugin.py").write_text(plugin_code, encoding="utf-8")
        else:
            # Write standard boilerplate
            events_str = (
                ", ".join(f'"{e}"' for e in events)
                if events
                else '"conversation.complete"'
            )
            boilerplate = inspect.cleandoc(f'''
                """
                {class_name} implementation.

                {description}
                """
                import logging
                from typing import Any, Dict, Optional

                from backend.plugins.base import BasePlugin, PluginContext, PluginResult

                logger = logging.getLogger(__name__)


                class {class_name}(BasePlugin):
                    """{description}

                    Subscribes to: [{events_str}]
                    """

                    SUPPORTED_ACCESS_LEVELS = ["conversation"]

                    def __init__(self, config: Dict[str, Any]):
                        super().__init__(config)
                        logger.info("{class_name} loaded")

                    async def initialize(self):
                        if not self.enabled:
                            return
                        logger.info("{class_name} initialized")

                    async def cleanup(self):
                        logger.info("{class_name} cleanup complete")

                    async def on_conversation_complete(self, context: PluginContext) -> Optional[PluginResult]:
                        logger.info(f"Processing conversation for user: {{context.user_id}}")
                        return PluginResult(success=True, message="OK")
            ''') + "\n"
            (plugin_dir / "plugin.py").write_text(boilerplate, encoding="utf-8")
        created_files.append("plugin.py")

        # __init__.py
        init_content = f'"""{class_name} for Chronicle."""\n\nfrom .plugin import {class_name}\n\n__all__ = ["{class_name}"]\n'
        (plugin_dir / "__init__.py").write_text(init_content, encoding="utf-8")
        created_files.append("__init__.py")

        # config.yml
        config_yml = {"description": description}
        with open(plugin_dir / "config.yml", "w", encoding="utf-8") as f:
            _yaml.dump(config_yml, f)
        created_files.append("config.yml")

        # README.md
        readme = f"# {class_name}\n\n{description}\n"
        (plugin_dir / "README.md").write_text(readme, encoding="utf-8")
        created_files.append("README.md")

        # Add disabled entry to plugins.yml
        plugins_yml_path = get_plugins_yml_path()
        if plugins_yml_path.exists():
            with open(plugins_yml_path, "r") as f:
                plugins_data = _yaml.load(f) or {}
        else:
            plugins_data = {}
            plugins_yml_path.parent.mkdir(parents=True, exist_ok=True)

        if "plugins" not in plugins_data:
            plugins_data["plugins"] = {}

        plugins_data["plugins"][plugin_name] = {
            "enabled": False,
            "events": events or ["conversation.complete"],
            "condition": {"type": "always"},
        }
        with open(plugins_yml_path, "w") as f:
            _yaml.dump(plugins_data, f)

        logger.info(f"Created plugin '{plugin_name}' at {plugin_dir}")
        return {
            "success": True,
            "plugin_id": plugin_name,
            "created_files": created_files,
            "plugin_dir": str(plugin_dir),
        }

    except Exception as e:
        # Clean up partial directory on error
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        logger.exception(f"Error creating plugin '{plugin_name}'")
        return {"success": False, "error": str(e)}


async def write_plugin_code(
    plugin_id: str,
    code: str,
    config_yml: Optional[str] = None,
) -> dict:
    """Overwrite an existing plugin's code.

    Args:
        plugin_id: Plugin identifier (directory name)
        code: New plugin.py source code
        config_yml: Optional new config.yml content (YAML string)

    Returns:
        Success dict with updated_files list
    """
    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id

    if not plugin_dir.exists():
        return {
            "success": False,
            "error": f"Plugin '{plugin_id}' not found at {plugin_dir}",
        }

    updated_files: list[str] = []

    try:
        # Write plugin.py
        (plugin_dir / "plugin.py").write_text(code, encoding="utf-8")
        updated_files.append("plugin.py")

        # Update __init__.py with extracted class name
        class_name = _extract_class_name(code)
        if class_name:
            init_content = f'"""{class_name} for Chronicle."""\n\nfrom .plugin import {class_name}\n\n__all__ = ["{class_name}"]\n'
            (plugin_dir / "__init__.py").write_text(init_content, encoding="utf-8")
            updated_files.append("__init__.py")

        # Optionally update config.yml
        if config_yml is not None:
            # Validate YAML
            _yaml.load(config_yml)
            (plugin_dir / "config.yml").write_text(config_yml, encoding="utf-8")
            updated_files.append("config.yml")

        logger.info(f"Updated plugin code for '{plugin_id}': {updated_files}")
        return {
            "success": True,
            "plugin_id": plugin_id,
            "updated_files": updated_files,
        }

    except Exception as e:
        logger.exception(f"Error writing code for plugin '{plugin_id}'")
        return {"success": False, "error": str(e)}


async def delete_plugin(plugin_id: str, remove_files: bool = False) -> dict:
    """Delete a plugin from plugins.yml and optionally remove its files.

    Args:
        plugin_id: Plugin identifier
        remove_files: If True, also delete the plugin directory

    Returns:
        Success dict
    """
    plugins_yml_path = get_plugins_yml_path()

    # Check plugins.yml
    if plugins_yml_path.exists():
        with open(plugins_yml_path, "r") as f:
            plugins_data = _yaml.load(f) or {}
    else:
        plugins_data = {}

    plugin_entry = plugins_data.get("plugins", {}).get(plugin_id)

    # Refuse if enabled
    if plugin_entry and plugin_entry.get("enabled"):
        return {
            "success": False,
            "error": f"Plugin '{plugin_id}' is currently enabled. Disable it first before deleting.",
        }

    # Remove from plugins.yml
    removed_from_yml = False
    if plugin_entry is not None:
        del plugins_data["plugins"][plugin_id]
        with open(plugins_yml_path, "w") as f:
            _yaml.dump(plugins_data, f)
        removed_from_yml = True

    # Optionally remove files
    files_removed = False
    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id
    if remove_files and plugin_dir.exists():
        shutil.rmtree(plugin_dir)
        files_removed = True
        logger.info(f"Removed plugin directory: {plugin_dir}")

    if not removed_from_yml and not files_removed:
        return {
            "success": False,
            "error": f"Plugin '{plugin_id}' not found in plugins.yml or on disk",
        }

    logger.info(
        f"Deleted plugin '{plugin_id}' (yml={removed_from_yml}, files={files_removed})"
    )
    return {
        "success": True,
        "plugin_id": plugin_id,
        "removed_from_yml": removed_from_yml,
        "files_removed": files_removed,
    }


# ── External Service Management (host service-manager agent proxy) ──────────
# The service manager agent (edge/service_manager.py) runs natively on the
# host and wraps services.py — the backend just proxies admin requests to it.

_SERVICE_MANAGER_TIMEOUT = 30.0


def _service_manager_config() -> tuple[str, str]:
    url = (os.getenv("SERVICE_MANAGER_URL") or "").rstrip("/")
    token = os.getenv("SERVICE_MANAGER_TOKEN") or ""
    return url, token


async def _service_manager_request(
    method: str,
    path: str,
    json_body: dict | None = None,
    *,
    params: dict | None = None,
    timeout: float = _SERVICE_MANAGER_TIMEOUT,
):
    """Proxy a request to the LOCAL service manager agent. Raises on failure.

    The backend always talks to its local agent — cross-node merge + control
    forwarding happens inside the agent (a host process with a real Tailnet
    identity), because a container can't present a Tailnet source IP for the peer
    agents' tailnet-trust to accept.

    ``timeout`` overrides the default for slow calls (e.g. the update status
    check, where the agent fetches from origin before answering).
    """
    url, token = _service_manager_config()
    if not url or not token:
        raise HTTPException(
            status_code=503,
            detail="Service manager not configured (SERVICE_MANAGER_URL / SERVICE_MANAGER_TOKEN)",
        )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                f"{url}{path}",
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502, detail=f"Service manager unreachable at {url}: {e}"
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


async def _local_node_host() -> str | None:
    """This node's hostname per the local agent — used to tell whether a provider
    switch targets the local pipeline (so we repoint hub config.yml) or a remote node.
    """
    try:
        node = await _service_manager_request("GET", "/node")
    except HTTPException:
        return None
    return node.get("host")


async def get_external_services():
    """List host-managed services across the cluster with health and provider info.

    The local agent merges this node's services with peer nodes' (each tagged with a
    ``node`` host + ``remote`` flag) so the WebUI can group and route control calls.
    Returns available=False (instead of an error) when the local agent is not
    configured or unreachable.
    """
    url, token = _service_manager_config()
    if not url or not token:
        return {"available": False, "reason": "not_configured"}
    try:
        data = await _service_manager_request("GET", "/services")
    except HTTPException as e:
        if e.status_code in (502, 503):
            return {"available": False, "reason": "unreachable", "detail": e.detail}
        raise
    return {"available": True, **data}


async def external_service_action(name: str, action: str, body: dict):
    """Start/stop/restart a host-managed service. ``body["node"]`` selects the owning
    node; the local agent forwards to that node when it isn't the local one."""
    result = await _service_manager_request("POST", f"/services/{name}/{action}", body)
    # Tag the operation with its node so the WebUI polls the right agent.
    if isinstance(result.get("operation"), dict):
        result["operation"]["node"] = body.get("node")
    return result


async def get_node_update_status(node: str | None = None, target: str | None = None):
    """Check whether a node can be updated (fetches from origin via the agent).

    The local agent inspects its git checkout (or forwards to ``node``'s agent) and
    reports the current describe/commit/branch plus whether ``target`` is ahead.
    Returns available=False (instead of raising) when the agent is not configured or
    unreachable, so the WebUI can hide/disable the update card. The agent's fetch can
    take several seconds, so this call uses an extended timeout.
    """
    url, token = _service_manager_config()
    if not url or not token:
        return {"available": False, "reason": "not_configured"}
    params = {}
    if node:
        params["node"] = node
    if target:
        params["target"] = target
    try:
        data = await _service_manager_request(
            "GET", "/update", params=params or None, timeout=90.0
        )
    except HTTPException as e:
        if e.status_code in (502, 503):
            return {"available": False, "reason": "unreachable", "detail": e.detail}
        raise
    return {"available": True, "node": node, **data}


async def trigger_node_update(body: dict):
    """Update a node's git checkout and rebuild/restart its services via the agent.

    ``body["node"]`` selects the owning node (the local agent forwards to that node
    when set). This is an explicit admin action, so failures raise through as
    HTTPExceptions rather than being wrapped in available=False. The agent's fetch +
    build can be slow, so this call uses an extended timeout."""
    result = await _service_manager_request("POST", "/update", body, timeout=90.0)
    # Tag the operation with its node so the WebUI polls the right agent.
    if isinstance(result.get("operation"), dict):
        result["operation"]["node"] = body.get("node")
    return result


# ASR_PROVIDER key (extras/asr-services/.env, drives which container runs) →
# STT model name in the model registry (drives which model entry the pipeline
# calls). A provider switch must update BOTH, or transcription keeps using the
# old model entry while a different container serves the port.
_ASR_PROVIDER_TO_STT_MODEL = {
    "vibevoice": "stt-vibevoice",
    "vibevoice-strixhalo": "stt-vibevoice",
    "faster-whisper": "stt-faster-whisper",
    "transformers": "stt-transformers",
    "nemo": "stt-nemo",
    "nemo-strixhalo": "stt-nemo",
    "parakeet": "stt-parakeet-batch",
    "qwen3-asr": "stt-qwen3-asr",
    "gemma4": "stt-gemma4",
    "nemotron": "stt-nemotron-batch",
}

# Streaming ASR provider → stt_stream model name. Mirror of the streaming options
# in services.py (STREAMING_ASR_PROVIDER_OPTIONS); a streaming switch repoints
# defaults.stt_stream so live transcription uses the newly selected provider.
_STREAMING_ASR_PROVIDER_TO_STT_STREAM_MODEL = {
    "nemotron": "stt-nemotron-stream",
    "smallest": "stt-smallest-stream",
    "deepgram": "stt-deepgram-stream",
    "qwen3-asr": "stt-qwen3-asr-stream",
}


async def set_external_service_provider(name: str, body: dict):
    """Switch the active provider (ASR/TTS) for a host-managed service.

    For ASR this also repoints config.yml at the matching model registry entry
    and hot-reloads the registry (+ signals workers), so the pipeline actually
    uses the newly selected provider. The batch lane drives defaults.stt; the
    streaming lane (body["lane"] == "streaming") drives defaults.stt_stream.

    ``body["node"]`` selects the owning node (the local agent forwards to a remote
    node when set). For a *remote* node we skip the hub-side config.yml/registry
    repoint (that's a local-pipeline concern — the hub operator points defaults at the
    remote provider separately).
    """
    node = body.get("node")
    result = await _service_manager_request("POST", f"/services/{name}/provider", body)
    if isinstance(result.get("operation"), dict):
        result["operation"]["node"] = node

    is_local = not node or node == await _local_node_host()
    if name == "asr-services" and is_local:
        streaming = body.get("lane") == "streaming"
        default_key = "stt_stream" if streaming else "stt"
        model_map = (
            _STREAMING_ASR_PROVIDER_TO_STT_STREAM_MODEL
            if streaming
            else _ASR_PROVIDER_TO_STT_MODEL
        )
        stt_model = model_map.get(body.get("provider", ""))
        if stt_model:
            save_config_section("defaults", {default_key: stt_model})
            load_models_config(force_reload=True)
            signal_worker_restart()
            logger.info(
                "ASR %s provider switched to %s — defaults.%s set to %s, "
                "registry reloaded, workers signaled",
                "streaming" if streaming else "batch",
                body.get("provider"),
                default_key,
                stt_model,
            )
            result["stt_model"] = stt_model
        else:
            logger.warning(
                "No STT model mapping for ASR provider %r (lane=%s) — defaults.%s unchanged",
                body.get("provider"),
                body.get("lane", "batch"),
                default_key,
            )

    return result


async def get_external_service_operation(operation_id: str, node: str | None = None):
    """Poll a long-running service operation. ``node`` is forwarded to the local agent,
    which routes the poll to the remote node that owns the operation."""
    params = {"node": node} if node else None
    result = await _service_manager_request(
        "GET", f"/operations/{operation_id}", params=params
    )
    if isinstance(result, dict):
        result["node"] = node
    return result


async def get_remote_control_status():
    """Status of the host's Claude remote-control session (for the System page).

    Returns available=False (instead of raising) when the agent is not configured
    or unreachable, so the WebUI can hide/disable the card.
    """
    url, token = _service_manager_config()
    if not url or not token:
        return {"available": False, "reason": "not_configured"}
    try:
        data = await _service_manager_request("GET", "/remote-control")
    except HTTPException as e:
        if e.status_code in (502, 503):
            return {"available": False, "reason": "unreachable", "detail": e.detail}
        raise
    return {"available": True, **data}


async def remote_control_action(action: str):
    """Start/stop/restart the host's Claude remote-control session via the agent."""
    return await _service_manager_request("POST", f"/remote-control/{action}")
