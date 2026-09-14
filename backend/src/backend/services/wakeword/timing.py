"""Per-command latency tracing for the voice-command path.

One :class:`WakeTimer` is created per executed voice command (acoustic wake or
follow-up) inside :func:`execute_voice_command`. It accumulates the duration of
each pipeline stage and, when the command finishes, emits a single structured
log line so the end-to-end latency — and where it went — is visible at a glance::

    ⏱️ wake-latency session=abc123 src=wake status=transcribed cmd='turn off the hall lights'
       | capture=2.10s asr=480ms route=[ha:210ms miss, hermes:1830ms ok] tts=540ms
         reply→device@+2.92s total≈5.50s est_play≈2.0s

What each field measures:

- ``capture``  arm → end-of-turn, owned and reported by the wakeword-service.
- ``asr``      batch command transcription (the dispatcher times this).
- ``route``    each plugin in the chain-of-responsibility, in order, with its
               wall time and outcome (``miss`` = declined / passed down the
               chain, ``ok`` = handled, ``fail`` = ran but reported failure).
- ``tts``      speech synthesis of the reply.
- ``reply→device`` offset, from the start of dispatch, at which the reply audio
               was published to the device downlink.
- ``total``    best-effort end-to-end ≈ capture + asr + (dispatch → downlink).
- ``est_play`` estimated playback length used by legacy log-only callers. Voice
               protocol v1 emits durable offered/started/done acknowledgements;
               :mod:`services.voice_latency` owns the device-clock interaction report.

All durations use a monotonic clock; ``capture``/``asr`` are passed in from the
dispatcher (which runs before this timer is created).
"""

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PluginSpan:
    """Timing + outcome of a single plugin in the dispatch chain."""

    plugin_id: str
    duration_ms: float
    handled: bool  # ran and stopped the chain (should_continue=False)
    success: bool

    def render(self) -> str:
        if self.handled:
            outcome = "ok" if self.success else "handled-fail"
        else:
            # Declined / passed the command down the chain (e.g. HA "miss").
            outcome = "miss"
        return f"{self.plugin_id}:{self.duration_ms:.0f}ms {outcome}"


class WakeTimer:
    """Accumulates stage durations for one voice command and logs a summary.

    The instance doubles as a lightweight observer for
    :meth:`PluginRouter.dispatch_event` via :meth:`on_plugin_done`.
    """

    def __init__(
        self,
        *,
        session_id: str,
        source: str,
        asr_status: str,
        command: str = "",
        capture_secs: Optional[float] = None,
        asr_ms: Optional[float] = None,
    ):
        self._t0 = time.perf_counter()
        self.session_id = session_id
        self.source = source
        self.asr_status = asr_status
        self.command = command
        self.capture_secs = capture_secs
        self.asr_ms = asr_ms

        self.plugin_spans: List[PluginSpan] = []
        self.dispatch_ms: Optional[float] = None
        self.tts_ms: Optional[float] = None
        self.downlink_at_ms: Optional[float] = None
        self.est_play_secs: Optional[float] = None

    def elapsed_ms(self) -> float:
        """Milliseconds since this timer was created (≈ start of dispatch)."""
        return (time.perf_counter() - self._t0) * 1000.0

    def record_plugin(self, plugin_id: str, duration_ms: float, result) -> None:
        """Record the timing + outcome of one plugin in the dispatch chain.

        ``result`` is the plugin's :class:`PluginResult` or ``None`` (a decline).
        """
        handled = bool(result is not None and not result.should_continue)
        success = bool(result is not None and getattr(result, "success", False))
        self.plugin_spans.append(
            PluginSpan(
                plugin_id=plugin_id,
                duration_ms=duration_ms,
                handled=handled,
                success=success,
            )
        )

    def mark_downlink(self) -> None:
        """Record the moment the reply audio was sent to the device."""
        self.downlink_at_ms = self.elapsed_ms()

    def log(self) -> None:
        """Emit the single structured latency line for this command."""
        parts: List[str] = []
        if self.capture_secs is not None:
            parts.append(f"capture={self.capture_secs:.2f}s")
        if self.asr_ms is not None:
            parts.append(f"asr={self.asr_ms:.0f}ms")
        if self.plugin_spans:
            route = ", ".join(span.render() for span in self.plugin_spans)
            parts.append(f"route=[{route}]")
        elif self.dispatch_ms is not None:
            parts.append(f"dispatch={self.dispatch_ms:.0f}ms")
        if self.tts_ms is not None:
            parts.append(f"tts={self.tts_ms:.0f}ms")
        if self.downlink_at_ms is not None:
            parts.append(f"reply→device@+{self.downlink_at_ms / 1000.0:.2f}s")

        # Best-effort end-to-end: stages that ran before this timer (capture, asr)
        # plus everything up to the downlink (or dispatch end if nothing spoken).
        total_ms = 0.0
        if self.capture_secs is not None:
            total_ms += self.capture_secs * 1000.0
        if self.asr_ms is not None:
            total_ms += self.asr_ms
        total_ms += (
            self.downlink_at_ms
            if self.downlink_at_ms is not None
            else (self.dispatch_ms or self.elapsed_ms())
        )
        parts.append(f"total≈{total_ms / 1000.0:.2f}s")

        if self.est_play_secs is not None:
            # Estimate only; audio-v2 traces use physical playback ACKs instead.
            parts.append(f"est_play≈{self.est_play_secs:.1f}s")

        cmd = (self.command or "")[:60]
        logger.info(
            "⏱️ wake-latency session=%s src=%s status=%s cmd=%r | %s",
            self.session_id,
            self.source,
            self.asr_status,
            cmd,
            " ".join(parts),
        )
