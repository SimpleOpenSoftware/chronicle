"""Chat lists amortize privacy work and preserve the scalar policy decisions."""

import asyncio
import random
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_entrypoints import START, db

from backend.chat_service import ChatService, ChatSession
from backend.routers.modules import chat_routes
from backend.services import privacy
from backend.services.privacy import PrivacySnapshot, merged, utc


def connected_request():
    async def receive():
        await asyncio.Event().wait()

    return SimpleNamespace(receive=receive)


def reference_spans(self, identifier, start, end):
    start, end = utc(start), utc(end)
    source = self.source(identifier or "")
    if not source:
        return [(start, end)] if end > start else []
    activated = (
        utc(source["privacy_enabled_from"])
        if source.get("privacy_enabled_from")
        else None
    )
    if source.get("privacy_updating"):
        return []
    source_id = source["source_id"]
    inventories = [row for row in self.display_sets if row["source_id"] == source_id]
    segments = self._segments_in(source_id, start, end)
    overrides = [r for r in self.overrides if r["source_id"] == source_id]
    requirements = [r for r in self.required_ranges if r["source_id"] == source_id]
    edges = {start, end}
    if activated is not None:
        edges.add(max(start, min(end, activated)))
    for inventory in inventories:
        edges.update(
            max(start, min(end, utc(inventory[key])))
            for key in ("observed_at", "transition_started_at")
        )
    for segment in segments:
        edges.update(
            max(start, min(end, segment[k])) for k in ("started_at", "ended_at")
        )
    for row in overrides + requirements:
        edges.update(
            max(start, min(end, utc(row[k]))) for k in ("started_at", "ended_at")
        )
    points = sorted(edges)
    allowed = []
    for low, high in zip(points, points[1:]):
        decisions = [
            r
            for r in overrides
            if utc(r["started_at"]) <= low and utc(r["ended_at"]) >= high
        ]
        if decisions:
            latest = max(decisions, key=lambda r: r.get("revision", 0))
            if latest["override"] == "allowed":
                allowed.append((low, high))
            continue
        tracks, safe, recorded = self._display_requirements(
            activated, inventories, requirements, low, high
        )
        applicable = self._applicable_segments(segments, tracks, recorded, low, high)
        states = [seg["state"] for seg in applicable]
        if any(state != "allowed" for state in states):
            continue
        for track in tracks:
            states = [seg["state"] for seg in applicable if seg["track_id"] == track]
            if not states or any(state != "allowed" for state in states):
                safe = False
                break
        if safe:
            allowed.append((low, high))
    return merged(allowed)


async def test_chat_list_reads_one_policy_and_one_quarantine_scan(db, monkeypatch):
    service = ChatService()
    service._initialized = True
    service.db = db
    service.sessions_collection = db.chat_sessions
    service.messages_collection = db.chat_messages
    monkeypatch.setattr(chat_routes, "get_chat_service", lambda: service)
    for i in range(50):
        session = ChatSession(
            session_id=f"chat-{i}", user_id="owner", title=f"Chat {i}"
        )
        await db.chat_sessions.insert_one(session.to_dict())
        await db.chat_messages.insert_one(
            {
                "user_id": "owner",
                "session_id": session.session_id,
                "role": "assistant",
                "metadata": {},
            }
        )
    load = AsyncMock(wraps=privacy._load_capture_snapshot)
    quarantine = AsyncMock(wraps=privacy.quarantined_vault_paths)
    monkeypatch.setattr(privacy, "_load_capture_snapshot", load)
    monkeypatch.setattr(privacy, "quarantined_vault_paths", quarantine)
    rows = await chat_routes.get_chat_sessions(
        request=connected_request(),
        limit=50,
        current_user=SimpleNamespace(id="owner"),
    )
    assert len(rows) == 50
    assert load.await_count == 1
    assert quarantine.await_count == 1
    assert all(row.message_count == 1 for row in rows)


def test_indexed_policy_matches_reference_for_overlapping_history():
    rng = random.Random(4721)
    at = lambda n: START + timedelta(seconds=n)
    for trial in range(60):
        sources = [
            {"source_id": "a", "privacy_enabled_from": at(5), "privacy_tracks": ["one"]}
        ]
        inventories = [
            {
                "source_id": "a",
                "observed_at": at(5),
                "transition_started_at": at(5),
                "track_ids": ["one"],
            }
        ]
        for i in range(3):
            observed = rng.randrange(6, 80)
            inventories.append(
                {
                    "source_id": "a",
                    "observed_at": at(observed),
                    "transition_started_at": at(observed - rng.randrange(5)),
                    "track_ids": rng.choice([["one"], ["one", "two"], []]),
                }
            )
        requirements = []
        overrides = []
        intervals = []
        for i in range(25):
            low = rng.randrange(80)
            high = low + rng.randrange(1, 30)
            intervals.append(
                {
                    "source_id": "a",
                    "track_id": rng.choice(["one", "two"]),
                    "segments": [
                        {
                            "started_at": at(low),
                            "ended_at": at(high),
                            "state": rng.choice(["allowed", "pending", "excluded"]),
                        }
                    ],
                }
            )
            if i < 4:
                requirements.append(
                    {
                        "source_id": "a",
                        "started_at": at(low),
                        "ended_at": at(high),
                        "track_ids": rng.choice([["one"], ["two"], []]),
                        "coverage": "historical_recorded_displays",
                        "inventory_refinement": "proof",
                    }
                )
                overrides.append(
                    {
                        "source_id": "a",
                        "started_at": at(low + 1),
                        "ended_at": at(high + 2),
                        "override": rng.choice(["allowed", "excluded"]),
                        "revision": i,
                    }
                )
        snapshot = PrivacySnapshot(
            sources, intervals, overrides, inventories, requirements
        )
        for i in range(20):
            low = rng.randrange(90)
            high = low + rng.randrange(1, 20)
            assert snapshot.allowed_spans(
                "a:input", at(low), at(high)
            ) == reference_spans(snapshot, "a:input", at(low), at(high)), (
                trial,
                low,
                high,
            )


async def test_chat_list_disconnect_cancels_scan(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def scan(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(privacy, "filter_chat_sessions", scan)
    monkeypatch.setattr(
        chat_routes,
        "get_chat_service",
        lambda: SimpleNamespace(get_user_sessions=AsyncMock(return_value=[object()])),
    )
    request = SimpleNamespace(
        receive=AsyncMock(return_value={"type": "http.disconnect"})
    )
    with pytest.raises(chat_routes.HTTPException) as exc:
        await chat_routes.get_chat_sessions(
            request=request, current_user=SimpleNamespace(id="owner")
        )
    assert exc.value.status_code == 499
    assert started.is_set() and cancelled.is_set()


async def test_batch_chat_privacy_reloads_after_revocation_and_isolates_rejected_chats(
    db, monkeypatch
):
    # An unproven answer is held by the quarantined recording. A receipt-backed
    # ordinary chat must survive, without inheriting that broad source fence.
    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "held",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    ordinary = ChatSession(session_id="ordinary", user_id="owner", title="Ordinary")
    historical = ChatSession(
        session_id="historical", user_id="owner", title="Historical"
    )
    for session in (ordinary, historical):
        await db.chat_messages.insert_one(
            {
                "user_id": "owner",
                "session_id": session.session_id,
                "role": "assistant",
                "metadata": {"evidence": {}} if session is ordinary else {},
            }
        )
    assert [
        s.session_id
        for s, _ in await privacy.filter_chat_sessions("owner", [historical, ordinary])
    ] == ["ordinary"]
    # The next request must see newly introduced holds, with no timed cache.
    ordinary.metadata = {
        "source_id": "screenpipe-test",
        "started_at": START,
        "ended_at": START + timedelta(seconds=5),
    }
    assert await privacy.filter_chat_sessions("owner", [ordinary]) == []
    assert await privacy.filter_chat_sessions(
        "someone-else",
        [ChatSession(session_id="ordinary", user_id="someone-else", title="Other")],
    )


async def test_chat_list_final_revision_conflict_is_423(monkeypatch):
    async def stale(*args):
        raise privacy.PrivacyHeld()

    monkeypatch.setattr(privacy, "filter_chat_sessions", stale)
    monkeypatch.setattr(
        chat_routes,
        "get_chat_service",
        lambda: SimpleNamespace(get_user_sessions=AsyncMock(return_value=[object()])),
    )
    with pytest.raises(chat_routes.HTTPException) as exc:
        await chat_routes.get_chat_sessions(
            request=connected_request(),
            current_user=SimpleNamespace(id="owner"),
        )
    assert exc.value.status_code == 423


def test_narrow_window_does_not_scan_unrelated_historical_requirements(monkeypatch):
    source = {
        "source_id": "a",
        "privacy_enabled_from": START,
        "privacy_tracks": ["one"],
    }
    requirements = [
        {
            "source_id": "a",
            "started_at": START + timedelta(seconds=i * 10),
            "ended_at": START + timedelta(seconds=(i + 1) * 10),
            "track_ids": ["one"],
            "coverage": "historical_recorded_displays",
            "inventory_refinement": "proof",
        }
        for i in range(10000)
    ]
    snapshot = PrivacySnapshot([source], [], required_ranges=requirements)
    calls = []
    original = PrivacySnapshot._display_requirements

    def counted(activated, inventories, requirements, low, high):
        calls.append(len(requirements))
        return original(activated, inventories, requirements, low, high)

    monkeypatch.setattr(PrivacySnapshot, "_display_requirements", staticmethod(counted))
    snapshot.allowed_spans(
        "a", START + timedelta(seconds=50001), START + timedelta(seconds=50002)
    )
    assert calls == [1]


async def test_standalone_chat_reuses_policy_for_session_and_messages(db, monkeypatch):
    load = AsyncMock(wraps=privacy._load_capture_snapshot)
    monkeypatch.setattr(privacy, "_load_capture_snapshot", load)
    await privacy.guard_chat("owner", "empty", {})
    assert load.await_count == 1


async def test_quarantine_cursor_closes_when_cancelled(monkeypatch):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Cursor:
        def batch_size(self, size):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            closed.set()

    class Database:
        def __getitem__(self, name):
            return SimpleNamespace(find=lambda *args: Cursor())

    monkeypatch.setattr(privacy, "database", lambda: Database())
    task = asyncio.create_task(
        privacy.quarantined_vault_paths("owner", snapshot=PrivacySnapshot([], []))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_real_chat_batch_fences_activation_before_return(db, monkeypatch):
    session = ChatSession(
        session_id="ordinary",
        user_id="owner",
        title="Ordinary",
        metadata={
            "source_id": "future-source",
            "started_at": START,
            "ended_at": START + timedelta(seconds=2),
        },
    )
    original = privacy._guard_chat_evidence

    async def change_after_admission(*args):
        await original(*args)
        await db.capture_sources.insert_one(
            {
                "user_id": "owner",
                "source_id": "future-source",
                "privacy_enabled_from": START,
                "privacy_revision": 1,
            }
        )

    monkeypatch.setattr(privacy, "_guard_chat_evidence", change_after_admission)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.filter_chat_sessions("owner", [session])


async def test_rejected_chat_quarantine_cannot_fence_unrelated_chat(db, monkeypatch):
    await db.conversations.insert_one(
        {
            "user_id": "owner",
            "conversation_id": "held",
            "source_id": "screenpipe-test",
            "started_at": START,
            "ended_at": START + timedelta(seconds=5),
        }
    )
    historical = ChatSession(
        session_id="historical", user_id="owner", title="Historical"
    )
    ordinary = ChatSession(
        session_id="ordinary",
        user_id="owner",
        title="Ordinary",
        metadata={"source_id": "ordinary-source", "started_at": START},
    )
    await db.chat_messages.insert_one(
        {
            "user_id": "owner",
            "session_id": "historical",
            "role": "assistant",
            "metadata": {},
        }
    )
    original = privacy._guard_chat_evidence

    async def change_rejected_source(*args):
        try:
            return await original(*args)
        except privacy.PrivacyHeld:
            await db.capture_sources.update_one(
                {"source_id": "screenpipe-test"}, {"$inc": {"privacy_revision": 1}}
            )
            raise

    monkeypatch.setattr(privacy, "_guard_chat_evidence", change_rejected_source)
    result = await privacy.filter_chat_sessions("owner", [historical, ordinary])
    assert [session.session_id for session, _ in result] == ["ordinary"]


async def test_evidence_free_chat_read_needs_no_historical_policy(db, monkeypatch):
    await db.chat_messages.insert_one(
        {
            "user_id": "owner",
            "session_id": "plain",
            "role": "assistant",
            "metadata": {"evidence": {}},
        }
    )
    load = AsyncMock(
        side_effect=AssertionError("Plain text has no historical policy dependency")
    )
    monkeypatch.setattr(privacy, "load_snapshot", load)
    await privacy.check_chat("owner", "plain", {})
    await privacy.check_payload("owner", [{"evidence": {}}])
    load.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_id": "screenpipe-test", "started_at": START},
        {"source": {"kind": "recording", "key": "recording"}},
        {"evidence": {"vault_notes": [{"path": "People/Person.md"}]}},
        {"speaker_recognition": {"privacy_gallery_receipt": {}}},
        {"privacy_reference_receipt": ["receipt"]},
        {"evidence_refs": [{"capture_session_ids": ["capture"]}]},
        {"raw_response": {"privacy_reference_receipt": []}},
    ],
)
async def test_chat_read_fast_path_never_skips_evidence(metadata, db, monkeypatch):
    load = AsyncMock(side_effect=privacy.PrivacyHeld())
    monkeypatch.setattr(privacy, "load_snapshot", load)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.check_chat("owner", "chat", metadata)
    load.assert_awaited_once()


async def test_capture_identity_only_reference_retains_whole_policy_fence(db):
    snapshot = await privacy.load_snapshot("owner")
    assert snapshot.permits_record(
        {"evidence_refs": [{"capture_session_ids": ["capture"]}]}
    )
    await db.capture_sources.update_one(
        {"source_id": "screenpipe-test"}, {"$inc": {"privacy_revision": 1}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("owner", snapshot)


@pytest.mark.parametrize("entry", ["payload", "chat", "batch"])
async def test_completed_chat_admission_releases_policy_without_cyclic_gc(
    db, monkeypatch, entry
):
    import gc
    import weakref

    references = []
    original = privacy.load_snapshot

    async def observe(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        references.append(weakref.ref(snapshot))
        return snapshot

    monkeypatch.setattr(privacy, "load_snapshot", observe)
    enabled = gc.isenabled()
    gc.disable()
    try:
        if entry == "payload":
            snapshot = await privacy.guard_payload(
                "owner", {"nested": [{"value": "plain"}]}
            )
            del snapshot
        elif entry == "chat":
            snapshot = await privacy.guard_chat("owner", "plain", {})
            del snapshot
        else:
            result = await privacy.filter_chat_sessions(
                "owner",
                [ChatSession(session_id="plain", user_id="owner", title="Plain")],
            )
            assert len(result) == 1
            del result
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert references and all(ref() is None for ref in references)
    finally:
        if enabled:
            gc.enable()


def test_compiled_policy_does_not_retain_raw_screening_documents():
    import weakref

    class EvidenceRow(dict):
        pass

    rows = [
        EvidenceRow(
            source_id="a",
            track_id="one",
            evidence=[{"captured_at": START}],
            segments=[
                {
                    "started_at": START,
                    "ended_at": START + timedelta(seconds=1),
                    "state": "allowed",
                }
            ],
        )
    ]
    original = weakref.ref(rows[0])
    snapshot = PrivacySnapshot(
        [{"source_id": "a", "privacy_enabled_from": START}], rows
    )
    del rows
    assert original() is None
    assert len(snapshot._segments_in("a", START, START + timedelta(seconds=1))) == 1


async def test_real_logging_middleware_delivers_disconnect_to_chat_scan(monkeypatch):
    from fastapi import FastAPI

    from backend.auth import current_active_user
    from backend.middleware.app_middleware import RequestLoggingMiddleware

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def scan(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(privacy, "filter_chat_sessions", scan)
    monkeypatch.setattr(
        chat_routes,
        "get_chat_service",
        lambda: SimpleNamespace(get_user_sessions=AsyncMock(return_value=[object()])),
    )
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(chat_routes.router)
    app.dependency_overrides[current_active_user] = lambda: SimpleNamespace(id="owner")
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/chat/sessions",
        "raw_path": b"/api/chat/sessions",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("test", 123),
        "server": ("test", 80),
    }
    # The router's actual prefix is part of this integration test.
    scope["path"] = chat_routes.router.prefix + "/sessions"
    scope["raw_path"] = scope["path"].encode()
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
