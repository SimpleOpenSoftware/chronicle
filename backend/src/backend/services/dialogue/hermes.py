"""One bounded Hermes request per effect, with durable remote run identity."""

from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from .continuations import publish_result, require_current
from .models import ActionConfirmation, ReplyChoice
from .store import changed, now


async def execute_hermes(service, thread, task, item, plugin):
    base = plugin.api_url.rstrip("/").removesuffix("/v1")
    headers = {"Authorization": f"Bearer {plugin.api_key}"} if plugin.api_key else {}
    continuation = task.continuation

    async def checkpoint(**values):
        nonlocal task, item, continuation
        continuation = changed(continuation, **values)
        task, item = await service.store.checkpoint_task(
            thread,
            changed(task, continuation=continuation, revision=task.revision + 1),
            item,
        )

    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
        if not continuation.run_id:
            if item.kind == "cancel":
                return await publish_result(
                    service,
                    thread,
                    task,
                    item,
                    "Hermes task cancelled before submission.",
                    status="cancelled",
                )
            if continuation.submission_started:
                return await publish_result(
                    service,
                    thread,
                    task,
                    item,
                    "Hermes submission could not be confirmed. It was not repeated.",
                    status="stale",
                )
            await checkpoint(
                submission_started=True, observe_until=now() + timedelta(minutes=10)
            )
            await require_current(service, thread, task, item)
            response = await client.post(
                f"{base}/v1/runs",
                headers=headers,
                json={
                    "input": continuation.request,
                    "session_id": f"chronicle-dialogue-{task.id}",
                },
            )
            response.raise_for_status()
            run_id = response.json().get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("Hermes did not return a run ID")
            await checkpoint(run_id=run_id)
        path = f"{base}/v1/runs/{quote(continuation.run_id, safe='')}"
        if item.kind == "cancel":
            await require_current(service, thread, task, item)
            response = await client.post(f"{path}/stop", headers=headers)
            if response.status_code != 404:
                response.raise_for_status()
            await checkpoint(cancel_requested=True)
        elif continuation.pending_request_id and task.reply_utterance_id:
            reply = await service.utterance(
                thread, task.reply_utterance_id, role="user"
            )
            endpoint = (
                "approval" if continuation.pending_kind == "approval" else "reply"
            )
            data = {
                "request_id": continuation.pending_request_id,
                "revision": continuation.pending_revision,
                "response_id": task.reply_utterance_id,
            }
            if continuation.pending_kind == "approval":
                # Only explicit advertised selections authorize a remote operation.
                row = await service.chat.messages_collection.find_one(
                    {"message_id": reply.id, "user_id": thread.user_id}
                )
                selection = row.get("metadata", {}).get("source", {})
                choice = (
                    selection.get("choice_id")
                    if selection.get("kind") == "selection"
                    else None
                )
                if choice not in {"once", "session", "always", "deny"}:
                    return await publish_result(
                        service,
                        thread,
                        task,
                        item,
                        "Choose an explicit approval option to continue this Hermes operation.",
                        wait=True,
                        choices=tuple(
                            ReplyChoice(id=c, label=c.title())
                            for c in ("once", "session", "always", "deny")
                        ),
                        expires_at=continuation.observe_until,
                    )
                data["choice"] = choice
                if choice != "deny":
                    expected = ActionConfirmation(
                        operation_id=continuation.pending_request_id,
                        operation_revision=str(continuation.pending_revision),
                        accepted_by_utterance_id=reply.id,
                    )
                    if (
                        task.confirmation != expected
                        or selection.get("task_id") != task.id
                    ):
                        raise ValueError(
                            "Approval is not bound to this operation revision"
                        )
            else:
                data["text"] = reply.text
            await require_current(service, thread, task, item)
            response = await client.post(
                f"{path}/{endpoint}", headers=headers, json=data
            )
            if response.status_code in {404, 409, 410}:
                return await publish_result(
                    service,
                    thread,
                    task,
                    item,
                    "That Hermes request is no longer active.",
                    status="stale",
                )
            response.raise_for_status()
            await checkpoint(pending_request_id=None, pending_kind=None)
        response = await client.get(path, headers=headers)
        if response.status_code == 404:
            return await publish_result(
                service,
                thread,
                task,
                item,
                "Hermes no longer has this run. It was not repeated.",
                status="stale",
            )
        response.raise_for_status()
        remote = response.json()
        status = remote.get("status")
        if status in {"completed", "failed", "cancelled"}:
            return await publish_result(
                service,
                thread,
                task,
                item,
                str(
                    remote.get("output")
                    or f"Hermes reported {status}. Already performed actions may remain."
                )[:32000],
                status=status,
            )
        pending = remote.get("pending_input")
        if pending and not continuation.cancel_requested:
            expires = (
                datetime.fromisoformat(pending["expires_at"])
                if pending.get("expires_at")
                else continuation.observe_until
            )
            return await publish_result(
                service,
                thread,
                task,
                item,
                pending["prompt"],
                wait=True,
                choices=tuple(
                    ReplyChoice.model_validate(c) for c in pending.get("choices", [])
                ),
                expires_at=expires,
                continuation=changed(
                    continuation,
                    pending_request_id=pending["id"],
                    pending_revision=pending["revision"],
                    pending_kind=pending["kind"],
                ),
            )
        if continuation.observe_until and continuation.observe_until <= now():
            return await publish_result(
                service,
                thread,
                task,
                item,
                "The Hermes observation window expired; remote work may still be running.",
                status="stale",
            )
        await service.store.defer(thread, task, item)
