# Speaker Recognition System

A comprehensive speaker recognition system with web-based UI for audio annotation, speaker enrollment, and data management.

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose
- Hugging Face account (for model access)
- 8GB+ RAM, 10GB+ disk space

### 1. Configure environment variables
```bash
cp .env.template .env
# Edit .env and add your Hugging Face token
```
Get your HF token from https://huggingface.co/settings/tokens
Accept the terms and conditions for
https://huggingface.co/pyannote/speaker-diarization-community-1


### 2. Choose CPU or GPU setup
```bash
# For CPU-only (lighter, works on any machine)
uv sync --group cpu

# For GPU acceleration (requires NVIDIA GPU + CUDA)
uv sync --group gpu
```

If you choose GPU, uncomment the deploy section with GPU requirements from docker-compose.yml

### 3. Run Setup Script

```bash
cd extras/speaker-recognition
./init.sh
```

This interactive setup will guide you through:
- Configuring your Hugging Face token
- Choosing compute mode (CPU/GPU)
- Setting up HTTPS for remote access (optional)

For non-interactive setup:
```bash
./init.sh --hf-token YOUR_TOKEN --compute-mode cpu
# Or for HTTPS with specific IP:
./init.sh --hf-token YOUR_TOKEN --compute-mode gpu --enable-https --server-ip YOUR_SERVER_IP
```

### 4. SSL Certificates (automatic)

HTTPS is fronted by **Caddy**, which obtains and auto-renews the certificate itself — no
manual generation step. `./wizard.sh` configures it based on your address:

- **`*.ts.net` (Tailscale)** → Caddy fetches the cert from the local `tailscaled` (the
  wizard mounts the socket). Trusted across your tailnet, auto-renewed.
- **Real domain** → Let's Encrypt, auto-renewed.
- **IP / `localhost`** → Caddy's internal-CA self-signed cert (accept the browser warning).

See [../../docs/ssl-certificates.md](../../docs/ssl-certificates.md) for the full model
(including the Docker-Desktop `static` fallback).

### 5. Start the system
```bash
# For CPU-only
docker compose --profile cpu up --build -d

# For GPU acceleration
docker compose --profile gpu up --build -d
```

This starts three services:
- **FastAPI backend** on port 8085 (internal API service)
- **React web UI** on port configured by REACT_UI_PORT (defaults vary by mode)
- **Nginx proxy** on ports 8444 (HTTPS) and 8081 (HTTP redirect)

**⚠️ Important**: Use the same profile when stopping:
```bash
# Stop CPU services
docker compose --profile cpu down

# Stop GPU services
docker compose --profile gpu down
```

### 6. Access the Web UI

**HTTPS Mode (Recommended for microphone access):**
- **Secure Access**: https://localhost:8444/ or https://your-ip:8444/
- **HTTP Redirect**: http://localhost:8081/ → https://localhost:8444/

**HTTP Mode (Fallback, microphone limited to localhost):**
- **Direct Access**: http://localhost:5174/ (or configured REACT_UI_PORT)
- **API Access**: http://localhost:8085/

**Microphone access requires HTTPS for network connections (not just localhost).**

### 7. Get Started
1. **Create a user** using the sidebar
2. **Upload audio** in the "Audio Viewer" page
3. **Annotate segments** in the "Annotation" page
4. **Enroll speakers** in the "Enrollment" page
5. **Manage & export** data in the "Speakers" page

## 🎯 What You Can Do

- **📁 Upload & Visualize**: Interactive waveforms, spectrograms, segment selection
- **📝 Annotate Audio**: Label speaker segments, handle unknown speakers
- **👤 Enroll Speakers**: Register speakers with quality assessment
- **👥 Manage Speakers**: View statistics, compare quality, bulk operations
- **📤 Export Data**: Download audio in organized folders or concatenated files
- **📊 Analytics**: Track quality trends and system usage

## 🛠️ System Architecture

- **Multi-user Support**: Each user manages their own speaker data
- **SQLite Database**: Local storage for annotations, speakers, and sessions
- **Quality Assessment**: Automatic audio quality scoring with recommendations
- **CPU/GPU Support**: Flexible deployment with dedicated dependency groups
- **Export Formats**:
  - Concatenated audio (max 10min per file)
  - Segmented files: `./exported_data/speaker-1/audio001.wav`
  - Metadata and annotations as JSON
- **Enrollment Tracking**: Tracks audio sample counts and total duration per speaker
- **Weighted Embeddings**: Smart speaker updates using weighted averaging

## 🎯 Processing Modes Comparison

The system offers multiple processing modes for different use cases:

| Mode | Name | Transcription | Diarization | Speaker ID | Use Case |
|------|------|--------------|-------------|------------|----------|
| **diarization-only** | Diarization Only | ❌ | ✅ Internal | ❌ | Basic speaker separation without identification |
| **speaker-identification** | Speaker Identification | ❌ | ✅ Internal | ✅ Enrolled | Identify known speakers without transcripts |
| **deepgram-enhanced** | Deepgram Enhanced | ✅ Deepgram | ✅ Deepgram | ✅ Replace | Full transcription with enhanced speaker names |
| **deepgram-transcript-internal-speakers** | Deepgram + Internal | ✅ Deepgram | ✅ Internal | ✅ Enrolled | Best transcription + precise speaker identification |
| **plain** | Plain (Legacy) | ❌ | ✅ Internal | ✅ Enrolled | Same as Speaker Identification |

### Mode Details

#### 🔹 Diarization Only
- **What it does**: Separates speakers into generic labels (Speaker A, Speaker B, etc.)
- **Best for**: Understanding speaker changes without needing names
- **Output**: Timestamped segments with generic speaker labels
- **Requirements**: Audio file only

#### 🔹 Speaker Identification
- **What it does**: Separates speakers AND identifies enrolled speakers by name
- **Best for**: Identifying known speakers in meetings or conversations
- **Output**: Timestamped segments with actual speaker names
- **Requirements**: Enrolled speakers in the system

#### 🔹 Deepgram Enhanced
- **What it does**: Uses Deepgram for both transcription and diarization, then replaces generic speaker labels with enrolled speaker names
- **Best for**: High-quality transcription with speaker identification
- **Output**: Full transcript with identified speaker names
- **Requirements**: Deepgram API key + enrolled speakers

#### 🔹 Deepgram + Internal Speakers
- **What it does**: Uses Deepgram for transcription only, internal system for precise speaker diarization and identification
- **Best for**: Maximum accuracy for both transcription and speaker identification
- **Output**: Deepgram transcript with precisely identified speakers
- **Requirements**: Deepgram API key + enrolled speakers

### Quick Mode Selection Guide

- **Need transcription?** → Use Deepgram modes
- **Only need speaker names?** → Use Speaker Identification
- **Just want to see speaker changes?** → Use Diarization Only
- **Maximum accuracy?** → Use Deepgram + Internal Speakers
- **Best balance?** → Use Deepgram Enhanced

## 🖥️ React Web UI

The modern React interface provides an enhanced user experience with:

### Pages
- **Audio Viewer**: Interactive waveform visualization with click-to-play
- **Annotation**: Label speaker segments with Deepgram transcript support
- **Enrollment**: Record or upload audio with real-time quality assessment
- **Speakers**: Manage enrolled speakers with sample counts and duration metrics
- **Inference**: Identify speakers in new audio files with confidence scores

### Key Features
- **Recording Support**: Direct microphone recording with WebM to WAV conversion
- **Enrollment Options**:
  - Create new speaker enrollment
  - Append to existing speaker (weighted embedding averaging)
  - Direct enrollment from annotation segments
- **Real-time Metrics**: Track sample counts and total audio duration
- **Quality Assessment**: SNR-based quality scoring with visual indicators
- **Export Options**: Download processed audio and annotation data

### Live Inference Improvements

The live inference feature has been significantly enhanced with the following improvements:

- **Dynamic Sample Rate Detection**: No longer assumes 16kHz, automatically detects browser audio context sample rate
- **Extended Audio Buffer Retention**: Increased from 30 seconds to 120 seconds for better utterance capture
- **Fixed Timing Synchronization**: Resolved timestamp display issues and audio/speaker alignment
- **Enhanced Debugging**: Comprehensive logging for troubleshooting live audio processing
- **Audio Buffer Stability**: Fixed stale closure issues with audio buffer management using React refs

### Confidence Threshold Control

The React UI includes an adjustable confidence threshold on the Inference page that controls speaker identification strictness:

- **Range**: 0.00 to 1.00 (adjustable in 0.05 increments)
- **Default**: 0.50 (balanced accuracy vs coverage)
- **"Less Strict" (lower values)**: More segments identified as known speakers, but potentially more false positives
- **"More Strict" (higher values)**: Fewer segments identified, but higher accuracy for identified speakers
- **Typical ECAPA-TDNN values**: 0.10-0.30 for most use cases
- **Additional filtering**: Results can be filtered by confidence after processing

## 📖 Deepgram Response Processing Utilities

The speaker recognition system includes robust utilities for handling Deepgram API responses. **Always use these utilities instead of manual parsing.**

### 🛠️ Core Utilities

#### DeepgramParser (`src/simple_speaker_recognition/utils/deepgram_parser.py`)
**Purpose**: Robust parsing of Deepgram JSON responses with speaker segmentation
**Key Features**:
- Handles both diarized and non-diarized responses
- Groups words by speaker changes into natural segments
- Automatic segment merging and filtering
- Speaker statistics and confidence tracking
- Converts to annotation format

**Basic Usage**:
```python
from simple_speaker_recognition.utils.deepgram_parser import DeepgramParser

parser = DeepgramParser(min_segment_duration=0.5)
parsed_data = parser.parse_deepgram_json("deepgram_response.json")

# Access structured data
segments = parsed_data['segments']  # Clean speaker segments
speakers = parsed_data['unique_speakers']  # List of speaker labels
transcript = parsed_data['transcript']  # Full transcript text
```

#### TranscriptProcessor (`src/simple_speaker_recognition/utils/transcript_processor.py`)
**Purpose**: High-level transcript processing and formatting utilities
**Key Features**:
- Extract segments directly from Deepgram response objects
- Format transcripts with speaker names and timestamps
- Merge consecutive segments and filter short ones
- Export to JSON with metadata and statistics
- Handle both diarized and non-diarized content gracefully

**Basic Usage**:
```python
from simple_speaker_recognition.utils.transcript_processor import TranscriptProcessor

processor = TranscriptProcessor()

# Extract segments from live Deepgram response
segments = processor.extract_segments_from_deepgram(deepgram_response)

# Format for display
transcript_text = processor.format_transcript_text(
    segments,
    include_timestamps=True,
    speaker_names={0: "John", 1: "Jane"}
)

# Quick processing with cleanup
from simple_speaker_recognition.utils.transcript_processor import quick_process_deepgram_response
clean_segments = quick_process_deepgram_response(deepgram_response)
```

### 🎯 YouTube Audio CLI Integration

The YouTube audio processing CLI uses these utilities for clean, robust Deepgram response handling:

```bash
# Download, convert, and transcribe with proper utilities
python scripts/youtube_cli.py "https://youtube.com/watch?v=..." --no-diarization

# With custom settings
DEEPGRAM_API_KEY=your_key uv run python scripts/youtube_cli.py "URL" \
  --language multi --output-dir custom-outputs --no-original
```

**Features**:
- Uses `TranscriptProcessor.extract_segments_from_deepgram()` for parsing
- Uses `TranscriptProcessor.format_transcript_text()` for output
- Handles both diarized and non-diarized transcription seamlessly
- Organized output structure with raw JSON, formatted transcripts, and audio files

**CLI Options**:
- `--deepgram-key KEY`: Deepgram API key (or use DEEPGRAM_API_KEY env var)
- `--output-dir DIR`: Output directory (default: outputs)
- `--language LANG`: Language for transcription (default: multi)
- `--no-original`: Skip downloading original high-quality audio
- `--no-diarization`: Disable speaker diarization

**Output Structure**:
```
outputs/
├── audio/
│   ├── video-title-original.wav          # Original quality
│   └── video-title-processed-16khz-mono-1.wav  # 16kHz mono for transcription
├── transcripts/
│   └── video-title-segment-1-transcript.txt    # Formatted transcript
├── json/
│   └── video-title-segment-1-deepgram-raw.json # Raw Deepgram response
└── video-title-SUMMARY.txt               # Processing summary
```

### 📚 Deepgram Terminology in Speaker Recognition Context

When working with Deepgram responses, you'll encounter different data structures:

#### Words
- **What**: Individual word-level transcription data with precise timestamps
- **Use**: Most accurate for speaker boundaries and timing
- **Example**: `{"word": "hello", "start": 1.23, "end": 1.45, "speaker": 0}`

#### Segments
- **What**: Groups of consecutive words from the same speaker
- **Use**: Natural speech chunks for annotation and training
- **Generated by**: DeepgramParser groups words by speaker changes
- **Example**: Speaker 1 talks from 0-5s, then Speaker 2 from 5-10s

#### Paragraphs/Sentences
- **What**: Deepgram's paragraph-level grouping based on pauses/context
- **Use**: Better for readability but less precise for speaker boundaries
- **Note**: May combine multiple speakers if they talk quickly
- **Example**: A paragraph might span 30-60 seconds with multiple speakers

#### Best Practice
The speaker recognition system uses **word-level data** to create segments because:
- Exact speaker change points (critical for training)
- No overlapping speakers in a segment
- Accurate timing for audio extraction

**⚠️ Important**: Always use the provided utilities instead of manual `channels[0]['alternatives'][0]` parsing:
- **❌ Don't**: `response_dict['results']['channels'][0]['alternatives'][0]['words']`
- **✅ Do**: `TranscriptProcessor.extract_segments_from_deepgram(response)`

When you see issues with transcript processing:
- Use the structured utilities for consistent results
- Check if you're mixing word-level vs paragraph-level data
- Re-process using current utilities for best results

## 📚 Documentation

For detailed documentation, API reference, and advanced usage:
- **[README.detailed.md](README.detailed.md)** - Comprehensive guide

## 🔧 Configuration

Environment variables:
```bash
# Required
HF_TOKEN="your_token"                    # Required: Hugging Face token for PyAnnote models

# Speaker Service Configuration
SPEAKER_SERVICE_HOST="0.0.0.0"          # Speaker service bind host
SPEAKER_SERVICE_PORT="8085"             # Speaker service port (default: 8085)
SPEAKER_SERVICE_URL="http://speaker-service:8085"  # URL for internal Docker communication
SIMILARITY_THRESHOLD="0.15"             # Speaker similarity threshold (0.1-0.3 typical for ECAPA-TDNN)
MAX_DIARIZE_DURATION="600"              # Neural window ceiling in seconds (10 minutes)
DIARIZE_CONCURRENT_CHUNKS="2"           # Bounded parallel windows inside one request
DIARIZE_CHUNK_OVERLAP="5.0"             # Future context at each window boundary

# React Web UI Configuration
REACT_UI_HOST="0.0.0.0"                # Web UI bind host
REACT_UI_PORT="5173"                    # Web UI port (default: 5173)
REACT_UI_HTTPS="true"                   # Enable HTTPS for microphone access (default: true)

# Optional
DEEPGRAM_API_KEY="your_key"             # For transcript import features
DEV="false"                             # Enable development mode with reload
```

Copy `.env.template` to `.env` and configure your settings:
```bash
cp .env.template .env
# Edit .env with your configuration
```

## 🔒 HTTPS and Microphone Access

**For Internal VPN/Network Usage:**

The React UI is configured with HTTPS enabled by default (`REACT_UI_HTTPS=true`) to support microphone recording features, which require secure contexts in modern browsers.

### **First-time Setup:**
1. **Access**: Navigate to https://localhost:5173
2. **Certificate Warning**: Your browser will show a security warning for the self-signed certificate
3. **Accept Certificate**: Click "Advanced" → "Proceed to localhost (unsafe)" or similar
4. **One-time Setup**: This only needs to be done once per browser

### **Why HTTPS is Required:**
- **Browser Security**: Modern browsers require HTTPS for microphone access via `getUserMedia()` API
- **Internal Networks**: Self-signed certificates are acceptable for VPN/internal tools
- **Recording Features**: Both Enrollment and Inference pages need microphone access for live recording

### **Alternative Access:**
- **HTTP Fallback**: Set `REACT_UI_HTTPS=false` in `.env` and use http://localhost:5173
- **Limitation**: Microphone recording will not work without HTTPS (file upload still works)

## 🚨 Troubleshooting

**Caddy/HTTPS not serving a valid certificate?**
- HTTPS is handled by **Caddy** (auto-managed certs); there is no manual cert step.
- For a Tailscale (`*.ts.net`) address, confirm the runtime directory is mounted into Caddy:
  `docker inspect speaker-recognition-caddy-1 --format '{{range .Mounts}}{{.Destination}} {{end}}'`
  should list `/var/run/tailscale`.
- Confirm the served cert: `echo | openssl s_client -connect localhost:8444 -servername <your-name> 2>/dev/null | openssl x509 -noout -issuer -enddate`
- See [../../docs/ssl-certificates.md](../../docs/ssl-certificates.md) for details.

**Can't access the web UI?**
- Check if services are running: `docker compose --profile cpu ps` (or `--profile gpu`)
- View logs: `docker compose --profile cpu logs web-ui`
- Check nginx logs: `docker compose --profile cpu logs nginx`

**Speaker service not responding?**
- Check backend logs: `docker compose --profile cpu logs speaker-service`
- Verify HF_TOKEN is set correctly

**Models not downloading?**
- Ensure HF_TOKEN has access to PyAnnote models
- Check network connection and disk space

## 🔄 Development

For local development without Docker:
```bash
# Terminal 1 - Backend
uv sync
uv run python speaker_service.py

# Terminal 2 - React Web UI
cd webui && npm run dev
```

## API Endpoints

### Health Check
```bash
GET /health
```
**Response:**
```json
{
  "status": "ok",
  "device": "cuda",
  "speakers": 5
}
```

### Speaker Enrollment - Single File
```bash
POST /enroll/upload
Content-Type: multipart/form-data
```
**Form Fields:**
- `file`: Audio file (WAV/FLAC, max 3 minutes)
- `speaker_id`: Unique speaker identifier
- `speaker_name`: Speaker display name
- `start`: Start time in seconds (optional)
- `end`: End time in seconds (optional)

**Response:**
```json
{
  "updated": false,
  "speaker_id": "john_doe"
}
```

### Speaker Enrollment - Batch
```bash
POST /enroll/batch
Content-Type: multipart/form-data
```
**Form Fields:**
- `files`: Multiple audio files for same speaker
- `speaker_id`: Unique speaker identifier
- `speaker_name`: Speaker display name

**Response:**
```json
{
  "updated": false,
  "speaker_id": "john_doe",
  "num_segments": 3,
  "num_files": 3,
  "total_duration": 45.2
}
```

### Speaker Enrollment - Append
```bash
POST /enroll/append
Content-Type: multipart/form-data
```
**Form Fields:**
- `files`: Multiple audio files to append to existing speaker
- `speaker_id`: Existing speaker identifier (must exist)

**Description:**
Appends new audio samples to an existing speaker enrollment using weighted embedding averaging. The system:
- Retrieves the existing speaker's embedding and sample count
- Processes new audio files to generate embeddings
- Computes weighted average: `(old_embedding * old_count + new_embeddings * new_count) / (old_count + new_count)`
- Updates the speaker with the combined embedding and new counts

**Response:**
```json
{
  "updated": true,
  "speaker_id": "john_doe",
  "previous_samples": 3,
  "new_samples": 2,
  "total_samples": 5,
  "previous_duration": 45.2,
  "new_duration": 28.7,
  "total_duration": 73.9
}
```

### Processing Mode Endpoints

#### Diarization Only
```bash
POST /v1/diarize-only
Content-Type: multipart/form-data
```
**Form Fields:**
- `file`: Audio file for diarization
- `min_duration`: Minimum segment duration (optional, default: 0.5)

**Response:**
```json
{
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start": 1.234,
      "end": 5.678,
      "duration": 4.444,
      "speaker_label": "SPEAKER_00",
      "identified": false,
      "status": "diarized_only"
    }
  ],
  "summary": {
    "total_duration": 120.5,
    "num_segments": 15,
    "num_speakers": 2,
    "speakers": ["SPEAKER_00", "SPEAKER_01"],
    "processing_mode": "diarization_only"
  }
}
```

#### Speaker Identification
```bash
POST /diarize-and-identify
Content-Type: multipart/form-data
```
**Form Fields:**
- `file`: Audio file for processing
- `min_duration`: Minimum segment duration (optional, default: 0.5)
- `similarity_threshold`: Speaker similarity threshold (optional, default: 0.15)
- `identify_only_enrolled`: Return only identified speakers (optional, default: false)
- `user_id`: User ID for speaker identification (optional)

**Response:**
```json
{
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start": 1.234,
      "end": 5.678,
      "duration": 4.444,
      "identified_as": "John Doe",
      "identified_id": "john_doe",
      "confidence": 0.892,
      "status": "identified"
    }
  ],
  "summary": {
    "total_duration": 120.5,
    "num_segments": 15,
    "num_diarized_speakers": 3,
    "identified_speakers": ["John Doe", "Jane Smith"],
    "unknown_speakers": ["SPEAKER_02"],
    "similarity_threshold": 0.15,
    "filtered": false
  }
}
```

#### Deepgram Enhanced
```bash
POST /v1/listen
Content-Type: multipart/form-data
Authorization: Token YOUR_DEEPGRAM_API_KEY
```
**Query Parameters:**
- `model`: Deepgram model (default: nova-3)
- `language`: Language code (default: multi)
- `diarize`: Enable diarization (default: true)
- `enhance_speakers`: Enable speaker identification (default: true)
- `user_id`: User ID for speaker identification
- `speaker_confidence_threshold`: Speaker confidence threshold (default: 0.15)

**Response:** Deepgram response with enhanced speaker identification

#### Deepgram + Internal Speakers (Hybrid)
```bash
POST /v1/transcribe-and-diarize
Content-Type: multipart/form-data
Authorization: Token YOUR_DEEPGRAM_API_KEY
```
**Query Parameters:**
- Same as Deepgram Enhanced, plus:
- `similarity_threshold`: Internal speaker matching threshold (default: 0.15)
- `min_duration`: Minimum segment duration (default: 1.0)

**Response:** Deepgram transcription with internal speaker diarization and identification

### List Speakers
```bash
GET /speakers
```
**Response:**
```json
{
  "speakers": [
    {
      "id": "john_doe",
      "name": "John Doe",
      "user_id": 1,
      "created_at": "2024-01-15T10:30:00",
      "updated_at": "2024-01-15T14:20:00",
      "audio_sample_count": 5,
      "total_audio_duration": 73.9
    }
  ]
}
```

### Reset All Speakers
```bash
POST /speakers/reset
```
**Response:**
```json
{
  "reset": true
}
```

### Delete Speaker
```bash
DELETE /speakers/{speaker_id}
```
**Response:**
```json
{
  "deleted": true
}
```

## API Endpoint Architecture

### Deepgram API Compatible Endpoints

The system provides **complete Deepgram API compatibility** with clearly separated endpoints:

| Protocol | Endpoint | Purpose | Usage |
|----------|----------|---------|-------|
| **POST** | `/v1/listen` | File upload transcription | Upload audio files for processing |
| **WebSocket** | `/v1/ws_listen` | Real-time streaming | Stream live audio for transcription |

**File Upload**: `POST https://your-domain/v1/listen` (multipart/form-data with audio file)
**WebSocket Streaming**: `wss://your-domain/v1/ws_listen?model=nova-3&language=en&user_id=1&confidence_threshold=0.15`

### Complete Endpoint Reference

#### Core Processing Endpoints
- `POST /v1/listen` - Deepgram-compatible file transcription with speaker enhancement
- `POST /v1/transcribe-and-diarize` - Hybrid mode: Deepgram transcription + internal speaker identification
- `POST /v1/diarize-only` - Pure speaker diarization without transcription
- `POST /diarize-and-identify` - Internal speaker identification with diarization

#### Streaming Endpoints
- `WSS /v1/ws_listen` - Deepgram-compatible WebSocket streaming with speaker identification
- `GET /v1/listen/info` - API documentation and capability information

#### Speaker Management
- `GET /speakers` - List enrolled speakers
- `POST /enroll/upload` - Enroll single speaker
- `POST /enroll/batch` - Batch speaker enrollment
- `POST /enroll/append` - Add samples to existing speaker
- `DELETE /speakers/{speaker_id}` - Remove speaker
- `POST /speakers/reset` - Reset all speakers

#### Health & Configuration
- `GET /health` - Service health check
- `GET /deepgram/config` - Get Deepgram API key for frontend

#### Authentication
API key passed via WebSocket subprotocols:
```javascript
const protocols = ['token', 'your_deepgram_api_key']
const ws = new WebSocket(url, protocols)
```

#### Event Types

**Client → Server**:
- Binary audio data (16-bit PCM, 16kHz, mono)

**Server → Client**:

##### `ready`
```json
{
  "type": "ready",
  "message": "WebSocket ready for audio streaming"
}
```

##### `utterance_boundary`
Server-side VAD detected speech segment with speaker identification:
```json
{
  "type": "utterance_boundary",
  "timestamp": 1234567890,
  "audio_segment": {
    "start": 1.2,
    "end": 3.8,
    "duration": 2.6
  },
  "transcript": "Hello world",
  "speaker_identification": {
    "speaker_id": "john_doe",
    "speaker_name": "John Doe",
    "confidence": 0.892,
    "status": "identified"
  }
}
```

##### `raw_deepgram`
All Deepgram API responses forwarded transparently:
```json
{
  "type": "raw_deepgram",
  "data": {
    // Complete Deepgram WebSocket response
    "channel": { ... },
    "is_final": true,
    "type": "Results"
  },
  "timestamp": 1234567890
}
```

##### `error`
```json
{
  "type": "error",
  "message": "Error description"
}
```

#### Features

- **Speaker Change Detection**: Server-side VAD using Pyannote
- **Real-time Speaker ID**: Identify enrolled speakers automatically
- **Complete Deepgram Access**: All Deepgram events forwarded via `raw_deepgram`
- **Debug Recording**: Server creates WAV files for troubleshooting
- **HTTPS/WSS Support**: Full browser microphone compatibility

#### True Deepgram Compatibility

These endpoints perfectly mimic Deepgram's API behavior:

```javascript
// Existing Deepgram WebSocket code works unchanged:
const ws = new WebSocket('wss://api.deepgram.com/v1/listen?model=nova-3', ['token', 'API_KEY'])

// Drop-in replacement - use /v1/ws_listen for streaming:
const ws = new WebSocket('wss://your-domain/v1/ws_listen?model=nova-3', ['token', 'API_KEY'])
```

```bash
# Existing Deepgram POST API works unchanged:
curl -X POST "https://api.deepgram.com/v1/listen" \
  -H "Authorization: Token API_KEY" -F "file=@audio.wav"

# Drop-in replacement:
curl -X POST "https://your-domain/v1/listen" \
  -H "Authorization: Token API_KEY" -F "file=@audio.wav"
```

#### Live Inference Modes

| Mode | Description | Best For |
|------|-------------|----------|
| **Live Inference** | `/v1/ws_listen` WebSocket (true Deepgram streaming replacement) | Production use, existing Deepgram clients |
| **Live Inference (Complex)** | Direct Deepgram streaming, client-side coordination | Advanced features, maximum control |

## Integration with Chronicle Backend

The Chronicle backend communicates with this service through the `client.py` module, which provides both async and sync interfaces for backward compatibility.

## Laptop Client

A command-line client (`laptop_client.py`) that can record from your microphone and interact with the speaker recognition service.

### Setup for Laptop Client

The laptop client requires PyAudio for microphone access:

```bash
# On Ubuntu/Debian
sudo apt-get install portaudio19-dev python3-pyaudio

# On macOS
brew install portaudio
pip install pyaudio

# On Windows
pip install pyaudio
```

### Usage Examples

```bash
# Start the speaker service first
docker compose --profile cpu up -d

# Enroll a new speaker (records 10 seconds)
python laptop_client.py enroll --speaker-id "john" --speaker-name "John Doe" --duration 10

# Identify a speaker (records 5 seconds)
python laptop_client.py identify --duration 5

# Verify against a specific speaker (records 3 seconds)
python laptop_client.py verify --speaker-id "john" --duration 3

# List all enrolled speakers
python laptop_client.py list

# Remove a speaker
python laptop_client.py remove --speaker-id "john"

# Use different service URL
python laptop_client.py --service-url "http://YOUR_SERVER_IP:8001" identify
```

### Laptop Client Features

- **Live Microphone Recording**: Records directly from your system microphone
- **Automatic Cleanup**: Temporary audio files are automatically cleaned up
- **Service Health Checks**: Verifies the speaker service is online before operations
- **Real-time Feedback**: Shows recording progress and results with emojis
- **Error Handling**: Graceful handling of network and audio errors

## Testing

### Integration Tests

The service includes comprehensive integration tests that validate the complete speaker recognition pipeline:

```bash
# Run integration tests (requires HF_TOKEN in environment)
cd extras/speaker-recognition
source .env && export HF_TOKEN && uv run pytest tests/test_speaker_service_integration.py -v -s
```

#### Test Requirements
- **Environment Variables**: `HF_TOKEN` must be set (for pyannote models)
- **Docker**: Must be available for test containers
- **Test Assets**: Audio files for Evan and Katelyn speakers (included in `tests/assets/`)

#### What the Tests Cover
1. **Service Health**: Verifies Docker containers start and service is accessible
2. **Speaker Enrollment**: Batch enrollment of multiple speakers with audio files
3. **Database Persistence**: Confirms speakers are stored correctly
4. **Individual Identification**: Tests single-speaker identification accuracy
5. **Conversation Processing**: Full conversation analysis with speaker diarization

#### Test Configuration
- **Test Compose**: Uses `docker-compose-test.yml` with isolated test containers
- **Test Port**: Service runs on port 8086 (vs. 8085 for development)
- **Keep Containers**: Set `SPEAKER_TEST_KEEP_CONTAINERS=1` to debug test failures

### Docker Compose Files

The service provides two Docker Compose configurations:

| File | Purpose | Service Port | Use Case |
|------|---------|-------------|----------|
| `docker-compose.yml` | Development/Production | 8085 | Normal usage, WebUI access |
| `docker-compose-test.yml` | Testing | 8086 | Integration tests, isolated environment |

## Integration with Chronicle Backend

The Chronicle backend communicates with this service through the `client.py` module, which provides both async and sync interfaces for backward compatibility.

## Performance Notes

### CPU vs GPU Performance
- **CPU Mode**: Slower inference (~10-30s for enrollment), smaller memory footprint (~2-4GB)
- **GPU Mode**: Faster inference (~2-5s for enrollment), requires NVIDIA GPU with CUDA (~4-8GB VRAM)
- **Model Loading**: First inference may be slow due to model loading (both modes)
- **Deployment**: Use CPU mode for CI/testing, GPU mode for production workloads

### General Performance
- Audio files should be accessible from both services (use shared volumes)
- Microphone recording dynamically detects browser sample rate (typically 44.1kHz or 48kHz) for optimal compatibility
- Microphone recording requires `pyaudio` and proper audio device setup

### Long-recording diarization

The 10-minute setting is a compute ceiling, not a Conversation boundary. For a longer
recording the service:

1. reads consecutive 10-minute core windows with five seconds of future context;
2. runs Community-1 neural segmentation on at most two independent windows at once;
3. clips each result back to its non-overlapping core interval;
4. embeds each window-local speaker and deterministically reconciles those labels into
   recording-wide speakers; and
5. merges adjacent same-speaker turns only after all windows are complete.

Community-1's recording-wide clustering uses VBx rather than the old unbounded
agglomerative path that motivated 60-second chunks. Each window still performs
sliding-window neural segmentation, speaker embedding, VBx clustering, and diarization
reconstruction. With a fixed window ceiling, total work is approximately linear in
recording duration and peak memory is bounded by the number and size of concurrent
windows rather than the full recording length. Separate API requests remain serialized.

The default concurrency of two is deliberately conservative for a 12 GB GPU. Measure
wall time, host peak and GPU peak on the deployment before raising it; higher concurrency
multiplies the bounded window working set and is a new benchmark, not a harmless tuning
change.
