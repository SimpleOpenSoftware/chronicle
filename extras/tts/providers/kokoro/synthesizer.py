"""
Kokoro TTS synthesizer implementation.

Kokoro-82M is a lightweight (82M param) StyleTTS2/ISTFTNet model with fixed
preset voices (no zero-shot cloning). It runs comfortably under ~1GB VRAM on
GPU (and on CPU), making it the quality-per-VRAM sweet spot for short replies.

Environment variables:
    TTS_MODEL: HuggingFace repo id (default: hexgrad/Kokoro-82M)
    TTS_VOICE: Preset voice name (default: af_heart)
    TTS_LANG_CODE: Kokoro language code (default: a = American English)
    TTS_SPEED: Speech speed multiplier (default: 1.0)
    TTS_DEVICE: auto, cpu, or cuda (default: auto)
    TTS_CPU_THREADS: Torch CPU inference threads (default: 4, CPU mode only)
"""

import asyncio
import io
import logging
import math
import os
import re
import wave
from typing import Optional

import numpy as np
import torch

logger = logging.getLogger(__name__)

# Kokoro generates at 24kHz.
KOKORO_SAMPLE_RATE = 24000


class KokoroSynthesizer:
    """Synthesizer using the Kokoro-82M model via the ``kokoro`` package."""

    def __init__(
        self,
        model_id: Optional[str] = None,
        voice: Optional[str] = None,
        lang_code: Optional[str] = None,
        speed: Optional[float] = None,
    ):
        self.model_id = model_id or os.getenv("TTS_MODEL", "hexgrad/Kokoro-82M")
        self.voice = voice or os.getenv("TTS_VOICE", "af_heart")
        self.lang_code = lang_code or os.getenv("TTS_LANG_CODE", "a")
        self.speed = float(
            speed if speed is not None else os.getenv("TTS_SPEED", "1.0")
        )
        if self.lang_code not in {"a", "b", "e", "f", "h", "i", "j", "p", "z"}:
            raise ValueError("Invalid Kokoro language code")
        self.hindi_voice = os.getenv("TTS_HINDI_VOICE", "hf_alpha")
        self.english_voice = os.getenv("TTS_ENGLISH_VOICE") or (
            self.voice if self.lang_code in {"a", "b"} else "af_heart"
        )
        self.english_code = (
            self.lang_code if self.lang_code in {"a", "b"} else self.english_voice[0]
        )
        self._validate_voice(self.voice, self.lang_code)
        self._validate_voice(self.hindi_voice, "h")
        self._validate_voice(
            self.english_voice,
            self.english_voice[:1] if self.english_voice[:1] in {"a", "b"} else "a",
        )
        if not math.isfinite(self.speed) or not 0.5 <= self.speed <= 2:
            raise ValueError("TTS_SPEED must be between 0.5 and 2")
        self.pipelines = {}
        self.pipeline = None
        self._is_loaded = False
        self._lock = asyncio.Lock()
        device = os.getenv("TTS_DEVICE", "auto").strip().lower()
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("TTS_DEVICE must be auto, cpu, or cuda")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        elif device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("TTS_DEVICE=cuda requires an available CUDA device")
        self._device = device
        if device == "cpu":
            threads = int(os.getenv("TTS_CPU_THREADS", "4"))
            if threads < 1:
                raise ValueError("TTS_CPU_THREADS must be positive")
            torch.set_num_threads(threads)

        logger.info(
            f"KokoroSynthesizer initialized: model={self.model_id}, "
            f"voice={self.voice}, lang_code={self.lang_code}, "
            f"speed={self.speed}, device={self._device}"
        )

    def load_model(self) -> None:
        """Load the Kokoro pipeline (downloads weights to HF cache on first use)."""
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        # Lazy import: kokoro is only needed once model loading starts.
        from kokoro import KPipeline

        logger.info(f"Loading Kokoro pipeline: {self.model_id} ({self.lang_code})")
        self.pipeline = KPipeline(
            lang_code=self.lang_code,
            repo_id=self.model_id,
            device=self._device,
        )
        self.pipelines[self.lang_code] = self.pipeline
        for code, configured_voice in (
            (self.english_code, self.english_voice),
            ("h", self.hindi_voice),
        ):
            if code not in self.pipelines:
                self.pipelines[code] = KPipeline(
                    lang_code=code, repo_id=self.model_id, model=self.pipeline.model
                )
            self.pipelines[code].load_voice(configured_voice)
        self.pipeline.load_voice(self.voice)
        self._is_loaded = True
        logger.info("Kokoro pipeline loaded successfully")

    async def synthesize(
        self,
        text: str,
        reference_audio_path: Optional[str] = None,
        reference_text: Optional[str] = None,
        **kwargs,
    ) -> tuple[bytes, int]:
        """
        Synthesize speech from text.

        Kokoro uses preset voices, so reference_audio_path/reference_text are
        ignored. The voice and speed come from config; a request may override
        them via the ``voice``/``speed`` kwargs.
        """
        if not self._is_loaded or self.pipeline is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        language = kwargs.get("language")
        if language not in {None, "en", "hi"}:
            raise ValueError("Kokoro request language must be en or hi")
        code = (
            "h"
            if language == "hi"
            else self.english_code if language == "en" else self.lang_code
        )
        voice = kwargs.get("voice") or (
            self.hindi_voice
            if code == "h"
            else self.english_voice if language == "en" else self.voice
        )
        self._validate_voice(voice, code)
        speed = float(kwargs.get("speed", self.speed))
        if not math.isfinite(speed) or not 0.5 <= speed <= 2:
            raise ValueError("Speech speed must be between 0.5 and 2")

        logger.info(f"Synthesizing {len(text)} chars (voice={voice}, speed={speed})")

        async with self._lock:
            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._synthesize_sync, text, voice, speed, code
            )

        return wav_bytes, KOKORO_SAMPLE_RATE

    @staticmethod
    def _validate_voice(voice: str, code: str):
        if (
            not isinstance(voice, str)
            or not re.fullmatch(r"[a-z][fm]_[a-z]+", voice)
            or not (voice[0] == code or {voice[0], code} <= {"a", "b"})
        ):
            raise ValueError("Voice must be a preset matching its Kokoro language")
        if code == "h" and voice not in {"hf_alpha", "hf_beta", "hm_omega", "hm_psi"}:
            raise ValueError("Unknown Hindi Kokoro voice")

    def _synthesize_sync(self, text: str, voice: str, speed: float, code: str) -> bytes:
        """Synchronous synthesis (runs in a thread pool)."""
        # KPipeline splits longer text into sentence chunks and yields one audio
        # tensor per chunk; concatenate them into a single waveform.
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio in self.pipelines[code](text, voice=voice, speed=speed):
            if audio is None:
                continue
            if isinstance(audio, torch.Tensor):
                audio = audio.detach().cpu().numpy()
            chunks.append(np.asarray(audio, dtype=np.float32).flatten())

        if not chunks:
            raise ValueError("Kokoro produced no audio for the given text")

        samples = np.concatenate(chunks)
        return self._to_wav_bytes(samples)

    def _to_wav_bytes(self, samples: np.ndarray) -> bytes:
        """Convert a float32 numpy waveform to 16-bit PCM WAV bytes."""
        samples = np.asarray(samples, dtype=np.float32).flatten()

        # Normalize to [-1, 1] if the model returns out-of-range values.
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        if peak > 1.0:
            samples = samples / peak

        pcm = (samples * 32767.0).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(KOKORO_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

        return buf.getvalue()

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
