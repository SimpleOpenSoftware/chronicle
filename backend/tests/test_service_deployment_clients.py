from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.model_registry import ModelDef
from backend.speaker_recognition_client import SpeakerRecognitionClient


def gateway(monkeypatch):
    monkeypatch.setenv("SERVICE_GATEWAY_URL", "http://authority:8775")
    monkeypatch.setenv("SERVICE_GATEWAY_TOKEN", "private-gateway-key")
    monkeypatch.delenv("SPEAKER_SERVICE_URL", raising=False)


def test_model_reference_uses_stable_gateway_and_disables_sdk_retries(monkeypatch):
    from backend.openai_factory import create_openai_client

    gateway(monkeypatch)
    model = ModelDef(
        name="local",
        model_type="llm",
        deployment="llm-services",
        deployment_endpoint="chat",
    )
    assert (
        model.resolved_url()
        == "http://authority:8775/deployments/llm-services/proxy/chat"
    )
    assert model.api_key == "private-gateway-key"
    assert create_openai_client(model.api_key, model.resolved_url()).max_retries == 0
    with pytest.raises(ValidationError, match="cannot also"):
        ModelDef(
            name="bad",
            model_type="llm",
            deployment="llm-services",
            deployment_endpoint="chat",
            model_url="http://stale:8080",
        )


def test_managed_speaker_client_has_explicit_auth_and_rejects_stale_url(monkeypatch):
    import backend.speaker_recognition_client as module

    gateway(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_models_registry",
        lambda: SimpleNamespace(
            speaker_recognition={"enabled": True, "deployment": "speaker-recognition"}
        ),
    )
    client = SpeakerRecognitionClient()
    assert (
        client.service_url
        == "http://authority:8775/deployments/speaker-recognition/proxy/speaker"
    )
    assert client._gateway_headers == {
        "X-Chronicle-Service-Token": "private-gateway-key"
    }
    monkeypatch.setenv("SPEAKER_SERVICE_URL", "http://wrong:8085")
    with pytest.raises(ValueError, match="cannot also"):
        SpeakerRecognitionClient()


def test_models_editor_roundtrip_preserves_deployment(monkeypatch):
    from backend.controllers.system_controller import _model_view

    gateway(monkeypatch)
    model = ModelDef(
        name="managed",
        model_type="llm",
        deployment="llm-services",
        deployment_endpoint="chat",
    )
    view = _model_view(
        model,
        {"managed": {"deployment": "llm-services", "deployment_endpoint": "chat"}},
        set(),
    )
    assert view["deployment"] == "llm-services"
    assert view["deployment_endpoint"] == "chat"
    assert "private-gateway-key" not in str(view)


@pytest.mark.asyncio
async def test_managed_tts_entrypoint_authenticates_and_preserves_audio(monkeypatch):
    import httpx

    from backend.services import tts_client

    gateway(monkeypatch)
    monkeypatch.setenv("TTS_DEPLOYMENT", "tts")
    monkeypatch.delenv("CHRONICLE_TTS_URL", raising=False)
    monkeypatch.delenv("TTS_URL", raising=False)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=b"RIFF-audio")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        tts_client.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw),
    )
    assert await tts_client.synthesize_speech("Hello") == b"RIFF-audio"
    assert (
        str(seen[0].url) == "http://authority:8775/deployments/tts/proxy/tts/synthesize"
    )
    assert seen[0].headers["x-chronicle-service-token"] == "private-gateway-key"
    monkeypatch.setenv("TTS_URL", "http://stale")
    with pytest.raises(ValueError, match="cannot also"):
        await tts_client.synthesize_speech("Hello")
