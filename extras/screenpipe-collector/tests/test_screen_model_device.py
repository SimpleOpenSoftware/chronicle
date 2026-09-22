"""Accelerator settings are explicit and distinguish prediction cache entries."""

import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from chronicle_screenpipe import screen_model


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    tensor_model = Mock()
    tensor_model.float.return_value = tensor_model
    tensor_model.to.return_value = tensor_model
    tensor_model.eval.return_value = tensor_model
    loader = Mock(return_value=tensor_model)
    torch = NS(
        __version__="synthetic-version",
        set_num_threads=Mock(),
        cuda=NS(is_available=lambda: False),
        backends=NS(
            cuda=NS(matmul=NS()),
            cudnn=NS(),
            mps=NS(is_available=lambda: True),
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "timm", NS(__version__="synthetic-version"))
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        NS(
            __version__="synthetic-version",
            AutoModelForImageClassification=NS(from_pretrained=loader),
            AutoImageProcessor=NS(from_pretrained=Mock()),
        ),
    )
    monkeypatch.setenv("PYTORCH_ENABLE_MPS_FALLBACK", "0")
    monkeypatch.setenv("PYTORCH_MPS_FAST_MATH", "0")
    monkeypatch.setenv("PYTORCH_MPS_PREFER_METAL", "0")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"synthetic weights")
    return NS(folder=tmp_path, torch=torch, loader=loader, model=tensor_model)


def test_unavailable_mps_fails_before_model_loading(runtime):
    runtime.torch.backends.mps.is_available = lambda: False
    with pytest.raises(RuntimeError, match="unavailable"):
        screen_model.FreepikModel(runtime.folder, device="mps")
    runtime.loader.assert_not_called()


@pytest.mark.parametrize(
    "flag",
    [
        "PYTORCH_ENABLE_MPS_FALLBACK",
        "PYTORCH_MPS_FAST_MATH",
        "PYTORCH_MPS_PREFER_METAL",
    ],
)
def test_mps_rejects_unvalidated_execution_settings(runtime, monkeypatch, flag):
    monkeypatch.setenv(flag, "1")
    with pytest.raises(ValueError, match=flag):
        screen_model.FreepikModel(runtime.folder, device="mps")
    runtime.loader.assert_not_called()


def test_mps_cache_identity_includes_chip_and_os(runtime, monkeypatch):
    import platform
    import subprocess

    monkeypatch.setattr(platform, "mac_ver", lambda: ("synthetic-os", (), "arm64"))
    monkeypatch.setattr(
        subprocess, "check_output", lambda *a, **kw: "Synthetic Chip A\n"
    )
    cpu = screen_model.FreepikModel(runtime.folder, device="cpu")
    mps = screen_model.FreepikModel(runtime.folder, device="mps")
    assert cpu.version != mps.version
    assert mps.device == "mps"
    runtime.model.to.assert_called_with("mps")
    monkeypatch.setattr(
        subprocess, "check_output", lambda *a, **kw: "Synthetic Chip B\n"
    )
    assert (
        screen_model.FreepikModel(runtime.folder, device="mps").version != mps.version
    )
    monkeypatch.setattr(platform, "mac_ver", lambda: ("different-os", (), "arm64"))
    assert (
        screen_model.FreepikModel(runtime.folder, device="mps").version != mps.version
    )
