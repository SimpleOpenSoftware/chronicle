"""Recoverable task effects; waiting never occupies a worker slot."""

import asyncio
import logging
from contextlib import aclosing

import backend.services.dialogue.capture as capture
import backend.services.dialogue.continuations as continuations

from .models import TERMINAL_STATUSES, DialogueThread
from .service import get_dialogue_service
from .store import _decode, changed, now

logger = logging.getLogger(__name__)


class DialogueWorker:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.running = False

    async def process(self, thread, effect_id):
        await self.service.thread(thread.id, thread.user_id)
        item = await self.store.claim(thread, effect_id)
        reconciling = False
        if item is None:
            state = await self.store.get(thread)
            uncertain = next(
                (
                    e
                    for e in state.effects
                    if e.id == effect_id and e.status == "uncertain"
                ),
                None,
            )
            if uncertain is None:
                return
            item = await self.store.claim(thread, effect_id, reconcile=True)
            if item is None:
                return
            reconciling = True
        if item.kind == "return":
            await self.service.record_return(thread, item)
            return
        if item.kind == "archive":
            await self.store.archive(thread, item)
            return

        async def renew():
            while True:
                await asyncio.sleep(15)
                await self.store.renew(thread, item.id, item.lease_token)

        async def execute():
            if item.kind == "present":

                await capture.present(self.service, thread, item, uncertain=reconciling)
                return
            state = await self.store.get(thread)
            task = next((t for t in state.tasks if t.id == item.task_id), None)
            if task is None:
                await self.store.settle(thread, item.id, item.lease_token)
                return
            if (
                reconciling
                and task.continuation.kind != "hermes"
                and task.status not in TERMINAL_STATUSES
            ):

                await continuations.publish_result(
                    self.service,
                    thread,
                    task,
                    item,
                    "This task was interrupted. Its external outcome could not be confirmed, so it was not repeated.",
                    status="stale",
                )
                return
            if task.revision != item.task_revision:
                await self.store.settle(thread, item.id, item.lease_token)
                return
            if task.continuation.kind != "conversation":

                await continuations.execute_continuation(
                    self.service, thread, task, item
                )
                return
            if item.kind == "cancel":
                await self.store.replace_task(
                    thread,
                    changed(task, revision=task.revision + 1),
                    expected_revision=task.revision,
                    command_id=f"cancelled:{item.id}",
                )
                await self.store.settle(thread, item.id, item.lease_token)
                return
            utterance = await self.service.utterance(
                thread, task.reply_utterance_id, role="user"
            )
            async with aclosing(
                self.service.chat.generate_response_stream(
                    thread.id, thread.user_id, utterance.text, resume=(task, item)
                )
            ) as events:
                async for event in events:
                    if event["type"] == "error":
                        raise RuntimeError(event["data"]["error"])

        work = asyncio.create_task(execute())
        renewal = asyncio.create_task(renew())
        try:
            await asyncio.wait((work, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal.done():
                renewal.result()
                raise RuntimeError("Dialogue lease renewal stopped")
            work.result()
        finally:
            work.cancel()
            renewal.cancel()
            await asyncio.gather(work, renewal, return_exceptions=True)

    async def sweep(self):
        rows = (
            await self.store.collection.find(
                {
                    "$or": [
                        {"effects.0": {"$exists": True}},
                        {"tasks.input_wait.expires_at": {"$lte": now()}},
                    ]
                }
            )
            .limit(128)
            .to_list(length=128)
        )
        semaphore = asyncio.Semaphore(4)

        async def process_row(row):
            thread = DialogueThread.model_validate(_decode(row["thread"]))
            try:
                async with semaphore:
                    await self.service.thread(thread.id, thread.user_id)
                    state = await self.store.expire(thread)
                    for item in state.effects:
                        await self.process(thread, item.id)
            except Exception:
                # Keep durable work visible for reconciliation; never discard a failed effect.
                logger.exception(
                    "Dialogue effect recovery failed for thread %s", thread.id
                )

        await asyncio.gather(*(process_row(row) for row in rows))

    async def run(self):
        self.running = True
        while self.running:
            await self.sweep()
            await asyncio.sleep(1)

    async def stop(self):
        self.running = False


async def run_dialogue_worker():
    worker = DialogueWorker(await get_dialogue_service())
    try:
        await worker.run()
    finally:
        await worker.stop()
