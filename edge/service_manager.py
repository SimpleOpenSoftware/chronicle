"""
Node agent — control + advertise a Chronicle node, from the WebUI.

A small host-side HTTP API that wraps services.py (the docker compose
orchestrator). Runs natively on the host — it MUST, because docker compose needs
host bind-mount paths and (on Docker Desktop/WSL2) a container can't bind the
Tailscale interface to advertise. One agent per machine.

It does two jobs that were previously two separate native processes:
  1. CONTROL   — start/stop/restart services + switch ASR/TTS providers (the
                 backend proxies the WebUI System page here via SERVICE_MANAGER_URL).
  2. ADVERTISE — announce this node's services (and itself, as ``chronicle-node``)
                 on the Tailnet via minidisc, so other nodes/the backend discover
                 them. This folds in the old standalone discovery agent.

It also exposes node identity (/node) and a live cluster view (/cluster).

Launched by services.py (any ./services start --all) with:
  SERVICE_MANAGER_TOKEN  — shared secret, required (auto-generated into
                           backend/.env on first start)
  SERVICE_MANAGER_PORT   — default 8775
  SERVICE_MANAGER_HOST   — default 0.0.0.0 (token-authed; must be reachable
                           from the backend container via host.docker.internal)

All endpoints except /health require "Authorization: Bearer <token>".
Compose operations run in a background thread (builds can take minutes);
POST endpoints return 202 with an operation id to poll. Tailnet advertising runs
in its own daemon thread so a bind failure never blocks the control API.
"""

import io
import ipaddress
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import uvicorn
from chronicle_setup import detect_cuda_version, detect_tailscale_info
from dotenv import dotenv_values, set_key
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from rich.console import Console

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Only genuinely repo-root modules belong below the sys.path insert; anything
# installed (chronicle_setup) imports normally above, or isort reclassifies this
# block differently depending on which files it is handed.
import clients  # noqa: E402  (repo-root clients.py — native client components)
import discovery  # noqa: E402  (repo-root discovery.py)
import services  # noqa: E402  (repo-root services.py)
import status  # noqa: E402  (repo-root status.py — restart-count helper)
import updates  # noqa: E402  (repo-root updates.py — version + self-update)
from service_operations import OperationRequest  # noqa: E402
from service_operations import OperationLock, diagnostics, execute, redact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [service-manager] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("SERVICE_MANAGER_TOKEN", "")
PORT = int(os.environ.get("SERVICE_MANAGER_PORT", "8775"))
HOST = os.environ.get("SERVICE_MANAGER_HOST", "0.0.0.0")

# Cluster control: trust requests that arrive from a Tailscale-range source IP even
# without the bearer token, so the hub can control this node's services without
# sharing tokens across the cluster. Tailnet IPs are only routable between your own
# tailnet peers, so this scopes "tokenless" control to trusted devices. Set
# SERVICE_MANAGER_TRUST_TAILNET=0 to require the token from everyone.
TRUST_TAILNET = os.environ.get("SERVICE_MANAGER_TRUST_TAILNET", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Tailscale's address ranges: IPv4 CGNAT 100.64.0.0/10 and the ULA IPv6 block.
_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

VALID_ACTIONS = ("start", "stop", "restart")

# Retained minidisc registry handle — advertising stops if this is GC'd, so it
# must live for the process lifetime. Set by the advertising daemon thread.
_advertise_registry = None
_advertise_lock = threading.Lock()
# How often the advertising thread refreshes live state (health/running) into the
# minidisc labels. advertise_service is keyed by port, so a refresh replaces the
# entry in place rather than duplicating it.
_ADVERTISE_REFRESH_SECS = int(os.environ.get("ADVERTISE_REFRESH_SECS", "30"))

# Provider-switchable services: service name → (env file key, provider→compose map)
_PROVIDER_ENV_KEYS = {
    "asr-services": ("ASR_PROVIDER", services.ASR_PROVIDER_TO_SERVICE),
    "tts": ("TTS_PROVIDER", services._TTS_PROVIDER_TO_SERVICE),
}

app = FastAPI(title="Chronicle Service Manager", docs_url=None, redoc_url=None)
_bearer = HTTPBearer(auto_error=False)


def _is_tailnet_ip(host: str | None) -> bool:
    """Whether host is a Tailscale-range address (the request came from a tailnet peer)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TAILNET_NETS)


def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    # Local imports let token checks run after repository path initialization.
    import secrets

    from deployment_guard import control_token

    shared_token = control_token(REPO_ROOT)
    if (
        shared_token
        and credentials is not None
        and secrets.compare_digest(credentials.credentials, shared_token)
    ):
        return
    # Valid bearer token always passes (local backend / docker-bridge callers).
    if TOKEN and credentials is not None and credentials.credentials == TOKEN:
        return
    # Otherwise, trust a tailnet peer (the hub controlling this node over the Tailnet).
    if TRUST_TAILNET and _is_tailnet_ip(
        request.client.host if request.client else None
    ):
        return
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Agent has no token configured")
    raise HTTPException(status_code=401, detail="Invalid or missing token")


# ── Operations: one compose operation at a time, polled by id ────────────────

_ops_lock = threading.Lock()  # guards _operations dict
_busy_lock = OperationLock()  # serializes compose operations
_operations: dict[str, dict] = {}
_MAX_OPERATIONS = 50


def _record_operation(service: str, action: str) -> dict:
    op = {
        "id": uuid.uuid4().hex[:12],
        "service": service,
        "action": action,
        "status": "running",
        "ok": None,
        "log": "",
        "phase": "",  # human-readable progress step, updated as the op runs
        "started_at": time.time(),
        "finished_at": None,
    }
    with _ops_lock:
        _operations[op["id"]] = op
        while len(_operations) > _MAX_OPERATIONS:
            oldest = min(_operations.values(), key=lambda o: o["started_at"])
            if oldest["status"] == "running":
                break
            del _operations[oldest["id"]]
    return op


def _report_operation_event(op: dict) -> None:
    """Append a service-control operation to the backend's rolling event ledger."""
    token = os.environ.get("SYSTEM_EVENT_INGEST_TOKEN") or TOKEN
    if not token:
        op["audit_warning"] = "System Events ingest token is not configured"
        logger.warning(
            "Operation %s/%s was not added to the system-event ledger: no ingest token",
            op["service"],
            op["action"],
        )
        return

    status = op["status"]
    service = op["service"]
    action = op["action"]
    operation_id = op["id"]
    metadata = {
        "event_type": "service_operation",
        "operation_id": operation_id,
        "node": platform.node(),
        "service": service,
        "action": action,
        "status": status,
        "ok": op["ok"],
        "started_at": op["started_at"],
        "finished_at": op["finished_at"],
        "phase": op["phase"],
    }
    if "results" in op:
        metadata["results"] = op["results"]
    detail = op["log"].strip() or None
    payload = {
        "severity": "error" if status == "failed" else "info",
        "category": "service",
        "source": f"service-manager:{platform.node()}",
        "title": f"{action.capitalize()} {service} {status} ({operation_id})",
        "detail": detail,
        "metadata": metadata,
    }
    try:
        response = requests.post(
            f"{_BACKEND_URL}/api/admin/system-events/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        op["audit_warning"] = "System Events unavailable; inspect node-manager logs"
        logger.warning(
            "Operation %s/%s could not be added to the system-event ledger: %s",
            service,
            action,
            exc,
        )


def _run_operation(op: dict, fn, guard_activation=True):
    """Run a compose operation in a thread, capturing services.py console output.

    ``fn`` is called with the op dict so it can publish progress via
    ``op["phase"]`` (polled by the WebUI).
    """

    def _go():
        buf = io.StringIO()
        original_console = services.console
        services.console = Console(file=buf, force_terminal=False, width=120)
        try:
            if guard_activation and (
                op["action"] == "start"
                or op["action"] == "restart"
                or op["action"].startswith("provider:")
            ):
                # Local import avoids the manager and placement router import cycle.
                from deployment_guard import activation

                with activation(REPO_ROOT, op["service"]):
                    ok = fn(op)
            else:
                ok = fn(op)
            outcome = {"ok": bool(ok), "status": "done" if ok else "failed"}
        except Exception as e:
            logger.exception("Operation %s/%s crashed", op["service"], op["action"])
            outcome = {"ok": False, "status": "failed"}
            buf.write(f"\nException: {e}\n")
        finally:
            services.console = original_console
            completion = {
                **op,
                **outcome,
                "log": redact(buf.getvalue()[-8000:]),
                "finished_at": time.time(),
            }
            try:
                _report_operation_event(completion)
            finally:
                _busy_lock.release()
                op.update(completion)

            logger.info(
                "Operation %s %s → %s", op["action"], op["service"], op["status"]
            )

    threading.Thread(target=_go, daemon=True).start()


def _start_operation(service: str, action: str, fn, guard_activation=True) -> dict:
    if not _busy_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="Another operation is already running"
        )
    op = _record_operation(service, action)
    logger.info("Operation %s %s started (%s)", action, service, op["id"])
    _report_operation_event(op)
    _run_operation(op, fn, guard_activation=guard_activation)
    return op


# ── Service info ─────────────────────────────────────────────────────────────


def _provider_info(service_name: str) -> dict | None:
    if service_name not in _PROVIDER_ENV_KEYS:
        return None
    env_key, provider_map = _PROVIDER_ENV_KEYS[service_name]
    env_path = REPO_ROOT / services.SERVICES[service_name]["path"] / ".env"
    env_values = dotenv_values(env_path) if env_path.exists() else {}
    current = (env_values.get(env_key) or "").strip("'\"")
    info = {
        "env_key": env_key,
        "current": current,
        "available": [
            {
                "key": key,
                "label": (
                    services._ASR_PROVIDER_LABELS.get(key, key)
                    if service_name == "asr-services"
                    else key
                ),
                # Whether this provider runs a local container. Switching to/from a
                # local provider is "heavy" (start/stop, possible model download);
                # the UI uses this to decide whether to gate the change behind Apply.
                "local": bool(provider_map.get(key)),
            }
            for key in provider_map
        ],
    }
    if service_name == "asr-services":
        # The active streaming provider is whatever defaults.stt_stream resolves to
        # (cloud providers leave STREAMING_ASR_PROVIDER empty), not just the env var.
        info["streaming_current"] = services.active_streaming_asr_provider()
        info["streaming_available"] = [
            {"key": key, "label": opt["label"], "local": bool(opt["service"])}
            for key, opt in services.STREAMING_ASR_PROVIDER_OPTIONS.items()
        ]
    return info


def _containers_running(name: str) -> bool:
    """Whether any compose container for this service is currently running.

    Uses the engine-aware status helper (docker compose ps vs podman ps by compose
    project label) so it works under both docker and podman-compose.
    """
    service_path = REPO_ROOT / services.SERVICES[name]["path"]
    try:
        return any(
            c.get("state") == "running" for c in services.compose_ps_json(service_path)
        )
    except Exception:
        return False


# A crash-looping container keeps flickering "Up <1s" so its health endpoint never
# answers — without this it would forever read "starting". Past this many container
# restarts we call it "unhealthy" instead. RestartCount is lifetime-cumulative but we
# only consult it while the endpoint is down AND containers are up, so a genuinely
# booting service (RestartCount stays 0 — it hasn't crashed) still reads "starting".
RESTART_LOOP_THRESHOLD = 3


def _max_restart_count(name: str) -> int:
    """Highest container RestartCount for this service (0 if unknown).

    Engine-aware via status.get_restart_counts (docker/podman inspect RestartCount).
    """
    service_path = REPO_ROOT / services.SERVICES[name]["path"]
    try:
        containers = services.compose_ps_json(service_path)
    except Exception:
        return 0
    names = [c["name"] for c in containers if c.get("name")]
    if not names:
        return 0
    counts = status.get_restart_counts(names)
    return max(counts.values(), default=0)


def _effective_health(name: str) -> tuple[str, str]:
    """(health, detail) with the container-state overrides the raw endpoint check
    can't see: distinguish a booting service from a crash loop.

    "stopped" from check_service_health only means the endpoint isn't answering. If
    containers are up we look at the restart count: a fresh boot (GPU model loads take
    minutes) still has RestartCount 0 → "starting"; a crash loop has climbed past the
    threshold → "unhealthy".
    """
    health, detail = services.check_service_health(name)
    if health == "stopped" and _containers_running(name):
        restarts = _max_restart_count(name)
        if restarts >= RESTART_LOOP_THRESHOLD:
            return (
                "unhealthy",
                f"crash loop: containers restarted {restarts}× without becoming healthy",
            )
        return ("starting", "containers up, waiting for health endpoint")
    return health, detail


def _service_entry(name: str, public_host: str | None = None, snapshot=None) -> dict:
    snapshot = snapshot or status.collect_services([name])[name]
    health = (
        "healthy"
        if snapshot["ready"]
        else (
            snapshot["container_status"]
            if snapshot["container_status"] != "running"
            else snapshot["health"]
        )
    )
    detail = snapshot["detail"]
    return {
        **snapshot,
        "name": name,
        # Label and ports are resolved from the service's .env, not the static
        # SERVICES entry: asr-services is one compose hosting many providers, so
        # its description/ports name only whichever provider was hardcoded.
        "description": services.service_display_label(name),
        "ports": services.service_display_ports(name),
        "enabled": services.check_service_enabled(name),
        "health": health,
        "health_detail": detail,
        "provider": _provider_info(name),
        "ui_url": services.service_ui_url(name, public_host or os.uname().nodename),
    }


# ── Cluster routing: control OTHER nodes' services via their agents ───────────
# The backend (in a container) can't present a Tailnet source IP, so it proxies
# everything to THIS agent. This agent runs natively on the host with a real
# Tailnet identity, so host→host calls to peer agents are tailnet-trusted — that's
# why cross-node merge + forwarding lives here, not in the backend.

_REMOTE_TIMEOUT = 10.0


def _self_host() -> str:
    return os.uname().nodename


def _remote_node_agents() -> list[dict]:
    """Other nodes' agents on the Tailnet as ``[{host, url}]`` (excludes self).

    Each full node advertises itself as ``chronicle-node`` on its agent port, so a
    discovered node entry is exactly that node's control URL.
    """
    self_host = _self_host()
    agents: list[dict] = []
    try:
        for svc in discovery.list_all_services() or []:
            labels = svc.get("labels", {})
            if labels.get("type") != "node":
                continue
            host = labels.get("host")
            address = svc.get("address")
            port = svc.get("port")
            if host and address and port and host != self_host:
                agents.append({"host": host, "url": f"http://{address}:{port}"})
    except Exception:  # noqa: BLE001 - discovery is best-effort
        logger.warning("remote node discovery failed", exc_info=True)
    return agents


def _remote_services(agent: dict) -> list[dict]:
    """A peer node's own services (scope=local avoids re-merge), tagged with its host."""
    try:
        resp = requests.get(
            f"{agent['url']}/services",
            params={"scope": "local"},
            timeout=_REMOTE_TIMEOUT,
        )
        if resp.ok:
            return [
                {**s, "node": agent["host"], "remote": True}
                for s in resp.json().get("services", [])
            ]
    except requests.RequestException:
        logger.debug("remote /services failed for %s", agent.get("host"))
    return []


def _remote_request(
    node: str,
    method: str,
    path: str,
    json_body: dict | None = None,
    timeout: float = _REMOTE_TIMEOUT,
):
    """Forward a control request to a peer node's agent (host→host, tailnet-trusted)."""
    agent = next((a for a in _remote_node_agents() if a["host"] == node), None)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"No node agent found for host {node!r}"
        )
    try:
        resp = requests.request(
            method, f"{agent['url']}{path}", json=json_body, timeout=timeout
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Node {node} unreachable: {e}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


# ── Tailnet advertising (folds in the old standalone discovery agent) ────────


def _node_labels() -> dict:
    """minidisc labels for this node's self-advertisement (``chronicle-node``).

    Stays consistent with the existing ``{host, type}`` schema (the WebUI Network
    page groups discovered services by ``labels.host`` and badges ``type=='edge'``).
    ``type='node'`` distinguishes the orchestrator from advertise-only edge nodes.
    All values must be strings (minidisc labels are str→str).
    """
    return {
        "host": os.uname().nodename,
        "type": "node",
        "arch": platform.machine(),
        "gpu": "1" if shutil.which("nvidia-smi") else "0",
        # Code version rides the advertisement so the hub sees cluster-wide
        # version drift without polling every agent.
        "version": updates.repo_version()["describe"],
    }


def _service_labels(svc_name: str, hostname: str) -> dict:
    """Per-service minidisc labels carrying LIVE state, not just 'enabled'.

    Reflects this node's own view — whether containers are up and the health
    verdict — so a consumer learns what's actually live from the advertisement
    itself. ``host``/``type`` are kept for the Network page (it groups by host).
    All values are strings (minidisc labels are str→str).
    """
    health, _detail = _effective_health(svc_name)
    return {
        "host": hostname,
        "type": "service",
        "service": svc_name,
        "enabled": "1",
        "running": "1" if _containers_running(svc_name) else "0",
        "health": health,  # healthy | partial | unhealthy | starting | stopped
    }


def _build_advertise_entries(triples) -> list:
    """(name, port, labels) for every enabled service + this node, with live state.

    ``triples`` are the ``(discovery_name, port, display_label)`` from
    ``services._get_advertised_services()`` (passed in to avoid recomputing).
    """
    hostname = os.uname().nodename
    disc_to_svc = {disc: svc for svc, disc in services._DISCOVERY_NAMES.items()}
    entries = [
        (
            disc_name,
            port,
            _service_labels(disc_to_svc.get(disc_name, disc_name), hostname),
        )
        for disc_name, port, _display in triples
    ]
    entries.append(("chronicle-node", PORT, _node_labels()))
    return entries


def _advertise_worker():
    """Advertise this node's services + itself on the Tailnet, refreshing live
    state on a timer.

    Runs in a daemon thread so a minidisc bind failure (common on Docker
    Desktop/WSL2) never blocks the control API. ``advertise_service`` is keyed by
    port, so re-advertising updates labels in place (no duplicates);
    ``unlist_service`` drops services that are no longer enabled.
    """
    global _advertise_registry
    registry = None
    advertised_ports: set = set()

    while True:
        try:
            triples = services._get_advertised_services()
        except Exception:
            logger.exception("Failed to compute advertised services")
            triples = []
        entries = _build_advertise_entries(triples)

        # Local manifest the backend Network page reads — written regardless of
        # whether minidisc binds, so the "advertising" list shows even where the
        # Tailscale interface isn't reachable.
        try:
            services._write_advertised_services(triples)
        except Exception:
            logger.exception("Failed to write advertised-services.json")

        if registry is None:
            registry = discovery.start_advertising(entries)
            if registry is not None:
                with _advertise_lock:
                    _advertise_registry = registry
                advertised_ports = {port for _n, port, _l in entries}
                logger.info("Advertising %d entr(ies) on the Tailnet", len(entries))
            else:
                logger.warning(
                    "Tailnet advertising unavailable (minidisc/tailscale not reachable); "
                    "retrying in %ds",
                    _ADVERTISE_REFRESH_SECS,
                )
        else:
            current = {port for _n, port, _l in entries}
            for name, port, labels in entries:
                try:
                    registry.advertise_service(port, name, labels)
                except Exception as e:  # noqa: BLE001 - refresh is best-effort
                    logger.debug("re-advertise %s failed (non-fatal): %s", name, e)
            for stale in advertised_ports - current:
                try:
                    registry.unlist_service(stale)
                except Exception as e:  # noqa: BLE001
                    logger.debug("unlist port %d failed (non-fatal): %s", stale, e)
            advertised_ports = current

        time.sleep(_ADVERTISE_REFRESH_SECS)


def _start_advertising_thread():
    threading.Thread(target=_advertise_worker, daemon=True, name="advertise").start()


# ── Host watchdog ────────────────────────────────────────────────────────────
#
# Host-level faults are invisible to every container health check: the containers
# stay up and Mongo/Redis stay green while the deployment is unusable from
# outside. This agent runs natively on the host, so it is the only component that
# can see them — and, being under `Restart=always`, the only one that keeps
# looking while nobody is home.

_WATCHDOG_DEFAULTS = {
    "enabled": True,
    "interval_secs": 300,
    "auto_repair": True,
    "failures_before_repair": 2,
    "min_repair_interval_secs": 1800,
}

# The backend is normally co-located; a service-only node has none, in which case
# reporting is skipped and the checks still run and repair locally.
_BACKEND_URL = os.environ.get("CHRONICLE_BACKEND_URL", "http://127.0.0.1:8000")

# check id → {"consecutive": int, "last_repair": float, "reported": bool}
_watchdog_state: dict = {}
_last_check_results: list = []


def _watchdog_config() -> dict:
    cfg = (services.load_config_yml() or {}).get("watchdog") or {}
    merged = dict(_WATCHDOG_DEFAULTS)
    for key in _WATCHDOG_DEFAULTS:
        if key in cfg:
            merged[key] = cfg[key]
    merged["checks"] = cfg.get("checks") or {}
    return merged


def _report_event(severity, title, detail, incident_key, resolves=False) -> None:
    """Push a finding into the backend's system-event ledger.

    Best-effort by design: a host fault often *is* a connectivity fault, so a
    failure to report must never stop the watchdog from repairing.
    """
    token = os.environ.get("SYSTEM_EVENT_INGEST_TOKEN") or TOKEN
    if not token:
        return
    try:
        requests.post(
            f"{_BACKEND_URL}/api/admin/system-events/ingest",
            json={
                "severity": severity,
                "category": "service",
                "source": "host-watchdog",
                "title": title,
                "detail": detail,
                "incident_key": incident_key,
                "resolves_incident": resolves,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("watchdog: could not report %r: %s", title, exc)


def run_host_checks() -> list:
    """Run the host checks, caching the results for the /checks route."""
    global _last_check_results
    _last_check_results = services.run_all_checks(services.build_check_context())
    return _last_check_results


def _watchdog_cycle(cfg: dict) -> None:
    try:
        reconcile_service_placement()
    except Exception as exc:
        logger.warning(
            "Placement reconciliation unavailable; leaving running instances unchanged: %s",
            exc,
        )
    enabled_checks = cfg.get("checks") or {}
    for result in run_host_checks():
        if enabled_checks.get(result.id) is False:
            continue
        state = _watchdog_state.setdefault(
            result.id, {"consecutive": 0, "last_repair": 0.0, "reported": False}
        )

        if result.status not in (services.FAIL, services.WARN):
            if state["reported"]:
                _report_event(
                    "info",
                    f"Resolved: {result.title}",
                    result.detail,
                    incident_key=f"host-check:{result.id}",
                    resolves=True,
                )
            state["consecutive"] = 0
            state["reported"] = False
            continue

        state["consecutive"] += 1
        if not state["reported"]:
            _report_event(
                "error" if result.status == services.FAIL else "warning",
                result.title,
                f"{result.detail}\n\n{result.remedy}".strip(),
                incident_key=f"host-check:{result.id}",
            )
            state["reported"] = True

        if result.status != services.FAIL or result.repair is None:
            continue
        if not cfg["auto_repair"]:
            continue
        # Require consecutive failures so one flaky probe can't restart anything,
        # and rate-limit so a genuinely broken host is never put in a restart loop.
        if state["consecutive"] < cfg["failures_before_repair"]:
            continue
        if time.time() - state["last_repair"] < cfg["min_repair_interval_secs"]:
            continue

        state["last_repair"] = time.time()
        logger.info("watchdog: repairing %s", result.id)
        # Serialise against user-initiated compose operations.
        with _busy_lock:
            try:
                repaired = bool(result.repair())
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a repair must never crash the loop
                logger.warning("watchdog: repair of %s raised: %s", result.id, exc)
                repaired = False
        _report_event(
            "info" if repaired else "error",
            f"{'Repaired' if repaired else 'Repair failed'}: {result.title}",
            result.detail,
            incident_key=f"host-check:{result.id}",
            resolves=repaired,
        )
        if repaired:
            state["consecutive"] = 0
            state["reported"] = False


def _watchdog_worker() -> None:
    while True:
        cfg = _watchdog_config()
        if cfg["enabled"]:
            try:
                _watchdog_cycle(cfg)
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any fault
                logger.warning("watchdog: cycle failed: %s", exc)
        time.sleep(max(60, int(cfg["interval_secs"])))


def _start_watchdog_thread():
    threading.Thread(target=_watchdog_worker, daemon=True, name="watchdog").start()


# ── Routes ───────────────────────────────────────────────────────────────────


class ActionBody(BaseModel):
    build: bool = False
    recreate: bool = False
    force: bool = False
    # Owning node host. When set and != this node, the request is forwarded to that
    # node's agent (host→host, tailnet-trusted). Omitted / self → handled locally.
    node: str | None = None


class ProviderBody(BaseModel):
    provider: str
    build: bool = False
    # "batch" switches ASR_PROVIDER (the stt model); "streaming" switches the
    # STREAMING_ASR_PROVIDER (the stt_stream model). Only meaningful for asr-services.
    lane: str = "batch"
    # Owning node host (see ActionBody.node).
    node: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "host": os.uname().nodename, "agent_port": PORT}


@app.get("/checks", dependencies=[Depends(require_token)])
def host_checks(refresh: bool = False):
    """Host-level check results for this node.

    Serves the watchdog's last cycle by default, since the probes shell out to the
    container engine and openssl and are too slow to run on every request. Pass
    ``?refresh=1`` to force a fresh run.
    """
    results = (
        run_host_checks()
        if (refresh or not _last_check_results)
        else _last_check_results
    )
    return {
        "status": services.worst_status(results),
        "checks": [r.as_dict() for r in results],
    }


@app.get("/node", dependencies=[Depends(require_token)])
def node_info():
    """This node's identity + hardware capabilities + per-service status.

    Used (now/by future cluster control) to place services sensibly — e.g. don't
    offer GPU-only services on an arm64 box with no NVIDIA GPU.
    """
    dns, ip = detect_tailscale_info()
    public_host = dns or ip or _self_host()
    return {
        "host": os.uname().nodename,
        "tailscale": {"dns": dns, "ip": ip},
        "arch": platform.machine(),
        "gpu": {
            # detect_cuda_version returns "" here when nvidia-smi is absent/unparseable
            "cuda": detect_cuda_version(default=""),
            "nvidia_smi": shutil.which("nvidia-smi") is not None,
        },
        "agent_port": PORT,
        "version": updates.repo_version(),
        "services": [_service_entry(name, public_host) for name in services.SERVICES],
        # Native client components (tray, ScreenPipe collector — clients.py):
        # user units, not containers, so they ride the same update flow but are
        # controlled via /clients/{name}/{action} instead of compose.
        "clients": [
            clients.component_status(name) for name in clients.CLIENT_COMPONENTS
        ],
    }


@app.get("/cluster", dependencies=[Depends(require_token)])
def cluster():
    """Live view of all chronicle-* services advertised on the Tailnet."""
    return {"services": discovery.list_all_services()}


@app.get("/services", dependencies=[Depends(require_token)])
def list_services(scope: str = "cluster"):
    """This node's services, tagged with node host.

    Default ``scope=cluster`` also folds in peer nodes' services (fanned out to their
    agents in parallel, best-effort). ``scope=local`` returns only this node — used by
    peers' fan-out so the merge doesn't recurse.
    """
    with _ops_lock:
        running = [o for o in _operations.values() if o["status"] == "running"]
    self_host = _self_host()
    dns, ip = detect_tailscale_info()
    public_host = dns or ip or self_host
    snapshots = status.collect_services()
    local = [
        {
            **_service_entry(name, public_host, snapshots[name]),
            "node": self_host,
            "remote": False,
        }
        for name in services.SERVICES
    ]
    operation = running[0] if running else None
    if scope == "local":
        return {"services": local, "operation": operation}

    agents = _remote_node_agents()
    remote: list[dict] = []
    if agents:
        with ThreadPoolExecutor(max_workers=min(8, len(agents))) as pool:
            for svc_list in pool.map(_remote_services, agents):
                remote.extend(svc_list)
    return {"services": local + remote, "operation": operation}


@app.get("/operations/{op_id}", dependencies=[Depends(require_token)])
def get_operation(op_id: str, node: str | None = None):
    # Remote operations live on the node that started them.
    if node and node != _self_host():
        return _remote_request(node, "GET", f"/operations/{op_id}")
    with _ops_lock:
        op = _operations.get(op_id)
    if not op:
        raise HTTPException(status_code=404, detail="Unknown operation")
    return op


@app.post("/operations", dependencies=[Depends(require_token)], status_code=202)
def submit_operation(body: OperationRequest):
    label = ",".join(body.services) if body.services else "all"
    return {
        "operation": _start_operation(
            label, body.action, lambda op: execute(body, op), guard_activation=False
        )
    }


@app.get("/diagnostics/status", dependencies=[Depends(require_token)])
def diagnostic_status(services: str = ""):
    try:
        return {
            "services": status.collect_services(
                services.split(",") if services else None
            )
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/diagnostics/{name}/{kind}", dependencies=[Depends(require_token)])
def service_diagnostics(
    name: str, kind: str, tail: int = 100, container: str | None = None
):
    if kind not in ("logs", "inspect"):
        raise HTTPException(404, "Unknown diagnostic")
    try:
        return diagnostics(name, kind, tail, container)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(502, redact(str(exc))) from exc


@app.post("/services/{name}/provider", dependencies=[Depends(require_token)])
def set_provider(name: str, body: ProviderBody):
    # Forward to the owning node's agent if this isn't it (node stripped so the
    # peer handles it as local).
    if body.node and body.node != _self_host():
        return _remote_request(
            body.node,
            "POST",
            f"/services/{name}/provider",
            {**body.dict(), "node": None},
        )
    if name not in _PROVIDER_ENV_KEYS:
        raise HTTPException(
            status_code=400, detail=f"{name} does not support provider switching"
        )

    # Local imports avoid loading placement admission during manager bootstrap.
    from deployment_guard import activation
    from service_deployments import PlacementError

    try:
        with activation(REPO_ROOT, name):
            pass
    except PlacementError as exc:
        raise HTTPException(409, str(exc)) from exc

    env_path = REPO_ROOT / services.SERVICES[name]["path"] / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)

    streaming = name == "asr-services" and body.lane == "streaming"
    # When True, the local-container set doesn't change, so we skip compose
    # entirely (the backend repoints defaults.stt_stream + signals workers).
    skip_compose = False

    if streaming:
        # Streaming lane: STREAMING_ASR_PROVIDER selects the stt_stream model.
        # Cloud providers (smallest/deepgram) have no local container, so the env
        # var is cleared and only the batch container is (re)started.
        env_key = "STREAMING_ASR_PROVIDER"
        options = services.STREAMING_ASR_PROVIDER_OPTIONS
        if body.provider not in options:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown streaming provider '{body.provider}'. Available: {', '.join(options)}",
            )
        # The streaming companion container (if any) for old vs new selection.
        old_provider = (
            dotenv_values(env_path).get("STREAMING_ASR_PROVIDER") or ""
        ).strip("'\"")
        old_service = services.STREAMING_ASR_PROVIDER_TO_SERVICE.get(old_provider)
        new_service = options[body.provider]["service"]
        # Only local-container providers belong in STREAMING_ASR_PROVIDER (it drives
        # which companion container `up` starts); cloud providers leave it empty.
        env_value = body.provider if new_service else ""
        # If the local container set is unchanged (e.g. cloud→cloud like
        # smallest→deepgram), don't churn containers — repointing the stt_stream
        # model is enough, and we avoid a needless heavy batch-model reload.
        skip_compose = old_service == new_service
    else:
        env_key, provider_map = _PROVIDER_ENV_KEYS[name]
        if body.provider not in provider_map:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider '{body.provider}'. Available: {', '.join(provider_map)}",
            )
        env_value = body.provider

    set_key(str(env_path), env_key, env_value, quote_mode="never")

    # Does the NEW selection run a local container? Drives both whether we start a
    # container below and the config.yml enabled flag. For asr-services this is a
    # property of *either* lane, read fresh from the .env we just wrote; every tts
    # provider runs a local container.
    if name == "asr-services":
        needs_container = services.asr_needs_local_container()
    else:
        needs_container = bool(services._TTS_PROVIDER_TO_SERVICE.get(body.provider))

    # Keep config.yml's enabled set in step with the provider choice: the System
    # page only shows/controls enabled services and ./services start --all only starts them, so
    # switching to a cloud (container-less) provider must disable the service and
    # switching to a local one must (re-)enable it — otherwise the dropdown that
    # made the switch disappears and the lifecycle drifts from the running state.
    if name == "asr-services" and services.set_service_enabled(name, needs_container):
        logger.info(
            "asr-services enabled=%s in config.yml after provider switch",
            needs_container,
        )

    def fn(op):
        if skip_compose:
            op["phase"] = "Applying provider (no container change)…"
            return True
        # down first: providers share one port, so the old container must go
        # before the new one binds.
        op["phase"] = "Stopping current service…"
        ok = services.run_compose_command(name, "down")
        # Cloud-only selection has no container to start — down is enough. A local
        # provider is (re)started whether or not it was running before, since
        # switching *to* a provider means you want it active.
        if not ok or not needs_container:
            return ok
        op["phase"] = f"Starting {body.provider}…"
        return services.run_compose_command(name, "up", build=body.build)

    op = _start_operation(name, f"provider:{body.provider}", fn)
    return {"operation": op}


# NOTE: declared after /services/{name}/provider so "provider" never matches {action}.
@app.post("/services/{name}/{action}", dependencies=[Depends(require_token)])
def service_action(name: str, action: str, body: ActionBody | None = None):
    body = body or ActionBody()
    # Forward to the owning node's agent if this isn't it.
    if body.node and body.node != _self_host():
        if action not in VALID_ACTIONS:
            raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
        return _remote_request(
            body.node,
            "POST",
            f"/services/{name}/{action}",
            {**body.dict(), "node": None},
        )
    if name not in services.SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")

    # Stopping/restarting the backend kills the WebUI (and this request's caller).
    # Require an explicit force flag so it can't happen by accident.
    if name == "backend" and action in ("stop", "restart") and not body.force:
        raise HTTPException(
            status_code=400,
            detail="Stopping the backend kills the WebUI. Pass force=true to confirm.",
        )

    request = OperationRequest(
        action=action, services=[name], build=body.build, recreate=body.recreate
    )
    op = _start_operation(
        name, action, lambda op: execute(request, op), guard_activation=False
    )
    return {"operation": op}


# ── Client components (native user units — tray, ScreenPipe collector) ────────
# These aren't compose services: they're login units managed by clients.py.
# Actions are quick (systemctl/launchctl), so they run inline — no operation id.


@app.get("/clients", dependencies=[Depends(require_token)])
def list_clients(node: str | None = None):
    if node and node != _self_host():
        return _remote_request(node, "GET", "/clients")
    return {
        "clients": [
            clients.component_status(name) for name in clients.CLIENT_COMPONENTS
        ],
        "binaries": clients.binary_checks(),
    }


@app.post("/clients/{name}/{action}", dependencies=[Depends(require_token)])
def client_action(name: str, action: str, body: ActionBody | None = None):
    body = body or ActionBody()
    if body.node and body.node != _self_host():
        return _remote_request(
            body.node,
            "POST",
            f"/clients/{name}/{action}",
            {**body.dict(), "node": None},
        )
    if name not in clients.CLIENT_COMPONENTS:
        raise HTTPException(status_code=404, detail=f"Unknown client component: {name}")
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
    if not clients.component_installed(name):
        raise HTTPException(
            status_code=400, detail=f"{name} is not installed on this node"
        )
    try:
        ok = clients.component_action(name, action)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not ok:
        raise HTTPException(status_code=500, detail=f"{name} {action} failed")
    return clients.component_status(name)


# ── Node self-update ──────────────────────────────────────────────────────────
#
# Updating a node = move the git checkout (branch pull or release-tag checkout,
# see updates.py) + rebuild/restart the enabled services + restart this agent so
# it too runs the new code. The hub drives peers through the same node-forwarding
# used by service actions, so one WebUI updates the whole cluster node-by-node.

# Update checks fetch from origin and a forwarded POST returns after only
# *starting* the peer's operation, but both can sit behind a slow git fetch —
# give them more headroom than plain control calls.
_UPDATE_FORWARD_TIMEOUT = 90.0


class UpdateBody(BaseModel):
    # Explicit tag/ref to update to. Omitted → upstream branch, else latest v* tag.
    target: str | None = None
    # Prebuilt image tag: pull registry images instead of building locally.
    prebuilt: str | None = None
    # Owning node host (see ActionBody.node).
    node: str | None = None


def _restart_self(delay: float = 3.0):
    """Restart this agent so it runs the just-updated code.

    Deferred a few seconds so the update operation's final poll can still read
    "done" from THIS process. Under systemd the unit restart re-resolves deps via
    uv; otherwise re-exec the same interpreter on the updated source (new deps in
    setup-requirements.txt then need a manual ./services start --all, which is rare enough
    to accept).
    """

    def _go():
        logger.info("Restarting agent to pick up updated code")
        if services._service_manager_managed():
            subprocess.run(
                ["systemctl", "--user", "restart", "chronicle-service-manager"]
            )
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Timer(delay, _go).start()


@app.get("/update", dependencies=[Depends(require_token)])
def update_check(node: str | None = None, target: str | None = None):
    """Whether an update is available for this node (fetches from origin).

    ``node`` routes the check to a peer's agent; ``target`` pins a specific
    tag/ref instead of the default (upstream branch, else latest release tag).
    """
    if node and node != _self_host():
        params = f"?target={target}" if target else ""
        return _remote_request(
            node, "GET", f"/update{params}", timeout=_UPDATE_FORWARD_TIMEOUT
        )
    return updates.check_update(target=target)


@app.post("/update", dependencies=[Depends(require_token)])
def update_node(body: UpdateBody | None = None):
    """Update this node: move the checkout, rebuild/restart enabled services,
    then restart this agent. Returns 202-style {operation} to poll; on the hub
    this restarts the backend too, so the WebUI must expect a brief outage.
    """
    body = body or UpdateBody()
    if body.node and body.node != _self_host():
        return _remote_request(
            body.node,
            "POST",
            "/update",
            {**body.dict(), "node": None},
            timeout=_UPDATE_FORWARD_TIMEOUT,
        )

    def fn(op):
        ok = updates.perform_update(
            target=body.target,
            prebuilt=body.prebuilt,
            progress=lambda msg: op.__setitem__("phase", msg),
        )
        if ok:
            # Even an "already up to date" run is safe to restart on — cheap,
            # and guarantees the agent never lags the checkout it manages.
            op["phase"] = "Restarting node agent…"
            _restart_self()
        return ok

    op = _start_operation("node", "update", fn)
    return {"operation": op}


# ── Claude remote-control session ────────────────────────────────────────────
#
# Lets the WebUI start/stop a `claude remote-control` server (in tmux) on this
# host, so new Claude Code sessions can be spawned from the phone. Backed by the
# services.py helpers (tmux + optional systemd unit).

_RC_ACTIONS = ("start", "stop", "restart")


@app.get("/remote-control", dependencies=[Depends(require_token)])
def remote_control_status():
    return services.remote_control_status()


@app.post("/remote-control/{action}", dependencies=[Depends(require_token)])
def remote_control_action(action: str):
    if action not in _RC_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action}")
    try:
        if action == "start":
            ok = services.start_remote_control()
        elif action == "stop":
            ok = services.stop_remote_control()
        else:  # restart
            services.stop_remote_control()
            ok = services.start_remote_control()
    except Exception as e:  # noqa: BLE001 - surface failure to the WebUI
        logger.exception("remote-control %s failed", action)
        raise HTTPException(
            status_code=500, detail=f"remote-control {action} failed: {e}"
        )
    if not ok:
        raise HTTPException(
            status_code=500,
            detail=f"remote-control {action} did not succeed (tmux/claude available?)",
        )
    return services.remote_control_status()


# Deployment policy is independent of the backend so boot admission works while
# the backend is unavailable. The gateway preserves streaming response bodies.
from edge.deployments import install as install_deployments

_deployment_store = install_deployments(app, sys.modules[__name__])


def main():
    if not TOKEN:
        logger.error("SERVICE_MANAGER_TOKEN not set — refusing to start")
        sys.exit(1)
    # Advertise on the Tailnet in the background — never blocks the control API.
    _start_advertising_thread()
    # Watch for host-level faults nothing else can see. Separate thread from the
    # advertiser: the probes are slower and run on a much longer interval.
    _start_watchdog_thread()
    logger.info("Node agent listening on %s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
