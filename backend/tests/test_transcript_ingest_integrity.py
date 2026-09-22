import io
import json
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from backend.services import forced_alignment
from backend.services import transcription as transcription_service
from backend.services.forced_alignment import estimate_words_from_segment_timing
from backend.services.transcript_integrity import (
    TranscriptTimingError,
    validate_and_normalize_transcript_timing,
)

_EMPTY_GALLERY = {
    "catalog_id": "d" * 32,
    "revision": "a" * 64,
    "unverified_speaker_ids": [],
    "held_speaker_ids": [],
    "active_operation_ids": [],
}


async def checked_gallery_result(result):
    from backend.services.speaker_gallery_privacy import GalleryRead, GalleryResult

    client = SimpleNamespace(
        service_url="http://synthetic-speaker.invalid",
        enrollment_catalog=AsyncMock(return_value=dict(_EMPTY_GALLERY)),
    )
    scope = GalleryRead(client, user_id="user")
    await scope.start()
    return GalleryResult(result, scope)


def wav_bytes(frames=800):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def test_alignment_url_requires_explicit_model_capability(monkeypatch):
    model = SimpleNamespace(
        capabilities=["transcription"],
        resolved_url=lambda: "https://api.smallest.ai/v1/transcribe",
    )
    registry = SimpleNamespace(get_default=lambda model_type: model)
    monkeypatch.setattr(forced_alignment, "get_models_registry", lambda: registry)

    assert forced_alignment._align_url() == ""


def test_alignment_url_uses_capable_local_asr_origin(monkeypatch):
    model = SimpleNamespace(
        capabilities=["transcription", "forced_alignment"],
        resolved_url=lambda: "http://rainbow:8767/transcribe?format=json",
    )
    registry = SimpleNamespace(get_default=lambda model_type: model)
    monkeypatch.setattr(forced_alignment, "get_models_registry", lambda: registry)

    assert forced_alignment._align_url() == "http://rainbow:8767/align"


def test_speaker_batches_are_bounded_by_count_and_total_duration():
    # Imported here so this test can address private batching behavior.
    from backend import speaker_recognition_client as module

    segments = [
        {"start": 0.0, "end": 100.0},
        {"start": 100.0, "end": 200.0},
        {"start": 200.0, "end": 300.0},
    ]

    assert module._pack_identification_batches(
        segments,
        [0, 1, 2],
        max_items=32,
        max_seconds=220.0,
    ) == [[0, 1], [2]]


def test_small_provider_overhang_is_clipped_before_storage():
    segments = [{"start": 8.0, "end": 10.7, "text": "last words"}]
    words = [{"start": 9.5, "end": 10.4, "word": "words"}]

    clean_segments, clean_words = validate_and_normalize_transcript_timing(
        segments, words, audio_duration=10.0
    )

    assert clean_segments[0]["end"] == 10.0
    assert clean_words[0]["end"] == 10.0
    # The paid-provider response remains untouched for the content-hash cache.
    assert segments[0]["end"] == 10.7
    assert words[0]["end"] == 10.4


def test_tiny_fully_outside_tail_item_is_discarded():
    segments, words = validate_and_normalize_transcript_timing(
        [{"start": 0.0, "end": 10.0, "text": "speech"}],
        [{"start": 10.002, "end": 10.004, "word": "tail"}],
        audio_duration=10.0,
    )

    assert len(segments) == 1
    assert words == []


def test_stale_transcript_clock_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 868.72, "end": 885.9, "text": "stale"}],
            [],
            audio_duration=130.0,
        )

    assert raised.value.code == "transcript_timing_out_of_bounds"
    assert raised.value.details["audio_duration"] == 130.0
    assert raised.value.details["max_timing"] == 885.9


def test_committed_transcription_cassettes_fit_their_audio_duration():
    cassette_dir = Path(__file__).parents[2] / "tests" / "cassettes"

    for cassette_path in sorted(cassette_dir.glob("*.json")):
        cassette = json.loads(cassette_path.read_text())
        batch = cassette["batch"]
        validate_and_normalize_transcript_timing(
            batch.get("segments", []),
            batch.get("words", []),
            audio_duration=cassette["duration_seconds"],
        )


def test_invalid_segment_order_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 4.0, "end": 3.0, "text": "backwards"}],
            [],
            audio_duration=10.0,
        )

    assert raised.value.code == "transcript_timing_invalid_range"


def test_segment_without_backing_chunk_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 100.94, "end": 102.36, "text": "phantom"}],
            [],
            audio_duration=141.25,
            audio_ranges=[(0.0, 98.0)],
        )

    assert raised.value.code == "transcript_audio_gap"


@pytest.mark.asyncio
async def test_speaker_wrapper_reports_audio_range_failure_as_data_error(monkeypatch):
    # Imported here so monkeypatch targets the module-level dependency.
    from backend import speaker_recognition_client as module

    async def bad_range(*_args, **_kwargs):
        raise ValueError(
            "Invalid time range: end_time (130.0s) must be > start_time (868.72s)"
        )

    monkeypatch.setattr(module, "reconstruct_audio_ranges", bad_range)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = AsyncMock()

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=[{"start": 868.72, "end": 885.9, "text": "stale", "speaker": "A"}],
        speech_segments=[{"start": 868.72, "end": 885.9}],
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    assert result["error"] == "transcript_data_error"
    assert result["segments"][0]["status"] == "data_error"
    assert "Speaker service" not in result["message"]
    client.identify_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_wrapper_does_not_send_empty_wav_to_service(monkeypatch):
    # Imported here so monkeypatch targets the module-level dependency.
    from backend import speaker_recognition_client as module

    async def empty_wav(*_args, **_kwargs):
        return b"RIFF" + (b"\x00" * 40)

    identify = AsyncMock()

    async def empty_wavs(*_args, **_kwargs):
        return [await empty_wav()]

    monkeypatch.setattr(module, "reconstruct_audio_ranges", empty_wavs)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = identify

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=[{"start": 100.94, "end": 102.36, "text": "gap", "speaker": "A"}],
        speech_segments=[{"start": 100.94, "end": 102.36}],
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    assert result["error"] == "transcript_data_error"
    identify.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_wrapper_reconstructs_and_identifies_segments_in_one_batch(
    monkeypatch,
):
    # Imported here so monkeypatch targets the module-level dependency.
    from backend import speaker_recognition_client as module

    reconstruct = AsyncMock(return_value=[wav_bytes(), wav_bytes()])
    identify_batch = AsyncMock(
        return_value={
            "results": [
                {
                    "segment_id": "0",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                    "embedding": [0.1, 0.2],
                    "embedding_model": "wespeaker-test",
                },
                {
                    "segment_id": "1",
                    "found": False,
                    "speaker_name": None,
                    "confidence": 0.4,
                    "status": "unknown",
                },
            ]
        }
    )
    monkeypatch.setattr(module, "reconstruct_audio_ranges", reconstruct)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = identify_batch
    segments = [
        {"start": 1.0, "end": 3.0, "text": "one", "speaker": "A"},
        {"start": 4.0, "end": 6.0, "text": "two", "speaker": "B"},
    ]

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=segments,
        speech_segments=segments,
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    reconstruct.assert_awaited_once_with(
        "conversation",
        [(1.0, 3.0), (4.0, 6.0)],
    )
    identify_batch.assert_awaited_once()
    assert [segment["status"] for segment in result["segments"]] == [
        "identified",
        "unknown",
    ]
    assert result["segments"][0]["identified_as"] == "Aryan"
    assert result["segments"][0]["_evaluation_embedding"] == [0.1, 0.2]
    assert result["segments"][0]["_embedding_model"] == "wespeaker-test"


@pytest.mark.asyncio
async def test_speaker_majority_vote_reconstructs_and_identifies_samples_in_one_batch(
    monkeypatch,
):
    # Imported here so monkeypatch targets the module-level dependency.
    from backend import speaker_recognition_client as module

    reconstruct = AsyncMock(return_value=[wav_bytes(), wav_bytes()])
    identify_batch = AsyncMock(
        return_value={
            "results": [
                {
                    "segment_id": "0",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                },
                {
                    "segment_id": "1",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.8,
                    "status": "identified",
                },
            ]
        }
    )
    monkeypatch.setattr(module, "reconstruct_audio_ranges", reconstruct)
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"similarity_threshold": 0.5},
    )
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.enabled = True
    client.service_url = "http://synthetic-speaker.invalid"
    client.enrollment_catalog = AsyncMock(return_value=dict(_EMPTY_GALLERY))
    client.identify_batch = identify_batch
    segments = [
        {"start": 1.0, "end": 4.0, "text": "one", "speaker": "A"},
        {"start": 5.0, "end": 7.0, "text": "two", "speaker": "A"},
    ]

    result = await client.identify_provider_segments(
        "conversation",
        segments,
        user_id="user",
        min_segment_duration=0.1,
    )

    reconstruct.assert_awaited_once_with(
        "conversation",
        [(1.0, 4.0), (5.0, 7.0)],
    )
    identify_batch.assert_awaited_once()
    assert [segment["identified_as"] for segment in result["segments"]] == [
        "Aryan",
        "Aryan",
    ]


@pytest.mark.asyncio
async def test_transcription_job_quarantines_bad_cached_timing(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import transcription_jobs as module

    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=130.0,
        transcript_integrity_error=None,
        save=AsyncMock(),
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(), find_one=AsyncMock(return_value=conversation)
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(
        module,
        "gather_transcription_context",
        AsyncMock(
            return_value=SimpleNamespace(
                combined="",
                hot_words="",
                user_jargon="",
                privacy_checks=[],
                reference_receipt=[],
            )
        ),
    )
    transcribe = AsyncMock(
        return_value={
            "text": "stale",
            "segments": [{"start": 868.72, "end": 885.9, "text": "stale"}],
            "words": [],
            "provider_name": "smallest",
            "provider_capabilities": {"diarization": True},
            "wav_size": 123,
        }
    )
    monkeypatch.setattr(module, "transcribe_audio_range", transcribe)
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"diarization_source": "pyannote"},
    )
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 130.0)]),
    )
    record_event = Mock()
    monkeypatch.setattr(module, "record_event_sync", record_event)

    with pytest.raises(TranscriptTimingError):
        await module.transcribe_full_audio_job.__wrapped__(
            "conversation",
            "version",
            trigger="reprocess",
            provider_model_name="stt-faster-whisper",
        )

    assert conversation.transcript_integrity_error.startswith(
        "transcript_timing_out_of_bounds:"
    )
    conversation.save.assert_awaited_once()
    record_event.assert_called_once()
    assert record_event.call_args.kwargs["category"] == "data_integrity"
    assert transcribe.await_args.kwargs["provider_model_name"] == "stt-faster-whisper"
    assert transcribe.await_args.kwargs["diarize"] is False


@pytest.mark.asyncio
async def test_transcribe_audio_range_can_select_explicit_registry_model(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import transcription_jobs as module

    provider = SimpleNamespace(
        name="faster-whisper",
        get_capabilities_dict=lambda: {"word_timestamps": True, "segments": True},
        transcribe=AsyncMock(
            return_value={
                "text": "hello",
                "segments": [{"start": 0.0, "end": 0.04, "text": "hello"}],
                "words": [
                    {
                        "word": "hello",
                        "start": 0.0,
                        "end": 0.04,
                        "confidence": 0.9,
                    }
                ],
            }
        ),
    )
    explicit_provider = Mock(return_value=provider)
    default_provider = Mock(side_effect=AssertionError("default provider was selected"))
    monkeypatch.setattr(module, "RegistryBatchTranscriptionProvider", explicit_provider)
    monkeypatch.setattr(module, "get_transcription_provider", default_provider)
    monkeypatch.setattr(
        module,
        "reconstruct_wav_from_conversation",
        AsyncMock(return_value=wav_bytes()),
    )
    monkeypatch.setattr(
        module,
        "condense_silence",
        lambda pcm, sample_rate, channels, sample_width: (pcm, None, 0.04),
    )

    result = await module.transcribe_audio_range(
        "conversation",
        provider_model_name="stt-faster-whisper",
    )

    explicit_provider.assert_called_once_with(model_name="stt-faster-whisper")
    default_provider.assert_not_called()
    provider.transcribe.assert_awaited_once()
    assert result["provider_name"] == "faster-whisper"
    assert result["words"][0]["word"] == "hello"


@pytest.mark.asyncio
async def test_streaming_provider_error_body_survives_context_close(monkeypatch):
    module = transcription_service

    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"detail":"CUDA runtime unavailable"}'

    response = httpx.Response(
        500,
        stream=ErrorBody(),
        request=httpx.Request("POST", "http://asr/transcribe"),
        headers={"content-type": "application/json"},
    )

    class StreamContext:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *args):
            await response.aclose()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return StreamContext()

    provider = object.__new__(module.RegistryBatchTranscriptionProvider)
    provider._name = "faster-whisper"
    provider._capabilities = set()
    provider._allow_fallback = False
    provider.model = SimpleNamespace(
        name="stt-faster-whisper",
        model_provider="faster-whisper",
        model_url="http://asr",
        api_key="",
        operations={
            "stt_transcribe": {
                "method": "POST",
                "path": "/transcribe",
                "content_type": "multipart/form-data",
            }
        },
        resolved_url=lambda: "http://asr",
    )
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError, match="CUDA runtime unavailable"):
        await provider._transcribe_uncached(
            b"RIFF-audio",
            16_000,
            context_info="known context",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("late_gallery_change", [False, True])
async def test_registered_speaker_job_runs_batch_provider_identification(
    monkeypatch, late_gallery_change
):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import speaker_jobs as module

    source_segment = module.Conversation.SpeakerSegment(
        start=1.0,
        end=4.0,
        text="hello there",
        speaker="SPEAKER_00",
    )
    version = module.Conversation.TranscriptVersion(
        version_id="source",
        transcript="hello there",
        segments=[source_segment],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
        diarization_source="provider",
        metadata={"provider_capabilities": {"diarization": True}},
    )
    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_ranges=[],
        audio_total_duration=5.0,
        transcript_integrity_error=None,
        get_transcript_version=lambda version_id: version,
        active_transcript=version,
        save=AsyncMock(),
    )
    identify_provider_segments = AsyncMock(
        return_value={
            "segments": [
                {
                    "start": 1.0,
                    "end": 4.0,
                    "text": "hello there",
                    "speaker": "SPEAKER_00",
                    "identified_as": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                    "_evaluation_embedding": [1.0, 0.0],
                }
            ]
        }
    )
    identify_provider_segments.return_value = await checked_gallery_result(
        identify_provider_segments.return_value
    )
    fake_client = SimpleNamespace(
        enabled=True,
        identify_provider_segments=identify_provider_segments,
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: fake_client)
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 5.0)]),
    )
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"diarization_source": "provider"},
    )
    monkeypatch.setattr(
        module,
        "get_misc_settings",
        lambda: {"per_segment_speaker_id": True},
    )
    monkeypatch.setattr(module, "get_user_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        module, "_human_speaker_annotations", AsyncMock(return_value=[])
    )
    compute_cluster_centroids = AsyncMock(return_value=({}, {}))
    monkeypatch.setattr(module, "compute_cluster_centroids", compute_cluster_centroids)
    diarization_artifact = SimpleNamespace(artifact_id="diarization-artifact")
    transcript_revision = SimpleNamespace(revision_id="transcript-revision")
    persist_diarization = AsyncMock(return_value=diarization_artifact)
    persist_revision = AsyncMock(return_value=transcript_revision)
    resolve_transcript_artifacts = AsyncMock(return_value=["raw-transcript-artifact"])
    monkeypatch.setattr(module, "persist_diarization_artifact", persist_diarization)
    monkeypatch.setattr(module, "persist_conversation_revision", persist_revision)
    monkeypatch.setattr(
        module,
        "resolve_transcript_artifact_ids",
        resolve_transcript_artifacts,
    )

    if late_gallery_change:
        from backend.services import privacy

        scope = identify_provider_segments.return_value.gallery_scope

        async def changed_labels(*args, **kwargs):
            scope.client.enrollment_catalog.return_value = {
                **_EMPTY_GALLERY,
                "revision": "b" * 64,
            }
            return []

        monkeypatch.setattr(module, "_human_speaker_annotations", changed_labels)
        with pytest.raises(privacy.PrivacyHeld):
            await module.recognise_speakers_job.__wrapped__("conversation", "source")
        persist_diarization.assert_not_awaited()
        persist_revision.assert_not_awaited()
        conversation.save.assert_not_awaited()
        return

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "source",
    )

    assert result["success"] is True
    receipt = identify_provider_segments.return_value.gallery_scope.receipt()
    assert version.metadata["speaker_recognition"]["privacy_gallery_receipt"] == receipt
    assert (
        persist_diarization.await_args.kwargs["configuration"][
            "privacy_gallery_receipt"
        ]
        == receipt
    )
    assert result["identified_speakers"] == ["Aryan"]
    identify_provider_segments.assert_awaited_once_with(
        conversation_id="conversation",
        segments=[
            {
                "start": 1.0,
                "end": 4.0,
                "text": "hello there",
                "speaker": "SPEAKER_00",
            }
        ],
        user_id="user",
        per_segment=True,
        min_segment_duration=0.5,
    )
    conversation.save.assert_awaited_once()
    persist_diarization.assert_awaited_once()
    persist_revision.assert_awaited_once()
    assert persist_revision.await_args.kwargs["transcript_artifact_ids"] == [
        "raw-transcript-artifact"
    ]
    resolve_transcript_artifacts.assert_awaited_once_with("conversation", version)
    compute_cluster_centroids.assert_not_awaited()
    assert version.metadata["cluster_centroids"] == {"Aryan": [1.0, 0.0]}


@pytest.mark.asyncio
async def test_registered_speaker_job_skips_event_only_transcript(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import speaker_jobs as module

    version = module.Conversation.TranscriptVersion(
        version_id="source",
        transcript="[Silence]",
        segments=[
            module.Conversation.SpeakerSegment(
                start=0.0,
                end=5.0,
                text="[Silence]",
                speaker="",
                segment_type="event",
            )
        ],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
    )
    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=5.0,
        transcript_integrity_error=None,
        get_transcript_version=lambda version_id: version,
        active_transcript=version,
        save=AsyncMock(),
    )
    fake_client = SimpleNamespace(enabled=True)
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: fake_client)
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 5.0)]),
    )

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "target",
        source_version_id="source",
    )

    assert result["success"] is True
    assert result["skip_reason"] == "No speech segments to identify"
    conversation.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_job_derives_edge_timing_normalization_before_processing(
    monkeypatch,
):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import speaker_jobs as module

    source = module.Conversation.TranscriptVersion(
        version_id="smallest-raw",
        transcript="[Silence]",
        segments=[
            module.Conversation.SpeakerSegment(
                start=0.0,
                end=5.003,
                text="[Silence]",
                speaker="",
                segment_type="event",
            )
        ],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
    )
    normalized = source.model_copy(deep=True)
    normalized.version_id = "timing-normalized"
    normalized.segments[0].end = 5.0
    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=5.0,
        transcript_integrity_error=None,
        get_transcript_version=lambda version_id: source,
        active_transcript=source,
        save=AsyncMock(),
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    normalize = AsyncMock(return_value=normalized)
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(
        module, "SpeakerRecognitionClient", lambda: SimpleNamespace(enabled=True)
    )
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 5.0)]),
    )
    monkeypatch.setattr(module, "persist_timing_normalized_revision", normalize)

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "target",
        source_version_id="smallest-raw",
    )

    assert result["success"] is True
    assert result["skip_reason"] == "No speech segments to identify"
    normalize.assert_awaited_once()
    assert normalize.await_args.kwargs["segments"][0]["end"] == 5.0
    conversation.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_registered_speaker_job_persists_word_clock_before_empty_pyannote_fallback(
    monkeypatch,
):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from backend.workers import speaker_jobs as module

    transcript_version_type = module.Conversation.TranscriptVersion
    word_type = module.Conversation.Word
    source = transcript_version_type(
        version_id="provider-raw",
        transcript="hello there friend I",
        words=[],
        segments=[
            module.Conversation.SpeakerSegment(
                start=0.0,
                end=1.0,
                text="hello",
                speaker="SPEAKER_00",
            ),
            module.Conversation.SpeakerSegment(
                start=1.2,
                end=2.0,
                text="there friend",
                speaker="SPEAKER_00",
            ),
            module.Conversation.SpeakerSegment(
                start=5.0,
                end=5.2,
                text="I",
                speaker="SPEAKER_00",
            ),
        ],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
        diarization_source="provider",
        metadata={"provider_capabilities": {"diarization": True}},
    )

    class FakeConversation:
        def __init__(self):
            self.conversation_id = "conversation"
            self.user_id = "user"
            self.client_id = "client"
            self.audio_ranges = []
            self.audio_total_duration = 6.0
            self.transcript_integrity_error = None
            self.transcript_versions = [source]
            self.active_transcript_version = source.version_id
            self.save = AsyncMock()

        @property
        def active_transcript(self):
            return self.get_transcript_version(self.active_transcript_version)

        def get_transcript_version(self, version_id):
            return next(
                (
                    version
                    for version in self.transcript_versions
                    if version.version_id == version_id
                ),
                None,
            )

        def add_transcript_version(self, *, set_as_active=False, **kwargs):
            version = transcript_version_type(
                created_at=datetime.now(timezone.utc),
                **kwargs,
            )
            self.transcript_versions.append(version)
            if set_as_active:
                self.active_transcript_version = version.version_id
            return version

        def apply_status(self, **kwargs):
            return None

    conversation = FakeConversation()

    async def persist_word_clock(_conversation, source_version, **kwargs):
        derived = transcript_version_type(
            version_id="word-timed-derived",
            transcript=source_version.transcript,
            words=[word_type.model_validate(word) for word in kwargs["words"]],
            segments=source_version.segments,
            provider=source_version.provider,
            created_at=datetime.now(timezone.utc),
            diarization_source=source_version.diarization_source,
            metadata={
                "reprocessing_type": "word_timing_synthesis",
                "source_version_id": source_version.version_id,
            },
        )
        conversation.transcript_versions.append(derived)
        conversation.active_transcript_version = derived.version_id
        return derived

    async def identify_word_spans(**kwargs):
        return await checked_gallery_result(
            {
                "segments": [
                    {
                        **segment,
                        "identified_as": None,
                        "confidence": 0.0,
                    }
                    for segment in kwargs["segments"]
                ]
            }
        )

    client = SimpleNamespace(
        enabled=True,
        diarize_identify_match=AsyncMock(
            return_value={"segments": [], "diarization_model": "community-1"}
        ),
        identify_provider_segments=AsyncMock(side_effect=identify_word_spans),
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    persist_word_timing = AsyncMock(side_effect=persist_word_clock)
    persist_diarization = AsyncMock(
        return_value=SimpleNamespace(artifact_id="diarization-artifact")
    )
    persist_revision = AsyncMock(
        return_value=SimpleNamespace(revision_id="transcript-revision")
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: client)
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 6.0)]),
    )
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"diarization_source": "pyannote", "collar": 2.0},
    )
    monkeypatch.setattr(module, "get_misc_settings", lambda: {})
    monkeypatch.setattr(
        module,
        "synthesize_words_via_alignment",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(module, "persist_word_timed_revision", persist_word_timing)
    monkeypatch.setattr(
        module,
        "get_user_by_id",
        AsyncMock(return_value=SimpleNamespace(email="test@example.com")),
    )
    monkeypatch.setattr(module, "generate_jwt_for_user", lambda *args: "token")
    monkeypatch.setattr(
        module, "_apply_background_references", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        module, "_human_speaker_annotations", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        module, "compute_cluster_centroids", AsyncMock(return_value=({}, {}))
    )
    monkeypatch.setattr(
        module, "_compact_embedded_speaker_history", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(module, "persist_diarization_artifact", persist_diarization)
    monkeypatch.setattr(module, "persist_conversation_revision", persist_revision)
    monkeypatch.setattr(
        module,
        "resolve_transcript_artifact_ids",
        AsyncMock(return_value=["raw-transcript-artifact"]),
    )

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "speaker-target",
        source_version_id="provider-raw",
    )

    assert result["success"] is True
    persist_word_timing.assert_awaited_once()
    assert persist_word_timing.await_args.kwargs["method"] == "segment_clock_estimate"
    assert (
        len(client.diarize_identify_match.await_args.kwargs["transcript_data"]["words"])
        == 4
    )
    client.identify_provider_segments.assert_awaited_once()
    fallback_spans = client.identify_provider_segments.await_args.kwargs["segments"]
    assert [(span["start"], span["end"], span["text"]) for span in fallback_spans] == [
        (0.0, 2.0, "hello there friend"),
        (5.0, 5.2, "I"),
    ]
    target = conversation.get_transcript_version("speaker-target")
    assert [segment.text for segment in target.segments] == ["hello there friend", "I"]
    assert target.metadata["source_version_id"] == "word-timed-derived"
    assert target.metadata["diarization_fallback"] == {
        "mode": "word_timeline",
        "reason": "pyannote_empty",
    }
    assert target.diarization_source == "word_timeline_fallback"
    assert persist_diarization.await_args.kwargs["provider"] == "word_timeline_fallback"
    assert persist_revision.await_args.args[1] is target


def test_full_diarization_requires_continuous_audio_coverage():
    # Imported here to exercise the worker's exact continuity policy.
    from backend.workers import speaker_jobs as module

    assert module._audio_ranges_cover_continuously(
        [(0.0, 10.0), (10.1, 20.0)],
        duration=20.0,
    )
    assert not module._audio_ranges_cover_continuously(
        [(0.0, 10.0), (12.0, 20.0)],
        duration=20.0,
    )
    assert not module._audio_ranges_cover_continuously(
        [(2.0, 20.0)],
        duration=20.0,
    )


def test_segment_clock_word_estimates_preserve_text_and_bounds():
    words = estimate_words_from_segment_timing(
        [{"start": 10.0, "end": 14.0, "text": "one two"}]
    )

    assert words == [
        {"word": "one", "start": 10.0, "end": 12.0, "confidence": 0.0},
        {"word": "two", "start": 12.0, "end": 14.0, "confidence": 0.0},
    ]


def test_speaker_job_recovers_spoken_text_mislabeled_as_event():
    # Imported here to exercise the worker's exact speech classification policy.
    from backend.workers import speaker_jobs as module

    spoken = module.Conversation.SpeakerSegment(
        start=1.0,
        end=4.0,
        text="We should decide which base model to use next.",
        speaker="",
        segment_type="event",
    )
    silence = module.Conversation.SpeakerSegment(
        start=4.0,
        end=6.0,
        text="[Silence]",
        speaker="",
        segment_type="event",
    )

    assert module._is_speech_segment(spoken)
    assert not module._is_speech_segment(silence)
