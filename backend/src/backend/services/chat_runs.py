"""Durable chat execution records; full exchanges use the shared artifact store.

The minimal run ledger is required before inference starts. Detailed recording is
best-effort and explicitly marked degraded. Payloads stay local until the owner
requests them, and are retained until the chat (or its run history) is deleted.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import anyio

import backend.services.inference_artifacts as inference_artifacts
from backend.observability.tracing import chronicle_span, set_span_attributes
from backend.services.inference_artifacts import (
    capture_inference_artifacts,
    persist_inference_run,
    read_inference_artifact,
)

logger = logging.getLogger(__name__)
_FINALIZE_TIMEOUT = 5
_current: ContextVar[ChatRun | None] = ContextVar("chat_run", default=None)
_parent: ContextVar[str | None] = ContextVar("chat_run_parent", default=None)


def now():
    return datetime.now(timezone.utc)


def current_run():
    return _current.get()


def utc_fields(row):
    # Motor returns naive UTC datetimes by default; JSON must carry an offset so
    # browsers in IST do not interpret database timestamps as local wall time.
    return {
        key: (
            value.replace(tzinfo=timezone.utc)
            if isinstance(value, datetime) and value.tzinfo is None
            else value
        )
        for key, value in row.items()
    }


def public_run(row):
    row = {
        k: v for k, v in row.items() if k not in {"_id", "user_id", "memory_space_id"}
    }
    row = utc_fields(row)
    lease = row.get("lease_until")
    if lease and lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    if row["status"] == "running" and lease and lease < now():
        row["status"] = "interrupted"
        row["error"] = (
            "Execution stopped reporting progress; no completed outcome was saved."
        )
    return row


class ChatRun:
    def __init__(self, db, session_id, user_id, memory_space_id):
        self.db = db
        self.id = str(uuid4())
        self.session_id = session_id
        self.user_id = user_id
        self.memory_space_id = memory_space_id
        self.degraded = False
        self.sequence = 0
        self.operation = f"chat_run_{self.id}"

    async def start(self, question=""):
        await self.db.chat_runs.insert_one(
            {
                "run_id": self.id,
                "session_id": self.session_id,
                "user_id": self.user_id,
                "memory_space_id": self.memory_space_id,
                "status": "running",
                "started_at": now(),
                "question": question[:200],
                "lease_until": now() + timedelta(seconds=90),
                "recording_degraded": False,
            }
        )

    async def update(self, **fields):
        try:
            await self.db.chat_runs.update_one({"run_id": self.id}, {"$set": fields})
        except Exception:
            self.degraded = True
            logger.exception("Run ledger update failed: %s", self.id)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(15)
            await self.update(lease_until=now() + timedelta(seconds=90))

    @contextmanager
    def activate(self):
        token = _current.set(self)
        parent = _parent.set(None)
        try:
            yield
        finally:
            _parent.reset(parent)
            _current.reset(token)

    async def artifact(self, step_id, phase, payload):
        # Snapshot before crossing a thread boundary: streaming mutates accumulators.
        payload = copy.deepcopy(payload)
        _, digest = await asyncio.to_thread(
            persist_inference_run,
            operation=self.operation,
            request={
                "run_id": self.id,
                "step_id": step_id,
                "phase": phase,
                "payload": payload,
            },
            stdout="",
            stderr="",
            result=None,
            reusable=False,
        )
        return {"operation": self.operation, "artifact_hash": digest}

    async def record_step(self, step, phase, payload):
        try:
            ref = await self.artifact(step.id, phase, payload)
            if phase == "request":
                await self.db.chat_run_steps.insert_one(
                    {
                        "run_id": self.id,
                        "step_id": step.id,
                        "parent_id": step.parent,
                        "sequence": step.sequence,
                        "kind": step.kind,
                        "name": step.name,
                        "started_at": step.started,
                        "status": "running",
                        "request": ref,
                    }
                )
            else:
                await self.db.chat_run_steps.update_one(
                    {"run_id": self.id, "step_id": step.id},
                    {
                        "$set": {
                            "status": step.status,
                            "finished_at": now(),
                            "duration_ms": round(
                                (now() - step.started).total_seconds() * 1000
                            ),
                            "response": ref,
                        }
                    },
                )
        except Exception:
            self.degraded = True
            logger.exception("Run detail recording failed: %s %s", self.id, step.id)
            await self.update(recording_degraded=True)

    async def finish(self, status, **fields):
        await self.update(
            status=status, finished_at=now(), recording_degraded=self.degraded, **fields
        )


class RunStep:
    def __init__(self, run, kind, name, request):
        self.id = str(uuid4())
        self.parent = _parent.get()
        self.kind, self.name = kind, name
        self.started = now()
        self.status = "succeeded"
        self.output = None
        self.request = request
        self.sequence = 0
        if run:
            run.sequence += 1
            self.sequence = run.sequence


@asynccontextmanager
async def run_step(kind, name, request=None):
    """One shared seam for tool and model attempts, including nested retrieval."""
    run = current_run()
    step = RunStep(run, kind, name, request)
    if not run:
        yield step
        return
    await run.record_step(step, "request", request)
    token = _parent.set(step.id)
    with capture_inference_artifacts() as nested_artifacts, chronicle_span(
        f"chat.{kind}",
        attributes={
            "chronicle.run_id": run.id if run else None,
            "chronicle.step_id": step.id,
            "chronicle.step.name": name,
        },
    ) as span:
        try:
            yield step
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                step.status = "cancelled"
            elif step.status != "incomplete":
                step.status = "failed"
            step.output = {
                "partial": step.output,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            raise
        finally:
            set_span_attributes(span, {"chronicle.outcome": step.status})
            _parent.reset(token)
            with anyio.move_on_after(_FINALIZE_TIMEOUT, shield=True) as cleanup:
                if run:
                    children = []
                    for ref in nested_artifacts:
                        if ref["operation"] != run.operation:
                            try:
                                record = await asyncio.to_thread(
                                    read_inference_artifact,
                                    ref["operation"],
                                    ref["artifact_hash"],
                                )
                                children.append({**ref, "record": record})
                            except (OSError, ValueError):
                                run.degraded = True
                                children.append({**ref, "unavailable": True})
                    await run.record_step(
                        step,
                        "response",
                        {
                            "output": step.output,
                            "child_artifacts": children,
                        },
                    )
            if cleanup.cancel_called:
                run.degraded = True
                logger.error(
                    "Run detail finalization timed out: %s %s", run.id, step.id
                )
                with anyio.move_on_after(1, shield=True):
                    await run.update(recording_degraded=True)


async def run_detail(db, row):
    """Caller must authorize the owning chat before passing this run record."""
    result = public_run(row)
    steps = (
        await db.chat_run_steps.find({"run_id": row["run_id"]}, {"_id": 0})
        .sort("sequence", 1)
        .to_list(length=None)
    )
    steps = [utc_fields(step) for step in steps]
    for step in steps:
        if step["status"] == "running" and result["status"] != "running":
            step["status"] = "recording_incomplete"
            result["recording_degraded"] = True
        for phase in ("request", "response"):
            ref = step.get(phase)
            if ref:
                try:
                    artifact = await asyncio.to_thread(read_inference_artifact, **ref)
                    step[f"{phase}_payload"] = artifact["request"]["payload"]
                except (OSError, ValueError):
                    step[f"{phase}_unavailable"] = True
                    result["recording_degraded"] = True
    result["steps"] = steps
    return result


async def delete_runs(db, session_id, user_id):

    rows = await db.chat_runs.find(
        {"session_id": session_id, "user_id": user_id}
    ).to_list(length=None)
    for row in rows:
        if public_run(row)["status"] == "running":
            raise ValueError(
                "Wait for the active chat run to finish before deleting its history."
            )
    for row in rows:
        await asyncio.to_thread(
            inference_artifacts.delete_chat_run_artifacts, row["run_id"]
        )
        await db.chat_run_steps.delete_many({"run_id": row["run_id"]})
        await db.chat_messages.update_many(
            {
                "session_id": session_id,
                "user_id": user_id,
                "metadata.run_id": row["run_id"],
            },
            {"$unset": {"metadata.run_id": ""}},
        )
        await db.chat_runs.delete_one({"run_id": row["run_id"]})
