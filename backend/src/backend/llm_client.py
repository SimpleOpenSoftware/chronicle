"""
Abstract LLM client interface for unified LLM operations across different providers.

This module provides a standardized interface for LLM operations that works with
OpenAI, Ollama, and other OpenAI-compatible APIs.
"""

import asyncio
import copy
import logging
from abc import ABC, abstractmethod
from contextlib import aclosing
from typing import Any, Dict, Optional

import anyio
import openai

from backend.model_registry import get_models_registry
from backend.openai_factory import create_openai_client, model_supports_temperature
from backend.services.chat_runs import current_run, run_step
from backend.services.memory.config import load_config_yml as _load_root_config
from backend.services.memory.config import resolve_value as _resolve_value

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, model: str | None = None, temperature: float = 0.1):
        self.model = model
        self.temperature = temperature
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def generate(
        self, prompt: str, model: str | None = None, temperature: float | None = None
    ) -> str:
        """Generate text completion from prompt."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict:
        """Check if the LLM service is available and healthy."""
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this client."""
        pass


class OpenAILLMClient(LLMClient):
    """OpenAI-compatible LLM client that works with OpenAI, Ollama, and other compatible APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
    ):
        super().__init__(model, temperature)
        # Do not read from environment here; values are provided by config.yml
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        if not self.api_key or not self.base_url or not self.model:
            raise ValueError(
                f"LLM configuration incomplete: api_key={'set' if self.api_key else 'MISSING'}, base_url={'set' if self.base_url else 'MISSING'}, model={'set' if self.model else 'MISSING'}"
            )

        # Initialize OpenAI client with optional Langfuse tracing
        try:
            self.client = create_openai_client(
                api_key=self.api_key, base_url=self.base_url, is_async=False
            )
            self.logger.info(f"OpenAI client initialized, base_url: {self.base_url}")
        except ImportError:
            self.logger.error(
                "OpenAI library not installed. Install with: pip install openai"
            )
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        """Generate text completion using OpenAI-compatible API."""
        try:
            model_name = model or self.model
            temp = temperature if temperature is not None else self.temperature

            params: dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
            }
            if model_supports_temperature(model_name):
                params["temperature"] = temp
            response = self.client.chat.completions.create(**params)
            return response.choices[0].message.content.strip()
        except Exception as e:
            self.logger.error(f"Error generating completion: {e}")
            raise

    def chat_with_tools(
        self,
        messages: list,
        tools: list | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ):
        """Chat completion with tool/function calling support. Returns raw response object."""
        model_name = model or self.model
        params: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
        }
        if model_supports_temperature(model_name):
            params["temperature"] = (
                temperature if temperature is not None else self.temperature
            )
        if tools:
            params["tools"] = tools
        return self.client.chat.completions.create(**params)

    async def health_check(self) -> Dict:
        """Check OpenAI-compatible service health by calling models.list()."""
        if not self.api_key or not self.base_url or not self.model:
            return {
                "status": "⚠️ Configuration incomplete",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }

        try:
            async_client = create_openai_client(
                api_key=self.api_key, base_url=self.base_url, is_async=True
            )
            await asyncio.wait_for(async_client.models.list(), timeout=5.0)
            return {
                "status": "✅ Connected",
                "healthy": True,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except openai.AuthenticationError:
            return {
                "status": "❌ Auth Failed — check API key",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except asyncio.TimeoutError:
            return {
                "status": "❌ Connection Timeout",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except openai.APIConnectionError:
            return {
                "status": "❌ Connection Failed — service unreachable",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return {
                "status": f"❌ Error: {e}",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }

    def get_default_model(self) -> str:
        """Get the default model for this client."""
        return self.model or "gpt-4o-mini"


class LLMClientFactory:
    """Factory for creating LLM clients based on configuration registry."""

    @staticmethod
    def create_client() -> LLMClient:
        """Create an LLM client based on model registry configuration (config.yml)."""
        registry = get_models_registry()

        if registry:
            llm_def = registry.get_default("llm")
            if llm_def:
                logger.info(
                    f"Creating LLM client from registry: {llm_def.name} ({llm_def.model_provider})"
                )
                params = llm_def.model_params or {}
                return OpenAILLMClient(
                    api_key=llm_def.api_key,
                    base_url=llm_def.resolved_url(),
                    model=llm_def.model_name,
                    temperature=params.get("temperature", 0.1),
                )

        raise ValueError("No default LLM defined in config.yml")

    @staticmethod
    def get_supported_providers() -> list:
        """Get list of supported LLM providers."""
        return ["openai", "ollama"]


# Global LLM client instance
_llm_client = None


def get_llm_client() -> LLMClient:
    """Get the global LLM client instance (singleton pattern)."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClientFactory.create_client()
    return _llm_client


def reset_llm_client():
    """Reset the global LLM client instance (useful for testing)."""
    global _llm_client
    _llm_client = None


# Transport-level failures that warrant one retry against defaults.fallback_llm.
# APITimeoutError subclasses APIConnectionError; InternalServerError covers 5xx.
# Auth/4xx errors are config problems the fallback would not fix.
_FALLBACK_EXCEPTIONS = (
    openai.APIConnectionError,
    openai.InternalServerError,
    asyncio.TimeoutError,
)


def _is_context_length_error(exc: Exception) -> bool:
    """Return whether a provider 400 specifically reports context overflow."""
    if not isinstance(exc, openai.BadRequestError):
        return False

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"code", "type", "message"}:
                    values.append(str(item).lower())
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(getattr(exc, "body", None))
    details = " ".join(values)
    return any(
        marker in details
        for marker in (
            "exceed_context_size_error",
            "context_length_exceeded",
            "exceeds the available context size",
            "maximum context length",
        )
    )


def _get_fallback_model_def():
    """ModelDef named by defaults.fallback_llm, or None when unset/invalid or
    identical to defaults.llm (retrying the same model is pointless)."""
    registry = get_models_registry()
    if not registry:
        return None
    fb_name = registry.defaults.get("fallback_llm")
    if not fb_name or fb_name == registry.defaults.get("llm"):
        return None
    fb = registry.get_by_name(fb_name)
    if not fb or fb.model_type != "llm":
        return None
    return fb


def _create_fallback_client() -> Optional[OpenAILLMClient]:
    """Build a one-off client for the fallback LLM (singleton-path retries)."""
    fb = _get_fallback_model_def()
    if not fb:
        return None
    try:
        params = fb.model_params or {}
        return OpenAILLMClient(
            api_key=fb.api_key,
            base_url=fb.resolved_url(),
            model=fb.model_name,
            temperature=params.get("temperature", 0.1),
        )
    except Exception as e:  # noqa: BLE001 - fallback must never mask the original error
        logger.error(f"Failed to create fallback LLM client: {e}")
        return None


async def _generate_with_op(
    op,
    prompt: str,
    model: str | None,
    temperature: float | None,
    operation: str,
    request_trace: list | None = None,
) -> str:
    """One generation attempt against a ResolvedLLMOperation."""
    client = op.get_client(is_async=True)
    api_params = op.to_api_params()
    if temperature is not None:
        api_params["temperature"] = temperature
    if model is not None:
        api_params["model"] = model
    if not model_supports_temperature(api_params.get("model")):
        api_params.pop("temperature", None)
    api_params["messages"] = op.prepare_messages([{"role": "user", "content": prompt}])
    exchange = {"request": api_params}
    if request_trace is not None:
        request_trace.append(exchange)
    response = await client.chat.completions.create(**api_params)
    if request_trace is not None:
        exchange["response"] = response.model_dump(mode="json")
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        # Reasoning models can consume the whole completion budget on
        # reasoning and return empty content with finish_reason=length.
        raise RuntimeError(
            f"LLM returned empty content for operation {operation!r} "
            f"(model={api_params.get('model')}, "
            f"finish_reason={choice.finish_reason}) — if 'length', "
            f"raise the operation's max_tokens"
        )
    return content


# Async wrapper for blocking LLM operations
async def async_generate(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
    request_trace: list | None = None,
) -> str:
    """Async wrapper for LLM text generation.

    When ``operation`` is provided, parameters are resolved from the
    ``llm_operations`` config section via ``get_llm_operation()``.
    The resolved config determines model, temperature, max_tokens, etc.
    Explicit ``model``/``temperature`` kwargs still override the resolved values.

    If the primary model is unreachable (connection failure, timeout, 5xx) and
    ``defaults.fallback_llm`` names a different model, the call is retried once
    against the fallback.

    Tracing is handled automatically by the OTEL instrumentor; use
    ``set_otel_session()`` at job boundaries to group calls by session.
    """
    operation = operation or "default"
    if operation:
        registry = get_models_registry()
        if registry:
            op = registry.get_llm_operation(operation)
            try:
                return await _generate_with_op(
                    op, prompt, model, temperature, operation, request_trace
                )
            except _FALLBACK_EXCEPTIONS as e:
                fb_op = registry.get_fallback_llm_operation(operation, primary=op)
                if fb_op is None:
                    raise
                logger.warning(
                    f"Primary LLM {op.model_name!r} failed for operation "
                    f"{operation!r} ({e}); retrying with fallback LLM "
                    f"{fb_op.model_name!r}"
                )
                # No explicit model override on the retry — it would repoint
                # the fallback endpoint back at the (dead) primary model.
                return await _generate_with_op(
                    fb_op, prompt, None, temperature, operation, request_trace
                )

    # Fallback: use singleton client
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: client.generate(prompt, model, temperature)
        )
    except _FALLBACK_EXCEPTIONS as e:
        fb_client = _create_fallback_client()
        if fb_client is None:
            raise
        logger.warning(
            f"Primary LLM failed ({e}); retrying with fallback LLM "
            f"{fb_client.model!r}"
        )
        return await loop.run_in_executor(
            None, lambda: fb_client.generate(prompt, None, temperature)
        )


async def async_chat_with_tools(
    messages: list,
    tools: list | None = None,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
    force_fallback: bool = False,
    timeout_seconds: float | None = None,
    request_trace: list | None = None,
):
    """Async wrapper for chat completion with tool calling.

    When ``operation`` is provided, parameters are resolved from config.
    Unreachable-primary and context-overflow calls retry once against
    ``defaults.fallback_llm``. The model registry owns which operations use routing
    roles such as ``fast_llm``.
    ``timeout_seconds`` bounds each primary/fallback attempt independently.
    ``force_fallback`` is used for a semantic retry after a provider returned a
    syntactically valid but incomplete result.
    Tracing is handled automatically by the OTEL instrumentor.
    """

    async def _chat_once(op, model_override):
        client = op.get_client(is_async=True)
        api_params = op.to_api_params()
        if temperature is not None:
            api_params["temperature"] = temperature
        if model_override is not None:
            api_params["model"] = model_override
        api_params["messages"] = op.prepare_messages(messages)
        if tools:
            api_params["tools"] = tools

        exchange = (
            {"request": copy.deepcopy(api_params)} if request_trace is not None else {}
        )
        if request_trace is not None:
            request_trace.append(exchange)
        async with run_step(
            "model",
            operation or "chat",
            {
                "provider": op.model_def.model_provider if current_run() else None,
                "parameters": api_params,
            },
        ) as attempt:
            request = client.chat.completions.create(**api_params)
            if timeout_seconds is None:
                response = await request
            else:
                if timeout_seconds <= 0:
                    raise ValueError("timeout_seconds must be positive")
                response = await asyncio.wait_for(request, timeout=timeout_seconds)
            if request_trace is not None:
                exchange["response"] = response.model_dump(mode="json")
            if current_run():
                attempt.output = response.model_dump(mode="json")
        return response

    if operation:
        registry = get_models_registry()
        if registry:
            op = registry.get_llm_operation(operation)
            if force_fallback:
                fb_op = registry.get_fallback_llm_operation(
                    operation,
                    primary=op,
                )
                if fb_op is None:
                    raise RuntimeError(
                        f"No fallback LLM is configured for operation {operation!r}"
                    )
                logger.warning(
                    "Using fallback LLM %r for semantic retry of operation %r",
                    fb_op.model_name,
                    operation,
                )
                return await _chat_once(fb_op, None)
            try:
                return await _chat_once(op, model)
            except Exception as e:
                if not isinstance(
                    e, _FALLBACK_EXCEPTIONS
                ) and not _is_context_length_error(e):
                    raise
                fb_op = registry.get_fallback_llm_operation(
                    operation,
                    primary=op,
                )
                if fb_op is None:
                    raise
                logger.warning(
                    f"Primary LLM {op.model_name!r} failed for operation "
                    f"{operation!r} ({e}); retrying with fallback LLM "
                    f"{fb_op.model_name!r}"
                )
                return await _chat_once(fb_op, None)

    # Fallback: use singleton client
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: client.chat_with_tools(messages, tools, model, temperature)
        )
    except _FALLBACK_EXCEPTIONS as e:
        fb_client = _create_fallback_client()
        if fb_client is None:
            raise
        logger.warning(
            f"Primary LLM failed ({e}); retrying with fallback LLM "
            f"{fb_client.model!r}"
        )
        return await loop.run_in_executor(
            None, lambda: fb_client.chat_with_tools(messages, tools, None, temperature)
        )


def _accumulate_tool_call_delta(acc: Dict[int, Dict], delta_tool_calls) -> None:
    """Fold one streamed ``delta.tool_calls`` fragment into the accumulator.

    Providers split a single tool call across many chunks: the id and function
    name usually arrive once, while ``arguments`` streams in as JSON fragments
    that are only parseable once concatenated.
    """
    for tc in delta_tool_calls or []:
        entry = acc.setdefault(
            tc.index,
            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tc.id:
            entry["id"] = tc.id
        if tc.function:
            if tc.function.name:
                entry["function"]["name"] += tc.function.name
            if tc.function.arguments:
                entry["function"]["arguments"] += tc.function.arguments


async def async_chat_with_tools_stream(
    messages: list,
    tools: list | None = None,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
    *,
    allow_fallback: bool = True,
):
    """Streaming counterpart of :func:`async_chat_with_tools`.

    Yields ``{"type": "content", "text": <delta>}`` as text arrives, then exactly
    one terminal ``{"type": "done", "content", "tool_calls", "finish_reason"}``.
    Callers that need the assembled result read the terminal event; callers that
    only want to show progress read the content deltas.

    Both branches of a tool-calling round stream through here, because the caller
    cannot know in advance whether a round will produce a tool call or prose.

    Fallback differs from the non-streaming path on purpose: once a delta has been
    handed to the caller it has usually reached the user, so retrying against the
    fallback model would duplicate or contradict text already on screen. We
    therefore only fall back when the primary failed before emitting anything.
    """

    async def _attempt(op, model_override):
        client = op.get_client(is_async=True)
        api_params = op.to_api_params()
        if temperature is not None:
            api_params["temperature"] = temperature
        if model_override is not None:
            api_params["model"] = model_override
        api_params["messages"] = op.prepare_messages(messages)
        if tools:
            api_params["tools"] = tools
        api_params["stream"] = True
        api_params["stream_options"] = {"include_usage": True}
        async with run_step(
            "model",
            operation,
            {
                "provider": op.model_def.model_provider if current_run() else None,
                "parameters": api_params,
            },
        ) as attempt:
            content_parts = []
            reasoning_parts = []
            tool_call_acc = {}
            finish_reason = None
            usage = None
            stream = await client.chat.completions.create(**api_params)
            try:
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage.model_dump(mode="json")
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    if delta.content:
                        content_parts.append(delta.content)
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if getattr(delta, "tool_calls", None):
                        _accumulate_tool_call_delta(tool_call_acc, delta.tool_calls)
                    attempt.output = {
                        "content": "".join(content_parts),
                        "reasoning_content": "".join(reasoning_parts),
                        "tool_calls": [tool_call_acc[i] for i in sorted(tool_call_acc)],
                        "finish_reason": finish_reason,
                        "usage": usage,
                    }
                    if delta.content:
                        yield {"type": "content", "text": delta.content}
                attempt.output = {
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                    "tool_calls": [tool_call_acc[i] for i in sorted(tool_call_acc)],
                    "finish_reason": finish_reason,
                    "usage": usage,
                }
                if finish_reason not in ("stop", "tool_calls"):
                    attempt.status = "incomplete"
            finally:
                with anyio.move_on_after(5, shield=True):
                    await stream.close()
        yield {"type": "done", **attempt.output}

    if not operation:
        raise ValueError("async_chat_with_tools_stream requires an operation name")

    registry = get_models_registry()
    if not registry:
        raise RuntimeError("No models registry configured; cannot stream chat")

    op = registry.get_llm_operation(operation)
    emitted = False
    try:
        async with aclosing(_attempt(op, model)) as events:
            async for event in events:
                emitted = emitted or event["type"] == "content"
                yield event
        return
    except Exception as e:
        if emitted or not allow_fallback:
            raise
        if not isinstance(e, _FALLBACK_EXCEPTIONS) and not _is_context_length_error(e):
            raise
        fb_op = registry.get_fallback_llm_operation(operation, primary=op)
        if fb_op is None:
            raise
        logger.warning(
            f"Primary LLM {op.model_name!r} failed for streaming operation "
            f"{operation!r} ({e}); retrying with fallback LLM {fb_op.model_name!r}"
        )

    async with aclosing(_attempt(fb_op, None)) as events:
        async for event in events:
            yield event


async def async_health_check() -> Dict:
    """Async LLM health check."""
    client = get_llm_client()
    return await client.health_check()


async def _async_health_check_named_default(
    defaults_key: str, label: str
) -> Optional[Dict]:
    """Health check for a SEPARATE LLM named by ``defaults[defaults_key]``.

    Returns None when the key is unset or points at the same model as
    ``defaults.llm`` (that model is already covered by :func:`async_health_check`).
    """
    registry = get_models_registry()
    if not registry:
        return None
    name = registry.defaults.get(defaults_key)
    if not name or name == registry.defaults.get("llm"):
        return None
    model_def = registry.get_by_name(name)
    if not model_def:
        return None

    url = model_def.resolved_url()
    result = {"base_url": url, "default_model": model_def.model_name}
    if not model_def.api_key or not url or not model_def.model_name:
        return {**result, "status": "⚠️ Configuration incomplete", "healthy": False}
    for attempt in range(2):
        try:
            client = create_openai_client(
                api_key=model_def.api_key, base_url=url, is_async=True
            )
            await asyncio.wait_for(client.models.list(), timeout=5.0)
            return {**result, "status": "✅ Connected", "healthy": True}
        except openai.AuthenticationError:
            return {
                **result,
                "status": "❌ Auth Failed — check API key",
                "healthy": False,
            }
        except asyncio.TimeoutError:
            if attempt == 0:
                logger.info("%s health check timed out; retrying once", label)
                continue
            return {**result, "status": "❌ Connection Timeout", "healthy": False}
        except openai.APIConnectionError:
            return {
                **result,
                "status": "❌ Connection Failed — service unreachable",
                "healthy": False,
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"{label} health check failed: {e}")
            return {**result, "status": f"❌ Error: {e}", "healthy": False}

    raise RuntimeError("unreachable named LLM health-check state")


async def async_health_check_fast() -> Optional[Dict]:
    """Health check for a SEPARATE fast LLM, or None if none is configured."""
    return await _async_health_check_named_default("fast_llm", "Fast LLM")


async def async_health_check_fallback() -> Optional[Dict]:
    """Health check for a SEPARATE fallback LLM, or None if none is configured."""
    return await _async_health_check_named_default("fallback_llm", "Fallback LLM")
