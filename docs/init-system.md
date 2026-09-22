# Chronicle Initialization System

## Quick Links

- **👉 [Start Here: Quick Start Guide](../quickstart.md)** - Main setup path for new users
- **📚 [Full Documentation](../AGENTS.md)** - Comprehensive reference
- **🏗️ [Architecture Details](overview.md)** - Technical deep dive
- **🐧 [Running with Podman](podman.md)** - Use Podman instead of Docker (engine selection, rootless/GPU)

---

## Overview

Chronicle uses a unified initialization system with clean separation of concerns:

- **Configuration** (`wizard.py`) - Set up service configurations, API keys, and .env files
- **Service Management** (`services.py`) - Start, stop, and manage running services

The root orchestrator handles service selection and delegates configuration to individual service scripts. In general, setup scripts only configure and do not start services automatically. Exception: `extras/asr-services` is a startup script. This prevents unnecessary resource usage and gives you control over when services actually run.

> **New to Chronicle?** Most users should start with the [Quick Start Guide](../quickstart.md) instead of this detailed reference.

## Architecture

### Root Orchestrator
- **Location**: `/wizard.py`
- **Purpose**: Service selection and delegation only
- **Does NOT**: Handle service-specific configuration or duplicate setup logic

### Service Scripts
- **Backend**: `backend/init.py` - Complete Python-based interactive setup
- **Speaker Recognition**: `extras/speaker-recognition/init.py` - Python-based interactive setup
- **ASR Services**: `extras/asr-services/setup.sh` - Service startup script

## Usage

### Orchestrated Setup (Recommended)
Set up multiple services together with automatic URL coordination:

```bash
# From project root (using convenience script)
./wizard.sh

# Or use direct command:
uv run --with-requirements setup-requirements.txt python wizard.py
```

The orchestrator will:
1. Show service status and availability
2. Let you select which services to configure
3. Automatically pass service URLs between services
4. Display next steps for starting services

### Individual Service Setup
Each service can be configured independently:

All setup commands run **from the repository root** — `setup-requirements.txt`
lists the shared `chronicle-setup` package as a relative path, and uv resolves
that against the working directory. Each `init.py` finds its own files from
`__file__`, so this does not change which `.env` it writes.

```bash
# Chronicle Backend only
uv run --with-requirements setup-requirements.txt python backend/init.py

# Speaker Recognition only
uv run --with-requirements setup-requirements.txt python extras/speaker-recognition/init.py

# ASR Services only
uv run --with-requirements setup-requirements.txt python extras/asr-services/init.py
```

## Service Details

### Chronicle Backend
- **Interactive setup** for authentication, LLM, and transcription (memory is the agentic Markdown vault — no provider choice)
- **Accepts arguments**: `--speaker-service-url`, `--parakeet-asr-url`
- **Generates**: Complete `.env` file with all required configuration
- **Default ports**: Backend (8000), WebUI (5173)

### Speaker Recognition
- **Prompts for**: Hugging Face token, compute mode (cpu/gpu)
- **Service port**: 8085
- **WebUI port**: 5173
- **Requires**: HF_TOKEN for pyannote models

### ASR Services
- **Starts**: Parakeet ASR service via Docker Compose
- **Service port**: 8767
- **Purpose**: Offline speech-to-text processing
- **No configuration required**

## Automatic URL Coordination

When using the orchestrated setup, service URLs are automatically configured:

| Service Selected     | Backend Gets Configured With                                     |
|----------------------|-------------------------------------------------------------------|
| Speaker Recognition  | `SPEAKER_SERVICE_URL=http://host.docker.internal:8085`           |
| ASR Services         | `PARAKEET_ASR_URL=http://host.docker.internal:8767`              |

This eliminates the need to manually configure service URLs when running services on the same machine.
Note (Linux): If `host.docker.internal` is unavailable, add `extra_hosts: - "host.docker.internal:host-gateway"` to the relevant services in `docker-compose.yml`.

## Key Benefits

✅ **No Unnecessary Building** - Services are only started when you explicitly request them
✅ **Resource Efficient** - Parakeet ASR won't start if you're using cloud transcription
✅ **Clean Separation** - Configuration vs service management are separate concerns
✅ **Unified Control** - Single command to start/stop all services
✅ **Selective Starting** - Choose which services to run based on your current needs

## Ports & Access

### HTTP Mode (Default - No SSL Required)

| Service | API Port | Web UI Port | Access URL |
|---------|----------|-------------|------------|
| **Chronicle Backend** | 8000 | 5173 | http://localhost:8000 (API), http://localhost:5173 (Dashboard) |
| **Speaker Recognition** | 8085 | 5175* | http://localhost:8085 (API), http://localhost:5175 (WebUI) |
| **Langfuse** | 3002 | 3002 | http://localhost:3002 (WebUI/API) |
| **Parakeet ASR** | 8767 | - | http://localhost:8767 (API) |

*Speaker Recognition WebUI port is configurable via REACT_UI_PORT

Note: Browsers require HTTPS for microphone access over network.

### HTTPS Mode (For Microphone Access)

| Service | HTTP Port | HTTPS Port | Access URL |
|---------|-----------|------------|------------|
| **Chronicle Backend** | 80->443 | 443 | https://localhost/ (Main), https://localhost/api/ (API) |
| **Speaker Recognition** | 8081->8444 | 8444 | https://localhost:8444/ (Main), https://localhost:8444/api/ (API) |
| **Langfuse** | 3002 (direct fallback) | 3443 | https://localhost:3443/ (WebUI/API) |

Caddy services start automatically with the standard compose command when HTTPS is configured.

See [ssl-certificates.md](ssl-certificates.md) for HTTPS/SSL setup details.

### Container-to-Container Communication
Services use `host.docker.internal` for inter-container communication:
- `http://127.0.0.1:8085` - Speaker Recognition
- `http://host.docker.internal:8767` - Parakeet ASR

## Node Agent (WebUI control + Tailnet advertising)

The **node agent** (`edge/service_manager.py`) is a small host-side HTTP API that does
two jobs for one machine:

1. **Control** — lets the WebUI System page start/stop/restart services and switch the
   active ASR/TTS provider (it wraps `services.py`).
2. **Advertise** — announces this node's services (and itself, as `chronicle-node`) on
   the Tailnet via minidisc, so the backend/other nodes discover them. The advertised
   labels carry **live** state — `running` and `health` — refreshed on a timer
   (`ADVERTISE_REFRESH_SECS`, default 30s), not just what's enabled. This folds in the
   old standalone discovery agent, which has been removed.

It must run natively on the host: docker compose needs host bind-mount paths, and (on
Docker Desktop/WSL2) a container can't bind the Tailscale interface to advertise.

- **Started automatically** by `./services start --all` / `./services restart --all` on **any** start (so a
  service-only node with no backend still advertises). `stop --all` leaves the
  agent running; `./services manager stop` stops it explicitly
- **Manual control**: `./services manager start|stop|restart`
- **Identity / cluster**: `GET /node` (host, Tailscale name/IP, arch, GPU) and `GET /cluster` (live tailnet view); both token-gated. `GET /health` is unauthed.
- **Port**: 8775 (override with `SERVICE_MANAGER_PORT`)
- **Auth**: bearer token, auto-generated into `backend/.env` as `SERVICE_MANAGER_TOKEN` on first start; the backend reads the same value and proxies admin-only requests (`/api/admin/services/*`) to the agent
- **Distributed setups**: run the agent on the machine that hosts the services and point the backend at it via `SERVICE_MANAGER_URL` (e.g. `http://gpu-box.ts.net:8775`); copy the token into both machines' configuration
- **Logs / PID**: `edge/service-manager.log`, `edge/.service-manager.pid`
- **Remote service nodes**: a GPU box / RPi that runs a single service joins the cluster either via the wizard (*Setup type → Join a cluster*) or the one-liner `edge/install.sh <service>` — **both default to the node agent** (advertise + control + reboot persistence) and need no `SERVICE_MANAGER_URL` wiring. The legacy advertise-only `edge-agent` sidecar (`--profile edge`) is the secondary fallback via `edge/install.sh --advertise-only`, for boxes where you don't want a host process (no control, no WSL2). See [edge/README.md](../edge/README.md).

### Auto-start on boot (systemd user services)

`manager install` installs **two** systemd *user* services (with linger, so they
start without an interactive login):

1. **`chronicle-service-manager`** — the node agent itself. It runs **natively on the
   host**, not in Docker, so it does **not** come back after a reboot on its own; a
   fresh boot otherwise leaves the WebUI System page showing "Service manager not up,
   use ./services start --all". `Type=exec`, started immediately on install.
2. **`chronicle-stack`** — a `Type=oneshot` that runs `./services start --all` on
   boot to bring the **container stack** back (enabled services from `config.yml`
   that placement permits on this node, exactly what `./services start --all` does).
   `ExecStop` runs `./services stop --all`, so stopping or restarting this unit
   also stops the containers. Start and stop each have a 15-minute budget.
   Under **Docker** the containers' `restart:`
   policy revives them on boot, so this is belt-and-suspenders; under **rootless
   Podman** it's essential — Podman is daemonless and nothing re-applies `restart:`
   policies after a reboot (see [podman.md](podman.md)). Ordered
   `After=chronicle-service-manager.service`; enabled for boot only (installing it
   does **not** kick off a full `start --all` — `./services start --all` owns the running stack).

The wizard offers both near the end of setup ("Auto-start on boot"); you can also do
it manually:

```bash
# Install both units (~/.config/systemd/user/chronicle-{service-manager,stack}.service),
# enable linger, start the agent now and register the stack for boot
./services manager install

# Remove both
./services manager uninstall
```

Once installed, `./services start --all` uses systemd to ensure the **node agent** is running;
the CLI submits container operations to that agent. `./services stop --all` leaves the managed agent
running so it remains available for service controls. Inspect boot registration with
`systemctl --user status chronicle-service-manager chronicle-stack`.

Use `systemctl --user start|stop|restart chronicle-stack` to manage the stack through
systemd, or use `./services` for operator commands. CLI operations do not
update the oneshot unit's active state; after a direct stop, use `./services start --all` or
`systemctl --user restart chronicle-stack` to bring containers back. Inspect logs
with `journalctl --user -u chronicle-stack` or `-u chronicle-service-manager`.

Start, stop and restart return a nonzero exit status if any selected service fails.
Bulk start/restart skips groups assigned only to other nodes, allows declared HA
instances, and fails closed if the placement plan cannot be read. Each activation
still acquires its normal reservation. Explicit service requests remain subject to
admission; stop does not require the authority, including for excluded instances.
Start/restart waits for selected services to become ready; see the completion rules below.

To refresh an older installed unit, rerun `./services manager install`. Installing
the unit does not restart containers. If startup fails after partially starting
services, inspect the command output and use `./services stop --all` to stop that partial stack.

> **Upgrading from the old two-agent layout:** the standalone `chronicle-discovery`
> systemd unit is obsolete (the node agent advertises now). `./services start --all` and
> `./services manager install` auto-disable and remove a leftover `chronicle-discovery`
> unit, so no manual cleanup is needed.

> **Requires a systemd user instance.** On a normal Linux host this is available out
> of the box. On **WSL**, enable systemd first: add a `[boot]` section with
> `systemd=true` to `/etc/wsl.conf`, run `wsl --shutdown`, then reopen the terminal.
> If it's unavailable, the wizard/CLI prints this hint and skips installation.

From the WebUI (System page → External Services) you can start/stop/restart any
enabled service and switch ASR/TTS providers. Provider switches write the new
`ASR_PROVIDER`/`TTS_PROVIDER` to the service's `.env`, stop the old container, and
start the new one (they share a port). Tick "Build images" if the new provider's
image hasn't been built yet.

## Service Management

`./services` is the single operator entry point. It selects Docker or Podman from
configuration and uses the same lifecycle implementation as the WebUI. Run it from
the checkout; it resolves its own working directory. The old root start/stop/restart/
status scripts and Python CLI entry points have been removed.

### Everyday operations

```bash
./services status
./services status llm-services wakeword-service --detailed
./services status --json
./services start llm-services wakeword-service
./services restart backend
./services stop tts
./services start --all
./services stop --all
./services start backend --build --force-recreate
./services logs wakeword-service --tail 100
./services inspect llm-services
./services doctor
./services deployments --status
```

Lifecycle commands require service names or explicit `--all`, never both. Names
refer to service groups from the registry, not container names; help lists the
available groups. ASR/TTS provider selection remains in setup and WebUI controls.
A normal restart recreates containers in place without rebuilding images;
`restart --recreate` performs down/up. Use `--build` for image changes. Mounted code
must be present in the actual runtime mount; a deployment using a reviewed source
snapshot needs that snapshot refreshed before restart.

The current node is the default. Remote operations require an explicit registered
node ID; selection never silently moves to another owner:

```bash
./services status --node rainbow
./services restart tts --node rainbow
./services logs tts --node rainbow --tail 100
```

`--all` start/restart selects enabled services allowed on that node by the placement
plan. Stop can clean up excluded instances even if the authority is unavailable.
Local inventory, endpoint readiness, and backend dependency health are separate
status fields: a stopped local service may be supplied by another node.

### Completion and recovery

Normal lifecycle commands submit to the node manager, display an operation ID and
progress, and wait for completion. The local manager is started once if needed,
with a 30-second startup deadline. Read-only commands never start it. `stop --all`
leaves the manager running; use `./services manager stop` to stop it explicitly.

Start/restart launches every selected group before checking readiness. It waits up
to 300 seconds after the container actions finish; change this with `--timeout`.
Success requires running containers and provider-aware endpoint readiness, including
configured identity checks. Stop verifies that containers stopped. A timeout reports
failure without rolling back or resubmitting. Exit codes are 0 for success/accepted,
1 for operational failure, and 2 for invalid usage.

```bash
./services start llm-services --timeout 600
./services start llm-services --no-wait
./services operation OPERATION_ID
./services operation OPERATION_ID --node rainbow --json
```

`--no-wait` returns after acceptance; the manager continues readiness checks. Ctrl-C
stops waiting without cancelling the accepted action. Poll its ID instead of issuing
the action again. Operation polling is node-local and retained in manager memory;
a manager restart can lose that polling record. Consult logs/System Events rather
than assuming a lost record means the action did not happen.

If the manager cannot start, use explicit local recovery:

```bash
./services stop llm-services --direct
```

`--direct` uses the same executor and host lock without the manager. It cannot be
combined with `--node` or `--no-wait`. Admission still applies to start/restart, so an
unavailable placement authority blocks activation. There is no automatic fallback.

Readiness is not a complete end-to-end test: verify generation for an LLM, fresh
job activity for background work, and a spoken-device test for wakeword when those
capabilities are the subject of a repair.

### Operation history and diagnostics

CLI and WebUI lifecycle actions share node-manager operation tracking and **System
Events → service** reporting. Event reporting is best effort: if the backend is down,
the operation reports that history could not be written and node-manager logs remain
available. Direct recovery is explicitly marked as outside that ledger.

`logs` defaults to 100 lines per group container, with a maximum of 10,000; use
`--container NAME` to select a container within the group. Live following is not
implemented. `inspect` returns selected state, image, ports, mounts, and health timing
fields, excluding environment variables and health command credentials. Both support
`--json` and `--node`. Human operation timestamps are IST; JSON timestamps are Unix
seconds. JSON output contains no decorative text.

Use these commands before raw container-engine diagnostics. If a specific question
requires evidence they do not expose, state that gap and use a scoped read-only
engine command on the hosting node. Never dump full environments. Raw engine
lifecycle commands are reserved for isolated development, unmanaged services, or a
broken Chronicle CLI; explicit `--direct` is the first local recovery path.

```bash
journalctl --user -u chronicle-service-manager
journalctl --user -u chronicle-stack
```

An unmanaged manager writes `edge/service-manager.log`. For an unexpected restart,
check operation history first. Missing history does not prove a crash: direct
recovery, failed event reporting, and raw engine actions can leave no ledger entry.

## Configuration Files

### Generated Files
- `backend/.env` - Backend configuration with all services
- `extras/speaker-recognition/.env` - Speaker service configuration
- All services backup existing `.env` files automatically

### Required Dependencies
- **Root**: `setup-requirements.txt` (rich>=13.0.0)
- **Backend**: `setup-requirements.txt` (rich>=13.0.0, pyyaml>=6.0.0)
- **Extras**: No additional setup dependencies required

### Why `chronicle-setup` is installed editable

`setup-requirements.txt` lists the shared package as `-e ./extras/chronicle-setup`.
The `-e` matters: `uv run --with-requirements` caches the environment it
builds and reuses it while this file's text is unchanged, so a non-editable path
dependency stays frozen at the sources it was first built from. Edits to
`chronicle_setup/` are then silently ignored by the wizard, every `init.py`, and
`./services doctor`, which keep running a stale wheel from `~/.cache/uv`.

Measured on uv 0.6.16: neither `uv cache clean chronicle-setup` nor a
`[tool.uv] cache-keys` entry invalidates it — only `--reinstall-package`, which
every invocation would have to remember. Editable installs link to the source
tree instead. If a change to this package appears to have no effect, check that
the `-e` is still there:

```bash
uv run --with-requirements setup-requirements.txt \
  python -c "import chronicle_setup, sys; print(chronicle_setup.__file__)"
# want the repo path, not a ~/.cache/uv/archive-* path
```

## Troubleshooting

Start with the [service-management workflow](#service-management): detailed status,
host diagnostics, placement checks, and then scoped logs. Check that the configured
container engine is available (Docker needs its daemon; rootless Podman does not).
For Podman host prerequisites and engine-specific faults, see [podman.md](podman.md).
Use the actual deployment address for endpoint checks; localhost applies only on the
hosting node. Prefer HTTPS and use `curl -k` for an internal/self-signed certificate.
