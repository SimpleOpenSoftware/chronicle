"""Real-browser Audio V2 acceptance test against a running Chronicle deployment.

Run with an explicitly non-sensitive 16 kHz mono WAV fixture:
  uv run --with playwright python scripts/live_audio_v2_e2e.py \
    --base https://chronicle.example.com --audio /tmp/smallest-official-16k.wav
"""

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright


def _env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _kind(payload: str) -> str:
    body = json.loads(payload)
    ignored = {"event_id", "sent_at"}
    return next((key for key in body if key not in ignored), "unknown")


def _value(body: dict, *path: str) -> str | None:
    value = body
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value else None


def _conversations(response) -> list[dict]:
    payload = response.json()
    return payload if isinstance(payload, list) else payload.get("conversations", [])


def _conversation_id(conversation: dict) -> str | None:
    return conversation.get("id") or conversation.get("conversation_id")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument(
        "--base", required=True, help="Chronicle deployment URL to test."
    )
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument(
        "--expect-wakeword",
        choices=("hey_hermes", "hermes"),
        help="Arm a side-effect-free production detector probe on this capture.",
    )
    parser.add_argument(
        "--expect-conversation",
        action="store_true",
        help="Require the capture to materialize through the public Conversations API.",
    )
    parser.add_argument(
        "--conversation-timeout",
        type=int,
        default=90,
        help="Seconds to wait for asynchronous Conversation materialization.",
    )
    parser.add_argument(
        "--env", type=Path, default=Path(__file__).resolve().parents[2] / ".env"
    )
    args = parser.parse_args()
    if not args.audio.is_file():
        raise SystemExit(f"audio fixture does not exist: {args.audio}")

    credentials = _env(args.env)
    trace = {
        "binary_sent": 0,
        "controls_sent": [],
        "controls_received": [],
        "console_errors": [],
        "page_errors": [],
        "socket_closed": False,
        "client_id": None,
        "capture_session_id": None,
        "wake_probe": None,
        "conversation": None,
    }

    with sync_playwright() as playwright:
        api = playwright.request.new_context(ignore_https_errors=True)
        response = api.post(
            f"{args.base}/auth/jwt/login",
            form={
                "username": credentials["ADMIN_EMAIL"],
                "password": credentials["ADMIN_PASSWORD"],
            },
        )
        if not response.ok:
            raise RuntimeError(f"authentication failed: HTTP {response.status}")
        token = response.json()["access_token"]
        baseline_conversation_ids: set[str] = set()
        if args.expect_conversation:
            baseline_response = api.get(
                f"{args.base}/api/conversations?include_unprocessed=true&limit=50",
                headers={"Authorization": f"Bearer {token}"},
            )
            if not baseline_response.ok:
                raise AssertionError(
                    "conversation baseline read failed: "
                    f"HTTP {baseline_response.status}"
                )
            baseline_conversation_ids = {
                conversation_id
                for item in _conversations(baseline_response)
                if (conversation_id := _conversation_id(item)) is not None
            }

        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                f"--use-file-for-fake-audio-capture={args.audio.resolve()}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True, permissions=["microphone"]
        )
        page = context.new_page()
        page.goto(args.base, wait_until="domcontentloaded")
        page.evaluate("token => localStorage.setItem('root_token', token)", token)

        def on_websocket(socket) -> None:
            if "/ws/audio" not in socket.url:
                return

            def sent(payload) -> None:
                if isinstance(payload, bytes):
                    trace["binary_sent"] += 1
                else:
                    trace["controls_sent"].append(_kind(payload))

            def received(payload) -> None:
                if isinstance(payload, str):
                    body = json.loads(payload)
                    kind = _kind(payload)
                    trace["controls_received"].append(kind)
                    if kind == "hello":
                        trace["client_id"] = _value(
                            body, "hello", "clientId", "value"
                        ) or _value(body, "hello", "client_id", "value")
                    elif kind == "capture_started":
                        trace["capture_session_id"] = _value(
                            body,
                            "captureStarted",
                            "binding",
                            "captureSessionId",
                            "value",
                        ) or _value(
                            body,
                            "capture_started",
                            "binding",
                            "capture_session_id",
                            "value",
                        )

            socket.on("framesent", sent)
            socket.on("framereceived", received)
            socket.on("close", lambda: trace.__setitem__("socket_closed", True))

        page.on("websocket", on_websocket)
        page.on(
            "console",
            lambda message: (
                trace["console_errors"].append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: trace["page_errors"].append(str(error)))

        page.goto(f"{args.base}/live-record", wait_until="domcontentloaded")
        page.get_by_text("Ready to Record", exact=True).wait_for(timeout=15_000)
        button = page.locator("button.w-24.h-24")
        button.click()
        page.get_by_text("Recording in Progress", exact=True).wait_for(timeout=15_000)
        if args.expect_wakeword:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (
                trace["client_id"] and trace["capture_session_id"]
            ):
                page.wait_for_timeout(50)
            if not trace["client_id"] or not trace["capture_session_id"]:
                raise AssertionError(
                    "capture binding was not observed before wake probe"
                )
            probe_deadline = time.monotonic() + 8
            while True:
                probe_response = api.post(
                    f"{args.base}/api/wakeword/probes",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "client_id": trace["client_id"],
                        "audio_session_id": trace["capture_session_id"],
                        "wakeword": args.expect_wakeword,
                        "timeout_seconds": max(args.seconds, 15),
                    },
                )
                if probe_response.ok or time.monotonic() >= probe_deadline:
                    break
                if probe_response.status != 404:
                    break
                page.wait_for_timeout(100)
            if not probe_response.ok:
                raise AssertionError(
                    f"wake probe start failed: HTTP {probe_response.status} "
                    f"{probe_response.text()}"
                )
            trace["wake_probe"] = probe_response.json()
        page.wait_for_timeout(args.seconds * 1000)
        page.get_by_text("Listening…", exact=True).wait_for(
            state="detached", timeout=15_000
        )
        button.click()
        page.get_by_text("Ready to Record", exact=True).wait_for(timeout=15_000)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            received = Counter(trace["controls_received"])
            if received["capture_stopped"] == 1 and trace["socket_closed"]:
                break
            page.wait_for_timeout(100)

        trace["controls_sent"] = dict(Counter(trace["controls_sent"]))
        trace["controls_received"] = dict(Counter(trace["controls_received"]))
        if args.expect_wakeword:
            probe_id = trace["wake_probe"]["probe_id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                probe_response = api.get(
                    f"{args.base}/api/wakeword/probes/{probe_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if not probe_response.ok:
                    raise AssertionError(
                        f"wake probe read failed: HTTP {probe_response.status}"
                    )
                trace["wake_probe"] = probe_response.json()
                if trace["wake_probe"]["status"] != "listening":
                    break
                page.wait_for_timeout(100)
        if args.expect_conversation:
            deadline = time.monotonic() + args.conversation_timeout
            while time.monotonic() < deadline:
                conversations_response = api.get(
                    f"{args.base}/api/conversations?include_unprocessed=true&limit=50",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if not conversations_response.ok:
                    raise AssertionError(
                        "conversation read failed: "
                        f"HTTP {conversations_response.status}"
                    )
                conversation = next(
                    (
                        item
                        for item in _conversations(conversations_response)
                        if _conversation_id(item) not in baseline_conversation_ids
                    ),
                    None,
                )
                if conversation is not None:
                    trace["conversation"] = {
                        "id": _conversation_id(conversation),
                        "audio_ranges": conversation.get("audio_ranges", []),
                        "active_transcript_version": conversation.get(
                            "active_transcript_version"
                        ),
                    }
                    break
                page.wait_for_timeout(250)
        browser.close()

    accepted = trace["controls_received"].get("capture_packet_accepted", 0)
    transcripts = trace["controls_received"].get("transcript_update", 0)
    print(json.dumps(trace, indent=2))
    assert trace["binary_sent"] > 0
    assert accepted == trace["binary_sent"]
    assert transcripts > 0
    assert trace["controls_received"].get("capture_stopped") == 1
    assert trace["socket_closed"] is True
    assert trace["console_errors"] == []
    assert trace["page_errors"] == []
    if args.expect_wakeword:
        assert trace["wake_probe"]["status"] == "detected"
        assert trace["wake_probe"]["detection"]["wakeword"] == args.expect_wakeword
    if args.expect_conversation:
        assert trace["conversation"] is not None


if __name__ == "__main__":
    main()
