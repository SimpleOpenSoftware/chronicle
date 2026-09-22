"""Exercise the actual HTTP service and language pipeline with fake model weights."""

import io
import os
import sys
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from common.base_service import create_tts_app
from fastapi.testclient import TestClient
from providers.kokoro.service import KokoroService


class Pipeline:
    instances = []
    calls = []

    def __init__(self, lang_code, repo_id, device=None, model=None):
        self.lang_code = lang_code
        self.model = model if model is not None else object()
        self.instances.append(self)

    def load_voice(self, voice):
        if voice not in {"af_heart", "hf_alpha", "hf_beta"}:
            raise ValueError("Unknown voice")

    def __call__(self, text, *, voice, speed):
        self.load_voice(voice)
        self.calls.append((self.lang_code, voice, text))
        yield text, "phonemes", np.zeros(480, dtype=np.float32)


class KokoroDialogueTest(unittest.TestCase):
    def test_http_language_selection_and_shared_weights(self):
        Pipeline.instances.clear()
        Pipeline.calls.clear()
        with patch.dict(
            os.environ,
            {"TTS_DEVICE": "cpu", "TTS_LANG_CODE": "a", "TTS_VOICE": "af_heart"},
        ), patch.dict(sys.modules, {"kokoro": SimpleNamespace(KPipeline=Pipeline)}):
            with TestClient(create_tts_app(KokoroService("fixture-model"))) as client:
                assert client.get("/health").status_code == 200
                assert client.get("/info").json()["supported_languages"] == ["en", "hi"]
                for text, language, voice in [
                    ("Hello.", "en", "af_heart"),
                    ("नहीं, दूसरा वाला।", "hi", "hf_beta"),
                    ("आज 2 meetings हैं।", "hi", "hf_alpha"),
                ]:
                    response = client.post(
                        "/synthesize",
                        data={"text": text, "language": language, "voice": voice},
                    )
                    assert response.status_code == 200, response.text
                    with wave.open(io.BytesIO(response.content)) as audio:
                        assert audio.getframerate() == 24000
                        assert audio.getnframes() == 480
                    assert Pipeline.calls[-1] == (
                        "h" if language == "hi" else "a",
                        voice,
                        text,
                    )
                assert (
                    client.post(
                        "/synthesize",
                        data={"text": "नमस्ते", "language": "hi", "voice": "af_heart"},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/synthesize", data={"text": "Hi", "voice": "../../private"}
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/synthesize", data={"text": "Hi", "language": "unknown"}
                    ).status_code
                    == 422
                )
                assert len(Pipeline.instances) == 2
                assert Pipeline.instances[0].model is Pipeline.instances[1].model


if __name__ == "__main__":
    unittest.main()
