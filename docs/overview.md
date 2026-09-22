# Chronicle Overview

Chronicle is an open-source, self-hosted system for building a personal timeline of your life. It captures events — conversations, audio, images, and more — processes them with AI, and extracts memories and facts that accumulate over time into a personal knowledge base.

The goal is a personal AI that gets better the more you use it: the more context it has about you, the more useful it becomes.

## Core Ideas

- **Timeline of events**: Your life is a sequence of things that happen — someone talks, music plays, a photo is taken. Chronicle models these as timestamped events on a timeline.
- **Multimodal**: Audio is the primary input today, but the architecture supports images, visual context, and other data sources.
- **Memories from everything**: Events produce memories. A conversation yields facts about people, plans, and preferences. A photo yields location, context, and associations.
- **Self-hosted**: Runs on your hardware, your data stays with you.
- **Hackable**: Designed to be forked, modified, and extended. Pluggable providers for transcription, LLM, and analysis; memories live in an agentic Markdown vault.

## How It Works

```
Audio/Images/Data  →  Ingestion  →  Processing  →  Memories
                                                      ↓
                                               Markdown Vault
                                                      ↓
                                              Retrieval & Search
```

### Audio Pipeline (Primary)

1. **Capture**: OMI devices, microphones, or uploaded files stream audio
2. **Transcription**: Deepgram (cloud) or Parakeet (local) converts speech to text
3. **Speaker Recognition**: Optional identification of who said what (pyannote)
4. **Memory Extraction**: LLM extracts facts, preferences, and context from transcripts
5. **Storage**: Memories recorded as notes in an agentic Markdown vault (Obsidian-style, at `data/conversation_docs/<user_id>/`)

### Image Pipeline (In Development)

1. **Import**: Zip upload, or sync from external services (e.g., Immich)
2. **Analysis**: Extract EXIF metadata, captions, detected objects
3. **Memory Extraction**: Same LLM pipeline, different source type
4. **Storage**: Same Markdown vault, queryable alongside conversation memories

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Chronicle System                    │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ┌──────────────┐    ┌──────────────┐  ┌──────────┐ │
│  │ Mobile App   │◄──►│   Backend    │◄►│ MongoDB  │ │
│  │ (React       │    │   (FastAPI)  │  │          │ │
│  │  Native)     │    │              │  └──────────┘ │
│  └──────────────┘    └──────┬───────┘               │
│                             │                        │
│  ┌──────────────┐    ┌──────▼───────┐  ┌──────────┐ │
│  │ Web UI       │    │   Workers    │  │ Markdown │ │
│  │ (React)      │    │  (RQ/Redis)  │  │  Vault   │ │
│  │              │    │              │  │ (memory) │ │
│  └──────────────┘    └──────────────┘  └──────────┘ │
│                                                       │
│  Transcription:  Deepgram (cloud) or Parakeet (local) │
│  LLM:           OpenAI (cloud) or Ollama (local)      │
│  Optional:      Speaker Recognition                   │
└─────────────────────────────────────────────────────┘
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **Backend** | `backend/` | FastAPI server, audio processing, API |
| **Web UI** | `backend/webui/` | React dashboard for conversations and memories |
| **Mobile App** | `app/` | React Native app for OMI device pairing |
| **Speaker Recognition** | `extras/speaker-recognition/` | Voice identification service |
| **ASR Services** | `extras/asr-services/` | Local speech-to-text (Parakeet) |
| **TTS Services** | `extras/tts/` | Text-to-speech (TADA, Fish Speech, KittenTTS) |
| **HAVPE Relay** | `extras/havpe-relay/` | ESP32 audio bridge |
| **Desktop tray** | `extras/chronicle-tray/` | Cross-platform tray/menu-bar app (Linux + macOS): vault sync, ScreenPipe controls, pendant |

### Pluggable Providers

Chronicle is designed around swappable providers:

- **Transcription**: Deepgram API or local Parakeet ASR
- **LLM**: OpenAI or local Ollama
- **Memory Storage**: agentic Markdown vault (the single source of truth)
- **Speaker Recognition**: pyannote-based service (optional)
- **Text-to-Speech**: TADA, Fish Speech (GPU), or KittenTTS (CPU) — optional

## Repository Structure

```
chronicle/
├── app/                     # React Native mobile app
├── backends/
│   ├── advanced/            # Main backend (FastAPI + WebUI)
│   ├── simple/              # Minimal backend for learning
│   └── other-backends/      # Example/alternative implementations
├── extras/
│   ├── speaker-recognition/ # Voice identification
│   ├── asr-services/        # Local ASR (Parakeet)
│   ├── tts/                 # Text-to-speech (TADA, Fish Speech, KittenTTS)
│   ├── havpe-relay/         # ESP32 audio bridge
│   ├── chronicle-tray/      # cross-platform desktop tray (Linux + macOS)
│   └── vault-sync/          # vault ⇄ Obsidian sync core used by the tray
├── config/                  # Central configuration
├── docs/                    # Documentation
├── tests/                   # Integration tests (Robot Framework)
├── wizard.py                # Setup wizard
└── services.py              # Service lifecycle manager
```

## Getting Started

See [quickstart.md](../quickstart.md) for setup instructions.

```bash
# Setup
./wizard.sh

# Start
./services start --all

# Access
open http://localhost:5173
```

## Further Reading

- [Quick Start Guide](../quickstart.md) — Step-by-step setup
- [Initialization System](init-system.md) — Setup wizard internals and port configuration
- [Audio Pipeline Architecture](audio-pipeline-architecture.md) — Deep technical reference
- [SSL Certificates](ssl-certificates.md) — HTTPS setup
- [Authentication Architecture](backend/auth.md) — Backend authentication internals
- [Memory System](backend/memories.md) — Agentic Markdown vault architecture
