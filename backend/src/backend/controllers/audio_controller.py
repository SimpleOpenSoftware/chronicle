"""
Audio file upload and processing controller.

Handles audio file uploads and processes them directly.
Simplified to write files immediately and enqueue transcription.

Also includes audio cropping operations that work with the Conversation model.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError
from rq import Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job

from backend.client_manager import generate_client_id
from backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    start_post_conversation_jobs,
    transcription_queue,
)
from backend.models.audio_capture import (
    AudioRangeRef,
    ConversationTranscriptRevision,
    TranscriptArtifact,
)
from backend.models.conversation import Conversation, create_conversation
from backend.models.user import User
from backend.services.audio_claims import apply_audio_ranges
from backend.services.transcription import is_transcription_available
from backend.utils.audio_chunk_utils import convert_audio_to_chunks
from backend.utils.audio_utils import (
    SUPPORTED_AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AudioValidationError,
    convert_any_to_wav,
    validate_and_prepare_audio,
)
from backend.workers.transcription_jobs import transcribe_full_audio_job

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


def _same_audio_claim(left: AudioRangeRef, right: AudioRangeRef) -> bool:
    fields = (
        "capture_source_id",
        "time_basis",
        "chunk_ids",
        "started_at",
        "ended_at",
        "capture_session_ids",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


async def _is_original_claim_after_silence_trim(
    conversation: Conversation, audio_range: AudioRangeRef
) -> bool:
    """Recognize a replay using immutable evidence, without restoring trimmed audio.

    Silence trimming changes the semantic claim after transcription. The active
    projection must still describe that claim, and its original provider input
    must exactly identify this retry; containment alone cannot establish identity.
    """
    if not conversation.active_transcript_revision_id:
        return False
    revision = await ConversationTranscriptRevision.find_one(
        {
            "revision_id": conversation.active_transcript_revision_id,
            "conversation_id": conversation.conversation_id,
        }
    )
    if revision is None or len(revision.transcript_artifact_ids) != 1:
        return False
    projection = revision.metadata.get("audio_projection")
    if (
        not isinstance(projection, dict)
        or projection.get("operation") != "silence_trim"
    ):
        return False
    try:
        projected = [
            AudioRangeRef.model_validate(value) for value in projection["audio_ranges"]
        ]
    except (KeyError, TypeError, ValidationError):
        return False
    if len(projected) != len(conversation.audio_ranges) or not all(
        _same_audio_claim(left, right)
        for left, right in zip(projected, conversation.audio_ranges)
    ):
        return False
    artifact = await TranscriptArtifact.find_one(
        {
            "artifact_id": revision.transcript_artifact_ids[0],
            "user_id": conversation.user_id,
            "status": "complete",
        }
    )
    return (
        artifact is not None
        and len(artifact.audio_ranges) == 1
        and _same_audio_claim(artifact.audio_ranges[0], audio_range)
    )


def _batch_transcription_job(
    conversation: Conversation,
    version_id: str,
    client_id: str,
):
    """Return the live deterministic transcript job, replacing a terminal failure."""
    if not is_transcription_available(mode="batch"):
        return None
    job_id = f"transcribe_{conversation.conversation_id[:12]}"
    try:
        existing = Job.fetch(job_id, connection=transcription_queue.connection)
        status = existing.get_status(refresh=True)
        status = status.value if hasattr(status, "value") else str(status)
        if status in {"queued", "started", "deferred", "scheduled"}:
            return existing
        if status == "finished" and conversation.has_meaningful_transcript:
            return existing
        existing.delete()
    except NoSuchJobError:
        pass

    return transcription_queue.enqueue(
        transcribe_full_audio_job,
        conversation.conversation_id,
        version_id,
        "batch",
        job_timeout=-1,
        result_ttl=JOB_RESULT_TTL,
        job_id=job_id,
        retry=Retry(max=4, interval=[60, 300, 900, 1800]),
        description=f"Transcribe uploaded file {conversation.conversation_id[:8]}",
        meta={
            "conversation_id": conversation.conversation_id,
            "client_id": client_id,
        },
    )


async def materialize_and_process_audio_claim(
    user: User,
    audio_range: AudioRangeRef,
    *,
    device_name: str,
    title: str,
    segmentation_key: str,
    external_source_id: str,
    external_source_type: str,
    data_purpose: str = "conversation",
    memory_excluded: bool = True,
    memory_exclusion_reason: str | None = "continuous_capture",
    skip_memory_extraction: bool = True,
    skip_title_summary: bool = True,
) -> Conversation:
    """Idempotently materialize one detected Conversation over existing capture.

    This is the semantic seam for continuous capture: no audio is uploaded or copied.
    The Conversation only claims a range that already exists in ``audio_chunks``.
    """
    client_id = generate_client_id(user, device_name)
    conversation = await Conversation.find_one(
        Conversation.segmentation_key == segmentation_key
    )
    if conversation is None:
        candidate = create_conversation(
            user_id=user.user_id,
            client_id=client_id,
            title=title,
            summary="Transcribing detected speech...",
            external_source_id=external_source_id,
            external_source_type=external_source_type,
            data_purpose=data_purpose,
            memory_excluded=memory_excluded,
            memory_exclusion_reason=memory_exclusion_reason,
            audio_ranges=[audio_range],
            origin="detected",
            started_at=audio_range.started_at,
            ended_at=audio_range.ended_at,
            segmentation_key=segmentation_key,
        )
        await apply_audio_ranges(candidate, [audio_range], save=False)
        try:
            await candidate.insert()
            conversation = candidate
        except DuplicateKeyError:
            conversation = await Conversation.find_one(
                Conversation.segmentation_key == segmentation_key
            )
            if conversation is None:
                raise

    if conversation.user_id != user.user_id:
        raise ValueError(f"segmentation key {segmentation_key} belongs to another user")
    if not conversation.audio_ranges:
        await apply_audio_ranges(conversation, [audio_range])
    elif len(conversation.audio_ranges) != 1 or not _same_audio_claim(
        conversation.audio_ranges[0], audio_range
    ):
        if not await _is_original_claim_after_silence_trim(conversation, audio_range):
            raise ValueError(
                f"segmentation key {segmentation_key} was reused for a different audio claim"
            )

    if conversation.processing_enqueued_at is None:
        version_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"chronicle:transcript:{conversation.conversation_id}",
            )
        )
        transcription_job = _batch_transcription_job(
            conversation, version_id, client_id
        )
        start_post_conversation_jobs(
            conversation_id=conversation.conversation_id,
            user_id=user.user_id,
            transcript_version_id=version_id,
            depends_on_job=transcription_job,
            client_id=client_id,
            skip_memory_extraction=skip_memory_extraction,
            skip_title_summary=skip_title_summary,
        )
        conversation.processing_enqueued_at = datetime.now(timezone.utc)
        await conversation.save()
    return conversation


async def upload_and_process_audio_files(
    user: User,
    files: list[UploadFile],
    device_name: str = "upload",
    source: str = "upload",
    annotation_only: bool = False,
    external_source_id: str | None = None,
    external_source_type: str | None = None,
    data_purpose: str | None = None,
    memory_excluded: bool | None = None,
    memory_exclusion_reason: str | None = None,
    conversation_origin: Literal["deliberate", "detected"] = "deliberate",
    # Wall-clock time of the first sample. Continuous capture knows this (the source
    # chunks are timestamped); a plain file upload does not, and passes None.
    captured_at: datetime | None = None,
    # Continuous capture can independently skip per-session memory extraction while
    # retaining speaker identification and title/summary enrichment for user-visible
    # detected conversations. The terminal event-dispatch job always runs — it owns
    # end_reason/completed_at/status, and it already declines to fire plugins for
    # memory-excluded conversations.
    skip_memory_extraction: bool = False,
    skip_title_summary: bool = False,
) -> dict:
    """
    Upload audio files and process them directly.

    Simplified flow:
    1. Validate and read WAV file
    2. Write audio file and create AudioSession immediately
    3. Enqueue transcription job (same as WebSocket path)

    Args:
        user: Authenticated user
        files: List of uploaded audio files
        device_name: Device identifier
        source: Source of the upload (e.g., 'upload', 'gdrive')
        annotation_only: Create editable transcription records without memory extraction
    """
    # Dataset purpose cannot be overridden by a personal memory preference.
    if annotation_only or data_purpose == "annotation":
        annotation_only = True
        data_purpose = "annotation"
        memory_excluded = True

    try:
        if not files:
            return JSONResponse(status_code=400, content={"error": "No files provided"})

        processed_files = []
        client_id = generate_client_id(user, device_name)

        for file_index, file in enumerate(files):
            try:
                # Validate file type
                filename = file.filename or "unknown"
                _, ext = os.path.splitext(filename.lower())
                if not ext or ext not in SUPPORTED_AUDIO_EXTENSIONS:
                    supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
                    processed_files.append(
                        {
                            "filename": filename,
                            "status": "error",
                            "error": f"Unsupported format '{ext}'. Supported: {supported}",
                        }
                    )
                    continue

                is_video_source = ext in VIDEO_EXTENSIONS

                audio_logger.info(
                    f"📁 Uploading file {file_index + 1}/{len(files)}: {filename}"
                )

                # Read file content
                content = await file.read()

                # Convert non-WAV files to WAV via FFmpeg
                if ext != ".wav":
                    try:
                        content = await convert_any_to_wav(content, ext)
                    except AudioValidationError as e:
                        processed_files.append(
                            {
                                "filename": filename,
                                "status": "error",
                                "error": str(e),
                            }
                        )
                        continue

                # Track external source for deduplication (Google Drive, etc.)
                if source == "gdrive":
                    external_source_id = (
                        external_source_id
                        or getattr(file, "file_id", None)
                        or getattr(file, "audio_uuid", None)
                    )
                    external_source_type = external_source_type or "gdrive"
                    if not external_source_id:
                        audio_logger.warning(
                            f"Missing file_id for gdrive file: {filename}"
                        )
                # Validate and prepare audio (read format from WAV file)
                try:
                    audio_data, sample_rate, sample_width, channels, duration = (
                        await validate_and_prepare_audio(
                            audio_data=content,
                            expected_sample_rate=16000,  # Expecting 16kHz
                            convert_to_mono=True,  # Convert stereo to mono
                            auto_resample=True,  # Auto-resample if sample rate doesn't match
                        )
                    )
                except AudioValidationError as e:
                    processed_files.append(
                        {
                            "filename": filename,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    continue

                audio_logger.info(
                    f"📊 {filename}: {duration:.1f}s ({sample_rate}Hz, {channels}ch, {sample_width} bytes/sample)"
                )

                # Generate title from filename
                title = (
                    filename.rsplit(".", 1)[0][:50]
                    if filename != "unknown"
                    else "Uploaded Audio"
                )

                conversation = create_conversation(
                    user_id=user.user_id,
                    client_id=client_id,
                    title=title,
                    summary=(
                        "Processing annotation-only audio file..."
                        if annotation_only
                        else "Processing uploaded audio file..."
                    ),
                    external_source_id=external_source_id,
                    external_source_type=external_source_type,
                    data_purpose=data_purpose
                    or ("annotation" if annotation_only else None),
                    memory_excluded=(
                        annotation_only if memory_excluded is None else memory_excluded
                    ),
                    memory_exclusion_reason=memory_exclusion_reason
                    or ("annotation_only_upload" if annotation_only else None),
                    origin=conversation_origin,
                    segmentation_key=(
                        f"detected-upload:{external_source_id}:v1"
                        if conversation_origin == "detected" and external_source_id
                        else None
                    ),
                )
                await conversation.insert()
                conversation_id = (
                    conversation.conversation_id
                )  # Get the auto-generated ID

                audio_logger.info(
                    f"📝 Created conversation {conversation_id} for uploaded file"
                )

                # Convert audio directly to MongoDB chunks
                try:
                    ingest = await convert_audio_to_chunks(
                        user_id=user.user_id,
                        capture_source_id=client_id,
                        audio_data=audio_data,
                        sample_rate=sample_rate,
                        channels=channels,
                        sample_width=sample_width,
                        captured_at=captured_at,
                        origin="upload",
                        external_source_id=external_source_id,
                        data_purpose=data_purpose
                        or ("annotation" if annotation_only else "normal_capture"),
                    )
                    await apply_audio_ranges(conversation, [ingest.audio_range])
                    audio_logger.info(
                        f"📦 Converted uploaded file to {ingest.chunk_count} MongoDB chunks "
                        f"(conversation {conversation_id[:12]})"
                    )
                except ValueError as val_error:
                    # Handle validation errors (e.g., file too long)
                    audio_logger.error(f"Audio validation failed: {val_error}")
                    processed_files.append(
                        {
                            "filename": filename,
                            "status": "error",
                            "error": str(val_error),
                        }
                    )
                    # Delete the conversation since it won't have audio chunks
                    await conversation.delete()
                    continue
                except Exception as chunk_error:
                    audio_logger.error(
                        f"Failed to convert uploaded file to chunks: {chunk_error}",
                        exc_info=True,
                    )
                    processed_files.append(
                        {
                            "filename": filename,
                            "status": "error",
                            "error": f"Audio conversion failed: {str(chunk_error)}",
                        }
                    )
                    # Delete the conversation since it won't have audio chunks
                    await conversation.delete()
                    continue

                # Enqueue batch transcription job first (file uploads need transcription)
                version_id = str(uuid.uuid4())
                transcribe_job_id = f"transcribe_{conversation_id[:12]}"

                # Check if transcription provider is available before enqueueing
                transcription_job = None
                if is_transcription_available(mode="batch"):
                    transcription_job = transcription_queue.enqueue(
                        transcribe_full_audio_job,
                        conversation_id,
                        version_id,
                        "batch",  # trigger
                        job_timeout=-1,
                        result_ttl=JOB_RESULT_TTL,
                        job_id=transcribe_job_id,
                        # Bulk uploads can trip provider rate limits (HTTP 429);
                        # spread retries out so the batch drains instead of failing.
                        retry=Retry(max=4, interval=[60, 300, 900, 1800]),
                        description=f"Transcribe uploaded file {conversation_id[:8]}",
                        meta={
                            "conversation_id": conversation_id,
                            "client_id": client_id,
                        },
                    )
                    audio_logger.info(
                        f"📥 Enqueued transcription job {transcription_job.id} for uploaded file"
                    )
                else:
                    audio_logger.warning(
                        f"⚠️ Skipping transcription for conversation {conversation_id}: "
                        "No transcription provider configured"
                    )

                # Enqueue post-conversation processing job chain (depends on transcription)
                job_ids = start_post_conversation_jobs(
                    conversation_id=conversation_id,
                    user_id=user.user_id,
                    transcript_version_id=version_id,
                    depends_on_job=transcription_job,
                    client_id=client_id,
                    skip_memory_extraction=annotation_only or skip_memory_extraction,
                    skip_title_summary=skip_title_summary,
                )

                file_result = {
                    "filename": filename,
                    "status": "started",  # RQ standard: job has been enqueued
                    "conversation_id": conversation_id,
                    "annotation_only": annotation_only,
                    "transcript_job_id": (
                        transcription_job.id if transcription_job else None
                    ),
                    "speaker_job_id": job_ids["speaker_recognition"],
                    "memory_job_id": job_ids["memory"],
                    "duration_seconds": round(duration, 2),
                }
                if is_video_source:
                    file_result["note"] = "Audio extracted from video file"
                processed_files.append(file_result)

                # Build job chain description
                job_chain = []
                if transcription_job:
                    job_chain.append(transcription_job.id)
                if job_ids["speaker_recognition"]:
                    job_chain.append(job_ids["speaker_recognition"])
                if job_ids["memory"]:
                    job_chain.append(job_ids["memory"])

                audio_logger.info(
                    f"✅ Processed {filename} → conversation {conversation_id}, "
                    f"jobs: {' → '.join(job_chain) if job_chain else 'none'}"
                )

            except (OSError, IOError) as e:
                # File I/O errors during audio processing
                audio_logger.exception(f"File I/O error processing {filename}")
                processed_files.append(
                    {
                        "filename": filename,
                        "status": "error",
                        "error": str(e),
                    }
                )
            except Exception as e:
                # Unexpected errors during file processing
                audio_logger.exception(f"Unexpected error processing file {filename}")
                processed_files.append(
                    {
                        "filename": filename,
                        "status": "error",
                        "error": str(e),
                    }
                )

        successful_files = [f for f in processed_files if f.get("status") == "started"]
        failed_files = [f for f in processed_files if f.get("status") == "error"]

        response_body = {
            "message": f"Uploaded and processing {len(successful_files)} file(s)",
            "client_id": client_id,
            "annotation_only": annotation_only,
            "files": processed_files,
            "summary": {
                "total": len(files),
                "started": len(successful_files),  # RQ standard
                "failed": len(failed_files),
            },
        }

        # Return appropriate HTTP status code based on results
        if len(failed_files) == len(files):
            # ALL files failed - return 400 Bad Request
            audio_logger.error(f"All {len(files)} file(s) failed to upload")
            return JSONResponse(status_code=400, content=response_body)
        elif len(failed_files) > 0:
            # SOME files failed (partial success) - return 207 Multi-Status
            audio_logger.warning(
                f"Partial upload: {len(successful_files)} succeeded, {len(failed_files)} failed"
            )
            return JSONResponse(status_code=207, content=response_body)
        else:
            # All files succeeded - return 200 OK
            return response_body

    except (OSError, IOError) as e:
        # File system errors during upload handling
        audio_logger.exception("File I/O error in upload_and_process_audio_files")
        return JSONResponse(
            status_code=500, content={"error": f"File upload failed: {str(e)}"}
        )
    except Exception as e:
        # Unexpected errors in upload handler
        audio_logger.exception("Unexpected error in upload_and_process_audio_files")
        return JSONResponse(
            status_code=500, content={"error": f"File upload failed: {str(e)}"}
        )
