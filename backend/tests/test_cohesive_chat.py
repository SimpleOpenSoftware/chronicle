"""The new chat interface, evidence contract, and approved save lifecycle."""

import copy
import json
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import chat_service
from backend.auth import current_active_user
from backend.routers.modules import chat_routes
from backend.services import chat_context as context
from backend.services import chat_review as review
from backend.services.chat_sources import (
    ChatSourceContext,
    ChatSourceRef,
    SourcePassage,
)
from backend.services.memory import note_review


def matches(row, query):
    for key, value in query.items():
        if key == "$or":
            if not any(matches(row, child) for child in value):
                return False
        elif isinstance(value, dict) and "$in" in value:
            if row.get(key) not in value["$in"]:
                return False
        elif row.get(key) != value:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, key, direction=None):
        entries = key if isinstance(key, list) else [(key, direction)]
        for field, order in reversed(entries):
            self.rows.sort(key=lambda r: r.get(field, 0), reverse=order < 0)
        return self

    def limit(self, amount):
        self.rows = self.rows[:amount] if amount else self.rows
        return self

    def skip(self, amount):
        self.rows = self.rows[amount:]
        return self

    async def to_list(self, length=None):
        return copy.deepcopy(self.rows[:length])

    def __aiter__(self):
        async def iterator():
            for row in self.rows:
                yield copy.deepcopy(row)

        return iterator()


class Collection:
    def __init__(self):
        self.rows = []

    def find(self, query, *args):
        return Cursor([r for r in self.rows if matches(r, query)])

    async def find_one(self, query, sort=None):
        cursor = self.find(query)
        if sort:
            cursor.sort(*sort[0])
        return copy.deepcopy(cursor.rows[0]) if cursor.rows else None

    async def insert_one(self, row):
        self.rows.append(copy.deepcopy(row))

    async def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if matches(row, query):
                row.update(copy.deepcopy(update.get("$set", {})))
                for key, value in update.get("$inc", {}).items():
                    row[key] = row.get(key, 0) + value
                return NS(modified_count=1, upserted_id=None)
        if upsert:
            row = {**query, **copy.deepcopy(update.get("$setOnInsert", {}))}
            self.rows.append(row)
            return NS(modified_count=0, upserted_id=row.get("_id"))
        return NS(modified_count=0, upserted_id=None)

    async def find_one_and_update(self, query, update, **kwargs):
        await self.update_one(query, update)
        return await self.find_one(query)


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


def source(key, size=100):
    return ChatSourceContext(
        ref=ChatSourceRef(kind="recording", key=key),
        title=key,
        url="/recordings/" + key,
        started_at=None,
        revision="revision-" + key,
        passages=[
            SourcePassage(
                id="S1",
                text="A" * size,
                url="/recordings/" + key,
                label=key,
                revision="revision-" + key,
            )
        ],
        coverage="All available source passages included.",
        total_passages=1,
    )


@pytest.fixture
def svc(monkeypatch, tmp_path):
    cs = chat_service.ChatService()
    cs._initialized = True
    cs.sessions_collection = Collection()
    cs.messages_collection = Collection()
    cs.db = NS(
        chat_save_proposals=Collection(),
        chat_runs=Collection(),
        chat_run_steps=Collection(),
    )
    from mongomock_motor import AsyncMongoMockClient

    dialogue_db = AsyncMongoMockClient().dialogue_test
    cs.db.dialogue_state = dialogue_db.dialogue_state
    cs.db.dialogue_interpretations = dialogue_db.dialogue_interpretations
    cs._get_tool_mode_system_prompt = AsyncMock(return_value="system")
    cs.memory_service = NS(_run_agent_with_note_guarantee=AsyncMock())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    for module in [chat_service, chat_routes, review]:
        monkeypatch.setattr(module, "distributed_lock", unlocked)
    monkeypatch.setattr(note_review, "vault_run_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review, "vault_run_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review, "service", AsyncMock(return_value=cs))
    monkeypatch.setattr(chat_routes, "get_chat_service", lambda: cs)
    monkeypatch.setattr(review, "record_vault_change", AsyncMock())
    monkeypatch.setattr(review, "dispatch_saved", AsyncMock())
    return cs


async def test_multisource_budget_namespaced_grouped_citations_and_scope(monkeypatch):
    resolve = AsyncMock(
        side_effect=lambda ref, uid, space: source(
            ref.key, 100 if ref.key == "short" else 25000
        )
    )
    monkeypatch.setattr(context, "resolve_source", resolve)
    refs = [ChatSourceRef(kind="recording", key=k) for k in ["short", "long"]]
    full = await context.resolve_context(refs + refs, "owner", "space")
    assert len(full.sources) == 2
    assert resolve.await_args_list[0].args[1:] == ("owner", "space")
    turn = full.for_turn("question")
    assert sum(len(p.text) for p in turn.passages) <= 32000
    assert len(turn.passages) == 2  # short source's unused capacity redistributed
    assert len({p.id for p in turn.passages}) == 2
    answer = "[" + ", ".join(p.id for p in turn.passages) + "]"
    retained = turn.evidence(answer, [], [])
    assert sum(len(c["passages"]) for c in retained["conversations"]) == 2
    with pytest.raises(ValueError):
        full.read("unattached")
    read = full.read(context.source_id(refs[0]))
    assert read["passages"]


async def test_new_session_history_readonly_attachment_omission_and_empty(
    svc, monkeypatch
):
    monkeypatch.setattr(
        context,
        "resolve_source",
        AsyncMock(side_effect=lambda ref, *_: source(ref.key)),
    )
    app = FastAPI()
    app.include_router(chat_routes.router)
    app.dependency_overrides[current_active_user] = lambda: NS(id="owner")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/chat/sessions", json={"sources": [{"kind": "recording", "key": "one"}]}
        )
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        url = "/chat/sessions/" + sid
        assert created.json()["interaction_version"] == 2
        renamed = await client.put(url, json={"title": "New title"})
        assert len(renamed.json()["sources"]) == 1
        removed = await client.put(url, json={"sources": []})
        assert (
            removed.json()["sources"] == []
            and len(removed.json()["context_changes"]) == 1
        )
        assert (await client.put(url, json={"sources": None})).status_code == 422
        assert (
            await client.post(
                "/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "memory_limit": 5,
                },
            )
        ).status_code == 422
        svc.sessions_collection.rows[0][
            "metadata"
        ] = {}  # historical data remains unmodified by requests
        for method, suffix, body in [
            ("post", "/extract-memories", {}),
            ("put", "", {"sources": []}),
        ]:
            assert (
                await getattr(client, method)(url + suffix, json=body)
            ).status_code == 409
        assert (
            await client.post(
                "/chat/completions",
                json={
                    "session_id": sid,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        ).status_code == 409
        assert (await client.get(url)).status_code == 200
        app.dependency_overrides[current_active_user] = lambda: NS(id="other")
        assert (await client.get(url + "/sources")).status_code == 404


async def complete_chat(svc):
    session = await svc.create_session("owner")
    for role, text in [
        ("user", "I will take a walk Friday."),
        ("assistant", "You could also try running; that is a suggestion."),
    ]:
        await svc.add_message(
            chat_service.ChatMessage(
                message_id=str(len(svc.messages_collection.rows)),
                session_id=session.session_id,
                user_id="owner",
                role=role,
                content=text,
            )
        )
    return session.session_id


async def test_queue_stages_whole_chat_then_applies_selected_with_audit(
    svc, monkeypatch, tmp_path
):
    sid = await complete_chat(svc)
    root = tmp_path / "conversation_docs" / "owner"
    root.mkdir(parents=True)
    (root / "Existing.md").write_text("Before")

    async def writer(_cls, stage, transcript, source_id, **kwargs):
        assert "assistant" in transcript and "suggestion" in transcript
        assert "NOT user commitments" in kwargs["guidance"]
        (stage / "Existing.md").write_text("After")
        (stage / "New.md").write_text("Walk Friday")
        return NS(
            summary="draft",
            touched=["Existing.md", "New.md"],
            errors=["read_note: initially missing, then created successfully"],
            truncated=False,
            stalled=False,
        )

    svc.memory_service._run_agent_with_note_guarantee.side_effect = writer
    proposal = await review.create_proposal(sid, "owner")
    await svc.add_message(
        chat_service.ChatMessage(
            message_id="later",
            session_id=sid,
            user_id="owner",
            role="user",
            content="This message came after the snapshot",
        )
    )
    assert (await review.process_chat_review_queue())["processed"] == 1
    pending = await review.get_proposal(sid, "owner")
    assert pending["state"] == "pending" and len(pending["messages"]) == 2
    assert (root / "Existing.md").read_text() == "Before" and not (
        root / "New.md"
    ).exists()
    selected = [
        next(c["change_id"] for c in pending["changes"] if c["note_path"] == "New.md")
    ]
    with pytest.raises(ValueError):
        await review.decide_proposal(
            sid, "owner", proposal["proposal_id"], "stale", selected
        )
    await review.decide_proposal(
        sid, "owner", proposal["proposal_id"], pending["generation"], selected
    )
    await review.process_chat_review_queue()
    assert (root / "New.md").read_text() == "Walk Friday" and (
        root / "Existing.md"
    ).read_text() == "Before"
    review.record_vault_change.assert_awaited_once()
    assert review.record_vault_change.await_args.kwargs["idempotency_key"].startswith(
        proposal["proposal_id"]
    )
    again = await review.decide_proposal(
        sid, "owner", proposal["proposal_id"], pending["generation"], selected
    )
    assert again["state"] == "applied"
    await review.process_chat_review_queue()
    review.dispatch_saved.assert_awaited_once()
    assert len(svc.db.chat_runs.rows) == 2 and all(
        r["status"] == "succeeded" for r in svc.db.chat_runs.rows
    )


def test_application_freshness_partial_failure_and_idempotent_replay(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(note_review, "vault_run_lock", lambda *_: nullcontext())
    root = tmp_path / "vault"
    root.mkdir()
    journal = tmp_path / "journal.json"
    changes = note_review.build_potential_changes({}, {"A.md": "one", "B.md": "two"})
    ids = [c.change_id for c in changes]
    real = note_review._atomic_write

    def fail_second(path, data):
        if path.name == "B.md":
            raise OSError("disk interrupted")
        real(path, data)

    monkeypatch.setattr(note_review, "_atomic_write", fail_second)
    with pytest.raises(OSError):
        note_review.apply_changes(root, "owner", changes, ids, journal)
    assert (root / "A.md").read_text() == "one" and not (root / "B.md").exists()
    monkeypatch.setattr(note_review, "_atomic_write", real)
    assert note_review.apply_changes(root, "owner", changes, ids, journal) == sorted(
        ids
    )
    assert note_review.apply_changes(root, "owner", changes, ids, journal) == sorted(
        ids
    )
    next_changes = note_review.build_potential_changes(
        {"A.md": "one"}, {"A.md": "changed"}
    )
    (root / "A.md").write_text("User edited this")
    with pytest.raises(note_review.ReviewConflict):
        note_review.apply_changes(
            root,
            "owner",
            next_changes,
            [next_changes[0].change_id],
            tmp_path / "next.json",
        )
    assert (root / "A.md").read_text() == "User edited this"


async def test_orphan_proposal_does_not_block_following_job(svc):
    sid = await complete_chat(svc)
    proposal = await review.create_proposal(sid, "owner")
    svc.db.chat_save_proposals.rows.insert(
        0,
        {
            **copy.deepcopy(svc.db.chat_save_proposals.rows[0]),
            "proposal_id": "orphan",
            "session_id": "missing",
            "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
        },
    )
    svc.memory_service._run_agent_with_note_guarantee.return_value = NS(
        summary="", touched=[], errors=[], truncated=False, stalled=False
    )
    await review.process_chat_review_queue()
    assert svc.db.chat_save_proposals.rows[0]["state"] == "failed"
    assert (await review.get_proposal(sid, "owner", proposal["proposal_id"]))[
        "state"
    ] == "pending"


async def test_typed_retrieval_has_no_synthetic_memory_entry(monkeypatch):
    from backend.services.memory.providers.chronicle import MemoryService

    provider = object.__new__(MemoryService)
    provider._initialized = True
    provider.scope_resolver = NS(require_space=AsyncMock())
    provider._run_search_agent = AsyncMock(
        return_value=(
            NS(
                answer="Grounded answer",
                notes=[{"path": "People/Alex.md", "content": "Alex"}],
                errors=[],
                truncated=False,
            ),
            "pi",
        )
    )
    result = await provider.retrieve_for_chat("Alex", "owner", memory_space_id="space")
    assert result.answer == "Grounded answer" and len(result.notes) == 1
    assert result.notes[0].path == "People/Alex.md" and result.notes[0].id.startswith(
        "V"
    )
    assert "search:owner" not in result.model_dump_json()


async def test_failed_application_retries_exact_approval_after_audit_failure(
    svc, monkeypatch, tmp_path
):
    sid = await complete_chat(svc)

    async def writer(_cls, stage, *args, **kwargs):
        (stage / "Takeaway.md").write_text("Take a walk Friday")
        return NS(
            summary="draft",
            touched=["Takeaway.md"],
            errors=[],
            truncated=False,
            stalled=False,
        )

    svc.memory_service._run_agent_with_note_guarantee.side_effect = writer
    created = await review.create_proposal(sid, "owner")
    await review.process_chat_review_queue()
    pending = await review.get_proposal(sid, "owner")
    ids = [c["change_id"] for c in pending["changes"]]
    await review.decide_proposal(
        sid, "owner", created["proposal_id"], pending["generation"], ids
    )
    review.record_vault_change.side_effect = RuntimeError(
        "audit temporarily unavailable"
    )
    await review.process_chat_review_queue()
    failed = await review.get_proposal(sid, "owner")
    assert failed["state"] == "failed" and failed["has_applied_changes"]
    root = tmp_path / "conversation_docs" / "owner"
    assert (root / "Takeaway.md").read_text() == "Take a walk Friday"
    with pytest.raises(ValueError):
        await review.decide_proposal(
            sid,
            "owner",
            created["proposal_id"],
            pending["generation"],
            [],
            discard=True,
        )
    assert (await review.create_proposal(sid, "owner"))["proposal_id"] == created[
        "proposal_id"
    ]
    review.record_vault_change.side_effect = None
    await review.decide_proposal(
        sid, "owner", created["proposal_id"], pending["generation"], [], retry=True
    )
    await review.process_chat_review_queue()
    result = await review.get_proposal(sid, "owner")
    assert result["state"] == "applied" and result["applied_change_ids"] == ids
    assert review.record_vault_change.await_count == 2
    assert (
        review.record_vault_change.await_args_list[0].kwargs["idempotency_key"]
        == review.record_vault_change.await_args_list[1].kwargs["idempotency_key"]
    )


async def test_discard_and_protected_instruction_drafts_never_write_accepted_vault(
    svc, tmp_path
):
    sid = await complete_chat(svc)

    async def writer(_cls, stage, *args, **kwargs):
        (stage / "AGENTS.md").write_text("unapproved operating instruction")
        return NS(
            summary="draft",
            touched=["AGENTS.md"],
            errors=[],
            truncated=False,
            stalled=False,
        )

    svc.memory_service._run_agent_with_note_guarantee.side_effect = writer
    proposal = await review.create_proposal(sid, "owner")
    await review.process_chat_review_queue()
    failed = await review.get_proposal(sid, "owner")
    assert failed["state"] == "failed" and "protected" in failed["error"]
    assert not (tmp_path / "conversation_docs" / "owner" / "AGENTS.md").exists()
    result = await review.decide_proposal(
        sid, "owner", proposal["proposal_id"], proposal["generation"], [], discard=True
    )
    assert result["state"] == "discarded"
    review.record_vault_change.assert_not_awaited()
    review.dispatch_saved.assert_not_awaited()


async def test_sources_cannot_change_during_a_turn(svc, monkeypatch):
    from backend.services.redis_lock import LockUnavailable

    sid = (await svc.create_session("owner")).session_id

    @asynccontextmanager
    async def occupied(*args, **kwargs):
        raise LockUnavailable("A reply is running")
        yield

    monkeypatch.setattr(chat_routes, "distributed_lock", occupied)
    with pytest.raises(Exception) as error:
        await chat_routes.update_chat_session(
            sid, chat_routes.ChatSessionUpdateRequest(sources=[]), NS(id="owner")
        )
    assert error.value.status_code == 409
    assert svc.sessions_collection.rows[0]["metadata"]["context_changes"] == []


async def test_earlier_messages_remain_readable_without_mutating_history(svc):
    for i in range(5):
        svc.messages_collection.rows.append(
            chat_service.ChatMessage(
                message_id=str(i),
                session_id="s",
                user_id="owner",
                role="user",
                content=str(i),
                timestamp=datetime(2026, 9, 14, 0, i, tzinfo=timezone.utc),
            ).to_dict()
        )
    latest = await svc.get_session_messages("s", "owner", limit=2)
    earlier = await svc.get_session_messages("s", "owner", limit=2, offset=2)
    assert [m.content for m in latest] == ["3", "4"]
    assert [m.content for m in earlier] == ["1", "2"]
    assert len(svc.messages_collection.rows) == 5
