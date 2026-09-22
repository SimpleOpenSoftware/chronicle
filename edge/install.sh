#!/usr/bin/env bash
# Chronicle Edge — one-liner service deployment on remote nodes.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/.../edge/install.sh | bash -s -- speaker-recognition
#   curl -sSL ... | bash -s -- asr-services --branch dev
#   curl -sSL ... | bash -s -- --client [--pendant]      # client node: tray + collectors, no containers
#
# Prerequisites: docker (with compose) or podman (with podman-compose), tailscale (connected), uv, git
#   Engine selected via CONTAINER_ENGINE (default docker); compose via COMPOSE_CMD.
#   --client needs only git + uv (no container engine, no GPU): it installs the
#   desktop tray / ScreenPipe collector as user units plus the node agent.
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
BRANCH="main"
REPO_URL="https://github.com/SimpleOpenSoftware/chronicle.git"
# Default: run the native node agent (control + advertise, reboot-survivable via
# systemd). --advertise-only uses the legacy containerized sidecar instead
# (advertise only, no control, no host process).
ADVERTISE_ONLY=0
# Client node: data capture/streaming only (tray, ScreenPipe collector).
CLIENT_MODE=0
PENDANT=0

# Resolve CHRONICLE_HOME: explicit env var > detect existing clone > default
if [[ -n "${CHRONICLE_HOME:-}" ]]; then
    : # User explicitly set it
elif [[ -d "$HOME/chronicle/.git" ]]; then
    CHRONICLE_HOME="$HOME/chronicle"
elif [[ -d "$PWD/.git" && -d "$PWD/edge" ]]; then
    CHRONICLE_HOME="$PWD"
else
    CHRONICLE_HOME="$HOME/chronicle"
fi

# ── Service → compose path mapping ───────────────────────────────────
declare -A SERVICE_PATHS=(
    [speaker-recognition]=extras/speaker-recognition
    [asr-services]=extras/asr-services
    [tts]=extras/tts
    [llm-services]=extras/llm-services
    [havpe-relay]=extras/havpe-relay
    [colpali-service]=extras/colpali-service
)

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${CYAN}[edge]${NC} $*"; }
ok()    { echo -e "${GREEN}[edge]${NC} $*"; }
warn()  { echo -e "${YELLOW}[edge]${NC} $*"; }
err()   { echo -e "${RED}[edge]${NC} $*" >&2; }

# ── Parse args ────────────────────────────────────────────────────────
SERVICE_NAME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --repo)   REPO_URL="$2"; shift 2 ;;
        --advertise-only|--sidecar) ADVERTISE_ONLY=1; shift ;;
        --client)  CLIENT_MODE=1; shift ;;
        --pendant) PENDANT=1; shift ;;
        --help|-h)
            echo "Usage: $0 <service-name> [--branch <branch>] [--repo <url>] [--advertise-only]"
            echo "       $0 --client [--pendant] [--branch <branch>] [--repo <url>]"
            echo ""
            echo "  Default: installs the native node agent (control + advertise, survives reboot)."
            echo "  --advertise-only: legacy containerized sidecar (advertise only, no control)."
            echo "  --client: client node — desktop tray + ScreenPipe collector as user units,"
            echo "            no containers/GPU. --pendant adds BLE wearable streaming."
            echo ""
            echo "Available services:"
            for svc in "${!SERVICE_PATHS[@]}"; do echo "  $svc"; done | sort
            exit 0
            ;;
        -*) err "Unknown option: $1"; exit 1 ;;
        *)  SERVICE_NAME="$1"; shift ;;
    esac
done

if [[ "$CLIENT_MODE" != "1" && -z "$SERVICE_NAME" ]]; then
    err "Service name required (or --client). Run with --help for usage."
    exit 1
fi

# ── Validate service name ────────────────────────────────────────────
if [[ "$CLIENT_MODE" != "1" ]]; then
    if [[ -z "${SERVICE_PATHS[$SERVICE_NAME]+_}" ]]; then
        err "Unknown service: $SERVICE_NAME"
        err "Available: ${!SERVICE_PATHS[*]}"
        exit 1
    fi
    COMPOSE_PATH="${SERVICE_PATHS[$SERVICE_NAME]}"
fi

# ── Check prerequisites ──────────────────────────────────────────────
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        err "Required command not found: $1"
        err "Please install $1 before running this script."
        exit 1
    fi
}

info "Checking prerequisites..."
check_cmd git
check_cmd uv

if [[ "$CLIENT_MODE" != "1" ]]; then
    # Container engine: docker (default) or podman. Override with CONTAINER_ENGINE.
    ENGINE="${CONTAINER_ENGINE:-docker}"
    check_cmd "$ENGINE"

    # Resolve the compose command (COMPOSE_CMD wins; else derive from the engine)
    if [ -n "${COMPOSE_CMD:-}" ]; then
        COMPOSE="$COMPOSE_CMD"
    elif [ "$ENGINE" = "podman" ]; then
        if command -v podman-compose &>/dev/null; then
            COMPOSE="podman-compose"
        else
            err "podman-compose not found. Install it (e.g. 'uv tool install podman-compose')."
            exit 1
        fi
    elif docker compose version &>/dev/null; then
        COMPOSE="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE="docker-compose"
    else
        err "docker compose not found. Install Docker Compose."
        exit 1
    fi
fi

# Check Tailscale. Edge services need it (discovery + hub control); a client
# node works without it (local backend URL), so only warn there.
if ! command -v tailscale &>/dev/null || ! tailscale status &>/dev/null; then
    if [[ "$CLIENT_MODE" == "1" ]]; then
        warn "Tailscale not connected — the hub won't discover/control this client node."
        warn "Install/connect it later for remote updates: https://tailscale.com/download"
        TAILSCALE_IP="none"
    elif ! command -v tailscale &>/dev/null; then
        err "Tailscale not found. Install from https://tailscale.com/download"
        exit 1
    else
        err "Tailscale is not connected. Run: sudo tailscale up"
        exit 1
    fi
else
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
fi
ok "Prerequisites OK (Tailscale IP: $TAILSCALE_IP)"

# ── Clone / update repo ──────────────────────────────────────────────
if [[ -d "$CHRONICLE_HOME/.git" && -d "$CHRONICLE_HOME/edge" ]]; then
    info "Using existing repo at $CHRONICLE_HOME"
    cd "$CHRONICLE_HOME"
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [[ "$current_branch" != "$BRANCH" ]]; then
        git fetch origin "$BRANCH"
        git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
    fi
    git pull --rebase --autostash origin "$BRANCH" || {
        warn "Could not auto-update (you may have local changes). Continuing with current state."
    }
else
    info "Cloning Chronicle (branch: $BRANCH) to $CHRONICLE_HOME..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CHRONICLE_HOME"
fi
cd "$CHRONICLE_HOME"
ok "Repository ready at $CHRONICLE_HOME"

# ── Client node: tray + collectors as user units, no containers ──────
if [[ "$CLIENT_MODE" == "1" ]]; then
    CLIENT_ARGS=""
    [[ "$PENDANT" == "1" ]] && CLIENT_ARGS="--pendant"
    info "Installing client components + node agent (no containers, no GPU)..."
    ./services client install $CLIENT_ARGS

    ok "────────────────────────────────────────"
    ok "  Client node ready!"
    ok ""
    ok "  Tray:     look for the Chronicle icon in your system tray / menu bar"
    ok "  Status:   ./services client status"
    ok "  Collector: pair ScreenPipe via the WebUI Timeline → Sources panel, then"
    ok "             uv run --project extras/screenpipe-collector chronicle-screenpipe pair ..."
    ok "             ./services client install screenpipe-collector"
    ok ""
    ok "  Updates from the hub restart these components automatically."
    ok "────────────────────────────────────────"
    exit 0
fi

# ── Resolve compose path ─────────────────────────────────────────────
SERVICE_DIR="$CHRONICLE_HOME/$COMPOSE_PATH"

if [[ ! -d "$SERVICE_DIR" ]]; then
    err "Service directory not found: $SERVICE_DIR"
    exit 1
fi

info "Service directory: $SERVICE_DIR"

# ── Resolve backend URL for edge services ─────────────────────────────
# On edge nodes, host.docker.internal doesn't reach the backend.
# Try minidisc discovery first, then prompt if needed.
info "Looking for Chronicle backend on Tailnet..."
BACKEND_URL=$(cd "$CHRONICLE_HOME" && PYTHONPATH="$CHRONICLE_HOME" uv run --with-requirements "$CHRONICLE_HOME/setup-requirements.txt" python3 -c "
try:
    from discovery import CHRONICLE_BACKEND, discover_service
    url = discover_service(CHRONICLE_BACKEND)
    if url:
        print(url, end='')
    else:
        print('minidisc found no chronicle-backend service on Tailnet', file=__import__('sys').stderr)
except Exception as e:
    print(f'discovery failed: {e}', file=__import__('sys').stderr)
")

if [[ -n "$BACKEND_URL" ]]; then
    ok "Auto-discovered backend at $BACKEND_URL"
else
    warn "Could not auto-discover backend. Make sure your Chronicle backend is running with Tailscale."
    read -rp "[edge] Enter your Chronicle backend URL (e.g. http://100.x.x.x:8000): " BACKEND_URL </dev/tty
    if [[ -z "$BACKEND_URL" ]]; then
        err "Backend URL is required for edge deployment."
        exit 1
    fi
fi

# ── Run init.py if it exists (interactive config) ─────────────────────
INIT_SCRIPT="$SERVICE_DIR/init.py"
if [[ -f "$INIT_SCRIPT" ]]; then
    # Only pass --backend-url to init scripts that declare it (e.g. havpe-relay).
    # The ASR/speaker/tts/llm servers don't accept it — they're discovered BY the
    # backend, not the other way round — and their argparse uses parse_args(), so
    # passing it unconditionally aborted them with "unrecognized arguments".
    INIT_ARGS=""
    if [[ -n "$BACKEND_URL" ]] && grep -q "backend-url" "$INIT_SCRIPT"; then
        INIT_ARGS="--backend-url $BACKEND_URL"
    fi
    info "Running configuration wizard for $SERVICE_NAME..."
    # From the root, so uv resolves ./extras/chronicle-setup in the requirements.
    cd "$CHRONICLE_HOME"
    uv run --with-requirements setup-requirements.txt \
        python "$INIT_SCRIPT" $INIT_ARGS </dev/tty
elif [[ -f "$SERVICE_DIR/setup.sh" ]]; then
    info "Running setup script for $SERVICE_NAME..."
    cd "$SERVICE_DIR"
    bash setup.sh </dev/tty
    cd "$CHRONICLE_HOME"
else
    warn "No init script found — using defaults."
fi

# ── Create container network ──────────────────────────────────────────
"$ENGINE" network create chronicle-network 2>/dev/null || true

# ── Start the service ─────────────────────────────────────────────────
# havpe-relay isn't a services.py-managed service, so it can only use the
# advertise-only sidecar (the node agent can't start/advertise it).
if [[ "$SERVICE_NAME" == "havpe-relay" && "$ADVERTISE_ONLY" != "1" ]]; then
    info "havpe-relay isn't node-agent-managed — using the advertise-only sidecar."
    ADVERTISE_ONLY=1
fi

if [[ "$ADVERTISE_ONLY" == "1" ]]; then
    # Secondary path: service + a containerized advertise-only sidecar that rides
    # this compose project's lifecycle. No host process, but advertise-only.
    cd "$SERVICE_DIR"
    info "Starting $SERVICE_NAME + advertise-only edge sidecar..."
    PROFILES="--profile edge"
    if [[ -f .env ]]; then
        PYTORCH_VERSION=$(grep -s '^PYTORCH_CUDA_VERSION=' .env | cut -d= -f2 | tr -d "'" | tr -d '"' || echo "")
        if [[ "$PYTORCH_VERSION" == "strixhalo" ]]; then
            PROFILES="$PROFILES --profile strixhalo"
        elif [[ "$PYTORCH_VERSION" == cu* ]]; then
            PROFILES="$PROFILES --profile gpu"
        elif [[ -n "$PYTORCH_VERSION" ]]; then
            PROFILES="$PROFILES --profile cpu"
        fi
    fi
    $COMPOSE $PROFILES up --build -d
    MODE_DESC="advertise-only sidecar (no control)"
    STATUS_CMD="cd $SERVICE_DIR && $COMPOSE $PROFILES ps"
    LOGS_CMD="cd $SERVICE_DIR && $COMPOSE $PROFILES logs -f"
    STOP_CMD="cd $SERVICE_DIR && $COMPOSE $PROFILES down"
else
    # Default path: native node agent — starts the service AND advertises it on the
    # Tailnet (with live health) AND survives reboot via a systemd user service.
    # services.py drives compose (incl. GPU profiles), so we don't replicate it here.
    cd "$CHRONICLE_HOME"
    info "Enabling $SERVICE_NAME in config.yml (node-only; backend stays off)..."
    uv run --with-requirements setup-requirements.txt python3 - "$SERVICE_NAME" <<'PY'
import sys
from config_manager import ConfigManager
svc = sys.argv[1]
m = ConfigManager()
m.ensure_config_yml()
services = dict(m.get_full_config().get("services") or {})
services[svc] = True
services["backend"] = False  # a join node never runs the backend
m.set_enabled_services(services)
print(f"enabled {svc}")
PY
    info "Starting $SERVICE_NAME + node agent (advertises on the Tailnet)..."
    ./services start "$SERVICE_NAME" --build
    info "Installing node agent for boot persistence (skipped if systemd unavailable)..."
    ./services manager install || true
    MODE_DESC="node agent (advertise + control, reboot-survivable)"
    STATUS_CMD="cd $CHRONICLE_HOME && ./services status"
    LOGS_CMD="$ENGINE ps   # then: $ENGINE logs -f <container>"
    STOP_CMD="cd $CHRONICLE_HOME && ./services stop $SERVICE_NAME"
fi

ok "────────────────────────────────────────"
ok "  $SERVICE_NAME is running!"
ok ""
ok "  Tailscale IP:  $TAILSCALE_IP"
ok "  Mode:          $MODE_DESC"
ok ""
ok "  Status:  $STATUS_CMD"
ok "  Logs:    $LOGS_CMD"
ok "  Stop:    $STOP_CMD"
ok ""
ok "  The service should appear on the Network page"
ok "  of your Chronicle backend dashboard."
ok "────────────────────────────────────────"
