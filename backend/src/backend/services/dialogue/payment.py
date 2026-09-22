"""Project the existing payment monitor's outcome onto its dialogue task."""

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import backend.chat_service as chat_service
import backend.services.chat_context as chat_context

from .models import TERMINAL_STATUSES
from .service import get_dialogue_service
from .store import DialogueConflict, changed


class DialoguePaymentStore:
    @classmethod
    async def open(cls, thread_id, user_id):
        self = cls()
        self.service = await get_dialogue_service()
        self.thread = await self.service.thread(thread_id, user_id)
        self.collection = self.service.chat.db.dialogue_plugin_checkpoints
        return self

    async def get(self, task_id):
        saved = await self.collection.find_one(
            {
                "_id": task_id,
                "thread_id": self.thread.id,
                "user_id": self.thread.user_id,
            }
        )
        if saved is None:
            return None
        state = await self.service.store.get(self.thread)
        task = next((t for t in state.tasks if t.id == task_id), None)
        if (
            task is not None
            and task.continuation.phase != "awaiting_payment"
            and saved["phase"] != "finished"
        ):
            return None
        return SimpleNamespace(
            interaction_id=task_id,
            phase=saved["phase"],
            plugin_state=saved["state"],
            status="ended" if saved["phase"] == "finished" else "active",
        )

    async def end(self, session, *, reason):
        await self.collection.update_one(
            {"_id": session.interaction_id, "user_id": self.thread.user_id},
            {"$set": {"phase": session.phase, "state": session.plugin_state}},
        )
        for _ in range(32):
            state = await self.service.store.get(self.thread)
            task = next(
                (t for t in state.tasks if t.id == session.interaction_id), None
            )
            if task is None or task.status in TERMINAL_STATUSES:
                return
            status = (
                "completed"
                if reason == "payment_success"
                else (
                    "stale"
                    if reason in {"payment_monitor_error", "payment_timeout"}
                    else "failed"
                )
            )
            try:
                await self.service.store.replace_task(
                    self.thread,
                    changed(
                        task, status=status, input_wait=None, revision=task.revision + 1
                    ),
                    expected_revision=task.revision,
                    command_id=f"payment:{session.plugin_state['order_id']}",
                )
                return
            except DialogueConflict:
                continue
        raise DialogueConflict("Payment result could not be recorded")

    async def publish(self, task_id, order_id, text):

        message = chat_service.ChatMessage(
            message_id=str(
                uuid5(NAMESPACE_URL, f"dialogue-payment:{self.thread.id}:{order_id}")
            ),
            session_id=self.thread.id,
            user_id=self.thread.user_id,
            memory_space_id=self.thread.memory_space_id,
            role="assistant",
            content=text,
            metadata={
                "dialogue_task_id": task_id,
                "evidence": chat_context.ChatContext().evidence(text, [], []),
            },
        )
        await self.service.chat.commit_message(message)
