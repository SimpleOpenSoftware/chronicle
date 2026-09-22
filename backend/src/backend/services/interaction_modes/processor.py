"""Serial state-machine execution for queued interaction inputs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis

import backend.services.dialogue.capture as capture
import backend.services.dialogue.service as service_module
from backend.observability.tracing import (
    chronicle_span,
    set_span_attributes,
    set_span_io,
)
from backend.plugins.router import PluginRouter
from backend.services.response_coordinator import ResponseCoordinator, StaleResponse
from backend.services.voice_latency import TimingIdentity, VoiceTrace
from backend.services.voice_sessions import VoiceSessionCoordinator

from .contracts import (
    InteractionContext,
    InteractionInput,
    InteractionResult,
    InteractionSession,
)
from .store import InteractionStore, interaction_lock_key

LOCK_SECONDS = 120


class InteractionBusyError(RuntimeError):
    """Another worker currently owns this interaction's serial transition."""


class StaleInteractionTurn(RuntimeError):
    """The turn was cooperatively cancelled before an external-effect fence."""


@dataclass
class InteractionDispatch:
    session: InteractionSession
    reply: Optional[str]
    lifecycle: str
    event_data: dict


class InteractionProcessor:
    """Invokes the owning plugin and durably applies each transition."""

    def __init__(self, redis_client: redis.Redis, router: PluginRouter):
        self.redis = redis_client
        self.router = router
        self.store = InteractionStore(redis_client)

    async def process(self, item: InteractionInput) -> Optional[InteractionDispatch]:
        if await self.store.is_processed(item.input_id):
            return None
        lock_key = interaction_lock_key(item.interaction_id)
        if not await self.redis.set(lock_key, item.input_id, ex=LOCK_SECONDS, nx=True):
            raise InteractionBusyError(
                f"interaction {item.interaction_id} is already processing"
            )

        try:
            session = await self.store.get(item.interaction_id)
            if session is None or session.status != "active":
                await self.store.mark_processed(item.input_id)
                return None

            with chronicle_span(
                "interaction.mode.input",
                tracer_name="chronicle.interactions",
                attributes={
                    "chronicle.interaction.id": session.interaction_id,
                    "chronicle.interaction.mode_id": session.mode_id,
                    "chronicle.interaction.plugin_id": session.owner_plugin_id,
                    "chronicle.interaction.input_kind": item.kind,
                    "chronicle.interaction.source": item.source,
                    "chronicle.client_id": item.client_id,
                    "langfuse.session.id": session.interaction_id,
                    "langfuse.user.id": session.user_id,
                },
            ) as span:
                set_span_io(
                    span,
                    input={
                        "kind": item.kind,
                        "source": item.source,
                        "text_chars": len(item.text),
                        "activation_phrase": item.activation_phrase,
                    },
                )
                trace = VoiceTrace(
                    self.redis,
                    TimingIdentity(
                        item.user_id,
                        item.client_id,
                        item.audio_session_id,
                        item.audio_interval.capture_epoch,
                        item.audio_interval.turn_id or item.input_id,
                        item.audio_interval.turn_revision,
                    ),
                )
                with trace.bind():
                    async with trace.span(
                        "mode_handler", detail=session.owner_plugin_id
                    ):
                        dispatch = await self._process_active(item, session)
                set_span_attributes(
                    span,
                    {
                        "chronicle.interaction.success": True,
                        "chronicle.interaction.lifecycle": dispatch.lifecycle,
                        "chronicle.interaction.phase": dispatch.session.phase,
                        "chronicle.interaction.status": dispatch.session.status,
                        "chronicle.interaction.end_reason": dispatch.session.end_reason,
                        "chronicle.interaction.turn_number": dispatch.session.turn_number,
                    },
                )
                set_span_io(span, output=_trace_dispatch(dispatch))
                return dispatch
        finally:
            await self.redis.delete(lock_key)

    async def _process_active(
        self, item: InteractionInput, session: InteractionSession
    ) -> InteractionDispatch:
        reason = session.expiry_reason(time.time())
        if reason:
            return await self._end(session, reason, processed_input_id=item.input_id)

        plugin = self.router.plugins.get(session.owner_plugin_id)
        if plugin is None or not plugin.enabled:
            return await self._end(
                session,
                "owner_unavailable",
                processed_input_id=item.input_id,
            )

        session.audio_session_id = item.audio_session_id
        session.capture_epoch = item.audio_interval.capture_epoch
        session.voice_session_id = item.audio_interval.voice_session_id
        session.response_generation = item.response_generation
        session.response_turn_id = item.audio_interval.turn_id or item.input_id
        session.response_turn_revision = item.audio_interval.turn_revision
        responses = ResponseCoordinator(self.redis, VoiceSessionCoordinator(self.redis))
        try:
            await responses.assert_generation(
                session.user_id,
                session.client_id,
                item.response_generation,
            )
        except StaleResponse:
            await self.store.mark_processed(item.input_id)
            return InteractionDispatch(
                session=session,
                reply=None,
                lifecycle="superseded",
                event_data={"response_suppressed": True},
            )

        if session.owner_plugin_id == "swiggy_instamart":

            thread = await capture.accept(
                self.redis,
                user_id=session.user_id,
                client_id=session.client_id,
                capture_id=item.audio_session_id,
                input_id=item.input_id,
                text=item.text or "Start an Instamart order",
                kind="instamart" if item.kind == "start" else None,
                generation=item.response_generation,
                interval=item.audio_interval,
            )
            session.phase = "dialogue"
            session.plugin_state = {"thread_id": thread.id} if thread else {}
            session.turn_number += 1
            session.last_activity_at = max(session.last_activity_at, item.received_at)
            await self.store.save(session, processed_input_id=item.input_id)
            return InteractionDispatch(
                session=session,
                reply=None,
                lifecycle="started" if item.kind == "start" else "updated",
                event_data={"thread_id": thread.id if thread else None},
            )

        effect_fenced = False

        async def checkpoint() -> None:
            # Deliberately does not mark this input processed: the external
            # effect still has to finish. A replay sees the checkpointed
            # phase/state and can reconcile without repeating unsafe intent.
            nonlocal effect_fenced
            try:
                await responses.assert_generation(
                    session.user_id,
                    session.client_id,
                    item.response_generation,
                )
            except StaleResponse as error:
                raise StaleInteractionTurn(
                    "turn superseded before irreversible effect"
                ) from error
            await self.store.save(session)
            effect_fenced = True

        context = InteractionContext(
            session=session,
            input=item,
            services=self.router._services,
            checkpoint=checkpoint,
        )
        try:
            if item.kind == "start":
                result = await plugin.on_interaction_start(context)
                lifecycle = "started"
            else:
                result = await plugin.on_interaction_turn(context)
                lifecycle = "updated"
        except StaleInteractionTurn:
            await self.store.mark_processed(item.input_id)
            return InteractionDispatch(
                session=session,
                reply=None,
                lifecycle="superseded",
                event_data={"response_suppressed": True},
            )
        result = result or InteractionResult()

        stale_after_await = False
        try:
            await responses.assert_generation(
                session.user_id,
                session.client_id,
                item.response_generation,
            )
        except StaleResponse:
            stale_after_await = True
        if stale_after_await and not effect_fenced:
            await self.store.mark_processed(item.input_id)
            return InteractionDispatch(
                session=session,
                reply=None,
                lifecycle="superseded",
                event_data={"response_suppressed": True},
            )

        if result.phase is not None:
            session.phase = result.phase
        if result.plugin_state is not None:
            session.plugin_state = result.plugin_state
        session.turn_number += 1
        session.last_activity_at = max(session.last_activity_at, item.received_at)

        if result.end:
            reason = result.end_reason or "completed"
            await self.store.end(
                session,
                reason=reason,
                processed_input_id=item.input_id,
            )
            await self._notify_end(session, reason)
            lifecycle = "ended"
        else:
            await self.store.save(session, processed_input_id=item.input_id)

        return InteractionDispatch(
            session=session,
            reply=None if stale_after_await else result.reply,
            lifecycle=lifecycle,
            event_data={
                **result.event_data,
                **({"response_suppressed": True} if stale_after_await else {}),
            },
        )

    async def expire_due(
        self, *, now: Optional[float] = None
    ) -> list[InteractionDispatch]:
        current_time = now if now is not None else time.time()
        dispatches: list[InteractionDispatch] = []
        for interaction_id in await self.store.due_interaction_ids(now=current_time):
            lock_key = interaction_lock_key(interaction_id)
            if not await self.redis.set(lock_key, "expiry", ex=LOCK_SECONDS, nx=True):
                continue
            try:
                session = await self.store.get(interaction_id)
                if session is not None and session.mode_id == "voice_conversation":
                    continue  # Its independent voice runtime owns these deadlines.
                if session is None or session.status != "active":
                    await self.redis.zrem("interaction:deadlines", interaction_id)
                    continue
                reason = session.expiry_reason(current_time)
                if reason:
                    dispatches.append(
                        await self._end(session, reason, now=current_time)
                    )
                else:
                    await self.store.save(session)
            finally:
                await self.redis.delete(lock_key)
        return dispatches

    async def fail(
        self, interaction_id: str, *, reason: str = "processing_error"
    ) -> Optional[InteractionDispatch]:
        session = await self.store.get(interaction_id)
        if session is None or session.status != "active":
            return None
        return await self._end(session, reason)

    async def _end(
        self,
        session: InteractionSession,
        reason: str,
        *,
        now: Optional[float] = None,
        processed_input_id: Optional[str] = None,
    ) -> InteractionDispatch:
        await self.store.end(
            session,
            reason=reason,
            now=now,
            processed_input_id=processed_input_id,
        )
        result = await self._notify_end(session, reason)
        return InteractionDispatch(
            session=session,
            reply=result.reply if result else None,
            lifecycle="ended",
            event_data=result.event_data if result else {},
        )

    async def _notify_end(
        self, session: InteractionSession, reason: str
    ) -> Optional[InteractionResult]:
        if session.owner_plugin_id == "swiggy_instamart":

            service = await service_module.get_dialogue_service()
            thread_id = session.plugin_state.get("thread_id")
            if thread_id:
                thread = await service.thread(
                    thread_id, session.user_id, writable=False
                )
                await service.store.release_audio(
                    thread, "wake:" + session.audio_session_id
                )
            return None
        plugin = self.router.plugins.get(session.owner_plugin_id)
        if plugin is None:
            return None
        context = InteractionContext(
            session=session,
            input=None,
            services=self.router._services,
            end_reason=reason,
        )
        return await plugin.on_interaction_end(context)


def _trace_dispatch(dispatch: InteractionDispatch) -> dict:
    """Return derived trace output without user text or plugin payload values."""
    return {
        "handled": True,
        "lifecycle": dispatch.lifecycle,
        "phase": dispatch.session.phase,
        "status": dispatch.session.status,
        "end_reason": dispatch.session.end_reason,
        "reply_chars": len(dispatch.reply or ""),
        "event_keys": sorted(dispatch.event_data),
    }
