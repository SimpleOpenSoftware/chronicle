#!/usr/bin/env python3
"""
Chronicle Backend Interactive Setup Script
Interactive configuration for all services and API keys
"""

import argparse
import json
import os
import platform
import secrets
import shutil
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from chronicle_setup import (
    ConfigManager,
    decide_cert_mode,
    detect_lan_ip,
    detect_tailscale_info,
    list_tailnet_peers,
    mask_value,
)
from chronicle_setup import prompt_password as util_prompt_password
from chronicle_setup import (
    prompt_with_existing_masked,
    read_env_value,
    tailscale_socket_path,
    update_caddy_socket_override,
)
from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text
from ruamel.yaml import YAML

# Anchored to this file, not the working directory: setup runs from the
# repository root so that setup-requirements.txt resolves, but every path this
# script reads or writes belongs to the backend's own directory.
SERVICE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_DIR.parent

# Match the Pi executor's conservative fallbacks without baking one machine's
# served context into every model selected by the wizard.
MANAGED_LLAMACPP_REGISTRY_MODELS = {"llamacpp-llm", "muse-glimmer-llm"}


class ChronicleSetup:
    def __init__(self, args=None):
        self.console = Console()
        self.config: Dict[str, Any] = {}
        self.args = args or argparse.Namespace()
        self.config_yml_path = REPO_ROOT / "config" / "config.yml"

        if not (SERVICE_DIR / "pyproject.toml").exists():
            self.console.print(
                f"[red][ERROR][/red] {SERVICE_DIR} does not look like the backend "
                "(no pyproject.toml)"
            )
            sys.exit(1)

        # Initialize ConfigManager (single source of truth for config.yml)
        self.config_manager = ConfigManager(service_path="backend")
        self.console.print(
            f"[blue][INFO][/blue] Using config.yml at: {self.config_manager.config_yml_path}"
        )

        # Verify config.yml exists - fail fast if missing
        if not self.config_manager.config_yml_path.exists():
            self.console.print(
                f"[red][ERROR][/red] config.yml not found at {self.config_manager.config_yml_path}"
            )
            self.console.print(
                "[red][ERROR][/red] Run wizard.py from project root to create config.yml"
            )
            sys.exit(1)

        # Ensure plugins.yml exists (copy from template if missing)
        self._ensure_plugins_yml_exists()

    def print_header(self, title: str):
        """Print a colorful header"""
        self.console.print()
        panel = Panel(Text(title, style="cyan bold"), style="cyan", expand=False)
        self.console.print(panel)
        self.console.print()

    def print_section(self, title: str):
        """Print a section header"""
        self.console.print()
        self.console.print(f"[magenta]► {title}[/magenta]")
        self.console.print("[magenta]" + "─" * len(f"► {title}") + "[/magenta]")

    def prompt_value(self, prompt: str, default: str = "") -> str:
        """Prompt for a value with optional default"""
        try:
            # Always provide a default to avoid EOF issues
            return Prompt.ask(prompt, default=default)
        except EOFError:
            self.console.print(f"Using default: {default}")
            return default

    def prompt_password(self, prompt: str) -> str:
        """Prompt for password (delegates to shared utility)"""
        return util_prompt_password(prompt, min_length=8, allow_generated=True)

    def prompt_choice(
        self, prompt: str, choices: Dict[str, str], default: str = "1"
    ) -> str:
        """Prompt for a choice from options"""
        self.console.print(prompt)
        for key, desc in choices.items():
            self.console.print(f"  {key}) {desc}")
        self.console.print()

        while True:
            try:
                choice = Prompt.ask("Enter choice", default=default)
                if choice in choices:
                    return choice
                self.console.print(
                    f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
                )
            except EOFError:
                self.console.print(f"Using default choice: {default}")
                return default

    def _ensure_plugins_yml_exists(self):
        """Ensure plugins.yml exists by copying from template if missing."""
        plugins_yml = REPO_ROOT / "config" / "plugins.yml"
        plugins_template = REPO_ROOT / "config" / "plugins.yml.template"

        if not plugins_yml.exists():
            if plugins_template.exists():
                self.console.print(
                    "[blue][INFO][/blue] plugins.yml not found, creating from template..."
                )
                shutil.copy2(plugins_template, plugins_yml)
                self.console.print(
                    f"[green]✅[/green] Created {plugins_yml} from template"
                )
                self.console.print(
                    "[yellow][NOTE][/yellow] Edit config/plugins.yml to configure plugins"
                )
                self.console.print(
                    "[yellow][NOTE][/yellow] Set HA_TOKEN in .env for Home Assistant integration"
                )
            else:
                raise RuntimeError(
                    f"Template file not found: {plugins_template}\n"
                    f"The repository structure is incomplete. Please ensure config/plugins.yml.template exists."
                )
        else:
            self.console.print(f"[blue][INFO][/blue] Found existing {plugins_yml}")

    def backup_existing_env(self):
        """Backup existing .env file"""
        env_path = SERVICE_DIR / ".env"
        if env_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = SERVICE_DIR / f".env.backup.{timestamp}"
            shutil.copy2(env_path, backup_path)
            self.console.print(
                f"[blue][INFO][/blue] Backed up existing .env file to {backup_path}"
            )

    def read_existing_env_value(self, key: str) -> str:
        """Read a value from existing .env file (delegates to shared utility)"""
        return read_env_value(str(SERVICE_DIR / ".env"), key)

    def mask_api_key(self, key: str, show_chars: int = 5) -> str:
        """Mask API key (delegates to shared utility)"""
        return mask_value(key, show_chars)

    def prompt_with_existing_masked(
        self,
        prompt_text: str,
        env_key: str,
        placeholders: list,
        is_password: bool = False,
        default: str = "",
    ) -> str:
        """
        Prompt for a value, showing masked existing value from .env if present.
        Delegates to shared utility from chronicle_setup.

        Args:
            prompt_text: The prompt to display
            env_key: The .env key to check for existing value
            placeholders: List of placeholder values to treat as "not set"
            is_password: Whether to mask the value (for passwords/tokens)
            default: Default value if no existing value

        Returns:
            User input value, existing value if reused, or default
        """
        # Use shared utility with auto-read from .env
        return prompt_with_existing_masked(
            prompt_text=prompt_text,
            env_file_path=str(SERVICE_DIR / ".env"),
            env_key=env_key,
            placeholders=placeholders,
            is_password=is_password,
            default=default,
        )

    def setup_authentication(self):
        """Configure authentication settings"""
        self.print_section("Authentication Setup")
        self.console.print("Configure admin account for the dashboard")
        self.console.print()

        # Read existing values for re-run support
        existing_email = self.read_existing_env_value("ADMIN_EMAIL")
        default_email = existing_email if existing_email else "admin@example.com"
        self.config["ADMIN_EMAIL"] = self.prompt_value("Admin email", default_email)

        # Allow reusing existing admin password
        existing_password = self.read_existing_env_value("ADMIN_PASSWORD")
        if existing_password:
            password = prompt_with_existing_masked(
                prompt_text="Admin password (min 8 chars)",
                existing_value=existing_password,
                is_password=True,
            )
            self.config["ADMIN_PASSWORD"] = password
        else:
            self.config["ADMIN_PASSWORD"] = self.prompt_password(
                "Admin password (min 8 chars)"
            )

        # Preserve existing AUTH_SECRET_KEY to avoid invalidating JWTs
        existing_secret = self.read_existing_env_value("AUTH_SECRET_KEY")
        if existing_secret:
            self.config["AUTH_SECRET_KEY"] = existing_secret
            self.console.print(
                "[blue][INFO][/blue] Reusing existing AUTH_SECRET_KEY (existing JWT tokens remain valid)"
            )
        else:
            self.config["AUTH_SECRET_KEY"] = secrets.token_hex(32)

        self.console.print("[green][SUCCESS][/green] Admin account configured")

    def _asr_url_for(
        self, env_key: str, default: str = "http://host.docker.internal:8767"
    ):
        """Resolve an offline ASR provider's URL env value from the wizard's source choice.

        - --asr-discover  → '' (left empty so the backend discovers chronicle-asr on
          the Tailnet live — 'configure from the Tailnet later')
        - --asr-url <url> → that URL (own / picked-from-Tailnet endpoint)
        - otherwise       → prompt (interactive standalone run), defaulting to local
        """
        if getattr(self.args, "asr_discover", False):
            self.console.print(
                f"[blue][INFO][/blue] {env_key} left empty — backend will discover "
                "chronicle-asr on the Tailnet at runtime"
            )
            return ""
        if getattr(self.args, "asr_url", None):
            self.console.print(f"[green]✅[/green] {env_key} = {self.args.asr_url}")
            return self.args.asr_url
        existing = read_env_value(str(SERVICE_DIR / ".env"), env_key) or default
        return self.prompt_value(f"{env_key}", existing)

    def setup_transcription(self):
        """Configure transcription provider - updates config.yml and .env"""
        # Check if transcription provider was provided via command line
        if (
            hasattr(self.args, "transcription_provider")
            and self.args.transcription_provider
        ):
            provider = self.args.transcription_provider
            self.console.print(
                f"[green]✅[/green] Transcription: {provider} (configured via wizard)"
            )

            # Map provider to choice
            if provider == "deepgram":
                choice = "1"
            elif provider == "parakeet":
                choice = "2"
            elif provider == "vibevoice":
                choice = "3"
            elif provider == "qwen3-asr":
                choice = "4"
            elif provider == "smallest":
                choice = "5"
            elif provider == "gemma4":
                choice = "6"
            elif provider == "af-next":
                choice = "7"
            elif provider == "granite":
                choice = "8"
            elif provider == "none":
                choice = "9"
            else:
                choice = "1"  # Default to Deepgram
        else:
            self.print_section("Speech-to-Text Configuration")

            self.console.print(
                "[blue][INFO][/blue] Provider selection is configured in config.yml (defaults.stt)"
            )
            self.console.print("[blue][INFO][/blue] API keys are stored in .env")
            self.console.print()

            # Interactive prompt
            is_macos = platform.system() == "Darwin"

            if is_macos:
                parakeet_desc = "Offline (Parakeet ASR - CPU-based, runs locally)"
                vibevoice_desc = "Offline (VibeVoice - CPU-based, built-in diarization)"
            else:
                parakeet_desc = "Offline (Parakeet ASR - GPU recommended, runs locally)"
                vibevoice_desc = (
                    "Offline (VibeVoice - GPU recommended, built-in diarization)"
                )

            qwen3_desc = (
                "Offline (Qwen3-ASR - GPU required, 52 languages, streaming + batch)"
            )

            smallest_desc = "Smallest.ai Pulse (cloud-based, fast, requires API key)"

            gemma4_desc = (
                "Offline (Gemma 4 E2B-it - GPU required, prompt-based diarization)"
            )

            af_next_desc = (
                "Offline (Audio Flamingo Next - GPU required, timestamped diarization; "
                "NONCOMMERCIAL license)"
            )

            granite_desc = (
                "Offline (IBM Granite Speech - GPU recommended, LLM-backbone; "
                "en/fr/de/es/pt)"
            )

            choices = {
                "1": "Deepgram (recommended - high quality, cloud-based)",
                "2": parakeet_desc,
                "3": vibevoice_desc,
                "4": qwen3_desc,
                "5": smallest_desc,
                "6": gemma4_desc,
                "7": af_next_desc,
                "8": granite_desc,
                "9": "None (skip transcription setup)",
            }

            choice = self.prompt_choice(
                "Choose your transcription provider:", choices, "1"
            )

        if choice == "1":
            self.console.print("[blue][INFO][/blue] Deepgram selected")
            self.console.print("Get your API key from: https://console.deepgram.com/")

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="Deepgram API key (leave empty to skip)",
                env_key="DEEPGRAM_API_KEY",
                placeholders=["your_deepgram_api_key_here", "your-deepgram-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                # Write API key to .env
                self.config["DEEPGRAM_API_KEY"] = api_key

                # Update config.yml to use Deepgram
                self.config_manager.update_config_defaults({"stt": "stt-deepgram"})

                self.console.print(
                    "[green][SUCCESS][/green] Deepgram configured in config.yml and .env"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-deepgram")
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - transcription will not work"
                )

        elif choice == "2":
            self.console.print("[blue][INFO][/blue] Offline Parakeet ASR selected")
            # Write URL to .env for ${PARAKEET_ASR_URL} placeholder in config.yml.
            # Empty ("" from --asr-discover) → runtime Tailnet discovery.
            self.config["PARAKEET_ASR_URL"] = self._asr_url_for("PARAKEET_ASR_URL")

            # Update config.yml to use Parakeet
            self.config_manager.update_config_defaults({"stt": "stt-parakeet-batch"})

            self.console.print(
                "[green][SUCCESS][/green] Parakeet configured in config.yml and .env"
            )
            self.console.print(
                "[blue][INFO][/blue] Set defaults.stt: stt-parakeet-batch"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Parakeet service: cd ../extras/asr-services && docker compose up nemo-asr"
            )

        elif choice == "3":
            self.console.print(
                "[blue][INFO][/blue] Offline VibeVoice ASR selected (built-in speaker diarization)"
            )
            existing_vibevoice_url = (
                read_env_value(str(SERVICE_DIR / ".env"), "VIBEVOICE_ASR_URL")
                or "http://host.docker.internal:8767"
            )
            vibevoice_url = self.prompt_value(
                "VibeVoice ASR URL", existing_vibevoice_url
            )

            # Write URL to .env for ${VIBEVOICE_ASR_URL} placeholder in config.yml
            self.config["VIBEVOICE_ASR_URL"] = vibevoice_url

            # Update config.yml to use VibeVoice
            self.config_manager.update_config_defaults({"stt": "stt-vibevoice"})

            self.console.print(
                "[green][SUCCESS][/green] VibeVoice configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-vibevoice")
            self.console.print(
                "[blue][INFO][/blue] VibeVoice provides built-in speaker diarization - pyannote will be skipped"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start VibeVoice service: cd ../extras/asr-services && docker compose up vibevoice-asr"
            )

        elif choice == "4":
            self.console.print(
                "[blue][INFO][/blue] Qwen3-ASR selected (52 languages, streaming + batch via vLLM)"
            )
            qwen3_url = self._asr_url_for("QWEN3_ASR_URL")
            # Stored without scheme (resolved_url re-adds it); empty → Tailnet discovery.
            self.config["QWEN3_ASR_URL"] = (
                qwen3_url.replace("http://", "").rstrip("/") if qwen3_url else ""
            )
            # Streaming companion (same host, port 8769); empty when discovering.
            if qwen3_url:
                stream_host = qwen3_url.replace("http://", "").split(":")[0]
                self.config["QWEN3_ASR_STREAM_URL"] = f"{stream_host}:8769"
            else:
                self.config["QWEN3_ASR_STREAM_URL"] = ""

            # Update config.yml to use Qwen3-ASR
            self.config_manager.update_config_defaults({"stt": "stt-qwen3-asr"})

            self.console.print(
                "[green][SUCCESS][/green] Qwen3-ASR configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-qwen3-asr")
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Qwen3-ASR: cd ../extras/asr-services && docker compose up qwen3-asr-wrapper qwen3-asr-bridge -d"
            )

        elif choice == "5":
            self.console.print("[blue][INFO][/blue] Smallest.ai Pulse selected")
            self.console.print("Get your API key from: https://smallest.ai/")

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="Smallest.ai API key (leave empty to skip)",
                env_key="SMALLEST_API_KEY",
                placeholders=["your_smallest_api_key_here", "your-smallest-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                # Write API key to .env
                self.config["SMALLEST_API_KEY"] = api_key

                # Update config.yml to use Smallest.ai (batch + streaming)
                self.config_manager.update_config_defaults(
                    {"stt": "stt-smallest", "stt_stream": "stt-smallest-stream"}
                )

                self.console.print(
                    "[green][SUCCESS][/green] Smallest.ai configured in config.yml and .env"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-smallest")
                self.console.print(
                    "[blue][INFO][/blue] Set defaults.stt_stream: stt-smallest-stream"
                )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - transcription will not work"
                )

        elif choice == "6":
            self.console.print(
                "[blue][INFO][/blue] Gemma 4 E2B-it selected (prompt-based diarization, batch + streaming)"
            )
            self.config["GEMMA4_ASR_URL"] = self._asr_url_for("GEMMA4_ASR_URL")

            # The same gemma4-asr service serves both batch (/transcribe) and
            # streaming (/stream), so enable both defaults at once.
            self.config_manager.update_config_defaults(
                {"stt": "stt-gemma4", "stt_stream": "stt-gemma4-stream"}
            )

            # Gemma 4 is an LLM-backbone ASR (capability "context_prompt"): it takes
            # free-form context, NOT acoustic keyword boosting. Unlike VibeVoice/
            # Deepgram, it would echo a wake-word boost list into the transcript, so
            # the backend withholds that list and uses this context string instead.
            existing_gemma4_context = (
                self.config_manager.get_full_config()
                .get("backend", {})
                .get("asr", {})
                .get("context", {})
                .get("stt-gemma4", "")
            )
            self.console.print(
                "[blue][INFO][/blue] Gemma 4 takes free-form context (domain, names, "
                "jargon) to disambiguate recognition. It informs transcription but is "
                "never transcribed. Leave blank to skip."
            )
            gemma4_context = self.prompt_value(
                "Gemma 4 ASR context (optional)", existing_gemma4_context
            )
            self.config_manager.update_backend_config(
                {"asr": {"context": {"stt-gemma4": gemma4_context.strip()}}}
            )

            self.console.print(
                "[green][SUCCESS][/green] Gemma 4 configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-gemma4")
            self.console.print(
                "[blue][INFO][/blue] Set defaults.stt_stream: stt-gemma4-stream"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Gemma 4 ASR: cd ../extras/asr-services && docker compose up gemma4-asr -d"
            )

        elif choice == "7":
            self.console.print(
                "[blue][INFO][/blue] Audio Flamingo Next selected "
                "(timestamped diarization, prompt-driven)"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] AF-Next is licensed under the NVIDIA OneWay "
                "Noncommercial License — research use only. Do not deploy in commercial "
                "products."
            )
            self.config["AF_NEXT_ASR_URL"] = self._asr_url_for("AF_NEXT_ASR_URL")

            self.config_manager.update_config_defaults({"stt": "stt-af-next"})

            self.console.print(
                "[green][SUCCESS][/green] Audio Flamingo Next configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-af-next")
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start AF-Next: "
                "cd ../extras/asr-services && docker compose up af-next-asr -d"
            )

        elif choice == "8":
            self.console.print(
                "[blue][INFO][/blue] IBM Granite Speech selected "
                "(LLM-backbone ASR; en/fr/de/es/pt)"
            )
            self.config["GRANITE_ASR_URL"] = self._asr_url_for("GRANITE_ASR_URL")

            self.config_manager.update_config_defaults({"stt": "stt-granite"})

            # Granite is an LLM-backbone ASR (capability "context_prompt"): it takes
            # free-form context, NOT acoustic keyword boosting. Like Gemma 4 it would
            # echo a wake-word boost list into the transcript, so the backend
            # withholds that list and uses this context string instead.
            existing_granite_context = (
                self.config_manager.get_full_config()
                .get("backend", {})
                .get("asr", {})
                .get("context", {})
                .get("stt-granite", "")
            )
            self.console.print(
                "[blue][INFO][/blue] Granite Speech takes free-form context (domain, "
                "names, jargon) to disambiguate recognition. It informs transcription "
                "but is never transcribed. Leave blank to skip."
            )
            granite_context = self.prompt_value(
                "Granite ASR context (optional)", existing_granite_context
            )
            self.config_manager.update_backend_config(
                {"asr": {"context": {"stt-granite": granite_context.strip()}}}
            )

            self.console.print(
                "[green][SUCCESS][/green] Granite Speech configured in config.yml and .env"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.stt: stt-granite")
            self.console.print(
                "[yellow][WARNING][/yellow] Remember to start Granite ASR: "
                "cd ../extras/asr-services && docker compose up granite-asr -d"
            )

        elif choice == "9":
            self.console.print("[blue][INFO][/blue] Skipping transcription setup")

    def setup_streaming_provider(self):
        """Configure a separate streaming provider if --streaming-provider was passed.

        When a different streaming provider is specified, sets defaults.stt_stream
        and enables always_batch_retranscribe (batch provider was set by setup_transcription).
        """
        if (
            not hasattr(self.args, "streaming_provider")
            or not self.args.streaming_provider
        ):
            return

        streaming_provider = self.args.streaming_provider
        self.console.print(
            f"\n[green]✅[/green] Streaming provider: {streaming_provider} (configured via wizard)"
        )

        # Map streaming provider to stt_stream config value
        provider_to_stt_stream = {
            "deepgram": "stt-deepgram-stream",
            "smallest": "stt-smallest-stream",
            "qwen3-asr": "stt-qwen3-asr",
            "gemma4": "stt-gemma4-stream",
            "nemotron": "stt-nemotron-stream",
        }

        stream_stt = provider_to_stt_stream.get(streaming_provider)
        if not stream_stt:
            self.console.print(
                f"[yellow][WARNING][/yellow] Unknown streaming provider: {streaming_provider}"
            )
            return

        # Set stt_stream (batch stt was already set by setup_transcription)
        self.config_manager.update_config_defaults({"stt_stream": stream_stt})

        # Enable always_batch_retranscribe
        full_config = self.config_manager.get_full_config()
        if "backend" not in full_config:
            full_config["backend"] = {}
        if "transcription" not in full_config["backend"]:
            full_config["backend"]["transcription"] = {}
        full_config["backend"]["transcription"]["always_batch_retranscribe"] = True
        self.config_manager.save_full_config(full_config)

        self.console.print(f"[blue][INFO][/blue] Set defaults.stt_stream: {stream_stt}")
        self.console.print(f"[blue][INFO][/blue] Enabled always_batch_retranscribe")

        # Prompt for streaming provider env vars if not already set
        if streaming_provider == "deepgram":
            existing_key = read_env_value(str(SERVICE_DIR / ".env"), "DEEPGRAM_API_KEY")
            if not existing_key or existing_key in (
                "your_deepgram_api_key_here",
                "your-deepgram-key-here",
            ):
                api_key = self.prompt_with_existing_masked(
                    prompt_text="Deepgram API key for streaming",
                    env_key="DEEPGRAM_API_KEY",
                    placeholders=[
                        "your_deepgram_api_key_here",
                        "your-deepgram-key-here",
                    ],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["DEEPGRAM_API_KEY"] = api_key
            else:
                # Preserve existing key so generate_env_file() doesn't lose it
                self.config["DEEPGRAM_API_KEY"] = existing_key
        elif streaming_provider == "smallest":
            existing_key = read_env_value(str(SERVICE_DIR / ".env"), "SMALLEST_API_KEY")
            if not existing_key or existing_key in (
                "your_smallest_api_key_here",
                "your-smallest-key-here",
            ):
                api_key = self.prompt_with_existing_masked(
                    prompt_text="Smallest.ai API key for streaming",
                    env_key="SMALLEST_API_KEY",
                    placeholders=[
                        "your_smallest_api_key_here",
                        "your-smallest-key-here",
                    ],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["SMALLEST_API_KEY"] = api_key
            else:
                # Preserve existing key so generate_env_file() doesn't lose it
                self.config["SMALLEST_API_KEY"] = existing_key
        elif streaming_provider == "qwen3-asr":
            existing_url = read_env_value(
                str(SERVICE_DIR / ".env"), "QWEN3_ASR_STREAM_URL"
            )
            if not existing_url:
                qwen3_url = self.prompt_value(
                    "Qwen3-ASR streaming URL", "http://host.docker.internal:8769"
                )
                stream_host = qwen3_url.replace("http://", "").rstrip("/")
                self.config["QWEN3_ASR_STREAM_URL"] = stream_host
        elif streaming_provider == "gemma4":
            # Streaming shares the gemma4-asr service (the /stream WS endpoint).
            existing_url = read_env_value(str(SERVICE_DIR / ".env"), "GEMMA4_ASR_URL")
            if not existing_url:
                gemma4_url = self.prompt_value(
                    "Gemma 4 ASR URL", "host.docker.internal:8767"
                )
                self.config["GEMMA4_ASR_URL"] = gemma4_url.replace(
                    "http://", ""
                ).rstrip("/")
        elif streaming_provider == "nemotron":
            # Nemotron serves batch + streaming from one container on 8772.
            existing_url = read_env_value(
                str(SERVICE_DIR / ".env"), "NEMOTRON_ASR_STREAM_URL"
            )
            if not existing_url:
                nemotron_url = self.prompt_value(
                    "Nemotron ASR URL", "host.docker.internal:8772"
                )
                self.config["NEMOTRON_ASR_STREAM_URL"] = nemotron_url.replace(
                    "http://", ""
                ).rstrip("/")

    def setup_live_segmentation(self):
        """Configure the live transcription path (defaults.live_segmentation).

        Writes "windowed_batch" when the wizard selected it (no streaming ASR), so the
        windowed-batch worker transcribes fixed windows of streamed audio. Defaults to
        "streaming_stt" otherwise.
        """
        mode = getattr(self.args, "live_segmentation", None)
        if not mode:
            return

        self.config_manager.update_config_defaults({"live_segmentation": mode})
        self.console.print(
            f"[blue][INFO][/blue] Set defaults.live_segmentation: {mode}"
        )
        if mode == "windowed_batch":
            self.console.print(
                "[blue][INFO][/blue] Continuous audio will be transcribed in windows "
                "(no streaming ASR required)"
            )

    def setup_llm(self):
        """Configure LLM provider - updates config.yml and .env"""
        # Check if LLM provider was provided via command line (from wizard.py)
        if hasattr(self.args, "llm_provider") and self.args.llm_provider:
            provider = self.args.llm_provider
            self.console.print(
                f"[green]✅[/green] LLM provider: {provider} (configured via wizard)"
            )
            choice = {
                "openai": "1",
                "ollama": "2",
                "none": "4",
                "llamacpp": "5",
                "custom": "3",
                "gemma4-unified": "6",
            }.get(provider, "1")
        else:
            # Standalone init.py run — read existing config as default
            existing_choice = "1"
            full_config = self.config_manager.get_full_config()
            existing_llm = full_config.get("defaults", {}).get("llm", "")
            if existing_llm == "local-llm":
                existing_choice = "2"
            elif existing_llm == "openai-llm":
                existing_choice = "1"
            elif existing_llm in MANAGED_LLAMACPP_REGISTRY_MODELS:
                existing_choice = "5"

            self.print_section("LLM Provider Configuration")
            self.console.print(
                "[blue][INFO][/blue] LLM configuration will be saved to config.yml"
            )
            self.console.print()

            choices = {
                "1": "OpenAI (GPT-4, GPT-3.5 - requires API key)",
                "2": "Ollama (local models - runs locally)",
                "3": "OpenAI-Compatible custom endpoint",
                "4": "Skip (no memory extraction)",
                "5": "llama.cpp (Chronicle-managed, local GGUF models)",
            }

            choice = self.prompt_choice(
                "Which LLM provider will you use?", choices, existing_choice
            )

        if choice == "1":
            self.console.print("[blue][INFO][/blue] OpenAI selected")
            self.console.print(
                "Get your API key from: https://platform.openai.com/api-keys"
            )

            # Use the new masked prompt function
            api_key = self.prompt_with_existing_masked(
                prompt_text="OpenAI API key (leave empty to skip)",
                env_key="OPENAI_API_KEY",
                placeholders=["your_openai_api_key_here", "your-openai-key-here"],
                is_password=True,
                default="",
            )

            if api_key:
                self.config["OPENAI_API_KEY"] = api_key
                # Update config.yml to use OpenAI models
                self.config_manager.update_config_defaults(
                    {"llm": "openai-llm", "embedding": "openai-embed"}
                )
                self.console.print(
                    "[green][SUCCESS][/green] OpenAI configured in config.yml"
                )
                self.console.print("[blue][INFO][/blue] Set defaults.llm: openai-llm")
                self.console.print(
                    "[blue][INFO][/blue] Set defaults.embedding: openai-embed"
                )
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No API key provided - memory extraction will not work"
                )

        elif choice == "2":
            self.console.print("[blue][INFO][/blue] Ollama selected")
            # Update config.yml to use Ollama models
            self.config_manager.update_config_defaults(
                {"llm": "local-llm", "embedding": "local-embed"}
            )
            self.console.print(
                "[green][SUCCESS][/green] Ollama configured in config.yml"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.llm: local-llm")
            self.console.print(
                "[blue][INFO][/blue] Set defaults.embedding: local-embed"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Make sure Ollama is running and models are pulled"
            )

        elif choice == "3":
            self.console.print(
                "[blue][INFO][/blue] OpenAI-Compatible custom endpoint selected"
            )
            self.console.print(
                "This works with any provider that exposes an OpenAI-compatible API"
            )
            self.console.print("(e.g., Groq, Together AI, LM Studio, vLLM, etc.)")
            self.console.print()

            # Prompt for base URL (required)
            base_url = self.prompt_value(
                "API Base URL (e.g., https://api.groq.com/openai/v1)", ""
            )
            if not base_url:
                self.console.print(
                    "[yellow][WARNING][/yellow] No base URL provided - skipping custom LLM setup"
                )
            else:
                # Prompt for API key
                api_key = self.prompt_with_existing_masked(
                    prompt_text="API Key (leave empty if not required)",
                    env_key="CUSTOM_LLM_API_KEY",
                    placeholders=["your_custom_llm_api_key_here"],
                    is_password=True,
                    default="",
                )
                if api_key:
                    self.config["CUSTOM_LLM_API_KEY"] = api_key

                # Prompt for model name (required)
                model_name = self.prompt_value(
                    "LLM Model name (e.g., llama-3.1-70b-versatile)", ""
                )
                if not model_name:
                    self.console.print(
                        "[yellow][WARNING][/yellow] No model name provided - skipping custom LLM setup"
                    )
                else:
                    # Create LLM model entry
                    llm_model = {
                        "name": "custom-llm",
                        "description": "Custom OpenAI-compatible LLM",
                        "model_type": "llm",
                        "model_provider": "openai",
                        "api_family": "openai",
                        "model_name": model_name,
                        "model_url": base_url,
                        "api_key": "${oc.env:CUSTOM_LLM_API_KEY,''}",
                        "model_params": {"temperature": 0.2, "max_tokens": 2000},
                        "model_output": "json",
                    }
                    self.config_manager.add_or_update_model(llm_model)

                    # Prompt for optional embedding model
                    embedding_model_name = self.prompt_value(
                        "Embedding model name (leave empty to use Ollama local-embed)",
                        "",
                    )

                    if embedding_model_name:
                        embed_dim_str = self.prompt_value(
                            "Embedding dimensions (e.g. 1536 for text-embedding-3-small, 3072 for text-embedding-3-large)",
                            "1536",
                        )
                        try:
                            embedding_dimensions = int(embed_dim_str)
                        except ValueError:
                            self.console.print(
                                f"[yellow][WARNING][/yellow] Invalid dimensions '{embed_dim_str}', using default 1536"
                            )
                            raise ValueError(f"Invalid dimensions '{embed_dim_str}'")

                        embed_model = {
                            "name": "custom-embed",
                            "description": "Custom OpenAI-compatible embeddings",
                            "model_type": "embedding",
                            "model_provider": "openai",
                            "api_family": "openai",
                            "model_name": embedding_model_name,
                            "model_url": base_url,
                            "api_key": "${oc.env:CUSTOM_LLM_API_KEY,''}",
                            "embedding_dimensions": embedding_dimensions,
                            "model_output": "vector",
                        }
                        self.config_manager.add_or_update_model(embed_model)
                        self.config_manager.update_config_defaults(
                            {"llm": "custom-llm", "embedding": "custom-embed"}
                        )
                        self.console.print(
                            "[green][SUCCESS][/green] Custom LLM and embedding configured in config.yml"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.llm: custom-llm"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.embedding: custom-embed"
                        )
                    else:
                        self.config_manager.update_config_defaults(
                            {"llm": "custom-llm", "embedding": "local-embed"}
                        )
                        self.console.print(
                            "[green][SUCCESS][/green] Custom LLM configured in config.yml"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.llm: custom-llm"
                        )
                        self.console.print(
                            "[blue][INFO][/blue] Set defaults.embedding: local-embed (Ollama)"
                        )
                        self.console.print(
                            "[yellow][WARNING][/yellow] Make sure Ollama is running for embeddings"
                        )

        elif choice == "4":
            self.console.print(
                "[blue][INFO][/blue] Skipping LLM setup - memory extraction disabled"
            )
            # Disable memory extraction in config.yml
            self.config_manager.update_memory_config({"extraction": {"enabled": False}})

        elif choice == "5":
            self.console.print(
                "[blue][INFO][/blue] llama.cpp selected (Chronicle-managed)"
            )
            # Preserve the concrete Qwen registry identity on reruns. Other local
            # llama.cpp choices use the generic entry, whose exact served identity is
            # synchronized later by extras/llm-services/init.py.
            current_llm = str(
                self.config_manager.get_config_defaults().get("llm") or ""
            )
            registry_llm = (
                current_llm
                if current_llm in MANAGED_LLAMACPP_REGISTRY_MODELS
                else "llamacpp-llm"
            )
            self.config_manager.update_config_defaults(
                {"llm": registry_llm, "embedding": "llamacpp-embed"}
            )
            # Re-sync the selected LLM and embedding entries from defaults.yml.
            # config.yml model entries override defaults *by name*, so a stale copy
            # would otherwise shadow endpoint/discovery fixes. The local-service
            # wizard subsequently reapplies its exact upstream identity and context.
            synced = self.config_manager.sync_models_from_defaults(
                [registry_llm, "llamacpp-embed"]
            )
            if synced:
                self.console.print(
                    "[blue][INFO][/blue] Re-synced model entries from defaults.yml: "
                    f"{', '.join(synced)} (model_url now follows LLM_BASE_URL)"
                )
            # Source of the llama.cpp endpoint (the llamacpp-llm entry reads LLM_BASE_URL):
            #   --llm-discover     → '' (backend discovers chronicle-llm on the Tailnet)
            #   --llm-base-url URL → pin a remote/own endpoint
            #   otherwise          → leave LLM_BASE_URL alone (host-local default applies)
            if getattr(self.args, "llm_discover", False):
                self.config["LLM_BASE_URL"] = ""
                self.console.print(
                    "[blue][INFO][/blue] LLM_BASE_URL left empty — backend will discover "
                    "chronicle-llm on the Tailnet at runtime"
                )
            elif getattr(self.args, "llm_base_url", None):
                self.config["LLM_BASE_URL"] = self.args.llm_base_url
                self.console.print(
                    f"[green]✅[/green] LLM_BASE_URL = {self.args.llm_base_url}"
                )
            self.console.print(f"[blue][INFO][/blue] Set defaults.llm: {registry_llm}")
            self.console.print(
                "[blue][INFO][/blue] Set defaults.embedding: llamacpp-embed"
            )

        elif choice == "6":
            self.console.print(
                "[blue][INFO][/blue] Gemma 4 unified STT+LLM mode selected"
            )
            self.console.print(
                "[blue][INFO][/blue] LLM requests will use the same Gemma 4 ASR service"
            )
            # gemma4-llm model definition exists in defaults.yml, pointing to GEMMA4_ASR_URL
            self.config_manager.update_config_defaults(
                {"llm": "gemma4-llm", "embedding": "local-embed"}
            )
            self.console.print(
                "[green][SUCCESS][/green] Gemma 4 unified mode configured in config.yml"
            )
            self.console.print("[blue][INFO][/blue] Set defaults.llm: gemma4-llm")
            self.console.print(
                "[blue][INFO][/blue] Set defaults.embedding: local-embed (Ollama)"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Embeddings require Ollama running with nomic-embed-text model"
            )

    def setup_fast_llm(self):
        """Optionally configure a separate fast LLM for quick, latency-sensitive
        tasks (wake-word follow-ups). Default: reuse the main LLM."""
        self.print_section("Fast LLM (optional)")
        self.console.print(
            "[blue][INFO][/blue] A 'fast LLM' handles quick tasks like wake-word "
            "follow-up commands (e.g. saying 'warmer' after an action runs)."
        )
        self.console.print(
            "By default this reuses your main LLM. You can point it at a faster/cheaper "
            "model instead (e.g. an 'instant' hosted model or a small local one)."
        )
        self.console.print()

        full_config = self.config_manager.get_full_config()
        existing_fast = full_config.get("defaults", {}).get("fast_llm", "")

        choices = {
            "1": "Reuse my main LLM (recommended)",
            "2": "Use a separate fast LLM (OpenAI-compatible endpoint)",
        }
        choice = self.prompt_choice(
            "Fast LLM for quick tasks?", choices, "2" if existing_fast else "1"
        )

        if choice == "1":
            # Empty default -> followup_resolution falls back to defaults.llm.
            self.config_manager.update_config_defaults({"fast_llm": ""})
            self.console.print(
                "[blue][INFO][/blue] Fast LLM = main LLM (defaults.fast_llm cleared)"
            )
            return

        existing_model = next(
            (m for m in full_config.get("models", []) if m.get("name") == "fast-llm"),
            {},
        )

        base_url = self.prompt_value(
            "Fast LLM API Base URL",
            existing_model.get("model_url", "https://api.openai.com/v1"),
        )
        if not base_url:
            self.console.print(
                "[yellow][WARNING][/yellow] No base URL - keeping main LLM for fast tasks"
            )
            self.config_manager.update_config_defaults({"fast_llm": ""})
            return

        api_key = self.prompt_with_existing_masked(
            prompt_text="Fast LLM API Key (leave empty if not required)",
            env_key="FAST_LLM_API_KEY",
            placeholders=["your_fast_llm_api_key_here"],
            is_password=True,
            default="",
        )
        if api_key:
            self.config["FAST_LLM_API_KEY"] = api_key

        model_name = self.prompt_value(
            "Fast LLM model name (e.g., gpt-5.5, gpt-4o-mini, llama-3.1-8b-instant)",
            existing_model.get("model_name", "gpt-5.5"),
        )
        if not model_name:
            self.console.print(
                "[yellow][WARNING][/yellow] No model name - keeping main LLM for fast tasks"
            )
            self.config_manager.update_config_defaults({"fast_llm": ""})
            return

        fast_model = {
            "name": "fast-llm",
            "description": "Fast LLM for quick tasks (wake-word follow-ups)",
            "model_type": "llm",
            "model_provider": "openai",
            "api_family": "openai",
            "model_name": model_name,
            "model_url": base_url,
            "api_key": "${oc.env:FAST_LLM_API_KEY,''}",
            "model_params": {"temperature": 0.1, "max_tokens": 200},
            "model_output": "json",
        }
        self.config_manager.add_or_update_model(fast_model)
        self.config_manager.update_config_defaults({"fast_llm": "fast-llm"})
        self.console.print(
            "[green][SUCCESS][/green] Separate fast LLM configured "
            "(defaults.fast_llm: fast-llm)"
        )

    def setup_fallback_llm(self):
        """Configure a fallback LLM that calls retry against when the primary
        LLM is unreachable (connection failure, timeout, 5xx).
        Default: OpenAI gpt-5.6-luna."""
        self.print_section("Fallback LLM (recommended)")
        self.console.print(
            "[blue][INFO][/blue] If your main LLM is unreachable (e.g. a local "
            "model is down), LLM calls are retried once against a fallback model."
        )
        self.console.print(
            "The default fallback is OpenAI gpt-5.6-luna (cheap, always available). "
            "It uses your OPENAI_API_KEY."
        )
        self.console.print()

        full_config = self.config_manager.get_full_config()
        defaults_cfg = full_config.get("defaults", {})
        existing_fb = defaults_cfg.get("fallback_llm") or ""
        explicitly_disabled = "fallback_llm" in defaults_cfg and not existing_fb
        existing_model = next(
            (
                m
                for m in full_config.get("models", [])
                if m.get("name") == "fallback-llm"
            ),
            {},
        )
        is_custom = bool(existing_fb) and (
            existing_fb != "fallback-llm"
            or (
                existing_model
                and existing_model.get("model_name") not in ("", "gpt-5.6-luna")
            )
        )

        choices = {
            "1": "OpenAI gpt-5.6-luna (recommended)",
            "2": "Custom OpenAI-compatible endpoint",
            "3": "No fallback",
        }
        default_choice = "3" if explicitly_disabled else ("2" if is_custom else "1")
        choice = self.prompt_choice(
            "Fallback LLM when the main LLM is down?", choices, default_choice
        )

        if choice == "3":
            self.config_manager.update_config_defaults({"fallback_llm": ""})
            self.console.print(
                "[blue][INFO][/blue] No fallback LLM — calls fail if the main "
                "LLM is unreachable (defaults.fallback_llm cleared)"
            )
            return

        if choice == "1":
            # Stock gpt-5.6-luna entry ships in defaults.yml; re-sync a stale
            # customized copy in config.yml so it doesn't shadow the default.
            if existing_model:
                self.config_manager.sync_models_from_defaults(["fallback-llm"])
            api_key = self.prompt_with_existing_masked(
                prompt_text="OpenAI API key for the fallback (leave empty to keep existing)",
                env_key="OPENAI_API_KEY",
                placeholders=["your_openai_api_key_here", "your-openai-key-here"],
                is_password=True,
                default="",
            )
            if api_key:
                self.config["OPENAI_API_KEY"] = api_key
            else:
                self.console.print(
                    "[yellow][WARNING][/yellow] No OPENAI_API_KEY — the fallback "
                    "will not work until one is set in .env"
                )
            self.config_manager.update_config_defaults({"fallback_llm": "fallback-llm"})
            self.console.print(
                "[green][SUCCESS][/green] Fallback LLM: OpenAI gpt-5.6-luna "
                "(defaults.fallback_llm: fallback-llm)"
            )
            return

        # Custom OpenAI-compatible fallback endpoint
        base_url = self.prompt_value(
            "Fallback LLM API Base URL",
            existing_model.get("model_url", "https://api.openai.com/v1"),
        )
        if not base_url:
            self.console.print(
                "[yellow][WARNING][/yellow] No base URL - fallback disabled"
            )
            self.config_manager.update_config_defaults({"fallback_llm": ""})
            return

        api_key = self.prompt_with_existing_masked(
            prompt_text="Fallback LLM API Key (leave empty if not required)",
            env_key="FALLBACK_LLM_API_KEY",
            placeholders=["your_fallback_llm_api_key_here"],
            is_password=True,
            default="",
        )
        if api_key:
            self.config["FALLBACK_LLM_API_KEY"] = api_key

        model_name = self.prompt_value(
            "Fallback LLM model name (e.g., gpt-5.6-luna, llama-3.1-8b-instant)",
            existing_model.get("model_name", "gpt-5.6-luna"),
        )
        if not model_name:
            self.console.print(
                "[yellow][WARNING][/yellow] No model name - fallback disabled"
            )
            self.config_manager.update_config_defaults({"fallback_llm": ""})
            return

        fallback_model = {
            "name": "fallback-llm",
            "description": "Fallback LLM used when the primary LLM is unreachable",
            "model_type": "llm",
            "model_provider": "openai",
            "api_family": "openai",
            "model_name": model_name,
            "model_url": base_url,
            "api_key": "${oc.env:FALLBACK_LLM_API_KEY,''}",
            "model_params": {"temperature": 0.2, "max_tokens": 4000},
            "model_output": "json",
        }
        self.config_manager.add_or_update_model(fallback_model)
        self.config_manager.update_config_defaults({"fallback_llm": "fallback-llm"})
        self.console.print(
            "[green][SUCCESS][/green] Custom fallback LLM configured "
            "(defaults.fallback_llm: fallback-llm)"
        )

    def setup_memory(self):
        """Configure memory provider - updates config.yml.

        Chronicle's agentic Markdown vault is currently the only memory provider,
        so there is no provider choice to make — we just ensure config.yml/.env
        record it, then choose the write and search agent backends.
        """
        self.config_manager.update_memory_config({"provider": "chronicle"})
        self.console.print(
            "[green][SUCCESS][/green] Memory: Chronicle agentic vault (config.yml + .env)"
        )
        self.setup_memory_agents()

    def setup_memory_agents(self):
        """Choose independent backends for vault writes and searches."""
        self.print_section("Memory agent backends")
        self.console.print(
            "[blue][INFO][/blue] Chronicle can use different agent backends to "
            "write conversations into the vault and to search it. Direct uses the "
            "configured LLM API, Codex uses a ChatGPT subscription for writes, and "
            "Pi can run either job through any configured model, including a local "
            "OpenAI-compatible model."
        )
        self.console.print()

        memory_config = self.config_manager.get_memory_config()
        existing_agents = memory_config.get("agents") or {}
        if not isinstance(existing_agents, dict):
            existing_agents = {}
        existing_write = existing_agents.get("write") or {}
        existing_search = existing_agents.get("search") or {}
        if not isinstance(existing_write, dict):
            existing_write = {}
        if not isinstance(existing_search, dict):
            existing_search = {}
        recovery_backend = (
            existing_write["recovery_backend"]
            if "recovery_backend" in existing_write
            else "direct"
        )
        if recovery_backend not in (None, "direct", "codex", "pi"):
            raise ValueError(
                "memory.agents.write.recovery_backend must be direct, codex, pi, "
                f"or null; got {recovery_backend!r}"
            )

        write_backend = str(existing_write.get("backend") or "direct").lower()
        write_choices = {
            "1": "Direct LLM tool loop (uses the configured LLM API)",
            "2": "Codex CLI (writes only; uses your ChatGPT subscription)",
            "3": "Pi agent (local or remote model from Chronicle's model registry)",
        }
        write_choice = self.prompt_choice(
            "Which backend should write memories?",
            write_choices,
            {"direct": "1", "codex": "2", "pi": "3"}.get(write_backend, "1"),
        )
        selected_write = {"1": "direct", "2": "codex", "3": "pi"}[write_choice]

        search_backend = str(existing_search.get("backend") or "direct").lower()
        search_choices = {
            "1": "Direct retrieval agent (uses the configured LLM API)",
            "2": "Pi agent (local or remote model from Chronicle's model registry)",
        }
        search_choice = self.prompt_choice(
            "Which backend should search memories?",
            search_choices,
            {"direct": "1", "pi": "2"}.get(search_backend, "1"),
        )
        selected_search = {"1": "direct", "2": "pi"}[search_choice]

        existing_backends = memory_config.get("backends") or {}
        if not isinstance(existing_backends, dict):
            existing_backends = {}
        existing_codex = existing_backends.get("codex") or {}
        existing_pi = existing_backends.get("pi") or {}
        if not isinstance(existing_codex, dict):
            existing_codex = {}
        if not isinstance(existing_pi, dict):
            existing_pi = {}

        codex_config = {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "sandbox_mode": "workspace-write",
            "timeout_seconds": 900,
            "max_used_percent": 80,
            "limit_id": "",
            **existing_codex,
        }
        pi_config = {
            "timeout_seconds": 900,
            "thinking": "off",
            **existing_pi,
        }
        # Model routing is owned by defaults + llm_operations. Older wizard runs
        # copied that policy into the executor adapter, causing provider switches to
        # require a second unrelated edit.
        pi_config.pop("model", None)

        if "pi" in (selected_write, selected_search, recovery_backend):
            effective_models = self._effective_model_registry()
            full_config = self.config_manager.get_full_config()
            defaults = full_config.get("defaults") or {}
            operations = full_config.get("llm_operations") or {}
            operation_names = set()
            if "pi" in (selected_write, recovery_backend):
                operation_names.add("memory_write")
            if selected_search == "pi":
                operation_names.add("memory_search")
            resolved_names = []
            for operation_name in sorted(operation_names):
                operation_config = operations.get(operation_name) or {}
                model_name = str(
                    operation_config.get("model") or defaults.get("llm") or ""
                ).strip()
                selected_model = effective_models.get(model_name)
                if selected_model is None:
                    raise ValueError(
                        f"{operation_name} resolves to model registry entry "
                        f"{model_name!r}, which does not exist"
                    )
                if str(selected_model.get("model_type") or "").lower() != "llm":
                    raise ValueError(
                        f"{operation_name} resolves to {model_name!r}, which is not an LLM"
                    )
                if str(selected_model.get("api_family") or "").lower() != "openai":
                    raise ValueError(
                        f"{operation_name} resolves to {model_name!r}, which is not "
                        "OpenAI-compatible"
                    )
                resolved_names.append(f"{operation_name} → {model_name}")
            self.console.print(
                "[green][SUCCESS][/green] Pi will use central model routing: "
                + ", ".join(resolved_names)
                + ". No Pi auth directory or host login is required."
            )

        if "codex" in (selected_write, recovery_backend):
            codex_home = Path(
                os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
            ).expanduser()
            auth_file = codex_home / "auth.json"
            if not shutil.which("codex"):
                self.console.print(
                    "[yellow][WARNING][/yellow] No `codex` CLI found on this host. "
                    "The containers ship their own binary, but you still need "
                    "subscription auth: install Codex and run `codex login` "
                    "(ChatGPT sign-in), then re-run init."
                )
            if auth_file.is_file():
                self.console.print(
                    f"[green]✅[/green] Found Codex subscription auth at {auth_file}"
                )
            else:
                self.console.print(
                    f"[yellow][WARNING][/yellow] No Codex auth at {auth_file} — "
                    "the configured Codex write backend is unavailable and Chronicle "
                    "readiness will fail until `codex login` has been run on this host."
                )
            # CODEX_HOME is read-write because Codex rotates refresh tokens. Pi does
            # not use this mount or require a host-side auth directory.
            self.config["CODEX_HOME_DIR"] = str(codex_home)
            # bubblewrap cannot run nested inside the rootless-podman containers;
            # the container is the isolation boundary.
            codex_config["sandbox_mode"] = "danger-full-access"

        # Replace the agent/backend subtrees and remove the obsolete flat executor
        # keys. This is an active-development schema change, not a compatibility
        # layer: only the nested shape is written back.
        full_config = self.config_manager.get_full_config()
        saved_memory = full_config.setdefault("memory", {})
        saved_memory.pop("agent_executor", None)
        saved_memory.pop("codex", None)
        saved_memory.pop("pi", None)
        saved_memory["agents"] = {
            "write": {
                "backend": selected_write,
                "recovery_backend": recovery_backend,
            },
            "search": {"backend": selected_search},
        }
        saved_memory["backends"] = {
            "direct": {},
            "codex": codex_config,
            "pi": pi_config,
        }
        llm_operations = full_config.get("llm_operations")
        removed_legacy_operation = False
        if llm_operations is not None:
            if not isinstance(llm_operations, dict):
                raise ValueError("llm_operations must be a mapping")
            removed_legacy_operation = (
                llm_operations.pop("memory_agent", None) is not None
            )
        self.config_manager.save_full_config(full_config)

        recovery_label = "disabled" if recovery_backend is None else recovery_backend
        if removed_legacy_operation:
            self.console.print(
                "[yellow][WARNING][/yellow] Removed obsolete "
                "llm_operations.memory_agent; memory_write and memory_search now "
                "use their explicit configuration (or the current defaults)."
            )
        self.console.print(
            "[green][SUCCESS][/green] Memory agents configured: "
            f"write={selected_write} (recovery={recovery_label}), "
            f"search={selected_search}"
        )

    def _effective_model_registry(self) -> Dict[str, Dict[str, Any]]:
        """Return the same name-based defaults+user model view used at runtime."""
        defaults_path = REPO_ROOT / "config" / "defaults.yml"
        defaults: Dict[str, Any] = {}
        if defaults_path.is_file():
            yaml = YAML(typ="safe")
            with defaults_path.open("r", encoding="utf-8") as handle:
                defaults = yaml.load(handle) or {}

        effective: Dict[str, Dict[str, Any]] = {}
        for source in (defaults, self.config_manager.get_full_config()):
            for model in source.get("models", []) or []:
                if isinstance(model, dict) and model.get("name"):
                    # Runtime config_loader uses whole-entry replacement by name.
                    effective[str(model["name"])] = dict(model)
        return effective

    def setup_optional_services(self):
        """Configure optional services"""
        # Check if speaker service URL provided via args
        has_speaker_arg = (
            hasattr(self.args, "speaker_service_url") and self.args.speaker_service_url
        )
        has_asr_arg = (
            hasattr(self.args, "parakeet_asr_url") and self.args.parakeet_asr_url
        )

        if has_speaker_arg:
            self.config["SPEAKER_SERVICE_URL"] = self.args.speaker_service_url
            self.console.print(
                f"[green]✅[/green] Speaker Recognition: {self.args.speaker_service_url} (configured via wizard)"
            )

        if has_asr_arg:
            self.config["PARAKEET_ASR_URL"] = self.args.parakeet_asr_url
            self.console.print(
                f"[green]✅[/green] Parakeet ASR: {self.args.parakeet_asr_url} (configured via wizard)"
            )

        # Speaker / TTS source = "configure from the Tailnet later": leave the URL
        # empty so the backend discovers chronicle-speaker / chronicle-tts at runtime.
        speaker_discover = getattr(self.args, "speaker_discover", False)
        if speaker_discover:
            self.config["SPEAKER_SERVICE_URL"] = ""
            self.console.print(
                "[blue][INFO][/blue] SPEAKER_SERVICE_URL left empty — backend will "
                "discover chronicle-speaker on the Tailnet at runtime"
            )

        if getattr(self.args, "tts_discover", False):
            self.config["CHRONICLE_TTS_URL"] = ""
            self.console.print(
                "[blue][INFO][/blue] CHRONICLE_TTS_URL left empty — backend will "
                "discover chronicle-tts on the Tailnet at runtime"
            )
        elif getattr(self.args, "tts_url", None):
            self.config["CHRONICLE_TTS_URL"] = self.args.tts_url
            self.console.print(
                f"[green]✅[/green] TTS: {self.args.tts_url} (configured via wizard)"
            )

        # Only show interactive section if not all configured via args
        if not has_speaker_arg and not speaker_discover:
            try:
                enable_speaker = Confirm.ask(
                    "Enable Speaker Recognition?", default=False
                )
            except EOFError:
                self.console.print("Using default: No")
                enable_speaker = False

            if enable_speaker:
                speaker_url = self.prompt_value(
                    "Speaker Recognition service URL",
                    "http://host.docker.internal:8001",
                )
                self.config["SPEAKER_SERVICE_URL"] = speaker_url
                self.console.print(
                    "[green][SUCCESS][/green] Speaker Recognition configured"
                )
                self.console.print(
                    "[blue][INFO][/blue] Start with: cd ../extras/speaker-recognition && docker compose up -d"
                )

        # Check if Tailscale auth key provided via args
        if hasattr(self.args, "ts_authkey") and self.args.ts_authkey:
            self.config["TS_AUTHKEY"] = self.args.ts_authkey
            self.console.print(
                f"[green][SUCCESS][/green] Tailscale auth key configured (Docker integration enabled)"
            )

    def _discover_tailnet_http(
        self, port: int, probe: Callable[[str], Optional[str]]
    ) -> list:
        """Probe online Tailnet nodes (this one included) for an HTTP service.

        ``probe(base_url)`` returns a display string when the service answers,
        None otherwise. Returns [{host, url, detail}], preferring MagicDNS names
        over 100.x IPs (which can change over time).
        """
        nodes = [
            node
            for node in list_tailnet_peers()
            if node["online"] and (node["dns_name"] or node["ip"])
        ]
        if not nodes:
            return []

        def check(node):
            address = node["dns_name"] or node["ip"]
            url = f"http://{address}:{port}"
            try:
                detail = probe(url)
            except (urllib.error.URLError, OSError, ValueError):
                return None
            return (
                {"host": node["host"], "url": url, "detail": detail} if detail else None
            )

        with self.console.status(
            f"Scanning {len(nodes)} Tailnet node(s) on port {port}..."
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(check, nodes))
        return [result for result in results if result]

    def _select_tailnet_service_url(
        self,
        label: str,
        port: int,
        probe: Callable[[str], Optional[str]],
        existing: str,
    ) -> str:
        """Pick a service URL by scanning the Tailnet, or enter one manually.

        Returns "" when nothing was chosen. A re-run defaults to keeping the
        existing URL (press-Enter-through); a fresh run defaults to scanning.
        """
        self.console.print(f"Where does {label} run?")
        self.console.print(f"  1) Scan the Tailnet for it (port {port})")
        self.console.print("  2) Enter the URL manually")
        default_choice = "2" if existing else "1"
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
        except EOFError:
            choice = default_choice

        if choice == "1":
            found = self._discover_tailnet_http(port, probe)
            if found:
                self.console.print(f"[green]Found {len(found)} on the Tailnet:[/green]")
                for index, entry in enumerate(found, 1):
                    self.console.print(
                        f"  {index}) {entry['host']} — {entry['url']} ({entry['detail']})"
                    )
                default_pick = "1"
                for index, entry in enumerate(found, 1):
                    if existing and entry["url"].rstrip("/") == existing.rstrip("/"):
                        default_pick = str(index)
                        break
                try:
                    pick = int(Prompt.ask("Pick one", default=default_pick)) - 1
                except (EOFError, ValueError):
                    pick = int(default_pick) - 1
                if 0 <= pick < len(found):
                    return found[pick]["url"]
            else:
                self.console.print(
                    f"[yellow]No {label} found on the Tailnet — enter the URL manually.[/yellow]"
                )
        return self.prompt_value(f"{label} URL (e.g. http://host:{port})", existing)

    def _probe_immich(self, base_url: str) -> str | None:
        """Immich version string when ``base_url`` answers the Immich ping."""
        with urllib.request.urlopen(f"{base_url}/api/server/ping", timeout=3) as res:
            if json.load(res).get("res") != "pong":
                return None
        with urllib.request.urlopen(f"{base_url}/api/server/version", timeout=3) as res:
            version = json.load(res)
            return f"Immich v{version['major']}.{version['minor']}.{version['patch']}"

    def _check_immich(self, url: str, key: str) -> str | None:
        """Probe an Immich server; return an error message or None if healthy."""
        base = url.rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/server/ping", timeout=5) as res:
                if json.load(res).get("res") != "pong":
                    return "server did not answer the Immich ping"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"server unreachable: {exc}"
        request = urllib.request.Request(
            f"{base}/api/people?size=1", headers={"x-api-key": key}
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                return None
        except urllib.error.HTTPError as exc:
            return f"API key rejected (HTTP {exc.code})"
        except (urllib.error.URLError, OSError) as exc:
            return f"people API unreachable: {exc}"

    def _enable_immich_cron_jobs(self):
        """Enable the Immich cron jobs in config.yml (schedules from defaults.yml)."""
        jobs = {
            "immich_memories": (
                "Discover a bounded set of salient Immich photo candidates",
                "30 4 * * *",
            ),
            "person_photos": (
                "Embed Immich face photos into the vault's People notes",
                "45 4 * * *",
            ),
        }
        config = self.config_manager.get_full_config()
        cron_jobs = config.setdefault("cron_jobs", {})
        for job_id, (description, schedule) in jobs.items():
            entry = cron_jobs.setdefault(
                job_id, {"description": description, "schedule": schedule}
            )
            entry["enabled"] = True
        self.config_manager.save_full_config(config)

    def setup_immich(self):
        """Configure the Immich photo library integration"""
        self.print_section("Immich Photo Library (Optional)")
        self.console.print(
            "Connect a self-hosted Immich server to discover salient photos for the"
        )
        self.console.print(
            "timeline and to embed each known person's face photo in their vault note."
        )
        self.console.print()

        existing_url = self.read_existing_env_value("IMMICH_URL")
        try:
            enable = Confirm.ask(
                "Configure Immich integration?", default=bool(existing_url)
            )
        except EOFError:
            self.console.print("Using default: No")
            enable = False
        if not enable:
            return

        url = self._select_tailnet_service_url(
            "Immich", 2283, self._probe_immich, existing_url
        )
        if not url:
            self.console.print(
                "[yellow][WARNING][/yellow] No Immich URL — skipping Immich setup"
            )
            return
        key = self.prompt_with_existing_masked(
            "Immich API key (Immich → Account Settings → API Keys)",
            "IMMICH_API_KEY",
            placeholders=[],
            is_password=True,
        )
        self.config["IMMICH_URL"] = url
        self.config["IMMICH_API_KEY"] = key

        error = self._check_immich(url, key)
        if error is None:
            self.console.print("[green][SUCCESS][/green] Immich server verified")
        else:
            self.console.print(
                f"[yellow][WARNING][/yellow] Immich check failed — {error}"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Saved anyway; fix the URL/key before "
                "enabling the cron jobs does anything useful"
            )

        # Photos are imported for one Chronicle account; the backend defaults to
        # the admin account, so a fresh install needs no ObjectId here.
        existing_user = self.read_existing_env_value("IMMICH_USER_ID")
        user_id = self.prompt_value(
            "Chronicle user ObjectId owning the photos (Enter = admin account)",
            existing_user,
        )
        self.config["IMMICH_USER_ID"] = user_id

        try:
            enable_crons = Confirm.ask(
                "Enable the daily Immich cron jobs (photo discovery + person photos)?",
                default=True,
            )
        except EOFError:
            self.console.print("Using default: Yes")
            enable_crons = True
        if enable_crons:
            self._enable_immich_cron_jobs()
            self.console.print(
                "[green][SUCCESS][/green] Cron jobs enabled (manage them under "
                "Queue & Events in the dashboard)"
            )
        self.console.print("[green][SUCCESS][/green] Immich integration configured")

    def _probe_homeassistant(self, base_url: str) -> str | None:
        """Instance name when ``base_url`` serves the Home Assistant frontend."""
        with urllib.request.urlopen(f"{base_url}/manifest.json", timeout=3) as res:
            name = str(json.load(res).get("name") or "")
        return name if "home assistant" in name.lower() else None

    def _check_homeassistant(self, url: str, token: str) -> str | None:
        """Probe the HA REST API with the token; error message or None if healthy."""
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                return None
        except urllib.error.HTTPError as exc:
            return f"token rejected (HTTP {exc.code})"
        except (urllib.error.URLError, OSError) as exc:
            return f"server unreachable: {exc}"

    def _enable_homeassistant_plugin(self):
        """Flip the homeassistant plugin to enabled in config/plugins.yml."""
        plugins_yml = REPO_ROOT / "config" / "plugins.yml"
        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(plugins_yml.read_text(encoding="utf-8")) or {}
        entry = data.setdefault("plugins", {}).setdefault("homeassistant", {})
        if entry.get("enabled") is True:
            return
        entry["enabled"] = True
        with plugins_yml.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)

    def setup_homeassistant(self):
        """Configure the Home Assistant smart-home plugin"""
        self.print_section("Home Assistant (Optional)")
        self.console.print(
            "Voice-command smart home control through the Home Assistant plugin."
        )
        self.console.print()

        existing_url = self.read_existing_env_value("HA_URL")
        try:
            enable = Confirm.ask(
                "Configure Home Assistant integration?", default=bool(existing_url)
            )
        except EOFError:
            self.console.print("Using default: No")
            enable = False
        if not enable:
            return

        url = self._select_tailnet_service_url(
            "Home Assistant", 8123, self._probe_homeassistant, existing_url
        )
        if not url:
            self.console.print(
                "[yellow][WARNING][/yellow] No Home Assistant URL — skipping setup"
            )
            return
        token = self.prompt_with_existing_masked(
            "Long-lived access token (HA → Profile → Security → Long-lived tokens)",
            "HA_TOKEN",
            placeholders=[],
            is_password=True,
        )
        self.config["HA_URL"] = url
        self.config["HA_TOKEN"] = token

        error = self._check_homeassistant(url, token)
        if error is None:
            self.console.print("[green][SUCCESS][/green] Home Assistant API verified")
        else:
            self.console.print(
                f"[yellow][WARNING][/yellow] Home Assistant check failed — {error}"
            )
            self.console.print(
                "[yellow][WARNING][/yellow] Saved anyway; fix the URL/token in "
                "backend/.env"
            )

        try:
            enable_plugin = Confirm.ask(
                "Enable the Home Assistant plugin in config/plugins.yml?", default=True
            )
        except EOFError:
            self.console.print("Using default: Yes")
            enable_plugin = True
        if enable_plugin:
            self._enable_homeassistant_plugin()
            self.console.print(
                "[green][SUCCESS][/green] Plugin enabled (trigger keywords and "
                "events stay as configured in config/plugins.yml)"
            )

    def setup_langfuse(self):
        """Configure LangFuse observability and prompt management"""
        self.console.print()
        self.console.print(
            "[bold cyan]LangFuse Observability & Prompt Management[/bold cyan]"
        )

        # Check if keys were passed from wizard (langfuse init already ran)
        langfuse_pub = getattr(self.args, "langfuse_public_key", None)
        langfuse_sec = getattr(self.args, "langfuse_secret_key", None)

        if langfuse_pub and langfuse_sec:
            # Auto-configure from wizard — no prompts needed
            langfuse_host = (
                getattr(self.args, "langfuse_host", None) or "http://langfuse-web:3000"
            )
            self.config["LANGFUSE_HOST"] = langfuse_host
            self.config["LANGFUSE_PUBLIC_KEY"] = langfuse_pub
            self.config["LANGFUSE_SECRET_KEY"] = langfuse_sec
            self.config["LANGFUSE_BASE_URL"] = langfuse_host

            # Derive browser-accessible URL for deep-links
            public_url = (
                getattr(self.args, "langfuse_public_url", None)
                or "http://localhost:3002"
            )
            self._save_langfuse_public_url(public_url)

            source = "external" if "langfuse-web" not in langfuse_host else "local"
            self.console.print(
                f"[green][SUCCESS][/green] LangFuse auto-configured ({source})"
            )
            self.console.print(f"[blue][INFO][/blue] Host: {langfuse_host}")
            self.console.print(f"[blue][INFO][/blue] Public URL: {public_url}")
            self.console.print(
                f"[blue][INFO][/blue] Public key: {self.mask_api_key(langfuse_pub)}"
            )
            return

        # Manual configuration (standalone init.py run)
        self.console.print(
            "Enable LLM tracing, observability, and prompt management with LangFuse"
        )
        self.console.print("Self-host: cd ../extras/langfuse && docker compose up -d")
        self.console.print()

        try:
            enable_langfuse = Confirm.ask("Enable LangFuse?", default=False)
        except EOFError:
            self.console.print("Using default: No")
            enable_langfuse = False

        if enable_langfuse:
            host = self.prompt_with_existing_masked(
                prompt_text="LangFuse host URL",
                env_key="LANGFUSE_HOST",
                placeholders=[""],
                is_password=False,
                default="http://langfuse-web:3000",
            )
            public_key = self.prompt_with_existing_masked(
                prompt_text="LangFuse public key",
                env_key="LANGFUSE_PUBLIC_KEY",
                placeholders=[""],
                is_password=False,
                default="",
            )
            secret_key = self.prompt_with_existing_masked(
                prompt_text="LangFuse secret key",
                env_key="LANGFUSE_SECRET_KEY",
                placeholders=[""],
                is_password=True,
                default="",
            )

            if host:
                self.config["LANGFUSE_HOST"] = host
                self.config["LANGFUSE_BASE_URL"] = host
            if public_key:
                self.config["LANGFUSE_PUBLIC_KEY"] = public_key
            if secret_key:
                self.config["LANGFUSE_SECRET_KEY"] = secret_key

            # Browser-accessible URL for deep-links (stored in config.yml, not .env)
            public_url = Prompt.ask(
                "LangFuse browser URL (for dashboard links)",
                default="http://localhost:3002",
            )
            if public_url:
                self._save_langfuse_public_url(public_url)

            self.console.print("[green][SUCCESS][/green] LangFuse configured")
        else:
            self.console.print("[blue][INFO][/blue] LangFuse disabled")

    def _save_langfuse_public_url(self, public_url: str):
        """Save the Langfuse browser-accessible URL to config.yml."""
        full_config = self.config_manager.get_full_config()
        if "observability" not in full_config:
            full_config["observability"] = {}
        if "langfuse" not in full_config["observability"]:
            full_config["observability"]["langfuse"] = {}
        full_config["observability"]["langfuse"]["public_url"] = public_url
        self.config_manager.save_full_config(full_config)

    def setup_network(self):
        """Configure network settings"""
        self.print_section("Network Configuration")

        self.config["BACKEND_PUBLIC_PORT"] = self.prompt_value("Backend port", "8000")
        self.config["WEBUI_PORT"] = self.prompt_value("Web UI port", "5173")

    def setup_https(self):
        """Configure HTTPS settings for microphone access"""
        # Check if HTTPS configuration provided via command line
        if hasattr(self.args, "enable_https") and self.args.enable_https:
            enable_https = True
            server_ip = getattr(self.args, "server_ip", "localhost")
            self.console.print(
                f"[green]✅[/green] HTTPS: {server_ip} (configured via wizard)"
            )
        else:
            # Interactive configuration
            self.print_section("HTTPS Configuration (Optional)")

            try:
                enable_https = Confirm.ask(
                    "Enable HTTPS for microphone access?", default=False
                )
            except EOFError:
                self.console.print("Using default: No")
                enable_https = False

            if enable_https:
                self.console.print(
                    "[blue][INFO][/blue] HTTPS enables microphone access in browsers"
                )

                # Try to auto-detect Tailscale address
                ts_dns, ts_ip = detect_tailscale_info()

                if ts_dns:
                    self.console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale DNS: {ts_dns}"
                    )
                    if ts_ip:
                        self.console.print(
                            f"[green][AUTO-DETECTED][/green] Tailscale IP:  {ts_ip}"
                        )
                    default_address = ts_dns
                elif ts_ip:
                    self.console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale IP: {ts_ip}"
                    )
                    default_address = ts_ip
                else:
                    self.console.print("[blue][INFO][/blue] Tailscale not detected")
                    self.console.print(
                        "[blue][INFO][/blue] To find your Tailscale address: tailscale status --json | jq -r '.Self.DNSName'"
                    )
                    default_address = "localhost"

                self.console.print(
                    "[blue][INFO][/blue] For local-only access, use 'localhost'"
                )

                # Use the new masked prompt function (not masked for IP, but shows existing)
                server_ip = self.prompt_with_existing_masked(
                    prompt_text="Server IP/Domain for SSL certificate",
                    env_key="SERVER_IP",
                    placeholders=["localhost", "your-server-ip-here"],
                    is_password=False,
                    default=default_address,
                )

        if enable_https:
            script_dir = Path(__file__).parent

            # Decide how the TLS cert is managed (same logic the wizard uses).
            cert_mode = decide_cert_mode(server_ip)

            if cert_mode == "static":
                # Host-issued cert file (e.g. Docker Desktop on macOS, where Caddy can't
                # reach the tailscaled socket). Warn if it's missing; the wizard normally
                # generates it and the services.py startup hook keeps it fresh on restart.
                cert_file = script_dir / ".." / ".." / "certs" / "server.crt"
                if not cert_file.exists():
                    self.console.print(
                        "[yellow][WARNING][/yellow] No certificate found in certs/; "
                        "run ./wizard.sh, or it will be generated on first start."
                    )

            # Generate Caddyfile from template
            self.console.print(
                "[blue][INFO][/blue] Creating Caddyfile configuration..."
            )
            caddyfile_template = script_dir / "Caddyfile.template"
            caddyfile_path = script_dir / "Caddyfile"

            if caddyfile_template.exists():
                try:
                    # Check if Caddyfile exists as a directory (common issue)
                    if caddyfile_path.exists() and caddyfile_path.is_dir():
                        self.console.print(
                            "[red]❌ ERROR: 'Caddyfile' exists as a directory![/red]"
                        )
                        self.console.print(
                            "[yellow]   Please remove it manually:[/yellow]"
                        )
                        self.console.print(
                            f"[yellow]   rm -rf {caddyfile_path}[/yellow]"
                        )
                        self.console.print(
                            "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                        )
                        self.config["HTTPS_ENABLED"] = "false"
                    else:
                        with open(caddyfile_template, "r") as f:
                            caddyfile_content = f.read()

                        # Replace TAILSCALE_IP with server_ip
                        caddyfile_content = caddyfile_content.replace(
                            "TAILSCALE_IP", server_ip
                        )

                        # Also serve the machine's LAN IP so devices that are on
                        # the local network but not the tailnet can reach the
                        # dashboard (Caddy falls back to its internal CA for the
                        # IP address, so the browser shows a one-time warning).
                        lan_ip = detect_lan_ip()
                        if lan_ip and lan_ip != server_ip:
                            caddyfile_content = caddyfile_content.replace(
                                f"localhost {server_ip} {{",
                                f"localhost {server_ip} {lan_ip} {{",
                            )
                            caddyfile_content = caddyfile_content.replace(
                                f"https://{server_ip}:3443 {{",
                                f"https://{server_ip}:3443 https://{lan_ip}:3443 {{",
                            )
                            # Clients connecting to a raw IP send no SNI, and
                            # behind the container engine's port-forward Caddy
                            # cannot infer the IP from the connection either;
                            # default_sni routes SNI-less handshakes to the LAN
                            # IP site instead of aborting with a TLS alert.
                            caddyfile_content = (
                                "{\n"
                                f"    default_sni {lan_ip}\n"
                                "}\n\n" + caddyfile_content
                            )
                            self.console.print(
                                f"[blue][INFO][/blue] Also serving LAN address: {lan_ip}"
                            )

                        # Static mode serves the shared host-issued cert in every site
                        # block (Chronicle + LangFuse). Caddy mode leaves the marker as
                        # a comment and obtains/renews certificates itself.
                        if cert_mode == "static":
                            caddyfile_content = caddyfile_content.replace(
                                "# TLS_CERT_DIRECTIVE",
                                "tls /certs/server.crt /certs/server.key",
                            )

                        with open(caddyfile_path, "w") as f:
                            f.write(caddyfile_content)

                        self.console.print(
                            f"[green][SUCCESS][/green] Caddyfile created for: {server_ip}"
                        )
                        self.config["HTTPS_ENABLED"] = "true"
                        self.config["SERVER_IP"] = server_ip
                        self.config["HTTPS_CERT_MODE"] = cert_mode

                        # Caddy-managed certs on a Tailscale address need the tailscaled
                        # socket mounted in; write/remove the compose override to match.
                        self._write_caddy_socket_override(
                            script_dir, cert_mode, server_ip
                        )

                        # Configure webui-dev for same-origin API calls through Caddy
                        self.config["VITE_BACKEND_URL"] = ""
                        self.config["VITE_HMR_PORT"] = "443"
                        allowed_hosts = f"localhost 127.0.0.1 {server_ip}"
                        if lan_ip and lan_ip != server_ip:
                            allowed_hosts += f" {lan_ip}"
                        self.config["VITE_ALLOWED_HOSTS"] = allowed_hosts

                except Exception as e:
                    self.console.print(
                        f"[red]❌ ERROR: Caddyfile generation failed: {e}[/red]"
                    )
                    self.console.print(
                        "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                    )
                    self.config["HTTPS_ENABLED"] = "false"
            else:
                self.console.print("[red]❌ ERROR: Caddyfile.template not found[/red]")
                self.console.print(
                    "[red]   HTTPS will NOT work without a proper Caddyfile![/red]"
                )
                self.config["HTTPS_ENABLED"] = "false"
        else:
            self.config["HTTPS_ENABLED"] = "false"

    def _write_caddy_socket_override(self, service_dir, cert_mode, server_address):
        """Maintain Caddy's Tailscale mount without replacing local overrides."""
        override_path = service_dir / "docker-compose.override.yml"
        socket = tailscale_socket_path()
        mounted = update_caddy_socket_override(
            override_path,
            socket_path=socket,
            enabled=(
                cert_mode == "caddy"
                and server_address.endswith(".ts.net")
                and bool(socket)
            ),
        )
        if mounted:
            self.console.print(
                "[green][SUCCESS][/green] Caddy will auto-manage the Tailscale "
                "certificate (tailscaled socket mounted)"
            )

    def generate_env_file(self):
        """Generate .env file from template and update with configuration.

        Preserves existing .env values that weren't explicitly set during this
        wizard run, preventing silent data loss on re-runs.
        """
        env_path = SERVICE_DIR / ".env"
        env_template = SERVICE_DIR / ".env.template"

        # Read ALL existing .env values before overwriting so we can preserve
        # keys that weren't touched during this wizard run (e.g., API keys
        # configured in a previous run for services not reconfigured now).
        preserved_values = {}
        if env_path.exists():
            preserved_values = dotenv_values(str(env_path))

        # Backup existing .env
        self.backup_existing_env()

        # Copy template to .env
        if env_template.exists():
            shutil.copy2(env_template, env_path)
            self.console.print("[blue][INFO][/blue] Copied .env.template to .env")
        else:
            self.console.print(
                "[yellow][WARNING][/yellow] .env.template not found, creating new .env"
            )
            env_path.touch(mode=0o600)

        env_path_str = str(env_path)

        # Merge: self.config (this run) takes priority over preserved (previous run).
        # This ensures new values win, but old values survive if untouched.
        merged = {**preserved_values, **self.config}

        for key, value in merged.items():
            if value is not None:  # Only set values that were explicitly configured
                set_key(env_path_str, key, value)

        # Ensure secure permissions
        os.chmod(env_path, 0o600)

        self.console.print(
            "[green][SUCCESS][/green] .env file configured successfully with secure permissions"
        )

        # Note: config.yml is automatically saved by ConfigManager when updates are made
        self.console.print(
            "[blue][INFO][/blue] Configuration saved to config.yml and .env (via ConfigManager)"
        )

    def copy_config_templates(self):
        """Copy other configuration files"""

        if (
            not (SERVICE_DIR / "diarization_config.json").exists()
            and (SERVICE_DIR / "diarization_config.json.template").exists()
        ):
            shutil.copy2(
                SERVICE_DIR / "diarization_config.json.template",
                SERVICE_DIR / "diarization_config.json",
            )
            self.console.print(
                "[green][SUCCESS][/green] diarization_config.json created"
            )

    def show_summary(self):
        """Show configuration summary"""
        self.print_section("Configuration Summary")
        self.console.print()

        self.console.print(
            f"✅ Admin Account: {self.config.get('ADMIN_EMAIL', 'Not configured')}"
        )

        # Get current config from ConfigManager (single source of truth)
        config_yml = self.config_manager.get_full_config()

        # Show transcription from config.yml
        stt_default = config_yml.get("defaults", {}).get("stt", "not set")
        stt_model = next(
            (m for m in config_yml.get("models", []) if m.get("name") == stt_default),
            None,
        )
        stt_provider = (
            stt_model.get("model_provider", "unknown")
            if stt_model
            else "not configured"
        )
        self.console.print(
            f"✅ Transcription: {stt_provider} ({stt_default}) - config.yml"
        )

        # Show LLM config from config.yml
        llm_default = config_yml.get("defaults", {}).get("llm", "not set")
        embedding_default = config_yml.get("defaults", {}).get("embedding", "not set")
        self.console.print(f"✅ LLM: {llm_default} (config.yml)")
        if llm_default == "gemma4-llm" and stt_default == "stt-gemma4":
            self.console.print(
                "   [dim](unified: STT and LLM share the same Gemma 4 model)[/dim]"
            )
        self.console.print(f"✅ Embedding: {embedding_default} (config.yml)")

        # Show memory provider from config.yml
        memory_provider = config_yml.get("memory", {}).get("provider", "chronicle")
        self.console.print(f"✅ Memory Provider: {memory_provider} (config.yml)")

        if self.config.get("IMMICH_URL"):
            self.console.print(f"✅ Immich: {self.config['IMMICH_URL']}")
        if self.config.get("HA_URL"):
            self.console.print(f"✅ Home Assistant: {self.config['HA_URL']}")

        # Auto-determine URLs based on HTTPS configuration
        if self.config.get("HTTPS_ENABLED") == "true":
            server_ip = self.config.get("SERVER_IP", "localhost")
            self.console.print(f"✅ Backend URL: https://{server_ip}/")
            self.console.print(f"✅ Dashboard URL: https://{server_ip}/")
        else:
            backend_port = self.config.get("BACKEND_PUBLIC_PORT", "8000")
            webui_port = self.config.get("WEBUI_PORT", "5173")
            self.console.print(f"✅ Backend URL: http://localhost:{backend_port}")
            self.console.print(f"✅ Dashboard URL: http://localhost:{webui_port}")

    def show_next_steps(self):
        """Show next steps"""
        self.print_section("Next Steps")
        self.console.print()

        # Get current config from ConfigManager (single source of truth)
        config_yml = self.config_manager.get_full_config()

        self.console.print("1. Start the main services:")
        self.console.print("   [cyan]docker compose up --build -d[/cyan]")
        self.console.print()

        # Auto-determine URLs for next steps
        if self.config.get("HTTPS_ENABLED") == "true":
            server_ip = self.config.get("SERVER_IP", "localhost")
            self.console.print("2. Access the dashboard:")
            self.console.print(f"   [cyan]https://{server_ip}/[/cyan]")
            self.console.print()
            self.console.print("3. Check service health:")
            self.console.print(f"   [cyan]curl -k https://{server_ip}/health[/cyan]")
        else:
            webui_port = self.config.get("WEBUI_PORT", "5173")
            backend_port = self.config.get("BACKEND_PUBLIC_PORT", "8000")
            self.console.print("2. Access the dashboard:")
            self.console.print(f"   [cyan]http://localhost:{webui_port}[/cyan]")
            self.console.print()
            self.console.print("3. Check service health:")
            self.console.print(
                f"   [cyan]curl http://localhost:{backend_port}/health[/cyan]"
            )

        if self.config.get("TRANSCRIPTION_PROVIDER") == "offline":
            self.console.print()
            self.console.print("5. Start Parakeet ASR:")
            self.console.print(
                "   [cyan]cd ../extras/asr-services && docker compose up parakeet -d[/cyan]"
            )

    def run(self):
        """Run the complete setup process"""
        self.print_header("🚀 Chronicle Interactive Setup")
        self.console.print(
            "This wizard will help you configure Chronicle with all necessary services."
        )
        self.console.print(
            "[dim]Safe to run again — it backs up your config and preserves previous values.[/dim]"
        )
        self.console.print(
            "[dim]When unsure, just press Enter — the defaults will work.[/dim]"
        )
        self.console.print()

        try:
            # Backup existing config
            self.backup_existing_env()

            # Run setup steps
            self.setup_authentication()
            self.setup_transcription()
            self.setup_streaming_provider()
            self.setup_live_segmentation()
            self.setup_llm()
            self.setup_fast_llm()
            self.setup_fallback_llm()
            self.setup_memory()
            self.setup_optional_services()
            self.setup_immich()
            self.setup_homeassistant()
            self.setup_langfuse()
            self.setup_network()
            self.setup_https()

            # Generate files
            self.print_header("Configuration Complete!")
            self.generate_env_file()
            self.copy_config_templates()

            # Show results
            self.show_summary()
            self.show_next_steps()

            self.console.print()
            self.console.print("[green][SUCCESS][/green] Setup complete! 🎉")
            self.console.print()
            self.console.print("📝 [bold]Configuration files updated:[/bold]")
            self.console.print(f"  • .env - API keys and environment variables")
            self.console.print(
                f"  • ../config/config.yml - Model and memory provider configuration"
            )
            self.console.print()
            self.console.print("For detailed documentation, see:")
            self.console.print("  • quickstart.md")
            self.console.print("  • MEMORY_PROVIDERS.md")
            self.console.print("  • AGENTS.md")

        except KeyboardInterrupt:
            self.console.print()
            self.console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            self.console.print(f"[red][ERROR][/red] Setup failed: {e}")
            sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Chronicle Backend Setup")
    parser.add_argument(
        "--speaker-service-url",
        help="Speaker Recognition service URL (default: prompt user)",
    )
    parser.add_argument(
        "--speaker-discover",
        action="store_true",
        help="Leave SPEAKER_SERVICE_URL empty so the backend discovers "
        "chronicle-speaker on the Tailnet at runtime.",
    )
    parser.add_argument(
        "--tts-url",
        help="Text-to-speech endpoint URL (own/remote/Tailnet-picked) → CHRONICLE_TTS_URL.",
    )
    parser.add_argument(
        "--tts-discover",
        action="store_true",
        help="Leave CHRONICLE_TTS_URL empty so the backend discovers chronicle-tts "
        "on the Tailnet at runtime.",
    )
    parser.add_argument(
        "--parakeet-asr-url", help="Parakeet ASR service URL (default: prompt user)"
    )
    parser.add_argument(
        "--transcription-provider",
        choices=[
            "deepgram",
            "parakeet",
            "vibevoice",
            "qwen3-asr",
            "smallest",
            "gemma4",
            "af-next",
            "none",
        ],
        help="Transcription provider (default: prompt user)",
    )
    parser.add_argument(
        "--asr-url",
        help="Offline ASR endpoint URL (own/remote/Tailnet-picked). Written to the "
        "selected provider's *_ASR_URL env var.",
    )
    parser.add_argument(
        "--asr-discover",
        action="store_true",
        help="Leave the ASR URL empty so the backend discovers chronicle-asr on the "
        "Tailnet at runtime ('configure from the Tailnet later').",
    )
    parser.add_argument(
        "--llm-base-url",
        help="Pin the llama.cpp LLM endpoint (own/remote/Tailnet-picked) via LLM_BASE_URL.",
    )
    parser.add_argument(
        "--llm-discover",
        action="store_true",
        help="Leave LLM_BASE_URL empty so the backend discovers chronicle-llm on the "
        "Tailnet at runtime.",
    )
    parser.add_argument(
        "--enable-https",
        action="store_true",
        help="Enable HTTPS configuration (default: prompt user)",
    )
    parser.add_argument(
        "--server-ip",
        help="Server IP/domain for SSL certificate (default: prompt user)",
    )
    parser.add_argument(
        "--ts-authkey",
        help="Tailscale auth key for Docker integration (default: prompt user)",
    )
    parser.add_argument(
        "--langfuse-public-key",
        help="LangFuse project public key (from langfuse init or external)",
    )
    parser.add_argument(
        "--langfuse-secret-key",
        help="LangFuse project secret key (from langfuse init or external)",
    )
    parser.add_argument(
        "--langfuse-host",
        help="LangFuse host URL (default: http://langfuse-web:3000 for local)",
    )
    parser.add_argument(
        "--langfuse-public-url",
        help="Browser-accessible LangFuse URL for dashboard deep-links "
        "(e.g. http://my-host:3002). Stored in config.yml as observability.langfuse.public_url",
    )
    parser.add_argument(
        "--streaming-provider",
        choices=["deepgram", "smallest", "qwen3-asr"],
        help="Streaming provider when different from batch (enables batch re-transcription)",
    )
    parser.add_argument(
        "--live-segmentation",
        choices=["streaming_stt", "windowed_batch"],
        help="Live transcription path: streaming_stt (default) or windowed_batch "
        "(batch-transcribe fixed windows when no streaming ASR)",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["openai", "ollama", "llamacpp", "custom", "gemma4-unified", "none"],
        help="LLM provider for memory extraction (default: prompt user)",
    )

    args = parser.parse_args()

    setup = ChronicleSetup(args)
    setup.run()


if __name__ == "__main__":
    main()
