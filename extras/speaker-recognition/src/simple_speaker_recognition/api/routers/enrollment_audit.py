"""Enrollment health audit + corrective actions (relabel / delete enrolled clips).

Backs the "Enrollment Health" UI: surfaces contaminated/mislabeled/junk enrolment clips
from the per-clip embeddings, plays them back, and lets the user relabel a clip to the
speaker it actually sounds like or delete it — recomputing the affected centroids so the
fix takes effect on the next identification.
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from simple_speaker_recognition.api.core.utils import secure_temp_file
from simple_speaker_recognition.core.enrollment_audit import (
    compute_audit,
    recompute_speaker_centroid,
)
from simple_speaker_recognition.core.gallery_privacy import (
    allowed_speakers,
    require_available,
    require_unmanaged_segment,
)
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import (
    EnrollmentAuditDecision,
    ProcessingJob,
    Speaker,
    SpeakerAudioSegment,
)
from simple_speaker_recognition.utils.audio_processing import get_audio_info

router = APIRouter()
log = logging.getLogger("speaker_service")
BACKFILL_JOB_TYPE = "enrollment_segment_backfill"
_backfill_tasks: set[asyncio.Task] = set()


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


def _resolve_seg_path(seg: SpeakerAudioSegment) -> Path:
    """Resolve a segment's audio path, refusing anything outside the enrollment dir."""
    base = get_auth().enrollment_audio_dir.resolve()
    path = (base / seg.audio_file_path).resolve()
    if not str(path).startswith(str(base)):
        raise HTTPException(
            403, "Segment audio path is outside the enrollment directory"
        )
    return path


def _remove_manifest_audio_path(audio_file_path: str) -> None:
    """Remove a deleted/quarantined clip from its enrollment manifest."""
    base = get_auth().enrollment_audio_dir.resolve()
    relative_path = Path(audio_file_path)
    manifest_path = base / relative_path.parent / "enrollment_manifest.json"
    if not manifest_path.exists():
        return

    with manifest_path.open() as manifest_file:
        manifest = json.load(manifest_file)
    audio_files = manifest.get("audio_files", [])
    if not isinstance(audio_files, list):
        raise ValueError(f"Invalid audio_files in enrollment manifest {manifest_path}")

    normalized_path = relative_path.as_posix()
    retained_files = [
        item
        for item in audio_files
        if not isinstance(item, dict) or item.get("path") != normalized_path
    ]
    if len(retained_files) == len(audio_files):
        return

    manifest["audio_files"] = retained_files
    manifest["total_files"] = len(retained_files)
    manifest["last_updated"] = datetime.now().isoformat()
    temporary_path = manifest_path.with_suffix(".json.tmp")
    with temporary_path.open("w") as manifest_file:
        json.dump(manifest, manifest_file, indent=2)
    temporary_path.replace(manifest_path)


@router.get("/enrollment/health")
async def enrollment_health(
    user_id: Optional[str] = Query(
        None, description="Scope audit to a user's speakers"
    ),
    before: Optional[datetime] = Query(
        None, description="Only include clips created before this timestamp"
    ),
):
    """Contamination audit over per-clip enrollment embeddings."""
    session = get_db_session()
    try:
        return compute_audit(session, user_id, before)
    finally:
        session.close()


def _backfill_job_response(job: ProcessingJob) -> dict:
    output = json.loads(job.output_data) if job.output_data else None
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "result": output,
        "error": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


async def _run_segment_backfill(job_id: int, user_id: str) -> None:
    """Populate missing per-clip embeddings using the service's loaded GPU model."""
    # Lazy: service.py includes this router, so a top-level import is circular.
    from .. import service

    session = get_db_session()
    try:
        job = session.query(ProcessingJob).filter(ProcessingJob.id == job_id).one()
        job.status = "running"
        job.started_at = datetime.utcnow()
        session.commit()

        speakers = (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.user_id == user_id)
            .all()
        )
        speaker_ids = {speaker.id for speaker in speakers}
        existing = {
            (row.speaker_id, os.path.basename(row.audio_file_path))
            for row in session.query(SpeakerAudioSegment)
            .join(Speaker)
            .filter(Speaker.user_id == user_id)
            .all()
        }
        enrollment_dir = get_auth().enrollment_audio_dir.resolve()
        user_dir = enrollment_dir / str(user_id)
        candidates = (
            [
                (speaker_dir.name, wav)
                for speaker_dir in sorted(user_dir.iterdir())
                if speaker_dir.is_dir() and speaker_dir.name in speaker_ids
                for wav in sorted(speaker_dir.glob("*.wav"))
                if (speaker_dir.name, wav.name) not in existing
            ]
            if user_dir.exists()
            else []
        )

        added = failed = 0
        total = len(candidates)
        for index, (speaker_id, wav) in enumerate(candidates, start=1):
            try:
                require_available(session, speaker_id)
                duration = float(service.audio_backend.loader.get_duration(str(wav)))
                wave = service.audio_backend.load_wave(wav)
                vector = np.asarray(
                    await service.audio_backend.async_embed(wave), dtype=np.float32
                ).reshape(-1)
                norm = np.linalg.norm(vector)
                if not np.isfinite(norm) or norm <= 0:
                    raise ValueError("embedding was not finite")
                vector /= norm
                require_available(session, speaker_id)
                relative_path = str(wav.resolve().relative_to(enrollment_dir))
                session.add(
                    SpeakerAudioSegment(
                        speaker_id=speaker_id,
                        audio_file_path=relative_path,
                        original_file_path=wav.name,
                        start_time=0.0,
                        end_time=duration,
                        duration_seconds=duration,
                        embedding=json.dumps(vector.tolist()),
                    )
                )
                added += 1
            except Exception:
                failed += 1
                log.exception("Could not backfill enrollment clip %s", wav)

            job.progress = round(index * 100 / total, 1) if total else 100.0
            session.commit()

        job.status = "completed"
        job.progress = 100.0
        job.output_data = json.dumps(
            {"added": added, "failed": failed, "already_available": len(existing)}
        )
        job.completed_at = datetime.utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        job = session.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            session.commit()
        log.exception("Enrollment segment backfill job %s failed", job_id)
    finally:
        session.close()


@router.post("/enrollment/segments/backfill", status_code=202)
async def start_segment_backfill(user_id: str = Query(...)):
    """Start or attach to the idempotent per-clip embedding backfill."""
    session = get_db_session()
    try:
        active = (
            session.query(ProcessingJob)
            .filter(
                ProcessingJob.job_type == BACKFILL_JOB_TYPE,
                ProcessingJob.status.in_(["pending", "running"]),
                ProcessingJob.input_data == json.dumps({"user_id": user_id}),
            )
            .order_by(ProcessingJob.id.desc())
            .first()
        )
        if active:
            return _backfill_job_response(active)

        job = ProcessingJob(
            job_type=BACKFILL_JOB_TYPE,
            input_data=json.dumps({"user_id": user_id}),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        response = _backfill_job_response(job)
        task = asyncio.create_task(_run_segment_backfill(job.id, user_id))
        _backfill_tasks.add(task)
        task.add_done_callback(_backfill_tasks.discard)
        return response
    finally:
        session.close()


@router.get("/enrollment/segments/backfill")
async def get_segment_backfill(user_id: str = Query(...)):
    """Return the latest backfill job so refreshed pages reattach to it."""
    session = get_db_session()
    try:
        job = (
            session.query(ProcessingJob)
            .filter(
                ProcessingJob.job_type == BACKFILL_JOB_TYPE,
                ProcessingJob.input_data == json.dumps({"user_id": user_id}),
            )
            .order_by(ProcessingJob.id.desc())
            .first()
        )
        return _backfill_job_response(job) if job else None
    finally:
        session.close()


@router.get("/enrollment/segments/{segment_id}/audio")
async def segment_audio(segment_id: int):
    """Stream a single enrolled clip for playback in the audit UI."""
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        require_available(session, seg.speaker_id)
        path = _resolve_seg_path(seg)
    finally:
        session.close()

    if not path.exists():
        raise HTTPException(404, "Audio file missing on disk")
    return FileResponse(str(path), media_type="audio/wav", filename=path.name)


def _unit(v: np.ndarray) -> Optional[np.ndarray]:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    if v.size == 0 or not np.all(np.isfinite(v)) or n == 0:
        return None
    return v / n


class EmbeddingScoreRequest(BaseModel):
    speaker_id: str
    embeddings: list[list[float]] = Field(..., min_length=1, max_length=5000)


class EnrollmentAuditReviewRequest(BaseModel):
    decision: str = Field(pattern="^(confirmed_correct|reset)$")


def _score_embeddings(embeddings: list[list[float]], speaker_id: str) -> list[dict]:
    """Compare precomputed unit embeddings with one live speaker gallery."""
    session = get_db_session()
    try:
        target = (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.id == speaker_id)
            .first()
        )
        if not target:
            raise HTTPException(404, "Target speaker not found")
        centroid = (
            _unit(json.loads(target.embedding_data)) if target.embedding_data else None
        )
        if centroid is None:
            raise HTTPException(422, "Target speaker has no valid centroid")

        clip_vectors = []
        for seg in (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.speaker_id == speaker_id)
            .all()
        ):
            if seg.embedding:
                vector = _unit(json.loads(seg.embedding))
                if vector is not None:
                    clip_vectors.append(vector)

        others = []
        for other in (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.id != speaker_id, Speaker.user_id == target.user_id)
            .all()
        ):
            if other.embedding_data:
                vector = _unit(json.loads(other.embedding_data))
                if vector is not None:
                    others.append((other, vector))

        # Score a corpus batch as matrix multiplications. The old nested Python loops
        # did one np.dot call per corpus/gallery pair, which dominated the supposedly
        # cheap cached-embedding refresh at a few thousand corpus vectors.
        valid: list[tuple[int, np.ndarray]] = []
        results: list[dict] = [{"error": "invalid_embedding"} for _ in embeddings]
        for index, values in enumerate(embeddings):
            emb = _unit(values)
            if emb is not None and emb.shape == centroid.shape:
                valid.append((index, emb))
        if not valid:
            return results

        matrix = np.stack([embedding for _, embedding in valid])
        centroid_scores = matrix @ centroid
        gallery_scores = matrix @ np.stack(clip_vectors).T if clip_vectors else None
        other_scores = (
            matrix @ np.stack([vector for _, vector in others]).T if others else None
        )
        for row, (result_index, _embedding) in enumerate(valid):
            best_other = None
            if other_scores is not None:
                best_index = int(np.argmax(other_scores[row]))
                other, _vector = others[best_index]
                best_other = {
                    "speaker_id": other.id,
                    "name": other.name,
                    "score": round(float(other_scores[row, best_index]), 4),
                }
            results[result_index] = {
                "sim_centroid": round(float(centroid_scores[row]), 4),
                "max_clip_sim": (
                    round(float(np.max(gallery_scores[row])), 4)
                    if gallery_scores is not None
                    else None
                ),
                "n_gallery_clips": len(clip_vectors),
                "best_other": best_other,
            }
        return results
    finally:
        session.close()


@router.post("/enrollment/candidates/score-embeddings")
async def score_enrollment_embeddings(body: EmbeddingScoreRequest):
    """Score cached corpus embeddings without decoding or embedding audio again."""
    return {"scores": _score_embeddings(body.embeddings, body.speaker_id)}


@router.post("/enrollment/candidates/score")
async def score_enrollment_candidate(
    file: UploadFile = File(..., description="Candidate clip (single speaker)"),
    speaker_id: str = Form(..., description="Target enrolled speaker"),
):
    """Score a candidate clip's enrollment value for one target speaker.

    Returns the clip's cosine to the target centroid, its redundancy against the
    target's existing per-clip gallery (max cosine — high means the gallery already
    covers this acoustic condition), and the closest *other* enrolled speaker, so a
    caller can rank candidates by marginal information instead of blind similarity.
    """
    with secure_temp_file() as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        duration = get_audio_info(str(tmp_path)).get("duration_seconds")
        # Lazy: service.py includes this router, so a top-level import is circular.
        from .. import service

        wav = service.audio_backend.load_wave(tmp_path)
        emb = _unit(await service.audio_backend.async_embed(wav))
        if emb is None:
            raise HTTPException(422, "Could not extract a valid embedding from clip")
    finally:
        tmp_path.unlink(missing_ok=True)

    session = get_db_session()
    try:
        target = (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.id == speaker_id)
            .first()
        )
        if not target:
            raise HTTPException(404, "Target speaker not found")
        centroid = (
            _unit(json.loads(target.embedding_data)) if target.embedding_data else None
        )
        if centroid is None:
            raise HTTPException(422, "Target speaker has no valid centroid")

        clip_sims = []
        for seg in (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.speaker_id == speaker_id)
            .all()
        ):
            if not seg.embedding:
                continue
            v = _unit(json.loads(seg.embedding))
            if v is not None:
                clip_sims.append(float(np.dot(emb, v)))

        best_other = None
        others = (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.id != speaker_id, Speaker.user_id == target.user_id)
            .all()
        )
        for other in others:
            if not other.embedding_data:
                continue
            v = _unit(json.loads(other.embedding_data))
            if v is None:
                continue
            score = float(np.dot(emb, v))
            if best_other is None or score > best_other["score"]:
                best_other = {
                    "speaker_id": other.id,
                    "name": other.name,
                    "score": round(score, 4),
                }

        return {
            "duration": round(float(duration), 3) if duration else None,
            "sim_centroid": round(float(np.dot(emb, centroid)), 4),
            "max_clip_sim": round(max(clip_sims), 4) if clip_sims else None,
            "n_gallery_clips": len(clip_sims),
            "best_other": best_other,
        }
    finally:
        session.close()


@router.post("/enrollment/candidates/embed")
async def embed_enrollment_candidate(
    file: UploadFile = File(..., description="Human-labeled evaluation clip"),
):
    """Extract a unit speaker embedding without changing the live gallery."""
    with secure_temp_file() as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        duration = get_audio_info(str(tmp_path)).get("duration_seconds")
        # Lazy: service.py includes this router, so a top-level import is circular.
        from .. import service

        wav = service.audio_backend.load_wave(tmp_path)
        emb = _unit(await service.audio_backend.async_embed(wav))
        if emb is None:
            raise HTTPException(422, "Could not extract a valid embedding from clip")
        return {
            "embedding": emb.tolist(),
            "duration": round(float(duration), 3) if duration else None,
            "embedding_model": service.audio_backend.EMBEDDING_MODEL_ID,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/enrollment/candidates/embedding-info")
async def enrollment_embedding_info():
    """Fingerprint used to invalidate cached evaluation embeddings."""
    # Lazy: service.py includes this router, so a top-level import is circular.
    from .. import service

    return {
        "embedding_model": service.audio_backend.EMBEDDING_MODEL_ID,
    }


@router.post("/enrollment/segments/{segment_id}/relabel")
async def relabel_segment(
    segment_id: int,
    target_speaker_id: str = Form(..., description="Speaker to move this clip to"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Move a mislabeled clip to the speaker it actually belongs to.

    Moves the audio file into the target speaker's directory, reassigns the segment, and
    recomputes both speakers' centroids.
    """
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        require_available(session, seg.speaker_id)
        require_unmanaged_segment(session, seg)
        src_id = seg.speaker_id
        if src_id == target_speaker_id:
            raise HTTPException(400, "Segment is already assigned to that speaker")

        target = (
            session.query(Speaker)
            .filter(allowed_speakers())
            .filter(Speaker.id == target_speaker_id)
            .first()
        )
        if not target:
            raise HTTPException(404, "Target speaker not found")

        base = get_auth().enrollment_audio_dir.resolve()
        old_path = _resolve_seg_path(seg)
        new_dir = base / str(target.user_id) / target_speaker_id
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / old_path.name
        if old_path.exists():
            shutil.move(str(old_path), str(new_path))

        seg.audio_file_path = str(new_path.relative_to(base))
        seg.speaker_id = target_speaker_id
        session.commit()

        recompute_speaker_centroid(session, db, src_id)
        recompute_speaker_centroid(session, db, target_speaker_id)
        log.info(
            "Relabeled segment %s: %s -> %s", segment_id, src_id, target_speaker_id
        )
        return {
            "relabeled": True,
            "segment_id": segment_id,
            "from": src_id,
            "to": target_speaker_id,
        }
    finally:
        session.close()


@router.post("/enrollment/segments/{segment_id}/audit-review")
async def review_enrollment_flag(
    segment_id: int,
    request: EnrollmentAuditReviewRequest,
):
    """Confirm a heuristic flag as a false positive, or restore automatic review."""
    session = get_db_session()
    try:
        segment = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not segment:
            raise HTTPException(404, "Segment not found")
        require_available(session, segment.speaker_id)

        existing = (
            session.query(EnrollmentAuditDecision)
            .filter(EnrollmentAuditDecision.segment_id == segment_id)
            .first()
        )
        if request.decision == "reset":
            if existing:
                session.delete(existing)
        elif existing:
            existing.decision = request.decision
            existing.updated_at = datetime.utcnow()
        else:
            session.add(
                EnrollmentAuditDecision(
                    segment_id=segment_id,
                    decision=request.decision,
                )
            )
        session.commit()
        return {
            "segment_id": segment_id,
            "review_state": None if request.decision == "reset" else request.decision,
        }
    finally:
        session.close()


@router.post("/enrollment/segments/{segment_id}/delete")
async def delete_segment(
    segment_id: int,
    hard: bool = Form(
        False,
        description="Permanently delete the audio file instead of quarantining it",
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Remove a junk clip from a speaker's voiceprint.

    By default the audio is **quarantined** — moved out of the enrollment tree into a
    ``quarantine/<speaker_id>/`` folder under the data dir — so it's removed from the
    gallery (and re-averaged out of the centroid) but recoverable. Pass ``hard=true`` to
    delete the file permanently instead. Either way the SpeakerAudioSegment row is
    dropped so the clip no longer counts toward the voiceprint.
    """
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        require_available(session, seg.speaker_id)
        require_unmanaged_segment(session, seg)
        src_id = seg.speaker_id
        audio_file_path = seg.audio_file_path

        quarantined_to = None
        try:
            path = _resolve_seg_path(seg)
            if path.exists():
                if hard:
                    path.unlink()
                else:
                    quarantine_dir = get_auth().data_dir / "quarantine" / src_id
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    # Prefix with segment id so re-quarantined same-name clips don't clash.
                    dest = quarantine_dir / f"{segment_id}_{path.name}"
                    shutil.move(str(path), str(dest))
                    quarantined_to = str(dest)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                500, f"Could not move/delete audio for segment {segment_id}: {e}"
            ) from e

        _remove_manifest_audio_path(audio_file_path)

        session.delete(seg)
        session.commit()

        recompute_speaker_centroid(session, db, src_id)
        log.info(
            "Removed segment %s from speaker %s (%s)",
            segment_id,
            src_id,
            "hard-deleted" if hard else f"quarantined -> {quarantined_to}",
        )
        return {
            "deleted": True,
            "hard": hard,
            "segment_id": segment_id,
            "speaker_id": src_id,
            "quarantined_to": quarantined_to,
        }
    finally:
        session.close()
