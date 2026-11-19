import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronicle_tray.screenpipe_settings import (
    _audio_modes,
    _audio_sources,
    _capture_argv,
    _capture_settings_from_argv,
    _forward_audio_setting,
    _save_forward_audio_setting,
    _toggled_audio_modes,
)


def _argv(flags: str = "") -> list[str]:
    return ["/usr/bin/screenpipe", "record", "--api-auth", "true", *flags.split()]


def test_capture_settings_default_to_audio_and_screen_enabled():
    assert _capture_settings_from_argv(_argv()) == ("both", True)


def test_save_capture_settings_updates_only_capture_flags():
    args = _capture_argv(
        _argv("--disable-audio"),
        audio_mode="system",
        screen_enabled=False,
        audio_devices=["Speakers (output)"],
    )

    assert _capture_settings_from_argv(args) == ("system", False)
    # Unrelated flags survive the edit.
    assert args[args.index("--api-auth") + 1] == "true"
    assert "--disable-audio" not in args
    assert args.count("--disable-vision") == 1


def test_save_capture_settings_is_idempotent():
    args = _capture_argv(
        _argv("--disable-audio --disable-vision"),
        audio_mode="off",
        screen_enabled=False,
    )

    assert args.count("--disable-audio") == 1
    assert args.count("--disable-vision") == 1


def test_save_capture_settings_supports_both_default_sources():
    args = _capture_argv(
        _argv("--disable-audio"), audio_mode="both", screen_enabled=True
    )

    assert _capture_settings_from_argv(args) == ("both", True)
    assert args[args.index("--use-system-default-audio") + 1] == "true"


def test_save_capture_settings_supports_microphone_only():
    args = _capture_argv(
        _argv(),
        audio_mode="mic",
        screen_enabled=True,
        audio_devices=["Built-in Mic (input)", "Speakers (output)"],
    )

    assert _capture_settings_from_argv(args) == ("mic", True)
    # A device name with spaces stays one argv element — no shell quoting needed.
    assert args[args.index("--audio-device") + 1] == "Built-in Mic (input)"


def test_audio_forwarding_setting_is_independent_from_capture_unit(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"source_id": "capture-node", "forward_audio": "both"}')

    _save_forward_audio_setting("output", path)

    assert _forward_audio_setting(path) == "output"
    assert json.loads(path.read_text())["source_id"] == "capture-node"


def test_audio_source_modes_round_trip():
    assert _audio_sources("both") == {"system", "mic"}
    assert _audio_sources("input", forwarding=True) == {"mic"}
    assert _audio_modes({"system", "mic"}, {"system"}) == ("both", "output")


def test_forwarding_a_source_that_is_not_recorded_is_rejected():
    with pytest.raises(ValueError):
        _audio_modes({"mic"}, {"system", "mic"})


def test_switching_audio_off_stops_forwarding_too():
    assert _toggled_audio_modes("both", "both", False) == ("off", "none")


def test_switching_audio_back_on_restores_the_remembered_sources():
    assert _toggled_audio_modes("off", "none", True, ("mic", "input")) == (
        "mic",
        "input",
    )


def test_switching_audio_on_without_a_memory_enables_every_source():
    assert _toggled_audio_modes("off", "none", True) == ("both", "both")


def test_switching_audio_on_when_it_is_already_on_changes_nothing():
    assert _toggled_audio_modes("mic", "none", True, ("both", "both")) == (
        "mic",
        "none",
    )
