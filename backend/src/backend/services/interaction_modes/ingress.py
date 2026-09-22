"""Fast transcript ingress for active and newly activated interaction modes."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis

from backend.services.response_coordinator import ResponseCoordinator
from backend.services.voice_sessions import VoiceSessionCoordinator

from .contracts import (
    AudioInterval,
    InteractionInput,
    InteractionSession,
    InteractionSource,
)
from .episode_claims import AudioEpisodeArbiter, AudioEpisodeClaim
from .registry import InteractionRegistry
from .store import InteractionStore

INPUT_STREAM = "interaction:inputs"


@dataclass(frozen=True)
class InteractionIngressResult:
    consumed: bool
    accepted: bool = False
    interaction_id: Optional[str] = None
    mode_id: Optional[str] = None
    reason: Optional[str] = None


class InteractionIngress:
    """Claims activations and enqueues turns without doing plugin or MCP work."""

    def __init__(self, redis_client: redis.Redis, registry: InteractionRegistry):
        self.redis = redis_client
        self.registry = registry
        self.store = InteractionStore(redis_client)

    async def submit(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_interval: AudioInterval,
        text: str,
        source: InteractionSource,
        response_generation: Optional[int] = None,
        episode_claim: Optional[AudioEpisodeClaim] = None,
        now: Optional[float] = None,
    ) -> InteractionIngressResult:
        received_at = now if now is not None else time.time()
        text = (text or "").strip()
        if not text:
            return InteractionIngressResult(consumed=False, reason="empty")

        active = await self.store.get_active(user_id, client_id, now=received_at)
        if active is not None and active.mode_id == "voice_conversation":
            return InteractionIngressResult(
                consumed=True, reason="voice_requires_committed_audio"
            )
        match = self.registry.match(text) if active is None else None
        if active is None and match is None:
            return InteractionIngressResult(consumed=False, reason="no_mode")

        if episode_claim is not None:
            if (
                episode_claim.interval != audio_interval
                or episode_claim.source != source
            ):
                raise ValueError("preclaimed episode does not match ingress input")
        else:
            claimed = await AudioEpisodeArbiter(self.redis).claim(
                user_id=user_id,
                client_id=client_id,
                interval=audio_interval,
                source=source,
                now=received_at,
            )
            if not claimed.accepted:
                return InteractionIngressResult(
                    consumed=True,
                    interaction_id=active.interaction_id if active else None,
                    mode_id=active.mode_id if active else match.definition.mode_id,
                    reason="episode_already_claimed",
                )

        if response_generation is None:
            response_generation = await ResponseCoordinator(
                self.redis, VoiceSessionCoordinator(self.redis)
            ).begin_turn(user_id, client_id)
        elif response_generation < 1:
            raise ValueError("response_generation must be positive")

        input_id = str(uuid.uuid4())
        response_turn_id = audio_interval.turn_id or input_id
        activation_phrase: Optional[str] = None
        if active is None:
            interaction_id = str(uuid.uuid4())
            definition = match.definition
            active = InteractionSession(
                interaction_id=interaction_id,
                mode_id=definition.mode_id,
                owner_plugin_id=match.owner_plugin_id,
                user_id=user_id,
                client_id=client_id,
                audio_session_id=audio_interval.audio_session_id,
                capture_epoch=audio_interval.capture_epoch,
                voice_session_id=audio_interval.voice_session_id,
                response_generation=response_generation,
                response_turn_id=response_turn_id,
                response_turn_revision=audio_interval.turn_revision,
                phase="starting",
                plugin_state={},
                started_at=received_at,
                last_activity_at=received_at,
                idle_timeout_seconds=definition.idle_timeout_seconds,
                max_duration_seconds=definition.max_duration_seconds,
            )
            if not await self.store.create(active):
                active = await self.store.get_active(
                    user_id, client_id, now=received_at
                )
                if active is None:
                    return InteractionIngressResult(
                        consumed=True, reason="activation_race"
                    )
                kind = "turn"
                queued_text = text
            else:
                kind = "start"
                queued_text = match.remainder
                activation_phrase = match.activation_phrase
        else:
            kind = "turn"
            queued_text = text

        item = InteractionInput(
            input_id=input_id,
            interaction_id=active.interaction_id,
            kind=kind,
            user_id=user_id,
            client_id=client_id,
            audio_interval=audio_interval,
            text=queued_text,
            source=source,
            received_at=received_at,
            response_generation=response_generation,
            activation_phrase=activation_phrase,
        )
        await self.redis.xadd(
            INPUT_STREAM,
            {"input": json.dumps(item.to_dict(), separators=(",", ":"))},
        )
        return InteractionIngressResult(
            consumed=True,
            accepted=True,
            interaction_id=active.interaction_id,
            mode_id=active.mode_id,
            reason=kind,
        )
