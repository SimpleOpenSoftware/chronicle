"""Durable dialogue transitions survive projection failure and duplicate delivery."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from backend.audio_contract.v2 import audio_pb2 as pb
from backend.services.interaction_modes.contracts import InteractionSession
from backend.services.interaction_modes.store import (
    VOICE_EFFECT_STREAM,
    InteractionStore,
)
from backend.services.interaction_modes.voice.runtime_journal import (
    GROUP,
    STREAM,
    VoiceJournalProjector,
    journal_entry,
)

pytestmark = pytest.mark.unit


def session(identity="dialogue"):
    now = time.time()
    return InteractionSession(
        interaction_id=identity,
        mode_id="voice_conversation",
        owner_plugin_id="chronicle_voice",
        user_id="owner",
        client_id="browser",
        audio_session_id="capture",
        capture_epoch=1,
        voice_session_id="voice",
        response_generation=1,
        response_turn_id="turn",
        response_turn_revision=0,
        phase="listening",
        plugin_state={"history": []},
        started_at=now,
        last_activity_at=now,
        idle_timeout_seconds=60,
        max_duration_seconds=1200,
    )


async def test_atomic_duplicate_turn_writes_one_effect_and_immutable_revision():
    redis = FakeRedis()
    store = InteractionStore(redis)
    assert await store.create(session())

    def accept(value):
        value.plugin_state["history"].append({"role": "user", "content": "Hello"})
        value.plugin_state["utterance_ids"] = ["committed-user-utterance"]
        value.phase = "thinking"
        return [
            pb.VoiceEffect(
                effect_id="reply",
                interaction_id=value.interaction_id,
                kind=pb.VOICE_EFFECT_KIND_RESPONSE,
                generation=1,
            )
        ]

    results = await asyncio.gather(
        *(
            store.transition("dialogue", accept, input_id="capture:turn:1")
            for _ in range(8)
        )
    )
    assert sum(applied for _, applied in results) == 1
    current = await store.get("dialogue")
    assert current.revision == 1
    assert current.plugin_state["history"] == [{"role": "user", "content": "Hello"}]
    assert await redis.xlen(VOICE_EFFECT_STREAM) == 1
    entries = [
        pb.VoiceJournalEntry.FromString(fields[b"entry"])
        for _, fields in await redis.xrange(STREAM)
    ]
    assert [entry.event_id for entry in entries] == ["dialogue:0", "dialogue:1"]
    assert not entries[0].messages
    assert not entries[1].messages
    assert list(entries[1].utterance_ids) == ["committed-user-utterance"]
    await redis.aclose()


async def test_old_session_task_completion_cannot_delete_new_active_pointer():
    redis = FakeRedis()
    store = InteractionStore(redis)
    assert await store.create(session("old"))

    def end(value):
        value.status = "ended"
        return []

    await store.transition("old", end)
    assert await store.create(session("new"))

    def complete(value):
        value.plugin_state["tasks"] = {
            "task": {
                "name": "delegate_to_hermes",
                "status": "completed",
                "result": {"answer": "Done"},
            }
        }
        return []

    await store.transition("old", complete)
    await store.delete_pointer_if_owned("owner", "browser", "old")
    assert (await store.get_active("owner", "browser")).interaction_id == "new"
    await redis.aclose()


async def test_projection_failure_retains_evidence_and_duplicate_upsert_is_idempotent():
    redis = FakeRedis()
    await InteractionStore(redis).create(session())
    await redis.xgroup_create(STREAM, GROUP, "0")
    batch = await redis.xreadgroup(GROUP, "test", {STREAM: ">"})
    identity, fields = batch[0][1][0]
    documents = {}

    async def upsert(query, update, *, upsert):
        documents.setdefault(query["_id"], update["$setOnInsert"])

    collection = type("Collection", (), {})()
    collection.update_one = AsyncMock(
        side_effect=ConnectionError("Mongo temporarily unavailable")
    )
    projector = VoiceJournalProjector(redis, collection)
    with pytest.raises(ConnectionError):
        await projector.project(identity, fields)
    assert await redis.xlen(STREAM) == 1
    assert (await redis.xpending(STREAM, GROUP))["pending"] == 1
    collection.update_one.side_effect = upsert
    await projector.project(identity, fields)
    await projector.project(identity, fields)
    assert list(documents) == ["dialogue:0"]
    assert documents["dialogue:0"]["user_id"] == "owner"
    assert documents["dialogue:0"]["recorded_at"].tzinfo is not None
    assert await redis.xlen(STREAM) == 0
    assert (await redis.xpending(STREAM, GROUP))["pending"] == 0
    await redis.aclose()


def test_journal_records_heard_cursor_and_consulted_note_revision():
    value = session()
    value.plugin_state = {
        "history": [
            {"role": "assistant", "content": "Heard part", "interrupted": True}
        ],
        "response": {"response_id": "reply", "rendered_samples": 9600},
        "tasks": {
            "lookup": {
                "name": "search_memories",
                "status": "completed",
                "arguments": {"query": "where"},
                "result": {
                    "answer": "Here",
                    "evidence": [
                        {
                            "path": "Places/Home.md",
                            "revision": "sha",
                            "text": "Excerpt",
                            "coverage": "Consulted excerpt",
                        }
                    ],
                },
            }
        },
    }
    entry = journal_entry(value)
    assert entry.rendered_samples == 9600
    assert not entry.messages
    assert entry.tasks[0].evidence[0].revision == "sha"


async def test_projector_entrypoint_retains_poison_without_starving_other_engagements():
    redis = FakeRedis()
    await redis.xadd(STREAM, {"entry": b"invalid protobuf"})
    await InteractionStore(redis).create(session())
    stored = asyncio.Event()

    async def update(*args, **kwargs):
        stored.set()

    collection = type("Collection", (), {})()
    collection.update_one = AsyncMock(side_effect=update)
    projector = VoiceJournalProjector(redis, collection)
    task = asyncio.create_task(projector.run())
    try:
        await asyncio.wait_for(stored.wait(), 1)
        await projector.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert collection.update_one.await_args.args[0] == {"_id": "dialogue:0"}
        assert (await redis.xpending(STREAM, GROUP))["pending"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.aclose()


def test_native_utterance_journal_references_exact_capture_interval_without_stt():
    value = session()
    value.plugin_state["turns"] = {
        "work": {
            "status": "queued",
            "audio_interval": {
                "turn_id": "spoken-turn",
                "turn_revision": 2,
                "start_ms": 1000,
                "end_ms": 2500,
            },
        }
    }
    entry = journal_entry(value)
    assert entry.binding.capture_session_id.value == "capture"
    assert entry.turns[0].turn_id.value == "spoken-turn"
    assert entry.turns[0].start_ms == 1000
    assert entry.turns[0].end_ms == 2500
    assert not entry.messages
