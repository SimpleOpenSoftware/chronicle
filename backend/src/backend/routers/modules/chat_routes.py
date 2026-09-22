"""
Chat API routes for Chronicle with streaming support and memory integration.

This module provides:
- RESTful chat session management endpoints
- Server-Sent Events (SSE) for streaming responses
- Memory-enhanced conversational AI
- User-scoped data isolation
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

import backend.services.chat_review as chat_review
import backend.services.privacy as privacy
from backend.auth import current_active_user
from backend.chat_service import ChatSession, get_chat_service
from backend.services.chat_context import (
    INTERACTION_VERSION,
    require_writable,
    resolve_context,
    unique_sources,
)
from backend.services.chat_runs import delete_runs, public_run, run_detail
from backend.services.chat_sources import (
    ChatSourceRef,
    SourceUnavailable,
    resolve_source,
)
from backend.services.memory.scope import MemoryScope, MemoryScopeResolver
from backend.services.redis_lock import LockUnavailable, distributed_lock
from backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

from backend.services.dialogue.models import DialogueView, Utterance
from backend.services.dialogue.service import DialogueCommandRequest, DialogueService
from backend.services.dialogue.store import DialogueConflict
from backend.services.privacy import PrivacyHeld


@router.get("/sessions/{session_id}/dialogue", response_model=DialogueView)
async def get_dialogue(
    session_id: str, current_user: User = Depends(current_active_user)
):
    chat = get_chat_service()
    if not chat._initialized:
        await chat.initialize()
    service = DialogueService(chat)
    try:
        thread = await service.thread(session_id, str(current_user.id), writable=False)
        return await service.snapshot(thread)
    except PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/sessions/{session_id}/dialogue/tasks/{task_id}/commands",
    response_model=DialogueView,
)
async def command_dialogue_task(
    session_id: str,
    task_id: str,
    request: DialogueCommandRequest,
    current_user: User = Depends(current_active_user),
):
    chat = get_chat_service()
    if not chat._initialized:
        await chat.initialize()
    service = DialogueService(chat)
    try:
        thread = await service.thread(session_id, str(current_user.id))
        return await service.submit(thread, task_id, request)
    except PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc
    except DialogueConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# Pydantic models for API

# --- OpenAI-compatible chat completion models ---


class ChatCompletionMessage(BaseModel):
    role: str = Field(
        ..., description="The role of the message author (system, user, assistant)"
    )
    content: str = Field(..., description="The message content")


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: List[ChatCompletionMessage] = Field(
        ..., min_length=1, description="List of messages in the conversation"
    )
    model: Optional[str] = Field(
        None, description="Model to use (ignored, uses server-configured model)"
    )
    stream: Optional[bool] = Field(True, description="Whether to stream the response")
    temperature: Optional[float] = Field(
        None, description="Sampling temperature (ignored, uses server config)"
    )
    session_id: Optional[str] = Field(
        None, description="Chronicle session ID (creates new if not provided)"
    )


class ChatCompletionChunkDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]
    chronicle_metadata: Optional[Dict[str, Any]] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[Dict[str, int]] = None
    session_id: Optional[str] = None
    chronicle_metadata: Optional[Dict[str, Any]] = None


class ChatMessageResponse(BaseModel):
    message_id: str
    session_id: str
    role: str
    content: str
    timestamp: str
    memories_used: List[str] = []
    source_citations: List[Dict[str, Any]] = Field(default_factory=list)
    source_revision: Optional[str] = None
    source_coverage: Optional[str] = None
    run_id: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    utterance: Utterance | None = None


class ChatSessionResponse(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: Optional[int] = 0
    source: Optional[ChatSourceRef] = None
    sources: List[ChatSourceRef] = Field(default_factory=list)
    interaction_version: int | None = None
    context_changes: List[Dict[str, Any]] = Field(default_factory=list)
    memory_space_id: Optional[str] = None


class ChatSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: List[ChatSourceRef] = Field(default_factory=list, max_length=10)
    memory_space_id: Optional[str] = None
    title: Optional[str] = Field(None, max_length=200, description="Session title")


class ChatSessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=1, max_length=200)
    sources: List[ChatSourceRef] | None = Field(None, max_length=10)


def session_response(session):
    return ChatSessionResponse(
        session_id=session.session_id,
        title=session.title,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        source=session.metadata.get("source"),
        sources=session.metadata.get("sources", []),
        interaction_version=session.metadata.get("interaction_version"),
        context_changes=session.metadata.get("context_changes", []),
        memory_space_id=session.memory_space_id,
    )


def writable(session):
    try:
        require_writable(session.metadata)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class ChatStatisticsResponse(BaseModel):
    total_sessions: int
    total_messages: int
    last_chat: Optional[str] = None


async def owned_chat(chat_service, session_id: str, user_id: str):
    if not chat_service._initialized:
        await chat_service.initialize()
    row = await chat_service.sessions_collection.find_one(
        {"session_id": session_id, "user_id": user_id}
    )
    if row is None:
        return None
    session = ChatSession.from_dict(row)

    try:
        await privacy.check_chat(user_id, session_id, session.metadata)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, "Chat evidence is held by privacy settings") from exc
    if session.memory_space_id:
        await MemoryScopeResolver().require_space(
            MemoryScope(user_id, session.memory_space_id)
        )
    return session


@router.get("/sessions/{session_id}/runs")
async def get_chat_runs(
    session_id: str, current_user: User = Depends(current_active_user)
):
    service = get_chat_service()
    if not await owned_chat(service, session_id, str(current_user.id)):
        raise HTTPException(404, "Chat session not found")
    rows = (
        await service.db.chat_runs.find(
            {"session_id": session_id, "user_id": str(current_user.id)}
        )
        .sort("started_at", -1)
        .limit(100)
        .to_list(length=100)
    )
    return [public_run(row) for row in rows]


@router.get("/sessions/{session_id}/runs/{run_id}")
async def get_chat_run(
    session_id: str, run_id: str, current_user: User = Depends(current_active_user)
):
    service = get_chat_service()
    if not await owned_chat(service, session_id, str(current_user.id)):
        raise HTTPException(404, "Chat session not found")
    row = await service.db.chat_runs.find_one(
        {"run_id": run_id, "session_id": session_id, "user_id": str(current_user.id)}
    )
    if not row:
        raise HTTPException(404, "Run not found")
    return await run_detail(service.db, row)


@router.delete("/sessions/{session_id}/runs")
async def delete_chat_runs(
    session_id: str, current_user: User = Depends(current_active_user)
):
    service = get_chat_service()
    session = await owned_chat(service, session_id, str(current_user.id))
    if session is None:
        raise HTTPException(404, "Chat session not found")
    writable(session)
    try:
        await delete_runs(service.db, session_id, str(current_user.id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": True}


@router.get("/sessions/{session_id}/source")
async def get_chat_source(
    session_id: str, current_user: User = Depends(current_active_user)
):
    session = await owned_chat(get_chat_service(), session_id, str(current_user.id))
    if session is None:
        raise HTTPException(404, "Chat session not found")
    source = session.metadata.get("source")
    if source is None:
        raise HTTPException(404, "This chat has no attached source")
    try:
        return await resolve_source(
            ChatSourceRef.model_validate(source),
            str(current_user.id),
            session.memory_space_id,
        )
    except SourceUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions", response_model=ChatSessionResponse)
async def create_chat_session(
    request: ChatSessionCreateRequest, current_user: User = Depends(current_active_user)
):
    """Create a new chat session."""
    try:
        chat_service = get_chat_service()
        if request.memory_space_id:
            await MemoryScopeResolver().require_space(
                MemoryScope(str(current_user.id), request.memory_space_id)
            )
        session = await chat_service.create_session(
            user_id=str(current_user.id),
            title=request.title,
            sources=request.sources,
            memory_space_id=request.memory_space_id,
        )

        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
            sources=session.metadata.get("sources", []),
            interaction_version=session.metadata.get("interaction_version"),
            context_changes=session.metadata.get("context_changes", []),
            source=session.metadata.get("source"),
            memory_space_id=session.memory_space_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as e:
        logger.error(f"Failed to create chat session for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create chat session",
        )


@router.get("/sessions", response_model=List[ChatSessionResponse])
async def get_chat_sessions(
    request: Request,
    limit: int = 50,
    memory_space_id: Optional[str] = None,
    current_user: User = Depends(current_active_user),
):
    """Get all chat sessions for the current user."""
    try:
        chat_service = get_chat_service()
        sessions = await chat_service.get_user_sessions(
            user_id=str(current_user.id),
            limit=min(limit, 100),
            memory_space_id=memory_space_id,
        )

        # Consume disconnects directly: Request.is_disconnected() polls inside
        # an immediately cancelled scope, which cannot traverse the logging
        # middleware's async receive wrapper reliably. This GET has no body.
        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        task = asyncio.create_task(
            privacy.filter_chat_sessions(str(current_user.id), sessions)
        )
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                {task, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                await watcher
                raise HTTPException(499, "Chat loading request disconnected")
            admitted = await task
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
        session_responses = []
        for session, message_count in admitted:
            session_responses.append(
                ChatSessionResponse(
                    session_id=session.session_id,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    updated_at=session.updated_at.isoformat(),
                    sources=session.metadata.get("sources", []),
                    interaction_version=session.metadata.get("interaction_version"),
                    context_changes=session.metadata.get("context_changes", []),
                    source=session.metadata.get("source"),
                    memory_space_id=session.memory_space_id,
                    message_count=message_count,
                )
            )

        return session_responses
    except PrivacyHeld as exc:
        raise HTTPException(423, "Chat evidence changed during loading") from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get chat sessions for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat sessions",
        )


@router.get("/sessions/{session_id}", response_model=ChatSessionResponse)
async def get_chat_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    """Get a specific chat session."""
    try:
        chat_service = get_chat_service()
        session = await owned_chat(chat_service, session_id, str(current_user.id))

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        return ChatSessionResponse(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
            sources=session.metadata.get("sources", []),
            interaction_version=session.metadata.get("interaction_version"),
            context_changes=session.metadata.get("context_changes", []),
            source=session.metadata.get("source"),
            memory_space_id=session.memory_space_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get chat session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat session",
        )


@router.put("/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_chat_session(
    session_id: str,
    request: ChatSessionUpdateRequest,
    current_user: User = Depends(current_active_user),
):
    service = get_chat_service()
    user_id = str(current_user.id)
    try:
        async with distributed_lock(
            f"chat-interaction:{session_id}",
            timeout=120,
            blocking_timeout=0,
            renew=True,
        ):
            session = await owned_chat(service, session_id, user_id)
            if session is None:
                raise HTTPException(404, "Chat session not found")
            writable(session)
            if session.memory_space_id:
                await MemoryScopeResolver().require_space(
                    MemoryScope(user_id, session.memory_space_id), writable=True
                )
            if "sources" in request.model_fields_set:
                if request.sources is None:
                    raise HTTPException(422, "Use an empty list to remove attachments")
                refs = unique_sources(request.sources)
                await resolve_context(refs, user_id, session.memory_space_id)
                serialized = [r.model_dump(mode="json") for r in refs]
                if serialized != session.metadata.get("sources", []):
                    session.metadata["sources"] = serialized
                    session.metadata.setdefault("context_changes", []).append(
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "sources": serialized,
                        }
                    )
            changes = {
                "metadata": session.metadata,
                "updated_at": datetime.now(timezone.utc),
            }
            if request.title is not None:
                changes["title"] = request.title
            await service.sessions_collection.update_one(
                {"session_id": session_id, "user_id": user_id}, {"$set": changes}
            )
            return session_response(await owned_chat(service, session_id, user_id))
    except (ValueError, LockUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/sessions/{session_id}/sources")
async def get_chat_sources(
    session_id: str, current_user: User = Depends(current_active_user)
):
    session = await owned_chat(get_chat_service(), session_id, str(current_user.id))
    if session is None:
        raise HTTPException(404, "Chat session not found")
    try:
        return await resolve_context(
            [
                ChatSourceRef.model_validate(r)
                for r in session.metadata.get("sources", [])
            ],
            str(current_user.id),
            session.memory_space_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/sessions/{session_id}")
async def delete_chat_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    """Delete a chat session and all its messages."""
    try:
        chat_service = get_chat_service()
        success = await chat_service.delete_session(session_id, str(current_user.id))

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        return {"message": "Chat session deleted successfully"}
    except HTTPException:
        raise
    except (ValueError, LockUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as e:
        logger.error(
            f"Failed to delete chat session {session_id} for user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete chat session",
        )


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(current_active_user),
):
    """Get all messages in a chat session."""
    try:
        chat_service = get_chat_service()

        # Verify session exists and belongs to user
        session = await owned_chat(chat_service, session_id, str(current_user.id))
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
            )

        messages = await chat_service.get_session_messages(
            session_id=session_id,
            user_id=str(current_user.id),
            limit=limit,
            offset=offset,
        )

        try:
            await privacy.check_payload(
                str(current_user.id), [msg.metadata for msg in messages]
            )
        except privacy.PrivacyHeld as exc:
            raise HTTPException(
                423, "Chat evidence is held by privacy settings"
            ) from exc

        return [
            ChatMessageResponse(
                message_id=msg.message_id,
                session_id=msg.session_id,
                role=msg.role,
                content=msg.content,
                timestamp=msg.timestamp.isoformat(),
                memories_used=msg.memories_used,
                source_citations=msg.metadata.get("source_citations", []),
                source_revision=msg.metadata.get("source_revision"),
                source_coverage=msg.metadata.get("source_coverage"),
                run_id=msg.metadata.get("run_id"),
                evidence=msg.metadata.get("evidence"),
                utterance=msg.utterance(),
            )
            for msg in messages
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get messages for session {session_id}, user {current_user.id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve messages",
        )


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest, current_user: User = Depends(current_active_user)
):
    """OpenAI-compatible chat completions endpoint with streaming support."""
    try:
        chat_service = get_chat_service()

        # Create new session if not provided
        if not request.session_id:
            session = await chat_service.create_session(str(current_user.id))
            session_id = session.session_id
        else:
            session_id = request.session_id
            session = await owned_chat(chat_service, session_id, str(current_user.id))
            if not session:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Chat session not found",
                )

        writable(session)

        # Extract the latest user message
        user_messages = [m for m in request.messages if m.role == "user"]
        if not user_messages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one user message is required",
            )
        message_content = user_messages[-1].content

        model_name = getattr(chat_service.llm_client, "model", None) or "chronicle"
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if request.stream:
            return StreamingResponse(
                _stream_openai_format(
                    chat_service,
                    session_id,
                    str(current_user.id),
                    message_content,
                    completion_id,
                    created,
                    model_name,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            return await _non_streaming_response(
                chat_service,
                session_id,
                str(current_user.id),
                message_content,
                completion_id,
                created,
                model_name,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process message for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message",
        )


async def _stream_openai_format(
    chat_service,
    session_id: str,
    user_id: str,
    message_content: str,
    completion_id: str,
    created: int,
    model_name: str,
):
    """Map internal streaming events to OpenAI SSE chunk format."""
    previous_text = ""
    try:
        async with aclosing(
            chat_service.generate_response_stream(
                session_id=session_id,
                user_id=user_id,
                message_content=message_content,
            )
        ) as events:
            async for event in events:
                event_type = event.get("type")

                if event_type in {"evidence", "source_context", "run", "dialogue"}:
                    # First chunk: send role + chronicle metadata
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=model_name,
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(role="assistant"),
                            )
                        ],
                        chronicle_metadata={
                            "session_id": session_id,
                            **(
                                {"source_context": event["data"]}
                                if event_type == "source_context"
                                else event["data"]
                            ),
                        },
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"

                elif event_type == "status":
                    # Progress only — no delta, so the OpenAI shape stays valid for
                    # third-party clients, which ignore chronicle_metadata entirely.
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=model_name,
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(),
                            )
                        ],
                        chronicle_metadata={
                            "session_id": session_id,
                            "status": event["data"],
                        },
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"

                elif event_type == "token_reset":
                    # The streamed text belonged to a tool round and has been retracted.
                    # Re-baseline the delta cursor so the next round starts from empty.
                    previous_text = ""
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=model_name,
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(),
                            )
                        ],
                        chronicle_metadata={
                            "session_id": session_id,
                            "reset_content": True,
                        },
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"

                elif event_type == "token":
                    # Internal events carry accumulated text; compute delta
                    accumulated = event["data"]
                    delta_text = accumulated[len(previous_text) :]
                    previous_text = accumulated
                    if delta_text:
                        chunk = ChatCompletionChunk(
                            id=completion_id,
                            created=created,
                            model=model_name,
                            choices=[
                                ChatCompletionChunkChoice(
                                    delta=ChatCompletionChunkDelta(content=delta_text),
                                )
                            ],
                        )
                        yield f"data: {chunk.model_dump_json()}\n\n"

                elif event_type == "complete":
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=model_name,
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(),
                                finish_reason="stop",
                            )
                        ],
                        chronicle_metadata={
                            "session_id": session_id,
                            "run_id": event["data"].get("run_id"),
                            "recording_degraded": event["data"].get(
                                "recording_degraded", False
                            ),
                            "message_id": event["data"].get("message_id"),
                            "evidence": event["data"].get("evidence"),
                        },
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"

                elif event_type == "error":
                    error_obj = {
                        "error": {
                            "message": event["data"].get("error", "Unknown error"),
                            "type": "server_error",
                        },
                        "chronicle_metadata": {
                            "run_id": event["data"].get("run_id"),
                            "recording_degraded": event["data"].get(
                                "recording_degraded", False
                            ),
                        },
                    }
                    yield f"data: {json.dumps(error_obj)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Error in streaming response: {e}")
        error_obj = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_obj)}\n\n"


async def _non_streaming_response(
    chat_service,
    session_id: str,
    user_id: str,
    message_content: str,
    completion_id: str,
    created: int,
    model_name: str,
) -> ChatCompletionResponse:
    """Collect all events and return a single ChatCompletionResponse."""
    full_content = ""
    metadata: Dict[str, Any] = {"session_id": session_id}

    async with aclosing(
        chat_service.generate_response_stream(
            session_id=session_id,
            user_id=user_id,
            message_content=message_content,
        )
    ) as events:
        async for event in events:
            event_type = event.get("type")

            if event_type in {"evidence", "run"}:
                metadata.update(event["data"])
            elif event_type == "source_context":
                metadata["source_context"] = event["data"]
            elif event_type == "token":
                full_content = event["data"]  # accumulated text
            elif event_type == "token_reset":
                # Retracted tool-round narration must not survive into the reply when
                # a later round ends without producing any text of its own.
                full_content = ""
            elif event_type == "complete":
                metadata.update(event["data"])
                metadata["message_id"] = event["data"].get("message_id")
                metadata["evidence"] = event["data"].get("evidence")
            elif event_type == "error":
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=event["data"].get("error", "Unknown error"),
                )

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_name,
        choices=[
            ChatCompletionChoice(
                message=ChatCompletionMessage(
                    role="assistant", content=full_content.strip()
                ),
            )
        ],
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        session_id=session_id,
        chronicle_metadata=metadata,
    )


@router.get("/statistics", response_model=ChatStatisticsResponse)
async def get_chat_statistics(current_user: User = Depends(current_active_user)):
    """Get chat statistics for the current user."""
    try:
        chat_service = get_chat_service()
        stats = await chat_service.get_chat_statistics(str(current_user.id))

        return ChatStatisticsResponse(
            total_sessions=stats["total_sessions"],
            total_messages=stats["total_messages"],
            last_chat=stats["last_chat"].isoformat() if stats["last_chat"] else None,
        )
    except Exception as e:
        logger.error(f"Failed to get chat statistics for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve chat statistics",
        )


@router.post("/sessions/{session_id}/extract-memories")
async def extract_memories_from_session(
    session_id: str, current_user: User = Depends(current_active_user)
):
    session = await owned_chat(get_chat_service(), session_id, str(current_user.id))
    if session is None:
        raise HTTPException(404, "Chat session not found")
    raise HTTPException(
        409, "Direct extraction is unavailable. Review and save from a new chat."
    )


class ProposalDecision(BaseModel):
    generation: str
    selected_change_ids: List[str] = Field(default_factory=list)


async def review_call(function, *args, **kwargs):
    try:
        return await function(*args, **kwargs)
    except (ValueError, LockUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions/{session_id}/save-proposals", status_code=202)
async def create_save_proposal(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(current_active_user),
):

    result = await review_call(
        chat_review.create_proposal, session_id, str(current_user.id)
    )
    background_tasks.add_task(chat_review.process_chat_review_queue)
    return result


@router.get("/sessions/{session_id}/save-proposals/latest")
async def latest_save_proposal(
    session_id: str, current_user: User = Depends(current_active_user)
):

    return await review_call(chat_review.get_proposal, session_id, str(current_user.id))


@router.get("/sessions/{session_id}/save-proposals/{proposal_id}")
async def read_save_proposal(
    session_id: str, proposal_id: str, current_user: User = Depends(current_active_user)
):

    return await review_call(
        chat_review.get_proposal, session_id, str(current_user.id), proposal_id
    )


@router.post("/sessions/{session_id}/save-proposals/{proposal_id}/{action}")
async def decide_save_proposal(
    session_id: str,
    proposal_id: str,
    action: str,
    body: ProposalDecision,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(current_active_user),
):

    if action not in {"approve", "discard", "retry"}:
        raise HTTPException(404, "Unknown review action")
    result = await review_call(
        chat_review.decide_proposal,
        session_id,
        str(current_user.id),
        proposal_id,
        body.generation,
        body.selected_change_ids,
        discard=action == "discard",
        retry=action == "retry",
    )
    background_tasks.add_task(chat_review.process_chat_review_queue)
    return result


@router.get("/health")
async def chat_health_check():
    """Health check endpoint for chat service."""
    try:
        chat_service = get_chat_service()
        # Simple health check - verify service can be initialized
        if not chat_service._initialized:
            await chat_service.initialize()

        return {"status": "healthy", "service": "chat", "timestamp": time.time()}
    except Exception as e:
        logger.error(f"Chat service health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat service is not available",
        )
