"""Single source of truth for Redis key names used across the backend.

Redis keys were historically built as inline f-strings at each call site, which is
a misnaming hazard: a typo in one of a dozen sites silently points at a different
key. This module centralizes the key *names* as typed builder functions so a key
can be constructed in exactly one place.

It is intentionally dependency-light — stdlib only, no imports of ``workers``,
``services``, ``models``, or ``controllers`` — so it can be imported from anywhere
(including ``utils.conversation_utils``, which the RQ workers import, and
``SessionStore``) without risk of an import cycle.

**Current scope:** session-scoped pointer/lock keys, ``speech_detection_job`` keys,
raw audio stream names, final transcription result stream names, and device
downlink channels. Other key families (``sse:*``, some admin/debug keys, …) are
still built inline and may migrate here later.

**Invariant:** every WebSocket recording has a unique ``session_id``. A client may
have many historical sessions, and each session owns an immutable raw-audio WAL.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class _Identity:
    """Runtime-distinct string identity.

    These are intentionally not ``NewType`` aliases. ``NewType`` helps static
    checkers, but at runtime it is still just ``str``. These value objects let key
    builders reject a session identity where a device identity is required.
    """

    value: str

    @classmethod
    def from_value(cls, value: object, field_name: str | None = None):
        label = field_name or cls.__name__
        if value is None:
            raise ValueError(f"{label} is required")
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"{label} cannot be empty")
        return cls(value)

    def __str__(self) -> str:
        return self.value


class ClientId(_Identity):
    """Stable connected device identity, e.g. ``a421c9-elato``."""


class SessionId(_Identity):
    """Per-WebSocket/audio-stream identity."""


class UserId(_Identity):
    """Chronicle user identity carried across process boundaries."""


class AudioStreamName(_Identity):
    """Redis stream name for raw audio WAL entries."""


class DeviceDownlinkChannel(_Identity):
    """Redis Pub/Sub channel consumed by a connected device WebSocket."""


class TranscriptionResultsStream(_Identity):
    """Redis stream name for final transcription results."""


def _identity_value(identity: _Identity | str) -> str:
    return identity.value if isinstance(identity, _Identity) else identity


def _require_identity(value: object, expected_type: type[_Identity]) -> _Identity:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"expected {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


# --- session-scoped ---


def audio_session(session_id: str | SessionId) -> str:
    """Hash holding the cross-process session state (owned by ``SessionStore``)."""
    return f"audio:session:{_identity_value(session_id)}"


def session_signal(session_id: str | SessionId) -> str:
    """Pub/sub channel for session lifecycle signals."""
    return f"session:signal:{_identity_value(session_id)}"


def session_conversation_count(session_id: str | SessionId) -> str:
    """Counter of conversations opened during the session."""
    return f"session:conversation_count:{_identity_value(session_id)}"


def speech_detection_job(session_id: str | SessionId) -> str:
    """Pointer to the live speech-detection job id for the session."""
    return f"speech_detection_job:{_identity_value(session_id)}"


def speech_detection_enqueue_lock(session_id: str | SessionId) -> str:
    """Single-flight lock guarding speech-detection job enqueue bursts."""
    return f"speech_detection_enqueue_lock:{_identity_value(session_id)}"


def audio_stream(session_id: SessionId) -> AudioStreamName:
    """Redis stream for the session's raw audio WAL."""
    session_id = _require_identity(session_id, SessionId)
    return AudioStreamName.from_value(f"audio:stream:{session_id}")


def parse_audio_stream_name(stream_name: str | AudioStreamName) -> SessionId:
    """Extract a ``SessionId`` from ``audio:stream:{session_id}``."""
    value = _identity_value(stream_name)
    prefix = "audio:stream:"
    if not value.startswith(prefix):
        raise ValueError(f"not an audio stream name: {value!r}")
    return SessionId.from_value(value.removeprefix(prefix), "session_id")


def transcription_results_stream(session_id: SessionId) -> TranscriptionResultsStream:
    """Redis stream for the session's final transcription results."""
    session_id = _require_identity(session_id, SessionId)
    return TranscriptionResultsStream.from_value(f"transcription:results:{session_id}")


def device_downlink_channel(client_id: ClientId) -> DeviceDownlinkChannel:
    """Redis Pub/Sub channel for backend-to-device output."""
    client_id = _require_identity(client_id, ClientId)
    return DeviceDownlinkChannel.from_value(f"device:downlink:{client_id}")


# --- interactive voice and coordinated responses ---


def active_voice_session(user_id: str | UserId, client_id: str | ClientId) -> str:
    """Pointer to the sole non-ended voice session for one authenticated device."""

    return f"voice:active:{_identity_value(user_id)}:{_identity_value(client_id)}"


def voice_session(voice_session_id: str) -> str:
    """Hash containing one version-one voice-session state machine."""

    return f"voice:session:{voice_session_id}"


def response_generation(user_id: str | UserId, client_id: str | ClientId) -> str:
    """Client-wide monotonic output generation shared by every response producer."""

    return (
        f"voice:response:generation:{_identity_value(user_id)}:"
        f"{_identity_value(client_id)}"
    )


def current_response(user_id: str | UserId, client_id: str | ClientId) -> str:
    """Pointer to the current coordinated response for one authenticated device."""

    return (
        f"voice:response:current:{_identity_value(user_id)}:"
        f"{_identity_value(client_id)}"
    )


def voice_response(response_id: str) -> str:
    """Hash containing one response lifecycle record."""

    return f"voice:response:{response_id}"


def timeline_evidence_revision(user_id: str | UserId) -> str:
    """Per-user monotonic evidence-revision counter (INCR) for dirty-range fencing."""
    return f"timeline:evidence_revision:{_identity_value(user_id)}"


def timeline_publication_lock(user_id: str | UserId) -> str:
    """Serialize evidence generations with Timeline publication for one user."""
    return f"timeline:publish:{_identity_value(user_id)}"


def dirty_range_enqueue_lock(dirty_range_id: str) -> str:
    """Single-flight lock guarding reconciliation-job enqueue for one dirty range."""
    return f"timeline:dirty_range_enqueue_lock:{dirty_range_id}"


def voice_processing_owner(interaction_id: str) -> str:
    """Tiny admitted-effect pointer; changed only with interaction ownership."""
    return f"interaction:voice:processing:{interaction_id}"
