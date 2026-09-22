# Running Chronicle with Podman

For everyday status, start/stop/restart, recovery checks, and the boundary for raw
engine diagnostics, use the [service-management workflow](init-system.md#service-management).
This page covers Podman setup and engine-specific troubleshooting.

Chronicle's service lifecycle (`services.py`, `status.py`, the service-manager
agent) works with either **Docker** (default) or **Podman**. This is useful where
Docker Desktop is unavailable (e.g. locked-down work machines) or when you prefer a
single rootless engine across machines.

Choosing the engine does **not** require editing any compose files — the repo's
existing compose definitions (profiles, healthchecks, `host.docker.internal`, and
NVIDIA `deploy.resources` GPU blocks) run unmodified under Podman.

## Selecting the engine

Precedence: environment variable → `config/config.yml` → `docker` default.

```yaml
# config/config.yml
container_engine: podman        # "docker" (default) or "podman"
# compose_cmd: podman-compose   # optional; default derives from container_engine
```

Or per-shell:

```bash
export CONTAINER_ENGINE=podman          # used for `network`/`inspect`/`ps`
export COMPOSE_CMD="podman-compose"     # used for up/down/build/restart
```

`config.yml` is the recommended home because the **service-manager** and
**discovery** agents run as systemd *user* services and do not inherit your shell
environment.

### Why `podman-compose` (not `docker compose` against the podman socket)

On Ubuntu 24.04 (Podman 4.9), `docker compose` talking to Podman's Docker-compat
socket does **not** perform CDI GPU injection — GPU services start without a GPU.
`podman-compose` drives the `podman` CLI directly, where CDI works. So for podman,
the compose front-end is `podman-compose`. The only consequence is that
`podman-compose`'s `ps` output is not docker-compatible; Chronicle handles this
internally by querying `podman ps` scoped to the compose project label.

## Host setup (rootless)

1. **Install the engine + compose + (GPU) toolkit:**
   ```bash
   sudo apt install -y podman
   uv tool install podman-compose            # or: sudo apt install podman-compose
   # GPU hosts only:
   sudo apt install -y nvidia-container-toolkit
   ```

2. **GPU hosts — generate the CDI spec** (works in WSL2 with the NVIDIA WSL driver):
   ```bash
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   nvidia-ctk cdi list          # expect nvidia.com/gpu=all
   # quick check:
   podman run --rm --device nvidia.com/gpu=all \
     docker.io/nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
   ```

3. **Clear any Docker Desktop credential helper** (otherwise image pulls fail
   invoking `docker-credential-desktop`):
   ```bash
   # if ~/.docker/config.json contains "credsStore": "desktop..."
   cp ~/.docker/config.json ~/.docker/config.json.bak
   echo '{}' > ~/.docker/config.json
   ```

4. **Select podman** in `config/config.yml` (`container_engine: podman`).

5. **Start services as usual:** `./services start --all`, `./services status`, `./services stop --all` — they all
   route through the selected engine.

## Migrating an existing Docker install

- **Data ownership.** Docker creates bind-mounted data (`backend/data/...`)
  owned by `root` (root-running containers) or by a service UID like `999`
  (mongo/redis drop privileges at runtime). Rootless Podman remaps container UIDs
  through your subuid range, so it can't write either until re-owned. `podman
  unshare chown` alone fails on the root-owned dirs because real root isn't mapped
  into your namespace — so it's two steps:
  ```bash
  # 1) real root pulls everything back to your user (fixes root-owned dirs;
  #    container-root maps to you, so root-running services are then correct)
  sudo chown -R "$(id -un):$(id -gn)" backend/data
  # 2) remap services that run as a non-root UID inside (mongo + redis = 999),
  #    set from inside the podman user namespace
  podman unshare chown -R 999:999 backend/data/mongo_data
  podman unshare chown -R 999:999 backend/data/redis_data
  ```
  General rule for any straggler that can't write: `podman unshare chown -R
  <container_uid>:<container_uid> <dir>`.
- **Free the ports.** Stop/decommission Docker Desktop (or `docker compose down`
  the old stack) so Podman can bind 6379/27017/8000/etc.
- **Docker Desktop PATH shims.** `/usr/bin/docker` and `/usr/bin/docker-compose` are
  often Docker Desktop symlinks that go stale when it's removed. They don't affect
  Podman, but a stale `docker-compose` shim is why a self-contained tool
  (`podman-compose`) is used rather than borrowing Docker Desktop's binary.

## Caveats

- **Privileged ports (Caddy).** Rootless `ip_unprivileged_port_start` is `1024`, so
  Caddy's `443` binds but its `80` HTTP→HTTPS redirect does not. Only relevant with
  the `https`/Caddy profile. If you need it:
  ```bash
  sudo sysctl net.ipv4.ip_unprivileged_port_start=80    # persist in /etc/sysctl.d
  ```
- **inotify watch exhaustion (WebUI 502 / Vite crash loop).** Containers share the
  host's `fs.inotify.max_user_watches` pool (default 65536). A host-side IDE file
  watcher (Cursor/VSCode server) recursively watching this large repo can consume
  nearly the entire pool, starving the webui container's Vite dev server → it
  crash-loops with `ENOSPC` on `watch` and Caddy returns 502. (Instances are not the
  issue — those stay low.) Two fixes, complementary:
  ```bash
  # immediate: raise the watch limit (VSCode/Cursor docs recommend this on big repos)
  echo 'fs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-inotify.conf && sudo sysctl --system
  ```
  Also add `files.watcherExclude` in the IDE for heavy paths (`**/node_modules`,
  `**/.venv`, `**/data/**`, `**/model_cache/**`, `golden_data`, `experiments`) so the
  IDE isn't watching tens of thousands of model/data files.
- **Reboot persistence.** Docker's daemon restarts `restart: unless-stopped`
  containers on boot. Rootless Podman is daemonless — nothing re-applies `restart:`
  policies after a reboot (the policy still covers crash-restart while the box is
  up, just not the reboot gap). Instead of Podman's own `podman-restart.service`
  (which only matches `restart-policy=always`, *not* `unless-stopped`), Chronicle
  ships a **`chronicle-stack`** systemd *user* oneshot that runs `./services start
  --all` on boot — the same path as `./services start --all`, respecting the `config.yml`
  enabled set and authoritative node placement. Its `ExecStop` runs `services.py
  stop --all`, so `systemctl --user stop chronicle-stack` shuts down the containers
  while leaving the managed node agent available. It installs (with linger)
  alongside the node agent via the wizard's
  "Auto-start on boot" prompt or `./services manager install`, and is ordered
  `After=chronicle-service-manager.service`. So no compose-file edits or
  `podman-restart.service` are needed. Manage/inspect it with `systemctl --user
  status chronicle-stack` and `journalctl --user -u chronicle-stack`.
- **Implicit `default` network.** docker-compose silently creates a `<project>_default`
  network for services that declare no `networks:`. podman-compose instead errors
  `missing networks: default` once any *other* network (e.g. external
  `chronicle-network`) is present. Fix: declare `default: {}` in that compose file's
  top-level `networks:` (a no-op under docker). Only `extras/langfuse` needed this;
  all compose files now parse under both engines.
- **Missing bind-mount sources.** Docker silently creates a bind-mount source
  directory that doesn't exist; crun refuses to start the container instead
  (`cannot stat '<path>': No such file or directory: OCI runtime attempted to
  invoke a command that was not found` — the message names a *command*, but the
  problem is the mount). Git cannot store an empty directory, so any runtime dir
  a compose file mounts is missing on a fresh checkout. The failure is quiet:
  the container is left in `created`, not `exited`, so nothing is listening and
  `./services start --all` reports success for the rest of the stack. Seen on kraken, where
  `extras/asr-services`' `./debug` and `./lora_adapters` left the ASR service
  unstarted for days and dictation 503'd with "Cannot reach transcription
  service". Fix: commit a `.gitkeep` in each mounted runtime dir (done for those
  two). Diagnose with `podman inspect <container> --format '{{.State.Error}}'`,
  and audit a compose file with:
  ```bash
  grep -oE '^\s+- \./[^:]+:' docker-compose.yml | sed -E 's|.*\./||; s|:$||' \
    | sort -u | while read d; do [ -e "$d" ] || echo "MISSING $d"; done
  ```
- **aardvark-dns stops forwarding external queries.** Ubuntu 24.04 ships
  aardvark-dns 1.4.0, which can permanently stop forwarding upstream after a single
  transient failure of the host resolver — while still answering container-name
  lookups. The result is that Mongo and Redis stay green and every container health
  check passes while nothing can reach the internet. It only re-reads its upstreams
  when its config changes, which Podman rewrites on any container add/remove, so a
  stable stack stays broken indefinitely. Diagnose by asking the resolver from
  *inside* a container — from the host it looks fine:
  ```bash
  podman exec <container> getent hosts api.openai.com    # fails
  podman exec <container> getent hosts mongo             # still works
  ```
  Chronicle prevents this by giving each service explicit `dns:` upstreams
  (`x-public-dns` in `backend/docker-compose.yml`). On a DNS-enabled
  network these become aardvark's *per-container* upstreams — the container's
  `resolv.conf` still points at aardvark, so container-name resolution is
  unaffected, but the broken global fallback is bypassed. `./services doctor`
  checks for it; the node agent's watchdog repairs it by churning a throwaway
  container to force a reload.

  Pinning only *public* resolvers trades one outage for another: MagicDNS names
  are served solely by `100.100.100.100`, so `*.ts.net` stops resolving in
  containers (public and container names keep working) and anything addressed by
  tailnet name fails with `[Errno -2] Name or service not known`. Hence
  `100.100.100.100` first — see
  [compose-stack.md](backend/compose-stack.md#dns-pinning-x-public-dns). Same
  asymmetric diagnosis:
  ```bash
  getent hosts <peer>.<tailnet>.ts.net                          # host: works
  podman exec <container> getent hosts <peer>.<tailnet>.ts.net  # container: fails
  ```

- **Docker Desktop auto-start.** On Windows, Docker Desktop relaunches on login and
  its `restart: unless-stopped` containers reclaim host ports (27017/6379/…) via the
  WSL relay, blocking Podman. Uninstall it (or disable login-start + `compose down`
  the old stack) — note its `/usr/bin/docker{,-compose}` symlinks then dangle
  harmlessly.
- **Windows Firewall (WSL2).** Windows Defender Firewall blocks inbound LAN traffic
  to WSL2-hosted ports by default, so phones / companion Macs silently can't reach
  the backend (Docker Desktop's proxy used to get allowed implicitly; rootless
  podman gets nothing). Chronicle manages the rules itself: `./services start`
  converges them automatically on WSL2 hosts, and `./services firewall sync|list|clear`
  drives them manually. Every managed rule is named `Chronicle: <service> <label>
  <port>/<proto>` and scoped to `localsubnet` + the Tailscale CGNAT range
  (`100.64.0.0/10`) — nothing is exposed to the public internet, and `clear` removes
  exactly the rules Chronicle created. Adding rules needs elevation: WSL interop from
  an interactive shell is usually unelevated (the exact `netsh` commands are printed
  for an admin PowerShell), while sessions over Windows sshd are elevated and apply
  directly. Non-WSL2 hosts: everything is a no-op.

## Toward seamless setup (TODO)

The rootless-podman host prerequisites above are currently manual. To make podman a
one-command choice for the user, the wizard / `services.py` should automate them when
`container_engine: podman` is selected. Each is independently scriptable:

| Manual step today | Who should automate it | Notes |
|---|---|---|
| `unqualified-search-registries=["docker.io"]` + `short-name-mode="permissive"` | wizard (write `~/.config/containers/registries.conf` if absent) | idempotent; required for builds to resolve short names |
| `nvidia-ctk cdi generate` (GPU hosts) | wizard (already detects CUDA via `chronicle_setup.detect_cuda_version`) | needs sudo; prompt + run |
| `sysctl net.ipv4.ip_unprivileged_port_start=80` | `services.py` preflight when the `https`/Caddy profile is active | needs sudo; only if Caddy enabled. Could also drop Caddy's `:80` listener for rootless |
| Data-dir ownership remap (`sudo chown` + `podman unshare chown 999`) | a `services.py migrate-podman` helper (one-shot) | only when migrating an existing docker install |
| Clear Docker Desktop `credsStore` | wizard (detect `desktop` credsStore, offer to neutralize) | harmless on fresh hosts |

Until automated, `docs/podman.md` (this file) is the checklist. The **engine
abstraction itself** (`container_engine`/`compose_cmd`) is already seamless — only the
host prerequisites need wiring into setup.
