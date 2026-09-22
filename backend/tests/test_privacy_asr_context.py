"""ASR training/context privacy; all examples and external calls are synthetic."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401

from backend.services import privacy
from backend.services.transcription import context
from backend.workers import finetuning_jobs as jobs
from backend.workers import transcription_jobs


class Redis:
    def __init__(self):
        self.values = {}
        self.get = AsyncMock(side_effect=lambda key: self.values.get(key))
        self.set = AsyncMock(side_effect=self.write)
        self.close = AsyncMock()
        self.aclose = AsyncMock()

    async def write(self, key, value, **kwargs):
        self.values[key] = value


@pytest.fixture
async def training(evidence, monkeypatch):
    redis = Redis()
    monkeypatch.setattr(jobs, "create_async_redis", lambda **kwargs: redis)
    model = SimpleNamespace(
        model_url="http://synthetic-asr", resolved_url=lambda: "http://synthetic-asr"
    )
    monkeypatch.setattr(
        jobs,
        "get_models_registry",
        lambda: SimpleNamespace(get_default=lambda _: model),
    )
    decode = AsyncMock(return_value=b"synthetic audio")
    monkeypatch.setattr(jobs, "reconstruct_wav_from_conversation", decode)
    post = AsyncMock(return_value=SimpleNamespace(status_code=200))

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **kwargs: Client())
    return SimpleNamespace(redis=redis, decode=decode, post=post)


@pytest.mark.parametrize("stage", ["entry", "decode", "context", "post"])
async def test_training_checks_before_audio_external_call_and_completion(
    evidence, training, stage
):
    if stage != "entry":
        await allow(evidence)
    if stage == "decode":

        async def decode(*args):
            await revoke(evidence)
            return b"synthetic audio"

        training.decode.side_effect = decode
    if stage == "context":

        async def get(*args):
            await revoke(evidence)
            return "Synthetic vocabulary"

        training.redis.get.side_effect = get
    if stage == "post":

        async def post(*args, **kwargs):
            await revoke(evidence)
            return SimpleNamespace(status_code=200)

        training.post.side_effect = post
    result = await jobs.run_asr_finetuning_job()
    assert result["privacy_held"] == 1
    assert result["annotations_consumed"] == 0
    evidence.annotation.save.assert_not_awaited()
    if stage == "entry":
        training.decode.assert_not_awaited()
    if stage != "post":
        training.post.assert_not_awaited()


async def test_training_allows_audio_but_ignores_unversioned_jargon(evidence, training):
    await allow(evidence)
    training.redis.values["asr:jargon:evidence-owner"] = "Synthetic stale vocabulary"
    result = await jobs.run_asr_finetuning_job()
    assert result["conversations_exported"] == 1
    assert result["annotations_consumed"] == 1
    payload = training.post.call_args.kwargs
    assert "Synthetic stale vocabulary" not in payload["data"]["labels"]
    assert (
        json.loads(payload["data"]["labels"])[0]["segments"][0]["text"]
        == "Synthetic text"
    )
    assert payload["files"][0][1][1].getvalue() == b"synthetic audio"


async def test_cached_jargon_is_bound_to_current_revision(evidence, monkeypatch):
    await allow(evidence)
    redis = Redis()
    monkeypatch.setattr(context, "create_async_redis", lambda **kwargs: redis)
    monkeypatch.setattr(
        context,
        "get_prompt_registry",
        lambda: SimpleNamespace(get_prompt=AsyncMock(return_value="Static vocabulary")),
    )
    snapshot = await privacy.load_snapshot("evidence-owner")
    redis.values[context.jargon_cache_key("evidence-owner", snapshot)] = json.dumps(
        {"text": "Synthetic vocabulary", "privacy_reference_receipt": []}
    )
    result = await context.gather_transcription_context("evidence-owner")
    assert result.user_jargon == "Synthetic vocabulary"
    assert len(result.privacy_checks) == 1
    assert "Synthetic vocabulary" not in json.dumps(result.to_metadata())
    await revoke(evidence)
    result = await context.gather_transcription_context("evidence-owner")
    assert result.user_jargon == ""
    assert result.combined == "Static vocabulary"


async def test_stale_context_stops_at_real_transcription_entry(evidence, monkeypatch):
    await allow(evidence)
    snapshot = await privacy.load_snapshot("evidence-owner")
    await revoke(evidence)
    provider = AsyncMock(side_effect=AssertionError("Must not resolve provider"))
    monkeypatch.setattr(transcription_jobs, "get_transcription_provider", provider)
    with pytest.raises(privacy.PrivacyHeld):
        await transcription_jobs.transcribe_audio_range(
            None,
            context_info="Synthetic vocabulary",
            context_privacy_checks=[("evidence-owner", snapshot)],
        )
    provider.assert_not_called()


@pytest.mark.parametrize("stage", ["allowed", "memory", "llm", "cache"])
async def test_jargon_cron_fences_generation_and_cache_write(
    evidence, monkeypatch, stage, caplog
):
    await allow(evidence)
    redis = Redis()
    monkeypatch.setattr(jobs, "create_async_redis", lambda **kwargs: redis)
    monkeypatch.setattr(
        jobs,
        "User",
        SimpleNamespace(
            find_all=lambda: SimpleNamespace(
                to_list=AsyncMock(return_value=[SimpleNamespace(id="evidence-owner")])
            )
        ),
    )

    async def memories(**kwargs):
        if stage == "memory":
            await revoke(evidence)
        return [
            SimpleNamespace(
                id="Conversations/synthetic-recording.md", content="Synthetic memory"
            )
        ]

    monkeypatch.setattr(
        jobs, "get_memory_service", lambda: SimpleNamespace(get_all_memories=memories)
    )
    monkeypatch.setattr(
        jobs,
        "get_prompt_registry",
        lambda: SimpleNamespace(get_prompt=AsyncMock(return_value="Synthetic prompt")),
    )

    async def generate(*args):
        if stage == "llm":
            await revoke(evidence)
        return "Synthetic vocabulary"

    generate_mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(jobs, "async_generate", generate_mock)
    if stage == "cache":

        async def write(key, value, **kwargs):
            await redis.write(key, value, **kwargs)
            await revoke(evidence)

        redis.set.side_effect = write
    result = await jobs.run_asr_jargon_extraction_job()
    assert result["users_processed"] == (1 if stage == "allowed" else 0)
    assert "Synthetic vocabulary" not in caplog.text
    if stage == "memory":
        generate_mock.assert_not_awaited()
    if stage in {"memory", "llm"}:
        redis.set.assert_not_awaited()
    if stage == "cache":
        text, _, _ = await context.cached_jargon("evidence-owner", redis)
        assert text == ""


@pytest.fixture
async def provider(evidence, monkeypatch):
    from backend.services import transcription

    instance = transcription.RegistryBatchTranscriptionProvider.__new__(
        transcription.RegistryBatchTranscriptionProvider
    )
    instance.model = SimpleNamespace(
        model_provider="synthetic",
        name="synthetic",
        model_dump=lambda: {"model_provider": "synthetic", "name": "synthetic"},
    )
    instance._name = "synthetic"
    instance._allow_fallback = False
    instance._transcribe_uncached = AsyncMock(return_value={"text": "Synthetic result"})
    monkeypatch.setattr(
        transcription,
        "Conversation",
        SimpleNamespace(
            get_pymongo_collection=lambda: SimpleNamespace(database=evidence.db)
        ),
    )
    return instance


async def test_response_cache_uses_exact_context_and_preserves_duplicates(
    evidence, provider
):
    await provider.transcribe(
        b"synthetic audio", 16000, context_info="Synthetic vocabulary"
    )
    await provider.transcribe(
        b"synthetic audio", 16000, context_info="Synthetic vocabulary"
    )
    assert provider._transcribe_uncached.await_count == 1
    await provider.transcribe(b"synthetic audio", 16000, context_info="")
    assert provider._transcribe_uncached.await_count == 2
    assert await evidence.db.transcription_response_cache.count_documents({}) == 2


@pytest.mark.parametrize(
    "stage", ["cache_lookup", "provider_result", "privacy_failure"]
)
async def test_provider_fences_cache_results_and_never_falls_back_on_hold(
    evidence, provider, stage, monkeypatch
):
    await allow(evidence)
    snapshot = await privacy.load_snapshot("evidence-owner")
    from backend.services import transcription

    monkeypatch.setattr(
        transcription,
        "get_models_registry",
        lambda: pytest.fail("A privacy hold must not trigger fallback"),
    )
    if stage == "cache_lookup":

        async def lookup(*args, **kwargs):
            await revoke(evidence)
            return None, None, {"text": "Synthetic cached result"}

        provider._lookup_cached_transcription = lookup
    elif stage == "provider_result":

        async def transcribe(*args, **kwargs):
            await revoke(evidence)
            return {"text": "Synthetic result"}

        provider._transcribe_uncached.side_effect = transcribe
    else:
        provider._transcribe_uncached.side_effect = privacy.PrivacyHeld()
    with pytest.raises(privacy.PrivacyHeld):
        await provider.transcribe(
            b"synthetic audio", 16000, privacy_checks=[("evidence-owner", snapshot)]
        )
    assert await evidence.db.transcription_response_cache.count_documents({}) == 0
    if stage == "cache_lookup":
        provider._transcribe_uncached.assert_not_awaited()


async def test_provider_rechecks_after_prompt_lookup_before_http(
    evidence, provider, monkeypatch
):
    from backend.services import transcription

    await allow(evidence)
    snapshot = await privacy.load_snapshot("evidence-owner")
    provider.model.operations = {}
    provider.model.api_key = None
    provider.model.resolved_url = lambda: "http://synthetic-asr"
    provider._capabilities = set()

    async def prompt(*args):
        await revoke(evidence)
        return "Synthetic vocabulary"

    monkeypatch.setattr(
        transcription, "get_prompt_registry", lambda: SimpleNamespace(get_prompt=prompt)
    )
    monkeypatch.setattr(transcription, "_get_plugin_keywords", lambda: [])
    post = AsyncMock(side_effect=AssertionError("Held content must not reach HTTP"))

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(transcription.httpx, "AsyncClient", lambda **kwargs: Client())
    with pytest.raises(privacy.PrivacyHeld):
        await transcription.RegistryBatchTranscriptionProvider._transcribe_uncached(
            provider,
            b"synthetic audio",
            16000,
            privacy_checks=[("evidence-owner", snapshot)],
        )
    post.assert_not_awaited()
