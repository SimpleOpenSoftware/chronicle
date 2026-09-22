"""
Audio chunk utilities for Opus encoding/decoding and WAV reconstruction.

This module provides functions for:
- Converting PCM audio to Opus-compressed format
- Decoding Opus audio back to PCM
- Building complete WAV files from PCM data
- Retrieving audio chunks from MongoDB

All FFmpeg operations use subprocess with proper error handling and cleanup.
"""

import array
import asyncio
import io
import logging
import math
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from bson import Binary
from pymongo.errors import DuplicateKeyError

import backend.services.privacy as privacy
from backend.models.audio_capture import (
    AudioCaptureSession,
    AudioRangeRef,
    CaptureEffects,
    as_utc,
)
from backend.models.audio_chunk import AudioChunkDocument
from backend.models.conversation import Conversation
from backend.models.waveform import WaveformData
from backend.services.audio_claims import (
    AudioClaimError,
    ClaimedChunk,
    range_duration,
    resolve_audio_ranges,
    resolve_conversation_audio,
)
from backend.services.corpus_reconciliation import encoded_identity, pcm_identity

logger = logging.getLogger(__name__)


async def encode_pcm_to_opus(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: int = 24,
) -> bytes:
    """
    Encode raw PCM audio to Opus format using FFmpeg with stdin/stdout pipes.

    Args:
        pcm_data: Raw PCM audio bytes (signed 16-bit little-endian)
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        bitrate: Opus bitrate in kbps (default: 24 for speech)

    Returns:
        Opus-encoded audio bytes

    Raises:
        RuntimeError: If FFmpeg encoding fails
    """
    # FFmpeg: read PCM from stdin, write Opus to stdout
    # -f ogg wraps opus in an ogg container for stdout piping
    cmd = [
        "ffmpeg",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        f"{bitrate}k",
        "-vbr",
        "on",
        "-application",
        "voip",
        "-f",
        "ogg",
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=pcm_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        logger.error(f"FFmpeg Opus encoding failed: {error_msg}")
        raise RuntimeError(f"Opus encoding failed: {error_msg}")

    logger.debug(
        f"Encoded PCM ({len(pcm_data)} bytes) → Opus ({len(stdout)} bytes), "
        f"compression ratio: {len(stdout)/len(pcm_data):.3f}"
    )

    return stdout


async def decode_opus_to_pcm(
    opus_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """
    Decode Opus audio to raw PCM format using FFmpeg with stdin/stdout pipes.

    Args:
        opus_data: Opus-encoded audio bytes (ogg/opus container)
        sample_rate: Target sample rate in Hz (default: 16000)
        channels: Target number of channels (default: 1 for mono)

    Returns:
        Raw PCM audio bytes (signed 16-bit little-endian)

    Raises:
        RuntimeError: If FFmpeg decoding fails
    """
    # FFmpeg: read Opus from stdin, write PCM to stdout
    cmd = [
        "ffmpeg",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=opus_data)

    if process.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        logger.error(f"FFmpeg Opus decoding failed: {error_msg}")
        raise RuntimeError(f"Opus decoding failed: {error_msg}")

    logger.debug(f"Decoded Opus ({len(opus_data)} bytes) → PCM ({len(stdout)} bytes)")

    return stdout


def _build_wav_bytes(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Synchronously encode a WAV container around PCM bytes."""

    wav_buffer = io.BytesIO()
    try:
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return wav_buffer.getvalue()
    finally:
        wav_buffer.close()


async def build_wav_from_pcm(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Build a complete WAV file from raw PCM data without blocking the event loop.

    Args:
        pcm_data: Raw PCM audio bytes (signed 16-bit little-endian)
        sample_rate: Sample rate in Hz (default: 16000)
        channels: Number of audio channels (default: 1 for mono)
        sample_width: Bytes per sample (default: 2 for 16-bit)

    Returns:
        Complete WAV file as bytes (including headers)

    Example:
        >>> pcm_bytes = b"..."  # Raw PCM audio
        >>> wav_bytes = await build_wav_from_pcm(pcm_bytes)
        >>> # wav_bytes can be served via StreamingResponse
    """
    # ``wave.writeframes`` copies the entire PCM payload. On the ten-hour corpus file
    # that monopolized FastAPI's event-loop thread for 2.14 seconds, delaying health
    # checks and live audio reads. The bytes are still built in memory, but on the
    # default worker pool so other async work remains schedulable.
    wav_bytes = await asyncio.to_thread(
        _build_wav_bytes,
        pcm_data,
        sample_rate,
        channels,
        sample_width,
    )
    logger.debug(
        "Built WAV file: %d bytes (PCM: %d, header: %d)",
        len(wav_bytes),
        len(pcm_data),
        len(wav_bytes) - len(pcm_data),
    )
    return wav_bytes


async def retrieve_audio_chunks(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> List[AudioChunkDocument]:
    """Resolve a Conversation claim and return its chunks in presentation order.

    ``start_index`` and ``limit`` address the ordered claim, not storage sequence.
    Exact edge clipping is intentionally handled by the PCM reconstruction helpers;
    callers that need playable audio must not concatenate this result directly.
    """

    await privacy.require_conversation(conversation_id)
    resolved = await resolve_conversation_audio(conversation_id)
    chunks: list[AudioChunkDocument] = []
    seen: set[str] = set()
    for item in resolved:
        chunk_id = str(item.chunk.id)
        if chunk_id not in seen:
            chunks.append(item.chunk)
            seen.add(chunk_id)
    chunks = chunks[start_index : None if limit is None else start_index + limit]

    logger.debug(
        f"Retrieved {len(chunks)} chunks for conversation {conversation_id[:8]}... "
        f"(start_index={start_index}, limit={limit})"
    )

    return chunks


def audio_cache_duration_matches(cached_seconds: float, current_seconds: float) -> bool:
    """Whether a cache built for ``cached_seconds`` still matches the conversation's
    current audio duration (within a small tolerance for partial final chunks).

    Caches derived from the audio-chunk set (waveform, vad_analysis) become stale
    when the chunk set changes in place (e.g. the reconnect-duplicate dedup). The
    serving paths use this to validate the cache against the source of truth
    (``Conversation.audio_total_duration``) and regenerate/ignore it when stale.
    """
    tolerance = max(2.0, 0.02 * max(cached_seconds, current_seconds))
    return abs((cached_seconds or 0.0) - (current_seconds or 0.0)) <= tolerance


async def invalidate_conversation_audio_caches(conversation_id: str) -> dict:
    """Drop caches derived from a conversation's audio chunks (waveform + the
    vad_analysis summary) so they regenerate from the current chunk set.

    Call this whenever a conversation's chunk set changes IN PLACE (same
    conversation_id) — e.g. a re-chunk/trim or a manual dedup repair. Split/merge
    move chunks to fresh conversation_ids, so children regenerate naturally and
    don't need this. Lazy regeneration happens on next read.
    """
    waveforms_deleted = (
        await WaveformData.find(
            WaveformData.conversation_id == conversation_id
        ).delete()
    ).deleted_count

    vad_cleared = False
    conv = await Conversation.find_one(Conversation.conversation_id == conversation_id)
    if conv is not None and conv.vad_analysis is not None:
        conv.vad_analysis = None
        await conv.save()
        vad_cleared = True

    return {"waveforms_deleted": waveforms_deleted, "vad_cleared": vad_cleared}


async def concatenate_chunks_to_pcm(
    chunks: List[AudioChunkDocument],
) -> bytes:
    """
    Decode and concatenate multiple audio chunks into a single PCM buffer.

    Concatenates ogg/opus data from all chunks and decodes in a single ffmpeg
    call, avoiding per-chunk subprocess overhead.

    Args:
        chunks: List of AudioChunkDocument instances (should be pre-sorted)

    Returns:
        Concatenated PCM audio bytes
    """
    if not chunks:
        return b""

    if len(chunks) == 1:
        return await decode_opus_to_pcm(
            opus_data=chunks[0].audio_data,
            sample_rate=chunks[0].sample_rate,
            channels=chunks[0].channels,
        )

    # Concatenate ogg/opus containers into a chained ogg stream.
    # ffmpeg handles chained ogg streams natively — one decode call for all chunks.
    combined_opus = b"".join(bytes(chunk.audio_data) for chunk in chunks)

    pcm_data = await decode_opus_to_pcm(
        opus_data=combined_opus,
        sample_rate=chunks[0].sample_rate,
        channels=chunks[0].channels,
    )

    logger.debug(f"Batch decoded {len(chunks)} chunks → {len(pcm_data)} bytes PCM")

    return pcm_data


def _clip_pcm(
    pcm_data: bytes,
    *,
    start_seconds: float,
    end_seconds: float,
    sample_rate: int,
    channels: int,
) -> bytes:
    """Clip decoded 16-bit PCM on complete sample frames."""
    bytes_per_frame = channels * 2
    bytes_per_second = sample_rate * bytes_per_frame
    start_byte = int(start_seconds * bytes_per_second)
    end_byte = int(end_seconds * bytes_per_second)
    start_byte = max(0, start_byte - start_byte % bytes_per_frame)
    end_byte = min(len(pcm_data), end_byte - end_byte % bytes_per_frame)
    return pcm_data[start_byte:end_byte]


async def _claimed_window_to_pcm(
    resolved: Sequence[ClaimedChunk],
    start_time: float,
    end_time: float,
    *,
    decode_cache: Optional[dict[str, bytes]] = None,
) -> tuple[bytes, int, int]:
    """Render an exact presentation-time window from already-resolved claims."""
    if start_time < 0 or end_time <= start_time:
        raise ValueError(f"Invalid time range: start={start_time}s, end={end_time}s")

    selected: list[tuple[ClaimedChunk, float, float]] = []
    for item in resolved:
        item_start = item.conversation_start_seconds
        item_end = item_start + item.duration_seconds
        overlap_start = max(start_time, item_start)
        overlap_end = min(end_time, item_end)
        if overlap_end > overlap_start:
            selected.append((item, overlap_start, overlap_end))
    if not selected:
        raise AudioClaimError(f"No claimed audio covers [{start_time}, {end_time}]")

    sample_rate = selected[0][0].chunk.sample_rate
    channels = selected[0][0].chunk.channels

    # Preserve the canonical full-capture decode used by the paid-transcription
    # cache. Decoding each independent Ogg link separately is acoustically valid but
    # produces different PCM bytes, defeating content-addressed reuse. The fast path
    # is safe only when the requested window contains whole presentation-contiguous
    # chunks; clipped playback continues through the exact per-chunk path below.
    whole_chunk_window = decode_cache is None
    expected_start = start_time
    for item, overlap_start, overlap_end in selected:
        chunk_duration = float(item.chunk.duration)
        if (
            abs(overlap_start - item.conversation_start_seconds) > 0.001
            or abs(
                overlap_end - (item.conversation_start_seconds + item.duration_seconds)
            )
            > 0.001
            or abs(item.clip_start_seconds) > 0.001
            or abs(item.clip_end_seconds - chunk_duration) > 0.001
            or abs(item.conversation_start_seconds - expected_start) > 0.001
        ):
            whole_chunk_window = False
            break
        expected_start += item.duration_seconds
    if whole_chunk_window and abs(expected_start - end_time) <= 0.001:
        pcm_data = await concatenate_chunks_to_pcm(
            [item.chunk for item, _, _ in selected]
        )
        return pcm_data, sample_rate, channels

    output = bytearray()
    for item, overlap_start, overlap_end in selected:
        chunk = item.chunk
        if chunk.sample_rate != sample_rate or chunk.channels != channels:
            raise AudioClaimError("Audio format changes inside one conversation claim")
        cache_key = str(chunk.id)
        decoded = decode_cache.get(cache_key) if decode_cache is not None else None
        if decoded is None:
            decoded = await decode_opus_to_pcm(
                bytes(chunk.audio_data),
                sample_rate=sample_rate,
                channels=channels,
            )
            if decode_cache is not None:
                decode_cache[cache_key] = decoded
        source_start = item.clip_start_seconds + (
            overlap_start - item.conversation_start_seconds
        )
        source_end = item.clip_start_seconds + (
            overlap_end - item.conversation_start_seconds
        )
        output.extend(
            _clip_pcm(
                decoded,
                start_seconds=source_start,
                end_seconds=source_end,
                sample_rate=sample_rate,
                channels=channels,
            )
        )
    return bytes(output), sample_rate, channels


async def get_trimmed_opus_for_time_range(
    conversation_id: str, start_time: float, end_time: float
) -> bytes:
    """
    Get exact-trimmed ogg/opus audio for a time range.

    Decodes the stored chunks overlapping the range, clips the PCM to the
    exact boundaries, and re-encodes as a single ogg/opus stream. Raw chunk
    concatenation is not usable here: it is chunk-aligned (~10s granularity)
    and produces a chained ogg stream, which browsers stop playing after the
    first link.

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Ogg/opus bytes covering exactly start_time..end_time

    Raises:
        ValueError: If the conversation or range has no audio
    """
    start_timer = time.time()

    clipped_pcm, sample_rate, channels = await get_clipped_pcm_for_time_range(
        conversation_id, start_time, end_time
    )

    if not clipped_pcm:
        raise ValueError(
            f"No audio chunks found for {conversation_id} "
            f"in range {start_time:.1f}s-{end_time:.1f}s"
        )

    opus_data = await encode_pcm_to_opus(
        pcm_data=clipped_pcm,
        sample_rate=sample_rate,
        channels=channels,
    )

    logger.info(
        f"Trimmed opus for {conversation_id[:8]}...: "
        f"{start_time:.1f}s - {end_time:.1f}s "
        f"({len(opus_data)} bytes, {time.time() - start_timer:.2f}s)"
    )

    return opus_data


async def get_opus_for_conversation(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> bytes:
    """Render a playable Ogg/Opus stream from the selected semantic claim."""
    resolved = await resolve_conversation_audio(conversation_id)
    selected = list(
        resolved[start_index : None if limit is None else start_index + limit]
    )
    if not selected:
        raise ValueError(f"No audio claim found for conversation {conversation_id}")
    start = selected[0].conversation_start_seconds
    end = selected[-1].conversation_start_seconds + selected[-1].duration_seconds
    pcm_data, sample_rate, channels = await _claimed_window_to_pcm(selected, start, end)
    opus_data = await encode_pcm_to_opus(
        pcm_data,
        sample_rate=sample_rate,
        channels=channels,
    )

    logger.info(
        f"Serving {len(selected)} claimed chunks for {conversation_id[:8]}... "
        f"({len(opus_data)} bytes)"
    )

    return opus_data


async def reconstruct_wav_from_conversation(
    conversation_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
) -> bytes:
    """
    Reconstruct a complete WAV file from MongoDB chunks.

    This is a high-level convenience function that:
    1. Retrieves chunks from MongoDB
    2. Decodes Opus → PCM
    3. Concatenates PCM data
    4. Builds WAV file with headers

    Args:
        conversation_id: Parent conversation ID
        start_index: First chunk to include (default: 0)
        limit: Maximum chunks to include (default: None for all)

    Returns:
        Complete WAV file as bytes

    Raises:
        ValueError: If no chunks found for conversation

    Example:
        >>> # Get complete audio for conversation
        >>> wav_data = await reconstruct_wav_from_conversation(conversation_id)
        >>>
        >>> # Get first 60 seconds (6 chunks @ 10s each)
        >>> wav_data = await reconstruct_wav_from_conversation(conversation_id, limit=6)
    """
    resolved = await resolve_conversation_audio(conversation_id)
    selected = list(
        resolved[start_index : None if limit is None else start_index + limit]
    )
    if not selected:
        raise ValueError(f"No audio claim found for conversation {conversation_id}")
    start = selected[0].conversation_start_seconds
    end = selected[-1].conversation_start_seconds + selected[-1].duration_seconds
    pcm_data, sample_rate, channels = await _claimed_window_to_pcm(selected, start, end)

    # Build WAV file
    wav_data = await build_wav_from_pcm(
        pcm_data=pcm_data,
        sample_rate=sample_rate,
        channels=channels,
    )

    logger.info(
        f"Reconstructed WAV for conversation {conversation_id[:8]}...: "
        f"{len(selected)} claimed chunks, {len(wav_data)} bytes, "
        f"{len(pcm_data) / sample_rate / channels / 2:.1f}s duration"
    )

    return wav_data


async def reconstruct_wav_from_claims(
    audio_ranges: Sequence[AudioRangeRef],
    start_time: float = 0.0,
    end_time: float | None = None,
) -> bytes:
    """Render capture evidence before a semantic Conversation exists."""

    await privacy.require_audio_ranges(audio_ranges)
    if not audio_ranges:
        raise ValueError("No audio ranges supplied")
    resolved = await resolve_audio_ranges(audio_ranges)
    total_duration = range_duration(audio_ranges)
    start = max(0.0, float(start_time))
    end = total_duration if end_time is None else min(float(end_time), total_duration)
    pcm_data, sample_rate, channels = await _claimed_window_to_pcm(resolved, start, end)
    return await build_wav_from_pcm(
        pcm_data=pcm_data,
        sample_rate=sample_rate,
        channels=channels,
    )


async def reconstruct_audio_segments(
    conversation_id: str,
    segment_duration: float = 1200.0,  # 20-minute bounded compute window
    overlap: float = 30.0,  # 30 seconds overlap for continuity
):
    """
    Reconstruct audio from MongoDB chunks in time-bounded segments.

    This function yields audio segments from a conversation, allowing
    processing of large files without loading everything into memory.

    Args:
        conversation_id: Parent conversation ID
        segment_duration: Duration of each segment in seconds (default: 900 = 15 minutes)
        overlap: Overlap between segments in seconds (default: 30)

    Yields:
        Tuple of (wav_bytes, start_time, end_time) for each segment

    Example:
        >>> # Process 73-minute conversation in 15-minute chunks
        >>> async for wav_data, start, end in reconstruct_audio_segments(conv_id):
        ...     # Process segment (only ~27 MB in memory at a time)
        ...     result = await process_segment(wav_data, start, end)

    Note:
        Overlap is added to all segments except the final one, to ensure
        speaker continuity across segment boundaries. Overlapping regions
        should be merged during post-processing.
    """
    resolved = await resolve_conversation_audio(conversation_id)
    total_duration = sum(item.duration_seconds for item in resolved)
    if total_duration <= 0:
        logger.warning(
            f"Conversation {conversation_id} has zero duration, no segments to yield"
        )
        return

    start_time = 0.0

    while start_time < total_duration:
        # Calculate segment end time with overlap
        end_time = min(start_time + segment_duration + overlap, total_duration)

        pcm_data, sample_rate, channels = await _claimed_window_to_pcm(
            resolved, start_time, end_time
        )

        # Build WAV file for this segment
        wav_bytes = await build_wav_from_pcm(
            pcm_data=pcm_data,
            sample_rate=sample_rate,
            channels=channels,
        )

        logger.info(
            f"Yielding segment for {conversation_id[:8]}...: "
            f"{start_time:.1f}s - {end_time:.1f}s "
            f"({len(wav_bytes)} bytes)"
        )

        yield (wav_bytes, start_time, end_time)

        # Move to next segment (no overlap on the starting edge)
        start_time += segment_duration


async def get_clipped_pcm_for_time_range(
    conversation_id: str, start_time: float, end_time: float
) -> tuple[bytes, int, int]:
    """
    Decode conversation audio and clip it to an exact time range.

    Fetches the MongoDB chunks overlapping the range, batch-decodes them in
    a single ffmpeg call, and clips the PCM to the exact requested
    boundaries (chunk boundaries are ~10s, so decoding alone is not enough).

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Tuple of (pcm_data, sample_rate, channels). pcm_data is empty when
        no chunks overlap the range.

    Raises:
        ValueError: If conversation not found, has no audio, or range is invalid
    """
    resolved = await resolve_conversation_audio(conversation_id)
    total_duration = sum(item.duration_seconds for item in resolved)
    start = max(0.0, float(start_time))
    end = min(float(end_time), total_duration)
    return await _claimed_window_to_pcm(resolved, start, end)


async def reconstruct_audio_segment(
    conversation_id: str, start_time: float, end_time: float
) -> bytes:
    """
    Reconstruct audio for a specific time range from MongoDB chunks.

    This function returns a single audio segment for the specified time range,
    enabling on-demand access to conversation audio without loading the entire
    file into memory. Used by the audio segment API endpoint.

    Args:
        conversation_id: Conversation ID
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        WAV audio bytes (16kHz mono or original format)

    Raises:
        ValueError: If conversation not found or has no audio
        Exception: If audio reconstruction fails

    Example:
        >>> # Get first 60 seconds of audio
        >>> wav_bytes = await reconstruct_audio_segment(conv_id, 0.0, 60.0)
        >>> # Save to file
        >>> with open("segment.wav", "wb") as f:
        ...     f.write(wav_bytes)
    """
    start_timer = time.time()
    wav_bytes = (
        await reconstruct_audio_ranges(
            conversation_id,
            [(start_time, end_time)],
        )
    )[0]

    processing_time = time.time() - start_timer

    logger.info(
        f"Reconstructed audio segment for {conversation_id[:8]}...: "
        f"{start_time:.1f}s - {end_time:.1f}s "
        f"({len(wav_bytes)} bytes WAV, "
        f"processing time: {processing_time:.2f}s)"
    )

    return wav_bytes


async def reconstruct_audio_ranges(
    conversation_id: str,
    ranges: List[tuple[float, float]],
    *,
    max_window_seconds: float = 120.0,
) -> List[bytes]:
    """Reconstruct presentation-time ranges from immutable range claims.

    Work is grouped into bounded windows, and no storage or semantic boundary is
    inferred from those windows.
    """
    if not ranges:
        return []
    resolved = await resolve_conversation_audio(conversation_id)
    return await reconstruct_resolved_audio_ranges(
        resolved,
        ranges,
        conversation_id=conversation_id,
        max_window_seconds=max_window_seconds,
    )


async def reconstruct_resolved_audio_ranges(
    resolved: Sequence[ClaimedChunk],
    ranges: List[tuple[float, float]],
    *,
    conversation_id: str = "resolved audio claim",
    max_window_seconds: float = 120.0,
) -> List[bytes]:
    """Render several ranges after resolving their immutable claim once.

    Corpus jobs often inspect hundreds of short turns from one recording. Passing the
    already-resolved claim prevents every turn from reloading the complete chunk set
    from MongoDB, while the existing compute windows keep decoded PCM caches bounded.
    """
    if not ranges:
        return []
    if max_window_seconds <= 0:
        raise ValueError("max_window_seconds must be positive")

    total_duration = sum(item.duration_seconds for item in resolved)
    if total_duration <= 0:
        raise ValueError(f"Conversation {conversation_id} has no audio")

    indexed_ranges = []
    for index, (start_time, end_time) in enumerate(ranges):
        start = float(start_time)
        end = min(float(end_time), total_duration)
        if start < 0 or end <= start:
            raise ValueError(
                f"Invalid time range [{start_time}, {end_time}] for {conversation_id}"
            )
        indexed_ranges.append((index, start, end))
    indexed_ranges.sort(key=lambda item: (item[1], item[2]))

    windows: List[List[tuple[int, float, float]]] = []
    for item in indexed_ranges:
        if not windows or item[2] - windows[-1][0][1] > max_window_seconds:
            windows.append([item])
        else:
            windows[-1].append(item)

    results: List[Optional[bytes]] = [None] * len(ranges)
    for window in windows:
        decode_cache: dict[str, bytes] = {}
        for original_index, start, end in window:
            pcm_data, sample_rate, channels = await _claimed_window_to_pcm(
                resolved, start, end, decode_cache=decode_cache
            )
            results[original_index] = await build_wav_from_pcm(
                pcm_data,
                sample_rate=sample_rate,
                channels=channels,
            )

        missing = [item for item in window if results[item[0]] is None]
        if missing:
            _, start, end = missing[0]
            raise ValueError(
                f"Audio chunk gap prevents exact range reconstruction for "
                f"[{start}, {end}] in {conversation_id}"
            )

    if any(result is None for result in results):
        raise RuntimeError("Range reconstruction did not produce every requested clip")
    return [result for result in results if result is not None]


def filter_transcript_by_time(
    transcript_data: dict, start_time: float, end_time: float
) -> dict:
    """
    Filter transcript data to only include words within a time range.

    Args:
        transcript_data: Dict with 'text' and 'words' keys
        start_time: Start time in seconds
        end_time: End time in seconds

    Returns:
        Filtered transcript data with only words in time range

    Example:
        >>> transcript = {"text": "full text", "words": [...100 words...]}
        >>> segment = filter_transcript_by_time(transcript, 0.0, 900.0)  # First 15 minutes
        >>> # segment contains only words from 0-900 seconds
    """
    if not transcript_data or "words" not in transcript_data:
        return transcript_data

    words = transcript_data.get("words", [])

    if not words:
        return transcript_data

    # Filter words by time range
    filtered_words = []
    for word in words:
        word_start = word.get("start", 0)
        word_end = word.get("end", 0)

        # Include word if it overlaps with the time range
        if word_start < end_time and word_end > start_time:
            filtered_words.append(word)

    # Rebuild text from filtered words
    filtered_text = " ".join(word.get("word", "") for word in filtered_words)

    return {"text": filtered_text, "words": filtered_words}


@dataclass(frozen=True)
class CaptureIngestResult:
    capture_session_id: str
    audio_range: AudioRangeRef
    chunk_count: int
    duration_seconds: float
    compression_ratio: float


async def convert_audio_to_chunks(
    *,
    user_id: str,
    capture_source_id: str,
    audio_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
    chunk_duration: float = 10.0,
    captured_at: Optional[datetime] = None,
    capture_session_id: Optional[str] = None,
    origin: str = "upload",
    external_source_id: Optional[str] = None,
    data_purpose: str = "normal_capture",
) -> CaptureIngestResult:
    """
    Convert raw PCM audio directly to MongoDB chunks without disk intermediary.

    This is the preferred method as it avoids unnecessary disk I/O.
    Used for both WebSocket streaming and file uploads.

    Args:
        user_id: Owner of the capture evidence
        capture_source_id: Stable device/channel identity
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate (default: 16000 Hz)
        channels: Number of channels (default: 1 = mono)
        sample_width: Bytes per sample (default: 2 = 16-bit)
        chunk_duration: Duration of each chunk in seconds (default: 10.0)
        captured_at: Absolute UTC time the first sample was captured. Each chunk
            records its own ``captured_at`` from this, which is what survives the
            renumbering done by split, merge and silence trimming.

    Returns:
        Capture identity, range claim, and storage statistics

    Example:
        >>> # Convert from memory without disk write
        >>> result = await convert_audio_to_chunks(
        ...     user_id="user-id",
        ...     capture_source_id="upload-device",
        ...     audio_data=pcm_bytes,
        ...     sample_rate=16000,
        ...     channels=1,
        ...     sample_width=2,
        ... )
        >>> print(f"Created {result.chunk_count} chunks")
    """
    logger.info(f"📦 Converting audio to MongoDB chunks: {len(audio_data)} bytes PCM")

    if not audio_data:
        raise ValueError("audio_data must not be empty")
    capture_session_id = capture_session_id or str(uuid.uuid4())
    time_basis = "recorded" if captured_at is not None else "unknown"
    captured_at = captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)

    bytes_per_second = sample_rate * sample_width * channels
    total_duration_seconds = len(audio_data) / bytes_per_second
    ended_at = captured_at + timedelta(seconds=total_duration_seconds)
    content_sha256 = pcm_identity(audio_data, sample_rate, channels, sample_width)
    chunk_size_bytes = int(chunk_duration * bytes_per_second)
    if origin in {"upload", "batch", "import"}:
        processing_profile = "imported"
        effects = CaptureEffects.not_applicable()
    elif origin == "screenpipe":
        processing_profile = "source_native"
        effects = CaptureEffects.unreported()
    elif origin == "streaming":
        processing_profile = "ambient"
        effects = CaptureEffects.unreported()
    else:
        raise ValueError(f"Unsupported capture origin: {origin!r}")

    # Imports may reuse an existing imported recording. Device captures instead
    # retain their source/time identity even when their PCM is identical (for
    # example silence or audio shared by microphone and system tracks). Their
    # deterministic capture_session_id owns retries below.
    canonical = None
    if processing_profile == "imported":
        canonical = (
            await AudioCaptureSession.find(
                AudioCaptureSession.user_id == user_id,
                AudioCaptureSession.content_sha256 == content_sha256,
                AudioCaptureSession.processing_profile == "imported",
            )
            .sort("+started_at")
            .first_or_none()
        )
    if canonical is not None and canonical.capture_session_id != capture_session_id:
        canonical_chunks = (
            await AudioChunkDocument.find(
                AudioChunkDocument.capture_session_id == canonical.capture_session_id
            )
            .sort("+sequence")
            .to_list()
        )
        if not canonical_chunks or canonical.ended_at is None:
            raise ValueError(
                "PCM dedupe candidate is incomplete: " f"{canonical.capture_session_id}"
            )
        original_size = sum(chunk.original_size for chunk in canonical_chunks)
        compressed_size = sum(chunk.compressed_size for chunk in canonical_chunks)
        logger.info(
            "♻️ Reusing canonical PCM capture %s for duplicate session %s",
            canonical.capture_session_id,
            capture_session_id,
        )
        return CaptureIngestResult(
            capture_session_id=canonical.capture_session_id,
            audio_range=AudioRangeRef(
                capture_source_id=canonical.capture_source_id,
                time_basis=canonical.time_basis,
                capture_session_ids=[canonical.capture_session_id],
                chunk_ids=[str(chunk.id) for chunk in canonical_chunks],
                started_at=as_utc(canonical.started_at),
                ended_at=as_utc(canonical.ended_at),
            ),
            chunk_count=len(canonical_chunks),
            duration_seconds=(
                as_utc(canonical.ended_at) - as_utc(canonical.started_at)
            ).total_seconds(),
            compression_ratio=(
                compressed_size / original_size if original_size else 0.0
            ),
        )

    def validate_existing_capture(existing: AudioCaptureSession) -> None:
        mismatches = []
        for field, expected in (
            ("user_id", user_id),
            ("capture_source_id", capture_source_id),
            ("sample_rate", sample_rate),
            ("channels", channels),
            ("sample_width", sample_width),
            ("content_sha256", content_sha256),
            ("time_basis", time_basis),
            ("capture_epoch", 0),
            ("processing_profile", processing_profile),
            ("effects", effects),
            ("voice_session_id", None),
        ):
            if getattr(existing, field) != expected:
                mismatches.append(field)
        if abs((as_utc(existing.started_at) - captured_at).total_seconds()) > 0.001:
            mismatches.append("started_at")
        if mismatches:
            raise ValueError(
                f"capture_session_id {capture_session_id} was reused for different "
                f"audio ({', '.join(mismatches)})"
            )

    existing_capture = await AudioCaptureSession.find_one(
        AudioCaptureSession.capture_session_id == capture_session_id
    )
    if existing_capture is not None:
        validate_existing_capture(existing_capture)

    capture = existing_capture or AudioCaptureSession(
        capture_session_id=capture_session_id,
        user_id=user_id,
        capture_source_id=capture_source_id,
        client_id=capture_source_id,
        origin=origin,
        time_basis=time_basis,
        capture_epoch=0,
        processing_profile=processing_profile,
        effects=effects,
        voice_session_id=None,
        status="active",
        external_source_id=external_source_id,
        content_sha256=content_sha256,
        data_purpose=data_purpose,
        started_at=captured_at,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )
    if existing_capture is None:
        try:
            await capture.insert()
        except DuplicateKeyError:
            # A concurrent retry won capture creation. Validate and resume its
            # partial chunks rather than creating a second evidence copy.
            winner = await AudioCaptureSession.find_one(
                AudioCaptureSession.capture_session_id == capture_session_id
            )
            if winner is None:
                raise
            validate_existing_capture(winner)
            capture = winner

    stored = (
        await AudioChunkDocument.find(
            AudioChunkDocument.capture_session_id == capture_session_id
        )
        .sort("+sequence")
        .to_list()
    )
    offset = 0
    for expected_sequence, chunk in enumerate(stored):
        expected_size = min(chunk_size_bytes, len(audio_data) - offset)
        expected_started_at = captured_at + timedelta(seconds=offset / bytes_per_second)
        if (
            chunk.sequence != expected_sequence
            or chunk.user_id != user_id
            or chunk.capture_source_id != capture_source_id
            or chunk.original_size != expected_size
            or abs((as_utc(chunk.captured_at) - expected_started_at).total_seconds())
            > 0.001
        ):
            raise ValueError(
                f"partial capture {capture_session_id} is inconsistent at sequence "
                f"{expected_sequence}"
            )
        offset += expected_size

    if offset > len(audio_data):
        raise ValueError(f"partial capture {capture_session_id} exceeds source audio")

    # Insert in batches of 100 chunks (~16 min at 10s/chunk) to avoid
    # accumulating all chunks in memory for very long audio files.
    BATCH_INSERT_SIZE = 100
    chunks_to_insert = []
    chunk_index = len(stored)
    total_original_size = sum(chunk.original_size for chunk in stored)
    total_compressed_size = sum(chunk.compressed_size for chunk in stored)

    while offset < len(audio_data):
        # Extract chunk PCM data
        chunk_end = min(offset + chunk_size_bytes, len(audio_data))
        chunk_pcm = audio_data[offset:chunk_end]

        if len(chunk_pcm) == 0:
            break

        # Calculate absolute chunk timing
        chunk_start_time = offset / bytes_per_second
        chunk_duration_actual = (chunk_end - offset) / bytes_per_second

        # Encode to Opus
        opus_data = await encode_pcm_to_opus(
            pcm_data=chunk_pcm,
            sample_rate=sample_rate,
            channels=channels,
            bitrate=24,  # 24kbps for speech
        )

        # Create MongoDB document
        audio_chunk = AudioChunkDocument(
            user_id=user_id,
            capture_source_id=capture_source_id,
            capture_session_id=capture_session_id,
            sequence=chunk_index,
            audio_data=Binary(opus_data),
            content_sha256=encoded_identity(opus_data),
            original_size=len(chunk_pcm),
            compressed_size=len(opus_data),
            duration=chunk_duration_actual,
            captured_at=captured_at + timedelta(seconds=chunk_start_time),
            sample_rate=sample_rate,
            channels=channels,
        )

        # Add to batch
        chunks_to_insert.append(audio_chunk)

        # Update stats
        total_original_size += len(chunk_pcm)
        total_compressed_size += len(opus_data)
        chunk_index += 1
        offset = chunk_end

        logger.debug(
            f"💾 Prepared chunk {chunk_index}: "
            f"{len(chunk_pcm)} → {len(opus_data)} bytes"
        )

        # Flush batch to MongoDB when batch size reached
        if len(chunks_to_insert) >= BATCH_INSERT_SIZE:
            await AudioChunkDocument.insert_many(chunks_to_insert)
            logger.info(
                f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
                f"(chunks {chunk_index - len(chunks_to_insert)}-{chunk_index - 1})"
            )
            chunks_to_insert = []

    # Insert remaining chunks
    if chunks_to_insert:
        await AudioChunkDocument.insert_many(chunks_to_insert)
        logger.info(
            f"✅ Batch inserted {len(chunks_to_insert)} chunks to MongoDB "
            f"({total_duration_seconds:.1f}s audio)"
        )

    compression_ratio = (
        total_compressed_size / total_original_size if total_original_size > 0 else 0.0
    )
    stored = (
        await AudioChunkDocument.find(
            AudioChunkDocument.capture_session_id == capture_session_id
        )
        .sort("+sequence")
        .to_list()
    )
    audio_range = AudioRangeRef(
        capture_source_id=capture_source_id,
        time_basis=time_basis,
        capture_session_ids=[capture_session_id],
        chunk_ids=[str(chunk.id) for chunk in stored],
        started_at=captured_at,
        ended_at=ended_at,
    )
    capture.status = "complete"
    capture.ended_at = ended_at
    await capture.save()

    logger.info(
        f"✅ Converted audio to {chunk_index} MongoDB chunks: "
        f"{total_original_size / 1024 / 1024:.2f} MB → "
        f"{total_compressed_size / 1024 / 1024:.2f} MB "
        f"(compression: {compression_ratio:.3f}, "
        f"{(1 - compression_ratio) * 100:.1f}% savings)"
    )

    return CaptureIngestResult(
        capture_session_id=capture_session_id,
        audio_range=audio_range,
        chunk_count=chunk_index,
        duration_seconds=total_duration_seconds,
        compression_ratio=compression_ratio,
    )


async def convert_wav_to_chunks(
    *,
    user_id: str,
    capture_source_id: str,
    wav_file_path: Path,
    chunk_duration: float = 10.0,
    captured_at: Optional[datetime] = None,
    capture_session_id: Optional[str] = None,
    origin: str = "upload",
    external_source_id: Optional[str] = None,
    data_purpose: str = "normal_capture",
) -> CaptureIngestResult:
    """
    Convert an existing WAV file to MongoDB audio chunks.

    DEPRECATED: Use convert_audio_to_chunks() instead to avoid disk I/O.

    Used for uploaded audio files to ensure consistency with streaming audio storage.
    Reads WAV file, splits into 10-second chunks, encodes to Opus, and stores in MongoDB.

    Args:
        user_id: Owner of the capture evidence
        capture_source_id: Stable device/channel identity
        wav_file_path: Path to existing WAV file
        chunk_duration: Duration of each chunk in seconds (default: 10.0)

    Returns:
        Capture identity, range claim, and storage statistics

    Raises:
        FileNotFoundError: If WAV file doesn't exist

    Example:
        >>> # Convert uploaded file to chunks
        >>> result = await convert_wav_to_chunks(
        ...     user_id="user-id",
        ...     capture_source_id="upload-device",
        ...     wav_file_path=Path("/path/to/uploaded.wav")
        ... )
        >>> print(f"Created {result.chunk_count} chunks")
    """
    if not wav_file_path.exists():
        raise FileNotFoundError(f"WAV file not found: {wav_file_path}")

    logger.info(f"📦 Converting WAV file to MongoDB chunks: {wav_file_path}")

    # Read WAV file
    with wave.open(str(wav_file_path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        total_frames = wav.getnframes()

        # Read all PCM data
        pcm_data = wav.readframes(total_frames)

    logger.info(
        f"📁 Read WAV: {len(pcm_data)} bytes PCM, "
        f"{sample_rate}Hz, {channels}ch, {sample_width*8}-bit"
    )

    return await convert_audio_to_chunks(
        user_id=user_id,
        capture_source_id=capture_source_id,
        audio_data=pcm_data,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        chunk_duration=chunk_duration,
        captured_at=captured_at,
        capture_session_id=capture_session_id,
        origin=origin,
        external_source_id=external_source_id,
        data_purpose=data_purpose,
    )


async def wait_for_audio_chunks(
    conversation_id: str,
    max_wait_seconds: int = 30,
    min_chunks: int = 1,
) -> bool:
    """
    Wait for MongoDB audio chunks to be available for a conversation.

    Replaces wait_for_audio_file() for MongoDB-based storage.
    Polls MongoDB until chunks exist or timeout occurs.

    Args:
        conversation_id: Conversation ID to check
        max_wait_seconds: Maximum wait time in seconds (default: 30)
        min_chunks: Minimum number of chunks required (default: 1)

    Returns:
        True if chunks are available, False if timeout

    Example:
        >>> # Wait for chunks before transcription
        >>> if await wait_for_audio_chunks(conversation_id):
        ...     await transcribe_full_audio_job(...)
        ... else:
        ...     logger.error("No audio chunks available")
    """
    wait_start = time.time()

    while time.time() - wait_start < max_wait_seconds:
        # Query chunk count
        try:
            chunks = await retrieve_audio_chunks(
                conversation_id=conversation_id,
                start_index=0,
                limit=1,  # Just check if any exist
            )
        except AudioClaimError:
            # A deliberate Conversation may be visible while its capture ingest is
            # still committing and before the range claim is attached.
            chunks = []

        if len(chunks) >= min_chunks:
            wait_duration = time.time() - wait_start
            logger.info(
                f"✅ Audio chunks ready for conversation {conversation_id[:12]} "
                f"after {wait_duration:.1f}s ({len(chunks)} chunks found)"
            )
            return True

        # Log progress every 5 seconds
        elapsed = time.time() - wait_start
        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
            logger.info(
                f"⏳ Waiting for audio chunks (conversation {conversation_id[:12]})... "
                f"({elapsed:.0f}s elapsed)"
            )

        await asyncio.sleep(0.5)  # Check every 500ms

    # Log at WARNING (not ERROR): this generic util doesn't know *why* the session
    # ended. A benign client disconnect (device walked out of range — no audio ever
    # persisted) and a genuine persistence failure both land here. The caller knows
    # the end reason and emits the severity-appropriate line, so emitting ERROR here
    # would raise a spurious system event for every dead-zone reconnect.
    logger.warning(
        f"⏱️ Audio chunks not found after {max_wait_seconds}s "
        f"(conversation: {conversation_id[:12]}) — caller decides how to handle"
    )
    return False


# Peak target for a normalized preview. Not full scale: a little headroom keeps a
# boosted clip from clipping on playback.
PREVIEW_PEAK = 0.89
# Beyond this the source is silence, and multiplying silence only produces loud
# silence while making the noise floor sound like a fault.
MAX_PREVIEW_GAIN = 200.0


def normalize_wav_peak(wav: bytes) -> tuple[bytes, float]:
    """Peak-normalize a WAV clip, returning it with the applied gain in dB.

    Written for review tools that ask a human to judge audio. The clips worth judging
    are often far below the ones that behave normally — measured across one episode,
    windows where VAD reported speech but the transcriber produced nothing ran -43 to
    -58 dBFS RMS, against -15 to -42 for windows that transcribed cleanly. Played raw
    those are indistinguishable from silence, and a listener concludes the tool is
    broken rather than that the audio is quiet.

    The gain is returned rather than swallowed, because it is itself evidence: "this
    needed +42 dB" says something about the window, and anyone judging loudness has to
    know they are not hearing the original level.

    Non-16-bit input is returned untouched with zero gain.
    """

    with wave.open(io.BytesIO(wav)) as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    if params.sampwidth != 2 or not frames:
        return wav, 0.0
    samples = array.array("h")
    samples.frombytes(frames)
    peak = max((abs(value) for value in samples), default=0)
    if peak == 0:
        return wav, 0.0
    gain = min(PREVIEW_PEAK * 32767 / peak, MAX_PREVIEW_GAIN)
    if gain <= 1.0:
        return wav, 0.0
    boosted = array.array(
        "h", (max(-32768, min(32767, int(value * gain))) for value in samples)
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as sink:
        sink.setparams(params)
        sink.writeframes(boosted.tobytes())
    return out.getvalue(), round(20 * math.log10(gain), 1)
