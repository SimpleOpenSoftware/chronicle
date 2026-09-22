"""
Audio file upload and serving routes.

Handles audio file uploads, processing job management, and audio file serving.
Audio is served from MongoDB chunks with Opus compression.
"""

import io
import logging
import re
import wave
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

import backend.services.privacy as privacy
from backend.app_config import get_audio_chunk_dir
from backend.auth import (
    current_active_user_optional,
    current_superuser,
    get_user_from_token_param,
)
from backend.controllers import audio_controller
from backend.models.conversation import Conversation
from backend.models.user import User
from backend.utils.audio_chunk_utils import (
    build_wav_from_pcm,
    concatenate_chunks_to_pcm,
    get_opus_for_conversation,
    get_trimmed_opus_for_time_range,
    reconstruct_audio_segment,
    reconstruct_wav_from_conversation,
    retrieve_audio_chunks,
)
from backend.utils.gdrive_audio_utils import (
    AudioValidationError,
    download_audio_files_from_drive,
)

router = APIRouter(prefix="/audio", tags=["audio"])


def _safe_filename(conversation: "Conversation") -> str:
    """Build a filesystem-safe filename from the conversation title, falling back to ID."""
    title = conversation.title
    if not title:
        return conversation.conversation_id
    # Replace anything that isn't alphanumeric, space, hyphen, or underscore
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    # Collapse whitespace to single underscore
    safe = re.sub(r"\s+", "_", safe)
    return safe[:120] or conversation.conversation_id


@router.post("/upload_audio_from_gdrive")
async def upload_audio_from_drive_folder(
    gdrive_folder_id: str = Query(
        ...,
        description="Google Drive Folder ID containing audio files (e.g., the string after /folders/ in the URL)",
    ),
    current_user: User = Depends(current_superuser),
    device_name: str = Query(default="upload"),
    annotation_only: bool = Query(
        default=False,
        description="Create editable annotation records without memory extraction",
    ),
):
    try:
        files = await download_audio_files_from_drive(gdrive_folder_id, current_user.id)
    except AudioValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return await audio_controller.upload_and_process_audio_files(
        current_user,
        files,
        device_name,
        source="gdrive",
        annotation_only=annotation_only,
    )


@router.post("/upload_audio_from_gdrive/annotation")
async def upload_audio_from_drive_folder_for_annotation(
    gdrive_folder_id: str = Query(..., description="Google Drive folder ID"),
    current_user: User = Depends(current_superuser),
    device_name: str = Query(default="annotation-import"),
):
    """Import a Drive folder into the annotation workspace without memory writes."""
    return await upload_audio_from_drive_folder(
        gdrive_folder_id=gdrive_folder_id,
        current_user=current_user,
        device_name=device_name,
        annotation_only=True,
    )


@router.get("/get_audio/{conversation_id}")
async def get_conversation_audio(
    conversation_id: str,
    request: Request,
    format: str = Query(default="opus", description="Audio format: opus or wav"),
    token: Optional[str] = Query(
        default=None, description="JWT token for audio element access"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Serve complete audio file for a conversation from MongoDB chunks.

    With format=opus (default), serves raw ogg/opus data directly — no
    server-side decoding needed. With format=wav, decodes to WAV.

    Args:
        conversation_id: The conversation ID
        format: Audio format - "opus" (default, compressed) or "wav" (uncompressed)
        token: Optional JWT token as query param (for audio elements)
        current_user: Authenticated user (from header)

    Returns:
        StreamingResponse with audio data
    """
    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(
        current_user.user_id
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        policy = await privacy.require_record(conversation)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc

    filename = _safe_filename(conversation)

    if format == "opus":
        try:
            opus_data = await get_opus_for_conversation(conversation_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

        file_size = len(opus_data)
        range_header = request.headers.get("range")

        if not range_header:
            return privacy.PrivacyBytesResponse(
                privacy_snapshots=[(str(conversation.user_id), policy)],
                content=opus_data,
                media_type="audio/ogg",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}.ogg"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes",
                    "X-Audio-Source": "mongodb-chunks-direct",
                    "X-Chunk-Count": str(conversation.audio_chunks_count or 0),
                },
            )

        # Handle Range requests
        try:
            range_str = range_header.replace("bytes=", "")
            range_start, range_end = range_str.split("-")
            range_start = int(range_start) if range_start else 0
            range_end = int(range_end) if range_end else file_size - 1
            range_start = max(0, range_start)
            range_end = min(file_size - 1, range_end)
            content_length = range_end - range_start + 1

            return privacy.PrivacyBytesResponse(
                privacy_snapshots=[(str(conversation.user_id), policy)],
                content=opus_data[range_start : range_end + 1],
                status_code=206,
                media_type="audio/ogg",
                headers={
                    "Content-Range": f"bytes {range_start}-{range_end}/{file_size}",
                    "Content-Length": str(content_length),
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": f'inline; filename="{filename}.ogg"',
                },
            )
        except (ValueError, IndexError):
            return privacy.PrivacyBytesResponse(
                privacy_snapshots=[(str(conversation.user_id), policy)],
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

    # format=wav: decode to WAV (legacy path)
    try:
        wav_data = await reconstruct_wav_from_conversation(conversation_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to reconstruct audio: {str(e)}"
        )

    file_size = len(wav_data)
    range_header = request.headers.get("range")

    if not range_header:
        return privacy.PrivacyStreamingResponse(
            privacy_snapshots=[(str(conversation.user_id), policy)],
            content=(
                wav_data[offset : offset + 65536]
                for offset in range(0, len(wav_data), 65536)
            ),
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="{filename}.wav"',
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "X-Audio-Source": "mongodb-chunks",
                "X-Chunk-Count": str(conversation.audio_chunks_count or 0),
            },
        )

    try:
        range_str = range_header.replace("bytes=", "")
        range_start, range_end = range_str.split("-")
        range_start = int(range_start) if range_start else 0
        range_end = int(range_end) if range_end else file_size - 1
        range_start = max(0, range_start)
        range_end = min(file_size - 1, range_end)
        content_length = range_end - range_start + 1

        return privacy.PrivacyBytesResponse(
            privacy_snapshots=[(str(conversation.user_id), policy)],
            content=wav_data[range_start : range_end + 1],
            status_code=206,
            media_type="audio/wav",
            headers={
                "Content-Range": f"bytes {range_start}-{range_end}/{file_size}",
                "Content-Length": str(content_length),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{filename}.wav"',
            },
        )
    except (ValueError, IndexError):
        return privacy.PrivacyBytesResponse(
            privacy_snapshots=[(str(conversation.user_id), policy)],
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )


@router.get("/stream_audio/{conversation_id}")
async def stream_conversation_audio(
    conversation_id: str,
    token: Optional[str] = Query(
        default=None, description="JWT token for audio element access"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Stream audio file for a conversation with progressive chunk delivery.

    Better UX for long conversations - starts playback before full download completes.

    Uses cursor-based pagination to stream chunks in batches of 20, decoding
    and serving each batch as it's retrieved.

    Supports both header-based auth (Authorization: Bearer) and query param token
    for <audio> element compatibility.

    Args:
        conversation_id: The conversation ID
        token: Optional JWT token as query param (for audio elements)
        current_user: Authenticated user (from header)

    Returns:
        StreamingResponse with chunked WAV data (Transfer-Encoding: chunked)

    Raises:
        404: If conversation or audio chunks not found
        403: If user doesn't own the conversation
        401: If not authenticated
    """
    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(
        current_user.user_id
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        policy = await privacy.require_record(conversation)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc

    # Check if chunks exist
    if not conversation.audio_chunks_count or conversation.audio_chunks_count == 0:
        raise HTTPException(
            status_code=404, detail="No audio data for this conversation"
        )

    async def stream_chunks():
        """Generator that yields WAV data in batches."""
        # First, yield WAV header with placeholder size
        # (actual size will be updated by client or ignored in streaming mode)
        SAMPLE_RATE = 16000
        CHANNELS = 1
        SAMPLE_WIDTH = 2

        # Build minimal WAV header (44 bytes)
        # We'll write a placeholder size since we're streaming
        wav_header = io.BytesIO()
        with wave.open(wav_header, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            # Write empty frame to establish header
            wav.writeframes(b"")

        # Yield header
        yield wav_header.getvalue()

        # Stream chunks in batches of 20
        start_index = 0
        batch_size = 20

        while start_index < conversation.audio_chunks_count:
            await privacy.assert_current(str(conversation.user_id), policy)
            # Retrieve batch of chunks
            chunks = await retrieve_audio_chunks(
                conversation_id=conversation_id,
                start_index=start_index,
                limit=batch_size,
            )

            if not chunks:
                break

            # Decode and concatenate this batch
            await privacy.assert_current(str(conversation.user_id), policy)
            pcm_batch = await concatenate_chunks_to_pcm(chunks)

            # Yield PCM data (client's WAV parser handles the stream)
            yield pcm_batch

            # Move to next batch
            start_index += batch_size

    filename = _safe_filename(conversation)
    return privacy.PrivacyStreamingResponse(
        privacy_snapshots=[(str(conversation.user_id), policy)],
        content=stream_chunks(),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'inline; filename="{filename}.wav"',
            "X-Audio-Source": "mongodb-chunks-stream",
            "X-Chunk-Count": str(conversation.audio_chunks_count or 0),
            "X-Total-Duration": str(conversation.audio_total_duration or 0),
        },
    )


@router.get("/chunks/{conversation_id}")
async def get_audio_chunk_range(
    conversation_id: str,
    start_time: float = Query(..., description="Start time in seconds"),
    end_time: float = Query(..., description="End time in seconds"),
    format: str = Query(default="opus", description="Audio format: opus or wav"),
    token: Optional[str] = Query(
        default=None, description="JWT token for audio element access"
    ),
    current_user: Optional[User] = Depends(current_active_user_optional),
):
    """
    Serve audio for a time range.

    With format=opus (default), serves a single ogg/opus stream trimmed to
    the exact time range (decoded, clipped, and re-encoded server-side).
    With format=wav, decodes to exact time-clipped WAV.

    Example:
        GET /api/audio/chunks/uuid?start_time=15.5&end_time=25.5&token=xxx
    """
    logger = logging.getLogger(__name__)

    # Try token param if header auth failed
    if not current_user and token:
        current_user = await get_user_from_token_param(token)

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify conversation exists and user has access
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check ownership (admins can access all)
    if not current_user.is_superuser and conversation.user_id != str(
        current_user.user_id
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        policy = await privacy.require_record(conversation)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc

    # Validate time range
    if start_time < 0 or end_time <= start_time:
        raise HTTPException(status_code=400, detail="Invalid time range")

    if (
        conversation.audio_total_duration
        and end_time > conversation.audio_total_duration
    ):
        end_time = conversation.audio_total_duration

    if format == "opus":
        try:
            opus_data = await get_trimmed_opus_for_time_range(
                conversation_id, start_time, end_time
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

        return privacy.PrivacyBytesResponse(
            privacy_snapshots=[(str(conversation.user_id), policy)],
            content=opus_data,
            media_type="audio/ogg",
            headers={
                "Content-Disposition": f"inline; filename=chunk_{start_time}_{end_time}.ogg",
                "Content-Length": str(len(opus_data)),
                "X-Audio-Duration": str(end_time - start_time),
                "X-Start-Time": str(start_time),
                "X-End-Time": str(end_time),
            },
        )

    # format=wav: decode to exact time-clipped WAV
    try:
        wav_data = await reconstruct_audio_segment(
            conversation_id, start_time, end_time
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to reconstruct audio segment: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to reconstruct audio: {str(e)}"
        )

    return privacy.PrivacyBytesResponse(
        privacy_snapshots=[(str(conversation.user_id), policy)],
        content=wav_data,
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"inline; filename=chunk_{start_time}_{end_time}.wav",
            "Content-Length": str(len(wav_data)),
            "X-Audio-Duration": str(end_time - start_time),
            "X-Start-Time": str(start_time),
            "X-End-Time": str(end_time),
        },
    )


@router.post("/upload")
async def upload_audio_files(
    current_user: User = Depends(current_superuser),
    files: list[UploadFile] = File(...),
    device_name: str = Query(
        default="upload", description="Device name for uploaded files"
    ),
    annotation_only: bool = Query(
        default=False,
        description="Create editable annotation records without memory extraction",
    ),
):
    """
    Upload and process audio files. Admin only.

    Audio files are stored as MongoDB chunks and enqueued for processing via RQ jobs.
    This allows for scalable processing of large files without blocking the API.

    Returns:
        - List of uploaded files with their processing job IDs
        - Summary of enqueued vs failed uploads
    """
    return await audio_controller.upload_and_process_audio_files(
        current_user, files, device_name, annotation_only=annotation_only
    )


@router.post("/upload/annotation")
async def upload_audio_files_for_annotation(
    files: list[UploadFile] = File(...),
    current_user: User = Depends(current_superuser),
    device_name: str = Query(
        default="annotation-upload",
        description="Device name for annotation workspace uploads",
    ),
):
    """Transcribe raw audio into the annotation workspace without memory writes."""
    return await audio_controller.upload_and_process_audio_files(
        current_user,
        files,
        device_name,
        annotation_only=True,
    )
