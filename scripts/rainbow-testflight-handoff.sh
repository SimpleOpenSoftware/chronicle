#!/usr/bin/env bash

set -euo pipefail

readonly BRANCH="feature/full-duplex-voice-cutover"
readonly REPOSITORY="SimpleOpenSoftware/chronicle"

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 /path/to/chronicle /path/to/new-worktree EXPECTED_SHA" >&2
  exit 2
fi

readonly SOURCE_REPO="$1"
readonly WORKTREE_PATH="$2"
readonly EXPECTED_SHA="$3"

if [[ ! -d "${SOURCE_REPO}/.git" && ! -f "${SOURCE_REPO}/.git" ]]; then
  echo "Source repository is not a Git checkout: ${SOURCE_REPO}" >&2
  exit 2
fi
if [[ -e "${WORKTREE_PATH}" ]]; then
  echo "Refusing to reuse an existing worktree path: ${WORKTREE_PATH}" >&2
  exit 2
fi
if [[ ! "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "EXPECTED_SHA must be a full 40-character commit SHA" >&2
  exit 2
fi

git -C "${SOURCE_REPO}" fetch --prune origin \
  "refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"

readonly REMOTE_SHA="$(git -C "${SOURCE_REPO}" rev-parse "refs/remotes/origin/${BRANCH}")"
if [[ "${REMOTE_SHA}" != "${EXPECTED_SHA}" ]]; then
  echo "Remote branch is ${REMOTE_SHA}, expected ${EXPECTED_SHA}" >&2
  exit 1
fi

git -C "${SOURCE_REPO}" worktree add --detach "${WORKTREE_PATH}" "${EXPECTED_SHA}"

if [[ -n "$(git -C "${WORKTREE_PATH}" status --porcelain=v1)" ]]; then
  echo "New worktree is unexpectedly dirty" >&2
  exit 1
fi

pushd "${WORKTREE_PATH}/app" >/dev/null

if [[ "$(node --version)" != v22.* ]]; then
  echo "Rainbow must use Node 22 to match the TestFlight workflow" >&2
  exit 1
fi

npm ci
npm run typecheck
npm run test:phone-audio-diagnostics
npm run check:theme
npx --no-install expo config --type public --json >/dev/null
npx --no-install expo-modules-autolinking verify --platform ios --verbose
npx --no-install expo-modules-autolinking resolve --platform apple --json >/dev/null

if ! command -v swift >/dev/null 2>&1; then
  echo "Swift is required for the native cancellation policy gate" >&2
  exit 1
fi
swift test --package-path modules/chronicle-duplex-audio/ios

popd >/dev/null

echo
echo "Rainbow validation passed for ${EXPECTED_SHA}."
echo "Review the generated Expo config and autolinking output, then submit with:"
echo
echo "gh workflow run ios-testflight.yml --repo ${REPOSITORY} --ref ${BRANCH}"
