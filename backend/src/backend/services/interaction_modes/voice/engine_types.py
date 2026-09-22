"""In-process speech-engine events, independent of capture and delivery transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

VoiceActivity = Literal["generating_text", "synthesizing_speech"]
VoiceActivityObserver = Callable[[VoiceActivity, bool], None]


@dataclass(frozen=True)
class VoiceText:
    text: str
    phrase_index: int
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class VoiceAudio:
    """Unpadded signed PCM16LE; sample positions share one response timeline."""

    pcm: bytes
    phrase_index: int
    phrase_text: str
    start_sample: int
    phrase_final: bool
    sample_rate: int = 24_000
    type: Literal["audio"] = "audio"

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // 2


@dataclass(frozen=True)
class VoiceToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class VoiceCompleted:
    text: str
    finish_reason: str
    total_samples: int
    type: Literal["completed"] = "completed"


VoiceEngineEvent = VoiceText | VoiceAudio | VoiceToolCall | VoiceCompleted


@dataclass(frozen=True)
class VoiceEngineSettings:
    """Bound full-phrase TTS until the configured synthesizer supports streaming.

    One prefetched synthesis overlaps delivery of the current phrase. Each
    phrase is bounded to 3.82 seconds; together with the coordinator's existing
    two-second reservoir and codec/browser framing, application-owned unrendered
    audio remains below ten seconds. The renderer/transport limit stays two
    seconds. One prefetch task owns its slot through synthesis and consumption,
    preventing a third synthesis from running ahead. Generation cancellation
    closes that task and its provider, discarding all unsent audio.
    """

    operation: str = "voice_conversation"
    max_phrase_chars: int = 32
    max_response_chars: int = 8192
    max_phrase_audio_seconds: float = 3.82
    provider_timeout_seconds: float = 30.0
    max_wav_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not self.operation:
            raise ValueError("voice operation must be configured")
        if not 1 <= self.max_phrase_chars <= self.max_response_chars:
            raise ValueError("phrase and response character limits must be positive")
        if self.max_phrase_audio_seconds <= 0 or self.provider_timeout_seconds <= 0:
            raise ValueError("voice duration limits must be positive")
        if self.max_wav_bytes <= 0:
            raise ValueError("WAV byte limit must be positive")


class VoiceEngineError(RuntimeError):
    """A response failed; callers must terminate its generation, never fall back."""
