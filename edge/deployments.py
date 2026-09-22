"""Node-agent deployment control and streaming HTTP inference gateway."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from deployment_guard import activation, control_headers, placement_config
from service_deployments import DeploymentStore, PlacementError, Plan

# Catalog-changing operations always stay on the primary. Unlisted POSTs are
# deliberately not inferred to be safe from their names.
SPEAKER_READ_PATHS = {
    "identify",
    "identify/batch",
    "diarize-and-identify",
    "v1/diarize-identify-match",
    "v1/reidentify-clusters",
    "v1/embed-clusters",
    "enrollment/candidates/score",
    "enrollment/candidates/score-embeddings",
    "enrollment/candidates/embed",
}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


from service_health import probe


class Gateway:
    def __init__(self, store, client_factory=httpx.AsyncClient):
        self.store = store
        self.client_factory = client_factory

    async def select(self, client, name, deployment, role, method, path, revision):
        instances = deployment.instances
        results = await asyncio.gather(
            *(probe(client, i.endpoints[role]) for i in instances)
        )
        candidates = range(len(instances)) if deployment.mode == "ha" else range(1)
        if deployment.state == "speaker_catalog" and deployment.mode == "ha":
            readonly = method in ("GET", "HEAD") or (
                method == "POST" and path in SPEAKER_READ_PATHS
            )
            if not readonly:
                raise HTTPException(
                    409,
                    "Speaker HA uses read-only catalog snapshots; switch to single mode to edit enrollments",
                )
        for index in candidates:
            if results[index]["healthy"]:
                return instances[index], results
        raise HTTPException(
            503,
            detail={
                "message": "No eligible ready instance",
                "instances": [
                    {"node": i.node, **{k: v for k, v in s.items() if k != "data"}}
                    for i, s in zip(instances, results)
                ],
            },
        )

    async def proxy(self, request, name, role, path):
        plan = await asyncio.to_thread(self.store.read)
        deployment = plan.deployments.get(name)
        if not deployment or role not in deployment.instances[0].endpoints:
            raise HTTPException(404, "Unknown deployment endpoint")
        client = self.client_factory(
            timeout=httpx.Timeout(3600, connect=5), follow_redirects=False
        )
        try:
            instance, _ = await self.select(
                client, name, deployment, role, request.method, path, plan.revision
            )
            endpoint = instance.endpoints[role]
            url = endpoint.url.rstrip("/") + ("/" + path if path else "")
            if request.url.query:
                url += "?" + request.url.query
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower()
                not in HOP_HEADERS
                | {"authorization", "x-chronicle-service-token", "cookie"}
            }
            upstream = client.build_request(
                request.method, url, headers=headers, content=request.stream()
            )
            # Never retry submitted requests, including a response timeout/5xx.
            response = await client.send(upstream, stream=True)
        except BaseException as exc:
            await client.aclose()
            if isinstance(exc, httpx.HTTPError):
                raise HTTPException(
                    502,
                    "Upstream request failed; it was not replayed",
                    headers={"x-should-retry": "false"},
                ) from exc
            raise

        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in HOP_HEADERS | {"set-cookie"}
        }
        headers["X-Chronicle-Instance"] = instance.node
        headers["x-should-retry"] = "false"
        return StreamingResponse(
            chunks(), status_code=response.status_code, headers=headers
        )


class ActivationBody(BaseModel):
    service: str
    node: str
    stop_revision: int | None = None


def install(app, manager):
    root = manager.REPO_ROOT
    store = DeploymentStore(root / "config" / "service-deployments.sqlite3")
    gateway = Gateway(store)
    router = APIRouter()

    def authority():
        cfg = placement_config(root)
        if not cfg.get("authority"):
            raise HTTPException(409, "This node is not the deployment authority")
        return cfg

    def forward(method, path, body=None):
        cfg = placement_config(root)
        try:
            r = requests.request(
                method,
                cfg["coordinator_url"].rstrip("/") + path,
                json=body,
                headers=control_headers(root),
                timeout=120,
            )
            if not r.ok:
                raise HTTPException(r.status_code, r.json().get("detail", r.text))
            return r.json()
        except (KeyError, requests.RequestException) as exc:
            raise HTTPException(503, "Deployment authority is not available") from exc

    def inspect(node, url, service):
        cfg = placement_config(root)
        if node == cfg.get("node_id"):
            return local_state(service)
        try:
            response = requests.get(
                f"{url}/deployment-state/{service}",
                headers=control_headers(root),
                timeout=5,
            )
            response.raise_for_status()
            state = response.json()
            if state.get("node") != node or state.get("coordinator_url", "").rstrip(
                "/"
            ) != cfg.get("coordinator_url", "").rstrip("/"):
                raise PlacementError(
                    f"{node} reports a different deployment authority or node identity"
                )
            return state
        except requests.RequestException as exc:
            raise PlacementError(
                f"Cannot establish {service} state on {node}; unreachable is not stopped"
            ) from exc

    def local_state(service):
        if service not in manager.services.SERVICES:
            raise HTTPException(404, "Unknown lifecycle service")
        cfg = placement_config(root)
        try:
            containers = manager.services.compose_ps_json(
                root / manager.services.SERVICES[service]["path"]
            )
        except Exception as exc:
            raise HTTPException(503, f"Cannot inspect container state: {exc}") from exc
        with manager._ops_lock:
            busy = any(
                o["service"] == service and o["status"] == "running"
                for o in manager._operations.values()
            )
        return {
            "protocol": 1 if cfg.get("coordinator_url") and cfg.get("node_id") else 0,
            "node": cfg.get("node_id"),
            "coordinator_url": cfg.get("coordinator_url"),
            "running": any(
                c["state"] in ("running", "paused", "restarting") for c in containers
            ),
            "busy": busy,
            "containers": containers,
        }

    def proxy_auth(request: Request):
        # Model SDKs use Authorization, speaker clients use the dedicated header.
        # Control credentials never propagate to an inference server.
        token = request.headers.get("x-chronicle-service-token") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        # Secrets is needed only when bootstrapping a missing control token.
        import secrets

        if not manager.TOKEN or not secrets.compare_digest(token, manager.TOKEN):
            raise HTTPException(401, "Invalid service gateway token")

    @router.get(
        "/deployment-state/{service}", dependencies=[Depends(manager.require_token)]
    )
    def state(service: str):
        return local_state(service)

    @router.get("/deployments", dependencies=[Depends(manager.require_token)])
    def get_plan():
        cfg = placement_config(root)
        if not cfg.get("authority"):
            try:
                r = requests.get(
                    cfg["coordinator_url"].rstrip("/") + "/deployments",
                    headers=control_headers(root),
                    timeout=15,
                )
                r.raise_for_status()
                return r.json()
            except (KeyError, requests.RequestException) as exc:
                raise HTTPException(
                    503, "Deployment authority is not available"
                ) from exc
        return {"plan": store.read().model_dump(), "activations": store.reservations()}

    @router.put("/deployments", dependencies=[Depends(manager.require_token)])
    def configure(plan: Plan):
        if not placement_config(root).get("authority"):
            return forward("PUT", "/deployments", plan.model_dump())
        unknown = set(plan.deployments) - set(manager.services.SERVICES)
        if unknown:
            raise HTTPException(422, f"Unknown lifecycle services: {sorted(unknown)}")
        try:
            saved = store.configure(plan, inspect)
        except PlacementError as exc:
            raise HTTPException(409, str(exc)) from exc
        manager._report_event(
            "info",
            "Service deployment plan updated",
            f"Revision {saved.revision}",
            "deployment-plan",
            resolves=True,
        )
        return {"plan": saved.model_dump()}

    @router.post(
        "/deployments/activate",
        dependencies=[Depends(manager.require_token), Depends(authority)],
    )
    def activate(body: ActivationBody):
        try:
            return {
                "token": store.acquire(
                    body.service, body.node, inspect, stop_revision=body.stop_revision
                )
            }
        except PlacementError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete(
        "/deployments/activations/{token}",
        dependencies=[Depends(manager.require_token), Depends(authority)],
    )
    def release(token: str):
        store.release(token)
        return {"released": True}

    @router.get("/deployments/status", dependencies=[Depends(manager.require_token)])
    async def status():
        if not placement_config(root).get("authority"):
            return await asyncio.to_thread(forward, "GET", "/deployments/status")
        plan = await asyncio.to_thread(store.read)
        rows = []
        async with httpx.AsyncClient() as client:
            for name, deployment in plan.deployments.items():
                for role in deployment.instances[0].endpoints:
                    try:
                        selected, results = await gateway.select(
                            client,
                            name,
                            deployment,
                            role,
                            "GET",
                            "health",
                            plan.revision,
                        )
                        selected_node = selected.node
                    except HTTPException as exc:
                        selected_node = None
                        results = exc.detail["instances"]
                    rows.append(
                        {
                            "service": name,
                            "endpoint": role,
                            "mode": deployment.mode,
                            "selected_node": selected_node,
                            "instances": [
                                {
                                    "node": i.node,
                                    "healthy": s["healthy"],
                                    "reason": s["reason"],
                                }
                                for i, s in zip(deployment.instances, results)
                            ],
                        }
                    )
        # Keep process exclusion separate from endpoint readiness.
        violations = []
        for name, deployment in plan.deployments.items():
            allowed = {i.node for i in deployment.instances}
            for node, url in plan.nodes.items():
                try:
                    state = await asyncio.to_thread(inspect, node, url, name)
                    if state["running"] and node not in allowed:
                        violations.append(
                            {
                                "service": name,
                                "node": node,
                                "reason": "Unexpected running instance",
                            }
                        )
                except PlacementError as exc:
                    violations.append(
                        {"service": name, "node": node, "reason": str(exc)}
                    )
        return {"revision": plan.revision, "routes": rows, "violations": violations}

    @router.api_route(
        "/deployments/{name}/proxy/{role}/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(proxy_auth), Depends(authority)],
    )
    async def proxy(request: Request, name: str, role: str, path: str):
        return await gateway.proxy(request, name, role, path)

    def reconcile_excluded():
        cfg = placement_config(root)
        if not cfg or not cfg.get("reconcile_excluded", True):
            return
        # The controller must be reachable: stale policy never shuts down a node.
        snapshot = get_plan()["plan"]
        plan = Plan.model_validate(snapshot)
        node = cfg["node_id"]
        for name, deployment in plan.deployments.items():
            if node in {i.node for i in deployment.instances}:
                continue
            state = local_state(name)
            if state["running"] and not state["busy"]:

                def stop(op, service=name):
                    op["phase"] = "Stopping instance excluded by deployment policy"
                    with activation(root, service, stop_revision=plan.revision):
                        return manager.services.run_compose_command(service, "down")

                manager._start_operation(name, "placement-stop", stop)
                break

    manager.reconcile_service_placement = reconcile_excluded
    app.include_router(router)
    return store
