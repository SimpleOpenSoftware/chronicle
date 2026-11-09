"""The k8s config seed keeps settings made in the app across redeploys."""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from backend.config_seed import main, merge_missing, seed_config_dir


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load(path: Path):
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.fixture
def dirs(tmp_path):
    seed, config = tmp_path / "seed", tmp_path / "config"
    _write(seed / "defaults.yml", "llm:\n  timeout: 30\n")
    return seed, config


def test_merge_adds_only_missing_keys_and_keeps_live_values():
    live = {"backend": {"cleanup": {"days": 7}}, "models": [{"name": "live"}]}
    seed = {
        "backend": {"cleanup": {"days": 30, "enabled": True}, "new": {"x": 1}},
        "models": [{"name": "repo"}],
    }

    added = merge_missing(live, seed)

    assert live == {
        "backend": {"cleanup": {"days": 7, "enabled": True}, "new": {"x": 1}},
        "models": [{"name": "live"}],
    }
    assert sorted(added) == ["backend.cleanup.enabled", "backend.new"]


def test_first_start_copies_seed(dirs):
    seed, config = dirs
    _write(seed / "config.yml", "defaults:\n  llm: repo-llm\n")
    _write(seed / "plugins.yml", "plugins: {}\n")

    seed_config_dir(seed, config)

    assert _load(config / "config.yml") == {"defaults": {"llm": "repo-llm"}}
    assert _load(config / "plugins.yml") == {"plugins": {}}
    assert _load(config / "defaults.yml") == {"llm": {"timeout": 30}}


def test_redeploy_keeps_app_settings_and_comments(dirs):
    seed, config = dirs
    _write(seed / "config.yml", "defaults:\n  llm: repo-llm\n  stt: repo-stt\n")
    _write(
        config / "config.yml",
        "# saved from the Settings page\ndefaults:\n  llm: app-llm\n",
    )

    seed_config_dir(seed, config)

    text = (config / "config.yml").read_text(encoding="utf-8")
    assert "# saved from the Settings page" in text
    assert _load(config / "config.yml") == {
        "defaults": {"llm": "app-llm", "stt": "repo-stt"}
    }


def test_defaults_are_always_replaced(dirs):
    seed, config = dirs
    _write(config / "defaults.yml", "llm:\n  timeout: 5\n")

    seed_config_dir(seed, config)

    assert _load(config / "defaults.yml") == {"llm": {"timeout": 30}}


def test_unchanged_live_file_is_not_rewritten(dirs):
    seed, config = dirs
    _write(seed / "config.yml", "a: 1\n")
    _write(config / "config.yml", "a:   2   # spacing kept\n")

    seed_config_dir(seed, config)

    assert (config / "config.yml").read_text(
        encoding="utf-8"
    ) == "a:   2   # spacing kept\n"


def test_missing_defaults_in_seed_fails(tmp_path):
    with pytest.raises(FileNotFoundError, match="make config-k8s"):
        seed_config_dir(tmp_path / "empty", tmp_path / "config")


def test_entry_point_seeds_directory(dirs):
    seed, config = dirs

    assert main([str(seed), str(config)]) == 0
    assert (config / "defaults.yml").exists()
