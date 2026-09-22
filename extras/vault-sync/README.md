# Chronicle Vault Sync

Keeps your Chronicle Obsidian vault (`data/conversation_docs/{your_user}` on the
server) synced to a local folder, so you can open it in **Obsidian** with full
backlinks, graph view, etc.

Under the hood it runs a private, headless [Syncthing](https://syncthing.net) and pairs
it with the server automatically through the Chronicle backend — you never touch the
Syncthing UI.

> **The tray UI moved.** Vault sync now appears as a section in the unified
> [Chronicle tray](../chronicle-tray/) (`extras/chronicle-tray`), alongside
> ScreenPipe and pendant streaming. This project keeps the sync engine
> (the `chronicle_vault_sync` package), while the tray reads client
> configuration from the repository-root `.env`. If you had the
> old single-purpose app installed as a login item, `uv run python main.py
> uninstall` removes it (installing the new tray also removes it automatically).

```
Server Syncthing  ◀── sync protocol :22000 (Tailscale, LAN, …) ──▶  Mac Syncthing
  (data/conversation_docs/{user})                                    (~/ChronicleVault)
        ▲                                                                   │
        │  pairing brokered by backend  /api/vault-sync                     ▼
        └───────────────  Mac authenticates with its JWT  ───────────  Obsidian
```

Tailscale is **not required** — the Mac only needs *some* route to the backend and to
port 22000 on the server: a Tailnet name, a plain LAN IP when you're on the same wifi
(e.g. a work laptop that can't run Tailscale), or a public domain all work. With an
explicit server address configured, the local Syncthing never touches relays or global
discovery, so nothing leaves your network.

## Prerequisites

```bash
brew install syncthing   # the sync engine
# uv (if you don't have it): curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Arch/CachyOS:

```bash
sudo pacman -S obsidian syncthing
```

You also need the Chronicle **server** side running with vault sync enabled — see
[Server setup](#server-setup-once) below.

## Setup

The easiest path is the root wizard — pick **Setup type → 4) Companion device**:

```bash
./wizard.sh    # from the repo root
```

It checks prerequisites (offers to `brew install syncthing`), asks for the backend URL
and your login, verifies the pairing broker end-to-end, and can install the app as a
login item. Or run the same setup directly / configure by hand:

```bash
cd extras/vault-sync
uv run --with-requirements setup-requirements.txt python extras/vault-sync/init.py

# manual alternative — client config lives in the repository-root .env:
cd ../..
cp .env.template .env  # if the repository-root .env does not exist yet
#   edit .env: set CHRONICLE_API_KEY and BACKEND_URL
#   (mint the key in the webui: Settings → API Keys)

cd extras/chronicle-tray && uv run chronicle-tray
```

> **Self-signed HTTPS (LAN servers):** this app is not a browser — it doesn't need
> HTTPS. If your server's cert is self-signed (Caddy internal CA on an IP address),
> point `BACKEND_URL` at the plain-HTTP backend port instead:
> `BACKEND_URL=http://<server-ip>:8000`.

The Mac needs only two things: `BACKEND_URL` and `CHRONICLE_API_KEY`. Running
`init.py` mints the key for you — it asks for your Chronicle password once and
stores only the resulting key, never the password. The pairing broker hands back
everything else (server device id, sync address) — you never set `VAULT_SYNC_*` on
the Mac; those are server-only.

> **macOS + Tailscale: set `BACKEND_URL` explicitly.** Auto-discovery (minidisc) needs
> the `tailscaled` unix socket at `/var/run/tailscale/tailscaled.sock`, which the **macOS
> GUI / App-Store Tailscale app does not expose** (it runs as a sandboxed network
> extension). So on a Mac, discovery returns nothing and the app would fall back to
> `localhost`. Just point at your server's MagicDNS name — that *is* discovery for a
> fixed server, and needs no socket:
> ```bash
> BACKEND_URL=https://<your-host>.ts.net
> ```
> Confirm with `test -S /var/run/tailscale/tailscaled.sock && echo present || echo absent`
> (absent is normal). The app logs exactly which path it took — see **View Logs**.

On launch it starts Syncthing, authenticates to Chronicle, pairs, and begins syncing
into `~/ChronicleVault` (or `LOCAL_VAULT_DIR`). From the menu:

- **Open in Obsidian** — opens the synced folder as a vault
- **Choose Vault Folder…** — pick a different local folder (re-pairs automatically)
- **Sync Now / Re-pair** — re-run the handshake
- **View Logs** — recent activity

### Run it as a login item (always on)

```bash
cd ../chronicle-tray && uv run chronicle-tray install
```

On macOS this installs a launchd agent; on Linux a systemd user service attached
to the graphical session (see `extras/chronicle-tray/README.md`).

## Server setup (once)

On the machine running the Chronicle backend:

1. Add a strong key and the sync address(es) the Mac should dial to
   `backend/.env` — comma-separate several and the client tries each, e.g.
   your Tailnet name for remote devices plus the LAN IP for devices without Tailscale:
   ```bash
   VAULT_SYNC_API_KEY=<any long random string>
   VAULT_SYNC_ADDRESS=tcp://<your-host>.ts.net:22000,tcp://192.168.1.20:22000
   ```
2. Start the Syncthing service (it's behind a compose profile) and (re)create the
   backend so it picks up the new env + the broker route:
   ```bash
   cd backend
   docker compose --profile vault-sync up -d vault-syncthing
   docker compose up -d --force-recreate chronicle-backend
   ```
   Make sure port **22000** is reachable from your Mac (it is over Tailscale; on a
   Windows/WSL2 host run `uv run --with-requirements setup-requirements.txt python
   ./services firewall sync` from the repo root — it manages the Windows Firewall
   rules for all enabled services, vault sync included).
3. Verify the broker chain (should return a `server_device_id` + your `sync_address`):
   ```bash
   curl -s -H "Authorization: Bearer $CHRONICLE_API_KEY" \
     http://localhost:8000/api/vault-sync/info
   ```

The backend's `/api/vault-sync` broker configures the server Syncthing for you when the
Mac pairs — no manual Syncthing setup on the server either.

> **Use `--force-recreate`, not `restart`.** The backend bind-mounts `discovery.py` as a
> single file; editing it changes the inode and a plain `docker compose restart` fails to
> re-establish the mount (especially on Docker Desktop / WSL). `up -d --force-recreate`
> recreates the container with fresh mounts. A recreate is also what injects newly added
> `.env` values into the container's environment.

## Notes

- **Conflicts**: Syncthing keeps both sides; simultaneous edits to the same note from
  the server (AI) and Obsidian produce a `*.sync-conflict-*.md` file rather than losing
  data. In practice the AI mostly creates/appends and you mostly curate, so this is rare.
- **Multiple Macs**: pair each one; they all share the same server folder.
- The local Syncthing uses its own home dir and GUI port (`8385` by default), so it
  won't interfere with any Syncthing you already run.
