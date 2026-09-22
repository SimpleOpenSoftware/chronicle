"""One durable gallery-hold predicate shared by readers, writers and rebuilds."""

from sqlalchemy import select

from simple_speaker_recognition.database.models import (
    EnrollmentOperation,
    Speaker,
    SpeakerPrivacyHold,
)


class GalleryHeld(Exception):
    pass


def allowed_speakers():
    return ~Speaker.id.in_(select(SpeakerPrivacyHold.speaker_id))


def require_available(session, speaker_id):
    if session.get(SpeakerPrivacyHold, speaker_id):
        raise GalleryHeld("Speaker enrollment held for privacy review")


def require_unmanaged_segment(session, segment):
    # Direct relabel moves both the file and segment and would sever the durable
    # operation's provenance. Chronicle must quarantine and re-prepare instead.
    if (
        session.query(EnrollmentOperation)
        .filter_by(audio_file_path=segment.audio_file_path)
        .first()
    ):
        raise GalleryHeld("Evidence-bound enrollment requires a new reviewed operation")
