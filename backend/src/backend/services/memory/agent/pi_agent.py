"""Pi CLI executor for Chronicle's memory write and retrieval agents.

Pi supplies the agent loop, but it never receives filesystem or shell tools.  A generated
JavaScript extension registers Chronicle's canonical vault schemas and forwards each call
to a short-lived, bearer-authenticated HTTP server bound to loopback.  The Python side then
dispatches through :class:`VaultTools`, preserving the same validation, locking, and audit
tracking as the direct executor.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import backend.services.privacy as privacy
from backend.model_registry import AppModels, ResolvedLLMOperation, get_models_registry
from backend.services.inference_artifacts import canonical_hash, persist_inference_run

from ..session_write import WriteSourcePermissions
from ..telemetry import (
    current_memory_attempt,
    current_otel_context,
    memory_span,
    record_llm_usage_span,
    set_observation_io,
    set_safe_span_attributes,
    text_payload,
)
from .edit_engine import EditError
from .inspection_evidence import INSPECTION_TOOLS, InspectionEvidence, inspection_window
from .memory_agent import (
    AGENT_SYSTEM_PROMPT_ID,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    MAX_SEARCH_ROUNDS,
    MAX_TOOL_ROUNDS,
    PI_SEARCH_FAILURE_ANSWER,
    SEARCH_FINAL_SYNTHESIS_SYSTEM_SUFFIX,
    SEARCH_SYSTEM_PROMPT,
    MemoryAgentResult,
    VaultSearchResult,
    _get_prompt,
    _search_final_synthesis_prompt,
    allow_new_categories,
    build_write_task,
    forbidden_folders,
    immutable_sections,
    required_notes,
    write_source_permissions,
    write_system_prompt,
)
from .operating_memory import (
    RECALL_OPERATING_MEMORY_SCHEMA,
    OperatingMemoryStore,
    VaultWithOperatingMemoryTools,
)
from .pi_native_tools import PiNativeTextTools
from .vault_skill import write_skill
from .vault_tools import (
    VAULT_SEARCH_TOOL_SCHEMAS,
    VAULT_TOOL_SCHEMAS,
    VaultToolError,
    VaultTools,
)

logger = logging.getLogger("memory_service.agent.pi")

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_CONTEXT_WINDOW = 32768
DEFAULT_MAX_TOKENS = 4096
MIN_PROMPT_HEADROOM_TOKENS = 1024
MAX_PI_WRITE_TOOL_CALLS = MAX_TOOL_ROUNDS * 4
MAX_INSPECTION_CALLS_WITHOUT_MUTATION = 24
_PI_TOOL_CALLS_PER_SEARCH_ROUND = 4
_STDERR_TAIL_CHARS = 2000
_MAX_GATEWAY_REQUEST_BYTES = 16 * 1024 * 1024
_GATEWAY_DRAIN_TIMEOUT_SECONDS = 65.0
_TERMINAL_SEARCH_EVIDENCE_RE = re.compile(
    r"^Search result unchanged \[(?:grep|glob):([0-9a-f]{12})\]:"
)
_ALL_VAULT_TOOLS = frozenset(
    schema["function"]["name"] for schema in VAULT_TOOL_SCHEMAS
)
_READ_ONLY_VAULT_TOOLS = frozenset(
    schema["function"]["name"] for schema in VAULT_SEARCH_TOOL_SCHEMAS
)
if not _READ_ONLY_VAULT_TOOLS < _ALL_VAULT_TOOLS:
    raise RuntimeError(
        "Pi vault schemas must define a nonempty write-only tool difference"
    )
# verify_vault only reads; it is absent from the search agent's set because a read-only
# retrieval run has nothing to verify, not because it mutates.
_MUTATING_VAULT_TOOLS = _ALL_VAULT_TOOLS - _READ_ONLY_VAULT_TOOLS - {"verify_vault"}


def _apply_operating_guidance(
    system_prompt: str, operating_store: OperatingMemoryStore
) -> str:
    """Append one stable, versioned guidance snapshot for the lifetime of a Pi run."""

    operating_guidance = operating_store.read_agents()
    if not operating_guidance:
        return system_prompt
    return (
        f"{system_prompt}\n\n"
        "# Learned operating guidance\n"
        "This versioned guidance was learned from prior Chronicle Pi traces. "
        "Treat it as reusable heuristics, not fixed file routing; current vault "
        "evidence wins when it disagrees.\n\n"
        f"{operating_guidance}"
    )


# Built-ins remain disabled: cwd is not a sandbox, including in Pi 0.85.1.
# Confined VaultTools reads and locked edits reuse public native factories against
# virtual text buffers. Only Chronicle resolves paths and commits validated content.
_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_PROXY_ENV_NAMES = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
}
_PI_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_PI_COMPAT_BOOLEAN_FIELDS = {
    "supportsDeveloperRole",
    "supportsReasoningEffort",
    "supportsStore",
    "supportsStrictMode",
    "supportsUsageInStreaming",
}
_PI_MAX_TOKEN_FIELDS = {"max_completion_tokens", "max_tokens"}
_PI_THINKING_FORMATS = {
    "ant-ling",
    "chat-template",
    "deepseek",
    "openai",
    "openrouter",
    "qwen",
    "qwen-chat-template",
    "string-thinking",
    "together",
    "zai",
}


class PiExecutorError(RuntimeError):
    """A Pi configuration, startup, or pre-execution validation failure."""


class _PiLimitExceeded(RuntimeError):
    """A gateway preflight rejected a tool call beyond the configured hard cap."""


@dataclass(frozen=True)
class _PiRuntimeConfig:
    binary: str
    provider: str
    model: str
    base_url: str
    api_key: str
    thinking: str
    max_tokens: int
    context_window: int
    timeout_seconds: int
    reasoning: bool
    temperature: float
    sampling: Dict[str, Any] = field(default_factory=dict)
    response_format: Optional[Dict[str, Any]] = None
    seed: Optional[int] = None
    input_modalities: List[str] = field(default_factory=lambda: ["text"])
    system_prompt_prefix: str = ""
    compat: Dict[str, Any] = field(default_factory=dict)
    reasoning_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.reasoning_allowed:
            object.__setattr__(self, "thinking", "off")


@dataclass
class _PiEventResult:
    summary: str = ""
    rounds: int = 0
    tool_calls: int = 0
    usage: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    fatal_errors: List[str] = field(default_factory=list)
    stop_reason: str = ""
    assistant_content: List[Any] = field(default_factory=list)
    truncated: bool = False
    agent_ended: bool = False
    terminated_after_tool: bool = False
    # Retained for the durable inference audit. These are deliberately excluded
    # from MemoryAgentResult and logs because the JSONL/tool stream can contain private
    # transcript and vault content.
    stdout: str = ""
    stderr: str = ""
    returncode: Optional[int] = None
    terminated_by_chronicle: bool = False
    failure_kind: str | None = None


def _pi_settings(registry: AppModels) -> Dict[str, Any]:
    """Return exactly ``memory.backends.pi`` from the merged Chronicle config."""
    memory = registry.memory or {}
    backends = memory.get("backends")
    if backends is None:
        backends = {}
    if not isinstance(backends, dict):
        raise PiExecutorError("memory.backends must be a mapping")
    settings = backends.get("pi")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise PiExecutorError("memory.backends.pi must be a mapping")
    if settings.get("model") not in (None, ""):
        raise PiExecutorError(
            "memory.backends.pi.model is obsolete; select models with defaults or "
            "llm_operations"
        )
    return dict(settings)


def _positive_int(value: Any, *, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise PiExecutorError(f"memory.backends.pi.{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PiExecutorError(
            f"memory.backends.pi.{name} must be a positive integer"
        ) from exc
    if parsed <= 0:
        raise PiExecutorError(f"memory.backends.pi.{name} must be a positive integer")
    return parsed


def _optional_seed(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise PiExecutorError("memory.backends.pi.seed must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PiExecutorError(
            "memory.backends.pi.seed must be a non-negative integer"
        ) from exc
    if parsed < 0:
        raise PiExecutorError("memory.backends.pi.seed must be a non-negative integer")
    return parsed


def _provider_slug(provider: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-") or "llm"
    return f"chronicle-{suffix}"


def _thinking_level(value: Any, operation: ResolvedLLMOperation) -> str:
    if not operation.reasoning_allowed:
        return "off"
    if value is None or value == "":
        value = operation.effective_reasoning_effort
    if value is None or value == "":
        value = "off"
    if isinstance(value, bool):
        value = "low" if value else "off"
    level = str(value).strip().lower()
    if level in ("none", "0"):
        level = "off"
    if level not in _THINKING_LEVELS:
        allowed = ", ".join(sorted(_THINKING_LEVELS))
        raise PiExecutorError(
            f"memory.backends.pi.thinking must be one of {allowed}; got {value!r}"
        )
    return level


def _validated_pi_compat(value: Any, *, source: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PiExecutorError(f"{source} must be a mapping")

    allowed = _PI_COMPAT_BOOLEAN_FIELDS | {"maxTokensField", "thinkingFormat"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PiExecutorError(
            f"{source} contains unsupported field(s): {', '.join(unknown)}"
        )

    compat = dict(value)
    for name in _PI_COMPAT_BOOLEAN_FIELDS:
        if name in compat and not isinstance(compat[name], bool):
            raise PiExecutorError(f"{source}.{name} must be a boolean")
    if (
        "maxTokensField" in compat
        and compat["maxTokensField"] not in _PI_MAX_TOKEN_FIELDS
    ):
        allowed_tokens = ", ".join(sorted(_PI_MAX_TOKEN_FIELDS))
        raise PiExecutorError(
            f"{source}.maxTokensField must be one of {allowed_tokens}"
        )
    if (
        "thinkingFormat" in compat
        and compat["thinkingFormat"] not in _PI_THINKING_FORMATS
    ):
        allowed_formats = ", ".join(sorted(_PI_THINKING_FORMATS))
        raise PiExecutorError(
            f"{source}.thinkingFormat must be one of {allowed_formats}"
        )
    return compat


def _pi_model_compat(
    model_def: Any, resolved: ResolvedLLMOperation, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Derive conservative OpenAI-completions compatibility, then apply overrides."""
    provider = str(model_def.model_provider).strip().lower()
    model = str(resolved.model_name).strip().lower()
    local_provider = any(
        marker in provider for marker in ("llama.cpp", "llamacpp", "ollama", "local")
    )
    official_openai = provider == "openai"

    compat: Dict[str, Any] = {
        "supportsDeveloperRole": official_openai and not local_provider,
        "supportsReasoningEffort": official_openai and not local_provider,
        "maxTokensField": (
            "max_completion_tokens"
            if official_openai
            and (model.startswith("gpt-5") or re.match(r"o\d", model))
            else "max_tokens"
        ),
    }
    if bool(model_def.thinking):
        if "qwen" in model:
            compat["thinkingFormat"] = "qwen-chat-template"
        elif "deepseek" in model:
            compat["thinkingFormat"] = "deepseek"
        elif "glm" in model or "zai" in provider:
            compat["thinkingFormat"] = "zai"

    model_params = model_def.model_params or {}
    compat.update(
        _validated_pi_compat(
            model_params.get("pi_compat"),
            source=f"models.{model_def.name}.model_params.pi_compat",
        )
    )
    compat.update(
        _validated_pi_compat(settings.get("compat"), source="memory.backends.pi.compat")
    )
    return compat


def _resolve_pi_config(
    operation: str, *, force_fallback: bool = False
) -> _PiRuntimeConfig:
    registry = get_models_registry()
    if registry is None:
        raise PiExecutorError("Chronicle model registry is unavailable")
    settings = _pi_settings(registry)

    resolved = registry.get_llm_operation(operation, model_override=None)
    if force_fallback:
        fallback = registry.get_fallback_llm_operation(operation, primary=resolved)
        if fallback is None:
            raise PiExecutorError(
                f"no distinct fallback LLM is configured for operation {operation!r}"
            )
        resolved = fallback

    model_def = resolved.model_def
    if str(model_def.api_family).lower() != "openai":
        raise PiExecutorError(
            f"Pi backend requires an OpenAI-compatible LLM; "
            f"model {model_def.name!r} uses api_family={model_def.api_family!r}"
        )
    base_url = resolved.base_url.strip()
    if not base_url:
        raise PiExecutorError(f"model {model_def.name!r} has no resolvable base URL")

    model_params = model_def.model_params or {}
    prefix = model_def.system_prompt_prefix
    capabilities = {
        str(item).strip().lower() for item in (model_def.capabilities or [])
    }
    input_modalities = ["text", "image"] if "vision" in capabilities else ["text"]
    context_default = getattr(model_def, "context_window", None)
    if context_default is None:
        context_default = model_params.get("context_window")
    context_window = _positive_int(
        settings.get("context_window", context_default),
        name="context_window",
        default=DEFAULT_CONTEXT_WINDOW,
    )
    max_tokens = _positive_int(
        (
            resolved.max_tokens
            if resolved.max_tokens is not None
            else settings.get("max_tokens")
        ),
        name="max_tokens",
        default=DEFAULT_MAX_TOKENS,
    )
    if max_tokens > context_window - MIN_PROMPT_HEADROOM_TOKENS:
        raise PiExecutorError(
            "memory.backends.pi.max_tokens must leave at least "
            f"{MIN_PROMPT_HEADROOM_TOKENS} tokens of context for the prompt"
        )

    available, binary = pi_executor_available()
    if not available:
        raise PiExecutorError(binary)

    return _PiRuntimeConfig(
        binary=binary,
        provider=_provider_slug(str(model_def.model_provider)),
        model=resolved.model_name,
        base_url=base_url,
        api_key=resolved.api_key or "no-key",
        thinking=_thinking_level(settings.get("thinking"), resolved),
        max_tokens=max_tokens,
        context_window=context_window,
        timeout_seconds=_positive_int(
            settings.get("timeout_seconds"),
            name="timeout_seconds",
            default=DEFAULT_TIMEOUT_SECONDS,
        ),
        reasoning=bool(model_def.thinking),
        reasoning_allowed=resolved.reasoning_allowed,
        temperature=resolved.temperature,
        sampling={
            key: model_params[key]
            for key in (
                "top_p",
                "top_k",
                "min_p",
                "presence_penalty",
                "frequency_penalty",
                "repeat_penalty",
            )
            if model_params.get(key) is not None
        },
        response_format=resolved.response_format,
        seed=_optional_seed(settings.get("seed", model_params.get("seed"))),
        input_modalities=input_modalities,
        system_prompt_prefix=prefix.strip(),
        compat=_pi_model_compat(model_def, resolved, settings),
    )


def validate_pi_executor_config(
    operation: str, *, force_fallback: bool = False
) -> None:
    """Validate that one operation resolves to a runnable Pi configuration."""
    config = _resolve_pi_config(operation, force_fallback=force_fallback)
    try:
        PiNativeTextTools.validate_runtime(config.binary)
    except EditError as exc:
        raise PiExecutorError(str(exc)) from exc


def pi_executor_available() -> tuple[bool, str]:
    """Return whether the Pi executable is available; Pi needs no separate auth file."""
    requested = os.environ.get("PI_BINARY", "pi")
    binary = shutil.which(requested)
    if not binary:
        return False, f"Pi binary {requested!r} not found on PATH"
    return True, binary


class _VaultToolGateway:
    """Short-lived loopback HTTP adapter around one canonical ``VaultTools`` instance."""

    def __init__(
        self,
        vault_root: Path,
        schemas: Sequence[Dict[str, Any]],
        *,
        max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
        on_limit: Optional[Callable[[], None]] = None,
        required_notes: Sequence[str] = (),
        forbidden_folders: Sequence[str] = (),
        immutable_sections: Sequence[tuple[str, str]] = (),
        allow_new_categories: bool = True,
        user_id: str = "",
        tool_handler: Any = None,
        terminate_on_verified: bool = False,
        max_identical_tool_calls: Optional[int] = None,
        native_binary: Optional[str] = None,
    ):
        self.tools = tool_handler or VaultTools(
            vault_root,
            trace_context=current_otel_context(),
            required_notes=required_notes,
            forbidden_folders=forbidden_folders,
            immutable_sections=immutable_sections,
            allow_new_categories=allow_new_categories,
            user_id=user_id,
        )
        self._native_text_tools: PiNativeTextTools | None = None
        canonical_tools = getattr(self.tools, "vault_tools", self.tools)
        if (
            native_binary
            and isinstance(canonical_tools, VaultTools)
            and any(
                schema["function"]["name"] in {"read_note", "edit_note"}
                for schema in schemas
            )
        ):
            self._native_text_tools = PiNativeTextTools(native_binary)
            canonical_tools.text_operations = self._native_text_tools
        self.schemas = list(schemas)
        self.allowed_names = {
            str(schema.get("function", {}).get("name", "")) for schema in self.schemas
        }
        self.mutating_names = frozenset(
            getattr(self.tools, "mutating_tools", _MUTATING_VAULT_TOOLS)
        )
        self.token = secrets.token_urlsafe(32)
        self.terminate_on_verified = bool(terminate_on_verified)
        self.max_identical_tool_calls = (
            _positive_run_limit(
                max_identical_tool_calls,
                name="Pi max_identical_tool_calls",
            )
            if max_identical_tool_calls is not None
            else None
        )
        self.terminal_completion = False
        self.required_notes = tuple(required_notes)
        self.errors: List[str] = []
        self._inspection_evidence = InspectionEvidence()
        self.read_notes = self._inspection_evidence.notes
        self._read_window_hashes: Dict[tuple[str, str], str] = {}
        self._previous_tool_signature: Optional[str] = None
        self._consecutive_identical_call_count = 0
        self._rejected_call_counts: Dict[str, int] = {}
        self._terminal_search_evidence_ids: set[str] = set()
        self._inspection_calls_since_mutation = 0
        self._max_inspection_calls_without_mutation = (
            MAX_INSPECTION_CALLS_WITHOUT_MUTATION if self.mutating_names else None
        )
        self.call_count = 0
        self.max_tool_calls = max_tool_calls
        self.limit_error: Optional[str] = None
        self.limit_kind = "budget_exhausted"
        self.close_error: Optional[str] = None
        # ``_closed`` fences admission immediately. ``_state_frozen`` is delayed
        # until admitted handlers drain (or the bounded drain expires), allowing
        # their completed read/error audit to reach the returned result without
        # permitting a late handler to race result assembly.
        self._closed = False
        self._state_frozen = False
        self._on_limit = on_limit
        self._state_lock = threading.Lock()
        self._request_condition = threading.Condition()
        self._active_requests = 0
        self._close_lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Pi vault-tool gateway has not been started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/tool"

    def __enter__(self) -> "_VaultToolGateway":
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path not in {"/limit", "/tool"}:
                    self._send_json(404, {"error": "not found"})
                    return
                authorization = self.headers.get("Authorization") or ""
                if not secrets.compare_digest(authorization, f"Bearer {gateway.token}"):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "invalid Content-Length"})
                    return
                if length <= 0 or length > _MAX_GATEWAY_REQUEST_BYTES:
                    self._send_json(413, {"error": "invalid request size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"error": "request body must be JSON"})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400, {"error": "request body must be an object"})
                    return
                if self.path == "/limit":
                    reason = payload.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        self._send_json(400, {"error": "limit reason must be a string"})
                        return
                    # Authentication and parsing completed inside an accepted
                    # request, so retain this hard-limit outcome if close races it.
                    gateway.set_limit(reason.strip(), admitted=True)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                name = payload.get("name")
                arguments = payload.get("arguments", {})
                if not isinstance(name, str) or name not in gateway.allowed_names:
                    self._send_json(400, {"error": f"unknown tool: {name}"})
                    return
                if not isinstance(arguments, dict):
                    self._send_json(400, {"error": "tool arguments must be an object"})
                    return
                try:
                    result = gateway.dispatch(name, arguments)
                except _PiLimitExceeded as exc:
                    self._send_json(429, {"error": str(exc)})
                    return
                terminate = gateway.should_terminate(name, result)
                response_payload: Dict[str, Any] = {"result": result}
                available_tools = getattr(gateway.tools, "available_tools", None)
                if available_tools is not None:
                    response_payload["available_tools"] = available_tools
                if terminate:
                    response_payload["terminate"] = True
                self._send_json(200, response_payload)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    logger.debug("Pi disconnected before gateway response was written")

        class GatewayServer(ThreadingHTTPServer):
            # Track accepted work ourselves so close can drain it with a hard deadline.
            daemon_threads = True
            block_on_close = False

            def process_request(self, request: Any, client_address: Any) -> None:
                gateway._request_started()
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    gateway._request_finished()
                    raise

            def process_request_thread(self, request: Any, client_address: Any) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    gateway._request_finished()

        self._server = GatewayServer(("127.0.0.1", 0), Handler)
        server = self._server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="chronicle-pi-vault-tools",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self._close()

    def _request_started(self) -> None:
        with self._request_condition:
            self._active_requests += 1

    def _request_finished(self) -> None:
        with self._request_condition:
            self._active_requests -= 1
            self._request_condition.notify_all()

    def _record_close_error(self, error: str) -> None:
        with self._state_lock:
            if self.close_error is None:
                self.close_error = error
                self.errors.append(error)

    def _close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                self._closed = True

            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            if server is not None:
                server.shutdown()
                server.server_close()

            # Mutating calls serialize through this gate and recheck ``_closed``
            # after acquiring it. Waiting here lets the one canonical mutation that
            # already started finish (including its VaultTools audit updates); queued
            # calls can only acquire the gate afterward and are rejected. _close runs
            # in asyncio.to_thread(), so even a slow finite vault operation never
            # blocks the event loop.
            with self._mutation_lock:
                pass

            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    self._record_close_error(
                        "Pi vault gateway server did not stop within 5 seconds"
                    )

            deadline = time.monotonic() + _GATEWAY_DRAIN_TIMEOUT_SECONDS
            with self._request_condition:
                while self._active_requests:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._record_close_error(
                            "Pi vault gateway requests did not drain within "
                            f"{_GATEWAY_DRAIN_TIMEOUT_SECONDS:g} seconds; "
                            "vault mutations are complete, but read/error details "
                            "may be omitted"
                        )
                        break
                    self._request_condition.wait(timeout=remaining)

            with self._state_lock:
                self._state_frozen = True
            if self._native_text_tools is not None:
                self._native_text_tools.close()

    async def aclose(self) -> None:
        """Stop and drain the HTTP server without blocking the asyncio event loop."""
        await asyncio.to_thread(self._close)

    def set_limit(
        self, reason: str, *, admitted: bool = False, kind: str = "budget_exhausted"
    ) -> None:
        notify = False
        with self._state_lock:
            if (
                not self._state_frozen
                and (admitted or not self._closed)
                and self.limit_error is None
            ):
                self.limit_error = reason
                self.limit_kind = kind
                notify = True
        if notify and self._on_limit is not None:
            self._on_limit()

    def dispatch(self, name: str, arguments: Dict[str, Any]) -> str:
        limit_error: Optional[str] = None
        limit_kind = "budget_exhausted"
        with self._state_lock:
            if self._closed:
                return "Error: Pi vault gateway is closing"
            if self.call_count >= self.max_tool_calls:
                limit_error = (
                    f"Pi tool-call limit exceeded ({self.max_tool_calls}); "
                    "the extra vault mutation was rejected"
                )
            else:
                # Authorized calls count even if VaultTools rejects their arguments.
                self.call_count += 1
                signature = canonical_hash({"name": name, "arguments": arguments})
                if signature == self._previous_tool_signature:
                    self._consecutive_identical_call_count += 1
                else:
                    self._previous_tool_signature = signature
                    self._consecutive_identical_call_count = 1
                if (
                    self.max_identical_tool_calls is not None
                    and self._consecutive_identical_call_count
                    > self.max_identical_tool_calls
                ):
                    limit_error = (
                        "Pi repeated an identical tool call consecutively more than "
                        f"{self.max_identical_tool_calls} times"
                    )
                    limit_kind = "repeated_tool_call"
                if (
                    limit_error is None
                    and self._max_inspection_calls_without_mutation is not None
                    and name not in self.mutating_names
                    and name != "verify_vault"
                ):
                    self._inspection_calls_since_mutation += 1
                    if (
                        self._inspection_calls_since_mutation
                        > self._max_inspection_calls_without_mutation
                    ):
                        limit_error = (
                            "Pi exceeded "
                            f"{self._max_inspection_calls_without_mutation} inspection "
                            "calls without a successful vault mutation"
                        )
        if limit_error is not None:
            # This call passed the admission check above, so preserve its limit
            # outcome if shutdown starts between releasing the lock and recording it.
            self.set_limit(limit_error, admitted=True, kind=limit_kind)
            raise _PiLimitExceeded(limit_error)
        if name in self.mutating_names:
            return self._dispatch_serialized_mutation(name, arguments)
        return self._execute_tool(name, arguments)

    def should_terminate(self, name: str, result: str) -> bool:
        """Return true only for a successful final verification with its record present."""

        completion = getattr(self.tools, "is_complete", None)
        if completion is not None and completion(name, result):
            with self._state_lock:
                if self._state_frozen:
                    return False
                self.terminal_completion = True
            return True

        if (
            not self.terminate_on_verified
            or name != "verify_vault"
            or result != "Vault verification passed: no problems found."
        ):
            return False
        touched = bool(getattr(self.tools, "touched", ()))
        required_present = not self.required_notes or all(
            (self.tools.root / relative).is_file() for relative in self.required_notes
        )
        # A successful no-op verification can be legitimate, but it is not a safe
        # terminal signal: the model may have verified before doing the requested work.
        if not touched or not required_present:
            return False
        with self._state_lock:
            if self._state_frozen:
                return False
            self.terminal_completion = True
        return True

    def _execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """Dispatch one tool and preserve its result metadata when still relevant."""
        try:
            privacy_guard = getattr(self, "privacy_guard", None)
            if privacy_guard:
                privacy_guard()
            result = self.tools.dispatch(name, arguments)
            if privacy_guard:
                privacy_guard()
            inspection_path = str(arguments.get("path", "?"))
            if name in INSPECTION_TOOLS:
                resolve_path = getattr(self.tools, "inspection_path", None)
                if resolve_path is not None:
                    inspection_path = resolve_path(inspection_path)
        except VaultToolError as exc:
            error = f"{name}: {exc}"
            repeated = False
            with self._state_lock:
                if not self._state_frozen:
                    self.errors.append(error)
                    signature = canonical_hash([name, arguments, str(exc)])
                    count = self._rejected_call_counts.get(signature, 0) + 1
                    self._rejected_call_counts[signature] = count
                    repeated = (
                        self.max_identical_tool_calls is not None
                        and count > self.max_identical_tool_calls
                    )
            if repeated:
                reason = (
                    "Pi repeated an unchanged rejected tool call more than "
                    f"{self.max_identical_tool_calls} times, including interleaved calls"
                )
                self.set_limit(reason, admitted=True, kind="repeated_tool_call")
                raise _PiLimitExceeded(reason) from exc
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface unexpected tool failures
            error = f"{name}: {type(exc).__name__}: {exc}"
            with self._state_lock:
                if not self._state_frozen:
                    self.errors.append(error)
            logger.exception("Pi vault tool %s crashed", name)
            return f"Error: {type(exc).__name__}: {exc}"

        if name in self.mutating_names:
            # A successful mutation can make a prior search result stale. Failed and
            # no-op mutations return from the exception paths above and do not reset
            # the evidence breaker.
            with self._state_lock:
                if not self._state_frozen:
                    self._terminal_search_evidence_ids.clear()
                    self._inspection_calls_since_mutation = 0
                    self._rejected_call_counts.clear()

        terminal_match = _TERMINAL_SEARCH_EVIDENCE_RE.match(result)
        if terminal_match:
            evidence_id = terminal_match.group(1)
            repeated_terminal_evidence = False
            with self._state_lock:
                if not self._state_frozen:
                    repeated_terminal_evidence = (
                        evidence_id in self._terminal_search_evidence_ids
                    )
                    self._terminal_search_evidence_ids.add(evidence_id)
            if repeated_terminal_evidence:
                limit_error = (
                    "Pi repeated terminal search evidence "
                    f"{name}:{evidence_id} after it was already declared unchanged"
                )
                self.set_limit(limit_error, admitted=True)
                raise _PiLimitExceeded(limit_error)

        if name in INSPECTION_TOOLS:
            path = inspection_path
            window_key = (path, inspection_window(name, arguments))
            result_hash = canonical_hash(result)[:12]
            repeated = False
            with self._state_lock:
                if not self._state_frozen:
                    self._inspection_evidence.record(name, arguments, result, path=path)
                    repeated = (
                        not bool(arguments.get("refresh", False))
                        and self._read_window_hashes.get(window_key) == result_hash
                    )
                    self._read_window_hashes[window_key] = result_hash
            if repeated:
                return (
                    f"[unchanged {name} window {result_hash}; content already "
                    "returned earlier in this Pi run. Pass refresh=true only if you "
                    "need the same bytes repeated.]"
                )
        return result

    def _dispatch_serialized_mutation(
        self, name: str, arguments: Dict[str, Any]
    ) -> str:
        """Run one canonical mutation, rejecting calls queued behind shutdown."""
        with self._mutation_lock:
            with self._state_lock:
                if self._closed:
                    return "Error: Pi vault gateway is closing"
            # Keep error capture inside the mutation gate too. Shutdown therefore
            # cannot observe a completed mutation without its corresponding audit
            # error, even when it marked the gateway closed while the tool ran.
            result = self._execute_tool(name, arguments)
            if not result.lstrip().startswith("Error:"):
                with self._state_lock:
                    self._previous_tool_signature = None
                    self._consecutive_identical_call_count = 0
            return result


def _extension_source(
    schemas: Sequence[Dict[str, Any]],
    *,
    gateway_url: str,
    token: str,
    temperature: float = 0.2,
    sampling: Optional[Dict[str, Any]] = None,
    response_format: Optional[Dict[str, Any]] = None,
    disable_thinking: bool = False,
    seed: Optional[int] = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
    recover_repeated_tool_errors: bool = False,
    result_repair_tools: Optional[Dict[str, str]] = None,
    available_tools: Optional[List[str]] = None,
) -> str:
    """Generate a dependency-free ESM extension exposing only canonical schemas."""
    tool_defs = [schema["function"] for schema in schemas]
    return f"""\
const gatewayUrl = {json.dumps(gateway_url)};
const limitUrl = {json.dumps(gateway_url.removesuffix("/tool") + "/limit")};
const bearerToken = {json.dumps(token)};
const toolDefinitions = {json.dumps(tool_defs, separators=(",", ":"))};
const temperature = {json.dumps(temperature)};
const sampling = {json.dumps(sampling or {})};
const responseFormat = {json.dumps(response_format, separators=(",", ":"))};
const disableThinking = {json.dumps(disable_thinking)};
const seed = {json.dumps(seed)};
const maxToolRounds = {max_tool_rounds};
const maxToolCalls = {max_tool_calls};
const recoverRepeatedToolErrors = {json.dumps(recover_repeated_tool_errors)};
const resultRepairTools = {json.dumps(result_repair_tools or {})};
const initialAvailableTools = {json.dumps(available_tools)};

export default function (pi) {{
  let toolRounds = 0;
  let toolCalls = 0;
  let roundHasTool = false;
  let limitReported = false;
  let repeatRecoveryUsed = false;
  let repeatRecoveryPending = false;
  const rejectedReads = new Map();
  const rejectedSubmissions = new Set();
  let availableTools = initialAvailableTools;
  function updateActiveTools() {{
    if (rejectedSubmissions.size || availableTools !== null) {{
      pi.setActiveTools(toolDefinitions.filter((tool) => !rejectedSubmissions.has(tool.name) && (availableTools === null || availableTools.includes(tool.name))).map((tool) => tool.name));
    }}
  }}

  pi.on("session_start", (_event, context) => {{
    repeatRecoveryUsed = context.sessionManager.getBranch().some(
      (entry) => entry.type === "custom" && entry.customType === "chronicle-repeat-recovery"
    );
    for (const entry of context.sessionManager.getBranch()) {{
      if (entry.type === "custom" && entry.customType === "chronicle-result-repair") {{
        rejectedSubmissions.add(entry.data.tool);
      }}
    }}
    updateActiveTools();
  }});

  pi.on("turn_end", (_event, context) => {{
    if (!repeatRecoveryPending || repeatRecoveryUsed) return;
    repeatRecoveryPending = false;
    repeatRecoveryUsed = true;
    pi.appendEntry("chronicle-repeat-recovery", {{ reason: "repeated rejected tool call" }});
    pi.sendUserMessage(
      "The last repeated tool requests were rejected and added no new evidence. Use the retained results, inspect a different passage if necessary, and complete the remaining task within the existing budget. Full sources remain available.",
      {{ deliverAs: "steer" }},
    );
  }});

  pi.on("before_provider_request", (event) => ({{
    ...event.payload,
    ...sampling,
    temperature,
    ...(responseFormat === null ? {{}} : {{ response_format: responseFormat }}),
    ...(seed === null ? {{}} : {{ seed }}),
    ...(disableThinking ? {{ chat_template_kwargs: {{ ...event.payload.chat_template_kwargs, enable_thinking: false }} }} : {{}}),
  }}));

  pi.on("turn_start", () => {{
    roundHasTool = false;
  }});

  async function stopAtLimit(reason, context) {{
    if (!limitReported) {{
      limitReported = true;
      try {{
        await fetch(limitUrl, {{
          method: "POST",
          headers: {{
            "Authorization": `Bearer ${{bearerToken}}`,
            "Content-Type": "application/json",
          }},
          body: JSON.stringify({{ reason }}),
        }});
      }} catch (_error) {{
        // The blocking result still prevents the tool call if the process-level
        // watcher cannot be notified because the gateway is already closing.
      }}
    }}
    context.abort();
    return {{ block: true, reason }};
  }}

  pi.on("tool_call", async (_event, context) => {{
    const nextToolRound = toolRounds + (roundHasTool ? 0 : 1);
    if (nextToolRound > maxToolRounds) {{
      return stopAtLimit(`Pi tool-round limit exceeded (${{maxToolRounds}})`, context);
    }}
    const nextToolCall = toolCalls + 1;
    if (nextToolCall > maxToolCalls) {{
      return stopAtLimit(`Pi tool-call limit exceeded (${{maxToolCalls}})`, context);
    }}
    toolRounds = nextToolRound;
    toolCalls = nextToolCall;
    roundHasTool = true;
  }});

  for (const definition of toolDefinitions) {{
    pi.registerTool({{
      name: definition.name,
      label: definition.name,
      description: definition.description,
      parameters: definition.parameters,
      executionMode: "sequential",
      async execute(_toolCallId, params, signal) {{
        const response = await fetch(gatewayUrl, {{
          method: "POST",
          headers: {{
            "Authorization": `Bearer ${{bearerToken}}`,
            "Content-Type": "application/json",
          }},
          body: JSON.stringify({{ name: definition.name, arguments: params }}),
          signal,
        }});
        const body = await response.text();
        if (!response.ok) {{
          throw new Error(`Chronicle vault gateway ${{response.status}}: ${{body}}`);
        }}
        let payload;
        try {{
          payload = JSON.parse(body);
        }} catch (error) {{
          throw new Error(`Chronicle vault gateway returned invalid JSON: ${{error}}`);
        }}
        if (typeof payload.result !== "string") {{
          throw new Error("Chronicle vault gateway response omitted string result");
        }}
        if (Array.isArray(payload.available_tools)) {{
          availableTools = payload.available_tools;
          updateActiveTools();
        }}
        if (payload.result.trimStart().startsWith("Error:")) {{
          if (resultRepairTools[definition.name] && !rejectedSubmissions.has(definition.name)) {{
            // A saved rejected candidate is now edited in place. Retiring full
            // submission prevents generation of the same large payload again.
            rejectedSubmissions.add(definition.name);
            pi.appendEntry("chronicle-result-repair", {{ tool: definition.name, repairTool: resultRepairTools[definition.name] }});
            updateActiveTools();
          }}
          if (recoverRepeatedToolErrors && !repeatRecoveryUsed) {{
            const signature = JSON.stringify([definition.name, params]);
            const count = (rejectedReads.get(signature) ?? 0) + 1;
            rejectedReads.set(signature, count);
            if (count >= 2) repeatRecoveryPending = true;
          }}
          throw new Error(payload.result);
        }}
        return {{
          content: [{{ type: "text", text: payload.result }}],
          details: {{}},
          terminate: payload.terminate === true,
        }};
      }},
    }});
  }}
}}
"""


def _models_payload(config: _PiRuntimeConfig) -> Dict[str, Any]:
    return {
        "providers": {
            config.provider: {
                "baseUrl": config.base_url,
                "api": "openai-completions",
                # Pi resolves leading ! as a shell command and $NAME as an env
                # reference. Chronicle supplies a literal key, so escape Pi's value
                # syntax before writing the isolated model registry.
                "apiKey": _pi_literal_config_value(config.api_key),
                "compat": config.compat,
                "models": [
                    {
                        "id": config.model,
                        "name": config.model,
                        "reasoning": config.reasoning,
                        "input": config.input_modalities,
                        "contextWindow": config.context_window,
                        "maxTokens": config.max_tokens,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }


def _pi_literal_config_value(value: str) -> str:
    """Encode one literal for Pi's models.json config-value resolver."""
    escaped = value.replace("$", "$$")
    return f"${escaped}" if escaped.startswith("!") else escaped


def _pi_subprocess_env(temp_dir: Path) -> Dict[str, str]:
    """Pass only networking/runtime essentials, never the backend's secret-filled env."""
    env = {
        name: value for name, value in os.environ.items() if name in _PI_ENV_ALLOWLIST
    }
    # Pi's HTTP stack installs an environment-proxy dispatcher globally. Without an
    # explicit bypass, its generated vault extension can send the loopback gateway's
    # bearer token, tool arguments, and note content through HTTP(S)_PROXY. Merge both
    # conventional spellings because proxy libraries disagree about case.
    no_proxy_entries: List[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        for entry in env.get(name, "").split(","):
            normalized = entry.strip()
            if normalized and normalized not in no_proxy_entries:
                no_proxy_entries.append(normalized)
    for loopback in ("127.0.0.1", "localhost", "::1"):
        if loopback not in no_proxy_entries:
            no_proxy_entries.append(loopback)
    no_proxy = ",".join(no_proxy_entries)
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(temp_dir),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    return env


def _redact(value: str, *secrets_to_remove: str) -> str:
    redacted = value
    secrets_by_length = sorted(
        {secret for secret in secrets_to_remove if secret and secret != "no-key"},
        key=len,
        reverse=True,
    )
    for secret in secrets_by_length:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _url_credential_values(value: str) -> List[str]:
    """Extract encoded and decoded URL userinfo without treating the host as secret."""
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
    except ValueError:
        return []
    if "@" not in parsed.netloc:
        return []

    userinfo = parsed.netloc.rsplit("@", 1)[0]
    values = [value, userinfo, unquote(userinfo)]
    # Redact a standalone credential when it is distinctive enough not to turn
    # every one-letter match in a diagnostic into noise. The complete URL and
    # userinfo are always redacted, regardless of length.
    credential = parsed.password if parsed.password is not None else parsed.username
    if credential and len(credential) >= 4:
        values.extend([credential, unquote(credential)])
    return [item for item in values if item]


def _pi_redaction_values(config: _PiRuntimeConfig) -> List[str]:
    values = [config.api_key]
    values.extend(_url_credential_values(config.base_url))
    for name in _PROXY_ENV_NAMES:
        proxy = os.environ.get(name)
        if proxy:
            values.extend(_url_credential_values(proxy))
    return values


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(part for part in parts if part).strip()


def _event_error(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        message = value.get("message") or value.get("errorMessage")
        if isinstance(message, str):
            return message.strip()
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


def _tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        text = _message_text(result)
        if text:
            return text
    return _event_error(result)


def _usage(message: Dict[str, Any]) -> Dict[str, int]:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return {}
    field_map = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cacheRead": "input_cached_tokens",
        "cacheWrite": "input_cache_write_tokens",
        "reasoning": "output_reasoning_tokens",
        "totalTokens": "total_tokens",
    }
    parsed: Dict[str, int] = {}
    for source, target in field_map.items():
        value = raw.get(source)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parsed[target] = int(value)
    return parsed


def _parse_events(stdout: str, *, allow_terminal_tool: bool = False) -> _PiEventResult:
    """Parse Pi's JSONL contract, preserving protocol and agent failures."""
    parsed = _PiEventResult()
    assistant_messages: List[Dict[str, Any]] = []
    pending_retry_errors: List[str] = []
    last_assistant_stop_reason = ""

    # JSONL records are delimited by LF, not by every Unicode character that
    # ``str.splitlines`` considers a line boundary. Pi's JSON serializer may emit
    # U+2028 inside a valid JSON string; splitting there corrupts otherwise valid
    # message and agent_end events.
    for line_number, raw_line in enumerate(stdout.split("\n"), start=1):
        line = raw_line.strip()
        if not line:
            continue
        # Pi 0.83 emits cumulative message snapshots. ``message_end`` carries the
        # authoritative complete message, so parsing every intermediate snapshot is
        # both redundant and disproportionately expensive for long runs.
        if '"type":"message_update"' in line[:80]:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            parsed.fatal_errors.append(
                f"Pi emitted invalid JSONL on line {line_number}: {exc.msg}"
            )
            continue
        if not isinstance(event, dict):
            parsed.fatal_errors.append(
                f"Pi emitted non-object JSONL on line {line_number}"
            )
            continue

        event_type = str(event.get("type") or "")
        if event_type == "tool_execution_start":
            parsed.tool_calls += 1
        elif event_type == "tool_execution_end" and event.get("isError") is True:
            name = str(event.get("toolName") or "unknown")
            parsed.errors.append(
                f"{name}: {_tool_result_text(event.get('result')) or 'tool failed'}"
            )
        elif event_type == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            assistant_messages.append(message)
            content = message.get("content")
            if isinstance(content, list):
                parsed.assistant_content = list(content)
            parsed.rounds += 1
            for key, value in _usage(message).items():
                parsed.usage[key] = parsed.usage.get(key, 0) + value
            text = _message_text(message)
            if text:
                parsed.summary = text
            error_message = message.get("errorMessage")
            stop_reason = str(message.get("stopReason") or "").lower()
            last_assistant_stop_reason = stop_reason
            if stop_reason == "error":
                pending_retry_errors.append(
                    f"Pi assistant error: "
                    f"{_event_error(error_message) if error_message else 'request failed'}"
                )
            elif error_message:
                parsed.fatal_errors.append(
                    f"Pi assistant error: {_event_error(error_message)}"
                )
            if stop_reason == "aborted":
                parsed.fatal_errors.append(
                    f"Pi assistant stopped with reason {stop_reason!r}"
                )
        elif event_type == "agent_end":
            parsed.agent_ended = True
            if not parsed.summary:
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if (
                            isinstance(message, dict)
                            and message.get("role") == "assistant"
                        ):
                            text = _message_text(message)
                            if text:
                                parsed.summary = text
                                if not assistant_messages:
                                    parsed.rounds = 1
                                    parsed.usage.update(_usage(message))
                                break
        elif event_type == "extension_error":
            extension_event = str(event.get("event") or "unknown event")
            detail = event.get("error", event.get("message", event))
            parsed.fatal_errors.append(
                f"Pi extension error during {extension_event}: {_event_error(detail)}"
            )
        elif event_type == "error":
            detail = event.get("message", event.get("error", event))
            parsed.fatal_errors.append(f"Pi error: {_event_error(detail)}")
        elif event_type == "auto_retry_end":
            if event.get("success") is True:
                parsed.errors.extend(
                    f"recovered after {error}" for error in pending_retry_errors
                )
                pending_retry_errors.clear()
            elif event.get("success") is False:
                detail = event.get("finalError") or "provider retry failed"
                parsed.fatal_errors.append(f"Pi retry failed: {_event_error(detail)}")
                pending_retry_errors.clear()

    if not parsed.agent_ended:
        parsed.fatal_errors.append("Pi JSONL stream ended without agent_end")
    # A length-limited tool call can be rejected and retried successfully in a later
    # turn. Only the final assistant stop is evidence that the run ended incomplete.
    parsed.stop_reason = last_assistant_stop_reason
    parsed.truncated = parsed.stop_reason == "length"
    parsed.fatal_errors.extend(pending_retry_errors)
    if not parsed.summary and not parsed.truncated and not allow_terminal_tool:
        parsed.fatal_errors.append("Pi completed without a final assistant message")
    return parsed


def _compact_artifact_stdout(stdout: str) -> tuple[str, dict[str, Any]]:
    """Drop cumulative Pi message snapshots while retaining complete operations."""

    kept: list[str] = []
    dropped = 0
    records = stdout.split("\n")
    for index, line in enumerate(records):
        terminator = "\n" if index < len(records) - 1 else ""
        if '"type":"message_update"' in line[:80]:
            dropped += 1
            continue
        kept.append(line + terminator)
    if not dropped:
        return stdout, {}
    compact = "".join(kept)
    return compact, {
        "raw_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "raw_stdout_chars": len(stdout),
        "stored_stdout_chars": len(compact),
        "dropped_cumulative_message_updates": dropped,
    }


def _positive_run_limit(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise PiExecutorError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PiExecutorError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise PiExecutorError(f"{name} must be a positive integer")
    return parsed


async def _settle_cleanup_task(
    task: "asyncio.Task[Any]",
) -> tuple[Any, Optional[BaseException], bool]:
    """Settle a cleanup task despite caller cancellation and report that cancellation."""
    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                caller_cancelled = True
                current.uncancel()
                continue
            # The cleanup task itself was cancelled.
            break
        except BaseException:
            break

    if task.cancelled():
        return None, asyncio.CancelledError(), caller_cancelled
    try:
        return task.result(), None, caller_cancelled
    except BaseException as exc:  # noqa: BLE001 - caller decides cleanup precedence
        return None, exc, caller_cancelled


async def _kill_and_wait(
    process: Any, communication_task: "asyncio.Task[tuple[bytes, bytes]]"
) -> tuple[bytes, bytes, Optional[str]]:
    """Kill Pi, drain its pipes, and explicitly reap it without masking the caller."""
    try:
        if process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass

    communication_result, error, cancelled_while_communicating = (
        await _settle_cleanup_task(communication_task)
    )
    if error is not None:
        stdout_bytes, stderr_bytes = b"", b""
        if isinstance(error, asyncio.CancelledError):
            communication_error = "Pi communication was cancelled during cleanup"
        else:
            communication_error = (
                f"Pi communication failed: {type(error).__name__}: {error}"
            )
    else:
        stdout_bytes, stderr_bytes = communication_result
        communication_error = None

    wait_task = asyncio.create_task(process.wait())
    _wait_result, wait_error, cancelled_while_waiting = await _settle_cleanup_task(
        wait_task
    )
    if wait_error is not None and not isinstance(wait_error, ProcessLookupError):
        detail = f"Pi process wait failed: {type(wait_error).__name__}: {wait_error}"
        communication_error = (
            f"{communication_error}; {detail}" if communication_error else detail
        )
    if cancelled_while_communicating or cancelled_while_waiting:
        raise asyncio.CancelledError
    return stdout_bytes, stderr_bytes, communication_error


async def _close_gateway(gateway: _VaultToolGateway) -> None:
    """Drain the gateway before propagating cancellation to the caller."""
    close_task = asyncio.create_task(gateway.aclose())
    _result, error, caller_cancelled = await _settle_cleanup_task(close_task)
    if caller_cancelled:
        raise asyncio.CancelledError
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            raise PiExecutorError("Pi vault gateway close was unexpectedly cancelled")
        raise error


async def _stream_pi_process(process, payload, on_event):
    """Drain both pipes while forwarding complete JSON events, never cutting a response."""

    async def stdout():
        chunks, pending = [], b""
        while chunk := await process.stdout.read(65536):
            chunks.append(chunk)
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue  # The authoritative full-stream parser reports malformed output.
                await on_event(event)
        if pending:
            try:
                event = json.loads(pending)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                await on_event(event)
        return b"".join(chunks)

    if payload:
        process.stdin.write(payload)
        await process.stdin.drain()
    process.stdin.close()
    output, errors = await asyncio.gather(stdout(), process.stderr.read())
    await process.wait()
    return output, errors


async def _invoke_pi(
    vault_root: Path,
    *,
    prompt: str,
    system_prompt: str,
    schemas: Sequence[Dict[str, Any]],
    config: _PiRuntimeConfig,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    max_tool_calls: int = MAX_PI_WRITE_TOOL_CALLS,
    required_notes: Sequence[str] = (),
    forbidden_folders: Sequence[str] = (),
    immutable_sections: Sequence[tuple[str, str]] = (),
    allow_new_categories: bool = True,
    user_id: str = "",
    load_vault_skill: bool = True,
    telemetry_attributes: Optional[Dict[str, Any]] = None,
    tool_handler: Any = None,
    terminate_on_verified: bool = False,
    max_identical_tool_calls: Optional[int] = None,
    images: Optional[List[Tuple[str, bytes]]] = None,
    session_file: Optional[Path] = None,
    on_event: Any = None,
    yield_signal: Optional[asyncio.Event] = None,
    recover_repeated_tool_errors: bool = False,
) -> tuple[_PiEventResult, _VaultToolGateway]:
    """Run Pi and preserve gateway audit state for every post-start failure."""

    privacy_owner = privacy.processing_owner(user_id or Path(vault_root).name)
    privacy_snapshot = await privacy.load_snapshot(privacy_owner)
    privacy_paths = await privacy.quarantined_vault_paths(privacy_owner)
    started_ns = time.time_ns()
    max_tool_rounds = _positive_run_limit(max_tool_rounds, name="Pi max_tool_rounds")
    max_tool_calls = _positive_run_limit(max_tool_calls, name="Pi max_tool_calls")
    loop = asyncio.get_running_loop()
    limit_signal = asyncio.Event()
    gateway = _VaultToolGateway(
        vault_root,
        schemas,
        max_tool_calls=max_tool_calls,
        max_identical_tool_calls=max_identical_tool_calls,
        on_limit=lambda: loop.call_soon_threadsafe(limit_signal.set),
        required_notes=required_notes,
        forbidden_folders=forbidden_folders,
        immutable_sections=immutable_sections,
        allow_new_categories=allow_new_categories,
        user_id=user_id,
        tool_handler=tool_handler,
        terminate_on_verified=terminate_on_verified,
        native_binary=config.binary,
    )
    canonical_tools = getattr(gateway.tools, "vault_tools", gateway.tools)
    if hasattr(canonical_tools, "privacy_excluded_paths"):
        canonical_tools.privacy_excluded_paths = privacy_paths

    await privacy.guard_payload(
        privacy_owner,
        {
            "episode_keys": list(
                getattr(canonical_tools, "allowed_source_episode_keys", ())
            )
        },
    )

    def check_privacy():
        asyncio.run_coroutine_threadsafe(
            privacy.assert_current(privacy_owner, privacy_snapshot), loop
        ).result(timeout=10)

    gateway.privacy_guard = check_privacy
    events: Optional[_PiEventResult] = None
    redaction_values = [
        *_pi_redaction_values(config),
        _pi_literal_config_value(config.api_key),
        gateway.token,
    ]

    with tempfile.TemporaryDirectory(prefix="chronicle-pi-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        gateway.__enter__()
        try:
            extension_path = temp_dir / "chronicle-runtime.mjs"
            extension_path.write_text(
                _extension_source(
                    schemas,
                    gateway_url=gateway.url,
                    token=gateway.token,
                    temperature=config.temperature,
                    sampling=config.sampling,
                    response_format=config.response_format,
                    disable_thinking=not config.reasoning_allowed,
                    seed=config.seed,
                    max_tool_rounds=max_tool_rounds,
                    max_tool_calls=max_tool_calls,
                    recover_repeated_tool_errors=recover_repeated_tool_errors,
                    result_repair_tools=getattr(
                        tool_handler, "result_repair_tools", None
                    ),
                    available_tools=getattr(tool_handler, "available_tools", None),
                ),
                encoding="utf-8",
            )
            extension_path.chmod(0o600)
            models_path = temp_dir / "models.json"
            models_path.write_text(
                json.dumps(_models_payload(config), indent=2),
                encoding="utf-8",
            )
            models_path.chmod(0o600)
            system_prompt_path = temp_dir / "system-prompt.md"
            effective_system_prompt = system_prompt
            if config.system_prompt_prefix:
                effective_system_prompt = (
                    f"{config.system_prompt_prefix}\n\n{system_prompt}"
                )
            system_prompt_path.write_text(effective_system_prompt, encoding="utf-8")
            system_prompt_path.chmod(0o600)

            command = [
                config.binary,
                "--mode",
                "json",
                "--provider",
                config.provider,
                "--model",
                config.model,
                "--thinking",
                config.thinking,
                "--system-prompt",
                str(system_prompt_path),
                "--offline",
                "--no-context-files",
                "--no-extensions",
                "--no-prompt-templates",
                "--no-themes",
                "--no-builtin-tools",
            ]
            command.extend(
                ["--session", str(session_file)] if session_file else ["--no-session"]
            )
            if schemas and load_vault_skill:
                # The vault's shape travels as a skill rather than more system prompt,
                # so it is generated from the same templates the vault is scaffolded
                # from and cannot drift from what verify_vault enforces.
                skill_path = write_skill(temp_dir)
                skill_path.chmod(0o600)
                command.extend(["--skill", str(skill_path)])
            else:
                # Final synthesis and non-vault agents have nothing to teach here.
                command.append("--no-skills")
            command.extend(["-e", str(extension_path)])
            if schemas:
                tool_names = ",".join(
                    str(schema["function"]["name"]) for schema in schemas
                )
                command.extend(["--tools", tool_names])

            stdin_payload: bytes | None = prompt.encode("utf-8")
            if images:
                prompt_path = temp_dir / "recording-prompt.md"
                prompt_path.write_text(prompt, encoding="utf-8")
                prompt_path.chmod(0o600)
                attachments = [f"@{prompt_path}"]
                for index, (filename, data) in enumerate(images):
                    suffix = Path(filename).suffix.lower()
                    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                        suffix = ".jpg"
                    image_path = temp_dir / f"screen-context-{index + 1}{suffix}"
                    image_path.write_bytes(data)
                    image_path.chmod(0o600)
                    attachments.append(f"@{image_path}")
                command.extend(
                    [
                        *attachments,
                        "Follow the attached recording prompt. Treat the attached "
                        "images as user-selected supporting evidence for that recording.",
                    ]
                )
                stdin_payload = None

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(vault_root),
                    env=_pi_subprocess_env(temp_dir),
                )
            except OSError as exc:
                raise PiExecutorError(f"Pi failed to start: {exc}") from exc

            communication_task = asyncio.create_task(
                _stream_pi_process(process, stdin_payload, on_event)
                if on_event is not None
                else process.communicate(stdin_payload)
            )
            limit_task = asyncio.create_task(limit_signal.wait())
            yield_task = (
                asyncio.create_task(yield_signal.wait())
                if yield_signal is not None
                else None
            )
            wait_tasks = {communication_task, limit_task}
            if yield_task is not None:
                wait_tasks.add(yield_task)
            failures: List[str] = []
            terminated_by_chronicle = False
            yielded_at_checkpoint = False
            try:
                done, _pending = await asyncio.wait(
                    wait_tasks,
                    timeout=config.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    terminated_by_chronicle = True
                    failures.append(f"Pi timed out after {config.timeout_seconds}s")
                    stdout_bytes, stderr_bytes, cleanup_error = await _kill_and_wait(
                        process, communication_task
                    )
                    if cleanup_error:
                        failures.append(cleanup_error)
                elif limit_task in done and gateway.limit_error:
                    terminated_by_chronicle = True
                    failures.append(gateway.limit_error)
                    stdout_bytes, stderr_bytes, cleanup_error = await _kill_and_wait(
                        process, communication_task
                    )
                    if cleanup_error:
                        failures.append(cleanup_error)
                elif yield_task in done and communication_task not in done:
                    terminated_by_chronicle = True
                    yielded_at_checkpoint = True
                    failures.append("Pi yielded at a complete native checkpoint")
                    stdout_bytes, stderr_bytes, cleanup_error = await _kill_and_wait(
                        process, communication_task
                    )
                    if cleanup_error:
                        failures.append(cleanup_error)
                else:
                    try:
                        stdout_bytes, stderr_bytes = await communication_task
                    except Exception as exc:  # noqa: BLE001 - return auditable failure
                        terminated_by_chronicle = True
                        failures.append(
                            f"Pi communication failed: {type(exc).__name__}: {exc}"
                        )
                        stdout_bytes, stderr_bytes, cleanup_error = (
                            await _kill_and_wait(process, communication_task)
                        )
                        if cleanup_error:
                            failures.append(cleanup_error)
            except asyncio.CancelledError:
                await _kill_and_wait(process, communication_task)
                raise
            finally:
                limit_task.cancel()
                if yield_task is not None:
                    yield_task.cancel()
                await asyncio.gather(
                    limit_task,
                    *([yield_task] if yield_task is not None else []),
                    return_exceptions=True,
                )

            try:
                stdout = stdout_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                failures.append(f"Pi emitted non-UTF-8 stdout: {exc}")
            try:
                stderr = stderr_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                failures.append(f"Pi emitted non-UTF-8 stderr: {exc}")

            redacted_stderr = _redact(
                stderr[-_STDERR_TAIL_CHARS:].strip(), *redaction_values
            )
            if not terminated_by_chronicle and process.returncode != 0:
                suffix = f": {redacted_stderr}" if redacted_stderr else ""
                failures.append(f"Pi exited with status {process.returncode}{suffix}")
            elif redacted_stderr:
                logger.warning("Pi stderr: %s", redacted_stderr)

            events = _parse_events(
                stdout, allow_terminal_tool=gateway.terminal_completion
            )
            if gateway.terminal_completion:
                events.terminated_after_tool = True
                if not events.summary:
                    events.summary = "Task completed."
            events.stdout = stdout
            events.stderr = stderr
            events.returncode = process.returncode
            events.terminated_by_chronicle = terminated_by_chronicle
            events.failure_kind = (
                gateway.limit_kind
                if gateway.limit_error
                else (
                    "time_slice"
                    if yielded_at_checkpoint or (terminated_by_chronicle and not done)
                    else (
                        "interrupted"
                        if terminated_by_chronicle
                        else "provider_failure" if process.returncode != 0 else None
                    )
                )
            )
            if gateway.limit_error and gateway.limit_error not in failures:
                failures.append(gateway.limit_error)
            events.fatal_errors.extend(failures)
        finally:
            await _close_gateway(gateway)

    if events is None:  # pragma: no cover - every non-exception path assigns events
        raise PiExecutorError("Pi finished without a result")
    # An authenticated /limit request may already be admitted when shutdown starts.
    # Gateway close drains it before freezing state, so reconcile the final value only
    # now; copying it before close would silently lose that hard-limit outcome.
    if gateway.limit_error and gateway.limit_error not in events.fatal_errors:
        events.fatal_errors.append(gateway.limit_error)
    events.summary = _redact(events.summary, *redaction_values)
    events.errors = [
        _redact(error, *redaction_values) for error in [*events.errors, *gateway.errors]
    ]
    events.fatal_errors = [
        _redact(error, *redaction_values) for error in events.fatal_errors
    ]
    events.errors.extend(events.fatal_errors)
    if events.fatal_errors or gateway.close_error:
        events.truncated = True
    elif events.truncated:
        events.errors.append("Pi response was truncated")
    await privacy.assert_current(privacy_owner, privacy_snapshot)
    visible_output = (
        {"completion": text_payload(events.summary)}
        if events.summary
        else {
            "assistant_content": text_payload(
                json.dumps(events.assistant_content, ensure_ascii=False, default=str)
            )
        }
    )
    record_llm_usage_span(
        "pi_model_run",
        provider=config.provider,
        model=config.model,
        usage=events.usage,
        start_time_ns=started_ns,
        end_time_ns=time.time_ns(),
        input={
            "system_prompt": text_payload(effective_system_prompt),
            "prompt": text_payload(prompt),
            "image_count": len(images or []),
        },
        output={
            **visible_output,
            "stop_reason": events.stop_reason,
            "truncated": events.truncated,
            "error_count": len(events.errors),
        },
        attributes={
            "chronicle.memory.executor": "pi",
            "chronicle.memory.attempt": current_memory_attempt(),
            "chronicle.memory.pi.usage_scope": "subprocess_aggregate",
            "chronicle.memory.pi.phase": "tool_agent" if schemas else "final_synthesis",
            "chronicle.memory.rounds": events.rounds,
            "chronicle.memory.tool_calls": max(events.tool_calls, gateway.call_count),
            "gen_ai.request.temperature": config.temperature,
            **({"gen_ai.request.seed": config.seed} if config.seed is not None else {}),
            "gen_ai.request.max_tokens": config.max_tokens,
            "chronicle.memory.pi.context_window": config.context_window,
            "chronicle.memory.pi.thinking": config.thinking,
            **(
                {"gen_ai.response.finish_reasons": [events.stop_reason]}
                if events.stop_reason
                else {}
            ),
            "chronicle.memory.truncated": events.truncated,
            "chronicle.memory.error_count": len(events.errors),
            **(telemetry_attributes or {}),
        },
    )
    return events, gateway


class PiMemoryAgent:
    """Run Chronicle's write agent through the isolated Pi CLI harness."""

    def __init__(
        self,
        vault_root: Path,
        operation: str = "memory_write",
        *,
        force_fallback: bool = False,
        terminate_on_verified: bool = False,
        max_tool_rounds: Optional[int] = None,
        max_identical_tool_calls: Optional[int] = None,
    ):
        self.root = Path(vault_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.operation = operation
        self.force_fallback = force_fallback
        self.terminate_on_verified = bool(terminate_on_verified)
        if max_tool_rounds is None:
            self.max_tool_rounds = MAX_TOOL_ROUNDS
            # Resolve this at construction time so deployments and tests can tune the
            # independent hard call ceiling without also changing the round ceiling.
            self.max_tool_calls = MAX_PI_WRITE_TOOL_CALLS
        else:
            self.max_tool_rounds = _positive_run_limit(
                max_tool_rounds, name="Pi max_tool_rounds"
            )
            self.max_tool_calls = self.max_tool_rounds * 4
        self.max_identical_tool_calls = (
            _positive_run_limit(
                max_identical_tool_calls,
                name="Pi max_identical_tool_calls",
            )
            if max_identical_tool_calls is not None
            else None
        )

    async def run(
        self,
        transcript: str,
        conversation_id: str,
        *,
        date: Optional[str] = None,
        duration_minutes: Optional[float] = None,
        title: Optional[str] = None,
        vault_summary: str = "",
        guidance: str = "",
        record: str = "conversation",
        source_permissions: WriteSourcePermissions | None = None,
        images: Optional[List[Tuple[str, bytes]]] = None,
    ) -> MemoryAgentResult:

        privacy_owner = privacy.processing_owner(self.root.name)
        await privacy.guard_payload(
            privacy_owner,
            {
                "conversation_id": conversation_id,
                "episode_keys": (
                    list(source_permissions.episode_keys) if source_permissions else []
                ),
            },
        )
        private_paths = await privacy.quarantined_vault_paths(privacy_owner)
        if private_paths:
            vault_summary = ""
        config = _resolve_pi_config(self.operation, force_fallback=self.force_fallback)
        date = date or datetime.now(timezone.utc).isoformat()
        system_prompt = await _get_prompt(*write_system_prompt(record), vault_summary)
        user_id = self.root.name
        operating_store = OperatingMemoryStore(user_id)
        if not private_paths:
            system_prompt = _apply_operating_guidance(system_prompt, operating_store)
        task = build_write_task(
            transcript,
            conversation_id,
            date=date,
            duration_minutes=duration_minutes,
            title=title,
            guidance=guidance,
            record=record,
        )
        with memory_span(
            "pi_memory_agent",
            attributes={
                "openinference.span.kind": "AGENT",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": config.provider,
                "gen_ai.request.model": config.model,
                "gen_ai.conversation.id": conversation_id,
                "session.id": conversation_id,
                "langfuse.session.id": conversation_id,
                "chronicle.memory.operation": self.operation,
                "chronicle.memory.executor": "pi",
                "chronicle.memory.attempt": current_memory_attempt(),
                "chronicle.memory.force_fallback": self.force_fallback,
                "chronicle.memory.transcript_chars": len(transcript),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": conversation_id,
                    "transcript": text_payload(transcript),
                    "title": text_payload(title),
                    "guidance": text_payload(guidance),
                },
            )
            vault_tools = VaultTools(
                self.root,
                trace_context=current_otel_context(),
                required_notes=required_notes(record, conversation_id),
                forbidden_folders=forbidden_folders(record),
                immutable_sections=immutable_sections(record),
                allow_new_categories=allow_new_categories(record),
                user_id=user_id,
            )
            permissions = write_source_permissions(
                record, transcript, source_permissions
            )
            vault_tools.allowed_source_episode_keys = set(permissions.episode_keys)
            vault_tools.source_claims = permissions.claim_sources
            vault_tools.require_source_episode_keys = record in {
                "day",
                "session",
            } and bool(permissions.episode_keys)
            schemas = list(VAULT_TOOL_SCHEMAS)
            tool_handler: Any = vault_tools
            if not private_paths and operating_store.has_active_skills():
                schemas.append(RECALL_OPERATING_MEMORY_SCHEMA)
                tool_handler = VaultWithOperatingMemoryTools(
                    vault_tools, operating_store
                )
            events, gateway = await _invoke_pi(
                self.root,
                prompt=task,
                system_prompt=system_prompt,
                schemas=schemas,
                config=config,
                max_tool_rounds=self.max_tool_rounds,
                max_tool_calls=self.max_tool_calls,
                max_identical_tool_calls=self.max_identical_tool_calls,
                required_notes=required_notes(record, conversation_id),
                forbidden_folders=forbidden_folders(record),
                immutable_sections=immutable_sections(record),
                allow_new_categories=allow_new_categories(record),
                user_id=user_id,
                tool_handler=tool_handler,
                terminate_on_verified=self.terminate_on_verified,
                images=images,
            )
            result = MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=max(events.rounds, 1),
                touched=sorted(gateway.tools.touched),
                summary=events.summary or "(Pi failed before completing)",
                tool_calls=max(events.tool_calls, gateway.call_count),
                removed=list(gateway.tools.removed),
                errors=events.errors,
                usage=events.usage,
                truncated=events.truncated,
                verified=gateway.tools.verified,
                source_evidence_keys_by_path={
                    path: sorted(keys)
                    for path, keys in vault_tools.source_evidence_keys_by_path.items()
                },
                source_episode_keys_by_path={
                    path: sorted(keys)
                    for path, keys in vault_tools.source_episode_keys_by_path.items()
                },
            )
            try:
                artifact_stdout, stdout_compaction = (
                    (events.stdout, {})
                    if record == "session"
                    else _compact_artifact_stdout(events.stdout)
                )
                request_hash, artifact_hash = await asyncio.to_thread(
                    persist_inference_run,
                    operation="pi_memory",
                    request={
                        "system_prompt": system_prompt,
                        "prompt": task,
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "record": record,
                        "source_permissions": {
                            "episode_keys": list(permissions.episode_keys),
                            "claim_sources": permissions.claim_sources,
                        },
                        "vault_before_sha256": canonical_hash(gateway.tools.baseline()),
                        "operation": self.operation,
                        "model": config.model,
                        "provider": config.provider,
                        "thinking": config.thinking,
                        "temperature": config.temperature,
                        "sampling": config.sampling,
                        "seed": config.seed,
                        "context_window": config.context_window,
                        "max_tokens": config.max_tokens,
                        "max_tool_rounds": self.max_tool_rounds,
                        "max_tool_calls": self.max_tool_calls,
                        "max_identical_tool_calls": self.max_identical_tool_calls,
                    },
                    stdout=artifact_stdout,
                    stderr=events.stderr,
                    result={
                        "summary": result.summary,
                        "touched": result.touched,
                        "removed": result.removed,
                        "errors": result.errors,
                        "usage": result.usage,
                        "rounds": result.rounds,
                        "tool_calls": result.tool_calls,
                        "truncated": result.truncated,
                        "verified": result.verified,
                    },
                    metadata={
                        "returncode": events.returncode,
                        "terminated_by_chronicle": events.terminated_by_chronicle,
                        **(
                            {"terminated_after_tool": True}
                            if events.terminated_after_tool
                            else {}
                        ),
                        **stdout_compaction,
                    },
                    reusable=False,
                )
                result.inference_artifacts.append(
                    {
                        "operation": "pi_memory",
                        "request_hash": request_hash,
                        "artifact_hash": artifact_hash,
                    }
                )
            except Exception:
                # The inference has already run and may have mutated the vault. An
                # unavailable audit volume must not trigger a second attempt.
                logger.exception("Failed to persist Pi inference artifact")
                if record == "session":
                    raise
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": not result.truncated,
                    "chronicle.memory.rounds": result.rounds,
                    "chronicle.memory.tool_calls": result.tool_calls,
                    "chronicle.memory.touched_count": len(result.touched),
                    "chronicle.memory.removed_count": len(result.removed),
                    "chronicle.memory.error_count": len(result.errors),
                    "chronicle.memory.truncated": result.truncated,
                    **{
                        f"chronicle.memory.usage.{key}": value
                        for key, value in result.usage.items()
                    },
                },
            )
            set_observation_io(
                span,
                output={
                    "summary": text_payload(result.summary),
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "touched_count": len(result.touched),
                    "removed_count": len(result.removed),
                    "error_count": len(result.errors),
                    "truncated": result.truncated,
                },
            )
            return result


def _pi_final_search_prompt(query: str, read_notes: Dict[str, str]) -> str:
    """Build a no-tools synthesis request from evidence Pi already chose to read."""
    return _search_final_synthesis_prompt(query, read_notes)


async def _search_vault_with_pi_impl(
    query: str,
    vault_root: Path,
    *,
    operation: str = "memory_search",
    max_rounds: int = MAX_SEARCH_ROUNDS,
    vault_summary: str = "",
    user_id: str = "",
    notes_only: bool = False,
) -> VaultSearchResult:
    """Run Chronicle's read-only retrieval agent through Pi."""
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    max_rounds = _positive_run_limit(max_rounds, name="Pi search max_rounds")
    config = _resolve_pi_config(operation)

    resolved_user_id = user_id or root.name
    private_paths = await privacy.quarantined_vault_paths(
        privacy.processing_owner(resolved_user_id)
    )
    if private_paths:
        vault_summary = ""
    system_prompt = await _get_prompt(
        "memory.search_system", SEARCH_SYSTEM_PROMPT, vault_summary
    )
    operating_store = OperatingMemoryStore(resolved_user_id)
    if not private_paths:
        system_prompt = _apply_operating_guidance(system_prompt, operating_store)
    prompt = (
        f"{query}\n\n"
        f"Use at most {max_rounds} tool rounds. Return the answer when you have enough evidence."
    )
    events, gateway = await _invoke_pi(
        root,
        prompt=prompt,
        system_prompt=system_prompt,
        schemas=(
            [
                schema
                for schema in VAULT_SEARCH_TOOL_SCHEMAS
                if schema["function"]["name"]
                in {"grep", "glob", "read_note", "read_slice"}
            ]
            if notes_only
            else VAULT_SEARCH_TOOL_SCHEMAS
        ),
        config=config,
        max_tool_rounds=max_rounds,
        max_tool_calls=max_rounds * _PI_TOOL_CALLS_PER_SEARCH_ROUND,
        user_id=resolved_user_id,
    )
    answer = PI_SEARCH_FAILURE_ANSWER if events.truncated else events.summary
    rounds = events.rounds
    usage = dict(events.usage)
    errors = list(events.errors)
    warnings: List[str] = []
    final_synthesis_used = False
    final_events: Optional[_PiEventResult] = None

    # Pi's extension must abort an attempted tool turn beyond the hard cap so a
    # poorly behaved model cannot keep reading indefinitely. If it had already read
    # evidence, retain that safety property while giving Pi one fresh, no-tools
    # completion to synthesize an answer or abstain. This remains the Pi backend: the
    # same isolated Pi CLI/model configuration performs both phases, and the second
    # process is given neither Chronicle's extension nor any built-in Pi tools.
    if gateway.limit_error and gateway.read_notes:
        final_synthesis_used = True
        try:
            final_events, _final_gateway = await _invoke_pi(
                root,
                prompt=_pi_final_search_prompt(query, gateway.read_notes),
                system_prompt=(
                    f"{system_prompt}\n\n{SEARCH_FINAL_SYNTHESIS_SYSTEM_SUFFIX}"
                ),
                schemas=(),
                config=config,
                max_tool_rounds=1,
                max_tool_calls=1,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the audited first phase
            detail = _redact(
                f"{type(exc).__name__}: {exc}",
                *_pi_redaction_values(config),
            )
            errors.append(f"Pi final search synthesis failed: {detail}")
            logger.error("Pi final search synthesis failed: %s", detail)
        else:
            rounds += final_events.rounds
            for key, value in final_events.usage.items():
                usage[key] = usage.get(key, 0) + value
            errors.extend(final_events.errors)
            if not final_events.truncated and final_events.summary:
                answer = final_events.summary
                # A hard cap deliberately kills Pi before `agent_end`; those two
                # first-phase events describe why final synthesis was needed, not a
                # failed answer. Preserve them as warnings so callers and benchmark
                # manifests retain the audit without treating successful recovery as
                # an error.
                recovered_events = {
                    gateway.limit_error,
                    "Pi JSONL stream ended without agent_end",
                }
                retained_errors = []
                for error in errors:
                    if error in recovered_events:
                        warnings.append(error)
                    else:
                        retained_errors.append(error)
                errors = retained_errors

    result = VaultSearchResult(
        answer=answer,
        notes=[
            {"path": path, "content": content}
            for path, content in gateway.read_notes.items()
        ],
        rounds=max(rounds, 1),
        usage=usage,
        errors=errors,
        warnings=warnings,
        tool_calls=gateway.call_count,
        final_synthesis_used=final_synthesis_used,
        truncated=answer.strip() == PI_SEARCH_FAILURE_ANSWER,
    )
    try:
        combined_stdout = events.stdout
        combined_stderr = events.stderr
        if final_events is not None:
            combined_stdout = "\n".join(
                part for part in (combined_stdout, final_events.stdout) if part
            )
            combined_stderr = "\n".join(
                part for part in (combined_stderr, final_events.stderr) if part
            )
        artifact_stdout, stdout_compaction = _compact_artifact_stdout(combined_stdout)
        await asyncio.to_thread(
            persist_inference_run,
            operation="pi_memory_search",
            request={
                "system_prompt": system_prompt,
                "prompt": prompt,
                "query": query,
                "user_id": resolved_user_id,
                "operation": operation,
                "record": "search",
                "model": config.model,
                "provider": config.provider,
                "thinking": config.thinking,
                "temperature": config.temperature,
                "sampling": config.sampling,
                "context_window": config.context_window,
                "max_tokens": config.max_tokens,
                "max_tool_rounds": max_rounds,
                "max_tool_calls": max_rounds * _PI_TOOL_CALLS_PER_SEARCH_ROUND,
            },
            stdout=artifact_stdout,
            stderr=combined_stderr,
            result={
                "summary": result.answer,
                "read_paths": [note["path"] for note in result.notes],
                "touched": [],
                "removed": [],
                "errors": result.errors,
                "warnings": result.warnings,
                "usage": result.usage,
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "truncated": result.truncated,
                "verified": None,
            },
            metadata={
                "returncode": events.returncode,
                "final_synthesis_used": result.final_synthesis_used,
                **stdout_compaction,
            },
            reusable=False,
        )
    except Exception:
        # Retrieval already completed. Artifact storage must not turn a valid answer
        # into a user-visible failure or cause the model call to be repeated.
        logger.exception("Failed to persist Pi search inference artifact")
    return result


async def search_vault_with_pi(
    query: str,
    vault_root: Path,
    *,
    operation: str = "memory_search",
    max_rounds: int = MAX_SEARCH_ROUNDS,
    vault_summary: str = "",
    user_id: str = "",
    notes_only: bool = False,
) -> VaultSearchResult:
    """Trace one complete Pi retrieval, including optional cap synthesis."""

    with memory_span(
        "pi_memory_search_agent",
        attributes={
            "openinference.span.kind": "AGENT",
            "gen_ai.operation.name": "invoke_agent",
            "chronicle.memory.operation": operation,
            "chronicle.memory.executor": "pi",
            "chronicle.memory.attempt": current_memory_attempt(),
            "chronicle.memory.query_chars": len(query),
            "chronicle.memory.max_rounds": max_rounds,
        },
    ) as span:
        set_observation_io(
            span,
            input={
                "query": text_payload(query),
                "vault_summary": text_payload(vault_summary),
                "max_rounds": max_rounds,
            },
        )
        result = await _search_vault_with_pi_impl(
            query,
            vault_root,
            operation=operation,
            max_rounds=max_rounds,
            vault_summary=vault_summary,
            user_id=user_id,
            **({"notes_only": True} if notes_only else {}),
        )
        set_safe_span_attributes(
            span,
            {
                "chronicle.memory.success": not result.truncated,
                "chronicle.memory.rounds": result.rounds,
                "chronicle.memory.tool_calls": result.tool_calls,
                "chronicle.memory.notes_read_count": len(result.notes),
                "chronicle.memory.error_count": len(result.errors),
                "chronicle.memory.warning_count": len(result.warnings),
                "chronicle.memory.final_synthesis_used": result.final_synthesis_used,
                "chronicle.memory.truncated": result.truncated,
                **{
                    f"chronicle.memory.usage.{key}": value
                    for key, value in result.usage.items()
                },
            },
        )
        set_observation_io(
            span,
            output={
                "answer": text_payload(result.answer),
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "notes_read_count": len(result.notes),
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
                "final_synthesis_used": result.final_synthesis_used,
                "truncated": result.truncated,
            },
        )
        return result
