"""Persist immutable processing evidence and mutable Conversation projections.

Transcript and diarization artifacts own provider output. A Conversation revision is
the immutable derived history used by recordings, memory, and Timeline consumers. The
embedded ``transcript_versions`` list is a bounded read cache of provider sources plus
the active projection; repeated derived attempts are never accumulated there.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Sequence

from pymongo.errors import DuplicateKeyError

import backend.services.privacy as privacy
from backend.models.audio_capture import (
    AbsoluteWord,
    AudioRangeRef,
    ConversationTranscriptRevision,
    DiarizationArtifact,
    DiarizationTurn,
    TranscriptArtifact,
    TranscriptUtterance,
    as_utc,
)
from backend.models.conversation import Conversation
from backend.services.audio_claims import map_presentation_interval, range_duration


class ProcessingArtifactConflict(RuntimeError):
    """A retry key was reused for different immutable provider output."""


# Streaming ASR providers timestamp on codec/model frames, while the durable audio
# claim ends on the last decoded sample. A sub-frame overrun is provider quantization,
# not evidence that audio exists beyond the claim. Keep the generic claim mapper
# strict and reconcile only transcript evidence at this provider boundary.
_TRANSCRIPT_BOUNDARY_TOLERANCE_SECONDS = 0.5


def _content_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: (
            item.isoformat() if isinstance(item, datetime) else str(item)
        ),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def absolute_time_for_offset(
    ranges: Sequence[AudioRangeRef],
    offset_seconds: float,
    *,
    prefer_previous_boundary: bool = False,
) -> datetime:
    """Map gap-elided presentation seconds onto an absolute capture timestamp."""
    if not ranges:
        raise ValueError("cannot map a timestamp without audio ranges")
    offset = float(offset_seconds)
    total = range_duration(ranges)
    tolerance = 0.001
    if offset < -tolerance or offset > total + tolerance:
        raise ValueError(f"timestamp {offset} lies outside audio duration {total}")
    offset = min(max(offset, 0.0), total)

    cursor = 0.0
    for index, audio_range in enumerate(ranges):
        next_cursor = cursor + audio_range.duration_seconds
        on_end = abs(offset - next_cursor) <= tolerance
        if (
            offset < next_cursor
            or index == len(ranges) - 1
            or (on_end and prefer_previous_boundary)
        ):
            local = min(max(offset - cursor, 0.0), audio_range.duration_seconds)
            return as_utc(audio_range.started_at) + timedelta(seconds=local)
        cursor = next_cursor
    raise AssertionError("audio range offset mapping fell through")


def _absolute_word(
    ranges: Sequence[AudioRangeRef], word: dict[str, Any]
) -> AbsoluteWord:
    text = str(word.get("word", word.get("text", "")))
    start, end = _bounded_transcript_interval(
        ranges,
        float(word.get("start", 0.0)),
        float(word.get("end", word.get("start", 0.0))),
    )
    return AbsoluteWord(
        text=text,
        start_seconds=start,
        end_seconds=end,
        audio_spans=map_presentation_interval(ranges, start, end),
        confidence=word.get("confidence"),
        provider_speaker=(
            str(word["speaker"]) if word.get("speaker") is not None else None
        ),
    )


def _transcript_utterance(
    ranges: Sequence[AudioRangeRef], segment: dict[str, Any]
) -> TranscriptUtterance:
    start, end = _bounded_transcript_interval(
        ranges,
        float(segment.get("start", 0.0)),
        float(segment.get("end", segment.get("start", 0.0))),
    )
    return TranscriptUtterance(
        text=str(segment.get("text", "")),
        start_seconds=start,
        end_seconds=end,
        audio_spans=map_presentation_interval(ranges, start, end),
        words=[_absolute_word(ranges, word) for word in segment.get("words", []) or []],
        provider_speaker=(
            str(segment["speaker"]) if segment.get("speaker") is not None else None
        ),
        confidence=segment.get("confidence"),
    )


def _bounded_transcript_interval(
    ranges: Sequence[AudioRangeRef], start: float, end: float
) -> tuple[float, float]:
    total = range_duration(ranges)
    if (
        not math.isfinite(start)
        or not math.isfinite(end)
        or end < start
        or start < -_TRANSCRIPT_BOUNDARY_TOLERANCE_SECONDS
        or end > total + _TRANSCRIPT_BOUNDARY_TOLERANCE_SECONDS
    ):
        # Preserve the central mapper's precise invariant errors for real corruption.
        map_presentation_interval(ranges, start, end)
    return min(max(start, 0.0), total), min(max(end, 0.0), total)


def _capture_source_ids(ranges: Sequence[AudioRangeRef]) -> list[str]:
    return list(dict.fromkeys(item.capture_source_id for item in ranges))


async def resolve_transcript_artifact_ids(
    conversation_id: str,
    version: Conversation.TranscriptVersion,
) -> list[str]:
    """Resolve the immutable STT evidence behind one embedded version.

    New provider versions carry their artifact ID directly.  Cutover revisions retain
    the same relationship in the standalone revision store, while deliberately leaving
    the imported embedded payload unchanged.  Speaker projections must follow either
    representation so re-diarization never severs transcript provenance.
    """

    metadata = version.metadata or {}
    direct_ids = metadata.get("transcript_artifact_ids") or []
    if isinstance(direct_ids, str):
        direct_ids = [direct_ids]
    singular_id = metadata.get("transcript_artifact_id")
    if singular_id:
        direct_ids = [*direct_ids, singular_id]
    normalized = list(dict.fromkeys(str(item) for item in direct_ids if item))
    if normalized:
        return normalized

    revision = await ConversationTranscriptRevision.find_one(
        {
            "conversation_id": str(conversation_id),
            "metadata.source_version_id": str(version.version_id),
            "transcript_artifact_ids.0": {"$exists": True},
        }
    )
    if revision is None:
        return []
    return list(
        dict.fromkeys(str(item) for item in revision.transcript_artifact_ids if item)
    )


async def persist_transcript_artifact(
    *,
    user_id: str,
    audio_ranges: Sequence[AudioRangeRef],
    retry_key: str,
    provider: str,
    model: str | None,
    transcript: str,
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    raw_response: dict[str, Any] | None = None,
) -> TranscriptArtifact:
    """Persist one immutable ASR result, idempotently by processing retry key."""

    visibility = privacy.ConversationPrivacyFilter()
    if raw_response and "privacy_reference_receipt" in raw_response:
        await visibility.require_reference_receipt(
            user_id, raw_response["privacy_reference_receipt"]
        )
    if not audio_ranges:
        raise ValueError("transcript artifact requires an audio claim")
    payload = {
        "provider": provider,
        "model": model,
        "transcript": transcript,
        "words": words,
        "segments": segments,
    }
    if raw_response and "privacy_reference_receipt" in raw_response:
        payload["privacy_reference_receipt"] = raw_response["privacy_reference_receipt"]
    digest = _content_digest(payload)
    existing = await TranscriptArtifact.find_one(
        TranscriptArtifact.retry_key == retry_key
    )
    if existing is not None:
        if existing.raw_response.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"transcript retry key {retry_key!r} has different output"
            )
        await visibility.assert_current()
        return existing

    response = dict(raw_response or {})
    response["segments"] = segments
    response["relative_words"] = words
    response["relative_segments"] = segments
    response["content_sha256"] = digest
    artifact = TranscriptArtifact(
        retry_key=retry_key,
        user_id=str(user_id),
        capture_source_ids=_capture_source_ids(audio_ranges),
        audio_ranges=list(audio_ranges),
        provider=provider,
        model=model,
        transcript=transcript,
        words=[_absolute_word(audio_ranges, word) for word in words],
        utterances=[
            _transcript_utterance(audio_ranges, segment) for segment in segments
        ],
        raw_response=response,
    )
    try:
        async with visibility.publication():
            await artifact.insert()
        return artifact
    except DuplicateKeyError:
        winner = await TranscriptArtifact.find_one(
            TranscriptArtifact.retry_key == retry_key
        )
        if winner is None or winner.raw_response.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"transcript retry key {retry_key!r} raced with different output"
            )
        await visibility.assert_current()
        return winner


async def persist_diarization_artifact(
    *,
    user_id: str,
    audio_ranges: Sequence[AudioRangeRef],
    retry_key: str,
    provider: str,
    model: str | None,
    segments: list[dict[str, Any]],
    configuration: dict[str, Any],
) -> DiarizationArtifact:
    """Persist neural/provider speaker turns independently of transcript text."""
    if not audio_ranges:
        raise ValueError("diarization artifact requires an audio claim")
    payload = {
        "provider": provider,
        "model": model,
        "segments": segments,
        "configuration": configuration,
    }
    digest = _content_digest(payload)
    existing = await DiarizationArtifact.find_one(
        DiarizationArtifact.retry_key == retry_key
    )
    if existing is not None:
        if existing.configuration.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"diarization retry key {retry_key!r} has different output"
            )
        return existing

    stored_configuration = dict(configuration)
    stored_configuration["content_sha256"] = digest
    turns: list[DiarizationTurn] = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", segment.get("start", 0.0)))
        if end <= start:
            raise ValueError(
                f"diarization turn must have positive duration: {start}-{end}"
            )
        turns.append(
            DiarizationTurn(
                start_seconds=start,
                end_seconds=end,
                audio_spans=map_presentation_interval(audio_ranges, start, end),
                speaker=str(segment.get("speaker", "Unknown")),
                identified_as=segment.get("identified_as"),
                confidence=segment.get("confidence"),
                embedding=segment.get("embedding"),
            )
        )

    artifact = DiarizationArtifact(
        retry_key=retry_key,
        user_id=str(user_id),
        capture_source_ids=_capture_source_ids(audio_ranges),
        audio_ranges=list(audio_ranges),
        provider=provider,
        model=model,
        turns=turns,
        configuration=stored_configuration,
    )
    try:
        await artifact.insert()
        return artifact
    except DuplicateKeyError:
        winner = await DiarizationArtifact.find_one(
            DiarizationArtifact.retry_key == retry_key
        )
        if winner is None or winner.configuration.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"diarization retry key {retry_key!r} raced with different output"
            )
        return winner


async def persist_conversation_revision(
    conversation: Conversation,
    version: Conversation.TranscriptVersion,
    *,
    retry_key: str,
    transcript_artifact_ids: Sequence[str] = (),
    diarization_artifact_ids: Sequence[str] = (),
) -> ConversationTranscriptRevision:
    """Write an immutable revision and point the Conversation read model at it."""
    words = [word.model_dump(mode="json") for word in version.words]
    segments = [segment.model_dump(mode="json") for segment in version.segments]
    payload = {
        "conversation_id": conversation.conversation_id,
        "transcript_artifact_ids": list(transcript_artifact_ids),
        "diarization_artifact_ids": list(diarization_artifact_ids),
        "transcript": version.transcript or "",
        "words": words,
        "segments": segments,
        "provider": version.provider,
        "model": version.model,
        "diarization_source": version.diarization_source,
        "metadata": version.metadata,
    }
    digest = _content_digest(payload)
    existing = await ConversationTranscriptRevision.find_one(
        ConversationTranscriptRevision.retry_key == retry_key
    )
    if existing is not None:
        if existing.metadata.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"revision retry key {retry_key!r} has different output"
            )
        conversation.active_transcript_revision_id = existing.revision_id
        return existing

    metadata = dict(version.metadata)
    metadata["content_sha256"] = digest
    revision = ConversationTranscriptRevision(
        retry_key=retry_key,
        conversation_id=conversation.conversation_id,
        transcript_artifact_ids=list(transcript_artifact_ids),
        diarization_artifact_ids=list(diarization_artifact_ids),
        transcript=version.transcript or "",
        words=words,
        segments=segments,
        provider=version.provider,
        model=version.model,
        diarization_source=version.diarization_source,
        metadata=metadata,
    )
    try:
        await revision.insert()
    except DuplicateKeyError:
        winner = await ConversationTranscriptRevision.find_one(
            ConversationTranscriptRevision.retry_key == retry_key
        )
        if winner is None or winner.metadata.get("content_sha256") != digest:
            raise ProcessingArtifactConflict(
                f"revision retry key {retry_key!r} raced with different output"
            )
        revision = winner
    conversation.active_transcript_revision_id = revision.revision_id
    return revision


async def persist_timing_normalized_revision(
    conversation: Conversation,
    source_version: Conversation.TranscriptVersion,
    *,
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
    audio_duration: float,
) -> Conversation.TranscriptVersion:
    """Create an auditable operational view for harmless provider edge overhangs.

    The provider version and its raw artifact remain immutable.  The normalized view
    keeps their text/provider/model, clips only the already-validated timing copy, and
    points a standalone Conversation revision at the same transcript artifact.  A
    deterministic content-derived ID makes retries idempotent.

    The caller owns saving ``conversation`` after this returns.
    """

    normalized_payload = {
        "source_version_id": source_version.version_id,
        "audio_duration": float(audio_duration),
        "segments": segments,
        "words": words,
    }
    normalization_digest = _content_digest(normalized_payload)
    version_id = f"timing-normalized-{normalization_digest[:24]}"
    transcript_artifact_ids = await resolve_transcript_artifact_ids(
        conversation.conversation_id,
        source_version,
    )
    metadata: dict[str, Any] = {
        "reprocessing_type": "timing_normalization",
        "source_version_id": source_version.version_id,
        "trigger": "transcript_integrity_repair",
        "timing_validation": {
            "status": "normalized_edge_overhang",
            "tolerance_seconds": 1.0,
            "audio_duration": float(audio_duration),
            "normalization_sha256": normalization_digest,
        },
    }
    if "privacy_reference_receipt" in source_version.metadata:
        metadata["privacy_reference_receipt"] = source_version.metadata[
            "privacy_reference_receipt"
        ]
    if transcript_artifact_ids:
        metadata["transcript_artifact_ids"] = transcript_artifact_ids

    normalized_words = [Conversation.Word.model_validate(word) for word in words]
    normalized_segments = [
        Conversation.SpeakerSegment.model_validate(segment) for segment in segments
    ]
    version = conversation.get_transcript_version(version_id)
    if version is None:
        version = conversation.add_transcript_version(
            version_id=version_id,
            transcript=source_version.transcript or "",
            words=normalized_words,
            segments=normalized_segments,
            provider=source_version.provider,
            model=source_version.model,
            metadata=metadata,
            set_as_active=True,
        )
        version.diarization_source = source_version.diarization_source
    else:
        expected = {
            "transcript": source_version.transcript or "",
            "words": [word.model_dump(mode="json") for word in normalized_words],
            "segments": [
                segment.model_dump(mode="json") for segment in normalized_segments
            ],
            "provider": source_version.provider,
            "model": source_version.model,
            "diarization_source": source_version.diarization_source,
            "metadata": metadata,
        }
        actual = {
            "transcript": version.transcript or "",
            "words": [word.model_dump(mode="json") for word in version.words],
            "segments": [
                segment.model_dump(mode="json") for segment in version.segments
            ],
            "provider": version.provider,
            "model": version.model,
            "diarization_source": version.diarization_source,
            "metadata": version.metadata,
        }
        if _content_digest(actual) != _content_digest(expected):
            raise ProcessingArtifactConflict(
                f"timing-normalized version {version_id!r} has different output"
            )
        conversation.active_transcript_version = version_id

    await persist_conversation_revision(
        conversation,
        version,
        retry_key=(
            f"timing-normalization:{conversation.conversation_id}:"
            f"{source_version.version_id}:{normalization_digest}"
        ),
        transcript_artifact_ids=transcript_artifact_ids,
    )
    conversation.transcript_integrity_error = None
    return version


async def persist_word_timed_revision(
    conversation: Conversation,
    source_version: Conversation.TranscriptVersion,
    *,
    words: list[dict[str, Any]],
    method: str,
    audio_duration: float,
) -> Conversation.TranscriptVersion:
    """Persist derived word clocks for a provider transcript that had none.

    The provider text, utterances, and raw TranscriptArtifact remain immutable. The
    derived revision adds only the word clock needed to project text onto a fresh
    neural speaker timeline. ``method`` makes acoustic alignment distinguishable from
    the last-resort proportional estimate based on provider segment spans.

    The caller owns saving ``conversation`` after this returns.
    """

    if not words:
        raise ValueError("word-timed revision requires at least one timed word")
    if method not in {
        "forced_alignment",
        "segment_clock_estimate",
        "embedded_segment_words",
        "legacy_metadata_words",
        "provided_job_words",
    }:
        raise ValueError(f"unsupported word timing method: {method}")

    source_segments = [
        segment.model_dump(mode="python") for segment in source_version.segments
    ]
    timing_payload = {
        "source_version_id": source_version.version_id,
        "audio_duration": float(audio_duration),
        "method": method,
        "words": words,
    }
    timing_digest = _content_digest(timing_payload)
    version_id = f"word-timed-{timing_digest[:24]}"
    transcript_artifact_ids = await resolve_transcript_artifact_ids(
        conversation.conversation_id,
        source_version,
    )
    metadata: dict[str, Any] = {
        "reprocessing_type": "word_timing_synthesis",
        "source_version_id": source_version.version_id,
        "trigger": "speaker_projection_preflight",
        "word_timing": {
            "status": "synthesized",
            "method": method,
            "audio_duration": float(audio_duration),
            "timing_sha256": timing_digest,
        },
    }
    provider_capabilities = (source_version.metadata or {}).get("provider_capabilities")
    if "privacy_reference_receipt" in source_version.metadata:
        metadata["privacy_reference_receipt"] = source_version.metadata[
            "privacy_reference_receipt"
        ]
    if provider_capabilities:
        metadata["provider_capabilities"] = provider_capabilities
    if transcript_artifact_ids:
        metadata["transcript_artifact_ids"] = transcript_artifact_ids

    timed_words = [Conversation.Word.model_validate(word) for word in words]
    segments = [
        Conversation.SpeakerSegment.model_validate(segment)
        for segment in source_segments
    ]
    version = conversation.get_transcript_version(version_id)
    if version is None:
        version = conversation.add_transcript_version(
            version_id=version_id,
            transcript=source_version.transcript or "",
            words=timed_words,
            segments=segments,
            provider=source_version.provider,
            model=source_version.model,
            metadata=metadata,
            set_as_active=True,
        )
        version.diarization_source = source_version.diarization_source
    else:
        expected = {
            "transcript": source_version.transcript or "",
            "words": [word.model_dump(mode="json") for word in timed_words],
            "segments": [segment.model_dump(mode="json") for segment in segments],
            "provider": source_version.provider,
            "model": source_version.model,
            "diarization_source": source_version.diarization_source,
            "metadata": metadata,
        }
        actual = {
            "transcript": version.transcript or "",
            "words": [word.model_dump(mode="json") for word in version.words],
            "segments": [
                segment.model_dump(mode="json") for segment in version.segments
            ],
            "provider": version.provider,
            "model": version.model,
            "diarization_source": version.diarization_source,
            "metadata": version.metadata,
        }
        if _content_digest(actual) != _content_digest(expected):
            raise ProcessingArtifactConflict(
                f"word-timed version {version_id!r} has different output"
            )
        conversation.active_transcript_version = version_id

    await persist_conversation_revision(
        conversation,
        version,
        retry_key=(
            f"word-timing:{conversation.conversation_id}:"
            f"{source_version.version_id}:{timing_digest}"
        ),
        transcript_artifact_ids=transcript_artifact_ids,
    )
    return version
