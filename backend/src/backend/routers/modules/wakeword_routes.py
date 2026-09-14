"""Wake-word data-collection proxy.

Thin, auth-gated proxy in front of the standalone wakeword-service's
data-collection API (its ``/streams``, ``/prime``, ``/samples*`` endpoints). The
browser never talks to the wakeword-service directly — it goes through Chronicle
auth here, and we scope streams/clips to the calling user's own ``client_id``s
(prefix = last 6 of their ObjectId), with admins seeing everything.

The flywheel this serves:
  - ``POST /api/wakeword/prime``  -> "I'll say the wake word now" on a live stream;
    the next utterance is captured as a labeled positive (false-negative mining).
  - ``GET  /api/wakeword/samples`` + ``/label`` -> review captured false-positive
    candidates, marking each true/false so they roll into the negative/positive set.
"""

import logging
import os
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from backend.auth import current_active_user, current_superuser
from backend.client_manager import owns_client_id, user_id_prefix
from backend.database import get_database
from backend.redis_factory import create_async_redis
from backend.services import voice_latency
from backend.services.plugin_service import get_plugin_router
from backend.services.wakeword.executor import open_followup_window
from backend.services.wakeword.followup import handle_dial_followup
from backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wakeword", tags=["wakeword"])

# Standalone wakeword-service, reachable by container name on chronicle-network.
WAKEWORD_SERVICE_URL = os.getenv(
    "WAKEWORD_SERVICE_URL", "http://chronicle-wakeword-service:8770"
)
SERVICE_MANAGER_URL = (os.getenv("SERVICE_MANAGER_URL") or "").rstrip("/")
SERVICE_MANAGER_TOKEN = os.getenv("SERVICE_MANAGER_TOKEN") or ""
WAKEWORD_SERVICE_NAME = os.getenv("WAKEWORD_SERVICE_NAME", "wakeword-service")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=WAKEWORD_SERVICE_URL, timeout=15.0)


def _owns(user: User, client_id: str) -> bool:
    return user.is_superuser or owns_client_id(user, client_id)


def _service_manager_ready() -> bool:
    return bool(SERVICE_MANAGER_URL and SERVICE_MANAGER_TOKEN)


def _service_manager_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_MANAGER_TOKEN}"}


def _service_manager_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=SERVICE_MANAGER_URL, timeout=20.0)


def _extract_wakeword_names(models_body: dict[str, Any]) -> list[str]:
    words = []
    for item in models_body.get("wakewords", []):
        name = item.get("name")
        if isinstance(name, str) and name:
            words.append(name)
    return words


def _word_mode(word: dict[str, Any]) -> Literal["dispatch", "collect_only", "off"]:
    if bool(word.get("disabled", False)):
        return "off"
    if bool(word.get("collect_only", False)):
        return "collect_only"
    return "dispatch"


async def _get_wakeword_models() -> dict[str, Any]:
    async with _client() as client:
        resp = await client.get("/models")
        resp.raise_for_status()
        return resp.json()


async def _set_collect_only_for_words(
    wakewords: list[str], collect_only: bool
) -> list[dict[str, Any]]:
    updated = []
    async with _client() as client:
        for wakeword in wakewords:
            resp = await client.post(
                "/collect_only",
                json={"wakeword": wakeword, "collect_only": collect_only},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
            updated.append(resp.json())
    return updated


async def _set_disabled_for_words(
    wakewords: list[str], disabled: bool
) -> list[dict[str, Any]]:
    updated = []
    async with _client() as client:
        for wakeword in wakewords:
            resp = await client.post(
                "/disabled",
                json={"wakeword": wakeword, "disabled": disabled},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
            updated.append(resp.json())
    return updated


async def _get_service_state(name: str) -> dict[str, Any] | None:
    if not _service_manager_ready():
        return None
    async with _service_manager_client() as sm_client:
        resp = await sm_client.get("/services", headers=_service_manager_headers())
        resp.raise_for_status()
    for service in resp.json().get("services", []):
        if service.get("name") == name:
            return service
    return None


async def _service_action(
    name: str, action: Literal["start", "stop"]
) -> dict[str, Any]:
    if not _service_manager_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "Service manager is not configured. "
                "Set SERVICE_MANAGER_URL and SERVICE_MANAGER_TOKEN for off-mode control."
            ),
        )
    async with _service_manager_client() as sm_client:
        resp = await sm_client.post(
            f"/services/{name}/{action}",
            headers=_service_manager_headers(),
            json={},
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except ValueError:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


class WakewordModeRequest(BaseModel):
    mode: Literal["dispatch", "collect_only", "off"]
    wakeword: str | None = None


class PrimeRequest(BaseModel):
    # Optional: when omitted, prime the caller's currently-active stream.
    client_id: str | None = None
    # Which wake word to enroll for (required for /prime; ignored by /unprime).
    wakeword: str | None = None


class ProbeRequest(BaseModel):
    client_id: str
    audio_session_id: str
    wakeword: str
    timeout_seconds: float = 15


class LabelRequest(BaseModel):
    label: str  # "wake" -> positive, "not_wake" -> negative


class CollectOnlyRequest(BaseModel):
    wakeword: str
    collect_only: bool  # True -> shadow (farm-only), False -> normal dispatch word


class VerifierEnabledRequest(BaseModel):
    wakeword: str
    enabled: bool  # True -> consult the second-stage verifier, False -> stage-1 only


class DisabledRequest(BaseModel):
    wakeword: str
    disabled: bool  # True -> fully off for this word; False -> enabled


class DialSimRequest(BaseModel):
    direction: Literal["CW", "CCW"]
    # Optional: open/refresh the follow-up window with this as the "last command"
    # before applying the dial, so you can test the whole mapping without speaking
    # a wake command first (e.g. "make the study lights warmer").
    seed_command: str | None = None
    # Session to act on. Defaults to a per-user simulation session that persists
    # across calls (the dial re-opens the window on each success).
    session_id: str | None = None


@router.post("/simulate-dial")
async def simulate_dial(
    req: DialSimRequest, current_user: User = Depends(current_superuser)
):
    """Simulate a rotary-dial detent during a follow-up window (no device needed).

    Mirrors what the HAVPE dial does live: with a follow-up window open, CW/CCW maps
    to a contextual light adjustment (warmer/cooler or brighter/dimmer, inheriting
    the room from the last command) and runs it through the real Home Assistant
    path. Pass ``seed_command`` (e.g. "make the study lights warmer") to open the
    window first so one call exercises the whole mapping end-to-end on real lights.
    """
    session_id = req.session_id or f"sim-dial-{current_user.user_id}"
    router_obj = get_plugin_router()
    if router_obj is None:
        raise HTTPException(status_code=503, detail="Plugin router not ready")

    redis_client = create_async_redis(decode_responses=True)
    try:
        if req.seed_command:
            await open_followup_window(redis_client, session_id, req.seed_command)
        result = await handle_dial_followup(
            redis_client,
            router_obj,
            user_id=current_user.user_id,
            session_id=session_id,
            client_id=f"sim-{user_id_prefix(current_user)}",
            direction=req.direction,
        )
    finally:
        await redis_client.aclose()

    return {"session_id": session_id, "direction": req.direction, **result}


@router.get("/mode")
async def get_wakeword_mode(current_user: User = Depends(current_active_user)):
    """Get wake-word mode state for mobile control center.

    Returns:
      - running/service status
      - per-word mode (dispatch / collect_only / off)
      - global_mode:
        - off: service down/unavailable
        - collect_only: all words in collect-only
        - dispatch: all words in dispatch
        - mixed: words in different modes
    """
    service_state = None
    if _service_manager_ready():
        try:
            service_state = await _get_service_state(WAKEWORD_SERVICE_NAME)
        except httpx.HTTPError as e:
            logger.warning("Failed to read service-manager state for wakeword: %s", e)

    # If service manager explicitly says stopped, trust that over probing /models.
    if service_state and service_state.get("health") == "stopped":
        return {
            "global_mode": "off",
            "running": False,
            "service": WAKEWORD_SERVICE_NAME,
            "wakewords": [],
        }

    try:
        models = await _get_wakeword_models()
    except httpx.HTTPError:
        return {
            "global_mode": "off",
            "running": False,
            "service": WAKEWORD_SERVICE_NAME,
            "wakewords": [],
        }

    wakewords = models.get("wakewords", []) or []
    modes = {_word_mode(w) for w in wakewords}
    if not wakewords:
        global_mode = "dispatch"
    elif len(modes) == 1:
        global_mode = next(iter(modes))
    else:
        global_mode = "mixed"

    return {
        "global_mode": global_mode,
        "running": True,
        "service": WAKEWORD_SERVICE_NAME,
        "wakewords": [
            {
                **w,
                "mode": _word_mode(w),
            }
            for w in wakewords
        ],
    }


@router.post("/mode")
async def set_wakeword_mode(
    req: WakewordModeRequest, current_user: User = Depends(current_superuser)
):
    """Set wake-word mode globally or for a single word.

    If ``wakeword`` is omitted:
      - off: hard off (stop wakeword service via service-manager)
      - dispatch/collect_only: apply mode to all configured wake words

    If ``wakeword`` is provided:
      - applies mode only to that word.
    """
    if req.mode == "off" and req.wakeword is None:
        action_result = await _service_action(WAKEWORD_SERVICE_NAME, "stop")
        return {
            "global_mode": "off",
            "running": False,
            "service": WAKEWORD_SERVICE_NAME,
            "action": action_result,
            "wakewords": [],
        }

    # For non-hard-off updates, ensure service is running if we can.
    service_state = await _get_service_state(WAKEWORD_SERVICE_NAME)
    if service_state and service_state.get("health") == "stopped":
        await _service_action(WAKEWORD_SERVICE_NAME, "start")

    try:
        models = await _get_wakeword_models()
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Wake-word service unavailable while setting mode: {e}",
        )

    wakewords = _extract_wakeword_names(models)
    if req.wakeword is not None:
        if req.wakeword not in wakewords:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown wake word '{req.wakeword}'. Available: {wakewords}",
            )
        target_words = [req.wakeword]
    else:
        target_words = wakewords

    if not wakewords:
        return {
            "global_mode": "dispatch",
            "running": True,
            "service": WAKEWORD_SERVICE_NAME,
            "wakewords": [],
            "message": "No wake words configured.",
        }

    if req.mode == "dispatch":
        await _set_disabled_for_words(target_words, disabled=False)
        await _set_collect_only_for_words(target_words, collect_only=False)
    elif req.mode == "collect_only":
        await _set_disabled_for_words(target_words, disabled=False)
        await _set_collect_only_for_words(target_words, collect_only=True)
    else:  # off, per-word or "soft off all"
        await _set_collect_only_for_words(target_words, collect_only=False)
        await _set_disabled_for_words(target_words, disabled=True)

    refreshed = await _get_wakeword_models()
    refreshed_words = refreshed.get("wakewords", []) or []
    modes = {_word_mode(w) for w in refreshed_words}
    if not refreshed_words:
        global_mode = "dispatch"
    elif len(modes) == 1:
        global_mode = next(iter(modes))
    else:
        global_mode = "mixed"

    return {
        "global_mode": global_mode,
        "running": True,
        "service": WAKEWORD_SERVICE_NAME,
        "wakewords": [
            {
                **w,
                "mode": _word_mode(w),
            }
            for w in refreshed_words
        ],
    }


@router.get("/models")
async def list_models(current_user: User = Depends(current_active_user)):
    """Wake-word models the service has on disk (for the acoustic-condition picker)."""
    async with _client() as client:
        try:
            resp = await client.get("/models")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/collect_only")
async def set_collect_only(
    req: CollectOnlyRequest, current_user: User = Depends(current_superuser)
):
    """Toggle a wake word's collect-only (shadow) mode. Admin only — it changes the
    detector's global behavior for every stream, so it isn't user-scoped like the
    prime/label flow. The change takes effect live and persists across restarts.
    """
    async with _client() as client:
        try:
            resp = await client.post(
                "/collect_only",
                json={"wakeword": req.wakeword, "collect_only": req.collect_only},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/verifier_enabled")
async def set_verifier_enabled(
    req: VerifierEnabledRequest, current_user: User = Depends(current_superuser)
):
    """Toggle a wake word's second-stage verifier on/off. Admin only — like
    collect-only it changes detector behavior for every stream. The verifier stays
    loaded; disabling falls back to the stage-1 model alone. Effective live and
    persisted across restarts.
    """
    async with _client() as client:
        try:
            resp = await client.post(
                "/verifier_enabled",
                json={"wakeword": req.wakeword, "enabled": req.enabled},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/disabled")
async def set_disabled(
    req: DisabledRequest, current_user: User = Depends(current_superuser)
):
    """Toggle a wake word fully off/on. Admin only."""
    async with _client() as client:
        try:
            resp = await client.post(
                "/disabled",
                json={"wakeword": req.wakeword, "disabled": req.disabled},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/streams")
async def list_streams(current_user: User = Depends(current_active_user)):
    """Active audio streams the caller may prime (their own; all for admins)."""
    async with _client() as client:
        try:
            resp = await client.get("/streams")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    streams = resp.json().get("streams", [])
    return {
        "streams": [s for s in streams if _owns(current_user, s.get("client_id", ""))]
    }


@router.post("/prime")
async def prime(req: PrimeRequest, current_user: User = Depends(current_active_user)):
    """Arm a one-shot positive capture of ``wakeword`` on a live stream.

    With no ``client_id`` we resolve the caller's active stream (preferring the
    browser recorder), so the Live Record button is a single call. ``wakeword``
    declares which word is being enrolled (clean per-word positive collection).
    """
    if not req.wakeword:
        raise HTTPException(status_code=400, detail="wakeword is required to prime")
    async with _client() as client:
        try:
            client_id = req.client_id
            if client_id is None:
                streams_resp = await client.get("/streams")
                streams_resp.raise_for_status()
                owned = [
                    s.get("client_id", "")
                    for s in streams_resp.json().get("streams", [])
                    if _owns(current_user, s.get("client_id", ""))
                ]
                if not owned:
                    raise HTTPException(
                        status_code=404,
                        detail="No active stream to prime — start recording first.",
                    )
                # Prefer the browser recorder; else the first owned stream.
                client_id = next((c for c in owned if "recorder" in c), owned[0])
            elif not _owns(current_user, client_id):
                raise HTTPException(status_code=403, detail="Not your stream")

            resp = await client.post(
                "/prime", json={"client_id": client_id, "wakeword": req.wakeword}
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/unprime")
async def unprime(req: PrimeRequest, current_user: User = Depends(current_active_user)):
    """Manually end an in-progress prime capture (the Lab 'stop' button).

    Mirrors :func:`prime`'s stream resolution/ownership so the button can call it
    with the same (optional) ``client_id``.
    """
    async with _client() as client:
        try:
            client_id = req.client_id
            if client_id is None:
                streams_resp = await client.get("/streams")
                streams_resp.raise_for_status()
                owned = [
                    s.get("client_id", "")
                    for s in streams_resp.json().get("streams", [])
                    if _owns(current_user, s.get("client_id", ""))
                ]
                if not owned:
                    raise HTTPException(
                        status_code=404, detail="No active stream to stop."
                    )
                client_id = next((c for c in owned if "recorder" in c), owned[0])
            elif not _owns(current_user, client_id):
                raise HTTPException(status_code=403, detail="Not your stream")

            resp = await client.post("/unprime", json={"client_id": client_id})
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/probes", status_code=201)
async def start_probe(
    req: ProbeRequest, current_user: User = Depends(current_active_user)
):
    """Start a caller-owned production detector probe with no plugin dispatch."""
    if not _owns(current_user, req.client_id):
        raise HTTPException(status_code=403, detail="Not your stream")
    async with _client() as client:
        try:
            resp = await client.post(
                "/probes",
                json={
                    "client_id": req.client_id,
                    "audio_session_id": req.audio_session_id,
                    "wakeword": req.wakeword,
                    "timeout_seconds": req.timeout_seconds,
                },
            )
            if resp.status_code in (400, 404, 409):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {exc}"
            ) from exc
    return resp.json()


async def _owned_probe(
    client: httpx.AsyncClient, probe_id: str, current_user: User
) -> dict[str, Any]:
    resp = await client.get(f"/probes/{probe_id}")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=resp.json().get("detail"))
    resp.raise_for_status()
    probe = resp.json()
    if not _owns(current_user, probe.get("client_id", "")):
        raise HTTPException(status_code=403, detail="Not your wake probe")
    return probe


@router.get("/probes/{probe_id}")
async def get_probe(probe_id: str, current_user: User = Depends(current_active_user)):
    async with _client() as client:
        try:
            return await _owned_probe(client, probe_id, current_user)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {exc}"
            ) from exc


@router.delete("/probes/{probe_id}")
async def cancel_probe(
    probe_id: str, current_user: User = Depends(current_active_user)
):
    async with _client() as client:
        try:
            await _owned_probe(client, probe_id, current_user)
            resp = await client.delete(f"/probes/{probe_id}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=resp.json().get("detail"))
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {exc}"
            ) from exc


@router.get("/samples")
async def list_samples(
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """List a wake word's captured clips in a bucket, scoped to the caller's streams."""
    async with _client() as client:
        try:
            resp = await client.get(
                "/samples", params={"wakeword": wakeword, "bucket": bucket}
            )
            if resp.status_code == 400:
                raise HTTPException(status_code=400, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    body = resp.json()
    body["samples"] = [
        s
        for s in body.get("samples", [])
        if _owns(current_user, s.get("client_id", ""))
    ]
    return body


@router.get("/samples/stats")
async def sample_stats(current_user: User = Depends(current_active_user)):
    """Per-bucket clip counts for the dashboard."""
    async with _client() as client:
        try:
            resp = await client.get("/samples/stats")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/dedupe")
async def dedupe_samples(
    wakeword: str = Query(...), current_user: User = Depends(current_active_user)
):
    """Remove exact-duplicate clips within a wake word (keeps one per group)."""
    async with _client() as client:
        try:
            resp = await client.post("/samples/dedupe", params={"wakeword": wakeword})
            if resp.status_code == 400:
                raise HTTPException(status_code=400, detail=resp.json().get("detail"))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/samples/{clip_id}/audio")
async def sample_audio(clip_id: str, current_user: User = Depends(current_active_user)):
    """Stream a clip's WAV bytes for in-browser playback."""
    async with _client() as client:
        try:
            resp = await client.get(f"/samples/{clip_id}/audio")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="clip not found")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return Response(content=resp.content, media_type="audio/wav")


@router.post("/samples/{clip_id}/label")
async def label_sample(
    clip_id: str, req: LabelRequest, current_user: User = Depends(current_active_user)
):
    """Apply a review label (wake/not_wake), moving the clip into positive/negative."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/label", json={"label": req.label}
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/{clip_id}/move")
async def move_sample(
    clip_id: str,
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """Move a clip to a different wake word's bucket (default pending)."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/move",
                params={"wakeword": wakeword, "bucket": bucket},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.post("/samples/{clip_id}/copy")
async def copy_sample(
    clip_id: str,
    wakeword: str = Query(...),
    bucket: str = Query("pending"),
    current_user: User = Depends(current_active_user),
):
    """Copy a clip into another wake word's bucket (source stays) — shared FP fan-out."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/samples/{clip_id}/copy",
                params={"wakeword": wakeword, "bucket": bucket},
            )
            if resp.status_code in (400, 404):
                raise HTTPException(
                    status_code=resp.status_code, detail=resp.json().get("detail")
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.delete("/samples/{clip_id}")
async def delete_sample(
    clip_id: str, current_user: User = Depends(current_active_user)
):
    """Delete a clip."""
    async with _client() as client:
        try:
            resp = await client.delete(f"/samples/{clip_id}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="clip not found")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503, detail=f"Wake-word service unreachable: {e}"
            )
    return resp.json()


@router.get("/latency")
async def voice_interaction_latency(
    limit: int = Query(20, ge=1, le=100),
    client_id: str | None = Query(None),
    current_user: User = Depends(current_active_user),
):
    """Recent voice turns, including incomplete/failed traces, owned by this user."""
    return await voice_latency.recent_voice_reports(
        get_database(), user_id=str(current_user.id), client_id=client_id, limit=limit
    )
