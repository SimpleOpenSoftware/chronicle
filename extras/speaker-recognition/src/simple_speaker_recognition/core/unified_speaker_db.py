"""Unified speaker database combining SQLite metadata with FAISS performance."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

import faiss
import numpy as np

from simple_speaker_recognition.core.gallery_privacy import (
    allowed_speakers,
    require_available,
)
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker, User
from simple_speaker_recognition.database.queries import UserQueries

log = logging.getLogger(__name__)


def _normalize(arr: np.ndarray) -> np.ndarray:
    """Normalize array to unit length."""
    return arr / np.linalg.norm(arr, axis=-1, keepdims=True)


class UnifiedSpeakerDB:
    """Unified speaker database combining SQLite metadata with FAISS performance."""

    def __init__(self, emb_dim: int, base_dir: Path, similarity_thr: float):
        self._lock = asyncio.Lock()
        self.emb_dim = emb_dim
        self.similarity_thr = similarity_thr
        self.base_dir = base_dir
        self.index_path = base_dir / "faiss.index"

        # FAISS index for fast similarity search using cosine similarity (inner product)
        self.index: faiss.IndexFlatIP = faiss.IndexFlatIP(emb_dim)

        # Mapping from FAISS index position to (user_id, speaker_id)
        self.faiss_to_speaker: Dict[int, Tuple[str, str]] = {}

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _load_state(self) -> None:
        """Load FAISS index and rebuild mapping from SQLite."""
        if self.index_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
                log.info("Loaded FAISS index from %s", self.index_path)
            except Exception as e:
                log.warning("Could not load FAISS index, creating new: %s", e)
                self.index = faiss.IndexFlatIP(self.emb_dim)

        # Rebuild mapping from SQLite
        self._rebuild_faiss_mapping()

    def _rebuild_faiss_mapping(self) -> None:
        """Rebuild FAISS index from SQLite data."""
        # SQLite is authoritative. A deletion, empty database or failed rebuild
        # must never leave the previous (possibly quarantined) vectors searchable.
        self.index = faiss.IndexFlatIP(self.emb_dim)
        self.faiss_to_speaker.clear()
        db = get_db_session()
        try:
            speakers = db.query(Speaker).filter(allowed_speakers()).all()
            self.faiss_to_speaker.clear()

            if not speakers:
                log.info("No speakers found in database")
                return

            # Recreate FAISS index for inner product (cosine similarity)
            self.index = faiss.IndexFlatIP(self.emb_dim)

            vectors = []
            for speaker in speakers:
                embedding_data = cast(Optional[str], speaker.embedding_data)
                if embedding_data:
                    try:
                        embedding = np.array(
                            json.loads(embedding_data), dtype=np.float32
                        )
                        if (
                            embedding.shape != (self.emb_dim,)
                            or not np.all(np.isfinite(embedding))
                            or not np.isfinite(np.linalg.norm(embedding))
                            or np.linalg.norm(embedding) <= 0
                        ):
                            raise ValueError("Invalid embedding vector")
                        vector_index = len(vectors)
                        vectors.append(embedding)
                        self.faiss_to_speaker[vector_index] = (
                            cast(str, speaker.user_id),
                            cast(str, speaker.id),
                        )
                    except (json.JSONDecodeError, ValueError, TypeError):
                        log.warning("Skipping invalid gallery embedding")

            if vectors:
                # Normalize all embeddings before adding to FAISS
                normalized_vectors = np.stack([_normalize(v) for v in vectors]).astype(
                    np.float32
                )
                self.index.add(normalized_vectors)
                log.info(
                    "Rebuilt FAISS index with %d speakers (normalized embeddings)",
                    len(vectors),
                )

        except Exception:
            self.index = faiss.IndexFlatIP(self.emb_dim)
            self.faiss_to_speaker.clear()
            log.error("Speaker gallery rebuild failed; gallery held")
            raise
        finally:
            db.close()

    def _save_faiss_index(self) -> None:
        """Save FAISS index to disk."""
        try:
            faiss.write_index(self.index, str(self.index_path))
        except Exception as e:
            log.error("Error saving FAISS index: %s", e)

    async def add_speaker(
        self,
        speaker_id: str,
        name: str,
        embedding: np.ndarray,
        user_id: str,
        sample_count: int = 1,
        total_duration: float = 0.0,
    ) -> bool:
        """Add speaker with user association; return True if updated (False if new)."""
        async with self._lock:
            db = get_db_session()
            try:
                require_available(db, speaker_id)
                # Check if speaker exists
                existing_speaker = (
                    db.query(Speaker)
                    .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                    .first()
                )

                is_update = existing_speaker is not None

                # Prepare embedding data
                embedding_json = json.dumps(embedding.tolist())

                if is_update:
                    # Update existing speaker (replace enrollment)
                    existing_speaker.name = name  # type: ignore[assignment]
                    existing_speaker.embedding_data = embedding_json  # type: ignore[assignment]
                    existing_speaker.audio_sample_count = sample_count  # type: ignore[assignment]
                    existing_speaker.total_audio_duration = total_duration  # type: ignore[assignment]
                    log.info(
                        "Updated existing speaker: %s (user: %s) with %d samples",
                        speaker_id,
                        user_id,
                        sample_count,
                    )

                    # For updates, we need to rebuild since FAISS doesn't support updates
                    db.commit()
                    self._rebuild_faiss_mapping()
                    self._save_faiss_index()
                else:
                    # The tenant is a Chronicle user id, so nothing seeds it ahead of
                    # time; its row is created here, on that user's first enrolment.
                    UserQueries.get_or_create_user(db, user_id)
                    # Create new speaker
                    new_speaker = Speaker(
                        id=speaker_id,
                        name=name,
                        user_id=user_id,
                        embedding_data=embedding_json,
                        audio_sample_count=sample_count,
                        total_audio_duration=total_duration,
                    )
                    db.add(new_speaker)
                    db.commit()
                    log.info(
                        "Added new speaker: %s (user: %s) with %d samples",
                        speaker_id,
                        user_id,
                        sample_count,
                    )

                    # For new speakers, add to FAISS index incrementally
                    normalized_embedding = _normalize(
                        embedding.astype(np.float32)
                    ).reshape(1, -1)
                    self.index.add(normalized_embedding)

                    # Update mapping (new index position is ntotal - 1)
                    new_faiss_idx = self.index.ntotal - 1
                    self.faiss_to_speaker[new_faiss_idx] = (user_id, speaker_id)

                    self._save_faiss_index()

                return is_update

            except Exception as e:
                db.rollback()
                log.error("Error adding speaker %s: %s", speaker_id, e)
                raise
            finally:
                db.close()

    async def delete_speaker(self, speaker_id: str, user_id: str) -> None:
        """Delete a speaker from the database."""
        async with self._lock:
            db = get_db_session()
            try:
                speaker = (
                    db.query(Speaker)
                    .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                    .first()
                )

                if not speaker:
                    raise KeyError(f"Speaker {speaker_id} not found for user {user_id}")

                db.delete(speaker)
                db.commit()

                # Rebuild FAISS index
                self._rebuild_faiss_mapping()
                self._save_faiss_index()

                log.info("Deleted speaker: %s (user: %s)", speaker_id, user_id)

            except Exception as e:
                db.rollback()
                log.error("Error deleting speaker %s: %s", speaker_id, e)
                raise
            finally:
                db.close()

    async def reset_user(self, user_id: str) -> None:
        """Clear all speakers for a specific user."""
        async with self._lock:
            db = get_db_session()
            try:
                db.query(Speaker).filter(Speaker.user_id == user_id).delete()
                db.commit()

                # Rebuild FAISS index
                self._rebuild_faiss_mapping()
                self._save_faiss_index()

                log.info("Reset all speakers for user: %s", user_id)

            except Exception as e:
                db.rollback()
                log.error("Error resetting speakers for user %s: %s", user_id, e)
                raise
            finally:
                db.close()

    async def _rank_candidates(
        self, embedding: np.ndarray, user_id: Optional[str] = None
    ) -> List[Dict]:
        """Return gallery candidates for an embedding, sorted by descending similarity.

        Each candidate is {id, name, user_id, similarity, distance}. Empty list if the
        index is empty or nothing matches the user filter.
        """
        return (await self._rank_candidates_batch([embedding], user_id=user_id))[0]

    async def _rank_candidates_batch(
        self,
        embeddings: List[np.ndarray] | np.ndarray,
        user_id: Optional[str] = None,
    ) -> List[List[Dict]]:
        """Rank gallery candidates for many embeddings with one FAISS and SQL query."""
        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.ndim != 2 or matrix.shape[1] != self.emb_dim:
            raise ValueError(
                f"Expected embeddings shaped (N, {self.emb_dim}), got {matrix.shape}"
            )
        if not len(matrix):
            return []
        if self.index.ntotal == 0:
            return [[] for _ in range(len(matrix))]

        normalized = _normalize(matrix)
        k = min(10, self.index.ntotal)
        similarities, indices = self.index.search(normalized, k)

        candidate_keys = {
            self.faiss_to_speaker[index]
            for row in indices
            for index in row
            if index != -1
            and index in self.faiss_to_speaker
            and (user_id is None or self.faiss_to_speaker[index][0] == user_id)
        }

        db = get_db_session()
        try:
            speaker_ids = [speaker_id for _, speaker_id in candidate_keys]
            speakers = (
                db.query(Speaker).filter(Speaker.id.in_(speaker_ids)).all()
                if speaker_ids
                else []
            )
            metadata = {
                (cast(str, speaker.user_id), cast(str, speaker.id)): speaker
                for speaker in speakers
            }
            ranked_rows: List[List[Dict]] = []
            for row_indices, row_similarities in zip(indices, similarities):
                candidates: List[Dict] = []
                for index, similarity in zip(row_indices, row_similarities):
                    if index == -1 or index not in self.faiss_to_speaker:
                        continue
                    candidate_user_id, speaker_id = self.faiss_to_speaker[index]
                    if user_id is not None and candidate_user_id != user_id:
                        continue
                    speaker = metadata.get((candidate_user_id, speaker_id))
                    if speaker is None:
                        continue
                    cosine_similarity = float(similarity)
                    candidates.append(
                        {
                            "id": speaker.id,
                            "name": speaker.name,
                            "user_id": speaker.user_id,
                            "similarity": cosine_similarity,
                            "distance": 1.0 - cosine_similarity,
                        }
                    )
                ranked_rows.append(
                    sorted(
                        candidates, key=lambda item: item["similarity"], reverse=True
                    )
                )
            return ranked_rows
        except Exception as e:
            log.error("Error during identification: %s", e)
            raise
        finally:
            db.close()

    async def identify(
        self,
        embedding: np.ndarray,
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
    ) -> Tuple[bool, Optional[Dict], float]:
        """Identify speaker from embedding using FAISS search (best match only)."""
        found, speaker, similarity, _ = await self.identify_with_candidates(
            embedding,
            user_id=user_id,
            similarity_threshold=similarity_threshold,
        )
        return found, speaker, similarity

    async def identify_with_candidates(
        self,
        embedding: np.ndarray,
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
    ) -> Tuple[bool, Optional[Dict], float, List[Dict]]:
        """Like :meth:`identify` but also returns the ranked candidate list.

        Lets callers apply an open-set margin (best vs. runner-up) and exclusive
        assignment across multiple diarized speakers.
        """
        return (
            await self.identify_batch_with_candidates(
                [np.asarray(embedding, dtype=np.float32).reshape(-1)],
                user_id=user_id,
                similarity_threshold=similarity_threshold,
            )
        )[0]

    async def identify_batch_with_candidates(
        self,
        embeddings: List[np.ndarray] | np.ndarray,
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
    ) -> List[Tuple[bool, Optional[Dict], float, List[Dict]]]:
        """Identify an ordered embedding batch using one gallery lookup."""
        threshold = (
            self.similarity_thr
            if similarity_threshold is None
            else similarity_threshold
        )
        ranked_rows = await self._rank_candidates_batch(embeddings, user_id=user_id)
        results: List[Tuple[bool, Optional[Dict], float, List[Dict]]] = []
        for ranked in ranked_rows:
            if not ranked:
                results.append((False, None, 0.0, []))
                continue
            best = ranked[0]
            best_similarity = best["similarity"]
            if best_similarity >= threshold:
                results.append(
                    (
                        True,
                        {
                            "id": best["id"],
                            "name": best["name"],
                            "user_id": best["user_id"],
                        },
                        best_similarity,
                        ranked,
                    )
                )
            else:
                results.append((False, None, best_similarity, ranked))
        return results

    async def verify(
        self, speaker_id: str, embedding: np.ndarray, user_id: str
    ) -> float:
        """Verify speaker identity against stored embedding."""
        db = get_db_session()
        try:
            require_available(db, speaker_id)
            speaker = (
                db.query(Speaker)
                .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                .first()
            )

            embedding_data = (
                cast(Optional[str], speaker.embedding_data) if speaker else None
            )
            if not speaker or not embedding_data:
                raise KeyError(f"Speaker {speaker_id} not enrolled for user {user_id}")

            stored_emb = np.array(json.loads(embedding_data), dtype=np.float32)
            return float(
                np.dot(_normalize(embedding.flatten()), _normalize(stored_emb))
            )

        except Exception as e:
            log.error("Error during verification: %s", e)
            raise
        finally:
            db.close()

    def get_speakers_for_user(self, user_id: str) -> List[Dict]:
        """Get all speakers for a specific user."""
        db = get_db_session()
        try:
            speakers = (
                db.query(Speaker)
                .filter(Speaker.user_id == user_id, allowed_speakers())
                .all()
            )
            return [
                {
                    "id": cast(str, speaker.id),
                    "name": cast(str, speaker.name),
                    "user_id": cast(str, speaker.user_id),
                    "created_at": speaker.created_at,
                    "updated_at": speaker.updated_at,
                    "audio_sample_count": cast(
                        Optional[int], speaker.audio_sample_count
                    )
                    or 0,
                    "total_audio_duration": cast(
                        Optional[float], speaker.total_audio_duration
                    )
                    or 0.0,
                }
                for speaker in speakers
            ]
        finally:
            db.close()

    def get_speakers_with_embeddings(self, user_id: str) -> Dict[str, Dict]:
        """Get all speakers with their embeddings for a specific user."""
        db = get_db_session()
        try:
            speakers = (
                db.query(Speaker)
                .filter(Speaker.user_id == user_id, allowed_speakers())
                .all()
            )
            result = {}
            for speaker in speakers:
                embedding_data = cast(Optional[str], speaker.embedding_data)
                if embedding_data:
                    try:
                        embedding = json.loads(embedding_data)
                        result[cast(str, speaker.id)] = {
                            "name": cast(str, speaker.name),
                            "embedding": embedding,
                        }
                    except (json.JSONDecodeError, ValueError) as e:
                        log.warning(
                            "Invalid embedding for speaker %s: %s", speaker.id, e
                        )
            return result
        finally:
            db.close()

    def get_speaker_count(self) -> int:
        """Get total number of enrolled speakers."""
        db = get_db_session()
        try:
            return db.query(Speaker).count()
        finally:
            db.close()

    def ensure_user(self, user_id: str) -> str:
        """Ensure a tenant row exists for ``user_id``, and return it.

        Replaces the old ``ensure_admin_user`` startup seed. A tenant is Chronicle's
        user id now, so the service cannot invent one before a caller names it: it is
        created the first time that user enrols. Seeding at startup also inserted a
        row with no primary key once the column stopped autoincrementing, which
        crashed the service before it could serve health.
        """
        db = get_db_session()
        try:
            return cast(str, UserQueries.get_or_create_user(db, user_id).id)
        finally:
            db.close()
