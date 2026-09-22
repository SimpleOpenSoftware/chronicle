"""
Kokoro TTS Service.

FastAPI service for the Kokoro-82M provider — a lightweight, low-VRAM
(<~1GB) TTS with fixed preset voices. Exposes the standard /health, /info,
and /synthesize endpoints via the shared app factory.
"""

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn
from common.base_service import BaseTTSService, create_tts_app
from providers.kokoro.synthesizer import KokoroSynthesizer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Kokoro language code → human-readable language.
_LANG_BY_CODE = {
    "a": "en",  # American English
    "b": "en",  # British English
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh",
}


class KokoroService(BaseTTSService):
    """
    TTS service using Kokoro-82M — lightweight (~82M params, <~1GB VRAM),
    GPU or CPU, no API key, fixed preset voices (Apache 2.0).

    Environment variables:
        TTS_MODEL: HuggingFace repo id (default: hexgrad/Kokoro-82M)
        TTS_VOICE: Preset voice name (default: af_heart)
        TTS_LANG_CODE: Kokoro language code (default: a = American English)
        TTS_SPEED: Speech speed multiplier (default: 1.0)
    """

    def __init__(self, model_id: Optional[str] = None):
        super().__init__(model_id)
        self.synthesizer: Optional[KokoroSynthesizer] = None

    @property
    def provider_name(self) -> str:
        return "kokoro"

    async def warmup(self) -> None:
        """Load the model and run a short warmup synthesis."""
        logger.info(f"Initializing Kokoro with model: {self.model_id}")

        loop = asyncio.get_event_loop()
        self.synthesizer = KokoroSynthesizer(self.model_id)
        await loop.run_in_executor(None, self.synthesizer.load_model)

        logger.info("Warming up model...")
        try:
            await self.synthesizer.synthesize("Hello.")
            logger.info("Model warmed up successfully")
        except Exception as e:
            logger.warning(f"Warmup failed (non-critical): {e}")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        if self.synthesizer is None:
            raise RuntimeError("Service not initialized")

        return await self.synthesizer.synthesize(
            text=text,
            reference_audio_path=reference_audio_path,
            reference_text=reference_text,
            **kwargs,
        )

    def get_capabilities(self) -> list[str]:
        return ["lightweight", "low_vram", "preset_voices", "request_language"]

    def get_supported_languages(self) -> Optional[list[str]]:
        return ["en", "hi"]


def main():
    """Main entry point for the Kokoro TTS service."""
    parser = argparse.ArgumentParser(description="Kokoro TTS Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8770, help="Port to bind to")
    parser.add_argument("--model", help="Model identifier", required=False)
    args = parser.parse_args()

    if args.model:
        os.environ["TTS_MODEL"] = args.model

    model_id = os.getenv("TTS_MODEL", "hexgrad/Kokoro-82M")

    service = KokoroService(model_id)
    app = create_tts_app(service)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
