"""The registered voice LLM operation cannot silently switch providers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest

from backend import llm_client


@pytest.mark.asyncio
async def test_voice_stream_failure_never_resolves_fallback_operation(monkeypatch):
    error = openai.APIConnectionError(
        request=httpx.Request("POST", "http://primary/chat/completions")
    )
    create = AsyncMock(side_effect=error)
    op = SimpleNamespace(
        get_client=lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
        to_api_params=lambda: {"model": "primary"},
        prepare_messages=lambda value: value,
        model_def=SimpleNamespace(model_provider="openai"),
        model_name="primary",
    )
    registry = SimpleNamespace(
        get_llm_operation=Mock(return_value=op), get_fallback_llm_operation=Mock()
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)
    with pytest.raises(openai.APIConnectionError):
        async for _ in llm_client.async_chat_with_tools_stream(
            [{"role": "user", "content": "Hello"}],
            operation="voice_conversation",
            allow_fallback=False,
        ):
            pytest.fail("A failed primary must not emit voice output")
    registry.get_llm_operation.assert_called_once_with("voice_conversation")
    registry.get_fallback_llm_operation.assert_not_called()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_exact_stt_uses_selected_provider_without_fallback(monkeypatch):
    from backend.services import transcription
    from backend.services.interaction_modes import committed_turns

    model = SimpleNamespace(
        name="selected-stt",
        model_type="stt",
        model_provider="smallest",
        capabilities=[],
    )
    fallback = Mock()
    registry = SimpleNamespace(
        get_default=lambda kind: model,
        get_by_name=Mock(return_value=model),
        defaults={"fallback_stt": "other-stt"},
    )
    monkeypatch.setattr(transcription, "get_models_registry", lambda: registry)
    provider = transcription.get_transcription_provider(
        mode="batch", allow_fallback=False
    )
    assert not provider._allow_fallback
    provider._lookup_cached_transcription = AsyncMock(return_value=(None, None, None))
    provider._transcribe_uncached = AsyncMock(
        side_effect=RuntimeError("selected STT unavailable")
    )
    factory = Mock(return_value=provider)
    monkeypatch.setattr(committed_turns, "get_transcription_provider", factory)
    assembler = committed_turns.CommittedTranscriptAssembler(
        object(), allow_provider_fallback=False
    )
    with pytest.raises(RuntimeError, match="selected STT unavailable"):
        await assembler.exact_transcriber(bytes(640), 16000, 1, 2)
    factory.assert_called_once_with(mode="batch", allow_fallback=False)
    registry.get_by_name.assert_called_once_with("selected-stt")
