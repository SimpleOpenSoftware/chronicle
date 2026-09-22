"""Metadata-only identity of a gallery and the evidence covering its vectors."""

import hashlib
import json

from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import (
    EnrollmentOperation,
    Speaker,
    SpeakerAudioSegment,
    SpeakerCatalogIdentity,
    SpeakerPrivacyHold,
)


def catalog_snapshot(user_id=None):
    with get_db_session() as session:
        identity = session.get(SpeakerCatalogIdentity, 1)
        if identity is None:
            raise RuntimeError("Speaker catalog identity unavailable")
        query = session.query(Speaker)
        if user_id is not None:
            query = query.filter(Speaker.user_id == user_id)
        speakers = query.all()
        ids = [speaker.id for speaker in speakers]
        segments = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.speaker_id.in_(ids))
            .all()
        )
        operations = (
            session.query(EnrollmentOperation)
            .filter(
                EnrollmentOperation.speaker_id.in_(ids),
                EnrollmentOperation.state == "active",
            )
            .all()
        )
        holds = {
            row.speaker_id
            for row in session.query(SpeakerPrivacyHold)
            .filter(SpeakerPrivacyHold.speaker_id.in_(ids))
            .all()
        }
        # Explicitly enrolled clips are standalone assets. A journal is not
        # required for direct uploads, and source recordings may be unavailable.
        # Keep malformed/incomplete profiles held independently of provenance.
        unverified = []
        for speaker in speakers:
            if not speaker.embedding_data or speaker.id in holds:
                continue
            rows = [seg for seg in segments if seg.speaker_id == speaker.id]
            if (
                not rows
                or len(rows) != speaker.audio_sample_count
                or any(
                    not segment.embedding or not segment.audio_file_path
                    for segment in rows
                )
            ):
                unverified.append(speaker.id)
        body = {
            "speakers": sorted(
                (s.id, s.user_id, s.name, s.embedding_data, s.audio_sample_count)
                for s in speakers
            ),
            "segments": sorted(
                (
                    s.id,
                    s.speaker_id,
                    s.audio_file_path,
                    s.embedding,
                    s.start_time,
                    s.end_time,
                )
                for s in segments
            ),
            "operations": sorted(
                (o.id, o.state, o.binding, o.segment_id) for o in operations
            ),
            "holds": sorted(holds),
        }
        revision = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "catalog_id": identity.catalog_id,
            "revision": revision,
            "unverified_speaker_ids": sorted(unverified),
            "held_speaker_ids": sorted(holds),
            "active_operation_ids": sorted(
                op.id for op in operations if op.speaker_id not in holds
            ),
        }


def target_owner(speaker_id=None, segment_id=None):
    with get_db_session() as session:
        if segment_id is not None:
            segment = session.get(SpeakerAudioSegment, segment_id)
            speaker_id = segment.speaker_id if segment else None
        speaker = session.get(Speaker, speaker_id) if speaker_id is not None else None
        return str(speaker.user_id) if speaker else None
