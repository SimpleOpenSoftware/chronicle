"""Wake activation stage timings over immutable facts.

These wall-clock/ACK intervals are diagnostic, not acoustic end-to-end latency.
The device-clock speaking/waiting/playback report lives in services.voice_latency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .interaction_ledger import WakeInteractionFact

_REQUIRED_STAGES = (
    "armed",
    "end_of_turn",
    "command_resolved",
    "dispatched",
    "response_queued",
    "response_ready",
    "response_offered",
    "response_playing",
    "response_done",
)


@dataclass(frozen=True)
class WakeLatencyReport:
    wake_trace_id: str
    status: str
    observed_stages: tuple[str, ...]
    missing_stages: tuple[str, ...]
    metrics_ms: Mapping[str, float]
    plugins: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _elapsed_ms(
    stages: Mapping[str, WakeInteractionFact], start: str, end: str
) -> float | None:
    if start not in stages or end not in stages:
        return None
    elapsed = stages[end].occurred_at - stages[start].occurred_at
    return round(elapsed.total_seconds() * 1000.0, 3)


def build_wake_latency_report(
    facts: Iterable[WakeInteractionFact],
) -> WakeLatencyReport:
    """Reduce one trace into timings owned by explicit producer interfaces."""
    ordered = sorted(facts, key=lambda fact: (fact.occurred_at, fact.ordinal))
    if not ordered:
        raise ValueError("wake latency report requires at least one fact")
    trace_ids = {fact.wake_trace_id for fact in ordered}
    if len(trace_ids) != 1:
        raise ValueError("wake latency report facts must belong to one trace")
    stages = {fact.stage: fact for fact in ordered}
    missing = tuple(stage for stage in _REQUIRED_STAGES if stage not in stages)

    metrics: dict[str, float] = {}
    spans = (
        ("wake_capture", "armed", "end_of_turn"),
        ("turn_commit", "end_of_turn", "command_resolved"),
        ("response_queue", "dispatched", "response_queued"),
        ("tts", "response_queued", "response_ready"),
        ("offer_enqueue", "response_ready", "response_offered"),
        ("device_start", "response_offered", "response_playing"),
        ("playback", "response_playing", "response_done"),
        ("end_of_turn_to_playing", "end_of_turn", "response_playing"),
        ("arm_to_playing", "armed", "response_playing"),
        ("arm_to_done", "armed", "response_done"),
    )
    for name, start, end in spans:
        elapsed = _elapsed_ms(stages, start, end)
        if elapsed is not None:
            metrics[name] = elapsed

    dispatched = stages.get("dispatched")
    plugins: tuple[Mapping[str, Any], ...] = ()
    if dispatched is not None:
        dispatch_ms = dispatched.payload.get("dispatch_ms")
        if dispatch_ms is not None:
            metrics["plugin_dispatch"] = round(float(dispatch_ms), 3)
        plugins = tuple(dict(item) for item in dispatched.payload.get("plugins", ()))

    # Keep output stable in causal order, including plugin timing at the dispatch seam.
    metric_order = (
        "wake_capture",
        "turn_commit",
        "plugin_dispatch",
        "response_queue",
        "tts",
        "offer_enqueue",
        "device_start",
        "playback",
        "end_of_turn_to_playing",
        "arm_to_playing",
        "arm_to_done",
    )
    metrics = {name: metrics[name] for name in metric_order if name in metrics}
    return WakeLatencyReport(
        wake_trace_id=ordered[0].wake_trace_id,
        status="complete" if not missing else "incomplete",
        observed_stages=tuple(fact.stage for fact in ordered),
        missing_stages=missing,
        metrics_ms=metrics,
        plugins=plugins,
    )
