# Chronicle Tray

The one desktop tray for a Chronicle client machine (macOS menu bar / Linux
system tray). It replaces the separate vault-sync and wearable menu bar apps
with a single icon whose menu shows only what this machine can do:

| Section | What it does | Shown when |
|---------|--------------|------------|
| **Vault Sync** | Syncs your Obsidian vault with the server via a private Syncthing (config in the repository-root `.env`) | `syncthing` binary installed |
| **ScreenPipe** | Local capture stats, start/stop/restart for `screenpipe.service` and the Chronicle collector, a **Capture** submenu (audio/video master switches) and a **Capture settings…** dialog for per-source record/forward choices | `screenpipe` installed or a local DB exists |
| **Pendant** | Scan/connect/stream from BLE wearables (OMI, Neo1, Friend; config in `extras/local-wearable-client/`) | installed with `--pendant` |

Unavailable sections show a disabled hint line with the install command instead
of vanishing.

The ScreenPipe line also reports the active recorder owner. Chronicle disables the
ScreenPipe desktop app's login autostart when installing its recorder, while leaving
the app manually launchable as an external-server viewer. Opening the viewer leaves
Chronicle's recorder and its controls running because the viewer reads Timeline data
from that recorder on port 3030. An unknown process on port 3030 is reported as a
conflict instead of appearing as an inactive recorder.

## Run / install

```bash
cd extras/chronicle-tray
uv run chronicle-tray                    # foreground (default: run)
uv run chronicle-tray install            # login service (launchd / systemd user unit)
uv run chronicle-tray install --pendant  # + BLE wearable streaming deps
uv run chronicle-tray status|restart|logs|uninstall
```

Installing the tray removes the superseded `chronicle-desktop.service` /
`com.chronicle.vault-sync` units (two trays would fight over the same private
Syncthing). Vault sync reads `BACKEND_URL` and `CHRONICLE_API_KEY` from the
repository-root `.env`.

The service unit is defined in the repo-root `clients.py` (shared with
`./services client` and the node agent), and runs `uv run` from this checkout,
so a node update (`./services update` or the WebUI update button) restarts the
tray on the new code automatically.

## Section internals

The tray reuses the sibling projects' logic in place rather than duplicating it:
vault sync from the `chronicle-vault-sync` package and pendant BLE from the
`chronicle-wearable` package, both declared as path dependencies. Their state
directories and backend pairing flows are reused unchanged.
