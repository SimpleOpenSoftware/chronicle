# SSL Certificates & HTTPS

Chronicle uses **Caddy** for automatic HTTPS. The backend Caddy serves both the main
dashboard and Langfuse; speaker recognition has its own Caddy on a non-conflicting port.

## Why HTTPS is Needed

Modern browsers require HTTPS for:
- **Microphone access** over network (not localhost)
- **Secure WebSocket connections** (WSS)
- **Remote access** via Tailscale/VPN
- **Production deployments**

Note: the **native mobile app** does not require HTTPS — only browsers do.

## How certificates are managed

Both services front their containers with Caddy:

| Service | Config | HTTP → HTTPS ports |
|---------|--------|--------------------|
| Chronicle Backend | `backend/Caddyfile` | `80` → `443` |
| Langfuse | `backend/Caddyfile` | direct `3002`; HTTPS `3443` |
| Speaker Recognition | `extras/speaker-recognition/Caddyfile` | `8081` → `8444` |

With a Tailscale host such as `node.example.ts.net`, the browser UIs are therefore
`https://node.example.ts.net`, `https://node.example.ts.net:3443` (Langfuse), and
`https://node.example.ts.net:8444` (speaker recognition). The WebUI System page shows
these canonical URLs for each running node.

The wizard records the chosen approach as `HTTPS_CERT_MODE` in each service's `.env`:

### `caddy` mode (default)

Caddy obtains **and auto-renews** the certificate itself — no cert files on disk, no
renewal cron. The Caddyfile has no `tls` directive; Caddy picks the right source from
the site address:

- **`*.ts.net` (Tailscale)** → fetched from the local `tailscaled` at TLS-handshake
  time. The wizard adds a read-only mount of the host Tailscale runtime directory to
  the machine-local `docker-compose.override.yml`; unrelated local overrides are
  preserved. Trusted on all devices in your tailnet.
- **Real domain** → Let's Encrypt via ACME (requires ports 80/443 reachable). Trusted
  everywhere.
- **IP address / `localhost`** → Caddy's internal CA (self-signed). Browsers show a
  warning you can accept; the native app connects fine.

Renewal is automatic (~30 days before expiry), so a Caddy-managed deployment never hits
an expired cert.

### `static` mode (Docker Desktop + Tailscale fallback)

On Docker Desktop (macOS/Windows) the `tailscaled` socket isn't reachable from the
Docker VM, so Caddy can't fetch a `*.ts.net` cert. There the wizard issues the cert on
the host (`tailscale cert` → `certs/server.crt`/`server.key`, mounted into Caddy) and the
Caddyfile includes a `tls /certs/...` directive. `services.py` renews it on every
`./services start --all` / `./services restart --all` if it's within 21 days of expiry — no cron needed, since
those boxes restart frequently.

## Setup via Wizard

When you run `./wizard.sh`, the wizard:
1. Asks if you want to enable HTTPS
2. Prompts for your Tailscale name, domain, or IP
3. Picks `caddy` or `static` mode automatically (based on the address and whether the
   `tailscaled` socket is available)
4. Generates the Caddyfile (and, for Caddy-managed Tailscale, the socket-mount override)
5. Updates CORS settings for HTTPS origins

**No manual certificate generation required.**

## Browser Certificate Warnings

For `*.ts.net` and real domains the certificate is publicly trusted — no warning. For an
IP or `localhost` address Caddy serves a self-signed internal-CA cert, so browsers warn:

1. Click "Advanced"
2. Click "Proceed … (unsafe)"
3. Microphone access will now work

## Troubleshooting

**HTTPS stops working after a Tailscale restart or re-login**:

Two distinct faults, and the Caddy log tells you which:

- `dial unix /var/run/tailscale/tailscaled.sock: connect: connection refused` — a
  **stale socket mount**. Bind-mounting a unix socket pins an inode, and systemd's
  `RuntimeDirectory=tailscale` deletes and recreates `/run/tailscale` on every
  `tailscaled` restart, so the container keeps a deleted socket. Chronicle now
  mounts the *directory* and installs a `RuntimeDirectoryPreserve=yes` drop-in
  (`./services doctor --install-tailscaled-dropin`, needs root) so the directory
  survives too. To recover an already-affected container, restart it — the engine
  re-applies mounts on start, so no rebuild is needed.
- `Access denied: cert access denied` — the **Tailscale operator is unset**. Caddy
  fetches the cert as container-root, which rootless Podman maps to the host user,
  so it only works when `OperatorUser` is that user. `tailscale login` silently
  clears the pref, meaning re-authenticating a node breaks TLS. Fix with
  `sudo tailscale set --operator=$USER`.

Verify with SNI, never a bare connection — Caddy serves its own internal-CA
certificate for `localhost` and returns 200 even while the real hostname is failing:

```bash
echo | openssl s_client -connect localhost:443 -servername <your-name>.ts.net \
  2>/dev/null | openssl x509 -noout -issuer -enddate     # want issuer=Let's Encrypt
```

`./services doctor` checks all of the above, and the node agent's watchdog
repairs the socket and operator faults automatically.

**HTTPS not working**:
- Check the Caddy containers are running: `docker compose ps` (look for `caddy`)
- Confirm the served cert: `echo | openssl s_client -connect localhost:443 -servername <your-name> 2>/dev/null | openssl x509 -noout -issuer -enddate`
- For Caddy-managed Tailscale, confirm the runtime directory is mounted: `docker inspect <caddy-container> --format '{{range .Mounts}}{{.Destination}} {{end}}'` should list `/var/run/tailscale`
- Check you're using `https://` not `http://`

**Tailscale cert won't issue** (`500 ... failed to create DNS record`):
- Ensure **MagicDNS** and **HTTPS Certificates** are enabled at https://login.tailscale.com/admin/dns
- These 500s are usually transient on Tailscale's side; retry after a short wait

**Microphone not accessible**:
- Ensure you're accessing via HTTPS (not HTTP)
- Accept the browser certificate warning (IP/localhost only)
- From a remote device use the Tailscale name/IP, not `localhost`
