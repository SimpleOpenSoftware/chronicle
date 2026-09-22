"""Model registry and config loader.

Loads a single source of truth from config.yml and exposes model
definitions (LLM, embeddings, etc.) in a provider-agnostic way.

Now using Pydantic for robust validation and type safety.
Environment variable resolution is handled by OmegaConf in the config module.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

import backend.service_deployment as service_deployment

# Import config merging for defaults.yml + config.yml integration
# OmegaConf handles environment variable resolution (${VAR:-default} syntax)
from backend.config import get_config, get_config_yml_path
from backend.openai_factory import create_openai_client, is_reasoning_model

logger = logging.getLogger(__name__)

# Operations whose normal routing role is not the primary LLM. Keeping this policy
# beside the resolver makes the effective route inspectable without teaching every
# caller or diagnostic which default it should use.
LLM_OPERATION_DEFAULT_ROLES: Dict[str, str] = {
    "followup_resolution": "fast_llm",
    "plugin_assistant": "fast_llm",
    "timeline_merge": "fast_llm",
    "timeline_session_account": "fast_llm",
}

# Tailnet service-URL discovery cache. A model whose model_url is empty but which
# carries a `discovery_service` (e.g. chronicle-asr / chronicle-llm) resolves its URL
# live from minidisc — so a remote ASR/LLM node coming online is picked up without
# re-config. Cached briefly so the hot transcription/LLM paths don't do a minidisc
# lookup on every call.
_DISCOVERY_TTL_SECS = 30.0
_discovery_cache: Dict[str, tuple[float, str]] = {}


def _with_scheme(url: str) -> str:
    """Prepend http:// to a scheme-less host:port (env vars often store bare host:port).
    Leaves '' and already-schemed URLs (http/https/ws/wss) untouched."""
    if url and "://" not in url:
        return "http://" + url
    return url


def _discover_service_url(service_name: str) -> str:
    """Minidisc URL for a chronicle-* service, cached for _DISCOVERY_TTL_SECS. '' if none."""
    now = time.monotonic()
    cached = _discovery_cache.get(service_name)
    if cached and now - cached[0] < _DISCOVERY_TTL_SECS:
        return cached[1]
    url = ""
    try:
        # `discovery` is the repo-root module the compose file mounts into the
        # container; it is not importable in a plain checkout, so importing it at
        # module level would make model_registry unimportable outside Docker.
        from discovery import resolve_service_url

        url = resolve_service_url(None, service_name, default="") or ""
    except Exception:  # noqa: BLE001 - discovery is best-effort
        url = ""
    _discovery_cache[service_name] = (now, url)
    if url:
        logger.debug("Discovered %s at %s (tailnet)", service_name, url)
    return url


class ModelDef(BaseModel):
    """Model definition with validation.

    Represents a single model configuration (LLM, embedding, STT, TTS, etc.)
    from config.yml with automatic validation and type checking.
    """

    model_config = ConfigDict(
        extra="allow",  # Allow extra fields for extensibility
        validate_assignment=True,  # Validate on attribute assignment
        arbitrary_types_allowed=True,
    )

    name: str = Field(..., min_length=1, description="Unique model identifier")
    model_type: str = Field(
        ..., description="Model type: llm, embedding, stt, tts, etc."
    )
    model_provider: str = Field(
        default="unknown",
        description="Provider name: openai, ollama, deepgram, parakeet, vibevoice, etc.",
    )
    api_family: str = Field(
        default="openai", description="API family: openai, http, websocket, etc."
    )
    model_name: str = Field(default="", description="Provider-specific model name")
    model_url: str = Field(default="", description="Base URL for API requests")
    api_key: Optional[str] = Field(
        default=None, description="API key or authentication token"
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable description"
    )
    model_params: Dict[str, Any] = Field(
        default_factory=dict, description="Model-specific parameters"
    )
    system_prompt_prefix: str = Field(
        default="",
        description=(
            "Provider/model instruction prepended to system prompts, such as Muse "
            "Glimmer's documented reasoning-strength control."
        ),
    )
    model_output: Optional[str] = Field(
        default=None, description="Output format: json, text, vector, etc."
    )
    thinking: bool = Field(
        default=False,
        description=(
            "Local thinking/reasoning model whose extended thinking is toggled via the "
            "chat template (chat_template_kwargs.enable_thinking), NOT the OpenAI "
            "top-level `reasoning_effort` (which llama.cpp silently ignores). When set, "
            "an operation's reasoning_effort is translated to enable_thinking for this "
            "model (e.g. gemma/qwen served by llama.cpp)."
        ),
    )
    reasoning_policy: Literal["off", "per_operation"] = Field(
        default="off",
        description="Permission for local thinking models. Off overrides operation and adapter effort; per_operation requires an explicit operation effort to enable reasoning.",
    )

    @property
    def reasoning_allowed(self) -> bool:
        return not self.thinking or self.reasoning_policy == "per_operation"

    embedding_dimensions: Optional[int] = Field(
        default=None, ge=1, description="Embedding vector dimensions"
    )
    operations: Dict[str, Any] = Field(
        default_factory=dict, description="API operation definitions"
    )
    capabilities: List[str] = Field(
        default_factory=list,
        description=(
            "Provider capabilities. Output capabilities: word_timestamps, segments, "
            "diarization; forced_alignment advertises Chronicle's /align endpoint. "
            "ASR hint mechanism (mutually exclusive): "
            "'keyword_boosting' — accepts a hot-word list as an acoustic recognition "
            "hint that biases decoding without leaking into the transcript "
            "(Deepgram keyterm, VibeVoice prompt, Parakeet); 'context_prompt' — an "
            "LLM-backbone ASR that takes free-form context as prompt text (Gemma 4). "
            "context_prompt providers are given the user-authored asr_context only, "
            "never the wake-word boost list (which an LLM would echo into output)."
        ),
    )
    asr_context: Optional[str] = Field(
        default=None,
        description=(
            "Free-form context string for 'context_prompt' STT providers (e.g. a "
            "domain/topic description). Informs an LLM-backbone ASR without being "
            "transcribed. User overrides are stored under backend.asr.context.<name>."
        ),
    )
    deployment: Optional[str] = None
    deployment_endpoint: Optional[str] = None

    discovery_service: Optional[str] = Field(
        default=None,
        description=(
            "minidisc service name (e.g. 'chronicle-asr', 'chronicle-llm'). When set "
            "and model_url is empty, the base URL is resolved live from the Tailnet, so "
            "a remote ASR/LLM node coming online is used without re-config. An explicit "
            "model_url (env/config) always takes precedence over discovery."
        ),
    )
    discovery_default: Optional[str] = Field(
        default=None,
        description=(
            "Fallback base URL used when discovery_service is set but nothing is "
            "advertised yet (e.g. a host-local default)."
        ),
    )
    discovery_path: Optional[str] = Field(
        default=None,
        description=(
            "Path appended to a *discovered* bare host:port (e.g. '/v1' for an "
            "OpenAI-compatible LLM). Not applied to an explicit model_url or "
            "discovery_default (those are expected to already be complete)."
        ),
    )

    @field_validator("model_name", mode="before")
    @classmethod
    def default_model_name(cls, v: Any, info) -> str:
        """Default model_name to name if not provided."""
        if not v and info.data.get("name"):
            return info.data["name"]
        return v or ""

    @field_validator("model_url", mode="before")
    @classmethod
    def validate_url(cls, v: Any) -> str:
        """Ensure URL doesn't have trailing whitespace."""
        if isinstance(v, str):
            return v.strip()
        return v or ""

    @field_validator("api_key", mode="before")
    @classmethod
    def sanitize_api_key(cls, v: Any) -> Optional[str]:
        """Sanitize API key, treat empty strings as None."""
        if isinstance(v, str):
            v = v.strip()
            if not v or v.lower() in ["dummy", "none", "null"]:
                return None
            return v
        return v

    def resolved_url(self) -> str:
        """Effective base URL, resolving Tailnet discovery when not explicitly set.

        Order: explicit model_url (env/config) → minidisc discovery_service (with
        discovery_path appended to the bare host:port) → discovery_default. An
        advertised remote node is picked up live (cached), so a 'configure from the
        Tailnet later' setup needs no re-config when the node comes online. URLs
        without a scheme get http:// prepended. Returns '' when nothing is available.
        """
        if self.deployment:

            return service_deployment.gateway_url(
                self.deployment, self.deployment_endpoint
            )
        if self.model_url:
            return _with_scheme(self.model_url)
        if self.discovery_service:
            discovered = _discover_service_url(self.discovery_service)
            if discovered:
                if self.discovery_path:
                    discovered = discovered.rstrip("/") + self.discovery_path
                return _with_scheme(discovered)
        return _with_scheme(self.discovery_default or "")

    @model_validator(mode="after")
    def validate_model(self) -> ModelDef:
        """Cross-field validation."""
        if bool(self.deployment) != bool(self.deployment_endpoint):
            raise ValueError("deployment and deployment_endpoint must be set together")
        if self.deployment:
            if (
                self.model_url
                or self.discovery_service
                or self.discovery_default
                or self.discovery_path
            ):
                raise ValueError(
                    "Managed models cannot also configure model_url or discovery"
                )

            object.__setattr__(self, "api_key", service_deployment.gateway_token())

        # Ensure embedding models have dimensions specified
        if self.model_type == "embedding" and not self.embedding_dimensions:
            # Common defaults
            defaults = {
                "text-embedding-3-small": 1536,
                "text-embedding-3-large": 3072,
                "text-embedding-ada-002": 1536,
                "nomic-embed-text-v1.5": 768,
            }
            if self.model_name in defaults:
                self.embedding_dimensions = defaults[self.model_name]

        return self


class LLMOperationConfig(BaseModel):
    """Per-operation LLM config as written in YAML.

    Each field is optional so users can override only what they need;
    unset fields are resolved from the model's model_params at runtime.
    """

    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    response_format: Optional[str] = None  # "json" → {"type": "json_object"}
    reasoning_effort: Optional[str] = (
        None  # "minimal"|"low"|... — reasoning models only
    )


class ResolvedLLMOperation(BaseModel):
    """Everything needed to make an LLM call. No further lookups required.

    Works uniformly for OpenAI, Ollama, Groq, or any OpenAI-compatible provider.
    The model_def carries all provider details (api_key, base_url, model_name).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_def: ModelDef
    temperature: float
    max_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None  # {"type": "json_object"} or None
    reasoning_effort: Optional[str] = None  # OpenAI reasoning models OR thinking models

    @property
    def reasoning_allowed(self) -> bool:
        if not self.model_def.thinking:
            return True
        return self.model_def.reasoning_allowed and str(
            self.effective_reasoning_effort
        ).strip().lower() not in {"none", "minimal", "off", "0"}

    @property
    def effective_reasoning_effort(self) -> Optional[str]:
        """Model permission is authoritative; model capability is not an opt-in."""
        if self.model_def.thinking:
            if not self.model_def.reasoning_allowed:
                return "none"
            return self.reasoning_effort or "none"
        return self.reasoning_effort

    @property
    def model_name(self) -> str:
        return self.model_def.model_name

    @property
    def api_key(self) -> Optional[str]:
        return self.model_def.api_key

    @property
    def base_url(self) -> str:
        return self.model_def.resolved_url()

    def to_api_params(self) -> Dict[str, Any]:
        """Returns kwargs for client.chat.completions.create().

        Works for OpenAI, Ollama, Groq — all OpenAI-compatible. For OpenAI
        reasoning-class models (gpt-5*, o1/o3/o4), `temperature` is omitted,
        `max_tokens` is renamed to `max_completion_tokens`, and `reasoning_effort`
        is forwarded as a top-level param — matching OpenAI's stricter API surface.

        Local *thinking* models (``model_def.thinking``, e.g. gemma/qwen served by
        llama.cpp) silently IGNORE the OpenAI top-level `reasoning_effort`, so an
        operation's reasoning_effort is instead translated to the chat-template switch
        the server actually honors (``chat_template_kwargs.enable_thinking``), sent via
        the OpenAI SDK's ``extra_body``. Extended thinking is bounded server-side by
        llama.cpp's ``--reasoning-budget``.
        """
        model_name = self.model_def.model_name
        openai_reasoning = is_reasoning_model(model_name)

        params: Dict[str, Any] = {"model": model_name}
        model_params = self.model_def.model_params or {}
        if not openai_reasoning:
            params["temperature"] = self.temperature
            if model_params.get("top_p") is not None:
                params["top_p"] = model_params["top_p"]
        if self.max_tokens is not None:
            key = "max_completion_tokens" if openai_reasoning else "max_tokens"
            params[key] = self.max_tokens
        if self.response_format is not None:
            params["response_format"] = self.response_format

        extra_body: Dict[str, Any] = {}
        if model_params.get("top_k") is not None:
            extra_body["top_k"] = model_params["top_k"]

        effective_effort = self.effective_reasoning_effort
        if effective_effort:
            if openai_reasoning:
                effort = effective_effort.strip().lower()
                # "none" is accepted by versioned GPT-5.1+ models. Earlier GPT-5
                # variants (gpt-5, gpt-5-mini/nano) require "minimal" instead.
                unqualified_model_name = model_name.lower().rsplit("/", 1)[-1]
                version_match = re.match(r"^gpt-5\.(\d+)", unqualified_model_name)
                supports_none = bool(version_match and int(version_match.group(1)) >= 1)
                if effort in ("none", "off", "0") and not supports_none:
                    effort = "minimal"
                params["reasoning_effort"] = effort
            elif self.model_def.model_provider == "openrouter":
                extra_body["reasoning"] = {"effort": effective_effort.strip().lower()}
            elif self.model_def.thinking:
                # "none"/"minimal"/"off"/"0" → thinking off; any other level → on.
                enable = effective_effort.strip().lower() not in (
                    "none",
                    "minimal",
                    "off",
                    "0",
                )
                extra_body["chat_template_kwargs"] = {"enable_thinking": enable}
        if extra_body:
            params["extra_body"] = extra_body
        return params

    def prepare_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply the selected model's documented system-prompt contract."""
        prefix = self.model_def.system_prompt_prefix.strip()
        prepared = [dict(message) for message in messages]
        if not prefix:
            return prepared
        for message in prepared:
            if message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{prefix}\n\n{content}"
                return prepared
            break
        prepared.insert(0, {"role": "system", "content": prefix})
        return prepared

    def get_client(self, is_async: bool = False):
        """Create an OpenAI-compatible client for this operation.

        Uses create_openai_client which handles Langfuse tracing.
        """
        return create_openai_client(
            api_key=self.model_def.api_key or "",
            base_url=self.model_def.resolved_url(),
            is_async=is_async,
        )


class AppModels(BaseModel):
    """Application models registry.

    Contains default model selections and all available model definitions.
    """

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
    )

    defaults: Dict[str, str] = Field(
        default_factory=dict, description="Default model names for each model_type"
    )
    models: Dict[str, ModelDef] = Field(
        default_factory=dict,
        description="All available model definitions keyed by name",
    )
    memory: Dict[str, Any] = Field(
        default_factory=dict, description="Memory service configuration"
    )
    speaker_recognition: Dict[str, Any] = Field(
        default_factory=dict, description="Speaker recognition service configuration"
    )
    llm_operations: Dict[str, LLMOperationConfig] = Field(
        default_factory=dict,
        description="Per-operation LLM configuration (temperature, model override, etc.)",
    )

    def get_by_name(self, name: str) -> Optional[ModelDef]:
        """Get a model by its unique name.

        Args:
            name: Model name to look up

        Returns:
            ModelDef if found, None otherwise
        """
        return self.models.get(name)

    def get_default(self, model_type: str) -> Optional[ModelDef]:
        """Get the default model for a given type.

        Args:
            model_type: Type of model (llm, embedding, stt, tts, etc.)

        Returns:
            Default ModelDef for the type, or first available model of that type,
            or None if no models of that type exist
        """
        # Try explicit default first
        name = self.defaults.get(model_type)
        if name:
            model = self.get_by_name(name)
            if model:
                return model

        # Fallback: first model of that type
        for m in self.models.values():
            if m.model_type == model_type:
                return m

        return None

    def get_all_by_type(self, model_type: str) -> List[ModelDef]:
        """Get all models of a specific type.

        Args:
            model_type: Type of model to filter by

        Returns:
            List of ModelDef objects matching the type
        """
        return [m for m in self.models.values() if m.model_type == model_type]

    def llm_operation_role(self, name: str) -> str:
        """Return the deployment-wide default role used by an unpinned operation."""
        return LLM_OPERATION_DEFAULT_ROLES.get(name, "llm")

    def list_model_types(self) -> List[str]:
        """Get all unique model types in the registry.

        Returns:
            Sorted list of model types
        """
        return sorted(set(m.model_type for m in self.models.values()))

    def get_llm_operation(
        self,
        name: str,
        *,
        model_override: Optional[str] = None,
    ) -> ResolvedLLMOperation:
        """Resolve a named LLM operation to a self-contained config.

        Resolution:
          1. Look up llm_operations[name] (empty LLMOperationConfig if missing)
          2. Resolve model_def: model_override → get_by_name, else op.model →
             get_by_name, else the operation's centrally defined default role
          3. Merge parameters: operation > model_def.model_params > safe fallback
          4. Return ResolvedLLMOperation ready for use

        Args:
            name: Operation name (e.g. "memory_extraction", "chat")
            model_override: pin a specific model by name, overriding both the
                operation's model and the defaults (used for fallback retries).

        Returns:
            ResolvedLLMOperation with model_def, temperature, max_tokens, response_format

        Raises:
            RuntimeError: If no model can be resolved for the operation
        """
        op_config = self.llm_operations.get(name, LLMOperationConfig())
        default_role = self.llm_operation_role(name)

        # Resolve model definition
        if model_override:
            model_def = self.get_by_name(model_override)
            if not model_def:
                raise RuntimeError(
                    f"LLM operation '{name}' requested override model "
                    f"'{model_override}' which is not defined in the models list"
                )
        elif op_config.model:
            model_def = self.get_by_name(op_config.model)
            if not model_def:
                raise RuntimeError(
                    f"LLM operation '{name}' references model '{op_config.model}' "
                    f"which is not defined in the models list"
                )
        else:
            model_def = self.get_default(default_role)
            if not model_def and default_role != "llm":
                model_def = self.get_default("llm")
            if not model_def:
                raise RuntimeError(
                    f"No model specified for operation '{name}' and no default LLM defined"
                )

        # Merge parameters: operation config > model_params > safe fallback
        model_params = model_def.model_params or {}

        temperature = (
            op_config.temperature
            if op_config.temperature is not None
            else model_params.get("temperature", 0.2)
        )
        max_tokens = (
            op_config.max_tokens
            if op_config.max_tokens is not None
            else model_params.get("max_tokens")
        )
        reasoning_effort = (
            op_config.reasoning_effort
            if op_config.reasoning_effort is not None
            else (None if model_def.thinking else model_params.get("reasoning_effort"))
        )

        # Convert "json" shorthand to OpenAI format
        response_format = None
        if op_config.response_format == "json":
            response_format = {"type": "json_object"}

        return ResolvedLLMOperation(
            model_def=model_def,
            temperature=float(temperature),
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )

    def explain_llm_operation(self, name: str) -> Dict[str, Optional[str]]:
        """Return the effective model and the one configuration source that chose it."""
        op_config = self.llm_operations.get(name, LLMOperationConfig())
        if op_config.model:
            role = None
            source = f"llm_operations.{name}.model"
        else:
            role = self.llm_operation_role(name)
            source = f"defaults.{role}"
        resolved = self.get_llm_operation(name)
        return {
            "model": resolved.model_def.name,
            "model_name": resolved.model_name,
            "provider": resolved.model_def.model_provider,
            "role": role,
            "source": source,
        }

    def get_fallback_llm_operation(
        self,
        name: str,
        *,
        primary: ResolvedLLMOperation,
    ) -> Optional[ResolvedLLMOperation]:
        """Resolve operation ``name`` against ``defaults.fallback_llm``.

        Returns None when no fallback is configured, when the fallback entry is
        missing or not an LLM, or when it is the same model the primary attempt
        already used (retrying it would be pointless).
        """
        fb_name = self.defaults.get("fallback_llm")
        if not fb_name or fb_name == primary.model_def.name:
            return None
        fb_def = self.get_by_name(fb_name)
        if not fb_def or fb_def.model_type != "llm":
            logger.warning(
                "defaults.fallback_llm=%r does not name an LLM model — ignoring",
                fb_name,
            )
            return None
        return self.get_llm_operation(name, model_override=fb_name)


# Global registry singleton
_REGISTRY: Optional[AppModels] = None


def _find_config_path() -> Path:
    """
    Find config.yml using canonical path from config module.

    DEPRECATED: Use backend.config.get_config_yml_path() directly.
    Kept for backward compatibility.

    Returns:
        Path to config.yml
    """
    return get_config_yml_path()


def validate_model_routing_authority(config: Any) -> None:
    """Reject executor-level model pins that bypass the model registry's routing.

    Executors may keep execution knobs such as timeout, context window, and Pi
    compatibility. Model selection belongs only to ``defaults`` and
    ``llm_operations`` so changing a deployment route has one authoritative seam.
    """
    obsolete_paths = (
        ("memory", "backends", "pi", "model"),
        ("timeline", "pi", "model"),
    )
    for path in obsolete_paths:
        value: Any = config
        for key in path:
            if not hasattr(value, "get"):
                value = None
                break
            value = value.get(key)
            if value is None:
                break
        if value not in (None, ""):
            dotted = ".".join(path)
            raise ValueError(
                f"{dotted} is obsolete; select models with defaults or "
                "llm_operations instead"
            )


def load_models_config(force_reload: bool = False) -> Optional[AppModels]:
    """Load model configuration from merged defaults.yml + config.yml.

    This function loads defaults.yml and config.yml, merges them with user overrides,
    validates model definitions using Pydantic, and caches the result.
    Environment variables are resolved by OmegaConf during config loading.

    Args:
        force_reload: If True, reload from disk even if already cached

    Returns:
        AppModels instance with validated configuration, or None if config not found

    Raises:
        ValidationError: If config.yml has invalid model definitions
    """
    global _REGISTRY
    if _REGISTRY is not None and not force_reload:
        return _REGISTRY

    # Get merged configuration (defaults + user config)
    # OmegaConf resolves environment variables automatically
    try:
        raw = get_config(force_reload=force_reload)
    except Exception as e:
        logging.error(f"Failed to load merged configuration: {e}")
        return None

    validate_model_routing_authority(raw)

    # Extract sections
    defaults = raw.get("defaults", {}) or {}
    model_list = raw.get("models", []) or []
    memory_settings = raw.get("memory", {}) or {}
    speaker_recognition_cfg = raw.get("speaker_recognition", {}) or {}
    llm_ops_raw = raw.get("llm_operations", {}) or {}

    # Parse and validate models using Pydantic
    models: Dict[str, ModelDef] = {}
    for m in model_list:
        try:
            # Pydantic will handle validation automatically
            model_def = ModelDef(**m)
            models[model_def.name] = model_def
        except ValidationError as e:
            # Log but don't fail the entire registry load
            logging.warning(f"Failed to load model '{m.get('name', 'unknown')}': {e}")
            continue

    # Parse LLM operation configs
    llm_operations: Dict[str, LLMOperationConfig] = {}
    for op_name, op_dict in llm_ops_raw.items():
        try:
            llm_operations[op_name] = LLMOperationConfig(**(op_dict or {}))
        except ValidationError as e:
            logging.warning(f"Failed to load llm_operation '{op_name}': {e}")

    # Create and cache registry
    _REGISTRY = AppModels(
        defaults=defaults,
        models=models,
        memory=memory_settings,
        speaker_recognition=speaker_recognition_cfg,
        llm_operations=llm_operations,
    )
    return _REGISTRY


def get_models_registry() -> Optional[AppModels]:
    """Get the global models registry.

    This is the primary interface for accessing model configurations.
    The registry is loaded once and cached for performance.

    Returns:
        AppModels instance, or None if config.yml not found

    Example:
        >>> registry = get_models_registry()
        >>> if registry:
        ...     llm = registry.get_default('llm')
        ...     print(f"Default LLM: {llm.name} ({llm.model_provider})")
    """
    return load_models_config(force_reload=False)
