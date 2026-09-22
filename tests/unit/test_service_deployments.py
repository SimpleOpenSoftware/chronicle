from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from service_deployments import DeploymentStore, PlacementError, Plan


def plan(mode="single", owners=("rainbow",)):
    return Plan.model_validate(
        {
            "nodes": {"kraken": "http://kraken:8775", "rainbow": "http://rainbow:8775"},
            "deployments": {
                "llm-services": {
                    "mode": mode,
                    "instances": [
                        {
                            "node": node,
                            "endpoints": {
                                "chat": {
                                    "url": f"http://{node}:8083/v1",
                                    "health_url": f"http://{node}:8083/health",
                                    "readiness": {"model": "same-model"},
                                    "identity_url": f"http://{node}:8083/health",
                                    "identity": {"model": "same-model"},
                                }
                            },
                        }
                        for node in owners
                    ],
                }
            },
        }
    )


def stopped(*args):
    return {"running": False, "protocol": 1, "busy": False}


def test_single_owner_denies_foreign_node_and_duplicate_process(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    store.configure(plan(), stopped)
    with pytest.raises(PlacementError, match="excluded"):
        store.acquire("llm-services", "kraken", stopped)

    def unexpected(node, *args):
        return {**stopped(), "running": node == "kraken"}

    with pytest.raises(PlacementError, match="Unexpected"):
        store.acquire("llm-services", "rainbow", unexpected)


def test_concurrent_activations_and_plan_edit_cannot_race(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    saved = store.configure(plan(), stopped)

    def start(_):
        try:
            return store.acquire("llm-services", "rainbow", stopped)
        except PlacementError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(start, range(2)))
    assert len([t for t in tokens if t]) == 1
    with pytest.raises(PlacementError, match="in progress"):
        store.configure(saved, stopped)
    # Process restart does not silently expire an outstanding permission.
    assert DeploymentStore(tmp_path / "plan.db").reservations()
    store.release(next(t for t in tokens if t))
    store.configure(saved, stopped)


def test_move_requires_positive_stop_proof_and_revision(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    original = store.configure(plan(), stopped)
    new = plan(owners=("kraken",)).model_copy(update={"revision": original.revision})
    with pytest.raises(PlacementError, match="must be stopped"):
        store.configure(
            new, lambda node, *_: {**stopped(), "running": node == "rainbow"}
        )

    def offline(*_):
        raise PlacementError("unreachable")

    with pytest.raises(PlacementError, match="unreachable"):
        store.configure(new, offline)
    assert store.read() == original
    store.configure(new, stopped)
    with pytest.raises(PlacementError, match="reload"):
        store.configure(new, stopped)


def test_ha_allows_only_declared_replicas(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    store.configure(plan("ha", ("kraken", "rainbow")), stopped)
    token = store.acquire(
        "llm-services", "rainbow", lambda *_: {**stopped(), "running": True}
    )
    assert token
    store.release(token)
    with pytest.raises(PlacementError, match="Register"):
        store.acquire("llm-services", "third", stopped)


def test_speaker_ha_rejects_unversioned_mutable_catalogs():
    data = plan("ha", ("kraken", "rainbow")).model_dump()
    deployment = data["deployments"].pop("llm-services")
    data["deployments"]["speaker-recognition"] = deployment
    deployment["state"] = "speaker_catalog"
    with pytest.raises(ValidationError, match="catalog_fingerprint"):
        Plan.model_validate(data)


def test_compose_entry_point_does_not_execute_when_authority_is_down(
    monkeypatch, tmp_path
):
    # Local imports keep each test's monkeypatch state isolated from collection.
    import services
    from deployment_guard import requests

    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.yml").write_text(
        "service_placement:\n  node_id: rainbow\n  coordinator_url: http://authority:8775\n"
    )
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))

    def down(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", down)
    monkeypatch.setattr(
        services,
        "_run_compose_command",
        lambda *a, **kw: pytest.fail("compose bypassed authority"),
    )
    assert services.run_compose_command("speaker-recognition", "up") is False


def test_cli_recreate_denied_before_stopping(monkeypatch, tmp_path):
    # Local imports keep each test's monkeypatch state isolated from collection.
    import services
    from deployment_guard import requests

    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.yml").write_text(
        "service_placement:\n  node_id: kraken\n  coordinator_url: http://authority:8775\n"
    )
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    monkeypatch.setattr(services, "check_service_enabled", lambda *_: True)
    monkeypatch.setattr(services, "_start_service_manager", lambda: None)

    def down(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", down)
    monkeypatch.setattr(
        services,
        "run_compose_command",
        lambda *a, **kw: pytest.fail("stopped before admission"),
    )
    # Imported after service fakes are installed for this executor test.
    import service_operations

    monkeypatch.setattr(services, "ensure_docker_network", lambda: True)
    monkeypatch.setattr(services, "firewall_sync", lambda **kw: True)
    assert not service_operations.execute(
        service_operations.OperationRequest(
            action="restart", services=["speaker-recognition"], recreate=True
        ),
        {},
    )


def test_first_placement_cannot_race_unconfigured_activation(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    empty = Plan(nodes=plan().nodes)
    saved = store.configure(empty, stopped)
    token = store.acquire("llm-services", "kraken", stopped)
    proposed = plan().model_copy(update={"revision": saved.revision})
    with pytest.raises(PlacementError, match="in progress"):
        store.configure(proposed, stopped)
    store.release(token)
    with pytest.raises(PlacementError, match="must be stopped"):
        store.configure(
            proposed, lambda node, *_: {**stopped(), "running": node == "kraken"}
        )


def test_stale_reconciliation_cannot_stop_new_owner(tmp_path):
    store = DeploymentStore(tmp_path / "plan.db")
    old = store.configure(plan(), stopped)
    store.configure(
        plan(owners=("kraken",)).model_copy(update={"revision": old.revision}), stopped
    )
    with pytest.raises(PlacementError, match="no longer authorized"):
        store.acquire("llm-services", "kraken", stopped, stop_revision=old.revision)


def test_health_only_ha_identity_is_rejected():
    p = plan("ha", ("kraken", "rainbow")).model_dump()
    for i in p["deployments"]["llm-services"]["instances"]:
        i["endpoints"]["chat"]["identity"] = {"status": "ok"}
    with pytest.raises(ValidationError, match="model identity"):
        Plan.model_validate(p)


def test_boot_waits_for_agent_readiness_before_compose(monkeypatch, tmp_path):
    # Local imports keep each test's monkeypatch state isolated from collection.
    from types import SimpleNamespace

    import services

    monkeypatch.setattr(
        services,
        "load_config_yml",
        lambda: {"service_placement": {"node_id": "rainbow"}},
    )
    monkeypatch.setattr(services, "_start_service_manager", lambda: None)
    monkeypatch.setattr(services, "ensure_docker_network", lambda: True)
    monkeypatch.setattr(services, "check_service_enabled", lambda _: True)
    monkeypatch.setattr(services, "firewall_sync", lambda **_: None)
    monkeypatch.setattr(services, "preflight", lambda: None)
    calls = []

    def health(*a, **kw):
        calls.append("health")
        if len(calls) == 1:
            raise services.requests.ConnectionError("starting")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(services.requests, "get", health)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        services,
        "run_compose_command",
        lambda *a, **kw: calls.append("compose") or True,
    )
    # Imported after manager fakes are installed for this bootstrap test.
    import service_cli
    import status

    monkeypatch.setattr(
        service_cli,
        "request",
        lambda *a, **kw: calls.append("compose")
        or {"operation": {"id": "test", "status": "done", "ok": True}},
    )
    assert service_cli.main(["start", "speaker-recognition"]) == 0
    assert calls == ["health", "health", "compose"]
