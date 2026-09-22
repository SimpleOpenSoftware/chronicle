#!/usr/bin/env python3
"""Guided host-native setup for a Chronicle ScreenPipe capture node."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()
PROJECT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT.parents[1]
sys.path.insert(0, str(REPO_ROOT))

import clients  # noqa: E402  (needs REPO_ROOT on sys.path)


def screenpipe_command() -> str | None:
    return shutil.which("screenpipe")


def list_audio_devices(binary: str) -> list[str]:
    result = subprocess.run(
        [binary, "audio", "list", "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [entry["name"] for entry in json.loads(result.stdout)["data"]]


def audio_arguments(mode: str, devices: list[str]) -> list[str]:
    if mode == "off":
        return ["--disable-audio"]
    if mode == "both":
        return ["--use-system-default-audio", "true"]
    suffix = "(output)" if mode == "system" else "(input)"
    matches = [device for device in devices if device.lower().endswith(suffix)]
    if not matches:
        raise ValueError(f"no {mode} audio device is available")
    return ["--use-system-default-audio", "false", "--audio-device", matches[0]]


def recorder_argv(
    binary: str,
    audio_mode: str = "both",
    devices: list[str] | None = None,
) -> list[str]:
    args = [
        binary,
        "record",
        "--audio-transcription-engine",
        "disabled",
        "--use-all-monitors",
        "true",
        # Leave scheduling headroom below privacy screening's 30-second gap
        # limit. Pin the cadence across recorder power-profile transitions.
        "--idle-capture-interval-ms",
        "20000",
        "--use-pii-removal",
        "true",
        "--disable-keyboard-capture",
        "--disable-clipboard-capture",
        "--prioritize-input-latency",
        "--pause-on-drm-content",
        # The meeting detector stays ON: on macOS/Windows it persists meeting
        # bounds (with titles) that the companion mirrors into Chronicle.
        "--disable-telemetry",
        "--video-quality",
        "balanced",
        "--retention-days",
        "90",
        "--retention-mode",
        "media",
        "--api-auth",
        "true",
    ]
    args.extend(audio_arguments(audio_mode, devices or []))
    return args


def install_recorder(
    binary: str,
    api_key: str,
    audio_mode: str = "both",
    devices: list[str] | None = None,
) -> None:
    """Install the recorder as a user service (systemd unit / launchd agent)."""
    clients.write_component_spec(
        "screenpipe",
        recorder_argv(binary, audio_mode, devices),
        {"SCREENPIPE_API_KEY": api_key},
    )
    clients.install_component("screenpipe")


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure a Chronicle capture node")
    parser.add_argument("--backend")
    args = parser.parse_args()

    console.print("\n🖥️  [bold cyan]Chronicle capture node[/bold cyan]")
    binary = screenpipe_command()
    if not binary:
        console.print(
            "[red]✗ ScreenPipe is not installed.[/red] Install it independently, verify "
            "[cyan]screenpipe record --help[/cyan], then rerun this wizard."
        )
        raise SystemExit(1)
    console.print(f"[green]✅[/green] ScreenPipe detected: [cyan]{binary}[/cyan]")

    backend = Prompt.ask(
        "Chronicle backend URL", default=args.backend or "http://127.0.0.1:8000"
    )
    devices = list_audio_devices(binary)
    audio_mode = Prompt.ask(
        "Local audio capture",
        choices=("off", "system", "mic", "both"),
        default="system",
    )
    forward_default = (
        "none"
        if audio_mode == "off"
        else "output" if audio_mode == "system" else audio_mode.replace("mic", "input")
    )
    forward_audio = Prompt.ask(
        "Audio sent to Chronicle",
        choices=("none", "output", "input", "both"),
        default=forward_default,
    )
    console.print(
        "Open Chronicle → Timeline → Sources and create a pairing code. "
        "The code expires after 10 minutes."
    )
    code = Prompt.ask("Pairing code").strip()
    if not code:
        raise SystemExit("pairing code is required")

    api_key = secrets.token_urlsafe(32)
    run(
        "uv",
        "run",
        "--project",
        str(PROJECT),
        "chronicle-screenpipe",
        "pair",
        "--backend",
        backend,
        "--code",
        code,
        "--screenpipe-dir",
        str(Path.home() / ".screenpipe"),
        "--screenpipe-url",
        "http://127.0.0.1:3030",
        "--screenpipe-token",
        api_key,
        "--forward-audio",
        forward_audio,
    )
    install_recorder(binary, api_key, audio_mode, devices)
    run(
        "uv",
        "run",
        "--project",
        str(PROJECT),
        "chronicle-screenpipe",
        "install-service",
    )
    clients.component_action("screenpipe-collector", "restart")

    if Confirm.ask("Check service status now?", default=True):
        for component in ("screenpipe", "screenpipe-collector"):
            status = clients.component_status(component)
            state = (
                "[green]active[/green]"
                if status["active"]
                else (
                    "[red]not running[/red]" if status["installed"] else "not installed"
                )
            )
            if component == "screenpipe" and status.get("detail"):
                state += f" — {status['detail']}"
            console.print(f"  {status['description']}: {state}")

    console.print(
        "\n[green]✅ Capture node connected.[/green] Activity should appear in "
        "Chronicle Timeline after ScreenPipe records a stable app/window span."
    )
    if clients.IS_MACOS:
        console.print(
            "\n[yellow]macOS:[/yellow] grant [cyan]Screen & System Audio Recording[/cyan] "
            "and [cyan]Accessibility[/cyan] to the ScreenPipe binary in System Settings → "
            "Privacy & Security (plus [cyan]Microphone[/cyan] if capturing input audio). "
            "A launchd agent cannot raise those prompts itself — until they are granted "
            "the recorder starts but captures nothing. After granting, restart it from "
            "the tray or with:\n  [cyan]launchctl kickstart -k "
            f"gui/$(id -u)/{clients.CLIENT_COMPONENTS['screenpipe']['label']}[/cyan]"
        )


if __name__ == "__main__":
    main()
