# Pi 0.85 requires Node >=22.19. Keep the runtime version explicit instead of
# inheriting whichever Node happens to be present in a Python base image.
ARG NODE_VERSION=22.19.0

# ============================================
# Builder stage - install dependencies
# ============================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Install system dependencies needed for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    git \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Copy dependency files first (cache-friendly)
COPY pyproject.toml uv.lock ./

# Build a project .venv from the lockfile (includes git deps like Graphiti)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra galileo --extra benchmark --no-install-project


# ============================================
# notesmd-cli builder - vault note rename/move with backlink rewrite
# ============================================
# The memory agent uses `notesmd-cli move` to rename a person note and rewrite every
# [[wikilink]] to it across the vault in one shot (verified non-destructive), and the
# fork adds `create --template` (seed a note from a Templates/ note + {{date}}/{{title}}).
# Pinned to a specific commit so image builds are reproducible; bump NOTESMD_REF to update.
# Note this clones from GitHub at build time, so the fork must stay reachable.
FROM golang:1.25-bookworm AS notesmd-builder
ARG NOTESMD_REPO=https://github.com/AnkushMalaker/notesmd-cli
ARG NOTESMD_REF=c906cc10
RUN git clone ${NOTESMD_REPO} /src \
    && cd /src \
    && git checkout ${NOTESMD_REF} \
    && CGO_ENABLED=0 go build -trimpath -mod=vendor -o /out/notesmd-cli .


# ============================================
# Codex CLI fetcher - optional memory-agent backend
# ============================================
# Static musl binary used when the memory write agent selects the Codex backend
# (ChatGPT subscription). Auth is NOT baked in — it comes from the CODEX_HOME volume
# mount (see docker-compose.yml / the setup wizard). Codex is not a search backend.
FROM debian:bookworm-slim AS codex-fetcher
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG CODEX_VERSION=0.147.0
ARG TARGETARCH
RUN case "${TARGETARCH:-amd64}" in \
      arm64) triple=aarch64-unknown-linux-musl ;; \
      *) triple=x86_64-unknown-linux-musl ;; \
    esac \
    && curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${triple}.tar.gz" \
       | tar -xz -C /tmp \
    && curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-${triple}.tar.gz" \
       | tar -xz -C /tmp \
    && install -m 0755 "/tmp/codex-${triple}" /usr/local/bin/codex \
    && install -m 0755 "/tmp/codex-code-mode-host-${triple}" \
       /usr/local/bin/codex-code-mode-host


# ============================================
# Pi CLI fetcher - local/OpenAI-compatible memory-agent backend
# ============================================
# Install once, then copy only Node and the self-contained global package into both
# final stages. Pi 0.85.1 declares Node >=22.19.0. It does not need an auth volume for
# Chronicle: the runtime writes an ephemeral models.json for the resolved model.
FROM node:${NODE_VERSION}-bookworm-slim AS pi-fetcher
ARG PI_VERSION=0.85.1
RUN npm install --global --prefix /opt/pi --omit=dev --ignore-scripts --no-audit --no-fund \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force


# ============================================
# Production stage
# ============================================
FROM python:3.12-slim-bookworm AS prod

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    curl \
    ffmpeg \
    ripgrep \
    libc++1 \
    && rm -rf /var/lib/apt/lists/*

# notesmd-cli on PATH so the memory agent auto-detects it (shutil.which).
COPY --from=notesmd-builder /out/notesmd-cli /usr/local/bin/notesmd-cli
# Codex CLI on PATH for the optional Codex memory-write backend.
COPY --from=codex-fetcher /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex-fetcher /usr/local/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host
# Pi CLI and its compatible Node runtime. npm itself is not needed at runtime.
COPY --from=pi-fetcher /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-fetcher /usr/local/LICENSE /usr/local/LICENSE.node
COPY --from=pi-fetcher /opt/pi /opt/pi

WORKDIR /app

# Copy locked virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Source layout + venv defaults for runtime shells and commands
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:/opt/pi/bin:${PATH}"
ENV PYTHONPATH=/app/src:/app

# Code version baked at build time (git describe locally via services.py, the
# release tag in CI) — surfaced by the backend's /version endpoint so a running
# container reports what it was built from.
ARG CHRONICLE_BUILD_VERSION=dev
ENV CHRONICLE_BUILD_VERSION=${CHRONICLE_BUILD_VERSION}

# Copy application code
COPY . .

# diarization_config.json (if present) is already included by "COPY . ." above.
# A separate optional-glob COPY ("json*") errors under buildah/podman when the
# file is absent, and is redundant under docker — so it's intentionally omitted.

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]


# ============================================
# Dev/Test stage - includes test dependencies
# ============================================
FROM python:3.12-slim-bookworm AS dev

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    curl \
    ffmpeg \
    ripgrep \
    libc++1 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# notesmd-cli on PATH so the memory agent auto-detects it (shutil.which).
COPY --from=notesmd-builder /out/notesmd-cli /usr/local/bin/notesmd-cli
# Codex CLI on PATH for the optional Codex memory-write backend.
COPY --from=codex-fetcher /usr/local/bin/codex /usr/local/bin/codex
# Pi CLI and its compatible Node runtime. npm itself is not needed at runtime.
COPY --from=pi-fetcher /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-fetcher /usr/local/LICENSE /usr/local/LICENSE.node
COPY --from=pi-fetcher /opt/pi /opt/pi

WORKDIR /app

# For dev, create project .venv with test deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra galileo --extra benchmark --group test --no-install-project && \
    rm /bin/uv /bin/uvx

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:/opt/pi/bin:${PATH}"
ENV PYTHONPATH=/app/src:/app

# Copy application code
COPY . .

# diarization_config.json (if present) is already included by "COPY . ." above.
# A separate optional-glob COPY ("json*") errors under buildah/podman when the
# file is absent, and is redundant under docker — so it's intentionally omitted.

# Copy and make startup script executable
COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]
