# ColPali Service — visual search over saved screenshots

Finds a screenshot you saved by what it *looks like*, for the queries that defeat
both its written description and its OCR — "the one with the blue chart", "the
dark dashboard with the histogram".

**This service is additive.** Chronicle describes every shared screenshot on arrival
and writes that prose plus verbatim OCR into a `Media/<digest>.md` vault note, which
the ordinary memory search already finds with ripgrep. This service improves ranking
for the visual tail; it is never required. The node hosting it may be asleep, and
screenshot search keeps working.

## Why late interaction

A ColPali-family model emits one vector per image *patch* rather than one per image,
and scores with MaxSim: each query token takes its best-matching patch, and those
maxima are summed. That is what makes it strong on screenshots specifically, where
the answer usually lives in one small region rather than in the picture's overall
gist — the failure mode of single-vector CLIP-style embeddings.

## Setup

```bash
# From the repository root
uv run --with-requirements setup-requirements.txt python extras/colpali-service/init.py

cd extras/colpali-service
docker compose up colpali -d --build      # podman-compose on a podman host
curl http://localhost:8790/health
```

## Models

| Model | VRAM | Notes |
|---|---|---|
| `vidore/colSmol-256M` (default) | ~0.5–1 GB | Fits beside a desktop's own graphics use |
| `vidore/colqwen2.5-v0.2` | ~7 GB | Better retrieval; little headroom on a 12 GB card |

Pick the small one on a machine you also *use*. Free VRAM on a desktop is a snapshot,
not a budget — every browser and Electron app claims some as it opens. Embedding is a
one-shot cost per image and never on the query path, so the accuracy difference
matters less here than it would in a latency-sensitive setting.

The model is **loaded lazily and unloaded after `COLPALI_IDLE_UNLOAD_SECONDS`**
(default 900). A ten-to-twenty second cold start on the first embed of the day is
invisible because nothing is waiting on it — the backend queues work and reconciles.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness for discovery. Never touches the GPU or loads a model. |
| `/info` | GET | Model id, capabilities, idle-unload setting |
| `/embed` | POST | multipart `file` + `doc_id`, `user_id`, `metadata` (JSON). Idempotent per `doc_id`. |
| `/search` | POST | `{query, user_id, limit}` → ranked `{doc_id, score, metadata}` |
| `/documents` | GET | `?user_id=` → doc ids indexed **by the currently loaded model** |
| `/documents/{doc_id}` | DELETE | `?user_id=` — retention |

## Index storage

No vector database. One `float16` `.npy` per document under
`/index/{user_id}/{doc_id}.npy`, plus an append-only `manifest.jsonl`.

A personal corpus of deliberately-saved images is thousands of items, where
brute-force MaxSim in numpy is single-digit milliseconds — an index server would be
pure operational cost. One file per document also makes an append a single atomic
write, with no rewrite of a growing blob and no window in which the index is corrupt.

`/documents` reports only documents built by the *currently loaded* model. That is
what lets the backend notice a model change or a wiped `/index` volume and re-embed,
instead of silently mixing incomparable vector spaces.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `COLPALI_MODEL` | `vidore/colSmol-256M` | Hugging Face model id |
| `COLPALI_PORT` | `8790` | Published host port |
| `COLPALI_DEVICE` | auto | `cuda` / `cpu` override |
| `COLPALI_IDLE_UNLOAD_SECONDS` | `900` | `0` keeps the model resident |
| `HF_TOKEN` | — | Avoids rate limits; needed for gated repos |
| `PYTORCH_CUDA_VERSION` | `cu126` | Torch wheel index (`cu126`/`cu128`) |

## Notes

- Registered in `services.py`, so `./services start --all` / `./services status` and the WebUI System
  page control it, and the node agent advertises it as `chronicle-colpali`.
- On a node without the agent, the advertise-only sidecar works:
  `docker compose --profile edge up -d`.
- `index/` and `model_cache/` carry a `.gitkeep` because podman refuses to create a
  missing bind-mount source and leaves the container silently in `created`.
