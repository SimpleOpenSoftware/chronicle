"""Private native Pi checkpoints for read-only investigations.

The OS lock fences processes sharing Chronicle's artifact volume. A checkpoint is
one atomic record containing a complete native turn and its matching tool trace.
Cost reservations survive rollback to an earlier turn; interruption buys no budget.
"""

import fcntl
import json
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.services.inference_artifacts import _atomic_write, _root, canonical_hash


class InvestigationIncomplete(ValueError):
    def __init__(self, kind, detail, *, checkpoint=False):
        super().__init__(detail)
        self.kind, self.checkpoint = kind, checkpoint


def complete_native_prefix(data):
    """Return the last complete turn, ignoring partial writes or pending tool results."""
    lines, pending, calls, safe, safe_calls = [], set(), 0, 0, 0
    for line in data.splitlines(keepends=True):
        try:
            entry = json.loads(line)
        except ValueError:
            break
        message = entry.get("message", {})
        if message.get("role") == "assistant":
            if message.get("stopReason") in {"aborted", "error", "length"}:
                break
            pending.update(
                c["id"]
                for c in message.get("content", [])
                if c.get("type") == "toolCall"
            )
        elif message.get("role") == "toolResult":
            identifier = message.get("toolCallId")
            if identifier not in pending:
                break
            pending.remove(identifier)
            calls += 1
        lines.append(line)
        if not pending:
            safe, safe_calls = len(lines), calls
    return "".join(lines[:safe]), safe_calls


class InvestigationState:
    def __init__(self, root):
        self.root = root
        self.session_file = root / "active.jsonl"
        self.pointer = root / "checkpoint.json"
        self.ledger = root / "budget.json"
        self.cost = (
            json.loads(self.ledger.read_text())
            if self.ledger.exists()
            else {"calls": 0, "rounds": 0}
        )
        self.trace = []
        self.resumed = False
        self.events_path = root / f"events-{uuid.uuid4().hex}.jsonl"
        self._cost_lock = threading.Lock()

    def reserve(self, field):
        with self._cost_lock:
            self.cost[field] += 1
            _atomic_write(self.ledger, json.dumps(self.cost).encode())
            return self.cost[field]

    def restore(self, tools, notes, current):
        if not self.pointer.exists():
            return False
        checkpoint = json.loads(self.pointer.read_text())
        if not current(checkpoint["context"], notes):
            # Keep stale history inspectable. A fresh investigation has its own budget.
            self.pointer.rename(self.root / f"stale-{uuid.uuid4().hex}.json")
            self.ledger.rename(self.root / f"stale-budget-{uuid.uuid4().hex}.json")
            self.cost = {"calls": 0, "rounds": 0}
            return False
        tools.replay(checkpoint["trace"])
        _atomic_write(self.session_file, checkpoint["native"].encode())
        self.resumed = True
        return True

    def checkpoint(self, tools):
        if tools.result is not None or not self.session_file.exists():
            return False
        native, calls = complete_native_prefix(self.session_file.read_text())
        if not calls or calls > len(tools.trace):
            return False
        # Reconstruct only the references present at this native boundary. A later
        # gateway call may already have completed while stdout was being drained.
        restored = tools.replay_prefix(tools.trace[:calls])
        if restored.result is not None:
            return False
        record = {
            "native": native,
            "trace": tools.trace[:calls],
            "context": restored.dependencies(),
        }
        _atomic_write(self.pointer, json.dumps(record, ensure_ascii=False).encode())
        return True


@contextmanager
def own_investigation(request):
    root = _root() / "timeline-investigations" / canonical_hash(request)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "owner.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InvestigationIncomplete(
                "owned", "This investigation is already running"
            ) from exc
        try:
            yield InvestigationState(root)
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)
