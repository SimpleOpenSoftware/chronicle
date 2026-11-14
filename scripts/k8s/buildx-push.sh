#!/usr/bin/env bash
# Skaffold custom builder: build one artifact on a remote buildx builder and push it.
#
# Skaffold's docker builder loads images into the local daemon before pushing, which
# fails with a docker-container buildx builder, and the cluster nodes are amd64 while a
# Mac builds arm64. This builds natively on the builder named by BUILDX_BUILDER
# (config.env) and pushes straight to the registry.
#
# Usage (as a Skaffold custom.buildCommand, run from the artifact context):
#   buildx-push.sh <dockerfile> [docker buildx build args...]
# Pass a bare `--build-arg NAME` to take NAME's value from the environment.
# Skaffold provides IMAGE, BUILD_CONTEXT and (with --platform) PLATFORMS.
set -euo pipefail

dockerfile="$1"
shift

exec docker buildx build \
  --builder "${BUILDX_BUILDER:?set BUILDX_BUILDER in config.env}" \
  --platform "${PLATFORMS:-linux/amd64}" \
  --file "$dockerfile" \
  --tag "${IMAGE:?IMAGE is set by Skaffold}" \
  --push \
  "$@" \
  "${BUILD_CONTEXT:-.}"
