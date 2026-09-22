"""
Annotation routes for Chronicle API.

Handles annotation CRUD operations for memories and transcripts.
Supports both user edits and AI-powered suggestions.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from backend.auth import current_active_user
from backend.constants import BACKGROUND_SPEECH_LABEL, NOISE_LABEL
from backend.controllers import background_bucket_controller
from backend.controllers.queue_controller import conversation_edit_chain_in_flight
from backend.models.annotation import (
    Annotation,
    AnnotationResponse,
    AnnotationSource,
    AnnotationStatus,
    AnnotationType,
    AnnotationUpdate,
    DeletionAnnotationCreate,
    DiarizationAnnotationCreate,
    InsertAnnotationCreate,
    MemoryAnnotationCreate,
    TimingAnnotationCreate,
    TitleAnnotationCreate,
    TranscriptAnnotationCreate,
)
from backend.models.conversation import Conversation
from backend.models.job import JobPriority
from backend.services import privacy
from backend.services.memory import get_memory_service
from backend.services.memory.audit import MemoryCause, UpdateStrategy
from backend.users import User
from backend.workers.memory_jobs import enqueue_memory_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])


async def _annotation_responses(annotations, user_id):
    """Copies of transcript/note text obey their original evidence policy."""
    visibility = privacy.ConversationPrivacyFilter()
    records = [
        {"conversation_id": a.conversation_id} for a in annotations if a.conversation_id
    ]
    permitted = {row["conversation_id"] for row in await visibility.filter(records)}
    note_snapshot = None
    visible = []
    for annotation in annotations:
        if annotation.conversation_id:
            if annotation.conversation_id in permitted:
                visible.append(annotation)
        elif (
            annotation.annotation_type == AnnotationType.MEMORY and annotation.memory_id
        ):
            if note_snapshot is None:
                note_snapshot = await privacy.load_snapshot(user_id)
            try:
                await privacy.guard_payload(
                    user_id, {"note_path": annotation.memory_id}, snapshot=note_snapshot
                )
            except privacy.PrivacyHeld:
                continue
            visible.append(annotation)
    await visibility.assert_current()
    if note_snapshot is not None:
        await privacy.assert_current(user_id, note_snapshot)
    return [AnnotationResponse.model_validate(a) for a in visible]


async def _annotation_privacy(annotation, user_id):
    """Bind an edit to the original evidence owner and one policy revision."""
    if annotation.conversation_id:
        record = await privacy.database().conversations.find_one(
            {"conversation_id": annotation.conversation_id},
            privacy._RECORD_PROJECTION,
        )
        if not record:
            raise privacy.PrivacyHeld()
        return str(record["user_id"]), await privacy.require_record(record)
    if annotation.annotation_type == AnnotationType.MEMORY and annotation.memory_id:
        owner = str(user_id)
        snapshot = await privacy.guard_payload(
            owner, {"note_path": annotation.memory_id}
        )
        return owner, snapshot
    raise privacy.PrivacyHeld()


async def _save_with_privacy(document, owner, snapshot):
    """Order database publication against policy changes and their invalidation.

    Keep the lock around the database write only. Memory jobs and other services
    may acquire the same publication lock independently.
    """
    async with privacy.distributed_lock(
        privacy.timeline_publication_lock(owner),
        timeout=120,
        blocking_timeout=30,
        renew=True,
    ):
        await privacy.assert_current(owner, snapshot)
        await document.save()
        await privacy.assert_current(owner, snapshot)


def _apply_diarization_label(segment, corrected_speaker: str) -> None:
    """Apply a diarization correction to a copied segment in place.

    The reserved NOISE_LABEL means "this is background/noise, not a person":
    relabel it, drop any prior identification, and reclassify it to a non-speech
    (event) segment so it falls out of speech∩speaker overlap and speech-only
    playback. Any other label is a normal speaker correction.
    """
    segment.speaker = corrected_speaker
    # A human correction supersedes the model identity claim.  Keeping the old
    # ``identified_as`` makes every consumer that prefers recognized identities
    # (including Data Audit speaker gates) continue to see the rejected name.
    segment.identified_as = None
    segment.confidence = None
    if corrected_speaker == NOISE_LABEL:
        segment.segment_type = Conversation.SegmentType.EVENT


def _should_reprocess_memory(conversation: Conversation) -> bool:
    """False for annotation/training imports that are explicitly excluded from memory."""
    return not getattr(conversation, "memory_excluded", False)


@router.get("/suggestions")
async def get_pending_suggestions(
    current_user: User = Depends(current_active_user),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Get pending AI-generated suggestions for the current user.

    Returns MODEL_SUGGESTION annotations with PENDING status,
    enriched with conversation context (title, transcript snippet,
    audio path) for the swipe review UI.
    """
    try:
        annotations = (
            await Annotation.find(
                Annotation.user_id == current_user.user_id,
                Annotation.source == AnnotationSource.MODEL_SUGGESTION,
                Annotation.status == AnnotationStatus.PENDING,
            )
            .sort("-created_at")
            .limit(limit)
            .to_list()
        )

        if not annotations:
            return []

        visibility = privacy.ConversationPrivacyFilter()
        permitted = {
            row["conversation_id"]
            for row in await visibility.filter(
                [{"conversation_id": a.conversation_id} for a in annotations]
            )
        }
        annotations = [a for a in annotations if a.conversation_id in permitted]

        # Batch-fetch conversations for context
        conversation_ids = list(
            {a.conversation_id for a in annotations if a.conversation_id}
        )
        conversations = await Conversation.find(
            {"conversation_id": {"$in": conversation_ids}},
        ).to_list()
        conv_map = {c.conversation_id: c for c in conversations}

        results = []
        for a in annotations:
            conv = conv_map.get(a.conversation_id)

            segment_start = None
            segment_end = None
            if conv and a.segment_index is not None:
                transcript = conv.active_transcript
                if (
                    transcript
                    and transcript.segments
                    and a.segment_index < len(transcript.segments)
                ):
                    seg = transcript.segments[a.segment_index]
                    segment_start = seg.start
                    segment_end = seg.end

            results.append(
                {
                    "id": a.id,
                    "annotation_type": a.annotation_type,
                    "conversation_id": a.conversation_id,
                    "segment_index": a.segment_index,
                    "original_text": a.original_text,
                    "corrected_text": a.corrected_text,
                    "created_at": a.created_at.isoformat(),
                    "conversation_title": conv.title if conv else None,
                    "transcript_snippet": _get_segment_context(conv, a.segment_index),
                    "segment_start": segment_start,
                    "segment_end": segment_end,
                }
            )

        await visibility.assert_current()
        return results

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching suggestions: {type(e).__name__}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch suggestions: {type(e).__name__}"
        )


def _get_segment_context(
    conversation, segment_index: int | None, context_size: int = 1
) -> str | None:
    """Get a snippet of transcript around the flagged segment for context."""
    if not conversation or segment_index is None:
        return None
    transcript = conversation.active_transcript
    if not transcript or not transcript.segments:
        return None
    start = max(0, segment_index - context_size)
    end = min(len(transcript.segments), segment_index + context_size + 1)
    lines = []
    for i in range(start, end):
        seg = transcript.segments[i]
        prefix = ">>> " if i == segment_index else "    "
        lines.append(f"{prefix}{seg.speaker}: {seg.text}")
    return "\n".join(lines)


@router.post("/memory", response_model=AnnotationResponse)
async def create_memory_annotation(
    annotation_data: MemoryAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for memory edit.

    - Validates user owns memory
    - Creates annotation record
    - Updates memory content in vector store
    - Re-embeds if content changed
    """
    try:
        privacy_owner = str(current_user.user_id)
        privacy_snapshot = await privacy.guard_payload(
            privacy_owner, {"note_path": annotation_data.memory_id}
        )
        await privacy.assert_current(privacy_owner, privacy_snapshot)
        memory_service = get_memory_service()

        # Verify memory ownership
        try:
            memory = await memory_service.get_memory(
                annotation_data.memory_id, current_user.user_id
            )
            await privacy.assert_current(privacy_owner, privacy_snapshot)
            if not memory:
                raise HTTPException(status_code=404, detail="Memory not found")
        except privacy.PrivacyHeld:
            raise
        except Exception as e:
            logger.error(f"Error fetching memory: {type(e).__name__}")
            raise HTTPException(status_code=404, detail="Memory not found")

        # Create annotation
        annotation = Annotation(
            annotation_type=AnnotationType.MEMORY,
            user_id=current_user.user_id,
            memory_id=annotation_data.memory_id,
            original_text=annotation_data.original_text,
            corrected_text=annotation_data.corrected_text,
            status=annotation_data.status,
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created memory annotation {annotation.id} for memory {annotation_data.memory_id}"
        )

        # Update memory content if accepted
        if annotation.status == AnnotationStatus.ACCEPTED:
            try:
                await privacy.assert_current(privacy_owner, privacy_snapshot)
                await memory_service.update_memory(
                    memory_id=annotation_data.memory_id,
                    content=annotation_data.corrected_text,
                    user_id=current_user.user_id,
                )
                await privacy.assert_current(privacy_owner, privacy_snapshot)
                logger.info(
                    f"Updated memory {annotation_data.memory_id} with corrected text"
                )
            except privacy.PrivacyHeld:
                raise
            except Exception as e:
                logger.error(f"Error updating memory: {type(e).__name__}")
                # Annotation is saved, but memory update failed - log but don't fail the request
                logger.warning(
                    f"Memory annotation {annotation.id} saved but memory update failed"
                )

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating memory annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create memory annotation: {type(e).__name__}",
        )


@router.post("/transcript", response_model=AnnotationResponse)
async def create_transcript_annotation(
    annotation_data: TranscriptAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for transcript segment edit.

    - Validates user owns conversation
    - Creates annotation record (NOT applied to transcript yet)
    - Annotation is marked as unprocessed (processed=False)
    - Visual indication in UI (pending badge)
    - Use unified apply endpoint to apply all annotations together
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        # Validate segment index
        active_transcript = conversation.active_transcript
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        segment = active_transcript.segments[annotation_data.segment_index]

        # Create annotation (NOT applied yet)
        annotation = Annotation(
            annotation_type=AnnotationType.TRANSCRIPT,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            original_text=segment.text,  # Use current segment text
            corrected_text=annotation_data.corrected_text,
            status=AnnotationStatus.PENDING,  # Changed from ACCEPTED
            processed=False,  # Not applied yet
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created transcript annotation {annotation.id} for conversation {annotation_data.conversation_id} segment {annotation_data.segment_index}"
        )

        # Do NOT modify transcript immediately
        # Do NOT trigger memory reprocessing yet
        # User must click "Apply Changes" button to apply all annotations together

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating transcript annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create transcript annotation: {type(e).__name__}",
        )


@router.get("/memory/{memory_id}", response_model=List[AnnotationResponse])
async def get_memory_annotations(
    memory_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all annotations for a memory."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.MEMORY,
            Annotation.memory_id == memory_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return await _annotation_responses(annotations, str(current_user.user_id))

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching memory annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch memory annotations: {type(e).__name__}",
        )


@router.get("/transcript/{conversation_id}", response_model=List[AnnotationResponse])
async def get_transcript_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all annotations for a conversation's transcript."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.TRANSCRIPT,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return await _annotation_responses(annotations, str(current_user.user_id))

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching transcript annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch transcript annotations: {type(e).__name__}",
        )


@router.patch("/{annotation_id}/status")
async def update_annotation_status(
    annotation_id: str,
    status: AnnotationStatus,
    current_user: User = Depends(current_active_user),
):
    """
    Accept or reject AI-generated suggestions.

    Used for pending model suggestions in the UI.
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        privacy_owner, privacy_snapshot = await _annotation_privacy(
            annotation, current_user.user_id
        )
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        old_status = annotation.status
        annotation.status = status
        annotation.updated_at = datetime.now(timezone.utc)

        # If accepting a pending suggestion, apply the correction
        if (
            status == AnnotationStatus.ACCEPTED
            and old_status == AnnotationStatus.PENDING
        ):
            # Promote to SPEECH_SUGGESTION_CORRECTION if user edited the AI suggestion
            if (
                annotation.source == AnnotationSource.MODEL_SUGGESTION
                and annotation.model_suggested_text is not None
                and annotation.is_transcript_annotation()
            ):
                annotation.annotation_type = AnnotationType.SPEECH_SUGGESTION_CORRECTION
                logger.info("Promoted annotation to SPEECH_SUGGESTION_CORRECTION")

            if annotation.is_memory_annotation():
                # Update memory
                try:
                    memory_service = get_memory_service()
                    await privacy.assert_current(privacy_owner, privacy_snapshot)
                    await memory_service.update_memory(
                        memory_id=annotation.memory_id,
                        content=annotation.corrected_text,
                        user_id=current_user.user_id,
                    )
                    await privacy.assert_current(privacy_owner, privacy_snapshot)
                    logger.info(f"Applied suggestion to memory {annotation.memory_id}")
                except privacy.PrivacyHeld:
                    raise
                except Exception as e:
                    logger.error(
                        f"Error applying memory suggestion: {type(e).__name__}"
                    )
                    # Don't fail the status update if memory update fails
            elif (
                annotation.is_transcript_annotation()
                or annotation.is_speech_suggestion_correction()
            ):
                # Update transcript segment (same logic for both TRANSCRIPT and SPEECH_SUGGESTION_CORRECTION)
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == annotation.conversation_id,
                        Conversation.user_id == annotation.user_id,
                    )
                    await privacy.assert_current(privacy_owner, privacy_snapshot)
                    if conversation:
                        transcript = conversation.active_transcript
                        if transcript and annotation.segment_index < len(
                            transcript.segments
                        ):
                            transcript.segments[annotation.segment_index].text = (
                                annotation.corrected_text
                            )
                            await _save_with_privacy(
                                conversation, privacy_owner, privacy_snapshot
                            )
                            logger.info(
                                f"Applied suggestion to transcript segment {annotation.segment_index}"
                            )
                except privacy.PrivacyHeld:
                    raise
                except Exception as e:
                    logger.error(
                        f"Error applying transcript suggestion: {type(e).__name__}"
                    )
                    # Don't fail the status update if segment update fails
            elif annotation.is_title_annotation():
                # Update conversation title
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == annotation.conversation_id,
                        Conversation.user_id == annotation.user_id,
                    )
                    await privacy.assert_current(privacy_owner, privacy_snapshot)
                    if conversation:
                        conversation.title = annotation.corrected_text
                        await _save_with_privacy(
                            conversation, privacy_owner, privacy_snapshot
                        )
                        logger.info(
                            f"Applied title suggestion to conversation {annotation.conversation_id}"
                        )
                except privacy.PrivacyHeld:
                    raise
                except Exception as e:
                    logger.error(f"Error applying title suggestion: {type(e).__name__}")
                    # Don't fail the status update if title update fails

        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(f"Updated annotation {annotation_id} status to {status}")

        return {
            "status": "updated",
            "annotation_id": annotation_id,
            "new_status": status,
        }

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error updating annotation status: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update annotation status: {type(e).__name__}",
        )


# === Generic Annotation Management ===


@router.delete("/{annotation_id}")
async def delete_annotation(
    annotation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Delete an unprocessed annotation.

    - Only allows deleting annotations that haven't been applied yet (processed=False)
    - Returns 404 if not found, 400 if already processed
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        if annotation.processed:
            raise HTTPException(
                status_code=400, detail="Cannot delete a processed annotation"
            )

        await annotation.delete()
        logger.info(f"Deleted annotation {annotation_id}")

        return {"status": "deleted", "annotation_id": annotation_id}

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error deleting annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete annotation: {type(e).__name__}",
        )


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
async def update_annotation(
    annotation_id: str,
    update_data: AnnotationUpdate,
    current_user: User = Depends(current_active_user),
):
    """
    Update an unprocessed annotation in-place.

    - Only allows updating annotations that haven't been applied yet (processed=False)
    - Updates corrected_text, corrected_speaker, insert_text, or insert_segment_type
    - Replaces creating duplicate annotations when re-editing
    """
    try:
        annotation = await Annotation.find_one(
            Annotation.id == annotation_id,
            Annotation.user_id == current_user.user_id,
        )
        if not annotation:
            raise HTTPException(status_code=404, detail="Annotation not found")

        privacy_owner, privacy_snapshot = await _annotation_privacy(
            annotation, current_user.user_id
        )
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        if annotation.processed:
            raise HTTPException(
                status_code=400, detail="Cannot update a processed annotation"
            )

        if update_data.corrected_text is not None:
            # Auto-capture AI's original suggestion before user overwrites it
            if (
                annotation.source == AnnotationSource.MODEL_SUGGESTION
                and annotation.model_suggested_text is None
                and annotation.corrected_text
                and update_data.corrected_text != annotation.corrected_text
            ):
                annotation.model_suggested_text = annotation.corrected_text
            annotation.corrected_text = update_data.corrected_text
        if update_data.model_suggested_text is not None:
            annotation.model_suggested_text = update_data.model_suggested_text
        if update_data.corrected_speaker is not None:
            annotation.corrected_speaker = update_data.corrected_speaker
        if update_data.insert_text is not None:
            annotation.insert_text = update_data.insert_text
        if update_data.insert_segment_type is not None:
            annotation.insert_segment_type = update_data.insert_segment_type
        if update_data.insert_speaker is not None:
            annotation.insert_speaker = update_data.insert_speaker

        annotation.updated_at = datetime.now(timezone.utc)
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(f"Updated annotation {annotation_id}")

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error updating annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update annotation: {type(e).__name__}",
        )


# === Insert Annotation Routes ===


@router.post("/insert", response_model=AnnotationResponse)
async def create_insert_annotation(
    annotation_data: InsertAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create an INSERT annotation to add a new segment between existing segments.

    - Validates conversation ownership and index bounds
    - Creates a pending annotation that will be applied with other annotations
    - insert_after_index=-1 means insert before the first segment
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=400, detail="No active transcript found")

        segment_count = len(active_transcript.segments)
        if (
            annotation_data.insert_after_index < -1
            or annotation_data.insert_after_index >= segment_count
        ):
            raise HTTPException(
                status_code=400,
                detail=f"insert_after_index must be between -1 and {segment_count - 1}",
            )

        if annotation_data.insert_segment_type not in ("event", "note", "speech"):
            raise HTTPException(
                status_code=400,
                detail="insert_segment_type must be 'event', 'note', or 'speech'",
            )

        annotation = Annotation(
            annotation_type=AnnotationType.INSERT,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            insert_after_index=annotation_data.insert_after_index,
            insert_text=annotation_data.insert_text,
            insert_segment_type=annotation_data.insert_segment_type,
            insert_speaker=annotation_data.insert_speaker,
            insert_start=annotation_data.insert_start,
            insert_end=annotation_data.insert_end,
            status=AnnotationStatus.PENDING,
            processed=False,
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created insert annotation {annotation.id} for conversation "
            f"{annotation_data.conversation_id} after index {annotation_data.insert_after_index}"
        )

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating insert annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create insert annotation: {type(e).__name__}",
        )


@router.get("/insert/{conversation_id}", response_model=List[AnnotationResponse])
async def get_insert_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all insert annotations for a conversation."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.INSERT,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return await _annotation_responses(annotations, str(current_user.user_id))

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching insert annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch insert annotations: {type(e).__name__}",
        )


# === Title Annotation Routes ===


@router.post("/title", response_model=AnnotationResponse)
async def create_title_annotation(
    annotation_data: TitleAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for conversation title edit.

    - Validates user owns conversation
    - Creates annotation record (instantly applied)
    - Updates conversation title immediately
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        # Create annotation (instantly applied)
        annotation = Annotation(
            annotation_type=AnnotationType.TITLE,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            original_text=annotation_data.original_text,
            corrected_text=annotation_data.corrected_text,
            status=AnnotationStatus.ACCEPTED,
            processed=True,
            processed_at=datetime.now(timezone.utc),
            processed_by="instant",
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created title annotation {annotation.id} for conversation {annotation_data.conversation_id}"
        )

        # Apply title change immediately
        try:
            conversation.title = annotation_data.corrected_text
            await _save_with_privacy(conversation, privacy_owner, privacy_snapshot)
            logger.info(
                f"Updated title for conversation {annotation_data.conversation_id}"
            )
        except privacy.PrivacyHeld:
            raise
        except Exception as e:
            logger.error(f"Error updating conversation title: {type(e).__name__}")
            # Annotation is saved but title update failed — log but don't fail the request

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating title annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create title annotation: {type(e).__name__}",
        )


@router.get("/title/{conversation_id}", response_model=List[AnnotationResponse])
async def get_title_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all title annotations for a conversation (audit trail)."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.TITLE,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return await _annotation_responses(annotations, str(current_user.user_id))

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching title annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch title annotations: {type(e).__name__}",
        )


# === Diarization Annotation Routes ===


@router.post("/diarization", response_model=AnnotationResponse)
async def create_diarization_annotation(
    annotation_data: DiarizationAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create annotation for speaker identification correction.

    - Validates user owns conversation
    - Creates annotation record (NOT applied to transcript yet)
    - Annotation is marked as unprocessed (processed=False)
    - Visual indication in UI (strikethrough + corrected name)
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        # Validate segment index
        active_transcript = conversation.active_transcript
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        # Create annotation (NOT applied yet)
        annotation = Annotation(
            annotation_type=AnnotationType.DIARIZATION,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            original_speaker=annotation_data.original_speaker,
            corrected_speaker=annotation_data.corrected_speaker,
            segment_start_time=annotation_data.segment_start_time,
            original_text="",  # Not used for diarization
            corrected_text="",  # Not used for diarization
            status=annotation_data.status,
            processed=False,  # Not applied or sent to training yet
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created diarization annotation {annotation.id} for conversation {annotation_data.conversation_id} segment {annotation_data.segment_index}"
        )

        # Accumulation loop: either background decision feeds its matching bucket
        # immediately, so future suggestions improve as the user labels.
        bucket_type = {
            NOISE_LABEL: "noise",
            BACKGROUND_SPEECH_LABEL: "background_speech",
        }.get(annotation_data.corrected_speaker)
        if bucket_type and annotation_data.segment_start_time is not None:
            try:
                start = float(annotation_data.segment_start_time)
                seg = active_transcript.segments[annotation_data.segment_index]
                seg_end = float(getattr(seg, "end", None) or start + 4.0)
                await privacy.assert_current(privacy_owner, privacy_snapshot)
                await background_bucket_controller.add_background_clip(
                    annotation_data.conversation_id,
                    start,
                    seg_end,
                    bucket_type=bucket_type,
                    source="triage",
                    user=current_user,
                )
                await privacy.assert_current(privacy_owner, privacy_snapshot)
            except privacy.PrivacyHeld:
                raise
            except Exception as e:  # noqa: BLE001 - bucket add is best-effort
                logger.warning(
                    f"Background bucket add failed for {annotation.id}: {type(e).__name__}"
                )

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating diarization annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create diarization annotation: {type(e).__name__}",
        )


@router.post("/timing", response_model=AnnotationResponse)
async def create_timing_annotation(
    annotation_data: TimingAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create a TIMING annotation that adjusts an existing segment's start/end.

    Used by the waveform region editor (move/resize a segment's span). Not applied
    immediately — staged as a pending correction and committed by ``/apply``, which
    re-derives a new transcript version. Validates ownership, segment index and that
    end > start.
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        active_transcript = conversation.active_transcript
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        if annotation_data.new_end <= annotation_data.new_start:
            raise HTTPException(
                status_code=400, detail="new_end must be greater than new_start"
            )

        annotation = Annotation(
            annotation_type=AnnotationType.TIMING,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            new_start=annotation_data.new_start,
            new_end=annotation_data.new_end,
            status=annotation_data.status,
            processed=False,
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created timing annotation {annotation.id} for conversation "
            f"{annotation_data.conversation_id} segment {annotation_data.segment_index} "
            f"→ [{annotation_data.new_start:.2f}, {annotation_data.new_end:.2f}]"
        )

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating timing annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create timing annotation: {type(e).__name__}",
        )


@router.get("/timing/{conversation_id}", response_model=List[AnnotationResponse])
async def get_timing_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all TIMING annotations for a conversation."""
    annotations = await Annotation.find(
        Annotation.conversation_id == conversation_id,
        Annotation.user_id == current_user.user_id,
        Annotation.annotation_type == AnnotationType.TIMING,
    ).to_list()
    return await _annotation_responses(annotations, str(current_user.user_id))


@router.post("/deletion", response_model=AnnotationResponse)
async def create_deletion_annotation(
    annotation_data: DeletionAnnotationCreate,
    current_user: User = Depends(current_active_user),
):
    """
    Create a DELETION annotation that removes an existing segment.

    Staged as a pending correction (not applied immediately) and committed by
    ``/apply``, which re-derives a new transcript version with the segment dropped.
    Validates ownership and segment index.
    """
    try:
        conversation = await Conversation.find_one(
            Conversation.conversation_id == annotation_data.conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        active_transcript = conversation.active_transcript
        if not active_transcript or annotation_data.segment_index >= len(
            active_transcript.segments
        ):
            raise HTTPException(status_code=400, detail="Invalid segment index")

        annotation = Annotation(
            annotation_type=AnnotationType.DELETION,
            user_id=current_user.user_id,
            conversation_id=annotation_data.conversation_id,
            segment_index=annotation_data.segment_index,
            status=annotation_data.status,
            processed=False,
        )
        await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created deletion annotation {annotation.id} for conversation "
            f"{annotation_data.conversation_id} segment {annotation_data.segment_index}"
        )

        await privacy.assert_current(privacy_owner, privacy_snapshot)
        return AnnotationResponse.model_validate(annotation)

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error creating deletion annotation: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create deletion annotation: {type(e).__name__}",
        )


@router.get("/deletion/{conversation_id}", response_model=List[AnnotationResponse])
async def get_deletion_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all DELETION annotations for a conversation."""
    annotations = await Annotation.find(
        Annotation.conversation_id == conversation_id,
        Annotation.user_id == current_user.user_id,
        Annotation.annotation_type == AnnotationType.DELETION,
    ).to_list()
    return await _annotation_responses(annotations, str(current_user.user_id))


@router.get("/diarization/{conversation_id}", response_model=List[AnnotationResponse])
async def get_diarization_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """Get all diarization annotations for a conversation."""
    try:
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
        ).to_list()

        return await _annotation_responses(annotations, str(current_user.user_id))

    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error fetching diarization annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch diarization annotations: {type(e).__name__}",
        )


@router.post("/diarization/{conversation_id}/apply")
async def apply_diarization_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Apply pending diarization annotations to create new transcript version.

    - Finds all unprocessed diarization annotations for conversation
    - Creates NEW transcript version with corrected speaker labels
    - Marks annotations as processed (processed=True, processed_by="apply")
    - Chains memory reprocessing since speaker changes affect meaning
    - Returns job status with new version_id
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        # Get unprocessed diarization annotations
        annotations = await Annotation.find(
            Annotation.annotation_type == AnnotationType.DIARIZATION,
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
            Annotation.processed == False,  # Only unprocessed
        ).to_list()
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        if not annotations:
            return JSONResponse(
                content={
                    "message": "No pending annotations to apply",
                    "applied_count": 0,
                }
            )

        # Single-flight: don't stack a new edit on top of an in-flight one. Apply
        # creates a new transcript version and enqueues memory under deterministic
        # job_ids; overlapping with another apply/reprocess races on the
        # conversation's full-document save() and can clobber it.
        in_flight = conversation_edit_chain_in_flight(conversation_id)
        if in_flight:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "This conversation is still processing a previous edit. Try again in a moment.",
                    "in_flight_job_id": in_flight,
                },
            )

        # Get active transcript version
        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=404, detail="No active transcript found")

        # Create NEW transcript version with corrected speakers
        new_version_id = str(uuid.uuid4())

        # Copy segments and apply corrections (most recent annotation wins)
        corrected_segments = []
        for segment_idx, segment in enumerate(active_transcript.segments):
            # Find annotation for this segment index (most recent wins if duplicates)
            annotations_for_segment = sorted(
                [a for a in annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            annotation_for_segment = (
                annotations_for_segment[0] if annotations_for_segment else None
            )

            if annotation_for_segment:
                # Apply correction
                corrected_segment = segment.model_copy()
                _apply_diarization_label(
                    corrected_segment, annotation_for_segment.corrected_speaker
                )
                corrected_segments.append(corrected_segment)
            else:
                # No correction, keep original
                corrected_segments.append(segment.model_copy())

        # Add new version — carry over provider_capabilities so downstream
        # processing knows the provider's diarization/word_timestamp support.
        source_capabilities = active_transcript.metadata.get(
            "provider_capabilities", {}
        )
        new_version = conversation.add_transcript_version(
            version_id=new_version_id,
            transcript=active_transcript.transcript,  # Same transcript text
            words=active_transcript.words,  # Same word timings
            segments=corrected_segments,  # Corrected speaker labels
            provider=active_transcript.provider,
            model=active_transcript.model,
            processing_time_seconds=None,
            metadata={
                "reprocessing_type": "diarization_annotations",
                "source_version_id": active_transcript.version_id,
                "trigger": "manual_annotation_apply",
                "applied_annotation_count": len(annotations),
                "provider_capabilities": source_capabilities,
            },
            set_as_active=True,
        )
        if active_transcript.diarization_source:
            new_version.diarization_source = active_transcript.diarization_source

        await _save_with_privacy(conversation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Created new transcript version {new_version_id} with {len(annotations)} diarization corrections"
        )

        # Mark annotations as processed
        for annotation in annotations:
            annotation.processed = True
            annotation.processed_at = datetime.now(timezone.utc)
            annotation.processed_by = "apply"
            await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)

        # Chain memory reprocessing unless this is an annotation/training import.
        # Diarization-only edits change speaker attribution, so use the same
        # speaker-diff strategy as a speaker reprocess (it falls back to full
        # re-extraction if no diff applies).
        if _should_reprocess_memory(conversation):
            await privacy.assert_current(privacy_owner, privacy_snapshot)
            enqueue_memory_processing(
                conversation_id=conversation_id,
                priority=JobPriority.NORMAL,
                cause=MemoryCause.ANNOTATION_APPLY,
                strategy=UpdateStrategy.SPEAKER_DIFF,
            )
        else:
            logger.info(
                f"Skipping memory reprocessing for memory-excluded conversation {conversation_id[:8]}"
            )

        return JSONResponse(
            content={
                "message": "Diarization annotations applied",
                "version_id": new_version_id,
                "applied_count": len(annotations),
                "status": "success",
            }
        )

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error applying diarization annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply diarization annotations: {type(e).__name__}",
        )


@router.post("/{conversation_id}/apply")
async def apply_all_annotations(
    conversation_id: str,
    current_user: User = Depends(current_active_user),
):
    """
    Apply all pending annotations (diarization + transcript) to create new version.

    - Finds all unprocessed annotations (both DIARIZATION and TRANSCRIPT types)
    - Creates ONE new transcript version with all changes applied
    - Marks all annotations as processed
    - Triggers memory reprocessing once
    """
    try:
        # Verify conversation ownership
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == current_user.user_id,
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        privacy_owner = str(conversation.user_id)
        privacy_snapshot = await privacy.require_record(conversation)
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        # Get ALL unprocessed annotations (both types)
        annotations = await Annotation.find(
            Annotation.conversation_id == conversation_id,
            Annotation.user_id == current_user.user_id,
            Annotation.processed == False,
        ).to_list()
        await privacy.assert_current(privacy_owner, privacy_snapshot)

        if not annotations:
            return JSONResponse(
                content={
                    "message": "No pending annotations to apply",
                    "diarization_count": 0,
                    "transcript_count": 0,
                }
            )

        # Separate by type
        diarization_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.DIARIZATION
        ]
        transcript_annotations = [
            a
            for a in annotations
            if a.annotation_type
            in (AnnotationType.TRANSCRIPT, AnnotationType.SPEECH_SUGGESTION_CORRECTION)
        ]
        insert_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.INSERT
        ]
        timing_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.TIMING
        ]
        deletion_annotations = [
            a for a in annotations if a.annotation_type == AnnotationType.DELETION
        ]
        deleted_indices = {
            a.segment_index for a in deletion_annotations if a.segment_index is not None
        }

        # Single-flight: don't stack a new edit on top of an in-flight one (see
        # apply_diarization_annotations — same deterministic-job-id save race).
        in_flight = conversation_edit_chain_in_flight(conversation_id)
        if in_flight:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "This conversation is still processing a previous edit. Try again in a moment.",
                    "in_flight_job_id": in_flight,
                },
            )

        # Get active transcript
        active_transcript = conversation.active_transcript
        if not active_transcript:
            raise HTTPException(status_code=404, detail="No active transcript found")

        # Create new version with ALL corrections applied
        new_version_id = str(uuid.uuid4())
        corrected_segments = []
        # Segments marked for deletion are dropped AFTER inserts are positioned, so
        # insert_after_index (which references original indices) stays valid. Track by
        # object identity since indices shift once inserts are spliced in.
        deleted_obj_ids: set[int] = set()

        # For diarization/transcript: if multiple annotations exist for same segment,
        # pick the most recently updated one
        for segment_idx, segment in enumerate(active_transcript.segments):
            corrected_segment = segment.model_copy()
            if segment_idx in deleted_indices:
                deleted_obj_ids.add(id(corrected_segment))

            # Apply diarization correction (most recent wins)
            diar_for_segment = sorted(
                [a for a in diarization_annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            if diar_for_segment:
                _apply_diarization_label(
                    corrected_segment, diar_for_segment[0].corrected_speaker
                )

            # Apply transcript correction (most recent wins)
            transcript_for_segment = sorted(
                [a for a in transcript_annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            if transcript_for_segment:
                corrected_segment.text = transcript_for_segment[0].corrected_text

            # Apply timing correction (most recent wins) — waveform region move/resize
            timing_for_segment = sorted(
                [a for a in timing_annotations if a.segment_index == segment_idx],
                key=lambda a: a.updated_at,
                reverse=True,
            )
            if timing_for_segment and timing_for_segment[0].new_end is not None:
                corrected_segment.start = timing_for_segment[0].new_start
                corrected_segment.end = timing_for_segment[0].new_end

            corrected_segments.append(corrected_segment)

        # Apply inserts from highest index to lowest (stable indexing)
        if insert_annotations:
            sorted_inserts = sorted(
                insert_annotations,
                key=lambda a: a.insert_after_index,
                reverse=True,
            )
            for ins in sorted_inserts:
                idx = ins.insert_after_index  # -1 = before first
                insert_pos = idx + 1  # Convert to list insertion position

                # Timing: prefer an explicit waveform-drawn span; otherwise fall back to
                # a zero-duration marker at the neighbouring boundary (legacy inserts).
                if ins.insert_start is not None and ins.insert_end is not None:
                    seg_start = ins.insert_start
                    seg_end = ins.insert_end
                else:
                    if insert_pos > 0 and insert_pos <= len(corrected_segments):
                        boundary_time = corrected_segments[insert_pos - 1].end
                    elif insert_pos == 0 and corrected_segments:
                        boundary_time = corrected_segments[0].start
                    else:
                        boundary_time = 0.0
                    seg_start = boundary_time
                    seg_end = boundary_time

                new_segment = Conversation.SpeakerSegment(
                    start=seg_start,
                    end=seg_end,
                    text=ins.insert_text or "",
                    speaker=ins.insert_speaker or "",
                    segment_type=ins.insert_segment_type or "event",
                )
                corrected_segments.insert(insert_pos, new_segment)

        # Drop segments marked for deletion (now that inserts have been positioned
        # against the original indices).
        if deleted_obj_ids:
            corrected_segments = [
                s for s in corrected_segments if id(s) not in deleted_obj_ids
            ]

        # Re-order by start time so moved/inserted segments read in chronological order.
        # Stable sort keeps the original list order for equal starts — important for the
        # intentional same-time overlapping segments (two speakers at once).
        corrected_segments.sort(key=lambda s: s.start)

        # Add new version — carry over provider_capabilities so downstream
        # processing knows the provider's diarization/word_timestamp support.
        source_capabilities = active_transcript.metadata.get(
            "provider_capabilities", {}
        )
        new_version = conversation.add_transcript_version(
            version_id=new_version_id,
            transcript=active_transcript.transcript,
            words=active_transcript.words,  # Preserved (may be misaligned for text edits)
            segments=corrected_segments,
            provider=active_transcript.provider,
            model=active_transcript.model,
            metadata={
                "reprocessing_type": "unified_annotations",
                "source_version_id": active_transcript.version_id,
                "trigger": "manual_annotation_apply",
                "diarization_count": len(diarization_annotations),
                "transcript_count": len(transcript_annotations),
                "insert_count": len(insert_annotations),
                "timing_count": len(timing_annotations),
                "deletion_count": len(deletion_annotations),
                "provider_capabilities": source_capabilities,
            },
            set_as_active=True,
        )
        if active_transcript.diarization_source:
            new_version.diarization_source = active_transcript.diarization_source

        await _save_with_privacy(conversation, privacy_owner, privacy_snapshot)
        logger.info(
            f"Applied {len(annotations)} annotations "
            f"(diarization: {len(diarization_annotations)}, "
            f"transcript: {len(transcript_annotations)}, "
            f"insert: {len(insert_annotations)}, "
            f"timing: {len(timing_annotations)}, "
            f"deletion: {len(deletion_annotations)})"
        )

        # Mark all annotations as processed
        for annotation in annotations:
            annotation.processed = True
            annotation.processed_at = datetime.now(timezone.utc)
            annotation.processed_by = "apply"
            annotation.status = AnnotationStatus.ACCEPTED
            await _save_with_privacy(annotation, privacy_owner, privacy_snapshot)

        # Trigger memory reprocessing (once for all changes) unless this is an
        # annotation/training import. Combined apply may change transcript text as
        # well as speakers, so re-extract in full for normal conversations.
        if _should_reprocess_memory(conversation):
            await privacy.assert_current(privacy_owner, privacy_snapshot)
            enqueue_memory_processing(
                conversation_id=conversation_id,
                priority=JobPriority.NORMAL,
                cause=MemoryCause.ANNOTATION_APPLY,
                strategy=UpdateStrategy.FULL,
            )
        else:
            logger.info(
                f"Skipping memory reprocessing for memory-excluded conversation {conversation_id[:8]}"
            )

        return JSONResponse(
            content={
                "message": (
                    f"Applied {len(diarization_annotations)} diarization, "
                    f"{len(transcript_annotations)} transcript, "
                    f"{len(insert_annotations)} insert, "
                    f"{len(timing_annotations)} timing, and "
                    f"{len(deletion_annotations)} deletion annotations"
                ),
                "version_id": new_version_id,
                "diarization_count": len(diarization_annotations),
                "transcript_count": len(transcript_annotations),
                "insert_count": len(insert_annotations),
                "timing_count": len(timing_annotations),
                "deletion_count": len(deletion_annotations),
                "status": "success",
            }
        )

    except HTTPException:
        raise
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.error(f"Error applying annotations: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply annotations: {type(e).__name__}",
        )
