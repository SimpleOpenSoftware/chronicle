"""Local Freepik inference with explicit, content-addressed image preprocessing."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
from pathlib import Path

MODEL_ID = "Freepik/nsfw_image_detector"
MODEL_REVISION = "15b85477e4fd2000db76ae9aae0f89a72f95e2e3"
LABELS = ("neutral", "low", "medium", "high")


class FreepikModel:
    def __init__(self, model_dir=None, *, device="cpu"):
        if device not in ("cpu", "cuda", "mps"):
            raise ValueError("Privacy device must be cpu, cuda or mps")
        if device == "mps":
            for flag in (
                "PYTORCH_ENABLE_MPS_FALLBACK",
                "PYTORCH_MPS_FAST_MATH",
                "PYTORCH_MPS_PREFER_METAL",
            ):
                if os.environ.get(flag, "0") != "0":
                    raise ValueError(f"Privacy inference requires {flag}=0")
        # Load the optional model runtime only when screening inference is requested.
        import timm

        # Load the optional model runtime only when screening inference is requested.
        import torch

        # Load the optional model runtime only when screening inference is requested.
        import transformers

        # Load the optional model runtime only when screening inference is requested.
        from huggingface_hub import snapshot_download

        # Load the optional model runtime only when screening inference is requested.
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        torch.set_num_threads(4)
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("Configured privacy GPU is unavailable")
        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("Configured privacy GPU is unavailable")
        # Full precision is the evaluated policy. Never silently downgrade or
        # switch execution settings when a configured accelerator is unavailable.
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        self.device = device
        hardware = (
            "cuda-sm" + "".join(map(str, torch.cuda.get_device_capability()))
            if device == "cuda"
            else "cpu"
        )
        if device == "mps":
            chip = (
                subprocess.check_output(
                    ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                    text=True,
                    timeout=5,
                )
                .strip()
                .replace(" ", "-")
            )
            system_version = platform.mac_ver()[0]
            if not chip or not system_version:
                raise RuntimeError("Privacy GPU execution identity is unavailable")
            hardware = f"mps-{chip}-macos{system_version}:fastmath-off:fallback-off:prefermetal-off"
        folder = (
            Path(model_dir)
            if model_dir
            else Path(
                snapshot_download(
                    MODEL_ID,
                    revision=MODEL_REVISION,
                    allow_patterns=["config.json", "model.safetensors"],
                )
            )
        )
        self.model = (
            AutoModelForImageClassification.from_pretrained(
                folder, local_files_only=True
            )
            .float()
            .to(device)
            .eval()
        )
        self.processor = AutoImageProcessor.from_pretrained(
            folder, local_files_only=True, use_fast=False
        )
        digest = hashlib.sha256()
        for name in ("config.json", "model.safetensors"):
            digest.update((folder / name).read_bytes())
        self.version = f"{digest.hexdigest()}:freepik-pad-tiles60-v1:fp32:tf32-off:{hardware}:torch{torch.__version__}:transformers{transformers.__version__}:timm{timm.__version__}"

    def prepare(self, data):
        # Load the optional model runtime only when screening inference is requested.
        from PIL import Image, ImageOps

        picture = Image.open(io.BytesIO(data)).convert("RGB")
        width, height = picture.size
        crop_width, crop_height = round(width * 0.6), round(height * 0.6)
        regions = [(0, 0, width, height)] + [
            (x, y, crop_width, crop_height)
            for y in (0, height - crop_height)
            for x in (0, width - crop_width)
        ]
        views = [picture.crop((x, y, x + w, y + h)) for x, y, w, h in regions]
        # Default model center-crops discard the edges of desktop screenshots.
        views = [
            ImageOps.pad(view, (max(view.size), max(view.size)), color=(127, 127, 127))
            for view in views
        ]
        inputs = self.processor(images=views, return_tensors="pt")
        digest = hashlib.sha256(repr(regions).encode())
        for key in sorted(inputs):
            tensor = inputs[key].contiguous()
            digest.update(repr((key, tuple(tensor.shape), str(tensor.dtype))).encode())
            digest.update(tensor.numpy().tobytes())
        gray = list(picture.convert("L").resize((9, 8)).getdata())
        bits = [
            gray[y * 9 + x + 1] > gray[y * 9 + x] for y in range(8) for x in range(8)
        ]
        perceptual = f"{int(''.join('1' if bit else '0' for bit in bits), 2):016x}"
        return (inputs, regions), digest.hexdigest(), perceptual

    def predict(self, prepared):
        # Load the optional model runtime only when screening inference is requested.
        import torch

        inputs, regions = prepared
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            logits = self.model(**inputs).logits
            if logits.shape != (len(regions), 4) or not torch.isfinite(logits).all():
                raise ValueError("Invalid classifier output")
            probabilities = logits.softmax(-1).tolist()
        # A region identifies a model input crop, not a localized body detection.
        return [
            {"class": label, "score": score, "region": list(region)}
            for region, scores in zip(regions, probabilities)
            for label, score in zip(LABELS, scores)
        ]
