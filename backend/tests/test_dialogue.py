"""Shared dialogue's real Mongo-facing interface, including competing writers."""

import asyncio
from datetime import timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError

from backend.services.dialogue.models import (
    ConversationContinuation,
    DialogueTask,
    DialogueThread,
    InputWait,
    ReplyChoice,
    TaskCommand,
    Utterance,
)
from backend.services.dialogue.store import (
    DialogueConflict,
    DialogueStore,
    changed,
    now,
)


@pytest.fixture
def store():
    return DialogueStore(AsyncMongoMockClient().dialogue_test)


@pytest.fixture
def thread():
    return DialogueThread(id="thread", user_id="user", memory_space_id="space")


def waiting(identity="task", *, expires_at=None):
    return DialogueTask(
        id=identity,
        thread_id="thread",
        title="Choose a room",
        status="awaiting_input",
        revision=0,
        continuation=ConversationContinuation(request="Turn on the lights"),
        input_wait=InputWait(
            after_utterance_id="assistant-1",
            expires_at=expires_at,
            choices=(ReplyChoice(id="study", label="Study"),),
        ),
    )


def test_models_are_strict_and_do_not_assume_alternation():
    for identity in ("one", "two"):
        assert (
            Utterance(
                id=identity,
                thread_id="thread",
                role="user",
                text="हाँ",
                created_at=now(),
            ).role
            == "user"
        )
    with pytest.raises(ValidationError):
        Utterance(
            id="one", thread_id="thread", role="human", text="No", created_at=now()
        )
    with pytest.raises(ValidationError):
        changed(waiting(), status="completed")
    with pytest.raises(ValidationError):
        InputWait(
            after_utterance_id="a",
            choices=(
                ReplyChoice(id="x", label="One"),
                ReplyChoice(id="x", label="Two"),
            ),
        )
    assert "additionalProperties" in str(DialogueTask.model_json_schema())


async def test_wait_reply_and_effect_are_one_atomic_transition(store, thread):
    await store.add_task(thread, waiting(), command_id="start")
    command = TaskCommand(
        id="reply",
        task_id="task",
        revision=0,
        action="reply",
        utterance_id="u",
        choice_id="study",
    )
    result = await store.command(thread, command)
    assert result.tasks[0].status == "running"
    assert result.tasks[0].input_wait is None
    assert len(result.effects) == 1
    assert await store.command(thread, command) == result
    assert await DialogueStore(store.db).get(thread) == result


async def test_competing_devices_cannot_both_answer(store, thread):
    await store.add_task(thread, waiting(), command_id="start")
    results = await asyncio.gather(
        *[
            store.command(
                thread,
                TaskCommand(
                    id=device,
                    task_id="task",
                    revision=0,
                    action="reply",
                    utterance_id=device,
                ),
            )
            for device in ("phone", "web")
        ],
        return_exceptions=True
    )
    assert sum(isinstance(result, DialogueConflict) for result in results) == 1
    assert len((await store.get(thread)).effects) == 1


async def test_foreground_wait_pauses_and_return_is_offered_once(store, thread):
    await store.add_task(thread, waiting(), command_id="first")
    await store.add_task(thread, waiting("aside"), command_id="second")
    state = await store.get(thread)
    assert [task.status for task in state.tasks] == ["paused", "awaiting_input"]
    await store.replace_task(
        thread,
        changed(state.tasks[1], status="completed", input_wait=None, revision=1),
        expected_revision=0,
        command_id="done",
    )
    assert (await store.offer_return(thread)).id == "task"
    assert await store.offer_return(thread) is None
    state = await store.get(thread)
    resumed = await store.command(
        thread,
        TaskCommand(
            id="resume",
            task_id="task",
            revision=state.tasks[0].revision,
            action="resume",
        ),
    )
    assert resumed.foreground_task_id == "task"
    assert resumed.tasks[0].status == "awaiting_input"


async def test_expiration_and_archive_recovery(store, thread):
    await store.add_task(
        thread, waiting(expires_at=now() - timedelta(seconds=1)), command_id="start"
    )
    state = await store.expire(thread)
    assert state.tasks[0].status == "stale"
    item = await store.claim(thread, state.effects[0].id)
    await store.archive(thread, item)
    assert not (await store.get(thread)).tasks
    assert await store.db.dialogue_tasks.count_documents({}) == 1


async def test_expired_effect_is_uncertain_not_reexecuted(store, thread):
    await store.add_task(thread, waiting(), command_id="start")
    state = await store.command(
        thread,
        TaskCommand(
            id="reply", task_id="task", revision=0, action="reply", utterance_id="u"
        ),
    )
    item = await store.claim(thread, state.effects[0].id)
    await store.transition(
        thread,
        lambda s: changed(
            s, effects=(changed(item, lease_until=now() - timedelta(seconds=1)),)
        ),
    )
    assert await store.claim(thread, item.id) is None
    assert (await store.get(thread)).effects[0].status == "uncertain"
    with pytest.raises(DialogueConflict):
        await store.settle(thread, item.id, item.lease_token)


async def test_thread_scope_and_single_audio_owner(store, thread):
    await store.bind_audio(thread, "phone", "engagement")
    with pytest.raises(DialogueConflict):
        await store.get(changed(thread, user_id="intruder"))
    with pytest.raises(DialogueConflict):
        await store.bind_audio(thread, "web", "second")
    await store.release_audio(thread, "wrong")
    assert (await store.get(thread)).audio_owner.client_id == "phone"
    await store.release_audio(thread, "engagement")
    await store.bind_audio(thread, "web", "second")


async def test_invalid_selection_and_task_limit(store, thread):
    for index in range(8):
        await store.add_task(thread, waiting(str(index)), command_id=str(index))
    with pytest.raises(DialogueConflict):
        await store.add_task(thread, waiting("ninth"), command_id="ninth")
    with pytest.raises(ValueError, match="Choice"):
        await store.command(
            thread,
            TaskCommand(
                id="bad",
                task_id="7",
                revision=0,
                action="reply",
                utterance_id="u",
                choice_id="missing",
            ),
        )


@pytest.fixture
async def chat(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from backend.chat_service import ChatService
    from backend.services import privacy

    db = AsyncMongoMockClient().dialogue_entrypoint_test
    service = ChatService()
    service._initialized = True
    service.db = db
    service.sessions_collection = db.chat_sessions
    service.messages_collection = db.chat_messages
    service._get_tool_mode_system_prompt = AsyncMock(return_value="You are Chronicle.")
    monkeypatch.setattr(privacy, "database", lambda: db)
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path))

    @asynccontextmanager
    async def unlocked(*args, **kwargs):
        yield

    monkeypatch.setattr("backend.chat_service.distributed_lock", unlocked)
    await db.chat_sessions.insert_one(
        {
            "session_id": "thread",
            "user_id": "user",
            "title": "Lights",
            "created_at": now(),
            "updated_at": now(),
            "metadata": {"interaction_version": 2},
        }
    )
    return service


async def test_chat_ask_user_and_worker_resume_use_one_transcript(chat, monkeypatch):
    import json

    from backend.services.dialogue.service import (
        DialogueCommandRequest,
        DialogueService,
    )
    from backend.services.dialogue.worker import DialogueWorker

    async def ask(*args, **kwargs):
        assert any(t["function"]["name"] == "ask_user" for t in kwargs["tools"])
        yield {
            "type": "done",
            "finish_reason": "tool_calls",
            "content": "",
            "tool_calls": [
                {
                    "id": "ask",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps(
                            {
                                "title": "Choose room",
                                "prompt": "Which room?",
                                "choices": [{"id": "study", "label": "Study"}],
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("backend.chat_service.async_chat_with_tools_stream", ask)
    events = [
        e
        async for e in chat.generate_response_stream(
            "thread", "user", "Help me choose a room"
        )
    ]
    assert events[-1]["type"] == "complete"
    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    view = await service.snapshot(thread)
    assert view.tasks[0].status == "awaiting_input"
    assert await chat.messages_collection.count_documents({}) == 2
    assert "effects" not in view.model_dump()

    request = DialogueCommandRequest(
        id="selection", revision=0, action="reply", choice_id="study"
    )
    await service.submit(thread, view.tasks[0].id, request)
    await service.submit(thread, view.tasks[0].id, request)

    async def reply(messages, **kwargs):
        assert sum(m.get("content") == "Study" for m in messages) == 1
        yield {"type": "content", "text": "The study it is."}
        yield {
            "type": "done",
            "finish_reason": "stop",
            "content": "The study it is.",
            "tool_calls": [],
        }

    monkeypatch.setattr("backend.chat_service.async_chat_with_tools_stream", reply)
    worker = DialogueWorker(service)
    await worker.sweep()
    await worker.sweep()
    assert await chat.messages_collection.count_documents({}) == 4
    assert not (await service.snapshot(thread)).tasks
    assert (
        await chat.db.dialogue_tasks.count_documents({"task.status": "completed"}) == 1
    )


async def test_wait_cannot_reference_foreign_or_user_utterance(chat):
    from backend.services.dialogue.service import DialogueService

    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    await service.store.add_task(thread, waiting(), command_id="start")
    with pytest.raises(ValueError, match="assistant"):
        await service.command(
            thread, TaskCommand(id="c", task_id="task", revision=0, action="cancel")
        )


@pytest.mark.parametrize(
    "utterance",
    ["नहीं, रहने दो", "haan lekin mat karo", "YES BUT DO NOT DO IT", "YES NO"],
)
async def test_interpretation_is_not_action_confirmation(chat, monkeypatch, utterance):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from backend.services.dialogue.service import DialogueService

    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    await service.store.add_task(thread, waiting(), command_id="start")
    model = AsyncMock(
        return_value=NS(
            choices=[NS(message=NS(content='{"intent":"cancel","task_id":"task"}'))]
        )
    )
    monkeypatch.setattr("backend.llm_client.async_chat_with_tools", model)
    decision = await service.interpret(thread, utterance)
    assert decision.intent == "cancel"
    assert (await service.store.get(thread)).tasks[0].confirmation is None
    assert model.call_args.kwargs["messages"][-1]["content"] == utterance


async def test_voice_commits_original_text_and_reopens_with_only_heard_context(
    chat, monkeypatch
):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from backend.services.dialogue import voice as module
    from backend.services.dialogue.service import DialogueService
    from backend.services.interaction_modes.contracts import AudioInterval

    service = DialogueService(chat)
    monkeypatch.setattr(module, "get_dialogue_service", AsyncMock(return_value=service))
    adapter = module.VoiceDialogue()
    identity, history = await adapter.open(
        "user", None, "phone", "engagement", "thread"
    )
    assert history == []
    session = NS(
        user_id="user",
        client_id="phone",
        interaction_id="engagement",
        plugin_state={"thread_id": identity},
    )
    effect = NS(task_id="turn-1", effect_id="response-1")
    interval = AudioInterval(
        audio_session_id="capture", capture_epoch=1, start_ms=0, end_ms=1000
    )
    await adapter.input(session, effect, "नहीं, दूसरा वाला", interval)
    await adapter.input(session, effect, "नहीं, दूसरा वाला", interval)
    await adapter.output(
        session,
        effect,
        "There are 2 options. The second is available.",
        "There are two options.",
        480,
        "audio-response",
        "interrupted",
    )
    assert await chat.messages_collection.count_documents({}) == 2
    await adapter.close(session)
    _, history = await adapter.open("user", None, "web", "next", "thread")
    assert history == [
        {"role": "user", "content": "नहीं, दूसरा वाला"},
        {"role": "assistant", "content": "There are two options."},
    ]
    assistant = await chat.messages_collection.find_one({"role": "assistant"})
    assert assistant["content"] == "There are 2 options. The second is available."
    assert assistant["metadata"]["utterance_outcome"] == "interrupted"
    assert (
        await service.snapshot(await service.thread("thread", "user"))
    ).audio_client_id == "web"


async def test_command_identity_binds_payload_even_after_resolution(store, thread):
    await store.add_task(thread, waiting(), command_id="start")
    original = TaskCommand(
        id="same",
        task_id="task",
        revision=0,
        action="reply",
        utterance_id="one",
        choice_id="study",
    )
    await store.command(thread, original)
    with pytest.raises(DialogueConflict, match="different input"):
        await store.command(thread, changed(original, utterance_id="two"))


async def test_targeted_reply_rejects_changed_text_on_lost_ack_retry(chat):
    from backend.chat_service import ChatMessage
    from backend.services.dialogue.service import (
        DialogueCommandRequest,
        DialogueService,
    )

    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    await chat.add_message(
        ChatMessage(
            message_id="assistant-1",
            session_id="thread",
            user_id="user",
            role="assistant",
            content="Which room?",
            metadata={"evidence": {}},
        )
    )
    await service.store.add_task(thread, waiting(), command_id="start")
    request = DialogueCommandRequest(
        id="same", revision=0, action="reply", text="Study"
    )
    await service.submit(thread, "task", request)
    with pytest.raises(DialogueConflict, match="different input"):
        await service.submit(thread, "task", changed(request, text="Kitchen"))
    assert await chat.messages_collection.count_documents({"role": "user"}) == 1


async def test_committed_prompt_delivery_references_one_utterance(chat, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from backend.services.dialogue import voice as module
    from backend.services.dialogue.service import AskUser, DialogueService

    service = DialogueService(chat)
    monkeypatch.setattr(module, "get_dialogue_service", AsyncMock(return_value=service))
    thread = await service.thread("thread", "user")
    session = NS(
        user_id="user",
        client_id="phone",
        interaction_id="engaged",
        started_at=0,
        plugin_state={"thread_id": "thread"},
    )
    message, _ = await service.ask(
        thread,
        "Choose",
        AskUser(title="Choose", prompt="Which room?"),
        run_id="delivery-run",
        evidence={},
    )
    adapter = module.VoiceDialogue()
    assert (await adapter.pending_output(session))["message_id"] == message.message_id
    utterance = await adapter.delivery(session, message.message_id)
    assert utterance.text == "Which room?"
    assert await adapter.delivery(session, message.message_id) is None
    assert await adapter.pending_output(session) is None
    effect = NS(task_id="utterance:" + message.message_id, effect_id="playback")
    await adapter.output(
        session, effect, utterance.text, utterance.text, 480, "response", None
    )
    assert await chat.messages_collection.count_documents({}) == 1
    row = await chat.messages_collection.find_one({"message_id": message.message_id})
    assert row["metadata"]["voice_delivery"]["heard_text"] == utterance.text


async def test_home_assistant_clarifies_before_calling_existing_executor(
    chat, monkeypatch
):
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from backend.services.dialogue import continuations
    from backend.services.dialogue.service import (
        DialogueCommandRequest,
        DialogueService,
        StartTask,
    )
    from backend.services.dialogue.worker import DialogueWorker

    plugin = NS(
        _ensure_cache_initialized=AsyncMock(),
        entity_cache=NS(
            entity_details={"light.study": {"attributes": {"friendly_name": "Study"}}},
            area_entities={},
        ),
        mcp_client=NS(call_service=AsyncMock()),
    )
    monkeypatch.setattr(
        continuations, "plugin_for", AsyncMock(return_value=(None, plugin))
    )
    model = AsyncMock(
        side_effect=[
            NS(
                choices=[
                    NS(message=NS(content=json.dumps({"clarification": "Which room?"})))
                ]
            ),
            NS(
                choices=[
                    NS(
                        message=NS(
                            content=json.dumps(
                                {"action": "turn_on", "entity_ids": ["light.study"]}
                            )
                        )
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr("backend.llm_client.async_chat_with_tools", model)
    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    state = await service.start(
        thread,
        StartTask(kind="home_assistant", title="Lights", request="Turn on the light"),
        command_id="start-ha",
    )
    worker = DialogueWorker(service)
    await worker.process(thread, state.effects[0].id)
    plugin.mcp_client.call_service.assert_not_called()
    task = (await service.snapshot(thread)).tasks[0]
    assert task.status == "awaiting_input"
    await service.submit(
        thread,
        task.id,
        DialogueCommandRequest(
            id="room", revision=task.revision, action="reply", text="Study"
        ),
    )
    await worker.sweep()
    await worker.sweep()
    plugin.mcp_client.call_service.assert_awaited_once_with(
        "light", "turn_on", ["light.study"]
    )
    assert (
        await chat.db.dialogue_tasks.count_documents({"task.status": "completed"}) == 1
    )


async def test_instamart_dialogue_preserves_explicit_checkout_review(chat, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from test_swiggy_instamart_mode import (
        _FakeServices,
        _FakeSwiggy,
        _plugin,
        plugin_module,
    )

    from backend.services.dialogue import continuations
    from backend.services.dialogue.service import (
        DialogueCommandRequest,
        DialogueService,
        StartTask,
    )
    from backend.services.dialogue.worker import DialogueWorker

    client = _FakeSwiggy(existing_cart=True)
    plugin = _plugin(client)
    plugin.linked_user_id = "user"
    monkeypatch.setattr(
        continuations,
        "plugin_for",
        AsyncMock(return_value=(NS(_services=_FakeServices()), plugin)),
    )
    monkeypatch.setattr(
        plugin_module,
        "enqueue_instamart_payment_monitor",
        lambda **kwargs: "payment-job",
    )
    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    worker = DialogueWorker(service)
    state = await service.start(
        thread,
        StartTask(kind="instamart", title="Groceries", request="show cart"),
        command_id="shopping",
    )
    await worker.process(thread, state.effects[0].id)

    async def reply(text):
        task = (await service.snapshot(thread)).tasks[0]
        await service.submit(
            thread,
            task.id,
            DialogueCommandRequest(
                id="input-" + str(task.revision),
                revision=task.revision,
                action="reply",
                text=text,
            ),
        )
        await worker.sweep()
        return (await service.snapshot(thread)).tasks[0]

    task = await reply("हाँ")
    assert task.continuation.phase == "existing_cart_decision"
    task = await reply("keep cart")
    task = await reply("complete order")
    assert task.continuation.phase == "awaiting_confirmation"
    assert task.input_wait.choices[0].id == "confirm_order"
    for text in ["yes", "हाँ", "haan lekin mat karo"]:
        task = await reply(text)
        assert task.continuation.phase == "awaiting_confirmation"
    assert not any(name == "checkout" for name, _ in client.calls)
    task = await reply("confirm order")
    assert task.continuation.phase == "awaiting_payment"
    assert task.confirmation.operation_revision == task.continuation.review_revision
    assert sum(name == "checkout" for name, _ in client.calls) == 1
    checkpoint = await chat.db.dialogue_plugin_checkpoints.find_one({"_id": task.id})
    assert checkpoint["state"]["order_id"] == "order-1"


async def test_hermes_approval_targets_exact_request_and_reconciles_lost_ack(
    chat, monkeypatch
):
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    import httpx

    from backend.services.dialogue import continuations, hermes
    from backend.services.dialogue.service import (
        DialogueCommandRequest,
        DialogueService,
        StartTask,
    )
    from backend.services.dialogue.worker import DialogueWorker

    plugin = NS(api_url="https://hermes.test", api_key="")
    monkeypatch.setattr(
        continuations, "plugin_for", AsyncMock(return_value=(None, plugin))
    )
    calls = []
    approved = []

    def remote(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "remote-run"})
        if request.url.path.endswith("/approval"):
            body = json.loads(request.content)
            approved.append(body)
            if len(approved) == 1:
                raise httpx.ReadError("lost acknowledgement")
            assert approved[0] == body
            return httpx.Response(200, json={"success": True})
        if len(approved) > 1:
            return httpx.Response(200, json={"status": "completed", "output": "Done."})
        return httpx.Response(
            200,
            json={
                "status": "running",
                "pending_input": {
                    "id": "approval-1",
                    "revision": 7,
                    "kind": "approval",
                    "prompt": "Allow this exact command?",
                    "choices": [
                        {"id": "once", "label": "Once"},
                        {"id": "deny", "label": "Deny"},
                    ],
                    "expires_at": (now() + timedelta(minutes=1)).isoformat(),
                },
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        hermes.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(remote), **kwargs),
    )
    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    worker = DialogueWorker(service)
    state = await service.start(
        thread,
        StartTask(
            kind="hermes", title="Run command", request="Run my requested command"
        ),
        command_id="remote",
    )
    await worker.process(thread, state.effects[0].id)
    task = (await service.snapshot(thread)).tasks[0]
    await service.submit(
        thread,
        task.id,
        DialogueCommandRequest(
            id="vague", revision=task.revision, action="reply", text="yes"
        ),
    )
    await worker.sweep()
    assert not approved
    task = (await service.snapshot(thread)).tasks[0]
    await service.submit(
        thread,
        task.id,
        DialogueCommandRequest(
            id="approve", revision=task.revision, action="reply", choice_id="once"
        ),
    )
    item = (await service.store.get(thread)).effects[0]
    with pytest.raises(httpx.ReadError):
        await worker.process(thread, item.id)
    state = await service.store.get(thread)
    await service.store.transition(
        thread,
        lambda state: changed(
            state,
            effects=tuple(
                changed(e, lease_until=now() - timedelta(seconds=1))
                for e in state.effects
            ),
        ),
    )
    await worker.process(thread, item.id)
    await worker.sweep()
    assert approved[0]["request_id"] == "approval-1" and approved[0]["revision"] == 7
    assert approved[0]["choice"] == "once"
    assert calls.count(("POST", "/v1/runs")) == 1
    assert (
        await chat.db.dialogue_tasks.count_documents({"task.status": "completed"}) == 1
    )


async def test_capture_wake_handoff_admits_once_without_dispatching_twice(
    chat, monkeypatch
):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from fakeredis.aioredis import FakeRedis

    from backend.redis_keys import ClientId, SessionId
    from backend.services.audio_stream.session_store import SessionStore
    from backend.services.dialogue import capture, continuations
    from backend.services.dialogue.service import DialogueService
    from backend.services.wakeword.executor import execute_voice_command

    service = DialogueService(chat)
    monkeypatch.setattr(
        capture, "get_dialogue_service", AsyncMock(return_value=service)
    )
    monkeypatch.setattr(capture, "capture_thread_id", lambda *args: "thread")
    monkeypatch.setattr(
        continuations, "plugin_for", AsyncMock(return_value=(None, NS()))
    )
    monkeypatch.setattr(
        SessionStore,
        "read",
        AsyncMock(
            return_value=NS(
                user_id="user",
                client_id="phone",
                voice_session_id="voice",
                capture_epoch=1,
                memory_space_id=None,
            )
        ),
    )
    redis = FakeRedis(decode_responses=True)
    try:
        thread = await capture.accept(
            redis,
            user_id="user",
            client_id="phone",
            capture_id="capture",
            input_id="start",
            text="Lights please",
            kind="home_assistant",
        )
        state = await service.store.get(thread)
        task = state.tasks[0]
        from backend.chat_service import ChatMessage

        await chat.commit_message(
            ChatMessage(
                message_id="prompt",
                session_id="thread",
                user_id="user",
                role="assistant",
                content="Which room?",
            )
        )
        await service.store.replace_task(
            thread,
            changed(
                task,
                status="awaiting_input",
                input_wait=InputWait(after_utterance_id="prompt"),
                revision=1,
            ),
            expected_revision=0,
            command_id="wait",
        )
        from backend.services.dialogue.models import UtteranceInterpretation

        service.interpret = AsyncMock(
            return_value=UtteranceInterpretation(intent="answer", task_id=task.id)
        )
        router = NS(dispatch_event=AsyncMock())
        for _ in range(2):
            assert (
                await execute_voice_command(
                    redis,
                    router,
                    user_id="user",
                    session_id=SessionId.from_value("capture"),
                    client_id=ClientId.from_value("phone"),
                    command="Study",
                    response_turn_id="answer",
                )
                == ""
            )
        router.dispatch_event.assert_not_awaited()
        assert (
            await chat.messages_collection.count_documents(
                {"role": "user", "content": "Study"}
            )
            == 1
        )
        state = await service.store.get(thread)
        assert (
            sum(e.task_revision == 2 and e.kind == "resume" for e in state.effects) == 1
        )
    finally:
        await redis.aclose()


async def test_registered_instamart_mode_hands_off_to_shared_dialogue(
    chat, monkeypatch
):
    import time
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from fakeredis.aioredis import FakeRedis

    from backend.services.audio_stream.session_store import SessionStore
    from backend.services.dialogue import capture, continuations
    from backend.services.dialogue.service import DialogueService
    from backend.services.interaction_modes.contracts import (
        AudioInterval,
        InteractionInput,
        InteractionSession,
    )
    from backend.services.interaction_modes.processor import InteractionProcessor
    from backend.services.response_coordinator import ResponseCoordinator
    from backend.services.voice_sessions import VoiceSessionCoordinator

    service = DialogueService(chat)
    monkeypatch.setattr(
        capture, "get_dialogue_service", AsyncMock(return_value=service)
    )
    monkeypatch.setattr(capture, "capture_thread_id", lambda *args: "thread")
    plugin = NS(
        enabled=True, on_interaction_start=AsyncMock(), on_interaction_turn=AsyncMock()
    )
    monkeypatch.setattr(
        continuations, "plugin_for", AsyncMock(return_value=(None, plugin))
    )
    monkeypatch.setattr(
        SessionStore,
        "read",
        AsyncMock(
            return_value=NS(
                user_id="user",
                client_id="phone",
                voice_session_id="voice",
                capture_epoch=1,
                memory_space_id=None,
            )
        ),
    )
    redis = FakeRedis(decode_responses=True)
    try:
        generation = await ResponseCoordinator(
            redis, VoiceSessionCoordinator(redis)
        ).begin_turn("user", "phone")
        session = InteractionSession(
            interaction_id="mode",
            mode_id="swiggy_order",
            owner_plugin_id="swiggy_instamart",
            user_id="user",
            client_id="phone",
            audio_session_id="capture",
            capture_epoch=1,
            voice_session_id="voice",
            response_generation=generation,
            response_turn_id="turn",
            response_turn_revision=0,
            phase="starting",
            plugin_state={},
            started_at=time.time(),
            last_activity_at=time.time(),
            idle_timeout_seconds=600,
            max_duration_seconds=1800,
        )
        processor = InteractionProcessor(
            redis, NS(plugins={"swiggy_instamart": plugin})
        )
        await processor.store.create(session)
        item = InteractionInput(
            input_id="start",
            interaction_id="mode",
            kind="start",
            user_id="user",
            client_id="phone",
            audio_interval=AudioInterval(
                audio_session_id="capture",
                capture_epoch=1,
                start_ms=0,
                end_ms=1000,
                voice_session_id="voice",
            ),
            text="Get milk",
            source="committed",
            received_at=time.time(),
            response_generation=generation,
        )
        result = await processor.process(item)
        assert result.session.plugin_state == {"thread_id": "thread"}
        assert result.reply is None
        assert await processor.process(item) is None
        plugin.on_interaction_start.assert_not_awaited()
        state = await service.store.get(await service.thread("thread", "user"))
        assert len(state.tasks) == 1 and len(state.effects) == 1
        assert state.tasks[0].continuation.kind == "instamart"
    finally:
        await redis.aclose()


async def test_interpretation_replay_after_task_archive_is_idempotent(
    chat, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.chat_service import ChatMessage
    from backend.services.dialogue.models import UtteranceInterpretation
    from backend.services.dialogue.service import DialogueService

    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    await chat.commit_message(
        ChatMessage(
            message_id="assistant-1",
            session_id="thread",
            user_id="user",
            role="assistant",
            content="Which room?",
        )
    )
    await chat.commit_message(
        ChatMessage(
            message_id="user-1",
            session_id="thread",
            user_id="user",
            role="user",
            content="Cancel",
        )
    )
    await service.store.add_task(thread, waiting(), command_id="start")
    service.interpret = AsyncMock(
        return_value=UtteranceInterpretation(intent="cancel", task_id="task")
    )
    task, _ = await service.prepare_turn(
        thread, "user-1", "Cancel", claim_conversation=False
    )
    state = await service.store.get(thread)
    from backend.services.dialogue.worker import DialogueWorker

    await DialogueWorker(service).process(
        thread, next(e.id for e in state.effects if e.kind == "cancel")
    )
    state = await service.store.get(thread)
    archive = next(e for e in state.effects if e.kind == "archive")
    await service.store.archive(thread, await service.store.claim(thread, archive.id))
    replay, _ = await service.prepare_turn(
        thread, "user-1", "Cancel", claim_conversation=False
    )
    assert replay.id == task.id and replay.status == "cancelled"
    service.interpret.assert_awaited_once()


@pytest.mark.parametrize("terminal", ["done", "cancelled"])
async def test_capture_presentation_waits_for_playback_receipt(
    chat, monkeypatch, terminal
):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from fakeredis.aioredis import FakeRedis

    from backend.chat_service import ChatMessage
    from backend.services.audio_stream.session_store import SessionStore
    from backend.services.dialogue.models import CapturePresentation
    from backend.services.dialogue.service import DialogueService
    from backend.services.dialogue.store import effect
    from backend.services.dialogue.worker import DialogueWorker
    from backend.services.response_coordinator import ResponseCoordinator

    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr("backend.redis_factory.create_async_redis", lambda: redis)
    monkeypatch.setattr(
        SessionStore,
        "read",
        AsyncMock(return_value=NS(voice_session_id="voice", capture_epoch=1)),
    )
    delivery = AsyncMock(return_value=NS(response_id="response"))
    monkeypatch.setattr(
        "backend.services.response_delivery.deliver_text_response", delivery
    )
    monkeypatch.setattr(
        ResponseCoordinator,
        "expire_stalled",
        AsyncMock(
            side_effect=[
                NS(state="playing", response_id="response"),
                NS(state=terminal, response_id="response", rendered_samples=123),
            ]
        ),
    )
    service = DialogueService(chat)
    thread = await service.thread("thread", "user")
    await chat.commit_message(
        ChatMessage(
            message_id="result",
            session_id="thread",
            user_id="user",
            role="assistant",
            content="Which room?",
        )
    )
    task = changed(waiting(), input_wait=InputWait(after_utterance_id="result"))
    await service.store.add_task(thread, task, command_id="start")
    await service.store.bind_audio(thread, "phone", "wake:capture")
    binding = CapturePresentation(
        client_id="phone",
        capture_session_id="capture",
        capture_epoch=1,
        voice_session_id="voice",
        generation=1,
        turn_id="input",
        turn_revision=0,
        expires_at=now() + timedelta(seconds=60),
    )
    item = changed(effect(task, "present"), utterance_id="result")
    await service.store.transition(
        thread,
        lambda state: changed(state, capture_presentation=binding, effects=(item,)),
    )
    await DialogueWorker(service).process(thread, item.id)
    row = await chat.messages_collection.find_one({"message_id": "result"})
    assert row["metadata"]["voice_delivery"]["heard_text"] == (
        "Which room?" if terminal == "done" else ""
    )
    assert row["metadata"]["voice_delivery"]["rendered_samples"] == 123
    assert await chat.messages_collection.count_documents({}) == 1
    delivery.assert_awaited_once()
    await DialogueWorker(service).process(thread, item.id)
    delivery.assert_awaited_once()


async def test_archived_task_cannot_restart_after_bounded_receipts_expire(
    store, thread
):
    task = changed(waiting(), status="completed", input_wait=None)
    from backend.services.dialogue.store import effect

    archive = effect(task, "archive")
    await store.transition(
        thread, lambda state: changed(state, tasks=(task,), effects=(archive,))
    )
    await store.archive(thread, await store.claim(thread, archive.id))
    assert not (await store.get(thread)).applied_commands
    result = await store.add_task(
        thread, changed(task, status="running"), command_id="old-start"
    )
    assert not result.tasks and not result.effects
