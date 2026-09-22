"""Adapt existing wake/mode entry points to dialogue; capture and playback stay external."""

import asyncio
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

import backend.chat_service as chat_service
import backend.plugins.base as base
import backend.redis_factory as redis_factory
import backend.redis_keys as redis_keys
import backend.services.audio_stream.session_store as session_store
import backend.services.privacy as privacy_module
import backend.services.response_coordinator as response_coordinator
import backend.services.response_delivery as response_delivery
import backend.services.voice_sessions as voice_sessions
import backend.services.wakeword.executor as executor

from .models import CapturedSpeech, CapturePresentation, UtteranceDelivery
from .service import StartTask, get_dialogue_service
from .store import changed, now


def capture_thread_id(user_id, capture_id):
    return str(uuid5(NAMESPACE_URL, f"dialogue-capture:{user_id}:{capture_id}"))


async def accept(
    redis,
    *,
    user_id,
    client_id,
    capture_id,
    input_id,
    text,
    kind=None,
    generation=None,
    interval=None,
):

    service = await get_dialogue_service()
    sid = capture_thread_id(user_id, capture_id)
    existing = await service.chat.sessions_collection.find_one(
        {"session_id": sid, "user_id": user_id}
    )
    if existing is None and kind is None:
        return None
    view = await session_store.SessionStore(redis).read(capture_id)
    if (
        view is None
        or view.user_id != user_id
        or view.client_id != client_id
        or not view.voice_session_id
    ):
        raise ValueError("Dialogue capture is no longer bound to this user and device")
    provenance = {"evidence_refs": [{"capture_session_ids": [capture_id]}]}
    await privacy_module.guard_payload(user_id, provenance)
    if existing is None:
        session = chat_service.ChatSession(
            sid,
            user_id,
            title=text[:80],
            memory_space_id=view.memory_space_id or None,
            metadata={"interaction_version": 2},
        )
        await service.chat.sessions_collection.update_one(
            {"_id": sid}, {"$setOnInsert": session.to_dict()}, upsert=True
        )
    thread = await service.thread(sid, user_id)
    if thread.memory_space_id != (view.memory_space_id or None):
        raise ValueError("Capture Memory Space differs from its dialogue")
    if generation is None:
        generation = await response_coordinator.ResponseCoordinator(
            redis, voice_sessions.VoiceSessionCoordinator(redis)
        ).begin_turn(user_id, client_id)
    await service.store.bind_audio(thread, client_id, "wake:" + capture_id)
    binding = CapturePresentation(
        client_id=client_id,
        capture_session_id=capture_id,
        capture_epoch=view.capture_epoch,
        voice_session_id=view.voice_session_id,
        generation=generation,
        turn_id=input_id,
        turn_revision=interval.turn_revision if interval else 0,
        expires_at=now() + timedelta(seconds=60),
    )
    await service.store.transition(
        thread, lambda state: changed(state, capture_presentation=binding)
    )
    identity = str(uuid5(NAMESPACE_URL, f"dialogue-capture-input:{sid}:{input_id}"))
    await service.chat.commit_message(
        chat_service.ChatMessage(
            message_id=identity,
            session_id=sid,
            user_id=user_id,
            role="user",
            content=text,
            memory_space_id=thread.memory_space_id,
            metadata={
                **provenance,
                "source": CapturedSpeech(
                    interval=interval, capture_session_id=capture_id
                ).model_dump(mode="json"),
            },
        )
    )
    task, _ = await service.prepare_turn(
        thread, identity, text, claim_conversation=False
    )
    if task is None and kind is not None:
        await service.start(
            thread,
            StartTask(kind=kind, title=text[:200], request=text),
            command_id=identity + ":start",
        )
    return thread if task is not None or kind is not None else None


async def from_plugin(context, kind):

    if context.services is None:
        raise ValueError("Plugin services are unavailable")
    text = (context.data.get("command") or context.data.get("transcript") or "").strip()
    if not text:
        return None
    thread = await accept(
        context.services._async_redis,
        user_id=context.user_id,
        client_id=context.data["client_id"],
        capture_id=context.data["session_id"],
        input_id=context.data["dialogue_input_id"],
        text=text,
        kind=kind,
        generation=context.data.get("response_generation"),
    )
    return base.PluginResult(
        success=True, message="", data={"thread_id": thread.id}, should_continue=False
    )


async def present(service, thread, item, *, uncertain):

    state = await service.store.get(thread)
    binding = state.capture_presentation
    if (
        uncertain
        or binding is None
        or binding.expires_at <= now()
        or (state.foreground_task_id and state.foreground_task_id != item.task_id)
        or not state.audio_owner
        or state.audio_owner.expires_at <= now()
        or state.audio_owner.engagement_id != "wake:" + binding.capture_session_id
    ):
        await service.store.settle(thread, item.id, item.lease_token)
        return
    redis = redis_factory.create_async_redis()
    try:
        view = await session_store.SessionStore(redis).read(binding.capture_session_id)
        if (
            view is None
            or view.voice_session_id != binding.voice_session_id
            or view.capture_epoch != binding.capture_epoch
        ):
            await service.store.settle(thread, item.id, item.lease_token)
            return
        await service.thread(thread.id, thread.user_id)
        utterance = await service.utterance(thread, item.utterance_id, role="assistant")
        pending = UtteranceDelivery(
            utterance_id=utterance.id, client_id=binding.client_id
        )
        admitted = await service.chat.messages_collection.update_one(
            {
                "message_id": utterance.id,
                "user_id": thread.user_id,
                "metadata.voice_delivery": {"$exists": False},
            },
            {"$set": {"metadata.voice_delivery": pending.model_dump(mode="json")}},
        )
        if not admitted.modified_count:
            await service.store.settle(thread, item.id, item.lease_token)
            return
        privacy = await privacy_module.guard_chat(thread.user_id, thread.id, {})
        coordinator = response_coordinator.ResponseCoordinator(
            redis, voice_sessions.VoiceSessionCoordinator(redis)
        )

        async def deliver():
            record = await response_delivery.deliver_text_response(
                redis,
                redis_keys.ClientId.from_value(binding.client_id),
                redis_keys.SessionId.from_value(binding.capture_session_id),
                utterance.text,
                generation=binding.generation,
                turn_id=binding.turn_id,
                turn_revision=binding.turn_revision,
            )
            if record is None:
                return None
            while True:
                record = await coordinator.expire_stalled(record.response_id)
                if record.state in {"done", "failed", "cancelled", "superseded"}:
                    return record
                await asyncio.sleep(0.1)

        delivery = asyncio.create_task(deliver())
        try:
            while not delivery.done():
                await asyncio.wait({delivery}, timeout=0.25)
                await privacy_module.assert_current(thread.user_id, privacy)
            delivered = delivery.result()
        except BaseException:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            await coordinator.begin_turn(
                thread.user_id, binding.client_id, reason="dialogue_delivery_cancelled"
            )
            raise
        record = delivered
        complete = bool(delivered and delivered.state == "done")
        receipt = changed(
            pending,
            response_id=record.response_id if record else None,
            rendered_samples=delivered.rendered_samples if delivered else 0,
            heard_text=utterance.text if complete else "",
            outcome="delivered" if complete else "interrupted",
        )
        await service.chat.messages_collection.update_one(
            {"message_id": utterance.id, "user_id": thread.user_id},
            {"$set": {"metadata.voice_delivery": receipt.model_dump(mode="json")}},
        )
        if complete:
            await executor.open_followup_window(
                redis, binding.capture_session_id, utterance.text
            )
        await service.store.settle(thread, item.id, item.lease_token)
    finally:
        await redis.aclose()
