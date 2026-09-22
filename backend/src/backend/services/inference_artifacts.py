"""Durable, content-addressed records for model and agent inference runs.

The store is provider- and billing-neutral: a run may use a paid API, a subscription,
or a local model. Reusability is an explicit property of an individual operation.
Read-only deterministic runs may be cached; context-dependent mutations are retained
for observability but are never replayed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid as uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import DATA_DIR

_artifact_observer: ContextVar[list | None] = ContextVar(
    "inference_artifact_observer", default=None
)


@contextmanager
def capture_inference_artifacts():
    """Link nested inference attempts, including failed calls, to their owning job."""
    refs = []
    token = _artifact_observer.set(refs)
    try:
        yield refs
    finally:
        _artifact_observer.reset(token)


@dataclass(frozen=True)
class ReusableInferenceRun:
    """A validated cache hit plus the exact immutable record that supplied it."""

    result: Any
    request_hash: str
    artifact_hash: str


def _root() -> Path:
    return Path(os.getenv("INFERENCE_ARTIFACT_DIR", DATA_DIR / "inference_artifacts"))


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def persist_inference_run(
    *,
    operation: str,
    request: dict[str, Any],
    stdout: str,
    stderr: str,
    result: Any,
    metadata: dict[str, Any] | None = None,
    reusable: bool = False,
) -> tuple[str, str]:
    """Persist one complete inference interaction and return content hashes."""

    request_hash = canonical_hash(request)
    record = {
        "format": "chronicle-inference-artifact-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "request_hash": request_hash,
        "request": request,
        "stdout": stdout,
        "stderr": stderr,
        "result": result,
        "metadata": metadata or {},
        "reusable": reusable,
    }
    artifact_hash = canonical_hash(record)
    base = _root() / operation
    artifact_path = base / "artifacts" / f"{artifact_hash}.json.gz"
    payload = json.dumps(record, ensure_ascii=False, default=str).encode()
    _atomic_write(artifact_path, gzip.compress(payload, compresslevel=6))

    if reusable:
        pointer = json.dumps(
            {"artifact_hash": artifact_hash, "request_hash": request_hash},
            separators=(",", ":"),
        ).encode()
        _atomic_write(base / "requests" / f"{request_hash}.json", pointer)
    observer = _artifact_observer.get()
    if observer is not None:
        observer.append(
            {
                "operation": operation,
                "request_hash": request_hash,
                "artifact_hash": artifact_hash,
            }
        )
    return request_hash, artifact_hash


def promote_inference_run(
    operation: str, request_hash: str, artifact_hash: str
) -> None:
    """Make one already-persisted artifact reusable after external validation.

    Staged inference is persisted before it is trusted. The shared Timeline executor
    owns deterministic validation and calls this only after its stage barrier passes;
    the immutable artifact remains marked non-reusable and the validation decision is
    represented by this atomic request pointer.
    """

    base = _root() / operation
    artifact_path = base / "artifacts" / f"{artifact_hash}.json.gz"
    with gzip.open(artifact_path, "rt", encoding="utf-8") as stream:
        record = json.load(stream)
    if (
        record.get("operation") != operation
        or record.get("request_hash") != request_hash
    ):
        raise ValueError(
            f"Inference artifact does not match promotion: {artifact_path}"
        )
    pointer = json.dumps(
        {
            "artifact_hash": artifact_hash,
            "request_hash": request_hash,
            "validated": True,
        },
        separators=(",", ":"),
    ).encode()
    _atomic_write(base / "requests" / f"{request_hash}.json", pointer)


def invalidate_reusable_result(operation: str, request: dict[str, Any]) -> None:
    """Drop only a rejected cache pointer while retaining its immutable artifact."""

    request_hash = canonical_hash(request)
    (_root() / operation / "requests" / f"{request_hash}.json").unlink(missing_ok=True)


def load_reusable_run(
    operation: str, request: dict[str, Any]
) -> ReusableInferenceRun | None:
    """Return a validated cache hit and its content-addressed provenance."""

    request_hash = canonical_hash(request)
    base = _root() / operation
    pointer_path = base / "requests" / f"{request_hash}.json"
    if not pointer_path.is_file():
        return None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    artifact_path = base / "artifacts" / f"{pointer['artifact_hash']}.json.gz"
    with gzip.open(artifact_path, "rt", encoding="utf-8") as stream:
        record = json.load(stream)
    validated_pointer = pointer.get("validated") is True
    if record.get("request_hash") != request_hash or not (
        record.get("reusable") or validated_pointer
    ):
        raise ValueError(f"Invalid inference cache entry: {artifact_path}")
    return ReusableInferenceRun(
        result=record.get("result"),
        request_hash=request_hash,
        artifact_hash=str(pointer["artifact_hash"]),
    )


def load_reusable_result(operation: str, request: dict[str, Any]) -> Any | None:
    """Return a prior successful structured result for an identical request."""

    run = load_reusable_run(operation, request)
    return None if run is None else run.result


def read_inference_artifact(operation: str, artifact_hash: str) -> dict[str, Any]:
    """Read an immutable exchange by validated identity."""

    if not re.fullmatch(r"[a-zA-Z0-9_-]+", operation) or not re.fullmatch(
        r"[0-9a-f]{64}", artifact_hash
    ):
        raise ValueError("Invalid inference artifact identity")
    with gzip.open(
        _root() / operation / "artifacts" / f"{artifact_hash}.json.gz",
        "rt",
        encoding="utf-8",
    ) as stream:
        record = json.load(stream)
    if canonical_hash(record) != artifact_hash:
        raise ValueError("Inference artifact checksum mismatch")
    return record


def load_inference_runs(operation: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """Load the newest durable records for offline observability/optimization."""

    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    artifacts = _root() / operation / "artifacts"
    if not artifacts.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in artifacts.glob("*.json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict) or record.get("operation") != operation:
            raise ValueError(f"Invalid inference artifact: {path}")
        record["artifact_hash"] = path.name.removesuffix(".json.gz")
        records.append(record)
    records.sort(key=lambda item: str(item.get("recorded_at", "")), reverse=True)
    return records[:limit]


def delete_chat_run_artifacts(run_id: str) -> None:
    """Delete payloads belonging exclusively to one authorized chat run."""

    if str(uuid.UUID(run_id)) != run_id:
        raise ValueError("Invalid chat run identity")
    path = _root() / f"chat_run_{run_id}"
    if path.exists():
        shutil.rmtree(path)
