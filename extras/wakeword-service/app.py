"""Acoustic wake-word service (multi-wake-word).

Standalone service that consumes the live ``audio:stream:*`` Redis stream,
detects one or more acoustic wake words (e.g. "hey hermes" + "hermes") in
parallel, captures the following command turn via Silero VAD + Smart Turn v3,
and publishes a ``wake_word.detected`` event (tagged with which word fired) to
the ``wakeword:detections`` Redis stream for the backend to forward to the
Hermes plugin.

Wake words are configured via ``WAKEWORD_MODELS`` as a comma-separated list of
``name:file`` pairs (file relative to the models dir), e.g.::

    WAKEWORD_MODELS=hey_hermes:hey_hermes_c.onnx,hermes:hermes.onnx

The wake-word NAME (``hey_hermes``) is decoupled from the model FILE
(``hey_hermes_c.onnx``) so a model can be swapped/retrained without renaming its
sample-data directory or its plugin condition. List order is arming PRIORITY when
several words fire on one frame (put the lower-FP word first). Each word may have
a per-deployment second-stage verifier at ``models/<name>_verifier.npz`` (auto-
enabled when present) and per-word threshold/patience overrides.

Runs a small FastAPI app for health/status + the data-collection flywheel API.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from active_turn_consumer import ActiveTurnConsumer
from consumer import DETECTIONS_STREAM, GROUP_NAME, STREAM_PATTERN, WakeWordConsumer
from detector import HermesDetector
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from loop_monitor import LoopMonitor
from pydantic import BaseModel
from samples import BUCKETS, PENDING, SampleStore

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wakeword-service")

# Directory holding the wake-word ONNX models (+ optional verifiers).
MODELS_DIR = os.getenv("WAKEWORD_MODELS_DIR", "/app/models")
# Wake words to run, as "name:file" pairs (file relative to MODELS_DIR). Order is
# arming priority. The lower-FP two-word phrase goes first.
WAKEWORD_MODELS = os.getenv("WAKEWORD_MODELS", "hey_hermes:hey_hermes_c.onnx")
# Pre-multi-wake-word flat sample dirs (data/samples/<bucket>) are all "hey
# hermes" — relocate them under this word so they don't contaminate a second one.
LEGACY_WAKEWORD = os.getenv("WAKEWORD_LEGACY", "hey_hermes")
# Words in collect-only (shadow) mode fire to farm false-positive review data but
# never dispatch a command / play a tone / block a real wake word. Use this to run
# a not-yet-trusted word (e.g. the FP-prone single "hermes") live for data only.
WAKEWORD_COLLECT_ONLY = os.getenv("WAKEWORD_COLLECT_ONLY", "")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# Default operating point (per-word overrides via WAKEWORD_THRESHOLDS / _PATIENCES).
THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.9"))
# patience=2 (consecutive frames) is the shippable operating point for "hey
# hermes": 0.9/2 -> 5.4 FP/hr at 95.6% recall on held-out noise.
PATIENCE = int(os.getenv("WAKEWORD_PATIENCE", "2"))
# Per-word overrides, e.g. WAKEWORD_THRESHOLDS=hermes:0.95 (single short words are
# FP-prone, so they typically need a higher threshold/patience than a phrase).
WAKEWORD_THRESHOLDS = os.getenv("WAKEWORD_THRESHOLDS", "")
WAKEWORD_PATIENCES = os.getenv("WAKEWORD_PATIENCES", "")
# Explicit per-word verifier overrides, e.g. hermes:/app/models/hermes_verifier.npz.
WAKEWORD_VERIFIERS = os.getenv("WAKEWORD_VERIFIERS", "")
_vt = os.getenv("WAKEWORD_VERIFIER_THRESHOLD", "")
VERIFIER_THRESHOLD = float(_vt) if _vt else None

DEBOUNCE_SECS = float(os.getenv("WAKEWORD_DEBOUNCE_SECS", "3.0"))
VAD_THRESHOLD = float(os.getenv("WAKEWORD_VAD_THRESHOLD", "0.5"))
# End-of-turn is decided by the Smart Turn MODEL at each speech->silence pause
# (eot_min_silence -> first query, eot_recheck -> re-query cadence). stop_secs is
# now only a LONG backstop for when the model never fires (pure trailing silence).
STOP_SECS = float(os.getenv("WAKEWORD_STOP_SECS", "6.0"))
EOT_MIN_SILENCE_SECS = float(os.getenv("WAKEWORD_EOT_MIN_SILENCE_SECS", "0.2"))
EOT_RECHECK_SECS = float(os.getenv("WAKEWORD_EOT_RECHECK_SECS", "0.3"))
MAX_ARM_SECS = float(os.getenv("WAKEWORD_MAX_ARM_SECS", "15.0"))
# Minimum spoken (VAD) speech a captured command must contain to be sent for
# batch ASR. Below this the capture is a near-silent false arm; the backend skips
# transcription so self-diarizing ASR can't hallucinate a phantom command.
MIN_COMMAND_SPEECH_SECS = float(os.getenv("WAKEWORD_MIN_COMMAND_SPEECH_SECS", "0.3"))
SERVICE_PORT = int(os.getenv("WAKEWORD_SERVICE_PORT", "8770"))
EVENT_LOOP_STALL_SECONDS = float(os.getenv("EVENT_LOOP_STALL_SECONDS", "1.0"))
EVENT_LOOP_HEALTH_LAG_MS = float(os.getenv("EVENT_LOOP_HEALTH_LAG_MS", "100"))
# Root for the data-collection clip store (mounted volume in docker-compose).
DATA_DIR = os.getenv("WAKEWORD_DATA_DIR", "/app/data/samples")
# "Prime + say it" capture tuning (data collection). Hard upper bound of 10 s
# on the whole priming session; the utterance itself caps at PRIME_MAX_SECS and
# normally ends a beat after a trailing-silence gap (so capture lands in ~5 s).
PRIME_TIMEOUT_SECS = float(os.getenv("WAKEWORD_PRIME_TIMEOUT_SECS", "10.0"))
PRIME_TRAIL_SILENCE_SECS = float(os.getenv("WAKEWORD_PRIME_TRAIL_SILENCE_SECS", "0.6"))
PRIME_MAX_SECS = float(os.getenv("WAKEWORD_PRIME_MAX_SECS", "4.0"))
# VAD gate while priming — dropped well below the live VAD threshold so the
# known-incoming utterance is auto-caught even when spoken softly.
PRIME_VAD_THRESHOLD = float(os.getenv("WAKEWORD_PRIME_VAD_THRESHOLD", "0.3"))
# Optional explicit ONNX paths (vendored into models/). Empty -> pipecat bundle.
SMART_TURN_MODEL_PATH = os.getenv("SMART_TURN_MODEL_PATH", "") or None
SILERO_VAD_MODEL_PATH = os.getenv("SILERO_VAD_MODEL_PATH", "") or None


def _parse_models(spec: str) -> dict[str, str]:
    """Parse ``WAKEWORD_MODELS`` into an ordered ``{name: abs_model_path}``."""
    models: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, file = (p.strip() for p in item.split(":", 1))
        else:
            file = item
            name = os.path.splitext(os.path.basename(file))[0]
        path = file if os.path.isabs(file) else os.path.join(MODELS_DIR, file)
        models[name] = path
    return models


def _parse_kv(spec: str, cast) -> dict:
    """Parse a ``name:value,name:value`` spec into a typed dict."""
    out: dict = {}
    for item in spec.split(","):
        item = item.strip()
        if ":" in item:
            k, v = (p.strip() for p in item.split(":", 1))
            out[k] = cast(v)
    return out


MODELS = _parse_models(WAKEWORD_MODELS)
WAKEWORDS = list(MODELS.keys())
THRESHOLDS = _parse_kv(WAKEWORD_THRESHOLDS, float)
PATIENCES = _parse_kv(WAKEWORD_PATIENCES, int)
COLLECT_ONLY = [w.strip() for w in WAKEWORD_COLLECT_ONLY.split(",") if w.strip()]

# Runtime collect-only overrides set from the Wake-Word Lab UI are persisted here
# (in the mounted ./data volume, so they survive restarts and work even when the
# backend runs on a different machine and can't reach the compose .env). Once this
# file exists it is authoritative over WAKEWORD_COLLECT_ONLY, which is then just the
# initial default for a fresh deployment.
COLLECT_ONLY_STATE_PATH = os.path.join(
    os.path.dirname(DATA_DIR.rstrip("/")), "collect_only.json"
)


def _initial_collect_only() -> list[str]:
    """Collect-only words at startup: persisted UI override if present, else the
    env default. Filtered to currently-configured words (a word dropped from
    WAKEWORD_MODELS is silently ignored)."""
    words = COLLECT_ONLY
    if os.path.exists(COLLECT_ONLY_STATE_PATH):
        try:
            with open(COLLECT_ONLY_STATE_PATH) as fh:
                data = json.load(fh)
            if isinstance(data, list):
                words = [str(w) for w in data]
        except (OSError, ValueError) as e:
            logger.warning(
                f"could not read collect-only state {COLLECT_ONLY_STATE_PATH}: {e} "
                f"— falling back to WAKEWORD_COLLECT_ONLY"
            )
    return [w for w in words if w in MODELS]


def _save_collect_only(words) -> None:
    """Persist the current collect-only set so it survives a restart."""
    with open(COLLECT_ONLY_STATE_PATH, "w") as fh:
        json.dump(sorted(words), fh)


# Runtime per-word OFF overrides from the UI. Disabled words neither dispatch
# nor emit collect-only shadow events. Persisted in the same mounted data dir.
DISABLED_STATE_PATH = os.path.join(
    os.path.dirname(DATA_DIR.rstrip("/")), "disabled.json"
)


def _initial_disabled() -> list[str]:
    """Words disabled at startup from persisted state (or none)."""
    if os.path.exists(DISABLED_STATE_PATH):
        try:
            with open(DISABLED_STATE_PATH) as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [str(w) for w in data if w in MODELS]
        except (OSError, ValueError) as e:
            logger.warning(
                f"could not read disabled state {DISABLED_STATE_PATH}: {e} "
                f"— defaulting to all enabled"
            )
    return []


def _save_disabled(words) -> None:
    """Persist the current disabled set so it survives a restart."""
    with open(DISABLED_STATE_PATH, "w") as fh:
        json.dump(sorted(words), fh)


# Runtime verifier on/off overrides from the Lab UI, persisted alongside the
# collect-only state (same ./data volume, same restart/cross-machine semantics).
# Stores the set of words whose verifier is toggled OFF; a word absent here keeps
# its verifier ON (when one is loaded). Authoritative over defaults once written.
VERIFIER_DISABLED_STATE_PATH = os.path.join(
    os.path.dirname(DATA_DIR.rstrip("/")), "verifier_disabled.json"
)


def _initial_verifier_disabled() -> list[str]:
    """Words with their verifier disabled at startup: the persisted UI override if
    present (filtered to currently-configured words), else none."""
    if os.path.exists(VERIFIER_DISABLED_STATE_PATH):
        try:
            with open(VERIFIER_DISABLED_STATE_PATH) as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [str(w) for w in data if w in MODELS]
        except (OSError, ValueError) as e:
            logger.warning(
                f"could not read verifier-disabled state "
                f"{VERIFIER_DISABLED_STATE_PATH}: {e} — defaulting to all enabled"
            )
    return []


def _save_verifier_disabled(words) -> None:
    """Persist the current verifier-disabled set so it survives a restart."""
    with open(VERIFIER_DISABLED_STATE_PATH, "w") as fh:
        json.dump(sorted(words), fh)


def _resolve_verifiers() -> dict[str, str]:
    """Per-word verifier paths: explicit override, else ``<name>_verifier.npz`` if
    it exists. Words with no verifier file (and no override) get no entry."""
    overrides = _parse_kv(WAKEWORD_VERIFIERS, str)
    out: dict[str, str] = {}
    for name in WAKEWORDS:
        if name in overrides:
            out[name] = overrides[name]
            continue
        default = os.path.join(MODELS_DIR, f"{name}_verifier.npz")
        if os.path.exists(default):
            out[name] = default
    return out


VERIFIERS = _resolve_verifiers()


def _validate_models() -> None:
    """Refuse to start unless every configured wake model file exists on disk."""
    available = (
        sorted(f for f in os.listdir(MODELS_DIR) if f.endswith((".onnx", ".pt")))
        if os.path.isdir(MODELS_DIR)
        else []
    )
    required = dict(MODELS)
    if SMART_TURN_MODEL_PATH:
        required["smart-turn"] = SMART_TURN_MODEL_PATH
    if SILERO_VAD_MODEL_PATH:
        required["silero-vad"] = SILERO_VAD_MODEL_PATH
    missing = {name: p for name, p in required.items() if not os.path.exists(p)}
    if missing:
        details = "; ".join(f"{name} '{p}'" for name, p in missing.items())
        raise FileNotFoundError(
            f"Configured model(s) not found on disk: {details}. "
            f"Available .onnx in {MODELS_DIR}: {available or '(none)'}. "
            f"Train/vendor the model and set WAKEWORD_MODELS accordingly."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the detector and start the background consumer."""
    if not MODELS:
        raise RuntimeError("WAKEWORD_MODELS is empty — configure at least one word")
    _validate_models()

    detector = HermesDetector(
        models=MODELS,
        threshold=THRESHOLD,
        patience=PATIENCE,
        thresholds=THRESHOLDS,
        patiences=PATIENCES,
        debounce_secs=DEBOUNCE_SECS,
        vad_threshold=VAD_THRESHOLD,
        stop_secs=STOP_SECS,
        max_arm_secs=MAX_ARM_SECS,
        eot_min_silence_secs=EOT_MIN_SILENCE_SECS,
        eot_recheck_secs=EOT_RECHECK_SECS,
        smart_turn_model_path=SMART_TURN_MODEL_PATH,
        silero_vad_model_path=SILERO_VAD_MODEL_PATH,
        prime_timeout_secs=PRIME_TIMEOUT_SECS,
        prime_trail_silence_secs=PRIME_TRAIL_SILENCE_SECS,
        prime_max_secs=PRIME_MAX_SECS,
        prime_vad_threshold=PRIME_VAD_THRESHOLD,
        min_command_speech_secs=MIN_COMMAND_SPEECH_SECS,
        verifiers=VERIFIERS,
        verifier_threshold=VERIFIER_THRESHOLD,
        collect_only=_initial_collect_only(),
        verifiers_disabled=_initial_verifier_disabled(),
        disabled=_initial_disabled(),
    )
    app.state.detector = detector
    store = SampleStore(DATA_DIR, WAKEWORDS, legacy_wakeword=LEGACY_WAKEWORD)
    app.state.store = store
    consumer = WakeWordConsumer(
        detector=detector, redis_url=REDIS_URL, sample_store=store
    )
    app.state.consumer = consumer
    consumer_task = asyncio.create_task(consumer.start())
    app.state.consumer_task = consumer_task
    active_turn_consumer = ActiveTurnConsumer(
        redis_url=REDIS_URL,
        smart_turn_model_path=SMART_TURN_MODEL_PATH,
        silero_vad_model_path=SILERO_VAD_MODEL_PATH,
        vad_threshold=VAD_THRESHOLD,
    )
    app.state.active_turn_consumer = active_turn_consumer
    active_turn_task = asyncio.create_task(active_turn_consumer.start())
    app.state.active_turn_task = active_turn_task
    loop_monitor = LoopMonitor(
        "wakeword-service",
        redis_url=REDIS_URL,
        stall_seconds=EVENT_LOOP_STALL_SECONDS,
    )
    app.state.loop_monitor = loop_monitor
    loop_monitor_task = asyncio.create_task(loop_monitor.run())
    app.state.loop_monitor_task = loop_monitor_task
    logger.info(
        f"Wake-word service ready (words={', '.join(WAKEWORDS)}, group={GROUP_NAME})"
    )

    try:
        yield
    finally:
        await consumer.stop()
        await active_turn_consumer.stop()
        consumer_task.cancel()
        active_turn_task.cancel()
        loop_monitor_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        try:
            await active_turn_task
        except asyncio.CancelledError:
            pass
        try:
            await loop_monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Hermes Wake-Word Service", lifespan=lifespan)


def _collect_only_now() -> set:
    """The live collect-only set (the detector's, once started) so summaries
    reflect runtime UI toggles, not just the boot-time env/state."""
    detector = getattr(app.state, "detector", None)
    return set(detector.collect_only) if detector is not None else set(COLLECT_ONLY)


def _verifier_disabled_now() -> set:
    """The live set of words whose verifier is toggled OFF (the detector's, once
    started), so summaries reflect runtime UI toggles."""
    detector = getattr(app.state, "detector", None)
    return set(detector.verifiers_disabled) if detector is not None else set()


def _disabled_now() -> set:
    """The live set of disabled wake words (runtime UI toggles)."""
    detector = getattr(app.state, "detector", None)
    return set(detector.disabled) if detector is not None else set()


def _wakeword_summaries() -> list[dict]:
    """Per-word config summary for the dashboard."""
    collect_only = _collect_only_now()
    verifier_disabled = _verifier_disabled_now()
    disabled = _disabled_now()
    out = []
    for name in WAKEWORDS:
        has_verifier = name in VERIFIERS and os.path.exists(VERIFIERS[name])
        out.append(
            {
                "name": name,
                "model": os.path.basename(MODELS[name]),
                # ``verifier`` = a verifier file is loaded for this word (capability);
                # ``verifier_enabled`` = it is currently consulted (the UI toggle).
                "verifier": has_verifier,
                "verifier_enabled": has_verifier and name not in verifier_disabled,
                "threshold": THRESHOLDS.get(name, THRESHOLD),
                "patience": PATIENCES.get(name, PATIENCE),
                "collect_only": name in collect_only,
                "disabled": name in disabled,
            }
        )
    return out


@app.get("/health")
async def health(response: Response):
    """Liveness, scheduling delay, and real-time consumer progress.

    The consumer runs as a background task. If it dies (e.g. an unrecoverable
    error in the discovery loop), uvicorn keeps serving requests — so reporting
    "ok" purely on the HTTP server being up would mask a service that no longer
    consumes any audio. Reflect the real consumer state and return 503 when it
    is not alive, so orchestrators / the dashboard see the failure.
    """
    consumer: WakeWordConsumer | None = getattr(app.state, "consumer", None)
    task: asyncio.Task | None = getattr(app.state, "consumer_task", None)
    consumer_alive = bool(
        consumer is not None
        and consumer.running
        and task is not None
        and not task.done()
    )
    active_turn_consumer: ActiveTurnConsumer | None = getattr(
        app.state, "active_turn_consumer", None
    )
    active_turn_task: asyncio.Task | None = getattr(app.state, "active_turn_task", None)
    active_turn_alive = bool(
        active_turn_consumer is not None
        and active_turn_consumer.running
        and active_turn_task is not None
        and not active_turn_task.done()
    )
    wake_consumer_health = consumer.health() if consumer is not None else None
    active_turn_health = (
        active_turn_consumer.health() if active_turn_consumer is not None else None
    )
    loop_monitor: LoopMonitor | None = getattr(app.state, "loop_monitor", None)
    loop_monitor_task: asyncio.Task | None = getattr(
        app.state, "loop_monitor_task", None
    )
    loop_monitor_alive = bool(
        loop_monitor is not None
        and loop_monitor_task is not None
        and not loop_monitor_task.done()
    )
    loop_health = loop_monitor.stats() if loop_monitor is not None else None
    loop_p95_ms = loop_health.get("lag_p95_ms") if loop_health else None
    loop_responsive = bool(
        loop_monitor_alive
        and (loop_p95_ms is None or loop_p95_ms <= EVENT_LOOP_HEALTH_LAG_MS)
    )
    healthy = bool(
        consumer_alive
        and wake_consumer_health
        and wake_consumer_health.get("healthy", False)
        and active_turn_alive
        and active_turn_health
        and active_turn_health.get("healthy", False)
        and loop_responsive
    )
    status_str = "ok" if healthy else "unhealthy"
    if status_str != "ok":
        response.status_code = 503
    return {
        "status": status_str,
        "consumer_alive": consumer_alive,
        "wake_consumer": wake_consumer_health,
        "active_turn_consumer_alive": active_turn_alive,
        "active_turn_consumer": active_turn_health,
        "loop_monitor_alive": loop_monitor_alive,
        "loop_responsive": loop_responsive,
        "loop_health_lag_threshold_ms": EVENT_LOOP_HEALTH_LAG_MS,
        "event_loop": loop_health,
        "wakewords": _wakeword_summaries(),
        "redis_url": REDIS_URL,
        "consumer_group": GROUP_NAME,
        "stream_pattern": STREAM_PATTERN,
        "detections_stream": DETECTIONS_STREAM,
    }


@app.get("/status")
async def status():
    """Active stream count from the running consumer."""
    consumer: WakeWordConsumer = app.state.consumer
    active = sum(1 for t in consumer._stream_tasks.values() if not t.done())
    return {
        "running": consumer.running,
        "active_streams": active,
        "known_streams": list(consumer._stream_tasks.keys()),
    }


@app.get("/models")
async def models():
    """Configured wake words (for the dashboard picker + the Wake-Word Lab).

    ``available``/``active`` are kept for the existing acoustic-condition picker
    (now the list of wake-word NAMES); ``wakewords`` carries the richer per-word
    config the Lab uses to render one section per word.
    """
    return {
        "available": WAKEWORDS,
        "active": WAKEWORDS[0] if WAKEWORDS else None,
        "wakewords": _wakeword_summaries(),
    }


# --------------------------------------------------------------------------- #
# Data-collection API (the training flywheel): prime positive capture, review
# captured clips, label them true/false positive — all scoped per wake word.
# Consumed by the backend proxy at /api/wakeword/* — see backend
# .../routers/modules/wakeword_routes.py
# --------------------------------------------------------------------------- #


class PrimeRequest(BaseModel):
    client_id: str
    wakeword: str


class UnprimeRequest(BaseModel):
    client_id: str


class ProbeRequest(BaseModel):
    client_id: str
    audio_session_id: str
    wakeword: str
    timeout_seconds: float = 15


class LabelRequest(BaseModel):
    label: str  # "wake" -> positive, "not_wake" -> negative


class CollectOnlyRequest(BaseModel):
    wakeword: str
    collect_only: bool  # True -> shadow (farm-only), False -> normal dispatch word


class VerifierEnabledRequest(BaseModel):
    wakeword: str
    enabled: bool  # True -> consult the second-stage verifier, False -> stage-1 only


class DisabledRequest(BaseModel):
    wakeword: str
    disabled: bool  # True -> fully off for this word; False -> enabled


def _require_wakeword(wakeword: str) -> None:
    if wakeword not in MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown wake word '{wakeword}' (have {WAKEWORDS})",
        )


@app.post("/collect_only")
async def set_collect_only(req: CollectOnlyRequest):
    """Toggle a wake word between normal dispatch and collect-only (shadow) mode.

    Collect-only words fire live to farm false-positive review data but never
    dispatch a command / play a tone / block a real wake word. Mutates the running
    detector's live set (effective on the next frame) and persists the choice so it
    survives a restart. Returns the refreshed per-word summaries.
    """
    _require_wakeword(req.wakeword)
    detector: HermesDetector = app.state.detector
    if req.collect_only:
        detector.collect_only.add(req.wakeword)
    else:
        detector.collect_only.discard(req.wakeword)
    _save_collect_only(detector.collect_only)
    logger.info(
        f"collect-only {'ON' if req.collect_only else 'OFF'} for '{req.wakeword}' "
        f"(now: {sorted(detector.collect_only) or '(none)'})"
    )
    return {"wakewords": _wakeword_summaries()}


@app.post("/verifier_enabled")
async def set_verifier_enabled(req: VerifierEnabledRequest):
    """Toggle a wake word's second-stage verifier on/off at runtime.

    The verifier stays LOADED either way — disabling only skips its check so arms
    dispatch on the stage-1 acoustic model alone. Useful to A/B the verifier's
    false-positive suppression, or to fall back to stage-1 if a verifier regresses.
    Mutates the running detector (effective next frame) and persists the choice.
    Has no effect for a word with no verifier loaded. Returns refreshed summaries.
    """
    _require_wakeword(req.wakeword)
    detector: HermesDetector = app.state.detector
    if req.enabled:
        detector.verifiers_disabled.discard(req.wakeword)
    else:
        detector.verifiers_disabled.add(req.wakeword)
    _save_verifier_disabled(detector.verifiers_disabled)
    logger.info(
        f"verifier {'ON' if req.enabled else 'OFF'} for '{req.wakeword}' "
        f"(disabled now: {sorted(detector.verifiers_disabled) or '(none)'})"
    )
    return {"wakewords": _wakeword_summaries()}


@app.post("/disabled")
async def set_disabled(req: DisabledRequest):
    """Toggle a wake word fully off/on at runtime.

    Disabled words neither dispatch commands nor emit collect-only shadow events.
    Effective immediately and persisted across restarts.
    """
    _require_wakeword(req.wakeword)
    detector: HermesDetector = app.state.detector
    if req.disabled:
        detector.disabled.add(req.wakeword)
    else:
        detector.disabled.discard(req.wakeword)
    _save_disabled(detector.disabled)
    logger.info(
        f"disabled {'ON' if req.disabled else 'OFF'} for '{req.wakeword}' "
        f"(disabled now: {sorted(detector.disabled) or '(none)'})"
    )
    return {"wakewords": _wakeword_summaries()}


@app.get("/streams")
async def streams():
    """Active audio streams the UI can prime for a positive capture."""
    consumer: WakeWordConsumer = app.state.consumer
    return {"streams": consumer.active_clients()}


@app.post("/prime")
async def prime(req: PrimeRequest):
    """Arm a one-shot positive capture of ``wakeword`` on a streaming client.

    The next utterance on that stream is saved as a labeled positive for that
    word regardless of model score (the false-negative / hard-positive path).
    """
    _require_wakeword(req.wakeword)
    consumer: WakeWordConsumer = app.state.consumer
    if not consumer.prime(req.client_id, req.wakeword):
        raise HTTPException(
            status_code=404,
            detail=f"No active stream '{req.client_id}' to prime",
        )
    return {"client_id": req.client_id, "wakeword": req.wakeword, "primed": True}


@app.post("/unprime")
async def unprime(req: UnprimeRequest):
    """Manually end an in-progress prime capture (the UI 'stop' button).

    Finalizes whatever was heard so far and saves it for review — the capture is
    never silently dropped.
    """
    consumer: WakeWordConsumer = app.state.consumer
    if not consumer.unprime(req.client_id):
        raise HTTPException(
            status_code=404,
            detail=f"No priming stream '{req.client_id}' to stop",
        )
    return {"client_id": req.client_id, "unprimed": True}


@app.post("/probes", status_code=201)
async def start_probe(req: ProbeRequest):
    """Run production wake detection for one stream with every effect suppressed."""
    consumer: WakeWordConsumer = app.state.consumer
    try:
        return consumer.start_probe(
            req.client_id,
            req.audio_session_id,
            req.wakeword,
            timeout_seconds=req.timeout_seconds,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "already active" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/probes/{probe_id}")
async def get_probe(probe_id: str):
    consumer: WakeWordConsumer = app.state.consumer
    try:
        return consumer.get_probe(probe_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/probes/{probe_id}")
async def cancel_probe(probe_id: str):
    consumer: WakeWordConsumer = app.state.consumer
    try:
        return consumer.cancel_probe(probe_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/samples")
async def list_samples(wakeword: str = Query(...), bucket: str = Query("pending")):
    """List captured clips in a wake word's bucket (pending/positive/negative)."""
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    return {
        "wakeword": wakeword,
        "bucket": bucket,
        "samples": store.list(wakeword, bucket),
    }


@app.get("/samples/stats")
async def sample_stats():
    """Per-wake-word, per-bucket clip counts for the data-collection dashboard."""
    store: SampleStore = app.state.store
    return store.stats()


@app.post("/samples/dedupe")
async def dedupe_samples(wakeword: str = Query(...)):
    """Remove exact-duplicate clips within a wake word (across all buckets).

    Keeps one representative per identical-audio group (a labeled clip over a
    pending one), deleting the rest — including duplicate pending clips.
    """
    _require_wakeword(wakeword)
    store: SampleStore = app.state.store
    return store.dedupe(wakeword)


@app.get("/samples/{clip_id}/audio")
async def sample_audio(clip_id: str):
    """Return a clip's WAV bytes for in-browser playback."""
    store: SampleStore = app.state.store
    path = store.wav_path(clip_id)
    if path is None:
        raise HTTPException(status_code=404, detail="clip not found")
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(content=data, media_type="audio/wav")


@app.post("/samples/{clip_id}/label")
async def label_sample(clip_id: str, req: LabelRequest):
    """Apply a review label, moving the clip into positive/negative."""
    store: SampleStore = app.state.store
    try:
        return store.label(clip_id, req.label)
    except KeyError:
        raise HTTPException(status_code=404, detail="clip not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/samples/{clip_id}/move")
async def move_sample(
    clip_id: str, wakeword: str = Query(...), bucket: str = Query(PENDING)
):
    """Move a clip to a different wake word's bucket (default pending).

    For the acoustic-overlap case: a live arm attributed to the priority word that
    is really another word's utterance (e.g. a bare "hermes" that landed in
    hey_hermes). Harvests it into the right word as real-usage training data.
    """
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    try:
        return store.move(clip_id, wakeword, bucket)
    except KeyError:
        raise HTTPException(status_code=404, detail="clip not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/samples/{clip_id}/copy")
async def copy_sample(
    clip_id: str, wakeword: str = Query(...), bucket: str = Query(PENDING)
):
    """Copy a clip into another wake word's bucket (source stays).

    For a false positive that fired several words — it's a hard negative for each,
    so fan it out instead of moving it.
    """
    _require_wakeword(wakeword)
    if bucket not in BUCKETS:
        raise HTTPException(status_code=400, detail=f"bad bucket (expected {BUCKETS})")
    store: SampleStore = app.state.store
    try:
        return store.copy(clip_id, wakeword, bucket)
    except KeyError:
        raise HTTPException(status_code=404, detail="clip not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/samples/{clip_id}")
async def delete_sample(clip_id: str):
    """Delete a clip (WAV + metadata)."""
    store: SampleStore = app.state.store
    if not store.delete(clip_id):
        raise HTTPException(status_code=404, detail="clip not found")
    return {"deleted": clip_id}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
