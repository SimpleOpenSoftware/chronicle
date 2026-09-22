#!/usr/bin/env python3
"""
Chronicle Health Status Checker
Show runtime health status of all services
"""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from rich.console import Console
from rich.table import Table

# Import service definitions from services.py
from services import (
    SERVICES,
    check_service_enabled,
    check_service_health,
    compose_ps_json,
    container_engine,
    service_display_label,
    service_health_endpoint_urls,
)

console = Console()


def get_restart_counts(container_names: List[str]) -> Dict[str, int]:
    """Get restart counts for containers via docker inspect"""
    if not container_names:
        return {}
    try:
        result = subprocess.run(
            [container_engine(), "inspect", "--format", "{{.Name}} {{.RestartCount}}"]
            + container_names,
            capture_output=True,
            text=True,
            timeout=10,
        )
        counts = {}
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parts = line.strip().rsplit(" ", 1)
                if len(parts) == 2:
                    name = parts[0].lstrip("/")
                    try:
                        counts[name] = int(parts[1])
                    except ValueError:
                        counts[name] = 0
        return counts
    except Exception:
        return {}


def get_container_status(service_name: str) -> Dict[str, Any]:
    """Get Docker container status for a service"""
    service = SERVICES[service_name]
    service_path = Path(__file__).resolve().parent / service["path"]

    if not service_path.exists():
        return {"status": "not_found", "containers": []}

    try:
        # Get container status (engine-aware: docker compose ps vs podman ps by
        # compose project label). Only active-profile containers are reported.
        raw_containers = compose_ps_json(service_path)

        containers = []
        for container in raw_containers:
            # Skip test containers - they're not part of production services
            if "-test-" in container["name"].lower():
                continue
            containers.append(container)

        if not containers:
            return {"status": "stopped", "containers": []}

        # Fetch restart counts via docker inspect
        container_names = [c["name"] for c in containers]
        restart_counts = get_restart_counts(container_names)
        for container in containers:
            container["restart_count"] = restart_counts.get(container["name"], 0)

        # Determine overall status
        all_running = all(c["state"] == "running" for c in containers)
        any_running = any(c["state"] == "running" for c in containers)

        if all_running:
            status = "running"
        elif any_running:
            status = "partial"
        else:
            status = "stopped"

        return {"status": status, "containers": containers}

    except subprocess.TimeoutExpired:
        return {"status": "timeout", "containers": []}
    except Exception as e:
        return {"status": "error", "containers": [], "error": str(e)}


def check_http_health(url: str, timeout: int = 5) -> Dict[str, Any]:
    """Check HTTP health endpoint"""
    try:
        response = requests.get(url, timeout=timeout)

        if response.status_code == 200:
            # Try to parse JSON response
            try:
                data = response.json()
                return {"healthy": True, "status_code": 200, "data": data}
            except json.JSONDecodeError:
                return {"healthy": True, "status_code": 200, "data": None}
        else:
            return {"healthy": False, "status_code": response.status_code, "data": None}

    except requests.exceptions.ConnectionError:
        return {"healthy": False, "error": "Connection refused"}
    except requests.exceptions.Timeout:
        return {"healthy": False, "error": "Timeout"}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def get_service_health(service_name: str) -> Dict[str, Any]:
    """Get comprehensive health status for a service"""
    # Check if configured
    if not check_service_enabled(service_name):
        return {
            "configured": False,
            "container_status": "not_configured",
            "health": None,
        }

    # Get container status
    container_info = get_container_status(service_name)

    # Resolve the same endpoint set used by services.py and the node agent. This
    # keeps the CLI from maintaining its own hard-coded host/port table.
    endpoint_urls = service_health_endpoint_urls(service_name)
    health_check = None
    if endpoint_urls:
        health_status, detail = check_service_health(service_name)
        health_check = {
            "healthy": health_status == "healthy",
            "status": health_status,
            "error": detail or health_status,
            "data": None,
        }

        # The detailed view includes Chronicle's dependency breakdown from
        # /health; the lifecycle endpoint for backend readiness is /readiness.
        if service_name == "backend":
            parsed = urlsplit(endpoint_urls[0][1])
            backend_health_url = urlunsplit(parsed._replace(path="/health"))
            detailed = check_http_health(backend_health_url)
            health_check["data"] = detailed.get("data")

    return {
        "configured": True,
        "container_status": container_info["status"],
        "containers": container_info.get("containers", []),
        "health": health_check,
    }


def get_backend_worker_health() -> Optional[Dict[str, Any]]:
    """Get internal worker health from the backend /health endpoint.

    Returns worker_count, failed queues, etc. from the Redis section of health data.
    This catches internal worker crash loops that Docker restart counts miss.
    """
    try:
        endpoint_urls = service_health_endpoint_urls("backend")
        if not endpoint_urls:
            return None
        parsed = urlsplit(endpoint_urls[0][1])
        backend_health_url = urlunsplit(parsed._replace(path="/health"))
        response = requests.get(backend_health_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            redis_info = data.get("services", {}).get("redis", {})
            return {
                "worker_count": redis_info.get("worker_count", 0),
                "active_workers": redis_info.get("active_workers", 0),
                "idle_workers": redis_info.get("idle_workers", 0),
                "queues": redis_info.get("queues", {}),
            }
    except Exception:
        pass
    return None


def collect_services(names=None):
    """One node-local snapshot shared by CLI, operations, and manager API."""
    # Kept local to avoid loading async placement probes for legacy status helpers.
    import asyncio
    import socket

    import httpx

    import services as core
    from deployment_guard import control_headers, placement_config
    from service_deployments import Plan
    from service_health import probe

    root = Path(core.__file__).resolve().parent
    cfg = placement_config(root)
    node = cfg.get("node_id") or socket.gethostname()
    plan = None
    placement_error = None
    if cfg:
        try:
            response = requests.get(
                cfg["coordinator_url"].rstrip("/") + "/deployments",
                headers=control_headers(root),
                timeout=3,
            )
            response.raise_for_status()
            plan = Plan.model_validate(response.json()["plan"])
        except (requests.RequestException, ValueError, KeyError) as exc:
            placement_error = str(exc)

    async def probe_endpoints(endpoints):
        async with httpx.AsyncClient() as client:
            return await asyncio.gather(*(probe(client, e) for e in endpoints))

    data = {}
    for name in names if names is not None else SERVICES:
        if name not in SERVICES:
            raise ValueError(f"Unknown service: {name}")
        info = get_container_status(name)
        configured = check_service_enabled(name)
        state, detail = check_service_health(name)
        placement = "local"
        if plan and name in plan.deployments:
            deployment = plan.deployments[name]
            instance = next((i for i in deployment.instances if i.node == node), None)
            placement = "assigned" if instance else "excluded"
            if instance:
                # Only configured endpoint contracts on this instance; never probe another owner.
                probes = asyncio.run(probe_endpoints(list(instance.endpoints.values())))
                if not all(p["healthy"] for p in probes):
                    state = "unhealthy"
                    detail = "; ".join(p["reason"] for p in probes if not p["healthy"])
        if placement_error:
            placement = "unknown"
        ready = info["status"] == "running" and state == "healthy"
        if not detail:
            detail = f"containers: {info['status']}; endpoints: {state}"
        entry = {
            "name": name,
            "node": node,
            "configured": configured,
            "description": service_display_label(name),
            "placement": placement,
            "placement_error": placement_error,
            "container_status": info["status"],
            "containers": info.get("containers", []),
            "health": state,
            "ready": ready,
            "detail": detail,
        }
        if name == "backend":
            endpoints = service_health_endpoint_urls(name)
            if endpoints:
                parsed = urlsplit(endpoints[0][1])
                entry["dependencies"] = (
                    check_http_health(urlunsplit(parsed._replace(path="/health"))).get(
                        "data"
                    )
                    or {}
                ).get("services", {})
        data[name] = entry
    return data


def render_services(data, detailed=False):
    table = Table(title="Chronicle services")
    for label in ("Node", "Service", "Enabled", "Placement", "Containers", "Ready"):
        table.add_column(label)
    for entry in data.values():
        table.add_row(
            entry["node"],
            entry["name"],
            str(entry["configured"]),
            entry["placement"],
            entry["container_status"],
            str(entry["ready"]),
        )
    console.print(table)
    if detailed:
        # Local import breaks the status and operation diagnostics dependency cycle.
        from service_operations import redact

        for name, entry in data.items():
            console.print(f"{name}: {entry['detail']}", markup=False)
            console.print(redact(json.dumps(entry, indent=2), name), markup=False)
