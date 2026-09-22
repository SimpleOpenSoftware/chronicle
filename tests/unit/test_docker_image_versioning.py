"""Tests for the Docker image versioning & GHCR deployment feature.

Covers three areas without requiring Docker or network access:
  1. services.py  --use-prebuilt flag (argument parsing + env-var injection)
  2. docker-compose.yml files contain the expected image: fields
  3. push-images.sh / pull-images.sh reject missing inputs
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# Helper: import services.py from the repo root (it lives there, not in a pkg).
# services.py depends on python-dotenv and rich which may not be installed in
# the lightweight test environment, so we stub them at sys.modules level.
# ---------------------------------------------------------------------------


def _stub_missing(name: str, attrs: dict):
    """Insert a minimal fake module under *name* if it isn't already importable."""
    if name in sys.modules:
        return
    fake = MagicMock()
    for k, v in attrs.items():
        setattr(fake, k, v)
    sys.modules[name] = fake


def _import_services():
    # Stub third-party deps that aren't installed in the bare test runner
    _stub_missing("dotenv", {"dotenv_values": lambda path: {}})
    _stub_missing("rich", {})
    _stub_missing("rich.console", {"Console": MagicMock})
    _stub_missing("rich.markup", {"escape": lambda value: value})
    _stub_missing("rich.table", {"Table": MagicMock})
    _stub_missing(
        "chronicle_setup",
        {
            "ConfigManager": MagicMock,
            "ensure_tailscale_cert": lambda *a, **kw: None,
            "read_env_value": lambda *a, **kw: None,
        },
    )

    spec = importlib.util.spec_from_file_location("services", REPO_ROOT / "services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# 1. services.py — --use-prebuilt flag
# ===========================================================================


class TestUsePrebuiltFlag:
    @pytest.mark.parametrize(
        "override,registry",
        [
            ({}, "ghcr.io/simpleopensoftware/"),
            ({"DOCKERHUB_USERNAME": "myuser"}, "myuser/"),
            (
                {
                    "CHRONICLE_REGISTRY": "ghcr.io/custom/",
                    "DOCKERHUB_USERNAME": "myuser",
                },
                "ghcr.io/custom/",
            ),
        ],
    )
    def test_prebuilt_request_reaches_engine_and_restores_environment(
        self, monkeypatch, override, registry
    ):
        # Local imports run after this test module installs lightweight dependency stubs.
        from contextlib import nullcontext

        import deployment_guard
        import service_cli
        import services
        import status

        calls = []
        for key in ("CHRONICLE_REGISTRY", "CHRONICLE_TAG", "DOCKERHUB_USERNAME"):
            monkeypatch.delenv(key, raising=False)
        for key, value in override.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(services, "check_service_enabled", lambda _: True)
        monkeypatch.setattr(services, "ensure_docker_network", lambda: True)
        monkeypatch.setattr(services, "firewall_sync", lambda **kw: True)
        monkeypatch.setattr(
            deployment_guard, "activation", lambda *a, **kw: nullcontext()
        )
        monkeypatch.setattr(
            status,
            "collect_services",
            lambda names: {n: {"ready": True} for n in names},
        )
        monkeypatch.setattr(
            services,
            "run_compose_command",
            lambda *a, **kw: calls.append(
                (
                    os.environ.get("CHRONICLE_REGISTRY"),
                    os.environ.get("CHRONICLE_TAG"),
                    kw["build"],
                )
            )
            or True,
        )
        assert (
            service_cli.main(["start", "tts", "--direct", "--use-prebuilt", "v1.0.0"])
            == 0
        )
        assert calls == [(registry, "v1.0.0", False)]
        assert os.environ.get("CHRONICLE_REGISTRY") == override.get(
            "CHRONICLE_REGISTRY"
        )
        assert "CHRONICLE_TAG" not in os.environ


# ===========================================================================
# 2. docker-compose YAML validation
# ===========================================================================


def _load_compose(relative_path: str) -> dict:
    path = REPO_ROOT / relative_path
    with open(path) as f:
        return yaml.safe_load(f)


def _image_for(compose: dict, service: str) -> str | None:
    return compose.get("services", {}).get(service, {}).get("image")


def _has_chronicle_vars(image_str: str | None) -> bool:
    """True when the image field uses both CHRONICLE_REGISTRY and CHRONICLE_TAG."""
    if image_str is None:
        return False
    return "CHRONICLE_REGISTRY" in image_str and "CHRONICLE_TAG" in image_str


class TestBackendDockerComposeImages:
    COMPOSE = _load_compose("backend/docker-compose.yml")

    def test_chronicle_backend_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "chronicle-backend"))

    def test_workers_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "workers"))

    def test_annotation_cron_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "annotation-cron"))

    def test_webui_dev_is_built_locally(self):
        webui = self.COMPOSE["services"]["webui-dev"]
        assert "build" in webui
        assert "image" not in webui

    def test_webui_dev_build_and_runtime_include_audio_v2_contracts(self):
        webui = self.COMPOSE["services"]["webui-dev"]
        assert webui["build"] == {
            "context": "..",
            "dockerfile": "backend/webui/Dockerfile.dev",
        }
        volumes = webui["volumes"]
        assert (
            "../contracts/audio/v2/typescript:"
            "/workspace/contracts/audio/v2/typescript:ro"
        ) in volumes
        assert not any("voice_protocol/v1" in volume for volume in volumes)

    def test_webui_dev_mounts_tailwind_palette_configuration(self):
        volumes = self.COMPOSE["services"]["webui-dev"]["volumes"]
        assert (
            "./webui/tailwind.config.js:"
            "/workspace/backend/webui/tailwind.config.js:ro"
        ) in volumes
        assert (
            "./webui/chronicle-espresso-preset.js:"
            "/workspace/backend/webui/chronicle-espresso-preset.js:ro"
        ) in volumes

    def test_backend_services_share_same_image_name(self):
        """chronicle-backend, workers, and annotation-cron should use the same image."""
        backend_img = _image_for(self.COMPOSE, "chronicle-backend")
        workers_img = _image_for(self.COMPOSE, "workers")
        cron_img = _image_for(self.COMPOSE, "annotation-cron")
        assert backend_img == workers_img == cron_img

    def test_image_names_with_defaults_are_local(self):
        """With empty env vars the image names should have no registry prefix."""
        for service in ("chronicle-backend", "workers", "annotation-cron"):
            image = _image_for(self.COMPOSE, service)
            # The default expansion of ${CHRONICLE_REGISTRY:-} is ""
            # so the name should start with "chronicle-"
            assert image is not None
            assert "chronicle-" in image


class TestSpeakerRecognitionDockerComposeImages:
    COMPOSE = _load_compose("extras/speaker-recognition/docker-compose.yml")

    def test_speaker_service_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "speaker-service"))

    def test_web_ui_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "web-ui"))

    def test_caddy_image_is_not_changed(self):
        """Third-party caddy image must stay unchanged (no chronicle vars)."""
        caddy_img = _image_for(self.COMPOSE, "caddy")
        assert caddy_img is not None
        assert "CHRONICLE_REGISTRY" not in (caddy_img or "")


class TestAsrServicesDockerComposeImages:
    COMPOSE = _load_compose("extras/asr-services/docker-compose.yml")

    @pytest.mark.parametrize(
        "service",
        [
            "nemo-asr",
            "faster-whisper-asr",
            "vibevoice-asr",
            "transformers-asr",
            "qwen3-asr-wrapper",
            "qwen3-asr-bridge",
        ],
    )
    def test_asr_service_has_chronicle_image(self, service):
        assert _has_chronicle_vars(
            _image_for(self.COMPOSE, service)
        ), f"Service '{service}' is missing CHRONICLE_REGISTRY/CHRONICLE_TAG in image: field"

    def test_all_asr_images_are_distinct(self):
        """Each ASR service must resolve to a different image name."""
        services = [
            "nemo-asr",
            "faster-whisper-asr",
            "vibevoice-asr",
            "transformers-asr",
            "qwen3-asr-wrapper",
            "qwen3-asr-bridge",
        ]
        images = [_image_for(self.COMPOSE, s) for s in services]
        assert len(images) == len(
            set(images)
        ), "ASR service image names must all be unique"


class TestHavpeRelayDockerComposeImages:
    COMPOSE = _load_compose("extras/havpe-relay/docker-compose.yml")

    def test_havpe_relay_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "havpe-relay"))


# ===========================================================================
# 3. Bash script input validation
# ===========================================================================


class TestPushScriptValidation:
    """push-images.sh must reject missing inputs without running docker."""

    SCRIPT = SCRIPTS_DIR / "push-images.sh"

    def _run(self, args: list[str], env_override: dict | None = None):
        env = {**os.environ, **(env_override or {})}
        env.pop("DOCKERHUB_USERNAME", None)  # start clean
        env.pop("CHRONICLE_PUSH_REGISTRY", None)  # start clean
        if env_override:
            env.update(env_override)
        return subprocess.run(
            ["bash", str(self.SCRIPT)] + args,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_exits_nonzero_without_any_registry_env(self):
        result = self._run(["v1.0.0"])
        assert result.returncode != 0

    def test_error_message_mentions_registry_options(self):
        result = self._run(["v1.0.0"])
        assert (
            "CHRONICLE_PUSH_REGISTRY" in result.stderr
            or "DOCKERHUB_USERNAME" in result.stderr
        )

    def test_exits_nonzero_without_tag(self):
        result = self._run([], env_override={"DOCKERHUB_USERNAME": "testuser"})
        assert result.returncode != 0

    def test_error_message_mentions_tag_when_tag_missing(self):
        result = self._run([], env_override={"DOCKERHUB_USERNAME": "testuser"})
        assert "TAG" in result.stderr

    def test_script_is_executable(self):
        assert os.access(self.SCRIPT, os.X_OK), "push-images.sh must be executable"


class TestPullScriptValidation:
    """pull-images.sh defaults to GHCR and rejects missing TAG."""

    SCRIPT = SCRIPTS_DIR / "pull-images.sh"

    def _run(self, args: list[str], env_override: dict | None = None):
        env = {**os.environ, **(env_override or {})}
        env.pop("DOCKERHUB_USERNAME", None)
        env.pop("CHRONICLE_REGISTRY", None)
        if env_override:
            env.update(env_override)
        return subprocess.run(
            ["bash", str(self.SCRIPT)] + args,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_exits_nonzero_without_tag(self):
        result = self._run([])
        assert result.returncode != 0

    def test_error_message_mentions_tag_when_tag_missing(self):
        result = self._run([])
        assert "TAG" in result.stderr

    def test_defaults_to_ghcr_without_env_vars(self):
        """pull-images.sh should NOT error when no DOCKERHUB_USERNAME is set (GHCR default)."""
        # We can't actually pull, but we can verify the script doesn't exit
        # at the validation stage. It will fail at docker pull which is fine.
        result = self._run(["v1.0.0"])
        # Should not fail at the input validation stage (returncode 1 with our error message)
        # It will fail later at docker pull, but the stderr should not contain our validation errors
        assert "DOCKERHUB_USERNAME" not in result.stderr
        assert "env var is required" not in result.stderr

    def test_script_is_executable(self):
        assert os.access(self.SCRIPT, os.X_OK), "pull-images.sh must be executable"
