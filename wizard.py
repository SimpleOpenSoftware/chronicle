#!/usr/bin/env python3
"""
Chronicle Root Setup Orchestrator
Handles service selection and delegation only - no configuration duplication
"""

import shutil
import subprocess
import sys
from pathlib import Path

# Shared setup helpers (extras/chronicle-setup, installed via setup-requirements.txt)
from chronicle_setup import (
    ConfigManager,
    decide_cert_mode,
    detect_tailscale_info,
    enable_tailscaled_at_boot,
    generate_tailscale_certs,
    is_placeholder,
    mask_value,
    prompt_password,
    prompt_with_existing_masked,
    read_env_value,
    tailscaled_enabled_at_boot,
)
from dotenv import set_key
from rich.console import Console
from rich.prompt import Confirm, Prompt

import discovery
import services

console = Console()


def get_existing_stt_provider(config_yml: dict):
    """Map config.yml defaults.stt value back to wizard provider name, or None."""
    stt = config_yml.get("defaults", {}).get("stt", "")
    mapping = {
        "stt-deepgram": "deepgram",
        "stt-deepgram-stream": "deepgram",
        "stt-parakeet-batch": "parakeet",
        "stt-vibevoice": "vibevoice",
        "stt-qwen3-asr": "qwen3-asr",
        "stt-smallest": "smallest",
        "stt-smallest-stream": "smallest",
        "stt-gemma4": "gemma4",
        "stt-af-next": "af-next",
        "stt-granite": "granite",
    }
    return mapping.get(stt)


def get_existing_stream_provider(config_yml: dict):
    """Map config.yml defaults.stt_stream value back to wizard streaming provider name, or None."""
    stt_stream = config_yml.get("defaults", {}).get("stt_stream", "")
    mapping = {
        "stt-deepgram-stream": "deepgram",
        "stt-smallest-stream": "smallest",
        "stt-qwen3-asr": "qwen3-asr",
        "stt-qwen3-asr-stream": "qwen3-asr",
        "stt-gemma4-stream": "gemma4",
        "stt-nemotron-stream": "nemotron",
    }
    return mapping.get(stt_stream)


# The repository root. Setup commands run from here because setup-requirements.txt
# lists chronicle-setup as a relative path and uv resolves that against the working
# directory. Each init.py locates its own files from __file__, so running from the
# root does not change which .env a service writes.
REPO_ROOT = Path(__file__).resolve().parent
LOCAL_LLAMACPP_BASE_URL = "http://llama-cpp-llm:8080/v1"


def setup_command(script_path: str) -> list:
    """Command that runs a service's init.py, to be launched with cwd=REPO_ROOT."""
    return [
        "uv",
        "run",
        "--with-requirements",
        "setup-requirements.txt",
        "python",
        script_path,
    ]


SERVICES = {
    "backend": {
        "backend": {
            "path": "backend",
            "cmd": setup_command("backend/init.py"),
            "description": "Chronicle backend with full feature set",
            "required": True,
        }
    },
    "extras": {
        "speaker-recognition": {
            "path": "extras/speaker-recognition",
            "cmd": setup_command("extras/speaker-recognition/init.py"),
            "description": "Speaker identification and enrollment",
        },
        "asr-services": {
            "path": "extras/asr-services",
            "cmd": setup_command("extras/asr-services/init.py"),
            "description": "Offline speech-to-text",
        },
        "langfuse": {
            "path": "extras/langfuse",
            "cmd": setup_command("extras/langfuse/init.py"),
            "description": "LLM observability and prompt management (local)",
        },
        "llm-services": {
            "path": "extras/llm-services",
            "cmd": setup_command("extras/llm-services/init.py"),
            "description": "Local LLM via llama.cpp (chat + embeddings)",
        },
        "wakeword-service": {
            "path": "extras/wakeword-service",
            "cmd": setup_command("extras/wakeword-service/init.py"),
            "description": "Hermes acoustic wake-word detection",
        },
        "tts": {
            "path": "extras/tts",
            "cmd": setup_command("extras/tts/init.py"),
            "description": "Text-to-speech (TADA / Fish Speech / KittenTTS)",
        },
        "colpali-service": {
            "path": "extras/colpali-service",
            "cmd": setup_command("extras/colpali-service/init.py"),
            "description": "Visual search over saved screenshots (ColPali)",
        },
    },
}

# Repo-root .env is the canonical store for the shared Hugging Face token: the wizard
# reads/writes it here, and each service's init.py also falls back to it. So a token
# set once (here or by hand) flows to every service that pulls models from HF.
ROOT_ENV_PATH = str(REPO_ROOT / ".env")

# Services whose containers pull (possibly gated) models from HuggingFace and thus
# benefit from an HF token (avoids 429 IP rate-limits, unlocks gated repos). The
# wizard prompts once if any of these is selected and passes --hf-token to each.
HF_TOKEN_SERVICES = {
    "speaker-recognition",
    "asr-services",
    "llm-services",
    "tts",
    "wakeword-service",
    "colpali-service",
}


def discover_available_plugins():
    """
    Discover plugins by scanning plugins directory.

    Returns:
        Dictionary mapping plugin_id to plugin metadata:
        {
            'plugin_id': {
                'has_setup': bool,
                'setup_path': Path or None,
                'dir': Path
            }
        }
    """
    # Plugin implementations live at the repository root; only the framework
    # (base.py, router.py, ...) is under backend/plugins. Scanning
    # the framework directory found zero plugins, so none were ever offered.
    plugins_dir = REPO_ROOT / "plugins"

    if not plugins_dir.exists():
        console.print(
            f"[yellow]Warning: Plugins directory not found: {plugins_dir}[/yellow]"
        )
        return {}

    discovered = {}
    skip_dirs = {"__pycache__", "__init__.py", "base.py", "router.py"}

    for plugin_dir in plugins_dir.iterdir():
        if not plugin_dir.is_dir() or plugin_dir.name in skip_dirs:
            continue

        plugin_id = plugin_dir.name
        setup_script = plugin_dir / "setup.py"

        discovered[plugin_id] = {
            "has_setup": setup_script.exists(),
            "setup_path": setup_script if setup_script.exists() else None,
            "dir": plugin_dir,
        }

    return discovered


def check_service_exists(service_name, service_config):
    """Check if service directory and script exist"""
    service_path = REPO_ROOT / service_config["path"]
    if not service_path.exists():
        return False, f"Directory {service_path} does not exist"

    # For services with Python init scripts, check if init.py exists
    if service_name in [
        "backend",
        "speaker-recognition",
        "asr-services",
        "langfuse",
        "llm-services",
        "wakeword-service",
        "tts",
        "colpali-service",
    ]:
        script_path = service_path / "init.py"
        if not script_path.exists():
            return False, f"Script {script_path} does not exist"
    else:
        # For other extras, check if setup.sh exists
        script_path = service_path / "setup.sh"
        if not script_path.exists():
            return (
                False,
                f"Script {script_path} does not exist (will be created in Phase 2)",
            )

    return True, "OK"


def select_services(
    transcription_provider=None,
    config_yml=None,
    memory_provider=None,
    llm_provider=None,
):
    """Let user select which services to setup"""
    config_yml = config_yml or {}
    console.print("🚀 [bold cyan]Chronicle Service Setup[/bold cyan]")
    console.print("Select which services to configure:\n")

    selected = []

    # Backend is required
    console.print("📱 [bold]Backend (Required):[/bold]")
    console.print("  ✅ Chronicle Backend - Full AI features")
    selected.append("backend")

    # Services that will be auto-added based on provider choices
    auto_added = set()
    if transcription_provider in (
        "parakeet",
        "vibevoice",
        "qwen3-asr",
        "gemma4",
        "af-next",
        "granite",
    ):
        auto_added.add("asr-services")
    if llm_provider == "llamacpp":
        auto_added.add("llm-services")

    # Optional extras
    console.print("\n🔧 [bold]Optional Services:[/bold]")
    for service_name, service_config in SERVICES["extras"].items():
        # Skip services that will be auto-added based on earlier choices
        if service_name in auto_added:
            if service_name == "llm-services":
                label = "llama.cpp"
            else:
                label = {
                    "vibevoice": "VibeVoice",
                    "parakeet": "Parakeet",
                    "qwen3-asr": "Qwen3-ASR",
                    "gemma4": "Gemma 4",
                    "af-next": "Audio Flamingo Next",
                    "granite": "Granite Speech",
                }.get(transcription_provider, transcription_provider)
            console.print(
                f"  ✅ {service_config['description']} ({label}) [dim](auto-selected)[/dim]"
            )
            continue

        # LangFuse is handled separately via setup_langfuse_choice()
        if service_name == "langfuse":
            continue

        # Check if service exists
        exists, msg = check_service_exists(service_name, service_config)
        if not exists:
            console.print(f"  ⏸️  {service_config['description']} - [dim]{msg}[/dim]")
            continue

        # Default to whatever was enabled last time (config.yml services map is the
        # source of truth) so a re-run is press-Enter-through. Smart per-service
        # heuristics below can still flip a never-configured service on.
        prior_enabled = bool(
            (config_yml.get("services") or {}).get(service_name, False)
        )

        # Determine smart default based on existing config
        if service_name == "speaker-recognition":
            # Also default True if speaker-recognition .env has a valid HF_TOKEN
            speaker_env = "extras/speaker-recognition/.env"
            existing_hf = read_env_value(speaker_env, "HF_TOKEN")
            default_enable = prior_enabled or bool(
                existing_hf
                and not is_placeholder(
                    existing_hf,
                    "your_huggingface_token_here",
                    "your-huggingface-token-here",
                    "hf_xxxxx",
                )
            )
        else:
            default_enable = prior_enabled

        try:
            enable_service = Confirm.ask(
                f"  Setup {service_config['description']}?", default=default_enable
            )
        except EOFError:
            console.print(f"Using default: {'Yes' if default_enable else 'No'}")
            enable_service = default_enable

        if enable_service:
            selected.append(service_name)

    return selected


def persist_enabled_services(selected_services):
    """Write the enabled-services map to config.yml — the source of truth for the
    lifecycle (services.py ``--all``).

    Replaces the old approach of renaming an unselected service's ``.env`` away to
    signal "disabled". Enabled/disabled is now declared explicitly in
    config/config.yml ``services:``, decoupled from whether a ``.env`` exists, so a
    stale or half-written ``.env`` never counts as "configured". Secrets in ``.env``
    are left untouched.
    """
    lifecycle_names = ["backend"] + list(SERVICES["extras"].keys())
    selected_lifecycle = set(selected_services)

    enabled = {name: (name in selected_lifecycle) for name in lifecycle_names}
    ConfigManager().set_enabled_services(enabled)

    on = ", ".join(name for name, is_on in enabled.items() if is_on)
    console.print(f"🧩 [dim]Enabled services written to config.yml: {on}[/dim]")


def run_service_setup(
    service_name,
    selected_services,
    https_enabled=False,
    server_ip=None,
    hf_token=None,
    transcription_provider="deepgram",
    admin_email=None,
    admin_password=None,
    langfuse_public_key=None,
    langfuse_secret_key=None,
    langfuse_host=None,
    langfuse_public_url=None,
    streaming_provider=None,
    llm_provider=None,
    memory_provider=None,
    hardware_profile=None,
    live_segmentation="streaming_stt",
    asr_url=None,
    asr_discover=False,
    llm_base_url=None,
    llm_discover=False,
    speaker_url=None,
    speaker_discover=False,
    tts_url=None,
    tts_discover=False,
):
    """Execute individual service setup script"""
    if service_name == "backend":
        service = SERVICES["backend"][service_name]

        # For the Chronicle backend, pass URLs of other selected services and HTTPS config.
        cmd = service["cmd"].copy()
        # Speaker Recognition URL: local service → compose DNS name; otherwise honor
        # the wizard's source choice (remote endpoint, or discover on the Tailnet).
        if "speaker-recognition" in selected_services:
            cmd.extend(["--speaker-service-url", "http://speaker-service:8085"])
        elif speaker_discover:
            cmd.append("--speaker-discover")
        elif speaker_url:
            cmd.extend(["--speaker-service-url", speaker_url])

        # TTS endpoint source (own/remote/Tailnet/discover).
        if tts_discover:
            cmd.append("--tts-discover")
        elif tts_url:
            cmd.extend(["--tts-url", tts_url])

        # Legacy local-parakeet wiring — skipped when the wizard chose an ASR source
        # (own/Tailnet/discover), which drives the URL via --asr-url/--asr-discover.
        if "asr-services" in selected_services and not (asr_url or asr_discover):
            cmd.extend(["--parakeet-asr-url", "host.docker.internal:8767"])

        # Pass transcription provider choice from wizard
        if transcription_provider:
            cmd.extend(["--transcription-provider", transcription_provider])

        # ASR source (where the offline provider runs): discover on the Tailnet,
        # or pin an own/remote/picked URL. Overrides the local default above.
        if asr_discover:
            cmd.append("--asr-discover")
        elif asr_url:
            cmd.extend(["--asr-url", asr_url])

        # LLM source for the Chronicle-managed llama.cpp endpoint.
        if llm_discover:
            cmd.append("--llm-discover")
        elif llm_base_url:
            cmd.extend(["--llm-base-url", llm_base_url])

        # Pass streaming provider (different from batch) for re-transcription setup
        if streaming_provider:
            cmd.extend(["--streaming-provider", streaming_provider])

        # Pass live-segmentation mode (windowed_batch when no streaming ASR)
        if live_segmentation:
            cmd.extend(["--live-segmentation", live_segmentation])

        # Add HTTPS configuration
        if https_enabled and server_ip:
            cmd.extend(["--enable-https", "--server-ip", server_ip])

        # Pass LLM provider choice
        if llm_provider:
            cmd.extend(["--llm-provider", llm_provider])

        # Pass LangFuse keys from langfuse init or external config
        if langfuse_public_key and langfuse_secret_key:
            cmd.extend(["--langfuse-public-key", langfuse_public_key])
            cmd.extend(["--langfuse-secret-key", langfuse_secret_key])
            if langfuse_host:
                cmd.extend(["--langfuse-host", langfuse_host])
            if langfuse_public_url:
                cmd.extend(["--langfuse-public-url", langfuse_public_url])

    else:
        service = SERVICES["extras"][service_name]
        cmd = service["cmd"].copy()

        # Centralized HF token: every HuggingFace-backed service gets the same token
        # (resolved once by setup_hf_token_if_needed / join_cluster) so its init.py
        # writes it into that service's .env.
        if service_name in HF_TOKEN_SERVICES and hf_token:
            cmd.extend(["--hf-token", hf_token])

        # Add HTTPS configuration for services that support it
        if service_name == "speaker-recognition" and https_enabled and server_ip:
            cmd.extend(["--enable-https", "--server-ip", server_ip])

        # For speaker-recognition, pass remaining centralized configuration
        if service_name == "speaker-recognition":
            # Define the speaker env path
            speaker_env_path = "extras/speaker-recognition/.env"

            # Pass explicit hardware profile selection when provided by wizard
            if hardware_profile == "strixhalo":
                cmd.extend(["--pytorch-cuda-version", "strixhalo"])
                cmd.extend(["--compute-mode", "gpu"])
                console.print(
                    "[blue][INFO][/blue] Using AMD Strix Halo profile for speaker recognition"
                )

            if not hf_token:
                console.print(
                    "[yellow][WARNING][/yellow] No HF_TOKEN provided - speaker recognition may fail to download models"
                )

            # Pass Deepgram API key from backend if available
            backend_env_path = "backend/.env"
            deepgram_key = read_env_value(backend_env_path, "DEEPGRAM_API_KEY")
            if deepgram_key and not is_placeholder(
                deepgram_key, "your_deepgram_api_key_here", "your-deepgram-api-key-here"
            ):
                cmd.extend(["--deepgram-api-key", deepgram_key])
                console.print(
                    "[blue][INFO][/blue] Found existing DEEPGRAM_API_KEY from backend config, reusing"
                )

            # Pass compute mode from existing .env if available
            compute_mode = read_env_value(speaker_env_path, "COMPUTE_MODE")
            if hardware_profile != "strixhalo" and compute_mode in ["cpu", "gpu"]:
                cmd.extend(["--compute-mode", compute_mode])
                console.print(
                    f"[blue][INFO][/blue] Found existing COMPUTE_MODE ({compute_mode}), reusing"
                )

        # For asr-services, pass provider from wizard's transcription choice and reuse CUDA version
        if service_name == "asr-services":
            # Map wizard transcription provider to asr-services provider name
            if hardware_profile == "strixhalo":
                wizard_to_asr_provider = {
                    "vibevoice": "vibevoice-strixhalo",
                    "parakeet": "nemo-strixhalo",
                    "qwen3-asr": "qwen3-asr",
                    "gemma4": "gemma4",
                    "af-next": "af-next",
                }
            else:
                wizard_to_asr_provider = {
                    "vibevoice": "vibevoice",
                    "parakeet": "nemo",
                    "qwen3-asr": "qwen3-asr",
                    "gemma4": "gemma4",
                    "af-next": "af-next",
                    "granite": "granite",
                    "nemotron": "nemotron",
                }
            # Prefer the batch provider; fall back to the streaming provider when the
            # batch one is cloud (no local container) but streaming is local — e.g.
            # batch=deepgram + streaming=nemotron must still configure the nemotron
            # container in asr-services.
            asr_provider = wizard_to_asr_provider.get(
                transcription_provider
            ) or wizard_to_asr_provider.get(streaming_provider)
            if asr_provider:
                cmd.extend(["--provider", asr_provider])
                console.print(
                    f"[blue][INFO][/blue] Pre-selecting ASR provider: {asr_provider}"
                )

            speaker_env_path = "extras/speaker-recognition/.env"
            cuda_version = read_env_value(speaker_env_path, "PYTORCH_CUDA_VERSION")
            if hardware_profile == "strixhalo":
                cmd.extend(["--pytorch-cuda-version", "strixhalo"])
                console.print(
                    "[blue][INFO][/blue] Using AMD Strix Halo profile for ASR services"
                )
            elif cuda_version and cuda_version in [
                "cu126",
                "cu128",
                "strixhalo",
            ]:
                cmd.extend(["--pytorch-cuda-version", cuda_version])
                console.print(
                    f"[blue][INFO][/blue] Found existing PYTORCH_CUDA_VERSION ({cuda_version}) from speaker-recognition, reusing"
                )

        # For langfuse, pass admin credentials from backend
        if service_name == "langfuse":
            if admin_email:
                cmd.extend(["--admin-email", admin_email])
            if admin_password:
                cmd.extend(["--admin-password", admin_password])
            if langfuse_public_url:
                cmd.extend(["--public-url", langfuse_public_url])

    console.print(f"\n🔧 [bold]Setting up {service_name}...[/bold]")

    # Check if service exists before running
    exists, msg = check_service_exists(service_name, service)
    if not exists:
        console.print(f"❌ {service_name} setup failed: {msg}")
        return False

    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            check=True,
            timeout=300,  # 5 minute timeout for service setup
        )

        console.print(f"✅ {service_name} setup completed")
        return True

    except FileNotFoundError as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        console.print(
            f"[yellow]   Check that the service directory exists: {service['path']}[/yellow]"
        )
        console.print(
            f"[yellow]   And that 'uv' is installed and on your PATH[/yellow]"
        )
        return False
    except subprocess.TimeoutExpired as e:
        console.print(f"❌ {service_name} setup timed out after {e.timeout}s")
        console.print(f"[yellow]   Configuration may be partially written.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(f"[yellow]   {' '.join(service['cmd'])}[/yellow]")
        return False
    except subprocess.CalledProcessError as e:
        console.print(f"❌ {service_name} setup failed with exit code {e.returncode}")
        console.print(f"[yellow]   Check the error output above for details.[/yellow]")
        console.print(f"[yellow]   To retry just this service:[/yellow]")
        console.print(f"[yellow]   {' '.join(service['cmd'])}[/yellow]")
        return False
    except Exception as e:
        console.print(f"❌ {service_name} setup failed: {e}")
        return False


def show_service_status():
    """Show which services are available"""
    console.print("\n📋 [bold]Service Status:[/bold]")

    # Check backend
    exists, msg = check_service_exists("backend", SERVICES["backend"]["backend"])
    status = "✅" if exists else "❌"
    console.print(f"  {status} Chronicle Backend - {msg}")

    # Check extras
    for service_name, service_config in SERVICES["extras"].items():
        exists, msg = check_service_exists(service_name, service_config)
        status = "✅" if exists else "⏸️"
        console.print(f"  {status} {service_config['description']} - {msg}")


def run_plugin_setup(plugin_id, plugin_info):
    """Run a plugin's setup.py script"""
    setup_path = plugin_info["setup_path"]

    try:
        # Run plugin setup script interactively (don't capture output)
        # This allows the plugin to prompt for user input
        result = subprocess.run(
            setup_command(str(setup_path)),
            cwd=REPO_ROOT,
        )

        if result.returncode == 0:
            console.print(f"\n[green]✅ {plugin_id} configured successfully[/green]")
            return True
        else:
            console.print(
                f"\n[red]❌ {plugin_id} setup failed with exit code {result.returncode}[/red]"
            )
            return False

    except Exception as e:
        console.print(f"[red]❌ Error running {plugin_id} setup: {e}[/red]")
        return False


def setup_plugins():
    """Discover and setup plugins via delegation"""
    console.print("\n🔌 [bold cyan]Plugin Configuration[/bold cyan]")
    console.print("Chronicle supports community plugins for extended functionality.\n")

    # Discover available plugins
    available_plugins = discover_available_plugins()

    if not available_plugins:
        console.print("[dim]No plugins found[/dim]")
        return

    # Ask about enabling community plugins
    try:
        enable_plugins = Confirm.ask("Enable community plugins?", default=True)
    except EOFError:
        console.print("Using default: Yes")
        enable_plugins = True

    if not enable_plugins:
        console.print("[dim]Skipping plugin configuration[/dim]")
        return

    # For each plugin with setup script
    configured_count = 0
    for plugin_id, plugin_info in available_plugins.items():
        if not plugin_info["has_setup"]:
            console.print(
                f"[dim]  {plugin_id}: No setup wizard available (configure manually)[/dim]"
            )
            continue

        # Ask if user wants to configure this plugin
        try:
            configure = Confirm.ask(f"  Configure {plugin_id} plugin?", default=False)
        except EOFError:
            configure = False

        if configure:
            # Delegate to plugin's setup script
            console.print(f"\n[cyan]Running {plugin_id} setup wizard...[/cyan]")
            success = run_plugin_setup(plugin_id, plugin_info)
            if success:
                configured_count += 1

    console.print(f"\n[green]✅ Configured {configured_count} plugin(s)[/green]")


def setup_git_hooks():
    """Setup pre-commit hooks for development"""
    console.print("\n🔧 [bold]Setting up development environment...[/bold]")

    # Check if git is available
    if not shutil.which("git"):
        console.print(
            "⚠️  [yellow]git not found, skipping git hooks setup (optional)[/yellow]"
        )
        return

    try:
        # Install pre-commit via uv tool (uv is our package manager)
        subprocess.run(
            ["uv", "tool", "install", "pre-commit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        # Install git hooks
        result = subprocess.run(
            ["pre-commit", "install", "--hook-type", "pre-push"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            console.print(
                "✅ [green]Git hooks installed (tests will run before push)[/green]"
            )
        else:
            console.print("⚠️  [yellow]Could not install git hooks (optional)[/yellow]")

        # Also install pre-commit hook
        subprocess.run(
            ["pre-commit", "install", "--hook-type", "pre-commit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    except Exception as e:
        console.print(f"⚠️  [yellow]Could not setup git hooks: {e} (optional)[/yellow]")


def _existing_hf_token():
    """Existing HF token, sourced like other shared secrets: backend .env first
    (the canonical hub on a main machine), then the repo-root .env (the per-node
    store for backend-less join nodes), then the legacy speaker-recognition .env.
    """
    for path in (
        "backend/.env",
        ROOT_ENV_PATH,
        "extras/speaker-recognition/.env",
    ):
        value = read_env_value(path, "HF_TOKEN")
        if value and not is_placeholder(
            value,
            "your_huggingface_token_here",
            "your-huggingface-token-here",
            "hf_xxxxx",
        ):
            return value
    return None


def _persist_hf_token(hf_token):
    """Write the resolved token to the canonical store: backend .env if it exists
    (main machine), else the repo-root .env (backend-less join node). Both are
    gitignored. Each service's init.py reads from the same locations.
    """
    backend_env = REPO_ROOT / "backend" / ".env"
    target = str(backend_env) if backend_env.exists() else ROOT_ENV_PATH
    Path(target).touch(mode=0o600, exist_ok=True)
    set_key(target, "HF_TOKEN", hf_token, quote_mode="never")
    return target


def setup_hf_token_if_needed(selected_services):
    """Prompt once for a shared Hugging Face token if any selected service needs it.

    Sources/stores it like other shared secrets (backend .env, falling back to the
    repo-root .env for join nodes) and returns it so run_service_setup can pass it to
    each service's init.py.

    Args:
        selected_services: List of service names selected by user

    Returns:
        HF_TOKEN string if provided, None otherwise
    """
    needing = [s for s in selected_services if s in HF_TOKEN_SERVICES]
    if not needing:
        return None

    console.print("\n🤗 [bold cyan]Hugging Face Token Configuration[/bold cyan]")
    console.print(
        "Used by HuggingFace-backed services ([cyan]"
        + ", ".join(needing)
        + "[/cyan]) — unlocks gated models and avoids download rate-limits."
    )
    console.print(
        "\n[blue][INFO][/blue] Get your token from: https://huggingface.co/settings/tokens"
    )

    # The pyannote models are gated and need explicit per-model agreement; only show
    # this when speaker-recognition is among the selected services.
    if "speaker-recognition" in needing:
        console.print()
        console.print(
            "[yellow]⚠️  Speaker recognition also needs you to accept these gated model agreements:[/yellow]"
        )
        console.print("   1. [cyan]Speaker Diarization[/cyan]")
        console.print(
            "      https://huggingface.co/pyannote/speaker-diarization-community-1"
        )
        console.print("   2. [cyan]Segmentation Model[/cyan]")
        console.print("      https://huggingface.co/pyannote/segmentation-3.0")
        console.print("   3. [cyan]Segmentation Model[/cyan]")
        console.print("      https://huggingface.co/pyannote/segmentation-3.1")
        console.print("   4. [cyan]Embedding Model[/cyan]")
        console.print(
            "      https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM"
        )
        console.print()
        console.print(
            "[yellow]→[/yellow] Open each link and click 'Agree and access repository'"
        )
        console.print(
            "[yellow]→[/yellow] Use the same Hugging Face account as your token"
        )
    console.print()

    hf_token = prompt_with_existing_masked(
        prompt_text="Hugging Face Token",
        existing_value=_existing_hf_token(),
        placeholders=[
            "your_huggingface_token_here",
            "your-huggingface-token-here",
            "hf_xxxxx",
        ],
        is_password=True,
        default="",
    )

    if hf_token:
        target = _persist_hf_token(hf_token)
        console.print(
            f"[green]✅ HF_TOKEN configured: {mask_value(hf_token)}[/green] "
            f"[dim](saved to {target})[/dim]\n"
        )
        return hf_token
    else:
        console.print(
            "[yellow]⚠️  No HF_TOKEN provided — gated/large HuggingFace models may fail to download[/yellow]\n"
        )
        return None


# Providers that support real-time streaming
STREAMING_CAPABLE = {"deepgram", "smallest", "qwen3-asr", "gemma4", "nemotron"}

# STT providers that can also serve as LLM (unified multimodal models)
UNIFIED_CAPABLE_STT = {"gemma4"}


def _scan_tailnet_services(discovery_name: str) -> list:
    """Advertised instances of a chronicle-* service on the Tailnet as [{host, url}].

    Empty when Tailscale/minidisc is unavailable or nothing is advertised.
    """
    try:
        found = []
        for svc in discovery.list_all_services() or []:
            if svc.get("name") != discovery_name:
                continue
            addr, port = svc.get("address"), svc.get("port")
            host = (svc.get("labels") or {}).get("host", addr)
            if addr and port:
                found.append({"host": host, "url": f"http://{addr}:{port}"})
        return found
    except Exception:
        return []


def _infer_source_mode(current):
    """Infer a prior source mode from an existing URL value (for press-Enter defaults).

    ``None`` = not configured before; ``""`` = was set empty (discover/later); a local
    host → local; a Tailscale address → tailnet; anything else → own.
    """
    if current is None:
        return None
    if current == "":
        return "later"
    low = current.lower()
    if any(
        h in low
        for h in (
            "host.docker.internal",
            "localhost",
            "127.0.0.1",
            "172.17.0.1",
            "speaker-service",
            "llama-cpp-llm",
        )
    ):
        return "local"
    if ".ts.net" in low or any(
        low.split("://")[-1].startswith(p) for p in ("100.", "fd7a:")
    ):
        return "tailnet"
    return "own"


def select_service_source(
    label: str,
    discovery_name: str,
    allow_later: bool = True,
    allow_local: bool = True,
    current: str = None,
):
    """Ask WHERE a remote-capable service runs (hub default), returning a source dict.

    Returns one of:
      {"mode": "local"}                       — run it on this hub (caller's default flow)
      {"mode": "own",     "url": "<url>"}     — an existing/external endpoint
      {"mode": "tailnet", "url": "<url>"}     — pin a node advertised on the Tailnet now
      {"mode": "later"}                       — leave unset; backend discovers it at runtime

    ``allow_local=False`` drops the on-this-hub option (used when the service was
    already declined for local setup), defaulting to discover-later. ``current`` is the
    previously-configured URL ('' = was discover) used to default the menu + prefill, so
    a re-run is press-Enter-through.
    """
    console.print(f"\n🛰️  [bold cyan]{label} — where does it run?[/bold cyan]")
    # Stable choice keys regardless of which options are shown.
    options: dict[str, tuple[str, str]] = {}
    if allow_local:
        options["1"] = ("local", "On this hub (run it here)")
    options["2"] = ("own", "My own / external endpoint (enter a URL)")
    options["3"] = ("tailnet", "Pick a node advertised on the Tailnet now")
    if allow_later:
        options["4"] = (
            "later",
            "Configure from the Tailnet later (auto-discover at runtime)",
        )
    for k, (_mode, desc) in options.items():
        console.print(f"  {k}) {desc}")

    default_choice = "1" if allow_local else ("4" if allow_later else "2")
    # Default to the previously-configured source so a re-run is press-Enter-through.
    prior_mode = _infer_source_mode(current)
    mode_to_key = {m: k for k, (m, _d) in options.items()}
    if prior_mode in mode_to_key:
        default_choice = mode_to_key[prior_mode]
        console.print(f"[dim]  (previously: {prior_mode})[/dim]")

    # No-op fallback when a sub-step is abandoned: prefer local, else discover-later.
    _fallback = {"mode": "local"} if allow_local else {"mode": "later"}
    try:
        choice = Prompt.ask("Enter choice", default=default_choice)
    except EOFError:
        choice = default_choice
    if choice not in options:
        choice = default_choice

    if choice == "2":
        own_default = current if _infer_source_mode(current) == "own" else ""
        try:
            url = Prompt.ask(
                f"{label} endpoint URL (e.g. http://host:8767)", default=own_default
            ).strip()
        except EOFError:
            url = own_default
        if url:
            return {"mode": "own", "url": url}
        console.print("[yellow]No URL entered — falling back.[/yellow]")
        return _fallback

    if choice == "3":
        found = _scan_tailnet_services(discovery_name)
        if not found:
            console.print(
                f"[yellow]No '{discovery_name}' advertised on the Tailnet.[/yellow] "
                + (
                    "Falling back to 'configure later'."
                    if allow_later
                    else "Using fallback."
                )
            )
            return {"mode": "later"} if allow_later else _fallback
        console.print(f"[green]Found {len(found)} on the Tailnet:[/green]")
        for i, a in enumerate(found, 1):
            console.print(f"  {i}) {a['host']} — {a['url']}")
        # Pre-select the previously-pinned node if it's still advertised.
        default_pick = "1"
        if current:
            base = current.rstrip("/").removesuffix("/v1")
            for i, a in enumerate(found, 1):
                if a["url"].rstrip("/") == base:
                    default_pick = str(i)
                    break
        try:
            pick = Prompt.ask("Pick one", default=default_pick)
            idx = int(pick) - 1
        except (EOFError, ValueError):
            idx = int(default_pick) - 1
        if 0 <= idx < len(found):
            console.print(f"[green]✅[/green] Using {found[idx]['url']}")
            return {"mode": "tailnet", "url": found[idx]["url"]}
        return {"mode": "later"} if allow_later else _fallback

    if choice == "4" and allow_later:
        console.print(
            "[green]✅[/green] Will auto-discover on the Tailnet at runtime (no URL pinned)"
        )
        return {"mode": "later"}

    return _fallback


def select_transcription_provider(
    config_yml: dict = None, default_provider: str = None
):
    """Ask user which transcription (batch/high-quality) provider they want.

    ``default_provider`` (the streaming provider chosen first) pre-selects the
    matching batch option when that provider can also do batch — the user can still
    pick a different provider for higher-quality batch transcription.
    """
    config_yml = config_yml or {}
    existing_provider = get_existing_stt_provider(config_yml)

    provider_to_choice = {
        "deepgram": "1",
        "parakeet": "2",
        "vibevoice": "3",
        "qwen3-asr": "4",
        "smallest": "5",
        "gemma4": "6",
        "af-next": "7",
        "granite": "8",
        "none": "9",
    }
    choice_to_provider = {v: k for k, v in provider_to_choice.items()}

    # Prefer the streaming provider (it can also do batch), else existing config.
    preferred = default_provider if default_provider in provider_to_choice else None
    default_choice = provider_to_choice.get(preferred or existing_provider, "1")

    console.print("\n🎤 [bold cyan]Transcription Provider[/bold cyan]")
    console.print(
        "Choose your speech-to-text provider for [bold]batch[/bold]/high-quality transcription:"
    )
    if preferred:
        console.print(
            f"[dim]Defaulting to {preferred} (your streaming choice — it does batch too). "
            f"Pick another for a different/higher-quality batch engine.[/dim]"
        )
    elif existing_provider:
        provider_labels = {
            "deepgram": "Deepgram",
            "parakeet": "Parakeet ASR",
            "vibevoice": "VibeVoice ASR",
            "qwen3-asr": "Qwen3-ASR",
            "smallest": "Smallest.ai Pulse",
            "gemma4": "Gemma 4",
            "af-next": "Audio Flamingo Next",
            "granite": "Granite Speech",
        }
        console.print(
            f"[blue][INFO][/blue] Current: {provider_labels.get(existing_provider, existing_provider)}"
        )
    console.print()

    choices = {
        "1": "Deepgram (cloud, streaming + batch)",
        "2": "Parakeet ASR (offline, batch only, GPU)",
        "3": "VibeVoice ASR (offline, batch only, built-in diarization, GPU)",
        "4": "Qwen3-ASR (offline, streaming + batch, 52 languages, GPU)",
        "5": "Smallest.ai Pulse (cloud, streaming + batch)",
        "6": "Gemma 4 (offline, streaming + batch, prompt-based diarization, MTP, GPU)",
        "7": "Audio Flamingo Next (offline, batch, timestamped diarization, GPU; noncommercial license)",
        "8": "IBM Granite Speech (offline, batch, LLM-backbone, en/fr/de/es/pt, GPU)",
        "9": "None (skip transcription setup)",
    }

    for key, desc in choices.items():
        marker = " [dim](default)[/dim]" if key == default_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
            if choice in choices:
                return choice_to_provider[choice]
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            console.print(f"Using default: {choices.get(default_choice, 'Deepgram')}")
            return choice_to_provider.get(default_choice, "deepgram")


def select_streaming_provider(config_yml: dict = None):
    """Ask which real-time streaming provider to use (or skip).

    Asked BEFORE the batch provider so a streaming-capable choice can default the
    batch selection — the common case is one provider doing both, but the user can
    still pick a different (e.g. higher-quality) batch engine next.

    Returns:
        Streaming provider name, or None if streaming is skipped.
    """
    config_yml = config_yml or {}
    existing_stream = get_existing_stream_provider(config_yml)

    console.print("\n🔊 [bold cyan]Real-time Streaming Transcription[/bold cyan]")
    console.print(
        "Choose a provider for [bold]real-time[/bold] (live) transcription. "
        "You'll pick the batch/high-quality provider next."
    )
    console.print(
        "[dim]A provider that also does batch will be offered as the batch default too.[/dim]"
    )
    console.print()

    options = [
        ("deepgram", "Deepgram (cloud, streaming)"),
        ("smallest", "Smallest.ai Pulse (cloud, streaming)"),
        ("qwen3-asr", "Qwen3-ASR (offline, streaming, GPU)"),
        (
            "gemma4",
            "Gemma 4 (offline, streaming-ish, prompt-based diarization, MTP, GPU)",
        ),
        (
            "nemotron",
            "Nemotron 3.5 (offline, true cache-aware streaming ~100ms, GPU)",
        ),
    ]
    streaming_choices = {}
    provider_map = {}
    for idx, (name, desc) in enumerate(options, start=1):
        streaming_choices[str(idx)] = desc
        provider_map[str(idx)] = name
    skip_key = str(len(options) + 1)
    streaming_choices[skip_key] = "Skip (no real-time streaming)"
    provider_map[skip_key] = None

    # Default to the previously-configured streaming provider, else skip.
    default_stream_choice = skip_key
    for k, v in provider_map.items():
        if v and v == existing_stream:
            default_stream_choice = k
            break

    for key, desc in streaming_choices.items():
        marker = " [dim](current)[/dim]" if key == default_stream_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_stream_choice)
            if choice in streaming_choices:
                result = provider_map[choice]
                if result:
                    console.print(f"[green]✅[/green] Streaming: {result}")
                else:
                    console.print("[blue][INFO][/blue] No real-time streaming")
                return result
            console.print(
                f"[red]Invalid choice. Please select from {list(streaming_choices.keys())}[/red]"
            )
        except EOFError:
            return provider_map.get(default_stream_choice)


def select_live_segmentation(batch_provider):
    """When there's no streaming ASR, offer windowed-batch live transcription.

    Without a streaming ASR, a continuously-streaming source is only transcribed when
    it disconnects (24h+ for always-on sources). Windowed batch transcribes fixed
    ~30s windows so conversations are created incrementally as audio streams in.

    Returns:
        "windowed_batch" or "streaming_stt".
    """
    console.print(
        "\n🪟 [bold cyan]Live transcription without streaming ASR[/bold cyan]"
    )
    console.print(
        f"{batch_provider} is batch-only and you skipped streaming. Without live "
        "transcription, a continuously-streaming source is only transcribed when it "
        "disconnects."
    )
    try:
        enable = Confirm.ask(
            "Enable windowed batch transcription (transcribe ~every 30s as audio streams in)?",
            default=True,
        )
    except EOFError:
        return "streaming_stt"

    if enable:
        console.print("[green]✅[/green] Live segmentation: windowed_batch")
        return "windowed_batch"
    return "streaming_stt"


def derive_langfuse_public_url(
    langfuse_mode, langfuse_external, server_ip, https_enabled
):
    """Derive the browser-accessible LangFuse URL used for dashboard deep-links.

    This becomes ``observability.langfuse.public_url`` in config.yml, which the
    backend serves to the web UI for Langfuse trace/session links.

    - external mode: the host the user entered is already browser-accessible.
    - local mode with Chronicle HTTPS: Caddy serves the bundled instance on 3443.
    - local mode without HTTPS: fall back to the directly-published HTTP port 3002.
      In both cases use the selected/detected Tailscale name when available.
    """
    if langfuse_mode == "external":
        return langfuse_external.get("host")

    host = server_ip
    if not host:
        ts_dns, ts_ip = detect_tailscale_info()
        host = ts_dns or ts_ip or "localhost"
    scheme = "https" if https_enabled else "http"
    port = "3443" if https_enabled else "3002"
    return f"{scheme}://{host}:{port}"


def setup_langfuse_choice():
    """Ask user about LangFuse configuration: local or external.

    LangFuse is always enabled (required for prompt management and observability).
    The only choice is whether to use the bundled local instance or an existing external one.

    Returns:
        Tuple of (mode, config) where:
        - mode: 'local' or 'external'
        - config: dict with keys {host, public_key, secret_key} for external, empty for local
    """
    console.print("\n📊 [bold cyan]LangFuse Configuration[/bold cyan]")
    console.print("LangFuse provides LLM observability, tracing, and prompt management")
    console.print()

    try:
        has_existing = Confirm.ask(
            "Use an existing external LangFuse instance instead of local?",
            default=False,
        )
    except EOFError:
        console.print("Using default: No (will set up locally)")
        has_existing = False

    if not has_existing:
        # Check if the local langfuse directory exists
        exists, msg = check_service_exists("langfuse", SERVICES["extras"]["langfuse"])
        if exists:
            console.print("[green]✅[/green] Will set up local LangFuse instance")
            return "local", {}
        else:
            console.print(f"[yellow]⚠️  Local LangFuse not available: {msg}[/yellow]")
            console.print(
                "[yellow]   Will proceed without LangFuse — add it later when available[/yellow]"
            )
            return "local", {}

    # External LangFuse — collect connection details
    console.print()
    console.print("[bold]Enter your external LangFuse connection details:[/bold]")

    backend_env_path = "backend/.env"

    existing_host = read_env_value(backend_env_path, "LANGFUSE_HOST")
    # Don't treat the local docker host as an existing external value
    if existing_host and "langfuse-web" in existing_host:
        existing_host = None

    host = prompt_with_existing_masked(
        prompt_text="LangFuse host URL",
        existing_value=existing_host,
        placeholders=[""],
        is_password=False,
        default="https://cloud.langfuse.com",
    )

    existing_pub = read_env_value(backend_env_path, "LANGFUSE_PUBLIC_KEY")
    public_key = prompt_with_existing_masked(
        prompt_text="LangFuse public key",
        existing_value=existing_pub,
        placeholders=[""],
        is_password=False,
        default="",
    )

    existing_sec = read_env_value(backend_env_path, "LANGFUSE_SECRET_KEY")
    secret_key = prompt_with_existing_masked(
        prompt_text="LangFuse secret key",
        existing_value=existing_sec,
        placeholders=[""],
        is_password=True,
        default="",
    )

    if not (host and public_key and secret_key):
        console.print(
            "[yellow]⚠️  Incomplete LangFuse configuration — skipping[/yellow]"
        )
        return None, {}

    console.print(f"[green]✅[/green] External LangFuse configured: {host}")
    return "external", {
        "host": host,
        "public_key": public_key,
        "secret_key": secret_key,
    }


def select_hardware_profile(
    selected_services, transcription_provider, streaming_provider
):
    """Select hardware profile for GPU-backed optional services.

    Returns:
        "strixhalo" for AMD Strix Halo profile, otherwise None.
    """
    strix_capable_providers = {"parakeet", "vibevoice"}
    needs_hardware_choice = (
        "speaker-recognition" in selected_services
        or transcription_provider in strix_capable_providers
        or streaming_provider in strix_capable_providers
    )

    if not needs_hardware_choice:
        return None

    console.print("\n🧠 [bold cyan]Hardware Profile[/bold cyan]")
    console.print(
        "Choose target hardware for GPU services (speaker recognition and offline ASR):"
    )
    choices = {
        "1": "Standard (CPU/NVIDIA CUDA)",
        "2": "AMD Strix Halo (ROCm, gfx1151 / Ryzen AI Max)",
    }
    for key, desc in choices.items():
        console.print(f"  {key}) {desc}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default="1")
            if choice == "1":
                return None
            if choice == "2":
                console.print(
                    "[green]✅[/green] Using AMD Strix Halo profile where supported"
                )
                return "strixhalo"
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            return None


def select_llm_provider(
    config_yml: dict = None, transcription_provider: str = None
) -> str:
    """Ask user which LLM provider to use for memory extraction.

    Uses Langfuse-style flow: "Do you have your own LLM?" → Yes: custom URL → No: pick managed option.
    When transcription_provider is a unified-capable model (e.g. Gemma 4), offers to reuse
    it for LLM tasks too.

    Returns:
        "openai", "ollama", "llamacpp", "gemma4-unified", or "none"
    """
    config_yml = config_yml or {}
    existing_llm = config_yml.get("defaults", {}).get("llm", "")
    existing_is_custom = existing_llm in ("custom-llm",)
    existing_is_unified = existing_llm == "gemma4-llm"

    console.print("\n🤖 [bold cyan]LLM Provider[/bold cyan]")
    console.print(
        "Choose your language model provider for memory extraction and analysis:"
    )
    console.print()

    # If the STT provider is a unified-capable model, offer to reuse it for LLM
    if transcription_provider in UNIFIED_CAPABLE_STT:
        provider_labels = {"gemma4": "Gemma 4"}
        label = provider_labels.get(transcription_provider, transcription_provider)
        console.print(
            f"[green]💡[/green] {label} is a multimodal model that can also handle LLM tasks "
            "(memory extraction, chat, summaries)."
        )
        console.print(
            f"[dim]This reuses the same model already loaded for STT — no extra GPU memory needed.[/dim]"
        )
        default_unified = existing_is_unified or True
        try:
            use_unified = Confirm.ask(
                f"Use {label} for both STT and LLM?",
                default=default_unified,
            )
        except EOFError:
            use_unified = default_unified
        if use_unified:
            console.print(
                f"[green]✅[/green] {label} will handle both STT and LLM (unified mode)"
            )
            return "gemma4-unified"
        console.print(f"[dim]OK, choosing a separate LLM provider instead.[/dim]")
        console.print()

    # Step 1: Do you have your own LLM endpoint?
    try:
        has_own = Confirm.ask(
            "Do you have your own OpenAI-compatible LLM endpoint?",
            default=existing_is_custom,
        )
    except EOFError:
        has_own = existing_is_custom

    if has_own:
        # User has their own endpoint — this maps to the existing "custom" flow in init.py
        console.print(
            "[green]✅[/green] Will configure custom LLM endpoint in backend setup"
        )
        return "custom"

    # Step 2: Pick from managed options
    llm_to_choice = {
        "openai-llm": "1",
        "local-llm": "2",
        "llamacpp-llm": "3",
        "muse-glimmer-llm": "3",
    }
    default_choice = llm_to_choice.get(existing_llm, "1")

    choices = {
        "1": "OpenAI (GPT-4o-mini, requires API key)",
        "2": "Ollama (local models, runs on your machine)",
        "3": "llama.cpp (Chronicle-managed, local GGUF models, GPU recommended)",
        "4": "None (skip memory extraction)",
    }

    for key, desc in choices.items():
        marker = " [dim](current)[/dim]" if key == default_choice else ""
        console.print(f"  {key}) {desc}{marker}")
    console.print()

    while True:
        try:
            choice = Prompt.ask("Enter choice", default=default_choice)
            if choice in choices:
                return {"1": "openai", "2": "ollama", "3": "llamacpp", "4": "none"}[
                    choice
                ]
            console.print(
                f"[red]Invalid choice. Please select from {list(choices.keys())}[/red]"
            )
        except EOFError:
            console.print(f"Using default: {choices.get(default_choice, 'OpenAI')}")
            return {"1": "openai", "2": "ollama", "3": "llamacpp", "4": "none"}.get(
                default_choice, "openai"
            )


def maybe_install_agent_services():
    """Offer to install the boot-persistence systemd user services.

    Installs two units (with linger): the native node agent (:8775 — WebUI control
    + Tailnet advertising), which runs on the host and so doesn't survive a reboot
    on its own; and a oneshot that runs ``start --all`` on boot to bring the
    container stack back. The latter matters under rootless Podman, which — unlike
    Docker — has no daemon to re-apply ``restart:`` policies after a reboot.
    """
    console.print("\n🔁 [bold cyan]Auto-start on boot (Optional)[/bold cyan]")
    console.print(
        "Installs systemd user services so both the node agent (:8775) and your"
    )
    console.print(
        "container stack come back after a reboot. (Rootless Podman, unlike Docker,"
    )
    console.print(
        "has no daemon to revive containers on boot, so this is needed there.)"
    )

    if not services._systemd_user_available():
        services._print_systemd_unavailable_help()
        return

    try:
        install = Confirm.ask(
            "Install it as a systemd user service so it auto-starts on boot?",
            default=True,
        )
    except EOFError:
        console.print("Using default: Yes")
        install = True

    if install:
        services.install_systemd_agents()


def maybe_enable_remote_control():
    """Offer to run a Claude remote-control session so you can start Claude Code
    sessions on this machine from the Claude mobile app.

    Off by default: this launches `claude remote-control` (in tmux) and, if you
    accept, installs it as a systemd user service so it survives reboots. Requires
    the claude CLI (logged in) and tmux on the host.
    """
    console.print("\n📱 [bold cyan]Claude Code from your phone (Optional)[/bold cyan]")
    console.print(
        "Run a `claude remote-control` server on this host so you can spawn new"
    )
    console.print(
        "Claude Code sessions from the Claude mobile app (Code tab). It runs in tmux"
    )
    console.print(
        "and can auto-start on boot. Toggle it any time from the WebUI System page."
    )

    if shutil.which("claude") is None:
        console.print(
            "[dim]claude CLI not found — skipping. Install Claude Code and log in, "
            "then run: ./services remote-control install[/dim]"
        )
        return
    if shutil.which("tmux") is None:
        console.print("[dim]tmux not found — skipping (install tmux first).[/dim]")
        return

    try:
        enable = Confirm.ask(
            "Enable Claude remote-control (start new sessions from your phone)?",
            default=False,
        )
    except EOFError:
        console.print("Using default: No")
        enable = False

    if not enable:
        return

    if services._systemd_user_available():
        services.install_remote_control()
    else:
        # No systemd user instance (e.g. WSL without systemd=true) — start it now
        # in tmux; it won't survive a reboot.
        services._print_systemd_unavailable_help()
        services.start_remote_control()


# Services that make sense to run on a service-only node joining a cluster
# (the compute-heavy / GPU ones the backend reaches over the Tailnet).
JOINABLE_SERVICES = {
    "asr-services": "Offline speech-to-text (ASR) — GPU",
    "speaker-recognition": "Speaker identification — GPU",
    "tts": "Text-to-speech — GPU",
    "llm-services": "Local LLM via llama.cpp — GPU",
    "wakeword-service": "Acoustic wake-word detection",
    "colpali-service": "Visual search over saved screenshots — GPU",
}


def select_setup_type():
    """Ask whether this machine is the main hub, joins a cluster, or is a companion.

    Returns ``"join"`` for a service-only node that contributes a service to an
    existing backend, ``"companion"`` for a laptop/desktop that only views data from
    an existing server (vault sync client — no backend, no containers), else
    ``"main"`` (the normal full single-machine / hub setup). Defaults to ``"main"``
    so re-running the wizard on the hub is unchanged.
    """
    console.print("\n🏗️  [bold cyan]Setup type[/bold cyan]")
    console.print(
        "  1) Main machine — run the Chronicle backend here (single machine or cluster hub)"
    )
    console.print(
        "  2) Join a cluster — this machine only runs a service (e.g. GPU ASR) and"
    )
    console.print(
        "     advertises it to an existing backend on your Tailnet (no backend here)"
    )
    console.print(
        "  3) Capture node — run ScreenPipe + the Chronicle companion (no containers)"
    )
    console.print(
        "  4) Companion device — this laptop/desktop just views your Chronicle data:"
    )
    console.print(
        "     syncs your memory vault locally for Obsidian (macOS menu bar app;"
    )
    console.print("     no backend, no containers, no Tailscale required)")
    console.print()
    choice = Prompt.ask("Enter choice", default="1")
    return {"2": "join", "3": "capture", "4": "companion"}.get(choice.strip(), "main")


def setup_capture_node():
    """Delegate ScreenPipe capture-node setup to its separate companion."""
    init_script = REPO_ROOT / "extras/screenpipe-collector/init.py"
    if not init_script.exists():
        console.print(f"[red]✗ Capture-node setup is missing: {init_script}[/red]")
        return False

    backend_url = discovery.discover_service(discovery.CHRONICLE_BACKEND)
    cmd = setup_command("extras/screenpipe-collector/init.py")
    if backend_url:
        console.print(
            f"[green]✅[/green] Found Chronicle at [cyan]{backend_url}[/cyan]"
        )
        cmd.extend(["--backend", backend_url])
    try:
        subprocess.run(cmd, cwd=REPO_ROOT, check=True)
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        console.print(f"[red]✗ Capture-node setup failed: {exc}[/red]")
        return False


def setup_companion():
    """Configure THIS machine as a companion device (vault viewer, no backend).

    Delegates to extras/vault-sync/init.py: a macOS menu bar app that keeps this
    user's memory vault synced from an existing Chronicle server so it can be
    browsed locally (e.g. in Obsidian). Needs only the server's URL + the user's
    login; the server address can be a Tailnet name, LAN IP, or public domain.
    """
    console.print("\n💻 [bold cyan]Companion device setup[/bold cyan]")
    if sys.platform != "darwin":
        console.print(
            "[red]✗ The companion (vault sync) app is macOS-only for now.[/red] "
            "Linux/Windows\n  support is planned. You can still pair a plain Syncthing "
            "manually against the\n  backend's /api/vault-sync broker — see "
            "extras/vault-sync/README.md."
        )
        return
    subprocess.run(setup_command("extras/vault-sync/init.py"), cwd=REPO_ROOT)


def join_cluster():
    """Configure THIS machine as a service-only node joining an existing cluster.

    Discovers the hub (backend) on the Tailnet, lets you pick which service(s) this
    box provides, runs their init wizards, starts them, and runs the node agent so
    they advertise on the Tailnet — the hub discovers and uses them automatically.
    This box does NOT run the backend.
    """
    console.print("\n🔗 [bold cyan]Join an existing Chronicle cluster[/bold cyan]")
    console.print(
        "This machine will run one or more services (e.g. GPU ASR) and advertise them on\n"
        "your Tailnet. Your main Chronicle backend then discovers and uses them.\n"
    )

    # 0. Tailscale prereq — discovery AND advertising both need it. Check up front so
    # a missing/down Tailscale gives the real cause, not a misleading "no backend found".
    if shutil.which("tailscale") is None:
        console.print(
            "[red]✗ Tailscale isn't installed.[/red] A join node finds the hub and advertises\n"
            "  its services over your Tailnet. Install it (https://tailscale.com/download),\n"
            "  run [cyan]sudo tailscale up[/cyan], then re-run this wizard."
        )
        return
    try:
        ts_connected = (
            subprocess.run(["tailscale", "status"], capture_output=True).returncode == 0
        )
    except OSError:
        ts_connected = False
    if not ts_connected:
        console.print(
            "[red]✗ Tailscale is installed but not connected.[/red] Run "
            "[cyan]sudo tailscale up[/cyan]\n  (approve this device on your tailnet), then re-run."
        )
        if not Confirm.ask(
            "Continue anyway? (discovery/advertising won't work — you'd wire service URLs manually)",
            default=False,
        ):
            return

    # 0b. Tailscale running now ≠ Tailscale after a reboot. If the unit is started but
    # not enabled, it silently won't come back on boot and the node drops off the
    # Tailnet (services unreachable) until someone notices. Offer to make it stick.
    if tailscaled_enabled_at_boot() is False:
        console.print(
            "[yellow]⚠️  Tailscale is running but not enabled to start on boot.[/yellow]\n"
            "   After a reboot this node would silently drop off the Tailnet (services\n"
            "   unreachable) until you start it again manually."
        )
        if Confirm.ask(
            "Enable tailscaled to start on boot now? (sudo systemctl enable --now tailscaled)",
            default=True,
        ):
            if enable_tailscaled_at_boot():
                console.print(
                    "[green]✅[/green] tailscaled enabled — it'll survive reboots now."
                )
            else:
                console.print(
                    "[red]✗ Couldn't enable it.[/red] Run it yourself: "
                    "[cyan]sudo systemctl enable --now tailscaled[/cyan]"
                )

    # 1. Discover the hub + what's already advertised in the cluster.
    console.print("🔍 Looking for your Chronicle backend on the Tailnet…")
    backend_url = discovery.discover_service(discovery.CHRONICLE_BACKEND)
    claimed = {s.get("name") for s in discovery.list_all_services()}
    if backend_url:
        console.print(f"[green]✅[/green] Found backend at [cyan]{backend_url}[/cyan]")
    else:
        console.print(
            "[yellow]⚠️  No backend discovered on the Tailnet.[/yellow] Make sure your main\n"
            "   machine is running with Tailscale and this box is on the same Tailnet."
        )
        if not Confirm.ask("Continue anyway?", default=True):
            return
    if claimed:
        console.print("\n[dim]Already advertised on the Tailnet:[/dim]")
        for name in sorted(n for n in claimed if n):
            console.print(f"   [dim]• {name}[/dim]")

    # 2. Pick the service(s) this node will provide.
    disc_names = services._DISCOVERY_NAMES  # lifecycle name → chronicle-* name
    console.print("\n📦 [bold]Which service(s) will THIS machine provide?[/bold]")
    keys = list(JOINABLE_SERVICES)
    for i, svc in enumerate(keys, 1):
        taken = disc_names.get(svc) in claimed
        tag = (
            "  [yellow](already in cluster — a 2nd one is usually unnecessary)[/yellow]"
            if taken
            else ""
        )
        console.print(f"  {i}) {svc} — {JOINABLE_SERVICES[svc]}{tag}")
    raw = Prompt.ask("Enter number(s), comma-separated", default="1")
    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(keys):
            svc = keys[int(part) - 1]
            if svc not in chosen:
                chosen.append(svc)
    if not chosen:
        console.print("[red]No valid services selected. Aborting.[/red]")
        return
    console.print(f"[green]✅[/green] This node will provide: {', '.join(chosen)}")

    # 3. Hardware profile (e.g. Strix Halo) for GPU services.
    hardware_profile = select_hardware_profile(chosen, None, None)

    # 4. Enable ONLY these services in config.yml — a join node runs no backend.
    persist_enabled_services(chosen)

    # 4b. A join node has no backend .env, so the shared HF token lives in the repo-root
    #     .env here. Prompt once (if any chosen service needs it) and pass it through.
    hf_token = setup_hf_token_if_needed(chosen)

    # 5. Configure each chosen service (runs its init.py interactively).
    for svc in chosen:
        run_service_setup(
            svc, chosen, hf_token=hf_token, hardware_profile=hardware_profile
        )

    # 6. Start the service(s) + the node agent (which advertises on the Tailnet).
    #    build=True because images won't exist yet on a fresh node.
    console.print("\n🚀 Starting services + node agent…")
    # Deferred so the setup wizard does not initialize the operator CLI at startup.
    from service_cli import main as service_cli

    if service_cli(["start", *chosen, "--build"]) != 0:
        console.print(
            "[red]Service startup failed; inspect ./services status --detailed[/red]"
        )
        return

    # 7. Offer boot persistence for the node agent (systemd user service).
    maybe_install_agent_services()

    # 8. Next steps + the one wiring gotcha.
    console.print("\n🎉 [bold green]This node has joined the cluster![/bold green]")
    console.print(
        "   • It's advertising on your Tailnet — it'll appear on the backend's Network page."
    )
    console.print(
        "   • [yellow]Wiring note:[/yellow] if your backend pins the service URL to "
        "host.docker.internal/localhost"
    )
    console.print(
        "     (e.g. PARAKEET_ASR_URL), clear it or point it at this box's Tailscale name so the"
    )
    console.print(
        "     backend uses THIS node; otherwise minidisc discovery wires it automatically."
    )


def check_container_engine() -> bool:
    """Container-engine prereq — Chronicle runs everything in containers.

    Resolves the configured engine (docker default, or podman via config.yml /
    CONTAINER_ENGINE) and verifies both the engine binary and its compose front-end
    are installed and the runtime is reachable. Mirrors the Tailscale prereq style:
    on any problem we explain it and let the user continue at their own risk, since
    there is no container-less install path.

    Returns True to proceed, False to abort the wizard.
    """
    engine = services.container_engine()
    compose = (
        services.compose_base()
    )  # e.g. ['docker', 'compose'] or ['podman-compose']

    if shutil.which(engine) is None:
        console.print(
            f"[red]✗ {engine} isn't installed.[/red] Chronicle runs every service in "
            "containers —\n"
            "  there is no install path without a container engine. Install "
            f"[cyan]{engine}[/cyan]\n"
            "  (https://docs.docker.com/engine/install/ or https://podman.io/docs/installation),\n"
            "  then re-run this wizard."
        )
        return Confirm.ask(
            "Continue anyway? (nothing will start until a container engine is installed)",
            default=False,
        )

    # compose front-end: docker ships it as a plugin (`docker compose`), podman as a
    # separate `podman-compose` binary — check whichever the resolved command needs.
    compose_bin = compose[0]
    if shutil.which(compose_bin) is None:
        hint = (
            "It ships with Docker Desktop / the docker-compose-plugin package."
            if compose_bin == "docker"
            else "Install it with [cyan]pip install podman-compose[/cyan] (or your package manager)."
        )
        console.print(
            f"[red]✗ {' '.join(compose)} isn't available.[/red] Chronicle uses it to "
            "build and run\n"
            f"  the service stack. {hint}"
        )
        return Confirm.ask("Continue anyway?", default=False)

    # Runtime liveness: `<engine> info` exits non-zero if the daemon/runtime is down
    # (e.g. Docker Desktop not started, dockerd not running).
    try:
        up = (
            subprocess.run([engine, "info"], capture_output=True, timeout=20).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        up = False
    if not up:
        console.print(
            f"[red]✗ {engine} is installed but the runtime isn't reachable.[/red] "
            f"Start it\n"
            "  (e.g. launch Docker Desktop, or run [cyan]sudo systemctl start docker[/cyan]) "
            "and re-run."
        )
        return Confirm.ask(
            "Continue anyway? (services won't start until the engine is running)",
            default=False,
        )

    console.print(
        f"[green]✅[/green] Container engine: [cyan]{engine}[/cyan] "
        f"(compose: [cyan]{' '.join(compose)}[/cyan])"
    )
    return True


def main():
    """Main orchestration logic"""
    console.print("🎉 [bold green]Welcome to Chronicle![/bold green]\n")
    console.print("[dim]This wizard is safe to run as many times as you like.[/dim]")
    console.print(
        "[dim]It backs up your existing config and preserves previously entered values.[/dim]"
    )
    console.print(
        "[dim]When unsure, just press Enter — the defaults will work.[/dim]\n"
    )

    # Ensure config.yml exists (create from template if needed) — also resolves which
    # container engine (docker/podman) the rest of the wizard will use.
    config_mgr = ConfigManager()
    config_mgr.ensure_config_yml()

    # Setup git hooks first
    setup_git_hooks()

    # Show what's available
    show_service_status()

    # Read existing config.yml once — used as defaults for ALL wizard questions below
    config_yml = config_mgr.get_full_config()

    # Capture nodes and companion devices are host-native and intentionally do not
    # require containers, so both fork off before the container-engine prereq.
    setup_type = select_setup_type()
    if setup_type == "capture":
        setup_capture_node()
        return
    if setup_type == "companion":
        setup_companion()
        return

    # All hub and compute-node services below run in containers.
    if not check_container_engine():
        return

    # Fork: a service-only node joining an existing cluster takes a separate, much
    # shorter path (no backend / LLM / memory setup here) and returns.
    if setup_type == "join":
        join_cluster()
        return

    # Ask about the real-time STREAMING provider FIRST.
    streaming_provider = select_streaming_provider(config_yml)

    # Then the batch/high-quality provider, defaulting to the streaming provider
    # when it can also do batch (one provider for both is the simple common case,
    # but the user can still choose a different/higher-quality batch engine).
    transcription_provider = select_transcription_provider(
        config_yml, default_provider=streaming_provider
    )

    # If batch and streaming are the same provider there is no separate streaming
    # engine to wire — setup_transcription sets both defaults.stt and stt_stream.
    # Only pass streaming_provider to init.py when it actually differs from batch.
    if streaming_provider == transcription_provider:
        streaming_provider = None
        console.print(
            f"[green]✅[/green] Using {transcription_provider} for both batch and streaming"
        )
    elif streaming_provider:
        console.print(
            f"[blue][INFO][/blue] Batch: {transcription_provider}, "
            f"streaming: {streaming_provider} (batch-retranscribe enabled)"
        )

    # No streaming ASR (batch-only provider + streaming skipped) → offer windowed batch
    live_segmentation = "streaming_stt"
    if (
        transcription_provider not in ("none", None)
        and transcription_provider not in STREAMING_CAPABLE
        and streaming_provider is None
    ):
        live_segmentation = select_live_segmentation(transcription_provider)

    # LLM Provider selection (asked once here, passed to init.py — avoids double-ask)
    llm_provider = select_llm_provider(config_yml, transcription_provider)

    # Chronicle's agentic Markdown vault is the only memory provider — no choice.
    memory_provider = "chronicle"

    # Service Selection (pass provider choices so we skip asking about auto-added services)
    selected_services = select_services(
        transcription_provider, config_yml, memory_provider, llm_provider
    )

    # ── Service source: where each remote-capable compute service runs ──────────
    # Hub flow defaults to "on this hub", but offers own/external endpoint, pinning a
    # Tailnet-advertised node, or deferring to runtime discovery. A *remote* ASR/LLM
    # choice (own/Tailnet/later) also suppresses auto-adding the local service below.
    backend_env = "backend/.env"
    OFFLINE_DISCOVERABLE_ASR = {"parakeet", "qwen3-asr", "gemma4", "af-next"}
    _ASR_ENV_KEY = {
        "parakeet": "PARAKEET_ASR_URL",
        "qwen3-asr": "QWEN3_ASR_URL",
        "gemma4": "GEMMA4_ASR_URL",
        "af-next": "AF_NEXT_ASR_URL",
    }
    asr_url, asr_discover, asr_remote = None, False, False
    if transcription_provider in OFFLINE_DISCOVERABLE_ASR:
        asr_current = read_env_value(backend_env, _ASR_ENV_KEY[transcription_provider])
        src = select_service_source("ASR", "chronicle-asr", current=asr_current)
        asr_remote = src["mode"] != "local"
        if src["mode"] == "local":
            asr_url = "http://host.docker.internal:8767"
        elif src["mode"] in ("own", "tailnet"):
            asr_url = src["url"]
        elif src["mode"] == "later":
            asr_discover = True

    llm_base_url, llm_discover, llm_remote = None, False, False
    if llm_provider == "llamacpp":
        llm_current = read_env_value(backend_env, "LLM_BASE_URL")
        src = select_service_source(
            "Local LLM (llama.cpp)", "chronicle-llm", current=llm_current
        )
        llm_remote = src["mode"] != "local"
        if src["mode"] == "local":
            # Both compose projects join the external chronicle-network. Use
            # container DNS so llama.cpp's published host port can stay loopback-only.
            llm_base_url = LOCAL_LLAMACPP_BASE_URL
        elif src["mode"] in ("own", "tailnet"):
            # Discovered/picked URLs are bare host:port → ensure the OpenAI /v1 path.
            u = src["url"].rstrip("/")
            llm_base_url = u if u.endswith("/v1") else u + "/v1"
        elif src["mode"] == "later":
            llm_discover = True

    # Speaker Recognition + TTS: when NOT run locally, optionally point the backend at
    # a remote/own endpoint or let it auto-discover one on the Tailnet.
    speaker_url, speaker_discover = None, False
    if "speaker-recognition" not in selected_services:
        speaker_current = read_env_value(backend_env, "SPEAKER_SERVICE_URL")
        prior_remote = _infer_source_mode(speaker_current) in (
            "own",
            "tailnet",
            "later",
        )
        try:
            if Confirm.ask(
                "Use a remote / external Speaker Recognition service?",
                default=prior_remote,
            ):
                src = select_service_source(
                    "Speaker Recognition",
                    "chronicle-speaker",
                    allow_local=False,
                    current=speaker_current,
                )
                if src["mode"] in ("own", "tailnet"):
                    speaker_url = src["url"]
                else:
                    speaker_discover = True
        except EOFError:
            pass

    tts_url, tts_discover = None, False
    if "tts" in selected_services:
        # Running TTS locally on the host — pin the local endpoint (the compose no
        # longer defaults CHRONICLE_TTS_URL, so an unset value would mean 'discover').
        tts_url = "http://host.docker.internal:8770"
    else:
        tts_current = read_env_value(backend_env, "CHRONICLE_TTS_URL")
        prior_set = _infer_source_mode(tts_current) in ("own", "tailnet", "later")
        try:
            if Confirm.ask(
                "Configure a Text-to-Speech (TTS) endpoint?", default=prior_set
            ):
                src = select_service_source(
                    "Text-to-Speech",
                    "chronicle-tts",
                    allow_local=False,
                    current=tts_current,
                )
                if src["mode"] in ("own", "tailnet"):
                    tts_url = src["url"]
                else:
                    tts_discover = True
        except EOFError:
            pass

    # Auto-add asr-services if a LOCAL ASR was chosen (not a remote/discover source)
    local_asr_providers = (
        "parakeet",
        "vibevoice",
        "qwen3-asr",
        "gemma4",
        "af-next",
        "granite",
        "nemotron",
    )
    needs_asr = not asr_remote and (
        transcription_provider in local_asr_providers
        or (streaming_provider and streaming_provider in local_asr_providers)
    )
    if needs_asr and "asr-services" not in selected_services:
        reason = (
            transcription_provider
            if transcription_provider in local_asr_providers
            else streaming_provider
        )
        console.print(
            f"[blue][INFO][/blue] Auto-adding ASR services for {reason} transcription"
        )
        selected_services.append("asr-services")

    # Auto-add llm-services if llama.cpp runs LOCALLY (not a remote/discover source)
    if (
        llm_provider == "llamacpp"
        and not llm_remote
        and "llm-services" not in selected_services
    ):
        exists, _ = check_service_exists(
            "llm-services", SERVICES["extras"]["llm-services"]
        )
        if exists:
            console.print(
                "[blue][INFO][/blue] LLM provider is llama.cpp — auto-adding llm-services"
            )
            selected_services.append("llm-services")

    if not selected_services:
        console.print("\n[yellow]No services selected. Exiting.[/yellow]")
        return

    # LangFuse Configuration (before service setup so keys can be passed to backend)
    langfuse_mode, langfuse_external = setup_langfuse_choice()
    if langfuse_mode == "local" and "langfuse" not in selected_services:
        selected_services.append("langfuse")

    # HF Token Configuration (if services require it)
    hardware_profile = select_hardware_profile(
        selected_services, transcription_provider, streaming_provider
    )

    hf_token = setup_hf_token_if_needed(selected_services)

    # HTTPS Configuration (for services that need it)
    https_enabled = False
    server_ip = None

    # Check if we have services that benefit from HTTPS
    https_services = {
        "backend",
        "speaker-recognition",
    }  # backend always needs HTTPS.
    needs_https = bool(https_services.intersection(selected_services))

    if needs_https:
        console.print("\n🔒 [bold cyan]HTTPS Configuration[/bold cyan]")
        console.print(
            "HTTPS enables microphone access in browsers and secure connections"
        )

        # Default to existing HTTPS_ENABLED setting
        existing_https = read_env_value("backend/.env", "HTTPS_ENABLED")
        default_https = existing_https == "true"

        try:
            https_enabled = Confirm.ask(
                "Enable HTTPS for selected services?", default=default_https
            )
        except EOFError:
            console.print(f"Using default: {'Yes' if default_https else 'No'}")
            https_enabled = default_https

        if https_enabled:
            # Try to auto-detect Tailscale address
            ts_dns, ts_ip = detect_tailscale_info()

            if ts_dns:
                console.print(
                    f"\n[green][AUTO-DETECTED][/green] Tailscale DNS: {ts_dns}"
                )
                if ts_ip:
                    console.print(
                        f"[green][AUTO-DETECTED][/green] Tailscale IP:  {ts_ip}"
                    )
                console.print(
                    "[green][AUTO-DETECTED][/green] Minidisc service discovery enabled — "
                    "cross-machine services will find each other automatically"
                )
                default_address = ts_dns
            elif ts_ip:
                console.print(f"\n[green][AUTO-DETECTED][/green] Tailscale IP: {ts_ip}")
                console.print(
                    "[green][AUTO-DETECTED][/green] Minidisc service discovery enabled — "
                    "cross-machine services will find each other automatically"
                )
                default_address = ts_ip
            else:
                console.print("\n[blue][INFO][/blue] Tailscale not detected")
                console.print(
                    "[blue][INFO][/blue] To find your Tailscale address: tailscale status --json | jq -r '.Self.DNSName'"
                )
                default_address = None

            console.print("[blue][INFO][/blue] For local-only access, use 'localhost'")
            console.print("Examples: localhost, myhost.tail1234.ts.net, 100.64.1.2")

            # Check for existing SERVER_IP from backend .env
            backend_env_path = "backend/.env"
            existing_ip = read_env_value(backend_env_path, "SERVER_IP")

            # Use existing value, or auto-detected address, or localhost as default
            effective_default = default_address or "localhost"

            server_ip = prompt_with_existing_masked(
                prompt_text="Server IP/Domain for SSL certificates",
                existing_value=existing_ip,
                placeholders=["localhost", "your-server-ip-here"],
                is_password=False,
                default=effective_default,
            )

            console.print(f"[green]✅[/green] HTTPS configured for: {server_ip}")

            # Decide how the TLS cert is managed. The per-service init scripts derive
            # the same mode (from server_ip + tailscaled socket) and render their
            # Caddyfile/compose to match, so nothing needs to be threaded through here.
            cert_mode = decide_cert_mode(server_ip)
            if cert_mode == "static":
                # *.ts.net with no mountable tailscaled socket (e.g. Docker Desktop on
                # macOS): issue the cert on the host now. The services.py startup hook
                # renews it on restart — no cron needed.
                console.print(
                    "\n[blue][INFO][/blue] Generating host-issued TLS certificate..."
                )
                if generate_tailscale_certs("certs"):
                    console.print(
                        f"[green]✅[/green] Tailscale cert generated in certs/ for {server_ip}"
                    )
                else:
                    console.print(
                        "[yellow]⚠️  Certificate generation failed; it will be retried "
                        "automatically on the next service start.[/yellow]"
                    )
            else:
                # Caddy obtains and auto-renews the cert itself: *.ts.net via the mounted
                # tailscaled socket, a real domain via Let's Encrypt, IP/localhost via
                # Caddy's internal CA. No host cert file, no renewal cron.
                console.print(
                    f"\n[green]✅[/green] Caddy will obtain and auto-renew the TLS "
                    f"certificate for {server_ip} (no host cert file, no renewal cron)"
                )
                console.print(
                    "[blue][INFO][/blue] Trusted automatically for *.ts.net and real "
                    "domains; IP/localhost get a self-signed cert you accept in the browser."
                )

            # If this box is served over a Tailscale address, both reachability and
            # (Caddy-managed) cert renewal depend on tailscaled being up. Started-but-
            # not-enabled means it silently won't come back after a reboot — offer to
            # make it stick, same as the join-node path.
            served_over_tailscale = server_ip.endswith(".ts.net") or (
                bool(ts_ip) and server_ip == ts_ip
            )
            if served_over_tailscale and tailscaled_enabled_at_boot() is False:
                console.print(
                    "\n[yellow]⚠️  Tailscale is running but not enabled to start on boot.[/yellow]\n"
                    "   After a reboot this box would silently drop off the Tailnet —\n"
                    "   the dashboard/API would be unreachable and the TLS cert wouldn't renew."
                )
                if Confirm.ask(
                    "Enable tailscaled to start on boot now? (sudo systemctl enable --now tailscaled)",
                    default=True,
                ):
                    if enable_tailscaled_at_boot():
                        console.print(
                            "[green]✅[/green] tailscaled enabled — it'll survive reboots now."
                        )
                    else:
                        console.print(
                            "[red]✗ Couldn't enable it.[/red] Run it yourself: "
                            "[cyan]sudo systemctl enable --now tailscaled[/cyan]"
                        )

    # Pure Delegation - Run Each Service Setup
    console.print(f"\n📋 [bold]Setting up {len(selected_services)} services...[/bold]")

    # Record which services are enabled (config.yml is the lifecycle source of truth)
    persist_enabled_services(selected_services)

    success_count = 0
    failed_services = []

    # Pre-populate langfuse keys from external config (if user chose external mode)
    langfuse_public_key = langfuse_external.get("public_key")
    langfuse_secret_key = langfuse_external.get("secret_key")
    langfuse_host = langfuse_external.get(
        "host"
    )  # None for local (backend defaults to langfuse-web)

    # Browser-accessible URL for Langfuse dashboard deep-links (stored in config.yml).
    # Derived from server_ip/Tailscale so links don't hardcode localhost.
    langfuse_public_url = derive_langfuse_public_url(
        langfuse_mode, langfuse_external, server_ip, https_enabled
    )

    # Determine setup order: langfuse first (to get API keys), then backend (with langfuse keys), then others
    setup_order = []
    if "langfuse" in selected_services:
        setup_order.append("langfuse")
    if "backend" in selected_services:
        setup_order.append("backend")
    for service in selected_services:
        if service not in setup_order:
            setup_order.append(service)

    # Read admin credentials from existing backend .env (for langfuse init reuse)
    backend_env_path = "backend/.env"
    wizard_admin_email = read_env_value(backend_env_path, "ADMIN_EMAIL")
    wizard_admin_password = read_env_value(backend_env_path, "ADMIN_PASSWORD")

    for service in setup_order:
        if run_service_setup(
            service,
            selected_services,
            https_enabled,
            server_ip,
            hf_token,
            transcription_provider,
            admin_email=wizard_admin_email,
            admin_password=wizard_admin_password,
            langfuse_public_key=langfuse_public_key,
            langfuse_secret_key=langfuse_secret_key,
            langfuse_host=langfuse_host,
            langfuse_public_url=langfuse_public_url,
            streaming_provider=streaming_provider,
            llm_provider=llm_provider,
            memory_provider=memory_provider,
            hardware_profile=hardware_profile,
            live_segmentation=live_segmentation,
            asr_url=asr_url,
            asr_discover=asr_discover,
            llm_base_url=llm_base_url,
            llm_discover=llm_discover,
            speaker_url=speaker_url,
            speaker_discover=speaker_discover,
            tts_url=tts_url,
            tts_discover=tts_discover,
        ):
            success_count += 1

            # After local langfuse setup, read generated API keys for backend
            if service == "langfuse":
                langfuse_env_path = "extras/langfuse/.env"
                langfuse_public_key = read_env_value(
                    langfuse_env_path, "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"
                )
                langfuse_secret_key = read_env_value(
                    langfuse_env_path, "LANGFUSE_INIT_PROJECT_SECRET_KEY"
                )
                if langfuse_public_key and langfuse_secret_key:
                    console.print(
                        "[blue][INFO][/blue] LangFuse API keys will be passed to backend configuration"
                    )
        else:
            failed_services.append(service)

    # Plugin Configuration (AFTER backend .env is created)
    # This ensures plugins can add their secrets to the existing .env file
    # without the backend init overwriting them
    setup_plugins()

    # Optional: install the native host agents (service manager + discovery) as
    # systemd user services so they auto-start on boot like the containers do.
    if "backend" in selected_services:
        maybe_install_agent_services()
        # Optional (off by default): a Claude remote-control session so you can
        # start Claude Code sessions on this host from the Claude mobile app.
        maybe_enable_remote_control()

    # Final Summary
    console.print(f"\n🎊 [bold green]Setup Complete![/bold green]")
    console.print(
        f"✅ {success_count}/{len(selected_services)} services configured successfully"
    )

    if failed_services:
        console.print(f"❌ Failed services: {', '.join(failed_services)}")

    # Next Steps
    console.print("\n📖 [bold]Next Steps:[/bold]")

    # Configuration info
    console.print("")
    console.print("📝 [bold cyan]Configuration Files Updated:[/bold cyan]")
    console.print("   • [green].env files[/green] - API keys and service URLs")
    console.print(
        "   • [green]config.yml[/green] - Model definitions and memory provider settings"
    )
    console.print("")

    # Development Environment Setup
    console.print("1. Setup development environment (git hooks, testing):")
    console.print("   [cyan]make setup-dev[/cyan]")
    console.print(
        "   [dim]This installs pre-commit hooks to run tests before pushing[/dim]"
    )
    console.print("")

    # Service Management Commands
    console.print("2. Start all configured services:")
    console.print("   [cyan]./services start --all[/cyan]")
    console.print("   [dim]Or: ./services start --all --build[/dim]")
    console.print("")
    console.print("3. Or start individual services:")

    configured_services = []
    if "backend" in selected_services and "backend" not in failed_services:
        configured_services.append("backend")
    if (
        "speaker-recognition" in selected_services
        and "speaker-recognition" not in failed_services
    ):
        configured_services.append("speaker-recognition")
    if "asr-services" in selected_services and "asr-services" not in failed_services:
        configured_services.append("asr-services")
    if "langfuse" in selected_services and "langfuse" not in failed_services:
        configured_services.append("langfuse")

    # LangFuse prompt management info
    if langfuse_mode == "local" and "langfuse" not in failed_services:
        console.print("")
        console.print(
            "[bold cyan]Prompt Management:[/bold cyan] Once services are running, edit AI prompts at:"
        )
        prompts_url = f"{langfuse_public_url.rstrip('/')}/project/chronicle/prompts"
        console.print(f"   [link={prompts_url}]{prompts_url}[/link]")
    elif langfuse_mode == "external" and langfuse_host:
        console.print("")
        console.print(
            f"[bold cyan]Prompt Management:[/bold cyan] Edit AI prompts at your LangFuse instance:"
        )
        console.print(f"   {langfuse_host}")

    if configured_services:
        service_list = " ".join(configured_services)
        console.print(f"   [cyan]./services start {service_list}[/cyan]")

    console.print("")
    console.print("3. Check service status:")
    console.print("   [cyan]./services status[/cyan]")
    console.print("   [dim]Or: ./services status[/dim]")

    console.print("")
    console.print("4. Stop services when done:")
    console.print("   [cyan]./services stop --all[/cyan]")
    console.print("   [dim]Or: ./services stop --all[/dim]")

    # Show minidisc discovery info if Tailscale is available
    ts_dns_final, ts_ip_final = detect_tailscale_info()
    if ts_dns_final or ts_ip_final:
        console.print("")
        console.print(
            "🔍 [bold cyan]Distributed Setup:[/bold cyan] Minidisc service discovery is active"
        )
        console.print(
            "   Services on other Tailnet machines (HAVPE relay, ASR, etc.) will"
        )
        console.print(
            "   auto-discover this backend — no manual URL configuration needed"
        )

    console.print(f"\n🚀 [bold]Enjoy Chronicle![/bold]")

    # Show individual service usage
    console.print(f"\n💡 [dim]Tip: You can also setup services individually:[/dim]")
    console.print(
        f"[dim]   uv run --with-requirements setup-requirements.txt python backend/init.py[/dim]"
    )
    console.print(
        f"[dim]   uv run --with-requirements setup-requirements.txt python extras/speaker-recognition/init.py[/dim]"
    )
    console.print(
        f"[dim]   uv run --with-requirements setup-requirements.txt python extras/asr-services/init.py[/dim]"
    )


if __name__ == "__main__":
    main()
