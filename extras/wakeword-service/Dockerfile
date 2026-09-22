# syntax=docker/dockerfile:1

#########################  builder  #################################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency manifest first for cache-friendly installs.
COPY pyproject.toml ./
# nanowakeword (interpreter core) is installed from a locally-built wheel staged
# by build_nanowakeword.sh — PyPI lacks 2.1.4 (the version the model was exported
# with). Run ./build_nanowakeword.sh before `docker compose build`.
COPY vendor/ ./vendor/
RUN uv pip install --system --no-cache \
        "fastapi>=0.115,<1" \
        "uvicorn[standard]>=0.30,<1" \
        "redis>=5.0,<6" \
        "protobuf>=6.31.1,<7" \
        "numpy>=1.26,<3" \
        "onnxruntime>=1.20,<2" \
        "pipecat-ai==0.0.108" \
        "soxr>=0.5,<2" \
        "setuptools==69.5.1" \
        ./vendor/nanowakeword-*.whl

#########################  runtime  #################################
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

# Bring in the installed site-packages and console scripts from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Service code + vendored ONNX models (hermes.onnx, smart-turn, silero, feature).
# models/ also carries the second-stage verifier weights (hey_hermes_verifier.npz).
COPY app.py consumer.py detector.py identities.py samples.py verifier.py hubert_detector.py \
    active_turn_consumer.py turn_segmenter.py loop_monitor.py ./
COPY audio_contract ./audio_contract
COPY models ./models
COPY tones ./tones

# Pre-place the nanowakeword feature ONNX (melspec + embedding) into the package
# location the NanoInterpreter expects, so no network fetch happens at runtime.
RUN NW_MODELS="/usr/local/lib/python3.12/site-packages/nanowakeword/interpreter/models" && \
    mkdir -p "$NW_MODELS/mel_spectrogram" "$NW_MODELS/embedding" && \
    if [ -f /app/models/melspectrogram.onnx ]; then \
        cp /app/models/melspectrogram.onnx "$NW_MODELS/mel_spectrogram/"; fi && \
    if [ -f /app/models/embedding_model.onnx ]; then \
        cp /app/models/embedding_model.onnx "$NW_MODELS/embedding/"; fi

# HuBERT backend (.pt wake models, e.g. hermes_hubert_convattn.pt): CUDA torch +
# torchaudio (the cu121 wheels bundle the CUDA runtime libs; the host only needs the
# NVIDIA driver + the nvidia container runtime). The HuBERT-base weights are then
# pre-cached into the image so no model download happens at runtime. Routing in
# detector.py sends ``.pt`` models here and ``.onnx`` to NanoInterpreter, so an
# ONNX-only deploy still works (it just carries torch unused).
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu121 \
        "torch==2.5.1" "torchaudio==2.5.1"
RUN python -c "import torchaudio; torchaudio.pipelines.HUBERT_BASE.get_model(); print('HuBERT-base cached')"

ENV WAKEWORD_MODEL_PATH=/app/models/hey_hermes.onnx \
    SMART_TURN_MODEL_PATH=/app/models/smart-turn-v3.2-cpu.onnx \
    SILERO_VAD_MODEL_PATH=/app/models/silero_vad.onnx \
    WAKEWORD_SERVICE_PORT=8770

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8770/health || exit 1

CMD ["python", "app.py"]
