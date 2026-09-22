#!/usr/bin/env bash
# push-images.sh — Tag and push locally-built Chronicle images to a registry
#
# Usage:
#   CHRONICLE_PUSH_REGISTRY=ghcr.io/myuser/ ./scripts/push-images.sh v1.0.0 "stable before refactor"
#   DOCKERHUB_USERNAME=myuser ./scripts/push-images.sh v1.0.0 "description"   # backward compat
#
# Requirements:
#   - Images must already be built locally (run ./services start --all --build first)
#   - CHRONICLE_PUSH_REGISTRY or DOCKERHUB_USERNAME env var must be set
#   - Must be logged in to the target registry (docker login)

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
# Registry precedence: CHRONICLE_PUSH_REGISTRY > DOCKERHUB_USERNAME
if [[ -n "${CHRONICLE_PUSH_REGISTRY:-}" ]]; then
    REGISTRY="${CHRONICLE_PUSH_REGISTRY}"
elif [[ -n "${DOCKERHUB_USERNAME:-}" ]]; then
    REGISTRY="${DOCKERHUB_USERNAME}/"
else
    error "CHRONICLE_PUSH_REGISTRY or DOCKERHUB_USERNAME env var is required."
    echo "  GHCR:      CHRONICLE_PUSH_REGISTRY=ghcr.io/yourusername/ ./scripts/push-images.sh v1.0.0" >&2
    echo "  DockerHub: DOCKERHUB_USERNAME=yourusername ./scripts/push-images.sh v1.0.0" >&2
    exit 1
fi

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
    error "TAG argument is required."
    echo "  Example: CHRONICLE_PUSH_REGISTRY=ghcr.io/myuser/ ./scripts/push-images.sh v1.0.0 'description'" >&2
    exit 1
fi

DESCRIPTION="${2:-}"

# ── Image inventory ────────────────────────────────────────────────────────────
# Format: "local-image-name:registry-image-name"
# local-image-name  = what docker-compose builds to locally (with empty CHRONICLE_REGISTRY)
# registry-image-name = what gets pushed to DockerHub
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

# ── Collect git info ───────────────────────────────────────────────────────────
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
DATE=$(date +%Y-%m-%d)

echo ""
echo -e "${BOLD}Chronicle Image Push${RESET}"
echo -e "  Registry : ${REGISTRY}"
echo -e "  Tag      : ${TAG}"
echo -e "  Git SHA  : ${GIT_SHA}"
echo -e "  Date     : ${DATE}"
[[ -n "$DESCRIPTION" ]] && echo -e "  Desc     : ${DESCRIPTION}"
echo ""

# ── Push loop ─────────────────────────────────────────────────────────────────
PUSHED=()
SKIPPED=()

for entry in "${IMAGES[@]}"; do
    LOCAL_NAME="${entry%%:*}:latest"
    REMOTE_BASE="${REGISTRY}${entry##*:}"
    REMOTE_TAG="${REMOTE_BASE}:${TAG}"
    REMOTE_LATEST="${REMOTE_BASE}:latest"

    echo -e "${CYAN}── ${LOCAL_NAME}${RESET}"

    # Check if image exists locally
    if ! docker image inspect "${LOCAL_NAME}" > /dev/null 2>&1; then
        warn "  Not found locally — skipping (run ./services start --all --build to build it first)"
        SKIPPED+=("${LOCAL_NAME}")
        continue
    fi

    # Tag and push versioned tag
    info "  Tagging  → ${REMOTE_TAG}"
    docker tag "${LOCAL_NAME}" "${REMOTE_TAG}"

    info "  Pushing  → ${REMOTE_TAG}"
    if docker push "${REMOTE_TAG}"; then
        success "  Pushed ${REMOTE_TAG}"
    else
        error "  Failed to push ${REMOTE_TAG}"
        SKIPPED+=("${LOCAL_NAME}")
        continue
    fi

    # Also tag and push :latest
    info "  Tagging  → ${REMOTE_LATEST}"
    docker tag "${LOCAL_NAME}" "${REMOTE_LATEST}"

    info "  Pushing  → ${REMOTE_LATEST}"
    if docker push "${REMOTE_LATEST}"; then
        success "  Pushed ${REMOTE_LATEST}"
    else
        warn "  Failed to push :latest tag (versioned tag was already pushed)"
    fi

    PUSHED+=("${entry##*:}")
    echo ""
done

# ── Update releases.json ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASES_FILE="${SCRIPT_DIR}/releases.json"

# Build JSON array of pushed image names
IMAGES_JSON="["
for i in "${!PUSHED[@]}"; do
    [[ $i -gt 0 ]] && IMAGES_JSON+=","
    IMAGES_JSON+="\"${PUSHED[$i]}\""
done
IMAGES_JSON+="]"

NEW_ENTRY="{\"tag\":\"${TAG}\",\"description\":\"${DESCRIPTION}\",\"date\":\"${DATE}\",\"git_sha\":\"${GIT_SHA}\",\"images_pushed\":${IMAGES_JSON}}"

if [[ -f "$RELEASES_FILE" ]]; then
    # Append to existing array
    EXISTING=$(cat "$RELEASES_FILE")
    # Strip trailing ] and append new entry
    TRIMMED="${EXISTING%]}"
    # Handle empty array
    if [[ "$TRIMMED" == "[" ]]; then
        echo "[${NEW_ENTRY}]" > "$RELEASES_FILE"
    else
        echo "${TRIMMED},${NEW_ENTRY}]" > "$RELEASES_FILE"
    fi
else
    echo "[${NEW_ENTRY}]" > "$RELEASES_FILE"
fi

success "Recorded release in scripts/releases.json"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Summary ──────────────────────────────────────────────────────${RESET}"
printf "%-40s %s\n" "Image" "Status"
printf "%-40s %s\n" "─────────────────────────────────────" "──────"

for entry in "${IMAGES[@]}"; do
    IMAGE_NAME="${entry##*:}"
    LOCAL_NAME="${entry%%:*}:latest"
    STATUS=""
    for p in "${PUSHED[@]}"; do
        [[ "$p" == "$IMAGE_NAME" ]] && STATUS="${GREEN}pushed${RESET}" && break
    done
    for s in "${SKIPPED[@]}"; do
        [[ "$s" == "$LOCAL_NAME" ]] && STATUS="${YELLOW}skipped${RESET}" && break
    done
    [[ -z "$STATUS" ]] && STATUS="${YELLOW}skipped${RESET}"
    printf "%-40s " "${IMAGE_NAME}"
    echo -e "${STATUS}"
done

echo ""
if [[ ${#PUSHED[@]} -gt 0 ]]; then
    success "Pushed ${#PUSHED[@]} image(s) as ${TAG}"
    echo ""
    echo "To restore this snapshot:"
    echo "  CHRONICLE_REGISTRY=${REGISTRY} ./scripts/pull-images.sh ${TAG}"
    echo "  CHRONICLE_REGISTRY=${REGISTRY} ./services start --all --use-prebuilt ${TAG}"
fi
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
    warn "${#SKIPPED[@]} image(s) were skipped (not found locally)"
fi
