"""Centralized OpenAI client factory.

Single source of truth for creating OpenAI/AsyncOpenAI clients. All other
modules that need an OpenAI client should use this factory instead of
creating clients directly.

Clients are cached by (api_key, base_url, is_async) to avoid repeated
SSL context creation (~400ms per instantiation).

Tracing is handled by the OTEL instrumentor (see observability/otel_setup.py),
which auto-instruments all OpenAI calls at startup. No per-client wrapping needed.
"""

import logging
import os

import openai

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, str, bool], openai.OpenAI | openai.AsyncOpenAI] = {}


def is_reasoning_model(model: str | None) -> bool:
    """Return True for OpenAI reasoning-class models (o1/o3/o4/gpt-5 family).

    These models have stricter API surface than standard chat models — they
    reject non-default `temperature` and require `max_completion_tokens`
    instead of `max_tokens`. Callers should adapt API params accordingly.
    """
    if not model:
        return False
    # OpenAI models routed through an OpenAI-compatible aggregator use qualified
    # IDs such as ``openai/gpt-5.6-luna``. Parameter compatibility is determined
    # by the provider model name, not by that routing namespace.
    m = model.lower().rsplit("/", 1)[-1]
    return m.startswith(("o1", "o3", "o4")) or m.startswith("gpt-5")


def model_supports_temperature(model: str | None) -> bool:
    """Return False for models that reject non-default temperature."""
    return not is_reasoning_model(model)


def create_openai_client(api_key: str, base_url: str, is_async: bool = False):
    """Get or create a cached OpenAI client.

    Clients are cached by (api_key, base_url, is_async). If the API key or
    base URL changes (e.g. config reload), a new client is created automatically.

    Args:
        api_key: OpenAI API key
        base_url: OpenAI API base URL
        is_async: Whether to return AsyncOpenAI or sync OpenAI client

    Returns:
        OpenAI or AsyncOpenAI client instance
    """
    cache_key = (api_key, base_url, is_async)
    client = _client_cache.get(cache_key)
    if client is not None:
        return client

    gateway = os.getenv("SERVICE_GATEWAY_URL", "").rstrip("/")
    options = (
        {"max_retries": 0}
        if gateway and base_url.startswith(gateway + "/deployments/")
        else {}
    )
    if is_async:
        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, **options)
    else:
        client = openai.OpenAI(api_key=api_key, base_url=base_url, **options)

    _client_cache[cache_key] = client
    logger.info(
        f"Created {'async' if is_async else 'sync'} OpenAI client for {base_url}"
    )
    return client
