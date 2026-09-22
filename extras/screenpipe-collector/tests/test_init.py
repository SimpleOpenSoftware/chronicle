import importlib.util
from pathlib import Path

INIT_PATH = Path(__file__).parents[1] / "init.py"
SPEC = importlib.util.spec_from_file_location("screenpipe_collector_init", INIT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _flag(argv, option):
    return argv[argv.index(option) + 1]


def test_recorder_argv_uses_privacy_defaults_and_api_auth():
    argv = MODULE.recorder_argv(
        "/usr/bin/screenpipe",
        "system",
        ["Speakers (output)"],
    )
    assert argv[:2] == ["/usr/bin/screenpipe", "record"]
    assert _flag(argv, "--audio-transcription-engine") == "disabled"
    assert _flag(argv, "--idle-capture-interval-ms") == "20000"
    assert "--disable-keyboard-capture" in argv
    assert "--disable-clipboard-capture" in argv
    assert _flag(argv, "--api-auth") == "true"
    assert _flag(argv, "--use-system-default-audio") == "false"
    # One argv element, so a device name with spaces needs no quoting.
    assert _flag(argv, "--audio-device") == "Speakers (output)"


def test_install_recorder_saves_argv_and_api_key_to_the_component_spec(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        MODULE.clients,
        "write_component_spec",
        lambda name, argv, env: saved.update(name=name, argv=list(argv), env=dict(env)),
    )
    monkeypatch.setattr(
        MODULE.clients, "install_component", lambda name: saved.update(installed=name)
    )

    MODULE.install_recorder("/usr/bin/screenpipe", "local-key", "both", [])

    assert saved["name"] == "screenpipe"
    assert saved["installed"] == "screenpipe"
    assert saved["env"] == {"SCREENPIPE_API_KEY": "local-key"}
    assert _flag(saved["argv"], "--use-system-default-audio") == "true"
    assert _flag(saved["argv"], "--idle-capture-interval-ms") == "20000"


def test_audio_arguments_keep_microphone_and_system_independent():
    devices = ["Mic (input)", "Speakers (output)"]

    assert MODULE.audio_arguments("system", devices)[-1] == "Speakers (output)"
    assert MODULE.audio_arguments("mic", devices)[-1] == "Mic (input)"
    assert MODULE.audio_arguments("both", devices) == [
        "--use-system-default-audio",
        "true",
    ]
