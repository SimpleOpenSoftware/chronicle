"""Speaker management endpoints."""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from simple_speaker_recognition.api.core.utils import require_speaker_owner
from simple_speaker_recognition.constants import DEFAULT_SIMILARITY_THRESHOLD
from simple_speaker_recognition.core.gallery_privacy import (
    allowed_speakers,
    require_available,
)
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker

# These will be imported from the main service.py when we integrate
# from ..service import get_db, auth

router = APIRouter()
log = logging.getLogger("speaker_service")


# Import dependencies from parent service module
# These will be properly resolved when the service starts
async def get_db():
    """Get speaker database dependency."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return await service.get_db()


def get_auth():
    """Get auth settings."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return service.auth


@router.get("/speakers")
async def list_speakers(
    user_id: Optional[str] = None, db: UnifiedSpeakerDB = Depends(get_db)
):
    """List all enrolled speakers, optionally filtered by user."""
    db_session = get_db_session()
    try:
        if user_id is not None:
            # Filter by user
            query_speakers = (
                db_session.query(Speaker)
                .filter(allowed_speakers())
                .filter(Speaker.user_id == user_id)
                .all()
            )
        else:
            # Return all speakers
            query_speakers = db_session.query(Speaker).filter(allowed_speakers()).all()

        speakers = [
            {
                "id": speaker.id,
                "name": speaker.name,
                "user_id": speaker.user_id,
                "created_at": (
                    speaker.created_at.isoformat() if speaker.created_at else None
                ),
                "updated_at": (
                    speaker.updated_at.isoformat() if speaker.updated_at else None
                ),
                "audio_sample_count": speaker.audio_sample_count or 0,
                "total_audio_duration": speaker.total_audio_duration or 0.0,
            }
            for speaker in query_speakers
        ]
    finally:
        db_session.close()

    return {"speakers": speakers}


@router.get("/speakers/analysis")
async def get_speakers_analysis(
    user_id: Optional[str] = Query(
        default=None, description="User ID to filter speakers"
    ),
    method: str = Query(
        default="umap", description="Dimensionality reduction method: umap, tsne, pca"
    ),
    cluster_method: str = Query(
        default="dbscan", description="Clustering method: dbscan, kmeans"
    ),
    similarity_threshold: float = Query(
        default=DEFAULT_SIMILARITY_THRESHOLD,
        description="Threshold for finding similar speakers",
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Get comprehensive analysis of speaker embeddings including clustering and visualization data."""
    # Lazy import: `analysis` pulls in umap/numba, which can trigger
    # ROCm/llvmlite startup-time segfaults on Strix Halo when imported at module load.
    from simple_speaker_recognition.utils.analysis import create_speaker_analysis

    log.info(
        f"Generating speaker analysis for user_id={user_id}, method={method}, cluster_method={cluster_method}"
    )

    db_session = get_db_session()
    try:
        # Get speakers, optionally filtered by user
        if user_id is not None:
            query_speakers = (
                db_session.query(Speaker)
                .filter(allowed_speakers())
                .filter(Speaker.user_id == user_id)
                .all()
            )
        else:
            query_speakers = db_session.query(Speaker).filter(allowed_speakers()).all()

        if not query_speakers:
            return {
                "status": "success",
                "message": "No speakers found",
                "visualization": {
                    "speakers": [],
                    "embeddings_2d": [],
                    "embeddings_3d": [],
                    "cluster_labels": [],
                    "colors": [],
                },
                "clustering": {"n_clusters": 0, "method": cluster_method},
                "similar_speakers": [],
                "quality_metrics": {"n_speakers": 0},
                "parameters": {
                    "reduction_method": method,
                    "cluster_method": cluster_method,
                    "similarity_threshold": similarity_threshold,
                },
            }

        # Extract embeddings for analysis and create a mapping of ID to name
        embeddings_dict = {}
        speaker_id_to_name = {}
        for speaker in query_speakers:
            if speaker.embedding_data:
                try:
                    embedding = np.array(
                        json.loads(speaker.embedding_data), dtype=np.float32
                    )
                    embeddings_dict[speaker.id] = embedding
                    speaker_id_to_name[speaker.id] = speaker.name
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"Invalid embedding data for speaker {speaker.id}: {e}")
                    continue

        if not embeddings_dict:
            return {
                "status": "success",
                "message": "No valid embeddings found",
                "visualization": {
                    "speakers": [],
                    "embeddings_2d": [],
                    "embeddings_3d": [],
                    "cluster_labels": [],
                    "colors": [],
                },
                "clustering": {"n_clusters": 0, "method": cluster_method},
                "similar_speakers": [],
                "quality_metrics": {"n_speakers": 0},
                "parameters": {
                    "reduction_method": method,
                    "cluster_method": cluster_method,
                    "similarity_threshold": similarity_threshold,
                },
            }

        # Perform analysis
        analysis_result = create_speaker_analysis(
            embeddings_dict=embeddings_dict,
            method=method,
            cluster_method=cluster_method,
            similarity_threshold=similarity_threshold,
            speaker_names=speaker_id_to_name,
        )

        # Add speaker names to the result for frontend use
        if "visualization" in analysis_result:
            analysis_result["visualization"]["speaker_names"] = speaker_id_to_name

        return analysis_result

    except Exception as e:
        log.error(f"Error in speaker analysis: {e}")
        raise HTTPException(500, f"Analysis failed: {str(e)}")
    finally:
        db_session.close()


@router.get("/speakers/{speaker_id}/audio")
async def get_speaker_audio_files(
    speaker_id: str,
    user_id: str = Query(..., description="Chronicle user id owning this speaker"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Get list of saved audio files for a speaker."""
    require_speaker_owner(speaker_id, user_id)

    # Check if manifest file exists
    auth = get_auth()
    manifest_path = (
        auth.enrollment_audio_dir
        / str(user_id)
        / speaker_id
        / "enrollment_manifest.json"
    )

    if not manifest_path.exists():
        return {
            "speaker_id": speaker_id,
            "audio_files": [],
            "total_files": 0,
            "message": "No audio files found for this speaker",
        }

    try:
        with open(manifest_path, "r") as f:
            manifest_data = json.load(f)

        return {
            "speaker_id": speaker_id,
            "user_id": user_id,
            "audio_files": manifest_data.get("audio_files", []),
            "total_files": manifest_data.get("total_files", 0),
            "last_updated": manifest_data.get("last_updated"),
        }

    except Exception as e:
        log.error(f"Error reading manifest for speaker {speaker_id}: {e}")
        raise HTTPException(500, f"Failed to read audio files manifest: {str(e)}")


@router.get("/speakers/{speaker_id}/audio/{filename}")
async def download_speaker_audio_file(
    speaker_id: str,
    filename: str,
    user_id: str = Query(..., description="Chronicle user id owning this speaker"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Download a specific audio file for a speaker."""
    require_speaker_owner(speaker_id, user_id)

    # Construct file path (security: only allow files within speaker's directory)
    auth = get_auth()
    speaker_audio_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
    file_path = speaker_audio_dir / filename

    # Security check: ensure file is within the expected directory
    try:
        file_path = file_path.resolve()
        speaker_audio_dir = speaker_audio_dir.resolve()
        if not str(file_path).startswith(str(speaker_audio_dir)):
            raise HTTPException(
                403, "Access to file outside speaker directory is forbidden"
            )
    except Exception:
        raise HTTPException(400, "Invalid file path")

    if not file_path.exists():
        raise HTTPException(
            404, f"Audio file {filename} not found for speaker {speaker_id}"
        )

    if not file_path.is_file():
        raise HTTPException(400, f"Path {filename} is not a file")

    return FileResponse(path=str(file_path), filename=filename, media_type="audio/wav")


@router.post("/speakers/reset")
async def reset_speakers(
    user_id: Optional[str] = None, db: UnifiedSpeakerDB = Depends(get_db)
):
    """Reset all speakers (optionally for a specific user)."""
    if user_id is not None:
        await db.reset_user(user_id)
    else:
        # Reset all speakers (admin function)
        db_session = get_db_session()
        try:
            db_session.query(Speaker).delete()
            db_session.commit()
        finally:
            db_session.close()

    return {"reset": True}


@router.delete("/speakers/{speaker_id}")
async def delete_speaker(
    speaker_id: str,
    user_id: str = Query(..., description="Chronicle user id owning this speaker"),
    delete_audio: bool = False,
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Delete a speaker and optionally delete associated audio files."""
    try:
        require_speaker_owner(speaker_id, user_id)

        # Delete audio files if requested
        audio_deleted = False
        if delete_audio:
            auth = get_auth()
            speaker_audio_dir = auth.enrollment_audio_dir / str(user_id) / speaker_id
            if speaker_audio_dir.exists():
                try:
                    shutil.rmtree(speaker_audio_dir)
                    audio_deleted = True
                    log.info(
                        f"Deleted audio directory for speaker {speaker_id}: {speaker_audio_dir}"
                    )
                except Exception as e:
                    log.warning(
                        f"Failed to delete audio directory for speaker {speaker_id}: {e}"
                    )

        # Delete speaker from database
        await db.delete_speaker(speaker_id, user_id)

        result = {"deleted": True}
        if delete_audio:
            result["audio_deleted"] = audio_deleted

        return result

    except KeyError:
        raise HTTPException(404, "speaker not found") from None


@router.get("/speakers/export")
async def export_speakers(
    user_id: Optional[str] = Query(
        None, description="Export speakers for specific user (admin can export all)"
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Export all speakers with embeddings for backup.

    Returns a JSON file containing all speaker data including embeddings,
    suitable for backup and restore operations.
    """
    db_session = get_db_session()
    try:
        # Query speakers based on user_id
        query = db_session.query(Speaker).filter(allowed_speakers())
        if user_id:
            query = query.filter(Speaker.user_id == user_id)

        speakers = query.all()

        # Build export data
        export_data = {
            "version": "1.0",
            "export_date": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "total_speakers": len(speakers),
            "speakers": [],
        }

        for speaker in speakers:
            speaker_data = {
                "id": speaker.id,
                "name": speaker.name,
                "user_id": speaker.user_id,
                "created_at": (
                    speaker.created_at.isoformat() + "Z" if speaker.created_at else None
                ),
                "updated_at": (
                    speaker.updated_at.isoformat() + "Z" if speaker.updated_at else None
                ),
                "audio_sample_count": speaker.audio_sample_count,
                "total_audio_duration": speaker.total_audio_duration,
                "embedding_version": speaker.embedding_version,
                "notes": speaker.notes,
            }

            # Include embedding data if available
            if speaker.embedding_data:
                try:
                    embedding = json.loads(speaker.embedding_data)
                    speaker_data["embedding_data"] = embedding
                except json.JSONDecodeError:
                    log.warning(f"Invalid embedding data for speaker {speaker.id}")
                    speaker_data["embedding_data"] = None
            else:
                speaker_data["embedding_data"] = None

            export_data["speakers"].append(speaker_data)

        # Return as downloadable JSON file
        return export_data

    except Exception as e:
        log.error(f"Error exporting speakers: {e}")
        raise HTTPException(status_code=500, detail="Failed to export speakers")
    finally:
        db_session.close()


@router.post("/speakers/import")
async def import_speakers(
    file: UploadFile = File(..., description="JSON file containing speaker data"),
    merge_strategy: str = Form(
        "skip", description="How to handle conflicts: 'skip' or 'replace'"
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Import speakers from a JSON backup file.

    Supports two merge strategies:
    - skip: Skip speakers that already exist
    - replace: Replace existing speakers with imported data
    """
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="File must be a JSON file")

    # Read and parse JSON file
    try:
        contents = await file.read()
        import_data = json.loads(contents)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {str(e)}")

    # Validate format
    if "version" not in import_data or "speakers" not in import_data:
        raise HTTPException(status_code=400, detail="Invalid import file format")

    if import_data["version"] != "1.0":
        raise HTTPException(
            status_code=400, detail=f"Unsupported version: {import_data['version']}"
        )

    # Import statistics
    imported_count = 0
    skipped_count = 0
    replaced_count = 0
    errors = []

    db_session = get_db_session()
    try:
        for speaker_data in import_data["speakers"]:
            try:
                require_available(db_session, speaker_data["id"])
                # Check if speaker exists
                existing_speaker = (
                    db_session.query(Speaker)
                    .filter(Speaker.id == speaker_data["id"])
                    .first()
                )

                if existing_speaker:
                    if merge_strategy == "skip":
                        skipped_count += 1
                        continue
                    elif merge_strategy == "replace":
                        # Delete existing speaker
                        db_session.delete(existing_speaker)
                        replaced_count += 1

                # Create new speaker
                speaker = Speaker(
                    id=speaker_data["id"],
                    name=speaker_data["name"],
                    user_id=speaker_data["user_id"],
                    audio_sample_count=speaker_data.get("audio_sample_count", 0),
                    total_audio_duration=speaker_data.get("total_audio_duration", 0.0),
                    embedding_version=speaker_data.get("embedding_version", 1),
                    notes=speaker_data.get("notes"),
                )

                # Set embedding data if available
                if speaker_data.get("embedding_data"):
                    speaker.embedding_data = json.dumps(speaker_data["embedding_data"])

                db_session.add(speaker)
                imported_count += 1

            except Exception as e:
                errors.append(
                    f"Failed to import speaker {speaker_data.get('id', 'unknown')}: {str(e)}"
                )
                log.error(f"Error importing speaker: {e}")

        # Commit all changes
        db_session.commit()

        # Rebuild FAISS index after import
        db._rebuild_faiss_mapping()
        db._save_faiss_index()

    except Exception as e:
        db_session.rollback()
        log.error(f"Error during import: {e}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
    finally:
        db_session.close()

    # Return import summary
    return {
        "success": True,
        "imported": imported_count,
        "skipped": skipped_count,
        "replaced": replaced_count,
        "errors": errors,
        "total_processed": len(import_data["speakers"]),
    }
