#!/usr/bin/env python3
"""Chronicle Hermes Wake-Word Service setup.

Configures the standalone acoustic wake-word detector. Writes a `.env` from the
template (preserving existing values) and warns if the trained model is missing.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from chronicle_setup import read_env_value
from dotenv import set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()
HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
ENV_TEMPLATE = HERE / ".env.template"
MODEL_PATH = HERE / "models" / "hey_hermes.onnx"


def resolve_hf_token(arg_token: str | None) -> str | None:
    """HF token, in priority order: --hf-token arg, backend .env, repo-root .env,
    this service's own .env.

    Mirrors how the wizard sources shared secrets: ``backend/.env`` is the
    canonical hub on a main machine; the repo-root ``.env`` is the per-node store for
    backend-less cluster-join nodes.
    """
    if arg_token:
        return arg_token
    repo_root = HERE.parent.parent
    for path in (
        repo_root / "backend" / ".env",
        repo_root / ".env",
        ENV_PATH,
    ):
        value = read_env_value(str(path), "HF_TOKEN")
        if value:
            return value
    return None


def configure(non_interactive: bool = False, hf_token: str | None = None) -> None:
    """Create/update .env and report model status."""
    console.print(
        Panel.fit(
            "Hermes Acoustic Wake-Word Service",
            subtitle="standalone detector on the live audio stream",
        )
    )

    if not ENV_PATH.exists():
        if ENV_TEMPLATE.exists():
            shutil.copy(ENV_TEMPLATE, ENV_PATH)
            console.print(f"[green]Created {ENV_PATH} from template[/green]")
        else:
            ENV_PATH.touch()

    defaults = {
        "REDIS_URL": "redis://redis:6379/0",
        # Host 8770 is used by the tts/kittentts service; wakeword maps to host 8771.
        "WAKEWORD_PORT": "8771",
        "WAKEWORD_THRESHOLD": "0.9",
        "WAKEWORD_PATIENCE": "2",
        "WAKEWORD_DEBOUNCE_SECS": "3.0",
        "WAKEWORD_VAD_THRESHOLD": "0.5",
        "WAKEWORD_STOP_SECS": "2.0",
        "WAKEWORD_MAX_ARM_SECS": "15.0",
        "LOG_LEVEL": "INFO",
    }

    for key, default in defaults.items():
        existing = read_env_value(str(ENV_PATH), key)
        if non_interactive:
            value = existing or default
        else:
            value = Prompt.ask(key, default=existing or default)
        set_key(str(ENV_PATH), key, value, quote_mode="never")

    # HF token (optional): persisted so it's available if a wake-word backend pulls
    # gated HuggingFace weights. The bundled HuBERT-base is cached at build time from
    # the PyTorch CDN, so this isn't exercised today, but keeps the plumbing uniform.
    resolved_token = resolve_hf_token(hf_token)
    if resolved_token:
        set_key(str(ENV_PATH), "HF_TOKEN", resolved_token, quote_mode="never")

    console.print(f"[green]Wrote configuration to {ENV_PATH}[/green]")

    if not MODEL_PATH.exists():
        console.print(
            Panel.fit(
                f"[yellow]Wake-word model not found at {MODEL_PATH}.[/yellow]\n"
                "Train it first:\n"
                "  cd training && ./fetch_datasets.sh\n"
                "  .venv-train/bin/nanowakeword -c hermes_config.yaml\n"
                "  cp trained_models/hermes/model/hermes.onnx ../models/hermes.onnx\n"
                "The service will refuse to start until hermes.onnx is present.",
                title="Model required",
            )
        )
    else:
        console.print(f"[green]Wake-word model present: {MODEL_PATH}[/green]")

    console.print("\n[bold]Next:[/bold] start from the repository root:")
    console.print("  ./services start wakeword-service --build")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes wake-word service setup")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults / existing values without prompting.",
    )
    parser.add_argument(
        "--hf-token",
        help="Hugging Face token (avoids HF rate-limits / unlocks gated repos)",
    )
    args = parser.parse_args()
    configure(
        non_interactive=args.non_interactive or not sys.stdin.isatty(),
        hf_token=args.hf_token,
    )


if __name__ == "__main__":
    main()
