"""Authenticated semantic timeline APIs."""

import asyncio
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

import backend.models.session_memory as session_memory_module
import backend.services.inference_artifacts as inference_artifacts
import backend.services.privacy as privacy
import backend.services.timeline.memory_sources as memory_sources
from backend.auth import current_active_user
from backend.models.audio_chunk import AudioChunkDocument
from backend.models.conversation import Conversation
from backend.models.notification import NotificationIntent
from backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    MemoryReviewProposal,
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
    TimelineReconciliationRequest,
    TimelineSemanticGroupRevision,
    clip_audio_ranges,
    merge_audio_ranges,
    utcnow,
)
from backend.models.user import User
from backend.redis_keys import timeline_publication_lock
from backend.services.audio_claims import resolve_audio_ranges
from backend.services.job_progress import read_job_progress
from backend.services.redis_lock import distributed_lock
from backend.services.timeline import sessions as session_memory
from backend.services.timeline.activity_policy import (
    episode_is_recording_only,
    episode_requires_activity_review,
    rejection_basis,
)
from backend.services.timeline.consolidation import (
    ConsolidationResolutionError,
    ConsolidationSynthesisError,
    active_semantic_groups,
    activity_review_suggestions,
    create_manual_semantic_group,
    queue_day_consolidation,
    remove_semantic_group,
    resolve_day_consolidation,
    snapshot_episodes,
    suggestions_match_snapshot,
)
from backend.services.timeline.dirty_ranges import (
    DirtyRangeDismissalError,
    dismiss_failed_range,
)
from backend.services.timeline.dispatch import enqueue_episode_detailed_summary
from backend.services.timeline.episode_summary import (
    STRUCTURAL_CONFIRMATION_FIELDS,
    episode_structure_is_stable,
)
from backend.services.timeline.evidence import load_reconciliation_evidence
from backend.services.timeline.evidence_relations import (
    EvidenceRelationPreview,
    infer_evidence_relations,
)
from backend.services.timeline.explicit_reconciliation import (
    reconciliation_request_payload,
    request_explicit_reconciliation,
)
from backend.services.timeline.manual_publication import (
    ManualPublicationConflict,
    day_for_exact_episode,
    publish_manual_episode_change,
)
from backend.services.timeline.memory import episode_semantic_memory_enabled
from backend.services.timeline.merge_synthesis import synthesize_merged_episode_account
from backend.services.timeline.projection import active_day_episodes
from backend.services.timeline.review import (
    MemoryReviewError,
    create_memory_selection,
    episode_review_outcomes,
    generate_memory_review,
    process_memory_review_decision,
    process_memory_review_queue,
    queue_memory_review_regeneration,
    request_memory_correction,
    resolve_memory_review,
)
from backend.services.timeline.review_projection import build_day_review_projection
from backend.services.timeline.timezone import canonical_timezone

router = APIRouter(prefix="/timeline", tags=["timeline"])

# One manual request may not dirty an unbounded interval: the reconciler expands
# agentically from what it is given, so the request is the outer budget.
MANUAL_RECONCILE_MAX_RANGE = timedelta(hours=6)


class DismissFailedRangeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _at(value: datetime) -> datetime:
    """UTC-normalize a timestamp that is known to be present."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _validate_timezone(value: str) -> str:
    try:
        return canonical_timezone(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown IANA timezone") from error


def _run_payload(run: TimelineAnalysisRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "state": run.state,
        "attempts": run.attempts,
        "retry_after": _utc(run.retry_after),
        "error": run.error,
        "requested_evidence_revision": run.evidence_revision,
        "processed_evidence_revision": run.processed_evidence_revision,
        "created_at": _utc(run.created_at),
        "completed_at": _utc(run.completed_at),
    }


def _episode_payload(episode: TimelineEpisode) -> dict:
    return {
        "episode_id": episode.episode_id,
        "episode_key": episode.episode_key,
        "revision": episode.revision,
        "started_at": _utc(episode.started_at),
        "ended_at": _utc(episode.ended_at),
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "detailed_summary": episode.detailed_summary,
        "status": episode.status,
        "confirmed_at": _utc(episode.confirmed_at),
        "confirmed_fields": episode.confirmed_fields,
        "memory_policy": episode.memory_policy,
        "requires_activity_review": episode_requires_activity_review(episode),
        "memory_eligible": episode_semantic_memory_enabled(episode),
        "salience": episode.salience,
        "confidence": episode.confidence,
        "activity_mode": episode.activity_mode,
        "entities": episode.entities,
        "attributes": episode.attributes,
        "assertions": [
            assertion.model_dump(mode="json") for assertion in episode.assertions
        ],
        "evidence": [
            ref.model_copy(
                update={
                    "started_at": _utc(ref.started_at),
                    "ended_at": _utc(ref.ended_at),
                }
            ).model_dump(mode="json")
            for ref in episode.evidence_refs
        ],
        "related_episode_ids": episode.related_episode_ids,
        "related_conversation_ids": episode.related_conversation_ids,
        "audio_ranges": [item.model_dump(mode="json") for item in episode.audio_ranges],
        "parent_episode_id": episode.parent_episode_id,
        "has_thumbnail": bool(episode.representative_image),
    }


def _proposal_payload(
    proposal: MemoryReviewProposal | None,
    *,
    full: bool = False,
    exchanges: bool = False,
):
    if proposal is None:
        return None
    payload = {
        "proposal_id": proposal.proposal_id,
        "request_id": proposal.request_id,
        "generation": proposal.generation,
        "selected_episodes": [ref.model_dump() for ref in proposal.selected_episodes],
        "local_date": proposal.local_date,
        "timezone": proposal.timezone,
        "active": proposal.active,
        "replacement_proposal_id": proposal.replacement_proposal_id,
        "supersedes_proposal_id": proposal.supersedes_proposal_id,
        "freshness": proposal.freshness.model_dump() if proposal.freshness else None,
        "state": proposal.state,
        "snapshot_id": proposal.snapshot_id,
        "change_count": len(proposal.changes),
        "accepted_change_ids": proposal.accepted_change_ids,
        "rejected_change_ids": proposal.rejected_change_ids,
        "error": proposal.error,
        "created_at": _utc(proposal.created_at),
        "generated_at": _utc(proposal.generated_at),
        "resolved_at": _utc(proposal.resolved_at),
    }
    if full:
        payload["revision_feedback"] = proposal.revision_feedback
        payload["account"] = proposal.account
        payload["changes"] = [
            change.model_dump(mode="json") for change in proposal.changes
        ]
    if exchanges:
        payload["accepted_context"] = proposal.accepted_context
        payload["source_digest"] = proposal.source_digest
        payload["source_scope"] = proposal.source_scope
        payload["inference_runs"] = proposal.inference_runs
        payload["writer_inference_artifacts"] = proposal.writer_inference_artifacts
    payload.update(
        session_key=proposal.session_key,
        stage=proposal.stage,
        questions=proposal.questions,
        completed_sources=proposal.completed_sources,
        total_sources=proposal.total_sources,
        investigation_activity=proposal.investigation_activity,
        failure_kind=proposal.failure_kind,
    )
    return payload


@router.get("/review/proposals/{proposal_id}/exchanges")
async def get_memory_model_exchanges(
    proposal_id: str, user: User = Depends(current_active_user)
):
    proposal = await MemoryReviewProposal.find_one(
        {"proposal_id": proposal_id, "user_id": str(user.id)}
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Memory proposal not found")

    policy = await privacy.guard_payload(str(user.id), proposal)
    payload = _proposal_payload(proposal, full=True, exchanges=True)

    payload["inference_runs"] = []
    refs = list(proposal.inference_runs)
    assessment = proposal.refresh_assessment or {}
    if (
        assessment.get("operation")
        and assessment.get("artifact_hash")
        and not any(ref["artifact_hash"] == assessment["artifact_hash"] for ref in refs)
    ):
        refs.append({key: assessment[key] for key in ("operation", "artifact_hash")})
    for ref in refs:
        artifact = await asyncio.to_thread(
            inference_artifacts.read_inference_artifact,
            ref["operation"],
            ref["artifact_hash"],
        )
        payload["inference_runs"].append(
            {
                **ref,
                "request": artifact["request"],
                "exchanges": artifact.get("metadata", {}).get("exchanges", []),
                "model_input": artifact.get("metadata", {}).get("model_input"),
                "tool_calls": artifact.get("metadata", {}).get("tool_calls", []),
                "context": artifact.get("metadata", {}).get("context"),
                "output": artifact["stdout"],
                "result": artifact["result"],
            }
        )
    payload["writer_exchanges"] = [
        await asyncio.to_thread(
            inference_artifacts.read_inference_artifact,
            ref["operation"],
            ref["artifact_hash"],
        )
        for ref in proposal.writer_inference_artifacts
    ]
    await privacy.guard_payload(str(user.id), payload, snapshot=policy)
    await privacy.assert_current(str(user.id), policy)
    return payload


def _semantic_group_payload(group: TimelineSemanticGroupRevision) -> dict[str, Any]:
    return group.model_copy(
        update={
            "started_at": _utc(group.started_at),
            "ended_at": _utc(group.ended_at),
            "created_at": _utc(group.created_at),
        }
    ).model_dump(mode="json")


async def _day_proposal(day: TimelineDay | None) -> MemoryReviewProposal | None:
    return None  # Memory selections are listed independently of day structural review.


def _consolidation_payload(
    day: TimelineDay, episodes: list[TimelineEpisode]
) -> dict[str, Any]:
    """Return only proposals derived from the day membership being displayed."""

    suggestions = activity_review_suggestions(day.consolidation_suggestions, episodes)
    stale = not suggestions_match_snapshot(day.consolidation_suggestions, day)
    filtered_all = (
        day.consolidation_state == "ready"
        and bool(day.consolidation_suggestions)
        and not suggestions
    )
    return {
        "state": "" if stale or filtered_all else day.consolidation_state,
        "snapshot_id": day.consolidation_snapshot_id,
        "model": day.consolidation_model,
        "suggestions": [] if stale else suggestions,
        "error": day.consolidation_error,
        "generated_at": day.consolidation_generated_at,
    }


def _review_payload(day: TimelineDay | None, proposal: MemoryReviewProposal | None):
    if day is None:
        return None
    return {
        "state": day.review_state,
        "review_snapshot_id": day.review_snapshot_id,
        "episodes_reviewed_at": _utc(day.episodes_reviewed_at),
        "resolved_at": _utc(day.review_resolved_at),
        "outcome": day.review_outcome,
        "error": day.review_error,
        "proposal": _proposal_payload(proposal, full=True),
    }


async def _reconciliation_payload(
    owner: str, local_date: date, timezone_name: str
) -> dict:
    zone = ZoneInfo(timezone_name)
    started_at = datetime.combine(
        local_date, datetime.min.time(), tzinfo=zone
    ).astimezone(timezone.utc)
    ended_at = (started_at.astimezone(zone) + timedelta(days=1)).astimezone(
        timezone.utc
    )
    ranges = (
        await DirtyEvidenceRange.find(
            {
                "user_id": owner,
                "state": {
                    "$in": [
                        "pending",
                        "authorized_pending",
                        "leased",
                        "waiting",
                        "awaiting_context",
                        "context_pending",
                        "failed",
                    ]
                },
                "started_at": {"$lt": ended_at},
                "ended_at": {"$gt": started_at},
            }
        )
        .sort("started_at")
        .to_list()
    )
    return {"ranges": [_dirty_range_payload(item) for item in ranges]}


def _dirty_range_payload(item: DirtyEvidenceRange) -> dict:
    return {
        "dirty_range_id": item.dirty_range_id,
        "started_at": _utc(item.started_at),
        "ended_at": _utc(item.ended_at),
        "state": item.state,
        "trigger_reasons": item.trigger_reasons,
        "attempts": item.attempts,
        "error": item.last_error,
        "resolution_history": [
            resolution.model_dump(mode="json") for resolution in item.resolution_history
        ],
    }


@router.get("/day")
async def get_timeline_day(
    local_date: date = Query(alias="date"),
    timezone: str = Query(),
    user: User = Depends(current_active_user),
):

    timezone = _validate_timezone(timezone)
    owner = str(user.id)
    policy = await privacy.load_snapshot(owner)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == owner,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone,
    )
    episodes: list[TimelineEpisode] = []
    snapshot_unavailable = False
    if day and day.current_snapshot is not None:
        try:
            episodes = await snapshot_episodes(day)
        except (ConsolidationResolutionError, privacy.PrivacyHeld):
            episodes = []
            snapshot_unavailable = True
    else:
        episodes = await active_day_episodes(owner, local_date, timezone)
    episodes = [
        episode for episode in episodes if not episode_is_recording_only(episode)
    ]
    zone = ZoneInfo(timezone)

    episodes = await privacy.filter_records(episodes, owner)
    day_start = datetime.combine(
        local_date, datetime.min.time(), tzinfo=zone
    ).astimezone(UTC)
    day_end = datetime.combine(
        local_date + timedelta(days=1), datetime.min.time(), tzinfo=zone
    ).astimezone(UTC)
    spans = await AudioEvidenceSpan.find(
        {
            "user_id": owner,
            "started_at": {"$lt": day_end},
            "ended_at": {"$gt": day_start},
            "state": "no_speech",
        }
    ).to_list()
    recording_intervals = [
        {
            "started_at": _at(span.started_at),
            "ended_at": _at(span.ended_at),
            "source": span.locator.track_id,
            "state": span.state,
            "covered_seconds": span.covered_seconds,
            "missing_seconds": span.missing_seconds,
            "acoustic_active_seconds": span.acoustic_active_seconds,
        }
        for span in spans
    ]
    latest = (
        await TimelineAnalysisRun.find(
            TimelineAnalysisRun.user_id == owner,
            TimelineAnalysisRun.local_date == local_date,
            TimelineAnalysisRun.timezone == timezone,
        )
        .sort("-created_at")
        .first_or_none()
    )
    proposal = await _day_proposal(day)
    semantic_groups = []
    if day and not snapshot_unavailable:
        active_groups = active_semantic_groups(day)
        if active_groups:
            # Resolve every exact member, including members on another day.
            # A saved group summary cannot be separated when a member is absent
            # or private, even if the displayed day's own episodes are allowed.
            resolved = await session_memory.resolved_sessions(day, episodes)
            exact_groups = {
                (group.group_key, group.revision)
                for resolved_owner, group, _members in resolved
                if resolved_owner.local_date == day.local_date
            }
            semantic_groups = await privacy.filter_payloads(
                [g for g in active_groups if (g.group_key, g.revision) in exact_groups],
                owner,
            )
    reconciliation = await _reconciliation_payload(owner, local_date, timezone)
    latest_request = (
        await TimelineReconciliationRequest.find(
            TimelineReconciliationRequest.user_id == owner,
            TimelineReconciliationRequest.local_date == local_date,
            TimelineReconciliationRequest.timezone == timezone,
        )
        .sort("-created_at")
        .first_or_none()
    )
    request_payload = None
    if latest_request:
        request_payload = reconciliation_request_payload(latest_request)
        request_payload["progress"] = await asyncio.to_thread(
            read_job_progress, latest_request.job_id
        )
    payload = {
        "date": local_date,
        "timezone": timezone,
        "current_snapshot_id": day.current_snapshot_id if day else None,
        "reviewed_snapshot_id": day.reviewed_snapshot_id if day else None,
        "applied_snapshot_id": day.applied_snapshot_id if day else None,
        "snapshot_state": day.snapshot_state if day else "dirty",
        "coverage": {
            **(day.coverage if day else {}),
            "recording_intervals": recording_intervals,
            "privacy_intervals": await privacy.list_intervals(
                owner, day_start, min(day_end, datetime.now(UTC))
            ),
        },
        "analysis": _run_payload(latest) if not snapshot_unavailable else None,
        "review": _review_payload(day, proposal) if not snapshot_unavailable else None,
        "consolidation": (
            _consolidation_payload(day, episodes)
            if day and not snapshot_unavailable
            else None
        ),
        "semantic_groups": [
            _semantic_group_payload(group) for group in semantic_groups
        ],
        "review_decision_count": len(day.review_decisions) if day else 0,
        "reconciliation": (
            reconciliation if not snapshot_unavailable else {"ranges": []}
        ),
        "latest_reconciliation": request_payload if not snapshot_unavailable else None,
        "review_projection": build_day_review_projection(
            episodes,
            semantic_group_revisions=semantic_groups,
            local_date=local_date,
            timezone_name=timezone,
        ),
        "episodes": [_episode_payload(episode) for episode in episodes],
    }
    if episodes or semantic_groups:
        await privacy.guard_payload(
            owner, [*episodes, *semantic_groups], snapshot=policy
        )
    return payload


class ReconciliationDayRequest(BaseModel):
    timezone: str
    skip_immich: bool = False


@router.post("/reconciliation/day/{local_date}", status_code=202)
async def reconcile_timeline_day(
    local_date: date,
    body: ReconciliationDayRequest,
    user: User = Depends(current_active_user),
):
    timezone = _validate_timezone(body.timezone)
    try:
        request, created = await request_explicit_reconciliation(
            user=user,
            local_date=local_date,
            timezone_name=timezone,
            skip_immich=body.skip_immich,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {**reconciliation_request_payload(request), "created": created}


@router.get("/reconciliation/{request_id}")
async def get_reconciliation_request(
    request_id: str,
    user: User = Depends(current_active_user),
):
    request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request_id,
        TimelineReconciliationRequest.user_id == str(user.id),
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Reconciliation request not found")
    payload = reconciliation_request_payload(request)
    payload["progress"] = await asyncio.to_thread(read_job_progress, request.job_id)
    if request.notification_id:
        intent = await NotificationIntent.find_one(
            NotificationIntent.notification_id == request.notification_id,
            NotificationIntent.user_id == str(user.id),
        )
        if intent is not None:
            payload["notification_status"] = intent.state
    return payload


class ReviewDayRequest(BaseModel):
    timezone: str
    snapshot_id: str = Field(min_length=64, max_length=64)


class ConfirmSessionStructuresRequest(ReviewDayRequest):
    episodes: list[EpisodeRevisionRef] = Field(min_length=1, max_length=200)


class ResolveConsolidationRequest(ReviewDayRequest):
    accepted_suggestion_ids: list[str] = Field(default_factory=list)
    finalize: bool = True


class CreateSemanticGroupRequest(ReviewDayRequest):
    episode_ids: list[str] = Field(min_length=2)


class RegenerateMemoryReviewRequest(BaseModel):
    feedback: str | None = Field(default=None, min_length=1, max_length=4000)


class ResolveMemoryReviewRequest(BaseModel):
    generation: int = Field(ge=1)
    accepted_change_ids: list[str] = Field(default_factory=list)


@router.post("/review/day/{local_date}/consolidation")
async def suggest_timeline_day_consolidation(
    local_date: date,
    body: ReviewDayRequest,
    user: User = Depends(current_active_user),
):
    """Propose, but never apply, semantic merges for the active day generation."""

    timezone_name = _validate_timezone(body.timezone)
    owner = str(user.id)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == owner,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None or day.current_snapshot_id is None:
        raise HTTPException(status_code=404, detail="Timeline day not found")
    if body.snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    if day.review_state != "episodes_pending":
        raise HTTPException(
            status_code=409,
            detail="Episode grouping can only be reviewed before memory extraction",
        )
    await TimelineDay.get_pymongo_collection().update_one(
        {"_id": day.id, "current_snapshot_id": body.snapshot_id},
        {
            "$set": {
                "consolidation_state": "queued",
                "consolidation_snapshot_id": body.snapshot_id,
                "consolidation_error": None,
            }
        },
    )
    try:
        return await queue_day_consolidation(
            owner, local_date, timezone_name, body.snapshot_id
        )
    except Exception as error:
        await TimelineDay.get_pymongo_collection().update_one(
            {"_id": day.id, "current_snapshot_id": body.snapshot_id},
            {
                "$set": {
                    "consolidation_state": "failed",
                    "consolidation_error": str(error)[:1000],
                }
            },
        )
        raise HTTPException(
            status_code=503,
            detail=f"Could not queue grouping suggestions: {error}",
        ) from error


@router.post("/review/day/{local_date}/consolidation/resolve")
async def resolve_timeline_day_consolidation(
    local_date: date,
    body: ResolveConsolidationRequest,
    user: User = Depends(current_active_user),
):
    """Accept semantic overlays and retain every proposal decision for learning."""

    timezone_name = _validate_timezone(body.timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None:
        raise HTTPException(status_code=404, detail="Timeline day not found")
    if body.snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    try:
        groups = await resolve_day_consolidation(
            day, body.accepted_suggestion_ids, finalize=body.finalize
        )
    except ConsolidationResolutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ConsolidationSynthesisError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"groups": [_semantic_group_payload(group) for group in groups]}


@router.post("/review/day/{local_date}/groups")
async def create_timeline_semantic_group(
    local_date: date,
    body: CreateSemanticGroupRequest,
    user: User = Depends(current_active_user),
):
    """Manually relate episodes without widening or replacing their intervals."""

    timezone_name = _validate_timezone(body.timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None:
        raise HTTPException(status_code=404, detail="Timeline day not found")
    if body.snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    try:
        group = await create_manual_semantic_group(day, body.episode_ids)
    except ConsolidationResolutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _semantic_group_payload(group)


@router.delete("/review/day/{local_date}/groups/{group_id}", status_code=204)
async def delete_timeline_semantic_group(
    local_date: date,
    group_id: str,
    timezone: str = Query(),
    snapshot_id: str = Query(min_length=64, max_length=64),
    user: User = Depends(current_active_user),
):
    """Undo an accepted overlay without deleting its review history."""

    timezone_name = _validate_timezone(timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None:
        raise HTTPException(status_code=404, detail="Timeline day not found")
    if snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    try:
        await remove_semantic_group(day, group_id)
    except ConsolidationResolutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@router.get("/review/day/{local_date}/decisions")
async def get_timeline_review_decisions(
    local_date: date,
    timezone: str = Query(),
    user: User = Depends(current_active_user),
):
    """Return the reusable proposed -> human decision trail for one day."""

    timezone_name = _validate_timezone(timezone)
    owner = str(user.id)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == owner,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None:
        raise HTTPException(status_code=404, detail="Timeline day not found")
    proposals = (
        await MemoryReviewProposal.find(
            MemoryReviewProposal.user_id == owner,
            MemoryReviewProposal.local_date == local_date,
            MemoryReviewProposal.timezone == timezone_name,
        )
        .sort("created_at")
        .to_list()
    )

    proposals = await privacy.filter_payloads(proposals, owner)
    decisions = await privacy.filter_payloads(day.review_decisions, owner)
    return {
        "date": local_date,
        "timezone": timezone_name,
        "timeline_decisions": [item.model_dump(mode="json") for item in decisions],
        "memory_proposals": [
            _proposal_payload(proposal, full=True) for proposal in proposals
        ],
    }


@router.get("/review/queue")
async def timeline_memory_review_queue(
    timezone: str, user: User = Depends(current_active_user)
):
    timezone = _validate_timezone(timezone)
    days = (
        await TimelineDay.find(
            TimelineDay.user_id == str(user.id),
            TimelineDay.timezone == timezone,
            {"current_snapshot_id": {"$nin": [None, ""]}},
        )
        .sort("-local_date")
        .to_list()
    )
    proposals = await MemoryReviewProposal.find(
        MemoryReviewProposal.user_id == str(user.id),
        MemoryReviewProposal.timezone == timezone,
    ).to_list()
    items = []
    for day in days:
        tokens = (
            {
                f"{r.episode_key}:{r.revision}"
                for r in day.current_snapshot.episode_revisions
            }
            if day.current_snapshot
            else set()
        )
        rows = [p for p in proposals if tokens.intersection(p.selected_tokens)]
        outcomes = episode_review_outcomes(rows)
        progress = {
            "total": len(tokens),
            "undecided": len(tokens - set(outcomes)),
            "pending": sum(p.active for p in rows),
            "accepted": sum(
                o["state"] == "accepted" for k, o in outcomes.items() if k in tokens
            ),
            "partial": sum(
                o["state"] == "partial" for k, o in outcomes.items() if k in tokens
            ),
            "excluded": sum(
                o["state"] == "excluded" for k, o in outcomes.items() if k in tokens
            ),
        }
        items.append(
            {
                "date": day.local_date,
                "state": "episodes_pending",
                "outcome": None,
                "episode_count": (
                    len(day.current_snapshot.episode_revisions)
                    if day.current_snapshot
                    else 0
                ),
                "unexplained_count": 0,
                "capture_gap_count": 0,
                "proposal": None,
                "pending_count": sum(p.active for p in rows),
                "outcomes": outcomes,
                "progress": progress,
            }
        )
    return {"items": items}


class SessionMemoryRequest(BaseModel):
    timezone: str
    session_key: str
    revision: int = Field(ge=1)
    excluded_source_keys: list[str] = Field(default_factory=list)


class SessionDispositionRequest(SessionMemoryRequest):
    action: Literal["exclude", "defer", "resume", "include", "attribute", "clarify"]
    clarification: str | None = Field(default=None, max_length=2000)
    role: Literal["user_statement", "third_party", "media_content"] | None = None
    source_keys: list[str] = Field(min_length=1)
    scope_hash: str


@router.get("/sessions/{local_date}")
async def get_timeline_sessions(
    local_date: date, timezone: str, user: User = Depends(current_active_user)
):
    try:
        day = await session_memory.get_day(
            str(user.id), local_date, _validate_timezone(timezone)
        )
        episodes = await snapshot_episodes(day)

        preparation = await session_memory_module.SessionPreparation.find_one(
            {
                "user_id": str(user.id),
                "local_date": local_date,
                "timezone": timezone,
                "$or": [
                    {"snapshot_id": day.current_snapshot_id},
                    {"result_snapshot_id": day.current_snapshot_id},
                ],
            },
            sort=[("updated_at", -1)],
        )
        sessions = await session_memory.project_sessions(
            day, episodes, include_sources=False
        )
        if preparation:
            for session in sessions:
                reason = preparation.waiting_sessions.get(session["session_key"])
                if reason and session["state"] == "available":
                    session.update(state="waiting", waiting_reason=reason)
        return {
            "sessions": sessions,
            "preparation": (
                {
                    "state": preparation.state,
                    "attempts": preparation.attempts,
                    "job_id": preparation.job_id,
                    "error": preparation.error,
                    "waiting_sessions": preparation.waiting_sessions,
                    "completed_queries": len(preparation.inference_artifacts),
                }
                if preparation
                else None
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{local_date}/prepare", status_code=202)
async def prepare_timeline_sessions(
    local_date: date, body: ReviewDayRequest, user: User = Depends(current_active_user)
):
    try:
        day = await session_memory.get_day(
            str(user.id), local_date, _validate_timezone(body.timezone)
        )
        if day.current_snapshot_id != body.snapshot_id:
            raise ValueError("Timeline changed; refresh before preparing sessions")
        item = await session_memory.request_preparation(day, priority=100, force=True)
        return {"state": item.state, "job_id": item.job_id}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{local_date}/organization-exchanges")
async def get_session_organization_exchanges(
    local_date: date, timezone: str, user: User = Depends(current_active_user)
):

    timezone = _validate_timezone(timezone)
    item = await session_memory_module.SessionPreparation.find_one(
        {"user_id": str(user.id), "local_date": local_date, "timezone": timezone},
        sort=[("updated_at", -1)],
    )
    if item is None:
        raise HTTPException(
            status_code=404, detail="No session organization run exists for this date"
        )

    policy = await privacy.load_snapshot(str(user.id))
    payload = {
        "runs": [
            await asyncio.to_thread(
                inference_artifacts.read_inference_artifact,
                "session-organization-v1",
                key,
            )
            for key in item.inference_artifacts
        ]
    }
    await privacy.guard_payload(str(user.id), payload, snapshot=policy)
    await privacy.assert_current(str(user.id), policy)
    return payload


@router.get("/sessions/{local_date}/{session_key}/sources")
async def get_session_sources(
    local_date: date,
    session_key: str,
    timezone: str,
    revision: int,
    user: User = Depends(current_active_user),
):
    try:
        _, _, members = await session_memory.choose_session(
            str(user.id),
            local_date,
            _validate_timezone(timezone),
            session_key,
            revision,
        )

        sources = memory_sources.evidence_sources(members)
        sources = memory_sources.apply_dispositions(
            sources, await memory_sources.source_decisions(str(user.id), sources)
        )
        return {"sources": sources, "scope_hash": memory_sources.scope_hash(sources)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{local_date}/memory", status_code=202)
async def generate_timeline_session_memory(
    local_date: date,
    body: SessionMemoryRequest,
    user: User = Depends(current_active_user),
):
    try:
        rows = await session_memory.request_session_memory(
            str(user.id),
            local_date,
            _validate_timezone(body.timezone),
            body.session_key,
            body.revision,
            excluded_keys=body.excluded_source_keys,
            priority=100,
        )
        return {"proposals": [_proposal_payload(p, full=True) for p in rows]}
    except (ValueError, MemoryReviewError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{local_date}/disposition")
async def decide_timeline_session_memory(
    local_date: date,
    body: SessionDispositionRequest,
    user: User = Depends(current_active_user),
):
    try:
        return await session_memory.decide_session(
            str(user.id),
            local_date,
            _validate_timezone(body.timezone),
            body.session_key,
            body.revision,
            body.action,
            body.source_keys,
            body.scope_hash,
            body.role,
            body.clarification,
        )
    except session_memory.LockUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Timeline is updating. Your decision has not been saved yet; please try again shortly.",
            headers={"Retry-After": "2"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class CreateMemorySelectionRequest(ReviewDayRequest):
    episodes: list[EpisodeRevisionRef] = Field(min_length=1, max_length=200)


@router.get("/review/day/{local_date}/selections")
async def list_timeline_memory_selections(
    local_date: date, timezone: str, user: User = Depends(current_active_user)
):
    timezone = _validate_timezone(timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone,
    )
    tokens = (
        [
            f"{r.episode_key}:{r.revision}"
            for r in day.current_snapshot.episode_revisions
        ]
        if day and day.current_snapshot
        else []
    )
    rows = (
        await MemoryReviewProposal.find(
            {
                "user_id": str(user.id),
                "timezone": timezone,
                "$or": [
                    {"local_date": datetime.combine(local_date, datetime.min.time())},
                    {"selected_tokens": {"$in": tokens}},
                ],
            }
        )
        .sort("created_at")
        .to_list()
    )

    rows = await privacy.filter_payloads(rows, str(user.id))
    return {
        "proposals": [_proposal_payload(p, full=True) for p in rows],
        "outcomes": episode_review_outcomes(rows),
    }


async def _create_selection(local_date, body, background_tasks, user, *, exclude=False):
    timezone = _validate_timezone(body.timezone)
    try:
        proposals = await create_memory_selection(
            str(user.id),
            local_date,
            timezone,
            body.snapshot_id,
            body.episodes,
            exclude=exclude,
        )
    except MemoryReviewError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not exclude:
        background_tasks.add_task(process_memory_review_queue)
    return {"proposals": [_proposal_payload(p, full=True) for p in proposals]}


@router.post("/review/day/{local_date}/selections", status_code=202)
async def create_timeline_memory_selection(
    local_date: date,
    body: CreateMemorySelectionRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    return await _create_selection(local_date, body, background_tasks, user)


@router.post("/review/day/{local_date}/exclusions")
async def exclude_timeline_memory_selection(
    local_date: date,
    body: CreateMemorySelectionRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    return await _create_selection(
        local_date, body, background_tasks, user, exclude=True
    )


@router.post("/reconciliation/ranges/{dirty_range_id}/dismiss")
async def dismiss_terminal_reconciliation_range(
    dirty_range_id: str,
    body: DismissFailedRangeRequest,
    user: User = Depends(current_active_user),
):
    """Record the owner's decision to stop blocking on one terminal failure."""

    try:
        dismissed = await dismiss_failed_range(
            dirty_range_id,
            user_id=str(user.id),
            reason=body.reason,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DirtyRangeDismissalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _dirty_range_payload(dismissed)


@router.post("/review/day/{local_date}/episodes")
async def finalize_timeline_episode_review(
    local_date: date,
    body: ReviewDayRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    """Finalize whole-day structure only; memory selections are explicit requests."""

    timezone = _validate_timezone(body.timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone,
    )
    if day is None or day.current_snapshot is None:
        raise HTTPException(status_code=404, detail="No analysed Timeline day")
    if body.snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    if day.snapshot_state not in {
        "ready",
        "reviewed",
        "applied",
        "correction_required",
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Snapshot is not ready for review ({day.snapshot_state})",
        )
    reconciliation = await _reconciliation_payload(str(user.id), local_date, timezone)
    if reconciliation["ranges"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(reconciliation['ranges'])} evidence range(s) still need "
                "reconciliation before this episode account can be finalized"
            ),
        )
    if day.review_state not in ("episodes_pending", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Day is already in review state {day.review_state}",
        )
    episodes = await snapshot_episodes(day)
    consolidation = _consolidation_payload(day, episodes)
    if consolidation["state"] in {"queued", "generating"} or (
        consolidation["state"] == "ready" and consolidation["suggestions"]
    ):
        raise HTTPException(
            status_code=409,
            detail="Resolve or dismiss every grouping proposal before finalizing",
        )
    unstable = [
        item.episode_key
        for item in episodes
        if episode_requires_activity_review(item)
        and not episode_structure_is_stable(item)
    ]
    if unstable:
        raise HTTPException(
            status_code=409,
            detail=("Snapshot contains provisional structure: " + ", ".join(unstable)),
        )
    reviewed_at = utcnow()
    updated = await TimelineDay.get_pymongo_collection().update_one(
        {
            "_id": day.id,
            "current_snapshot_id": body.snapshot_id,
            "snapshot_state": {"$in": ["ready", "correction_required"]},
            "review_state": {"$in": ["episodes_pending", "failed"]},
        },
        {
            "$set": {
                "snapshot_state": "reviewed",
                "reviewed_snapshot_id": body.snapshot_id,
                "review_state": "episodes_pending",
                "review_snapshot_id": body.snapshot_id,
                "episodes_reviewed_at": reviewed_at,
                "review_error": None,
                "review_outcome": None,
                "revised_at": reviewed_at,
            }
        },
    )
    if updated.modified_count != 1:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    day = await TimelineDay.get(day.id)
    return _review_payload(day, await _day_proposal(day))


@router.post(
    "/review/day/{local_date}/episodes/{episode_key}/revisions/{revision}/confirm-structure"
)
async def confirm_timeline_episode_structure(
    local_date: date,
    episode_key: str,
    revision: int,
    body: ReviewDayRequest,
    user: User = Depends(current_active_user),
):
    result = await _confirm_episode_structures(
        local_date,
        [EpisodeRevisionRef(episode_key=episode_key, revision=revision)],
        body,
        user,
    )
    return result["episodes"][0]


@router.post("/review/day/{local_date}/confirm-session-structures")
async def confirm_timeline_session_structures(
    local_date: date,
    body: ConfirmSessionStructuresRequest,
    user: User = Depends(current_active_user),
):
    return await _confirm_episode_structures(local_date, body.episodes, body, user)


async def _confirm_episode_structures(local_date, refs, body, user):
    """Human-confirm one exact episode structure as an immutable successor revision."""

    timezone = _validate_timezone(body.timezone)
    day = await TimelineDay.find_one(
        TimelineDay.user_id == str(user.id),
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone,
    )
    if day is None or day.current_snapshot is None:
        raise HTTPException(status_code=404, detail="No analysed Timeline day")
    if body.snapshot_id != day.current_snapshot_id:
        raise HTTPException(status_code=409, detail="Timeline snapshot changed")
    if day.snapshot_state not in {
        "ready",
        "reviewed",
        "applied",
        "correction_required",
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Snapshot is not ready for review ({day.snapshot_state})",
        )
    if day.review_state not in {"episodes_pending", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Day is already in review state {day.review_state}",
        )

    if len({(ref.episode_key, ref.revision) for ref in refs}) != len(refs) or len(
        {ref.episode_key for ref in refs}
    ) != len(refs):
        raise HTTPException(
            status_code=422, detail="Each episode must be selected once"
        )
    predecessors = []
    successors = []
    for ref in refs:
        exact_ref = (ref.episode_key, ref.revision)
        snapshot_refs = {
            (item.episode_key, item.revision)
            for item in day.current_snapshot.episode_revisions
        }
        if exact_ref not in snapshot_refs:
            raise HTTPException(
                status_code=409,
                detail="Episode revision is no longer in this Timeline snapshot",
            )
        episode = await TimelineEpisode.find_one(
            TimelineEpisode.user_id == str(user.id),
            TimelineEpisode.episode_key == ref.episode_key,
            TimelineEpisode.revision == ref.revision,
        )
        if episode is None:
            raise HTTPException(
                status_code=409,
                detail="Timeline snapshot references an unavailable episode revision",
            )
        if episode.status not in {"open", "provisional"}:
            raise HTTPException(
                status_code=409,
                detail=f"Episode structure cannot be confirmed in state {episode.status}",
            )
        if not episode_requires_activity_review(episode):
            raise HTTPException(
                status_code=422,
                detail="Recording coverage or media content alone is not an activity to confirm",
            )
        if episode_structure_is_stable(episode):
            raise HTTPException(
                status_code=409,
                detail="Episode structure is already stable",
            )

        successor = episode.model_copy(deep=True)
        successor.id = None
        successor.episode_id = str(uuid.uuid4())
        successor.revision = episode.revision + 1
        _confirm(successor, STRUCTURAL_CONFIRMATION_FIELDS)
        predecessors.append(episode)
        successors.append(successor)
    try:
        await publish_manual_episode_change(
            day=day,
            predecessors=predecessors,
            successors=successors,
            action="episode_structure_confirm",
            before={
                "episodes": [_episode_review_snapshot(item) for item in predecessors]
            },
            after={"episodes": [_episode_review_snapshot(item) for item in successors]},
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    for successor in successors:
        await enqueue_episode_detailed_summary(successor)
    return {"episodes": [_episode_payload(item) for item in successors]}


@router.post("/review/proposals/{proposal_id}/resolve")
async def resolve_timeline_memory_review(
    proposal_id: str,
    body: ResolveMemoryReviewRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    proposal = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == proposal_id,
        MemoryReviewProposal.user_id == str(user.id),
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    if body.generation != proposal.generation:
        raise HTTPException(status_code=409, detail="Proposal generation changed")
    try:
        outcome = await resolve_memory_review(proposal, body.accepted_change_ids)
    except MemoryReviewError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    background_tasks.add_task(process_memory_review_decision, proposal)
    return {"outcome": outcome, "proposal": _proposal_payload(proposal, full=True)}


@router.post("/review/proposals/{proposal_id}/regenerate")
async def regenerate_timeline_memory_review(
    proposal_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
    body: RegenerateMemoryReviewRequest | None = None,
):
    """Discard a stale diff and derive it again from the current accepted vault."""

    proposal = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == proposal_id,
        MemoryReviewProposal.user_id == str(user.id),
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    try:
        replacement = await queue_memory_review_regeneration(
            proposal, feedback=body.feedback if body else None
        )
    except MemoryReviewError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session_memory.enqueue_memory(replacement)
    return {
        "outcome": "regenerating",
        "proposal": _proposal_payload(replacement, full=True),
    }


@router.post("/review/proposals/{proposal_id}/correct", status_code=202)
async def correct_timeline_memory_review(
    proposal_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_active_user),
):
    proposal = await MemoryReviewProposal.find_one(
        MemoryReviewProposal.proposal_id == proposal_id,
        MemoryReviewProposal.user_id == str(user.id),
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    try:
        correction = await request_memory_correction(proposal)
    except MemoryReviewError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    background_tasks.add_task(process_memory_review_queue)
    return {"proposal": _proposal_payload(correction, full=True)}


@router.get("/evidence-preview")
async def preview_evidence_relations(
    started_at: datetime,
    ended_at: datetime,
    timezone: str,
    user: User = Depends(current_active_user),
) -> EvidenceRelationPreview:
    """Inspect shadow cross-source candidates without mutating Timeline state.

    This endpoint intentionally exposes only bounded relation diagnostics. It does not
    enqueue reconciliation, merge Conversations, publish Episodes, or include full
    transcript text in its response.
    """

    started_at, ended_at = _at(started_at), _at(ended_at)
    if ended_at <= started_at:
        raise HTTPException(status_code=422, detail="Range must have positive duration")
    if ended_at - started_at > MANUAL_RECONCILE_MAX_RANGE:
        raise HTTPException(
            status_code=422,
            detail=(
                "Range must be at most "
                f"{int(MANUAL_RECONCILE_MAX_RANGE.total_seconds() // 3600)} hours"
            ),
        )
    bundle = await load_reconciliation_evidence(
        str(user.id),
        started_at,
        ended_at,
        timezone_name=_validate_timezone(timezone),
    )
    # A dense multi-source range can require thousands of lexical comparisons. The
    # preview is read-only but must not monopolize FastAPI's event loop.
    return await asyncio.to_thread(infer_evidence_relations, bundle.manifest)


@router.get("/analysis/{run_id}")
async def get_timeline_analysis(run_id: str, user: User = Depends(current_active_user)):
    run = await TimelineAnalysisRun.find_one(
        TimelineAnalysisRun.run_id == run_id,
        TimelineAnalysisRun.user_id == str(user.id),
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Timeline analysis not found")
    return _run_payload(run)


async def _owned_episode(episode_id: str, user: User) -> TimelineEpisode:
    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == str(user.id),
    )
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    try:
        await privacy.require_record(episode)
    except privacy.PrivacyHeld as exc:
        raise HTTPException(423, str(exc)) from exc
    return episode


async def _episode_audio_recordings(episode: TimelineEpisode) -> list[str]:
    """Live recordings whose audio overlaps the episode, in wall-clock order.

    Not the same question as which recordings the episode *cites*: the agent cites
    the evidence it reasoned over, so a 37-minute call can be represented by the one
    recording that carried its most quotable stretch. Playing the episode needs
    whatever audio covers the span, so this is derived from the interval instead.
    """
    # Compatibility/display metadata only. Playback truth is ``audio_ranges``; map
    # their current owners for the existing recording-detail links.
    chunk_ids = [
        chunk_id for item in episode.audio_ranges for chunk_id in item.chunk_ids
    ]
    if not chunk_ids:
        return []
    recordings = (
        await Conversation.find(
            Conversation.user_id == episode.user_id,
            Conversation.deleted == False,  # noqa: E712 - Beanie expression
            {"audio_ranges.chunk_ids": {"$in": chunk_ids}},
        )
        .sort("+started_at")
        .to_list()
    )
    return [item.conversation_id for item in recordings]


async def _episode_playback_ranges(episode: TimelineEpisode) -> list[dict]:
    """Resolve immutable episode ranges onto chunks' current playback coordinates."""
    chunk_ids = [
        chunk_id
        for audio_range in episode.audio_ranges
        for chunk_id in audio_range.chunk_ids
    ]
    if not chunk_ids:
        return []
    conversations = await Conversation.find(
        Conversation.user_id == episode.user_id,
        Conversation.deleted == False,  # noqa: E712 - Beanie expression
        {"audio_ranges.chunk_ids": {"$in": chunk_ids}},
    ).to_list()
    playback: list[dict] = []
    for conversation in conversations:
        claimed = await resolve_audio_ranges(conversation.audio_ranges)
        current: dict | None = None
        for item in claimed:
            captured = _at(item.chunk.captured_at)
            item_start = captured + timedelta(seconds=item.clip_start_seconds)
            item_end = captured + timedelta(seconds=item.clip_end_seconds)
            matching_ranges = [
                audio_range
                for audio_range in episode.audio_ranges
                if str(item.chunk.id) in audio_range.chunk_ids
                and _at(audio_range.started_at) < item_end
                and _at(audio_range.ended_at) > item_start
            ]
            for audio_range in matching_ranges:
                absolute_start = max(_at(audio_range.started_at), item_start)
                absolute_end = min(_at(audio_range.ended_at), item_end)
                relative_start = (
                    item.conversation_start_seconds
                    + (absolute_start - item_start).total_seconds()
                )
                relative_end = (
                    item.conversation_start_seconds
                    + (absolute_end - item_start).total_seconds()
                )
                if (
                    current is not None
                    and current["range_id"] == audio_range.range_id
                    and abs(current["end"] - relative_start) <= 0.25
                ):
                    current["end"] = relative_end
                    current["ended_at"] = absolute_end
                else:
                    current = {
                        "range_id": audio_range.range_id,
                        "conversation_id": conversation.conversation_id,
                        "start": relative_start,
                        "end": relative_end,
                        "started_at": absolute_start,
                        "ended_at": absolute_end,
                    }
                    playback.append(current)
    playback.sort(key=lambda item: item["started_at"])
    return playback


@router.get("/episodes/{episode_id}")
async def get_timeline_episode(
    episode_id: str, user: User = Depends(current_active_user)
):
    episode = await _owned_episode(episode_id, user)
    payload = _episode_payload(episode)
    payload["audio_recording_ids"] = await _episode_audio_recordings(episode)
    payload["audio_playback_ranges"] = await _episode_playback_ranges(episode)
    return payload


def _lineage_payload(episode: TimelineEpisode) -> dict:
    """The episode payload plus the fields a stable-key navigation needs."""

    payload = _episode_payload(episode)
    payload.update(
        {
            "episode_key": episode.episode_key,
            "revision": episode.revision,
            "status": episode.status,
            "predecessor_keys": episode.predecessor_keys,
            "predecessor_revisions": [
                item.model_dump(mode="json") for item in episode.predecessor_revisions
            ],
            "successor_keys": episode.successor_keys,
        }
    )
    return payload


async def _readable(episode: TimelineEpisode) -> bool:
    """Whether the exact revision belongs to a current canonical snapshot."""

    policy = await privacy.load_snapshot(episode.user_id)
    if not policy.permits_record(episode):
        return False

    day = await TimelineDay.find_one(
        TimelineDay.user_id == episode.user_id,
        TimelineDay.local_date == episode.local_date,
        TimelineDay.timezone == episode.timezone,
    )
    return bool(
        day
        and day.current_snapshot
        and (episode.episode_key, episode.revision)
        in {
            (item.episode_key, item.revision)
            for item in day.current_snapshot.episode_revisions
        }
    )


@router.get("/key/{episode_key}")
async def get_timeline_episode_by_key(
    episode_key: str, user: User = Depends(current_active_user)
):
    """Resolve a durable episode key to the claim that currently covers it.

    A URL, a vault link, or a person's bookmark names an episode by ``episode_key``,
    which survives reanalysis, editing, and revision. Splitting and merging can leave
    that key with no active row of its own; the answer is then the successor it was
    replaced by, or — when a split produced several — the choice between them. Only a
    key that never existed is a 404: a key whose lineage ended is still history the
    user is entitled to an answer about.
    """

    rows = await TimelineEpisode.find(
        TimelineEpisode.episode_key == episode_key,
        TimelineEpisode.user_id == str(user.id),
    ).to_list()
    if not rows:
        raise HTTPException(status_code=404, detail="Episode key not found")

    active = [
        row for row in rows if row.status != "superseded" and await _readable(row)
    ]
    if active:
        active.sort(key=lambda row: (row.revision, _at(row.revised_at)))
        payload = _lineage_payload(active[-1])
        payload["resolved"] = True
        return payload

    successors: list[str] = []
    for row in rows:
        for key in row.successor_keys:
            if key not in successors and key != episode_key:
                successors.append(key)
    return {
        "resolved": False,
        "episode_key": episode_key,
        "successor_keys": successors,
    }


class EpisodeUpdate(BaseModel):
    """Human corrections to one episode.

    Any field supplied here becomes human-owned and later analysis carries that field
    forward without changing the episode revision's lifecycle settlement state.
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    summary: Optional[str] = Field(default=None, max_length=1200)
    kind: Optional[str] = Field(default=None, min_length=1, max_length=80)
    memory_policy: Optional[Literal["auto", "reference", "remember"]] = None
    entities: Optional[list[str]] = None
    salience: Optional[Literal["background", "routine", "notable", "highlight"]] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


@router.patch("/episodes/{episode_id}")
async def update_timeline_episode(
    episode_id: str,
    body: EpisodeUpdate,
    user: User = Depends(current_active_user),
):
    episode = await _owned_episode(episode_id, user)
    before = _episode_review_snapshot(episode)
    try:
        day = await day_for_exact_episode(episode)
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No episode fields supplied")
    successor = episode.model_copy(deep=True)
    successor.id = None
    successor.episode_id = str(uuid.uuid4())
    successor.revision = episode.revision + 1
    for field, value in changes.items():
        setattr(successor, field, value)
    if _at(successor.ended_at) <= _at(successor.started_at):
        raise HTTPException(
            status_code=422, detail="Episode must have positive duration"
        )
    if {"started_at", "ended_at"} & set(changes):
        successor.detailed_summary = None
        successor.detailed_summary_scope_hash = None
        successor.detailed_summary_revision = None
        successor.detailed_summary_generated_at = None
    _confirm(successor, changes.keys())
    try:
        await publish_manual_episode_change(
            day=day,
            predecessors=[episode],
            successors=[successor],
            action="episode_update",
            before=before,
            after=_episode_review_snapshot(successor),
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _episode_payload(successor)


def _confirm(episode: TimelineEpisode, fields) -> None:
    """Pin only the human-owned fields without changing lifecycle settlement."""

    episode.pinned = True
    episode.confirmed_at = utcnow()
    episode.confirmed_fields = sorted(set(episode.confirmed_fields) | set(fields))
    episode.revised_at = utcnow()


def _episode_review_snapshot(episode: TimelineEpisode) -> dict[str, Any]:
    """Bounded semantic state used by the prompt-learning ledger."""

    return {
        "episode_id": episode.episode_id,
        "episode_key": episode.episode_key,
        "revision": episode.revision,
        "started_at": _utc(episode.started_at).isoformat(),
        "ended_at": _utc(episode.ended_at).isoformat(),
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "memory_policy": episode.memory_policy,
        "salience": episode.salience,
        "entities": episode.entities,
        "evidence_refs": [
            item.model_dump(mode="json") for item in episode.evidence_refs
        ],
        "confirmed_fields": episode.confirmed_fields,
    }


async def _chunk_spans(episode: TimelineEpisode) -> dict:
    """``chunk_id -> (captured_at, duration)`` for everything the episode claims.

    Read once per operation so ``clip_audio_ranges`` stays pure. A chunk missing from
    the result (deleted, or never anchored with ``captured_at``) is deliberately absent
    rather than defaulted: the caller decides what an unplaceable chunk means.
    """

    chunk_ids = [
        chunk_id for item in episode.audio_ranges for chunk_id in item.chunk_ids
    ]
    if not chunk_ids:
        return {}
    chunks = await AudioChunkDocument.find(
        {"_id": {"$in": [ObjectId(item) for item in chunk_ids]}}
    ).to_list()
    return {
        str(chunk.id): (chunk.captured_at, chunk.duration)
        for chunk in chunks
        if chunk.captured_at is not None
    }


def _refs_overlapping(episode: TimelineEpisode, start: datetime, end: datetime) -> list:
    """Evidence refs touching [start, end). A ref spanning the cut belongs to both sides."""

    kept = []
    for ref in episode.evidence_refs:
        ref_start = _at(ref.started_at)
        ref_end = _at(ref.ended_at) if ref.ended_at else ref_start
        if ref_start < end and ref_end >= start:
            kept.append(ref)
    return kept


class EpisodeSplit(BaseModel):
    at: datetime


@router.post("/episodes/{episode_id}/split")
async def split_timeline_episode(
    episode_id: str,
    body: EpisodeSplit,
    user: User = Depends(current_active_user),
):
    """Cut one episode into two immutable, structurally pinned revisions.

    Evidence is repartitioned by overlap rather than copied wholesale, so each half
    only cites what it actually covers. Assertions are filtered to the evidence that
    survives on their side; the model rejects an assertion citing absent evidence.
    """

    episode = await _owned_episode(episode_id, user)
    before = _episode_review_snapshot(episode)
    try:
        day = await day_for_exact_episode(episode)
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    at = _at(body.at)
    if not (_at(episode.started_at) < at < _at(episode.ended_at)):
        raise HTTPException(
            status_code=422, detail="Split point must fall strictly inside the episode"
        )

    spans = await _chunk_spans(episode)
    episode_start, episode_end = _at(episode.started_at), _at(episode.ended_at)

    head = episode.model_copy(deep=True)
    head.id = None
    head.episode_id = str(uuid.uuid4())
    tail = episode.model_copy(deep=True)
    tail.id = None
    tail.episode_id = str(uuid.uuid4())
    predecessor_ref = EpisodeRevisionRef(
        episode_key=episode.episode_key,
        revision=episode.revision,
    )
    # A split retires the original identity. Both halves are new event claims with
    # exact direct lineage to the immutable revision that was split.
    head.episode_key = str(uuid.uuid4())
    head.revision = 1
    tail.episode_key = str(uuid.uuid4())
    tail.revision = 1
    for part in (head, tail):
        part.predecessor_keys = [episode.episode_key]
        part.predecessor_revisions = [predecessor_ref]
        part.successor_keys = []
    tail.started_at = at
    tail.evidence_refs = _refs_overlapping(episode, at, episode_end)
    # The audio claim is cut with the episode. The deep copy above handed the tail
    # every original range, so without this both halves claim — and play — the whole
    # original recording. Chunks that cannot be placed stay with the head.
    tail.audio_ranges = clip_audio_ranges(
        episode.audio_ranges, at, episode_end, spans, keep_unplaceable=False
    )
    head_ranges = clip_audio_ranges(
        episode.audio_ranges, episode_start, at, spans, keep_unplaceable=True
    )
    head.ended_at = at
    head.evidence_refs = _refs_overlapping(episode, episode_start, at)
    head.audio_ranges = head_ranges

    for part in (head, tail):
        known = {ref.evidence_id for ref in part.evidence_refs}
        part.assertions = [
            assertion
            for assertion in part.assertions
            if set(assertion.evidence_ids) <= known
        ]
        _confirm(part, ["started_at", "ended_at"])
        part.detailed_summary = None
        part.detailed_summary_scope_hash = None
        part.detailed_summary_revision = None
        part.detailed_summary_generated_at = None

    try:
        await publish_manual_episode_change(
            day=day,
            predecessors=[episode],
            successors=[head, tail],
            action="episode_split",
            before=before,
            after={
                "episodes": [
                    _episode_review_snapshot(head),
                    _episode_review_snapshot(tail),
                ]
            },
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "episodes": [_episode_payload(head), _episode_payload(tail)],
    }


class EpisodeMerge(BaseModel):
    episode_ids: list[str] = Field(min_length=2)


@router.post("/episodes/merge")
async def merge_timeline_episodes(
    body: EpisodeMerge, user: User = Depends(current_active_user)
):
    """Collapse several episodes into one spanning their full extent.

    Every input is superseded by one fresh durable identity. Evidence, entities, and
    assertions are unioned, with exact input revisions retained as direct lineage.
    """

    episodes = [
        await _owned_episode(episode_id, user) for episode_id in body.episode_ids
    ]
    try:
        days = [await day_for_exact_episode(episode) for episode in episodes]
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if len({day.current_snapshot_id for day in days}) != 1:
        raise HTTPException(status_code=409, detail="Episodes must share one snapshot")
    day = days[0]
    episodes.sort(key=lambda episode: _at(episode.started_at))
    before = {"episodes": [_episode_review_snapshot(item) for item in episodes]}
    survivor, absorbed = episodes[0], episodes[1:]
    # A widened semantic event needs a widened account. Generate it before mutating
    # any rows so an unavailable or malformed model response leaves the day intact.
    account = await synthesize_merged_episode_account(episodes)

    merged = survivor.model_copy(deep=True)
    merged.id = None
    merged.episode_id = str(uuid.uuid4())
    merged.episode_key = str(uuid.uuid4())
    merged.revision = 1
    merged.ended_at = max(_at(episode.ended_at) for episode in episodes)
    merged.title = account.title
    merged.summary = account.summary
    merged.detailed_summary = None
    merged.detailed_summary_scope_hash = None
    merged.detailed_summary_revision = None
    merged.detailed_summary_generated_at = None
    # The survivor now spans every absorbed episode, so it has to claim their audio
    # too. Without this it kept only its own ranges and the rest were deleted with
    # their documents, leaving a merged episode able to play a fraction of itself.
    merged.audio_ranges = merge_audio_ranges(
        episode.audio_ranges for episode in episodes
    )
    seen = {ref.evidence_id for ref in merged.evidence_refs}
    for episode in absorbed:
        for ref in episode.evidence_refs:
            if ref.evidence_id not in seen:
                seen.add(ref.evidence_id)
                merged.evidence_refs.append(ref)
        merged.entities = sorted(set(merged.entities) | set(episode.entities))
        merged.assertions.extend(
            assertion
            for assertion in episode.assertions
            if set(assertion.evidence_ids) <= seen
        )
        merged.related_conversation_ids = sorted(
            set(merged.related_conversation_ids) | set(episode.related_conversation_ids)
        )
    merged.predecessor_keys = sorted(episode.episode_key for episode in episodes)
    merged.predecessor_revisions = [
        EpisodeRevisionRef(
            episode_key=episode.episode_key,
            revision=episode.revision,
        )
        for episode in episodes
    ]
    merged.successor_keys = []
    _confirm(merged, ["started_at", "ended_at", "title", "summary", "entities"])

    try:
        await publish_manual_episode_change(
            day=day,
            predecessors=episodes,
            successors=[merged],
            action="episode_merge",
            before=before,
            after=_episode_review_snapshot(merged),
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await enqueue_episode_detailed_summary(merged)
    return _episode_payload(merged)


@router.post("/episodes/{episode_id}/regenerate-account")
async def regenerate_timeline_episode_account(
    episode_id: str, user: User = Depends(current_active_user)
):
    """Regenerate title and summary from an episode's complete semantic evidence.

    This repairs episodes merged before merge-time synthesis existed and remains a
    safe retry seam: only the derived account changes; bounds, evidence and audio do
    not. The inference finishes before either field is written.
    """

    collection = TimelineEpisode.get_pymongo_collection()
    query = {"episode_id": episode_id, "user_id": str(user.id)}
    try:
        episode = await _owned_episode(episode_id, user)
    except ValidationError:
        # A pre-fix merge could persist an overlong generated summary before Beanie
        # rejected its returned document. Construct only enough trusted shape to run
        # synthesis; the corrected result below is validated before being returned.
        raw = await collection.find_one(query)
        if raw is None:
            raise HTTPException(status_code=404, detail="Episode not found")
        episode = TimelineEpisode.model_construct(**raw)
    account = await synthesize_merged_episode_account([episode], force=True)
    try:
        day = await day_for_exact_episode(episode)
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    successor = episode.model_copy(deep=True)
    successor.id = None
    successor.episode_id = str(uuid.uuid4())
    successor.revision = episode.revision + 1
    successor.title = account.title
    successor.summary = account.summary
    _confirm(successor, ["title", "summary"])
    try:
        await publish_manual_episode_change(
            day=day,
            predecessors=[episode],
            successors=[successor],
            action="episode_update",
            before=_episode_review_snapshot(episode),
            after=_episode_review_snapshot(successor),
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _episode_payload(successor)


class NotActivityRequest(BaseModel):
    local_date: date
    timezone: str
    snapshot_id: str
    revision: int = Field(ge=0)


@router.post("/episodes/{episode_id}/not-activity", status_code=204)
async def reject_timeline_activity(
    episode_id: str, body: NotActivityRequest, user: User = Depends(current_active_user)
):
    """Reject this episode interpretation, retaining capture and an evidence-scoped decision."""
    episode = await _owned_episode(episode_id, user)
    try:
        day = await TimelineDay.find_one(
            TimelineDay.user_id == str(user.id),
            TimelineDay.local_date == body.local_date,
            TimelineDay.timezone == _validate_timezone(body.timezone),
        )
        if day is None:
            raise HTTPException(status_code=404, detail="Timeline day not found")
        if (
            episode.revision != body.revision
            or day.current_snapshot_id != body.snapshot_id
        ):
            raise HTTPException(
                status_code=409, detail="Episode or Timeline snapshot changed"
            )
        await publish_manual_episode_change(
            day=day,
            predecessors=[episode],
            successors=[],
            action="episode_not_activity",
            before=_episode_review_snapshot(episode),
            after={
                "rejected_activity": rejection_basis(episode),
                "raw_recordings_preserved": True,
            },
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@router.delete("/episodes/{episode_id}", status_code=204)
async def delete_timeline_episode(
    episode_id: str, user: User = Depends(current_active_user)
):
    """Remove an episode from this generation.

    The deletion is not a negative label: nothing records that the interval should stay
    unexplained, so a later analysis run may propose an episode there again.
    """

    episode = await _owned_episode(episode_id, user)
    before = _episode_review_snapshot(episode)
    try:
        day = await day_for_exact_episode(episode)
        await publish_manual_episode_change(
            day=day,
            predecessors=[episode],
            successors=[],
            action="episode_delete",
            before=before,
            after={"superseded": True},
        )
    except ManualPublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@router.get("/episodes/{episode_id}/thumbnail")
async def get_episode_thumbnail(
    episode_id: str, user: User = Depends(current_active_user)
):
    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == str(user.id),
    )
    if episode is None or not episode.representative_image:
        raise HTTPException(status_code=404, detail="Episode thumbnail not found")
    if not await _readable(episode):
        raise HTTPException(status_code=404, detail="Episode thumbnail unavailable")
    return Response(
        content=episode.representative_image,
        media_type=episode.representative_image_type or "image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


class TimezoneRequest(BaseModel):
    timezone: str


@router.put("/timezone")
async def update_timeline_timezone(
    body: TimezoneRequest, user: User = Depends(current_active_user)
):
    user.timezone = _validate_timezone(body.timezone)
    await user.save()
    return {"timezone": user.timezone}


async def _photo_artifacts(request_id: str, user: User) -> Path:
    request = await TimelineReconciliationRequest.find_one(
        TimelineReconciliationRequest.request_id == request_id,
        TimelineReconciliationRequest.user_id == str(user.id),
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Reconciliation request not found")
    identity = request.immich_visual.artifact_id if request.immich_visual else ""
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise HTTPException(
            status_code=404, detail="No photo exploration artifacts for this request"
        )
    root = Path("data/timeline_photo_exploration").resolve()
    folder = (root / str(user.id) / request.local_date.isoformat() / identity).resolve()
    if not folder.is_relative_to(root) or not folder.is_dir():
        raise HTTPException(
            status_code=404, detail="Photo exploration artifacts are unavailable"
        )
    return folder


@router.get("/reconciliation/{request_id}/photos")
async def photo_exploration_details(
    request_id: str, user: User = Depends(current_active_user)
):
    folder = await _photo_artifacts(request_id, user)

    def read():
        return {
            "coverage": json.loads((folder / "coverage.json").read_text()),
            "rounds": json.loads((folder / "trace.json").read_text()),
        }

    try:
        return await asyncio.to_thread(read)
    except FileNotFoundError:
        raise HTTPException(
            status_code=409, detail="Photo exploration is still being recorded"
        )


@router.get("/reconciliation/{request_id}/photos/{round_number}/grid")
async def photo_exploration_grid(
    request_id: str, round_number: int, user: User = Depends(current_active_user)
):
    folder = await _photo_artifacts(request_id, user)
    if not 1 <= round_number <= 8:
        raise HTTPException(status_code=404, detail="Photo round not found")
    path = folder / f"round-{round_number:02d}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Photo grid not found")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )
