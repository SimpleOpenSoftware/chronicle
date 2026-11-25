"""Fallback setup must update saved defaults without replacing custom choices."""

import importlib.util
import io
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from chronicle_setup import ConfigManager
from rich.console import Console

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def init_module():
    spec = importlib.util.spec_from_file_location(
        "fallback_backend_init", REPO_ROOT / "backend" / "init.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_factory(tmp_path, init_module):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy2(REPO_ROOT / "config" / "defaults.yml", config_dir / "defaults.yml")

    def create(config, choice=None):
        (config_dir / "config.yml").write_text(yaml.safe_dump(config))
        setup = object.__new__(init_module.ChronicleSetup)
        setup.console = Console(file=io.StringIO(), width=160)
        setup.config = {}
        setup.config_manager = ConfigManager(repo_root=tmp_path)
        setup.prompt_choice = Mock(
            side_effect=lambda _prompt, _choices, default: choice or default
        )
        setup.prompt_value = Mock(side_effect=lambda _prompt, default: default)
        setup.prompt_with_existing_masked = Mock(return_value="")
        return setup

    return create


def test_new_setup_selects_shipped_fallback(setup_factory):
    setup = setup_factory({})

    setup.setup_fallback_llm()

    assert setup.prompt_choice.call_args.args[2] == "1"
    assert (
        setup.config_manager.get_full_config()["defaults"]["fallback_llm"]
        == "fallback-llm"
    )
    assert "gpt-5.4-mini" in setup.console.file.getvalue()


@pytest.mark.parametrize("model_name", ["gpt-5-nano", "gpt-5-nano-2025-08-07"])
def test_recommended_choice_replaces_retired_saved_model(setup_factory, model_name):
    setup = setup_factory(
        {
            "defaults": {"llm": "primary", "fallback_llm": "fallback-llm"},
            "models": [
                {"name": "primary", "model_name": "custom-primary"},
                {
                    "name": "fallback-llm",
                    "model_name": model_name,
                    "model_url": "https://api.openai.com/v1",
                },
            ],
        },
        choice="1",
    )

    setup.setup_fallback_llm()

    config = setup.config_manager.get_full_config()
    saved_model = next(m for m in config["models"] if m["name"] == "fallback-llm")
    assert saved_model["model_name"] == "gpt-5.4-mini"
    assert saved_model["api_key"] == "${oc.env:OPENAI_API_KEY,''}"
    assert saved_model["model_params"]["max_tokens"] == 4000
    assert saved_model["model_params"]["reasoning_effort"] == "medium"
    assert config["models"][0] == {"name": "primary", "model_name": "custom-primary"}
    assert config["defaults"]["llm"] == "primary"
    output = setup.console.file.getvalue()
    assert model_name in output
    assert "December 11, 2026" in output
    assert "option 1" in output


def test_rerun_keeps_fallback_disabled(setup_factory):
    setup = setup_factory({"defaults": {"fallback_llm": ""}})

    setup.setup_fallback_llm()

    assert setup.prompt_choice.call_args.args[2] == "3"
    assert setup.config_manager.get_full_config()["defaults"]["fallback_llm"] == ""
    setup.prompt_with_existing_masked.assert_not_called()


@pytest.mark.parametrize("model_url", [None, "", "https://api.openai.com/v1/"])
def test_rerun_resyncs_stock_fallback_url(setup_factory, model_url):
    setup = setup_factory(
        {
            "defaults": {"fallback_llm": "fallback-llm"},
            "models": [
                {
                    "name": "fallback-llm",
                    "model_name": "gpt-5.4-mini",
                    "model_url": model_url,
                }
            ],
        }
    )

    setup.setup_fallback_llm()

    assert setup.prompt_choice.call_args.args[2] == "1"
    saved_model = setup.config_manager.get_full_config()["models"][0]
    assert saved_model["model_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize("model_name", ["custom-llama", "gpt-5.4-mini", "gpt-5-nano"])
def test_rerun_preserves_custom_endpoint_and_model(setup_factory, model_name):
    setup = setup_factory(
        {
            "defaults": {"fallback_llm": "fallback-llm"},
            "models": [
                {
                    "name": "fallback-llm",
                    "model_name": model_name,
                    "model_url": "http://local.test/v1",
                }
            ],
        }
    )

    setup.setup_fallback_llm()

    assert setup.prompt_choice.call_args.args[2] == "2"
    config = setup.config_manager.get_full_config()
    saved_model = next(m for m in config["models"] if m["name"] == "fallback-llm")
    assert saved_model["model_name"] == model_name
    assert saved_model["model_url"] == "http://local.test/v1"
    assert "December 11, 2026" not in setup.console.file.getvalue()


def test_custom_setup_suggests_supported_fallback(setup_factory):
    setup = setup_factory({}, choice="2")

    setup.setup_fallback_llm()

    config = setup.config_manager.get_full_config()
    saved_model = next(m for m in config["models"] if m["name"] == "fallback-llm")
    assert saved_model["model_name"] == "gpt-5.4-mini"
    assert saved_model["model_url"] == "https://api.openai.com/v1"
