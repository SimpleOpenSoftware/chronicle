"""
System and utility routes for Chronicle API.

Handles metrics, auth config, and other system utilities.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from backend.auth import current_active_user, current_superuser
from backend.controllers import queue_controller, session_controller, system_controller
from backend.models.user import User
from backend.redis_factory import create_async_redis
from backend.services import plugin_assistant
from backend.services.audio_stream.reclaim import reclaim_settled_audio_streams
from backend.services.observability.loop_monitor import SNAPSHOT_KEY_PREFIX, get_monitor
from backend.services.plugin_service import get_plugin_router
from backend.services.status_reconciler import reconcile_conversation_statuses

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


# Request models for memory config endpoints
class MemoryConfigRequest(BaseModel):
    """Request model for memory configuration validation and updates."""

    config_yaml: str


class WakewordSpeakerGateRequest(BaseModel):
    """Request model for the wake-word speaker gate configuration.

    ``speakers`` are the allowed enrolled speakers ({speaker_id, name}); the gate
    only fires for these when ``enabled``.
    """

    enabled: bool
    speakers: list[dict] = []


@router.get("/system/network")
async def get_network_discovery(
    request: Request, current_user: User = Depends(current_superuser)
):
    """Get Tailscale and minidisc service discovery status. Admin only."""
    return await system_controller.get_network_discovery(request.app, current_user)


@router.get("/config/diagnostics")
async def get_config_diagnostics(current_user: User = Depends(current_superuser)):
    """Get configuration diagnostics including errors, warnings, and status. Admin only."""
    return await system_controller.get_config_diagnostics()


@router.get("/metrics")
async def get_current_metrics(current_user: User = Depends(current_superuser)):
    """Get current system metrics. Admin only."""
    return await system_controller.get_current_metrics()


@router.get("/auth/config")
async def get_auth_config():
    """Get authentication configuration for frontend."""
    return await system_controller.get_auth_config()


@router.get("/observability")
async def get_observability_config():
    """Get observability configuration for frontend (Langfuse deep-links).

    Returns non-secret data: enabled status and browser-accessible session URL.
    No authentication required.
    """
    return await system_controller.get_observability_config()


@router.get("/diarization-settings")
async def get_diarization_settings(current_user: User = Depends(current_superuser)):
    """Get current diarization settings. Admin only."""
    return await system_controller.get_diarization_settings()


@router.post("/diarization-settings")
async def save_diarization_settings(
    settings: dict, current_user: User = Depends(current_superuser)
):
    """Save diarization settings. Admin only."""
    return await system_controller.save_diarization_settings_controller(settings)


@router.get("/asr-context")
async def get_asr_context_config(current_user: User = Depends(current_superuser)):
    """Get the active STT providers' hint mechanism + context. Admin only."""
    return await system_controller.get_asr_context_config()


@router.post("/asr-context")
async def save_asr_context(
    payload: dict, current_user: User = Depends(current_superuser)
):
    """Save a context string for a context_prompt STT provider. Admin only."""
    return await system_controller.save_asr_context_controller(payload)


@router.get("/misc-settings")
async def get_misc_settings(current_user: User = Depends(current_superuser)):
    """Get miscellaneous configuration settings. Admin only."""
    return await system_controller.get_misc_settings()


@router.post("/misc-settings")
async def save_misc_settings(
    settings: dict, current_user: User = Depends(current_superuser)
):
    """Save miscellaneous configuration settings. Admin only."""
    return await system_controller.save_misc_settings_controller(settings)


@router.get("/timeline-grouping-settings")
async def get_timeline_grouping_settings(
    current_user: User = Depends(current_superuser),
):
    return await system_controller.get_timeline_grouping_settings()


@router.post("/timeline-grouping-settings")
async def save_timeline_grouping_settings(
    settings: dict, current_user: User = Depends(current_superuser)
):
    return await system_controller.save_timeline_grouping_settings(settings)


@router.get("/cleanup-settings")
async def get_cleanup_settings(current_user: User = Depends(current_superuser)):
    """Get cleanup configuration settings. Admin only."""
    return await system_controller.get_cleanup_settings_controller(current_user)


@router.post("/cleanup-settings")
async def save_cleanup_settings(
    auto_cleanup_enabled: bool = Body(
        ..., description="Enable automatic cleanup of soft-deleted conversations"
    ),
    retention_days: int = Body(
        ...,
        ge=1,
        le=365,
        description="Number of days to keep soft-deleted conversations",
    ),
    current_user: User = Depends(current_superuser),
):
    """Save cleanup configuration settings. Admin only."""
    return await system_controller.save_cleanup_settings_controller(
        auto_cleanup_enabled=auto_cleanup_enabled,
        retention_days=retention_days,
        user=current_user,
    )


@router.get("/speaker-configuration")
async def get_speaker_configuration(current_user: User = Depends(current_active_user)):
    """Get current user's primary speakers configuration."""
    return await system_controller.get_speaker_configuration(current_user)


@router.post("/speaker-configuration")
async def update_speaker_configuration(
    primary_speakers: list[dict], current_user: User = Depends(current_active_user)
):
    """Update current user's primary speakers configuration."""
    return await system_controller.update_speaker_configuration(
        current_user, primary_speakers
    )


@router.get("/wakeword-speaker-gate")
async def get_wakeword_speaker_gate(
    current_user: User = Depends(current_active_user),
):
    """Get current user's wake-word speaker gate configuration."""
    return await system_controller.get_wakeword_speaker_gate(current_user)


@router.post("/wakeword-speaker-gate")
async def update_wakeword_speaker_gate(
    payload: WakewordSpeakerGateRequest,
    current_user: User = Depends(current_active_user),
):
    """Update current user's wake-word speaker gate configuration."""
    return await system_controller.update_wakeword_speaker_gate(
        current_user, payload.enabled, payload.speakers
    )


@router.get("/enrolled-speakers")
async def get_enrolled_speakers(current_user: User = Depends(current_active_user)):
    """Get enrolled speakers from speaker recognition service."""
    return await system_controller.get_enrolled_speakers(current_user)


@router.get("/speaker-service-status")
async def get_speaker_service_status(current_user: User = Depends(current_superuser)):
    """Check speaker recognition service health status. Admin only."""
    return await system_controller.get_speaker_service_status()


# LLM Operations Configuration Endpoints


class ReconcileStatusRequest(BaseModel):
    dry_run: bool = False


@router.post("/admin/conversations/reconcile-status")
async def reconcile_conversation_status(
    dry_run: bool = Query(False, description="Preview the changes without writing"),
    body: Optional[ReconcileStatusRequest] = None,
    current_user: User = Depends(current_superuser),
):
    """Recompute conversation processing_status from facts (transcript present =>
    completed; none, once settled => failed). Self-heals drift left by crashed or
    timed-out jobs. Pass dry_run=true to preview without writing. Admin only.

    Accepted as a query parameter or in the body, and a preview requested either way
    wins. It used to be body-only while every other ``dry_run`` in the API is a query
    parameter, so the obvious ``?dry_run=true`` was silently dropped and the call
    wrote instead — the one direction this flag must never fail in.
    """
    preview = dry_run or (body.dry_run if body is not None else False)
    return await reconcile_conversation_statuses(dry_run=preview)


@router.get("/admin/llm-operations")
async def get_llm_operations(current_user: User = Depends(current_superuser)):
    """Get LLM operation configurations. Admin only."""
    return await system_controller.get_llm_operations()


@router.post("/admin/llm-operations")
async def save_llm_operations(
    operations: dict, current_user: User = Depends(current_superuser)
):
    """Save LLM operation configurations. Admin only."""
    return await system_controller.save_llm_operations(operations)


@router.post("/admin/llm-operations/test")
async def test_llm_model(
    model_name: Optional[str] = Body(None, embed=True),
    current_user: User = Depends(current_superuser),
):
    """Test an LLM model connection with a trivial prompt. Admin only."""
    return await system_controller.test_llm_model(model_name)


# Model Registry Management Endpoints


@router.get("/admin/models")
async def get_models(current_user: User = Depends(current_superuser)):
    """List all registry models grouped by type + active defaults. Admin only."""
    return await system_controller.get_models()


@router.post("/admin/defaults")
async def set_active_defaults(
    defaults: dict, current_user: User = Depends(current_superuser)
):
    """Repoint active-model defaults (llm/stt/stt_stream/...). Admin only."""
    return await system_controller.set_active_defaults(defaults)


@router.post("/admin/models")
async def upsert_model(model: dict, current_user: User = Depends(current_superuser)):
    """Add or update a model definition (incl. api key/url). Admin only."""
    return await system_controller.upsert_model(model)


@router.delete("/admin/models/{name}")
async def delete_model(name: str, current_user: User = Depends(current_superuser)):
    """Delete a config.yml model (not if it's an active default). Admin only."""
    return await system_controller.delete_model(name)


@router.post("/admin/models/test")
async def test_model(
    model_name: Optional[str] = Body(None, embed=True),
    current_user: User = Depends(current_superuser),
):
    """Connectivity test for a registry model (llm/embedding). Admin only."""
    return await system_controller.test_model(model_name)


# Memory Configuration Management Endpoints Removed - Project uses config.yml exclusively
@router.get("/admin/memory/config/raw")
async def get_memory_config_raw(current_user: User = Depends(current_superuser)):
    """Get memory configuration YAML from config.yml. Admin only."""
    return await system_controller.get_memory_config_raw()


@router.post("/admin/memory/config/raw")
async def update_memory_config_raw(
    config_yaml: str = Body(..., media_type="text/plain"),
    current_user: User = Depends(current_superuser),
):
    """Save memory YAML to config.yml and hot-reload. Admin only."""
    return await system_controller.update_memory_config_raw(config_yaml)


@router.post("/admin/memory/config/validate/raw")
async def validate_memory_config_raw(
    config_yaml: str = Body(..., media_type="text/plain"),
    current_user: User = Depends(current_superuser),
):
    """Validate posted memory YAML as plain text (used by Web UI). Admin only."""
    return await system_controller.validate_memory_config(config_yaml)


@router.post("/admin/memory/config/validate")
async def validate_memory_config(
    request: MemoryConfigRequest, current_user: User = Depends(current_superuser)
):
    """Validate memory configuration YAML sent as JSON (used by tests). Admin only."""
    return await system_controller.validate_memory_config(request.config_yaml)


@router.post("/admin/memory/config/reload")
async def reload_memory_config(current_user: User = Depends(current_superuser)):
    """Reload memory configuration from config.yml. Admin only."""
    return await system_controller.reload_memory_config()


@router.delete("/admin/memory/delete-all")
async def delete_all_user_memories(current_user: User = Depends(current_active_user)):
    """Delete all memories for the current user."""
    return await system_controller.delete_all_user_memories(current_user)


# Plugin Configuration Management Endpoints


@router.get("/admin/plugins/config", response_class=Response)
async def get_plugins_config(current_user: User = Depends(current_superuser)):
    """Get plugins configuration as YAML. Admin only."""
    try:
        yaml_content = await system_controller.get_plugins_config_yaml()
        return Response(content=yaml_content, media_type="text/plain")
    except Exception as e:
        logger.error(f"Failed to get plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/config")
async def save_plugins_config(
    request: Request, current_user: User = Depends(current_superuser)
):
    """Save plugins configuration from YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode("utf-8")
        result = await system_controller.save_plugins_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to save plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/config/validate")
async def validate_plugins_config(
    request: Request, current_user: User = Depends(current_superuser)
):
    """Validate plugins configuration YAML. Admin only."""
    try:
        yaml_content = await request.body()
        yaml_str = yaml_content.decode("utf-8")
        result = await system_controller.validate_plugins_config_yaml(yaml_str)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Failed to validate plugins config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Structured Plugin Configuration Endpoints (Form-based UI)


@router.post("/admin/plugins/reload")
async def reload_plugins(
    request: Request,
    current_user: User = Depends(current_superuser),
):
    """Hot-reload all plugins and signal workers to restart. Admin only.

    Reloads plugin code and configuration without a full container restart.
    Workers are signaled asynchronously via Redis and will restart after
    finishing their current job.
    """
    try:
        result = await system_controller.reload_plugins_controller(app=request.app)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Failed to reload plugins: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/system/restart-workers")
async def restart_workers(current_user: User = Depends(current_superuser)):
    """Signal all RQ workers to gracefully restart. Admin only.

    Workers finish their current job before restarting. Safe to run anytime.
    """
    try:
        result = await system_controller.restart_workers()
        return JSONResponse(content=result, status_code=202)
    except Exception as e:
        logger.error(f"Failed to restart workers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/system/restart-backend")
async def restart_backend(current_user: User = Depends(current_superuser)):
    """Schedule a backend restart. Admin only.

    Sends SIGTERM to the FastAPI process after a short delay.
    Docker will automatically restart the container.
    Active WebSocket connections will be dropped.
    """
    try:
        result = await system_controller.restart_backend()
        return JSONResponse(content=result, status_code=202)
    except Exception as e:
        logger.error(f"Failed to schedule backend restart: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/plugins/health")
async def get_plugins_health(current_user: User = Depends(current_superuser)):
    """Get plugin health status for all registered plugins. Admin only."""
    try:
        plugin_router = get_plugin_router()
        if not plugin_router:
            return {
                "total": 0,
                "initialized": 0,
                "failed": 0,
                "registered": 0,
                "plugins": [],
            }
        return plugin_router.get_health_summary()
    except Exception as e:
        logger.error(f"Failed to get plugins health: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/plugins/connectivity")
async def get_plugins_connectivity(current_user: User = Depends(current_superuser)):
    """Live connectivity check for all initialized plugins. Admin only.

    Runs each plugin's health_check() with a 10s timeout and returns results.
    """
    try:
        plugin_router = get_plugin_router()
        if not plugin_router:
            return {"plugins": {}}
        results = await plugin_router.check_connectivity()
        return {"plugins": results}
    except Exception as e:
        logger.error(f"Failed to check plugin connectivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/plugins/metadata")
async def get_plugins_metadata(current_user: User = Depends(current_superuser)):
    """Get plugin metadata for form-based configuration UI. Admin only.

    Returns:
        - Plugin information (name, description, enabled status)
        - Auto-generated schemas from config.yml
        - Current configuration with masked secrets
        - Orchestration settings (events, conditions)
    """
    try:
        return await system_controller.get_plugins_metadata()
    except Exception as e:
        logger.error(f"Failed to get plugins metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class PluginConfigRequest(BaseModel):
    """Request model for structured plugin configuration updates."""

    orchestration: Optional[dict] = None
    settings: Optional[dict] = None
    env_vars: Optional[dict] = None


class CreatePluginRequest(BaseModel):
    """Request model for creating a new plugin."""

    plugin_name: str
    description: str
    events: list[str] = []
    plugin_code: Optional[str] = None


class WritePluginCodeRequest(BaseModel):
    """Request model for writing plugin code."""

    code: str
    config_yml: Optional[str] = None


class PluginAssistantRequest(BaseModel):
    """Request model for plugin assistant chat."""

    messages: list[dict]


@router.post("/admin/plugins/config/structured/{plugin_id}")
async def update_plugin_config_structured(
    plugin_id: str,
    config: PluginConfigRequest,
    current_user: User = Depends(current_superuser),
):
    """Update plugin configuration from structured JSON (form data). Admin only.

    Updates the three-file plugin architecture:
    1. config/plugins.yml - Orchestration (enabled, events, condition)
    2. plugins/{plugin_id}/config.yml - Settings with ${ENV_VAR} references
    3. backend/.env - Actual secret values
    """
    try:
        config_dict = config.dict(exclude_none=True)
        result = await system_controller.update_plugin_config_structured(
            plugin_id, config_dict
        )
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update plugin config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/test-connection/{plugin_id}")
async def test_plugin_connection(
    plugin_id: str,
    config: PluginConfigRequest,
    current_user: User = Depends(current_superuser),
):
    """Test plugin connection/configuration without saving. Admin only.

    Calls the plugin's test_connection method to validate configuration
    (e.g., SMTP connection, Home Assistant API).
    """
    try:
        config_dict = config.dict(exclude_none=True)
        result = await system_controller.test_plugin_connection(plugin_id, config_dict)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to test plugin connection: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/create")
async def create_plugin(
    request: CreatePluginRequest,
    current_user: User = Depends(current_superuser),
):
    """Create a new plugin with boilerplate or LLM-generated code. Admin only."""
    try:
        result = await system_controller.create_plugin(
            plugin_name=request.plugin_name,
            description=request.description,
            events=request.events,
            plugin_code=request.plugin_code,
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "Unknown error")
            )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create plugin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/admin/plugins/{plugin_id}/code")
async def write_plugin_code(
    plugin_id: str,
    request: WritePluginCodeRequest,
    current_user: User = Depends(current_superuser),
):
    """Write or update plugin code. Admin only."""
    try:
        result = await system_controller.write_plugin_code(
            plugin_id=plugin_id,
            code=request.code,
            config_yml=request.config_yml,
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "Unknown error")
            )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to write plugin code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/admin/plugins/{plugin_id}")
async def delete_plugin(
    plugin_id: str,
    remove_files: bool = False,
    current_user: User = Depends(current_superuser),
):
    """Delete a plugin from plugins.yml and optionally remove files. Admin only."""
    try:
        result = await system_controller.delete_plugin(
            plugin_id=plugin_id,
            remove_files=remove_files,
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "Unknown error")
            )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete plugin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/plugins/assistant")
async def plugin_assistant_chat(
    body: PluginAssistantRequest,
    current_user: User = Depends(current_superuser),
):
    """AI-powered plugin configuration assistant. Admin only. Returns SSE stream."""
    messages = body.messages

    async def event_stream():
        try:
            async for event in plugin_assistant.generate_response_stream(messages):
                yield f"data: {json.dumps(event, default=str)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Plugin assistant error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'data': {'error': str(e)}})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/streaming/status")
async def get_streaming_status(
    request: Request, current_user: User = Depends(current_superuser)
):
    """Get status of active streaming sessions and Redis Streams health. Admin only."""
    return await session_controller.get_streaming_status(request, current_user)


@router.post("/streaming/reclaim")
async def reclaim_audio_streams(
    max_streams: int = 25, current_user: User = Depends(current_superuser)
):
    """Run the audio-stream reclaim sweep now, instead of waiting for its cron.

    This is deliberately the *same function* the ``audio_stream_reclaim`` cron job
    runs, not a parallel implementation. It replaces three buttons that between them
    called two endpoints implementing overlapping cleanup with different rules and
    misleading names — one that claimed to force-delete active streams but could
    not, and one named for stuck workers that by design refuses to touch them.

    Nothing here is a decision an operator needs to make: it is housekeeping that
    now happens on a timer. The endpoint remains so a sweep can be run on demand
    while diagnosing. Admin only.
    """
    return await reclaim_settled_audio_streams(max_streams=max_streams)


@router.get("/system/event-loop")
async def event_loop_health(current_user: User = Depends(current_superuser)):
    """Scheduling delay for every Chronicle event loop, and recent stalls.

    A blocked loop is invisible to container probes — the process is up and the
    port is open — while everything on it waits at once. Each process measures its
    own delay and publishes a snapshot; this collects them.

    ``lag_p50_ms`` on a healthy loop is a fraction of a millisecond. Sustained
    double digits mean it is saturated; ``recent_stalls`` carries the stack that
    was executing, sampled repeatedly during the stall. Admin only.
    """
    redis_client = create_async_redis()
    processes = []
    try:
        for key in await redis_client.keys(f"{SNAPSHOT_KEY_PREFIX}*"):
            raw = await redis_client.get(key)
            if raw:
                processes.append(json.loads(raw))
    finally:
        await redis_client.aclose()

    # This process publishes on the same timer as the rest, so prefer its live
    # stats over its own (up to 15s stale) snapshot.
    monitor = get_monitor()
    if monitor is not None:
        processes = [p for p in processes if p.get("process") != monitor.process]
        processes.append(monitor.stats())

    processes.sort(key=lambda p: p.get("process", ""))
    return {
        "processes": processes,
        "stalls_total": sum(p.get("stalls", 0) for p in processes),
    }


# External Service Management Endpoints (proxied to host service-manager agent)


class ServiceActionRequest(BaseModel):
    """Options for start/stop/restart of a host-managed service."""

    build: bool = False
    recreate: bool = False
    force: bool = False
    # Owning node host (from the merged service list). Omitted / local host → local
    # agent; another host → that node's agent over the Tailnet.
    node: str | None = None


class ServiceProviderRequest(BaseModel):
    """Switch the active provider (e.g. ASR model service) for a service."""

    provider: str
    build: bool = False
    # "batch" (default) switches the stt provider; "streaming" switches stt_stream.
    lane: str = "batch"
    # Owning node host (see ServiceActionRequest.node).
    node: str | None = None


class NodeUpdateRequest(BaseModel):
    """Options for updating a node's git checkout and rebuilding its services."""

    # Git ref (branch/tag/commit) to update to. Omitted → the node's current branch.
    target: str | None = None
    # Prebuilt image reference to deploy instead of building from source.
    prebuilt: str | None = None
    # Owning node host (see ServiceActionRequest.node).
    node: str | None = None


@router.get("/admin/service-deployments")
async def get_service_deployments(current_user: User = Depends(current_superuser)):
    return await system_controller._service_manager_request("GET", "/deployments")


@router.put("/admin/service-deployments")
async def put_service_deployments(
    plan: dict = Body(...), current_user: User = Depends(current_superuser)
):
    return await system_controller._service_manager_request(
        "PUT", "/deployments", plan, timeout=120
    )


@router.get("/admin/service-deployments/status")
async def get_service_deployment_status(
    current_user: User = Depends(current_superuser),
):
    return await system_controller._service_manager_request(
        "GET", "/deployments/status", timeout=120
    )


@router.get("/admin/services")
async def list_external_services(current_user: User = Depends(current_superuser)):
    """List host-managed services (ASR, TTS, speaker recognition, ...). Admin only.

    Returns available=False when no service manager agent is configured/reachable.
    """
    return await system_controller.get_external_services()


@router.get("/admin/services/operations/{operation_id}")
async def get_external_service_operation(
    operation_id: str,
    node: str | None = None,
    current_user: User = Depends(current_superuser),
):
    """Poll a long-running service start/stop/build operation. Admin only.

    ``node`` routes the poll to the agent that owns the operation (remote ops live
    on the remote node's agent).
    """
    return await system_controller.get_external_service_operation(operation_id, node)


@router.post("/admin/services/{name}/provider")
async def set_external_service_provider(
    name: str,
    body: ServiceProviderRequest,
    current_user: User = Depends(current_superuser),
):
    """Switch the active provider for a service (e.g. ASR model). Admin only."""
    return await system_controller.set_external_service_provider(name, body.dict())


@router.post("/admin/services/{name}/{action}")
async def external_service_action(
    name: str,
    action: str,
    body: ServiceActionRequest | None = None,
    current_user: User = Depends(current_superuser),
):
    """Start/stop/restart a host-managed service via the agent. Admin only."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
    return await system_controller.external_service_action(
        name, action, (body or ServiceActionRequest()).dict()
    )


@router.get("/admin/update")
async def get_node_update_status(
    node: str | None = None,
    target: str | None = None,
    current_user: User = Depends(current_superuser),
):
    """Check whether a node can be updated to a newer git ref. Admin only.

    Reports the node's current describe/commit/branch and whether ``target`` (or its
    current branch) is ahead. ``node`` selects the node (local agent forwards to a
    remote node when set). The agent fetches from origin, so this may take a few
    seconds. Returns available=False when no service manager agent is configured/reachable.
    """
    return await system_controller.get_node_update_status(node, target)


@router.post("/admin/update")
async def trigger_node_update(
    body: NodeUpdateRequest,
    current_user: User = Depends(current_superuser),
):
    """Update a node's git checkout and rebuild/restart its services. Admin only.

    Updates the node's git checkout to ``target``, rebuilds/restarts its services, and
    restarts the node agent. Updating the hub restarts the backend, causing a brief
    WebUI outage. ``node`` selects the node (local agent forwards to a remote node when
    set). Returns ``{operation}`` to poll via GET /admin/services/operations/{id}?node=.
    """
    return await system_controller.trigger_node_update(body.dict())


@router.get("/admin/remote-control")
async def get_remote_control_status(current_user: User = Depends(current_superuser)):
    """Status of the host's Claude remote-control session. Admin only.

    Returns available=False when no service manager agent is configured/reachable.
    """
    return await system_controller.get_remote_control_status()


@router.post("/admin/remote-control/{action}")
async def remote_control_action(
    action: str, current_user: User = Depends(current_superuser)
):
    """Start/stop/restart the host's Claude remote-control session. Admin only."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
    return await system_controller.remote_control_action(action)


# Memory Provider Configuration Endpoints


@router.get("/admin/memory/provider")
async def get_memory_provider(current_user: User = Depends(current_superuser)):
    """Get current memory provider configuration. Admin only."""
    return await system_controller.get_memory_provider()


@router.post("/admin/memory/provider")
async def set_memory_provider(
    provider: str = Body(..., embed=True),
    current_user: User = Depends(current_superuser),
):
    """Set memory provider and restart backend services. Admin only."""
    return await system_controller.set_memory_provider(provider)


# ── Prompt Management ──────────────────────────────────────────────────────
# Prompt editing is now handled via the LangFuse web UI at http://localhost:3002/prompts
