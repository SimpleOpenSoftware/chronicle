#!/usr/bin/env python3
"""
Chronicle Service Management
Start, stop, and manage configured services
"""

import argparse
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml
from chronicle_setup import (
    FAIL,
    NOT_APPLICABLE,
    OK,
    WARN,
    CheckContext,
    ConfigManager,
    detect_tailscale_info,
    ensure_tailscale_cert,
    read_env_value,
    run_all_checks,
    worst_status,
)
from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.markup import escape
from rich.table import Table

import clients

console = Console()


def load_config_yml():
    """Load config.yml from repository root"""
    config_path = Path(__file__).parent / "config" / "config.yml"
    if not config_path.exists():
        return None

    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        console.print(
            f"[yellow]⚠️  Warning: Could not load config/config.yml: {e}[/yellow]"
        )
        return None


def _engine_config():
    """Resolve (engine, compose_argv). Precedence: env > config.yml > docker default.

    The compose front-end differs per engine: docker uses the compose plugin
    (`docker compose`); podman uses `podman-compose`, whose CLI path performs CDI
    GPU injection (podman 4.x's docker-compat socket API does not).
    """
    cfg = load_config_yml() or {}
    engine = (
        os.environ.get("CONTAINER_ENGINE") or cfg.get("container_engine") or "docker"
    )
    default_compose = "podman-compose" if engine == "podman" else "docker compose"
    compose_raw = (
        os.environ.get("COMPOSE_CMD") or cfg.get("compose_cmd") or default_compose
    )
    return engine, shlex.split(compose_raw)


def container_engine():
    """Container engine binary: 'docker' (default) or 'podman'."""
    return _engine_config()[0]


def compose_base():
    """Base argv for compose up/down/build/restart.

    e.g. ['docker', 'compose'] or ['podman-compose']. shlex-split handles both the
    two-token plugin form and the single-token podman-compose form transparently.
    """
    return list(_engine_config()[1])


def compose_ps_json(service_path):
    """List a service's containers as normalized {name, state, status, health} dicts.

    Abstracts over docker vs podman, whose `ps` outputs differ:
    - docker: `docker compose ps --format json` (one JSON object per line; native
      fields Name/State/Status/Health).
    - podman: podman-compose's `ps` is NOT docker-compatible, so query the engine
      directly, scoped by the compose project working-dir label that podman-compose
      stamps on every container. Native podman fields are Names[]/State/Status, with
      health embedded in Status (e.g. "Up 3 seconds (healthy)").

    Raises on a failed command; callers handle the exception.
    """
    engine = container_engine()
    service_path = Path(service_path)

    if engine == "podman":
        label = (
            "label=com.docker.compose.project.working_dir=" f"{service_path.resolve()}"
        )
        result = subprocess.run(
            [engine, "ps", "-a", "--filter", label, "--format", "json"],
            cwd=service_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "podman ps failed")
        out = result.stdout.strip()
        if not out:
            return []
        raw = (
            json.loads(out)
            if out.startswith("[")
            else [json.loads(line) for line in out.splitlines() if line.strip()]
        )
        containers = []
        for c in raw:
            names = c.get("Names") or []
            name = (
                names[0]
                if isinstance(names, list) and names
                else c.get("Name", "unknown")
            )
            status = c.get("Status", "unknown")
            if "(healthy)" in status:
                health = "healthy"
            elif "(unhealthy)" in status:
                health = "unhealthy"
            elif "starting" in status:
                health = "starting"
            else:
                health = "none"
            containers.append(
                {
                    "name": name,
                    "state": c.get("State", "unknown"),
                    "status": status,
                    "health": health,
                }
            )
        return containers

    # docker
    result = subprocess.run(
        compose_base() + ["ps", "--format", "json"],
        cwd=service_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose ps failed")
    containers = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "name": c.get("Name", "unknown"),
                "state": c.get("State", "unknown"),
                "status": c.get("Status", "unknown"),
                "health": c.get("Health", "none"),
            }
        )
    return containers


SERVICES = {
    "langfuse": {
        "path": "extras/langfuse",
        "compose_file": "docker-compose.yml",
        "description": "LangFuse Observability & Prompt Management",
        "ports": ["3002", "3443"],
        "ui": {
            "http_port": "3002",
            "https_port": "3443",
            "https_caddyfile": "backend/Caddyfile",
            "https_marker": "langfuse-web:3000",
        },
        "health_endpoints": [
            {
                "label": "langfuse",
                "port_env": None,
                "default_port": "3002",
                "path": "/api/public/health",
            },
        ],
    },
    "backend": {
        "path": "backend",
        "compose_file": "docker-compose.yml",
        "description": "Chronicle Backend + WebUI",
        "ports": ["8000", "5173", "443"],
        "ui": {
            "http_port": "5173",
            "http_port_env": "WEBUI_DEV_PORT",
            "https_port": "443",
            "https_caddyfile": "backend/Caddyfile",
            "https_marker": "webui-dev:5173",
        },
        "health_endpoints": [
            {
                "label": "backend",
                "port_env": "BACKEND_PUBLIC_PORT",
                "default_port": "8000",
                "path": "/readiness",
            },
        ],
    },
    "speaker-recognition": {
        "path": "extras/speaker-recognition",
        "compose_file": "docker-compose.yml",
        "description": "Speaker Recognition Service",
        "ports": ["8085", "5175", "8444"],
        "ui": {
            "http_port": "5175",
            "http_port_env": "REACT_UI_PORT",
            "https_port": "8444",
            "https_caddyfile": "extras/speaker-recognition/Caddyfile",
            "https_marker": "web-ui:",
        },
        "health_endpoints": [
            {
                "label": "speaker",
                "port_env": "SPEAKER_SERVICE_PORT",
                "default_port": "8085",
                "path": "/health",
            },
        ],
    },
    "asr-services": {
        "path": "extras/asr-services",
        "compose_file": "docker-compose.yml",
        # Provider-neutral: this compose hosts every local ASR provider. The
        # displayed name comes from service_display_label(), which resolves the
        # configured ASR_PROVIDER; this is only the fallback for an unset one.
        "description": "Offline speech-to-text (ASR)",
        "ports": ["8767"],
        "health_endpoints": [
            {
                "label": "asr",
                "port_env": "ASR_PORT",
                "default_port": "8767",
                "path": "/health",
            },
        ],
    },
    "llm-services": {
        "path": "extras/llm-services",
        "compose_file": "docker-compose.yml",
        "description": "Local LLM via llama.cpp (chat + embeddings)",
        "ports": ["8083", "8082"],
        "health_endpoints": [
            {
                "label": "chat",
                "port_env": "LLM_PORT",
                "default_port": "8083",
                "bind_host_env": "LLM_BIND_HOST",
                "path": "/health",
            },
            {
                "label": "embeddings",
                "port_env": "EMBED_PORT",
                "default_port": "8082",
                "bind_host_env": "EMBED_BIND_HOST",
                "path": "/health",
            },
        ],
    },
    "wakeword-service": {
        "path": "extras/wakeword-service",
        "compose_file": "docker-compose.yml",
        "description": "Hermes Acoustic Wake-Word Detection",
        "ports": ["8771"],
        "health_endpoints": [
            {
                "label": "wakeword",
                "port_env": "WAKEWORD_PORT",
                "default_port": "8771",
                "path": "/health",
            },
        ],
    },
    "tts": {
        "path": "extras/tts",
        "compose_file": "docker-compose.yml",
        "description": "Text-to-Speech (TADA / Fish Speech / KittenTTS / Kokoro)",
        "ports": ["8770"],
        "health_endpoints": [
            {
                "label": "tts",
                "port_env": "TTS_PORT",
                "default_port": "8770",
                "path": "/health",
            },
        ],
    },
    "colpali-service": {
        "path": "extras/colpali-service",
        "compose_file": "docker-compose.yml",
        "description": "Visual search over saved screenshots (ColPali)",
        "ports": ["8790"],
        "health_endpoints": [
            {
                "label": "colpali",
                "port_env": "COLPALI_PORT",
                "default_port": "8790",
                "path": "/health",
            },
        ],
    },
}


def service_ui_url(service_name: str, host: str) -> str | None:
    """Return the canonical browser UI URL for a managed service on ``host``.

    Browser access is declarative in :data:`SERVICES`: the plain HTTP port is the
    fallback, while HTTPS is preferred only when the expected Caddy route exists in
    the generated Caddyfile. This keeps the WebUI from guessing that a published
    port is usable (the exact gap that previously made LangFuse's :3443 reset).
    """
    service = SERVICES.get(service_name)
    ui = service.get("ui") if service else None
    if not ui:
        return None

    service_path = Path(__file__).parent / service["path"]
    env_path = service_path / ".env"
    env_values = dotenv_values(env_path) if env_path.exists() else {}

    http_port = str(env_values.get(ui.get("http_port_env"), "") or ui["http_port"])
    https_port = str(ui["https_port"])
    caddyfile = Path(__file__).parent / ui["https_caddyfile"]
    marker = ui.get("https_marker")
    https_ready = caddyfile.is_file()
    if https_ready and marker:
        try:
            https_ready = marker in caddyfile.read_text()
        except OSError:
            https_ready = False

    scheme = "https" if https_ready else "http"
    port = https_port if https_ready else http_port
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    port_suffix = (
        "" if (scheme, port) in (("http", "80"), ("https", "443")) else f":{port}"
    )
    return f"{scheme}://{display_host}{port_suffix}"


_DISCOVERY_NAMES = {
    "backend": "chronicle-backend",
    "speaker-recognition": "chronicle-speaker",
    "asr-services": "chronicle-asr",
    "llm-services": "chronicle-llm",
    "wakeword-service": "chronicle-wakeword-service",
    "tts": "chronicle-tts",
    "colpali-service": "chronicle-colpali",
}


_ADVERTISED_SERVICES_PATH = (
    Path(__file__).parent / "config" / "advertised-services.json"
)

_ASR_PROVIDER_LABELS = {
    "vibevoice": "VibeVoice ASR",
    "vibevoice-strixhalo": "VibeVoice ASR (Strix Halo)",
    "faster-whisper": "Faster Whisper ASR",
    "transformers": "Transformers ASR",
    "nemo": "NeMo ASR",
    "nemo-strixhalo": "NeMo ASR (Strix Halo)",
    "parakeet": "Parakeet ASR",
    "qwen3-asr": "Qwen3 ASR",
    "gemma4": "Gemma 4 ASR",
    "af-next": "Audio Flamingo Next ASR",
    "granite": "Granite Speech ASR",
    "nemotron": "Nemotron ASR (batch + streaming)",
}

# TTS provider key (written to extras/tts/.env by init.py) → docker compose service.
_TTS_PROVIDER_TO_SERVICE = {
    "tada": "tada-tts",
    "fish_speech": "fish-tts",
    "kittentts": "kittentts-tts",
    "kokoro": "kokoro-tts",
}

# Batch ASR provider (ASR_PROVIDER) → docker compose service in extras/asr-services.
ASR_PROVIDER_TO_SERVICE = {
    "vibevoice": "vibevoice-asr",
    "vibevoice-strixhalo": "vibevoice-asr-strixhalo",
    "faster-whisper": "faster-whisper-asr",
    "transformers": "transformers-asr",
    "nemo": "nemo-asr",
    "nemo-strixhalo": "nemo-asr-strixhalo",
    "parakeet": "parakeet-asr",
    "qwen3-asr": "qwen3-asr-wrapper",
    "gemma4": "gemma4-asr",
    "af-next": "af-next-asr",
    "granite": "granite-asr",
    # Nemotron serves BOTH batch (HTTP /transcribe) and streaming (ws /stream) from
    # one container on 8772, so the batch lane reuses the streaming service.
    "nemotron": "nemotron-stream-asr",
}

# Streaming ASR provider (STREAMING_ASR_PROVIDER) options. Single source of truth
# for the System-page streaming selector: maps the provider key to its docker
# compose service (None = cloud provider, no local container), the stt_stream
# model registry entry the pipeline should use, and a UI label. A streaming
# stt_stream can run alongside a different batch stt provider (e.g. batch=vibevoice
# on 8767, streaming=nemotron on 8772) or be a pure cloud service (smallest/deepgram).
STREAMING_ASR_PROVIDER_OPTIONS = {
    "nemotron": {
        "service": "nemotron-stream-asr",
        "model": "stt-nemotron-stream",
        "label": "Nemotron 3.5 (local · 8772)",
        "port_env": ("NEMOTRON_STREAM_PORT", "8772"),
    },
    "smallest": {
        "service": None,
        "model": "stt-smallest-stream",
        "label": "Smallest.ai PULSE (cloud)",
        "port_env": None,
    },
    "deepgram": {
        "service": None,
        "model": "stt-deepgram-stream",
        "label": "Deepgram Nova 3 (cloud)",
        "port_env": None,
    },
    "qwen3-asr": {
        "service": "qwen3-asr-bridge",
        "model": "stt-qwen3-asr-stream",
        "label": "Qwen3-ASR (local)",
        "port_env": ("ASR_STREAM_PORT", "8769"),
    },
}

# Provider key → docker compose service for streaming providers that run a local
# container (cloud providers are absent). Derived from the options above so the
# start logic and the UI selector never drift.
STREAMING_ASR_PROVIDER_TO_SERVICE = {
    key: opt["service"]
    for key, opt in STREAMING_ASR_PROVIDER_OPTIONS.items()
    if opt["service"]
}


def active_streaming_asr_provider() -> str:
    """Return the provider key whose stt_stream model is the active default.

    Reads ``defaults.stt_stream`` from config.yml (the real pipeline source of
    truth — cloud providers leave STREAMING_ASR_PROVIDER empty) and reverse-maps
    it through STREAMING_ASR_PROVIDER_OPTIONS. Empty string if unset/unknown.
    """
    try:
        cfg = yaml.safe_load(
            (Path(__file__).parent / "config" / "config.yml").read_text()
        )
        model = (cfg.get("defaults") or {}).get("stt_stream")
    except (OSError, yaml.YAMLError):
        return ""
    for key, opt in STREAMING_ASR_PROVIDER_OPTIONS.items():
        if opt["model"] == model:
            return key
    return ""


def asr_needs_local_container(env_values: dict | None = None) -> bool:
    """Whether the asr-services compose has any local container to run.

    True when either lane selects a provider that runs a local container — the
    batch lane (ASR_PROVIDER → ASR_PROVIDER_TO_SERVICE) or the streaming lane
    (STREAMING_ASR_PROVIDER → STREAMING_ASR_PROVIDER_TO_SERVICE). Cloud-only
    selections (smallest/deepgram) need no container, so asr-services can stay
    disabled in config.yml. Reads extras/asr-services/.env unless ``env_values``
    is supplied.
    """
    if env_values is None:
        env_file = Path(__file__).parent / SERVICES["asr-services"]["path"] / ".env"
        env_values = dotenv_values(env_file) if env_file.exists() else {}
    asr_provider = (env_values.get("ASR_PROVIDER") or "").strip("'\"")
    streaming_provider = (env_values.get("STREAMING_ASR_PROVIDER") or "").strip("'\"")
    return bool(ASR_PROVIDER_TO_SERVICE.get(asr_provider)) or bool(
        STREAMING_ASR_PROVIDER_TO_SERVICE.get(streaming_provider)
    )


def set_service_enabled(name: str, enabled: bool) -> bool:
    """Flip one service's enabled flag in config.yml (``services:`` section).

    Preserves comments (ConfigManager uses ruamel). Returns True if the value
    actually changed. Used by the node agent so a provider switch keeps the
    lifecycle/UI enabled set consistent with the chosen provider (a cloud ASR
    provider needs no container, a local one does).
    """
    cm = ConfigManager()
    enabled_map = cm.get_enabled_services()
    if bool(enabled_map.get(name)) == bool(enabled):
        return False
    enabled_map[name] = bool(enabled)
    cm.set_enabled_services(enabled_map)
    return True


def _asr_health_port(env_values: dict, default_port) -> str:
    """Resolve the port the active ASR provider actually serves health on.

    Most providers bind ASR_PORT (8767). Nemotron is special: when it's the
    batch provider it serves both batch + streaming from the nemotron-stream-asr
    container, which binds NEMOTRON_STREAM_PORT (8772) — NOT ASR_PORT. Probing
    ASR_PORT there leaves the service stuck "starting" forever.
    """
    provider = (env_values.get("ASR_PROVIDER") or "").strip("'\"")
    if provider == "nemotron":
        return str(env_values.get("NEMOTRON_STREAM_PORT", "8772")).strip("'\"")
    return str(env_values.get("ASR_PORT", default_port)).strip("'\"")


def service_env_values(service_name: str) -> dict:
    """Read a service's ``.env``, anchored at the repo root (cwd-independent)."""
    env_path = Path(__file__).parent / SERVICES[service_name]["path"] / ".env"
    return dotenv_values(env_path) if env_path.exists() else {}


def _required_llm_endpoint_labels() -> set[str] | None:
    """Return local llama.cpp capabilities selected by the effective model routing.

    ``None`` is a conservative fallback when configuration cannot be inspected. The
    backend performs the authoritative validation; this lightweight mirror keeps the
    host service manager independent of backend imports while avoiding phantom health
    failures for remote providers.
    """
    config_dir = Path(__file__).parent / "config"
    try:
        shipped = yaml.safe_load((config_dir / "defaults.yml").read_text()) or {}
        local_path = config_dir / "config.yml"
        local = (
            yaml.safe_load(local_path.read_text()) or {} if local_path.exists() else {}
        )
    except (OSError, yaml.YAMLError, TypeError):
        return None

    default_names = dict(shipped.get("defaults") or {})
    default_names.update(local.get("defaults") or {})

    models: dict[str, dict] = {}
    for model_list in (shipped.get("models") or [], local.get("models") or []):
        for source in model_list:
            if not isinstance(source, dict) or not source.get("name"):
                continue
            name = str(source["name"])
            # Match config_loader: a user model replaces the shipped entry by name.
            models[name] = dict(source)

    operation_models = []
    operation_names = set((shipped.get("llm_operations") or {})) | set(
        local.get("llm_operations") or {}
    )
    for operation_name in operation_names:
        merged_operation = {
            **((shipped.get("llm_operations") or {}).get(operation_name) or {}),
            **((local.get("llm_operations") or {}).get(operation_name) or {}),
        }
        if merged_operation.get("model"):
            operation_models.append(str(merged_operation["model"]))

    selected_llms = {
        str(default_names[key])
        for key in ("llm", "fast_llm", "fallback_llm")
        if default_names.get(key)
    }
    selected_llms.update(operation_models)

    required: set[str] = set()
    for name in selected_llms:
        model = models.get(name)
        if model is None:
            return None
        provider = str(model.get("model_provider") or "").lower()
        if model.get("discovery_service") == "chronicle-llm" or any(
            marker in provider for marker in ("llamacpp", "llama.cpp")
        ):
            required.add("chat")

    embedding_name = default_names.get("embedding")
    if embedding_name:
        embedding = models.get(str(embedding_name))
        if embedding is None:
            return None
        provider = str(embedding.get("model_provider") or "").lower()
        if embedding.get("discovery_service") in {
            "chronicle-embed",
            "chronicle-embedding",
        } or any(marker in provider for marker in ("llamacpp", "llama.cpp")):
            required.add("embeddings")
    return required


def _active_health_endpoints(service_name: str) -> list[dict]:
    endpoints = list(SERVICES[service_name].get("health_endpoints", []))
    if service_name != "llm-services":
        return endpoints
    required = _required_llm_endpoint_labels()
    if required is None:
        return endpoints
    return [endpoint for endpoint in endpoints if endpoint["label"] in required]


def service_display_label(service_name: str, env_values: dict | None = None) -> str:
    """Human label for a service, provider-aware where one compose hosts many.

    ``asr-services`` is a multi-provider compose whose static description names
    only one of them, so resolve the label from the configured ASR_PROVIDER.
    Every consumer (Tailnet advertising, node agent → WebUI System page) goes
    through here so the displayed name can't drift from the running provider.
    """
    service = SERVICES[service_name]
    if service_name == "llm-services":
        required = _required_llm_endpoint_labels()
        if required == {"chat"}:
            return "Local llama.cpp (chat)"
        if required == {"embeddings"}:
            return "Local llama.cpp (embeddings)"
        if required == set():
            return "Local llama.cpp (not required)"
        return service["description"]
    if service_name != "asr-services":
        return service["description"]
    if env_values is None:
        env_values = service_env_values(service_name)
    provider = (env_values.get("ASR_PROVIDER") or "").strip("'\"")
    return _ASR_PROVIDER_LABELS.get(provider, service["description"])


def service_display_ports(
    service_name: str, env_values: dict | None = None
) -> list[str]:
    """Ports to display for a service, resolved from ``.env`` where dynamic.

    For ``asr-services`` the static port list is only correct for providers that
    bind ASR_PORT: nemotron serves from NEMOTRON_STREAM_PORT, and a streaming
    lane can run its own container alongside a different batch provider. Returns
    the ports of whichever lanes actually run a container on this node, so a
    cloud-only selection yields an empty list rather than a phantom port.
    """
    service = SERVICES[service_name]
    if env_values is None:
        env_values = service_env_values(service_name)

    if service_name != "asr-services":
        # Replace every endpoint's static display default with its configured
        # host-published port. Extra UI/HTTPS ports remain unchanged.
        replacements = {
            str(endpoint["default_port"]): str(
                env_values.get(endpoint["port_env"], endpoint["default_port"])
                if endpoint.get("port_env")
                else endpoint["default_port"]
            ).strip("'\"")
            for endpoint in _active_health_endpoints(service_name)
        }
        active_defaults = {
            str(endpoint["default_port"])
            for endpoint in _active_health_endpoints(service_name)
        }
        return [
            replacements.get(str(port), str(port))
            for port in service["ports"]
            if str(port) in active_defaults
            or str(port)
            not in {
                str(endpoint["default_port"])
                for endpoint in service.get("health_endpoints", [])
            }
        ]

    ports: list[str] = []
    batch = (env_values.get("ASR_PROVIDER") or "").strip("'\"")
    if ASR_PROVIDER_TO_SERVICE.get(batch):
        ports.append(_asr_health_port(env_values, service["ports"][0]))

    streaming = active_streaming_asr_provider()
    port_env = (STREAMING_ASR_PROVIDER_OPTIONS.get(streaming) or {}).get("port_env")
    if port_env:
        key, default = port_env
        port = str(env_values.get(key, default)).strip("'\"")
        if port not in ports:
            ports.append(port)
    return ports


def _get_advertised_services() -> list[tuple[str, int, str]]:
    """Return list of (discovery_name, port, label) for configured services."""
    triples: list[tuple[str, int, str]] = []
    for svc_name, discovery_name in _DISCOVERY_NAMES.items():
        if svc_name not in SERVICES or not check_service_enabled(svc_name):
            continue
        service = SERVICES[svc_name]
        endpoints = _active_health_endpoints(svc_name)
        if not endpoints:
            continue
        if svc_name == "llm-services":
            endpoints = [e for e in endpoints if e["label"] == "chat"]
            if not endpoints:
                continue
        endpoint = endpoints[0]
        port_env = endpoint.get("port_env")
        default_port = endpoint["default_port"]
        env_values = service_env_values(svc_name)
        if port_env:
            if svc_name == "asr-services" and port_env == "ASR_PORT":
                port = int(_asr_health_port(env_values, default_port))
            else:
                port = int(env_values.get(port_env, default_port))
        else:
            port = int(default_port)

        triples.append(
            (discovery_name, port, service_display_label(svc_name, env_values))
        )
    return triples


def _write_advertised_services(triples: list[tuple[str, int, str]]) -> None:
    """Write advertised services to config/advertised-services.json for the backend."""
    data = [
        {"name": name, "port": port, "label": label} for name, port, label in triples
    ]
    _ADVERTISED_SERVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ADVERTISED_SERVICES_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _remove_advertised_services() -> None:
    """Remove advertised-services.json when discovery agent stops."""
    _ADVERTISED_SERVICES_PATH.unlink(missing_ok=True)


def _get_backend_env_path() -> Path:
    return Path(__file__).parent / "backend" / ".env"


def _langfuse_enabled_in_backend() -> bool:
    """Check if backend is configured to send traces to LangFuse."""
    backend_env_path = _get_backend_env_path()
    return all(
        read_env_value(backend_env_path, key)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
    )


def _langfuse_is_external() -> bool:
    """Check if backend is configured for an external LangFuse (not local docker)."""
    backend_env_path = _get_backend_env_path()
    host = read_env_value(backend_env_path, "LANGFUSE_HOST")
    return bool(host) and "langfuse-web" not in host


def _ensure_langfuse_env() -> bool:
    """Ensure extras/langfuse/.env exists when backend enables LangFuse."""
    service = SERVICES["langfuse"]
    service_path = Path(service["path"])
    env_path = service_path / ".env"

    if env_path.exists():
        return True

    backend_env_path = _get_backend_env_path()
    if not _langfuse_enabled_in_backend():
        console.print(
            "[yellow]⚠️  LangFuse is enabled in services list but backend is not "
            "configured for LangFuse. Skipping.[/yellow]"
        )
        return False

    if _langfuse_is_external():
        console.print(
            "[blue]ℹ️  Backend is configured for an external LangFuse instance. "
            "Local LangFuse service not needed.[/blue]"
        )
        return False

    console.print(
        "[blue]ℹ️  LangFuse enabled in backend but extras/langfuse/.env is missing. "
        "Running LangFuse init...[/blue]"
    )

    cmd = ["uv", "run", "python3", "init.py"]
    admin_email = read_env_value(backend_env_path, "ADMIN_EMAIL") or ""
    admin_password = read_env_value(backend_env_path, "ADMIN_PASSWORD") or ""
    if admin_email:
        cmd.extend(["--admin-email", admin_email])
    if admin_password:
        cmd.extend(["--admin-password", admin_password])

    try:
        result = subprocess.run(cmd, cwd=service_path)
        if result.returncode != 0:
            console.print("[red]❌ LangFuse init failed[/red]")
            return False
    except Exception as e:
        console.print(f"[red]❌ LangFuse init error: {e}[/red]")
        return False

    if not env_path.exists():
        console.print("[red]❌ LangFuse .env not created; cannot start service[/red]")
        return False

    return True


def check_service_enabled(service_name):
    """Whether a service is enabled for the lifecycle.

    Source of truth is the ``services:`` section of config/config.yml (written by
    the wizard). This is intentionally decoupled from whether a service's ``.env``
    exists — a stale or half-written ``.env`` no longer counts as "configured".
    """
    config = load_config_yml() or {}
    return bool(config.get("services", {}).get(service_name, False))


def check_service_health(service_name):
    """Check runtime health of a service by hitting its health endpoints.

    Returns (status, detail) where status is one of:
        "healthy"  — every active endpoint reports readiness
        "partial"  — some endpoints down (detail says which)
        "unhealthy" — responding but returning errors
        "stopped"  — not reachable at all
    """
    service = SERVICES[service_name]
    endpoints = _active_health_endpoints(service_name)
    if not endpoints:
        if service_name == "llm-services":
            return ("healthy", "not required")
        return ("stopped", "no endpoints defined")

    results = []  # list of (label, ok: bool)
    any_unhealthy = False

    for label, url in service_health_endpoint_urls(service_name):
        try:
            resp = requests.get(url, timeout=2)
            ready = resp.status_code == 200
            if ready:
                try:
                    data = resp.json()
                    # Local import keeps legacy health helpers cheap to import.
                    from service_health import body_ready

                    ready = body_ready(data)
                except ValueError:
                    pass
            results.append((label, ready))
            any_unhealthy = any_unhealthy or not ready
        except (requests.ConnectionError, requests.Timeout):
            results.append((label, False))

    up = [r for r in results if r[1]]
    down = [r for r in results if not r[1]]

    if len(up) == len(results):
        return ("healthy", "")
    if len(down) == len(results):
        if any_unhealthy:
            return ("unhealthy", "")
        return ("stopped", "")
    down_labels = ", ".join(r[0] for r in down)
    return ("partial", f"{down_labels} down")


def service_health_endpoint_urls(service_name: str) -> list[tuple[str, str]]:
    """Resolve node-local health URLs from the service registry and its `.env`.

    The node agent runs beside the compose project. Endpoint port and bind-host
    overrides therefore belong to that node and must be resolved there, before
    health is reported to a central Chronicle deployment.
    """
    service = SERVICES[service_name]
    env_values = service_env_values(service_name)
    urls: list[tuple[str, str]] = []

    for endpoint in _active_health_endpoints(service_name):
        port_env = endpoint.get("port_env")
        default_port = endpoint["default_port"]
        if service_name == "asr-services" and port_env == "ASR_PORT":
            port = _asr_health_port(env_values, default_port)
        elif port_env:
            port = env_values.get(port_env, default_port)
        else:
            port = default_port

        bind_host_env = endpoint.get("bind_host_env")
        host = (
            str(env_values.get(bind_host_env, "127.0.0.1")).strip("'\"")
            if bind_host_env
            else "127.0.0.1"
        )
        # Wildcard addresses are valid bind targets, not dial targets. The node
        # agent is local to the compose project, so loop back for those binds.
        if host in {"", "0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"

        port = str(port).strip("'\"")
        urls.append((endpoint["label"], f"http://{host}:{port}{endpoint['path']}"))

    return urls


# Profile-gated services in the backend compose. `https` (caddy) is auto-enabled
# from the Caddyfile; the rest are opt-in via the backend BACKEND_PROFILES env var.
_BACKEND_ALL_PROFILES = ("https", "annotation", "vault-sync", "tailscale")


def _backend_profile_flags(service_path, command):
    """Return ``--profile`` flags for the backend compose.

    On ``down``/``status`` we enable ALL profiles so the command covers
    every profile-gated service (caddy, annotation-cron, vault-syncthing,
    tailscale) — otherwise ``docker compose down`` silently leaves
    inactive-profile containers running (the stale-container bug).

    On ``up`` we only enable what's actually wanted: ``https`` when a Caddyfile is
    present (auto), plus any profiles listed in the backend's ``BACKEND_PROFILES``
    env var (comma-separated, e.g. ``annotation,vault-sync``).
    """
    if command in ("down", "status"):
        profiles = list(_BACKEND_ALL_PROFILES)
    else:  # up
        profiles = []
        caddyfile = service_path / "Caddyfile"
        if caddyfile.exists() and caddyfile.is_file():
            profiles.append("https")
        env_file = service_path / ".env"
        extra = (
            dotenv_values(env_file).get("BACKEND_PROFILES", "")
            if env_file.exists()
            else ""
        ) or ""
        for name in extra.split(","):
            name = name.strip()
            if name and name not in profiles:
                profiles.append(name)

    flags = []
    for name in profiles:
        flags.extend(["--profile", name])
    return flags


def run_compose_command(service_name, command, build=False, force_recreate=False):
    """All Chronicle-managed activations pass through the deployment authority."""
    # Local imports avoid a cycle while preserving this existing public helper.
    from deployment_guard import activation
    from service_deployments import PlacementError

    try:
        if command in ("up", "start", "restart"):
            with activation(Path(__file__).resolve().parent, service_name):
                return _run_compose_command(
                    service_name, command, build, force_recreate
                )
        return _run_compose_command(service_name, command, build, force_recreate)
    except PlacementError as exc:
        console.print(f"[red]Deployment admission refused: {escape(str(exc))}[/red]")
        return False


def _run_compose_command(service_name, command, build=False, force_recreate=False):
    """Run docker compose command for a service"""
    service = SERVICES[service_name]
    service_path = Path(service["path"])

    if not service_path.exists():
        console.print(f"[red]❌ Service directory not found: {service_path}[/red]")
        return False

    compose_file = service_path / service["compose_file"]
    if not compose_file.exists():
        console.print(f"[red]❌ Docker compose file not found: {compose_file}[/red]")
        return False

    # Step 1: If build is requested, run build separately first (no timeout for CUDA builds)
    if build and command == "up":
        # Stamp the checkout's git-describe into images whose compose files take
        # a CHRONICLE_BUILD_VERSION build arg (backend), so a running container
        # can report what code it was built from (backend /version endpoint).
        # CI overrides this with the release tag before its own compose builds.
        import updates  # lazy: updates.py imports this module

        os.environ.setdefault(
            "CHRONICLE_BUILD_VERSION", updates.repo_version()["describe"] or "dev"
        )
        # Build command - need to specify profiles for build too
        build_cmd = compose_base()

        # Add profiles to build command (needed for profile-specific services)
        if service_name == "backend":
            build_cmd.extend(_backend_profile_flags(service_path, "up"))

        elif service_name == "speaker-recognition":
            env_file = service_path / ".env"
            if env_file.exists():
                env_values = dotenv_values(env_file)
                # Derive profile from PYTORCH_CUDA_VERSION
                pytorch_version = env_values.get("PYTORCH_CUDA_VERSION", "cpu")
                if pytorch_version == "strixhalo":
                    profile = "strixhalo"
                elif pytorch_version.startswith("cu"):
                    profile = "gpu"
                else:
                    profile = "cpu"
                build_cmd.extend(["--profile", profile])

        # For asr-services, only build the selected provider(s)
        asr_service_to_build = None
        streaming_asr_service_to_build = None
        if service_name == "asr-services":
            env_file = service_path / ".env"
            if env_file.exists():
                env_values = dotenv_values(env_file)
                asr_provider = env_values.get("ASR_PROVIDER", "").strip("'\"")
                streaming_asr_provider = env_values.get(
                    "STREAMING_ASR_PROVIDER", ""
                ).strip("'\"")

                asr_service_to_build = ASR_PROVIDER_TO_SERVICE.get(asr_provider)
                streaming_asr_service_to_build = STREAMING_ASR_PROVIDER_TO_SERVICE.get(
                    streaming_asr_provider
                )

                if asr_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building ASR provider: {asr_provider} ({asr_service_to_build})[/blue]"
                    )
                if streaming_asr_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building streaming ASR provider: {streaming_asr_provider} ({streaming_asr_service_to_build})[/blue]"
                    )

        # For tts, only build the selected provider (one runs at a time)
        tts_service_to_build = None
        if service_name == "tts":
            env_file = service_path / ".env"
            if env_file.exists():
                tts_provider = (
                    dotenv_values(env_file).get("TTS_PROVIDER", "").strip("'\"")
                )
                tts_service_to_build = _TTS_PROVIDER_TO_SERVICE.get(tts_provider)
                if tts_service_to_build:
                    console.print(
                        f"[blue]ℹ️  Building TTS provider: {tts_provider} ({tts_service_to_build})[/blue]"
                    )

        build_cmd.append("build")

        # If building ASR, only build the specific service(s)
        if asr_service_to_build:
            if asr_provider == "qwen3-asr":
                # Qwen3-ASR also needs the streaming bridge built
                build_cmd.extend([asr_service_to_build, "qwen3-asr-bridge"])
            else:
                build_cmd.append(asr_service_to_build)
        if streaming_asr_service_to_build:
            build_cmd.append(streaming_asr_service_to_build)

        # If building TTS, only build the selected provider
        if tts_service_to_build:
            build_cmd.append(tts_service_to_build)

        # Run build with streaming output (no timeout)
        console.print(
            f"[cyan]🔨 Building {service_name} (this may take several minutes for CUDA/GPU builds)...[/cyan]"
        )
        try:
            process = subprocess.Popen(
                build_cmd,
                cwd=service_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            if process.stdout is None:
                raise RuntimeError(
                    "Process stdout is None - unable to read command output"
                )

            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                # escape() the raw build line so brackets in build output
                # (e.g. "COPY ... [/app/.venv ...]") aren't parsed as rich markup,
                # which previously raised MarkupError and aborted the build.
                safe = escape(line)
                if "error" in line.lower() or "failed" in line.lower():
                    console.print(f"  [red]{safe}[/red]")
                elif "Successfully" in line or "built" in line.lower():
                    console.print(f"  [green]{safe}[/green]")
                elif "Building" in line or "Step" in line:
                    console.print(f"  [cyan]{safe}[/cyan]")
                elif "warning" in line.lower():
                    console.print(f"  [yellow]{safe}[/yellow]")
                else:
                    console.print(f"  [dim]{safe}[/dim]")

            process.wait()

            if process.returncode != 0:
                console.print(f"\n[red]❌ Build failed for {service_name}[/red]")
                return False

            console.print(f"[green]✅ Build completed for {service_name}[/green]")

        except Exception as e:
            console.print(f"[red]❌ Error building {service_name}: {e}[/red]")
            return False

    # Step 2: Run the actual command (up/down/status)
    up_flags = ["up", "-d", "--remove-orphans"]
    if force_recreate:
        up_flags.append("--force-recreate")

    cmd = compose_base()

    # Add profiles for backend service (down/status cover ALL profiles so no
    # profile-gated container is left orphaned; up only enables wanted profiles).
    if service_name == "backend":
        cmd.extend(_backend_profile_flags(service_path, command))

        caddyfile_path = service_path / "Caddyfile"
        if command == "up" and caddyfile_path.exists() and caddyfile_path.is_file():
            # Only the "static" cert mode keeps a host-issued cert file that we must
            # renew. In "caddy" mode Caddy obtains and auto-renews the cert itself, so
            # we leave it alone. Renew (if missing/near expiry) before Caddy starts so
            # it comes up holding a fresh cert. Cheap no-op when still valid; never
            # blocks startup on failure.
            cert_mode = read_env_value(str(service_path / ".env"), "HTTPS_CERT_MODE")
            if cert_mode == "static":
                certs_dir = Path(__file__).parent / "certs"
                renewed = ensure_tailscale_cert(str(certs_dir))
                if renewed is True:
                    console.print("[green]✅ Renewed Tailscale TLS certificate[/green]")
                elif renewed is False:
                    console.print(
                        "[yellow]⚠️  TLS cert is near expiry but renewal failed "
                        "(Tailscale unreachable or cert issuance error). Starting with "
                        "the existing cert; HTTPS clients may see warnings.[/yellow]"
                    )

    # Handle speaker-recognition service specially
    if service_name == "speaker-recognition" and command in ["up", "down"]:
        env_file = service_path / ".env"
        if env_file.exists():
            env_values = dotenv_values(env_file)
            # Derive profile from PYTORCH_CUDA_VERSION
            pytorch_version = env_values.get("PYTORCH_CUDA_VERSION", "cpu")
            if pytorch_version == "strixhalo":
                profile = "strixhalo"
            elif pytorch_version.startswith("cu"):
                profile = "gpu"
            else:
                profile = "cpu"

            cmd.extend(["--profile", profile])

            if command == "up":
                caddyfile_path = service_path / "Caddyfile"
                https_enabled = caddyfile_path.exists() and caddyfile_path.is_file()
                if https_enabled:
                    cmd.extend(up_flags)
                else:
                    profile_to_service = {
                        "gpu": "speaker-service-gpu",
                        "strixhalo": "speaker-service-strixhalo",
                        "cpu": "speaker-service",
                    }
                    cmd.extend(
                        up_flags
                        + [profile_to_service.get(profile, "speaker-service"), "web-ui"]
                    )
            elif command == "down":
                cmd.extend(["down"])
        else:
            if command == "up":
                cmd.extend(up_flags)
            elif command == "down":
                cmd.extend(["down"])

    # Handle asr-services - start only the configured provider(s)
    elif service_name == "asr-services" and command in ["up", "down"]:
        env_file = service_path / ".env"
        asr_service_name = None
        streaming_asr_service_name = None
        asr_provider = ""

        if env_file.exists():
            env_values = dotenv_values(env_file)
            asr_provider = env_values.get("ASR_PROVIDER", "").strip("'\"")
            streaming_asr_provider = env_values.get("STREAMING_ASR_PROVIDER", "").strip(
                "'\""
            )

            asr_service_name = ASR_PROVIDER_TO_SERVICE.get(asr_provider)
            streaming_asr_service_name = STREAMING_ASR_PROVIDER_TO_SERVICE.get(
                streaming_asr_provider
            )

            if asr_service_name:
                console.print(
                    f"[blue]ℹ️  Using ASR provider: {asr_provider} ({asr_service_name})[/blue]"
                )
            if streaming_asr_service_name:
                console.print(
                    f"[blue]ℹ️  Using streaming ASR provider: {streaming_asr_provider} ({streaming_asr_service_name})[/blue]"
                )

        if command == "up":
            if asr_service_name:
                services_to_start = [asr_service_name]
                # Qwen3-ASR also needs the streaming bridge
                if asr_provider == "qwen3-asr":
                    services_to_start.append("qwen3-asr-bridge")
            else:
                console.print(
                    "[yellow]⚠️  No ASR_PROVIDER configured, starting default service[/yellow]"
                )
                services_to_start = ["vibevoice-asr"]
            if streaming_asr_service_name:
                services_to_start.append(streaming_asr_service_name)
            cmd.extend(up_flags + services_to_start)
        elif command == "down":
            cmd.extend(["down"])

    # Handle tts - start only the configured provider (one runs at a time, port 8770)
    elif service_name == "tts" and command in ["up", "down"]:
        env_file = service_path / ".env"
        tts_service_name = None
        if env_file.exists():
            tts_provider = dotenv_values(env_file).get("TTS_PROVIDER", "").strip("'\"")
            tts_service_name = _TTS_PROVIDER_TO_SERVICE.get(tts_provider)
            if tts_service_name:
                console.print(
                    f"[blue]ℹ️  Using TTS provider: {tts_provider} ({tts_service_name})[/blue]"
                )

        if command == "up":
            if tts_service_name:
                cmd.extend(up_flags + [tts_service_name])
            else:
                console.print(
                    "[yellow]⚠️  No TTS_PROVIDER configured; run extras/tts/init.py[/yellow]"
                )
                cmd.extend(up_flags + ["kittentts-tts"])
        elif command == "down":
            # Plain down removes every tts container regardless of provider.
            cmd.extend(["down"])

    else:
        # Standard compose commands for other services
        if command == "up":
            cmd.extend(up_flags)
        elif command == "down":
            cmd.extend(["down"])
        elif command == "status":
            cmd.extend(["ps"])

    try:
        # Run the command with timeout (build already done if needed)
        result = subprocess.run(
            cmd,
            cwd=service_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,  # 2 minute timeout
        )

        if result.returncode == 0:
            return True
        else:
            console.print(f"[red]❌ Command failed[/red]")
            if result.stderr:
                console.print("[red]Error output:[/red]")
                for line in result.stderr.splitlines():
                    console.print(f"  [dim]{escape(line)}[/dim]")
            return False

    except subprocess.TimeoutExpired:
        console.print(
            f"[red]❌ Command timed out after 2 minutes for {service_name}[/red]"
        )
        return False
    except Exception as e:
        console.print(f"[red]❌ Error running command: {e}[/red]")
        return False


def ensure_docker_network():
    """Ensure chronicle-network exists"""
    try:
        # Check if network already exists
        engine = container_engine()
        result = subprocess.run(
            [engine, "network", "inspect", "chronicle-network"],
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            # Network doesn't exist, create it
            console.print("[blue]📡 Creating chronicle-network...[/blue]")
            subprocess.run(
                [engine, "network", "create", "chronicle-network"],
                check=True,
                capture_output=True,
            )
            console.print("[green]✅ chronicle-network created[/green]")
        else:
            console.print("[dim]📡 chronicle-network already exists[/dim]")
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to create network: {e}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]❌ Error checking/creating network: {e}[/red]")
        return False


# --- Service manager agent (native process, not Docker) ---
# Host-side HTTP API (edge/service_manager.py) that lets the backend (and thus
# the WebUI System page) start/stop/restart services and switch ASR/TTS
# providers. Runs natively because docker compose needs host bind-mount paths.

_SERVICE_MANAGER_PID = Path(__file__).parent / "edge" / ".service-manager.pid"
_SERVICE_MANAGER_LOG = Path(__file__).parent / "edge" / "service-manager.log"
_SERVICE_MANAGER_PORT = os.environ.get("SERVICE_MANAGER_PORT", "8775")


def _service_manager_running() -> bool:
    """Check if service manager agent process is alive."""
    if _service_manager_managed():
        return _unit_active("chronicle-service-manager")
    if not _SERVICE_MANAGER_PID.exists():
        return False
    try:
        pid = int(_SERVICE_MANAGER_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        _SERVICE_MANAGER_PID.unlink(missing_ok=True)
        return False


def _ensure_service_manager_token() -> str:
    """Read SERVICE_MANAGER_TOKEN from backend .env, generating it on first use.

    The backend .env is the single source of truth: the backend container gets
    the token via env_file, and the agent gets it from here at launch.
    """
    backend_env_path = _get_backend_env_path()
    token = read_env_value(backend_env_path, "SERVICE_MANAGER_TOKEN") or ""
    if not token:
        token = secrets.token_hex(24)
        backend_env_path.touch(exist_ok=True)
        set_key(
            str(backend_env_path), "SERVICE_MANAGER_TOKEN", token, quote_mode="never"
        )
        console.print(
            "[blue]ℹ️  Generated SERVICE_MANAGER_TOKEN in backend/.env[/blue]"
        )
    return token


def handle_client_command(args) -> None:
    """``./services client install|uninstall|status`` — client-node components.

    Client nodes stream data (tray, ScreenPipe collector) with no compose
    services; the components are native user units defined in clients.py.
    Install also puts the node agent on the machine (unless --no-agent) so the
    hub WebUI can see, control, and update it like any other node.
    """
    if args.client_action == "status":
        table = Table(title="Client components")
        table.add_column("Component")
        table.add_column("Description")
        table.add_column("Status")
        for name in clients.CLIENT_COMPONENTS:
            status = clients.component_status(name)
            if not status["installed"]:
                state = "[dim]not installed[/dim]"
            elif status["active"]:
                state = "[green]active[/green]"
            else:
                state = "[yellow]installed, inactive[/yellow]"
            if name == "screenpipe" and status.get("detail"):
                state += f" — {escape(status['detail'])}"
            table.add_row(name, status["description"], state)
        console.print(table)
        for check in clients.binary_checks():
            mark = "[green]✓[/green]" if check["found"] else "[yellow]✗[/yellow]"
            line = f"{mark} {check['name']} (needed by {check['needed_by']})"
            if not check["found"]:
                line += f" — {check['suggest']}"
            console.print(line)
        return

    names = args.components or ["tray"]
    invalid = [n for n in names if n not in clients.CLIENT_COMPONENTS]
    if invalid:
        console.print(f"[red]❌ Unknown component(s): {', '.join(invalid)}[/red]")
        console.print(f"Available: {', '.join(clients.CLIENT_COMPONENTS)}")
        return

    if args.client_action == "uninstall":
        for name in names:
            clients.uninstall_component(name)
            console.print(f"[green]✅ Removed {name}[/green]")
        return

    # install
    for check in clients.binary_checks():
        if not check["found"]:
            console.print(
                f"[yellow]⚠️  {check['name']} not found (needed by "
                f"{check['needed_by']}) — {check['suggest']}[/yellow]"
            )
    for name in names:
        extras = ("pendant",) if (name == "tray" and args.pendant) else ()
        try:
            clients.install_component(name, extras)
        except RuntimeError as e:
            console.print(f"[red]❌ {name}: {e}[/red]")
            continue
        console.print(f"[green]✅ Installed & started {name}[/green]")

    if args.no_agent:
        return
    # The node agent gives the hub WebUI visibility/control and delivers
    # updates (which restart these units). chronicle-stack is skipped — a
    # client node has no containers to bring up on boot.
    if sys.platform == "darwin":
        console.print(
            "[dim]Node agent auto-install is Linux/systemd-only for now — run "
            "'./services start --all' or './services manager start' to control this "
            "machine from the hub.[/dim]"
        )
    else:
        install_systemd_agents(["chronicle-service-manager"])


def _start_service_manager():
    """Start the service manager agent as a native background process."""
    # Heal legacy installs: the old standalone discovery agent is now folded in.
    _cleanup_legacy_discovery()
    if _service_manager_managed():
        result = _systemctl_user("start", "chronicle-service-manager", capture=False)
        if result.returncode:
            return False
        console.print(
            "[dim]🛠  Service manager managed by systemd (ensured started)[/dim]"
        )
        return True
    if _service_manager_running():
        console.print("[dim]🛠  Service manager already running[/dim]")
        return True

    agent_script = Path(__file__).parent / "edge" / "service_manager.py"
    if not agent_script.exists():
        console.print(
            "[yellow]⚠️  edge/service_manager.py not found, skipping[/yellow]"
        )
        return False

    env = dict(os.environ)
    env["SERVICE_MANAGER_TOKEN"] = _ensure_service_manager_token()
    env.setdefault("SERVICE_MANAGER_PORT", _SERVICE_MANAGER_PORT)

    log_file = open(_SERVICE_MANAGER_LOG, "a")
    try:
        proc = subprocess.Popen(
            [
                "uv",
                "run",
                "--with-requirements",
                "setup-requirements.txt",
                "--with",
                "fastapi",
                "--with",
                "uvicorn",
                "python",
                str(agent_script),
            ],
            cwd=Path(__file__).parent,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        console.print(f"[red]❌ Failed to start service manager: {e}[/red]")
        log_file.close()
        return False

    log_file.close()
    _SERVICE_MANAGER_PID.write_text(str(proc.pid))
    console.print(
        f"[green]✅ Service manager started (PID {proc.pid}, port {env['SERVICE_MANAGER_PORT']})[/green]"
    )
    return True


def _stop_service_manager():
    """Stop the service manager agent process."""
    if _service_manager_managed():
        console.print(
            "[dim]🛠  Service manager is a systemd service — left running "
            "(use 'systemctl --user stop chronicle-service-manager' to stop)[/dim]"
        )
        return
    if not _SERVICE_MANAGER_PID.exists():
        _remove_advertised_services()
        return

    try:
        pid = int(_SERVICE_MANAGER_PID.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        console.print(f"[green]✅ Service manager stopped (PID {pid})[/green]")
    except (ValueError, OSError):
        console.print("[dim]Service manager already stopped[/dim]")
    finally:
        _SERVICE_MANAGER_PID.unlink(missing_ok=True)
        # The node agent was the advertiser — clear the local manifest on stop.
        _remove_advertised_services()


# --- systemd user-service integration (auto-start agents on boot) ---
#
# The service manager and discovery agents are native host processes, not
# containers, so a plain ``./services start --all`` launch does not survive a reboot the way
# Docker's restart policy revives the containers. Optionally install them as
# systemd *user* services (with linger enabled) so they come back on boot. The
# unit's ExecStart re-invokes ``services.py <agent> run`` — a foreground runner
# that reuses the same token/advertise setup and then ``exec``s the agent, so
# systemd tracks the agent process directly.

_REPO_ROOT = Path(__file__).resolve().parent
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

# unit name -> per-unit systemd config. Keys:
#   subcmd            services.py subcommand for ExecStart
#   description       human description (Unit/Description=)
#   type              systemd service Type (default "exec")
#   restart           Restart= value (omit for oneshot)
#   remain_after_exit RemainAfterExit=yes (oneshot that should read as "active")
#   timeout_start_sec TimeoutStartSec= (oneshot stack `up` can be slow)
#   after             list of After= ordering deps
#   enable_now        enable --now on install (True) vs enable for boot only (False)
_SYSTEMD_UNITS = {
    "chronicle-service-manager": {
        "subcmd": "manager run",
        "description": "Chronicle node agent (WebUI control + Tailnet service advertising)",
        "type": "exec",
        "restart": "always",
        "restart_sec": 5,
        "enable_now": True,
    },
    # Boot persistence for the container stack. Rootless Podman is daemonless, so
    # unlike Docker nothing re-applies `restart:` policies after a reboot — this
    # oneshot runs the same `./services start --all` that ./services start --all uses to bring
    # the enabled stacks (per config.yml) back up. Ordered after the node agent;
    # enabled for boot only (install does not kick off a full stack `up`).
    "chronicle-stack": {
        "subcmd": "start --all",
        "stop_subcmd": "stop --all",
        "description": "Chronicle container stack (start enabled services on boot)",
        "type": "oneshot",
        "remain_after_exit": True,
        "timeout_start_sec": 900,
        "timeout_stop_sec": 900,
        "after": ["chronicle-service-manager.service"],
        "enable_now": False,
    },
}

_SYSTEMD_STATES = {
    "running",
    "degraded",
    "starting",
    "initializing",
    "maintenance",
    "stopping",
}


def _systemctl_user(*args, capture=True):
    """Run ``systemctl --user`` and return the CompletedProcess."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=capture,
        text=True,
    )


def _systemd_user_available() -> bool:
    """True if a systemd *user* instance is reachable.

    Not the case on hosts without systemd, or on WSL without ``systemd=true``
    in /etc/wsl.conf (the user bus is then unavailable).
    """
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (result.stdout or "").strip() in _SYSTEMD_STATES


def _unit_enabled(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl_user("is-enabled", unit).returncode == 0


def _unit_active(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl_user("is-active", unit).returncode == 0


def _service_manager_managed() -> bool:
    return _unit_enabled("chronicle-service-manager")


def _write_systemd_unit(unit: str) -> Path:
    cfg = _SYSTEMD_UNITS[unit]
    uv_path = shutil.which("uv") or "uv"
    # A systemd user unit gets a minimal PATH (no ~/.local/bin), so neither uv
    # nor binaries the agents shell out to (e.g. tailscale, podman-compose) would
    # resolve. Pin a sane PATH that includes uv's own directory.
    uv_dir = str(Path(uv_path).parent)
    unit_path_env = ":".join(
        [
            uv_dir,
            str(Path.home() / ".local" / "bin"),
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
    )
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    unit_lines = [f"Description={cfg['description']}"]
    for after in cfg.get("after", []):
        unit_lines.append(f"After={after}")

    service_lines = [
        f"Type={cfg.get('type', 'exec')}",
        f"WorkingDirectory={_REPO_ROOT}",
        f"Environment=PATH={unit_path_env}",
        f"ExecStart={uv_path} run --with-requirements setup-requirements.txt "
        f"python {_REPO_ROOT / 'service_cli.py'} {cfg['subcmd']}",
    ]
    if cfg.get("stop_subcmd"):
        service_lines.append(
            f"ExecStop={uv_path} run --with-requirements setup-requirements.txt "
            f"python {_REPO_ROOT / 'service_cli.py'} {cfg['stop_subcmd']}"
        )
    if cfg.get("remain_after_exit"):
        service_lines.append("RemainAfterExit=yes")
    if cfg.get("restart"):
        service_lines.append(f"Restart={cfg['restart']}")
        service_lines.append(f"RestartSec={cfg.get('restart_sec', 5)}")
    if cfg.get("timeout_start_sec") is not None:
        service_lines.append(f"TimeoutStartSec={cfg['timeout_start_sec']}")
    if cfg.get("timeout_stop_sec") is not None:
        service_lines.append(f"TimeoutStopSec={cfg['timeout_stop_sec']}")

    content = (
        "[Unit]\n"
        + "\n".join(unit_lines)
        + "\n\n[Service]\n"
        + "\n".join(service_lines)
        + "\n\n[Install]\nWantedBy=default.target\n"
    )
    unit_path = _SYSTEMD_USER_DIR / f"{unit}.service"
    unit_path.write_text(content)
    return unit_path


def _install_systemd_unit(unit: str) -> bool:
    """Write, enable and start a single user unit. Assumes systemd is available."""
    # Stop any background (Popen) instance first so the unit can bind the port.
    if unit == "chronicle-service-manager":
        _stop_service_manager()
        _cleanup_legacy_discovery()

    path = _write_systemd_unit(unit)
    # Keep the user instance (and thus the unit) alive after logout / reboot.
    subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True)
    _systemctl_user("daemon-reload")
    # The stack oneshot is boot-only: enabling it should register it for boot, not
    # trigger a full `start --all` as a side effect of install (./services start --all owns the
    # running stack). The agent enables --now so it comes up immediately.
    enable_now = _SYSTEMD_UNITS[unit].get("enable_now", True)
    enable_args = ["enable", "--now", unit] if enable_now else ["enable", unit]
    result = _systemctl_user(*enable_args, capture=False)
    if result.returncode != 0:
        console.print(f"[red]❌ Failed to enable {unit}[/red]")
        return False
    suffix = "& started" if enable_now else "(will start on boot)"
    console.print(f"[green]✅ Installed {suffix} {unit} (systemd user service)[/green]")
    console.print(f"[dim]   Unit: {path}[/dim]")
    return True


def _uninstall_systemd_unit(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        console.print("[dim]systemctl not found — nothing to uninstall[/dim]")
        return False
    _systemctl_user("disable", "--now", unit, capture=False)
    (_SYSTEMD_USER_DIR / f"{unit}.service").unlink(missing_ok=True)
    _systemctl_user("daemon-reload")
    console.print(f"[green]✅ Removed {unit} systemd user service[/green]")
    return True


def _print_systemd_unavailable_help():
    console.print(
        "[yellow]⚠️  No systemd user instance available — cannot install services.[/yellow]"
    )
    console.print(
        "[dim]   On WSL, enable it: add a '[boot]' section with 'systemd=true' to "
        "/etc/wsl.conf, run 'wsl --shutdown', then reopen the terminal.[/dim]"
    )


def install_systemd_agents(units=None) -> bool:
    """Install the given agent units (default: all) as systemd user services."""
    units = units or list(_SYSTEMD_UNITS)
    if not _systemd_user_available():
        _print_systemd_unavailable_help()
        return False
    ok = True
    for unit in units:
        ok = _install_systemd_unit(unit) and ok
    if ok:
        console.print(
            "[dim]These agents will now auto-start on boot. Manage them with "
            "'systemctl --user status/stop/restart <unit>'.[/dim]"
        )
    return ok


def uninstall_systemd_agents(units=None) -> bool:
    units = units or list(_SYSTEMD_UNITS)
    ok = True
    for unit in units:
        ok = _uninstall_systemd_unit(unit) and ok
    return ok


def _cleanup_legacy_discovery() -> None:
    """Remove the old standalone discovery agent, now folded into the node agent.

    Older installs ran a separate ``chronicle-discovery`` systemd user unit and/or
    a native ``edge/.discovery-agent.pid`` process. The node agent (service
    manager) now does the advertising, so a leftover discovery agent would
    double-advertise — and its unit's ExecStart (``services.py discovery run``) no
    longer exists, so it would crash-loop. Disable + remove it. Idempotent.
    """
    unit = "chronicle-discovery"
    unit_file = _SYSTEMD_USER_DIR / f"{unit}.service"
    if shutil.which("systemctl") and (_unit_enabled(unit) or unit_file.exists()):
        _systemctl_user("disable", "--now", unit)
        unit_file.unlink(missing_ok=True)
        _systemctl_user("daemon-reload")
        console.print("[dim]🧹 Removed legacy chronicle-discovery systemd unit[/dim]")

    pid_file = Path(__file__).parent / "edge" / ".discovery-agent.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(pid, signal.SIGTERM)
            console.print(f"[dim]🧹 Stopped legacy discovery agent (PID {pid})[/dim]")
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)


def _service_manager_exec():
    """Foreground exec of the service manager agent (for systemd ExecStart)."""
    agent_script = _REPO_ROOT / "edge" / "service_manager.py"
    if not agent_script.exists():
        console.print("[red]❌ edge/service_manager.py not found[/red]")
        sys.exit(1)
    os.environ["SERVICE_MANAGER_TOKEN"] = _ensure_service_manager_token()
    os.environ.setdefault("SERVICE_MANAGER_PORT", _SERVICE_MANAGER_PORT)
    os.chdir(_REPO_ROOT)
    # Absolute uv path: a systemd user unit's PATH does not include ~/.local/bin.
    uv = shutil.which("uv") or "uv"
    os.execv(
        uv,
        [
            uv,
            "run",
            "--with-requirements",
            "setup-requirements.txt",
            "--with",
            "fastapi",
            "--with",
            "uvicorn",
            "python",
            str(agent_script),
        ],
    )


# --- Claude remote-control session (control Claude Code from your phone) ---
#
# `claude remote-control` runs a persistent server that accepts multiple sessions
# you spawn from the Claude mobile app / claude.ai/code. It is an interactive TUI,
# so it needs a pty — we run it inside a detached tmux session on a dedicated
# socket (attach at the desktop with `tmux -L chronicle-rc attach -t
# chronicle-rc`). Persistence is via a systemd user
# unit that runs a supervisor wrapper (Type=simple, Restart=always) so it both
# survives reboot and auto-restarts if the server dies, with a WebUI start/stop
# toggle.

_CLAUDE_RC_UNIT = "chronicle-remote-control"
_CLAUDE_RC_SESSION = os.environ.get("CLAUDE_RC_SESSION", "chronicle-rc")
# Run on a DEDICATED tmux socket, never the user's default one. The tmux server
# spawns inside the systemd unit's cgroup, so a stop/restart (KillMode=control-
# group) tears down that whole server. On the shared default socket that would
# also kill the user's own working sessions; on a private socket it only affects
# remote-control. Attach with `tmux -L chronicle-rc attach -t chronicle-rc`.
_CLAUDE_RC_SOCKET = os.environ.get("CLAUDE_RC_SOCKET", "chronicle-rc")


def _rc_tmux_base() -> list[str]:
    """tmux argv prefix pinned to the remote-control's private socket."""
    return [shutil.which("tmux") or "tmux", "-L", _CLAUDE_RC_SOCKET]


def _claude_rc_dir() -> Path:
    """Directory the remote-control server (and its spawned sessions) runs in."""
    return Path(os.environ.get("CLAUDE_RC_DIR") or _REPO_ROOT)


def _claude_rc_name() -> str:
    """Session name shown in claude.ai/code (defaults to the hostname)."""
    return os.environ.get("CLAUDE_RC_NAME") or os.uname().nodename


def _claude_rc_command() -> list[str]:
    """The `claude remote-control` argv (same-dir spawn, default permission mode)."""
    claude = shutil.which("claude") or "claude"
    return [
        claude,
        "remote-control",
        "--spawn=same-dir",
        "--permission-mode=default",
        "--name",
        _claude_rc_name(),
    ]


def _tmux_session_running(session: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    return (
        subprocess.run(
            [*_rc_tmux_base(), "has-session", "-t", session],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _remote_control_managed() -> bool:
    return _unit_enabled(_CLAUDE_RC_UNIT)


def remote_control_status() -> dict:
    """Current state of the Claude remote-control session."""
    return {
        "running": _tmux_session_running(_CLAUDE_RC_SESSION),
        "managed": _remote_control_managed(),
        "session": _CLAUDE_RC_SESSION,
        "dir": str(_claude_rc_dir()),
        "name": _claude_rc_name(),
        "tmux_available": shutil.which("tmux") is not None,
        "claude_available": shutil.which("claude") is not None,
    }


def _start_remote_control_tmux() -> bool:
    """Launch the remote-control server in a detached tmux session (idempotent)."""
    if shutil.which("tmux") is None:
        console.print(
            "[red]❌ tmux not found — install tmux to run remote-control[/red]"
        )
        return False
    if shutil.which("claude") is None:
        console.print(
            "[red]❌ claude CLI not found — install Claude Code and log in first[/red]"
        )
        return False
    if _tmux_session_running(_CLAUDE_RC_SESSION):
        console.print(
            f"[dim]📱 Claude remote-control already running (tmux: {_CLAUDE_RC_SESSION})[/dim]"
        )
        return True
    rc_dir = _claude_rc_dir()
    # Pass the claude command as one string so tmux's shell runs it; when it exits
    # the session ends, so `has-session` faithfully reflects whether it is alive.
    cmd = " ".join(shlex.quote(part) for part in _claude_rc_command())
    result = subprocess.run(
        [
            *_rc_tmux_base(),
            "new-session",
            "-d",
            "-s",
            _CLAUDE_RC_SESSION,
            "-c",
            str(rc_dir),
            cmd,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(
            f"[red]❌ Failed to start remote-control: {result.stderr.strip()}[/red]"
        )
        return False
    console.print(
        f"[green]✅ Claude remote-control started in {rc_dir} "
        f"(tmux: {_CLAUDE_RC_SESSION}, name: {_claude_rc_name()})[/green]"
    )
    console.print(
        "[dim]   Spawn sessions from the Claude mobile app / claude.ai/code → Code tab.[/dim]"
    )
    return True


def _stop_remote_control_tmux() -> bool:
    if not _tmux_session_running(_CLAUDE_RC_SESSION):
        console.print("[dim]📱 Claude remote-control already stopped[/dim]")
        return True
    subprocess.run(
        [*_rc_tmux_base(), "kill-session", "-t", _CLAUDE_RC_SESSION],
        capture_output=True,
        text=True,
    )
    console.print("[green]✅ Claude remote-control stopped[/green]")
    return True


def start_remote_control() -> bool:
    """Start remote-control, deferring to systemd when it manages the unit."""
    if _remote_control_managed():
        _systemctl_user("start", _CLAUDE_RC_UNIT, capture=False)
        console.print(
            "[dim]📱 Remote-control managed by systemd (ensured started)[/dim]"
        )
        return True
    return _start_remote_control_tmux()


def stop_remote_control() -> bool:
    if _remote_control_managed():
        _systemctl_user("stop", _CLAUDE_RC_UNIT, capture=False)
        console.print("[dim]📱 Remote-control (systemd) stopped[/dim]")
        return True
    return _stop_remote_control_tmux()


def _write_remote_control_supervisor() -> Path:
    """Write the supervisor wrapper the systemd unit runs as its main process.

    The wrapper starts the detached tmux session (which gives the TUI its pty)
    and then *blocks* polling ``has-session``, so it stays alive exactly as long
    as the ``claude remote-control`` process does. When that process dies the
    session ends, the wrapper exits nonzero, and ``Restart=on-failure`` brings it
    back. Authentication failures exit successfully so an expired login does not
    create a restart loop. A
    plain ``tmux new-session -d`` could not be supervised this way: it returns 0
    immediately, so systemd lost track of the real process and never restarted
    it when it died.
    """
    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    rc_dir = _claude_rc_dir()
    rc_command = _claude_rc_command()
    claude = shlex.quote(rc_command[0])
    cmd_line = shlex.join(rc_command)
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    script_path = _SYSTEMD_USER_DIR / f"{_CLAUDE_RC_UNIT}.sh"
    # `tmux -L <socket>`: a PRIVATE server, so killing this unit's cgroup never
    # touches the user's default-socket sessions.
    script_path.write_text(f"""#!/bin/sh
# Auto-generated by services.py (_write_remote_control_supervisor). Do not edit.
set -u
SESSION={shlex.quote(_CLAUDE_RC_SESSION)}
# tmux pinned to a dedicated socket (-L) so this never disturbs other tmux servers.
TMUX="{shlex.quote(tmux)} -L {shlex.quote(_CLAUDE_RC_SOCKET)}"
# A missing or expired claude.ai login is durable until a human runs /login.
# Exit successfully so Restart=on-failure does not spin forever.
if ! {claude} auth status >/dev/null 2>&1; then
    echo "Claude authentication unavailable; run /login before starting remote-control." >&2
    exit 0
fi
# Clear any stale session of this name, then start fresh.
$TMUX kill-session -t "$SESSION" 2>/dev/null || true
$TMUX new-session -d -s "$SESSION" -c {shlex.quote(str(rc_dir))} {cmd_line} || exit 1
# Block while the remote-control TUI is alive; exit (-> systemd Restart) once it dies.
while $TMUX has-session -t "$SESSION" 2>/dev/null; do
    sleep 5
done
# An authenticated session disappearing is unexpected and should be restarted.
exit 1
""")
    script_path.chmod(0o755)
    return script_path


def _write_remote_control_unit() -> Path:
    """Write the chronicle-remote-control systemd user unit (supervised tmux)."""
    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    rc_dir = _claude_rc_dir()
    script_path = _write_remote_control_supervisor()
    uv_dir = str(Path(shutil.which("uv") or "uv").parent)
    unit_path_env = ":".join(
        [
            uv_dir,
            str(Path.home() / ".local" / "bin"),
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
    )
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = _SYSTEMD_USER_DIR / f"{_CLAUDE_RC_UNIT}.service"
    # Type=simple: the supervisor wrapper is the tracked main process and lives as
    # long as the tmux session does. Auth failures exit successfully and remain
    # stopped; unexpected session exits fail and are restarted with rate limiting.
    unit_path.write_text(f"""[Unit]
Description=Chronicle Claude remote-control session (control Claude Code from your phone)
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={rc_dir}
Environment=PATH={unit_path_env}
ExecStart=/bin/sh {script_path}
ExecStop=-{tmux} -L {_CLAUDE_RC_SOCKET} kill-session -t {_CLAUDE_RC_SESSION}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
""")
    return unit_path


def install_remote_control() -> bool:
    """Install + start the remote-control session as a systemd user service."""
    if shutil.which("claude") is None:
        console.print(
            "[red]❌ claude CLI not found — install Claude Code and log in first[/red]"
        )
        return False
    if shutil.which("tmux") is None:
        console.print("[red]❌ tmux not found — install tmux first[/red]")
        return False
    if not _systemd_user_available():
        _print_systemd_unavailable_help()
        return False
    # Drop any manual tmux instance so the unit owns the session name.
    _stop_remote_control_tmux()
    path = _write_remote_control_unit()
    subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True)
    _systemctl_user("daemon-reload")
    result = _systemctl_user("enable", "--now", _CLAUDE_RC_UNIT, capture=False)
    if result.returncode != 0:
        console.print(f"[red]❌ Failed to enable {_CLAUDE_RC_UNIT}[/red]")
        return False
    console.print(
        f"[green]✅ Installed & started {_CLAUDE_RC_UNIT} (systemd user service)[/green]"
    )
    console.print(f"[dim]   Unit: {path}[/dim]")
    console.print(
        f"[dim]   Sessions run in {_claude_rc_dir()}; spawn them from the Claude app "
        "(Code tab). Override dir/name with CLAUDE_RC_DIR / CLAUDE_RC_NAME.[/dim]"
    )
    return True


def uninstall_remote_control() -> bool:
    if shutil.which("systemctl") is None:
        console.print("[dim]systemctl not found — nothing to uninstall[/dim]")
        return False
    _systemctl_user("disable", "--now", _CLAUDE_RC_UNIT, capture=False)
    (_SYSTEMD_USER_DIR / f"{_CLAUDE_RC_UNIT}.service").unlink(missing_ok=True)
    _systemctl_user("daemon-reload")
    console.print(f"[green]✅ Removed {_CLAUDE_RC_UNIT} systemd user service[/green]")
    return True


# --- Windows Firewall (WSL2 hosts) -------------------------------------------
#
# On Windows the server runs inside WSL2 (mirrored networking): containers bind
# inside WSL, Windows exposes the ports, and Windows Defender Firewall blocks
# inbound LAN traffic unless an allow rule exists — so phones, companion Macs
# and other LAN clients silently can't connect. Docker Desktop's proxy used to
# get allowed implicitly; rootless podman gets nothing, and poking ad-hoc netsh
# rules by hand doesn't scale. These helpers own the rules in a contained way:
#
#   - every managed rule is named "Chronicle: <service> <label> <port>/<proto>"
#     — one prefix to list them all, one prefix to remove them all;
#   - rules are scoped to the local subnet + Tailscale CGNAT range, so nothing
#     is exposed to the public internet (see _FIREWALL_REMOTE_SCOPE);
#   - `sync` converges against the enabled-service set: missing rules are
#     added, stale "Chronicle: " rules (service disabled, port changed) are
#     removed. Rules without the prefix are never touched.
#
# `start` calls firewall_sync() automatically; on native Linux/macOS every
# entry point is a no-op. Adding rules needs elevation — WSL interop processes
# usually run unelevated (though Windows sshd sessions are elevated), so on
# failure the exact commands are printed for an admin PowerShell.

_FIREWALL_NETSH = "/mnt/c/Windows/System32/netsh.exe"
_FIREWALL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_FIREWALL_PREFIX = "Chronicle: "
# Ad-hoc rule names that predate the managed prefix scheme; sync removes them
# (their ports get prefixed rules instead). Only these exact names — anything
# else merely starting with "Chronicle" is not ours to touch.
_FIREWALL_LEGACY_RULES = ("Chronicle HTTPS", "Chronicle WebUI", "Chronicle Vault Sync")


def is_wsl2_host() -> bool:
    """True when running inside WSL2 with Windows interop available."""
    try:
        osrelease = Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False
    return "microsoft" in osrelease and Path(_FIREWALL_NETSH).exists()


def _firewall_specs(service_names) -> dict[str, tuple[int, str]]:
    """Desired rules for the given services: {rule_name: (port, proto)}.

    Ports are resolved from each service's .env (same overrides the health
    checks honour), so a custom port gets a matching rule and the default-port
    rule goes stale and is removed on the next sync.
    """
    specs: dict[str, tuple[int, str]] = {}

    def add(svc: str, label: str, port, proto: str = "TCP") -> None:
        try:
            port = int(str(port or "").strip("'\""))
        except ValueError:
            return
        specs[f"{_FIREWALL_PREFIX}{svc} {label} {port}/{proto}"] = (port, proto)

    for name in service_names:
        if name not in SERVICES:
            continue
        env = service_env_values(name)
        if name == "backend":
            add(name, "api", env.get("BACKEND_PUBLIC_PORT") or 8000)
            add(name, "webui", env.get("WEBUI_PORT") or 5173)
            if (env.get("HTTPS_ENABLED") or "").strip("'\"").lower() == "true":
                add(name, "https", 443)
                add(name, "http-redirect", 80)
            if env.get("VAULT_SYNC_API_KEY"):
                # Sync protocol (TCP + QUIC) and local-discovery broadcasts, so
                # vault clients and the server can find and dial each other.
                add(name, "vault-sync", 22000, "TCP")
                add(name, "vault-sync", 22000, "UDP")
                add(name, "vault-sync-discovery", 21027, "UDP")
        elif name == "langfuse":
            add(name, "web", 3002)
        elif name == "speaker-recognition":
            add(name, "api", env.get("SPEAKER_SERVICE_PORT") or 8085)
            add(name, "webui", env.get("REACT_UI_PORT") or 5175)
            if (env.get("HTTPS_ENABLED") or "").strip("'\"").lower() == "true":
                add(name, "https", 8444)
        elif name == "asr-services":
            add(name, "api", _asr_health_port(env, "8767"))
        elif name == "llm-services":
            add(name, "chat", env.get("LLM_PORT") or 8083)
            add(name, "embeddings", env.get("EMBED_PORT") or 8082)
        elif name == "wakeword-service":
            add(name, "api", env.get("WAKEWORD_PORT") or 8771)
        elif name == "tts":
            add(name, "api", env.get("TTS_PORT") or 8770)
        elif name == "colpali-service":
            add(name, "api", env.get("COLPALI_PORT") or 8790)

    if service_names:
        # The node agent runs on any start (WebUI control + Tailnet advertising).
        add(
            "node-agent",
            "api",
            os.environ.get("SERVICE_MANAGER_PORT") or _SERVICE_MANAGER_PORT,
        )
    return specs


def _firewall_existing_rules() -> tuple[list[str], list[str]] | None:
    """Existing (managed, legacy) Chronicle rule names, or None if listing failed.

    Uses PowerShell rather than parsing `netsh show rule` output, which is
    localized and unstable across Windows display languages.
    """
    try:
        result = subprocess.run(
            [
                _FIREWALL_POWERSHELL,
                "-NoProfile",
                "-Command",
                "Get-NetFirewallRule -DisplayName 'Chronicle*' -ErrorAction SilentlyContinue"
                " | Select-Object -ExpandProperty DisplayName",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    managed = [n for n in names if n.startswith(_FIREWALL_PREFIX)]
    legacy = [n for n in names if n in _FIREWALL_LEGACY_RULES]
    return managed, legacy


def _netsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_FIREWALL_NETSH, "advfirewall", "firewall", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


# LAN plus the Tailscale CGNAT range: tailscaled-in-WSL traffic never hits these
# inbound rules (it rides the tunnel), but Tailscale-on-Windows arrives with a
# 100.x source address and would be blocked by localsubnet alone. The public
# internet stays excluded either way.
_FIREWALL_REMOTE_SCOPE = "localsubnet,100.64.0.0/10"


def _firewall_add_cmd(name: str, port: int, proto: str) -> list[str]:
    # argv is passed to netsh.exe directly (no shell), so the space-containing
    # rule name needs no quoting here — only in the printed fallback commands.
    return [
        "add",
        "rule",
        f"name={name}",
        "dir=in",
        "action=allow",
        f"protocol={proto}",
        f"localport={port}",
        f"remoteip={_FIREWALL_REMOTE_SCOPE}",
    ]


def firewall_sync(quiet: bool = False) -> bool:
    """Converge Windows Firewall rules with the enabled-service set (WSL2 only).

    Always computes against ALL enabled services — never a subset — so starting
    one service can't remove another's rules. Returns True when nothing is left
    to do (including the non-WSL2 no-op case).
    """
    if not is_wsl2_host():
        return True

    enabled = [s for s in SERVICES if check_service_enabled(s)]
    desired = _firewall_specs(enabled)
    existing = _firewall_existing_rules()
    if existing is None:
        console.print(
            "[yellow]⚠️  Could not query Windows Firewall — skipping rule sync[/yellow]"
        )
        return False
    managed, legacy = existing

    to_add = {n: pp for n, pp in desired.items() if n not in managed}
    stale = [n for n in managed if n not in desired] + legacy
    if not to_add and not stale:
        if not quiet:
            console.print(
                f"[green]🧱 Windows Firewall: {len(desired)} Chronicle rules in sync[/green]"
            )
        return True

    failed: list[str] = []
    for name, (port, proto) in sorted(to_add.items()):
        result = _netsh(*_firewall_add_cmd(name, port, proto))
        if result.returncode == 0:
            console.print(f"[green]🧱 Firewall rule added: {name}[/green]")
        else:
            failed.append(
                f'netsh advfirewall firewall add rule name="{name}" dir=in '
                f"action=allow protocol={proto} localport={port} "
                f"remoteip={_FIREWALL_REMOTE_SCOPE}"
            )
    for name in stale:
        result = _netsh("delete", "rule", f"name={name}")
        if result.returncode == 0:
            console.print(f"[dim]🧱 Stale firewall rule removed: {name}[/dim]")
        else:
            failed.append(f'netsh advfirewall firewall delete rule name="{name}"')

    if failed:
        console.print(
            "[yellow]⚠️  Some firewall changes need elevation. Run in an admin "
            "PowerShell on the Windows host:[/yellow]"
        )
        for cmd in failed:
            console.print(f"   [cyan]{cmd}[/cyan]")
        return False
    return True


def firewall_list() -> None:
    """Show Chronicle-managed rules next to what the enabled services need."""
    if not is_wsl2_host():
        console.print(
            "Not a WSL2 host — Chronicle manages no Windows Firewall rules here."
        )
        return
    existing = _firewall_existing_rules()
    if existing is None:
        console.print("[red]❌ Could not query Windows Firewall[/red]")
        return
    managed, legacy = existing
    enabled = [s for s in SERVICES if check_service_enabled(s)]
    desired = _firewall_specs(enabled)
    console.print(
        f"[bold]Chronicle firewall rules[/bold] (prefix {_FIREWALL_PREFIX!r}):"
    )
    for name in sorted(set(managed) | set(desired)):
        if name in managed and name in desired:
            console.print(f"  [green]✅ {name}[/green]")
        elif name in desired:
            console.print(
                f"  [yellow]✚ {name} (missing — run 'firewall sync')[/yellow]"
            )
        else:
            console.print(f"  [dim]✗ {name} (stale — removed on next sync)[/dim]")
    for name in legacy:
        console.print(
            f"  [dim]✗ {name} (legacy ad-hoc rule — removed on next sync)[/dim]"
        )


def firewall_clear() -> bool:
    """Remove every Chronicle-managed rule (and only those)."""
    if not is_wsl2_host():
        return True
    existing = _firewall_existing_rules()
    if existing is None:
        console.print("[red]❌ Could not query Windows Firewall[/red]")
        return False
    managed, legacy = existing
    if not managed and not legacy:
        console.print("No Chronicle firewall rules found.")
        return True
    ok = True
    for name in managed + legacy:
        result = _netsh("delete", "rule", f"name={name}")
        if result.returncode == 0:
            console.print(f"[green]🧱 Removed: {name}[/green]")
        else:
            console.print(f"[red]❌ Could not remove {name} (needs elevation?)[/red]")
            ok = False
    return ok


def _compose_container_names(service_name):
    """Container names for a compose service, or [] if it isn't running.

    Goes through compose_ps_json so it works under both engines — the naming
    differs (``backend_caddy_1`` vs ``backend-caddy-1``), so these must never
    be hardcoded.
    """
    try:
        service_path = Path(__file__).parent / SERVICES[service_name]["path"]
        return [c["name"] for c in compose_ps_json(service_path) if c.get("name")]
    except Exception:
        return []


def build_check_context():
    """Assemble the host facts the checks need.

    Discovery lives here rather than in ``chronicle_setup.checks`` so that module
    stays free of imports from this one (which already imports it) and remains
    testable without a container engine.
    """
    # docker-compose-test.yml shares this working directory, and compose_ps_json is
    # scoped by that label — so the test stack's containers show up here too.
    # Probing one of those would report on the wrong deployment.
    containers = tuple(
        name
        for name in _compose_container_names("backend")
        if "-test" not in name and "_test" not in name
    )

    https_host = None
    backend_env = _get_backend_env_path()
    if backend_env.exists():
        enabled = (read_env_value(str(backend_env), "HTTPS_ENABLED") or "").lower()
        if enabled == "true":
            https_host, _ = detect_tailscale_info()

    return CheckContext(
        engine=container_engine(),
        # Probe DNS from every backend container: the fault is per-container, so
        # the first running one is a fair sample.
        dns_containers=containers,
        socket_containers=containers,
        https_host=https_host,
        operator_user=os.environ.get("USER") or None,
        network="chronicle-network",
    )


def install_tailscaled_dropin(quiet=False, interactive=True):
    """Stop systemd deleting /run/tailscale out from under container mounts.

    Returns True if the drop-in is in place afterwards. Needs root, so it is a
    no-op (not a failure) where sudo is unavailable — the watchdog's socket check
    is the fallback on those hosts.
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return False
    try:
        if _TAILSCALED_DROPIN.read_text() == _TAILSCALED_DROPIN_BODY:
            return True
    except OSError:
        pass

    sudo = ["sudo"] if interactive else ["sudo", "-n"]
    script = (
        f"mkdir -p {_TAILSCALED_DROPIN.parent} && "
        f"cat > {_TAILSCALED_DROPIN} << 'CHRONICLE_EOF'\n"
        f"{_TAILSCALED_DROPIN_BODY}"
        "CHRONICLE_EOF\n"
        "systemctl daemon-reload"
    )
    try:
        result = subprocess.run(
            [*sudo, "bash", "-c", script], capture_output=True, text=True, timeout=60
        )
    except (subprocess.SubprocessError, OSError):
        return False

    if result.returncode == 0:
        if not quiet:
            console.print(
                "[green]✅ tailscaled will now preserve /run/tailscale[/green]"
            )
        return True
    if not quiet:
        console.print(
            "[yellow]⚠️  Could not install the tailscaled drop-in (needs root).[/yellow]\n"
            f"   Run manually:\n     sudo mkdir -p {_TAILSCALED_DROPIN.parent}\n"
            f"     sudo tee {_TAILSCALED_DROPIN} <<'EOF'\n{_TAILSCALED_DROPIN_BODY}EOF\n"
            "     sudo systemctl daemon-reload"
        )
    return False


_STATUS_STYLE = {
    OK: ("[green]ok[/green]", "green"),
    WARN: ("[yellow]warn[/yellow]", "yellow"),
    FAIL: ("[red]FAIL[/red]", "red"),
    NOT_APPLICABLE: ("[dim]n/a[/dim]", "dim"),
}


def run_doctor(as_json=False, repair=False):
    """Report host-level faults that leave every service looking healthy.

    Returns a process exit code: 0 unless something actually failed, so this is
    usable from CI and from a shell one-liner.
    """
    ctx = build_check_context()
    results = run_all_checks(ctx)

    if repair:
        for result in results:
            if result.status == FAIL and result.repair is not None:
                if not as_json:
                    console.print(f"🔧 Repairing: {result.title}...")
                if result.repair():
                    # Re-run just this check so the report reflects reality.
                    results = run_all_checks(ctx)
                    break

    if as_json:
        print(
            json.dumps(
                {
                    "status": worst_status(results),
                    "checks": [r.as_dict() for r in results],
                },
                indent=2,
            )
        )
    else:
        table = Table(title="Chronicle host checks")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail", overflow="fold")
        for result in results:
            label, _ = _STATUS_STYLE.get(result.status, (result.status, ""))
            table.add_row(result.title, label, escape(result.detail))
        console.print(table)
        for result in results:
            if result.needs_attention and result.remedy:
                console.print(
                    f"\n[bold]{result.title}[/bold]\n  {escape(result.remedy)}"
                )

    return 1 if worst_status(results) == FAIL else 0


def preflight():
    """Warn about host faults before starting services.

    Deliberately advisory: a failing check never blocks a start, because a broken
    probe must not be able to keep Chronicle down. Repairs are not run here —
    ``./services doctor --repair`` and the node agent's watchdog own that.
    """
    try:
        results = run_all_checks(build_check_context())
    except Exception:
        return
    problems = [r for r in results if r.status == FAIL]
    if not problems:
        return
    console.print("\n[yellow]⚠️  Host checks found problems:[/yellow]")
    for result in problems:
        console.print(f"   • {escape(result.title)}: {escape(result.detail)}")
    console.print("   Run [bold]./services doctor[/bold] for details.\n")


def admin_main(argv=None):
    parser = argparse.ArgumentParser(description="Chronicle Service Management")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    deployment_parser = subparsers.add_parser(
        "deployments", help="Read or update the authoritative placement plan"
    )
    deployment_parser.add_argument(
        "--apply", type=Path, help="Apply a JSON plan with its expected revision"
    )
    deployment_parser.add_argument(
        "--status", action="store_true", help="Check routing and exclusion compliance"
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check for host-level faults (DNS, Tailscale, TLS, socket mounts)",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    doctor_parser.add_argument(
        "--repair",
        action="store_true",
        help="Apply the safe, idempotent repair for any failing check",
    )
    doctor_parser.add_argument(
        "--install-tailscaled-dropin",
        action="store_true",
        help="Stop systemd deleting /run/tailscale on tailscaled restart (needs root)",
    )

    # Update command — move the git checkout and restart services from it
    update_parser = subparsers.add_parser(
        "update", help="Update this node's code and restart its services"
    )
    update_parser.add_argument(
        "--check",
        action="store_true",
        help="Only check whether an update is available",
    )
    update_parser.add_argument(
        "--tag",
        metavar="REF",
        help="Update to a specific tag/ref (default: upstream branch, else latest release tag)",
    )
    update_parser.add_argument(
        "--prebuilt",
        metavar="TAG",
        help="Use prebuilt images at TAG instead of building locally",
    )
    update_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Move the checkout only; don't restart services",
    )

    # Service manager agent command
    manager_parser = subparsers.add_parser(
        "manager", help="Manage the service manager agent (WebUI start/stop control)"
    )
    manager_parser.add_argument(
        "manager_action",
        choices=["start", "stop", "restart", "run", "install", "uninstall"],
        help="Agent action ('run' = foreground, for systemd; 'install'/'uninstall' = systemd user service)",
    )

    # Client-node components: native user units (tray, ScreenPipe collector).
    # A "client node" only captures/streams data — no compose services, no GPU.
    client_parser = subparsers.add_parser(
        "client",
        help="Manage client-node components (desktop tray, ScreenPipe collector)",
    )
    client_parser.add_argument(
        "client_action", choices=["install", "uninstall", "status"]
    )
    client_parser.add_argument(
        "components",
        nargs="*",
        help=f"Components ({', '.join(clients.CLIENT_COMPONENTS)}); default: tray",
    )
    client_parser.add_argument(
        "--pendant",
        action="store_true",
        help="Tray: include BLE wearable (pendant) streaming support",
    )
    client_parser.add_argument(
        "--no-agent",
        action="store_true",
        help="Skip installing the node agent (no WebUI control / remote updates)",
    )

    # Windows Firewall command (WSL2 hosts; no-op elsewhere)
    fw_parser = subparsers.add_parser(
        "firewall",
        help="Manage Windows Firewall rules for LAN access (WSL2 hosts only)",
    )
    fw_parser.add_argument(
        "firewall_action",
        nargs="?",
        choices=["sync", "list", "clear"],
        default="sync",
        help="sync (default): converge rules with enabled services; "
        "list: show managed rules; clear: remove all Chronicle rules",
    )

    # Claude remote-control session command
    rc_parser = subparsers.add_parser(
        "remote-control",
        help="Manage the Claude remote-control session (control Claude Code from your phone)",
    )
    rc_parser.add_argument(
        "remote_control_action",
        choices=["start", "stop", "restart", "status", "install", "uninstall"],
        help="Action ('install'/'uninstall' = systemd user service for boot persistence)",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "deployments":
        # Placement dependencies load only for explicit deployment administration.
        from deployment_guard import control_headers, placement_config

        cfg = placement_config(Path(__file__).resolve().parent)
        url = cfg.get("coordinator_url", "").rstrip("/")
        if not url:
            parser.error(
                "Configure service_placement.coordinator_url and node_id first"
            )
        try:
            if args.apply:
                response = requests.put(
                    url + "/deployments",
                    json=json.loads(args.apply.read_text()),
                    headers=control_headers(Path(__file__).resolve().parent),
                    timeout=120,
                )
            else:
                response = requests.get(
                    url + ("/deployments/status" if args.status else "/deployments"),
                    headers=control_headers(Path(__file__).resolve().parent),
                    timeout=120,
                )
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
        except (requests.RequestException, ValueError, OSError) as exc:
            console.print(f"[red]Deployment request failed: {escape(str(exc))}[/red]")
            if isinstance(exc, requests.HTTPError):
                console.print(escape(exc.response.text))
            sys.exit(1)

    elif args.command == "doctor":
        if args.install_tailscaled_dropin:
            install_tailscaled_dropin()
        sys.exit(run_doctor(as_json=args.json, repair=args.repair))

    elif args.command == "update":
        import updates  # lazy: updates.py imports this module

        if args.check:
            info = updates.check_update(target=args.tag)
            cur, tgt = info["current"], info.get("target")
            where = f" (branch {cur['branch']})" if cur["branch"] else " (detached)"
            console.print(f"Current: {cur['describe']}{where}")
            if info.get("error"):
                console.print(f"[red]❌ {info['error']}[/red]")
                sys.exit(1)
            elif info["update_available"]:
                console.print(
                    f"[yellow]⬆️  Update available → {tgt['ref']} ({tgt['commit']})[/yellow]"
                )
            else:
                console.print("[green]✅ Up to date[/green]")
        else:
            ok = updates.perform_update(
                target=args.tag,
                prebuilt=args.prebuilt,
                restart_services=not args.no_restart,
            )
            sys.exit(0 if ok else 1)

    elif args.command == "manager":
        if args.manager_action in ("stop", "restart") and _service_manager_managed():
            return _systemctl_user(
                args.manager_action, "chronicle-service-manager", capture=False
            ).returncode
        if args.manager_action == "start":
            return 0 if _start_service_manager() else 1
        elif args.manager_action == "stop":
            _stop_service_manager()
        elif args.manager_action == "restart":
            _stop_service_manager()
            _start_service_manager()
        elif args.manager_action == "run":
            _service_manager_exec()
        elif args.manager_action == "install":
            # Installs the node agent AND the stack-on-boot oneshot.
            install_systemd_agents()
        elif args.manager_action == "uninstall":
            uninstall_systemd_agents()

    elif args.command == "client":
        handle_client_command(args)

    elif args.command == "firewall":
        if args.firewall_action == "sync":
            return 0 if firewall_sync() else 1
        elif args.firewall_action == "list":
            firewall_list()
        elif args.firewall_action == "clear":
            return 0 if firewall_clear() else 1

    elif args.command == "remote-control":
        if args.remote_control_action == "start":
            start_remote_control()
        elif args.remote_control_action == "stop":
            stop_remote_control()
        elif args.remote_control_action == "restart":
            stop_remote_control()
            start_remote_control()
        elif args.remote_control_action == "status":
            console.print(remote_control_status())
        elif args.remote_control_action == "install":
            install_remote_control()
        elif args.remote_control_action == "uninstall":
            uninstall_remote_control()
