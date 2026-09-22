"""Staged gallery writes with durable, irreversible quarantine tombstones.

Chronicle owns the privacy decision. This service owns staging and compensation:
prepare never enrolls, activate requires the original evidence binding, and a
quarantine wins against retries or an embedding already in flight. Raw audio is
retained outside the gallery's user/speaker manifest tree.
"""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Annotated, Literal
from weakref import WeakValueDictionary

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func

import simple_speaker_recognition.utils.audio_processing as audio_processing
from simple_speaker_recognition.core.gallery_privacy import (
    GalleryHeld,
    require_available,
)
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import (
    EnrollmentOperation,
    Speaker,
    SpeakerAudioSegment,
    SpeakerCatalogIdentity,
    SpeakerPrivacyHold,
    User,
)

Identity = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Revision = Annotated[int, Field(strict=True, ge=0)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_ids: list[Identity] = Field(min_length=1, max_length=1000)
    privacy_revisions: dict[Identity, dict[Identity, Revision]] = Field(min_length=1)
    capture_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    speaker_id: Identity
    speaker_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    mode: Literal["create", "append"]
    evidence: Evidence | None = None


class OperationError(Exception):
    """Content-free domain error safe to return at the HTTP boundary."""

    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def canonical(binding: Binding) -> str:
    value = binding.model_dump()
    if binding.evidence is not None:
        value["evidence"]["conversation_ids"] = sorted(
            set(binding.evidence.conversation_ids)
        )
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def vector(value, dimension):
    try:
        result = np.asarray(value, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(result))
        if (
            result.shape != (dimension,)
            or not np.all(np.isfinite(result))
            or not math.isfinite(norm)
            or norm <= 0
        ):
            raise ValueError()
        return result / norm
    except (ValueError, TypeError):
        raise OperationError(409, "Enrollment embedding unavailable") from None


_preparing: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class EnrollmentOperations:
    def __init__(self, gallery, audio_backend, audio_root: Path, expected_catalog=None):
        self.gallery = gallery
        self.audio_backend = audio_backend
        self.audio_root = audio_root.resolve()
        self.expected_catalog = expected_catalog

    def catalog_info(self):
        with get_db_session() as session:
            row = session.get(SpeakerCatalogIdentity, 1)
            if row is None:
                raise OperationError(503, "Speaker catalog identity unavailable")
            return {"catalog_id": row.catalog_id}

    def _require_catalog(self, session):
        row = session.get(SpeakerCatalogIdentity, 1)
        if (
            row is None
            or not self.expected_catalog
            or row.catalog_id != self.expected_catalog
        ):
            raise OperationError(409, "Speaker catalog identity changed")

    @staticmethod
    def _identity(operation_id):
        if len(operation_id) != 32 or any(
            c not in "0123456789abcdef" for c in operation_id
        ):
            raise OperationError(422, "Invalid enrollment operation")

    @staticmethod
    def _read(session, operation_id, user_id):
        op = session.get(EnrollmentOperation, operation_id)
        if op and op.user_id != user_id:
            raise OperationError(404, "Enrollment operation unavailable")
        return op

    @staticmethod
    def _available(op):
        if op and op.state == "quarantined":
            raise OperationError(423, "Enrollment quarantined")

    @staticmethod
    def _reply(op):
        return {
            "operation_id": op.id,
            "state": op.state,
            "speaker_id": op.speaker_id,
            "segment_id": op.segment_id,
        }

    def _target(self, session, binding):
        try:
            require_available(session, binding.speaker_id)
        except GalleryHeld:
            raise OperationError(
                423, "Speaker enrollment held for privacy review"
            ) from None
        speaker = session.get(Speaker, binding.speaker_id)
        if speaker and speaker.user_id != binding.user_id:
            raise OperationError(404, "Speaker unavailable")
        if binding.mode == "create" and speaker:
            raise OperationError(409, "Speaker already exists")
        if binding.mode == "append" and (not speaker or not speaker.embedding_data):
            raise OperationError(409, "Speaker enrollment unavailable")
        duplicate = (
            session.query(Speaker)
            .filter(
                Speaker.user_id == binding.user_id,
                func.lower(func.trim(Speaker.name)) == binding.speaker_name.lower(),
                Speaker.id != binding.speaker_id,
            )
            .first()
        )
        if duplicate:
            raise OperationError(409, "Speaker name already exists")
        if speaker and speaker.name != binding.speaker_name:
            raise OperationError(409, "Speaker changed since preparation")
        return speaker

    async def prepare(self, operation_id, binding: Binding, audio: bytes):
        self._identity(operation_id)
        if not audio or len(audio) > 32 * 1024 * 1024:
            raise OperationError(413, "Invalid enrollment audio size")
        digest = hashlib.sha256(audio).hexdigest()
        serialized = canonical(binding)
        lock = _preparing.setdefault(operation_id, asyncio.Lock())
        async with lock:
            async with self.gallery._lock:
                with get_db_session() as session:
                    self._require_catalog(session)
                    op = self._read(session, operation_id, binding.user_id)
                    self._available(op)
                    try:
                        require_available(session, binding.speaker_id)
                    except GalleryHeld:
                        raise OperationError(
                            423, "Speaker enrollment held for privacy review"
                        ) from None
                    if op:
                        if op.binding != serialized or op.audio_sha256 != digest:
                            raise OperationError(
                                409, "Enrollment operation identity conflict"
                            )
                        if op.state in {"prepared", "active"}:
                            return self._reply(op)
                    else:
                        self._target(session, binding)
                        op = EnrollmentOperation(
                            id=operation_id,
                            user_id=binding.user_id,
                            state="preparing",
                            binding=serialized,
                            audio_sha256=digest,
                            speaker_id=binding.speaker_id,
                            audio_file_path=f".privacy-operations/{operation_id}.wav",
                        )
                        session.add(op)
                        session.commit()
                    relative_path = op.audio_file_path

            path = self.audio_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The row precedes the file. An interrupted write is retried from the
            # exact bound bytes; activation separately verifies their checksum.
            with path.open("wb") as stream:
                os.chmod(path, 0o600)
                stream.write(audio)
                stream.flush()
                os.fsync(stream.fileno())
            try:

                info = await asyncio.to_thread(
                    audio_processing.get_audio_info, str(path)
                )
                duration = float(info["duration_seconds"])
                if not math.isfinite(duration) or not 0 < duration <= 180:
                    raise ValueError()
                wav = await asyncio.to_thread(self.audio_backend.load_wave, path)
                embedding = vector(
                    await self.audio_backend.async_embed(wav), self.gallery.emb_dim
                )
            except OperationError:
                raise
            except Exception:
                raise OperationError(
                    503, "Enrollment model or audio unavailable"
                ) from None

            async with self.gallery._lock:
                with get_db_session() as session:
                    self._require_catalog(session)
                    op = self._read(session, operation_id, binding.user_id)
                    self._available(op)
                    op.embedding = json.dumps(embedding.tolist())
                    op.duration_seconds = duration
                    op.state = "prepared"
                    session.commit()
                    return self._reply(op)

    async def activate(self, operation_id, binding: Binding):
        self._identity(operation_id)
        async with self.gallery._lock:
            with get_db_session() as session:
                self._require_catalog(session)
                op = self._read(session, operation_id, binding.user_id)
                self._available(op)
                if not op or op.binding != canonical(binding):
                    raise OperationError(409, "Enrollment operation identity conflict")
                try:
                    require_available(session, binding.speaker_id)
                except GalleryHeld:
                    raise OperationError(
                        423, "Speaker enrollment held for privacy review"
                    ) from None
                if op.state == "active":
                    segment = session.get(SpeakerAudioSegment, op.segment_id)
                    speaker = session.get(Speaker, binding.speaker_id)
                    if (
                        segment is None
                        or speaker is None
                        or not speaker.embedding_data
                        or segment.audio_file_path != op.audio_file_path
                        or segment.speaker_id != binding.speaker_id
                        or speaker.user_id != binding.user_id
                        or speaker.name != binding.speaker_name
                    ):
                        raise OperationError(423, "Enrollment contribution unavailable")
                    # Rebuild on replay too: a previous call may have committed
                    # SQLite then failed while refreshing the in-memory index.
                    self.gallery._rebuild_faiss_mapping()
                    return self._reply(op)
                if op.state != "prepared":
                    raise OperationError(409, "Enrollment is not prepared")
                speaker = self._target(session, binding)
                path = self.audio_root / op.audio_file_path
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    raise OperationError(423, "Enrollment audio unavailable") from None
                if digest != op.audio_sha256:
                    raise OperationError(423, "Enrollment audio changed")
                segments = (
                    session.query(SpeakerAudioSegment)
                    .filter_by(speaker_id=binding.speaker_id)
                    .all()
                )
                # Do not add identical bytes through a fresh operation ID. Only
                # replaying the original operation is an idempotent activation.
                for segment in segments:
                    try:
                        existing = (self.audio_root / segment.audio_file_path).resolve()
                        existing.relative_to(self.audio_root)
                        if hashlib.sha256(existing.read_bytes()).hexdigest() == digest:
                            raise OperationError(
                                409, "Enrollment audio already present"
                            )
                    except (OSError, ValueError):
                        raise OperationError(
                            423, "Existing enrollment audio unverified"
                        ) from None
                embedding = vector(json.loads(op.embedding), self.gallery.emb_dim)
                count = int(speaker.audio_sample_count or 0) if speaker else 0
                duration = float(speaker.total_audio_duration or 0) if speaker else 0.0
                if speaker:
                    if count <= 0 or not math.isfinite(duration):
                        raise OperationError(423, "Existing enrollment unverified")
                    old = vector(
                        json.loads(speaker.embedding_data), self.gallery.emb_dim
                    )
                    centroid = vector(old * count + embedding, self.gallery.emb_dim)
                else:
                    if not session.get(User, binding.user_id):
                        session.add(User(id=binding.user_id, username=binding.user_id))
                        session.flush()
                    speaker = Speaker(
                        id=binding.speaker_id,
                        user_id=binding.user_id,
                        name=binding.speaker_name,
                    )
                    session.add(speaker)
                    centroid = embedding
                speaker.embedding_data = json.dumps(centroid.tolist())
                speaker.audio_sample_count = count + 1
                speaker.total_audio_duration = duration + op.duration_seconds
                session.flush()
                segment = SpeakerAudioSegment(
                    speaker_id=binding.speaker_id,
                    audio_file_path=op.audio_file_path,
                    start_time=0,
                    end_time=op.duration_seconds,
                    duration_seconds=op.duration_seconds,
                    embedding=op.embedding,
                )
                session.add(segment)
                session.flush()
                op.segment_id = segment.id
                op.state = "active"
                session.commit()
                reply = self._reply(op)
            self.gallery._rebuild_faiss_mapping()
            self.gallery._save_faiss_index()
            return reply

    async def quarantine(self, operation_id, user_id):
        self._identity(operation_id)
        async with self.gallery._lock:
            with get_db_session() as session:
                self._require_catalog(session)
                op = self._read(session, operation_id, user_id)
                if not op:
                    op = EnrollmentOperation(
                        id=operation_id, user_id=user_id, state="quarantined"
                    )
                    session.add(op)
                if op.state == "active":
                    # Locate by immutable path, not a possibly recycled SQL id.
                    segments = (
                        session.query(SpeakerAudioSegment)
                        .filter_by(audio_file_path=op.audio_file_path)
                        .all()
                    )
                    affected = {s.speaker_id for s in segments} | {op.speaker_id}
                    for speaker_id in affected:
                        speaker = session.get(Speaker, speaker_id)
                        if not speaker:
                            continue
                        rows = (
                            session.query(SpeakerAudioSegment)
                            .filter_by(speaker_id=speaker_id)
                            .all()
                        )
                        remaining = [
                            s for s in rows if s.audio_file_path != op.audio_file_path
                        ]
                        separable = len(rows) == speaker.audio_sample_count and len(
                            {s.audio_file_path for s in rows}
                        ) == len(rows)
                        try:
                            vectors = [
                                vector(json.loads(s.embedding), self.gallery.emb_dim)
                                for s in remaining
                            ]
                            if not separable or not vectors:
                                raise ValueError()
                            centroid = vector(
                                np.sum(vectors, axis=0), self.gallery.emb_dim
                            )
                            speaker.embedding_data = json.dumps(centroid.tolist())
                            speaker.audio_sample_count = len(remaining)
                            speaker.total_audio_duration = sum(
                                s.duration_seconds for s in remaining
                            )
                        except (ValueError, TypeError, OperationError):
                            # Clearing the last contribution also holds profile
                            # metadata, including the name in lists and exports.
                            if not session.get(SpeakerPrivacyHold, speaker_id):
                                session.add(
                                    SpeakerPrivacyHold(
                                        speaker_id=speaker_id,
                                        user_id=speaker.user_id,
                                        operation_id=operation_id,
                                    )
                                )
                            speaker.embedding_data = None
                            speaker.audio_sample_count = 0
                            speaker.total_audio_duration = 0
                    for segment in segments:
                        session.delete(segment)
                op.state = "quarantined"
                session.commit()
                reply = self._reply(op)
            self.gallery._rebuild_faiss_mapping()
            self.gallery._save_faiss_index()
            return reply
