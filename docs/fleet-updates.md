# Fleet Updates

How Chronicle nodes learn about and apply code updates, and how the hub drives
updates across the cluster. (Phase 1+2 of the fleet-update plan: compute nodes.
Mobile OTA and ESP32 firmware distribution are separate, later phases.)

## Version truth

A node is a git checkout, so its version is `git describe` on that checkout
(e.g. `v0.2.2-32-g07e194ee`). `updates.py` (repo root) owns this:

- `repo_version()` → `{describe, commit, branch, dirty}` — `branch` is `None`
  for detached (release-tag) installs.
- The node agent reports it in `GET /node` (`version`) and rides it on the
  Tailnet advertisement (`chronicle-node` label `version`), so the hub sees
  cluster-wide version drift without polling.
- Container images bake `CHRONICLE_BUILD_VERSION` at build time (git describe
  locally, the release tag in CI) — the backend serves it at `GET /version` and
  in `/health`. If the image version lags the checkout version, the containers
  need a rebuild/restart.

## Update modes

`updates.py` picks the mode from how the node was installed:

- **branch mode** — HEAD is on a branch with an upstream (dev checkouts,
  `edge/install.sh --branch` installs): `git pull --rebase --autostash`.
- **release mode** — HEAD is detached (root `install.sh` clones the latest
  release tag) or an explicit `--tag`: fetch tags, check out the target
  (default: latest `v*` tag). Refuses on a dirty tree.

After the checkout moves, every enabled service (per `config/config.yml
services:`) is restarted with `up --build` — or with prebuilt registry images
when a prebuilt tag is given (same `CHRONICLE_REGISTRY`/`CHRONICLE_TAG` env
contract as `./services start --use-prebuilt`).

**Rollback:** if any service fails to come up on the new code, the checkout is
restored to the previous commit (detached — local branches are never rewritten)
and the services are restarted from the old code.

## CLI (any node)

```bash
./services update --check   # is an update available?
./services update           # update + rebuild/restart services
uv run ... python ./services update --tag v0.3.0                                     # pin a specific tag/ref
uv run ... python ./services update --prebuilt v0.3.0                                # pull GHCR images instead of building
uv run ... python ./services update --no-restart                                     # move the checkout only
```

## Node agent API (`:8775`)

- `GET /update?node=<host>&target=<ref>` — update check (fetches origin;
  `node` forwards to a peer's agent over the Tailnet, like service actions).
- `POST /update` `{target?, prebuilt?, node?}` — runs the update as a standard
  agent operation (`{operation}` to poll via `GET /operations/{id}`), with
  progress in `phase`. On success the agent restarts itself last (via its
  systemd unit, else re-exec) so it also runs the new code.

The backend proxies these for the WebUI as `GET/POST /admin/update` (admin
only); the System page shows per-node versions with check/update actions.
Updating the **hub** restarts the backend — the WebUI briefly disconnects.

## GitHub → users flow

1. CI (`backend-docker-compose-build.yml`) builds images on release/main and
   pushes to GHCR with `CHRONICLE_BUILD_VERSION` baked in.
2. A node's update check compares its checkout against its upstream branch or
   the latest `v*` release tag (via `git fetch`, no GitHub API dependency).
3. Applying an update moves the checkout and rebuilds locally by default;
   `prebuilt` switches to pulling the CI images instead.
4. The hub fans updates out node-by-node through the node agents.

## Known limits

- The agent re-exec path doesn't re-resolve Python deps; if
  `setup-requirements.txt` changed, re-run `./services start --all` (systemd-managed agents
  are fine — the unit restart goes through `uv run`).
- No automatic/scheduled updates — checks and applies are operator-triggered
  (by design for a system doing live audio capture).
- Mobile app (expo-updates currently disabled) and ESP32 firmware (ESPHome
  push-flash) are not covered yet.
