"""Readiness and environment contracts for the Faster-Whisper provider."""

import asyncio
import sys
import types
from unittest.mock import Mock

import pytest

# These tests replace the transcriber with a fake, so the real faster_whisper
# package (and its CUDA wheels) is never used. A stub module lets the service
# import without installing that provider's dependency group in CI.
if "faster_whisper" not in sys.modules:
    _stub = types.ModuleType("faster_whisper")
    _stub.WhisperModel = object
    sys.modules["faster_whisper"] = _stub

from providers.faster_whisper import service as module  # noqa: E402


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("", None), ("   ", None), (" en ", "en")],
)
def test_empty_language_means_autodetect(monkeypatch, configured, expected):
    monkeypatch.setenv("LANGUAGE", configured)

    service = module.FasterWhisperService("model")

    assert service.language == expected


def test_inference_warmup_failure_prevents_ready_service(monkeypatch):
    failure = RuntimeError("libcublas is unavailable")

    class BrokenTranscriber:
        def __init__(self, model_id):
            self.model_id = model_id

        def load_model(self):
            return None

        def transcribe(self, *args, **kwargs):
            raise failure

    monkeypatch.setattr(module, "FasterWhisperTranscriber", BrokenTranscriber)
    unlink = Mock()
    monkeypatch.setattr(module.os, "unlink", unlink)

    service = module.FasterWhisperService("model")

    with pytest.raises(RuntimeError, match="inference warmup failed") as raised:
        asyncio.run(service.warmup())

    assert raised.value.__cause__ is failure
    unlink.assert_called_once()
