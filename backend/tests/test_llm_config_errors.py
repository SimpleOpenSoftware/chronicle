"""Misconfigured LLMs and unwritable settings must fail loudly, not silently."""

import os
from pathlib import Path

import pytest

from backend import config_loader
from backend.config_loader import ConfigSaveError, get_raw_models, save_config_section
from backend.model_registry import (
    AppModels,
    LLMOperationConfig,
    MissingModelCredentialsError,
    ModelDef,
)


def _model(name: str, url: str, api_key) -> ModelDef:
    return ModelDef(
        name=name,
        model_type="llm",
        model_provider="openrouter",
        api_family="openai",
        model_name=name,
        model_url=url,
        api_key=api_key,
    )


def test_hosted_model_without_key_raises_when_client_is_built():
    hosted = _model("openrouter-qwen", "https://openrouter.ai/api/v1", "")
    registry = AppModels(
        defaults={"llm": hosted.name},
        models={hosted.name: hosted},
        llm_operations={"memory_write": LLMOperationConfig(model=hosted.name)},
    )

    op = registry.get_llm_operation("memory_write")

    with pytest.raises(MissingModelCredentialsError) as error:
        op.get_client(is_async=True)
    assert "openrouter-qwen" in str(error.value)
    assert "memory_write" in str(error.value)


def test_local_model_without_key_is_allowed():
    local = _model("local-llm", "http://llama-cpp-llm:8080/v1", "")
    assert local.require_api_key() == ""


def test_hosted_model_with_key_returns_key():
    hosted = _model("openai", "https://api.openai.com/v1", "sk-test")
    assert hosted.require_api_key() == "sk-test"


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield tmp_path
    config_loader._runtime_overrides.clear()


def test_raw_models_is_empty_when_config_has_no_models_key(config_dir: Path):
    (config_dir / "config.yml").write_text("llm_operations: {}\n")
    assert get_raw_models() == []


def test_save_to_read_only_config_raises_actionable_error(config_dir: Path):
    config_file = config_dir / "config.yml"
    config_file.write_text("llm_operations: {}\n")
    os.chmod(config_dir, 0o500)
    os.chmod(config_file, 0o400)
    try:
        with pytest.raises(ConfigSaveError) as error:
            save_config_section("llm_operations", {"memory_write": {"model": "x"}})
    finally:
        os.chmod(config_dir, 0o700)
        os.chmod(config_file, 0o600)
    assert "llm_operations" in str(error.value)
