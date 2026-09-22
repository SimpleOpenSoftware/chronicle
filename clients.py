"""
Client-node components: registry + native unit management.

A Chronicle *client node* is a machine that only captures and streams data —
a desktop/laptop running the tray, the ScreenPipe collector, vault sync — with
no compose services and no GPU. These run as native user units (systemd user
services on Linux, launchd agents on macOS), not containers, so services.py's
compose machinery never touches them. This module is the single place that
knows how to install/inspect/restart them, shared by:

  - ``./services client ...``            (CLI install/status/uninstall)
  - ``updates.py``                        (restart installed clients after a code update)
  - ``edge/service_manager.py``           (expose + control them from the hub WebUI)
  - the components' own CLIs              (e.g. ``chronicle-tray install``)

Stdlib-only on purpose: updates.py and the node agent must always be able to
import it, and client installs happen before any project venv exists.
"""

import http.client
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

IS_MACOS = sys.platform == "darwin"

_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_MAC_LOG_DIR = Path.home() / "Library" / "Logs" / "Chronicle"
_SPEC_DIR = (
    Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "chronicle"
    / "clients"
)
_SCREENPIPE_PORT = 3030

# A component is either a *project* component (a uv project in this repo, run as
# `uv run --project <path> <command>`) or a *spec* component, whose argv and
# environment are resolved at setup time and stored in _SPEC_DIR. The recorder is
# the latter: it is a third-party binary whose flags depend on the machine's audio
# devices and carries a per-node API key, none of which can be a static literal
# here. Both kinds install identically on both platforms.
#
# name -> component config. Keys:
#   path         project dir relative to the repo root (a uv project)
#   description  human description (unit Description= / WebUI)
#   unit         systemd user unit name (Linux)
#   label        launchd label (macOS)
#   command      argv appended to ``uv run --project <path>`` in the unit
#   graphical    needs a desktop session (tray): graphical-session target /
#                launchd ProcessType Interactive
#   after_units  extra systemd After= ordering deps (Linux only)
#   spec         set for spec components: argv/env come from _SPEC_DIR, and
#                `path`/`command` are absent
CLIENT_COMPONENTS = {
    "screenpipe": {
        "description": "ScreenPipe local recorder for Chronicle",
        "unit": "screenpipe.service",
        "label": "com.chronicle.screenpipe-recorder",
        "spec": True,
        # Screen capture needs a logged-in graphical session on both platforms.
        "graphical": True,
    },
    "tray": {
        "path": "extras/chronicle-tray",
        "description": "Chronicle desktop tray (vault sync, ScreenPipe, pendant)",
        "unit": "chronicle-tray.service",
        "label": "com.chronicle.tray",
        "command": ["chronicle-tray", "run"],
        "graphical": True,
    },
    "screenpipe-collector": {
        "path": "extras/screenpipe-collector",
        "description": "ScreenPipe → Chronicle forwarder (audio + app activity)",
        "unit": "chronicle-screenpipe.service",
        "label": "com.chronicle.screenpipe",
        "command": ["chronicle-screenpipe", "run"],
        "after_units": ["screenpipe.service"],
    },
}

# Superseded single-purpose units the unified tray replaces. Still restarted
# after updates while installed (they run from this checkout too); the vault
# ones are auto-removed when the tray is installed because two trays would
# fight over the same private Syncthing instance.
LEGACY_LINUX_UNITS = ["chronicle-desktop.service"]
LEGACY_MACOS_LABELS = ["com.chronicle.vault-sync", "com.chronicle.wearable-client"]
# Legacy units that hard-conflict with the tray (shared Syncthing home/ports).
_TRAY_CONFLICTS_LINUX = ["chronicle-desktop.service"]
_TRAY_CONFLICTS_MACOS = ["com.chronicle.vault-sync"]
# The pendant section replaces the wearable menu bar app (one BLE connection
# per device) — only a conflict when the tray is installed with that extra.
_PENDANT_CONFLICTS_MACOS = ["com.chronicle.wearable-client"]


def _find_uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv
    for candidate in (
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "uv not found — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    )


def _unit_path_env(uv_path: str) -> str:
    """PATH for units: user units get a minimal PATH, so pin one that resolves
    uv and the binaries components shell out to (syncthing, screenpipe)."""
    dirs = [
        str(Path(uv_path).parent),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def component_spec_path(name: str) -> Path:
    return _SPEC_DIR / f"{name}.json"


def write_component_spec(name: str, argv, env=None) -> Path:
    """Record the resolved argv/env for a spec component.

    Written 0600 because the environment carries the recorder's API key. Call
    before install_component(); the unit/plist is generated from this.
    """
    if not CLIENT_COMPONENTS[name].get("spec"):
        raise RuntimeError(f"{name} is not a spec component")
    _SPEC_DIR.mkdir(parents=True, exist_ok=True)
    path = component_spec_path(name)
    path.write_text(
        json.dumps({"argv": list(argv), "env": dict(env or {})}, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def update_component_argv(name: str, argv) -> None:
    """Replace a spec component's argv, preserving its environment, and
    regenerate the unit/plist so the change takes effect on next start."""
    spec = read_component_spec(name)
    write_component_spec(name, argv, spec.get("env", {}))
    install_component(name)


def read_component_spec(name: str) -> dict:
    path = component_spec_path(name)
    if not path.exists():
        raise RuntimeError(
            f"{name} has no saved spec at {path} — run the capture-node setup first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _component_project(name: str) -> Path:
    """Working directory for the unit. Spec components have no repo project."""
    cfg = CLIENT_COMPONENTS[name]
    return Path.home() if cfg.get("spec") else REPO_ROOT / cfg["path"]


def _exec_argv(name: str, extras=()) -> list[str]:
    cfg = CLIENT_COMPONENTS[name]
    if cfg.get("spec"):
        return list(read_component_spec(name)["argv"])
    uv = _find_uv()
    argv = [uv, "run", "--project", str(REPO_ROOT / cfg["path"])]
    for extra in extras:
        argv += ["--extra", extra]
    return argv + list(cfg["command"])


# ── systemd (Linux) ──────────────────────────────────────────────────────────


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def _systemd_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        result = _systemctl("is-system-running")
    except OSError:
        return False
    return (result.stdout or "").strip() in (
        "running",
        "degraded",
        "starting",
        "initializing",
        "maintenance",
        "stopping",
    )


def _install_linux(name: str, extras=()) -> None:
    cfg = CLIENT_COMPONENTS[name]
    if not _systemd_available():
        raise RuntimeError(
            "no systemd user instance — on WSL set systemd=true in /etc/wsl.conf"
        )

    uv = _find_uv()
    project = _component_project(name)
    afters = ["network-online.target"] + list(cfg.get("after_units", []))
    wanted = "default.target"
    if cfg.get("graphical"):
        afters.insert(0, "graphical-session.target")
        wanted = "graphical-session.target"
    # shlex.join so a flag value containing spaces (e.g. an --audio-device name
    # like "MacBook Pro Microphone (input)") survives systemd's own splitting.
    exec_start = shlex.join(_exec_argv(name, extras))
    env = {"PATH": _unit_path_env(uv)}
    if cfg.get("spec"):
        env.update(read_component_spec(name).get("env", {}))
    unit = _SYSTEMD_USER_DIR / cfg["unit"]
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\n"
        f"Description={cfg['description']}\n"
        + "".join(f"After={a}\n" for a in afters)
        + "\n[Service]\nType=simple\n"
        f"WorkingDirectory={project}\n"
        + "".join(f"Environment={k}={v}\n" for k, v in env.items())
        + f"ExecStart={exec_start}\n"
        "Restart=on-failure\nRestartSec=5\n"
        f"\n[Install]\nWantedBy={wanted}\n"
    )
    # The unit embeds the component's environment, which may hold an API key.
    unit.chmod(0o600)
    subprocess.run(["loginctl", "enable-linger"], capture_output=True)
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", cfg["unit"])
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl enable --now {cfg['unit']} failed: {result.stderr.strip()}"
        )


def _uninstall_linux_unit(unit: str) -> None:
    _systemctl("disable", "--now", unit)
    (_SYSTEMD_USER_DIR / unit).unlink(missing_ok=True)
    _systemctl("daemon-reload")


# ── launchd (macOS) ──────────────────────────────────────────────────────────


def _plist_path(label: str) -> Path:
    return _LAUNCH_AGENTS_DIR / f"{label}.plist"


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _dotenv_values(env_file: Path) -> dict:
    """Minimal KEY=VALUE parser (stdlib-only; unit env vars, not full dotenv)."""
    values = {}
    if not env_file.exists():
        return values
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _install_macos(name: str, extras=()) -> None:
    cfg = CLIENT_COMPONENTS[name]
    label = cfg["label"]
    project = _component_project(name)
    log_file = _MAC_LOG_DIR / f"{name}.log"
    _MAC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # launchd starts agents with no login env. The tray reads the repository-root
    # .env itself so edits take effect on restart instead of being copied into
    # (and potentially shadowed by) its plist. Other components retain their
    # project-local environment; spec components carry their own.
    if cfg.get("spec"):
        env = dict(read_component_spec(name).get("env", {}))
    elif name == "tray":
        env = {}
    else:
        env = _dotenv_values(project / ".env")
    env["PATH"] = _unit_path_env(_find_uv()) + ":" + os.environ.get("PATH", "")

    # The pendant (BLE) extra decodes Opus audio via opuslib, which loads the
    # native libopus through ctypes.util.find_library("opus"). That search does
    # not include Homebrew's lib dir, and launchd hands the agent a bare
    # environment, so point dyld's fallback search at the Homebrew prefixes
    # (Apple Silicon + Intel). Harmless when the dirs are absent.
    if "pendant" in extras:
        env["DYLD_FALLBACK_LIBRARY_PATH"] = "/opt/homebrew/lib:/usr/local/lib"

    plist = {
        "Label": label,
        "ProgramArguments": _exec_argv(name, extras),
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "EnvironmentVariables": env,
    }
    if cfg.get("graphical"):
        plist["ProcessType"] = "Interactive"

    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    # EnvironmentVariables may hold an API key.
    path.chmod(0o600)
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap {label} failed: {result.stderr.strip()}"
        )


def _uninstall_macos_label(label: str) -> None:
    path = _plist_path(label)
    if path.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    path.unlink(missing_ok=True)


# ── public API ───────────────────────────────────────────────────────────────


def component_installed(name: str) -> bool:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        return _plist_path(cfg["label"]).exists()
    return (_SYSTEMD_USER_DIR / cfg["unit"]).exists()


def component_active(name: str) -> bool:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        result = _launchctl("print", f"gui/{os.getuid()}/{cfg['label']}")
        if result.returncode != 0:
            return False
        return any(
            line.strip() == "state = running" or line.strip().startswith("pid = ")
            for line in result.stdout.splitlines()
        )
    return _systemctl("is-active", cfg["unit"]).returncode == 0


def _screenpipe_desktop_processes() -> list[dict]:
    """Return ScreenPipe desktop-app processes, excluding CLI/MCP companions."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []
    processes = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        lowered = command.lower()
        executable = lowered.split(maxsplit=1)[0]
        if (
            "/contents/macos/screenpipe-app" in lowered
            or executable.endswith("/screenpipe-app")
            or executable == "screenpipe-app"
        ):
            processes.append({"pid": int(fields[0]), "command": command})
    return processes


def _screenpipe_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", _SCREENPIPE_PORT), timeout=0.2):
            return True
    except OSError:
        return False


def _screenpipe_health() -> dict | None:
    connection = http.client.HTTPConnection("127.0.0.1", _SCREENPIPE_PORT, timeout=0.4)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read())
        return payload if isinstance(payload, dict) else None
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        return None
    finally:
        connection.close()


def _screenpipe_vision_capture(health: dict) -> dict | None:
    """Normalize recorder vision health across ScreenPipe versions.

    New recorders expose the portal lifecycle directly. Older builds only expose
    aggregate pipeline counters, and return that useful body with HTTP 503 once
    capture has stalled. Keep the direct contract authoritative, but turn a
    sustained stream of capture errors into the same tray-facing failure state.
    """
    capture = health.get("vision_capture")
    if isinstance(capture, dict):
        return capture

    pipeline = health.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    attempts = pipeline.get("capture_attempts")
    dropped_errors = pipeline.get("frames_dropped_error")
    drop_rate = pipeline.get("frame_drop_rate")
    sustained_errors = (
        isinstance(attempts, (int, float))
        and attempts >= 10
        and isinstance(dropped_errors, (int, float))
        and dropped_errors >= 5
        and isinstance(drop_rate, (int, float))
        and drop_rate >= 0.8
    )
    if health.get("vision_db_write_stalled") is not True and not sustained_errors:
        return None

    return {
        "requested": True,
        "state": "failed",
        "detail": (
            "Screen capture is not producing frames. Restart the recorder and "
            "share every requested monitor in the screen-sharing prompt."
        ),
    }


def screenpipe_runtime_status(chronicle_active: bool | None = None) -> dict:
    """Describe which local process owns ScreenPipe recording and port 3030."""
    desktop_processes = _screenpipe_desktop_processes()
    desktop_active = bool(desktop_processes)
    if chronicle_active is None:
        chronicle_active = component_active("screenpipe")
    port_open = _screenpipe_port_open()

    if chronicle_active:
        health = _screenpipe_health() if port_open else None
        return {
            "runtime_owner": "chronicle",
            "recording_active": port_open,
            "conflict": False,
            "detail": (
                (
                    "Chronicle recorder active — desktop viewer open"
                    if desktop_active
                    else "Chronicle recorder active"
                )
                if port_open
                else (
                    "Chronicle recorder starting; API not ready — desktop viewer waiting"
                    if desktop_active
                    else "Chronicle recorder starting; API not ready"
                )
            ),
            "desktop_pids": [process["pid"] for process in desktop_processes],
            "vision_capture": _screenpipe_vision_capture(health) if health else None,
        }

    if port_open:
        return {
            "runtime_owner": "unknown",
            "recording_active": True,
            "conflict": True,
            "detail": (
                "port 3030 is owned by an unrecognized process — desktop viewer open"
                if desktop_active
                else "port 3030 is owned by an unrecognized process"
            ),
            "desktop_pids": [process["pid"] for process in desktop_processes],
        }

    return {
        "runtime_owner": "none",
        "recording_active": False,
        "conflict": False,
        "detail": (
            "recorder inactive — desktop viewer waiting"
            if desktop_active
            else "recorder inactive"
        ),
        "desktop_pids": [process["pid"] for process in desktop_processes],
    }


def component_status(name: str) -> dict:
    installed = component_installed(name)
    status = {
        "name": name,
        "description": CLIENT_COMPONENTS[name]["description"],
        "installed": installed,
        "active": component_active(name) if installed else False,
    }
    if name == "screenpipe":
        status.update(screenpipe_runtime_status(status["active"]))
    return status


def installed_components() -> list[str]:
    return [name for name in CLIENT_COMPONENTS if component_installed(name)]


def install_component(name: str, extras=()) -> None:
    """Install (or reinstall) a component as a login user unit and start it.

    Installing the tray removes the superseded single-purpose tray units it
    replaces — two trays would race for the same private Syncthing instance.
    Raises RuntimeError with a human-readable reason on failure.
    """
    if name not in CLIENT_COMPONENTS:
        raise RuntimeError(f"Unknown client component: {name}")
    if name == "screenpipe":
        for message in disable_screenpipe_app_autostart():
            print(message)
    if name == "tray":
        conflicts = _TRAY_CONFLICTS_MACOS if IS_MACOS else _TRAY_CONFLICTS_LINUX
        if "pendant" in extras and IS_MACOS:
            conflicts = conflicts + _PENDANT_CONFLICTS_MACOS
        for legacy in conflicts:
            if _legacy_installed(legacy):
                _remove_legacy(legacy)
                print(f"Removed superseded unit {legacy} (replaced by the tray)")
    if IS_MACOS:
        _install_macos(name, extras)
    else:
        _install_linux(name, extras)


def uninstall_component(name: str) -> None:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        _uninstall_macos_label(cfg["label"])
    else:
        _uninstall_linux_unit(cfg["unit"])
    if cfg.get("spec"):
        component_spec_path(name).unlink(missing_ok=True)


def component_action(name: str, action: str) -> bool:
    """start | stop | restart an installed component. Returns success."""
    cfg = CLIENT_COMPONENTS[name]
    if name == "screenpipe" and action in {"start", "restart"}:
        runtime = screenpipe_runtime_status()
        if runtime["runtime_owner"] == "unknown":
            raise RuntimeError(
                f"Cannot {action} Chronicle recorder: {runtime['detail']}. "
                "Release port 3030 first."
            )
    if IS_MACOS:
        domain = f"gui/{os.getuid()}"
        plist = str(_plist_path(cfg["label"]))
        if action == "stop":
            return _launchctl("bootout", domain, plist).returncode == 0
        if action == "start":
            return _launchctl("bootstrap", domain, plist).returncode == 0
        if action == "restart":
            # kickstart -k can't revive a booted-out agent; a bootout/bootstrap
            # cycle restarts from any state and picks up plist edits.
            _launchctl("bootout", domain, plist)
            return _launchctl("bootstrap", domain, plist).returncode == 0
        raise RuntimeError(f"Unknown action: {action}")
    if action not in ("start", "stop", "restart"):
        raise RuntimeError(f"Unknown action: {action}")
    return _systemctl(action, cfg["unit"]).returncode == 0


def _macos_screenpipe_app_agents() -> list[tuple[str, Path]]:
    agents = []
    for path in _LAUNCH_AGENTS_DIR.glob("*screenpipe*.plist"):
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        argv = [str(value).lower() for value in plist.get("ProgramArguments", [])]
        label = plist.get("Label")
        if label and any(value.endswith("/screenpipe-app") for value in argv):
            agents.append((str(label), path))
    return agents


def _linux_screenpipe_app_units() -> list[str]:
    result = _systemctl("list-unit-files", "--no-legend", "--plain")
    if result.returncode != 0:
        return []
    units = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0]
        lowered = unit.lower()
        if lowered.startswith("app-screenpipe") and lowered.endswith(
            "@autostart.service"
        ):
            units.append(unit)
    return units


def disable_screenpipe_app_autostart() -> list[str]:
    """Select Chronicle as login recorder without uninstalling the desktop app."""
    messages = []
    if IS_MACOS:
        domain = f"gui/{os.getuid()}"
        for label, path in _macos_screenpipe_app_agents():
            _launchctl("bootout", domain, str(path))
            result = _launchctl("disable", f"{domain}/{label}")
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not disable ScreenPipe app autostart ({label}): "
                    f"{result.stderr.strip()}"
                )
            messages.append(
                f"Disabled ScreenPipe desktop app autostart ({label}); "
                "the app remains manually launchable"
            )
        return messages

    for unit in _linux_screenpipe_app_units():
        result = _systemctl("mask", "--now", unit)
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not disable ScreenPipe app autostart ({unit}): "
                f"{result.stderr.strip()}"
            )
        messages.append(
            f"Disabled ScreenPipe desktop app autostart ({unit}); "
            "the app remains manually launchable"
        )
    return messages


# ── legacy units (pre-unified-tray installs) ─────────────────────────────────


def _legacy_installed(unit_or_label: str) -> bool:
    if IS_MACOS:
        return _plist_path(unit_or_label).exists()
    return (_SYSTEMD_USER_DIR / unit_or_label).exists()


def _remove_legacy(unit_or_label: str) -> None:
    if IS_MACOS:
        _uninstall_macos_label(unit_or_label)
    else:
        _uninstall_linux_unit(unit_or_label)


def _restart_legacy(unit_or_label: str) -> bool:
    if IS_MACOS:
        return (
            _launchctl(
                "kickstart", "-k", f"gui/{os.getuid()}/{unit_or_label}"
            ).returncode
            == 0
        )
    return _systemctl("restart", unit_or_label).returncode == 0


def installed_legacy_units() -> list[str]:
    legacy = LEGACY_MACOS_LABELS if IS_MACOS else LEGACY_LINUX_UNITS
    return [u for u in legacy if _legacy_installed(u)]


# ── update integration ───────────────────────────────────────────────────────


# Flags the recommended recorder invocation no longer carries. The recorder's
# spec argv is per-node state that code updates never regenerate (and the
# tray's capture dialog edits only capture flags), so a retired flag would
# survive every update without this sweep. Currently: the meeting detector
# must stay ON — the collector mirrors the recorder's persisted meetings into
# Chronicle's session bounds (see docs/screenpipe.md).
_RETIRED_RECORDER_FLAGS = ("--disable-meeting-detector",)


def migrate_recorder_spec() -> bool:
    """Drop retired flags from the recorder spec; True if anything changed.

    Regenerates the unit/plist and restarts the recorder so the change takes
    effect — the one case worth interrupting a live capture session for.
    """
    name = "screenpipe"
    if not component_installed(name) or not component_spec_path(name).exists():
        return False
    spec = read_component_spec(name)
    argv = [arg for arg in spec["argv"] if arg not in _RETIRED_RECORDER_FLAGS]
    if argv == spec["argv"]:
        return False
    update_component_argv(name, argv)
    component_action(name, "restart")
    return True


def restart_installed(progress=None) -> list[tuple[str, bool]]:
    """Restart every installed client unit so it picks up updated code.

    Used by updates.perform_update() after the checkout moves: the units all
    ``uv run`` straight from this checkout, so a restart is all an update
    needs. Best-effort by design — a tray that fails to relaunch must not fail
    (or roll back) a node update. Returns [(unit, ok)].

    Spec components are skipped: they run a third-party binary this repo does
    not ship, so an update gives them nothing and a restart would drop a live
    capture session for no reason. The one exception is a retired-flag
    migration, which does regenerate and restart the recorder.
    """
    progress = progress or (lambda msg: None)
    results: list[tuple[str, bool]] = []
    try:
        if migrate_recorder_spec():
            progress("Recorder flags migrated (meeting detector re-enabled)")
            results.append(("screenpipe", True))
    except Exception:
        results.append(("screenpipe", False))
    for name in installed_components():
        if CLIENT_COMPONENTS[name].get("spec"):
            continue
        progress(f"Restarting {name}…")
        results.append((name, component_action(name, "restart")))
    for legacy in installed_legacy_units():
        progress(f"Restarting {legacy}…")
        results.append((legacy, _restart_legacy(legacy)))
    return results


# ── companion binaries ───────────────────────────────────────────────────────


def binary_checks() -> list[dict]:
    """Presence of the external binaries client components rely on, with an
    install suggestion for anything missing."""
    if IS_MACOS:
        syncthing_hint = "brew install syncthing"
    elif shutil.which("pacman"):
        syncthing_hint = "sudo pacman -S syncthing"
    else:
        syncthing_hint = "sudo apt install syncthing"
    checks = [
        {
            "name": "screenpipe",
            "needed_by": "screenpipe, screenpipe-collector",
            "found": shutil.which("screenpipe") is not None,
            "suggest": "install ScreenPipe: curl -fsSL get.screenpi.pe/cli | sh "
            "(see https://screenpi.pe), then `screenpipe service install`",
        },
        {
            "name": "syncthing",
            "needed_by": "tray (vault sync)",
            "found": shutil.which("syncthing") is not None,
            "suggest": syncthing_hint,
        },
    ]
    return checks
