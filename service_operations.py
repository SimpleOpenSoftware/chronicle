"""Shared node-local lifecycle execution and bounded diagnostics."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

import services


class OperationRequest(BaseModel):
    action: Literal["start", "stop", "restart"]
    services: list[str] = Field(default_factory=list)
    all: bool = False
    build: bool = False
    recreate: bool = False
    force_recreate: bool = False
    use_prebuilt: str | None = None
    timeout: float = Field(default=300, gt=0)

    @model_validator(mode="after")
    def validate_selection(self):
        if bool(self.services) == self.all:
            raise ValueError("Specify service names or --all, exclusively")
        unknown = set(self.services) - services.SERVICES.keys()
        if unknown:
            raise ValueError(f"Unknown services: {', '.join(sorted(unknown))}")
        if self.action == "stop" and (
            self.build or self.recreate or self.force_recreate or self.use_prebuilt
        ):
            raise ValueError("Build/recreate options do not apply to stop")
        if self.build and self.use_prebuilt:
            raise ValueError("--build and --use-prebuilt are mutually exclusive")
        self.services = list(dict.fromkeys(self.services))
        return self


class OperationLock:
    """One nonblocking lock shared by the manager, watchdog, and direct recovery."""

    def __init__(self):
        self.thread_lock = threading.Lock()
        self.file = None

    def acquire(self, blocking=False):
        if not self.thread_lock.acquire(blocking=blocking):
            return False
        root = str(Path(services.__file__).resolve().parent)
        digest = hashlib.sha256(root.encode()).hexdigest()[:16]
        path = (
            Path(tempfile.gettempdir())
            / f"chronicle-{os.getuid()}-{digest}.operation.lock"
        )
        try:
            self.file = path.open("a")
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.file.close()
            self.file = None
            self.thread_lock.release()
            return False
        except Exception:
            if self.file:
                self.file.close()
                self.file = None
            self.thread_lock.release()
            raise

    def release(self):
        if self.file:
            self.file.close()
            self.file = None
        self.thread_lock.release()


def select_targets(request):
    names = request.services
    if request.all:
        names = [
            name
            for name in services.SERVICES
            if services.check_service_enabled(name)
            or (name == "langfuse" and services._langfuse_enabled_in_backend())
        ]
        if request.action != "stop":
            # Local import avoids loading placement networking for stop-only operations.
            from deployment_guard import local_services

            names = local_services(Path(services.__file__).resolve().parent, names)
    return names


def execute(request: OperationRequest, op: dict) -> bool:
    """Run every selected action before waiting for readiness. Caller owns the lock."""
    # Local imports break the status and operation executor dependency cycle.
    import status
    from deployment_guard import activation

    names = select_targets(request)
    op["services"] = names
    results = op["results"] = {}
    previous = {
        key: os.environ.get(key) for key in ("CHRONICLE_REGISTRY", "CHRONICLE_TAG")
    }
    try:
        if request.use_prebuilt:
            os.environ["CHRONICLE_REGISTRY"] = os.environ.get("CHRONICLE_REGISTRY") or (
                f"{os.environ['DOCKERHUB_USERNAME']}/"
                if os.environ.get("DOCKERHUB_USERNAME")
                else "ghcr.io/simpleopensoftware/"
            )
            os.environ["CHRONICLE_TAG"] = request.use_prebuilt
        if request.action != "stop" and not services.ensure_docker_network():
            raise RuntimeError("Container network setup failed")
        for name in names:
            op["phase"] = f"{request.action}: {name}"
            try:
                if request.action != "stop" and not services.check_service_enabled(
                    name
                ):
                    raise RuntimeError("Service is disabled; configure it first")
                if (
                    name == "langfuse"
                    and request.action != "stop"
                    and not services._ensure_langfuse_env()
                ):
                    raise RuntimeError("LangFuse configuration is incomplete")
                if request.action == "stop":
                    ok = services.run_compose_command(name, "down")
                else:
                    with activation(Path(services.__file__).resolve().parent, name):
                        ok = True
                        if request.action == "restart" and request.recreate:
                            ok = services.run_compose_command(name, "down")
                        if ok:
                            ok = services.run_compose_command(
                                name,
                                "up",
                                build=request.build,
                                force_recreate=request.force_recreate
                                or (
                                    request.action == "restart" and not request.recreate
                                ),
                            )
                results[name] = {
                    "ok": bool(ok),
                    "state": "checking" if ok else "failed",
                    "detail": "" if ok else "Container action failed",
                }
            except Exception as exc:
                results[name] = {
                    "ok": False,
                    "state": "failed",
                    "detail": redact(str(exc), name),
                }
        if request.action != "stop":
            services.firewall_sync(quiet=True)
        pending = {name for name in names if results[name]["ok"]}
        deadline = time.monotonic() + request.timeout
        while pending:
            op["phase"] = (
                "Waiting for "
                + (
                    "stopped containers: "
                    if request.action == "stop"
                    else "readiness: "
                )
                + ", ".join(sorted(pending))
            )
            snapshots = (
                {
                    name: {
                        "container_status": status.get_container_status(name)["status"],
                        "detail": "",
                    }
                    for name in pending
                }
                if request.action == "stop"
                else status.collect_services(list(pending))
            )
            for name in list(pending):
                snapshot = snapshots[name]
                if request.action == "stop":
                    ready = snapshot["container_status"] == "stopped"
                else:
                    ready = snapshot["ready"]
                results[name]["detail"] = snapshot.get("detail", "")
                if ready:
                    results[name].update(
                        state="stopped" if request.action == "stop" else "ready"
                    )
                    pending.remove(name)
            if not pending:
                break
            if time.monotonic() >= deadline:
                for name in pending:
                    results[name].update(
                        ok=False,
                        state="failed",
                        detail="Readiness timeout: " + results[name]["detail"],
                    )
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        op["phase"] = "Complete"
        return all(result["ok"] for result in results.values())
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def redact(text: str, name: str | None = None) -> str:
    """Remove known configured secrets and common credential patterns from diagnostics."""
    # Loaded only when environment values must be removed from diagnostics.
    from dotenv import dotenv_values

    paths = [services._get_backend_env_path()]
    if name:
        paths.append(
            Path(services.__file__).resolve().parent
            / services.SERVICES[name]["path"]
            / ".env"
        )
    for path in paths:
        for key, value in dotenv_values(path).items():
            if (
                value
                and len(value) >= 4
                and re.search("TOKEN|SECRET|PASSWORD|API_KEY", key, re.I)
            ):
                text = text.replace(value, "<REDACTED>")
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1<REDACTED>", text)
    return re.sub(
        r"(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key))[\"\s]*[:=]\s*[\"]?)[^\s,\"}]+",
        r"\1<REDACTED>",
        text,
    )


def diagnostics(name, kind, tail=100, container=None):
    if name not in services.SERVICES:
        raise ValueError(f"Unknown service: {name}")
    if not 1 <= tail <= 10000:
        raise ValueError("tail must be between 1 and 10000")
    root = Path(services.__file__).resolve().parent
    containers = services.compose_ps_json(root / services.SERVICES[name]["path"])
    names = [item["name"] for item in containers]
    if container:
        if container not in names:
            raise ValueError("Container does not belong to the selected service group")
        names = [container]
    output = []
    for current in names:
        if kind == "logs":
            args = [services.container_engine(), "logs", "--tail", str(tail), current]
        else:
            # Engine-side projection: never retrieve environment or health command secrets.
            projection = '{"name":{{json .Name}},"image":{{json .Config.Image}},"state":{{json .State.Status}},"error":{{json .State.Error}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}},"healthcheck":{{if .Config.Healthcheck}}{"interval_ns":{{json .Config.Healthcheck.Interval}},"timeout_ns":{{json .Config.Healthcheck.Timeout}},"retries":{{json .Config.Healthcheck.Retries}}}{{else}}null{{end}}}'
            args = [
                services.container_engine(),
                "inspect",
                "--format",
                projection,
                current,
            ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError(redact(result.stderr.strip(), name))
        text = redact(result.stdout + result.stderr, name)
        if kind == "inspect":
            # JSON is needed only for the projected inspection response.
            import json

            output.append(json.loads(text))
        else:
            output.append({"container": current, "log": text})
    return {"service": name, "containers": output}
