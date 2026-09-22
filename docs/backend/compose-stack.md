# The Chronicle backend compose stack

Reference for `backend/docker-compose.yml` — what each service is for and
why the non-obvious settings are the way they are. The compose file itself carries
only short pointers back here.

Do not drive this file by hand for day-to-day operation: `./services start --all` / `./services stop --all` /
`./services restart --all` route through `services.py`, which selects the container engine
(`container_engine: docker|podman` in `config/config.yml`), exports build args, and
activates the right profiles. See [init-system.md](../init-system.md) and
[podman.md](../podman.md).

## Services

| Service | Profile | Published ports | Role |
|---|---|---|---|
| `chronicle-backend` | — | 8000 | FastAPI API, WebSocket ingestion, plugin host |
| `workers` | — | — | RQ workers + audio persistence + stream consumers |
| `annotation-cron` | `annotation` | — | periodic annotation/error-detection jobs |
| `intent-router` | — | 8791 | voice-command classifier (home automation vs agent) |
| `caddy` | `https` | 80, 443, 3443 | HTTPS reverse proxy (dashboard + Langfuse) |
| `webui-dev` | — | 5173 | React dashboard, Vite dev server with hot reload |
| `mongo` | — | 27017 | conversations, chunks, chat, annotations |
| `redis` | — | 6379 | audio WAL + RQ job queues |
| `vault-syncthing` | `vault-sync` | 22000/tcp+udp, 21027/udp | shares each user's vault to their Obsidian |
| `tailscale` | `tailscale` | — | in-container tailnet membership (rarely needed; the host usually runs Tailscale) |

The stack joins the external `chronicle-network` bridge so other Chronicle compose
projects (speaker recognition, ASR, wake word) can reach it by container name.

## Local llama.cpp network boundary

`extras/llm-services` publishes its host convenience ports on `127.0.0.1` by
default. The backend and workers do not use those published ports: both compose
projects join `chronicle-network`, so the in-container endpoints are
`http://llama-cpp-llm:8080/v1` and `http://llama-cpp-embed:8080/v1`. This keeps the
unauthenticated llama.cpp API off LAN/public interfaces while preserving Pi, Direct,
and embedding access.

`LLM_BIND_HOST` and `EMBED_BIND_HOST` are explicit escape hatches for a deployment
that genuinely needs host-interface publication. Any non-loopback bind must also set
`LLAMA_API_KEY` to the same value in `extras/llm-services/.env` and
`backend/.env`; the shipped model definitions forward that key to the
OpenAI-compatible client. Do not persist a `100.x` Tailnet address as a bind target:
it can change. Tailscale remains the authenticated ingress to Chronicle itself, not
to the raw model server.

## The shared backend image

`chronicle-backend`, `workers`, and `annotation-cron` are the *same image* under the
same tag, differing only in `command`. Their `build.args` must therefore stay
identical — in particular `CHRONICLE_BUILD_VERSION`, which `services.py` exports from
`git describe` before a build (CI sets it to the release tag) and which the backend
reports from `/version`. A mismatch means whichever service builds last wins, and the
reported version silently belongs to another build.

All three build the `prod` stage, which omits test dependencies. The shared image also
ships both optional CLI agent backends: the pinned Codex binary and Pi 0.83.0 with its
compatible Node 22.19.0 runtime. The Dockerfile installs Pi once in a fetcher stage and
copies the same installation into the `prod` and `dev` targets.

## Shared mounts

The three backend containers mount the same set:

| Mount | Why |
|---|---|
| `./src → /app/src` | source is bind-mounted, so backend code changes need a **restart**, not a rebuild |
| `../config → /app/config` | whole config directory: `config.yml`, `defaults.yml`, `plugins.yml` |
| `../plugins → /app/plugins` | external plugins, discovered at startup |
| `../discovery.py` | service-discovery module (read-only) |
| `./data`, `./data/audio_chunks`, `./data/debug_dir` | audio, vault, and debug artifacts on the host |
| `./benchmark` (backend only) | LongMemEval benchmark harness |
| `${CODEX_HOME_DIR:-./data/codex-home} → /codex-home` | Codex CLI auth when `memory.agents.write.backend: codex`; the wizard points it at the host's `~/.codex`, and it is read-write because Codex rotates its tokens |

Dependency or Dockerfile changes still require a rebuild.

Pi does not need a corresponding mount. When a write or search agent selects `pi`,
the backend resolves the configured Chronicle model entry and creates isolated Pi
configuration for that invocation. In particular, selecting a local
OpenAI-compatible model does not require a Pi login or a persistent `~/.pi` volume.

### The Tailscale socket directory

Both backend containers mount `/var/run/tailscale` — the **directory**, not the
socket file inside it — so minidisc can talk to the host `tailscaled`.

Bind-mounting the socket file pins an inode, and systemd's
`RuntimeDirectory=tailscale` deletes and recreates `/run/tailscale` on every
`tailscaled` restart. The container is then left holding a deleted socket that
refuses every connection until it is restarted. Mounting the directory pairs with the
`RuntimeDirectoryPreserve=yes` drop-in that `services.py` installs, so the directory
survives too. Full failure modes and diagnosis in
[ssl-certificates.md](../ssl-certificates.md).

## DNS pinning (`x-public-dns`)

Every service that makes outbound calls gets explicit `dns:` upstreams. On a
DNS-enabled network these are handed to the engine's embedded resolver (aardvark
under Podman) as *upstream* servers; the container's `resolv.conf` still points at
that resolver, so container-name lookups are unaffected.

Without them, aardvark falls back to the host resolver, and aardvark 1.4.0 can stop
forwarding external queries permanently after one transient upstream failure — while
still answering container names, so every health check stays green and nothing can
reach the internet.

`100.100.100.100` is listed **first** on purpose. Pinning only public resolvers makes
every Tailscale MagicDNS name (`*.ts.net`) unresolvable inside containers, which is
how services on other tailnet nodes are addressed (an Immich library, a remote ASR
box). Tailscale's resolver answers public names too, and `1.1.1.1` / `8.8.8.8` remain
as fallbacks for hosts with no tailnet. See
[podman.md](../podman.md#caveats) for the diagnosis recipe.

## Service notes

### `chronicle-backend`

Serves the API and WebSocket ingestion. `extra_hosts: host.docker.internal:host-gateway`
gives it the host network, which is how it reaches the node agent and any
host-side/tailnet services. Notable environment:

| Variable | Purpose |
|---|---|
| `VAULT_SYNC_SYNCTHING_URL` | Syncthing REST API on the internal network (`vault-syncthing:8384`) |
| `WAKEWORD_SERVICE_URL` | wake-word data-collection proxy to the standalone service on `chronicle-network` |
| `CHRONICLE_TTS_URL` | TTS endpoint for spoken device replies; empty means discover `chronicle-tts` on the Tailnet, and the wizard sets it explicitly for a local or pinned endpoint |
| `SERVICE_MANAGER_URL` | host node agent (`:8775`) that the WebUI System page drives; its token comes from `.env` |

Health is `/readiness`, so dependents wait for service dependencies, not just a
listening port.

### `workers`

One container running the whole worker fleet through `worker_orchestrator.py`, which
handles process supervision, health monitoring, and self-healing:

- 6 RQ workers (transcription, memory, default queues)
- 1 audio-persistence worker (audio queue)
- 1+ stream workers, conditional on the `stt_stream` provider in `config.yml`

No CUDA: the backend and workers only orchestrate jobs and call external services.

Tunables (defaults shown): `WORKER_CHECK_INTERVAL=10`, `MIN_RQ_WORKERS=6`,
`WORKER_STARTUP_GRACE_PERIOD=30`, `WORKER_SHUTDOWN_TIMEOUT=30`.

The healthcheck (`worker_healthcheck.py`) probes the actual work rather than the
process: it fails if the RQ fleet shrank below `MIN_RQ_WORKERS` or a stream-consumer
heartbeat went stale — the wedged-but-alive case a process check misses. Its
`start_period: 90s` covers orchestrator startup plus worker boot.

### `annotation-cron`

Periodic jobs for AI-assisted annotation: daily passes that surface potential errors
in transcripts and memories, weekly fine-tuning of the error-detection models from
user feedback. Set `DEV_MODE=true` in `.env` for 1-minute intervals when testing.

Optional; enable with the `annotation` profile.

### `intent-router`

Classifies a voice command as a home-automation request versus a general agent/chat
query (sub-millisecond Model2Vec + logistic regression). It lives in its own image so
the ML dependencies do not bloat the backend image; the Home Assistant plugin calls
`http://intent-router:8791/classify`. `../extras/intent-router` is mounted live, so
a retrained classifier takes effect without a rebuild.

### `caddy`

HTTPS termination for the dashboard and Langfuse — required for browser microphone
access over the network. Starts only under the `https` profile, which the wizard
enables once a `Caddyfile` exists. Certificate modes, Tailscale certs, and renewal are
covered in [ssl-certificates.md](../ssl-certificates.md).

### `webui-dev`

The only WebUI. Source is volume-mounted and served by the Vite dev server on `:5173`
with hot reload, so frontend changes appear without a rebuild; Caddy fronts it for
HTTPS. `VITE_ALLOWED_HOSTS` must list the hostnames used to reach it.

### `redis`

Both the RQ broker and the raw-audio write-ahead log. It runs with
`--appendonly yes --appendfsync always` so an `XADD` is acknowledged only after the
append is fsynced — otherwise a host crash can erase the last second of audio the
system already accepted. See [audio-durability.md](audio-durability.md).

### `vault-syncthing`

A Syncthing instance that shares each user's vault (`data/conversation_docs/{user_id}`)
so it can be opened in Obsidian. It is configured **exclusively** by the backend's
`/api/vault-sync` broker, never by hand.

Its REST API stays on the internal network (`vault-syncthing:8384`, authenticated with
`VAULT_SYNC_API_KEY`); only the sync protocol port 22000 is published, reached over
Tailscale. `VAULT_SYNC_PUID`/`PGID` must match the ownership of the
`conversation_docs` files. Enable with the `vault-sync` profile.

### `tailscale`

Optional in-container tailnet membership, under the `tailscale` profile. Most
deployments run Tailscale on the host instead and reach it through
`host.docker.internal` plus the mounted socket directory.

## Profiles

```bash
# services.py handles these; the raw equivalents are:
docker compose --profile https up -d          # Caddy / HTTPS
docker compose --profile vault-sync up -d     # Obsidian vault sharing
docker compose --profile annotation up -d     # annotation cron
docker compose --profile tailscale up -d      # in-container Tailscale
```

Under Podman substitute `podman-compose`.
