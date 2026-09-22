# Chronicle

Self-hostable AI system that captures audio/video data from OMI devices and other sources to generate memories, action items, and contextual insights about your conversations and daily interactions.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/main/install.sh | sh
```

This clones the latest release, installs dependencies, and launches the interactive setup wizard.

For step-by-step instructions, see the [setup guide](quickstart.md).

## Screenshots

### WebUI Dashboard

![WebUI Dashboard](.assets/advanced-dashboard-webui.png)

### Memory Search

![Memory Search](.assets/memory-dashboard.png)

### Desktop Menu Bar Client

![Menu Bar Client](.assets/menu-bar-client.png)

## What's Included

- **Mobile app** for OMI devices via Bluetooth
- **Backend services** (simple → advanced with full AI features)
- **Web dashboard** for conversation and memory management
- **Optional services**: Speaker recognition, offline ASR, wake word, TTS, screen capture

## Links

- **📚 [Setup Guide](quickstart.md)** - Start here
- **📖 [Documentation index](docs/README.md)** - All technical docs
- **🏗️ [Project Overview](docs/overview.md)** - Architecture and vision
- **🐳 [Docker/K8s](README-K8S.md)** - Container deployment
- **🔧 [AGENTS.md](AGENTS.md)** - Development conventions

## Project Structure

```
chronicle/
├── app/                     # React Native mobile app
│   ├── app/                # App components and screens
│   └── plugins/            # Expo plugins
├── backends/
│   ├── advanced/           # Main AI backend (FastAPI)
│   │   ├── src/           # Backend source code
│   │   ├── init.py        # Interactive setup wizard
│   │   └── docker-compose.yml
│   ├── simple/            # Basic backend implementation
│   └── other-backends/    # Example implementations
├── extras/                  # Optional services (see extras/ for the full set)
│   ├── speaker-recognition/  # Voice identification service
│   ├── asr-services/        # Offline speech-to-text (Parakeet)
│   ├── wakeword-service/    # Wake-word detection
│   ├── intent-router/       # Voice command routing
│   ├── tts/                 # Text-to-speech
│   └── vault-sync/          # Markdown vault sync
├── plugins/                # Action plugins (e.g. Home Assistant)
├── docs/                   # Technical documentation
├── config/                 # Central configuration files
├── tests/                  # Integration & unit tests
├── wizard.py              # Root setup orchestrator
├── services               # Operator CLI
├── services.py            # Shared lifecycle implementation
└── *.sh                   # Convenience scripts (wrappers)
```

## Service Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Chronicle System                      │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  ┌──────────────┐    ┌──────────────┐   ┌────────────┐ │
│  │ Mobile App   │◄──►│   Backend    │◄─►│  MongoDB   │ │
│  │ (React       │    │   (FastAPI)  │   │            │ │
│  │  Native)     │    │              │   └────────────┘ │
│  └──────────────┘    └──────────────┘                   │
│                            │                             │
│                            ▼                             │
│       ┌────────────────────┴────────────────┐          │
│       │                                     │          │
│  ┌────▼─────┐  ┌───────────┐  ┌──────────▼──┐        │
│  │ Deepgram │  │  OpenAI   │  │  Markdown   │        │
│  │   STT    │  │   LLM     │  │   Vault     │        │
│  │          │  │           │  │  (memory)   │        │
│  └──────────┘  └───────────┘  └─────────────┘        │
│                                                         │
│  Optional Services:                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │  Speaker     │  │  Parakeet    │  │  Ollama     │ │
│  │  Recognition │  │  (Local ASR) │  │  (Local     │ │
│  │              │  │              │  │   LLM)      │ │
│  └──────────────┘  └──────────────┘  └─────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Quick Command Reference

### Setup & Configuration
```bash
# Interactive setup wizard (recommended for first-time users)
./wizard.sh

# Full command (what the script wraps)
uv run --with-requirements setup-requirements.txt python wizard.py
```

Use `./wizard.sh` for configuration and `./services` for operations.

### Service Management

```bash
./services status --detailed
./services start llm-services wakeword-service
./services start --all
./services restart backend
./services stop --all
./services logs wakeword-service --tail 100
```

Start/restart waits for readiness. See the [operator workflow](docs/init-system.md#service-management)
for remote nodes, image builds, operation history, and explicit local recovery.

### Development
```bash
# Backend development
cd backend
uv run python src/backend/main.py

# Integration tests (needs a .env with API keys)
./backend/run-test.sh

# Mobile app
cd app
npm start
```

See [docs/testing.md](docs/testing.md) for the unit-test lanes and coverage.

### Health Checks
```bash
# Backend health
curl http://localhost:8000/health

# Web dashboard
open http://localhost:5173
```

## Vision

Have various sensors feed the state of *your* world to computers and AI, and get
something useful out of it. Wearable audio devices capture personal spoken context
best, so Chronicle starts there: multiple OMI-like devices feeding audio, and from it
memories, action items, and home automation. Devices with a camera — or smart
glasses — extend the same idea to visual context.

## Roadmap

- **Visual context capture** — screen capture ships today; wearable camera and smart
  glasses are not yet supported
- **Multi-device coordination** — single-device capture works; coordinating overlapping
  captures across devices is not implemented
