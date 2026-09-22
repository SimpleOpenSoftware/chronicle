"""Chronicle's operator CLI; node execution lives in service_operations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values
from pydantic import ValidationError

import services as core
import status
from service_operations import (
    OperationLock,
    OperationRequest,
    diagnostics,
    execute,
    redact,
)

ADMIN = {
    "manager",
    "client",
    "deployments",
    "update",
    "firewall",
    "remote-control",
    "doctor",
}
ROOT = Path(__file__).resolve().parent


def headers():
    # Deferred so administrative commands can run without placement configuration.
    from deployment_guard import control_token

    env = dotenv_values(core._get_backend_env_path())
    token = control_token(ROOT) or env.get("SERVICE_MANAGER_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def local_url():
    return f"http://127.0.0.1:{core._SERVICE_MANAGER_PORT}"


def request(url, method, path, **kwargs):
    response = requests.request(
        method, url + path, headers=headers(), timeout=30, **kwargs
    )
    if not response.ok:
        raise RuntimeError(f"{response.status_code}: {redact(response.text)}")
    return response.json()


def manager_ready(url):
    try:
        return requests.get(url + "/health", timeout=2).ok
    except requests.RequestException:
        return False


def ensure_manager():
    url = local_url()
    if manager_ready(url):
        return url
    # Deferred because the healthy-manager path needs no console redirection.
    from contextlib import redirect_stdout

    from rich.console import Console

    original = core.console
    core.console = Console(file=sys.stderr)
    try:
        with redirect_stdout(sys.stderr):
            core._start_service_manager()
    finally:
        core.console = original
    deadline = time.monotonic() + 30
    while not manager_ready(url):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Node manager did not start. Inspect manager logs; use --direct for explicit local recovery."
            )
        time.sleep(0.25)
    return url


def target_url(node):
    if not node:
        return local_url()
    # Deferred because local-only commands do not need placement configuration.
    from deployment_guard import placement_config

    cfg = placement_config(ROOT)
    if not cfg.get("coordinator_url"):
        raise ValueError("--node requires the registered deployment plan")
    plan = request(cfg["coordinator_url"].rstrip("/"), "GET", "/deployments")["plan"]
    if node not in plan["nodes"]:
        raise ValueError(f"Unknown registered node: {node}")
    return plan["nodes"][node].rstrip("/")


def print_operation(op, as_json=False):
    if as_json:
        print(json.dumps(op, indent=2))
        return
    stamp = datetime.fromtimestamp(
        op.get("started_at", time.time()), ZoneInfo("Asia/Kolkata")
    ).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"{op['id']} {op['status']} — {op.get('phase', '')} ({stamp})")
    for name, result in op.get("results", {}).items():
        print(f"  {name}: {result['state']} — {result.get('detail', '')}")
    if op.get("audit_warning"):
        print("Operation history: " + op["audit_warning"])
    if op.get("status") == "failed" and op.get("log"):
        print(redact(op["log"]))


def parser():
    p = argparse.ArgumentParser(
        prog="./services",
        description="Chronicle services: lifecycle, status, diagnostics, and node administration",
    )
    subs = p.add_subparsers(dest="command")
    for action in ("start", "stop", "restart"):
        sub = subs.add_parser(action)
        sub.add_argument(
            "services", nargs="*", help="Service groups: " + ", ".join(core.SERVICES)
        )
        sub.add_argument("--all", action="store_true")
        sub.add_argument("--node")
        sub.add_argument(
            "--direct",
            action="store_true",
            help="Explicit local recovery without the manager",
        )
        sub.add_argument("--no-wait", action="store_true")
        sub.add_argument(
            "--timeout",
            type=float,
            default=300,
            help="Readiness timeout in seconds after container actions (default: 300)",
        )
        sub.add_argument("--json", action="store_true")
        if action != "stop":
            sub.add_argument("--build", action="store_true")
            sub.add_argument("--use-prebuilt")
            sub.add_argument("--force-recreate", action="store_true")
            sub.add_argument(
                "--recreate", action="store_true", help="Full down/up restart"
            )
    sub = subs.add_parser("status")
    sub.add_argument("services", nargs="*")
    sub.add_argument("--node")
    sub.add_argument("--detailed", action="store_true")
    sub.add_argument("--json", action="store_true")
    for kind in ("logs", "inspect"):
        sub = subs.add_parser(kind)
        sub.add_argument("service", choices=list(core.SERVICES))
        sub.add_argument("--node")
        sub.add_argument("--container")
        sub.add_argument("--json", action="store_true")
        if kind == "logs":
            sub.add_argument("--tail", type=int, default=100)
    sub = subs.add_parser("operation")
    sub.add_argument("id")
    sub.add_argument("--node")
    sub.add_argument("--json", action="store_true")
    for command in sorted(ADMIN):
        subs.add_parser(
            command,
            add_help=False,
            help=f"{command} administration (use {command} --help)",
        )
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Administrative commands retain their existing implementation, through this entry point.
    if argv and argv[0] in ADMIN:
        return core.admin_main(argv)
    p = parser()
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        return 0
    try:
        if args.command in ("start", "stop", "restart"):
            if args.direct and (args.node or args.no_wait):
                p.error("--direct cannot be combined with --node or --no-wait")
            body = OperationRequest(
                action=args.command,
                **{
                    key: value
                    for key, value in vars(args).items()
                    if key in OperationRequest.model_fields
                },
            )
            if args.direct:
                # UUID generation is needed only for explicit direct operations.
                import uuid

                lock = OperationLock()
                if not lock.acquire():
                    raise RuntimeError("Another operation is already running")
                op = {
                    "id": uuid.uuid4().hex[:12],
                    "started_at": time.time(),
                    "status": "running",
                    "audit_warning": "Direct recovery: not recorded in System Events",
                }
                try:
                    # Keep machine output clean when the underlying engine emits progress.
                    from contextlib import redirect_stdout

                    from rich.console import Console

                    original = core.console
                    core.console = Console(file=sys.stderr)
                    try:
                        with redirect_stdout(sys.stderr):
                            ok = execute(body, op)
                    finally:
                        core.console = original
                    op.update(
                        ok=ok,
                        status="done" if ok else "failed",
                        finished_at=time.time(),
                    )
                finally:
                    lock.release()
                print_operation(op, args.json)
                return 0 if op["ok"] else 1
            url = target_url(args.node) if args.node else ensure_manager()
            op = request(url, "POST", "/operations", json=body.model_dump())[
                "operation"
            ]
            if args.no_wait:
                print_operation(op, args.json)
                return 0
            last = None
            while op["status"] == "running":
                if not args.json and op.get("phase") != last:
                    last = op.get("phase")
                    print(f"{op['id']}: {last or 'Accepted'}", flush=True)
                time.sleep(0.5)
                op = request(url, "GET", "/operations/" + op["id"])
            print_operation(op, args.json)
            return 0 if op["ok"] else 1
        if args.command == "status":
            unknown = set(args.services) - core.SERVICES.keys()
            if unknown:
                p.error("Unknown services: " + ", ".join(sorted(unknown)))
            if args.node:
                data = request(
                    target_url(args.node),
                    "GET",
                    "/diagnostics/status",
                    params={"services": ",".join(args.services)},
                )["services"]
            else:
                data = status.collect_services(args.services or None)
            if args.json:
                print(redact(json.dumps(data, indent=2)))
            else:
                status.render_services(data, args.detailed)
            return 0
        if args.command == "operation":
            op = request(target_url(args.node), "GET", "/operations/" + args.id)
            print_operation(op, args.json)
            return 1 if op["status"] == "failed" else 0
        if args.command in ("logs", "inspect"):
            if args.node:
                data = request(
                    target_url(args.node),
                    "GET",
                    f"/diagnostics/{args.service}/{args.command}",
                    params={
                        "tail": getattr(args, "tail", 100),
                        "container": args.container,
                    },
                )
            else:
                data = diagnostics(
                    args.service,
                    args.command,
                    getattr(args, "tail", 100),
                    args.container,
                )
            if args.command == "logs" and not args.json:
                for item in data["containers"]:
                    print(f"[{item['container']}]\n{item['log']}")
            else:
                print(json.dumps(data, indent=2))
            return 0
    except (ValidationError, ValueError) as exc:
        p.error(str(exc))
    except (requests.RequestException, RuntimeError, OSError) as exc:
        print(f"Error: {redact(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Stopped waiting; any accepted operation continues. Use ./services operation ID to inspect it.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
