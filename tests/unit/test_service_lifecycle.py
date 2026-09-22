"""Exercise CLI and boot lifecycle entry points without touching host services."""

import contextlib
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml

import deployment_guard
import service_cli
import services
import status
from service_deployments import PlacementError
from tests.unit.test_service_deployments import plan


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    monkeypatch.setattr(services, "load_config_yml", lambda: {})
    monkeypatch.setattr(services, "check_service_enabled", lambda _: True)
    monkeypatch.setattr(services, "ensure_docker_network", lambda: True)
    monkeypatch.setattr(services, "_start_service_manager", lambda: None)
    monkeypatch.setattr(services, "_stop_service_manager", lambda: None)
    monkeypatch.setattr(services, "firewall_sync", lambda **_: None)
    monkeypatch.setattr(services, "preflight", lambda: None)
    monkeypatch.setattr(services, "console", services.Console(file=io.StringIO()))
    # Fail closed if a test accidentally reaches the real container engine.
    monkeypatch.setattr(
        services, "_run_compose_command", lambda *a, **k: pytest.fail("real compose")
    )
    monkeypatch.setattr(
        status,
        "collect_services",
        lambda names: {n: {"ready": True, "detail": ""} for n in names},
    )
    monkeypatch.setattr(
        status, "get_container_status", lambda name: {"status": "stopped"}
    )
    monkeypatch.setattr(services, "_langfuse_enabled_in_backend", lambda: False)
    return tmp_path


def run_cli(monkeypatch, *args):
    return service_cli.main([*args, "--direct"])


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("ok", [True, False])
def test_cli_propagates_compose_result(lifecycle, monkeypatch, action, ok):
    calls = []
    monkeypatch.setattr(
        services, "run_compose_command", lambda *a, **k: calls.append(a) or ok
    )
    assert run_cli(monkeypatch, action, "speaker-recognition") == (0 if ok else 1)
    assert calls


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_partial_failure_still_attempts_remaining_services(
    lifecycle, monkeypatch, action
):
    calls = []

    def compose(name, *a, **k):
        calls.append(name)
        return name == "tts"

    monkeypatch.setattr(services, "run_compose_command", compose)
    assert run_cli(monkeypatch, action, "speaker-recognition", "tts") == 1
    assert calls == ["speaker-recognition", "tts"]


def test_start_network_failure_is_nonzero(lifecycle, monkeypatch):
    monkeypatch.setattr(services, "ensure_docker_network", lambda: False)
    assert run_cli(monkeypatch, "start", "speaker-recognition") == 1


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("args", [[], ["not-a-service"]])
def test_executable_reports_usage_errors(action, args):
    result = subprocess.run(
        [sys.executable, str(Path(service_cli.__file__).resolve()), action, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.parametrize("action", ["start", "restart"])
def test_agent_readiness_timeout_is_nonzero(lifecycle, monkeypatch, action):
    monkeypatch.setattr(service_cli, "manager_ready", lambda url: False)
    times = iter([0, 31])
    monkeypatch.setattr(service_cli.time, "monotonic", lambda: next(times))
    assert service_cli.main([action, "speaker-recognition"]) == 1


def test_restart_reservation_release_failure_is_nonzero(lifecycle, monkeypatch):
    @contextlib.contextmanager
    def fail_release(*a, **k):
        yield
        raise PlacementError("release failed")

    monkeypatch.setattr(deployment_guard, "activation", fail_release)
    monkeypatch.setattr(services, "run_compose_command", lambda *a, **k: True)
    assert run_cli(monkeypatch, "restart", "speaker-recognition") == 1


@pytest.mark.parametrize("action", ["start", "restart"])
def test_admission_denial_is_nonzero(lifecycle, monkeypatch, action):
    @contextlib.contextmanager
    def deny(*a, **k):
        raise PlacementError("denied")
        yield

    monkeypatch.setattr(deployment_guard, "activation", deny)
    assert run_cli(monkeypatch, action, "speaker-recognition") == 1


def configure_placement(root, monkeypatch, snapshot):
    config = {
        "service_placement": {
            "node_id": "kraken",
            "coordinator_url": "http://authority:8775",
        }
    }
    (root / "config/config.yml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(services, "load_config_yml", lambda: config)
    monkeypatch.setattr(
        services, "check_service_enabled", lambda n: n in {"llm-services", "tts"}
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/health"):
            return SimpleNamespace(ok=True)
        assert url == "http://authority:8775/deployments"
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"plan": snapshot}
        )

    monkeypatch.setattr(deployment_guard.requests, "get", get)
    monkeypatch.setattr(
        deployment_guard, "activation", lambda *a, **k: contextlib.nullcontext()
    )
    return calls


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize("owners", [("rainbow",), ("kraken",), ("rainbow", "kraken")])
def test_bulk_activation_selects_local_owner_and_ha_members(
    lifecycle, monkeypatch, action, owners
):
    snapshot = plan("ha" if len(owners) > 1 else "single", owners).model_dump()
    requests_made = configure_placement(lifecycle, monkeypatch, snapshot)
    invoked = []
    monkeypatch.setattr(
        services,
        "run_compose_command",
        lambda name, *a, **k: invoked.append(name) or True,
    )
    assert run_cli(monkeypatch, action, "--all") == 0
    assert set(invoked) == ({"llm-services", "tts"} if "kraken" in owners else {"tts"})
    assert requests_made.count("http://authority:8775/deployments") == 1


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize("fault", ["offline", "malformed", "unregistered"])
def test_bulk_activation_fails_closed(lifecycle, monkeypatch, action, fault):
    snapshot = plan().model_dump()
    if fault == "malformed":
        snapshot = {"deployments": []}
    elif fault == "unregistered":
        snapshot["nodes"].pop("kraken")
    configure_placement(lifecycle, monkeypatch, snapshot)
    if fault == "offline":
        original_get = deployment_guard.requests.get

        def get(url, **kwargs):
            if url.endswith("/deployments"):
                raise requests.ConnectionError("offline")
            return original_get(url, **kwargs)

        monkeypatch.setattr(deployment_guard.requests, "get", get)
    assert run_cli(monkeypatch, action, "--all") == 1


def test_stop_all_does_not_require_authority_or_filter_excluded_services(
    lifecycle, monkeypatch
):
    configure_placement(lifecycle, monkeypatch, plan().model_dump())
    monkeypatch.setattr(
        deployment_guard.requests,
        "get",
        lambda *a, **k: pytest.fail("stop needs no authority"),
    )
    invoked = []
    monkeypatch.setattr(
        services,
        "run_compose_command",
        lambda name, command, **k: invoked.append((name, command)) or True,
    )
    assert run_cli(monkeypatch, "stop", "--all") == 0
    assert set(invoked) == {("llm-services", "down"), ("tts", "down")}


def test_bulk_selection_is_authenticated_and_does_not_replace_admission(
    lifecycle, monkeypatch
):
    configure_placement(lifecycle, monkeypatch, plan(owners=("kraken",)).model_dump())
    monkeypatch.setenv("SERVICE_PLACEMENT_TOKEN", "test-control-token")
    original_get = deployment_guard.requests.get
    order = []

    def get(url, **kwargs):
        if url.endswith("/deployments"):
            assert kwargs["headers"] == {"Authorization": "Bearer test-control-token"}
            order.append("selected")
        return original_get(url, **kwargs)

    @contextlib.contextmanager
    def deny_changed_placement(*a, **k):
        order.append("admission")
        raise PlacementError("placement changed after selection")
        yield

    monkeypatch.setattr(deployment_guard.requests, "get", get)
    monkeypatch.setattr(deployment_guard, "activation", deny_changed_placement)
    assert run_cli(monkeypatch, "start", "--all") == 1
    assert order == ["selected", "admission", "admission"]


def test_stack_unit_has_bounded_stop(lifecycle, monkeypatch):
    monkeypatch.setattr(services, "_SYSTEMD_USER_DIR", lifecycle)
    unit = services._write_systemd_unit("chronicle-stack").read_text()
    assert "ExecStop=" in unit
    assert "service_cli.py stop --all\n" in unit
    assert "TimeoutStopSec=900\n" in unit
    assert (
        "service_cli.py stop --all\n"
        not in services._write_systemd_unit("chronicle-service-manager").read_text()
    )
