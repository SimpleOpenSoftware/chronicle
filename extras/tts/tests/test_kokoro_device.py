"""Device selection must be explicit and never silently abandon a CUDA request."""

import os
import unittest
from unittest.mock import patch

from providers.kokoro.synthesizer import KokoroSynthesizer


class KokoroDeviceTest(unittest.TestCase):
    @patch("providers.kokoro.synthesizer.torch.set_num_threads")
    @patch("providers.kokoro.synthesizer.torch.cuda.is_available", return_value=True)
    def test_cpu_does_not_probe_or_initialize_cuda(self, available, threads):
        with patch.dict(os.environ, {"TTS_DEVICE": "cpu", "TTS_CPU_THREADS": "4"}):
            synthesizer = KokoroSynthesizer()
        self.assertEqual(synthesizer._device, "cpu")
        available.assert_not_called()
        threads.assert_called_once_with(4)

    @patch("providers.kokoro.synthesizer.torch.cuda.is_available", return_value=False)
    def test_explicit_cuda_unavailable_fails(self, available):
        with patch.dict(os.environ, {"TTS_DEVICE": "cuda"}):
            with self.assertRaisesRegex(RuntimeError, "requires an available CUDA"):
                KokoroSynthesizer()

    @patch("providers.kokoro.synthesizer.torch.set_num_threads")
    @patch("providers.kokoro.synthesizer.torch.cuda.is_available", return_value=True)
    def test_auto_uses_available_cuda(self, available, threads):
        with patch.dict(os.environ, {"TTS_DEVICE": "auto"}):
            self.assertEqual(KokoroSynthesizer()._device, "cuda")
        threads.assert_not_called()

    def test_invalid_configuration_fails(self):
        for settings in [
            {"TTS_DEVICE": "typo"},
            {"TTS_DEVICE": "cpu", "TTS_CPU_THREADS": "0"},
            {"TTS_DEVICE": "cpu", "TTS_CPU_THREADS": "bad"},
        ]:
            with self.subTest(settings=settings), patch.dict(os.environ, settings):
                with self.assertRaises(ValueError):
                    KokoroSynthesizer()


if __name__ == "__main__":
    unittest.main()
