"""Reusable active-session turn segmentation over low-latency native frames."""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from typing import Literal

MAX_OPEN_TURN_MS = 60_000
MAX_REOPEN_WINDOW_MS = 7_000
PRE_ROLL_MS = 500
ENDPOINT_SILENCE_MS = 100


@dataclass(frozen=True)
class TurnPolicy:
    complete_grace_ms: int
    incomplete_grace_ms: int

    @classmethod
    def conversational(cls) -> "TurnPolicy":
        return cls(complete_grace_ms=800, incomplete_grace_ms=2_000)

    @classmethod
    def dictation(cls) -> "TurnPolicy":
        return cls(complete_grace_ms=2_500, incomplete_grace_ms=4_000)


@dataclass(frozen=True)
class TurnFrame:
    voice_session_id: str
    audio_session_id: str
    capture_epoch: int
    sequence: int
    monotonic_offset_ms: float
    duration_ms: float
    pcm: bytes
    speech: bool
    captured_at_ms: float | None = None
    device_monotonic_ms: float | None = None

    @property
    def end_ms(self) -> float:
        return self.monotonic_offset_ms + self.duration_ms


TurnEventKind = Literal["opened", "reopened", "soft_ended", "committed", "cancelled"]


@dataclass(frozen=True)
class TurnEvent:
    kind: TurnEventKind
    turn_id: str
    revision: int
    voice_session_id: str
    audio_session_id: str
    capture_epoch: int
    start_sequence: int
    end_sequence: int
    started_at_ms: float
    ended_at_ms: float
    pcm: bytes
    reason: str
    deadline_ms: float | None = None
    speech_started_device_ms: float | None = None
    speech_ended_device_ms: float | None = None
    speech_started_at_ms: float | None = None
    speech_ended_at_ms: float | None = None


@dataclass
class _OpenTurn:
    turn_id: str
    revision: int
    frames: list[TurnFrame]
    opened_at_ms: float
    silence_ms: float = 0
    soft_deadline_ms: float | None = None
    first_soft_end_ms: float | None = None


class TurnSegmenter:
    """Open, reopen, and commit turns without ever dispatching known partial audio."""

    def __init__(self, policy: TurnPolicy):
        self.policy = policy
        self._pre_roll: deque[TurnFrame] = deque()
        self._turn: _OpenTurn | None = None
        self._voice_session_id: str | None = None
        self._audio_session_id: str | None = None
        self._capture_epoch: int | None = None
        self._last_sequence: int | None = None

    def set_policy(self, policy: TurnPolicy) -> None:
        self.policy = policy

    def _remember_pre_roll(self, frame: TurnFrame) -> None:
        self._pre_roll.append(frame)
        cutoff = frame.end_ms - PRE_ROLL_MS
        while self._pre_roll and self._pre_roll[0].end_ms < cutoff:
            self._pre_roll.popleft()

    def _event(
        self,
        kind: TurnEventKind,
        turn: _OpenTurn,
        *,
        reason: str,
        deadline_ms: float | None = None,
    ) -> TurnEvent:
        first = turn.frames[0]
        last = turn.frames[-1]
        voiced = [frame for frame in turn.frames if frame.speech]
        onset, offset = (voiced[0], voiced[-1]) if voiced else (None, None)
        return TurnEvent(
            kind=kind,
            turn_id=turn.turn_id,
            revision=turn.revision,
            voice_session_id=last.voice_session_id,
            audio_session_id=last.audio_session_id,
            capture_epoch=last.capture_epoch,
            start_sequence=first.sequence,
            end_sequence=last.sequence,
            started_at_ms=first.monotonic_offset_ms,
            ended_at_ms=last.end_ms,
            pcm=b"".join(frame.pcm for frame in turn.frames),
            reason=reason,
            deadline_ms=deadline_ms,
            speech_started_device_ms=onset.device_monotonic_ms if onset else None,
            speech_ended_device_ms=(
                offset.device_monotonic_ms + offset.duration_ms
                if offset and offset.device_monotonic_ms is not None
                else None
            ),
            speech_started_at_ms=onset.captured_at_ms if onset else None,
            speech_ended_at_ms=(
                offset.captured_at_ms + offset.duration_ms
                if offset and offset.captured_at_ms is not None
                else None
            ),
        )

    def _cancel(self, reason: str) -> TurnEvent | None:
        if self._turn is None:
            return None
        event = self._event("cancelled", self._turn, reason=reason)
        self._turn = None
        return event

    def _open(self, frame: TurnFrame) -> TurnEvent:
        frames = list(self._pre_roll)
        if not frames or frames[-1].sequence != frame.sequence:
            frames.append(frame)
        self._turn = _OpenTurn(
            turn_id=str(uuid.uuid4()),
            revision=0,
            frames=frames,
            opened_at_ms=frame.monotonic_offset_ms,
        )
        return self._event("opened", self._turn, reason="vad_onset")

    async def push(
        self,
        frame: TurnFrame,
        *,
        semantic_complete: bool | None = None,
    ) -> list[TurnEvent]:
        events: list[TurnEvent] = []
        if frame.duration_ms <= 0 or not frame.pcm:
            raise ValueError("turn frame must contain positive-duration PCM")

        epoch_changed = self._capture_epoch is not None and (
            frame.voice_session_id != self._voice_session_id
            or frame.audio_session_id != self._audio_session_id
            or frame.capture_epoch != self._capture_epoch
        )
        if epoch_changed:
            cancelled = self._cancel("epoch_changed")
            if cancelled:
                events.append(cancelled)
            self._pre_roll.clear()
            self._last_sequence = None
        elif (
            self._last_sequence is not None
            and frame.sequence != self._last_sequence + 1
        ):
            cancelled = self._cancel("frame_gap")
            if cancelled:
                events.append(cancelled)
            self._pre_roll.clear()
            self._last_sequence = frame.sequence
            self._voice_session_id = frame.voice_session_id
            self._audio_session_id = frame.audio_session_id
            self._capture_epoch = frame.capture_epoch
            self._remember_pre_roll(frame)
            return events

        self._voice_session_id = frame.voice_session_id
        self._audio_session_id = frame.audio_session_id
        self._capture_epoch = frame.capture_epoch
        self._last_sequence = frame.sequence

        events.extend(await self.advance(frame.monotonic_offset_ms))
        if self._turn is None:
            self._remember_pre_roll(frame)
            if frame.speech:
                events.append(self._open(frame))
            return events

        turn = self._turn
        if turn.soft_deadline_ms is not None:
            if frame.speech:
                turn.revision += 1
                turn.soft_deadline_ms = None
                turn.silence_ms = 0
                turn.frames.append(frame)
                events.append(self._event("reopened", turn, reason="speech_resumed"))
            else:
                turn.frames.append(frame)
            return events

        turn.frames.append(frame)
        if frame.speech:
            turn.silence_ms = 0
        else:
            turn.silence_ms += frame.duration_ms

        elapsed_ms = frame.end_ms - turn.opened_at_ms
        if elapsed_ms >= MAX_OPEN_TURN_MS:
            events.append(self._event("committed", turn, reason="max_duration"))
            self._turn = None
            self._pre_roll.clear()
            return events

        if turn.silence_ms >= ENDPOINT_SILENCE_MS:
            grace_ms: int | None = None
            reason = ""
            if semantic_complete is True:
                grace_ms = self.policy.complete_grace_ms
                reason = "smart_turn_complete"
            elif turn.silence_ms >= self.policy.incomplete_grace_ms:
                grace_ms = 0
                reason = "incomplete_grace_elapsed"
            if grace_ms is not None:
                if turn.first_soft_end_ms is None:
                    turn.first_soft_end_ms = frame.end_ms
                reopen_cap = turn.first_soft_end_ms + MAX_REOPEN_WINDOW_MS
                turn.soft_deadline_ms = min(frame.end_ms + grace_ms, reopen_cap)
                events.append(
                    self._event(
                        "soft_ended",
                        turn,
                        reason=reason,
                        deadline_ms=turn.soft_deadline_ms,
                    )
                )
        return events

    async def advance(self, monotonic_offset_ms: float) -> list[TurnEvent]:
        turn = self._turn
        if (
            turn is None
            or turn.soft_deadline_ms is None
            or monotonic_offset_ms < turn.soft_deadline_ms
        ):
            return []
        event = self._event("committed", turn, reason="endpoint_grace_elapsed")
        self._turn = None
        self._pre_roll.clear()
        return [event]

    def cancel(self, reason: str) -> list[TurnEvent]:
        event = self._cancel(reason)
        self._pre_roll.clear()
        return [event] if event else []
