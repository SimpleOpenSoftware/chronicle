"""The mock profile must implement the production SDK's HTTP streaming contract."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from backend import llm_client


@pytest.fixture
async def server():
    source = Path(__file__).resolve().parents[2] / "tests/libs/mock_llm_server.py"
    spec = importlib.util.spec_from_file_location("chronicle_mock_llm", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with TestServer(module.create_app()) as server:
        yield server, module


@pytest.fixture
async def production_client(server, monkeypatch):
    endpoint, _ = server
    async with AsyncOpenAI(
        api_key="mock", base_url=str(endpoint.make_url("/v1")), max_retries=0
    ) as client:
        operation = SimpleNamespace(
            get_client=lambda **kwargs: client,
            to_api_params=lambda: {"model": "gpt-4o-mini"},
            prepare_messages=lambda messages: messages,
        )
        registry = SimpleNamespace(get_llm_operation=lambda name: operation)
        monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)
        yield client


async def test_streaming_content_reaches_production_client_with_finish_and_usage(
    server, production_client
):
    _, module = server
    messages = [{"role": "user", "content": "Say hello in हिन्दी."}]
    events = [
        event
        async for event in llm_client.async_chat_with_tools_stream(
            messages, operation="voice_conversation", allow_fallback=False
        )
    ]
    expected = module.create_chat_response({"messages": messages})
    assert len(events) > 2
    assert (
        "".join(event["text"] for event in events if event["type"] == "content")
        == expected["choices"][0]["message"]["content"]
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["finish_reason"] == "stop"
    assert events[-1]["content"] == expected["choices"][0]["message"]["content"]
    assert {key: events[-1]["usage"][key] for key in expected["usage"]} == expected[
        "usage"
    ]


async def test_streamed_fragmented_tool_call_reassembles_production_arguments(
    server, production_client
):
    _, module = server
    messages = [
        {
            "role": "user",
            "content": "conversation_id: test-stream\nTranscript (speaker-labelled): A: I grew a trumpet flower.",
        }
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "write_note", "parameters": {"type": "object"}},
        }
    ]
    events = [
        event
        async for event in llm_client.async_chat_with_tools_stream(
            messages, tools, operation="voice_conversation", allow_fallback=False
        )
    ]
    expected = module.create_chat_response({"messages": messages, "tools": tools})
    assert len(events) == 1 and events[0]["finish_reason"] == "tool_calls"
    assert events[0]["tool_calls"] == expected["choices"][0]["message"]["tool_calls"]
    arguments = json.loads(events[0]["tool_calls"][0]["function"]["arguments"])
    assert arguments["path"] == "Conversations/test-stream.md"
    assert "trumpet flower" in arguments["content"]


@pytest.mark.parametrize("include_usage", [False, True])
async def test_http_sse_terminates_and_json_mode_matches_nonstreamed_result(
    server, include_usage
):
    endpoint, _ = server
    body = {
        "messages": [{"role": "user", "content": "Judge this"}],
        "response_format": {"type": "json_object"},
    }
    async with aiohttp.ClientSession() as client:
        async with client.post(
            endpoint.make_url("/v1/chat/completions"), json=body
        ) as response:
            expected = await response.json()
        async with client.post(
            endpoint.make_url("/v1/chat/completions"),
            json={
                **body,
                "stream": True,
                "stream_options": {"include_usage": include_usage},
            },
        ) as response:
            assert response.content_type == "text/event-stream"
            frames = (await response.text()).strip().split("\n\n")
    assert frames[-1] == "data: [DONE]"
    chunks = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    content = "".join(
        choice["delta"].get("content", "")
        for chunk in chunks
        for choice in chunk["choices"]
    )
    assert json.loads(content) == json.loads(
        expected["choices"][0]["message"]["content"]
    )
    assert [chunk["usage"] for chunk in chunks if "usage" in chunk] == (
        [expected["usage"]] if include_usage else []
    )
    assert [
        choice["finish_reason"]
        for chunk in chunks
        for choice in chunk["choices"]
        if choice["finish_reason"]
    ] == ["stop"]
