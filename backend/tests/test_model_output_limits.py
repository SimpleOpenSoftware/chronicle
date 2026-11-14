"""An operation whose max_tokens its model cannot produce refuses to run.

Regression: timeline days failed with OpenAI HTTP 400 ("max_tokens is too large:
32000. This model supports at most 16384") because the shipped 32K timeline
allowance was sent unchanged to gpt-4o-mini. The fix stops before the request and
asks for a larger-output model; it does not shrink the allowance silently.
"""

from pathlib import Path

import pytest
import yaml

import backend.services.timeline.executor as timeline_executor
import backend.services.timeline.pi_executor as pi_executor
from backend.model_registry import (
    AppModels,
    LLMOperationConfig,
    ModelDef,
    OutputLimitExceeded,
    output_token_limit_findings,
)
from backend.services.memory.agent.pi_agent import _PiRuntimeConfig

DEFAULTS_YML = Path(__file__).resolve().parents[2] / "config" / "defaults.yml"


def _model(name: str, max_output_tokens=None) -> ModelDef:
    return ModelDef(
        name=name,
        model_type="llm",
        model_provider="openai",
        model_name="gpt-4o-mini",
        model_url="https://api.openai.com/v1",
        max_output_tokens=max_output_tokens,
        model_params={"max_tokens": 2000},
    )


def _registry(model: ModelDef, **operations) -> AppModels:
    return AppModels(
        defaults={"llm": model.name},
        models={model.name: model},
        llm_operations={
            name: LLMOperationConfig(max_tokens=value)
            for name, value in operations.items()
        },
    )


def test_oversized_operation_refuses_and_names_the_model():
    registry = _registry(_model("small", 16384), timeline_segmentation=32000)

    with pytest.raises(OutputLimitExceeded) as raised:
        registry.get_llm_operation("timeline_segmentation")

    message = str(raised.value)
    assert "llm_operations.timeline_segmentation.max_tokens needs 32000" in message
    assert "model 'small' accepts at most 16384" in message
    assert "Choose a model that supports at least 32000" in message


def test_operation_within_limit_is_unchanged():
    registry = _registry(_model("small", 16384), chat=8000)

    assert registry.get_llm_operation("chat").max_tokens == 8000


def test_model_without_declared_limit_passes_requests_through():
    registry = _registry(_model("local"), timeline_segmentation=32000)

    assert registry.get_llm_operation("timeline_segmentation").max_tokens == 32000


def test_route_explanation_still_works_for_a_misconfigured_operation():
    registry = _registry(_model("small", 16384), timeline_segmentation=32000)

    assert registry.explain_llm_operation("timeline_segmentation")["model"] == "small"


def test_findings_name_each_oversized_setting():
    registry = _registry(_model("small", 16384), timeline_segmentation=32000, chat=8000)

    findings = output_token_limit_findings(
        registry,
        [("timeline.pi.max_tokens", "timeline_segmentation", 32000)],
    )

    assert [f["setting"] for f in findings] == [
        "llm_operations.timeline_segmentation.max_tokens",
        "timeline.pi.max_tokens",
    ]
    assert findings[0]["model"] == "small"
    assert findings[0]["requested"] == 32000
    assert findings[0]["limit"] == 16384


def _pi_config(max_output_tokens) -> _PiRuntimeConfig:
    return _PiRuntimeConfig(
        binary="pi",
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="k",
        thinking="off",
        max_tokens=4096,
        context_window=98304,
        timeout_seconds=30,
        reasoning=False,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        model_entry="openai-llm",
    )


def test_timeline_pi_override_refuses_a_small_model(monkeypatch):
    monkeypatch.setattr(
        pi_executor, "_resolve_pi_config", lambda operation: _pi_config(16384)
    )
    executor = pi_executor.PiTimelineExecutor({"max_tokens": 32000})

    with pytest.raises(OutputLimitExceeded, match="timeline.pi.max_tokens needs 32000"):
        executor._final_stage_config(None)


def test_timeline_pi_override_kept_for_a_large_model(monkeypatch):
    monkeypatch.setattr(
        pi_executor, "_resolve_pi_config", lambda operation: _pi_config(None)
    )

    config = pi_executor.PiTimelineExecutor({"max_tokens": 32000})._final_stage_config(
        None
    )

    assert config.max_tokens == 32000


def test_timeline_preflight_refuses_before_a_day_is_queued(monkeypatch):
    monkeypatch.setattr(
        timeline_executor,
        "settings_dict",
        lambda: {"executor": "pi", "pi": {"max_tokens": 32000}},
    )
    monkeypatch.setattr(
        timeline_executor,
        "get_models_registry",
        lambda: _registry(_model("openai-llm", 16384)),
    )

    with pytest.raises(OutputLimitExceeded, match="model 'openai-llm'"):
        timeline_executor.check_output_limits()


def test_timeline_preflight_ignores_the_codex_executor(monkeypatch):
    monkeypatch.setattr(
        timeline_executor,
        "settings_dict",
        lambda: {"executor": "codex", "pi": {"max_tokens": 32000}},
    )
    monkeypatch.setattr(
        timeline_executor,
        "get_models_registry",
        lambda: _registry(_model("openai-llm", 16384)),
    )

    timeline_executor.check_output_limits()


def test_shipped_openai_models_declare_their_output_limits():
    models = {m["name"]: m for m in yaml.safe_load(DEFAULTS_YML.read_text())["models"]}

    assert models["openai-llm"]["model_name"] == "gpt-4.1-mini"
    assert models["openai-llm"]["max_output_tokens"] == 32768
    assert models["fallback-llm"]["model_name"] == "gpt-5-nano"
    assert models["fallback-llm"]["max_output_tokens"] == 128000


def test_config_diagnostics_ask_for_a_bigger_model(monkeypatch):
    # Imported here to keep the controller's import graph out of the other tests.
    import backend.controllers.system_controller as system_controller

    monkeypatch.setattr(
        system_controller,
        "load_config",
        lambda: {"timeline": {"pi": {"max_tokens": 32000}}},
    )
    registry = _registry(_model("openai-llm", 16384))

    issues = system_controller._output_limit_issues(registry)

    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert "needs a model with at least 32000 output tokens" in issues[0]["message"]
    assert "accepts at most 16384" in issues[0]["message"]
    assert issues[0]["resolution"].startswith("Settings > AI Model Settings")
