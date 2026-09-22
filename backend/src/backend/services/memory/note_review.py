"""Shared review staging, diffs, and crash-safe application of approved note edits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Iterable, Mapping, Optional

from backend.models.timeline import PotentialMemoryChange
from backend.services.memory.vault_lock import vault_run_lock


class ReviewConflict(ValueError):
    pass


def _hash(text: Optional[str]) -> Optional[str]:
    return (
        hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None
    )


def _atomic_write(target: Path, content: bytes) -> None:
    """Durably publish one file; shared by vault mutations and recovery artifacts."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{uuid.uuid4()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(
                temporary, target.stat().st_mode & 0o777 if target.exists() else 0o600
            )
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
        if path.is_file() and not path.is_symlink()
    }


def _summary(before: Optional[str], after: Optional[str]) -> str:
    if before is None:
        return f"Create {len((after or '').splitlines())} lines"
    if after is None:
        return f"Delete {len(before.splitlines())} lines"
    before_lines, after_lines = before.splitlines(), after.splitlines()
    return f"Update {len(before_lines)} → {len(after_lines)} lines"


def build_potential_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    source_episode_keys_by_path: Optional[Mapping[str, Iterable[str]]] = None,
) -> list[PotentialMemoryChange]:
    """Return a stable, reviewable vault diff."""

    changes: list[PotentialMemoryChange] = []
    for note_path in sorted(set(before) | set(after)):
        old, new = before.get(note_path), after.get(note_path)
        if old == new:
            continue
        operation = "create" if old is None else "delete" if new is None else "update"
        changes.append(
            PotentialMemoryChange(
                note_path=note_path,
                operation=operation,
                before_hash=_hash(old),
                after_hash=_hash(new),
                before_text=old,
                after_text=new,
                summary=_summary(old, new),
                source_episode_keys=list(
                    dict.fromkeys(
                        (source_episode_keys_by_path or {}).get(note_path, ())
                    )
                ),
            )
        )
    return changes


def safe_note(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ReviewConflict("Unsafe proposed note path")
    target = root / path
    if root.resolve() not in target.resolve().parents or any(
        p.is_symlink() for p in [target, *target.parents] if p != root.parent
    ):
        raise ReviewConflict("Proposed note escapes the vault or uses a symlink")
    return target


async def await_vault_commit(function, *args):
    """Retain the caller's publication lock until its filesystem thread stops.

    Cancelling asyncio.to_thread does not stop the thread. Propagate cancellation
    only after it has finished, so a subsequent privacy update cannot overtake a
    still-running write. The application journal handles partial-write recovery.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        # Retrieve a possible thread exception before propagating cancellation.
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


def apply_changes(
    root: Path,
    user_id: str,
    changes: list,
    selected_ids: list[str],
    journal: Path,
    expected: dict | None = None,
) -> list[str]:
    selected = [c for c in changes if c.change_id in selected_ids]
    if set(selected_ids) != {c.change_id for c in selected}:
        raise ReviewConflict("The selected changes do not belong to this proposal")
    with vault_run_lock(user_id):
        recorded = json.loads(journal.read_text()) if journal.exists() else None
        if recorded and set(recorded["accepted"]) != set(selected_ids):
            raise ReviewConflict(
                "An application already started with a different selection"
            )
        completed = set(recorded["completed"]) if recorded else set()
        if recorded and completed == set(selected_ids):
            return sorted(completed)
        current = _snapshot(root)
        restored = dict(current)
        for change in selected:
            safe_note(root, change.note_path)
            actual = _hash(current.get(change.note_path))
            if recorded and actual == change.after_hash:
                completed.add(change.change_id)
                if (
                    expected is None
                    or expected.get(change.note_path) == change.before_text
                ):
                    if change.before_text is None:
                        restored.pop(change.note_path, None)
                    else:
                        restored[change.note_path] = change.before_text
            elif actual != change.before_hash:
                raise ReviewConflict(
                    f"{change.note_path} changed. Generate a fresh preview."
                )
        if expected is not None and restored != expected:
            raise ReviewConflict("Accepted vault changed after freshness validation")

        def persist():
            _atomic_write(
                journal,
                json.dumps(
                    {"accepted": selected_ids, "completed": sorted(completed)}
                ).encode(),
            )

        persist()
        for change in selected:
            if change.change_id in completed:
                continue
            target = safe_note(root, change.note_path)
            if change.after_text is None:
                target.unlink(missing_ok=True)
                if target.parent.exists():
                    fd = os.open(target.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            else:
                _atomic_write(target, change.after_text.encode())
            completed.add(change.change_id)
            persist()
        return sorted(completed)
