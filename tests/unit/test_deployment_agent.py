import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge.deployments import install
from tests.unit.test_service_deployments import plan


def agent(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.yml").write_text(
        "service_placement:\n  node_id: rainbow\n  coordinator_url: http://rainbow:8775\n  authority: true\n"
    )
    app = FastAPI()
    manager = SimpleNamespace(
        REPO_ROOT=tmp_path,
        TOKEN="test-token",
        require_token=lambda: None,
        services=SimpleNamespace(
            SERVICES={"llm-services": {"path": "llm"}}, compose_ps_json=lambda path: []
        ),
        _ops_lock=threading.Lock(),
        _operations={},
        _report_event=lambda *a, **kw: None,
    )
    store = install(app, manager)
    return TestClient(app), manager, store


def test_registered_routes_validate_and_reject_excluded_start(tmp_path):
    client, manager, store = agent(tmp_path)
    p = plan().model_dump()
    p["nodes"] = {"rainbow": "http://rainbow:8775"}
    saved = client.put("/deployments", json=p)
    assert saved.status_code == 200
    assert client.get("/deployment-state/llm-services").json()["protocol"] == 1
    denied = client.post(
        "/deployments/activate", json={"service": "llm-services", "node": "kraken"}
    )
    assert denied.status_code == 409 and "Register node" in denied.json()["detail"]
    admitted = client.post(
        "/deployments/activate", json={"service": "llm-services", "node": "rainbow"}
    )
    assert admitted.status_code == 200
    assert client.put("/deployments", json=saved.json()["plan"]).status_code == 409
    assert (
        client.delete(
            "/deployments/activations/" + admitted.json()["token"]
        ).status_code
        == 200
    )
    assert client.put("/deployments", json=saved.json()["plan"]).status_code == 200


def test_gateway_does_not_accept_unauthenticated_inference(tmp_path):
    client, _, _ = agent(tmp_path)
    assert (
        client.post(
            "/deployments/llm-services/proxy/chat/chat/completions", json={}
        ).status_code
        == 401
    )


def test_reconciliation_stops_only_confirmed_excluded_instance(tmp_path):
    client, manager, store = agent(tmp_path)
    # Authority policy created using positive inventory proof, then simulate a raw
    # container start outside the managed CLI on the excluded local node.
    # The helper import stays local to keep this test fixture independent at collection.
    from tests.unit.test_service_deployments import stopped

    store.configure(plan(owners=("kraken",)), stopped)
    manager.services.compose_ps_json = lambda _: [{"state": "running"}]
    called = []
    manager._start_operation = lambda name, action, fn: called.append((name, action))
    manager.reconcile_service_placement()
    assert called == [("llm-services", "placement-stop")]


def test_real_agent_auth_accepts_shared_control_token_for_localhost(
    tmp_path, monkeypatch
):
    # Local imports allow manager globals to be patched for this isolated app.
    from fastapi import Depends

    from edge import service_manager

    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/.env").write_text("SERVICE_PLACEMENT_TOKEN=shared-control\n")
    monkeypatch.setattr(service_manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(service_manager, "TRUST_TAILNET", False)
    monkeypatch.setattr(service_manager, "TOKEN", "different-local-token")
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(service_manager.require_token)])
    def protected():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/protected").status_code == 401
    assert (
        client.get(
            "/protected", headers={"Authorization": "Bearer shared-control"}
        ).status_code
        == 200
    )
