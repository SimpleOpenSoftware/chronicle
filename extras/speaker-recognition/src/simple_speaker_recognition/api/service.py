"""FastAPI service for speaker recognition and diarization - fully refactored."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union, cast

import torch
import uvicorn
import yaml
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from omegaconf import OmegaConf
from pydantic import Field
from pydantic_settings import BaseSettings

import simple_speaker_recognition.database as database
import simple_speaker_recognition.database.models as models
from simple_speaker_recognition.api.core.utils import get_data_directory
from simple_speaker_recognition.constants import DEFAULT_SIMILARITY_THRESHOLD
from simple_speaker_recognition.core.audio_backend import AudioBackend
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import init_db
from simple_speaker_recognition.system_event_reporter import (
    install_system_event_reporter,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("speaker_service")

# Forward ERROR/CRITICAL logs to the backend's system-event ledger so failures
# inside this service surface on the admin "System Errors" page. No-op unless
# CHRONICLE_INGEST_URL/CHRONICLE_INGEST_TOKEN are configured.
install_system_event_reporter(source="speaker-recognition")


def load_speaker_config_from_root() -> dict:
    """
    Load speaker_recognition section from root config.yml using OmegaConf.

    Returns:
        Dictionary with speaker_recognition config, or empty dict if not found
    """
    try:
        config_dir = Path(os.getenv("CONFIG_DIR", "/app/config"))
        defaults_path = config_dir / "defaults.yml"
        config_path = config_dir / "config.yml"

        if not defaults_path.exists() and not config_path.exists():
            log.warning(f"No config files found in {config_dir}, using defaults")
            return {}

        # Load and merge configs using OmegaConf
        defaults = OmegaConf.load(defaults_path) if defaults_path.exists() else {}
        user_config = OmegaConf.load(config_path) if config_path.exists() else {}
        merged = OmegaConf.merge(defaults, user_config)

        # Extract speaker_recognition section
        speaker_config = merged.get("speaker_recognition", {})

        # Resolve environment variables and convert to dict
        resolved = OmegaConf.to_container(speaker_config, resolve=True)

        # Never serialize resolved configuration here: it can contain API keys and
        # Hugging Face tokens expanded from environment variables.
        log.info("Loaded speaker_recognition config from %s", config_dir)
        return resolved

    except Exception as e:
        log.warning(f"Failed to load root config: {e}, using defaults")
        return {}


class Settings(BaseSettings):
    """Service configuration settings."""

    similarity_threshold: float = Field(
        default=DEFAULT_SIMILARITY_THRESHOLD,
        description="Cosine similarity threshold for speaker identification",
    )
    data_dir: Path = Field(
        default_factory=get_data_directory,
        description="Directory for storing speaker data",
    )
    enrollment_audio_dir: Path = Field(
        default_factory=lambda: get_data_directory() / "enrollment_audio",
        description="Directory for storing enrollment audio files",
    )
    max_file_seconds: int = Field(
        default=180, description="Maximum file duration in seconds"
    )
    deepgram_api_key: Optional[str] = Field(
        default=None, description="Deepgram API key for wrapper service"
    )
    deepgram_base_url: str = Field(
        default="https://api.deepgram.com", description="Deepgram API base URL"
    )
    hf_token: Optional[str] = Field(
        default=None, description="Hugging Face token for Pyannote models"
    )

    # Backend API configuration for chunked processing
    # Loaded from root config.yml speaker_recognition section, can be overridden by env vars
    max_diarize_duration: int = Field(
        default=600,
        description="Maximum audio duration (seconds) for single PyAnnote call",
    )
    diarize_concurrent_chunks: int = Field(
        default=2,
        ge=1,
        description="Maximum neural segmentation windows processed concurrently",
    )
    diarize_chunk_overlap: float = Field(
        default=5.0, description="Overlap (seconds) between chunks for continuity"
    )
    backend_api_url: str = Field(
        default="http://host.docker.internal:8000",
        description="Backend API URL for fetching audio segments",
    )

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore extra environment variables

    def __init__(self, **kwargs):
        """Initialize settings, loading from root config.yml first, then env overrides."""
        # Load from root config.yml
        root_config = load_speaker_config_from_root()

        # Apply root config values as defaults (only if not provided in kwargs or env)
        if (
            "max_diarize_duration" not in kwargs
            and "MAX_DIARIZE_DURATION" not in os.environ
        ):
            kwargs["max_diarize_duration"] = root_config.get(
                "max_diarize_duration", 600
            )

        if (
            "diarize_concurrent_chunks" not in kwargs
            and "DIARIZE_CONCURRENT_CHUNKS" not in os.environ
        ):
            kwargs["diarize_concurrent_chunks"] = root_config.get(
                "diarize_concurrent_chunks", 2
            )

        if (
            "diarize_concurrent_chunks" not in kwargs
            and "DIARIZE_CONCURRENT_CHUNKS" not in os.environ
        ):
            kwargs["diarize_concurrent_chunks"] = root_config.get(
                "diarize_concurrent_chunks", 1
            )

        if (
            "diarize_chunk_overlap" not in kwargs
            and "DIARIZE_CHUNK_OVERLAP" not in os.environ
        ):
            kwargs["diarize_chunk_overlap"] = root_config.get(
                "diarize_chunk_overlap", 5.0
            )

        if "backend_api_url" not in kwargs and "BACKEND_API_URL" not in os.environ:
            kwargs["backend_api_url"] = root_config.get(
                "backend_api_url", "http://host.docker.internal:8000"
            )

        super().__init__(**kwargs)


# Get HF_TOKEN from environment and create settings
hf_token = os.getenv("HF_TOKEN")
if not hf_token:
    raise ValueError(
        "HF_TOKEN environment variable is required. Please set it before running the service."
    )

hf_token = cast(str, hf_token)
auth = Settings()  # Load other settings from env vars or .env file

# Override Deepgram API key from environment if available
if os.getenv("DEEPGRAM_API_KEY"):
    auth.deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")

# Set HF token in auth settings for consistency
auth.hf_token = hf_token

# Global variables for storing initialized resources
audio_backend: AudioBackend
speaker_db: UnifiedSpeakerDB
# Device selection with environment override
log.info(f"CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    log.info(f"CUDA device count: {torch.cuda.device_count()}")
    log.info(f"CUDA device name: {torch.cuda.get_device_name(0)}")
    log.info(f"CUDA version: {torch.version.cuda}")

compute_mode = os.getenv("COMPUTE_MODE", "cpu").lower()
if compute_mode == "gpu" and torch.cuda.is_available():
    device = torch.device("cuda")
    log.info("Using GPU mode via COMPUTE_MODE=gpu environment variable")
elif compute_mode == "gpu" and not torch.cuda.is_available():
    device = torch.device("cpu")
    log.warning(
        "COMPUTE_MODE=gpu requested but CUDA not available, falling back to CPU"
    )
else:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# --- CUDA health probe + self-heal watchdog ----------------------------------
# A `cudaErrorLaunchFailure` ("unspecified launch failure") poisons the process's
# CUDA context: every later kernel launch fails until the process restarts, yet a
# plain liveness check (return "ok") still looks healthy. So /health silently lied
# while 100% of /identify calls 500'd. These exercise the GPU for real so the fault
# is both *reported* (health → 503) and *recovered* (watchdog exits → restart policy
# brings the service back with a fresh context).
HEALTH_PROBE_INTERVAL_SECONDS = float(os.getenv("CUDA_PROBE_INTERVAL_SECONDS", "30"))
HEALTH_MAX_CONSECUTIVE_FAILURES = int(os.getenv("CUDA_PROBE_MAX_FAILURES", "3"))


def _probe_cuda() -> Optional[str]:
    """Launch one tiny GPU kernel and synchronize to detect a corrupted CUDA context.

    Returns None when the GPU is healthy (or the service is CPU-only), or a short error
    string when a kernel launch/sync fails — which, for a sticky launch failure, it will
    keep doing until the process restarts.
    """
    if device.type != "cuda":
        return None
    try:
        x = torch.ones(64, device=device) * 2.0
        torch.cuda.synchronize()
        _ = float(x.sum().item())  # force the result back off the GPU
        return None
    except Exception as e:  # noqa: BLE001 — any CUDA fault here means unhealthy
        return f"{type(e).__name__}: {e}"


async def _cuda_watchdog() -> None:
    """Self-heal a poisoned CUDA context.

    `cudaErrorLaunchFailure` is sticky — only a process restart clears it. Probe the GPU
    periodically and, after a few *consecutive* failures (to ride out a one-off blip under
    contention), exit so the container's ``restart: unless-stopped`` policy brings the
    service back with a fresh context instead of staying silently broken.
    """
    if device.type != "cuda":
        return
    consecutive = 0
    while True:
        await asyncio.sleep(HEALTH_PROBE_INTERVAL_SECONDS)
        err = await asyncio.to_thread(_probe_cuda)
        if err is None:
            consecutive = 0
            continue
        consecutive += 1
        log.error(
            "CUDA watchdog: GPU probe failed (%d/%d): %s",
            consecutive,
            HEALTH_MAX_CONSECUTIVE_FAILURES,
            err,
        )
        if consecutive >= HEALTH_MAX_CONSECUTIVE_FAILURES:
            log.critical(
                "CUDA context unrecoverable after %d consecutive probes — exiting so the "
                "container restarts with a fresh context",
                consecutive,
            )
            os._exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan event handler for startup and shutdown."""
    global audio_backend, speaker_db, _catalog_fingerprint

    # Startup: Initialize database and load models
    log.info("=== Speaker Recognition Service Starting ===")
    log.info("Version: 2025-08-05-refactored")
    log.info("This version uses modular router architecture")
    log.info("Initializing database...")
    init_db()

    log.info("Loading models...")
    assert hf_token is not None
    audio_backend = AudioBackend(
        hf_token,
        device,
        max_diarization_workers=auth.diarize_concurrent_chunks,
    )
    log.info(
        "Diarization runtime: %ds windows, %d concurrent, %.1fs overlap",
        auth.max_diarize_duration,
        auth.diarize_concurrent_chunks,
        auth.diarize_chunk_overlap,
    )
    speaker_db = UnifiedSpeakerDB(
        emb_dim=audio_backend.embedder.dimension,
        base_dir=auth.data_dir,
        similarity_thr=auth.similarity_threshold,
    )
    if _CATALOG_READ_ONLY:
        _catalog_fingerprint = await asyncio.to_thread(_snapshot_identity, speaker_db)
    log.info("Models ready ✔ – device=%s", device)

    # Ensure enrollment audio directory exists
    auth.enrollment_audio_dir.mkdir(parents=True, exist_ok=True)
    log.info("Enrollment audio directory ready: %s", auth.enrollment_audio_dir)

    # No tenant is seeded here. A tenant is a Chronicle user id, which only a caller
    # can supply, so rows are created on first enrolment instead.

    # Start the CUDA self-heal watchdog (no-op on CPU).
    watchdog_task = asyncio.create_task(_cuda_watchdog())
    log.info("CUDA watchdog started (device=%s)", device)

    # Yield control to the application
    yield

    # Shutdown: Clean up resources if needed
    watchdog_task.cancel()
    audio_backend.close()
    log.info("Shutting down speaker recognition service")


app = FastAPI(
    title="Simple Speaker Recognition Service", version="0.2.0", lifespan=lifespan
)

react_ui_host = (
    os.getenv("REACT_UI_HOST", "localhost") + ":" + os.getenv("REACT_UI_PORT", "5173")
)
# HA replicas serve immutable catalog snapshots. Freeze the identity published
# at startup and deny enrollment changes on every mutation entry point.
from .catalog_contract import ReadOnlyCatalog, fingerprint

_CATALOG_READ_ONLY = os.getenv("SPEAKER_CATALOG_READ_ONLY", "false").lower() == "true"
_catalog_fingerprint = None
app.add_middleware(ReadOnlyCatalog, enabled=_CATALOG_READ_ONLY)
from .gallery_fence import GalleryRevisionFence

app.add_middleware(GalleryRevisionFence)

from simple_speaker_recognition.core.gallery_privacy import GalleryHeld

from .privacy import gallery_privacy_hold

app.add_exception_handler(GalleryHeld, gallery_privacy_hold)


def _catalog_identity(db):
    # Writable single instances must not advertise a frozen catalog generation.
    if not _CATALOG_READ_ONLY:
        return {"catalog_fingerprint": None, "read_only": False}
    return {"catalog_fingerprint": _catalog_fingerprint, "read_only": True}


def _snapshot_identity(db):

    session = database.get_db_session()
    try:
        rows = [
            {
                "id": row.id,
                "user_id": row.user_id,
                "name": row.name,
                "embedding_data": row.embedding_data,
                "segments": [
                    {
                        column.name: getattr(segment, column.name)
                        for column in models.SpeakerAudioSegment.__table__.columns
                    }
                    for segment in session.query(models.SpeakerAudioSegment)
                    .filter(models.SpeakerAudioSegment.speaker_id == row.id)
                    .order_by(models.SpeakerAudioSegment.id)
                ],
            }
            for row in session.query(models.Speaker).all()
        ]
        return fingerprint(
            rows,
            {"dimension": db.emb_dim, "embedder": audio_backend.EMBEDDING_MODEL_ID},
        )
    finally:
        session.close()


# Add CORS middleware for direct WebSocket connections from HTTPS frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[react_ui_host, "https://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include all routers
from .routers import (
    deepgram_router,
    enrollment_audit_router,
    enrollment_operations_router,
    enrollment_router,
    identification_router,
    speakers_router,
    users_router,
    websocket_router,
)

# Include routers with appropriate tags and prefixes
app.include_router(users_router, tags=["users"])
app.include_router(speakers_router, tags=["speakers"])
app.include_router(enrollment_router, tags=["enrollment"])
app.include_router(enrollment_audit_router, tags=["enrollment-audit"])
app.include_router(enrollment_operations_router, tags=["enrollment-operations"])
app.include_router(identification_router, tags=["identification"])
app.include_router(deepgram_router, tags=["deepgram"])
app.include_router(websocket_router, tags=["websocket"])


async def get_db() -> UnifiedSpeakerDB:
    """Get speaker database dependency."""
    return speaker_db


@app.get("/health")
async def health(db: UnifiedSpeakerDB = Depends(get_db)):
    """Lightweight process liveness check.

    This route must remain responsive while a long diarization owns the GPU. CUDA
    fault detection is handled by the background watchdog and the readiness route.
    """
    return {
        "status": "ok",
        "version": "0.2.0-refactored",
        "device": str(device),
        "speakers": db.get_speaker_count(),
        "architecture": "modular-routers",
        **_catalog_identity(db),
    }


@app.get("/readiness")
async def readiness(db: UnifiedSpeakerDB = Depends(get_db)):
    """Exercise CUDA and report whether the service can run inference.

    The probe may wait behind an active GPU workload; callers needing process
    liveness should use ``/health`` instead.
    """
    cuda_error = await asyncio.to_thread(_probe_cuda)
    body = {
        "status": "ok" if cuda_error is None else "cuda_error",
        "version": "0.2.0-refactored",
        "device": str(device),
        "speakers": db.get_speaker_count(),
        "architecture": "modular-routers",
        **_catalog_identity(db),
    }
    if cuda_error is not None:
        body["cuda_error"] = cuda_error
        log.error("Health probe: CUDA context unhealthy: %s", cuda_error)
        return JSONResponse(status_code=503, content=body)
    return body


def main():
    """Main entry point for the service."""
    host = os.getenv("SPEAKER_SERVICE_HOST", "0.0.0.0")
    port = int(os.getenv("SPEAKER_SERVICE_PORT", "8085"))

    log.info(f"Starting Refactored Speaker Service on {host}:{port}")
    uvicorn.run(
        "simple_speaker_recognition.api.service:app",
        host=host,
        port=port,
        reload=bool(os.getenv("DEV", False)),
    )


if __name__ == "__main__":
    main()
