"""Enrollment health / contamination audit over per-clip speaker embeddings.

Pure vector math over ``SpeakerAudioSegment.embedding`` rows — no GPU and no model
load. Each enrolled clip is scored two ways:

* **self-similarity** (leave-one-out): cosine of the clip against the centroid of its
  speaker's *other* clips. Low/negative => the clip doesn't sound like the rest of the
  speaker's enrolment (noise, silence, wrong content, or a mislabel).
* **best-other**: cosine against every *other* speaker's centroid. If a clip matches a
  different speaker far better than its own, it's almost certainly filed under the wrong
  name.

This is what surfaces enrolment contamination (e.g. a clip of speaker A accidentally
saved under speaker B) which a single averaged centroid silently dilutes. It also backs
the corrective actions (relabel / delete) via :func:`recompute_speaker_centroid`.
"""

import collections
import json
import logging
import os
from datetime import datetime
from typing import Optional

import numpy as np

from simple_speaker_recognition.core.gallery_privacy import (
    allowed_speakers,
    require_available,
)
from simple_speaker_recognition.database.models import (
    EnrollmentAuditDecision,
    Speaker,
    SpeakerAudioSegment,
)

log = logging.getLogger("speaker_service")

# A clip is a *mislabel* suspect when another speaker beats its own by at least
# MISLABEL_MARGIN and is itself reasonably similar (>= MISLABEL_MIN).
MISLABEL_MARGIN = 0.15
MISLABEL_MIN = 0.40
# Below JUNK_SELF with no better home => junk (noise/silence/too short).
JUNK_SELF = 0.25
# Between JUNK_SELF and WEAK_SELF => borderline, worth a look but not clearly bad.
WEAK_SELF = 0.35


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _load_segments(session, user_id: Optional[str], before: Optional[datetime] = None):
    """Return [(SpeakerAudioSegment, Speaker, unit_vec)] with valid embeddings."""
    q = session.query(SpeakerAudioSegment, Speaker).join(
        Speaker, SpeakerAudioSegment.speaker_id == Speaker.id
    )
    q = q.filter(allowed_speakers())
    if user_id is not None:
        q = q.filter(Speaker.user_id == user_id)
    if before is not None:
        q = q.filter(SpeakerAudioSegment.created_at < before)

    rows = []
    for seg, spk in q.all():
        if not seg.embedding:
            continue
        try:
            v = np.asarray(json.loads(seg.embedding), dtype=np.float32).reshape(-1)
        except (json.JSONDecodeError, ValueError):
            continue
        if v.size == 0 or not np.all(np.isfinite(v)) or np.linalg.norm(v) == 0:
            continue
        rows.append((seg, spk, _norm(v)))
    return rows


def compute_audit(
    session, user_id: Optional[str] = None, before: Optional[datetime] = None
) -> dict:
    """Build the enrollment-health report for a user (or all users)."""
    rows = _load_segments(session, user_id, before)
    segment_ids = [seg.id for seg, _, _ in rows]
    decisions = {
        row.segment_id: row.decision
        for row in session.query(EnrollmentAuditDecision)
        .filter(EnrollmentAuditDecision.segment_id.in_(segment_ids))
        .all()
    }

    by_spk: dict = collections.defaultdict(list)  # speaker_id -> [(seg, vec)]
    name_by_id: dict = {}
    for seg, spk, v in rows:
        by_spk[spk.id].append((seg, v))
        name_by_id[spk.id] = spk.name

    # One full centroid per speaker (used as each clip's "other speaker" reference).
    cents = {
        sid: _norm(np.stack([v for _, v in lst]).mean(0)) for sid, lst in by_spk.items()
    }

    speakers_out = []
    for sid, lst in by_spk.items():
        M = np.stack([v for _, v in lst])
        clips = []
        selfs = []
        n_flag = 0
        for k, (seg, v) in enumerate(lst):
            if len(lst) > 1:
                rest = np.delete(M, k, axis=0)
                self_score = float(_norm(rest.mean(0)) @ v)
            else:
                self_score = None  # single-clip speaker: can't validate against itself

            best_id = best_name = None
            best = -2.0
            for s2, c2 in cents.items():
                if s2 == sid:
                    continue
                sc = float(c2 @ v)
                if sc > best:
                    best, best_id, best_name = sc, s2, name_by_id[s2]

            flags = []
            suggested = None
            if (
                self_score is not None
                and best >= MISLABEL_MIN
                and best - self_score > MISLABEL_MARGIN
            ):
                flags.append("mislabel")
                suggested = {
                    "speaker_id": best_id,
                    "name": best_name,
                    "score": round(best, 3),
                }
            elif self_score is not None and self_score < JUNK_SELF:
                flags.append("junk")
            elif self_score is not None and self_score < WEAK_SELF:
                flags.append("weak")

            heuristic_flags = flags
            review_state = decisions.get(seg.id)
            if review_state == "confirmed_correct":
                flags = []

            if flags:
                n_flag += 1
            if self_score is not None:
                selfs.append(self_score)

            clips.append(
                {
                    "segment_id": seg.id,
                    "filename": os.path.basename(seg.audio_file_path),
                    "duration": round(seg.duration_seconds or 0.0, 2),
                    "self_score": (
                        round(self_score, 3) if self_score is not None else None
                    ),
                    "best_other": (
                        {
                            "speaker_id": best_id,
                            "name": best_name,
                            "score": round(best, 3),
                        }
                        if best_id is not None
                        else None
                    ),
                    "flags": flags,
                    "heuristic_flags": heuristic_flags,
                    "review_state": review_state,
                    "suggested": suggested,
                }
            )

        clips.sort(
            key=lambda c: (c["self_score"] if c["self_score"] is not None else 1.0)
        )
        med = float(np.median(selfs)) if selfs else None
        if len(lst) == 1:
            verdict = "unverifiable"
        elif n_flag > 0:
            verdict = "contaminated"
        elif med is not None and med < WEAK_SELF:
            verdict = "weak"
        else:
            verdict = "clean"

        speakers_out.append(
            {
                "speaker_id": sid,
                "name": name_by_id[sid],
                "n_clips": len(lst),
                "n_flagged": n_flag,
                "median_self": round(med, 3) if med is not None else None,
                "verdict": verdict,
                "clips": clips,
            }
        )

    # Contaminated first, then weakest median self-similarity.
    speakers_out.sort(
        key=lambda s: (
            0 if s["n_flagged"] else 1,
            s["median_self"] if s["median_self"] is not None else 1.0,
        )
    )

    return {
        "speakers": speakers_out,
        "thresholds": {
            "mislabel_margin": MISLABEL_MARGIN,
            "mislabel_min": MISLABEL_MIN,
            "junk_self": JUNK_SELF,
            "weak_self": WEAK_SELF,
        },
        "total_clips": len(rows),
        "speakers_without_segments": _speakers_without_segments(
            session, user_id, set(by_spk)
        ),
    }


def _speakers_without_segments(session, user_id: Optional[str], have: set) -> list:
    """Speakers that have a centroid but no per-clip segments (can't be audited yet)."""
    q = session.query(Speaker)
    if user_id is not None:
        q = q.filter(Speaker.user_id == user_id)
    return [{"speaker_id": s.id, "name": s.name} for s in q.all() if s.id not in have]


def recompute_speaker_centroid(session, db, speaker_id: str) -> None:
    """Re-derive a speaker's single centroid from its remaining segment embeddings.

    Mirrors ``enroll_batch`` (mean of unit embeddings, renormalized), updates the
    ``Speaker`` row's centroid/counts, and refreshes the in-memory FAISS index so the
    next identification uses the cleaned voiceprint. If no segments remain the speaker's
    centroid is cleared (it drops out of the gallery until re-enrolled).
    """
    require_available(session, speaker_id)
    segs = (
        session.query(SpeakerAudioSegment)
        .filter(SpeakerAudioSegment.speaker_id == speaker_id)
        .all()
    )
    speaker = session.query(Speaker).filter(Speaker.id == speaker_id).first()
    if speaker is None:
        return

    embs = []
    dur = 0.0
    seen_audio_paths: set[str] = set()
    for s in segs:
        # Historical imports could create more than one row for the same stored WAV.
        # Counting those rows independently overweights one clip in the centroid and
        # inflates the public enrollment sample count/duration. New enrollments are
        # content-hash deduplicated, but keep this invariant at the scoring boundary
        # so an old or manually edited database cannot corrupt identification again.
        if s.audio_file_path in seen_audio_paths:
            log.error(
                "Ignoring duplicate enrollment audio path while rebuilding %s: "
                "segment_id=%s path=%s",
                speaker_id,
                s.id,
                s.audio_file_path,
            )
            continue
        seen_audio_paths.add(s.audio_file_path)
        if not s.embedding:
            continue
        try:
            v = np.asarray(json.loads(s.embedding), dtype=np.float32).reshape(-1)
        except (json.JSONDecodeError, ValueError):
            continue
        if v.size and np.all(np.isfinite(v)) and np.linalg.norm(v) > 0:
            embs.append(v)
            dur += s.duration_seconds or 0.0

    if embs:
        centroid = _norm(np.mean(np.stack(embs), axis=0))
        speaker.embedding_data = json.dumps(centroid.tolist())
        speaker.audio_sample_count = len(embs)
        speaker.total_audio_duration = dur
    else:
        speaker.embedding_data = None
        speaker.audio_sample_count = 0
        speaker.total_audio_duration = 0.0

    session.commit()
    db._rebuild_faiss_mapping()
    db._save_faiss_index()
