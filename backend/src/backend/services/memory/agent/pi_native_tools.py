"""Bounded, run-owned bridge to Pi's public native text-tool factories.

Only text crosses this bridge. VaultTools owns path access, the Redis transaction,
validation and persistence. The helper has no model, gateway credentials or vault path.
"""

import contextlib
import json
import os
import selectors
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .edit_engine import Edit, EditError
from .text_operations import MAX_READ_CHARS

_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
_CALL_TIMEOUT = 10.0  # Below the vault lock's 60-second lease, including queue wait.


def _pi_entrypoint(binary: str) -> str:
    resolved = shutil.which(binary)
    if not resolved:
        raise EditError(f"Pi executable not found: {binary}")
    # npm's executable can be dist/cli.js or dist/bundle/cli.js. Resolve the
    # package manifest rather than importing private core/tools implementation paths.
    for parent in Path(resolved).resolve().parents:
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        package = json.loads(manifest.read_text())
        if package.get("name") == "@earendil-works/pi-coding-agent":
            if package.get("version") != "0.85.1":
                raise EditError(
                    "Chronicle native vault tools require the pinned Pi 0.85.1 package"
                )
            return (parent / package["exports"]["."]["import"]).resolve().as_uri()
    raise EditError("Cannot locate Pi's public package entrypoint from its executable")


class PiNativeTextTools:
    """One lazy helper per Pi run; serialized requests, deadline, explicit close."""

    def __init__(self, binary: str):
        self.binary = binary
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def validate_runtime(binary: str) -> None:
        _pi_entrypoint(binary)
        if not shutil.which("node"):
            raise EditError("Node is required for Pi native vault tools")

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop()

    def _request(self, request: dict) -> dict:
        payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise EditError(
                "Note exceeds the native text bridge's 32 MiB request limit"
            )
        if not self._lock.acquire(timeout=_CALL_TIMEOUT):
            raise EditError("Native text helper busy; retry the tool call")
        try:
            if self._closed:
                raise EditError("Native text helper is closed")
            if self._process is None:
                entrypoint = _pi_entrypoint(self.binary)
                node = shutil.which("node")
                if not node:
                    raise EditError("Node is required for Pi native vault tools")
                self._process = subprocess.Popen(
                    [node, str(Path(__file__).with_suffix(".mjs")), entrypoint],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    cwd=Path(__file__).parent,
                    env={
                        key: os.environ[key]
                        for key in ("PATH", "LANG", "TZ")
                        if key in os.environ
                    },
                )
                os.set_blocking(self._process.stdin.fileno(), False)
                os.set_blocking(self._process.stdout.fileno(), False)
            process = self._process
            deadline = time.monotonic() + _CALL_TIMEOUT
            response = bytearray()
            pending = memoryview(payload)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while b"\n" not in response:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(remaining):
                        if key.fileobj is process.stdin:
                            written = os.write(process.stdin.fileno(), pending)
                            pending = pending[written:]
                            if not pending:
                                selector.unregister(process.stdin)
                        else:
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                raise EOFError
                            response.extend(chunk)
                            if len(response) > _MAX_MESSAGE_BYTES:
                                raise ValueError("oversized native response")
            result = json.loads(response)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("invalid native response")
            if not result["ok"]:
                raise EditError(result.get("error", "Native text operation failed"))
            return result
        except (OSError, EOFError, ValueError, TimeoutError) as exc:
            self._stop()
            raise EditError(
                "Pi native text helper failed or timed out; no vault write was committed. Retry the tool call."
            ) from exc
        finally:
            self._lock.release()

    def edit(self, content: str, edits: list[Edit], path: str) -> str:
        if not edits:
            raise EditError(f"No edits provided for {path}.")
        for edit in edits:
            if not edit.old_text or content.count(edit.old_text) != 1:
                raise EditError(
                    f"old_text must occur exactly once in {path}; read_note or read_slice "
                    "the current text and retry with a unique exact anchor, including whitespace."
                )
        result = self._request(
            {
                "operation": "edit",
                "content": content,
                "edits": [
                    {"oldText": e.old_text, "newText": e.new_text} for e in edits
                ],
            }
        )
        if not isinstance(result.get("content"), str):
            raise EditError(
                "Pi native editor returned no text; no vault write was committed"
            )
        return result["content"]

    def read(self, content: str, path: str, offset: int, limit: int) -> str:
        result = self._request(
            {
                "operation": "read",
                "content": content,
                "offset": offset + 1,
                "limit": limit,
            }
        )["result"]
        truncation = (result.get("details") or {}).get("truncation") or {}
        lines = content.split("\n")
        start_char = sum(len(line) + 1 for line in lines[:offset])
        if truncation.get("firstLineExceedsLimit"):
            return (
                f"[Line {offset + 1} exceeds native read's 50 KiB limit. "
                f"Use read_slice(path, char_offset={start_char}) to inspect it.]"
            )
        # Remove only the native continuation suffix, using known line counts;
        # the source text itself (including lookalike notices) is left untouched.
        text = result["content"][0]["text"]
        if truncation.get("truncated"):
            body = truncation["content"]
            next_line = offset + truncation["outputLines"]
        else:
            next_line = min(offset + limit, len(lines))
            remaining = len(lines) - next_line
            suffix = f"\n\n[{remaining} more lines in file. Use offset={next_line + 1} to continue.]"
            body = text.removesuffix(suffix) if remaining else text
        if len(body) > MAX_READ_CHARS:
            return body[:MAX_READ_CHARS] + (
                f"\n\n[Truncated at {MAX_READ_CHARS} characters. Continue with "
                f"read_slice(path, char_offset={start_char + MAX_READ_CHARS}).]"
            )
        if next_line < len(lines):
            return body + f"\n\n[Continue with read_note(path, offset={next_line}).]"
        return body
