"""
Cron job implementations for the Chronicle scheduler.

Jobs:
  - speaker_finetuning: sends applied diarization annotations to speaker service
  - asr_finetuning: exports annotated conversations to VibeVoice ASR for LoRA fine-tuning
  - asr_jargon_extraction: extracts jargon from recent memories, caches in Redis
"""

import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.constants import is_non_enrollable_speaker
from backend.llm_client import async_generate
from backend.model_registry import get_models_registry
from backend.models.annotation import Annotation, AnnotationType
from backend.models.conversation import Conversation
from backend.models.user import User
from backend.prompt_registry import get_prompt_registry
from backend.redis_factory import create_async_redis
from backend.services import privacy
from backend.services.memory import get_memory_service
from backend.services.speaker_enrollment import capture_evidence
from backend.services.transcription.context import cached_jargon, jargon_cache_key
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import (
    reconstruct_audio_segment,
    reconstruct_wav_from_conversation,
)

logger = logging.getLogger(__name__)

# TTL for cached jargon: 2 hours (job runs every 30 min, so always refreshed)
JARGON_CACHE_TTL = 7200

# Maximum number of recent memories to pull per user
MAX_RECENT_MEMORIES = 50

# How far back to look for memories (24 hours in seconds)
MEMORY_LOOKBACK_SECONDS = 86400


# ---------------------------------------------------------------------------
# Job 1: Speaker Fine-tuning
# ---------------------------------------------------------------------------


async def _record_failure(annotation, reason: str) -> None:
    """Persist a training failure on an annotation so it can be surfaced/cleared.

    Mirrors ``finetuning_routes._record_training_failure`` for the cron path: a
    failed annotation records its attempt count + reason instead of being silently
    re-tried forever, so the Fine-tuning page can show it and offer retry/discard.
    """
    annotation.training_attempts = (annotation.training_attempts or 0) + 1
    annotation.training_error = reason
    annotation.updated_at = datetime.now(timezone.utc)
    await annotation.save()


async def run_speaker_finetuning_job() -> dict:
    """Process applied diarization annotations and send to speaker recognition service.

    Invoked from the cron scheduler (Settings → Automation) to auto-enroll
    applied diarization relabels without an HTTP request. The deliberate,
    quality-gated manual path is ``/finetuning/enroll-selected`` (Data Audit).
    """
    # Find annotations ready for training
    annotations = await Annotation.find(
        Annotation.annotation_type == AnnotationType.DIARIZATION,
        Annotation.processed == True,
    ).to_list()

    ready_for_training = [
        a for a in annotations if not a.processed_by or "training" not in a.processed_by
    ]

    if not ready_for_training:
        logger.info("Speaker finetuning: no annotations ready for training")
        return {"processed": 0, "message": "No annotations ready for training"}

    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        logger.warning("Speaker finetuning: speaker recognition service is not enabled")
        return {"processed": 0, "message": "Speaker recognition service not enabled"}

    enrolled = 0
    appended = 0
    failed = 0
    cleaned = 0
    held = 0

    skipped = 0

    for annotation in ready_for_training:
        try:
            # Noise and placeholder "Unknown Speaker N" labels are not real people —
            # never enroll them as voiceprints. Mark trained so they aren't retried.
            if is_non_enrollable_speaker(annotation.corrected_speaker):
                annotation.processed_by = (
                    f"{annotation.processed_by},training"
                    if annotation.processed_by
                    else "training"
                )
                annotation.updated_at = datetime.now(timezone.utc)
                await annotation.save()
                skipped += 1
                continue

            conversation = await Conversation.find_one(
                Conversation.conversation_id == annotation.conversation_id
            )
            if not conversation or not conversation.active_transcript:
                logger.warning(
                    f"Conversation {annotation.conversation_id} not found — "
                    f"deleting orphaned annotation {annotation.id}"
                )
                await annotation.delete()
                cleaned += 1
                continue

            policy = await privacy.require_record(conversation)
            visibility = privacy.ConversationPrivacyFilter()
            visibility.snapshots[str(conversation.user_id)] = policy

            if annotation.segment_index >= len(conversation.active_transcript.segments):
                logger.warning(
                    f"Invalid segment index {annotation.segment_index} for "
                    f"conversation {annotation.conversation_id} — "
                    f"deleting orphaned annotation {annotation.id}"
                )
                await annotation.delete()
                cleaned += 1
                continue

            segment = conversation.active_transcript.segments[annotation.segment_index]

            evidence_records = await capture_evidence(
                visibility, [annotation.conversation_id]
            )
            wav_bytes = await reconstruct_audio_segment(
                conversation_id=annotation.conversation_id,
                start_time=segment.start,
                end_time=segment.end,
            )
            if not wav_bytes:
                failed += 1
                await _record_failure(annotation, "No audio for segment")
                continue

            await privacy.assert_current(str(conversation.user_id), policy)
            existing_speaker = await speaker_client.get_speaker_by_name(
                speaker_name=annotation.corrected_speaker,
                user_id=conversation.user_id,
            )

            await privacy.assert_current(str(conversation.user_id), policy)
            if existing_speaker:
                result = await speaker_client.append_to_speaker(
                    speaker_id=existing_speaker["id"],
                    audio_data=wav_bytes,
                    user_id=conversation.user_id,
                    speaker_name=annotation.corrected_speaker,
                    conversation_ids=[annotation.conversation_id],
                    visibility=visibility,
                    evidence_records=evidence_records,
                )
                await privacy.assert_current(str(conversation.user_id), policy)
                if "error" in result:
                    failed += 1
                    await _record_failure(
                        annotation, f"Append failed: {result.get('error')}"
                    )
                    continue
                if result.get("status") == "already_enrolled":
                    skipped += 1
                else:
                    appended += 1
            else:
                result = await speaker_client.enroll_new_speaker(
                    speaker_name=annotation.corrected_speaker,
                    audio_data=wav_bytes,
                    user_id=conversation.user_id,
                    conversation_ids=[annotation.conversation_id],
                    visibility=visibility,
                    evidence_records=evidence_records,
                )
                await privacy.assert_current(str(conversation.user_id), policy)
                if "error" in result:
                    failed += 1
                    await _record_failure(
                        annotation, f"Enroll failed: {result.get('error')}"
                    )
                    continue
                if result.get("status") == "already_enrolled":
                    skipped += 1
                else:
                    enrolled += 1

            async with visibility.publication():
                # Mark as trained (clear any prior failure record)
                annotation.processed_by = (
                    f"{annotation.processed_by},training"
                    if annotation.processed_by
                    else "training"
                )
                annotation.training_error = None
                annotation.updated_at = datetime.now(timezone.utc)
                await annotation.save()

        except privacy.PrivacyHeld:
            held += 1
            continue
        except Exception as e:
            logger.error(
                f"Speaker finetuning: error processing annotation {annotation.id}: {e}"
            )
            failed += 1
            try:
                await _record_failure(annotation, f"Exception: {str(e)[:50]}")
            except Exception:
                logger.error(
                    f"Speaker finetuning: failed to record failure for {annotation.id}"
                )

    total = enrolled + appended
    logger.info(
        f"Speaker finetuning complete: {total} processed "
        f"({enrolled} new, {appended} appended, {failed} failed, "
        f"{skipped} non-enrollable skipped, {cleaned} orphaned cleaned)"
    )
    return {
        "enrolled": enrolled,
        "appended": appended,
        "failed": failed,
        "skipped": skipped,
        "cleaned": cleaned,
        "processed": total,
        "privacy_held": held,
    }


# ---------------------------------------------------------------------------
# Job 2: ASR Fine-tuning (VibeVoice LoRA)
# ---------------------------------------------------------------------------

_ASR_TRAINING_MARKER = "asr_training"


def _build_vibevoice_label(conversation) -> dict:
    """Convert Chronicle conversation to VibeVoice training label format.

    Maps SpeakerSegment data to the JSON structure expected by VibeVoice's
    LoRA fine-tuning scripts: speaker ints, timestamped segments with text.
    """
    transcript = conversation.active_transcript
    if not transcript:
        return {}

    speaker_map: dict[str, int] = {}
    segments = []
    for seg in transcript.segments:
        speaker_id = speaker_map.setdefault(seg.speaker, len(speaker_map))
        segments.append(
            {
                "speaker": speaker_id,
                "text": seg.text,
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
            }
        )

    return {
        "audio_path": f"{conversation.conversation_id}.wav",
        "audio_duration": conversation.audio_total_duration,
        "segments": segments,
    }


async def run_asr_finetuning_job() -> dict:
    """Export annotated conversations to VibeVoice ASR service for LoRA fine-tuning.

    Finds transcript and diarization annotations that have been applied but not
    yet consumed by ASR training. Groups by conversation, reconstructs WAV audio,
    builds VibeVoice training labels, and POSTs to the ASR service's /fine-tune endpoint.
    """
    # Resolve STT service URL from model registry (same URL used for transcription)
    registry = get_models_registry()
    stt_model = registry.get_default("stt") if registry else None
    if not stt_model or not stt_model.model_url:
        logger.warning("ASR finetuning: no STT model configured in registry, skipping")
        return {
            "conversations_exported": 0,
            "annotations_consumed": 0,
            "message": "No STT model configured",
        }

    vibevoice_url = stt_model.resolved_url().rstrip("/")

    # Find applied annotations (TRANSCRIPT and DIARIZATION) not yet consumed by ASR training
    annotations = await Annotation.find(
        {
            "annotation_type": {
                "$in": [
                    AnnotationType.TRANSCRIPT.value,
                    AnnotationType.DIARIZATION.value,
                ]
            }
        },
        Annotation.processed == True,
    ).to_list()

    ready = [
        a
        for a in annotations
        if not a.processed_by or _ASR_TRAINING_MARKER not in a.processed_by
    ]

    if not ready:
        logger.info("ASR finetuning: no annotations ready for export")
        return {
            "conversations_exported": 0,
            "annotations_consumed": 0,
            "message": "No annotations ready",
        }

    # Group annotations by conversation_id
    by_conversation: dict[str, list[Annotation]] = {}
    for a in ready:
        if a.conversation_id:
            by_conversation.setdefault(a.conversation_id, []).append(a)

    errors = 0
    held = 0
    snapshots = []

    # Accumulate all conversations into a single batch for one POST
    all_files = []  # list of ("audio_files", (filename, BytesIO, mime))
    all_labels = []  # list of label dicts
    pending_annotations = []  # annotations to mark after success

    # Optionally load cached jargon for customized_context
    redis_client = create_async_redis(decode_responses=True)

    try:
        for conv_id, conv_annotations in by_conversation.items():
            try:
                conversation = await Conversation.find_one(
                    Conversation.conversation_id == conv_id
                )
                if not conversation or not conversation.active_transcript:
                    logger.warning(
                        f"ASR finetuning: conversation {conv_id} not found or no transcript"
                    )
                    errors += 1
                    continue

                policy = await privacy.require_record(conversation)
                owner = str(conversation.user_id)

                if not conversation.active_transcript.segments:
                    logger.info(
                        f"ASR finetuning: conversation {conv_id} has no segments, skipping"
                    )
                    continue

                # Reconstruct full WAV audio
                wav_data = await reconstruct_wav_from_conversation(conv_id)
                if not wav_data:
                    logger.warning(
                        f"ASR finetuning: no audio for conversation {conv_id}"
                    )
                    errors += 1
                    continue

                await privacy.assert_current(owner, policy)

                # Build training label
                label = _build_vibevoice_label(conversation)
                if not label.get("segments"):
                    logger.info(
                        f"ASR finetuning: no segments in label for {conv_id}, skipping"
                    )
                    continue

                # Try to add jargon context from Redis cache
                if conversation.user_id:
                    jargon, context_policy, _ = await cached_jargon(owner, redis_client)
                    if jargon:
                        snapshots.append((owner, context_policy))
                        label["customized_context"] = [
                            t.strip() for t in jargon.split(",") if t.strip()
                        ]

                all_files.append(
                    (
                        "audio_files",
                        (f"{conv_id}.wav", io.BytesIO(wav_data), "audio/wav"),
                    )
                )
                all_labels.append(label)
                pending_annotations.extend(conv_annotations)
                snapshots.append((owner, policy))

            except privacy.PrivacyHeld:
                held += 1
                continue
            except Exception as e:
                logger.error(
                    f"ASR finetuning: error processing conversation {conv_id}: {e}"
                )
                errors += 1

    finally:
        await redis_client.close()

    if not all_files:
        logger.info("ASR finetuning: no valid conversations to export")
        return {
            "conversations_exported": 0,
            "annotations_consumed": 0,
            "errors": errors,
            "privacy_held": held,
            "message": "No valid conversations to export",
        }

    # Single POST with all audio files and labels
    exported = 0
    consumed = 0

    async with httpx.AsyncClient(timeout=600) as client:
        try:
            for owner, snapshot in snapshots:
                await privacy.assert_current(owner, snapshot)
            response = await client.post(
                f"{vibevoice_url}/fine-tune",
                files=all_files,
                data={"labels": json.dumps(all_labels)},
            )

            for owner, snapshot in snapshots:
                await privacy.assert_current(owner, snapshot)

            if response.status_code == 200:
                exported = len(all_files)
                logger.info(
                    f"ASR finetuning: exported {exported} conversations in single batch"
                )

                # Mark all annotations as consumed
                for ann in pending_annotations:
                    for owner, snapshot in snapshots:
                        await privacy.assert_current(owner, snapshot)
                    ann.processed_by = (
                        f"{ann.processed_by},{_ASR_TRAINING_MARKER}"
                        if ann.processed_by
                        else _ASR_TRAINING_MARKER
                    )
                    ann.updated_at = datetime.now(timezone.utc)
                    await ann.save()
                    consumed += 1
            else:
                logger.error(
                    f"ASR finetuning: batch POST failed: " f"{response.status_code}"
                )
                errors += len(all_files)

        except privacy.PrivacyHeld:
            held += len(all_files)
        except Exception as e:
            logger.error("ASR finetuning: batch POST failed (%s)", type(e).__name__)
            errors += len(all_files)

    logger.info(
        f"ASR finetuning complete: {exported} conversations exported, "
        f"{consumed} annotations consumed, {errors} errors"
    )
    return {
        "conversations_exported": exported,
        "annotations_consumed": consumed,
        "errors": errors,
        "privacy_held": held,
    }


# ---------------------------------------------------------------------------
# Job 3: ASR Jargon Extraction
# ---------------------------------------------------------------------------


async def run_asr_jargon_extraction_job() -> dict:
    """Extract jargon from recent memories for all users and cache in Redis."""
    users = await User.find_all().to_list()
    processed = 0
    skipped = 0
    errors = 0

    redis_client = create_async_redis(decode_responses=True)
    try:
        for user in users:
            user_id = str(user.id)
            try:
                snapshot = await privacy.load_snapshot(user_id)
                await privacy.assert_current(user_id, snapshot)
                jargon = await _extract_jargon_for_user(user_id, snapshot)
                if jargon:
                    await privacy.assert_current(user_id, snapshot)
                    await redis_client.set(
                        jargon_cache_key(user_id, snapshot),
                        json.dumps(jargon),
                        ex=JARGON_CACHE_TTL,
                    )
                    await privacy.assert_current(user_id, snapshot)
                    processed += 1
                    logger.debug(
                        "Cached ASR vocabulary (%d characters)", len(jargon["text"])
                    )
                else:
                    skipped += 1
            except privacy.PrivacyHeld:
                skipped += 1
            except Exception as e:
                logger.error("Jargon extraction failed (%s)", type(e).__name__)
                errors += 1
    finally:
        await redis_client.close()

    logger.info(
        f"ASR jargon extraction complete: {processed} users processed, "
        f"{skipped} skipped, {errors} errors"
    )
    return {"users_processed": processed, "skipped": skipped, "errors": errors}


async def _extract_jargon_for_user(user_id: str, snapshot) -> Optional[dict]:
    """Pull recent memories, call LLM to extract jargon terms.

    Return terms and immutable note evidence, or None if nothing was found.
    """
    memory_service = get_memory_service()
    if memory_service is None:
        return None

    memories = await memory_service.get_all_memories(
        user_id=user_id,
        limit=MAX_RECENT_MEMORIES,
    )

    if not memories:
        return None

    # Concatenate memory content
    memory_text = "\n".join(m.content for m in memories if m.content)
    if not memory_text.strip():
        return None

    receipt = await privacy.vault_reference_receipt(
        user_id,
        [memory.id for memory in memories if memory.content],
        snapshot=snapshot,
    )

    # Use LLM to extract jargon
    registry = get_prompt_registry()
    prompt_template = await registry.get_prompt(
        "asr.jargon_extraction", memories=memory_text
    )

    await privacy.assert_current(user_id, snapshot)
    result = await async_generate(prompt_template)
    await privacy.assert_current(user_id, snapshot)

    # Clean up: strip whitespace, remove empty items
    if result:
        terms = [t.strip() for t in result.split(",") if t.strip()]
        if terms:
            return {"text": ", ".join(terms), "privacy_reference_receipt": receipt}

    return None
