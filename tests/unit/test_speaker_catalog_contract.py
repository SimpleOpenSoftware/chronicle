import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

path = (
    Path(__file__).resolve().parents[2]
    / "extras/speaker-recognition/src/simple_speaker_recognition/api/catalog_contract.py"
)
spec = importlib.util.spec_from_file_location("catalog_contract", path)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def test_catalog_identity_is_order_independent_but_tracks_speaker_and_model_changes():
    rows = [
        {"id": "b", "name": "One", "embedding_data": "[1,0]"},
        {"id": "a", "name": "Two", "embedding_data": "[0,1]"},
    ]
    original = contract.fingerprint(rows, "model-v1")
    assert contract.fingerprint(list(reversed(rows)), "model-v1") == original
    assert contract.fingerprint(rows, "model-v2") != original
    assert (
        contract.fingerprint([{**rows[0], "name": "Changed"}, rows[1]], "model-v1")
        != original
    )


def test_read_only_replica_rejects_mutating_http_and_websockets():
    app = FastAPI()
    app.add_middleware(contract.ReadOnlyCatalog, enabled=True)

    @app.post("/enroll/upload")
    def enroll():
        raise AssertionError("mutation ran")

    @app.post("/v1/reidentify-clusters")
    def identify():
        return {"assignments": {}}

    client = TestClient(app)
    assert client.post("/enroll/upload").status_code == 409
    assert client.post("/v1/reidentify-clusters").status_code == 200
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_gateway_and_speaker_agree_on_safe_inference_paths():
    from edge.deployments import SPEAKER_READ_PATHS

    assert contract.READ_PATHS == {"/" + p for p in SPEAKER_READ_PATHS}


@pytest.fixture
def speaker_service(monkeypatch, tmp_path):
    """Import the real app while replacing model, storage and router adapters."""
    from fastapi import APIRouter
    from pydantic import BaseModel

    calls = []

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    package = "simple_speaker_recognition"
    source = path.parents[1]
    module(package, __path__=[str(source)])
    module(f"{package}.api", __path__=[str(source / "api")])
    module(f"{package}.core", __path__=[])
    module(
        f"{package}.core.gallery_catalog",
        catalog_snapshot=lambda *_: {},
        target_owner=lambda **_: None,
    )
    module(
        f"{package}.core.gallery_privacy",
        GalleryHeld=type("GalleryHeld", (Exception,), {}),
    )
    module(f"{package}.api.core", __path__=[])
    module(f"{package}.api.core.utils", get_data_directory=lambda: tmp_path)
    module(f"{package}.constants", DEFAULT_SIMILARITY_THRESHOLD=0.5)
    module(
        f"{package}.system_event_reporter",
        install_system_event_reporter=lambda **kwargs: None,
    )
    # Settings itself still runs its production constructor and config loading.
    module("pydantic_settings", BaseSettings=BaseModel)
    module(
        "torch",
        cuda=SimpleNamespace(is_available=lambda: False),
        device=lambda kind: SimpleNamespace(type=kind),
    )

    class AudioBackend:
        EMBEDDING_MODEL_ID = "test-speaker-embedder"

        def __init__(self, token, device, *, max_diarization_workers):
            assert token == "test-token"
            self.embedder = SimpleNamespace(dimension=2)
            calls.append("load-model")

        def close(self):
            calls.append("close-model")

    class UnifiedSpeakerDB:
        def __init__(self, *, emb_dim, base_dir, similarity_thr):
            self.emb_dim = emb_dim
            calls.append("load-catalog")

        def get_speaker_count(self):
            return 1

    module(f"{package}.core.audio_backend", AudioBackend=AudioBackend)
    module(f"{package}.core.unified_speaker_db", UnifiedSpeakerDB=UnifiedSpeakerDB)

    class Speaker:
        pass

    class SpeakerAudioSegment:
        id = "id"
        speaker_id = "speaker_id"
        __table__ = SimpleNamespace(
            columns=[SimpleNamespace(name=name) for name in ("id", "speaker_id")]
        )

    class Query:
        def __init__(self, model):
            self.model = model

        def all(self):
            return [
                SimpleNamespace(
                    id="speaker-1", user_id="user-1", name="One", embedding_data="[1,0]"
                )
            ]

        def filter(self, condition):
            return self

        def order_by(self, column):
            return [SimpleNamespace(id="segment-1", speaker_id="speaker-1")]

    class Session:
        def query(self, model):
            calls.append(f"query-{model.__name__}")
            return Query(model)

        def close(self):
            calls.append("close-session")

    module(
        f"{package}.database",
        __path__=[],
        init_db=lambda: calls.append("init-db"),
        get_db_session=Session,
    )
    module(
        f"{package}.database.models",
        Speaker=Speaker,
        SpeakerAudioSegment=SpeakerAudioSegment,
    )
    router_names = (
        "deepgram",
        "enrollment_audit",
        "enrollment",
        "enrollment_operations",
        "identification",
        "speakers",
        "users",
        "websocket",
    )
    module(
        f"{package}.api.routers",
        **{f"{name}_router": APIRouter() for name in router_names},
    )
    monkeypatch.setitem(sys.modules, f"{package}.api.catalog_contract", contract)
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))

    def load(read_only):
        monkeypatch.setenv("SPEAKER_CATALOG_READ_ONLY", str(read_only).lower())
        service_spec = importlib.util.spec_from_file_location(
            f"{package}.api.service", path.with_name("service.py")
        )
        service = importlib.util.module_from_spec(service_spec)
        monkeypatch.setitem(sys.modules, service_spec.name, service)
        service_spec.loader.exec_module(service)
        return service, calls

    return load


@pytest.mark.parametrize("read_only", [False, True])
def test_real_speaker_lifespan_publishes_catalog_and_readiness(
    speaker_service, monkeypatch, read_only
):
    service, calls = speaker_service(read_only)
    expected = (
        contract.fingerprint(
            [
                {
                    "id": "speaker-1",
                    "user_id": "user-1",
                    "name": "One",
                    "embedding_data": "[1,0]",
                    "segments": [{"id": "segment-1", "speaker_id": "speaker-1"}],
                }
            ],
            {"dimension": 2, "embedder": "test-speaker-embedder"},
        )
        if read_only
        else None
    )

    with TestClient(service.app) as client:
        assert calls[:3] == ["init-db", "load-model", "load-catalog"]
        assert ("query-Speaker" in calls) is read_only
        if read_only:
            assert calls[-1] == "close-session"
        startup_calls = list(calls)
        for route in ("/health", "/readiness", "/readiness"):
            response = client.get(route)
            assert response.status_code == 200
            assert response.json()["read_only"] is read_only
            assert response.json()["catalog_fingerprint"] == expected
            assert response.json()["speakers"] == 1
        assert calls == startup_calls, "Health checks must not rescan catalog storage"

        # The actual readiness entry point must combine CUDA and catalog state.
        monkeypatch.setattr(service, "_probe_cuda", lambda: "simulated CUDA failure")
        response = client.get("/readiness")
        assert response.status_code == 503
        assert response.json()["cuda_error"] == "simulated CUDA failure"
        assert response.json()["catalog_fingerprint"] == expected
        assert client.get("/health").status_code == 200
        if read_only:
            assert client.post("/enroll/upload").status_code == 409

    assert calls[-1] == "close-model"
