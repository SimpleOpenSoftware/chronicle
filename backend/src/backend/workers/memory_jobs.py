"""
Memory-related RQ job functions.

This module contains jobs related to memory extraction and processing.

Supports two processing pathways:
1. **Normal extraction**: Extracts fresh facts from transcript, deduplicates
   against existing user memories, and proposes ADD/UPDATE/DELETE actions.
2. **Speaker reprocess**: When triggered after speaker re-identification,
   computes a diff between old and new speaker labels, fetches existing
   conversation-specific memories, and asks the LLM to make targeted
   corrections to speaker attribution in those memories.
"""

import logging
import re
import time
from typing import Any, Dict, List

from rq import get_current_job

import backend.services.privacy as privacy
from backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
    post_conv_enqueue_kwargs,
)
from backend.models.conversation import Conversation
from backend.models.job import JobPriority, async_job
from backend.observability.otel_setup import (
    set_otel_session,
    set_span_attrs,
    set_trace_io,
    traced_job,
)
from backend.plugins.events import PluginEvent
from backend.services.memory import get_memory_service
from backend.services.memory.audit import MemoryCause, UpdateStrategy, memory_provenance
from backend.services.memory.telemetry import text_payload
from backend.services.plugin_service import (
    dispatch_or_defer_space_event,
    dispatch_plugin_event,
)
from backend.services.recording_purpose import is_personal_recording
from backend.services.sse_publisher import publish_sse_event
from backend.users import get_user_by_id

logger = logging.getLogger(__name__)

MIN_CONVERSATION_LENGTH = 10
_OVERLAP_TOKEN = re.compile(r"[^\w']+")


def _normalise_overlap_token(token: str) -> str:
    return _OVERLAP_TOKEN.sub("", token).casefold()


def _trim_repeated_prefix(previous: str, current: str) -> str:
    """Remove a repeated word suffix/prefix caused by overlapping ASR windows."""
    previous_words = previous.split()
    current_words = current.split()
    limit = min(len(previous_words), len(current_words), 80)
    for size in range(limit, 2, -1):
        left = [_normalise_overlap_token(word) for word in previous_words[-size:]]
        right = [_normalise_overlap_token(word) for word in current_words[:size]]
        if left == right and all(left):
            return " ".join(current_words[size:]).strip()
    return current.strip()


def build_memory_transcript(
    segments: list, raw_transcript: str | None
) -> tuple[str, set[str]]:
    """Build bounded, speaker-labelled memory input from transcript segments.

    Provider window overlap is trimmed only for temporally adjacent speech segments.
    If the resulting segment text is still much larger than the durable raw transcript,
    the raw transcript wins so duplicated windows cannot multiply LLM input and facts.
    """
    dialogue_lines: list[str] = []
    transcript_speakers: set[str] = set()
    previous_speech_text = ""
    previous_speech_end: float | None = None

    for segment in sorted(segments or [], key=lambda item: (item.start, item.end)):
        text = segment.text.strip()
        speaker = segment.speaker
        seg_type = getattr(segment, "segment_type", "speech")
        if not text:
            continue
        if seg_type == "event":
            dialogue_lines.append(f"[{text}]" if not text.startswith("[") else text)
            continue
        if seg_type == "note":
            dialogue_lines.append(f"[Note: {text}]")
            continue

        if (
            previous_speech_end is not None
            and segment.start <= previous_speech_end + 1.5
        ):
            text = _trim_repeated_prefix(previous_speech_text, text)
        if text:
            dialogue_lines.append(f"{speaker}: {text}")
        previous_speech_text = segment.text.strip()
        previous_speech_end = segment.end
        if speaker and not speaker.casefold().startswith("unknown"):
            transcript_speakers.add(speaker.strip().lower())

    assembled = "\n".join(dialogue_lines)
    raw = raw_transcript.strip() if isinstance(raw_transcript, str) else ""
    if raw and len(assembled) > max(int(len(raw) * 1.75), len(raw) + 1000):
        logger.warning(
            "Memory transcript segments amplified source text (%d vs %d chars); "
            "using raw transcript",
            len(assembled),
            len(raw),
        )
        return raw, transcript_speakers
    return assembled, transcript_speakers


def compute_speaker_diff(
    old_segments: list,
    new_segments: list,
) -> List[Dict[str, Any]]:
    """Compare old and new transcript segments to identify speaker changes.

    Matches segments by time overlap and detects where speaker labels differ.

    Args:
        old_segments: Segments from the previous transcript version
        new_segments: Segments from the new (active) transcript version

    Returns:
        List of change dicts, each with keys:
        - ``type``: "speaker_change", "text_change", or "new_segment"
        - ``text``: The segment text
        - ``old_speaker`` / ``new_speaker``: For speaker changes
        - ``old_text`` / ``new_text``: For text changes
        - ``start`` / ``end``: Time boundaries
    """
    changes: List[Dict[str, Any]] = []

    for new_seg in new_segments:
        new_start = new_seg.start
        new_end = new_seg.end

        # Find best matching old segment by time overlap
        best_match = None
        best_overlap = 0.0

        for old_seg in old_segments:
            overlap_start = max(old_seg.start, new_start)
            overlap_end = min(old_seg.end, new_end)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_match = old_seg

        if best_match:
            # Check for speaker change
            if best_match.speaker != new_seg.speaker:
                changes.append(
                    {
                        "type": "speaker_change",
                        "text": new_seg.text.strip(),
                        "old_speaker": best_match.speaker,
                        "new_speaker": new_seg.speaker,
                        "start": new_start,
                        "end": new_end,
                    }
                )
            # Check for text change (less common in speaker reprocessing)
            if best_match.text.strip() != new_seg.text.strip():
                changes.append(
                    {
                        "type": "text_change",
                        "old_text": best_match.text.strip(),
                        "new_text": new_seg.text.strip(),
                        "speaker": new_seg.speaker,
                        "start": new_start,
                        "end": new_end,
                    }
                )
        else:
            # No matching old segment found
            changes.append(
                {
                    "type": "new_segment",
                    "text": new_seg.text.strip(),
                    "speaker": new_seg.speaker,
                    "start": new_start,
                    "end": new_end,
                }
            )

    return changes


@async_job(redis=True, beanie=True)
@traced_job(
    "memory_extraction", pipeline_stage="memory_extraction", gen_ai_operation="chat"
)
async def process_memory_job(
    conversation_id: str, *, redis_client=None
) -> Dict[str, Any]:
    """
    RQ job function for memory extraction and processing from conversations.

    V2 Architecture:
        1. Extracts memories from conversation transcript
        2. Checks primary speakers filter if configured
        3. Uses the Chronicle memory provider (agentic vault)
        4. Stores memory references in conversation document

    Note: Listening jobs are restarted by open_conversation_job (not here).
    This allows users to resume talking immediately after conversation closes,
    without waiting for memory processing to complete.

    Args:
        conversation_id: Conversation ID to process
        redis_client: Redis client (injected by decorator)

    Returns:
        Dict with processing results
    """

    await privacy.require_conversation(conversation_id)
    set_otel_session(conversation_id)
    start_time = time.time()
    logger.info(f"🔄 Starting memory processing for conversation {conversation_id}")

    # Get conversation data
    conversation_model = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation_model:
        logger.warning(f"No conversation found for {conversation_id}")
        return {"success": False, "error": "Conversation not found"}

    # This is the final safety boundary. Scheduling paths should avoid enqueueing
    # memory work for annotation datasets, but a stale or manual job must still be
    # unable to mutate the user's vault.
    if not is_personal_recording(conversation_model):
        return {
            "success": True,
            "skipped": True,
            "reason": "training_only",
            "conversation_id": conversation_id,
        }
    if conversation_model.memory_excluded:
        logger.info(
            f"Skipping memory processing for excluded conversation {conversation_id}"
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "memory_excluded",
            "conversation_id": conversation_id,
        }

    # Get client_id, user_id, and user_email from conversation/user
    client_id = conversation_model.client_id
    user_id = conversation_model.user_id
    memory_space_id = getattr(conversation_model, "memory_space_id", None)

    user = await get_user_by_id(user_id)
    if user:
        user_email = user.email
    else:
        logger.warning(f"Could not find user {user_id}")
        user_email = ""

    set_span_attrs(user_id=str(user_id), client_id=client_id)
    logger.info(
        f"🔄 Processing memory for conversation {conversation_id}, client={client_id}, user={user_id}"
    )

    full_conversation, transcript_speakers = build_memory_transcript(
        conversation_model.segments,
        conversation_model.transcript,
    )
    source_images: list[tuple[str, bytes]] = []
    if memory_space_id and conversation_model.memory_review_state == "extracting":
        # Lazy import keeps the optional vision path out of ordinary Main jobs.
        from backend.services.memory_space_context import (
            describe_selected_frames,
            selected_frames,
        )

        chosen_frames = selected_frames(conversation_model)
        source_images = [
            (f"frame-{frame.frame_id}.jpg", frame.data) for frame in chosen_frames
        ]
        if chosen_frames:
            try:
                visual_context = await describe_selected_frames(conversation_model)
            except Exception as exc:
                # The same pixels still go directly to multimodal write backends. A
                # failed description pass must not silently degrade to text-only.
                logger.warning(
                    "Selected screen-context description failed for %s: %s; "
                    "continuing with direct image attachments",
                    conversation_id,
                    exc,
                )
                visual_context = ""
            if visual_context:
                conversation_model.memory_context_description = visual_context
                await conversation_model.save()
                full_conversation = (
                    f"{full_conversation}\n\n"
                    "[User-selected screen evidence supporting this recording]\n"
                    f"{visual_context}"
                )

    # Fallback: if segments have no text content but transcript exists, use transcript
    # This handles cases where speaker recognition fails/is disabled
    if (
        len(full_conversation) < MIN_CONVERSATION_LENGTH
        and conversation_model.transcript
        and isinstance(conversation_model.transcript, str)
    ):
        logger.info(
            f"Segments empty or too short, falling back to transcript text for {conversation_id}"
        )
        full_conversation = conversation_model.transcript

    if len(full_conversation) < MIN_CONVERSATION_LENGTH:
        logger.warning(
            f"Conversation too short for memory processing: {conversation_id}"
        )
        if memory_space_id and conversation_model.memory_review_state == "extracting":
            conversation_model.memory_review_state = "failed"
            conversation_model.memory_review_error = "Conversation too short"
            await conversation_model.save()
        return {"success": False, "error": "Conversation too short"}

    set_trace_io(input={"transcript": text_payload(full_conversation)})

    # Check primary speakers filter (reuse `user` from above — no duplicate DB call)
    if user and user.primary_speakers:
        primary_speaker_names = {
            ps["name"].strip().lower() for ps in user.primary_speakers
        }

        if transcript_speakers and not transcript_speakers.intersection(
            primary_speaker_names
        ):
            logger.info(
                f"Skipping memory - no primary speakers found in conversation {conversation_id}"
            )
            if (
                memory_space_id
                and conversation_model.memory_review_state == "extracting"
            ):
                # The review has been consumed successfully even though the user's
                # existing speaker policy admitted no note content.
                conversation_model.memory_review_state = "extracted"
                conversation_model.memory_review_error = None
                await conversation_model.save()
            return {"success": True, "skipped": True, "reason": "No primary speakers"}

    # Read provenance from RQ job metadata. `cause` is descriptive (recorded on
    # the ledger); `strategy` is control flow (which update pathway runs). They
    # are independent — see services/memory/audit.py.
    current_rq_job = get_current_job()
    job_meta = current_rq_job.meta if current_rq_job and current_rq_job.meta else {}
    cause = job_meta.get("cause") or MemoryCause.AUTO_EXTRACTION.value
    strategy = job_meta.get("strategy") or UpdateStrategy.FULL.value

    memory_service = get_memory_service()

    # Never let an extraction error escape this job. It sits mid-chain
    # (recognize_speakers → memory → title_summary → event_complete), and under
    # RQ a raised exception marks the job failed and leaves every dependent job
    # DEFERRED FOREVER — so the conversation is stuck in "reprocessing" and never
    # gets a title/summary. Degrade gracefully (log + return failure dict) so the
    # chain continues. Common trigger: a long transcript exceeding the memory
    # LLM's context window (provider HTTP 400).
    try:
        with memory_provenance(cause, strategy):
            if strategy == UpdateStrategy.SPEAKER_DIFF:
                # === Speaker-diff pathway ===
                # Targeted update from the speaker-label diff between transcript
                # versions (used by speaker reprocess and diarization-annotation
                # apply); falls back to a full extraction if no diff is available.
                memory_result = await _process_speaker_diff_update(
                    memory_service=memory_service,
                    conversation_model=conversation_model,
                    full_conversation=full_conversation,
                    client_id=client_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    user_email=user_email,
                )
            else:
                # === Normal extraction pathway ===
                memory_result = await memory_service.add_memory(
                    full_conversation,
                    client_id,
                    conversation_id,
                    user_id,
                    user_email,
                    allow_update=True,
                    source_date=conversation_model.created_at.isoformat(),
                    source_duration_minutes=(
                        conversation_model.audio_total_duration / 60
                        if conversation_model.audio_total_duration is not None
                        else None
                    ),
                    source_title=conversation_model.title,
                    source_people=sorted(transcript_speakers),
                    source_images=source_images,
                    memory_space_id=memory_space_id,
                    admitted_space_write=True,
                )
    except Exception as e:
        logger.error(
            f"❌ Memory extraction failed for conversation {conversation_id} "
            f"(continuing chain so title/summary still runs): {e}",
            exc_info=True,
        )
        if memory_space_id and conversation_model.memory_review_state == "extracting":
            conversation_model.memory_review_state = "failed"
            conversation_model.memory_review_error = str(e)[:500]
            await conversation_model.save()
        return {"success": False, "error": f"Memory extraction error: {e}"}

    if memory_result:
        success, created_memory_ids = memory_result

        if success:
            processing_time = time.time() - start_time

            # Determine memory provider from memory service
            memory_provider = memory_service.provider_identifier

            # Vault changes are recorded in the memory_audit ledger by the provider
            # itself (see services/memory/audit.py); the job just surfaces the result.
            if created_memory_ids:
                publish_sse_event(
                    user_id,
                    "memory.processed",
                    {
                        "conversation_id": conversation_id,
                        "memory_count": len(created_memory_ids),
                    },
                )

                logger.info(
                    f"✅ Completed memory processing for conversation {conversation_id} - created {len(created_memory_ids)} memories in {processing_time:.2f}s"
                )

                # Update job metadata with memory information
                current_job = get_current_job()
                if current_job:
                    if not current_job.meta:
                        current_job.meta = {}

                    # Fetch memory details to display in UI
                    memory_details = []
                    try:
                        for memory_id in created_memory_ids[
                            :5
                        ]:  # Limit to first 5 for display
                            memory_entry = await memory_service.get_memory(
                                memory_id,
                                user_id,
                                memory_space_id=memory_space_id,
                            )
                            if memory_entry:
                                memory_details.append(
                                    {
                                        "memory_id": memory_id,
                                        "text": memory_entry.content[:200],
                                    }
                                )
                    except Exception as e:
                        logger.warning(f"Failed to fetch memory details for UI: {e}")

                    current_job.meta.update(
                        {
                            "conversation_id": conversation_id,
                            "memories_created": len(created_memory_ids),
                            "memory_ids": created_memory_ids[:5],  # Store first 5 IDs
                            "memory_details": memory_details,
                            "processing_time": processing_time,
                        }
                    )
                    current_job.save_meta()
            else:
                logger.info(
                    f"ℹ️ Memory processing completed for conversation {conversation_id} - no new memories created (deduplication) in {processing_time:.2f}s"
                )

            # NOTE: Listening jobs are restarted by open_conversation_job (not here)
            # This allows users to resume talking immediately after conversation closes,
            # without waiting for memory processing to complete.

            # Trigger memory-level plugins (ALWAYS dispatch when success, even with 0 new memories)
            memory_count = len(created_memory_ids) if created_memory_ids else 0
            try:
                event_data = {
                    "memories": created_memory_ids or [],
                    "conversation": {
                        "conversation_id": conversation_id,
                        "client_id": client_id,
                        "user_id": user_id,
                        "user_email": user_email,
                    },
                    "memory_count": memory_count,
                    "conversation_id": conversation_id,
                }
                event_metadata = {
                    "processing_time": processing_time,
                    "memory_provider": memory_provider,
                }
                description = (
                    f"conversation={conversation_id[:12]}, memories={memory_count}"
                )
                if memory_space_id:
                    await dispatch_or_defer_space_event(
                        event=PluginEvent.MEMORY_PROCESSED,
                        user_id=user_id,
                        memory_space_id=memory_space_id,
                        source_kind="conversation",
                        source_id=conversation_id,
                        data=event_data,
                        metadata=event_metadata,
                        description=description,
                    )
                else:
                    await dispatch_plugin_event(
                        event=PluginEvent.MEMORY_PROCESSED,
                        user_id=user_id,
                        data=event_data,
                        metadata=event_metadata,
                        description=description,
                    )
            except Exception as e:
                logger.warning(f"⚠️ Error triggering memory-level plugins: {e}")

            result = {
                "success": True,
                "memories_created": (
                    len(created_memory_ids) if created_memory_ids else 0
                ),
                "processing_time": processing_time,
            }
            if (
                memory_space_id
                and conversation_model.memory_review_state == "extracting"
            ):
                conversation_model.memory_review_state = "extracted"
                conversation_model.memory_review_error = None
                await conversation_model.save()
            set_trace_io(output=result)
            return result
        else:
            # Memory extraction failed
            if (
                memory_space_id
                and conversation_model.memory_review_state == "extracting"
            ):
                conversation_model.memory_review_state = "failed"
                conversation_model.memory_review_error = (
                    "Memory extraction returned failure"
                )
                await conversation_model.save()
            return {"success": False, "error": "Memory extraction returned failure"}
    else:
        if memory_space_id and conversation_model.memory_review_state == "extracting":
            conversation_model.memory_review_state = "failed"
            conversation_model.memory_review_error = "Memory service returned no result"
            await conversation_model.save()
        return {"success": False, "error": "Memory service returned False"}


async def _process_speaker_diff_update(
    memory_service,
    conversation_model,
    full_conversation: str,
    client_id: str,
    conversation_id: str,
    user_id: str,
    user_email: str,
):
    """Handle memory reprocessing after speaker re-identification.

    Computes the diff between the previous and current transcript versions
    (specifically speaker label changes), then delegates to the memory
    service's ``reprocess_memory`` method for targeted updates.

    Falls back to normal ``add_memory`` if diff computation fails or
    no meaningful changes are detected.

    Args:
        memory_service: Active memory service instance
        conversation_model: Conversation Beanie document
        full_conversation: New transcript as dialogue lines
        client_id: Client identifier
        conversation_id: Conversation identifier
        user_id: User identifier
        user_email: User email

    Returns:
        Tuple of (success, memory_ids) matching ``add_memory`` return type
    """
    active_version = conversation_model.active_transcript
    memory_space_id = getattr(conversation_model, "memory_space_id", None)

    if not active_version:
        logger.warning(
            f"🔄 Reprocess: no active transcript version for {conversation_id}, "
            f"falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation,
            client_id,
            conversation_id,
            user_id,
            user_email,
            allow_update=True,
            memory_space_id=memory_space_id,
            admitted_space_write=True,
        )

    # Find the source (previous) transcript version from metadata
    source_version_id = active_version.metadata.get("source_version_id")

    if not source_version_id:
        logger.warning(
            f"🔄 Reprocess: no source_version_id in active transcript metadata "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation,
            client_id,
            conversation_id,
            user_id,
            user_email,
            allow_update=True,
            memory_space_id=memory_space_id,
            admitted_space_write=True,
        )

    # Find the source version's segments
    source_version = None
    for v in conversation_model.transcript_versions:
        if v.version_id == source_version_id:
            source_version = v
            break

    if not source_version or not source_version.segments:
        logger.warning(
            f"🔄 Reprocess: source version {source_version_id} not found or has no segments "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation,
            client_id,
            conversation_id,
            user_id,
            user_email,
            allow_update=True,
            memory_space_id=memory_space_id,
            admitted_space_write=True,
        )

    # Compute the speaker diff
    transcript_diff = compute_speaker_diff(
        source_version.segments,
        active_version.segments,
    )

    if not transcript_diff:
        logger.info(
            f"🔄 Reprocess: no speaker changes detected between versions "
            f"for {conversation_id}, falling back to normal extraction"
        )
        return await memory_service.add_memory(
            full_conversation,
            client_id,
            conversation_id,
            user_id,
            user_email,
            allow_update=True,
            memory_space_id=memory_space_id,
            admitted_space_write=True,
        )

    # Build the previous transcript for context
    previous_lines = []
    for seg in source_version.segments:
        text = seg.text.strip()
        if text:
            previous_lines.append(f"{seg.speaker}: {text}")
    previous_transcript = "\n".join(previous_lines)

    logger.info(
        f"🔄 Reprocess: detected {len(transcript_diff)} changes "
        f"(speakers reprocessed) for {conversation_id}"
    )

    # Use the reprocess pathway
    return await memory_service.reprocess_memory(
        transcript=full_conversation,
        client_id=client_id,
        source_id=conversation_id,
        user_id=user_id,
        user_email=user_email,
        transcript_diff=transcript_diff,
        previous_transcript=previous_transcript,
        memory_space_id=memory_space_id,
        admitted_space_write=True,
    )


def enqueue_memory_processing(
    conversation_id: str,
    priority: JobPriority = JobPriority.NORMAL,
    *,
    cause: MemoryCause = MemoryCause.AUTO_EXTRACTION,
    strategy: UpdateStrategy = UpdateStrategy.FULL,
    depends_on=None,
    job_id: str | None = None,
    job_timeout: int | None = None,
):
    """
    Enqueue a memory processing job.

    The job fetches all needed data (client_id, user_id, user_email) from the
    conversation document internally, so only conversation_id is needed.

    ``cause`` records *why* the memory is being (re)processed on the audit ledger;
    ``strategy`` selects *how* the vault is updated (full re-extraction vs. a
    targeted speaker-label diff). See services/memory/audit.py.

    Returns RQ Job object for tracking.
    """
    timeout_mapping = {
        JobPriority.URGENT: 3600,  # 60 minutes
        JobPriority.HIGH: 2400,  # 40 minutes
        JobPriority.NORMAL: 1800,  # 30 minutes
        JobPriority.LOW: 900,  # 15 minutes
    }

    # job_id uses [:12] to match the deterministic id the post-conversation chain and
    # _clear_post_conversation_chain use — so a standalone re-enqueue collides with
    # (replaces) the chain's memory job rather than creating an orphan twin.
    resolved_job_id = job_id or f"memory_{conversation_id[:12]}"
    resolved_timeout = job_timeout or timeout_mapping.get(priority, 1800)
    job = memory_queue.enqueue(
        process_memory_job,
        conversation_id,  # Only argument needed - job fetches conversation data internally
        job_timeout=resolved_timeout,
        result_ttl=JOB_RESULT_TTL,
        job_id=resolved_job_id,
        description=f"Process memory for conversation {conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "memory",
            {
                "conversation_id": conversation_id,
                "cause": cause.value,
                "strategy": strategy.value,
            },
            depends_on=depends_on,
        ),
    )

    logger.info(
        f"📥 RQ: Enqueued memory job {job.id} for conversation {conversation_id}"
    )
    return job
