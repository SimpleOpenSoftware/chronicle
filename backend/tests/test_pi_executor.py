"""Pi memory executor: config resolution, isolation, JSONL, and vault-tool bridge."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.memory.agent import pi_agent, vault_tools
from backend.services.memory.agent.pi_agent import (
    PiExecutorError,
    PiMemoryAgent,
    _parse_events,
    _PiRuntimeConfig,
    search_vault_with_pi,
)
from backend.services.memory.conversation_note import (
    write_source_fallback_conversation_note,
)


@contextlib.contextmanager
def _no_lock(_user_id):
    yield


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(vault_tools, "vault_note_lock", _no_lock)


@pytest.fixture(autouse=True)
def no_inference_artifact_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_OPERATING_MEMORY_DIR", str(tmp_path / "operating-memory"))
    monkeypatch.setattr(
        pi_agent,
        "persist_inference_run",
        lambda **_kwargs: ("request-hash", "artifact-hash"),
    )


def _runtime_config(
    *,
    api_key="super-secret-key",
    base_url="http://kraken:8083/v1",
    temperature=0.2,
    sampling=None,
    response_format=None,
    seed=None,
    timeout_seconds=30,
):
    return _PiRuntimeConfig(
        reasoning_allowed=True,
        binary=os.environ.get("PI_BINARY", "pi"),
        provider="chronicle-llamacpp",
        model="qwen3.6-27b",
        base_url=base_url,
        api_key=api_key,
        thinking="low",
        max_tokens=4096,
        context_window=8192,
        timeout_seconds=timeout_seconds,
        reasoning=True,
        temperature=temperature,
        sampling=sampling or {},
        response_format=response_format,
        seed=seed,
    )


def _jsonl(*events):
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def _gateway_credentials(extension_source):
    url_match = re.search(r"^const gatewayUrl = (.+);$", extension_source, re.MULTILINE)
    token_match = re.search(
        r"^const bearerToken = (.+);$", extension_source, re.MULTILINE
    )
    assert url_match and token_match
    return json.loads(url_match.group(1)), json.loads(token_match.group(1))


def _call_gateway(extension_source, name, arguments):
    url, token = _gateway_credentials(extension_source)
    request = urllib.request.Request(
        url,
        data=json.dumps({"name": name, "arguments": arguments}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _report_gateway_limit(extension_source, reason):
    url, token = _gateway_credentials(extension_source)
    request = urllib.request.Request(
        url.removesuffix("/tool") + "/limit",
        data=json.dumps({"reason": reason}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_pi_subprocess_env_forces_loopback_proxy_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "models.internal,.tailnet")
    monkeypatch.setenv("no_proxy", "existing.internal")

    env = pi_agent._pi_subprocess_env(tmp_path)

    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert env["NO_PROXY"] == env["no_proxy"]
    assert env["NO_PROXY"].split(",") == [
        "models.internal",
        ".tailnet",
        "existing.internal",
        "127.0.0.1",
        "localhost",
        "::1",
    ]


def _fake_spawn(captured, *, events, tool_call=None, returncode=0, stderr=b""):
    async def spawn(*command, **kwargs):
        command = list(command)
        extension_path = Path(command[command.index("-e") + 1])
        agent_dir = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        captured.update(
            command=command,
            kwargs=kwargs,
            extension=extension_path.read_text(encoding="utf-8"),
            extension_mode=extension_path.stat().st_mode & 0o777,
            models=json.loads((agent_dir / "models.json").read_text(encoding="utf-8")),
            models_mode=(agent_dir / "models.json").stat().st_mode & 0o777,
            system_prompt=(agent_dir / "system-prompt.md").read_text(encoding="utf-8"),
            system_prompt_mode=(agent_dir / "system-prompt.md").stat().st_mode & 0o777,
        )

        class Process:
            def __init__(self):
                self.returncode = returncode
                self.killed = False
                self.waited = False

            async def communicate(self, input_bytes=None):
                captured["stdin"] = input_bytes.decode() if input_bytes else ""
                if tool_call is not None:
                    name, arguments = tool_call
                    captured["gateway_result"] = await asyncio.to_thread(
                        _call_gateway, captured["extension"], name, arguments
                    )
                return _jsonl(*events), stderr

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                return self.returncode

        return Process()

    return spawn


def _successful_events(*, summary, tool_name, usage):
    return [
        {"type": "session", "version": 3, "id": "test"},
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": tool_name,
                        "arguments": {},
                    }
                ],
                "usage": usage,
                "stopReason": "toolUse",
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": tool_name,
            "args": {},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": tool_name,
            "result": {"content": [{"type": "text", "text": "ok"}]},
            "isError": False,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": summary}],
                "usage": {"input": 5, "output": 2, "totalTokens": 7},
                "stopReason": "stop",
            },
        },
        {"type": "agent_end", "messages": []},
    ]


@pytest.mark.asyncio
async def test_pi_writer_attaches_selected_images_to_initial_message(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "record carefully"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=[
                {"type": "session", "version": 3, "id": "test"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "stopReason": "stop",
                    },
                },
                {"type": "agent_end", "messages": []},
            ],
        ),
    )

    await PiMemoryAgent(root).run(
        "I am showing the notebook now.",
        "conversation-1",
        images=[("rainbow.png", b"selected-frame")],
    )

    attachments = [arg for arg in captured["command"] if arg.startswith("@")]
    assert any(arg.endswith(".png") for arg in attachments)
    assert captured["stdin"] == ""


def test_pi_executor_availability_only_checks_binary(monkeypatch):
    monkeypatch.setenv("PI_BINARY", "custom-pi")
    monkeypatch.setattr(
        pi_agent.shutil,
        "which",
        lambda value: "/bin/pi" if value == "custom-pi" else None,
    )

    assert pi_agent.pi_executor_available() == (True, "/bin/pi")

    monkeypatch.setattr(pi_agent.shutil, "which", lambda _value: None)
    assert pi_agent.pi_executor_available() == (
        False,
        "Pi binary 'custom-pi' not found on PATH",
    )


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        ({"backends": []}, "memory.backends must be a mapping"),
        ({"backends": {"pi": []}}, "memory.backends.pi must be a mapping"),
    ],
)
def test_pi_settings_reject_falsy_non_mapping_sections(memory, message):
    registry = SimpleNamespace(memory=memory)

    with pytest.raises(PiExecutorError, match=message):
        pi_agent._pi_settings(registry)


def test_config_uses_registry_operation_budget_without_backend_model_override(
    monkeypatch,
):
    model_def = SimpleNamespace(
        name="qwen-registry-entry",
        model_provider="Llama.cpp Local",
        api_family="openai",
        thinking=True,
        reasoning_allowed=True,
        model_params={
            "context_window": 16384,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
        },
        capabilities=[],
        system_prompt_prefix="You are Qwen.",
    )
    resolved = SimpleNamespace(
        reasoning_allowed=True,
        model_def=model_def,
        model_name="upstream/qwen:Q4_K_M",
        base_url="http://kraken:8083/v1",
        api_key=None,
        max_tokens=9000,
        reasoning_effort="medium",
        effective_reasoning_effort="medium",
        temperature=0.37,
        response_format=None,
    )
    calls = []
    registry = SimpleNamespace(
        memory={
            "backends": {
                "pi": {
                    "thinking": "low",
                    "timeout_seconds": 77,
                    "context_window": 16384,
                    "max_tokens": 4096,
                }
            },
            # A similarly named legacy path must not be read.
            "pi": {"model": "wrong-model"},
        },
        get_llm_operation=lambda operation, model_override=None: (
            calls.append((operation, model_override)) or resolved
        ),
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        pi_agent, "pi_executor_available", lambda: (True, "/usr/local/bin/pi")
    )

    config = pi_agent._resolve_pi_config("memory_write")

    assert calls == [("memory_write", None)]
    assert config.provider == "chronicle-llama-cpp-local"
    assert config.model == "upstream/qwen:Q4_K_M"
    assert config.base_url == "http://kraken:8083/v1"
    assert config.api_key == "no-key"
    assert config.thinking == "low"
    assert config.max_tokens == 9000
    assert config.context_window == 16384
    assert config.timeout_seconds == 77
    assert config.temperature == 0.37
    assert config.sampling == {"top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}
    assert config.input_modalities == ["text"]
    assert config.system_prompt_prefix == "You are Qwen."
    assert config.compat == {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "qwen-chat-template",
    }


def test_pi_settings_rejects_obsolete_model_pin():
    registry = SimpleNamespace(
        memory={"backends": {"pi": {"model": "duplicated-routing-policy"}}}
    )

    with pytest.raises(PiExecutorError, match=r"memory\.backends\.pi\.model"):
        pi_agent._pi_settings(registry)


def test_config_accepts_max_thinking_and_explicit_compat_overrides(monkeypatch):
    model_def = SimpleNamespace(
        name="remote-model",
        model_provider="custom-openai",
        api_family="openai",
        thinking=True,
        reasoning_allowed=True,
        capabilities=["vision"],
        system_prompt_prefix="",
        model_params={
            "pi_compat": {
                "thinkingFormat": "deepseek",
                "supportsDeveloperRole": True,
            }
        },
    )
    resolved = SimpleNamespace(
        reasoning_allowed=True,
        model_def=model_def,
        model_name="deepseek-r1",
        base_url="https://models.example/v1",
        api_key="secret",
        max_tokens=2048,
        reasoning_effort="high",
        effective_reasoning_effort="high",
        temperature=0.41,
        response_format=None,
    )
    registry = SimpleNamespace(
        memory={
            "backends": {
                "pi": {
                    "thinking": "max",
                    "compat": {"maxTokensField": "max_completion_tokens"},
                }
            }
        },
        get_llm_operation=lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(pi_agent, "pi_executor_available", lambda: (True, "/bin/pi"))

    config = pi_agent._resolve_pi_config("memory_write")

    assert config.thinking == "max"
    assert config.input_modalities == ["text", "image"]
    assert config.compat == {
        "supportsDeveloperRole": True,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_completion_tokens",
        "thinkingFormat": "deepseek",
    }


def test_public_config_validator_resolves_requested_operation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: calls.append(
            (operation, force_fallback)
        )
        or _runtime_config(),
    )

    assert (
        pi_agent.validate_pi_executor_config("memory_search", force_fallback=True)
        is None
    )
    assert calls == [("memory_search", True)]


def test_models_payload_escapes_pi_config_value_syntax():
    config = _runtime_config(api_key="!printf compromised-$HOME-$$")

    payload = pi_agent._models_payload(config)

    assert payload["providers"]["chronicle-llamacpp"]["apiKey"] == (
        "$!printf compromised-$$HOME-$$$$"
    )


def test_config_rejects_output_budget_without_prompt_headroom(monkeypatch):
    model_def = SimpleNamespace(
        name="qwen",
        model_provider="llamacpp",
        api_family="openai",
        thinking=True,
        reasoning_allowed=True,
        model_params={},
        capabilities=[],
        system_prompt_prefix="",
    )
    resolved = SimpleNamespace(
        reasoning_allowed=True,
        model_def=model_def,
        model_name="qwen",
        base_url="http://localhost/v1",
        api_key="none-needed",
        max_tokens=7500,
        reasoning_effort="low",
        effective_reasoning_effort="low",
        temperature=0.2,
    )
    registry = SimpleNamespace(
        memory={"backends": {"pi": {"context_window": 8192, "max_tokens": 7500}}},
        get_llm_operation=lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(pi_agent, "pi_executor_available", lambda: (True, "/bin/pi"))

    with pytest.raises(PiExecutorError, match="leave at least 1024 tokens"):
        pi_agent._resolve_pi_config("memory_write")


@pytest.mark.parametrize("value", [True, 1.5, "1.5", 0, -1])
def test_config_rejects_non_positive_or_non_integral_limits(value):
    with pytest.raises(PiExecutorError, match="positive integer"):
        pi_agent._positive_int(value, name="timeout_seconds", default=900)


@pytest.mark.parametrize("value", [True, 1.5, "1.5", 0, -1])
def test_run_rejects_non_positive_or_non_integral_limits(value):
    with pytest.raises(PiExecutorError, match="positive integer"):
        pi_agent._positive_run_limit(value, name="Pi max_tool_rounds")


def test_gateway_derives_mutating_tools_from_canonical_schema_difference():
    write_tools = {
        schema["function"]["name"] for schema in vault_tools.VAULT_TOOL_SCHEMAS
    }
    read_only_tools = {
        schema["function"]["name"] for schema in vault_tools.VAULT_SEARCH_TOOL_SCHEMAS
    }

    assert write_tools - read_only_tools
    # verify_vault is write-set-only because a retrieval run has nothing to verify, not
    # because it mutates — the gateway must not serialise or audit it as a write.
    assert pi_agent._MUTATING_VAULT_TOOLS == (
        write_tools - read_only_tools - {"verify_vault"}
    )
    assert "verify_vault" not in pi_agent._MUTATING_VAULT_TOOLS


def test_memory_agent_accepts_an_isolated_tool_round_cap(tmp_path):
    agent = pi_agent.PiMemoryAgent(
        tmp_path / "vault",
        max_tool_rounds=16,
        max_identical_tool_calls=3,
    )

    assert agent.max_tool_rounds == 16
    assert agent.max_tool_calls == 64
    assert agent.max_identical_tool_calls == 3

    with pytest.raises(PiExecutorError, match="max_tool_rounds"):
        pi_agent.PiMemoryAgent(tmp_path / "invalid", max_tool_rounds=0)


def test_gateway_breaks_a_consecutive_identical_read_loop(tmp_path, unlocked):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=20,
        max_identical_tool_calls=2,
    )

    assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
    assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
    assert gateway.dispatch(
        "edit_note",
        {
            "path": "People/Alice.md",
            "edits": [{"old_text": "tea", "new_text": "coffee"}],
        },
    )
    assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
    assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
    with pytest.raises(pi_agent._PiLimitExceeded, match="identical tool call"):
        gateway.dispatch("read_note", {"path": "People/Alice.md"})
    assert gateway.limit_error and "consecutively" in gateway.limit_error


def test_gateway_does_not_count_identical_calls_across_other_inspection(
    tmp_path, unlocked
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=20,
        max_identical_tool_calls=2,
    )

    for _ in range(4):
        assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
        assert gateway.dispatch("glob", {"pattern": "Topics/*.md"})

    assert gateway.limit_error is None


def test_gateway_breaks_repeated_terminal_search_evidence_across_alternating_calls(
    tmp_path, unlocked
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea and coffee.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=20,
    )

    for pattern in ("tea", "tea", "coffee", "coffee"):
        assert gateway.dispatch("grep", {"pattern": pattern})
    with pytest.raises(pi_agent._PiLimitExceeded, match="terminal search evidence"):
        gateway.dispatch("grep", {"pattern": "tea"})

    assert gateway.limit_error and "grep:" in gateway.limit_error


def test_gateway_allows_terminal_search_evidence_after_a_successful_mutation(
    tmp_path, unlocked
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=20,
    )

    assert gateway.dispatch("grep", {"pattern": "tea"})
    assert gateway.dispatch("grep", {"pattern": "tea"}).startswith(
        "Search result unchanged"
    )
    assert gateway.dispatch(
        "edit_note",
        {
            "path": "People/Alice.md",
            "edits": [{"old_text": "tea", "new_text": "coffee"}],
        },
    )
    assert gateway.dispatch("grep", {"pattern": "coffee"})
    assert gateway.dispatch("grep", {"pattern": "coffee"}).startswith(
        "Search result unchanged"
    )

    assert gateway.limit_error is None


def test_write_gateway_breaks_inspection_churn_without_a_mutation(tmp_path, unlocked):
    root = tmp_path / "user"
    root.mkdir(parents=True)
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=100,
    )

    for index in range(pi_agent.MAX_INSPECTION_CALLS_WITHOUT_MUTATION):
        assert gateway.dispatch("glob", {"pattern": f"Topics/{index}-*.md"})
    with pytest.raises(pi_agent._PiLimitExceeded, match="inspection calls without"):
        gateway.dispatch("glob", {"pattern": "Topics/over-limit-*.md"})


def test_successful_mutation_resets_write_gateway_inspection_progress(
    tmp_path, unlocked
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=100,
    )

    for index in range(pi_agent.MAX_INSPECTION_CALLS_WITHOUT_MUTATION):
        assert gateway.dispatch("glob", {"pattern": f"Topics/{index}-*.md"})
    assert gateway.dispatch(
        "edit_note",
        {
            "path": "People/Alice.md",
            "edits": [{"old_text": "tea", "new_text": "coffee"}],
        },
    )
    for index in range(pi_agent.MAX_INSPECTION_CALLS_WITHOUT_MUTATION):
        assert gateway.dispatch("glob", {"pattern": f"Categories/{index}-*.md"})

    assert gateway.limit_error is None


def test_gateway_can_terminate_only_after_verified_required_note_exists(
    tmp_path, unlocked
):
    root = tmp_path / "user"
    required = "Conversations/case-1.md"
    (root / "Conversations").mkdir(parents=True)
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        required_notes=[required],
        terminate_on_verified=True,
    )
    gateway.tools.baseline()
    write_source_fallback_conversation_note(
        root / required,
        transcript="Speaker: A grounded test statement.",
        conversation_id="case-1",
        date="2026-08-15T00:00:00+00:00",
        duration_minutes=None,
        title=None,
    )
    gateway.tools.touched.add(required)
    gateway.__enter__()
    try:
        extension = pi_agent._extension_source(
            vault_tools.VAULT_TOOL_SCHEMAS,
            gateway_url=gateway.url,
            token=gateway.token,
        )
        response = _call_gateway(extension, "verify_vault", {})
    finally:
        gateway._close()

    assert response == {
        "result": "Vault verification passed: no problems found.",
        "terminate": True,
    }
    assert "terminate: payload.terminate === true" in extension


def test_gateway_does_not_terminate_before_successful_required_write(tmp_path):
    root = tmp_path / "user"
    required = "Conversations/case-1.md"
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        required_notes=[required],
        terminate_on_verified=True,
    )

    assert (
        gateway.should_terminate(
            "verify_vault", "Vault verification passed: no problems found."
        )
        is False
    )
    gateway.tools.touched.add("Topics/Incomplete.md")
    assert (
        gateway.should_terminate(
            "verify_vault", "Vault verification passed: no problems found."
        )
        is False
    )
    (root / required).parent.mkdir(parents=True)
    (root / required).write_text("incomplete", encoding="utf-8")
    assert gateway.should_terminate("verify_vault", "1 problem found.") is False
    assert gateway.terminal_completion is False


def test_terminal_tool_allows_agent_end_without_extra_text_turn():
    events = _parse_events(
        _jsonl(
            {"type": "agent_start"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "verify_vault"}],
                    "stopReason": "toolUse",
                },
            },
            {"type": "agent_end", "messages": []},
        ).decode(),
        allow_terminal_tool=True,
    )

    assert events.agent_ended is True
    assert events.rounds == 1
    assert events.fatal_errors == []
    assert events.summary == ""


def test_later_successful_turn_recovers_an_earlier_length_limited_tool_call():
    events = _parse_events(
        _jsonl(
            {"type": "agent_start"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "truncated"}],
                    "stopReason": "length",
                },
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Recovered and verified."}],
                    "stopReason": "stop",
                },
            },
            {"type": "agent_end", "messages": []},
        ).decode()
    )

    assert events.truncated is False
    assert events.stop_reason == "stop"
    assert events.assistant_content == [
        {"type": "text", "text": "Recovered and verified."}
    ]
    assert events.summary == "Recovered and verified."


@pytest.mark.asyncio
async def test_gateway_shutdown_does_not_block_event_loop(tmp_path, monkeypatch):
    gateway = pi_agent._VaultToolGateway(
        tmp_path / "user",
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    order = []
    original_close = gateway._close

    def slow_close():
        time.sleep(0.08)
        order.append("close")
        original_close()

    monkeypatch.setattr(gateway, "_close", slow_close)

    async def heartbeat():
        await asyncio.sleep(0.01)
        order.append("heartbeat")

    await asyncio.gather(gateway.aclose(), heartbeat())

    assert order == ["heartbeat", "close"]


@pytest.mark.asyncio
async def test_gateway_shutdown_waits_off_loop_for_active_mutation(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "write_note",
            {
                "path": "Conversations/late.md",
                "content": "must never land after shutdown",
            },
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)

    # Shutdown waits for the mutation gate in a worker thread, not on the event loop.
    assert not close_task.done()
    assert not (root / "Conversations" / "late.md").exists()
    release.set()
    await close_task

    response = await request_task
    assert response["result"].startswith("Wrote Conversations/late.md")
    assert (root / "Conversations" / "late.md").read_text() == (
        "must never land after shutdown"
    )
    assert gateway.tools.touched == {"Conversations/late.md"}

    rejected = gateway.dispatch(
        "write_note",
        {"path": "Conversations/too-late.md", "content": "must not land"},
    )
    assert rejected == "Error: Pi vault gateway is closing"
    assert not (root / "Conversations" / "too-late.md").exists()


@pytest.mark.asyncio
async def test_gateway_shutdown_captures_active_mutation_error_before_returning(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()

    def failing_dispatch(_name, _arguments):
        started.set()
        release.wait(timeout=2)
        raise vault_tools.VaultToolError("active mutation failed")

    monkeypatch.setattr(gateway.tools, "dispatch", failing_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "write_note",
            {"path": "Conversations/failed.md", "content": "must not land"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()

    release.set()
    await close_task

    # The error is captured while the mutation gate is still held, so callers may
    # safely assemble the result as soon as close returns.
    assert gateway.errors == ["write_note: active mutation failed"]
    response = await request_task
    assert response == {"result": "Error: active mutation failed"}
    assert not (root / "Conversations" / "failed.md").exists()


@pytest.mark.asyncio
async def test_gateway_shutdown_records_admitted_read_before_audit_freeze(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "read_note",
            {"path": "People/Alice.md"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()
    release.set()
    await close_task

    assert gateway.read_notes == {"People/Alice.md": "Alice prefers tea."}
    assert await request_task == {"result": "Alice prefers tea."}


@pytest.mark.asyncio
async def test_gateway_drain_timeout_freezes_late_read_audit(tmp_path, monkeypatch):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    monkeypatch.setattr(pi_agent, "_GATEWAY_DRAIN_TIMEOUT_SECONDS", 0.01)
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "read_note",
            {"path": "People/Alice.md"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    await gateway.aclose()

    assert gateway.read_notes == {}
    assert gateway.close_error == (
        "Pi vault gateway requests did not drain within 0.01 seconds; "
        "vault mutations are complete, but read/error details may be omitted"
    )
    errors_at_return = list(gateway.errors)

    release.set()
    assert await request_task == {"result": "Alice prefers tea."}
    assert gateway.read_notes == {}
    assert gateway.errors == errors_at_return


@pytest.mark.asyncio
async def test_gateway_shutdown_preserves_admitted_limit_signal(tmp_path, monkeypatch):
    gateway = pi_agent._VaultToolGateway(
        tmp_path / "user",
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    admitted_values = []
    original_set_limit = gateway.set_limit

    def delayed_set_limit(reason, *, admitted=False):
        admitted_values.append(admitted)
        started.set()
        release.wait(timeout=2)
        original_set_limit(reason, admitted=admitted)

    monkeypatch.setattr(gateway, "set_limit", delayed_set_limit)
    request_task = asyncio.create_task(
        asyncio.to_thread(_report_gateway_limit, extension, "hard limit reached")
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()
    release.set()
    await close_task

    assert await request_task == 204
    assert admitted_values == [True]
    assert gateway.limit_error == "hard limit reached"


def test_gateway_tool_call_cap_is_atomic_under_concurrency(tmp_path):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=2,
    )

    def read_note():
        try:
            return gateway.dispatch("read_note", {"path": "People/Alice.md"})
        except pi_agent._PiLimitExceeded:
            return "limited"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: read_note(), range(8)))

    assert results.count("Alice prefers tea.") == 1
    assert sum("unchanged read_note window" in result for result in results) == 1
    assert results.count("limited") == 6
    assert gateway.call_count == 2
    assert gateway.limit_error and "tool-call limit exceeded (2)" in gateway.limit_error


def test_gateway_elides_an_unchanged_repeated_read_window(tmp_path):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=4,
    )

    first = gateway.dispatch("read_note", {"path": "People/Alice.md"})
    repeated = gateway.dispatch("read_note", {"path": "People/Alice.md"})
    refreshed = gateway.dispatch(
        "read_note", {"path": "People/Alice.md", "refresh": True}
    )

    assert first == "Alice prefers tea."
    assert "unchanged read_note window" in repeated
    assert len(repeated) < len(first) + 200
    assert refreshed == first
    assert gateway.read_notes == {"People/Alice.md": first}


def test_gateway_returns_a_repeated_read_after_the_note_changes(tmp_path):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=3,
    )

    assert gateway.dispatch("read_note", {"path": "People/Alice.md"})
    note.write_text("Alice prefers coffee.", encoding="utf-8")

    changed = gateway.dispatch("read_note", {"path": "People/Alice.md"})

    assert changed == "Alice prefers coffee."
    assert gateway.read_notes == {"People/Alice.md": changed}


@pytest.mark.asyncio
async def test_search_rejects_nonpositive_hard_round_limit_before_spawn(tmp_path):
    with pytest.raises(PiExecutorError, match="search max_rounds"):
        await search_vault_with_pi("question", tmp_path / "user", max_rounds=0)


@pytest.mark.asyncio
async def test_write_uses_isolated_canonical_gateway_and_reports_audit_state(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    inference_artifact = {}
    content = "A canonical conversation note."
    events = _successful_events(
        summary="Recorded the conversation.",
        tool_name="write_note",
        usage={
            "input": 100,
            "output": 20,
            "cacheRead": 10,
            "cacheWrite": 3,
            "reasoning": 4,
            "totalTokens": 120,
        },
    )
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle write system prompt"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent,
        "persist_inference_run",
        lambda **kwargs: inference_artifact.update(kwargs)
        or ("request-hash", "artifact-hash"),
    )
    monkeypatch.setenv("CHRONICLE_SECRET_SHOULD_NOT_LEAK", "backend-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=events,
            tool_call=(
                "write_note",
                {"path": "Conversations/conv-1.md", "content": content},
            ),
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-1")

    assert (root / "Conversations" / "conv-1.md").read_text() == content
    assert result.touched == ["Conversations/conv-1.md"]
    assert result.removed == []
    assert result.summary == "Recorded the conversation."
    assert result.rounds == 2
    assert result.tool_calls == 1
    assert result.usage == {
        "input_tokens": 105,
        "output_tokens": 22,
        "input_cached_tokens": 10,
        "input_cache_write_tokens": 3,
        "output_reasoning_tokens": 4,
        "total_tokens": 127,
    }
    assert result.errors == []
    assert not result.truncated
    assert inference_artifact["operation"] == "pi_memory"
    assert inference_artifact["request"]["conversation_id"] == "conv-1"
    assert inference_artifact["request"]["user_id"] == "user"
    assert inference_artifact["request"]["record"] == "conversation"
    assert inference_artifact["request"]["model"] == "qwen3.6-27b"
    assert (
        inference_artifact["request"]["system_prompt"]
        == "Chronicle write system prompt"
    )
    assert "Speaker: hello" in inference_artifact["request"]["prompt"]
    assert inference_artifact["stdout"] == _jsonl(*events).decode()
    assert inference_artifact["stderr"] == ""
    assert inference_artifact["result"]["usage"] == result.usage
    assert inference_artifact["result"]["touched"] == ["Conversations/conv-1.md"]
    assert inference_artifact["metadata"] == {
        "returncode": 0,
        "terminated_by_chronicle": False,
    }
    assert not inference_artifact["reusable"]

    command = captured["command"]
    for flag in (
        "--mode",
        "--no-session",
        "--offline",
        "--no-context-files",
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
        "--no-builtin-tools",
        "--tools",
    ):
        assert flag in command
    # A tool-using run is taught the vault's shape by a generated skill rather than by
    # more system prompt, so skills are loaded, not disabled.
    assert "--no-skills" not in command
    assert "--skill" in command
    assert "--api-key" not in command
    assert "super-secret-key" not in command
    assert "Chronicle write system prompt" not in command
    assert command[command.index("--tools") + 1] == ",".join(
        schema["function"]["name"] for schema in vault_tools.VAULT_TOOL_SCHEMAS
    )
    assert "import " not in captured["extension"]
    assert "pi.registerTool" in captured["extension"]
    assert captured["extension_mode"] == 0o600
    assert captured["models_mode"] == 0o600
    assert captured["system_prompt_mode"] == 0o600
    assert captured["system_prompt"] == "Chronicle write system prompt"
    assert captured["models"]["providers"]["chronicle-llamacpp"]["apiKey"] == (
        "super-secret-key"
    )
    assert captured["kwargs"]["env"]["PI_OFFLINE"] == "1"
    assert captured["kwargs"]["env"]["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert "CHRONICLE_SECRET_SHOULD_NOT_LEAK" not in captured["kwargs"]["env"]
    assert "Speaker: hello" in captured["stdin"]


@pytest.mark.asyncio
@pytest.mark.parametrize("notes_only", [False, True])
async def test_search_returns_only_notes_read_through_canonical_tools(
    tmp_path, monkeypatch, notes_only
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    captured = {}
    inference_artifact = {}
    events = _successful_events(
        summary="Alice prefers tea (People/Alice.md).",
        tool_name="read_note",
        usage={"input": 30, "output": 1, "totalTokens": 31},
    )
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    pi_agent.OperatingMemoryStore("user").replace_agents(
        "# Retrieval\nStop when the selected evidence directly answers the query.",
        rationale="Test active retrieval guidance.",
        evidence_ids=["trace-test"],
    )
    monkeypatch.setattr(
        pi_agent,
        "persist_inference_run",
        lambda **kwargs: inference_artifact.update(kwargs)
        or ("request-hash", "artifact-hash"),
    )
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=events,
            tool_call=("read_note", {"path": "People/Alice.md"}),
        ),
    )

    result = await search_vault_with_pi(
        "What does Alice prefer?", root, notes_only=notes_only
    )

    assert result.answer == "Alice prefers tea (People/Alice.md)."
    assert result.notes == [
        {"path": "People/Alice.md", "content": "Alice prefers tea."}
    ]
    assert result.rounds == 2
    assert result.usage["input_tokens"] == 35
    assert result.errors == []
    assert captured["command"][captured["command"].index("--tools") + 1] == (
        "grep,glob,read_note,read_slice"
        if notes_only
        else "grep,glob,read_note,read_slice,search_images"
    )
    assert "Stop when the selected evidence" in captured["system_prompt"]
    assert inference_artifact["operation"] == "pi_memory_search"
    assert inference_artifact["request"]["record"] == "search"
    assert inference_artifact["request"]["user_id"] == "user"
    assert inference_artifact["result"]["read_paths"] == ["People/Alice.md"]
    assert "Alice prefers tea." not in json.dumps(inference_artifact["result"])


@pytest.mark.asyncio
async def test_search_limit_uses_pi_again_without_tools_for_final_synthesis(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    config = _runtime_config()
    calls = []

    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: config,
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    async def invoke(_root, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                pi_agent._PiEventResult(
                    rounds=7,
                    usage={"input_tokens": 20},
                    errors=[
                        "Pi tool-round limit exceeded (6)",
                        "Pi JSONL stream ended without agent_end",
                        "Pi completed without a final assistant message",
                    ],
                    fatal_errors=[
                        "Pi tool-round limit exceeded (6)",
                        "Pi JSONL stream ended without agent_end",
                        "Pi completed without a final assistant message",
                    ],
                    truncated=True,
                ),
                SimpleNamespace(
                    limit_error="Pi tool-round limit exceeded (6)",
                    call_count=0,
                    read_notes={
                        "Topics/Synthetic.md": "A synthetic fact for retrieval."
                    },
                ),
            )
        return (
            pi_agent._PiEventResult(
                summary="The synthetic fact is recorded.",
                rounds=1,
                usage={"input_tokens": 10, "output_tokens": 4},
                agent_ended=True,
            ),
            SimpleNamespace(limit_error=None, call_count=0, read_notes={}),
        )

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(pi_agent, "_invoke_pi", invoke)

    result = await search_vault_with_pi("What was recorded?", root)

    assert result.answer == "The synthetic fact is recorded."
    assert result.notes == [
        {
            "path": "Topics/Synthetic.md",
            "content": "A synthetic fact for retrieval.",
        }
    ]
    assert result.rounds == 8
    assert result.usage == {"input_tokens": 30, "output_tokens": 4}
    assert result.errors == []
    assert result.warnings == [
        "Pi tool-round limit exceeded (6)",
        "Pi JSONL stream ended without agent_end",
        "Pi completed without a final assistant message",
    ]
    assert calls[0]["schemas"] is pi_agent.VAULT_SEARCH_TOOL_SCHEMAS
    assert calls[1]["schemas"] == ()
    assert "Note evidence JSON" in calls[1]["prompt"]
    assert "no tools are available" in calls[1]["system_prompt"].lower()


@pytest.mark.asyncio
async def test_search_preserves_audited_failure_when_final_synthesis_raises(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    config = _runtime_config(api_key="private-test-key")
    calls = 0

    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: config,
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    async def invoke(_root, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                pi_agent._PiEventResult(
                    rounds=7,
                    errors=["Pi tool-round limit exceeded (6)"],
                    fatal_errors=["Pi tool-round limit exceeded (6)"],
                    truncated=True,
                ),
                SimpleNamespace(
                    limit_error="Pi tool-round limit exceeded (6)",
                    call_count=0,
                    read_notes={"Topics/Synthetic.md": "Audited evidence."},
                ),
            )
        raise pi_agent.PiExecutorError("failed with private-test-key")

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(pi_agent, "_invoke_pi", invoke)

    result = await search_vault_with_pi("What was recorded?", root)

    assert calls == 2
    assert result.answer == pi_agent.PI_SEARCH_FAILURE_ANSWER
    assert result.notes == [
        {"path": "Topics/Synthetic.md", "content": "Audited evidence."}
    ]
    assert result.rounds == 7
    assert result.errors == [
        "Pi tool-round limit exceeded (6)",
        "Pi final search synthesis failed: PiExecutorError: failed with [REDACTED]",
    ]


@pytest.mark.asyncio
async def test_no_tool_pi_invocation_loads_sampling_runtime_extension(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}

    def record_usage_span(*_args, **kwargs):
        captured["usage_span"] = kwargs

    async def spawn(*command, **kwargs):
        captured["command"] = list(command)
        captured["agent_dir"] = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        extension_path = Path(command[command.index("-e") + 1])
        captured["extension"] = extension_path.read_text(encoding="utf-8")
        captured["extension_mode"] = extension_path.stat().st_mode & 0o777

        class Process:
            returncode = 0

            async def communicate(self, input_bytes=None):
                captured["stdin"] = input_bytes.decode()
                return (
                    _jsonl(
                        {"type": "agent_start"},
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "final answer"}],
                                "stopReason": "stop",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        return Process()

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(pi_agent, "record_llm_usage_span", record_usage_span)

    events, gateway = await pi_agent._invoke_pi(
        root,
        prompt="final prompt",
        system_prompt="final system",
        schemas=(),
        config=_runtime_config(
            temperature=0.37,
            seed=42,
            sampling={"top_p": 0.8, "presence_penalty": 1.5},
        ),
        max_tool_rounds=1,
        max_tool_calls=1,
    )

    assert events.summary == "final answer"
    assert events.truncated is False
    assert events.stop_reason == "stop"
    assert gateway.call_count == 0
    assert "-e" in captured["command"]
    assert "--tools" not in captured["command"]
    assert "before_provider_request" in captured["extension"]
    assert "const temperature = 0.37;" in captured["extension"]
    sampling = re.search(
        r"^const sampling = (.+);$", captured["extension"], re.MULTILINE
    )
    assert sampling is not None
    assert json.loads(sampling.group(1)) == {"top_p": 0.8, "presence_penalty": 1.5}
    assert "const seed = 42;" in captured["extension"]
    assert "...(seed === null ? {} : { seed })," in captured["extension"]
    assert "pi.registerTool" in captured["extension"]
    assert captured["extension_mode"] == 0o600
    assert captured["stdin"] == "final prompt"
    assert captured["usage_span"]["input"]["system_prompt"]["chars"] == len(
        "final system"
    )
    assert captured["usage_span"]["input"]["prompt"]["chars"] == len("final prompt")
    assert captured["usage_span"]["output"]["completion"]["chars"] == len(
        "final answer"
    )
    assert captured["usage_span"]["output"]["stop_reason"] == "stop"
    assert captured["usage_span"]["attributes"]["gen_ai.response.finish_reasons"] == [
        "stop"
    ]


@pytest.mark.asyncio
async def test_length_limited_thinking_only_output_is_visible_in_usage_span(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}

    async def spawn(*_command, **_kwargs):
        class Process:
            returncode = 0

            async def communicate(self, _input_bytes=None):
                return (
                    _jsonl(
                        {"type": "agent_start"},
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "thinking", "thinking": "unfinished"}
                                ],
                                "stopReason": "length",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        return Process()

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        pi_agent,
        "record_llm_usage_span",
        lambda *_args, **kwargs: captured.update(kwargs),
    )

    events, _gateway = await pi_agent._invoke_pi(
        root,
        prompt="final prompt",
        system_prompt="final system",
        schemas=(),
        config=_runtime_config(),
        max_tool_rounds=1,
        max_tool_calls=1,
    )

    assert events.summary == ""
    assert events.stop_reason == "length"
    assert events.assistant_content == [{"type": "thinking", "thinking": "unfinished"}]
    assert captured["output"]["assistant_content"]["chars"] > len("unfinished")


@pytest.mark.asyncio
async def test_no_tool_pi_invocation_forwards_json_response_format(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}

    async def spawn(*command, **_kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        captured["extension"] = extension_path.read_text(encoding="utf-8")

        class Process:
            returncode = 0

            async def communicate(self, _input_bytes=None):
                return (
                    _jsonl(
                        {"type": "agent_start"},
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "{}"}],
                                "stopReason": "stop",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        return Process()

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    await pi_agent._invoke_pi(
        root,
        prompt="return json",
        system_prompt="return json",
        schemas=(),
        config=_runtime_config(response_format={"type": "json_object"}),
        max_tool_rounds=1,
        max_tool_calls=1,
    )

    assert 'const responseFormat = {"type":"json_object"};' in captured["extension"]
    assert (
        "...(responseFormat === null ? {} : { response_format: responseFormat }),"
        in captured["extension"]
    )


def test_parse_events_preserves_tool_and_protocol_errors():
    parsed = _parse_events(
        "\n".join(
            [
                json.dumps({"type": "agent_start"}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "read_note",
                        "result": {
                            "content": [{"type": "text", "text": "network failed"}]
                        },
                        "isError": True,
                    }
                ),
                json.dumps(
                    {
                        "type": "extension_error",
                        "extensionPath": "/tmp/chronicle-vault-tools.mjs",
                        "event": "tool_call",
                        "error": "hook crashed",
                    }
                ),
                "not-json",
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
    )

    assert parsed.errors == ["read_note: network failed"]
    assert any("invalid JSONL" in error for error in parsed.fatal_errors)
    assert any(
        "Pi extension error during tool_call: hook crashed" in error
        for error in parsed.fatal_errors
    )
    assert any("without a final assistant" in error for error in parsed.fatal_errors)


def test_parse_events_does_not_treat_unicode_line_separator_inside_json_as_jsonl_boundary():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "before\u2028after"}],
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {},
                    },
                }
            ),
            json.dumps({"type": "agent_end", "messages": []}),
        ]
    )

    parsed = _parse_events(stdout)

    assert parsed.agent_ended is True
    assert parsed.summary == "done"
    assert not any("invalid JSONL" in error for error in parsed.fatal_errors)


def test_artifact_stdout_drops_only_cumulative_message_updates():
    message_update = json.dumps(
        {
            "type": "message_update",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "partial"}],
            },
        },
        separators=(",", ":"),
    )
    message_end = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "complete"}],
            },
        },
        separators=(",", ":"),
    )
    stdout = f"{message_update}\n{message_end}\n"

    compact, metadata = pi_agent._compact_artifact_stdout(stdout)

    assert compact == f"{message_end}\n"
    assert metadata["raw_stdout_chars"] == len(stdout)
    assert metadata["stored_stdout_chars"] == len(compact)
    assert metadata["dropped_cumulative_message_updates"] == 1
    assert len(metadata["raw_stdout_sha256"]) == 64
    assert _parse_events(stdout).summary == "complete"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
def test_generated_extension_reports_tool_rejection_as_error(tmp_path):
    extension_path = tmp_path / "tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
            gateway_url="http://127.0.0.1:1/tool",
            token="test-token",
        )
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
const tools = [];
extension({{on() {{}}, registerTool(tool) {{ tools.push(tool); }} }});
let result = "Error: Repeated read limit reached";
globalThis.fetch = async () => ({{ok: true, text: async () => JSON.stringify({{result}})}});
let rejected = false;
try {{ await tools[0].execute("call", {{}}); }}
catch (error) {{ rejected = error.message === result; }}
if (!rejected) throw new Error("Tool failure was returned as success");
result = "Valid source text";
const success = await tools[0].execute("next", {{}});
if (success.content[0].text !== result) throw new Error("Success was changed");
"""
    run = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
def test_rejected_full_submission_switches_to_editing_and_survives_restart(tmp_path):
    from pydantic import BaseModel

    from backend.services.timeline.pi_tasks import InvestigationTools

    class Result(BaseModel):
        answer: str

    tools = InvestigationTools(tmp_path, {}, {}, Result)
    extension_path = tmp_path / "tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            tools.schemas,
            gateway_url="http://127.0.0.1:1/tool",
            token="test",
            result_repair_tools=tools.result_repair_tools,
        )
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
const entries = [];
let active = [];
function start() {{
  const handlers = {{}}, tools = {{}};
  extension({{
    on(name, handler) {{handlers[name] = handler;}},
    registerTool(tool) {{tools[tool.name] = tool;}},
    appendEntry(customType, data) {{entries.push({{type:'custom', customType, data}});}},
    setActiveTools(names) {{active=names;}},
  }});
  handlers.session_start({{}}, {{sessionManager:{{getBranch:()=>entries}}}});
  return tools;
}}
let result = 'Error: Claim 2 unsupported. Draft saved';
globalThis.fetch = async () => ({{ok:true,text:async()=>JSON.stringify({{result}})}});
let tools = start();
try {{await tools.finish_task.execute('submit', {{result:{{answer:'bad'}}}});}} catch (_) {{}}
if (active.includes('finish_task') || !active.includes('revise_result') || !active.includes('read_material')) throw Error('Repair tools unavailable');
active=[];tools=start();
if (active.includes('finish_task') || !active.includes('revise_result')) throw Error('Restart lost repair mode');
result='Task result accepted.';
const success=await tools.revise_result.execute('edit',{{edits:[]}});
if(success.content[0].text!==result) throw Error('Edited success lost');
"""
    run = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
def test_repeat_recovery_steers_only_after_complete_turn_and_once(tmp_path):
    extension_path = tmp_path / "tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
            gateway_url="http://127.0.0.1:1/tool",
            token="test-token",
            recover_repeated_tool_errors=True,
        )
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
let entries = [], continuations = 0;
const context = {{sessionManager: {{getBranch: () => entries}}}};
globalThis.fetch = async () => ({{ok: true, text: async () => JSON.stringify({{result: "Error: Repeated read limit"}})}});
async function run() {{
  const handlers = {{}}, tools = [];
  extension({{
    on(name, handler) {{handlers[name] = handler;}}, registerTool(tool) {{tools.push(tool);}},
    appendEntry(customType, data) {{entries.push({{type: "custom", customType, data}});}},
    sendUserMessage(_text, options) {{
      if (options.deliverAs !== "steer") throw new Error("Not a steering message");
      continuations++;
    }},
  }});
  handlers.session_start({{}}, context);
  const before = continuations;
  for (let i = 0; i < 2; i++) {{
    try {{await tools[0].execute("read", {{key: "source"}});}} catch (_) {{}}
  }}
  if (continuations !== before) throw new Error("Recovery started inside a tool execution");
  handlers.turn_end({{}}, context);
  handlers.turn_end({{}}, context);
}}
await run();
if (continuations !== 1) throw new Error("Did not steer exactly once");
await run();
if (continuations !== 1) throw new Error("Restart granted extra recovery");
"""
    run = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
def test_generated_extension_aborts_on_first_tool_beyond_round_limit(tmp_path):
    extension_path = tmp_path / "chronicle-vault-tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
            gateway_url="http://127.0.0.1:1/tool",
            token="test-token",
            temperature=0.37,
            sampling={"top_p": 0.8, "top_k": 20, "presence_penalty": 1.5},
            response_format={"type": "json_object"},
            max_tool_rounds=2,
            max_tool_calls=8,
        ),
        encoding="utf-8",
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
const handlers = {{}};
const tools = [];
const reports = [];
globalThis.fetch = async (url, options) => {{
  reports.push({{ url, body: JSON.parse(options.body) }});
  return {{ ok: true, status: 204, text: async () => "" }};
}};
extension({{
  on(name, handler) {{ handlers[name] = handler; }},
  registerTool(tool) {{ tools.push(tool); }},
}});
const originalPayload = {{ model: "qwen", messages: [] }};
const providerPayload = await handlers.before_provider_request(
  {{ payload: originalPayload }},
  {{}},
);
if (providerPayload.temperature !== 0.37) {{
  throw new Error(`expected temperature 0.37, got ${{providerPayload.temperature}}`);
}}
if (providerPayload.top_p !== 0.8 || providerPayload.top_k !== 20 || providerPayload.presence_penalty !== 1.5) {{
  throw new Error("Configured model sampling was not forwarded");
}}
if (providerPayload.response_format?.type !== "json_object") {{
  throw new Error("expected JSON response format on provider payload");
}}
if (providerPayload.model !== "qwen" || providerPayload.messages !== originalPayload.messages) {{
  throw new Error("provider hook did not preserve the original payload");
}}
if (Object.hasOwn(originalPayload, "temperature")) {{
  throw new Error("provider hook mutated the original payload");
}}
let aborted = 0;
const context = {{ abort() {{ aborted += 1; }} }};
handlers.turn_start();
await handlers.tool_call({{}}, context);
await handlers.tool_call({{}}, context);
handlers.turn_start();
await handlers.tool_call({{}}, context);
handlers.turn_start();
const blocked = await handlers.tool_call({{}}, context);
// Derived, not hardcoded: the contract is that the extension registers exactly the
// schemas it was handed, which is what stops a write tool leaking into search.
if (tools.length !== {len(vault_tools.VAULT_SEARCH_TOOL_SCHEMAS)}) {{
  throw new Error(`expected {len(vault_tools.VAULT_SEARCH_TOOL_SCHEMAS)} tools, got ${{tools.length}}`);
}}
if (tools.some((tool) => tool.executionMode !== "sequential")) {{
  throw new Error("all Chronicle tools must execute sequentially");
}}
if (aborted !== 1) throw new Error(`expected one abort, got ${{aborted}}`);
if (!blocked?.block) throw new Error("limit call was not blocked");
if (blocked.reason !== "Pi tool-round limit exceeded (2)") throw new Error(blocked.reason);
if (reports.length !== 1 || reports[0].body.reason !== blocked.reason) {{
  throw new Error("limit was not reported to Chronicle");
}}
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_parse_events_accepts_a_provider_error_that_pi_retried_successfully():
    parsed = _parse_events(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "temporarily unavailable",
                    },
                },
                {"type": "agent_end", "messages": []},
                {"type": "auto_retry_start", "attempt": 1},
                {"type": "auto_retry_end", "success": True, "attempt": 1},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "recovered"}],
                        "stopReason": "stop",
                    },
                },
                {"type": "agent_end", "messages": []},
            ]
        )
    )

    assert parsed.summary == "recovered"
    assert parsed.fatal_errors == []
    assert parsed.errors == [
        "recovered after Pi assistant error: temporarily unavailable"
    ]


@pytest.mark.asyncio
async def test_fatal_pi_event_returns_search_error_and_preserves_read_notes(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "search"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=[
                {"type": "agent_start"},
                {"type": "error", "message": "provider disconnected: super-secret-key"},
                {"type": "agent_end", "messages": []},
            ],
            tool_call=("read_note", {"path": "People/Alice.md"}),
        ),
    )

    result = await search_vault_with_pi("question", root)

    assert result.answer == "(Pi search failed before completing)"
    assert result.notes == [
        {"path": "People/Alice.md", "content": "Alice prefers tea."}
    ]
    assert any("provider disconnected: [REDACTED]" in error for error in result.errors)
    assert all("super-secret-key" not in error for error in result.errors)


@pytest.mark.asyncio
async def test_write_returns_auditable_truncated_result_after_nonzero_exit(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    content = "Written before Pi failed."
    proxy_url = "http://proxy-user:proxy-pass@proxy.internal:3128"
    model_url = "https://model-user:model-pass@models.internal/v1"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(base_url=model_url),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="The write landed before shutdown.",
                tool_name="write_note",
                usage={"input": 1, "output": 1, "totalTokens": 2},
            ),
            tool_call=(
                "write_note",
                {"path": "Conversations/conv-failed.md", "content": content},
            ),
            returncode=17,
            stderr=(
                "upstream rejected super-secret-key via "
                f"{proxy_url} proxy-pass and model-pass"
            ).encode(),
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-failed")

    assert (root / "Conversations" / "conv-failed.md").read_text() == content
    assert result.touched == ["Conversations/conv-failed.md"]
    assert result.summary == "The write landed before shutdown."
    assert result.truncated
    assert any("status 17" in error for error in result.errors)
    assert any("[REDACTED]" in error for error in result.errors)
    assert all("super-secret-key" not in error for error in result.errors)
    assert all("proxy-pass" not in error for error in result.errors)
    assert all("model-pass" not in error for error in result.errors)


@pytest.mark.asyncio
async def test_nonzero_exit_after_rename_preserves_removed_audit_state(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    old_note = root / "People" / "Alice.md"
    old_note.parent.mkdir(parents=True)
    old_content = "# Alice\n\n## About\n- Prefers tea.\n"
    old_note.write_text(old_content, encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="Renamed before failure.",
                tool_name="rename_person",
                usage={},
            ),
            tool_call=("rename_person", {"old_name": "Alice", "new_name": "Alicia"}),
            returncode=23,
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-rename")

    assert not old_note.exists()
    assert (root / "People" / "Alicia.md").read_text(encoding="utf-8") == old_content
    assert result.touched == ["People/Alicia.md"]
    assert result.removed == [
        {
            "old_path": "People/Alice.md",
            "new_path": "People/Alicia.md",
            "before": old_content,
        }
    ]
    assert result.truncated
    assert any("status 23" in error for error in result.errors)


@pytest.mark.asyncio
async def test_timeout_preserves_prior_write_and_waits_for_killed_process(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.01),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()
                self.called = False

            async def communicate(self, input_bytes=None):
                if not self.called:
                    self.called = True
                    await asyncio.to_thread(
                        _call_gateway,
                        extension,
                        "write_note",
                        {
                            "path": "Conversations/conv-timeout.md",
                            "content": "Durable before timeout.",
                        },
                    )
                await self.released.wait()
                return _jsonl({"type": "agent_start"}), b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-timeout")

    assert (root / "Conversations" / "conv-timeout.md").read_text() == (
        "Durable before timeout."
    )
    assert result.touched == ["Conversations/conv-timeout.md"]
    assert result.truncated
    assert any("timed out" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_cancellation_kills_and_waits_for_pi_subprocess(tmp_path, monkeypatch):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    started = asyncio.Event()
    note = root / "People" / "Alice.md"
    note.parent.mkdir()
    note.write_text("Alice prefers tea.")
    original_gateway_init = pi_agent._VaultToolGateway.__init__

    def gateway_init(gateway, *args, **kwargs):
        original_gateway_init(gateway, *args, **kwargs)
        captured["gateway"] = gateway

    monkeypatch.setattr(pi_agent._VaultToolGateway, "__init__", gateway_init)
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*_command, **_kwargs):
        extension = Path(_command[_command.index("-e") + 1]).read_text()

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                response = await asyncio.to_thread(
                    _call_gateway, extension, "read_note", {"path": "People/Alice.md"}
                )
                assert response["result"] == "Alice prefers tea."
                captured["native_process"] = captured[
                    "gateway"
                ]._native_text_tools._process
                started.set()
                await self.released.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(PiMemoryAgent(root).run("Speaker: hello", "conv-cancel"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured["process"].killed
    assert captured["process"].waited
    assert captured["native_process"].poll() is not None
    assert captured["gateway"]._native_text_tools._closed
    assert captured["gateway"]._native_text_tools._process is None


@pytest.mark.asyncio
async def test_cancellation_during_timeout_cleanup_is_repropagated_after_reap(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    cleanup_started = asyncio.Event()
    allow_drain = asyncio.Event()
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.01),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*_command, **_kwargs):
        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False

            async def communicate(self, input_bytes=None):
                await allow_drain.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                cleanup_started.set()

            async def wait(self):
                self.waited = True
                await allow_drain.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(
        PiMemoryAgent(root).run("Speaker: hello", "conv-cleanup")
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_drain.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_cancellation_during_gateway_close_waits_for_close_then_propagates(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(summary="done", tool_name="write_note", usage={}),
        ),
    )
    original_aclose = pi_agent._VaultToolGateway.aclose

    async def delayed_aclose(self):
        close_started.set()
        await allow_close.wait()
        await original_aclose(self)

    monkeypatch.setattr(pi_agent._VaultToolGateway, "aclose", delayed_aclose)

    task = asyncio.create_task(PiMemoryAgent(root).run("Speaker: hello", "conv-close"))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_invoke_reconciles_limit_admitted_during_gateway_close(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="done",
                tool_name="write_note",
                usage={},
            ),
        ),
    )
    original_aclose = pi_agent._VaultToolGateway.aclose

    async def limit_during_close(self):
        self.set_limit("hard limit admitted during close", admitted=True)
        await original_aclose(self)

    monkeypatch.setattr(pi_agent._VaultToolGateway, "aclose", limit_during_close)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-limit-close")

    assert result.truncated
    assert result.errors.count("hard limit admitted during close") == 1


@pytest.mark.asyncio
async def test_write_tool_call_limit_rejects_extra_mutation_and_terminates_pi(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(pi_agent, "MAX_PI_WRITE_TOOL_CALLS", 2, raising=False)
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.2),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **_kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                for index in range(3):
                    try:
                        await asyncio.to_thread(
                            _call_gateway,
                            extension,
                            "write_note",
                            {
                                "path": f"Conversations/{index}.md",
                                "content": f"note {index}",
                            },
                        )
                    except urllib.error.HTTPError as exc:
                        captured["limit_status"] = exc.code
                        captured["limit_body"] = exc.read().decode()
                        await self.released.wait()
                        return _jsonl({"type": "agent_start"}), b""
                return (
                    _jsonl(
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "unbounded"}],
                                "stopReason": "stop",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-limit")

    assert sorted(path.name for path in (root / "Conversations").glob("*.md")) == [
        "0.md",
        "1.md",
    ]
    assert captured["limit_status"] == 429
    assert result.truncated
    assert any("tool-call limit exceeded (2)" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_search_tool_round_limit_signal_terminates_pi(tmp_path, monkeypatch):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.2),
    )

    async def prompt(*_args, **_kwargs):
        return "search"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **_kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")
        captured["extension"] = extension

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                try:
                    captured["limit_status"] = _report_gateway_limit(
                        extension, "Pi tool-round limit exceeded (2)"
                    )
                except urllib.error.HTTPError as exc:
                    captured["limit_status"] = exc.code
                    return (
                        _jsonl(
                            {
                                "type": "message_end",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "unbounded"}],
                                    "stopReason": "stop",
                                },
                            },
                            {"type": "agent_end", "messages": []},
                        ),
                        b"",
                    )
                await self.released.wait()
                return _jsonl({"type": "agent_start"}), b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await search_vault_with_pi("question", root, max_rounds=2)

    assert 'pi.on("turn_start"' in captured["extension"]
    assert captured["limit_status"] == 204
    assert result.answer == "(Pi search failed before completing)"
    assert any("tool-round limit exceeded (2)" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited


def test_gateway_stops_interleaved_unchanged_failures(tmp_path):
    gateway = pi_agent._VaultToolGateway(
        tmp_path / "vault",
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=96,
        max_identical_tool_calls=3,
    )
    with gateway:
        extension = pi_agent._extension_source(
            vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
            gateway_url=gateway.url,
            token=gateway.token,
        )
        for _ in range(3):
            for path in ["People/Missing.md", "Topics/Missing.md"]:
                result = _call_gateway(extension, "read_note", {"path": path})
                assert result["result"].startswith("Error:")
        with pytest.raises(urllib.error.HTTPError) as failure:
            _call_gateway(extension, "read_note", {"path": "People/Missing.md"})
        assert failure.value.code == 429
        assert gateway.limit_kind == "repeated_tool_call"
        assert gateway.call_count == 7


def test_gateway_exposes_current_investigation_tools(tmp_path):
    from pydantic import BaseModel

    from backend.services.timeline.pi_tasks import InvestigationTools

    class Result(BaseModel):
        answer: str

    tools = InvestigationTools(tmp_path, {"source": "Evidence"}, {}, Result)
    tools.max_tool_calls = 3
    with pi_agent._VaultToolGateway(
        tmp_path, schemas=tools.schemas, tool_handler=tools
    ) as gateway:
        tools.on_call = lambda: gateway.call_count
        extension = pi_agent._extension_source(
            tools.schemas, gateway_url=gateway.url, token=gateway.token
        )
        response = _call_gateway(
            extension, "read_material", {"store": "evidence", "key": "source"}
        )
        assert set(response["available_tools"]) == {"finish_task"}
        response = _call_gateway(
            extension, "finish_task", {"result": {"answer": "Done"}}
        )
        assert response["terminate"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
@pytest.mark.parametrize("initial", [None, ["finish_task", "revise_result"]])
def test_extension_retires_unavailable_reads_including_on_resume(tmp_path, initial):
    from pydantic import BaseModel

    from backend.services.timeline.pi_tasks import InvestigationTools

    class Result(BaseModel):
        answer: str

    tools = InvestigationTools(tmp_path, {}, {}, Result)
    extension_path = tmp_path / "tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            tools.schemas,
            gateway_url="http://127.0.0.1:1/tool",
            token="test",
            available_tools=initial,
            result_repair_tools=tools.result_repair_tools,
        )
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
const handlers = {{}}, tools = {{}};
let active = null;
extension({{
  on(name, fn) {{handlers[name] = fn;}},
  registerTool(tool) {{tools[tool.name] = tool;}},
  setActiveTools(names) {{active = names;}},
  appendEntry() {{}},
}});
handlers.session_start({{}}, {{sessionManager: {{getBranch: () => []}}}});
if ({json.dumps(initial)} !== null && active.includes('read_material')) throw Error('Resume exposed exhausted reads');
globalThis.fetch = async () => ({{ok:true, text:async()=>JSON.stringify({{
  result:'Error: Candidate needs correction', available_tools:['finish_task','revise_result'],
}})}});
try {{await tools.finish_task.execute('submit', {{}});}} catch (_) {{}}
if (active.length !== 1 || active[0] !== 'revise_result') throw Error('Unavailable tool remains active');
"""
    run = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert run.returncode == 0, run.stderr
