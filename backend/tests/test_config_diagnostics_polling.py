"""The real health loop must inspect configured state without reparsing it."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from omegaconf import OmegaConf

from backend import config_loader
from backend.controllers import system_controller
from backend.services.observability import health_poller


@pytest.mark.asyncio
async def test_registered_health_poller_reuses_loaded_configuration(
    monkeypatch, tmp_path
):
    (tmp_path / "defaults.yml").write_text("backend: {example: true}\n")
    (tmp_path / "config.yml").write_text("{}\n")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CONFIG_FILE", "config.yml")
    monkeypatch.setattr(config_loader, "_config_cache", None)
    monkeypatch.setattr(config_loader, "_runtime_overrides", {})
    calls = []
    original = OmegaConf.load

    def counted(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(OmegaConf, "load", counted)
    config_loader.load_config()
    assert len(calls) == 2
    registry = SimpleNamespace(models={}, defaults={}, get_default=lambda _: None)
    monkeypatch.setattr(system_controller, "get_models_registry", lambda: registry)
    redis = SimpleNamespace(smembers=AsyncMock(return_value=set()), aclose=AsyncMock())
    monkeypatch.setattr(health_poller, "create_async_redis", lambda **_: redis)
    for name in ("_poll_external_services", "_poll_worker_fleet", "_poll_failed_jobs"):
        monkeypatch.setattr(health_poller, name, AsyncMock())
    passes = 0

    async def bounded_sleep(delay):
        nonlocal passes
        if delay == health_poller.POLL_INTERVAL_SECS:
            passes += 1
            if passes == 3:
                raise asyncio.CancelledError

    monkeypatch.setattr(
        health_poller,
        "asyncio",
        SimpleNamespace(sleep=bounded_sleep, CancelledError=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await health_poller.run_health_poller()
    assert passes == 3
    redis.aclose.assert_awaited_once()
    assert len(calls) == 2, "Periodic health diagnostics re-read unchanged YAML"


@pytest.mark.asyncio
async def test_diagnostics_retains_load_warnings_until_explicit_reload(
    monkeypatch, tmp_path
):
    import warnings

    (tmp_path / "defaults.yml").write_text("{}\n")
    config_path = tmp_path / "config.yml"
    config_path.write_text("value: first\n")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CONFIG_FILE", "config.yml")
    monkeypatch.setattr(config_loader, "_config_cache", None)
    monkeypatch.setattr(config_loader, "_config_warnings", ())
    monkeypatch.setattr(config_loader, "_runtime_overrides", {})
    registry = SimpleNamespace(models={}, defaults={}, get_default=lambda _: None)
    monkeypatch.setattr(system_controller, "get_models_registry", lambda: registry)
    original = OmegaConf.load
    calls = []

    def warned(path):
        calls.append(str(path))
        result = original(path)
        if result.get("value") == "first":
            warnings.warn(
                "some elements are missing: variable 'TEST_SETTING'", UserWarning
            )
        return result

    monkeypatch.setattr(OmegaConf, "load", warned)
    with pytest.warns(UserWarning, match="TEST_SETTING"):
        config_loader.load_config()
    for _ in range(3):
        result = await system_controller.get_config_diagnostics()
        assert any("TEST_SETTING" in item["message"] for item in result["warnings"])
    assert len(calls) == 2
    config_path.write_text("value: second\n")
    assert config_loader.reload_config().value == "second"
    result = await system_controller.get_config_diagnostics()
    assert not any("TEST_SETTING" in item["message"] for item in result["warnings"])
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_diagnostics_reports_registry_failure(monkeypatch):
    monkeypatch.setattr(system_controller, "get_config_load_warnings", lambda: ())

    def failed():
        raise ValueError("invalid model definition")

    monkeypatch.setattr(system_controller, "get_models_registry", failed)
    result = await system_controller.get_config_diagnostics()
    assert result["components"]["model_registry"]["status"] == "unhealthy"
    assert any(
        "invalid model definition" in item["message"] for item in result["issues"]
    )


def test_saved_runtime_override_survives_diagnostic_read_and_reload(
    monkeypatch, tmp_path
):
    (tmp_path / "defaults.yml").write_text("backend: {enabled: false}\n")
    (tmp_path / "alternate.yml").write_text("{}\n")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CONFIG_FILE", "alternate.yml")
    monkeypatch.setattr(config_loader, "_config_cache", None)
    monkeypatch.setattr(config_loader, "_config_warnings", ())
    monkeypatch.setattr(config_loader, "_runtime_overrides", {})
    assert config_loader.load_config().backend.enabled is False
    assert config_loader.save_config_section("backend", {"enabled": True})
    assert config_loader.get_config_load_warnings() == ()
    assert config_loader.load_config().backend.enabled is True
    assert config_loader.reload_config().backend.enabled is True
    assert OmegaConf.load(tmp_path / "config.yml").backend.enabled is True
