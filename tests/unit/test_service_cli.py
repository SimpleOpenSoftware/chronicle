"""Exercise public CLI and registered manager routes with fake engine/HTTP seams."""

import contextlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import deployment_guard
import service_cli as cli
import service_operations as operations
import services
import status
from edge import service_manager as manager


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    monkeypatch.setattr(manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(services, "check_service_enabled", lambda _: True)
    monkeypatch.setattr(services, "ensure_docker_network", lambda: True)
    monkeypatch.setattr(services, "firewall_sync", lambda **kw: True)
    monkeypatch.setattr(services, "_langfuse_enabled_in_backend", lambda: False)
    monkeypatch.setattr(
        deployment_guard, "activation", lambda *a, **kw: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        status,
        "collect_services",
        lambda names=None: {
            n: {"ready": True, "detail": ""} for n in names or services.SERVICES
        },
    )
    monkeypatch.setattr(
        status, "get_container_status", lambda name: {"status": "stopped"}
    )
    calls = []
    monkeypatch.setattr(
        services,
        "run_compose_command",
        lambda name, command, **kw: calls.append((name, command, kw)) or True,
    )
    monkeypatch.setattr(manager, "_report_operation_event", lambda op: None)
    manager.app.dependency_overrides[manager.require_token] = lambda: None
    yield calls
    manager.app.dependency_overrides.clear()


def wait(client, op):
    deadline = time.monotonic() + 3
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
        op = client.get("/operations/" + op["id"]).json()
    assert op["status"] != "running", op
    return op


def test_cli_through_registered_batch_route(runtime, monkeypatch, capsys):
    client = TestClient(manager.app)
    monkeypatch.setattr(cli, "ensure_manager", lambda: "http://manager")
    monkeypatch.setattr(
        cli,
        "request",
        lambda url, method, path, **kw: client.request(method, path, **kw).json(),
    )
    assert cli.main(["start", "llm-services", "wakeword-service", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "done"
    assert set(output["results"]) == {"llm-services", "wakeword-service"}
    assert [c[0] for c in runtime] == ["llm-services", "wakeword-service"]


def test_ui_and_cli_restart_use_same_execution(runtime):
    client = TestClient(manager.app)
    ui = client.post("/services/tts/restart", json={}).json()["operation"]
    assert wait(client, ui)["ok"]
    ui_calls = runtime[:]
    runtime.clear()
    op = client.post(
        "/operations", json={"action": "restart", "services": ["tts"]}
    ).json()["operation"]
    assert wait(client, op)["ok"]
    assert runtime == ui_calls
    assert runtime[0][1:] == ("up", {"build": False, "force_recreate": True})


def test_all_actions_happen_before_readiness(runtime, monkeypatch):
    def collect(names):
        assert len(runtime) == 2
        return {name: {"ready": True} for name in names}

    monkeypatch.setattr(status, "collect_services", collect)
    assert cli.main(["start", "tts", "wakeword-service", "--direct"]) == 0


def test_timeout_is_failure_without_stop_or_replay(runtime, monkeypatch):
    monkeypatch.setattr(
        status,
        "collect_services",
        lambda names: {n: {"ready": False, "detail": "loading"} for n in names},
    )
    client = TestClient(manager.app)
    response = client.post(
        "/operations", json={"action": "start", "services": ["tts"], "timeout": 0.01}
    )
    assert response.status_code == 202
    op = wait(client, response.json()["operation"])
    assert op["status"] == "failed"
    assert "loading" in op["results"]["tts"]["detail"]
    assert len(runtime) == 1


def test_loading_becomes_ready(runtime, monkeypatch):
    attempts = []

    def collect(names):
        attempts.append(1)
        return {name: {"ready": len(attempts) > 1} for name in names}

    monkeypatch.setattr(status, "collect_services", collect)
    assert cli.main(["start", "tts", "--direct", "--timeout", ".02"]) == 0
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "args",
    [
        ["start"],
        ["stop"],
        ["restart"],
        ["start", "oops"],
        ["start", "tts", "--all"],
        ["start", "tts", "--direct", "--node", "rainbow"],
        ["start", "tts", "--direct", "--no-wait"],
        ["start", "tts", "--timeout", "0"],
        ["start", "tts", "--build", "--use-prebuilt", "v1"],
    ],
)
def test_usage_errors_precede_any_effect(runtime, args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert not runtime


def test_read_only_status_does_not_boot_manager(runtime, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "ensure_manager", lambda: pytest.fail("read-only bootstrap")
    )
    assert cli.main(["status", "tts", "--json"]) == 0
    assert list(json.loads(capsys.readouterr().out)) == ["tts"]
    assert not runtime


def test_no_arguments_print_help_without_effect(runtime, capsys):
    assert cli.main([]) == 0
    assert "./services" in capsys.readouterr().out
    assert not runtime


def test_bootstrap_once_then_fail(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "manager_ready", lambda url: False)
    monkeypatch.setattr(
        services, "_start_service_manager", lambda: calls.append("start")
    )
    clock = iter([0, 31])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not start"):
        cli.ensure_manager()
    assert calls == ["start"]


def test_bootstrap_starts_once_and_succeeds(monkeypatch):
    states = iter([False, False, True])
    monkeypatch.setattr(cli, "manager_ready", lambda url: next(states))
    calls = []
    monkeypatch.setattr(
        services, "_start_service_manager", lambda: calls.append("start")
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli.ensure_manager() == cli.local_url()
    assert calls == ["start"]


def test_no_wait_submits_once(runtime, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "ensure_manager", lambda: "http://manager")
    monkeypatch.setattr(
        cli,
        "request",
        lambda *a, **kw: calls.append(a)
        or {"operation": {"id": "test", "status": "running"}},
    )
    assert cli.main(["start", "tts", "--no-wait", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "test"
    assert len(calls) == 1


def test_interrupt_does_not_resubmit(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "ensure_manager", lambda: "http://manager")

    def request(*a, **kw):
        calls.append(a[1])
        if a[1] == "GET":
            raise KeyboardInterrupt
        return {"operation": {"id": "test", "status": "running"}}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["start", "tts"]) == 130
    assert calls == ["POST", "GET"]


def test_remote_uses_registered_node_and_auth(monkeypatch, tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/.env").write_text("SERVICE_MANAGER_TOKEN=secret-token\n")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        services, "_get_backend_env_path", lambda: tmp_path / "backend/.env"
    )
    monkeypatch.setattr(deployment_guard, "control_token", lambda root: "")
    monkeypatch.setattr(
        deployment_guard,
        "placement_config",
        lambda root: {"coordinator_url": "http://coordinator"},
    )
    calls = []

    def request(method, url, **kw):
        calls.append((method, url, kw))
        return SimpleNamespace(
            ok=True,
            json=lambda: {
                "plan": {"nodes": {"rainbow": "http://rainbow.example.ts.net:8775"}}
            },
        )

    monkeypatch.setattr(cli.requests, "request", request)
    assert cli.target_url("rainbow") == "http://rainbow.example.ts.net:8775"
    assert calls[0][2]["headers"] == {"Authorization": "Bearer secret-token"}
    with pytest.raises(ValueError, match="Unknown registered node"):
        cli.target_url("missing")


def test_cross_instance_lock_prevents_direct_and_api_overlap(runtime):
    lock = operations.OperationLock()
    assert lock.acquire()
    try:
        assert cli.main(["stop", "tts", "--direct"]) == 1
        assert (
            TestClient(manager.app)
            .post("/operations", json={"action": "stop", "services": ["tts"]})
            .status_code
            == 409
        )
    finally:
        lock.release()
    assert not runtime


def test_manager_auth_required(runtime):
    manager.app.dependency_overrides.clear()
    assert TestClient(manager.app).post(
        "/operations", json={"action": "stop", "services": ["tts"]}
    ).status_code in (401, 503)


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_logs_scoped_and_redacted(runtime, monkeypatch, tmp_path, engine):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/.env").write_text("SERVICE_MANAGER_TOKEN=private-value\n")
    monkeypatch.setattr(services, "container_engine", lambda: engine)
    monkeypatch.setattr(services, "compose_ps_json", lambda path: [{"name": "owned"}])
    calls = []
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv)
        or SimpleNamespace(returncode=0, stdout="Bearer private-value\n", stderr=""),
    )
    data = operations.diagnostics("tts", "logs", 7)
    assert calls == [[engine, "logs", "--tail", "7", "owned"]]
    assert "private-value" not in json.dumps(data)
    with pytest.raises(ValueError, match="does not belong"):
        operations.diagnostics("tts", "logs", container="unrelated")


def test_inspection_never_requests_environment(runtime, monkeypatch):
    monkeypatch.setattr(services, "compose_ps_json", lambda path: [{"name": "owned"}])

    def run(argv, **kw):
        assert ".Env" not in " ".join(argv)
        assert ".Config.Cmd" not in " ".join(argv)
        return SimpleNamespace(returncode=0, stdout='{"state":"running"}', stderr="")

    monkeypatch.setattr(operations.subprocess, "run", run)
    assert (
        operations.diagnostics("tts", "inspect")["containers"][0]["state"] == "running"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"status": "loading"},
        {"status": "initializing"},
        {"healthy": False},
        {"ready": False},
    ],
)
def test_http_200_can_be_not_ready(monkeypatch, body):
    monkeypatch.setattr(services, "_active_health_endpoints", lambda name: [1])
    monkeypatch.setattr(
        services,
        "service_health_endpoint_urls",
        lambda name: [("model", "http://model/health")],
    )
    monkeypatch.setattr(
        services.requests,
        "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: body),
    )
    assert services.check_service_health("tts")[0] == "unhealthy"


def test_launcher_help_from_another_directory():
    launcher = Path(__file__).resolve().parents[2] / "services"
    result = subprocess.run(
        [str(launcher), "--help"],
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "inspect" in result.stdout and "operation" in result.stdout


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_explicit_manager_actions_reach_systemd(monkeypatch, action):
    monkeypatch.setattr(services, "_service_manager_managed", lambda: True)
    calls = []
    monkeypatch.setattr(
        services,
        "_systemctl_user",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert cli.main(["manager", action]) == 0
    assert calls == [(action, "chronicle-service-manager")]
