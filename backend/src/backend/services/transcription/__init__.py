"""
Transcription providers and registry-driven factory.

This module exposes a provider that reads its configuration from the
central model registry (config.yml). No environment-based selection
or provider-specific branching is used for batch transcription.
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import websockets

from backend.config_loader import get_backend_config
from backend.model_registry import get_models_registry
from backend.models.conversation import Conversation
from backend.prompt_registry import get_prompt_registry
from backend.services import privacy
from backend.services.plugin_service import get_plugin_router

from .base import (
    BaseTranscriptionProvider,
    BatchTranscriptionProvider,
    StreamingTranscriptionProvider,
)

logger = logging.getLogger(__name__)


def _get_plugin_keywords() -> list[str]:
    """Collect ASR keyword hints from all enabled plugins.

    Returns an empty list if the plugin system is not initialised yet.
    """
    try:
        router = get_plugin_router()
        if router:
            return router.get_asr_keywords()
    except Exception:
        pass
    return []


def _merge_hot_words(prompt_hot_words: str, plugin_keywords: list[str]) -> str:
    """Merge prompt-registry hot words with plugin keywords (deduplicated)."""
    parts: list[str] = []
    seen: set[str] = set()

    # Parse prompt registry hot words first
    if prompt_hot_words and prompt_hot_words.strip():
        for word in re.split(r"[,\n]+", prompt_hot_words):
            word = word.strip().lower()
            if word and word not in seen:
                seen.add(word)
                parts.append(word)

    # Add plugin keywords
    for kw in plugin_keywords:
        kw = kw.strip().lower()
        if kw and kw not in seen:
            seen.add(kw)
            parts.append(kw)

    return "\n".join(parts) if parts else ""


def _parse_hot_words_to_keyterm(hot_words_str: str) -> str:
    """Convert hot words string to Deepgram keyterm format.

    Splits on commas and newlines (context may arrive in either format).

    Input:  "hey hermes\\nchronicle\\nomi"  or  "hey hermes, chronicle, omi"
    Output: "hey hermes Hey Hermes chronicle Chronicle omi Omi"
    """
    if not hot_words_str or not hot_words_str.strip():
        return ""
    terms = []
    for word in re.split(r"[,\n]+", hot_words_str):
        word = word.strip().lower()
        if not word:
            continue
        terms.append(word)
    return " ".join(terms)


# ASR hint mechanisms (see ModelDef.capabilities). A provider consumes context in
# exactly one of two ways, which must NOT be conflated:
#   keyword_boosting — a hot-word list used as an acoustic recognition hint that
#       biases decoding without ever appearing in the output (Deepgram keyterm,
#       VibeVoice prompt, Parakeet context_info). Safe to feed plugin wake-words.
#   context_prompt — an LLM-backbone ASR that takes free-form context as prompt
#       text. Feeding it the wake-word list makes it echo those words into the
#       transcript (the leak we are fixing), so it gets the user-authored
#       asr_context ONLY — never the boost list.
CAP_KEYWORD_BOOSTING = "keyword_boosting"
CAP_CONTEXT_PROMPT = "context_prompt"


def _resolve_asr_context(model) -> str:
    """Resolve the user-authored context string for a context_prompt provider.

    Precedence: the ``backend.asr.context.<model_name>`` override (written by the
    System page / wizard) over the inline ``asr_context`` shipped on the model
    entry. Returns "" when neither is set.
    """
    try:
        asr_cfg = get_backend_config("asr") or {}
        ctx_map = asr_cfg.get("context", {}) or {}
        override = ctx_map.get(model.name)
        if override is not None and str(override).strip():
            return str(override).strip()
    except Exception as e:
        logger.debug(f"Failed to read backend.asr.context override: {e}")
    inline = getattr(model, "asr_context", None)
    return inline.strip() if isinstance(inline, str) else ""


def _resolve_asr_hint(
    model, capabilities: set, caller_context: Optional[str], prompt_hot_words: str
) -> tuple[Optional[str], str]:
    """Decide which kind of ASR hint to send to ``model`` and its text.

    Returns ``(kind, text)`` where ``kind`` is:
      "context" — free-form LLM context (context_prompt providers); the wake-word
          boost list is deliberately excluded so the LLM does not echo it.
      "keyword" — acoustic hot-word boost list (every other provider; current
          behaviour). Empty text means "no hint".
    """
    if CAP_CONTEXT_PROMPT in capabilities:
        context = (
            caller_context.strip() if caller_context else _resolve_asr_context(model)
        )
        return ("context", context)

    # Default (keyword/acoustic) path — preserves prior behaviour for all
    # non-LLM providers: merge prompt-registry hot words with plugin wake-words.
    base = caller_context if caller_context else prompt_hot_words
    return ("keyword", _merge_hot_words(base, _get_plugin_keywords()).strip())


def _dotted_get(d: dict | list | None, dotted: Optional[str]):
    """Safely extract a value from nested dict/list using dotted paths.

    Supports simple dot separators and list indexes like "results[0].alternatives[0].transcript".
    Returns None when the path can't be fully resolved.
    """
    if d is None or not dotted:
        return None
    cur = d
    for part in dotted.split("."):
        if not part:
            continue
        if "[" in part and part.endswith("]"):
            name, idx_str = part[:-1].split("[", 1)
            if name:
                cur = cur.get(name, {}) if isinstance(cur, dict) else {}
            try:
                idx = int(idx_str)
            except Exception:
                return None
            if isinstance(cur, list) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            cur = cur.get(part, None) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _normalize_provider_segments(segments: list) -> list:
    """Normalize provider-specific segment formats to a standard shape.

    Handles Deepgram paragraph format where:
    - text is nested in ``sentences[].text`` instead of a top-level ``text`` field
    - ``speaker`` is an integer (0, 1) instead of a string ("Speaker 0")

    After normalization every segment dict will have:
    - ``text`` (str): combined sentence text
    - ``speaker`` (str): "Speaker N" label
    - ``start`` / ``end`` (float): time span (preserved from original)
    """
    if not segments:
        return segments

    for seg in segments:
        if not isinstance(seg, dict):
            continue

        # Deepgram paragraphs: text lives inside sentences[], not top-level
        if "text" not in seg and "sentences" in seg:
            sentences = seg.get("sentences", [])
            seg["text"] = " ".join(
                s.get("text", "") for s in sentences if isinstance(s, dict)
            )

        # Normalise integer speaker IDs to "Speaker N" strings
        speaker = seg.get("speaker")
        if isinstance(speaker, (int, float)):
            seg["speaker"] = f"Speaker {int(speaker)}"

    return segments


class RegistryBatchTranscriptionProvider(BatchTranscriptionProvider):
    """Batch transcription provider driven by config.yml."""

    def __init__(self, model_name: Optional[str] = None):
        registry = get_models_registry()
        if not registry:
            raise RuntimeError("config.yml not found; cannot configure STT provider")
        if model_name:
            model = registry.get_by_name(model_name)
            if not model or model.model_type != "stt":
                raise RuntimeError(
                    f"Model '{model_name}' is not a configured STT model"
                )
        else:
            model = registry.get_default("stt")
            if not model:
                raise RuntimeError("No default STT model defined in config.yml")
        self.model = model
        self._name = model.model_provider or model.name
        # Load capabilities from config.yml model definition
        self._capabilities = set(model.capabilities) if model.capabilities else set()
        self._allow_fallback = model_name is None

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set:
        """Return provider capabilities from config.yml.

        Capabilities indicate what the provider can produce:
        - word_timestamps: Word-level timing data
        - segments: Speaker segments
        - diarization: Speaker labels in segments

        Returns:
            Set of capability strings
        """
        return self._capabilities

    def get_capabilities_dict(self) -> dict:
        """Return capabilities as a dict for metadata storage.

        Returns:
            Dict mapping capability names to True
        """
        return {cap: True for cap in self._capabilities}

    async def _lookup_cached_transcription(
        self,
        audio_data: bytes,
        sample_rate: int,
        *,
        diarize: bool,
        context_info: Optional[str] = None,
    ) -> tuple[Any | None, dict | None, dict | None]:
        """Return ``(collection, key, result)`` for the paid-response cache."""
        if self.model.model_provider == "mock":
            return None, None, None
        try:
            config = (
                self.model.model_dump()
                if hasattr(self.model, "model_dump")
                else dict(vars(self.model))
            )
            config.pop("api_key", None)
            fingerprint = json.dumps(
                {
                    "provider": self._name,
                    "config": config,
                    "diarize": diarize,
                    "sample_rate": sample_rate,
                    "context_sha256": hashlib.sha256(
                        (context_info or "").encode()
                    ).hexdigest(),
                },
                sort_keys=True,
                default=str,
            )
            cache_key = {
                "audio_sha256": hashlib.sha256(audio_data).hexdigest(),
                "request_sha256": hashlib.sha256(fingerprint.encode()).hexdigest(),
            }
            cache = Conversation.get_pymongo_collection().database[
                "transcription_response_cache"
            ]
            row = await cache.find_one(cache_key, {"result": 1})
            result = row.get("result") if row else None
            return cache, cache_key, result
        except Exception as e:
            logger.debug(f"Transcription cache lookup skipped: {e}")
            return None, None, None

    async def get_cached_transcription(
        self,
        audio_data: bytes,
        sample_rate: int,
        *,
        diarize: bool = False,
        context_info: Optional[str] = None,
    ) -> dict | None:
        """Look up a response without ever calling the transcription provider."""
        _cache, _cache_key, result = await self._lookup_cached_transcription(
            audio_data,
            sample_rate,
            diarize=diarize,
            context_info=context_info,
        )
        if result is not None:
            logger.info(
                f"♻️ Transcription cache hit for '{self._name}' "
                f"({len(audio_data)} bytes) — reusing stored response, "
                "no provider call"
            )
        return result

    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int,
        diarize: bool = False,
        context_info: Optional[str] = None,
        progress_callback=None,
        priority: bool = False,
        privacy_checks: list | None = None,
        **kwargs,
    ) -> dict:
        """Transcribe with a persistent response cache.

        Batch providers are typically paid, per-minute APIs; re-transcribing
        identical audio (reprocessing, retries after downstream failures, bulk
        speaker mining over backup files) must not bill twice. The normalized
        result is stored in Mongo keyed by the audio content hash plus the
        provider configuration (minus the API key, so key rotation keeps the
        cache). Context is an exact model input: changing or withholding private
        vocabulary must not reuse a response influenced by that vocabulary.
        Cache failures never block transcription.
        """
        for owner, snapshot in privacy_checks or []:
            await privacy.assert_current(owner, snapshot)
        cache, cache_key, cached_result = await self._lookup_cached_transcription(
            audio_data,
            sample_rate,
            diarize=diarize,
            context_info=context_info,
        )
        for owner, snapshot in privacy_checks or []:
            await privacy.assert_current(owner, snapshot)
        if cached_result is not None:
            logger.info(
                f"♻️ Transcription cache hit for '{self._name}' "
                f"({len(audio_data)} bytes) — reusing stored response, "
                "no provider call"
            )
            return cached_result

        try:
            result = await self._transcribe_uncached(
                audio_data,
                sample_rate,
                diarize=diarize,
                context_info=context_info,
                progress_callback=progress_callback,
                priority=priority,
                privacy_checks=privacy_checks,
                **kwargs,
            )
        except privacy.PrivacyHeld:
            raise
        except Exception:
            for owner, snapshot in privacy_checks or []:
                await privacy.assert_current(owner, snapshot)
            registry = get_models_registry()
            fallback_name = (
                registry.defaults.get("fallback_stt")
                if registry and self._allow_fallback
                else None
            )
            if not fallback_name or fallback_name == self.model.name:
                raise
            logger.exception(
                "Primary STT '%s' failed; retrying with fallback '%s'",
                self.model.name,
                fallback_name,
            )
            fallback = RegistryBatchTranscriptionProvider(model_name=fallback_name)
            return await fallback.transcribe(
                audio_data,
                sample_rate,
                diarize=diarize,
                context_info=context_info,
                progress_callback=progress_callback,
                priority=priority,
                privacy_checks=privacy_checks,
                **kwargs,
            )

        for owner, snapshot in privacy_checks or []:
            await privacy.assert_current(owner, snapshot)

        # Don't cache an empty transcript: the key doesn't cover the ASR
        # service's gate settings, so a cached "" would outlive a threshold change.
        if (
            cache is not None
            and cache_key is not None
            and (result or {}).get("text", "").strip()
        ):
            try:
                # Mongo documents cap at 16MB; skip pathological payloads.
                if len(json.dumps(result, default=str)) < 12_000_000:
                    await cache.update_one(
                        cache_key,
                        {
                            "$set": {
                                "provider": self._name,
                                "audio_bytes": len(audio_data),
                                "result": result,
                                "created_at": datetime.now(timezone.utc),
                            }
                        },
                        upsert=True,
                    )
            except Exception as e:
                logger.debug(f"Transcription cache write skipped: {e}")
        return result

    async def _transcribe_uncached(
        self,
        audio_data: bytes,
        sample_rate: int,
        diarize: bool = False,
        context_info: Optional[str] = None,
        progress_callback=None,
        priority: bool = False,
        privacy_checks: list | None = None,
        **kwargs,
    ) -> dict:
        # Special handling for mock provider (no HTTP server needed)
        if self.model.model_provider == "mock":
            # Lazy import: test/mock-only provider
            from .mock_provider import MockTranscriptionProvider

            mock = MockTranscriptionProvider(fail_mode=False)
            return await mock.transcribe(audio_data, sample_rate, diarize)

        op = (self.model.operations or {}).get("stt_transcribe") or {}
        method = (op.get("method") or "POST").upper()
        path = op.get("path") or "/listen"
        # Build URL
        base = self.model.resolved_url().rstrip("/")
        url = base + ("/" + path.lstrip("/"))

        # Check if we should use multipart file upload (for Parakeet)
        content_type = op.get("content_type", "audio/raw")
        use_multipart = content_type == "multipart/form-data"

        # Build headers (skip Content-Type for multipart as httpx will set it)
        headers = {}
        if not use_multipart:
            # Auto-detect WAV format from RIFF header and use correct Content-Type.
            # Sending WAV data as audio/raw can cause Deepgram to silently return
            # empty transcripts because it tries to decode the WAV header as raw PCM.
            if audio_data[:4] == b"RIFF":
                headers["Content-Type"] = "audio/wav"
            else:
                headers["Content-Type"] = "audio/raw"

        if self.model.api_key:
            # Allow templated header, otherwise fallback to Bearer/Token conventions by config
            hdrs = op.get("headers") or {}
            # Resolve simple ${VAR} placeholders in op headers using env (optional)
            for k, v in hdrs.items():
                if isinstance(v, str):
                    headers[k] = v.replace("${DEEPGRAM_API_KEY:-}", self.model.api_key)
                else:
                    headers[k] = v
        else:
            # When no API key, only add headers that don't require authentication
            hdrs = op.get("headers") or {}
            for k, v in hdrs.items():
                # Skip Authorization headers with empty/invalid values
                if k.lower() == "authorization" and (
                    not v
                    or v.strip().lower() in ["token", "token ", "bearer", "bearer "]
                ):
                    continue
                headers[k] = v

        # Query params
        query = op.get("query") or {}
        # Inject common params if placeholders used
        if "sample_rate" in query:
            query["sample_rate"] = str(sample_rate)
        if "diarize" in query:
            query["diarize"] = "true" if diarize else "false"

        # Resolve the ASR hint by the provider's hint mechanism (see
        # _resolve_asr_hint): keyword_boosting providers get the merged hot-word
        # boost list; context_prompt (LLM) providers get the user-authored
        # asr_context ONLY — never the wake-word list, which they would echo.
        prompt_hot_words = ""
        if not context_info:
            try:
                registry = get_prompt_registry()
                prompt_hot_words = await registry.get_prompt("asr.hot_words")
            except Exception as e:
                logger.debug(f"Failed to fetch asr.hot_words prompt: {e}")

        hint_kind, hot_words_str = _resolve_asr_hint(
            self.model, self._capabilities, context_info, prompt_hot_words
        )
        if hot_words_str:
            logger.debug(
                f"ASR hint for {self.model.name}: kind={hint_kind}, "
                f"characters={len(hot_words_str)}"
            )

        # For Deepgram: inject keyword boost as the keyterm query param.
        if (
            CAP_KEYWORD_BOOSTING in self._capabilities
            and self.model.model_provider == "deepgram"
            and hot_words_str.strip()
        ):
            keyterm = _parse_hot_words_to_keyterm(hot_words_str)
            if keyterm:
                query["keyterm"] = keyterm

        # NOTE: PULSE (smallest.ai) does NOT support keywords on WebSocket or
        # batch HTTP — any `keywords` query param causes 0 responses or HTTP 400.
        # Hot-word boosting for PULSE is not injected here.

        timeout = op.get("timeout", 300)
        # Use a longer read timeout for NDJSON progress responses — each
        # batch window can take minutes but the service keeps sending
        # progress lines between windows.
        read_timeout = op.get("read_timeout", timeout)
        try:
            timeouts = httpx.Timeout(timeout, read=read_timeout)
            async with httpx.AsyncClient(timeout=timeouts) as client:
                for owner, snapshot in privacy_checks or []:
                    await privacy.assert_current(owner, snapshot)
                if method == "POST":
                    if use_multipart:
                        # Send as multipart file upload (for Parakeet/VibeVoice)
                        files = {"file": ("audio.wav", audio_data, "audio/wav")}
                        form_data = {}
                        if hot_words_str and hot_words_str.strip():
                            form_data["context_info"] = hot_words_str.strip()
                        # Route latency-sensitive requests (e.g. wake-word command
                        # clips) to the service's dedicated priority GPU lane so they
                        # don't queue behind a long batch.
                        if priority:
                            form_data["priority"] = "1"

                        # Use streaming to handle NDJSON progress responses
                        async with client.stream(
                            "POST",
                            url,
                            headers=headers,
                            params=query,
                            files=files,
                            data=form_data,
                        ) as resp:
                            # The stream context closes the response while unwinding
                            # an HTTPStatusError. Read error bodies before raising so
                            # the provider's actual diagnostic survives that boundary.
                            if resp.is_error:
                                await resp.aread()
                            resp.raise_for_status()
                            content_type = resp.headers.get("content-type", "")

                            if "application/x-ndjson" in content_type:
                                # Batch progress: read events line by line
                                data = None
                                async for line in resp.aiter_lines():
                                    line = line.strip()
                                    if not line:
                                        continue
                                    event = json.loads(line)
                                    if (
                                        event.get("type") == "progress"
                                        and progress_callback
                                    ):
                                        progress_callback(event)
                                    elif event.get("type") == "result":
                                        data = event
                                if data is None:
                                    raise RuntimeError(
                                        f"NDJSON stream from '{self._name}' ended without a result event"
                                    )
                            else:
                                # Normal JSON response
                                await resp.aread()
                                data = resp.json()
                    else:
                        # Send as raw audio data (for Deepgram)
                        resp = await client.post(
                            url, headers=headers, params=query, content=audio_data
                        )
                        resp.raise_for_status()
                        data = resp.json()
                else:
                    resp = await client.get(url, headers=headers, params=query)
                    resp.raise_for_status()
                    data = resp.json()
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot reach transcription service '{self._name}' at {url}. "
                f"Is the service running? Check that the URL in config.yml "
                f"is correct and the service is accessible from inside Docker "
                f"(use 'host.docker.internal' instead of 'localhost')."
            ) from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # For streaming requests the body isn't consumed yet, so read it
            # to surface the actual error the transcription service reported.
            body = ""
            try:
                if not e.response.is_closed:
                    await e.response.aread()
                body = e.response.text
            except Exception as read_err:
                body = f"<could not read response body: {read_err}>"
            # Try to pull a cleaner message out of a JSON error payload.
            detail = body.strip()
            if detail:
                try:
                    payload = json.loads(detail)
                    if isinstance(payload, dict):
                        detail = str(
                            payload.get("detail")
                            or payload.get("error")
                            or payload.get("message")
                            or detail
                        )
                except (json.JSONDecodeError, ValueError):
                    pass
            # Keep the surfaced body bounded so a giant traceback doesn't bloat logs.
            if len(detail) > 2000:
                detail = detail[:2000] + "… (truncated)"
            hint = "Check your API key. " if status in (401, 403) else ""
            msg = (
                f"Transcription service '{self._name}' at {url} returned HTTP {status}. "
                f"{hint}"
            )
            if detail:
                msg += f"Service error: {detail}"
            raise RuntimeError(msg) from e

        # Extract normalized shape
        text, words, segments = "", [], []
        extract = (op.get("response", {}) or {}).get("extract") or {}
        if extract:
            text = _dotted_get(data, extract.get("text")) or ""
            words = _dotted_get(data, extract.get("words")) or []
            segments = _dotted_get(data, extract.get("segments")) or []
            segments = _normalize_provider_segments(segments)

            # Provider segments are always stored; the diarization_source setting
            # decides downstream whether the speaker pipeline trusts them or
            # re-diarizes with pyannote.
            logger.debug(
                f"Transcription: Extracted {len(words)} words, {len(segments)} provider segments"
            )

        return {"text": text, "words": words, "segments": segments}

    async def health_check(self) -> dict:
        """Check batch STT service reachability and auth by hitting the base URL."""
        base = self.model.resolved_url().rstrip("/")
        headers = {}
        if self.model.api_key:
            op = (self.model.operations or {}).get("stt_transcribe") or {}
            hdrs = op.get("headers") or {}
            for k, v in hdrs.items():
                if isinstance(v, str):
                    headers[k] = v.replace("${DEEPGRAM_API_KEY:-}", self.model.api_key)
                else:
                    headers[k] = v

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(base, headers=headers)
                if resp.status_code in (401, 403):
                    return {
                        "status": "❌ Auth Failed — check API key",
                        "healthy": False,
                    }
                return {"status": "✅ Connected", "healthy": True}
        except httpx.ConnectError:
            return {
                "status": "❌ Connection Failed — service unreachable",
                "healthy": False,
            }
        except httpx.TimeoutException:
            return {"status": "❌ Connection Timeout", "healthy": False}
        except Exception as e:
            return {"status": f"❌ Error: {e}", "healthy": False}


class RegistryStreamingTranscriptionProvider(StreamingTranscriptionProvider):
    """Streaming transcription provider using a config-driven WebSocket template."""

    _MIN_AUDIO_SEND_BYTES = 3200  # 100 ms of 16 kHz mono PCM; provider wants a few KB

    def __init__(self):
        registry = get_models_registry()
        if not registry:
            raise RuntimeError(
                "config.yml not found; cannot configure streaming STT provider"
            )
        model = registry.get_default("stt_stream")
        if not model:
            raise RuntimeError("No default stt_stream model defined in config.yml")
        self.model = model
        self._name = model.model_provider or model.name
        self._capabilities = set(model.capabilities) if model.capabilities else set()
        self._streams: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set:
        """Return provider capabilities from config.yml."""
        return self._capabilities

    async def transcribe(self, audio_data: bytes, sample_rate: int, **kwargs) -> dict:
        """Not used for streaming providers - use start_stream/process_audio_chunk/end_stream instead."""
        raise NotImplementedError(
            "Streaming providers do not support batch transcription"
        )

    async def start_stream(
        self, stream_id: str, sample_rate: int = 16000, diarize: bool = False
    ):
        base_url = self.model.resolved_url()
        ops = self.model.operations or {}

        # Build WebSocket URL with query parameters (for Deepgram streaming)
        query_params = ops.get("query", {})
        query_dict = dict(query_params) if query_params else {}

        # Override sample_rate if provided
        if sample_rate and "sample_rate" in query_dict:
            query_dict["sample_rate"] = sample_rate
        if diarize and "diarize" in query_dict:
            query_dict["diarize"] = "true"

        # Inject hot words for streaming — merge prompt registry + plugin keywords
        prompt_hot_words = ""
        try:
            registry = get_prompt_registry()
            prompt_hot_words = await registry.get_prompt("asr.hot_words")
        except Exception as e:
            logger.debug(f"Failed to fetch asr.hot_words for streaming: {e}")

        # Providers declare where their streaming protocol accepts recognition
        # hints: Deepgram uses the ``keyterm`` query parameter, while local
        # bridges can name a field on their JSON start message. Keeping this in
        # the registry operation prevents provider-specific client branches.
        _, streaming_hint = _resolve_asr_hint(
            self.model, self._capabilities, None, prompt_hot_words
        )

        if (
            CAP_KEYWORD_BOOSTING in self._capabilities
            and self.model.model_provider == "deepgram"
            and streaming_hint
        ):
            keyterm = _parse_hot_words_to_keyterm(streaming_hint)
            if keyterm:
                query_dict["keyterm"] = keyterm

        # Normalize boolean values to lowercase strings (Deepgram expects "true"/"false", not "True"/"False")
        normalized_query = {}
        for k, v in query_dict.items():
            if isinstance(v, bool):
                normalized_query[k] = "true" if v else "false"
            else:
                normalized_query[k] = v

        # Build query string with proper URL encoding (NO token in query)
        query_str = urlencode(normalized_query)
        url = f"{base_url}?{query_str}" if query_str else base_url

        # Debug: Log the URL
        logger.info(
            f"🔗 Connecting to streaming STT WebSocket [{self.model.model_provider}]: {url}"
        )

        # Connect to WebSocket with Authorization header
        headers = {}
        if self.model.api_key:
            auth_prefix = ops.get("auth_prefix") or "Token"
            headers["Authorization"] = f"{auth_prefix} {self.model.api_key}"

        ws = await websockets.connect(url, additional_headers=headers)

        # Send start message if required by provider
        start_msg = (ops.get("start", {}) or {}).get("message", {})
        if start_msg:
            # Inject session_id if placeholder present
            start_msg = json.loads(json.dumps(start_msg))  # deep copy
            start_msg.setdefault("session_id", stream_id)
            context_field = (ops.get("start", {}) or {}).get("context_field")
            if context_field and streaming_hint:
                start_msg[context_field] = streaming_hint
            # Apply sample rate and diarization if present
            if "config" in start_msg and isinstance(start_msg["config"], dict):
                start_msg["config"].setdefault("sample_rate", sample_rate)
                if diarize:
                    start_msg["config"]["diarize"] = True
            await ws.send(json.dumps(start_msg))

            # Wait for confirmation; non-fatal if not provided
            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except Exception:
                pass

        self._streams[stream_id] = {
            "ws": ws,
            "sample_rate": sample_rate,
            "final": None,
            "interim": [],
            "pending_audio": bytearray(),
        }

    async def process_audio_chunk(
        self, stream_id: str, audio_chunk: bytes
    ) -> dict | None:
        if stream_id not in self._streams:
            return None
        ws = self._streams[stream_id]["ws"]
        ops = self.model.operations or {}

        # Audio V2 emits atomic 20 ms / 640-byte frames. Waiting up to 50 ms for a
        # provider response after every frame throttles the uplink below real time.
        # Coalesce to the provider's documented few-kilobyte cadence first.
        pending = self._streams[stream_id]["pending_audio"]
        pending.extend(audio_chunk)
        if len(pending) < self._MIN_AUDIO_SEND_BYTES:
            return None
        audio_chunk = bytes(pending)
        pending.clear()

        # Send chunk header if required (for providers like Parakeet)
        chunk_hdr = (ops.get("chunk_header", {}) or {}).get("message", {})
        if chunk_hdr:
            hdr = json.loads(json.dumps(chunk_hdr))
            hdr.setdefault("type", "audio_chunk")
            hdr.setdefault("session_id", stream_id)
            hdr.setdefault("rate", self._streams[stream_id]["sample_rate"])
            await ws.send(json.dumps(hdr))

        # Send audio chunk (raw bytes for Deepgram, or after header for others)
        await ws.send(audio_chunk)

        # Non-blocking read for results
        expect = ops.get("expect", {}) or {}
        extract = expect.get("extract", {})
        interim_type = expect.get("interim_type")
        final_type = expect.get("final_type")

        try:
            # Try to read a message (non-blocking)
            msg = await asyncio.wait_for(ws.recv(), timeout=0.05)
            data = json.loads(msg)

            # Determine if this is interim or final result
            is_final = False
            if final_type and data.get("type") == final_type:
                is_final = data.get("is_final", False)
            elif interim_type and data.get("type") == interim_type:
                is_final = data.get("is_final", False)
            else:
                # Fallback: check is_final directly (for providers that don't use a type field)
                is_final = data.get("is_final", False)

            # Extract result data (guard against None from _dotted_get)
            text = (
                _dotted_get(data, extract.get("text"))
                if extract.get("text")
                else data.get("text", "")
            ) or ""
            words = (
                _dotted_get(data, extract.get("words"))
                if extract.get("words")
                else data.get("words", [])
            ) or []
            segments = (
                _dotted_get(data, extract.get("segments"))
                if extract.get("segments")
                else data.get("segments", [])
            ) or []
            segments = _normalize_provider_segments(segments)

            # Calculate confidence if available
            confidence = data.get("confidence", 0.0)
            if not confidence and words and isinstance(words, list):
                # Calculate average word confidence
                confidences = [
                    w.get("confidence", 0.0)
                    for w in words
                    if isinstance(w, dict) and "confidence" in w
                ]
                if confidences:
                    confidence = sum(confidences) / len(confidences)

            # Return result with is_final flag
            # Consumer decides what to do with interim vs final
            return {
                "text": text,
                "words": words,
                "segments": segments,
                "is_final": is_final,
                "confidence": confidence,
            }

        except asyncio.TimeoutError:
            # No message available yet
            return None
        except (ConnectionError, OSError, websockets.exceptions.ConnectionClosed):
            # Let the consumer reconnect; a dead transport is different from a
            # healthy socket that simply has no transcript available yet.
            raise
        except Exception as e:
            logger.error(f"Error processing audio chunk result for {stream_id}: {e}")
            return None

    async def end_stream(self, stream_id: str) -> dict:
        if stream_id not in self._streams:
            return {"text": "", "words": [], "segments": []}
        ws = self._streams[stream_id]["ws"]
        ops = self.model.operations or {}
        pending = self._streams[stream_id].get("pending_audio")
        if pending:
            await ws.send(bytes(pending))
            pending.clear()
        end_msg = (ops.get("end", {}) or {}).get("message", {"type": "stop"})
        await ws.send(json.dumps(end_msg))

        expect = ops.get("expect", {}) or {}
        final_type = expect.get("final_type")
        extract = expect.get("extract", {})

        final = None
        try:
            # Drain until final or close
            for _ in range(500):  # hard cap
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                data = json.loads(msg)
                if final_type and data.get("type") == final_type:
                    final = data
                    break
                elif not final_type and (data.get("is_final") or data.get("is_last")):
                    final = data
                    break
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

        self._streams.pop(stream_id, None)

        if not isinstance(final, dict):
            return {"text": "", "words": [], "segments": []}
        segments = (
            _dotted_get(final, extract.get("segments"))
            if extract
            else final.get("segments", [])
        ) or []
        return {
            "text": (
                _dotted_get(final, extract.get("text"))
                if extract
                else final.get("text", "")
            ),
            "words": (
                _dotted_get(final, extract.get("words"))
                if extract
                else final.get("words", [])
            ),
            "segments": _normalize_provider_segments(segments),
        }

    async def health_check(self) -> dict:
        """Check streaming STT service by attempting a WebSocket handshake."""
        base_url = self.model.resolved_url()
        ops = self.model.operations or {}
        headers = {}
        if self.model.api_key:
            auth_prefix = ops.get("auth_prefix") or "Token"
            headers["Authorization"] = f"{auth_prefix} {self.model.api_key}"

        try:
            ws = await asyncio.wait_for(
                websockets.connect(base_url, additional_headers=headers),
                timeout=5.0,
            )
            await ws.close()
            return {"status": "✅ Connected", "healthy": True}
        except asyncio.TimeoutError:
            return {"status": "❌ Connection Timeout", "healthy": False}
        except websockets.exceptions.InvalidStatus as e:
            code = getattr(e, "status_code", None) or getattr(
                getattr(e, "response", None), "status_code", None
            )
            if code in (401, 403):
                return {"status": "❌ Auth Failed — check API key", "healthy": False}
            return {"status": f"❌ HTTP {code or 'unknown'}", "healthy": False}
        except (OSError, ConnectionRefusedError):
            return {
                "status": "❌ Connection Failed — service unreachable",
                "healthy": False,
            }
        except Exception as e:
            return {"status": f"❌ Error: {e}", "healthy": False}


def get_transcription_provider(
    provider_name: Optional[str] = None,
    mode: Optional[str] = None,
    *,
    allow_fallback: bool = True,
) -> Optional[BaseTranscriptionProvider]:
    """Return a registry-driven transcription provider.

    - mode="batch": HTTP-based STT (default)
    - mode="streaming": WebSocket-based STT

    Note: The models registry returns None when config.yml is missing or invalid.
    We avoid broad exception handling here and simply return None when the
    required defaults are not configured.
    """
    registry = get_models_registry()
    if not registry:
        return None

    selected_mode = (mode or "batch").lower()
    if selected_mode == "streaming":
        if not registry.get_default("stt_stream"):
            return None
        return RegistryStreamingTranscriptionProvider()

    # batch mode
    if not registry.get_default("stt"):
        return None
    return RegistryBatchTranscriptionProvider(
        model_name=None if allow_fallback else registry.get_default("stt").name
    )


def is_transcription_available(mode: str = "batch") -> bool:
    """Check if transcription provider is available for given mode.

    Args:
        mode: Either "batch" or "streaming"

    Returns:
        True if a transcription provider is configured and available, False otherwise
    """
    provider = get_transcription_provider(mode=mode)
    return provider is not None


def get_mock_transcription_provider(
    fail_mode: bool = False,
) -> BaseTranscriptionProvider:
    """Return a mock transcription provider (for testing only).

    Args:
        fail_mode: If True, transcribe() will raise an exception to simulate transcription failure

    Returns:
        MockTranscriptionProvider instance
    """
    # Lazy import: test/mock-only provider
    from .mock_provider import MockTranscriptionProvider

    return MockTranscriptionProvider(fail_mode=fail_mode)


__all__ = [
    "get_transcription_provider",
    "is_transcription_available",
    "get_mock_transcription_provider",
    "RegistryBatchTranscriptionProvider",
    "RegistryStreamingTranscriptionProvider",
    "BaseTranscriptionProvider",
    "BatchTranscriptionProvider",
    "StreamingTranscriptionProvider",
]
