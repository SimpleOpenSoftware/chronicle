import re

OMI_SAMPLE_RATE = 16_000  # Hz
OMI_CHANNELS = 1
OMI_SAMPLE_WIDTH = 2  # bytes (16‑bit)

# A missing generated title must remain visibly machine-detectable. Never replace it
# with transcript text or a plausible label such as "Recording"/"Conversation".
TITLE_NOT_GENERATED = "[title not generated]"

# Reserved diarization label for segments triaged as background/noise (TV, media,
# ambient) rather than a real person. Used by the Data Audit speaker-triage flow:
# applying it sets the segment's speaker to this label AND reclassifies it to a
# non-speech (event) segment, and the enroll step skips it (you never enroll a
# noise voiceprint). Kept in sync with the frontend constant of the same name.
NOISE_LABEL = "Noise"
BACKGROUND_SPEECH_LABEL = "Background Speech"

# Display label for a diarized speaker that was NOT matched to an enrolled
# voiceprint. Speaker recognition assigns "Unknown Speaker 1", "Unknown Speaker 2",
# ... per conversation (identified speakers keep their real name; the remaining
# diarization labels become Unknown Speaker N, in order of appearance). These are
# cosmetic display labels only — they must NEVER be enrolled as real speakers,
# otherwise distinct people get merged into one bogus "Unknown Speaker" voiceprint.
# Kept in sync with the frontend constant of the same name.
UNKNOWN_SPEAKER_PREFIX = "Unknown Speaker"

# Matches the placeholder unknown labels: "Unknown", "Unknown 1",
# "Unknown Speaker", "Unknown Speaker 2", "unknown_speaker_3" (case-insensitive),
# and the raw diarization labels a transcript keeps when speaker recognition did
# not run: Deepgram's "Speaker 0" and pyannote's "SPEAKER_00".
_UNKNOWN_SPEAKER_RE = re.compile(
    r"^\s*(?:unknown(?:[ _]speaker)?(?:[ _]*\d+)?|speaker[ _]*\d+)\s*$", re.IGNORECASE
)


def is_unknown_speaker_label(name: str | None) -> bool:
    """True for placeholder unknown-speaker display labels (never enrollable)."""
    return bool(name) and bool(_UNKNOWN_SPEAKER_RE.match(name))


def is_non_enrollable_speaker(name: str | None) -> bool:
    """True when ``name`` must not be enrolled as a real voiceprint.

    Covers empty/whitespace names, the reserved NOISE_LABEL, and any placeholder
    "Unknown Speaker N" label. These are correction labels, not real people.
    """
    if not name or not name.strip():
        return True
    return name in {NOISE_LABEL, BACKGROUND_SPEECH_LABEL} or is_unknown_speaker_label(
        name
    )
