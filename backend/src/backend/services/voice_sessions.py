"""Redis-backed interactive voice-session identity and resume coordination."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Literal

import redis.asyncio as redis
from redis.exceptions import WatchError

from backend.models.audio_capabilities import VoiceCapabilities
from backend.redis_keys import (
    ClientId,
    UserId,
    active_voice_session,
    response_generation,
    voice_session,
)

AUDIO_CONTRACT_VERSION = 2

RESUME_GRACE_SECONDS = 15
ACTIVE_SESSION_TTL_SECONDS = 60 * 60
SESSION_RETENTION_SECONDS = 24 * 60 * 60
GENERATION_RETENTION_SECONDS = 24 * 60 * 60

VoiceSessionState = Literal[
    "starting",
    "ready_full",
    "ready_isolated",
    "ready_half",
    "reconfiguring",
    "reconnecting",
    "ended",
]


class VoiceSessionError(RuntimeError):
    """Base class for explicit interactive-voice protocol failures."""


class ClientUpgradeRequired(VoiceSessionError):
    """An authenticated client can capture but cannot activate interactive voice."""

    error_code = "client_upgrade_required"


class StaleVoiceBinding(VoiceSessionError):
    """An event did not match the active authenticated capture/socket binding."""

    error_code = "stale_voice_binding"


class InvalidVoiceTransition(VoiceSessionError):
    """An otherwise valid event is not legal from the current state."""

    error_code = "invalid_voice_transition"


@dataclass(frozen=True)
class VoiceSession:
    voice_session_id: str
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    socket_id: str
    state: VoiceSessionState
    generation: int
    created_at: float
    updated_at: float
    resume_expires_at: float
    capabilities: dict | None = None
    end_reason: str | None = None


@dataclass(frozen=True)
class VoiceSessionStartResult:
    session: VoiceSession
    resume_token: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _decode_hash(raw: dict) -> dict[str, str]:
    return {_decode(key): _decode(value) for key, value in raw.items()}


def _session_mapping(
    session: VoiceSession, *, resume_token_hash: str
) -> dict[str, str]:
    return {
        "voice_session_id": session.voice_session_id,
        "user_id": session.user_id,
        "client_id": session.client_id,
        "audio_session_id": session.audio_session_id,
        "capture_epoch": str(session.capture_epoch),
        "socket_id": session.socket_id,
        "state": session.state,
        "generation": str(session.generation),
        "created_at": str(session.created_at),
        "updated_at": str(session.updated_at),
        "resume_expires_at": str(session.resume_expires_at),
        "resume_token_hash": resume_token_hash,
        "capabilities": (
            json.dumps(session.capabilities, separators=(",", ":"), sort_keys=True)
            if session.capabilities is not None
            else ""
        ),
        "end_reason": session.end_reason or "",
    }


def _session_from_hash(raw: dict) -> VoiceSession | None:
    if not raw:
        return None
    values = _decode_hash(raw)
    capabilities = values.get("capabilities")
    return VoiceSession(
        voice_session_id=values["voice_session_id"],
        user_id=values["user_id"],
        client_id=values["client_id"],
        audio_session_id=values["audio_session_id"],
        capture_epoch=int(values["capture_epoch"]),
        socket_id=values["socket_id"],
        state=values["state"],
        generation=int(values["generation"]),
        created_at=float(values["created_at"]),
        updated_at=float(values["updated_at"]),
        resume_expires_at=float(values["resume_expires_at"]),
        capabilities=json.loads(capabilities) if capabilities else None,
        end_reason=values.get("end_reason") or None,
    )


class VoiceSessionCoordinator:
    """Own one atomic voice-session state machine per authenticated client."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    @staticmethod
    def _active_key(user_id: str, client_id: str) -> str:
        return active_voice_session(
            UserId.from_value(user_id), ClientId.from_value(client_id)
        )

    @staticmethod
    def _generation_key(user_id: str, client_id: str) -> str:
        return response_generation(
            UserId.from_value(user_id), ClientId.from_value(client_id)
        )

    async def get(self, voice_session_id: str) -> VoiceSession | None:
        return _session_from_hash(
            await self.redis.hgetall(voice_session(voice_session_id))
        )

    async def get_active(self, user_id: str, client_id: str) -> VoiceSession | None:
        raw_id = await self.redis.get(self._active_key(user_id, client_id))
        if raw_id is None:
            return None
        session = await self.get(_decode(raw_id))
        if session is None or session.state == "ended":
            return None
        return session

    async def start(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        capture_epoch: int,
        socket_id: str,
        advertised_protocol: int | None,
    ) -> VoiceSessionStartResult:
        """Start a fresh voice session, terminally replacing any old binding."""

        if advertised_protocol != AUDIO_CONTRACT_VERSION:
            raise ClientUpgradeRequired(
                "interactive voice requires Chronicle audio contract v2"
            )
        if capture_epoch < 0:
            raise ValueError("capture_epoch must be non-negative")
        for value, label in (
            (audio_session_id, "audio_session_id"),
            (socket_id, "socket_id"),
        ):
            if not value:
                raise ValueError(f"{label} is required")

        active_key = self._active_key(user_id, client_id)
        generation_key = self._generation_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(active_key, generation_key)
                    old_id = _decode(await pipe.get(active_key))
                    old_key = voice_session(old_id) if old_id else None
                    if old_key:
                        await pipe.watch(old_key)
                        if _decode(await pipe.get(active_key)) != old_id:
                            await pipe.unwatch()
                            continue
                    generation = int(_decode(await pipe.get(generation_key)) or 0)
                    now = time.time()
                    new_id = str(uuid.uuid4())
                    token = _new_token()
                    session = VoiceSession(
                        voice_session_id=new_id,
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=audio_session_id,
                        capture_epoch=capture_epoch,
                        socket_id=socket_id,
                        state="starting",
                        generation=generation,
                        created_at=now,
                        updated_at=now,
                        resume_expires_at=now + RESUME_GRACE_SECONDS,
                    )
                    pipe.multi()
                    if old_key:
                        pipe.hset(
                            old_key,
                            mapping={
                                "state": "ended",
                                "updated_at": str(now),
                                "end_reason": "audio_disconnect",
                                "resume_token_hash": "",
                            },
                        )
                        pipe.expire(old_key, SESSION_RETENTION_SECONDS)
                    new_key = voice_session(new_id)
                    pipe.hset(
                        new_key,
                        mapping=_session_mapping(
                            session, resume_token_hash=_token_hash(token)
                        ),
                    )
                    pipe.expire(new_key, SESSION_RETENTION_SECONDS)
                    pipe.set(
                        active_key,
                        new_id,
                        ex=ACTIVE_SESSION_TTL_SECONDS,
                    )
                    pipe.set(
                        generation_key,
                        generation,
                        ex=GENERATION_RETENTION_SECONDS,
                    )
                    await pipe.execute()
                    return VoiceSessionStartResult(session=session, resume_token=token)
                except WatchError:
                    continue

    @staticmethod
    def _require_binding(
        session: VoiceSession | None,
        *,
        voice_session_id: str,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        capture_epoch: int,
        socket_id: str,
    ) -> VoiceSession:
        if session is None or (
            session.voice_session_id != voice_session_id
            or session.user_id != user_id
            or session.client_id != client_id
            or session.audio_session_id != audio_session_id
            or session.capture_epoch != capture_epoch
            or session.socket_id != socket_id
            or session.state == "ended"
        ):
            raise StaleVoiceBinding("event does not match active voice binding")
        return session

    async def ready(
        self,
        *,
        voice_session_id: str,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        capture_epoch: int,
        socket_id: str,
        capabilities: VoiceCapabilities,
    ) -> VoiceSession:
        session_key = voice_session(voice_session_id)
        active_key = self._active_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(session_key, active_key)
                    session = self._require_binding(
                        _session_from_hash(await pipe.hgetall(session_key)),
                        voice_session_id=voice_session_id,
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=audio_session_id,
                        capture_epoch=capture_epoch,
                        socket_id=socket_id,
                    )
                    if _decode(await pipe.get(active_key)) != voice_session_id:
                        raise StaleVoiceBinding("voice session is not active")
                    if session.state not in {"starting", "reconfiguring"}:
                        raise InvalidVoiceTransition(
                            f"cannot become ready from {session.state}"
                        )
                    state_by_mode: dict[str, VoiceSessionState] = {
                        "duplex_full": "ready_full",
                        "duplex_isolated": "ready_isolated",
                        "duplex_half": "ready_half",
                    }
                    now = time.time()
                    ready = VoiceSession(
                        **{
                            **session.__dict__,
                            "state": state_by_mode[capabilities.mode],
                            "updated_at": now,
                            "capabilities": capabilities.model_dump(mode="json"),
                        }
                    )
                    pipe.multi()
                    pipe.hset(
                        session_key,
                        mapping={
                            "state": ready.state,
                            "updated_at": str(now),
                            "capabilities": json.dumps(
                                ready.capabilities,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        },
                    )
                    pipe.expire(active_key, ACTIVE_SESSION_TTL_SECONDS)
                    pipe.expire(session_key, SESSION_RETENTION_SECONDS)
                    await pipe.execute()
                    return ready
                except WatchError:
                    continue

    async def capabilities_changed(
        self,
        *,
        voice_session_id: str,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        capture_epoch: int,
        socket_id: str,
        capabilities: VoiceCapabilities,
        reason: str,
    ) -> VoiceSession:
        """Cancel output and enter reconfiguration on a new native engine epoch."""

        session_key = voice_session(voice_session_id)
        active_key = self._active_key(user_id, client_id)
        generation_key = self._generation_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(session_key, active_key, generation_key)
                    session = _session_from_hash(await pipe.hgetall(session_key))
                    if session is None or (
                        session.user_id != user_id
                        or session.client_id != client_id
                        or session.socket_id != socket_id
                        or session.state
                        not in {
                            "ready_full",
                            "ready_isolated",
                            "ready_half",
                        }
                        or _decode(await pipe.get(active_key)) != voice_session_id
                    ):
                        raise StaleVoiceBinding(
                            "capability change does not match active voice binding"
                        )
                    if capture_epoch <= session.capture_epoch:
                        raise StaleVoiceBinding(
                            "capability change must advance capture_epoch"
                        )
                    generation = int(_decode(await pipe.get(generation_key)) or 0) + 1
                    now = time.time()
                    changed = VoiceSession(
                        **{
                            **session.__dict__,
                            "audio_session_id": audio_session_id,
                            "capture_epoch": capture_epoch,
                            "state": "reconfiguring",
                            "generation": generation,
                            "updated_at": now,
                            "capabilities": capabilities.model_dump(mode="json"),
                        }
                    )
                    pipe.multi()
                    pipe.hset(
                        session_key,
                        mapping={
                            "audio_session_id": audio_session_id,
                            "capture_epoch": str(capture_epoch),
                            "state": "reconfiguring",
                            "generation": str(generation),
                            "updated_at": str(now),
                            "capabilities": json.dumps(
                                changed.capabilities,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            "last_change_reason": reason,
                        },
                    )
                    pipe.set(
                        generation_key,
                        generation,
                        ex=GENERATION_RETENTION_SECONDS,
                    )
                    pipe.expire(session_key, SESSION_RETENTION_SECONDS)
                    await pipe.execute()
                    return changed
                except WatchError:
                    continue

    async def disconnect(
        self, *, voice_session_id: str, socket_id: str
    ) -> VoiceSession:
        """Suppress output immediately and hold resume proof for exactly 15 seconds."""

        session_key = voice_session(voice_session_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(session_key)
                    session = _session_from_hash(await pipe.hgetall(session_key))
                    if session is None or session.socket_id != socket_id:
                        raise StaleVoiceBinding(
                            "disconnect does not match active socket"
                        )
                    if session.state == "ended":
                        return session
                    active_key = self._active_key(session.user_id, session.client_id)
                    generation_key = self._generation_key(
                        session.user_id, session.client_id
                    )
                    await pipe.watch(active_key, generation_key)
                    if _decode(await pipe.get(active_key)) != voice_session_id:
                        raise StaleVoiceBinding("voice session is not active")
                    generation = int(_decode(await pipe.get(generation_key)) or 0) + 1
                    now = time.time()
                    disconnected = VoiceSession(
                        **{
                            **session.__dict__,
                            "state": "reconnecting",
                            "generation": generation,
                            "updated_at": now,
                            "resume_expires_at": now + RESUME_GRACE_SECONDS,
                        }
                    )
                    pipe.multi()
                    pipe.hset(
                        session_key,
                        mapping={
                            "state": "reconnecting",
                            "generation": str(generation),
                            "updated_at": str(now),
                            "resume_expires_at": str(disconnected.resume_expires_at),
                            # The phone can prove only the last generation it saw
                            # before the transport vanished. The increment above is
                            # deliberately server-side so stale output is fenced,
                            # but cannot be guessed by an honest reconnecting client.
                            "resume_from_generation": str(session.generation),
                        },
                    )
                    pipe.set(
                        generation_key,
                        generation,
                        ex=GENERATION_RETENTION_SECONDS,
                    )
                    pipe.expire(active_key, RESUME_GRACE_SECONDS)
                    pipe.expire(session_key, SESSION_RETENTION_SECONDS)
                    await pipe.execute()
                    return disconnected
                except WatchError:
                    continue

    async def resume(
        self,
        *,
        previous_voice_session_id: str,
        user_id: str,
        client_id: str,
        previous_capture_epoch: int,
        resume_token: str,
        new_audio_session_id: str,
        new_capture_epoch: int,
        new_socket_id: str,
        last_response_generation: int,
    ) -> VoiceSessionStartResult:
        """Consume one resume proof and atomically rotate every session identity."""

        old_key = voice_session(previous_voice_session_id)
        active_key = self._active_key(user_id, client_id)
        generation_key = self._generation_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(old_key, active_key, generation_key)
                    raw_old = _decode_hash(await pipe.hgetall(old_key))
                    old = _session_from_hash(raw_old)
                    generation = int(_decode(await pipe.get(generation_key)) or 0)
                    proof = raw_old.get("resume_token_hash", "")
                    if (
                        old is None
                        or old.state != "reconnecting"
                        or old.user_id != user_id
                        or old.client_id != client_id
                        or old.capture_epoch != previous_capture_epoch
                        or _decode(await pipe.get(active_key))
                        != previous_voice_session_id
                        or time.time() > old.resume_expires_at
                        or not proof
                        or not hmac.compare_digest(proof, _token_hash(resume_token))
                        or last_response_generation
                        != int(raw_old.get("resume_from_generation", "-1"))
                        or new_capture_epoch <= previous_capture_epoch
                    ):
                        raise StaleVoiceBinding("resume proof is invalid or expired")
                    now = time.time()
                    new_id = str(uuid.uuid4())
                    token = _new_token()
                    resumed = VoiceSession(
                        voice_session_id=new_id,
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=new_audio_session_id,
                        capture_epoch=new_capture_epoch,
                        socket_id=new_socket_id,
                        state="starting",
                        generation=generation,
                        created_at=now,
                        updated_at=now,
                        resume_expires_at=now + RESUME_GRACE_SECONDS,
                    )
                    pipe.multi()
                    pipe.hset(
                        old_key,
                        mapping={
                            "state": "ended",
                            "updated_at": str(now),
                            "end_reason": "resumed",
                            "resume_token_hash": "",
                        },
                    )
                    pipe.expire(old_key, SESSION_RETENTION_SECONDS)
                    new_key = voice_session(new_id)
                    pipe.hset(
                        new_key,
                        mapping=_session_mapping(
                            resumed, resume_token_hash=_token_hash(token)
                        ),
                    )
                    pipe.expire(new_key, SESSION_RETENTION_SECONDS)
                    pipe.set(
                        active_key,
                        new_id,
                        ex=ACTIVE_SESSION_TTL_SECONDS,
                    )
                    await pipe.execute()
                    return VoiceSessionStartResult(session=resumed, resume_token=token)
                except WatchError:
                    continue

    async def binding_matches(
        self,
        *,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        voice_session_id: str,
        capture_epoch: int,
        socket_id: str,
        require_ready: bool = True,
    ) -> bool:
        """Double-check one downlink or acknowledgment against current state."""

        # Both keys are known from the envelope. Read the active pointer and
        # bound session together, without following a pointer in another trip.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.get(self._active_key(user_id, client_id))
            pipe.hgetall(voice_session(voice_session_id))
            active_id, raw = await pipe.execute()
        session = _session_from_hash(raw)
        if (
            _decode(active_id) != voice_session_id
            or session is None
            or session.state == "ended"
            or session.user_id != user_id
            or session.client_id != client_id
        ):
            return False
        ready_states = {"ready_full", "ready_isolated", "ready_half"}
        return (
            session.voice_session_id == voice_session_id
            and session.audio_session_id == audio_session_id
            and session.capture_epoch == capture_epoch
            and session.socket_id == socket_id
            and (not require_ready or session.state in ready_states)
        )

    async def end(
        self,
        *,
        voice_session_id: str,
        user_id: str,
        client_id: str,
        audio_session_id: str,
        capture_epoch: int,
        socket_id: str,
        reason: str,
    ) -> VoiceSession:
        """Terminally end a bound session and suppress every older output."""

        session_key = voice_session(voice_session_id)
        active_key = self._active_key(user_id, client_id)
        generation_key = self._generation_key(user_id, client_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(session_key, active_key, generation_key)
                    session = self._require_binding(
                        _session_from_hash(await pipe.hgetall(session_key)),
                        voice_session_id=voice_session_id,
                        user_id=user_id,
                        client_id=client_id,
                        audio_session_id=audio_session_id,
                        capture_epoch=capture_epoch,
                        socket_id=socket_id,
                    )
                    generation = int(_decode(await pipe.get(generation_key)) or 0) + 1
                    now = time.time()
                    ended = VoiceSession(
                        **{
                            **session.__dict__,
                            "state": "ended",
                            "generation": generation,
                            "updated_at": now,
                            "end_reason": reason,
                        }
                    )
                    pipe.multi()
                    pipe.hset(
                        session_key,
                        mapping={
                            "state": "ended",
                            "generation": str(generation),
                            "updated_at": str(now),
                            "end_reason": reason,
                            "resume_token_hash": "",
                        },
                    )
                    pipe.expire(session_key, SESSION_RETENTION_SECONDS)
                    pipe.set(
                        generation_key,
                        generation,
                        ex=GENERATION_RETENTION_SECONDS,
                    )
                    if _decode(await pipe.get(active_key)) == voice_session_id:
                        pipe.delete(active_key)
                    await pipe.execute()
                    return ended
                except WatchError:
                    continue
