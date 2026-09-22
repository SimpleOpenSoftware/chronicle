"""Bounded voice tools. Chronicle vault reads and remote Hermes are distinct effects."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx

import backend.services.dialogue.service as service
import backend.services.memory.service_factory as service_factory

from .settings import VoiceSettings

Checkpoint = Callable[[dict], Awaitable[None]]
Progress = Callable[[str], Awaitable[None]]


def _excerpt(text: str, byte_limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text, False
    return encoded[:byte_limit].decode("utf-8", errors="ignore"), True


def _voice_retrieval(result: dict) -> dict:
    # Voice stores consulted excerpts and exact note identities, not entire vault
    # documents. Bound bytes (including multilingual text), and report clipping.
    if len(result["notes"]) > 20:
        raise ValueError("voice retrieval returned too many note references")
    evidence, clipped = [], False
    for note in result["notes"]:
        if len(note["path"].encode("utf-8")) > 1024 or len(note["revision"]) > 128:
            raise ValueError("invalid voice note identity")
        text, shortened = _excerpt(note["text"], 2048)
        clipped |= shortened
        evidence.append(
            {
                "id": note["id"],
                "title": note["title"],
                "path": note["path"],
                "revision": note["revision"],
                "text": text,
                "coverage": (
                    "Consulted excerpt; shortened for voice"
                    if shortened
                    else note["coverage"]
                ),
            }
        )
    answer, shortened = _excerpt(result["answer"], 8192)
    return {
        "status": "completed",
        "answer": answer,
        "evidence": evidence,
        "coverage": "partial" if clipped or shortened else result["coverage"],
    }


@dataclass(frozen=True)
class VoiceToolContext:
    user_id: str
    memory_space_id: str | None
    interaction_id: str
    task_id: str
    history: Sequence[Mapping] = ()
    state: dict = field(default_factory=dict)


def _schema(name: str, description: str, argument: str):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {argument: {"type": "string", "maxLength": 8000}},
                "required": [argument],
                "additionalProperties": False,
            },
        },
    }


class VoiceTools:
    def __init__(
        self,
        *,
        vault_enabled: bool = False,
        hermes_plugin=None,
        retrieve=None,
        client: httpx.AsyncClient | None = None,
        settings: VoiceSettings | None = None,
    ):
        self.settings = settings or VoiceSettings(vault_retrieval_enabled=vault_enabled)
        self.vault_enabled = self.settings.vault_retrieval_enabled
        self.hermes_plugin = hermes_plugin
        self.retrieve = retrieve
        self.client = client
        self._cleanup: set[asyncio.Task] = set()

    @classmethod
    def from_config(cls, plugin_router=None):
        plugin = getattr(plugin_router, "plugins", {}).get("hermes")
        if plugin is not None and not plugin.enabled:
            plugin = None
        return cls(settings=VoiceSettings.load(), hermes_plugin=plugin)

    def schemas(self) -> list[dict]:

        result = [service.ASK_USER_TOOL, service.START_TASK_TOOL]
        if self.vault_enabled:
            result.append(
                _schema(
                    "search_memories",
                    "Read your selected Chronicle memory space to answer a personal question. This cannot change memories.",
                    "query",
                )
            )
        if self.hermes_plugin is not None:
            result.append(
                _schema(
                    "delegate_to_hermes",
                    "Delegate a requested task to the user's separate Hermes agent, which has its own tools and can take actions. Use for requested work beyond ordinary conversation, not for unsolicited actions.",
                    "request",
                )
            )
        return result

    async def execute(
        self,
        name: str,
        arguments: dict,
        *,
        context: VoiceToolContext,
        checkpoint: Checkpoint,
        on_progress: Progress | None = None,
    ) -> dict:
        allowed = {item["function"]["name"] for item in self.schemas()}
        if name not in allowed:
            return {"status": "failed", "answer": "This voice tool is disabled."}
        argument = "query" if name == "search_memories" else "request"
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {argument}
            or not isinstance(arguments[argument], str)
            or not 1 <= len(arguments[argument].strip()) <= 8000
        ):
            return {"status": "failed", "answer": "Invalid voice tool arguments."}
        if name == "search_memories":
            return await self._search(arguments[argument], context)
        return await self._hermes(arguments[argument], context, checkpoint, on_progress)

    async def _search(self, query: str, context: VoiceToolContext) -> dict:
        retrieve = self.retrieve
        if retrieve is None:

            retrieve = service_factory.get_memory_service().retrieve_for_chat
        # wait_for waits for subprocess cleanup; the voice deadline must not.
        task = asyncio.create_task(
            retrieve(
                query,
                context.user_id,
                memory_space_id=context.memory_space_id,
                notes_only=True,
            )
        )
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=self.settings.vault_timeout_seconds
            )
            if not done:
                self._cancel_in_background(task)
                return {
                    "status": "failed",
                    "answer": "Memory retrieval timed out.",
                    "coverage": "unavailable",
                }
            value = task.result()
            result = value.model_dump(mode="json")
            return _voice_retrieval(result)
        except asyncio.CancelledError:
            self._cancel_in_background(task)
            raise
        except Exception:
            return {
                "status": "failed",
                "answer": "Memory retrieval is unavailable.",
                "coverage": "unavailable",
            }

    def _cancel_in_background(self, task):
        task.cancel()
        self._cleanup.add(task)

        def settled(done):
            self._cleanup.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)

    def _connection(self):
        plugin = self.hermes_plugin
        if plugin is None:
            raise RuntimeError("Hermes is disabled")
        base = plugin.api_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        headers = (
            {"Authorization": f"Bearer {plugin.api_key}"} if plugin.api_key else {}
        )
        return base, headers

    async def _hermes(self, request, context, checkpoint, on_progress):
        base, headers = self._connection()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        run_id = context.state.get("remote_run_id")
        event_task = None
        try:
            if not run_id:
                if context.state.get("submission_started"):
                    return {
                        "status": "unknown",
                        "answer": "Hermes submission could not be confirmed. It was not repeated.",
                    }
                deadline = time.time() + self.settings.hermes_timeout_seconds
                await checkpoint(
                    {"submission_started": True, "observation_deadline": deadline}
                )
                history = [
                    {
                        "role": item["role"],
                        "content": str(item.get("content") or "")[:2000],
                    }
                    for item in context.history[-8:]
                    if item.get("role") in {"user", "assistant"}
                ]
                try:
                    response = await client.post(
                        f"{base}/v1/runs",
                        headers=headers,
                        json={
                            "input": request,
                            "session_id": f"chronicle-voice-{context.task_id}",
                            "conversation_history": history,
                        },
                    )
                    response.raise_for_status()
                    run_id = response.json().get("run_id")
                    if not isinstance(run_id, str) or not run_id or len(run_id) > 255:
                        raise ValueError("invalid remote run identity")
                except Exception:
                    return {
                        "status": "unknown",
                        "answer": "Hermes submission could not be confirmed. It was not repeated.",
                    }
                await checkpoint({"remote_run_id": run_id})
            path = f"{base}/v1/runs/{quote(run_id, safe='')}"
            event_task = asyncio.create_task(
                self._drain_events(client, path, headers, on_progress)
            )
            deadline = context.state.get("observation_deadline")
            if deadline is None:
                deadline = time.time() + self.settings.hermes_timeout_seconds
                await checkpoint({"observation_deadline": deadline})
            while time.time() < deadline:
                try:
                    response = await client.get(path, headers=headers)
                    if response.status_code == 404:
                        return {
                            "status": "unknown",
                            "answer": "Hermes no longer has this run. It was not repeated.",
                        }
                    response.raise_for_status()
                    state = response.json()
                except httpx.HTTPError:
                    await asyncio.sleep(1)
                    continue
                status = state.get("status")
                if status == "completed":
                    answer, shortened = _excerpt(
                        str(state.get("output") or "Hermes completed the task."), 8192
                    )
                    result = {"status": "completed", "answer": answer}
                    if shortened:
                        result["answer_truncated"] = True
                    return result
                if status == "failed":
                    return {
                        "status": "failed",
                        "answer": "Hermes reported that the task failed.",
                    }
                if status == "cancelled":
                    return {
                        "status": "cancelled",
                        "answer": "Hermes marked the run cancelled; already performed actions may remain.",
                    }
                if on_progress:
                    await on_progress(
                        str(state.get("last_event") or status or "working")[:200]
                    )
                await asyncio.sleep(1)
            return {
                "status": "unknown",
                "answer": "Stopped waiting for Hermes. Remote work may still be running.",
            }
        finally:
            if event_task:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            if owned:
                await client.aclose()

    @staticmethod
    async def _drain_events(client, path, headers, on_progress):
        # Drain the server's event queue; polling remains authoritative on reconnect.
        # Never expose reasoning.available or partial internal tool arguments.
        import json

        try:
            async with client.stream(
                "GET", f"{path}/events", headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:") or len(line) > 65536:
                        continue
                    try:
                        event = json.loads(line[5:])
                    except (ValueError, TypeError):
                        continue
                    if on_progress and event.get("event") in {
                        "tool.started",
                        "tool.completed",
                        "run.started",
                    }:
                        await on_progress(str(event["event"]))
        except httpx.HTTPError:
            return

    async def cancel(self, name: str, context: VoiceToolContext) -> dict:
        if name != "delegate_to_hermes":
            return {"status": "cancelled", "answer": "Retrieval cancelled."}
        run_id = context.state.get("remote_run_id")
        if not run_id:
            return {
                "status": "unknown",
                "answer": "No confirmed Hermes run is available to stop.",
            }
        base, headers = self._connection()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        try:
            response = await client.post(
                f"{base}/v1/runs/{quote(run_id, safe='')}/stop", headers=headers
            )
            response.raise_for_status()
            return {
                "status": "cancel_requested",
                "answer": "Asked Hermes to stop. Remote termination is not yet confirmed.",
            }
        except httpx.HTTPError:
            return {
                "status": "unknown",
                "answer": "Hermes stop could not be confirmed.",
            }
        finally:
            if owned:
                await client.aclose()

    async def aclose(self):
        pending = list(self._cleanup)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
