"""Regression tests for memory-agent choices in the backend setup wizard."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def init_module():
    path = Path(__file__).resolve().parents[1] / "init.py"
    spec = importlib.util.spec_from_file_location("chronicle_backend_init_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Console:
    def __init__(self):
        self.messages = []

    def print(self, *values, **_kwargs):
        self.messages.append(" ".join(str(value) for value in values))


class _ConfigManager:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.saved = None
        self.synced = []

    def get_memory_config(self):
        return self.config.get("memory", {})

    def get_config_defaults(self):
        return self.config.get("defaults", {})

    def get_full_config(self):
        return self.config

    def save_full_config(self, config):
        self.config = config
        self.saved = deepcopy(config)

    def update_config_defaults(self, defaults):
        self.config.setdefault("defaults", {}).update(defaults)

    def sync_models_from_defaults(self, names):
        self.synced.extend(names)
        return list(names)


def _setup(module, config, *, choices, pi_model="muse-glimmer-llm"):
    setup = object.__new__(module.ChronicleSetup)
    setup.console = _Console()
    setup.config = {}
    setup.config_manager = _ConfigManager(config)
    setup.print_section = lambda _title: None
    remaining = list(choices)
    setup.prompt_choice = lambda *_args, **_kwargs: remaining.pop(0)
    setup.prompt_value = lambda *_args, **_kwargs: pi_model
    return setup


def test_standalone_llm_rerun_preserves_qwen_registry_default(init_module):
    setup = _setup(
        init_module,
        {"defaults": {"llm": "muse-glimmer-llm", "embedding": "llamacpp-embed"}},
        choices=("5",),
    )
    setup.args = SimpleNamespace()

    setup.setup_llm()

    assert setup.config_manager.config["defaults"] == {
        "llm": "muse-glimmer-llm",
        "embedding": "llamacpp-embed",
    }
    assert setup.config_manager.synced == ["muse-glimmer-llm", "llamacpp-embed"]


@pytest.mark.parametrize("recovery", [None, "pi", "codex"])
def test_rerun_preserves_explicit_write_recovery_backend(init_module, recovery):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "muse-glimmer-llm"},
            "models": [
                {
                    "name": "muse-glimmer-llm",
                    "model_type": "llm",
                    "api_family": "openai",
                }
            ],
            "memory": {
                "agents": {
                    "write": {"backend": "pi", "recovery_backend": recovery},
                    "search": {"backend": "pi"},
                },
                "backends": {"pi": {"model": "muse-glimmer-llm"}},
            },
        },
        choices=("3", "2"),
    )

    setup.setup_memory_agents()

    assert (
        setup.config_manager.saved["memory"]["agents"]["write"]["recovery_backend"]
        == recovery
    )


def test_rerun_rejects_unknown_write_recovery_backend(init_module):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "muse-glimmer-llm"},
            "memory": {
                "agents": {
                    "write": {
                        "backend": "pi",
                        "recovery_backend": "custom-recovery",
                    },
                    "search": {"backend": "pi"},
                },
                "backends": {"pi": {"model": "muse-glimmer-llm"}},
            },
        },
        choices=("3", "2"),
    )

    with pytest.raises(ValueError, match="recovery_backend must be"):
        setup.setup_memory_agents()


def test_wizard_removes_obsolete_flat_executor_and_operation(init_module):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "muse-glimmer-llm"},
            "memory": {
                "agent_executor": "codex",
                "codex": {"model": "gpt-5.4-mini"},
                "pi": {"model": "obsolete-flat-model"},
            },
            "llm_operations": {
                "memory_agent": {"model": "openai-llm", "max_tokens": 8000},
                "chat": {"model": "openai-llm"},
            },
        },
        choices=("1", "1"),
    )

    setup.setup_memory_agents()

    saved = setup.config_manager.saved
    assert "agent_executor" not in saved["memory"]
    assert "codex" not in saved["memory"]
    assert "pi" not in saved["memory"]
    assert saved["memory"]["agents"]["write"]["backend"] == "direct"
    assert "memory_agent" not in saved["llm_operations"]
    assert saved["llm_operations"]["chat"] == {"model": "openai-llm"}
    assert any(
        "Removed obsolete llm_operations.memory_agent" in message
        for message in setup.console.messages
    )


@pytest.mark.parametrize(
    ("model", "message"),
    [
        ("missing-model", "does not exist"),
        ("custom-embed", "is not an LLM"),
        ("anthropic-llm", "is not OpenAI-compatible"),
    ],
)
def test_pi_routing_must_resolve_to_openai_compatible_llm(init_module, model, message):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": model},
            "models": [
                {
                    "name": "custom-embed",
                    "model_type": "embedding",
                    "api_family": "openai",
                },
                {
                    "name": "anthropic-llm",
                    "model_type": "llm",
                    "api_family": "anthropic",
                },
            ],
            "memory": {"agents": {}, "backends": {}},
        },
        choices=("3", "2"),
    )

    with pytest.raises(ValueError, match=message):
        setup.setup_memory_agents()


def test_new_pi_config_does_not_copy_model_routing_or_derived_limits(init_module):
    model = {
        "name": "wide-context",
        "model_type": "llm",
        "api_family": "openai",
        "context_window": 65536,
    }
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": model["name"]},
            "models": [model],
            "memory": {"agents": {}, "backends": {}},
        },
        choices=("3", "2"),
    )

    setup.setup_memory_agents()

    pi = setup.config_manager.saved["memory"]["backends"]["pi"]
    assert "model" not in pi
    assert "context_window" not in pi
    assert "max_tokens" not in pi


def test_wizard_removes_obsolete_pi_model_but_preserves_explicit_limits(init_module):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "wide-context"},
            "models": [
                {
                    "name": "wide-context",
                    "model_type": "llm",
                    "api_family": "openai",
                    "context_window": 65536,
                }
            ],
            "memory": {
                "agents": {
                    "write": {"backend": "pi", "recovery_backend": None},
                    "search": {"backend": "pi"},
                },
                "backends": {
                    "pi": {
                        "model": "old-backend-node-model",
                        "context_window": 8192,
                        "max_tokens": 2048,
                    }
                },
            },
        },
        choices=("3", "2"),
    )

    setup.setup_memory_agents()

    pi = setup.config_manager.saved["memory"]["backends"]["pi"]
    assert "model" not in pi
    assert pi["context_window"] == 8192
    assert pi["max_tokens"] == 2048


def test_recovery_only_pi_uses_central_routing(init_module):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "recovery-model"},
            "models": [
                {
                    "name": "recovery-model",
                    "model_type": "llm",
                    "api_family": "openai",
                    "context_window": 8192,
                }
            ],
            "memory": {
                "agents": {
                    "write": {
                        "backend": "direct",
                        "recovery_backend": "pi",
                    },
                    "search": {"backend": "direct"},
                },
                "backends": {"pi": {"model": "obsolete-copy"}},
            },
        },
        choices=("1", "1"),
    )

    setup.setup_memory_agents()

    pi = setup.config_manager.saved["memory"]["backends"]["pi"]
    assert "model" not in pi
    assert "context_window" not in pi
    assert "max_tokens" not in pi


def test_recovery_only_pi_rejects_incompatible_model(init_module):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "anthropic-recovery"},
            "models": [
                {
                    "name": "anthropic-recovery",
                    "model_type": "llm",
                    "api_family": "anthropic",
                }
            ],
            "memory": {
                "agents": {
                    "write": {
                        "backend": "direct",
                        "recovery_backend": "pi",
                    },
                    "search": {"backend": "direct"},
                },
                "backends": {"pi": {"model": "anthropic-recovery"}},
            },
        },
        choices=("1", "1"),
    )

    with pytest.raises(ValueError, match="is not OpenAI-compatible"):
        setup.setup_memory_agents()


def test_codex_without_auth_warns_that_readiness_fails(
    init_module, monkeypatch, tmp_path
):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "muse-glimmer-llm"},
            "memory": {"agents": {}, "backends": {}},
        },
        choices=("2", "1"),
    )
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(init_module.shutil, "which", lambda _binary: "/usr/bin/codex")

    setup.setup_memory_agents()

    output = "\n".join(setup.console.messages)
    assert "readiness will fail" in output
    assert "writes recover through the direct backend" not in output


def test_recovery_only_codex_sets_auth_mount_and_warns_readiness(
    init_module, monkeypatch, tmp_path
):
    setup = _setup(
        init_module,
        {
            "defaults": {"llm": "muse-glimmer-llm"},
            "memory": {
                "agents": {
                    "write": {
                        "backend": "direct",
                        "recovery_backend": "codex",
                    },
                    "search": {"backend": "direct"},
                },
                "backends": {},
            },
        },
        choices=("1", "1"),
    )
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(init_module.shutil, "which", lambda _binary: "/usr/bin/codex")

    setup.setup_memory_agents()

    assert setup.config["CODEX_HOME_DIR"] == str(codex_home)
    assert (
        setup.config_manager.saved["memory"]["backends"]["codex"]["sandbox_mode"]
        == "danger-full-access"
    )
    assert "readiness will fail" in "\n".join(setup.console.messages)
