"""Voice's adapter to shared dialogue. Capture and playback stay in their owners."""

import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import backend.audio_contract.v2.audio_pb2 as audio_pb2
import backend.chat_service as chat_service
import backend.services.chat_context as chat_context
import backend.services.chat_sources as chat_sources
import backend.services.dialogue.service as service_module
import backend.services.privacy as privacy

from .models import CapturedSpeech, DialogueThread, UtteranceDelivery
from .service import AskUser, StartTask, current_task, get_dialogue_service
from .store import DialogueConflict, changed


class VoiceDialogue:
    async def guard(self, session):
        """Capture one policy revision for the whole provider turn."""

        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        snapshot = await privacy.guard_chat(thread.user_id, thread.id, {})
        await privacy.guard_payload(
            thread.user_id,
            {"evidence_refs": [{"capture_session_ids": [session.audio_session_id]}]},
            snapshot=snapshot,
        )
        checked_at = 0.0

        async def verify():
            nonlocal checked_at
            if time.monotonic() - checked_at >= 0.25:
                await privacy.assert_current(thread.user_id, snapshot)
                checked_at = time.monotonic()

        return verify

    async def pending_output(self, session):
        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        return await service.chat.messages_collection.find_one(
            {
                "session_id": thread.id,
                "user_id": thread.user_id,
                "role": "assistant",
                "timestamp": {
                    "$gte": datetime.fromtimestamp(session.started_at, timezone.utc)
                },
                "metadata.dialogue_task_id": {"$exists": True},
                "metadata.voice_delivery": {"$exists": False},
            },
            sort=[("timestamp", 1), ("message_id", 1)],
        )

    async def delivery(self, session, utterance_id):
        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        utterance = await service.utterance(thread, utterance_id, role="assistant")
        delivery = UtteranceDelivery(
            utterance_id=utterance.id, client_id=session.client_id
        )
        result = await service.chat.messages_collection.update_one(
            {
                "message_id": utterance.id,
                "session_id": thread.id,
                "user_id": thread.user_id,
                "metadata.voice_delivery": {"$exists": False},
            },
            {"$set": {"metadata.voice_delivery": delivery.model_dump(mode="json")}},
        )
        return utterance if result.modified_count else None

    async def open(
        self, user_id, memory_space_id, client_id, engagement_id, thread_id=None
    ):

        service = await get_dialogue_service()
        if not thread_id:
            chat = await service.chat.create_session(
                user_id, memory_space_id=memory_space_id
            )
            thread_id = chat.session_id
        thread = await service.thread(thread_id, user_id)
        if thread.memory_space_id != memory_space_id:
            raise ValueError("Voice and dialogue must use the same Memory Space")
        await service.store.bind_audio(thread, client_id, engagement_id)
        messages = await service.chat.get_session_messages(thread.id, user_id, limit=30)
        history = []
        row = await service.chat.sessions_collection.find_one(
            {"session_id": thread.id, "user_id": user_id}
        )
        refs = [
            chat_sources.ChatSourceRef.model_validate(r)
            for r in row.get("metadata", {}).get("sources", [])
        ]
        if refs:
            context = (
                await chat_context.resolve_context(refs, user_id, memory_space_id)
            ).for_turn("", budget=12000)
            history.append(
                {
                    "role": "system",
                    "content": context.prompt(),
                    "_voice_source_context": context.model_dump(mode="json"),
                }
            )
        for message in messages:
            delivery = message.metadata.get("voice_delivery")
            text = delivery.get("heard_text", "") if delivery else message.content
            if text:
                history.append({"role": message.role, "content": text})
        return thread.id, history

    async def close(self, session):
        service = await get_dialogue_service()
        thread = DialogueThread(
            id=session.plugin_state["thread_id"],
            user_id=session.user_id,
            memory_space_id=session.plugin_state.get("memory_space_id"),
        )
        await service.store.release_audio(thread, session.interaction_id)

    async def control(self, session, command, marker):

        action = {
            audio_pb2.CONVERSATION_ACTION_PAUSE_TASK: "pause",
            audio_pb2.CONVERSATION_ACTION_RESUME_TASK: "resume",
            audio_pb2.CONVERSATION_ACTION_CANCEL_TASK: "cancel",
        }[command.action]
        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        return await service.submit(
            thread,
            command.task_id,
            service_module.DialogueCommandRequest(
                id=marker, revision=command.task_revision, action=action
            ),
        )

    async def heartbeat(self, session):
        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        await service.store.bind_audio(
            thread, session.client_id, session.interaction_id
        )

    async def input(self, session, effect, text, interval):

        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        identity = str(uuid5(NAMESPACE_URL, f"dialogue-voice-user:{effect.task_id}"))
        provenance = {
            "evidence_refs": [{"capture_session_ids": [interval.audio_session_id]}]
        }
        await privacy.guard_payload(thread.user_id, provenance)
        message = chat_service.ChatMessage(
            message_id=identity,
            session_id=thread.id,
            user_id=thread.user_id,
            role="user",
            content=text,
            memory_space_id=thread.memory_space_id,
            metadata={
                "source": CapturedSpeech(interval=interval).model_dump(mode="json"),
                **provenance,
            },
        )
        saved = await service.chat.commit_message(message)
        task, claimed = await service.prepare_turn(thread, identity, text)
        return (task, claimed) if task else None

    async def output(
        self,
        session,
        effect,
        text,
        heard,
        samples,
        response_id,
        error,
        continuation=None,
    ):

        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        existing_id = (
            effect.task_id.removeprefix("utterance:")
            if effect.task_id.startswith("utterance:")
            else None
        )
        identity = existing_id or str(
            uuid5(NAMESPACE_URL, f"dialogue-voice-assistant:{effect.effect_id}")
        )
        if text:
            context = next(
                (
                    chat_context.ChatContext.model_validate(m["_voice_source_context"])
                    for m in session.plugin_state.get("history", [])
                    if "_voice_source_context" in m
                ),
                chat_context.ChatContext(),
            )
            notes = [
                chat_context.VaultNoteEvidence.model_validate(note)
                for task in session.plugin_state.get("tasks", {}).values()
                for note in task.get("result", {}).get("evidence", [])
            ]
            delivery = UtteranceDelivery(
                utterance_id=identity,
                client_id=session.client_id,
                response_id=response_id,
                rendered_samples=samples,
                heard_text=heard,
                outcome="interrupted" if error else "delivered",
            )
            message = chat_service.ChatMessage(
                message_id=identity,
                session_id=thread.id,
                user_id=thread.user_id,
                role="assistant",
                content=text,
                memory_space_id=thread.memory_space_id,
                metadata={
                    "evidence": context.evidence(text, notes, []),
                    "voice_delivery": delivery.model_dump(mode="json"),
                    "utterance_outcome": "interrupted" if error else "completed",
                },
            )
            if existing_id:
                await service.chat.messages_collection.update_one(
                    {
                        "message_id": identity,
                        "session_id": thread.id,
                        "user_id": thread.user_id,
                    },
                    {
                        "$set": {
                            "metadata.voice_delivery": delivery.model_dump(mode="json")
                        }
                    },
                )
            else:
                await service.chat.commit_message(message)
        if continuation:
            original, claimed = continuation
            state = await service.store.get(thread)
            task = next((t for t in state.tasks if t.id == original.id), None)
            if (
                task
                and task.continuation.kind == "conversation"
                and task.status == "running"
                and task.revision == original.revision
            ):
                await service.store.replace_task(
                    thread,
                    changed(
                        task,
                        status="failed" if error else "completed",
                        revision=task.revision + 1,
                    ),
                    expected_revision=task.revision,
                    command_id=f"voice-result:{effect.effect_id}",
                )
            if claimed:
                await service.store.settle(thread, claimed.id, claimed.lease_token)
        if text and not error and not existing_id:
            await service.return_offer(
                thread, run_id=effect.effect_id, language_text=text
            )
        return identity

    async def tool(self, session, effect, call, continuation=None):

        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        if continuation and not continuation[1]:
            return {
                "status": continuation[0].status,
                "task_id": continuation[0].id,
                "answer": "This input was already routed to the task. Wait for its result; do not start it again.",
            }
        if call.name == "ask_user":
            token = current_task.set(continuation[0] if continuation else None)
            try:
                args = AskUser.model_validate(call.arguments)
                prompt, _ = await service.ask(
                    thread,
                    session.plugin_state.get("transcript") or args.title,
                    args,
                    run_id=effect.effect_id + ":" + call.call_id,
                    evidence=chat_context.ChatContext().evidence(args.prompt, [], []),
                )
                return {
                    "status": "awaiting_input",
                    "answer": args.prompt,
                    "utterance_id": prompt.message_id,
                }
            finally:
                current_task.reset(token)
        args = (
            StartTask.model_validate(call.arguments)
            if call.name == "start_task"
            else StartTask(
                kind="hermes",
                title=call.arguments["request"][:200],
                request=call.arguments["request"],
            )
        )
        state = await service.start(
            thread, args, command_id=effect.effect_id + ":" + call.call_id
        )
        return {
            "status": "running",
            "task_id": state.foreground_task_id,
            "answer": "The task is running in this dialogue thread. Do not claim it has finished.",
        }

    async def hints(self, session):
        service = await get_dialogue_service()
        thread = await service.thread(
            session.plugin_state["thread_id"], session.user_id
        )
        state = await service.store.get(thread)
        task = next((t for t in state.tasks if t.id == state.foreground_task_id), None)
        if task is None or task.input_wait is None:
            return None
        return (
            ", ".join(choice.label for choice in task.input_wait.choices)[:1000] or None
        )
