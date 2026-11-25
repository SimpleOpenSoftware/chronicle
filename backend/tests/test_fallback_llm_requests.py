"""Exercise fallback requests through the configured client and the OpenAI SDK."""

import json
from pathlib import Path

import httpx
import pytest
from omegaconf import OmegaConf
from openai import AsyncOpenAI

from backend import llm_client, model_registry
from backend.model_registry import AppModels, LLMOperationConfig, ModelDef


@pytest.mark.parametrize("entrypoint", ["generate", "chat", "stream"])
@pytest.mark.parametrize(
    "operation",
    [
        "unconfigured",
        "memory_extraction",
        "memory_write",
        "memory_search",
        "timeline_merge",
        "voice_conversation",
    ],
)
@pytest.mark.parametrize("custom_model", [False, True], ids=["shipped", "custom"])
@pytest.mark.parametrize("temperature", [None, 0.6])
async def test_fallback_request_preserves_operation_and_model_contract(
    monkeypatch, entrypoint, operation, custom_model, temperature
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-fallback-key")
    defaults = OmegaConf.load(
        Path(__file__).resolve().parents[2] / "config" / "defaults.yml"
    )
    fallback_config = next(m for m in defaults.models if m.name == "fallback-llm")
    fallback = ModelDef.model_validate(
        OmegaConf.to_container(fallback_config, resolve=True)
    )
    assert fallback.model_name == "gpt-5.4-mini"
    if custom_model:
        fallback = fallback.model_copy(
            update={
                "model_name": "llama3.1",
                "model_provider": "ollama",
                "model_url": "http://local.test/v1",
            }
        )
    operation_config = (
        LLMOperationConfig.model_validate(
            OmegaConf.to_container(defaults.llm_operations[operation])
        )
        if operation in defaults.llm_operations
        else LLMOperationConfig()
    )
    primary = ModelDef(
        name=operation_config.model or "primary",
        model_type="llm",
        model_provider="ollama",
        model_name="local-primary",
        model_url="http://primary.test/v1",
        api_key="test-primary-key",
    )
    registry = AppModels(
        defaults={"llm": primary.name, "fallback_llm": fallback.name},
        models={primary.name: primary, fallback.name: fallback},
        llm_operations={operation: operation_config},
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)
    requests = []
    tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "read_note",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        if operation in {"memory_write", "memory_search"} and entrypoint != "generate"
        else None
    )
    tool_call = {
        "id": "call-note",
        "type": "function",
        "function": {"name": "read_note", "arguments": "{}"},
    }
    content = '{"ok":true}'

    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        assert request.url.path == "/v1/chat/completions"
        if body["model"] == primary.model_name:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        message = {"role": "assistant", "content": None if tools else content}
        if tools:
            message["tool_calls"] = [tool_call]
        finish_reason = "tool_calls" if tools else "stop"
        response = {
            "id": "chatcmpl-fallback",
            "object": "chat.completion",
            "created": 0,
            "model": fallback.model_name,
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
        }
        if body.get("stream"):
            delta = {"role": "assistant", "content": None if tools else content}
            if tools:
                delta["tool_calls"] = [{"index": 0, **tool_call}]
            chunk = {
                **response,
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason}
                ],
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=response)

    clients = []

    def create_client(*, api_key, base_url, is_async):
        assert is_async
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(model_registry, "create_openai_client", create_client)
    messages = [{"role": "user", "content": "Return JSON or use the note tool."}]
    try:
        if entrypoint == "generate":
            result = await llm_client.async_generate(
                messages[0]["content"], operation=operation, temperature=temperature
            )
            assert json.loads(result) == {"ok": True}
        elif entrypoint == "chat":
            result = await llm_client.async_chat_with_tools(
                messages, tools, operation=operation, temperature=temperature
            )
            if tools:
                assert (
                    result.choices[0].message.tool_calls[0].function.name == "read_note"
                )
            else:
                assert json.loads(result.choices[0].message.content) == {"ok": True}
        else:
            events = [
                event
                async for event in llm_client.async_chat_with_tools_stream(
                    messages, tools, operation=operation, temperature=temperature
                )
            ]
            assert events[-1]["type"] == "done"
            if tools:
                assert events[-1]["tool_calls"] == [tool_call]
            else:
                assert json.loads(events[-1]["content"]) == {"ok": True}
    finally:
        for client in clients:
            await client.close()

    assert len(requests) == 2
    primary_request, primary_body = requests[0]
    fallback_request, body = requests[1]
    assert primary_request.url.host == "primary.test"
    expected_temperature = (
        temperature
        if temperature is not None
        else (
            operation_config.temperature
            if operation_config.temperature is not None
            else 0.2
        )
    )
    assert primary_body["temperature"] == expected_temperature
    assert fallback_request.url.host == (
        "local.test" if custom_model else "api.openai.com"
    )
    assert fallback_request.headers["authorization"] == "Bearer test-fallback-key"
    assert body["model"] == fallback.model_name
    assert body["messages"] == messages
    budget = operation_config.max_tokens or 4000
    if custom_model:
        assert body["temperature"] == expected_temperature
        assert body["max_tokens"] == budget
        assert "max_completion_tokens" not in body
    else:
        assert "temperature" not in body
        assert "max_tokens" not in body
        assert body["max_completion_tokens"] == budget
        assert body["reasoning_effort"] == (
            operation_config.reasoning_effort or "medium"
        )
    if operation_config.response_format == "json":
        assert body["response_format"] == {"type": "json_object"}
    if tools:
        assert body["tools"] == tools
