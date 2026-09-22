"""Single-document task transitions and effect admission for standalone Mongo.

Redis delivery is only a hint. A lost notification cannot lose committed work.
Transition callbacks are pure and may be replayed after a competing writer wins.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pymongo.errors as errors

from .models import (
    TERMINAL_STATUSES,
    ActionConfirmation,
    AudioOwner,
    CommandReceipt,
    DialogueEffect,
    DialogueState,
    DialogueTask,
    DialogueThread,
    TaskCommand,
)


class DialogueConflict(ValueError):
    """The requested task revision or audio owner is no longer current."""


def now():
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def changed(model, **updates):
    """Unlike model_copy(update=...), validate every state transition."""
    return type(model).model_validate({**model.model_dump(), **updates})


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def replayed(state, identity, digest=None):
    if identity not in state.applied_commands:
        return False
    receipt = next((r for r in state.command_receipts if r.id == identity), None)
    if digest is not None and (receipt is None or receipt.fingerprint != digest):
        raise DialogueConflict("Command identity was already used with different input")
    return True


def _decode(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def effect(task, kind):
    return DialogueEffect(
        id=str(
            uuid5(
                NAMESPACE_URL,
                f"dialogue:{task.thread_id}:{task.id}:{task.revision}:{kind}",
            )
        ),
        task_id=task.id,
        task_revision=task.revision,
        kind=kind,
        archived_task=task if kind == "archive" else None,
    )


class DialogueStore:
    def __init__(self, db):
        self.db = db
        self.collection = db.dialogue_state

    async def get(self, thread: DialogueThread) -> DialogueState:
        row = await self.collection.find_one({"_id": thread.id})
        if row is None:
            return DialogueState(thread=thread)
        state = DialogueState.model_validate(_decode(row))
        if state.thread != thread:
            raise DialogueConflict("Dialogue ownership or Memory Space changed")
        return state

    async def transition(self, thread, update, *, command_id=None, command_digest=None):

        initial = DialogueState(thread=thread)
        try:
            await self.collection.update_one(
                {"_id": thread.id},
                {"$setOnInsert": initial.model_dump(mode="python")},
                upsert=True,
            )
        except errors.DuplicateKeyError:
            pass
        for _ in range(32):
            state = await self.get(thread)
            if command_id and replayed(state, command_id, command_digest):
                return state
            result = update(state)
            if result == state:
                return state
            commands = state.applied_commands
            receipts = state.command_receipts
            if command_id:
                commands = (*commands, command_id)[-128:]
                if command_digest:
                    receipts = (
                        *receipts,
                        CommandReceipt(id=command_id, fingerprint=command_digest),
                    )[-128:]
                receipts = tuple(r for r in receipts if r.id in commands)
            result = changed(
                result,
                revision=state.revision + 1,
                applied_commands=commands,
                command_receipts=receipts,
            )
            saved = await self.collection.replace_one(
                {
                    "_id": thread.id,
                    "revision": state.revision,
                    "thread.user_id": thread.user_id,
                },
                {"_id": thread.id, **result.model_dump(mode="python")},
            )
            if saved.modified_count:
                return result
        raise DialogueConflict("Dialogue changed concurrently; reload and retry")

    async def add_task(self, thread, task, *, command_id):
        archived = await self.db.dialogue_tasks.find_one(
            {"_id": task.id, "user_id": thread.user_id}
        )
        if archived is not None:
            if archived["task"]["thread_id"] != thread.id:
                raise DialogueConflict("Archived task belongs to another thread")
            return await self.get(thread)

        def update(state):
            existing = next((t for t in state.tasks if t.id == task.id), None)
            if existing:
                if existing != task:
                    raise DialogueConflict("Task identity was already used")
                return state
            active_tasks = tuple(
                t for t in state.tasks if t.status not in TERMINAL_STATUSES
            )
            if len(active_tasks) >= 8:
                raise DialogueConflict(
                    "Resolve an unfinished task before starting another"
                )
            tasks = tuple(
                (
                    changed(t, status="paused", revision=t.revision + 1)
                    if t.id == state.foreground_task_id
                    and t.status in {"running", "awaiting_input"}
                    else t
                )
                for t in active_tasks
            )
            effects = state.effects
            for terminal in state.tasks:
                if terminal.status in TERMINAL_STATUSES and not any(
                    e.kind == "archive" and e.task_id == terminal.id for e in effects
                ):
                    effects = (*effects, effect(terminal, "archive"))
            effects = (
                (*effects, effect(task, "resume"))
                if task.status == "running"
                else effects
            )
            return changed(
                state, tasks=(*tasks, task), foreground_task_id=task.id, effects=effects
            )

        return await self.transition(thread, update, command_id=command_id)

    async def command(self, thread, command: TaskCommand, *, digest=None):
        def update(state):
            target = next((t for t in state.tasks if t.id == command.task_id), None)
            if target is None or target.revision != command.revision:
                raise DialogueConflict("Task changed; reload before replying")
            if target.status in TERMINAL_STATUSES:
                raise DialogueConflict("Task is already resolved")
            wait = target.input_wait
            if wait and wait.expires_at and wait.expires_at <= now():
                raise DialogueConflict("Input wait expired")
            if command.action == "reply":
                if not wait or not command.utterance_id:
                    raise DialogueConflict(
                        "Reply requires a waiting task and a user utterance"
                    )
                if command.choice_id and command.choice_id not in {
                    c.id for c in wait.choices
                }:
                    raise ValueError("Choice does not belong to this input wait")
                target = changed(
                    target,
                    status="running",
                    input_wait=None,
                    reply_utterance_id=command.utterance_id,
                    revision=target.revision + 1,
                )
                continuation = target.continuation
                if (
                    continuation.kind == "hermes"
                    and continuation.pending_kind == "approval"
                    and command.choice_id in {"once", "session", "always"}
                ):
                    target = changed(
                        target,
                        confirmation=ActionConfirmation(
                            operation_id=continuation.pending_request_id,
                            operation_revision=str(continuation.pending_revision),
                            accepted_by_utterance_id=command.utterance_id,
                        ),
                    )
            elif command.action == "resume":
                if target.status != "paused":
                    raise DialogueConflict("Only a paused task can resume")
                if not wait and any(
                    e.task_id == target.id
                    and e.kind == "resume"
                    and e.status in {"claimed", "uncertain"}
                    for e in state.effects
                ):
                    raise DialogueConflict(
                        "The previous execution must finish or reconcile before resuming"
                    )
                target = changed(
                    target,
                    status="awaiting_input" if wait else "running",
                    revision=target.revision + 1,
                )
            elif command.action == "pause":
                target = changed(target, status="paused", revision=target.revision + 1)
            else:
                target = changed(
                    target,
                    status=(
                        "running"
                        if target.continuation.kind == "hermes"
                        else "cancelled"
                    ),
                    input_wait=None,
                    revision=target.revision + 1,
                )
            foreground = state.foreground_task_id
            tasks = []
            for task in state.tasks:
                if task.id == target.id:
                    tasks.append(target)
                elif (
                    command.action in {"reply", "resume"}
                    and task.status == "awaiting_input"
                ):
                    tasks.append(
                        changed(task, status="paused", revision=task.revision + 1)
                    )
                else:
                    tasks.append(task)
            if command.action in {"reply", "resume"}:
                foreground = target.id
            elif foreground == target.id:
                foreground = None
            effects = state.effects
            if command.action == "reply" or (command.action == "resume" and not wait):
                effects = (*effects, effect(target, "resume"))
            elif command.action == "cancel":
                effects = (*effects, effect(target, "cancel"))
            return changed(
                state,
                tasks=tuple(tasks),
                foreground_task_id=foreground,
                effects=effects,
            )

        return await self.transition(
            thread,
            update,
            command_id=command.id,
            command_digest=digest or fingerprint(command.model_dump(mode="json")),
        )

    async def replace_task(
        self, thread, task, *, expected_revision, command_id, utterance_id=None
    ):
        def update(state):
            previous = next((t for t in state.tasks if t.id == task.id), None)
            if previous is None or previous.revision != expected_revision:
                raise DialogueConflict("Task changed before continuation completed")
            if task.revision != expected_revision + 1:
                raise ValueError("Task revisions must advance by one")
            tasks = tuple(task if t.id == task.id else t for t in state.tasks)
            foreground = state.foreground_task_id
            effects = state.effects
            if (
                utterance_id
                and state.capture_presentation
                and state.capture_presentation.expires_at > now()
            ):
                effects = (
                    *effects,
                    changed(effect(task, "present"), utterance_id=utterance_id),
                )
            if task.status in TERMINAL_STATUSES:
                effects = (*effects, effect(task, "archive"))
                if foreground == task.id:
                    foreground = None
            return changed(
                state, tasks=tasks, effects=effects, foreground_task_id=foreground
            )

        return await self.transition(thread, update, command_id=command_id)

    async def expire(self, thread):
        def update(state):
            tasks, effects = [], list(state.effects)
            foreground = state.foreground_task_id
            for task in state.tasks:
                if (
                    task.input_wait
                    and task.input_wait.expires_at
                    and task.input_wait.expires_at <= now()
                ):
                    task = changed(
                        task,
                        status="stale",
                        input_wait=None,
                        revision=task.revision + 1,
                    )
                    effects.append(effect(task, "archive"))
                    if foreground == task.id:
                        foreground = None
                tasks.append(task)
            owner = state.audio_owner
            if owner and owner.expires_at <= now():
                owner = None
            return changed(
                state,
                tasks=tuple(tasks),
                effects=tuple(effects),
                foreground_task_id=foreground,
                audio_owner=owner,
            )

        return await self.transition(thread, update)

    async def offer_return(self, thread, *, language="en"):
        """Reserve a single return offer durably before any presentation."""
        selected = []

        def update(state):
            selected.clear()
            if state.foreground_task_id:
                return state
            task = next(
                (
                    t
                    for t in reversed(state.tasks)
                    if t.status == "paused" and not t.return_offered
                ),
                None,
            )
            if task is None:
                return state
            selected.append(task)
            return changed(
                state,
                effects=(
                    *state.effects,
                    changed(effect(task, "return"), language=language),
                ),
                tasks=tuple(
                    (
                        changed(t, return_offered=True, revision=t.revision + 1)
                        if t.id == task.id
                        else t
                    )
                    for t in state.tasks
                ),
            )

        await self.transition(thread, update)
        return selected[0] if selected else None

    async def bind_audio(self, thread, client_id, engagement_id):
        def update(state):
            owner = state.audio_owner
            if (
                owner
                and owner.expires_at > now()
                and (owner.client_id, owner.engagement_id) != (client_id, engagement_id)
            ):
                raise DialogueConflict(
                    "End the other device's voice engagement before resuming here"
                )
            return changed(
                state,
                audio_owner=AudioOwner(
                    client_id=client_id,
                    engagement_id=engagement_id,
                    expires_at=now() + timedelta(seconds=60),
                ),
            )

        return await self.transition(thread, update)

    async def release_audio(self, thread, engagement_id):
        return await self.transition(
            thread,
            lambda state: (
                changed(state, audio_owner=None)
                if state.audio_owner
                and state.audio_owner.engagement_id == engagement_id
                else state
            ),
        )

    async def claim(self, thread, effect_id, *, reconcile=False):
        token = str(uuid4())
        selected = []

        def update(state):
            selected.clear()
            effects = []
            for item in state.effects:
                if item.id == effect_id:
                    if item.not_before and item.not_before > now():
                        effects.append(item)
                        continue
                    if (
                        item.status == "claimed"
                        and item.lease_until <= now()
                        and item.kind != "archive"
                    ):
                        # External work might already have happened. Never blind-replay it.
                        item = changed(
                            item, status="uncertain", lease_token=None, lease_until=None
                        )
                    elif (
                        item.status == "pending"
                        or (item.kind == "archive" and item.lease_until <= now())
                        or (reconcile and item.status == "uncertain")
                    ):
                        item = changed(
                            item,
                            status="claimed",
                            lease_token=token,
                            lease_until=now() + timedelta(seconds=90),
                        )
                        selected.append(item)
                effects.append(item)
            return changed(state, effects=tuple(effects))

        await self.transition(thread, update)
        return selected[0] if selected else None

    async def checkpoint_task(self, thread, task, item):
        def update(state):
            previous = next((t for t in state.tasks if t.id == task.id), None)
            owned = next((e for e in state.effects if e.id == item.id), None)
            if previous is None or task.revision != previous.revision + 1:
                raise DialogueConflict("Task changed before checkpoint")
            if (
                owned is None
                or owned.lease_token != item.lease_token
                or owned.status != "claimed"
                or owned.lease_until <= now()
            ):
                raise DialogueConflict("Checkpoint effect lease expired")
            return changed(
                state,
                tasks=tuple(task if t.id == task.id else t for t in state.tasks),
                effects=tuple(
                    changed(e, task_revision=task.revision) if e.id == item.id else e
                    for e in state.effects
                ),
            )

        state = await self.transition(thread, update)
        return task, next(e for e in state.effects if e.id == item.id)

    async def defer(self, thread, task, item, *, seconds=2):
        def update(state):
            previous = next((t for t in state.tasks if t.id == task.id), None)
            owned = next((e for e in state.effects if e.id == item.id), None)
            if previous is None or previous.revision != task.revision:
                raise DialogueConflict("Task changed before polling checkpoint")
            if (
                owned is None
                or owned.lease_token != item.lease_token
                or owned.lease_until <= now()
            ):
                raise DialogueConflict("Polling effect lease expired")
            next_task = changed(task, revision=task.revision + 1)
            next_effect = changed(
                effect(next_task, "resume"),
                not_before=now() + timedelta(seconds=seconds),
            )
            return changed(
                state,
                tasks=tuple(next_task if t.id == task.id else t for t in state.tasks),
                effects=(*(e for e in state.effects if e.id != item.id), next_effect),
            )

        return await self.transition(thread, update)

    async def renew(self, thread, effect_id, token):
        def update(state):
            item = next((e for e in state.effects if e.id == effect_id), None)
            if (
                item is None
                or item.status != "claimed"
                or item.lease_token != token
                or item.lease_until <= now()
            ):
                raise DialogueConflict("Effect execution lease is no longer owned")
            return changed(
                state,
                effects=tuple(
                    (
                        changed(e, lease_until=now() + timedelta(seconds=90))
                        if e.id == effect_id
                        else e
                    )
                    for e in state.effects
                ),
            )

        return await self.transition(thread, update)

    async def settle(self, thread, effect_id, token):
        def update(state):
            item = next((e for e in state.effects if e.id == effect_id), None)
            if item is None:
                return state
            if (
                item.lease_token != token
                or item.status != "claimed"
                or item.lease_until <= now()
            ):
                raise DialogueConflict("Effect execution lease is no longer owned")
            tasks = state.tasks
            if item.kind == "archive":
                tasks = tuple(
                    t
                    for t in tasks
                    if not (t.id == item.task_id and t.revision == item.task_revision)
                )
            return changed(
                state,
                tasks=tasks,
                effects=tuple(e for e in state.effects if e.id != effect_id),
            )

        return await self.transition(thread, update)

    async def archive(self, thread, item):
        if item.kind != "archive" or item.archived_task is None:
            raise ValueError("Archive effect requires a terminal task snapshot")
        await self.db.dialogue_tasks.update_one(
            {"_id": item.task_id, "user_id": thread.user_id},
            {
                "$setOnInsert": {
                    "user_id": thread.user_id,
                    "task": item.archived_task.model_dump(mode="python"),
                }
            },
            upsert=True,
        )
        await self.settle(thread, item.id, item.lease_token)
