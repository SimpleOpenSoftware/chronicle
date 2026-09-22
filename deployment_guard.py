"""Host-side admission adapter shared by CLI, boot and node-agent operations."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import requests
import yaml
from dotenv import dotenv_values

from service_deployments import PlacementError, Plan

_held = ContextVar("deployment_activations", default=frozenset())


def placement_config(root: Path) -> dict:
    path = root / "config" / "config.yml"
    if not path.exists():
        return {}
    config = yaml.safe_load(path.read_text()) or {}
    return config.get("service_placement") or {}


def control_token(root: Path) -> str:
    return (
        os.getenv("SERVICE_PLACEMENT_TOKEN")
        or dotenv_values(root / "backend" / ".env").get("SERVICE_PLACEMENT_TOKEN")
        or ""
    )


def control_headers(root: Path) -> dict:
    token = control_token(root)
    return {"Authorization": f"Bearer {token}"} if token else {}


def local_services(root: Path, services: list[str]) -> list[str]:
    """Select bulk activation targets; admission still fences each activation.

    Groups absent from the plan remain node-local. Both primary and warm standby
    instances are eligible. An unreadable plan must never become permission to
    start every enabled service.
    """
    cfg = placement_config(root)
    if not cfg:
        return services
    url = cfg.get("coordinator_url", "").rstrip("/")
    node = cfg.get("node_id")
    if not url or not node:
        raise PlacementError("service_placement requires coordinator_url and node_id")
    try:
        response = requests.get(
            f"{url}/deployments", headers=control_headers(root), timeout=15
        )
        response.raise_for_status()
        plan = Plan.model_validate(response.json()["plan"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise PlacementError(
            f"Cannot read deployment plan; refusing bulk activation: {exc}"
        ) from exc
    if node not in plan.nodes:
        raise PlacementError(
            f"Register node {node} in the deployment plan before starting services"
        )
    return [
        name
        for name in services
        if name not in plan.deployments
        or any(instance.node == node for instance in plan.deployments[name].instances)
    ]


@contextmanager
def activation(root: Path, service: str, *, stop_revision: int | None = None):
    cfg = placement_config(root)
    if not cfg or service in _held.get():
        yield
        return
    url = cfg.get("coordinator_url", "").rstrip("/")
    node = cfg.get("node_id")
    if not url or not node:
        raise PlacementError("service_placement requires coordinator_url and node_id")
    try:
        response = requests.post(
            f"{url}/deployments/activate",
            json={"service": service, "node": node, "stop_revision": stop_revision},
            headers=control_headers(root),
            timeout=60,
        )
        if not response.ok:
            raise PlacementError(response.json().get("detail", response.text))
        token = response.json().get("token")
    except requests.RequestException as exc:
        raise PlacementError(
            f"Deployment authority unavailable; refusing activation: {exc}"
        ) from exc
    marker = _held.set(_held.get() | {service})
    try:
        yield
    finally:
        _held.reset(marker)
        if token:
            try:
                response = requests.delete(
                    f"{url}/deployments/activations/{token}",
                    headers=control_headers(root),
                    timeout=15,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise PlacementError(
                    f"Activation completed but reservation {token} could not be released: {exc}"
                ) from exc
