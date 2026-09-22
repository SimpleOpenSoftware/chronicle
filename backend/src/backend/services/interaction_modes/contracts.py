"""Data contracts shared by interaction-mode plugins and the core runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

InteractionSource = Literal["committed", "streaming", "wake", "system"]
InteractionInputKind = Literal["start", "turn"]
InteractionStatus = Literal["active", "ended"]


@dataclass(frozen=True)
class AudioInterval:
    """One immutable audio episode in a capture session's native clock."""

    audio_session_id: str
    capture_epoch: int
    start_ms: float
    end_ms: float
    voice_session_id: Optional[str] = None
    turn_id: Optional[str] = None
    turn_revision: int = 0

    def __post_init__(self) -> None:
        if not self.audio_session_id:
            raise ValueError("audio_session_id is required")
        if self.capture_epoch < 0:
            raise ValueError("capture_epoch must be non-negative")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("audio interval must have positive ordered bounds")
        if self.turn_revision < 0:
            raise ValueError("turn_revision must be non-negative")


@dataclass(frozen=True)
class InteractionModeDefinition:
    """A plugin-owned mode and the phrases that may activate it."""

    mode_id: str
    activation_phrases: tuple[str, ...]
    idle_timeout_seconds: int = 10 * 60
    max_duration_seconds: int = 30 * 60

    def __post_init__(self) -> None:
        if not self.mode_id.strip():
            raise ValueError("interaction mode_id cannot be empty")
        if not self.activation_phrases:
            raise ValueError(
                f"interaction mode '{self.mode_id}' needs an activation phrase"
            )
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if self.max_duration_seconds < self.idle_timeout_seconds:
            raise ValueError("max_duration_seconds must be at least the idle timeout")


@dataclass
class InteractionInput:
    """One accepted utterance in an interaction session."""

    input_id: str
    interaction_id: str
    kind: InteractionInputKind
    user_id: str
    client_id: str
    audio_interval: AudioInterval
    text: str
    source: InteractionSource
    received_at: float
    response_generation: int
    activation_phrase: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InteractionInput":
        return cls(
            **{
                **value,
                "audio_interval": AudioInterval(**value["audio_interval"]),
            }
        )

    @property
    def audio_session_id(self) -> str:
        return self.audio_interval.audio_session_id


@dataclass
class InteractionSession:
    """Durable state for a mode, independent from semantic Conversations."""

    interaction_id: str
    mode_id: str
    owner_plugin_id: str
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    voice_session_id: Optional[str]
    response_generation: int
    response_turn_id: str
    response_turn_revision: int
    phase: str
    plugin_state: dict[str, Any]
    started_at: float
    last_activity_at: float
    idle_timeout_seconds: int
    max_duration_seconds: int
    status: InteractionStatus = "active"
    turn_number: int = 0
    ended_at: Optional[float] = None
    end_reason: Optional[str] = None
    revision: int = 0

    @property
    def idle_deadline(self) -> float:
        return self.last_activity_at + self.idle_timeout_seconds

    @property
    def hard_deadline(self) -> float:
        return self.started_at + self.max_duration_seconds

    @property
    def next_deadline(self) -> float:
        return min(self.idle_deadline, self.hard_deadline)

    def expiry_reason(self, now: float) -> Optional[str]:
        if self.status != "active":
            return self.end_reason or "ended"
        if now >= self.hard_deadline:
            return "max_duration"
        if now >= self.idle_deadline:
            return "idle_timeout"
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InteractionSession":
        return cls(**value)


@dataclass
class DialoguePluginInput:
    """A typed dialogue input does not invent a captured audio interval."""

    input_id: str
    text: str


@dataclass
class InteractionContext:
    """Context supplied to a mode plugin for a start, turn, or end callback."""

    session: InteractionSession
    input: Optional[InteractionInput | DialoguePluginInput]
    services: Any = None
    dialogue_thread_id: Optional[str] = None
    end_reason: Optional[str] = None
    # A plugin must await this after recording an intent in ``session`` and
    # before starting a non-idempotent external side effect. It is absent only
    # in isolated callback tests; the production processor always supplies it.
    checkpoint: Optional[Callable[[], Awaitable[None]]] = None


@dataclass
class InteractionResult:
    """A mode callback's requested state transition and user-facing reply.

    ``plugin_state`` is a full replacement, not a shallow patch.  This makes the
    Redis write deterministic and prevents removed cart candidates or stale
    confirmation flags from leaking into later turns.
    """

    reply: Optional[str] = None
    phase: Optional[str] = None
    plugin_state: Optional[dict[str, Any]] = None
    end: bool = False
    end_reason: Optional[str] = None
    event_data: dict[str, Any] = field(default_factory=dict)
