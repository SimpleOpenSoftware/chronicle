"""Selected source isolation, canonical resolution, and real chat entry points."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import chat_service
from backend.chat_service import ChatService, ChatSession
from backend.models.audio_capture import AudioRangeRef
from backend.services import chat_sources as sources

AT = datetime(2026, 9, 4, 7, tzinfo=timezone.utc)


class NS(SimpleNamespace):
    def model_dump(self):
        return vars(self).copy()


from contextlib import asynccontextmanager

from backend.services.chat_context import (
    ChatContext,
    VaultNoteEvidence,
    VaultRetrieval,
    source_id,
)


@pytest.fixture(autouse=True)
def isolated_chat_claim(monkeypatch):
    @asynccontextmanager
    async def unlocked(*args, **kwargs):
        yield

    monkeypatch.setattr("backend.chat_service.distributed_lock", unlocked)


def claim(start=0, end=100, chunk="chunk-a", basis="captured"):
    return AudioRangeRef(
        capture_source_id="phone",
        time_basis=basis,
        chunk_ids=[chunk],
        started_at=AT + timedelta(seconds=start),
        ended_at=AT + timedelta(seconds=end),
    )


def segment(text, start, end, speaker="Ankush"):
    return NS(
        text=text,
        start=start,
        end=end,
        speaker=speaker,
        identified_as=None,
        segment_type="speech",
    )


@pytest.fixture
def recording(monkeypatch):
    row = NS(
        conversation_id="recording-a",
        title="Therapy session",
        audio_ranges=[claim(basis="unknown")],
        segments=[segment("I will write in my journal.", 10, 20)],
        transcript="I will write in my journal.",
        revision="v1",
    )
    monkeypatch.setattr(sources, "owned_recording", AsyncMock(return_value=row))
    monkeypatch.setattr(sources, "recording_hash", lambda r: r.revision)
    return row


async def test_recording_has_no_invented_event_date_and_has_playback_citation(
    recording,
):
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="recording", key="recording-a"), "user-a"
    )
    assert context.started_at is None
    assert context.passages[0].url == "/recordings/recording-a?start=10&end=20"
    assert "journal" in context.passages[0].text
    assert sources.cited_passages("Journal [S1], bogus [S99]", context) == [
        context.passages[0].model_dump()
    ]


async def test_owned_recording_gets_exact_user_and_space(recording, monkeypatch):
    require = AsyncMock()
    monkeypatch.setattr(sources.MemoryScopeResolver, "require_space", require)
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="recording", key="recording-a"), "user-a", "space-a"
    )
    sources.owned_recording.assert_awaited_once_with("user-a", "recording-a", "space-a")
    assert "memory_space_id=space-a" in context.passages[0].url
    assert require.await_args.args[0].memory_space_id == "space-a"


async def test_denied_source_never_falls_back_to_vault(recording, monkeypatch):
    monkeypatch.setattr(
        sources, "owned_recording", AsyncMock(side_effect=LookupError())
    )
    with pytest.raises(sources.SourceUnavailable):
        await sources.resolve_source(
            sources.ChatSourceRef(kind="recording", key="recording-a"), "other-user"
        )


async def setup_episode(monkeypatch, recording, claims):
    episode = NS(
        episode_id="episode-a",
        episode_key="key-a",
        revision=1,
        user_id="user-a",
        status="active",
        title="Therapy",
        local_date=AT.date(),
        timezone="Asia/Kolkata",
        started_at=AT,
        audio_ranges=claims,
    )
    monkeypatch.setattr(
        sources.TimelineEpisode, "find_one", AsyncMock(return_value=episode)
    )
    monkeypatch.setattr(sources, "get_day", AsyncMock(return_value=NS()))
    monkeypatch.setattr(sources, "snapshot_episodes", AsyncMock(return_value=[episode]))
    monkeypatch.setattr(sources, "evidence_sources", lambda _: [])
    query = Mock(return_value=NS(to_list=AsyncMock(return_value=[recording])))
    monkeypatch.setattr(sources.Conversation, "find", query)
    return episode


async def test_episode_reads_full_claim_not_just_index_excerpt_and_excludes_neighbors(
    recording, monkeypatch
):
    recording.audio_ranges = [claim()]
    recording.segments = [
        segment("Unrelated boss task", 0, 9),
        segment("Journal daily", 11, 15),
        segment("Exercise weekly", 20, 25),
        segment("Unrelated friend task", 31, 40),
    ]
    await setup_episode(monkeypatch, recording, [claim(10, 30)])
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="episode", key="episode-a"), "user-a"
    )
    assert [p.text for p in context.passages] == [
        "Ankush: Journal daily",
        "Ankush: Exercise weekly",
    ]
    query = sources.Conversation.find.call_args.args[0]
    assert (
        query["user_id"] == "user-a"
        and query["memory_space_id"] is None
        and query["deleted"] is False
    )


async def test_trimmed_presentation_gap_is_mapped_and_boundary_segment_is_disclosed(
    recording, monkeypatch
):
    recording.audio_ranges = [claim(0, 10), claim(100, 110, "chunk-b")]
    recording.segments = [
        segment("Before", 1, 5),
        segment("Crosses seam", 9, 12),
        segment("In selected session", 13, 16),
    ]
    await setup_episode(monkeypatch, recording, [claim(100, 110, "chunk-b")])
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="episode", key="episode-a"), "user-a"
    )
    assert [p.text for p in context.passages] == ["Ankush: In selected session"]
    assert "incomplete" in context.coverage


async def test_unpublished_episode_cannot_be_discussed(recording, monkeypatch):
    await setup_episode(monkeypatch, recording, [claim()])
    monkeypatch.setattr(sources, "snapshot_episodes", AsyncMock(return_value=[]))
    with pytest.raises(sources.SourceUnavailable):
        await sources.resolve_source(
            sources.ChatSourceRef(kind="episode", key="episode-a"), "user-a"
        )


async def test_dated_session_uses_current_group_and_all_member_claims(
    recording, monkeypatch
):
    episode = await setup_episode(monkeypatch, recording, [claim()])
    group = NS(group_key="group-a", revision=7, title="Session group", started_at=AT)
    monkeypatch.setattr(
        sources,
        "resolved_sessions",
        AsyncMock(return_value=[(NS(local_date=AT.date()), group, [episode])]),
    )
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="session", key="group-a", local_date=AT.date()),
        "user-a",
    )
    assert context.title == "Session group"
    assert "session=group-a" in context.url


async def test_long_source_discloses_selected_coverage_and_retains_citation_ids(
    recording,
):
    recording.segments = [segment("journal " + "x" * 1700, i, i + 1) for i in range(50)]
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="recording", key="recording-a"), "user-a"
    )
    bounded = context.for_turn("journal", budget=4000)
    assert len(bounded.passages) == 2
    assert bounded.total_passages == 50
    assert "may miss other details" in bounded.coverage
    assert bounded.passages[0].id == context.passages[0].id


async def test_source_creation_and_streaming_retain_attachment_and_citations(
    recording, monkeypatch
):
    from mongomock_motor import AsyncMongoMockClient

    service = ChatService()
    service._initialized = True
    service.db = AsyncMock()
    dialogue_db = AsyncMongoMockClient().dialogue_test
    service.db.dialogue_state = dialogue_db.dialogue_state
    service.db.dialogue_interpretations = dialogue_db.dialogue_interpretations
    service.sessions_collection = AsyncMock()
    session = await service.create_session(
        "user-a", sources=[sources.ChatSourceRef(kind="recording", key="recording-a")]
    )
    stored = service.sessions_collection.insert_one.call_args.args[0]
    restored = ChatSession.from_dict(stored)
    assert restored.metadata["sources"][0]["key"] == "recording-a"
    service.sessions_collection.find_one.return_value = stored
    service.add_message = AsyncMock(return_value=True)
    service.get_session_messages = AsyncMock(return_value=[])
    service._get_tool_mode_system_prompt = AsyncMock(return_value="system")
    prompts = []
    cid = source_id(sources.ChatSourceRef(kind="recording", key="recording-a"))

    async def stream(messages, **kwargs):
        prompts.append(messages)
        yield {"type": "content", "text": f"Journal daily [{cid}_S1]"}
        yield {
            "type": "done",
            "content": f"Journal daily [{cid}_S1]",
            "tool_calls": [],
            "finish_reason": "stop",
        }

    monkeypatch.setattr(chat_service, "async_chat_with_tools_stream", stream)
    events = [
        e
        async for e in service.generate_response_stream(
            session.session_id, "user-a", "What were my action items?"
        )
    ]
    assert any(e["type"] == "source_context" for e in events)
    assert any(
        "subjects of this turn" in m["content"] and "journal" in m["content"]
        for m in prompts[0]
    )
    answer = service.add_message.call_args.args[0]
    assert (
        answer.metadata["evidence"]["conversations"][0]["passages"][0]["text"]
        == "Ankush: I will write in my journal."
    )
    original = answer.metadata["evidence"]["conversations"][0]["revision"]
    recording.revision = "v2"
    recording.segments = [segment("Now journal twice daily", 10, 20)]
    _ = [
        e
        async for e in service.generate_response_stream(
            session.session_id, "user-a", "And now?"
        )
    ]
    assert (
        service.add_message.call_args.args[0].metadata["evidence"]["conversations"][0][
            "revision"
        ]
        != original
    )
    assert (
        answer.metadata["evidence"]["conversations"][0]["passages"][0]["text"]
        == "Ankush: I will write in my journal."
    )


async def test_missing_source_blocks_real_chat_entry_before_model_call(
    recording, monkeypatch
):
    service = ChatService()
    service._initialized = True
    service.db = AsyncMock()
    service.sessions_collection = AsyncMock()
    service.sessions_collection.find_one.return_value = {
        "metadata": {
            "interaction_version": 2,
            "sources": [{"kind": "recording", "key": "recording-a"}],
        }
    }
    service.add_message = AsyncMock()
    monkeypatch.setattr(
        sources, "owned_recording", AsyncMock(side_effect=LookupError())
    )
    events = [
        e async for e in service.generate_response_stream("chat-a", "user-a", "Tasks?")
    ]
    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["run_id"]
    service.add_message.assert_not_called()


async def test_http_create_reload_and_citation_serialization(recording, monkeypatch):
    from backend.auth import current_active_user
    from backend.routers.modules import chat_routes

    service = ChatService()
    service._initialized = True
    service.db = AsyncMock()
    service.sessions_collection = AsyncMock()
    saved = {}

    async def insert(row):
        saved.update(row)

    service.sessions_collection.insert_one.side_effect = insert

    async def find(query):
        return (
            saved
            if query["user_id"] == saved.get("user_id")
            and query["session_id"] == saved.get("session_id")
            else None
        )

    service.sessions_collection.find_one.side_effect = find
    monkeypatch.setattr(chat_routes, "get_chat_service", lambda: service)
    app = FastAPI()
    app.include_router(chat_routes.router)
    app.dependency_overrides[current_active_user] = lambda: NS(id="user-a")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/sessions",
            json={"sources": [{"kind": "recording", "key": "recording-a"}]},
        )
        assert response.status_code == 200, response.text
        session_id = response.json()["session_id"]
        assert (await client.get(f"/chat/sessions/{session_id}")).json()["sources"][0][
            "key"
        ] == "recording-a"
        assert (
            (await client.get(f"/chat/sessions/{session_id}/sources"))
            .json()["sources"][0]["passages"][0]["id"]
            .endswith("_S1")
        )
        app.dependency_overrides[current_active_user] = lambda: NS(id="other-user")
        assert (
            await client.get(f"/chat/sessions/{session_id}/sources")
        ).status_code == 404


async def test_adjacent_episode_claims_do_not_drop_a_segment(recording, monkeypatch):
    recording.audio_ranges = [claim(0, 30)]
    recording.segments = [segment("Journal daily", 5, 15)]
    await setup_episode(monkeypatch, recording, [claim(0, 10), claim(10, 20)])
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="episode", key="episode-a"), "user-a"
    )
    assert len(context.passages) == 1


async def test_read_tool_reaches_late_passages_and_paginates(recording):
    recording.segments = [
        segment("Ordinary discussion " + "x" * 1700, i, i + 1) for i in range(50)
    ]
    recording.segments.append(
        segment("I will book a dentist appointment " + "x" * 1700, 70, 75)
    )
    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="recording", key="recording-a"), "user-a"
    )
    assert "dentist" not in " ".join(
        p.text for p in context.for_turn("What happened?").passages
    )
    assert context.read(query="dentist")["passages"][0]["id"] == "S51"
    first = context.read()
    second = context.read(offset=first["next_offset"])
    assert first["passages"][-1]["id"] != second["passages"][0]["id"]
    assert [p["id"] for p in sources.cited_passages("See [S1, S51]", context)] == [
        "S1",
        "S51",
    ]


async def test_selected_source_tool_runs_in_existing_chat_loop(recording, monkeypatch):
    import json

    context = await sources.resolve_source(
        sources.ChatSourceRef(kind="recording", key="recording-a"), "user-a"
    )
    service = ChatService()
    service._initialized = True
    service.db = AsyncMock()
    service.add_message = AsyncMock(return_value=True)
    service.get_session_messages = AsyncMock(return_value=[])
    service._get_tool_mode_system_prompt = AsyncMock(return_value="system")
    calls = []

    async def stream(messages, **kwargs):
        calls.append(kwargs["tools"])
        if len(calls) == 1:
            yield {
                "type": "done",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "function": {
                            "name": "read_selected_source",
                            "arguments": json.dumps(
                                {
                                    "query": "journal",
                                    "source_id": source_id(context.ref),
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            assert "journal" in messages[-1]["content"]
            yield {"type": "content", "text": "Journal [S1]"}
            yield {
                "type": "done",
                "content": "Journal [S1]",
                "tool_calls": [],
                "finish_reason": "stop",
            }

    monkeypatch.setattr(chat_service, "async_chat_with_tools_stream", stream)
    events = [
        e
        async for e in service._generate_response_tool_mode(
            "chat-a", "user-a", "Tasks?", source_context=ChatContext(sources=[context])
        )
    ]
    assert len(calls) == 2
    assert {t["function"]["name"] for t in calls[0]} == {
        "read_selected_source",
        "search_memories",
    }
    assert events[-1]["type"] == "complete"
    assert (
        service.add_message.call_args.args[0].metadata["evidence"]["conversations"][0][
            "passages"
        ][0]["id"]
        == "S1"
    )


@pytest.fixture(autouse=True)
def isolated_run_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path / "inference"))
