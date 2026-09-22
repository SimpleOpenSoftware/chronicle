"""Authoritative compute placement and durable activation reservations.

Discovery is inventory, never a lock. A reservation does not expire: elapsed time
cannot prove that a process stopped. Operators recover abandoned reservations
only after confirming that the originating activation process has exited.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlacementError(RuntimeError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Endpoint(StrictModel):
    url: str
    health_url: str
    # Ready replicas must return these exact fields from their health response.
    readiness: dict[str, str | int | bool] = Field(default_factory=dict)
    # Identity claims checked against provider metadata independently of liveness.
    identity_url: str | None = None
    identity: dict[str, str | int | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_urls(self):
        for value in (
            self.url,
            self.health_url,
            *([self.identity_url] if self.identity_url else []),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("Endpoints require an absolute HTTP(S) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "Endpoint URLs cannot contain credentials, queries or fragments"
                )
        return self


class Instance(StrictModel):
    node: str
    endpoints: dict[str, Endpoint]


class Deployment(StrictModel):
    mode: Literal["single", "ha"] = "single"
    instances: list[Instance] = Field(min_length=1)
    # Stateful services can only fail over if a snapshot contract is configured.
    state: Literal["stateless", "speaker_catalog"] = "stateless"

    @model_validator(mode="after")
    def validate_instances(self):
        nodes = [i.node for i in self.instances]
        if len(set(nodes)) != len(nodes):
            raise ValueError("Each deployment instance must belong to a distinct node")
        if self.mode == "single" and len(nodes) != 1:
            raise ValueError("Single mode requires exactly one owner")
        if self.mode == "ha" and len(nodes) < 2:
            raise ValueError("HA requires a primary and at least one warm standby")
        roles = set(self.instances[0].endpoints)
        if not roles or any(set(i.endpoints) != roles for i in self.instances):
            raise ValueError("All instances must expose the same endpoint roles")
        if any(not r.replace("-", "").replace("_", "").isalnum() for r in roles):
            raise ValueError("Invalid endpoint role")
        return self


class Plan(StrictModel):
    revision: int = Field(default=0, ge=0)
    # Stable explicit node ids -> node-agent addresses. No persisted Tailnet IPs.
    nodes: dict[str, str] = Field(default_factory=dict)
    deployments: dict[str, Deployment] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_membership(self):
        for node, url in self.nodes.items():
            Endpoint(url=url, health_url=url)
            if not node.replace("-", "").replace("_", "").isalnum():
                raise ValueError(
                    "Node ids must be letters, digits, hyphens or underscores"
                )
        for name, deployment in self.deployments.items():
            if not name.replace("-", "").isalnum():
                raise ValueError("Invalid deployment name")
            if any(i.node not in self.nodes for i in deployment.instances):
                raise ValueError(f"{name}: every instance must name a registered node")
            if name == "speaker-recognition" and deployment.state != "speaker_catalog":
                raise ValueError("Speaker recognition requires state=speaker_catalog")
            if deployment.mode == "ha" and deployment.state == "speaker_catalog":
                for role in deployment.instances[0].endpoints:
                    expected = deployment.instances[0].endpoints[role].readiness
                    if (
                        not expected.get("catalog_fingerprint")
                        or expected.get("read_only") is not True
                    ):
                        raise ValueError(
                            "Speaker HA requires a pinned catalog_fingerprint and read_only=true on every replica"
                        )
                    if any(
                        i.endpoints[role].readiness != expected
                        for i in deployment.instances
                    ):
                        raise ValueError(
                            "Speaker HA replicas must use the same catalog snapshot"
                        )
            if deployment.mode == "ha" and deployment.state == "stateless":
                for role in deployment.instances[0].endpoints:
                    expected = deployment.instances[0].endpoints[role].identity
                    if not expected or not any(
                        "model" in key.lower() or key.endswith(".id")
                        for key in expected
                    ):
                        raise ValueError(
                            f"{name}/{role}: HA requires a model identity contract, not only health status"
                        )
                    if any(
                        not i.endpoints[role].identity_url
                        or i.endpoints[role].identity != expected
                        for i in deployment.instances
                    ):
                        raise ValueError(
                            f"{name}/{role}: HA replicas require identical model identities and an identity_url"
                        )

        return self


class DeploymentStore:
    """One SQLite authority. Transactions cover plan changes and start admission."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS plan (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS activations (service TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, node TEXT NOT NULL, revision INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO plan VALUES (1, ?)", (Plan().model_dump_json(),)
            )

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def read(self) -> Plan:
        with self._db() as db:
            return self._read(db)

    @staticmethod
    def _read(db):
        return Plan.model_validate_json(
            db.execute("SELECT body FROM plan WHERE id=1").fetchone()[0]
        )

    def reservations(self):
        with self._db() as db:
            return [
                dict(zip(("service", "token", "node", "revision"), row))
                for row in db.execute("SELECT * FROM activations")
            ]

    def configure(
        self, proposed: Plan, inspect: Callable[[str, str, str], dict]
    ) -> Plan:
        with self._db() as db:
            current = self._read(db)
            if proposed.revision != current.revision:
                raise PlacementError("Plan changed; reload before saving")
            if db.execute("SELECT 1 FROM activations LIMIT 1").fetchone():
                raise PlacementError(
                    "An activation is in progress; finish it before changing placement"
                )
            # Removing a deployment would remove enforcement; require all instances stopped.
            for service in current.deployments.keys() | proposed.deployments.keys():
                allowed = (
                    {i.node for i in proposed.deployments[service].instances}
                    if service in proposed.deployments
                    else set()
                )
                for node, url in (current.nodes | proposed.nodes).items():
                    # Inspect former addresses too when a node's transport address changes.
                    urls = {url, current.nodes.get(node, url)}
                    for address in urls:
                        state = inspect(node, address, service)
                        if state.get("busy") or (
                            state["running"]
                            and (
                                node not in allowed
                                or current.nodes.get(node, url)
                                != proposed.nodes.get(node, url)
                            )
                        ):
                            raise PlacementError(
                                f"{service} on {node} must be stopped and idle before this placement can be saved"
                            )
                        if node in allowed and state.get("protocol") != 1:
                            raise PlacementError(
                                f"{node} has not enabled deployment enforcement"
                            )
            proposed = proposed.model_copy(update={"revision": current.revision + 1})
            db.execute(
                "UPDATE plan SET body=? WHERE id=1", (proposed.model_dump_json(),)
            )
            return proposed

    def acquire(
        self,
        service: str,
        node: str,
        inspect: Callable[[str, str, str], dict],
        *,
        stop_revision: int | None = None,
    ) -> str:
        with self._db() as db:
            plan = self._read(db)
            if node not in plan.nodes:
                raise PlacementError(
                    f"Register node {node} in the deployment plan before starting services"
                )
            deployment = plan.deployments.get(service)
            if db.execute(
                "SELECT 1 FROM activations WHERE service=?", (service,)
            ).fetchone():
                raise PlacementError(f"{service} already has an activation reservation")
            if stop_revision is not None:
                if (
                    plan.revision != stop_revision
                    or deployment is None
                    or node in {i.node for i in deployment.instances}
                ):
                    raise PlacementError(
                        "Placement changed; excluded-instance stop is no longer authorized"
                    )
            elif deployment is not None:
                allowed = {i.node for i in deployment.instances}
                if node not in allowed:
                    raise PlacementError(
                        f"{service} is assigned to {', '.join(sorted(allowed))}; {node} is excluded"
                    )
                for peer, url in plan.nodes.items():
                    state = inspect(peer, url, service)
                    if peer not in allowed and state["running"]:
                        raise PlacementError(
                            f"Unexpected {service} instance on {peer}; stop it before starting another"
                        )
                    if state.get("protocol") != 1:
                        raise PlacementError(
                            f"{peer} has not enabled deployment enforcement"
                        )
            # Reserve unconfigured groups too: first-time placement must not race
            # an enrolled CLI that has been admitted but has not started Compose.
            token = uuid.uuid4().hex
            db.execute(
                "INSERT INTO activations VALUES (?,?,?,?)",
                (service, token, node, plan.revision),
            )
            return token

    def release(self, token: str):
        with self._db() as db:
            db.execute("DELETE FROM activations WHERE token=?", (token,))
