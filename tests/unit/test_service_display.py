"""Displayed label and ports for services whose compose hosts many providers.

``asr-services`` is one compose for every local ASR provider, so its static
SERVICES entry names only whichever provider was hardcoded. Everything that
shows a service to a human — ``status.py``, Tailnet advertising, the node
agent feeding the WebUI System page — resolves both through these helpers so
the displayed name and ports cannot drift from what is actually configured.
"""

from pathlib import Path
from types import SimpleNamespace

import services


def _install_asr_env(monkeypatch, tmp_path: Path, env: str, stt_stream: str = ""):
    """Point services.py at a throwaway tree holding an asr-services .env."""
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    (tmp_path / "extras" / "asr-services").mkdir(parents=True)
    (tmp_path / "extras" / "asr-services" / ".env").write_text(env)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text(
        f"defaults:\n  stt_stream: {stt_stream}\n"
    )


def _install_llm_config(monkeypatch, tmp_path: Path, *, llm: str, embedding: str):
    """Install the smallest merged model registry needed by service status."""
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    (tmp_path / "extras" / "llm-services").mkdir(parents=True)
    (tmp_path / "extras" / "llm-services" / ".env").write_text(
        "LLM_PORT=8083\nEMBED_PORT=8082\n"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "defaults.yml").write_text(
        "models:\n"
        "  - name: local-chat\n"
        "    model_type: llm\n"
        "    model_provider: llamacpp\n"
        "    discovery_service: chronicle-llm\n"
        "  - name: remote-chat\n"
        "    model_type: llm\n"
        "    model_provider: openrouter\n"
        "  - name: local-embed\n"
        "    model_type: embedding\n"
        "    model_provider: llamacpp\n"
        "    discovery_service: chronicle-embed\n"
        "  - name: remote-embed\n"
        "    model_type: embedding\n"
        "    model_provider: openai\n"
    )
    (tmp_path / "config" / "config.yml").write_text(
        f"defaults:\n  llm: {llm}\n  embedding: {embedding}\n"
    )


def test_label_resolves_the_configured_asr_provider(monkeypatch, tmp_path):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\n")

    assert services.service_display_label("asr-services") == "VibeVoice ASR"


def test_label_falls_back_to_the_static_description_for_an_unset_provider(
    monkeypatch, tmp_path
):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=\n")

    assert services.service_display_label("asr-services") == (
        services.SERVICES["asr-services"]["description"]
    )


def test_label_is_the_static_description_for_single_provider_services(
    monkeypatch, tmp_path
):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\n")

    assert services.service_display_label("backend") == (
        services.SERVICES["backend"]["description"]
    )


def test_ports_use_the_configured_asr_port(monkeypatch, tmp_path):
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=vibevoice\nASR_PORT=9001\n")

    assert services.service_display_ports("asr-services") == ["9001"]


def test_nemotron_reports_its_stream_port_not_asr_port(monkeypatch, tmp_path):
    # Nemotron serves batch from the streaming container, so ASR_PORT is not
    # where it listens — showing 8767 would point at nothing.
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=nemotron\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
    )

    assert services.service_display_ports("asr-services") == ["8772"]


def test_a_cloud_only_selection_reports_no_ports(monkeypatch, tmp_path):
    # No local container runs, so a port here would be a phantom. The WebUI
    # relies on this being empty rather than a stale default.
    _install_asr_env(monkeypatch, tmp_path, "ASR_PROVIDER=deepgram\n")

    assert services.service_display_ports("asr-services") == []


def test_an_active_streaming_lane_adds_its_own_port(monkeypatch, tmp_path):
    # A streaming provider can run beside a different batch provider, so both
    # containers are up and both ports belong in the display.
    streaming = services.STREAMING_ASR_PROVIDER_OPTIONS["nemotron"]
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=vibevoice\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
        stt_stream=str(streaming["model"]),
    )

    assert services.service_display_ports("asr-services") == ["8767", "8772"]


def test_a_shared_port_is_not_listed_twice(monkeypatch, tmp_path):
    # Nemotron as both batch and streaming resolves to one container on one
    # port; the two lanes must collapse rather than render ":8772, :8772".
    streaming = services.STREAMING_ASR_PROVIDER_OPTIONS["nemotron"]
    _install_asr_env(
        monkeypatch,
        tmp_path,
        "ASR_PROVIDER=nemotron\nASR_PORT=8767\nNEMOTRON_STREAM_PORT=8772\n",
        stt_stream=str(streaming["model"]),
    )

    assert services.service_display_ports("asr-services") == ["8772"]


def test_llm_health_uses_each_endpoints_configured_bind_host(monkeypatch, tmp_path):
    """A Tailnet-only chat bind must not be probed through localhost."""
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    llm_dir = tmp_path / "extras" / "llm-services"
    llm_dir.mkdir(parents=True)
    (llm_dir / ".env").write_text(
        "LLM_PORT=8083\n"
        "EMBED_PORT=8082\n"
        "LLM_BIND_HOST=100.83.66.30\n"
        "EMBED_BIND_HOST=127.0.0.1\n"
    )
    requested = []

    def get(url, timeout):
        requested.append((url, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "ok"})

    monkeypatch.setattr(services.requests, "get", get)

    assert services.check_service_health("llm-services") == ("healthy", "")
    assert requested == [
        ("http://100.83.66.30:8083/health", 2),
        ("http://127.0.0.1:8082/health", 2),
    ]


def test_llm_health_dials_loopback_for_wildcard_binds(monkeypatch, tmp_path):
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    llm_dir = tmp_path / "extras" / "llm-services"
    llm_dir.mkdir(parents=True)
    (llm_dir / ".env").write_text("LLM_BIND_HOST=0.0.0.0\nEMBED_BIND_HOST=::\n")
    requested = []

    def get(url, timeout):
        requested.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"status": "ok"})

    monkeypatch.setattr(services.requests, "get", get)

    assert services.check_service_health("llm-services") == ("healthy", "")
    assert requested == [
        "http://127.0.0.1:8083/health",
        "http://127.0.0.1:8082/health",
    ]


def test_display_ports_resolve_endpoint_port_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(services, "__file__", str(tmp_path / "services.py"))
    llm_dir = tmp_path / "extras" / "llm-services"
    llm_dir.mkdir(parents=True)
    (llm_dir / ".env").write_text("LLM_PORT=18083\nEMBED_PORT=18082\n")

    assert services.service_display_ports("llm-services") == ["18083", "18082"]


def test_remote_chat_only_requires_local_embedding_health(monkeypatch, tmp_path):
    _install_llm_config(
        monkeypatch, tmp_path, llm="remote-chat", embedding="local-embed"
    )
    requested = []
    monkeypatch.setattr(
        services.requests,
        "get",
        lambda url, timeout: requested.append(url)
        or SimpleNamespace(status_code=200, json=lambda: {"status": "ok"}),
    )

    assert services.service_display_label("llm-services") == (
        "Local llama.cpp (embeddings)"
    )
    assert services.service_display_ports("llm-services") == ["8082"]
    assert services.check_service_health("llm-services") == ("healthy", "")
    assert requested == ["http://127.0.0.1:8082/health"]


def test_local_chat_and_remote_embeddings_only_require_chat(monkeypatch, tmp_path):
    _install_llm_config(
        monkeypatch, tmp_path, llm="local-chat", embedding="remote-embed"
    )

    assert services.service_display_label("llm-services") == "Local llama.cpp (chat)"
    assert services.service_display_ports("llm-services") == ["8083"]
    assert services.service_health_endpoint_urls("llm-services") == [
        ("chat", "http://127.0.0.1:8083/health")
    ]
