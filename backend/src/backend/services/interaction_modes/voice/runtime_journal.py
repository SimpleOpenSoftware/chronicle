"""Immutable typed dialogue outbox and idempotent Mongo evidence projection."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from redis.exceptions import ResponseError

from backend.audio_contract.v2 import audio_pb2 as pb

STREAM = "interaction:voice:journal"
GROUP = "voice-journal"
LOGGER = logging.getLogger(__name__)


def journal_entry(session):
    state = session.plugin_state
    entry = pb.VoiceJournalEntry(
        event_id=f"{session.interaction_id}:{session.revision}",
        interaction_id=session.interaction_id,
        thread_id=state.get("thread_id", ""),
        utterance_ids=state.get("utterance_ids", []),
        revision=session.revision,
        user_id=session.user_id,
        client_id=session.client_id,
        binding=pb.CaptureBinding(
            capture_session_id=pb.CaptureSessionId(value=session.audio_session_id),
            voice_session_id=pb.VoiceSessionId(value=session.voice_session_id or ""),
            capture_epoch=session.capture_epoch,
        ),
        memory_space_id=state.get("memory_space_id") or "",
        phase=session.phase,
        status=session.status,
        response_id=pb.ResponseId(
            value=state.get("response", {}).get("response_id", "")
        ),
        rendered_samples=state.get("response", {}).get("rendered_samples", 0),
    )
    entry.recorded_at.FromDatetime(datetime.now(timezone.utc))
    # Dialogue owns transcript content. The journal retains execution/delivery evidence.
    for identity, task in state.get("tasks", {}).items():
        result = task.get("result") or {}
        args = task.get("arguments") or {}
        item = entry.tasks.add(
            task_id=identity,
            name=task["name"],
            status=task["status"],
            request=str(args.get("query") or args.get("request") or ""),
            answer=str(result.get("answer") or ""),
            remote_run_id=str(task.get("state", {}).get("remote_run_id") or ""),
        )
        for evidence in result.get("evidence", []):
            item.evidence.add(
                path=evidence["path"],
                revision=evidence["revision"],
                text=evidence["text"],
                coverage=evidence["coverage"],
            )
    for identity, turn in state.get("turns", {}).items():
        interval = turn.get("audio_interval")
        if interval:
            entry.turns.add(
                work_id=identity,
                turn_id=pb.TurnId(value=interval.get("turn_id") or identity),
                turn_revision=interval["turn_revision"],
                start_ms=interval["start_ms"],
                end_ms=interval["end_ms"],
                status=turn["status"],
            )
    return entry


class VoiceJournalProjector:
    def __init__(self, redis, collection, *, consumer="voice-journal-worker"):
        self.redis, self.collection, self.consumer = redis, collection, consumer
        self.running = False

    async def project(self, message_id, fields):
        entry = pb.VoiceJournalEntry()
        entry.ParseFromString(fields.get(b"entry", fields.get("entry", b"")))
        if not entry.event_id or not entry.user_id or not entry.interaction_id:
            raise ValueError("invalid voice journal identity")
        document = MessageToDict(entry, preserving_proto_field_name=True)
        document["recorded_at"] = entry.recorded_at.ToDatetime(tzinfo=timezone.utc)
        await self.collection.update_one(
            {"_id": entry.event_id}, {"$setOnInsert": document}, upsert=True
        )
        # Mongo is now authoritative. A crash between ACK and deletion must not
        # orphan a transcript entry outside both new and pending consumption.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(STREAM, GROUP, message_id)
            pipe.xdel(STREAM, message_id)
            await pipe.execute()

    async def _project_entries(self, entries):
        for identity, fields in entries:
            try:
                await self.project(identity, fields)
            except (ValueError, DecodeError):
                # Keep malformed evidence pending for inspection, while valid
                # entries from other engagements continue to reach Mongo.
                LOGGER.exception("Malformed voice journal entry %s retained", identity)

    async def run(self):
        try:
            await self.redis.xgroup_create(STREAM, GROUP, "0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self.running = True
        while self.running:
            try:
                pending = await self.redis.xautoclaim(
                    STREAM, GROUP, self.consumer, 30000, start_id="0-0", count=50
                )
                await self._project_entries(pending[1])
                entries = await self.redis.xreadgroup(
                    GROUP, self.consumer, {STREAM: ">"}, count=50, block=1000
                )
                for _, batch in entries:
                    await self._project_entries(batch)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Voice journal projection failed; unacknowledged evidence retained"
                )
                await asyncio.sleep(1)

    async def stop(self):
        self.running = False
