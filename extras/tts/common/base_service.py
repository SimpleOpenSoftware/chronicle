"""
Abstract base class for TTS services.

Provides a common interface and FastAPI app setup for all TTS providers.
"""

import io
import logging
import os
import tempfile
import time
from abc import ABC, abstractmethod
from typing import Optional

from common.response_models import HealthResponse, InfoResponse
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

logger = logging.getLogger(__name__)


class BaseTTSService(ABC):
    """
    Abstract base class for TTS service implementations.

    Subclasses must implement:
    - synthesize(): Generate audio from text
    - warmup(): Initialize and warm up the model
    - get_capabilities(): Return list of supported capabilities
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.getenv("TTS_MODEL", "")
        self._is_ready = False

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name (e.g., 'tada', 'piper')."""
        pass

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize into speech.
            reference_audio_path: Optional path to reference audio for voice cloning.
            reference_text: Optional transcript of the reference audio.
            **kwargs: Provider-specific parameters (temperature, top_p, seed, etc.)

        Returns:
            Tuple of (wav_bytes, sample_rate).
        """
        pass

    @abstractmethod
    async def warmup(self) -> None:
        """Initialize and warm up the model. Called once during startup."""
        pass

    def get_model_id(self) -> str:
        return self.model_id

    @abstractmethod
    def get_capabilities(self) -> list[str]:
        """Return list of supported capabilities."""
        pass

    def get_supported_languages(self) -> Optional[list[str]]:
        """Return list of supported language codes, or None if multilingual."""
        return None

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    async def health_probe(self) -> bool:
        """Liveness of the thing that actually does the work.

        Default: the model was warmed up in-process, so readiness is enough.
        Providers backed by a SEPARATE process (e.g. Fish Speech runs its
        inference server as a subprocess) must override this to re-check that
        backend, so /health can't report green after the backend dies.
        """
        return self._is_ready


def create_tts_app(service: BaseTTSService) -> FastAPI:
    """
    Create a FastAPI application with standard TTS endpoints.

    Args:
        service: Initialized TTS service instance

    Returns:
        Configured FastAPI application
    """
    app = FastAPI(
        title=f"{service.provider_name.title()} TTS Service",
        version="1.0.0",
        description=f"TTS service using {service.provider_name} provider",
    )

    @app.on_event("startup")
    async def startup_event():
        logger.info(f"Starting {service.provider_name} TTS service...")
        await service.warmup()
        service._is_ready = True
        logger.info(f"{service.provider_name} TTS service ready")

    @app.get("/health", response_model=HealthResponse)
    async def health_check(response: Response):
        # Probe the real backend (subprocess/model), not just the startup flag,
        # so a backend that dies after boot flips health instead of staying green.
        try:
            alive = await service.health_probe()
        except Exception as e:  # noqa: BLE001 - a probe error means "not healthy"
            logger.warning(f"health_probe raised: {e}")
            alive = False
        if not alive:
            response.status_code = 503
        return HealthResponse(
            status="healthy" if alive else "initializing",
            model=service.get_model_id(),
            provider=service.provider_name,
        )

    @app.get("/info", response_model=InfoResponse)
    async def service_info():
        return InfoResponse(
            model_id=service.get_model_id(),
            provider=service.provider_name,
            capabilities=service.get_capabilities(),
            supported_languages=service.get_supported_languages(),
        )

    @app.post("/synthesize")
    async def synthesize(
        text: str = Form(...),
        reference_audio: Optional[UploadFile] = File(None),
        reference_text: Optional[str] = Form(None),
        language: Optional[str] = Form(None),
        voice: Optional[str] = Form(None),
        temperature: Optional[float] = Form(None),
        top_p: Optional[float] = Form(None),
        repetition_penalty: Optional[float] = Form(None),
        seed: Optional[int] = Form(None),
        max_new_tokens: Optional[int] = Form(None),
    ):
        """
        Synthesize speech from text.

        Accepts text and an optional reference audio file for voice cloning.
        Generation parameters (temperature, top_p, seed, etc.) are provider-specific.
        Returns WAV audio bytes.
        """
        if not service.is_ready:
            raise HTTPException(status_code=503, detail="Service not ready")

        request_start = time.time()
        logger.info(f"Synthesis request: {len(text)} chars")

        # Collect non-None generation kwargs
        gen_kwargs = {}
        if language is not None:
            supported = service.get_supported_languages()
            if supported is not None and language not in supported:
                raise HTTPException(
                    status_code=422, detail="Unsupported speech language"
                )
            gen_kwargs["language"] = language
        if voice is not None:
            gen_kwargs["voice"] = voice
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        if top_p is not None:
            gen_kwargs["top_p"] = top_p
        if repetition_penalty is not None:
            gen_kwargs["repetition_penalty"] = repetition_penalty
        if seed is not None:
            gen_kwargs["seed"] = seed
        if max_new_tokens is not None:
            gen_kwargs["max_new_tokens"] = max_new_tokens

        ref_audio_path = None
        try:
            # Save reference audio to temp file if provided
            if reference_audio is not None:
                audio_content = await reference_audio.read()
                suffix = ".wav"
                if reference_audio.filename:
                    ext = reference_audio.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("wav", "mp3", "flac", "ogg", "m4a"):
                        suffix = f".{ext}"
                with tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False
                ) as tmp_file:
                    tmp_file.write(audio_content)
                    ref_audio_path = tmp_file.name

            synth_start = time.time()
            wav_bytes, sample_rate = await service.synthesize(
                text=text,
                reference_audio_path=ref_audio_path,
                reference_text=reference_text,
                **gen_kwargs,
            )
            synth_time = time.time() - synth_start
            logger.info(f"Synthesis completed in {synth_time:.3f}s")

            total_time = time.time() - request_start
            logger.info(f"Total request time: {total_time:.3f}s")

            return Response(
                content=wav_bytes,
                media_type="audio/wav",
                headers={
                    "X-Sample-Rate": str(sample_rate),
                    "X-Provider": service.provider_name,
                    "X-Model": service.get_model_id(),
                },
            )

        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as e:
            error_time = time.time() - request_start
            logger.exception(f"Error after {error_time:.3f}s: {e}")
            raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")

        finally:
            if ref_audio_path:
                try:
                    os.unlink(ref_audio_path)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {ref_audio_path}: {e}")

    return app
