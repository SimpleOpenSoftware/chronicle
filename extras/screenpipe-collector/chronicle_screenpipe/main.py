from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
from pathlib import Path

import chronicle_screenpipe.privacy_history as privacy_history
import chronicle_screenpipe.privacy_inventory as privacy_inventory
import httpx

from .collector import Collector, Config


def _shutdown_signal(_signum, _frame) -> None:
    raise KeyboardInterrupt


def config_dir() -> Path:
    return (
        Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "chronicle-screenpipe"
    )


def state_dir() -> Path:
    return (
        Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "chronicle-screenpipe"
    )


def load_config() -> Config:
    raw = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
    raw["screenpipe_dir"] = Path(raw["screenpipe_dir"]).expanduser()
    return Config(**raw)


def pair(args: argparse.Namespace) -> None:
    response = httpx.post(
        f"{args.backend.rstrip('/')}/api/device-input/pair",
        json={
            "code": args.code,
            "name": args.name or platform.node(),
            "platform": platform.system().lower(),
            "provider": "screenpipe",
            "capabilities": ["audio", "observation", "screen_context"],
        },
        timeout=30,
    )
    response.raise_for_status()
    paired = response.json()
    target = config_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / "config.json"
    path.write_text(
        json.dumps(
            {
                "backend_url": args.backend,
                "source_id": paired["source_id"],
                "token": paired["token"],
                "screenpipe_dir": str(Path(args.screenpipe_dir).expanduser()),
                "screenpipe_url": args.screenpipe_url,
                "screenpipe_token": args.screenpipe_token,
                "forward_audio": args.forward_audio,
                "meeting_detection": args.meeting_detection,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    print(f"paired {paired['source_id']}; configuration saved to {path}")


def install_service() -> None:
    # Unit definition lives in the repo-root clients.py (shared with the
    # unified tray, `services.py client`, and the node agent's update path).
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    # Imported after the sys.path insert above, per the comment there.
    import clients

    clients.install_component("screenpipe-collector")
    print("installed and started the chronicle-screenpipe user service")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="Chronicle companion for ScreenPipe")
    sub = parser.add_subparsers(dest="command", required=True)
    pair_parser = sub.add_parser("pair")
    pair_parser.add_argument("--backend", required=True)
    pair_parser.add_argument("--code", required=True)
    pair_parser.add_argument("--name")
    pair_parser.add_argument("--screenpipe-dir", default="~/.screenpipe")
    pair_parser.add_argument("--screenpipe-url", default="http://127.0.0.1:3030")
    pair_parser.add_argument(
        "--screenpipe-token",
        default=os.getenv("SCREENPIPE_API_KEY"),
        help="token used by ScreenPipe's authenticated local API",
    )
    pair_parser.add_argument(
        "--forward-audio",
        choices=("none", "output", "input", "both"),
        default="both",
        help="which locally captured ScreenPipe audio sources Chronicle receives",
    )
    pair_parser.add_argument(
        "--meeting-detection",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="detect active calls from the PipeWire graph and tag forwarded audio",
    )
    sub.add_parser("run")
    history = sub.add_parser(
        "screen-history", help="hold a historical range and queue local screening"
    )
    history.add_argument(
        "--start", required=True, help="inclusive ISO timestamp with timezone"
    )
    history.add_argument(
        "--end", required=True, help="exclusive ISO timestamp with timezone"
    )
    inventory = sub.add_parser(
        "refine-screen-inventory", help="queue original historical display inventories"
    )
    inventory.add_argument("--start", required=True)
    inventory.add_argument("--end", required=True)
    inventory.add_argument("--original-policy-version", required=True)
    recheck = sub.add_parser(
        "recheck-screen-gaps", help="recheck earlier sampling-gap holds locally"
    )
    recheck.add_argument("--start", required=True)
    recheck.add_argument("--end", required=True)
    sub.add_parser("install-service")
    args = parser.parse_args()
    if args.command == "pair":
        pair(args)
    elif args.command == "run":
        signal.signal(signal.SIGTERM, _shutdown_signal)
        Collector(load_config(), state_dir()).run()
    elif args.command == "screen-history":

        print(
            json.dumps(
                privacy_history.queue_history(
                    load_config(), state_dir(), args.start, args.end
                )
            )
        )
    elif args.command == "recheck-screen-gaps":

        print(
            json.dumps(
                privacy_history.queue_gap_rechecks(
                    load_config(), state_dir(), args.start, args.end
                )
            )
        )
    elif args.command == "refine-screen-inventory":

        print(
            json.dumps(
                privacy_inventory.queue_inventory(
                    load_config(),
                    state_dir(),
                    args.start,
                    args.end,
                    args.original_policy_version,
                )
            )
        )
    else:
        install_service()


if __name__ == "__main__":
    main()
