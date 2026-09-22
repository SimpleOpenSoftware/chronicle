"""
Transcription-related RQ job functions.

This module contains all jobs related to speech-to-text transcription processing.
"""

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import time
import traceback
import wave
from datetime import datetime, timezone
from typing import Any, Dict

from pymongo.errors import DuplicateKeyError
from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job

import backend.services.privacy as privacy
from backend.config import (
    get_batch_chunk_seconds,
    get_diarization_settings,
    get_live_segmentation,
    get_streaming_fallback_timeout,
    require_speech_for_transcription,
)
from backend.constants import TITLE_NOT_GENERATED
from backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    enqueue_speech_detection,
    redis_conn,
    start_post_conversation_jobs,
    transcription_queue,
)
from backend.model_registry import get_models_registry
from backend.models.audio_capture import AudioRangeRef, TranscriptArtifact
from backend.models.conversation import Conversation, create_conversation
from backend.models.job import async_job
from backend.models.timeline import AudioEvidenceSpan
from backend.observability.otel_setup import (
    set_otel_session,
    set_span_attrs,
    set_trace_io,
    traced_job,
)
from backend.plugins.events import PluginEvent
from backend.services.audio_claims import (
    AudioClaimError,
    apply_audio_ranges,
    claim_entire_capture,
)
from backend.services.audio_stream import TranscriptionResultsAggregator
from backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
    SpeakerCheckStatus,
)
from backend.services.forced_alignment import align_audio_words
from backend.services.observability import record_event_sync
from backend.services.plugin_service import dispatch_or_defer_space_event
from backend.services.processing_artifacts import (
    persist_conversation_revision,
    persist_transcript_artifact,
)
from backend.services.sse_publisher import publish_sse_event_throttled
from backend.services.timeline.dirty_ranges import note_conversation_dirty
from backend.services.transcript_integrity import (
    TranscriptTimingError,
    load_transcript_audio_ranges,
    validate_and_normalize_transcript_timing,
)
from backend.services.transcription import (
    RegistryBatchTranscriptionProvider,
    get_transcription_provider,
    is_transcription_available,
)
from backend.services.transcription.context import gather_transcription_context
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import (
    reconstruct_audio_segment,
    reconstruct_wav_from_claims,
    reconstruct_wav_from_conversation,
)
from backend.utils.conversation_utils import analyze_speech, mark_conversation_deleted
from backend.utils.job_utils import check_job_alive, update_job_meta
from backend.utils.segment_utils import classify_segment_text
from backend.utils.silence_condense import condense_silence, remap_condensed_result
from backend.utils.vad_analysis import detect_speech_pcm

from .conversation_jobs import open_conversation_job
from .speaker_jobs import check_enrolled_speakers_job

logger = logging.getLogger(__name__)


async def _settle_audio_evidence_span(
    conversation: Conversation, word_count: int, state: str
) -> None:
    if conversation.external_source_type != "screenpipe":
        return
    span = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.conversation_id == conversation.conversation_id
    )
    if span is None:
        return
    span.word_count = word_count
    span.state = state
    await span.save()


async def apply_speaker_recognition(
    audio_path: str,
    transcript_text: str,
    words: list,
    segments: list,
    user_id: str,
    conversation_id: str | None = None,
) -> list:
    """
    Apply speaker recognition to segments using the speaker recognition service.

    This is a reusable helper function that can be called from any job.

    Args:
        audio_path: Path to the audio file
        transcript_text: Full transcript text
        words: Word-level timing data
        segments: List of Conversation.SpeakerSegment objects
        user_id: User ID
        conversation_id: Optional conversation ID for logging

    Returns:
        Updated list of segments with identified speakers
    """
    try:
        speaker_client = SpeakerRecognitionClient()
        if not speaker_client.enabled:
            logger.info(
                f"🎤 Speaker recognition disabled, using original speaker labels"
            )
            return segments

        logger.info(
            f"🎤 Speaker recognition enabled, identifying speakers{f' for {conversation_id}' if conversation_id else ''}..."
        )

        # Prepare transcript data with word-level timings
        transcript_data = {"text": transcript_text, "words": words}

        # Call speaker recognition service to match and identify speakers
        speaker_result = await speaker_client.diarize_identify_match(
            audio_path=audio_path, transcript_data=transcript_data, user_id=user_id
        )

        if not speaker_result or "segments" not in speaker_result:
            logger.info(
                f"🎤 Speaker recognition returned no segments, keeping original transcription segments"
            )
            return segments

        speaker_identified_segments = speaker_result["segments"]
        logger.info(
            f"🎤 Speaker recognition returned {len(speaker_identified_segments)} identified segments"
        )
        logger.info(f"🎤 Original segments: {len(segments)}")

        # Create time-based speaker mapping
        def get_speaker_at_time(timestamp: float, speaker_segments: list) -> str:
            """Get the identified speaker active at a given timestamp."""
            for seg in speaker_segments:
                seg_start = seg.get("start", 0.0)
                seg_end = seg.get("end", 0.0)
                if seg_start <= timestamp <= seg_end:
                    return seg.get("identified_as") or seg.get("speaker", "Unknown")
            return None

        # Update each segment's speaker based on its timestamp
        updated_count = 0
        for seg in segments:
            seg_mid = (seg.start + seg.end) / 2.0
            identified_speaker = get_speaker_at_time(
                seg_mid, speaker_identified_segments
            )

            if identified_speaker and identified_speaker != "Unknown":
                original_speaker = seg.speaker
                seg.speaker = identified_speaker
                updated_count += 1
                logger.debug(
                    f"🎤   Segment [{seg.start:.1f}-{seg.end:.1f}] '{original_speaker}' -> '{identified_speaker}'"
                )

        # Ensure segments remain sorted by start time
        segments.sort(key=lambda s: s.start)
        logger.info(
            f"🎤 Updated {updated_count}/{len(segments)} segments with speaker identifications"
        )

        return segments

    except Exception as speaker_error:
        logger.warning(f"⚠️ Speaker recognition failed: {speaker_error}")
        logger.warning(f"Continuing with original transcription speaker labels")
        logger.debug(traceback.format_exc())
        return segments


BATCH_CHUNK_SECONDS = 3600  # Hard cap even when configuration is more permissive
BATCH_CHUNK_OVERLAP_SECONDS = 5.0

# No-activity watchdog: only treat "zero transcription results" as a provider failure
# when audio is actually flowing in. A connected-but-quiet device (common right after a
# conversation ends and speech detection re-arms) produces no audio chunks, so zero
# results is expected, not a fault. If the last audio chunk arrived more than this many
# seconds ago the inflow is considered idle and the watchdog stays its hand.
AUDIO_INFLOW_IDLE_SECONDS = 30


def _needs_forced_alignment(words: list[dict]) -> bool:
    """Return whether provider 'words' are really timestamped multi-word phrases."""
    return not words or any(
        len(str(word.get("word", "")).split()) > 1 for word in words
    )


def _owned_window_items(
    items: list[dict],
    *,
    window_start: float,
    ownership_start: float,
    ownership_end: float,
    final_window: bool,
    clip_to_ownership: bool,
) -> list[dict]:
    """Offset timed provider items and assign overlap to exactly one window."""
    owned: list[dict] = []
    for source in items:
        item = dict(source)
        start = float(item.get("start", 0.0)) + window_start
        end = float(item.get("end", item.get("start", 0.0))) + window_start
        midpoint = (start + end) / 2.0
        if midpoint < ownership_start:
            continue
        if midpoint > ownership_end or (midpoint == ownership_end and not final_window):
            continue
        if clip_to_ownership:
            start = max(start, ownership_start)
            end = min(end, ownership_end)
            if end <= start:
                continue
        item["start"] = start
        item["end"] = end
        owned.append(item)
    return owned


def _word_text(word: dict) -> str:
    return str(
        word.get("punctuated_word") or word.get("word") or word.get("text") or ""
    ).strip()


def _segments_for_owned_window(
    segments: list[dict],
    owned_words: list[dict],
    *,
    window_start: float,
    ownership_start: float,
    ownership_end: float,
    final_window: bool,
) -> list[dict]:
    """Build seam-safe segments whose text and bounds contain their owned words."""
    if not owned_words:
        return _owned_window_items(
            segments,
            window_start=window_start,
            ownership_start=ownership_start,
            ownership_end=ownership_end,
            final_window=final_window,
            clip_to_ownership=True,
        )

    offset_segments = []
    for source in segments:
        segment = dict(source)
        segment["start"] = float(segment.get("start", 0.0)) + window_start
        segment["end"] = (
            float(segment.get("end", segment.get("start", 0.0))) + window_start
        )
        offset_segments.append(segment)

    coherent: list[dict] = []
    assigned_word_ids: set[int] = set()
    for segment in offset_segments:
        words = [
            word
            for word in owned_words
            if id(word) not in assigned_word_ids
            and float(segment["start"])
            <= (float(word.get("start", 0.0)) + float(word.get("end", 0.0))) / 2.0
            <= float(segment["end"])
        ]
        if words:
            item = dict(segment)
            item["start"] = min(float(word.get("start", 0.0)) for word in words)
            item["end"] = max(float(word.get("end", 0.0)) for word in words)
            item["text"] = " ".join(filter(None, (_word_text(word) for word in words)))
            coherent.append(item)
            assigned_word_ids.update(id(word) for word in words)
            continue

        if classify_segment_text(str(segment.get("text", ""))) == "event":
            midpoint = (float(segment["start"]) + float(segment["end"])) / 2.0
            if ownership_start <= midpoint and (
                midpoint < ownership_end or (final_window and midpoint <= ownership_end)
            ):
                item = dict(segment)
                item["start"] = max(float(item["start"]), ownership_start)
                item["end"] = min(float(item["end"]), ownership_end)
                if item["end"] > item["start"]:
                    coherent.append(item)

    # Providers occasionally emit a timed word just outside every utterance. Keep
    # that evidence renderable instead of silently losing it at the stitch seam.
    for word in owned_words:
        if id(word) in assigned_word_ids:
            continue
        speaker = "Speaker 0"
        midpoint = (float(word.get("start", 0.0)) + float(word.get("end", 0.0))) / 2.0
        for segment in offset_segments:
            if float(segment["start"]) <= midpoint <= float(segment["end"]):
                speaker = segment.get("speaker", speaker)
                break
        coherent.append(
            {
                "start": float(word.get("start", 0.0)),
                "end": float(word.get("end", 0.0)),
                "text": _word_text(word),
                "speaker": speaker,
            }
        )

    coherent.sort(key=lambda segment: (segment["start"], segment["end"]))
    return coherent


async def _align_result_words(result: dict, wav_data: bytes) -> dict:
    """Replace absent/phrase-level word timing with acoustic forced alignment."""
    words = result.get("words", [])
    if not _needs_forced_alignment(words):
        return result

    # Whisper long-form output may expose its timestamped decoding windows through
    # the words field when the fine-tuned model has no alignment-head metadata.
    # Those windows are much better alignment boundaries than one full-file segment.
    phrase_windows = [
        {
            "text": word.get("word", ""),
            "start": word.get("start", 0.0),
            "end": word.get("end", word.get("start", 0.0)),
        }
        for word in words
        if str(word.get("word", "")).strip()
        and word.get("end", word.get("start", 0.0)) > word.get("start", 0.0)
    ]
    alignment_segments = phrase_windows or result.get("segments", [])
    aligned_words = await align_audio_words(wav_data, alignment_segments)
    if aligned_words:
        result["words"] = aligned_words
    return result


def _build_wav(
    pcm_data: bytes, sample_rate: int, channels: int, sample_width: int
) -> bytes:
    """Build a WAV file from raw PCM data."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def transcribe_audio_range(
    conversation_id: str | None,
    start_time: float = 0.0,
    end_time: float | None = None,
    diarize: bool = True,
    context_info: str | None = None,
    progress_callback=None,
    audio_ranges: list[AudioRangeRef] | None = None,
    provider_model_name: str | None = None,
    context_privacy_checks: list | None = None,
) -> dict:
    """
    Reconstruct audio for a time range and transcribe it.

    Pure reconstruction + transcription — no DB writes, no plugins, no speech validation.
    Audio above the configured provider ceiling is split into overlapping windows
    and merged back onto the conversation presentation clock.

    Args:
        conversation_id: Conversation ID, or None when transcribing raw capture claims
        start_time: Start time in seconds (default 0.0)
        end_time: End time in seconds (None = full audio)
        diarize: Whether to request diarization
        context_info: ASR context hints
        progress_callback: Optional callback for batch progress
        audio_ranges: Immutable capture claims to transcribe instead of a Conversation
        provider_model_name: Explicit configured STT model; default uses the registry default

    Returns:
        Dict with text, segments, words, provider_name, provider_capabilities, wav_size, sample_rate
    """

    privacy_checks = list(context_privacy_checks or [])
    for owner, snapshot in privacy_checks:
        await privacy.assert_current(owner, snapshot)
    privacy_checks.extend(
        await privacy.require_audio_ranges(audio_ranges) if audio_ranges else []
    )
    if conversation_id:
        checked = await privacy.require_conversation(conversation_id)
        if checked:
            owner_row = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            privacy_checks.append((owner_row.user_id, checked))
    provider = (
        RegistryBatchTranscriptionProvider(model_name=provider_model_name)
        if provider_model_name
        else get_transcription_provider(mode="batch")
    )
    if not provider:
        raise ValueError("No batch transcription provider available")

    # Reconstruct audio
    if audio_ranges is not None:
        wav_data = await reconstruct_wav_from_claims(
            audio_ranges, start_time=start_time, end_time=end_time
        )
    elif conversation_id is None:
        raise ValueError("conversation_id or audio_ranges is required")
    elif start_time == 0.0 and end_time is None:
        wav_data = await reconstruct_wav_from_conversation(conversation_id)
    else:
        if end_time is None:
            # Get total duration from conversation
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if not conversation:
                raise ValueError(f"Conversation {conversation_id} not found")
            end_time = conversation.audio_total_duration or 0.0
        wav_data = await reconstruct_audio_segment(
            conversation_id, start_time, end_time
        )

    logger.info(
        f"📦 Reconstructed audio [{start_time:.1f}s - {end_time or 'end'}]: "
        f"{len(wav_data) / 1024 / 1024:.2f} MB"
    )

    # Read WAV header to get audio properties
    try:
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            actual_sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            n_frames = wf.getnframes()
            pcm_data = wf.readframes(n_frames)
    except Exception:
        actual_sample_rate = 16000
        channels = 1
        sample_width = 2
        # Strip WAV header (44 bytes) as fallback
        pcm_data = wav_data[44:] if len(wav_data) > 44 else wav_data

    duration = (
        len(pcm_data) / (actual_sample_rate * sample_width * channels)
        if (actual_sample_rate * sample_width * channels) > 0
        else 0
    )

    provider_capabilities = {}
    if hasattr(provider, "get_capabilities_dict"):
        provider_capabilities = provider.get_capabilities_dict()
    if not diarize:
        # Describe what this response contains, rather than every feature the
        # configured model could have produced in a different request.
        provider_capabilities = dict(provider_capabilities)
        provider_capabilities.pop("diarization", None)

    # Batch providers bill by audio duration, silence included — cut long
    # silences with the local VAD before sending, and map timestamps back to
    # the original timeline afterwards (see utils/silence_condense.py).
    condense_map = None
    condensed_pcm, mapping, speech_seconds = condense_silence(
        pcm_data, actual_sample_rate, channels, sample_width
    )
    no_speech = mapping is not None and not mapping
    if not no_speech and mapping is None and speech_seconds is None:
        # condense_silence never scored the audio (clip under MIN_AUDIO_SECONDS
        # or VAD failure). With the audio-filtering gate on, still require
        # speech before paying for transcription. Only a conclusive silent/empty
        # result rejects; unscored audio fails open.
        if require_speech_for_transcription():
            speech_detection = detect_speech_pcm(
                pcm_data,
                actual_sample_rate,
                channels,
                sample_width,
            )
            no_speech = speech_detection.should_reject
    if no_speech:
        logger.info(
            f"🔇 No speech detected in [{start_time:.1f}s - {end_time or 'end'}] "
            f"of {(conversation_id or 'capture')[:8]} — skipping paid transcription entirely"
        )
        return {
            "text": "",
            "segments": [],
            "words": [],
            "provider_name": provider.name,
            "provider_capabilities": provider_capabilities,
            "wav_size": 0,
            "sample_rate": actual_sample_rate,
        }
    if mapping is not None:
        condense_map = mapping
        pcm_data = condensed_pcm
        wav_data = _build_wav(pcm_data, actual_sample_rate, channels, sample_width)
        duration = (
            len(pcm_data) / (actual_sample_rate * sample_width * channels)
            if (actual_sample_rate * sample_width * channels) > 0
            else 0
        )

    chunk_seconds = min(
        float(BATCH_CHUNK_SECONDS),
        get_batch_chunk_seconds(provider.name),
    )

    if duration <= chunk_seconds:
        # Single chunk — transcribe directly
        transcribe_kwargs: dict = {
            "audio_data": wav_data,
            "sample_rate": actual_sample_rate,
            "diarize": diarize,
        }
        if progress_callback:
            transcribe_kwargs["progress_callback"] = progress_callback
        if context_info:
            transcribe_kwargs["context_info"] = context_info

        try:
            for privacy_owner, privacy_snapshot in privacy_checks:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
            transcribe_kwargs["privacy_checks"] = privacy_checks
            result = await provider.transcribe(**transcribe_kwargs)
            for privacy_owner, privacy_snapshot in privacy_checks:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
        except ConnectionError as e:
            raise RuntimeError(str(e))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Transcription failed ({type(e).__name__}): {e}")

        result = await _align_result_words(result, wav_data)
        if condense_map:
            result = remap_condensed_result(result, condense_map)
        return {
            "text": result.get("text", ""),
            "segments": result.get("segments", []),
            "words": result.get("words", []),
            "provider_name": provider.name,
            "provider_capabilities": provider_capabilities,
            "wav_size": len(wav_data),
            "sample_rate": actual_sample_rate,
        }

    # A previously successful whole-file request may predate a newly lowered
    # provider ceiling. Preserve that paid result before changing the audio hashes
    # by partitioning it into windows. This lookup must never call the provider.
    cached_lookup = getattr(provider, "get_cached_transcription", None)
    if cached_lookup is not None:
        cached_result = await cached_lookup(
            wav_data,
            actual_sample_rate,
            diarize=diarize,
            context_info=context_info,
        )
        for privacy_owner, privacy_snapshot in privacy_checks:
            await privacy.assert_current(privacy_owner, privacy_snapshot)
        if cached_result is not None:
            cached_result = await _align_result_words(cached_result, wav_data)
            if condense_map:
                cached_result = remap_condensed_result(cached_result, condense_map)
            for privacy_owner, privacy_snapshot in privacy_checks:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
            return {
                "text": cached_result.get("text", ""),
                "segments": cached_result.get("segments", []),
                "words": cached_result.get("words", []),
                "provider_name": provider.name,
                "provider_capabilities": provider_capabilities,
                "wav_size": len(wav_data),
                "sample_rate": actual_sample_rate,
            }

    # Multi-chunk: use bounded overlapping windows. Midpoint ownership assigns
    # every provider word/segment to exactly one side of each seam, while the
    # overlap gives the recognizer enough acoustic context for boundary words.
    frame_bytes = sample_width * channels
    chunk_size_bytes = int(chunk_seconds * actual_sample_rate) * frame_bytes
    overlap_seconds = min(
        BATCH_CHUNK_OVERLAP_SECONDS,
        chunk_seconds / 4.0,
    )
    overlap_bytes = int(overlap_seconds * actual_sample_rate) * frame_bytes
    step_bytes = chunk_size_bytes - overlap_bytes
    if step_bytes <= 0:
        raise ValueError("Batch transcription chunk overlap must be below its ceiling")

    all_segments = []
    all_words = []
    total_wav_size = 0
    windows: list[tuple[int, int]] = []
    window_start_byte = 0
    while window_start_byte < len(pcm_data):
        window_end_byte = min(window_start_byte + chunk_size_bytes, len(pcm_data))
        windows.append((window_start_byte, window_end_byte))
        if window_end_byte >= len(pcm_data):
            break
        window_start_byte += step_bytes

    logger.info(
        f"📦 Audio duration {duration:.0f}s exceeds the {provider.name} "
        f"request ceiling ({chunk_seconds:.0f}s); splitting into {len(windows)} "
        f"windows with {overlap_seconds:.1f}s overlap"
    )

    for index, (window_start_byte, window_end_byte) in enumerate(windows):
        chunk_pcm = pcm_data[window_start_byte:window_end_byte]
        chunk_wav = _build_wav(chunk_pcm, actual_sample_rate, channels, sample_width)
        chunk_start = window_start_byte / (actual_sample_rate * frame_bytes)
        chunk_end = window_end_byte / (actual_sample_rate * frame_bytes)

        logger.info(
            f"📦 Transcribing chunk {index + 1}/{len(windows)}: "
            f"[{chunk_start:.0f}s - {chunk_end:.0f}s]"
        )

        transcribe_kwargs = {
            "audio_data": chunk_wav,
            "sample_rate": actual_sample_rate,
            "diarize": diarize,
        }
        if progress_callback:
            transcribe_kwargs["progress_callback"] = progress_callback
        if context_info:
            transcribe_kwargs["context_info"] = context_info

        try:
            for privacy_owner, privacy_snapshot in privacy_checks:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
            transcribe_kwargs["privacy_checks"] = privacy_checks
            result = await provider.transcribe(**transcribe_kwargs)
            for privacy_owner, privacy_snapshot in privacy_checks:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
        except ConnectionError as e:
            raise RuntimeError(str(e))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Transcription failed ({type(e).__name__}): {e}")

        result = await _align_result_words(result, chunk_wav)
        ownership_start = (
            chunk_start
            if index == 0
            else (
                chunk_start + windows[index - 1][1] / (actual_sample_rate * frame_bytes)
            )
            / 2.0
        )
        ownership_end = (
            chunk_end
            if index == len(windows) - 1
            else (
                chunk_end + windows[index + 1][0] / (actual_sample_rate * frame_bytes)
            )
            / 2.0
        )
        owned_words = _owned_window_items(
            result.get("words", []),
            window_start=chunk_start,
            ownership_start=ownership_start,
            ownership_end=ownership_end,
            final_window=index == len(windows) - 1,
            clip_to_ownership=False,
        )
        all_words.extend(owned_words)
        all_segments.extend(
            _segments_for_owned_window(
                result.get("segments", []),
                owned_words,
                window_start=chunk_start,
                ownership_start=ownership_start,
                ownership_end=ownership_end,
                final_window=index == len(windows) - 1,
            )
        )
        total_wav_size += len(chunk_wav)

    merged_text = " ".join(filter(None, (_word_text(word) for word in all_words)))
    if not merged_text:
        merged_text = " ".join(
            str(segment.get("text", "")).strip()
            for segment in all_segments
            if str(segment.get("text", "")).strip()
        )
    merged = {
        "text": merged_text,
        "segments": all_segments,
        "words": all_words,
        "provider_name": provider.name,
        "provider_capabilities": provider_capabilities,
        "wav_size": total_wav_size,
        "sample_rate": actual_sample_rate,
    }
    # Chunk offsets above are condensed-timeline; map back to original time.
    if condense_map:
        remap_condensed_result(merged, condense_map)
    return merged


async def process_transcription_result(
    conversation_id: str,
    version_id: str,
    trigger: str,
    transcript_text: str,
    segments: list,
    words: list,
    provider_name: str,
    provider_capabilities: dict,
    wav_size: int,
    processing_time: float,
    user_id: str | None = None,
    client_id: str | None = None,
    transcript_artifact: TranscriptArtifact | None = None,
    context_reference_receipt: list[str] | None = None,
) -> dict:
    """
    Post-transcription processing: plugin dispatch, speech validation, segment processing,
    transcript version creation, DB save, job metadata update.

    Returns:
        Dict with processing results including transcript data for downstream jobs
    """

    await privacy.require_conversation(conversation_id)
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    if user_id is None:
        user_id = str(conversation.user_id) if conversation.user_id else None
    context_visibility = privacy.ConversationPrivacyFilter()
    context_provenance = {}
    if context_reference_receipt:
        await context_visibility.require_reference_receipt(
            str(user_id), context_reference_receipt
        )
        context_provenance = {"privacy_reference_receipt": context_reference_receipt}
    if client_id is None:
        client_id = (
            conversation.client_id if hasattr(conversation, "client_id") else None
        )

    # The provider response has already crossed the content-hash cache boundary in
    # RegistryBatchTranscriptionProvider.transcribe(). Validate a copy before any
    # plugin sees it or it becomes active: cache the paid result, never cache corruption
    # into the conversation timeline.
    try:
        audio_ranges = await load_transcript_audio_ranges(conversation_id)
        segments, words = validate_and_normalize_transcript_timing(
            segments,
            words,
            audio_duration=conversation.audio_total_duration or 0.0,
            audio_ranges=audio_ranges,
        )
    except TranscriptTimingError as error:
        reason = f"{error.code}: {error}"
        conversation.transcript_integrity_error = reason
        await conversation.save()
        details = {
            **error.details,
            "provider": provider_name,
            "trigger": trigger,
            "version_id": version_id,
        }
        record_event_sync(
            severity="error",
            category="data_integrity",
            source="transcription_ingest",
            title="Transcript timing rejected",
            detail=reason,
            user_id=user_id,
            client_id=client_id,
            conversation_id=conversation_id,
            metadata=details,
            incident_key=f"transcript-integrity:{conversation_id}",
        )
        logger.warning(
            "Rejected %s transcript for %s before activation: %s",
            provider_name,
            conversation_id,
            reason,
        )
        raise
    conversation.transcript_integrity_error = None

    if transcript_artifact is None:
        transcript_artifact = await persist_transcript_artifact(
            user_id=str(conversation.user_id),
            audio_ranges=conversation.audio_ranges,
            retry_key=f"transcription:{conversation_id}:{version_id}",
            provider=provider_name.lower() if provider_name else "unknown",
            model=provider_name or None,
            transcript=transcript_text,
            words=words,
            segments=segments,
            raw_response={
                **context_provenance,
                "provider_capabilities": provider_capabilities,
                "trigger": trigger,
                "wav_size": wav_size,
            },
        )

    # Guard: a batch / re-transcription that comes back without usable structure
    # (no words AND no segments) must NOT replace a good existing transcript.
    # nemo batch occasionally returns bare text with no word/segment timing; promoting
    # it as the active version silently drops diarization and playback alignment from
    # the streaming version that was already there. Skip instead of clobbering — only
    # real content (with timing) should become the active version. An actual provider
    # error is a different path (it raises upstream); an empty result is not an error.
    new_has_text = bool((transcript_text or "").strip())
    new_has_structure = bool(words) or bool(segments)
    existing = conversation.active_transcript
    existing_is_populated = bool(
        existing
        and (existing.words or existing.segments)
        and (existing.transcript or "").strip()
    )
    evidence_backfill_has_text = bool(
        trigger == "vault_rebuild_evidence"
        and existing
        and (existing.transcript or "").strip()
    )
    if (not new_has_text or not new_has_structure) and (
        existing_is_populated or evidence_backfill_has_text
    ):
        logger.warning(
            f"⚠️ {provider_name} '{trigger}' for {conversation_id} returned an empty/"
            f"contentless transcript (text={new_has_text}, words={len(words)}, "
            f"segments={len(segments)}); keeping existing active version "
            f"'{existing.version_id}' instead of replacing it."
        )
        return {
            "success": False,
            "skipped": True,
            "reason": "empty_or_contentless_transcription",
            "conversation_id": conversation_id,
            "version_id": version_id,
            "kept_active_version": existing.version_id,
        }

    # Trigger transcript-level plugins BEFORE speech validation
    await context_visibility.assert_current()
    if transcript_text and not conversation.memory_excluded:
        try:
            await dispatch_or_defer_space_event(
                event=PluginEvent.TRANSCRIPT_BATCH,
                user_id=user_id,
                memory_space_id=conversation.memory_space_id,
                source_kind="conversation",
                source_id=conversation_id,
                data={
                    "transcript": transcript_text,
                    "segment_id": f"{conversation_id}_batch",
                    "conversation_id": conversation_id,
                    "segments": segments,
                    "word_count": len(words),
                },
                metadata={"client_id": client_id, **context_provenance},
                description=f"conversation={conversation_id[:12]}, words={len(words)}",
            )
        except privacy.PrivacyHeld:
            raise
        except Exception as e:
            logger.exception(
                f"⚠️ Error triggering transcript plugins in batch mode: {e}"
            )
    elif transcript_text:
        logger.info(
            f"Skipping transcript plugins for memory-excluded conversation {conversation_id[:8]}"
        )

    # Validate meaningful speech
    transcript_data = {"text": transcript_text, "words": words}
    speech_analysis = analyze_speech(transcript_data)

    if not speech_analysis.get("has_speech", False):
        logger.warning(
            f"⚠️ No meaningful speech for conversation {conversation_id}: "
            f"{speech_analysis.get('reason', 'unknown')}"
        )
        await mark_conversation_deleted(
            conversation_id=conversation_id,
            deletion_reason="no_meaningful_speech_batch_transcription",
        )
        await _settle_audio_evidence_span(
            conversation,
            int(speech_analysis.get("word_count", 0)),
            "no_speech",
        )

        # Cancel dependent jobs
        current_job = get_current_job()
        if current_job:
            # Include event_complete: it depends on the memory/summary bundle, and
            # cancelling those (enqueue_dependents=False) would otherwise strand the
            # finalizer in the deferred registry forever. mark_conversation_deleted
            # already settled the status, so the finalizer is redundant here.
            job_patterns = [
                f"crop_{conversation_id[:12]}",
                f"speaker_{conversation_id[:12]}",
                f"memory_{conversation_id[:12]}",
                f"title_{conversation_id[:12]}",
                f"short_summary_{conversation_id[:12]}",
                f"detailed_summary_{conversation_id[:12]}",
                f"event_complete_{conversation_id[:12]}",
            ]
            cancelled_jobs = []
            for job_id in job_patterns:
                try:
                    dependent_job = Job.fetch(job_id, connection=redis_conn)
                    if dependent_job and dependent_job.get_status() in [
                        "queued",
                        "deferred",
                        "scheduled",
                    ]:
                        dependent_job.cancel()
                        cancelled_jobs.append(job_id)
                        logger.info(f"✅ Cancelled dependent job: {job_id}")
                except Exception as e:
                    if isinstance(e, NoSuchJobError):
                        logger.debug(f"Job {job_id} hash not found")
                    else:
                        logger.debug(
                            f"Job {job_id} not found or already completed: {e}"
                        )

            if cancelled_jobs:
                logger.info(
                    f"🚫 Cancelled {len(cancelled_jobs)} dependent jobs due to no meaningful speech"
                )

        return {
            "success": False,
            "conversation_id": conversation_id,
            "error": "no_meaningful_speech",
            "reason": speech_analysis.get("reason"),
            "word_count": speech_analysis.get("word_count", 0),
            "duration": speech_analysis.get("duration", 0.0),
            "deleted": True,
        }

    logger.info(
        f"✅ Meaningful speech validated: {speech_analysis.get('word_count')} words, "
        f"{speech_analysis.get('duration', 0):.1f}s"
    )

    # Get provider capabilities for downstream processing decisions
    provider_has_diarization = provider_capabilities.get("diarization", False)

    # Build speaker segments
    speaker_segments = []
    diarization_source = None
    segments_created_by = "speaker_service"

    if segments:
        speaker_segments = []
        for seg in segments:
            raw_speaker = seg.get("speaker")
            if raw_speaker is None:
                speaker = "Speaker 0"
            elif isinstance(raw_speaker, int):
                speaker = f"Speaker {raw_speaker}"
            else:
                speaker = str(raw_speaker)

            text = seg.get("text", "")
            classification = classify_segment_text(text)
            seg_type = "speech"
            if classification == "event":
                seg_type = "event"
                speaker = ""

            speaker_segments.append(
                Conversation.SpeakerSegment(
                    speaker=speaker,
                    start=seg.get("start", 0.0),
                    end=seg.get("end", 0.0),
                    text=text,
                    segment_type=seg_type,
                )
            )

        if provider_has_diarization:
            diarization_source = "provider"
            segments_created_by = "provider_diarization"
        else:
            segments_created_by = "provider"
    elif transcript_text and transcript_text.strip():
        # No provider segments: emit a single Speaker 0 segment spanning the whole
        # transcript so any non-empty result is always renderable in the UI (mirrors
        # the streaming path). Use word timings when present, else span 0..end. If
        # speaker recognition is enabled, recognize_speakers_job refines this later —
        # but we no longer *rely* on it (a word-less batch result, e.g. nemotron's
        # prompt-model decode, used to leave the conversation with 0 segments).
        start_time_audio = words[0].get("start", 0.0) if words else 0.0
        end_time_audio = words[-1].get("end", 0.0) if words else 0.0
        speaker_segments = [
            Conversation.SpeakerSegment(
                speaker="Speaker 0",
                start=start_time_audio,
                end=end_time_audio,
                text=transcript_text,
            )
        ]
        segments_created_by = "fallback"

    # Add new transcript version
    provider_normalized = provider_name.lower() if provider_name else "unknown"

    word_objects = [
        Conversation.Word(
            word=w.get("word", ""),
            start=w.get("start", 0.0),
            end=w.get("end", 0.0),
            confidence=w.get("confidence"),
        )
        for w in words
    ]

    metadata = {
        **context_provenance,
        "trigger": trigger,
        "audio_file_size": wav_size,
        "word_count": len(words),
        "segments_created_by": segments_created_by,
        "provider_capabilities": provider_capabilities,
        "transcript_artifact_id": transcript_artifact.artifact_id,
    }

    new_version = conversation.add_transcript_version(
        version_id=version_id,
        transcript=transcript_text,
        words=word_objects,
        segments=speaker_segments,
        provider=provider_normalized,
        model=provider_name,
        processing_time_seconds=processing_time,
        metadata=metadata,
        set_as_active=True,
    )

    if diarization_source:
        new_version.diarization_source = diarization_source

    if not transcript_text or len(transcript_text.strip()) == 0:
        conversation.title = TITLE_NOT_GENERATED
        conversation.summary = "No speech detected"

    async with context_visibility.publication():
        await conversation.save()
    await _settle_audio_evidence_span(conversation, len(words), "transcribed")

    # Trim the conversation's silence now that its transcript is attached and active.
    # This is where continuous capture lands: a 30-minute ScreenPipe recording is
    # mostly silence, and it must be trimmed BEFORE the post-conversation chain so
    # speaker recognition reads the same timeline the audio now has. The trim re-times
    # the active version in place, so re-read it for the result below.
    # Lazy import: circular dependency — conversation_jobs imports transcription_jobs.
    from backend.workers.conversation_jobs import maybe_trim_silence

    if await maybe_trim_silence(conversation_id) is not None:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        trimmed = next(
            (
                v
                for v in (conversation.transcript_versions if conversation else [])
                if v.version_id == version_id
            ),
            None,
        )
        # Trimming narrows the conversation's audio claims, so the interval worth
        # reconciling changed even though no new evidence arrived.
        await note_conversation_dirty(
            conversation_id, "silence_trim", source_revision=version_id
        )
        if trimmed is not None:
            speaker_segments = trimmed.segments or []
            words = [word.model_dump() for word in (trimmed.words or [])]
            transcript_text = trimmed.transcript or ""

    if conversation is None:
        raise ValueError(
            f"Conversation {conversation_id} disappeared after transcription"
        )
    revision_version = conversation.get_transcript_version(version_id)
    if revision_version is None:
        raise ValueError(
            f"Transcript version {version_id} disappeared after transcription"
        )
    async with context_visibility.publication():
        revision = await persist_conversation_revision(
            conversation,
            revision_version,
            retry_key=f"transcript-projection:{conversation_id}:{version_id}",
            transcript_artifact_ids=[transcript_artifact.artifact_id],
        )
        await conversation.save()

    await note_conversation_dirty(
        conversation_id,
        "transcript_revision",
        source_revision=revision.revision_id,
        source_kind="transcript",
    )

    logger.info(
        f"✅ Transcript processing completed for {conversation_id} in {processing_time:.2f}s"
    )

    update_job_meta(
        conversation_id=conversation_id,
        title=conversation.title,
        summary=conversation.summary,
        transcript_length=len(transcript_text),
        word_count=len(words),
        processing_time=processing_time,
    )

    result = {
        "success": True,
        "conversation_id": conversation_id,
        "version_id": version_id,
        "transcript_artifact_id": transcript_artifact.artifact_id,
        "transcript_revision_id": revision.revision_id,
        "audio_source": "mongodb_chunks",
        "transcript": transcript_text,
        "segments": [seg.model_dump() for seg in speaker_segments],
        "words": words,
        "provider": provider_name,
        "provider_capabilities": provider_capabilities,
        "diarization_source": diarization_source,
        "processing_time_seconds": processing_time,
        "trigger": trigger,
    }
    set_trace_io(
        output={
            "word_count": len(words),
            "segment_count": len(speaker_segments),
            "provider": provider_name,
        }
    )
    await context_visibility.assert_current()
    return result


@async_job(redis=True, beanie=True)
@traced_job("transcription", pipeline_stage="transcription")
async def transcribe_full_audio_job(
    conversation_id: str,
    version_id: str,
    trigger: str = "reprocess",
    *,
    provider_model_name: str | None = None,
    redis_client=None,
) -> Dict[str, Any]:
    """
    RQ job function for transcribing full audio to text (transcription only, no speaker recognition).

    This job:
    1. Reconstructs audio from MongoDB chunks
    2. Transcribes audio to text with generic speaker labels (Speaker 0, Speaker 1, etc.)
    3. Generates title and summary
    4. Saves transcript version to conversation
    5. Returns results for downstream jobs (speaker recognition, memory)

    Speaker recognition is handled by a separate job (recognise_speakers_job).

    Args:
        conversation_id: Conversation ID
        version_id: Version ID for new transcript
        trigger: Trigger source
        provider_model_name: Explicit configured STT model; default uses the registry default
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results including transcript data for next job
    """
    set_otel_session(conversation_id)
    logger.info(
        f"🔄 RQ: Starting transcript processing for conversation {conversation_id} (trigger: {trigger})"
    )

    start_time_wall = time.time()

    # Get the conversation for user context
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        raise ValueError(f"Conversation {conversation_id} not found")

    user_id = str(conversation.user_id) if conversation.user_id else None
    client_id = conversation.client_id if hasattr(conversation, "client_id") else None
    set_span_attrs(user_id=user_id, client_id=client_id)
    set_trace_io(
        input={
            "conversation_id": conversation_id,
            "version_id": version_id,
            "trigger": trigger,
            "provider_model_name": provider_model_name,
        }
    )

    # Build ASR context
    context_info = None
    context_privacy_checks = []
    context_reference_receipt = []
    try:
        asr_ctx = await gather_transcription_context(user_id=user_id)
        context_info = asr_ctx.combined
        context_privacy_checks = asr_ctx.privacy_checks
        context_reference_receipt = asr_ctx.reference_receipt

        # Log ASR context as span attributes
        set_span_attrs(
            asr_context_length=len(context_info),
        )
    except Exception as e:
        logger.warning(f"Failed to build ASR context: {e}")

    # Progress callback for RQ job metadata.
    # RQ job_timeout=-1 disables the parent-side kill, so the job runs as long
    # as the ASR service keeps sending progress. Application-level staleness is
    # handled by httpx read_timeout on the NDJSON stream (no progress → socket
    # timeout → exception → job fails naturally).
    last_progress_time = [start_time_wall]  # mutable ref for closure

    def _on_batch_progress(event: dict) -> None:
        last_progress_time[0] = time.time()
        job = get_current_job()
        if job:
            current = event.get("current", 0)
            total = event.get("total", 0)
            batch_progress = {
                "current": current,
                "total": total,
                "percent": int(current / total * 100) if total else 0,
                "message": f"Transcribing segment {current} of {total}",
            }
            job.meta["batch_progress"] = batch_progress
            job.save_meta()

            # Push batch progress to frontend via SSE (throttled to every 3s)
            if user_id:
                publish_sse_event_throttled(
                    user_id,
                    "job.progress",
                    {
                        "conversation_id": conversation_id,
                        "job_type": "transcribe_full_audio_job",
                        "batch_progress": batch_progress,
                    },
                )

    # Provider diarization is discarded when Pyannote is authoritative, so do
    # not pay its compute cost in that mode. Pyannote receives the complete audio
    # later and projects its neural turns onto these conversation-clock words.
    diarization_settings = get_diarization_settings()
    request_provider_diarization = (
        diarization_settings.get("diarization_source") == "provider"
    )

    # Transcribe full audio
    try:
        result = await transcribe_audio_range(
            conversation_id=conversation_id,
            diarize=request_provider_diarization,
            context_info=context_info,
            context_privacy_checks=context_privacy_checks,
            progress_callback=_on_batch_progress,
            provider_model_name=provider_model_name,
        )
    except ValueError as e:
        raise FileNotFoundError(
            f"No audio chunks found for conversation {conversation_id}: {e}"
        )
    except Exception as e:
        logger.error(f"Transcription failed for {conversation_id}: {e}", exc_info=True)
        raise

    processing_time = time.time() - start_time_wall

    return await process_transcription_result(
        context_reference_receipt=context_reference_receipt,
        conversation_id=conversation_id,
        version_id=version_id,
        trigger=trigger,
        transcript_text=result["text"],
        segments=result["segments"],
        words=result["words"],
        provider_name=result["provider_name"],
        provider_capabilities=result["provider_capabilities"],
        wav_size=result["wav_size"],
        processing_time=processing_time,
        user_id=user_id,
        client_id=client_id,
    )


async def materialize_batch_conversation(
    session_id: str,
    user_id: str,
    client_id: str,
    audio_ranges: list[AudioRangeRef],
    *,
    memory_space_id: str | None = None,
) -> "Conversation":
    """Materialize a detected Conversation only after batch ASR confirms speech."""
    if not audio_ranges:
        raise AudioClaimError("cannot materialize a Conversation without audio claims")
    segmentation_key = f"batch-fallback:{session_id}:v1"
    existing = await Conversation.find_one(
        Conversation.segmentation_key == segmentation_key
    )
    if existing is not None:
        return existing

    conversation_kwargs = dict(
        user_id=user_id,
        client_id=client_id,
        title=TITLE_NOT_GENERATED,
        summary="Processing audio with offline transcription...",
        origin="detected",
        started_at=min(audio_range.started_at for audio_range in audio_ranges),
        ended_at=max(audio_range.ended_at for audio_range in audio_ranges),
        segmentation_key=segmentation_key,
    )
    if memory_space_id is not None:
        conversation_kwargs["memory_space_id"] = memory_space_id
    conversation = create_conversation(**conversation_kwargs)
    await apply_audio_ranges(conversation, audio_ranges, save=False)
    try:
        await conversation.insert()
        return conversation
    except DuplicateKeyError:
        winner = await Conversation.find_one(
            Conversation.segmentation_key == segmentation_key
        )
        if winner is None:
            raise
        return winner


@async_job(redis=True, beanie=True)
async def transcription_fallback_check_job(
    session_id: str,
    user_id: str,
    client_id: str,
    conversation_id: str | None = None,
    timeout_seconds: int | None = None,
    memory_space_id: str | None = None,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """
    Check if streaming transcription succeeded, fallback to batch if needed.

    This job acts as a gate for post-conversation jobs:
    - If streaming transcript exists → Pass through immediately
    - If no transcript → Trigger batch transcription, wait for completion, enqueue post-jobs

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        conversation_id: Specific conversation ID to check (avoids matching old conversations)
        timeout_seconds: Max wait time for batch transcription (default 2 minutes)
        memory_space_id: Semantic destination inherited from the durable capture session
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with status (pass_through or batch_fallback_completed) and conversation details
    """
    if timeout_seconds is None:
        timeout_seconds = get_streaming_fallback_timeout()

    logger.info(f"🔍 Checking transcription status for session {session_id[:12]}")

    # Find the exact semantic conversation if speech already materialized one.
    if conversation_id:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
    else:
        conversation = None

    # Check if transcript exists (streaming succeeded)
    if conversation and conversation.active_transcript and conversation.transcript:
        logger.info(
            f"✅ Streaming transcript exists for session {session_id[:12]}, "
            f"passing through (conversation {conversation.conversation_id[:12]})"
        )
        return {
            "status": "pass_through",
            "transcript_source": "streaming",
            "conversation_id": conversation.conversation_id,
        }

    # No transcript → Trigger batch fallback
    logger.warning(
        f"⚠️ No streaming transcript found for session {session_id[:12]}, "
        f"attempting batch transcription fallback"
    )

    # Check if batch provider available
    if not is_transcription_available(mode="batch"):
        raise ValueError(
            "No batch transcription provider available for fallback. "
            "Configure a batch STT provider (e.g., Parakeet) or fix streaming provider."
        )

    try:
        fallback_ranges = (
            list(conversation.audio_ranges)
            if conversation and conversation.audio_ranges
            else await claim_entire_capture(session_id)
        )
    except AudioClaimError:
        logger.info(
            f"ℹ️ Session {session_id[:12]} ended without durable audio; "
            "skipping batch fallback without creating a Conversation"
        )
        return {
            "status": "skipped",
            "reason": "no_audio",
            "message": "No audio available for batch transcription",
            "session_id": session_id,
            "conversation_id": conversation.conversation_id if conversation else None,
        }

    chunks_count = len(
        {
            chunk_id
            for audio_range in fallback_ranges
            for chunk_id in audio_range.chunk_ids
        }
    )
    logger.info(
        f"✅ Found {chunks_count} immutable capture chunks for session "
        f"{session_id[:12]}, proceeding with batch transcription"
    )

    # Transcribe directly — transcribe_audio_range handles chunking internally
    version_id = f"batch_fallback_{session_id[:12]}"

    # Build ASR context
    context_info = None
    context_privacy_checks = []
    context_reference_receipt = []
    try:
        asr_ctx = await gather_transcription_context(user_id=user_id)
        context_info = asr_ctx.combined
        context_privacy_checks = asr_ctx.privacy_checks
        context_reference_receipt = asr_ctx.reference_receipt
    except Exception as e:
        logger.warning(f"Failed to build ASR context: {e}")

    start_time_wall = time.time()

    diarization_settings = get_diarization_settings()
    result = await transcribe_audio_range(
        None,
        diarize=diarization_settings.get("diarization_source") == "provider",
        context_info=context_info,
        context_privacy_checks=context_privacy_checks,
        audio_ranges=fallback_ranges,
    )

    processing_time = time.time() - start_time_wall

    duration = sum(audio_range.duration_seconds for audio_range in fallback_ranges)
    segments, words = validate_and_normalize_transcript_timing(
        result["segments"],
        result["words"],
        audio_duration=duration,
        audio_ranges=[(0.0, duration)],
    )
    range_fingerprint = hashlib.sha256(
        "\n".join(audio_range.range_id for audio_range in fallback_ranges).encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    transcript_artifact = await persist_transcript_artifact(
        user_id=user_id,
        audio_ranges=fallback_ranges,
        retry_key=f"capture-fallback:{session_id}:{range_fingerprint}",
        provider=result["provider_name"].lower(),
        model=result["provider_name"],
        transcript=result["text"],
        words=words,
        segments=segments,
        raw_response={
            **(
                {"privacy_reference_receipt": context_reference_receipt}
                if context_reference_receipt
                else {}
            ),
            "provider_capabilities": result["provider_capabilities"],
            "trigger": "batch_fallback",
            "wav_size": result["wav_size"],
        },
    )
    speech_analysis = analyze_speech({"text": result["text"], "words": words})
    if not speech_analysis.get("has_speech", False):
        logger.info(
            f"🔇 Batch fallback found no meaningful speech for capture "
            f"{session_id[:12]}; retained artifact {transcript_artifact.artifact_id} "
            "without creating a Conversation"
        )
        return {
            "status": "batch_fallback_no_speech",
            "transcript_source": "batch",
            "conversation_id": None,
            "transcript_artifact_id": transcript_artifact.artifact_id,
            "reason": speech_analysis.get("reason"),
        }

    if conversation is None:
        materialize_kwargs: dict[str, str] = {}
        if memory_space_id is not None:
            materialize_kwargs["memory_space_id"] = memory_space_id
        conversation = await materialize_batch_conversation(
            session_id, user_id, client_id, fallback_ranges, **materialize_kwargs
        )
    conv_id = conversation.conversation_id

    processing_result = await process_transcription_result(
        context_reference_receipt=context_reference_receipt,
        conversation_id=conv_id,
        version_id=version_id,
        trigger="batch_fallback",
        transcript_text=result["text"],
        segments=segments,
        words=words,
        provider_name=result["provider_name"],
        provider_capabilities=result["provider_capabilities"],
        wav_size=result["wav_size"],
        processing_time=processing_time,
        user_id=user_id,
        client_id=client_id,
        transcript_artifact=transcript_artifact,
    )

    # If no meaningful speech, conversation was marked deleted — skip post-processing
    if processing_result.get("deleted"):
        logger.info(
            f"🗑️ Batch fallback found no meaningful speech for {conv_id[:12]}, "
            f"skipping post-conversation jobs"
        )
        return {
            "status": "batch_fallback_no_speech",
            "transcript_source": "batch",
            "conversation_id": conv_id,
            "reason": processing_result.get("reason"),
        }

    # Enqueue post-conversation jobs
    post_jobs = start_post_conversation_jobs(
        conversation_id=conv_id,
        user_id=user_id,
        transcript_version_id=version_id,
        depends_on_job=None,
        client_id=client_id,
        end_reason=Conversation.EndReason.WEBSOCKET_DISCONNECT.value,
        trigger=Conversation.ProcessingTrigger.LIVE_SESSION.value,
        memory_space_id=memory_space_id,
    )

    logger.info(
        f"📋 Enqueued {len(post_jobs)} post-conversation jobs for "
        f"batch fallback conversation {conv_id[:12]}"
    )

    return {
        "status": "batch_fallback_completed",
        "transcript_source": "batch",
        "conversation_id": conv_id,
        "post_job_ids": post_jobs,
    }


async def _transcription_failure_context(
    store: "SessionStore",
    aggregator: "TranscriptionResultsAggregator",
    session_id: str,
    client_id: str,
) -> str:
    """Gather actionable diagnostics for a transcription-failure system event.

    The bare failure logs (e.g. "no transcription activity") are symptoms; the real
    cause usually lives in the streaming consumer's ``transcription_error`` flag (the
    provider exception), or — when the provider connected but produced nothing — in
    the configured provider name and chunk count. Returning all of it as a multi-line
    block means the System Errors page row carries the cause, not just the symptom.
    """
    lines: list[str] = []

    # The real upstream exception, if the streaming consumer recorded one.
    try:
        upstream = await store.get_transcription_error(session_id)
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        upstream = None
    if upstream:
        lines.append(f"   Provider error: {upstream}")

    # Which streaming provider was configured (so it's clear what to check).
    try:
        registry = get_models_registry()
        stream_model = registry.get_default("stt_stream") if registry else None
        provider_name = stream_model.name if stream_model else "none configured"
    except Exception:  # noqa: BLE001
        provider_name = "unknown"
    lines.append(f"   Streaming provider: {provider_name}")

    # Whether any audio was actually transcribed.
    try:
        combined = await aggregator.get_combined_results(session_id)
        chunk_count = combined.get("chunk_count", 0)
    except Exception:  # noqa: BLE001
        chunk_count = "unknown"
    lines.append(f"   Transcribed chunks: {chunk_count}")

    try:
        view = await store.read(session_id)
    except Exception:  # noqa: BLE001
        view = None
    if view:
        lines.append(
            f"   Provider connection: {view.transcription_provider_status or 'unknown'}"
        )
        lines.append(
            f"   Last audio sent: {view.transcription_last_audio_sent_at or 'never'}"
        )
        lines.append(
            f"   Last provider message: {view.transcription_last_message_at or 'never'}"
        )

    lines.append(f"   session={session_id} client={client_id}")
    return "\n".join(lines)


async def _session_ended_by_disconnect(store: "SessionStore", session_id: str) -> bool:
    """True when a zero-transcription session ended because the client dropped its
    WebSocket (e.g. the device walked out of network range) rather than because the
    transcription provider failed.

    A provider exception recorded on the session always wins — that is a genuine
    service fault regardless of how the socket closed. Only a clean
    ``websocket_disconnect`` with no recorded provider error is treated as benign, so
    it is logged at WARNING (not ERROR) and never raises a system-error event — a user
    walking through a dead zone shouldn't fill the System Errors page.
    """
    try:
        if await store.get_transcription_error(session_id):
            return False
        return (await store.get_completion_reason(session_id)) == "websocket_disconnect"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return False


def _speech_evidence_detected_at(
    combined: dict[str, Any],
    *,
    capture_started_at: float,
    observed_at: float,
) -> float:
    """Anchor semantic onset to the earliest timed streaming evidence.

    Streaming offsets use the capture session's audio clock. The speech gate may
    notice enough accumulated words much later; polling time must not exclude the
    evidence that caused materialization.
    """
    if capture_started_at <= 0:
        raise ValueError("capture_started_at must be positive")

    starts: list[float] = []

    def collect(items: list[dict[str, Any]]) -> None:
        for item in items:
            start = item.get("start")
            if isinstance(start, (int, float)) and math.isfinite(float(start)):
                starts.append(max(0.0, float(start)))
            nested = item.get("words") or []
            if nested:
                collect(nested)

    collect(combined.get("words") or [])
    collect(combined.get("segments") or [])
    if not starts:
        return observed_at
    return min(observed_at, capture_started_at + min(starts))


@async_job(redis=True, beanie=True)
async def stream_speech_detection_job(
    session_id: str, user_id: str, client_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """
    Listen for meaningful speech, optionally check for enrolled speakers, then start conversation.

    Simple flow:
        1. Listen for meaningful speech
        2. If speaker filter enabled → check for enrolled speakers
        3. If criteria met → start open_conversation_job and EXIT
        4. Conversation will restart new speech detection when complete

    Args:
        session_id: Stream session ID
        user_id: User ID
        client_id: Client ID
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with session info and conversation_job_id or no_speech_detected

    Note: user_email is fetched from the database when needed.
    """

    set_otel_session(session_id)
    logger.info(f"🔍 Starting speech detection for session {session_id[:12]}")

    # Setup
    aggregator = TranscriptionResultsAggregator(redis_client)
    current_job = get_current_job()
    store = SessionStore(redis_client)
    start_time = time.time()
    max_runtime = 7200  # 2 hours — sufficient gap between conversations; fresh job re-enqueued after each

    # Get conversation count
    conversation_count = await store.get_conversation_count(session_id)

    # Check if speaker filtering is enabled
    speaker_filter_enabled = (
        os.getenv("RECORD_ONLY_ENROLLED_SPEAKERS", "false").lower() == "true"
    )
    logger.info(
        f"📊 Conversation #{conversation_count + 1}, Speaker filter: {'enabled' if speaker_filter_enabled else 'disabled'}"
    )

    # Update job metadata to show status
    update_job_meta(
        status="listening_for_speech",
        session_id=session_id,
        client_id=client_id,
        session_level=True,  # Mark as session-level job
    )

    # Live-transcription mode. In "off" mode there is no streaming/windowed worker
    # filling the aggregator, so zero results is the expected steady state — not a
    # failure. The final transcript is produced by batch transcription when the
    # session ends (the "session ended without speech" path enqueues the fallback
    # on the full audio). Disable the no-activity watchdog in that case so it does
    # not abort a still-recording session at the 60s mark.
    live_segmentation = get_live_segmentation()
    expects_live_results = live_segmentation != "off"

    # Track when session closes for graceful shutdown
    session_closed_at = None
    final_check_grace_period = (
        15  # Wait up to 15 seconds for final transcription after session closes
    )
    last_speech_analysis = None  # Track last analysis for detailed logging
    max_runtime_reached = False  # Distinguish the max-runtime exit from session close
    no_activity_warning_logged = False

    # Main loop: Listen for speech
    while True:
        # Check if job still exists in Redis (detect zombie state)
        if not await check_job_alive(redis_client, current_job, session_id):
            break

        # Early transcription failure detection — check every iteration, not just grace period
        error_status = await store.get_transcription_error(session_id)
        if error_status:
            logger.error(f"❌ Transcription error detected: {error_status}")
            break

        # No-activity watchdog: if 60s elapsed with zero transcription results, the
        # live provider is down. Only meaningful when we actually expect live results
        # — in "off" mode nothing fills the aggregator by design, so skip it and let
        # the loop run until the session closes (full-audio batch fallback then runs).
        elapsed = time.time() - start_time
        if (
            expects_live_results
            and elapsed > 60
            and not session_closed_at
            and not no_activity_warning_logged
        ):
            watchdog_combined = await aggregator.get_combined_results(session_id)
            if not watchdog_combined.get("chunk_count", 0):
                # Only a real provider fault when audio is flowing in but the streaming
                # consumer produces nothing. A connected-but-quiet device (e.g. speech
                # detection just re-armed after a conversation ended and the user hasn't
                # spoken again) sends no audio, so zero results is expected — firing here
                # mislabels idle as "transcription service did not respond". When idle,
                # fall through and keep listening until audio resumes or the session
                # closes (the grace-period / disconnect handling below then applies).
                last_chunk_at = await store.get_last_chunk_at(session_id)
                audio_idle = (
                    last_chunk_at is None
                    or (time.time() - last_chunk_at) > AUDIO_INFLOW_IDLE_SECONDS
                )
                if not audio_idle:
                    diag = await _transcription_failure_context(
                        store, aggregator, session_id, client_id
                    )
                    logger.warning(
                        f"⚠️ No transcription activity after {elapsed:.0f}s — "
                        f"provider may be connected and awaiting recognizable speech\n"
                        f"{diag}"
                    )
                    no_activity_warning_logged = True

        # Check if session has closed
        session_closed = await store.get_status(session_id) in (
            SessionStatus.FINALIZING,
            SessionStatus.FINISHED,
        )

        if session_closed and session_closed_at is None:
            # Session just closed - start grace period for final transcription
            session_closed_at = time.time()
            logger.info(
                f"🛑 Session closed, waiting up to {final_check_grace_period}s for final transcription results..."
            )

        # Exit if grace period expired without speech
        if (
            session_closed_at
            and (time.time() - session_closed_at) > final_check_grace_period
        ):
            logger.info(f"✅ Session ended without speech (grace period expired)")
            break

        # Consume any stale close request. Plugin services gate on the active semantic
        # pointer, but this still closes the race where a request arrives during teardown.
        close_reason_str = await store.take_close_request(session_id)
        if close_reason_str:
            logger.info(
                f"🔒 Conversation close requested ({close_reason_str}) during speech detection — "
                f"no open conversation to close, flag consumed"
            )

        if time.time() - start_time > max_runtime:
            logger.warning(f"⏱️ Max runtime reached, exiting")
            max_runtime_reached = True
            break

        # Get transcription results
        combined = await aggregator.get_combined_results(session_id)
        if not combined["text"]:
            # Health check: detect transcription errors early during grace period
            if session_closed_at:
                # Check for streaming consumer errors in session metadata
                error_status = await store.get_transcription_error(session_id)
                if error_status:
                    error_msg = error_status
                    logger.error(f"❌ Transcription service error: {error_msg}")
                    logger.error(
                        f"❌ Session failed - transcription service unavailable"
                    )
                    break

                # Check if we've been waiting too long with no results at all.
                # Only an error when we expect live results — in "off" mode no live
                # activity is expected, so fall through to the normal grace-period
                # exit and let the batch fallback transcribe the full audio.
                grace_elapsed = time.time() - session_closed_at
                if (
                    expects_live_results
                    and grace_elapsed > 5
                    and not combined.get("chunk_count", 0)
                ):
                    # A healthy provider may legitimately produce no messages for
                    # silence/noise, so lack of transcript alone is not a service error.
                    diag = await _transcription_failure_context(
                        store, aggregator, session_id, client_id
                    )
                    if await _session_ended_by_disconnect(store, session_id):
                        logger.warning(
                            f"⚠️ Session ended by client disconnect before any "
                            f"transcription (after {grace_elapsed:.1f}s) — likely a "
                            f"network drop, not a service fault\n{diag}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Session ended without transcription\n"
                            f"   No transcription activity after {grace_elapsed:.1f}s "
                            f"of finalization grace\n"
                            f"{diag}"
                        )
                    break

            await asyncio.sleep(2)
            continue

        # Step 1: Check for meaningful speech
        transcript_data = {"text": combined["text"], "words": combined.get("words", [])}

        logger.info(
            f"🔤 TRANSCRIPT [SPEECH_DETECT] session={session_id}, "
            f"words={len(combined.get('words', []))}, text=\"{combined['text']}\""
        )

        speech_analysis = analyze_speech(transcript_data)
        last_speech_analysis = speech_analysis  # Track for final logging

        logger.info(
            f"🔍 {speech_analysis.get('word_count', 0)} words, "
            f"{speech_analysis.get('duration', 0):.1f}s, "
            f"has_speech: {speech_analysis.get('has_speech', False)}"
        )

        if not speech_analysis.get("has_speech", False):
            logger.info(
                f"⏳ Waiting for more speech - {speech_analysis.get('reason', 'unknown reason')}"
            )
            await asyncio.sleep(2)
            continue

        logger.info(f"💬 Meaningful speech detected!")

        # Add session event for speech detected
        observed_at = time.time()
        session_view = await store.read(session_id)
        if session_view is None:
            raise RuntimeError(
                f"Capture session {session_id} disappeared during speech detection"
            )
        speech_detected_at = _speech_evidence_detected_at(
            combined,
            capture_started_at=session_view.started_at,
            observed_at=observed_at,
        )
        await store.record_event(session_id, "speech_detected")
        await store.set_speech_detected_at(session_id, speech_detected_at)

        # Step 2: If speaker filter enabled, check for enrolled speakers
        identified_speakers = []
        speaker_check_job = None  # Initialize for later reference
        if speaker_filter_enabled:
            logger.info(f"🎤 Enqueuing speaker check job...")

            # Add session event for speaker check starting
            await store.record_event(session_id, "speaker_check_starting")
            await store.set_speaker_check(session_id, SpeakerCheckStatus.CHECKING)

            # Enqueue speaker check as a separate trackable job
            speaker_check_job = transcription_queue.enqueue(
                check_enrolled_speakers_job,
                session_id,
                user_id,
                client_id,
                job_timeout=300,  # 5 minutes for speaker recognition
                result_ttl=600,
                job_id=f"speaker-check_{session_id}_{conversation_count}",
                description=f"Speaker check for conversation #{conversation_count+1}",
                meta={"client_id": client_id},
            )

            # Poll for result (with timeout)
            max_wait = 30  # 30 seconds max
            poll_interval = 0.5
            waited = 0
            enrolled_present = False

            while waited < max_wait:
                try:
                    speaker_check_job.refresh()
                except Exception as e:
                    if isinstance(e, NoSuchJobError):
                        logger.warning(
                            f"⚠️ Speaker check job disappeared from Redis (likely completed quickly), assuming not enrolled"
                        )
                        break
                    else:
                        raise

                if speaker_check_job.is_finished:
                    result = speaker_check_job.result
                    enrolled_present = result.get("enrolled_present", False)
                    identified_speakers = result.get("identified_speakers", [])
                    logger.info(
                        f"✅ Speaker check completed: enrolled={enrolled_present}"
                    )

                    # Update session event for speaker check complete
                    await store.record_event(session_id, "speaker_check_complete")
                    await store.set_speaker_check(
                        session_id,
                        (
                            SpeakerCheckStatus.ENROLLED
                            if enrolled_present
                            else SpeakerCheckStatus.NOT_ENROLLED
                        ),
                    )
                    if identified_speakers:
                        await store.set_identified_speakers(
                            session_id, identified_speakers
                        )
                    break
                elif speaker_check_job.is_failed:
                    logger.warning(
                        f"⚠️ Speaker check job failed, assuming not enrolled"
                    )

                    # Update session event for speaker check failed
                    await store.record_event(session_id, "speaker_check_failed")
                    await store.set_speaker_check(session_id, SpeakerCheckStatus.FAILED)
                    break
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            else:
                # Timeout - assume not enrolled
                logger.warning(
                    f"⏱️ Speaker check timed out after {max_wait}s, assuming not enrolled"
                )
                enrolled_present = False

                # Update session event for speaker check timeout
                await store.record_event(session_id, "speaker_check_timeout")
                await store.set_speaker_check(session_id, SpeakerCheckStatus.TIMEOUT)

            # Log speaker check result but proceed with conversation regardless
            if enrolled_present:
                logger.info(
                    f"✅ Enrolled speaker(s) found: {', '.join(identified_speakers) if identified_speakers else 'Unknown'}"
                )
            else:
                logger.info(
                    f"ℹ️ No enrolled speakers found, but proceeding with conversation anyway"
                )

        # Step 3: Start conversation and EXIT

        # Enqueue conversation job with speech detection job ID
        speech_job_id = current_job.id if current_job else None

        open_job = transcription_queue.enqueue(
            open_conversation_job,
            session_id,
            user_id,
            client_id,
            speech_detected_at,
            speech_job_id,  # Pass speech detection job ID
            job_timeout=10800,  # 3 hours to match max_runtime in open_conversation_job
            result_ttl=JOB_RESULT_TTL,  # Use configured TTL (24 hours) instead of 10 minutes
            job_id=f"open-conv_{session_id}_{conversation_count}",
            description=f"Conversation #{conversation_count+1} for {session_id}",
            meta={"client_id": client_id},
        )

        # Store metadata in speech detection job
        if current_job:
            if not current_job.meta:
                current_job.meta = {}

            # Remove session_level flag now that conversation is starting
            current_job.meta.pop("session_level", None)

            current_job.meta.update(
                {
                    "conversation_job_id": open_job.id,
                    "speaker_check_job_id": (
                        speaker_check_job.id if speaker_check_job else None
                    ),
                    "detected_speakers": identified_speakers,
                    "speech_detected_at": datetime.fromtimestamp(
                        speech_detected_at
                    ).isoformat(),
                    "session_id": session_id,
                    "client_id": client_id,  # For job grouping
                }
            )
            current_job.save_meta()

        logger.info(
            f"✅ Started conversation job {open_job.id}, exiting speech detection"
        )

        return {
            "session_id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "conversation_job_id": open_job.id,
            "speech_detected_at": datetime.fromtimestamp(
                speech_detected_at
            ).isoformat(),
            "runtime_seconds": time.time() - start_time,
        }

    # Session ended without speech
    if last_speech_analysis:
        reason = last_speech_analysis.get("reason", "No transcription received")
    elif not expects_live_results:
        # "off" mode: no live transcription by design — the batch fallback below
        # transcribes the full audio. This is the normal path, not a failure.
        reason = "Live transcription disabled (off mode) — batch transcription pending"
    else:
        reason = "No transcription received"

    # No transcript is not itself a provider failure: streaming APIs may emit no
    # messages for silence/noise. Only an explicit transport/provider error is ERROR.
    if reason == "No transcription received":
        diag = await _transcription_failure_context(
            store, aggregator, session_id, client_id
        )
        provider_error = await store.get_transcription_error(session_id)
        if provider_error:
            logger.error(
                f"❌ Session failed - transcription provider error\n"
                f"   Reason: {provider_error}\n"
                f"   Runtime: {time.time() - start_time:.1f}s\n"
                f"{diag}"
            )
        elif await _session_ended_by_disconnect(store, session_id):
            logger.warning(
                f"⚠️ Session ended by client disconnect with no transcription "
                f"(runtime {time.time() - start_time:.1f}s) — likely a network drop, "
                f"not a service fault\n{diag}"
            )
        else:
            logger.warning(
                f"⚠️ Session ended without transcription\n"
                f"   Reason: {reason}\n"
                f"   Runtime: {time.time() - start_time:.1f}s\n"
                f"{diag}"
            )
    else:
        logger.info(
            f"✅ Session ended, deferring to batch transcription\n"
            f"   Reason: {reason}\n"
            f"   Runtime: {time.time() - start_time:.1f}s"
        )

    # Guard: never batch-transcribe a capture that is still being recorded.
    # Off-mode (expects_live_results=False) is excluded: it has no live worker, so
    # zero results is normal and its window-rotation logic below must still run.
    if expects_live_results and not max_runtime_reached:
        session_status = await store.get_status(session_id)
        if session_status == SessionStatus.ACTIVE:
            logger.warning(
                f"⚠️ Speech detection for {session_id[:12]} exited while session "
                f"still ACTIVE (reason: {reason}). Leaving capture evidence active; "
                f"re-arming listener and deferring to session-close handling."
            )
            # Brief backoff so a persistent provider error can't spin a tight
            # re-arm loop; single-flight keeps this to one listener regardless.
            await asyncio.sleep(5)
            enqueue_speech_detection(
                session_id, user_id, client_id, reason="active_rearm"
            )
            return {
                "session_id": session_id,
                "user_id": user_id,
                "client_id": client_id,
                "status": "rearmed_session_active",
                "reason": reason,
                "runtime_seconds": time.time() - start_time,
            }

    # No Conversation is required here. The fallback first transcribes the completed
    # capture and only then keeps a semantic Conversation if speech is meaningful.
    conversation = None
    config_timeout = get_streaming_fallback_timeout()
    conversation_id = conversation.conversation_id if conversation else None

    # The fallback reads audio chunks the audio-persistence job writes to MongoDB.
    # Those are separate jobs reacting to session-end independently, so the fallback
    # can race ahead and read 0 chunks before persistence has flushed them. Make the
    # ordering explicit via the RQ dependency graph instead of a tuned wait: the
    # fallback cannot start until the persistence job reaches a terminal state (its
    # final flush is durable). allow_failure=True so a persistence failure degrades to
    # the fallback's graceful "no audio → skip" path rather than deferring forever.
    #
    # Two guards:
    #   - Only depend on the persistence job while it's still live. If it already
    #     finished (or its key expired), the chunks are durable — depending on a
    #     missing job id would defer the fallback forever.
    #   - Skip the dependency in the off-mode rotation path (below), where the
    #     persistence job keeps running for the next window and would block for hours.
    fallback_depends_on = None
    is_rotation = max_runtime_reached and not expects_live_results
    if not is_rotation:
        try:
            persistence_job = Job.fetch(
                f"audio-persist_{session_id}",
                connection=transcription_queue.connection,
            )
            if persistence_job.get_status(refresh=True) not in (
                "finished",
                "failed",
                "stopped",
                "canceled",
            ):
                fallback_depends_on = Dependency(
                    jobs=[persistence_job], allow_failure=True
                )
        except NoSuchJobError:
            fallback_depends_on = None  # persistence done & expired — chunks durable

    session_view = await store.read(session_id)
    if session_view is None:
        raise RuntimeError(
            f"Capture session {session_id} disappeared before fallback scheduling"
        )

    fallback_job = transcription_queue.enqueue(
        transcription_fallback_check_job,
        session_id,
        user_id,
        client_id,
        conversation_id=conversation_id,
        timeout_seconds=config_timeout,
        memory_space_id=session_view.memory_space_id or None,
        job_timeout=config_timeout + 120,  # 2 min overhead for fallback check
        # Key the job per-conversation, not per-session. A shared
        # fallback_check_{session_id} id meant that when several Conversations in
        # one capture session ended close together, RQ kept only one and silently
        # dropped the rest. Per-Conversation ids let every claim get its own fallback.
        job_id=f"fallback_check_{conversation_id or session_id}",
        depends_on=fallback_depends_on,
        description=f"Transcription fallback check for {session_id[:8]} (no speech)",
        meta={"session_id": session_id, "client_id": client_id, "no_speech": True},
    )

    logger.info(
        f"📋 Enqueued transcription fallback check job {fallback_job.id} "
        f"for failed session {session_id[:12]} (no speech detected)"
    )

    # Off-mode long-session rotation re-arms processing; capture remains one durable
    # stream, and the compute window does not become a storage or semantic boundary.
    rotated_job_id = None
    if max_runtime_reached and not expects_live_results:
        status = await store.get_status(session_id)
        if status == SessionStatus.ACTIVE:
            next_count = await store.increment_conversation_count(session_id)

            # replaces_current=True: this job IS the tracked live detector and is
            # deliberately handing off to its successor, so skip the single-flight
            # liveness check (which would otherwise see this still-running job).
            rotated_job_id = enqueue_speech_detection(
                session_id,
                user_id,
                client_id,
                reason=f"offmode_rotation_window_{next_count + 1}",
                replaces_current=True,
            )
            logger.info(
                f"🔄 off-mode: hit {max_runtime}s compute cap — "
                f"re-enqueued speech detection {rotated_job_id} for active capture "
                f"{session_id[:12]} (window #{next_count + 1})"
            )
        else:
            logger.info(
                f"⏱️ off-mode: hit {max_runtime}s cap but session "
                f"status={status.value if status else None} — not re-enqueueing"
            )

    return {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "no_speech_detected": True,
        "fallback_job_id": fallback_job.id,
        "rotated_speech_detection_job_id": rotated_job_id,
        "reason": reason,
        "runtime_seconds": time.time() - start_time,
    }
