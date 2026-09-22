# AGENTS.md

This file provides guidance to coding agents working in this repository.

## Project Overview

Chronicle is at the core an AI-powered personal system - various devices, including but not limited to wearables from OMI can be used for at the very least audio capture, speaker specific transcription, memory extraction and retrieval.
On top of that - it is being designed to support other services, that can help a user with these inputs such as reminders, action items, personal diagnosis etc.

This supports a comprehensive web dashboard for management.

**⚠️ Active Development Notice**: This project is under active development. Do not create migration scripts or assume stable APIs. Only offer suggestions and improvements when requested.

**❌ No Backward Compatibility**: Do NOT add backward compatibility code unless explicitly requested. This includes fallback logic, legacy field support, or compatibility layers. Always ask before adding backward compatibility - in most cases the answer is no during active development.

## Local-Only Research Rule

**Do not stage, commit, or push research or experimental material unless the user explicitly authorizes the exact paths.** Treat it as machine-local by default, even when it is untracked and appears useful.

This includes research code and notes, datasets, recordings/media, model weights and checkpoints, archives, screenshots, logs, caches, benchmark/evaluation outputs, generated reports, backups, secrets, and machine-local configuration. In particular, do not add material from `experiments/`, `extras/ml-experiments/`, wake-word training/evaluation trees, or similar research directories while curating unrelated commits. Do not suggest bundling these files into a baseline or feature branch. Leave them untouched and untracked unless the user names the specific files to publish.

## Live Deployment Location

A Chronicle deployment often runs on a **different machine** from the checkout you are
working in — commonly a home server reached over a Tailnet. A stopped or unconfigured
local stack therefore does not mean Chronicle is unavailable.

This file is checked in and reads the same everywhere, so it deliberately names no
host. Run `hostname` first, then read `CLAUDE.local.md` (gitignored, per-user) for the
addresses that apply to the machine you are on — including the case where the checkout
you are in *is* the deployment, and everything is local.

For read-only checks of the running application (health checks, API inspection, or
browser/screenshot verification):

1. Use the deployment address from `CLAUDE.local.md`, if there is one. Prefer HTTPS;
   plain HTTP usually redirects. A self-signed or internal-CA cert needs `curl -k`.
2. Otherwise inspect the current Tailscale mesh with `tailscale status --json` and
   resolve the online peer serving Chronicle. Match on `OS` as well as `HostName` — a
   host running under WSL2 advertises the same hostname from both the Windows host and
   the Linux guest, and only the guest serves anything. Prefer the returned `DNSName`;
   use a Tailscale IP only as a temporary fallback, and never persist a `100.x` address
   because it may change.
3. If the host is reachable but the Chronicle endpoint is unclear, use the repository's
   `discovery.py`/minidisc support to discover the `chronicle-backend` service on the
   Tailnet.

Do not assume a remote deployment's checkout is clean or matches `origin` — check
`git status` there before pulling. Do not start a duplicate local deployment merely to
inspect the live application. Remote deployment, restarts, or other mutations still
require the user's request to change or deploy the running system.

### Verifying live historical data

Before claiming that captured data was not sent, stored, processed, or added to a
timeline:

- Anchor the investigation on the exact user-provided URL or ID. Confirm the live
  database and inspect a real document's field names and BSON types; do not guess that
  a domain ID is MongoDB `_id` or that a date is stored as a string. Treat an empty
  query as inconclusive until the same query shape returns a known positive control.
- Verify each layer separately: ingress/backfill records, canonical `audio_chunks`,
  `conversations`, timeline days/episodes, and the UI. Promotion between collections
  can leave an ingress collection empty even though canonical data exists.
- For timelines, use the stored `local_date` type and user timezone, follow explicit
  conversation/audio references, check time overlap, and distinguish active/open
  episodes from provisional or superseded runs.
- State exactly which claim the evidence supports: sent, stored, processed, linked, or
  merely visible in one UI route. Report UTC and user-local timestamps when time is
  relevant.

### Investigating a slow or unresponsive backend

When *everything* is slow at once — the dashboard hangs, health checks time out, a
WebSocket stops draining — suspect a blocked event loop before suspecting the
network or a dependency. FastAPI runs `async def` handlers on one loop, so a single
synchronous call (a blocking client, a large serialization) stops every other task
in that process while the container keeps reporting healthy.

Do not measure this by hand. Every Chronicle loop — the backend and each stream
worker — monitors its own scheduling delay:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/system/event-loop | python3 -m json.tool
```

A healthy loop's `lag_p50_ms` is a fraction of a millisecond. Sustained double
digits mean it is saturated. A stall past one second is recorded in **System
Events** under the `performance` category, carrying the stack that was executing —
sampled repeatedly during the stall, so the reported frame is evidence rather than a
guess. Repeat stalls from one cause collapse into a single incident.

Set `ASYNCIO_DEBUG=true` (with `ASYNCIO_SLOW_CALLBACK_SECONDS`) to have asyncio name
the exact offending callback instead of a stack. It is worth it while diagnosing and
costs real throughput, so turn it off afterwards. `py-spy dump --pid 1` inside the
container remains the deepest tool when the above is not enough. Implementation:
`services/observability/loop_monitor.py`.

### Investigating unexplained service restarts

Before treating an unexpected restart as a crash, check the admin **System Events**
page and filter to the `service` category. Start, stop, restart, and provider-switch
operations routed through `edge/service_manager.py` write a `running` entry followed by
a `done` or `failed` entry to the MongoDB `system_events` ledger. Each entry includes
the node, service, action, operation ID, timestamps, result, phase, and captured command
output. These records have the system-event ledger's rolling 30-day retention.

Use the operation ID to correlate a ledger entry with the node-agent logs. On a
systemd-managed node, inspect:

```bash
journalctl --user -u chronicle-service-manager
journalctl --user -u chronicle-stack
```

For an unmanaged node agent, inspect `edge/service-manager.log`. Normal CLI and WebUI
lifecycle commands report to the same ledger. Explicit `--direct` recovery, failed
ledger reporting, and raw engine operations can leave no entry; absence does not
prove an automatic restart.

## Service operations

For status, lifecycle, rebuilds, or unavailable-service reports, read
[the service-management workflow](docs/init-system.md#service-management) first.
Use `./services status --detailed`, then the CLI's `logs`, `inspect`, `doctor`, and
`deployments --status` commands as appropriate. Start/stop/restart requires named
groups or explicit `--all`; remote execution requires `--node` with a registered ID.
LLM, wakeword, and TTS are managed groups. Normal actions wait for readiness and
share WebUI operation tracking. Use explicit `--direct` only for local recovery
without the manager; admission still applies.

Use raw engine commands for evidence the CLI does not expose, isolated development,
unmanaged services, or a broken CLI. State the gap before going lower-level and
redact secrets. After repairs, exercise the affected capability as well as readiness.

## Initial Setup & Configuration

Chronicle includes an **interactive setup wizard** for easy configuration. The wizard guides you through:
- Service selection (backend + optional services)
- Authentication setup (admin account, JWT secrets)
- Transcription provider configuration (Deepgram or offline ASR)
- LLM provider setup (OpenAI or Ollama)
- Memory configuration (agentic Markdown vault — Chronicle's single memory provider)
- Network configuration and HTTPS setup
- Optional services (speaker recognition, Parakeet ASR)
- Immich photo library integration (photo discovery + person photos in the vault)

### Quick Start
```bash
# Run the interactive setup wizard from project root (recommended)
./wizard.sh

# Or use direct command:
uv run --with-requirements setup-requirements.txt python wizard.py

# For step-by-step instructions, see quickstart.md
```

**Operator entry points**: Use `./wizard.sh` for configuration and `./services` for service management. See [service operations](#service-operations).

**Temporary Tooling Rule**: When a Python tool or dependency is needed only for a one-off task, run it ephemerally with `uv run --with <package> ...`; do not install it into a project environment or add it to project dependencies. For a package-owned CLI, use `uvx --from <package> <entrypoint>`. This applies to browser automation, screenshots, data inspection, and other temporary utilities.

### Setup Documentation
For detailed setup instructions and troubleshooting, see:
- **[@quickstart.md](quickstart.md)**: Beginner-friendly step-by-step setup guide
- **[@docs/init-system.md](docs/init-system.md)**: Complete initialization system architecture and design

### Wizard Architecture
The initialization system uses a **root orchestrator pattern**:
- **`wizard.py`**: Root setup orchestrator for service selection and delegation
- **`backend/init.py`**: Backend configuration wizard
- **`extras/speaker-recognition/init.py`**: Speaker recognition setup
- **Service setup scripts**: Individual setup for ASR services

Key features:
- Interactive prompts with validation
- API key masking and secure credential handling
- Environment file generation with placeholders
- HTTPS configuration with SSL certificate generation
- Service status display and health checks
- Automatic backup of existing configurations

## Development Commands

### Backend Development (Chronicle Backend - Primary)
```bash
# From the repository root, start the managed backend
./services start backend

cd backend

# Standalone development alternative (use an isolated configuration)
uv run python src/main.py

# Code formatting and linting
uv run black src/
uv run isort src/

# Run tests
uv run pytest
uv run pytest tests/test_memory_service.py  # Single test file

# Run integration tests (local script mirrors CI)
./run-test.sh  # Complete integration test suite

# Environment setup
cp .env.template .env  # Configure environment variables

# Reset data (development)
sudo rm -rf backend/data/
```

### Running Tests

#### Quick Commands
All test operations are managed through a simple Makefile interface:

```bash
cd tests

# Full test workflow (recommended)
make test              # Start containers + run all tests (profile: mock, no credentials)

# Same suite against real backing services (see tests/profiles.yml)
make test PROFILE=deepgram-openai          # real Deepgram STT + real OpenAI LLM
make test PROFILE=deepgram-openai-speaker  # ...plus the real speaker service

# Or step by step
make start             # Start test containers (with health checks)
make all               # Run all test suites
make stop              # Stop containers (preserves volumes)

# Run specific test suites
make endpoints         # API endpoint tests
make integration       # End-to-end workflows
make infra             # Infrastructure resilience

# Quick iteration (reuse existing containers)
make test-quick        # Run tests without restarting containers
```

#### Container Management
All container operations automatically preserve logs before cleanup:

```bash
make start             # Start test containers
make stop              # Stop containers (keep volumes)
make restart           # Restart without rebuild
make rebuild           # Rebuild images + restart (for code changes)
make containers-clean  # SAVES LOGS → removes everything
make status            # Show container health
make logs SERVICE=<name>  # View specific service logs
```

**Log Preservation:** All cleanup operations save container logs to `tests/logs/YYYY-MM-DD_HH-MM-SS/`

#### Test Environment

Test services use isolated ports and database:
- **Ports:** Backend (8001), MongoDB (27018), Redis (6380)
- **Database:** `test_db` (separate from production)
- **Credentials:** `test-admin@example.com` / `test-admin-password-123`

**For complete test documentation, see `tests/README.md`**

### Mobile App Development
```bash
cd app

# Start Expo development server
npm start

# Platform-specific builds
npm run android
npm run ios
npm run web
```

### Additional Services

Start configured service groups from the repository root:

```bash
./services start asr-services
./services start speaker-recognition
./services start tts
```

Choose the ASR/TTS provider through setup or the WebUI System controls. For builds,
provider changes, and diagnostics, follow the
[service-management workflow](docs/init-system.md#service-management).
Speaker recognition integration tests remain in `extras/speaker-recognition/run-test.sh`.
The HAVPE relay has its own lifecycle; see `extras/havpe-relay/` for its setup.

## Architecture Overview

### Unknown speaker invariant

`Unknown Speaker N` is a conversation-local diarization placeholder, not a globally
stable speaker name. For example, `Unknown Speaker 1` in two conversations may describe
different people. Never use the literal placeholder as a cross-conversation identity,
filter option, enrollment name, or memory person. Corpus workflows must use a generic
unknown category or the compound identity `(conversation_id, local_speaker_label)` until
voice evidence and human review assign a real person.

### Key Components
- **Audio Pipeline**: Real-time Opus/PCM → Application-level processing → Deepgram transcription → memory extraction
- **Audio Protocol V2**: `/ws/audio` uses generated control messages and atomic raw-Opus media envelopes
- **Unified Pipeline**: Job-based tracking system for all audio processing (WebSocket and file uploads)
- **Job Tracker**: Tracks pipeline jobs with stage events (audio → transcription → memory) and completion status
- **Task Management**: BackgroundTaskManager tracks all async tasks to prevent orphaned processes
- **Unified Transcription**: Deepgram transcription with fallback to offline ASR services
- **Memory System**: Single agentic Markdown vault — a tool-calling memory agent records conversations and surgically edits Obsidian-style People/Topic/Category notes; a read-only retrieval agent drives ripgrep over the vault to answer queries
- **Authentication**: Email-based login with MongoDB ObjectId user system
- **Client Management**: Auto-generated client IDs as `{user_id_suffix}-{device_name}`, centralized ClientManager
- **Data Storage**: MongoDB (`audio_capture_sessions`, capture-owned `audio_chunks`, semantic conversations, processing artifacts, chat, annotations), disk media, and the Markdown vault (`data/conversation_docs/<user_id>/`) as the memory source of truth. An audio chunk's immutable `captured_at` is its wall-clock identity; Conversations reference chunks through absolute `AudioRangeRef` claims.
- **Web Interface**: React-based web dashboard with authentication and real-time monitoring

### Service Dependencies
```yaml
Required:
  - MongoDB: User data and conversations
  - Redis: Job queues (RQ workers) and session state
  - FastAPI Backend: Core audio processing
  - LLM Service: Memory agent (vault read/write) and action items (OpenAI or Ollama)

Recommended:
  - Transcription: Deepgram or offline ASR services

Optional:
  - Parakeet ASR: Offline transcription service
  - Speaker Recognition: Voice identification service
  - Caddy: HTTPS reverse proxy (auto-configured when HTTPS enabled)
```

## Data Flow Architecture

For any capture, streaming, playback, wake-word, transcription, or audio-import
change, read `docs/backend/audio-interface-map.md`. It is the boundary inventory and
migration ledger for the generated Chronicle audio contract.

1. **Audio Ingestion**: Devices stream raw Opus through generated audio-v2 envelopes with bearer authentication
2. **Typed Session Management**: Bound start/stop controls define capture sessions and reject stale sockets
3. **Application-Level Processing**: Global queues and processors handle all audio/transcription/memory tasks
4. **Speech-Driven Conversation Creation**: Continuous capture creates no user-facing row until speech is detected; deliberate recordings/uploads are visible immediately
5. **Audio Evidence and Conversations**: Opus audio documents are persisted in `audio_chunks`; `captured_at` remains stable when chunks are split, merged, or trimmed. A conversation is the current semantic/operational claim over those documents, not their temporal identity.
6. **Versioned Processing**: Transcript and memory versions tracked with active version pointers
7. **Memory Processing**: A tool-calling memory agent records each conversation and surgically edits People/Topic/Category notes in the Markdown vault
8. **Memory Storage**: Obsidian-style Markdown vault at `data/conversation_docs/<user_id>/` — the single source of truth, searched by a read-only retrieval agent via ripgrep
9. **Audio Optimization**: Long silent runs are removed from Conversation claims without moving, deleting, or re-encoding capture chunks
10. **Task Tracking**: BackgroundTaskManager ensures proper cleanup of all async operations

### Speech-Driven Architecture

**Core Principle**: User-visible Conversations represent deliberate recordings or detected speech-bearing intervals. Continuous capture persists without a provisional Conversation; raw audio time identity belongs to each `AudioChunkDocument.captured_at`.

**Storage Architecture**:
- **`audio_capture_sessions` Collection**: Stores technical ingest/recovery attempts; a reconnect may create another session without defining a semantic boundary.
- **`audio_chunks` Collection**: Stores ~10-second Opus evidence with capture source/session identity, immutable absolute UTC `captured_at`, and sequence. It has no Conversation owner or Conversation-relative coordinates.
- **`conversations` Collection**: Stores user-visible semantic claims in `audio_ranges`; a Conversation ID is lineage, not durable audio identity.
- **Processing artifact collections**: Store immutable transcript and diarization evidence over the same range claims; Conversation transcript revisions are derived projections.
- **Timeline episode audio**: `TimelineEpisode.audio_ranges` is the authoritative semantic audio claim: stable chunk IDs plus absolute UTC bounds. `related_conversation_ids` remains evidence/lineage context only.
- **Speech Detection**: Analyzes transcript content, duration, and meaningfulness before conversation creation
- **Automatic Filtering**: No user-facing conversations for silence, noise, or brief audio without speech

### Silence trimming and retention

After transcription, VAD-driven silence trimming keeps configurable context around
speech and only cuts long silent runs (defaults: 5 seconds of padding, a 120-second
minimum silent run, and at least 60 seconds saved). Trimming edits the Conversation's
range claims and writes a derived transcript revision. Capture chunks remain unchanged.

Conversation soft deletion and raw-audio retention are separate. The ordinary cleanup
job may purge old semantic Conversations and their waveform cache, but preserves capture
chunks. Raw deletion is disabled until an explicit capture-retention policy can prove
that every Conversation, Timeline episode, artifact, and annotation claim is handled.

**Benefits**:
- Clean user experience with only meaningful conversations displayed
- Reduced noise in conversation lists and memory processing
- Efficient storage utilization for speech-only content
- Automatic quality filtering without manual intervention

## Authentication & Security

- **User System**: Email-based authentication with MongoDB ObjectId user IDs
- **Client Registration**: Automatic `{objectid_suffix}-{device_name}` format
- **Data Isolation**: All data scoped by user_id with efficient permission checking
- **API Security**: JWT tokens required for all endpoints and WebSocket connections
- **Admin Bootstrap**: Automatic admin account creation with ADMIN_EMAIL/ADMIN_PASSWORD

## Configuration

### Required Environment Variables
```bash
# Authentication
AUTH_SECRET_KEY=your-super-secret-jwt-key-here
ADMIN_PASSWORD=your-secure-admin-password
ADMIN_EMAIL=admin@example.com

# LLM Configuration
LLM_PROVIDER=openai  # or ollama
OPENAI_API_KEY=your-openai-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini

# Speech-to-Text
DEEPGRAM_API_KEY=your-deepgram-key-here
# Optional: PARAKEET_ASR_URL=http://host.docker.internal:8767
# Optional: TRANSCRIPTION_PROVIDER=deepgram

# Memory Provider
MEMORY_PROVIDER=chronicle  # agentic Markdown vault (only valid value)

# Database
MONGODB_URI=mongodb://mongo:27017
# Database name: chronicle

# Network Configuration
HOST_IP=localhost
BACKEND_PUBLIC_PORT=8000
WEBUI_PORT=5173  # Vite dev server (the only webui; fronted by Caddy for HTTPS)
CORS_ORIGINS=http://localhost:5173,http://localhost:8000
```

### Memory Provider Configuration

Chronicle has a single memory provider, `chronicle`: an **agentic Markdown vault**. The vault (Obsidian-style notes at `data/conversation_docs/<user_id>/`) is the single source of truth. A tool-calling memory agent records each conversation and surgically edits People/Topic/Category notes; a read-only retrieval agent drives ripgrep over the vault to synthesize answers. There is no provider choice to configure — only an LLM for the agents to use.

```bash
# Memory provider (only valid value)
MEMORY_PROVIDER=chronicle

# LLM Configuration for the memory agent
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key-here
OPENAI_MODEL=gpt-4o-mini
```

### Transcription Provider Configuration

Chronicle supports multiple transcription services:

```bash
# Option 1: Deepgram (High quality, recommended)
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-key-here

# Option 2: Local ASR (Parakeet)
PARAKEET_ASR_URL=http://host.docker.internal:8767
```

### Additional Service Configuration
```bash
# LLM Processing
OLLAMA_BASE_URL=http://ollama:11434

# Speaker Recognition
SPEAKER_SERVICE_URL=http://speaker-recognition:8085
```

### Plugin Security Architecture

**Three-File Separation**:

1. **backend/.env** - Secrets (gitignored)
   ```bash
   SMTP_PASSWORD=abcdefghijklmnop
   OPENAI_API_KEY=sk-proj-...
   ```

2. **config/plugins.yml** - Orchestration (uses env var references)
   ```yaml
   plugins:
     email_summarizer:
       enabled: true
       smtp_password: ${SMTP_PASSWORD}  # Reference, not actual value!
   ```

3. **plugins/{plugin_id}/config.yml** - Non-secret defaults
   ```yaml
   subject_prefix: "Conversation Summary"
   ```

**CRITICAL**: Never hardcode secrets in `config/plugins.yml`. Always use `${ENV_VAR}` syntax.

## Quick API Reference

### Common Endpoints
- **GET /health**: Basic application health check
- **GET /readiness**: Service dependency validation
- **WS /ws/audio**: Audio-v2 WebSocket (generated control JSON plus atomic raw-Opus media)
- **GET /api/conversations**: User's conversations with transcripts
- **GET /api/memories/search**: Agentic vault search (retrieval agent over the Markdown vault)
- **POST /auth/jwt/login**: Email-based login (returns JWT token)

### Authentication Flow
```bash
# 1. Get auth token
curl -s -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=your-password-here" \
  http://localhost:8000/auth/jwt/login

# 2. Use token in API calls
curl -s -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8000/api/conversations
```

### Backend API Interaction Rules
- **Get token first**: Always authenticate in a separate Bash call, store the token, then use it in subsequent calls. Never chain login + API call in one command.
- **Read .env with Read tool**: Use the Read tool to get values from `.env` files. Don't use `grep | sed | cut` in Bash to extract env values.
- **Keep Bash simple**: Each Bash call should do one thing. Don't string together complex piped commands for backend queries.

### Development Reset Commands
```bash
# Reset all data (development only)
cd backend
sudo rm -rf data/

# Reset Docker volumes
docker compose down -v
docker compose up --build -d
```

## Add Existing Data

### Audio File Upload & Processing

The system supports processing existing audio files through the file upload API. This allows you to import and process pre-recorded conversations without requiring a live WebSocket connection.

**Upload and Process WAV Files:**
```bash
export USER_TOKEN="your-jwt-token"

# Upload single WAV file
curl -X POST "http://localhost:8000/api/audio/upload" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -F "files=@/path/to/audio.wav" \
  -F "device_name=file_upload"

# Upload multiple WAV files
curl -X POST "http://localhost:8000/api/audio/upload" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -F "files=@/path/to/recording1.wav" \
  -F "files=@/path/to/recording2.wav" \
  -F "device_name=import_batch"
```

**Response Example:**
```json
{
  "message": "Successfully processed 2 audio files",
  "processed_files": [
    {
      "filename": "recording1.wav",
      "sample_rate": 16000,
      "channels": 1,
      "duration_seconds": 120.5,
      "size_bytes": 3856000
    },
    {
      "filename": "recording2.wav",
      "sample_rate": 44100,
      "channels": 2,
      "duration_seconds": 85.2,
      "size_bytes": 7532800
    }
  ],
  "client_id": "user01-import_batch"
}
```

## HAVPE Relay Configuration

For ESP32 audio streaming using the HAVPE relay (`extras/havpe-relay/`):

```bash
# Environment variables for HAVPE relay
export AUTH_USERNAME="user@example.com"       # Email address
export AUTH_PASSWORD="your-password"
export DEVICE_NAME="havpe"                    # Device identifier

# Run the relay
cd extras/havpe-relay
uv run python main.py --backend-url http://your-server:8000 --backend-ws-url ws://your-server:8000
```

The relay will automatically:
- Authenticate using `AUTH_USERNAME` (email address)
- Generate client ID as `objectid_suffix-havpe`
- Forward ESP32 audio to the backend with proper authentication
- Handle token refresh and reconnection

## TTS Services

Provider-based text-to-speech (`extras/tts/`), built on the same provider pattern as `extras/asr-services/`. Run **one provider at a time**, all serving on port `8770` (configurable via `TTS_PORT`).

### Providers

| Provider | Service | Hardware | Highlights |
|----------|---------|----------|-----------|
| **TADA** (HumeAI) | `tada-tts` | GPU | Zero-shot voice cloning, 1:1 token alignment (no hallucinations), MIT. `tada-1b` (English) / `tada-3b-ml` (9 langs). Needs `HF_TOKEN` (Llama 3.2 base is gated). |
| **Fish Speech** (Fish Audio) | `fish-tts` | GPU | Dual-AR, 50+ langs, inline emotion/prosody tags (`[laugh]`, `[whispers]`), streaming. `s2-pro` (default) / `openaudio-s1-mini` / `fish-speech-1.5`. Optional `torch.compile`. |
| **KittenTTS** (KittenML) | `kittentts-tts` | CPU | Ultra-light (~25MB) ONNX, no GPU/API key, preset voices, English only. Uses dedicated `KITTEN_TTS_*` env vars. |
| **Kokoro** (hexgrad) | `kokoro-tts` | GPU/CPU | Lightweight (~82M, **<~1GB VRAM**) StyleTTS2, preset voices, 8 langs, Apache-2.0. Quality-per-VRAM sweet spot. Uses dedicated `KOKORO_TTS_*` env vars. |

### Setup & Run

```bash
# Configure (selects provider, model, CUDA version) — from the repository root
uv run --with-requirements setup-requirements.txt python extras/tts/init.py

# Start the configured provider through the managed TTS group
./services start tts

# Test
curl http://localhost:8770/health
curl -X POST http://localhost:8770/synthesize -F "text=Hello world." -o output.wav
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health (`healthy` / `initializing`) |
| `/info` | GET | Model id, provider, capabilities, supported languages |
| `/synthesize` | POST | Generate speech (multipart form) |

**POST /synthesize** — `text` (required); optional `reference_audio` (WAV) + `reference_text` for voice cloning; optional generation params (`temperature`, `top_p`, `repetition_penalty`, `seed`, `max_new_tokens`). Returns WAV bytes with `X-Sample-Rate`, `X-Provider`, `X-Model` headers.

**Notes:**
- Registered as `tts` in `services.py`; select the provider via setup or the WebUI System controls. See [service management](docs/init-system.md#service-management).
- GPU providers require CUDA 12.6+ (`PYTORCH_CUDA_VERSION=cu126`/`cu128`); `cu121` is unsupported (torch>=2.7).
- Add a provider by creating `extras/tts/providers/{name}/` with `service.py`, `synthesizer.py`, and `Dockerfile` (subclass `BaseTTSService`).
- An optional `edge-agent` sidecar (`--profile edge`) advertises the service on the Tailnet.

## Distributed Deployment

### Single Machine vs Distributed Setup

**Single Machine (Default):**
```bash
# Start configured services assigned to this node
./services start --all
```

**Distributed Setup (GPU + Backend separation):**

#### GPU Machine Setup
```bash
# From the repository root on the GPU node, start configured service groups
./services start asr-services speaker-recognition

# Optional external Ollama, outside Chronicle's managed service registry
docker run -d --gpus=all -p 11434:11434 \
  -v ollama:/root/.ollama \
  ollama/ollama:latest
```

#### Backend Machine Configuration
```bash
# .env configuration for distributed services
OLLAMA_BASE_URL=http://[gpu-machine-tailscale-ip]:11434
SPEAKER_SERVICE_URL=http://[gpu-machine-tailscale-ip]:8085
PARAKEET_ASR_URL=http://[gpu-machine-tailscale-ip]:8080

# From the repository root, start the managed backend
./services start backend
```

#### Tailscale Networking
```bash
# Install on each machine
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Find machine IPs
tailscale ip -4
```

**Benefits of Distributed Setup:**
- GPU services on dedicated hardware
- Lightweight backend on VPS/Raspberry Pi
- Automatic Tailscale IP support (100.x.x.x) - no CORS configuration needed
- Encrypted inter-service communication

**Service Examples:**
- GPU machine: LLM inference, ASR, speaker recognition
- Backend machine: FastAPI, WebUI, databases
- Database machine: MongoDB (optional separation)

## Development Notes

### Package Management
- **Backend**: Uses `uv` for Python dependency management (faster than pip)
- **Mobile**: Uses `npm` with React Native and Expo
- **Containers**: The service lifecycle (`services.py`/`status.py`/service-manager) supports **both Docker and Podman**, selected via `container_engine: docker|podman` in `config/config.yml` (or the `CONTAINER_ENGINE`/`COMPOSE_CMD` env vars). The repo's compose files run unmodified under either engine. For Podman it drives `podman-compose` and needs CDI for GPU. See **[@docs/podman.md](docs/podman.md)** for rootless/GPU setup and migration notes.

### Testing Strategy
- **Makefile-Based**: All test operations through simple `make` commands (`make test`, `make start`, `make stop`)
- **One suite, N service profiles**: tests are never selected by whether an API key is present. `tests/profiles.yml` declares which backing services are real for a run; stubs replay recorded real responses from `tests/cassettes/`, so the same assertions hold with or without credentials. Do not add a tag or a skip to work around a missing key — record a cassette (`make record-cassettes`) or fix the stub.
- **Exercise Production Entry Points**: When adding or changing a cron job, worker job, background task, event handler, or lifecycle hook, add a test that invokes the real registered entry-point function with its external dependencies faked. Testing only extracted helpers is insufficient: the entry-point test must execute dependency lookup and orchestration far enough to catch stale APIs, broken wiring, and initialization errors.
- **Background Failures Need a Direct Signal**: A healthy process does not prove its background jobs are healthy. For changes to scheduled or asynchronous work, run the entry point directly in tests and verify the relevant job status, metrics, or fresh logs after deployment/restart.
- **Log Preservation**: Container logs always saved before cleanup (never lose debugging info)
- **End-to-End Integration**: Robot Framework validates complete audio processing pipeline
- **Environment Flexibility**: Tests work with both local .env files and CI environment variables
- **CI/CD Integration**: Same test logic locally and in GitHub Actions

### Code Style
- **Python**: Black formatter with 100-character line length, isort for imports
- **TypeScript**: Standard React Native conventions
- **Import Guidelines**:
  - NEVER import modules in the middle of functions or files
  - ALL imports must be at the top of the file after the docstring
  - Use lazy imports sparingly and only when absolutely necessary for circular import issues
  - Group imports: standard library, third-party, local imports
  - **Enforced** by `scripts/check_import_placement.py`, which runs as a pre-commit
    and pre-push hook and in the `Code Style` CI workflow. An import nested inside a
    function or class fails the check unless a comment on the same line, or directly
    above it, explains why (≥4 words; `# noqa`-style directives don't count). One
    comment covers a contiguous run of imports:
    ```python
    def build_router():
        # Imported here to break the circular import with plugins.router.
        from backend.plugins.router import PluginRouter
    ```
    An import guarded by `try/except ImportError` needs no comment — the structure
    already says "optional dependency". The repository is currently clean, so any
    failure is something the change introduced.
- **Error Handling Guidelines**:
  - **Always raise errors, never silently ignore**: Use explicit error handling with proper exceptions rather than silent failures
  - **Understand data structures**: Research and understand input/response or class structure instead of adding defensive `hasattr()` checks

### Docker Build Cache Management
- **Default Behavior**: Docker automatically detects file changes in Dockerfile COPY/ADD instructions and invalidates cache as needed
- **No --no-cache by Default**: Only use `--no-cache` when explicitly needed (e.g., package updates, dependency issues)
- **Smart Caching**: Docker checks file modification times and content hashes to determine when rebuilds are necessary
- **Development Efficiency**: Trust Docker's cache system - it handles most development scenarios correctly

### Health Monitoring
The system includes comprehensive health checks:
- `/readiness`: Service dependency validation
- `/health`: Basic application status
- Memory debug system for transcript processing monitoring

### Integration Test Infrastructure
- **Makefile Interface**: Simple `make` commands for all operations (see `tests/README.md`)
- **Test Environment**: `docker-compose-test.yml` with isolated services on separate ports
- **Test Database**: Uses `test_db` database (separate from production)
- **Log Preservation**: All cleanup operations save logs to `tests/logs/` automatically
- **CI Compatibility**: Same test logic runs locally and in GitHub Actions

## Extended Documentation

For detailed technical documentation, see:
- **[@docs/README.md](docs/README.md)**: Documentation index
- **[@docs/overview.md](docs/overview.md)**: Architecture overview and technical deep dive
- **[@docs/init-system.md](docs/init-system.md)**: Initialization system and service management
- **[@docs/ssl-certificates.md](docs/ssl-certificates.md)**: HTTPS/SSL setup details
- **[@docs/podman.md](docs/podman.md)**: Running with Podman instead of Docker (engine selection, rootless/GPU setup)
- **[@docs/screenpipe.md](docs/screenpipe.md)**: ScreenPipe capture-node architecture, services, desktop controls, and troubleshooting
- **[@docs/audio-pipeline-architecture.md](docs/audio-pipeline-architecture.md)**: Audio pipeline design
- **[@docs/backend/compose-stack.md](docs/backend/compose-stack.md)**: Backend compose services, shared mounts, and profiles
- **[@docs/backend/auth.md](docs/backend/auth.md)**: Authentication architecture
- **[@docs/backend/memories.md](docs/backend/memories.md)**: Memory system documentation
- **[@docs/backend/plugin-development-guide.md](docs/backend/plugin-development-guide.md)**: Plugin development guide

### ScreenPipe Capture Nodes

Before changing ScreenPipe ingestion, the desktop tray, or capture-node services, read
[@docs/screenpipe.md](docs/screenpipe.md). ScreenPipe owns the high-volume local capture
store; Chronicle's companion sends compact activity metadata and serves bounded
snapshot/OCR requests. The desktop entry point is shared across macOS and Linux, with
platform UI adapters over common state, logging, and vault-sync code. The ScreenPipe UI
is an optional on-demand viewer and must not be required for background capture.

## Robot Framework Testing

**IMPORTANT: When writing or modifying Robot Framework tests, you MUST follow the testing guidelines.**

Before writing any Robot Framework test:
1. **Read [@tests/TESTING_GUIDELINES.md](tests/TESTING_GUIDELINES.md)** for comprehensive testing patterns and standards
2. **Check [@tests/tags.md](tests/tags.md)** for approved tags - only the 11 business tags and 4 execution tags are permitted
3. **SCAN existing resource files** for keywords - NEVER write code that duplicates existing keywords
4. **Follow the Arrange-Act-Assert pattern** with inline verifications (not abstracted to keywords)

Key Testing Rules:
- **Check Existing Keywords FIRST**: Before writing ANY test code, scan relevant resource files (`websocket_keywords.robot`, `queue_keywords.robot`, `conversation_keywords.robot`, etc.) for existing keywords
- **Tags**: ONLY use the 15 approved tags from tags.md, tab-separated (e.g., `[Tags]    infra	audio-streaming`)
- **Verifications**: Write assertions directly in tests, not in resource keywords
- **Keywords**: Only create keywords for reusable setup/action operations AFTER confirming no existing keyword exists
- **Resources**: Always check existing resource files before creating new keywords or duplicating logic
- **Naming**: Use descriptive names that explain business purpose, not technical implementation

**DO NOT:**
- Write inline code without checking if a keyword already exists for that operation
- Create custom tags (use only the 15 approved tags)
- Abstract verifications into keywords (keep them inline in tests)
- Use space-separated tags (must be tab-separated)
- Skip reading the guidelines before writing tests

## Notes for Coding Agents
For frontend UI changes and reviews, read
**[@docs/agents/frontend-ux-review.md](docs/agents/frontend-ux-review.md)**. Apply the
installed `frontend-design-principles` skill for hierarchy and visual judgment, and
the repository `screenshots` skill for rendered-page verification.

Check if the src/ is volume mounted. If not, do compose build so that code changes are reflected. Do not simply run `docker compose restart` as it will not rebuild the image.
Check `docs/backend/` for up-to-date information on the Chronicle backend.
All docker projects have .dockerignore following the exclude pattern. That means files need to be included for them to be visible to docker.
The uv package manager is used for all python projects. Wherever you'd call `python3 main.py` you'd call `uv run python main.py`
For temporary Python-backed tooling that is not part of the repo dependencies, prefer `uv run --with <package> python ...` instead of installing packages into the project. Use `uvx --from <package> <entrypoint>` when invoking a package's own CLI. For browser scripts/screenshots, use `uv run --with playwright python - <<'PY'` and import `playwright.sync_api`; for the Playwright CLI use `uvx --from playwright playwright ...`. Do not add transient Playwright/npm packages to `package.json` just to drive a one-off check.

**Compute-Intensive Workloads:**
- Chronicle is designed for heavy data and AI processing. Re-encoding or recomputation is acceptable when it improves correctness or output quality.
- Avoid wasteful repeated work: cache reusable artifacts, fingerprint model and configuration inputs, and reuse valid intermediate results.
- Prefer GPU acceleration whenever the workload and deployed service support it.

**Container Engine (Docker or Podman):**
- The project supports **both Docker and Podman**. The active engine is set by `container_engine` in `config/config.yml` (default `docker`); prefer the lifecycle scripts (`./services start --all`/`./services stop --all`/`./services restart --all`) which route through the selected engine. For the lower-level cases described in [service operations](#service-operations), use `podman-compose` under Podman. See **[@docs/podman.md](docs/podman.md)**.

**Docker Build Guidelines:**
- Use `docker compose build` (or `podman-compose build`) without `--no-cache` by default for faster builds
- Only use `--no-cache` when explicitly needed (e.g., if cached layers are causing issues or when troubleshooting build problems)
- The build cache is efficient and saves significant time during development

- Remember that whenever there's a python command, you should use uv run python3 instead
- **Run setup commands from the repository root.** `setup-requirements.txt` lists
  the shared `chronicle-setup` package as a relative path and uv resolves that
  against the working directory, so
  `uv run --with-requirements setup-requirements.txt python extras/<svc>/init.py`
  works while the old `cd extras/<svc> && ... ../../setup-requirements.txt` form
  now fails. Each `init.py` anchors its own paths to `__file__`, so this does not
  change which `.env` a service reads or writes.
