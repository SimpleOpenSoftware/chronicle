"""Reasoning parameter compatibility for configured LLM operations."""

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from backend.model_registry import (
    AppModels,
    LLMOperationConfig,
    ModelDef,
    ResolvedLLMOperation,
)
from backend.services.memory.agent.pi_agent import _thinking_level


def _operation(model_name: str) -> ResolvedLLMOperation:
    return ResolvedLLMOperation(
        model_def=ModelDef(
            name="test",
            model_name=model_name,
            model_type="llm",
            model_provider="openai",
            model_url="https://api.openai.com/v1",
        ),
        temperature=0.2,
        max_tokens=100,
        reasoning_effort="none",
    )


def test_gpt_5_4_preserves_none_reasoning_effort():
    assert _operation("gpt-5.4-mini").to_api_params()["reasoning_effort"] == "none"


def test_unversioned_gpt_5_uses_minimal_reasoning_effort():
    assert _operation("gpt-5-mini").to_api_params()["reasoning_effort"] == "minimal"


def test_openrouter_qualified_gpt_5_6_uses_reasoning_parameters():
    params = _operation("openai/gpt-5.6-luna").to_api_params()

    assert params["model"] == "openai/gpt-5.6-luna"
    assert params["max_completion_tokens"] == 100
    assert params["reasoning_effort"] == "none"
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_openrouter_non_openai_model_receives_reasoning_off():
    operation = ResolvedLLMOperation(
        model_def=ModelDef(
            name="qwen",
            model_name="qwen/qwen3.8-27b",
            model_type="llm",
            model_provider="openrouter",
            model_url="https://openrouter.ai/api/v1",
        ),
        temperature=0.0,
        max_tokens=8192,
        reasoning_effort="none",
    )

    assert operation.to_api_params()["extra_body"] == {"reasoning": {"effort": "none"}}


def test_muse_sampling_and_reasoning_prompt_follow_model_card():
    operation = ResolvedLLMOperation(
        model_def=ModelDef(
            name="muse-glimmer-llm",
            reasoning_policy="per_operation",
            model_name="meta-models/Muse-Glimmer-30B-GGUF:kquant-17gb",
            model_type="llm",
            model_provider="llamacpp",
            model_url="http://llama-cpp-llm:8080/v1",
            thinking=True,
            capabilities=["vision"],
            system_prompt_prefix="Reasoning strength: high",
            model_params={"temperature": 1.0, "top_p": 0.95, "top_k": 64},
        ),
        temperature=1.0,
        max_tokens=4096,
        reasoning_effort="high",
    )

    params = operation.to_api_params()
    assert params["temperature"] == 1.0
    assert params["top_p"] == 0.95
    assert params["extra_body"] == {
        "top_k": 64,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    original = [{"role": "system", "content": "Use Chronicle tools."}]
    prepared = operation.prepare_messages(original)
    assert prepared == [
        {
            "role": "system",
            "content": "Reasoning strength: high\n\nUse Chronicle tools.",
        }
    ]
    assert original == [{"role": "system", "content": "Use Chronicle tools."}]


def test_detailed_summary_disables_local_model_thinking():
    defaults = OmegaConf.load(
        Path(__file__).resolve().parents[2] / "config" / "defaults.yml"
    )

    assert defaults.llm_operations.detailed_summary.reasoning_effort == "none"


@pytest.mark.parametrize("name", ["timeline_merge", "timeline_session_account"])
def test_timeline_operations_emit_json_without_hidden_reasoning(name):
    defaults = OmegaConf.load(
        Path(__file__).resolve().parents[2] / "config" / "defaults.yml"
    )
    config = defaults.llm_operations[name]
    operation = ResolvedLLMOperation(
        model_def=ModelDef(
            name="qwen",
            model_name="Qwen3.8-27B",
            model_type="llm",
            model_provider="llamacpp",
            model_url="http://localhost:8080/v1",
            thinking=True,
        ),
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        reasoning_effort=config.reasoning_effort,
    )
    assert (
        operation.to_api_params()["extra_body"]["chat_template_kwargs"][
            "enable_thinking"
        ]
        is False
    )
    if name == "timeline_session_account":
        from backend.model_registry import LLM_OPERATION_DEFAULT_ROLES

        assert LLM_OPERATION_DEFAULT_ROLES[name] == "fast_llm"
        assert operation.to_api_params()["max_tokens"] > 4096


@pytest.mark.parametrize("effort", [None, "none", "low", "high"])
@pytest.mark.parametrize(
    "operation_name", ["chat", "memory_write", "new_unconfigured_operation"]
)
@pytest.mark.parametrize("allow", [False, True])
def test_local_reasoning_requires_model_permission_and_operation_opt_in(
    effort, operation_name, allow
):
    model = ModelDef(
        name="local",
        model_name="Qwen3.8-27B",
        model_type="llm",
        model_provider="llamacpp",
        model_url="http://localhost:8080/v1",
        thinking=True,
        reasoning_policy="per_operation" if allow else "off",
        model_params={"reasoning_effort": "high", "max_tokens": 2000},
    )
    registry = AppModels(
        defaults={"llm": "local"},
        models={"local": model},
        llm_operations={operation_name: LLMOperationConfig(reasoning_effort=effort)},
    )
    operation = registry.get_llm_operation(operation_name)
    enabled = operation.to_api_params()["extra_body"]["chat_template_kwargs"][
        "enable_thinking"
    ]
    assert enabled is (allow and effort in {"low", "high"})


def test_pi_adapter_override_cannot_bypass_model_reasoning_policy():
    operation = ResolvedLLMOperation(
        model_def=ModelDef(
            name="local",
            model_name="Qwen3.8-27B",
            model_type="llm",
            model_provider="llamacpp",
            model_url="http://localhost:8080/v1",
            thinking=True,
        ),
        temperature=0.0,
        reasoning_effort="high",
    )
    assert _thinking_level("high", operation) == "off"
