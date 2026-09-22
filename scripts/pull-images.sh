#!/usr/bin/env bash
# pull-images.sh — Pull Chronicle images and retag them locally
#
# Usage (GHCR — default, no account needed):
#   ./scripts/pull-images.sh v1.0.0
#
# Usage (custom registry):
#   CHRONICLE_REGISTRY=ghcr.io/myuser/ ./scripts/pull-images.sh v1.0.0
#   DOCKERHUB_USERNAME=myuser ./scripts/pull-images.sh v1.0.0   # backward compat
#
# After pulling, start with the prebuilt images:
#   ./services start --all --use-prebuilt v1.0.0

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}ℹ️  $*${RESET}"; }
success() { echo -e "${GREEN}✅ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${RESET}"; }
error()   { echo -e "${RED}❌ $*${RESET}" >&2; }

# ── Validate inputs ────────────────────────────────────────────────────────────
TAG="${1:-}"
if [[ -z "$TAG" ]]; then
    error "TAG argument is required."
    echo "  Example: ./scripts/pull-images.sh v1.0.0" >&2
    exit 1
fi

# Registry precedence: CHRONICLE_REGISTRY > DOCKERHUB_USERNAME > default GHCR
if [[ -n "${CHRONICLE_REGISTRY:-}" ]]; then
    REGISTRY="${CHRONICLE_REGISTRY}"
elif [[ -n "${DOCKERHUB_USERNAME:-}" ]]; then
    REGISTRY="${DOCKERHUB_USERNAME}/"
else
    REGISTRY="ghcr.io/simpleopensoftware/"
fi

# ── Image inventory ────────────────────────────────────────────────────────────
# Format: "local-image-name:registry-image-name"
# After pulling, each remote image is retagged to "<local-image-name>:<TAG>"
# so that docker-compose can find it when CHRONICLE_TAG=<TAG> is set.
IMAGES=(
    "chronicle-backend:chronicle-backend"
    "chronicle-speaker:chronicle-speaker"
    "chronicle-speaker-strixhalo:chronicle-speaker-strixhalo"
    "chronicle-speaker-webui:chronicle-speaker-webui"
    "chronicle-asr-nemo:chronicle-asr-nemo"
    "chronicle-asr-nemo-strixhalo:chronicle-asr-nemo-strixhalo"
    "chronicle-asr-faster-whisper:chronicle-asr-faster-whisper"
    "chronicle-asr-vibevoice:chronicle-asr-vibevoice"
    "chronicle-asr-vibevoice-strixhalo:chronicle-asr-vibevoice-strixhalo"
    "chronicle-asr-transformers:chronicle-asr-transformers"
    "chronicle-asr-qwen3-vllm:chronicle-asr-qwen3-vllm"
    "chronicle-asr-qwen3-wrapper:chronicle-asr-qwen3-wrapper"
    "chronicle-asr-qwen3-bridge:chronicle-asr-qwen3-bridge"
    "chronicle-havpe-relay:chronicle-havpe-relay"
)

echo ""
echo -e "${BOLD}Chronicle Image Pull${RESET}"
echo -e "  Registry : ${REGISTRY}"
echo -e "  Tag      : ${TAG}"
echo ""

# ── Pull loop ─────────────────────────────────────────────────────────────────
PULLED=()
FAILED=()

for entry in "${IMAGES[@]}"; do
    LOCAL_BASE="${entry%%:*}"
    REMOTE_BASE="${REGISTRY}${entry##*:}"
    REMOTE_TAG="${REMOTE_BASE}:${TAG}"
    LOCAL_TAG="${LOCAL_BASE}:${TAG}"

    echo -e "${CYAN}── ${entry##*:}${RESET}"
    info "  Pulling  ← ${REMOTE_TAG}"

    if docker pull "${REMOTE_TAG}"; then
        # Retag to local name so docker-compose finds it with CHRONICLE_TAG=<TAG>
        info "  Retagging → ${LOCAL_TAG}"
        docker tag "${REMOTE_TAG}" "${LOCAL_TAG}"
        success "  Ready as ${LOCAL_TAG}"
        PULLED+=("${entry##*:}")
    else
        warn "  Not found in registry — skipping (this service may not have been pushed)"
        FAILED+=("${entry##*:}")
    fi
    echo ""
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "${BOLD}── Summary ──────────────────────────────────────────────────────${RESET}"
printf "%-40s %s\n" "Image" "Status"
printf "%-40s %s\n" "─────────────────────────────────────" "──────"

for entry in "${IMAGES[@]}"; do
    IMAGE_NAME="${entry##*:}"
    STATUS=""
    for p in "${PULLED[@]}"; do
        [[ "$p" == "$IMAGE_NAME" ]] && STATUS="${GREEN}pulled${RESET}" && break
    done
    for f in "${FAILED[@]}"; do
        [[ "$f" == "$IMAGE_NAME" ]] && STATUS="${YELLOW}not found${RESET}" && break
    done
    [[ -z "$STATUS" ]] && STATUS="${YELLOW}not found${RESET}"
    printf "%-40s " "${IMAGE_NAME}"
    echo -e "${STATUS}"
done

echo ""
if [[ ${#PULLED[@]} -gt 0 ]]; then
    success "Pulled ${#PULLED[@]} image(s) tagged as ${TAG}"
    echo ""
    echo "Start services with prebuilt images:"
    echo -e "  ${BOLD}./services start --all --use-prebuilt ${TAG}${RESET}"
fi
if [[ ${#FAILED[@]} -gt 0 ]]; then
    warn "${#FAILED[@]} image(s) not found in registry (these services will fall back to local builds)"
fi
