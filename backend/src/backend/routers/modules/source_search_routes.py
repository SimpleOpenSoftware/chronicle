"""Search and recording-to-session navigation, scoped to the signed-in owner."""

import datetime as datetime
import zoneinfo as zoneinfo
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import backend.models.session_memory as session_memory
import backend.models.timeline as timeline
import backend.redis_keys as redis_keys
import backend.services.memory.scope as scope
import backend.services.redis_lock as redis_lock
import backend.services.timeline.accepted_context as accepted_context
import backend.services.timeline.explicit_reconciliation as explicit_reconciliation
import backend.services.timeline.review as review
import backend.services.timeline.sessions as sessions
from backend.auth import current_active_user
from backend.services import source_search
from backend.services.timeline import recording_sessions
from backend.users import User

router = APIRouter(tags=["Source search"])


@router.post("/search/retry-index", status_code=202)
async def retry_search_index(user: User = Depends(current_active_user)):
    await source_search.db().source_search_jobs.update_one(
        {"_id": "recovery", "state": "failed"},
        {"$set": {"attempts": 0, "state": "queued"}},
    )
    await source_search.recover_search_index()
    return {"state": "queued"}


@router.get("/search")
async def search_sources(
    q: str = Query(default="", max_length=200),
    kinds: list[Literal["recording", "episode", "session"]] = Query(
        default=["recording", "episode", "session"]
    ),
    fields: list[Literal["id", "title", "summary", "speakers", "transcript"]] = Query(
        default=["id", "title", "summary", "speakers", "transcript"]
    ),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    memory_space_id: str | None = None,
    user: User = Depends(current_active_user),
):
    await check_space(str(user.id), memory_space_id)
    return await source_search.search(
        str(user.id),
        q.strip(),
        kinds=kinds,
        fields=fields,
        limit=limit,
        offset=offset,
        memory_space_id=memory_space_id,
    )


class RecordingScope(BaseModel):
    memory_space_id: str | None = None


class UndatedMemoryRequest(RecordingScope):
    revision: int = Field(ge=1)
    excluded_source_keys: list[str] = Field(default_factory=list)


class OrganizeRecordingDay(BaseModel):
    local_date: str
    timezone: str = "Asia/Kolkata"


@router.post("/recordings/{identifier}/organize-day", status_code=202)
async def organize_recording_day(
    identifier: str,
    body: OrganizeRecordingDay,
    user: User = Depends(current_active_user),
):

    context = await recording_context(identifier, body.timezone, None, user)
    if body.local_date not in context["dates"]:
        raise HTTPException(409, "Choose one of the recording's known capture dates")
    identity = {
        "user_id": str(user.id),
        "local_date": body.local_date,
        "timezone": body.timezone,
    }
    await source_search.db().recording_organization_intents.update_one(
        identity, {"$set": {"recording_id": identifier}}, upsert=True
    )
    request, created = await explicit_reconciliation.request_explicit_reconciliation(
        user=user,
        local_date=datetime.date.fromisoformat(body.local_date),
        timezone_name=body.timezone,
    )
    await source_search.db().recording_organization_intents.update_one(
        identity, {"$set": {"request_id": request.request_id}}
    )
    return {
        **explicit_reconciliation.reconciliation_request_payload(request),
        "created": created,
    }


class UndatedDecision(RecordingScope):
    revision: int = Field(ge=1)
    action: Literal["exclude", "include", "clarify", "attribute"]
    source_keys: list[str] = Field(min_length=1)
    clarification: str | None = Field(default=None, max_length=2000)
    role: Literal["user_statement", "third_party", "media_content"] | None = None


@router.post("/sessions/undated/{identifier}/decision")
async def decide_undated(
    identifier: str, body: UndatedDecision, user: User = Depends(current_active_user)
):

    await check_space(str(user.id), body.memory_space_id)
    async with redis_lock.distributed_lock(
        redis_keys.timeline_publication_lock(str(user.id)),
        timeout=60,
        blocking_timeout=5,
    ):
        session = await session_memory.UndatedSession.find_one(
            {
                "session_key": identifier,
                "revision": body.revision,
                "user_id": str(user.id),
                "memory_space_id": body.memory_space_id,
            }
        )
        if session is None:
            raise HTTPException(404, "Session not found")
        try:
            recording = await recording_sessions.owned_recording(
                str(user.id), session.recording_id, body.memory_space_id
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if recording_sessions.recording_hash(recording) != session.source_hash:
            raise HTTPException(409, "Recording changed; prepare its current revision")
        if not set(body.source_keys) <= {s["key"] for s in session.sources}:
            raise HTTPException(409, "Source selection changed")
        if body.action == "clarify" and not (body.clarification or "").strip():
            raise HTTPException(422, "Enter a clarification")
        if body.action == "attribute" and body.role is None:
            raise HTTPException(422, "Choose a source role")
        rows = await timeline.MemoryReviewProposal.find(
            {"user_id": str(user.id), "session_key": identifier}
        ).to_list()
        if any(p.state == "applying" for p in rows):
            raise HTTPException(409, "Wait for the current note application to finish")
        await session_memory.MemorySourceDecision(
            user_id=str(user.id),
            memory_space_id=body.memory_space_id,
            session_key=identifier,
            action=body.action,
            sources=[s for s in session.sources if s["key"] in body.source_keys],
            clarification=body.clarification,
            role=body.role,
        ).insert()
        for proposal in rows:
            await proposal.set(
                {
                    "state": (
                        "correction_required"
                        if proposal.accepted_change_ids
                        else "stale"
                    ),
                    "active": False,
                }
            )
        return {"correction_required": any(p.accepted_change_ids for p in rows)}


async def check_space(user_id, space):
    if space:

        try:
            await scope.MemoryScopeResolver().require_space(
                scope.MemoryScope(user_id, space)
            )
        except scope.MemoryScopeError as exc:
            raise HTTPException(404, "Memory space not found") from exc


@router.get("/recordings/{identifier}/context")
async def recording_context(
    identifier: str,
    timezone: str = "Asia/Kolkata",
    memory_space_id: str | None = None,
    user: User = Depends(current_active_user),
):

    try:
        zoneinfo.ZoneInfo(timezone)
        await check_space(str(user.id), memory_space_id)
        return await recording_sessions.recording_context(
            str(user.id), identifier, timezone, memory_space_id
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/recordings/{identifier}/undated-session", status_code=201)
async def prepare_undated(
    identifier: str, body: RecordingScope, user: User = Depends(current_active_user)
):
    try:
        await check_space(str(user.id), body.memory_space_id)
        row = await recording_sessions.prepare_undated(
            str(user.id), identifier, body.memory_space_id
        )
        return row.model_dump(mode="json", exclude={"id"})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions/undated/{identifier}/memory", status_code=202)
async def generate_undated(
    identifier: str,
    body: UndatedMemoryRequest,
    user: User = Depends(current_active_user),
):
    try:
        await check_space(str(user.id), body.memory_space_id)
        row = await recording_sessions.generate_undated(
            str(user.id),
            identifier,
            body.revision,
            excluded_keys=body.excluded_source_keys,
            memory_space_id=body.memory_space_id,
        )
        return row.model_dump(mode="json", exclude={"id"})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/context-refreshes")
async def context_refreshes(
    local_date: str | None = None,
    memory_space_id: str | None = None,
    recording_id: str | None = None,
    user: User = Depends(current_active_user),
):

    await check_space(str(user.id), memory_space_id)
    query = {
        "user_id": str(user.id),
        "memory_space_id": memory_space_id,
        "state": {
            "$nin": ["stale", "corrected", "excluded", "rejected", "regenerating"]
        },
        "refresh_assessment.verdict": {"$in": ["useful", "uncertain"]},
    }
    if local_date:
        try:
            query["local_date"] = datetime.date.fromisoformat(local_date)
        except ValueError as exc:
            raise HTTPException(422, "Invalid local date") from exc
    if recording_id:
        query["recording_id"] = recording_id
    rows = (
        await timeline.MemoryReviewProposal.find(query)
        .sort("-created_at")
        .limit(100)
        .to_list()
    )
    return {
        "items": [
            {
                "proposal_id": p.proposal_id,
                "session_key": p.session_key,
                "local_date": p.local_date,
                "recording_id": p.recording_id,
                "state": p.state,
                "title": (p.account or {}).get("title", "Session"),
                "assessment": p.refresh_assessment,
                "correction_required": bool(p.accepted_change_ids),
            }
            for p in rows
        ]
    }


class ContextCheck(RecordingScope):
    local_date: str | None = None
    recording_id: str | None = None


@router.post("/context-refreshes/check", status_code=202)
async def check_opened_context(
    body: ContextCheck, user: User = Depends(current_active_user)
):

    await check_space(str(user.id), body.memory_space_id)
    query = {
        "user_id": str(user.id),
        "memory_space_id": body.memory_space_id,
        "replacement_proposal_id": None,
        "corrected_by_proposal_id": None,
    }
    if body.recording_id:
        query["recording_id"] = body.recording_id
    elif body.local_date:
        try:
            query["local_date"] = datetime.date.fromisoformat(body.local_date)
        except ValueError as exc:
            raise HTTPException(422, "Invalid local date") from exc
    else:
        raise HTTPException(422, "Choose a day or recording to assess")
    rows = (
        await timeline.MemoryReviewProposal.find(query)
        .sort("-created_at")
        .limit(100)
        .to_list()
    )
    for proposal in rows:
        await source_search.db().context_assessment_requests.update_one(
            {"_id": proposal.proposal_id},
            {
                "$set": {
                    "proposal_id": proposal.proposal_id,
                    "user_id": str(user.id),
                    "memory_space_id": body.memory_space_id,
                    "state": "queued",
                }
            },
            upsert=True,
        )
    if rows:
        await accepted_context.queue_context_assessment(
            str(user.id), body.memory_space_id
        )
    return {"queued": len(rows)}


@router.post("/context-refreshes/{proposal_id}/refresh", status_code=202)
async def refresh_context(proposal_id: str, user: User = Depends(current_active_user)):

    proposal = await timeline.MemoryReviewProposal.find_one(
        {"user_id": str(user.id), "proposal_id": proposal_id}
    )
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    await check_space(str(user.id), proposal.memory_space_id)
    try:
        if proposal.accepted_change_ids:
            replacement = await review.request_memory_correction(proposal)
            await sessions.enqueue_memory(replacement)
            await proposal.set({"refresh_assessment": None})
            return {
                "correction_required": True,
                "proposal_id": replacement.proposal_id,
                "state": replacement.state,
            }
        replacement = await review.queue_memory_review_regeneration(proposal)
        await sessions.enqueue_memory(replacement)
        await proposal.set({"refresh_assessment": None})
        return {"proposal_id": replacement.proposal_id, "state": replacement.state}
    except review.MemoryReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
