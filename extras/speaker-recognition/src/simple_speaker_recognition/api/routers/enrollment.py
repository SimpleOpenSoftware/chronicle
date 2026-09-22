"""Speaker enrollment endpoints."""

import hashlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func

from simple_speaker_recognition.api.core.utils import (
    get_data_directory,
    require_speaker_owner,
    secure_temp_file,
)
from simple_speaker_recognition.core.gallery_privacy import require_available
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker, SpeakerAudioSegment
from simple_speaker_recognition.utils.audio_processing import get_audio_info

# These will be imported from the main service.py when we integrate
# from ..service import get_db, audio_backend, auth

router = APIRouter()
log = logging.getLogger("speaker_service")


def audio_content_hash(audio_data: bytes) -> str:
    """Return the enrollment deduplication key for one encoded audio file."""
    return hashlib.sha256(audio_data).hexdigest()


def require_enrollment_available(speaker_id: str):
    with get_db_session() as session:
        require_available(session, speaker_id)


def existing_enrollment_hashes(user_id: str, speaker_id: str) -> set[str]:
    """Hash the audio already enrolled for one speaker.

    Enrollment galleries are small, so reading the files here keeps deduplication
    correct for existing databases without a schema migration or backfill.
    """
    speaker_dir = get_auth().enrollment_audio_dir / str(user_id) / speaker_id
    if not speaker_dir.exists():
        return set()
    hashes = set()
    for path in speaker_dir.iterdir():
        if not path.is_file() or path.name == "enrollment_manifest.json":
            continue
        try:
            hashes.add(audio_content_hash(path.read_bytes()))
        except OSError:
            log.warning("Could not hash existing enrollment audio %s", path)
    return hashes


# Import dependencies from parent service module
async def get_db():
    """Get speaker database dependency."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return await service.get_db()


def get_audio_backend():
    """Get audio backend."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return service.audio_backend


def get_auth():
    """Get auth settings."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return service.auth


def check_duplicate_speaker_name(
    user_id: str, speaker_name: str, exclude_speaker_id: Optional[str] = None
) -> bool:
    """Check if a speaker name already exists for the given user.

    Case-insensitive and whitespace-trimmed, so "Roshan", "roshan", and "roshan "
    all collide — preventing duplicate same-name speakers (which show up as repeated
    entries in the annotate dropdown). A DB-level unique index on (user_id, lower(name))
    is the race-proof backstop; this gives a friendly error before that fires.

    Args:
        user_id: User ID to check within
        speaker_name: Speaker name to check
        exclude_speaker_id: Speaker ID to exclude from check (for updates)

    Returns:
        True if duplicate found, False otherwise
    """
    db_session = get_db_session()
    try:
        normalized = (speaker_name or "").strip().lower()
        query = db_session.query(Speaker).filter(
            Speaker.user_id == user_id,
            func.lower(func.trim(Speaker.name)) == normalized,
        )

        if exclude_speaker_id:
            query = query.filter(Speaker.id != exclude_speaker_id)

        existing_speaker = query.first()
        return existing_speaker is not None

    finally:
        db_session.close()


def save_segment_record(
    speaker_id: str,
    saved_path: Path,
    duration: float,
    embedding: np.ndarray,
    start: float = 0.0,
    end: Optional[float] = None,
    original_file_path: Optional[str] = None,
) -> None:
    """Persist one enrolled clip's embedding as a SpeakerAudioSegment.

    These per-clip rows power the enrollment-health audit (and a future multi-vector
    gallery). Historically only a single averaged centroid per speaker was stored, so
    contaminated/mislabeled individual clips were invisible. Best-effort: a failure here
    never blocks enrollment itself.
    """
    auth = get_auth()
    base = auth.enrollment_audio_dir.resolve()
    try:
        rel_path = str(Path(saved_path).resolve().relative_to(base))
    except ValueError:
        rel_path = str(saved_path)

    db_session = get_db_session()
    try:
        db_session.add(
            SpeakerAudioSegment(
                speaker_id=speaker_id,
                audio_file_path=rel_path,
                original_file_path=original_file_path,
                start_time=start or 0.0,
                end_time=end if end is not None else (duration or 0.0),
                duration_seconds=duration or 0.0,
                embedding=json.dumps(np.asarray(embedding).reshape(-1).tolist()),
            )
        )
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        log.warning("Failed to persist segment embedding for %s: %s", speaker_id, e)
    finally:
        db_session.close()


def save_enrollment_audio(
    user_id: str,
    speaker_id: str,
    audio_data: bytes,
    filename: str,
    enrollment_type: str = "upload",
) -> Path:
    """Save enrollment audio file to disk.

    Args:
        user_id: User ID
        speaker_id: Speaker ID
        audio_data: Audio file data
        filename: Original filename
        enrollment_type: Type of enrollment (upload, recording, append)

    Returns:
        Path to saved audio file
    """
    # Create directory structure: data/enrollment_audio/{user_id}/{speaker_id}/
    auth = get_auth()
    speaker_audio_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
    speaker_audio_dir.mkdir(parents=True, exist_ok=True)

    # Generate a genuinely-unique filename. A second-resolution timestamp + a constant
    # caller-supplied stem (callers pass "segment.wav") collided whenever two clips were
    # enrolled in the same second, silently OVERWRITING earlier audio. Add microseconds
    # and a short random suffix so same-second / same-name clips never clash.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_filename = Path(filename).stem.replace(" ", "_").replace("/", "_")
    extension = Path(filename).suffix or ".wav"
    unique_filename = f"{timestamp}_{enrollment_type}_{safe_filename}_{uuid.uuid4().hex[:8]}{extension}"

    # Save audio file
    audio_path = speaker_audio_dir / unique_filename
    with open(audio_path, "wb") as f:
        f.write(audio_data)

    log.info(f"Saved enrollment audio: {audio_path}")
    return audio_path


def save_enrollment_manifest(
    user_id: str, speaker_id: str, audio_files: List[dict]
) -> Path:
    """Save or update enrollment manifest file.

    Args:
        user_id: User ID
        speaker_id: Speaker ID
        audio_files: List of audio file information

    Returns:
        Path to manifest file
    """
    auth = get_auth()
    manifest_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
    manifest_path = manifest_dir / "enrollment_manifest.json"

    # Load existing manifest if it exists
    existing_files = []
    if manifest_path.exists():
        try:
            with open(manifest_path, "r") as f:
                manifest_data = json.load(f)
                existing_files = manifest_data.get("audio_files", [])
        except Exception as e:
            log.warning(f"Failed to load existing manifest: {e}")

    # Combine existing and new files
    all_files = existing_files + audio_files

    # Create manifest data
    manifest_data = {
        "speaker_id": speaker_id,
        "user_id": user_id,
        "total_files": len(all_files),
        "last_updated": datetime.now().isoformat(),
        "audio_files": all_files,
    }

    # Save manifest
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)

    log.info(f"Updated enrollment manifest: {manifest_path}")
    return manifest_path


@router.post("/enroll/upload")
async def enroll_upload(
    file: UploadFile = File(..., description="WAV/FLAC <3 min"),
    speaker_id: str = Form(..., description="Unique speaker identifier"),
    speaker_name: str = Form(..., description="Speaker display name"),
    user_id: str = Form(..., description="Chronicle user id owning this speaker"),
    start: Optional[float] = Form(None, description="Start time in seconds"),
    end: Optional[float] = Form(None, description="End time in seconds"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Enroll a speaker from uploaded audio file."""
    require_enrollment_available(speaker_id)
    log.info(f"Enrolling speaker: {speaker_name} (ID: {speaker_id}, User: {user_id})")

    # Check for duplicate speaker name (allow updates to existing speaker with same ID)
    if check_duplicate_speaker_name(
        user_id, speaker_name, exclude_speaker_id=speaker_id
    ):
        raise HTTPException(
            400,
            f"Speaker name '{speaker_name}' already exists for this user. Please choose a different name.",
        )

    # Read file content
    file_content = await file.read()
    if audio_content_hash(file_content) in existing_enrollment_hashes(
        user_id, speaker_id
    ):
        log.info("Skipping duplicate enrollment audio for speaker %s", speaker_id)
        return {
            "status": "already_enrolled",
            "updated": True,
            "speaker_id": speaker_id,
            "audio_saved": False,
            "duplicate": True,
            "duplicates_skipped": 1,
        }

    # Persist temporary file for processing
    with secure_temp_file() as tmp:
        tmp.write(file_content)
        tmp_path = Path(tmp.name)
    try:
        log.info(f"Loading audio file: {tmp_path}")
        audio_backend = get_audio_backend()
        wav = audio_backend.load_wave(tmp_path, start, end)
        log.info(f"Audio loaded, shape: {wav.shape}")

        # Get audio info
        audio_info = get_audio_info(str(tmp_path))
        duration = audio_info["duration_seconds"]

        log.info("Computing speaker embedding...")
        emb = await audio_backend.async_embed(wav)
        log.info(f"Embedding computed, shape: {emb.shape}")

        # Save audio file to enrollment directory
        saved_path = save_enrollment_audio(
            user_id=user_id,
            speaker_id=speaker_id,
            audio_data=file_content,
            filename=file.filename or "upload.wav",
            enrollment_type="upload",
        )

        # Create manifest entry
        auth = get_auth()
        audio_file_info = {
            "filename": saved_path.name,
            "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
            "duration_seconds": duration,
            "start_time": start,
            "end_time": end,
            "upload_time": datetime.now().isoformat(),
            "original_filename": file.filename,
        }

        # Save manifest
        save_enrollment_manifest(user_id, speaker_id, [audio_file_info])

        log.info(f"Adding speaker to database...")
        updated = await db.add_speaker(
            speaker_id,
            speaker_name,
            emb[0],
            user_id,
            sample_count=1,
            total_duration=duration,
        )

        if updated:
            log.info(f"Successfully updated existing speaker: {speaker_id}")
        else:
            log.info(f"Successfully enrolled new speaker: {speaker_id}")

        save_segment_record(
            speaker_id,
            saved_path,
            duration,
            emb[0],
            start=start or 0.0,
            end=end,
            original_file_path=file.filename,
        )

        return {
            "status": "enrolled",
            "updated": updated,
            "speaker_id": speaker_id,
            "audio_saved": True,
            "audio_path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
        }
    except Exception as e:
        log.error(f"Error during enrollment: {e}")
        raise HTTPException(500, f"Enrollment failed: {str(e)}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/enroll/batch")
async def enroll_batch(
    files: List[UploadFile] = File(
        ..., description="Multiple audio files for same speaker"
    ),
    speaker_id: str = Form(..., description="Unique speaker identifier"),
    speaker_name: str = Form(..., description="Speaker display name"),
    user_id: str = Form(..., description="Chronicle user id owning this speaker"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Enroll a speaker using multiple audio segments, computing average embedding."""
    require_enrollment_available(speaker_id)
    log.info(
        f"Batch enrolling speaker: {speaker_name} (ID: {speaker_id}, User: {user_id}) with {len(files)} files"
    )

    # Check for duplicate speaker name (allow updates to existing speaker with same ID)
    if check_duplicate_speaker_name(
        user_id, speaker_name, exclude_speaker_id=speaker_id
    ):
        raise HTTPException(
            400,
            f"Speaker name '{speaker_name}' already exists for this user. Please choose a different name.",
        )

    embeddings = []
    temp_paths = []
    total_duration = 0.0
    saved_audio_files = []
    segment_records: List[tuple] = []
    seen_hashes = existing_enrollment_hashes(user_id, speaker_id)
    duplicates_skipped = 0

    try:
        # Process each audio file
        for i, file in enumerate(files):
            log.info(f"Processing file {i+1}/{len(files)}: {file.filename}")

            # Read file content
            file_content = await file.read()
            content_hash = audio_content_hash(file_content)
            if content_hash in seen_hashes:
                duplicates_skipped += 1
                log.info("Skipping duplicate batch clip %s", file.filename)
                continue
            seen_hashes.add(content_hash)

            # Save to temporary file for processing
            with secure_temp_file() as tmp:
                tmp.write(file_content)
                tmp_path = Path(tmp.name)
                temp_paths.append(tmp_path)

            # Load and embed
            try:
                # Get accurate duration from original file
                audio_info = get_audio_info(str(tmp_path))
                duration = audio_info["duration_seconds"]
                total_duration += duration

                audio_backend = get_audio_backend()
                wav = audio_backend.load_wave(tmp_path)
                emb = await audio_backend.async_embed(wav)
                embeddings.append(emb[0])

                # Save audio file to enrollment directory
                saved_path = save_enrollment_audio(
                    user_id=user_id,
                    speaker_id=speaker_id,
                    audio_data=file_content,
                    filename=file.filename or f"batch_{i+1}.wav",
                    enrollment_type="batch",
                )

                # Track saved audio file info
                auth = get_auth()
                audio_file_info = {
                    "filename": saved_path.name,
                    "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
                    "duration_seconds": duration,
                    "upload_time": datetime.now().isoformat(),
                    "original_filename": file.filename,
                    "batch_index": i + 1,
                }
                saved_audio_files.append(audio_file_info)
                segment_records.append((saved_path, duration, emb[0], file.filename))

                log.info(
                    f"Successfully embedded and saved file {i+1}, duration: {duration:.2f}s"
                )
            except Exception as e:
                log.warning(f"Failed to process file {i+1}: {e}")
                continue

        if not embeddings:
            if duplicates_skipped:
                return {
                    "status": "already_enrolled",
                    "updated": False,
                    "speaker_id": speaker_id,
                    "num_segments": 0,
                    "num_files": len(files),
                    "total_duration": 0.0,
                    "audio_saved": False,
                    "saved_files": 0,
                    "duplicates_skipped": duplicates_skipped,
                }
            raise HTTPException(400, "No valid audio files could be processed")

        # Save manifest with all audio files
        save_enrollment_manifest(user_id, speaker_id, saved_audio_files)

        # Compute average embedding
        log.info(f"Computing average embedding from {len(embeddings)} segments")
        embeddings_array = np.array(embeddings)
        average_embedding = np.mean(embeddings_array, axis=0)

        # Normalize the average embedding
        average_embedding = average_embedding / np.linalg.norm(average_embedding)

        log.info(f"Average embedding computed, shape: {average_embedding.shape}")

        # Add to database with proper counts
        updated = await db.add_speaker(
            speaker_id,
            speaker_name,
            average_embedding,
            user_id,
            sample_count=len(embeddings),
            total_duration=total_duration,
        )

        if updated:
            log.info(f"Successfully updated existing speaker: {speaker_id}")
        else:
            log.info(f"Successfully enrolled new speaker: {speaker_id}")

        # Persist per-clip embeddings now that the speaker row exists (FK).
        for sp, du, ev, orig in segment_records:
            save_segment_record(speaker_id, sp, du, ev, original_file_path=orig)

        return {
            "status": "enrolled",
            "updated": updated,
            "speaker_id": speaker_id,
            "num_segments": len(embeddings),
            "num_files": len(files),
            "total_duration": round(total_duration, 2),
            "audio_saved": True,
            "saved_files": len(saved_audio_files),
            "duplicates_skipped": duplicates_skipped,
        }

    except Exception as e:
        log.error(f"Error during batch enrollment: {e}")
        raise HTTPException(500, f"Batch enrollment failed: {str(e)}") from e
    finally:
        # Clean up temporary files
        for tmp_path in temp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except:
                pass


@router.post("/enroll/append")
async def enroll_append(
    files: List[UploadFile] = File(
        ..., description="Multiple audio files to append to existing speaker"
    ),
    speaker_id: str = Form(..., description="Existing speaker identifier"),
    user_id: str = Form(..., description="Chronicle user id owning this speaker"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Append audio segments to an existing speaker, computing weighted average embedding."""
    require_speaker_owner(speaker_id, user_id)
    require_enrollment_available(speaker_id)
    log.info(
        f"Appending to speaker: {speaker_id} (User: {user_id}) with {len(files)} files"
    )

    # First, verify the speaker exists
    db_session = get_db_session()
    try:
        existing_speaker = (
            db_session.query(Speaker)
            .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
            .first()
        )

        if not existing_speaker:
            raise HTTPException(
                404, f"Speaker {speaker_id} not found for user {user_id}"
            )

        if not existing_speaker.embedding_data:
            raise HTTPException(400, f"Speaker {speaker_id} has no existing embedding")

        # Get existing embedding and counts
        existing_embedding = np.array(
            json.loads(existing_speaker.embedding_data), dtype=np.float32
        )
        existing_count = existing_speaker.audio_sample_count or 1
        existing_duration = existing_speaker.total_audio_duration or 0.0

        log.info(
            f"Existing speaker has {existing_count} samples, {existing_duration:.2f}s duration"
        )

    finally:
        db_session.close()

    embeddings = []
    temp_paths = []
    new_total_duration = 0.0
    saved_audio_files = []
    segment_records: List[tuple] = []
    seen_hashes = existing_enrollment_hashes(user_id, speaker_id)
    duplicates_skipped = 0

    try:
        # Process each new audio file
        for i, file in enumerate(files):
            log.info(f"Processing new file {i+1}/{len(files)}: {file.filename}")

            # Read file content
            file_content = await file.read()
            content_hash = audio_content_hash(file_content)
            if content_hash in seen_hashes:
                duplicates_skipped += 1
                log.info("Skipping duplicate appended clip %s", file.filename)
                continue
            seen_hashes.add(content_hash)

            # Save to temporary file for processing
            with secure_temp_file() as tmp:
                tmp.write(file_content)
                tmp_path = Path(tmp.name)
                temp_paths.append(tmp_path)

            # Load and embed
            try:
                # Get accurate duration from original file
                audio_info = get_audio_info(str(tmp_path))
                duration = audio_info["duration_seconds"]
                new_total_duration += duration

                audio_backend = get_audio_backend()
                wav = audio_backend.load_wave(tmp_path)
                emb = await audio_backend.async_embed(wav)
                embeddings.append(emb[0])

                # Save audio file to enrollment directory
                saved_path = save_enrollment_audio(
                    user_id=user_id,
                    speaker_id=speaker_id,
                    audio_data=file_content,
                    filename=file.filename or f"append_{i+1}.wav",
                    enrollment_type="append",
                )

                # Track saved audio file info
                auth = get_auth()
                audio_file_info = {
                    "filename": saved_path.name,
                    "path": str(saved_path.relative_to(auth.enrollment_audio_dir)),
                    "duration_seconds": duration,
                    "upload_time": datetime.now().isoformat(),
                    "original_filename": file.filename,
                    "append_index": i + 1,
                    "append_operation": True,
                }
                saved_audio_files.append(audio_file_info)
                segment_records.append((saved_path, duration, emb[0], file.filename))

                log.info(
                    f"Successfully embedded and saved new file {i+1}, duration: {duration:.2f}s"
                )
            except Exception as e:
                log.warning(f"Failed to process file {i+1}: {e}")
                continue

        if not embeddings:
            if duplicates_skipped:
                return {
                    "status": "already_enrolled",
                    "updated": False,
                    "speaker_id": speaker_id,
                    "previous_samples": existing_count,
                    "new_samples": 0,
                    "total_samples": existing_count,
                    "total_duration": round(existing_duration, 2),
                    "audio_saved": False,
                    "saved_files": 0,
                    "duplicates_skipped": duplicates_skipped,
                }
            raise HTTPException(400, "No valid audio files could be processed")

        # Update manifest with appended files
        save_enrollment_manifest(user_id, speaker_id, saved_audio_files)

        new_count = len(embeddings)
        total_count = existing_count + new_count

        # Weighted average: (old*old_count + new*new_count) / total → updated voiceprint.
        new_average_embedding = np.mean(np.array(embeddings), axis=0)
        weighted_embedding = (
            existing_embedding * existing_count + new_average_embedding * new_count
        ) / total_count
        weighted_embedding = weighted_embedding / np.linalg.norm(weighted_embedding)
        log.info(
            f"Weighted average embedding from {existing_count} + {new_count} = "
            f"{total_count} samples"
        )

        await db.add_speaker(
            speaker_id,
            existing_speaker.name,
            weighted_embedding,
            user_id,
            sample_count=total_count,
            total_duration=existing_duration + new_total_duration,
        )

        log.info(f"Successfully appended to speaker: {speaker_id}")

        # Persist each appended clip's embedding for the enrollment-health audit.
        for sp, du, ev, orig in segment_records:
            save_segment_record(speaker_id, sp, du, ev, original_file_path=orig)

        return {
            "status": "enrolled",
            "updated": True,
            "speaker_id": speaker_id,
            "previous_samples": existing_count,
            "new_samples": new_count,
            "total_samples": total_count,
            "total_duration": round(existing_duration + new_total_duration, 2),
            "audio_saved": True,
            "saved_files": len(saved_audio_files),
            "duplicates_skipped": duplicates_skipped,
        }

    except Exception as e:
        log.error(f"Error during append enrollment: {e}")
        raise HTTPException(500, f"Append enrollment failed: {str(e)}") from e
    finally:
        # Clean up temporary files
        for tmp_path in temp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except:
                pass
