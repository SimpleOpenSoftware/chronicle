"""Adapters to existing action owners; this module owns no capture or playback."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

import backend.chat_service as chat_service
import backend.llm_client as llm_client
import backend.services.chat_context as chat_context
import backend.services.interaction_modes.contracts as contracts
import backend.services.plugin_service as plugin_service
import backend.services.privacy as privacy_module

from .models import (
    ActionConfirmation,
    DialogueModel,
    InputWait,
    InstamartContinuation,
    ReplyChoice,
)
from .store import DialogueConflict, changed, now


async def plugin_for(kind):

    router = await plugin_service.ensure_plugin_router()
    identity = {
        "home_assistant": "homeassistant",
        "instamart": "swiggy_instamart",
        "hermes": "hermes",
    }[kind]
    plugin = router.plugins.get(identity) if router else None
    if plugin is None or not plugin.enabled:
        raise ValueError(f"{identity} is not enabled")
    return router, plugin


async def require_current(service, thread, task, item):
    await service.thread(thread.id, thread.user_id)
    if await service.chat.db.dialogue_tasks.find_one(
        {"_id": task.id, "user_id": thread.user_id}
    ):
        raise DialogueConflict("This task already has a terminal archive")
    state = await service.store.get(thread)
    current = next((t for t in state.tasks if t.id == task.id), None)
    if current is None or current.revision != task.revision:
        raise DialogueConflict("Task changed before external work")
    lease = next((e for e in state.effects if e.id == item.id), None)
    if (
        lease is None
        or lease.status != "claimed"
        or lease.lease_token != item.lease_token
        or lease.lease_until <= now()
    ):
        raise DialogueConflict("External work no longer owns its execution lease")
    return state


async def publish_result(
    service,
    thread,
    task,
    item,
    text,
    *,
    wait=False,
    choices=(),
    expires_at=None,
    continuation=None,
    status="completed",
):

    state = await require_current(service, thread, task, item)
    utterance_id = str(uuid5(NAMESPACE_URL, f"dialogue-result:{item.id}"))
    message = chat_service.ChatMessage(
        message_id=utterance_id,
        session_id=thread.id,
        user_id=thread.user_id,
        role="assistant",
        content=text,
        memory_space_id=thread.memory_space_id,
        metadata={
            "dialogue_task_id": task.id,
            "evidence": chat_context.ChatContext().evidence(text, [], []),
        },
    )
    # The outbox identity is the message identity: crash recovery cannot duplicate a result.
    await service.chat.commit_message(message)
    next_task = changed(
        task,
        revision=task.revision + 1,
        status=(
            ("awaiting_input" if state.foreground_task_id == task.id else "paused")
            if wait
            else status
        ),
        input_wait=(
            InputWait(
                after_utterance_id=utterance_id, choices=choices, expires_at=expires_at
            )
            if wait
            else None
        ),
        continuation=continuation or task.continuation,
    )
    await service.store.replace_task(
        thread,
        next_task,
        expected_revision=task.revision,
        command_id=f"result:{item.id}",
        utterance_id=utterance_id,
    )
    await service.store.settle(thread, item.id, item.lease_token)
    if not wait:
        await service.return_offer(thread, run_id=item.id, language_text=text)
    return next_task


class HomeAction(DialogueModel):
    action: (
        Literal[
            "turn_on",
            "turn_off",
            "toggle",
            "open_cover",
            "close_cover",
            "set_temperature",
        ]
        | None
    ) = None
    entity_ids: tuple[str, ...] = ()
    clarification: str | None = None
    brightness_pct: int | None = Field(default=None, ge=0, le=100)
    temperature: float | None = None


async def home_assistant(service, thread, task, item, plugin):

    if item.kind == "cancel":
        return await publish_result(
            service,
            thread,
            task,
            item,
            "Home Assistant task cancelled.",
            status="cancelled",
        )
    await plugin._ensure_cache_initialized()
    cache = plugin.entity_cache
    if cache is None or not cache.entity_details:
        raise ValueError("Home Assistant entities are unavailable")
    reply = (
        await service.utterance(thread, task.reply_utterance_id, role="user")
        if task.reply_utterance_id
        else None
    )
    entities = [
        {"id": key, "name": value.get("attributes", {}).get("friendly_name", key)}
        for key, value in cache.entity_details.items()
    ]
    privacy = await privacy_module.guard_chat(thread.user_id, thread.id, {})
    response = await llm_client.async_chat_with_tools(
        messages=[
            {
                "role": "system",
                "content": (
                    "Resolve a requested Home Assistant operation using only the supplied entity IDs. "
                    "If the target, scope or action is missing or ambiguous, return clarification in the user's language and no action. "
                    "Never interpret a negated command as permission to execute it. Return JSON matching "
                    + json.dumps(HomeAction.model_json_schema())
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "request": task.continuation.request,
                        "reply": reply.text if reply else None,
                        "entities": entities,
                        "areas": cache.area_entities,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        tools=None,
        operation="plugin_assistant",
        timeout_seconds=30,
    )
    action = HomeAction.model_validate_json(response.choices[0].message.content or "")
    await privacy_module.assert_current(thread.user_id, privacy)
    if action.clarification or not action.action or not action.entity_ids:
        return await publish_result(
            service,
            thread,
            task,
            item,
            action.clarification or "Which device or room do you mean?",
            wait=True,
        )
    if any(identity not in cache.entity_details for identity in action.entity_ids):
        raise ValueError("Home Assistant returned an unknown entity")
    await require_current(service, thread, task, item)
    parameters = {
        key: value
        for key, value in {
            "brightness_pct": action.brightness_pct,
            "temperature": action.temperature,
        }.items()
        if value is not None
    }
    # Existing HA client remains the action executor. Each effect is leased before calls.
    for domain in sorted({identity.split(".")[0] for identity in action.entity_ids}):
        await require_current(service, thread, task, item)
        await privacy_module.assert_current(thread.user_id, privacy)
        await plugin.mcp_client.call_service(
            domain,
            action.action,
            [
                identity
                for identity in action.entity_ids
                if identity.startswith(domain + ".")
            ],
            **parameters,
        )
    return await publish_result(
        service,
        thread,
        task,
        item,
        f"Home Assistant completed {action.action.replace('_', ' ')} for {len(action.entity_ids)} device(s).",
    )


async def instamart(service, thread, task, item, router, plugin):

    collection = service.chat.db.dialogue_plugin_checkpoints
    saved = await collection.find_one(
        {
            "_id": task.continuation.checkpoint_id,
            "user_id": thread.user_id,
            "thread_id": thread.id,
        }
    )
    if saved is None:
        raise ValueError("Instamart checkpoint is unavailable")
    if item.kind == "cancel":
        return await publish_result(
            service,
            thread,
            task,
            item,
            "Order dialogue cancelled. Existing cart and any placed order remain unchanged.",
            status="cancelled",
        )
    reply = (
        await service.utterance(thread, task.reply_utterance_id, role="user")
        if task.reply_utterance_id
        else None
    )
    timestamp = time.time()
    session = contracts.InteractionSession(
        interaction_id=task.id,
        mode_id="swiggy_order",
        owner_plugin_id="swiggy_instamart",
        user_id=thread.user_id,
        client_id="",
        audio_session_id="",
        capture_epoch=0,
        voice_session_id=None,
        response_generation=0,
        response_turn_id=reply.id if reply else item.id,
        response_turn_revision=task.revision,
        phase=saved["phase"],
        plugin_state=saved["state"],
        started_at=timestamp,
        last_activity_at=timestamp,
        idle_timeout_seconds=600,
        max_duration_seconds=1800,
    )

    async def checkpoint():
        await require_current(service, thread, task, item)
        await collection.update_one(
            {"_id": task.continuation.checkpoint_id, "user_id": thread.user_id},
            {
                "$set": {
                    "phase": session.phase,
                    "state": session.plugin_state,
                    "effect_id": item.id,
                }
            },
        )

    context = contracts.InteractionContext(
        session=session,
        input=contracts.DialoguePluginInput(
            input_id=reply.id if reply else item.id,
            text=reply.text if reply else saved["request"],
        ),
        services=router._services,
        checkpoint=checkpoint,
        dialogue_thread_id=thread.id,
    )
    if (
        saved["phase"] == "awaiting_confirmation"
        and reply
        and reply.text.strip().casefold() == "confirm order"
    ):
        task, item = await service.store.checkpoint_task(
            thread,
            changed(
                task,
                confirmation=ActionConfirmation(
                    operation_id=task.id,
                    operation_revision=saved["state"]["review_fingerprint"],
                    accepted_by_utterance_id=reply.id,
                ),
                revision=task.revision + 1,
            ),
            item,
        )
    result = await (
        plugin.on_interaction_start(context)
        if saved["phase"] == "start"
        else plugin.on_interaction_turn(context)
    )
    if result is None:
        raise ValueError("Instamart returned no result")
    session.phase = result.phase or session.phase
    session.plugin_state = (
        result.plugin_state if result.plugin_state is not None else session.plugin_state
    )
    await checkpoint()
    continuation = InstamartContinuation(
        checkpoint_id=task.continuation.checkpoint_id,
        phase=session.phase,
        review_revision=session.plugin_state.get("review_fingerprint"),
    )
    expires = None
    if session.phase == "awaiting_confirmation":
        expires = now() + timedelta(seconds=plugin.review_valid_seconds)
    choices = {
        "confirm_address": (
            ReplyChoice(id="yes", label="Yes"),
            ReplyChoice(id="no", label="Change address"),
        ),
        "existing_cart_decision": (
            ReplyChoice(id="keep", label="Keep cart"),
            ReplyChoice(id="clear", label="Clear cart"),
        ),
        "awaiting_confirmation": (
            ReplyChoice(id="confirm_order", label="Confirm order"),
        ),
    }.get(session.phase, ())
    text = result.reply or "Order task updated."
    if result.event_data.get("payment_url"):
        text += "\n" + result.event_data["payment_url"]
    return await publish_result(
        service,
        thread,
        task,
        item,
        text,
        wait=not result.end,
        choices=choices,
        expires_at=expires,
        continuation=continuation,
        status=(
            "stale"
            if result.end_reason
            in {"checkout_outcome_unknown", "checkout_tracking_missing"}
            else "completed"
        ),
    )


async def execute_continuation(service, thread, task, item):
    router, plugin = await plugin_for(task.continuation.kind)
    if task.continuation.kind == "home_assistant":
        await home_assistant(service, thread, task, item, plugin)
    elif task.continuation.kind == "instamart":
        await instamart(service, thread, task, item, router, plugin)
    else:
        # Defer this dependency to break the import cycle through backend.services.dialogue.hermes
        # -> backend.services.dialogue.continuations.
        from .hermes import execute_hermes

        await execute_hermes(service, thread, task, item, plugin)
