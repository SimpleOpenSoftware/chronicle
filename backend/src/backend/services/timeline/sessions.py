"""Session organization, memory dispositions and bounded background preparation."""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rq as rq
from pymongo.errors import DuplicateKeyError

import backend.services.privacy as privacy
import backend.services.sse_publisher as sse_publisher
import backend.services.timeline.session_organization as session_organization
from backend.models.session_memory import (
    MemorySourceDecision,
    SessionPreparation,
    SessionProposalView,
)
from backend.models.timeline import (
    EpisodeRevisionRef,
    MemoryReviewProposal,
    TimelineDay,
    TimelineEpisode,
    TimelineReviewDecision,
    TimelineSemanticGroupRevision,
    utcnow,
)
from backend.redis_keys import timeline_publication_lock
from backend.services.redis_lock import LockUnavailable, distributed_lock

from .consolidation import (
    _publish_group_revisions,
    active_semantic_groups,
    snapshot_episodes,
)
from .memory_sources import (
    account_sources,
    apply_dispositions,
    evidence_sources,
    memory_sources,
    scope_hash,
    scope_questions,
    source_decisions,
    utc,
)


async def get_day(user_id, local_date, timezone_name):
    day = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if day is None or day.current_snapshot is None or day.pending_publication_id:
        raise ValueError("Session publication is not ready")
    return day


def session_groups(day, episodes):
    """Exact semantic groups and singleton activities; never chronological buckets."""
    ids = {e.episode_id for e in episodes}
    groups = [g for g in active_semantic_groups(day) if set(g.episode_ids) <= ids]
    covered = {key for g in groups for key in g.episode_ids}
    for episode in episodes:
        if episode.episode_id in covered:
            continue
        groups.append(
            TimelineSemanticGroupRevision(
                group_key=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, f"chronicle-session:{episode.episode_key}"
                    )
                ),
                revision=episode.revision + 1,
                member_revisions=[
                    EpisodeRevisionRef(
                        episode_key=episode.episode_key, revision=episode.revision
                    )
                ],
                episode_ids=[episode.episode_id],
                source_snapshot_id=day.current_snapshot_id,
                origin="automatic",
                title=episode.title,
                summary=episode.summary or episode.title,
                started_at=episode.started_at,
                ended_at=episode.ended_at,
            )
        )
    return sorted(groups, key=lambda g: utc(g.started_at))


async def resolved_sessions(day, episodes):
    """Project the same committed session on every day its evidence intersects."""
    ids = {e.episode_id for e in episodes}
    owners = await TimelineDay.find(
        {
            "user_id": day.user_id,
            "timezone": day.timezone,
            "semantic_group_history.episode_ids": {"$in": sorted(ids)},
        }
    ).to_list()
    owners = {owner.local_date: owner for owner in owners}
    owners[day.local_date] = day
    resolved, covered = [], set()
    for owner in owners.values():
        if owner.pending_publication_id:
            continue
        for group in active_semantic_groups(owner):
            if not ids.intersection(group.episode_ids):
                continue
            members = await TimelineEpisode.find(
                {
                    "user_id": day.user_id,
                    "$or": [r.model_dump() for r in group.member_revisions],
                }
            ).to_list()
            if len(members) != len(group.member_revisions) or any(
                e.status == "superseded" for e in members
            ):
                continue
            resolved.append((owner, group, members))
            covered.update(group.episode_ids)
    for group in session_groups(
        day, [e for e in episodes if e.episode_id not in covered]
    ):
        resolved.append(
            (day, group, [e for e in episodes if e.episode_id in group.episode_ids])
        )
    return sorted(resolved, key=lambda item: utc(item[1].started_at))


async def project_sessions(day, episodes, *, include_sources=True):
    resolved = await resolved_sessions(day, episodes)
    all_members = list(
        {e.episode_id: e for _, _, members in resolved for e in members}.values()
    )
    decisions = await source_decisions(day.user_id, evidence_sources(all_members))
    tokens = [f"{e.episode_key}:{e.revision}" for e in all_members]
    proposals = (
        await MemoryReviewProposal.find(
            {"user_id": day.user_id, "selected_tokens": {"$in": tokens}}
        )
        .sort("created_at")
        .project(SessionProposalView)
        .to_list()
    )
    result = []
    for owner, group, members in resolved:
        sources = apply_dispositions(evidence_sources(members), decisions)
        refs = {f"{r.episode_key}:{r.revision}" for r in group.member_revisions}
        matches = [
            p
            for p in proposals
            if p.session_key == group.group_key or set(p.selected_tokens) == refs
        ]
        proposal = matches[-1] if matches else None
        corrections = [
            p
            for p in matches
            if p.state == "correction_required"
            and p.accepted_change_ids
            and not p.corrected_by_proposal_id
        ]
        if corrections and (
            proposal is None
            or proposal.state in {"excluded", "stale", "correction_required"}
        ):
            proposal = corrections[-1]
        same_scope = proposal is not None and proposal.source_scope_hash == scope_hash(
            sources
        )
        account = proposal.account if same_scope and proposal.account else {}
        eligible = memory_sources(sources)
        questions = []
        state = "available"
        if sources and all(s.get("deferred") for s in sources):
            state = "deferred"
        elif not account_sources(sources):
            state = "excluded"
        if proposal and proposal.accepted_change_ids and not same_scope:
            state = "correction_required"
        elif proposal and state not in {"excluded", "deferred"} and same_scope:
            state = proposal.state
            questions = list(dict.fromkeys([*questions, *proposal.questions]))
        result.append(
            {
                "session_key": group.group_key,
                "revision": group.revision,
                "owner_local_date": owner.local_date,
                "origin": group.origin,
                "title": account.get("title", group.title),
                "summary": account.get("summary")
                or f"{len(members)} episode{'s' if len(members) != 1 else ''} · {len(sources)} source excerpt{'s' if len(sources) != 1 else ''}",
                "started_at": utc(group.started_at),
                "ended_at": utc(group.ended_at),
                "episodes": [r.model_dump() for r in group.member_revisions],
                "episode_ids": group.episode_ids,
                "sources": sources if include_sources else [],
                "source_keys": [s["key"] for s in sources],
                "scope_hash": scope_hash(sources),
                "state": state,
                "questions": questions,
                "proposal_id": proposal.proposal_id if proposal else None,
                "change_count": proposal.change_count if proposal else 0,
                "stage": proposal.stage if proposal else None,
                "completed_sources": proposal.completed_sources if proposal else 0,
                "total_sources": proposal.total_sources if proposal else 0,
                "investigation_activity": (
                    proposal.investigation_activity if proposal else {}
                ),
                "failure_kind": proposal.failure_kind if proposal else None,
                "error": proposal.error if proposal else None,
                "refresh_assessment": proposal.refresh_assessment if proposal else None,
            }
        )

    return await privacy.filter_payloads(result, day.user_id)


async def choose_session(user_id, local_date, timezone_name, session_key, revision):
    day = await get_day(user_id, local_date, timezone_name)
    episodes = await snapshot_episodes(day)
    resolved = next(
        (
            item
            for item in await resolved_sessions(day, episodes)
            if item[1].group_key == session_key and item[1].revision == revision
        ),
        None,
    )
    if resolved is None:
        raise ValueError("Session changed; refresh before deciding")

    await privacy.guard_payload(user_id, resolved[2])
    return resolved


async def decide_session(
    user_id,
    local_date,
    timezone_name,
    session_key,
    revision,
    action,
    source_keys,
    expected_scope,
    role=None,
    clarification=None,
):
    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=120, blocking_timeout=5
    ):
        day, group, members = await choose_session(
            user_id, local_date, timezone_name, session_key, revision
        )
        decisions = await source_decisions(user_id, evidence_sources(members))
        sources = apply_dispositions(evidence_sources(members), decisions)
        if scope_hash(sources) != expected_scope:
            raise ValueError("Source scope changed; refresh before deciding")
        if not source_keys or not set(source_keys) <= {s["key"] for s in sources}:
            raise ValueError("Choose current source evidence")
        if action == "attribute" and role is None:
            raise ValueError("Choose an attribution for this source")
        if action == "clarify" and not (clarification or "").strip():
            raise ValueError("Enter the clarification to retain with this evidence")
        tokens = [f"{e.episode_key}:{e.revision}" for e in members]
        rows = await MemoryReviewProposal.find(
            {
                "user_id": user_id,
                "selected_tokens": {"$in": tokens},
                "$or": [{"active": True}, {"accepted_change_ids.0": {"$exists": True}}],
            }
        ).to_list()
        if any(p.state == "applying" for p in rows):
            raise ValueError(
                "Memory is being applied; refresh its outcome before changing sources"
            )
        await MemorySourceDecision(
            user_id=user_id,
            session_key=session_key,
            action=action,
            role=role,
            clarification=clarification,
            sources=[s for s in sources if s["key"] in source_keys],
        ).insert()
        for proposal in ([] if action in {"defer", "resume"} else rows):
            if proposal.state == "applying":
                # The same publication lock fences apply-time source validation.
                raise ValueError(
                    "Memory is being applied; refresh its outcome before changing sources"
                )
            proposal.state = (
                "correction_required" if proposal.accepted_change_ids else "stale"
            )
            proposal.active = False
            await proposal.save()
    correction_required = action not in {"defer", "resume"} and any(
        p.accepted_change_ids for p in rows
    )
    if action not in {"defer", "resume"} and not correction_required:
        await request_preparation(day, priority=100, force=True)
    return {"action": action, "correction_required": correction_required}


async def request_session_memory(
    user_id,
    local_date,
    timezone_name,
    session_key,
    revision,
    *,
    excluded_keys=(),
    priority=0,
):
    # Defer this dependency to break the import cycle through backend.services.timeline.review
    # -> backend.services.timeline.sessions.
    from .review import create_memory_selection

    day, group, members = await choose_session(
        user_id, local_date, timezone_name, session_key, revision
    )
    raw_sources = evidence_sources(members)
    known = {
        s["key"]
        for s in apply_dispositions(
            raw_sources, await source_decisions(user_id, raw_sources)
        )
    }
    if not set(excluded_keys) <= known:
        raise ValueError("Source exclusions are outside this session")
    proposals = await create_memory_selection(
        user_id,
        day.local_date,
        timezone_name,
        day.current_snapshot_id,
        group.member_revisions,
        session_key=session_key,
        session_revision=group.revision,
        session_owner_date=day.local_date,
        excluded_source_keys=excluded_keys,
        priority=priority,
    )
    for proposal in proposals:
        await enqueue_memory(proposal)
    return proposals


def _enqueue(function, identifier, *, priority, label, continuation=False):
    # Defer this dependency to break the import cycle through backend.controllers ->
    # backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.controllers.queue_controller import (
        _job_is_live,
        memory_queue,
        redis_conn,
    )

    key = f"session-enqueue:{function.__name__}:{identifier}"
    lock = redis_conn.lock(key, timeout=15, blocking_timeout=1)
    with lock:
        job_id = f"{function.__name__}_{identifier}"
        if _job_is_live(job_id):
            return job_id
        job = memory_queue.enqueue(
            function,
            identifier,
            job_id=job_id,
            # A new explicit request gets prompt admission. Once it yields, it
            # rotates behind waiting work instead of repeatedly overtaking it.
            at_front=priority > 0 and not continuation,
            job_timeout=1800,
            result_ttl=86400,
            failure_ttl=86400,
            description=label,
            meta={"session_memory": True},
        )
        return job.id


async def enqueue_memory(proposal):
    # Defer this dependency to break the import cycle through backend.workers ->
    # backend.controllers -> backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.workers.session_jobs import generate_session_memory_job

    if proposal.state != "queued":
        return
    if proposal.attempts >= 3:
        await MemoryReviewProposal.get_pymongo_collection().update_one(
            {"_id": proposal.id, "state": "queued"},
            {
                "$set": {
                    "state": "failed",
                    "error": "Generation retries exhausted after three attempts",
                }
            },
        )
        return
    proposal.job_id = await asyncio.to_thread(
        _enqueue,
        generate_session_memory_job,
        proposal.proposal_id,
        priority=proposal.priority,
        label="Prepare session memory draft",
        continuation=proposal.stage != "queued",
    )
    await proposal.set({"job_id": proposal.job_id})


async def enqueue_decision(proposal):
    # Defer this dependency to break the import cycle through backend.workers ->
    # backend.controllers -> backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.workers.session_jobs import apply_session_memory_job

    proposal.job_id = await asyncio.to_thread(
        _enqueue,
        apply_session_memory_job,
        proposal.proposal_id,
        priority=100,
        label="Validate and apply reviewed memory changes",
    )
    await proposal.set({"job_id": proposal.job_id})


async def publish_progress(proposal):
    """Persist on the owning job and emit the same status to the user's event stream."""

    job = rq.get_current_job()
    if job is None:
        return
    payload = {
        "session_key": proposal.session_key,
        "proposal_id": proposal.proposal_id,
        "state": proposal.state,
        "stage": proposal.stage,
        "attempt": proposal.attempts,
        "completed_sources": proposal.completed_sources,
        "total_sources": proposal.total_sources,
        "investigation_activity": proposal.investigation_activity,
        "failure_kind": proposal.failure_kind,
        "updated_at": utcnow().isoformat(),
    }

    def save():
        if job:
            job.meta["session_progress"] = payload
            job.meta["session_events"] = [*job.meta.get("session_events", []), payload][
                -80:
            ]
            job.save_meta()
        sse_publisher.publish_sse_event(
            proposal.user_id, "session.memory.progress", payload
        )

    try:
        await asyncio.to_thread(save)
    except Exception:

        logging.getLogger(__name__).warning(
            "Could not emit session job progress", exc_info=True
        )


async def request_preparation(day, *, priority=0, force=False, organize_only=False):
    # Defer this dependency to break the import cycle through backend.workers ->
    # backend.controllers -> backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.workers.session_jobs import prepare_sessions_job

    identity = dict(
        user_id=day.user_id,
        local_date=day.local_date,
        timezone=day.timezone,
        snapshot_id=day.current_snapshot_id,
    )
    item = await SessionPreparation.find_one(identity)
    if item is None:
        item = SessionPreparation(
            **identity, priority=priority, organize_only=organize_only
        )
        try:
            await item.insert()
        except DuplicateKeyError:
            item = await SessionPreparation.find_one(identity)
    if item.state == "complete" and not force:
        # A missed invalidation event must not strand a current stale session.
        force = any(
            session["state"] == "stale"
            for session in await project_sessions(day, await snapshot_episodes(day))
        )
    if organize_only and not item.organize_only:
        await item.set({"organize_only": True})
    if force:
        await SessionPreparation.get_pymongo_collection().update_one(
            {"_id": item.id}, {"$inc": {"requested_revision": 1}}
        )
        await SessionPreparation.get_pymongo_collection().update_one(
            {"_id": item.id, "state": {"$in": ["complete", "failed", "stale"]}},
            {"$set": {"state": "queued", "attempts": 0, "error": None}},
        )
        item = await SessionPreparation.get(item.id)
    if item.state in {"queued", "running", "waiting", "failed"} and item.attempts < 3:
        item.priority = max(priority, item.priority)
        await SessionPreparation.get_pymongo_collection().update_one(
            {"_id": item.id}, {"$max": {"priority": item.priority}}
        )
        item.job_id = await asyncio.to_thread(
            _enqueue,
            prepare_sessions_job,
            str(item.id),
            priority=item.priority,
            label="Organize meaningful sessions",
        )
        await item.set({"job_id": item.job_id})
    return item


async def persist_preparation(item):
    """Worker-owned fields only: an arriving correction must survive every save."""
    fields = {
        "state",
        "attempts",
        "error",
        "result_snapshot_id",
        "inference_artifacts",
        "waiting_sessions",
        "updated_at",
        "completed_revision",
    }
    await SessionPreparation.get_pymongo_collection().update_one(
        {"_id": item.id},
        {
            "$set": {
                key: value for key, value in item.model_dump().items() if key in fields
            }
        },
    )


async def prepare_sessions(item):
    day = await get_day(item.user_id, item.local_date, item.timezone)
    if day.current_snapshot_id != (item.result_snapshot_id or item.snapshot_id):
        item.state = "stale"
        await persist_preparation(item)
        return
    episodes = await snapshot_episodes(day)
    existing = [
        g
        for owner, g, _ in await resolved_sessions(day, episodes)
        if any(
            g.group_key == saved.group_key and g.revision == saved.revision
            for saved in active_semantic_groups(owner)
        )
    ]
    existing_keys = {g.group_key for g in existing}
    for session in await project_sessions(day, episodes):
        if (
            not item.organize_only
            and session["session_key"] in existing_keys
            and session["state"]
            in {
                "available",
                "stale",
            }
        ):
            try:
                await request_session_memory(
                    day.user_id,
                    day.local_date,
                    day.timezone,
                    session["session_key"],
                    session["revision"],
                    priority=item.priority,
                )
            except (ValueError, RuntimeError) as exc:
                item.waiting_sessions[session["session_key"]] = str(exc)[:1000]
    covered = {id for g in existing for id in g.episode_ids}
    remaining = [e for e in episodes if e.episode_id not in covered]
    new_groups = []

    async def record(artifact):
        item.inference_artifacts = list(
            dict.fromkeys([*item.inference_artifacts, artifact])
        )
        item.updated_at = utcnow()
        await persist_preparation(item)

    units = await session_organization.organize_sessions(remaining, record=record)
    for unit in units:
        members = unit.members
        new_groups.append(
            TimelineSemanticGroupRevision(
                group_key=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "chronicle-session:"
                        + ":".join(sorted(e.episode_key for e in members)),
                    )
                ),
                member_revisions=[
                    EpisodeRevisionRef(episode_key=e.episode_key, revision=e.revision)
                    for e in members
                ],
                episode_ids=[e.episode_id for e in members],
                source_snapshot_id=day.current_snapshot_id,
                origin="automatic",
                title=unit.title,
                summary=unit.summary,
                reason=unit.summary[:500],
                started_at=min(utc(e.started_at) for e in members),
                ended_at=max(utc(e.ended_at) for e in members),
            )
        )
    # A new singleton's first persisted revision starts at 1.
    new_groups = [
        g.model_copy(
            update={
                "revision": max(
                    (
                        old.revision
                        for old in day.semantic_group_history
                        if old.group_key == g.group_key
                    ),
                    default=0,
                )
                + 1
            }
        )
        for g in new_groups
    ]
    if new_groups:
        current = await get_day(day.user_id, day.local_date, day.timezone)
        if current.current_snapshot_id != (item.result_snapshot_id or item.snapshot_id):
            item.state = "stale"
            await persist_preparation(item)
            return
        await _publish_group_revisions(
            current,
            new_groups,
            [
                TimelineReviewDecision(
                    run_id=day.current_snapshot_id,
                    action="session_organized",
                    episode_ids=g.episode_ids,
                    after=g.model_dump(mode="json"),
                )
                for g in new_groups
            ],
        )
    day = await get_day(day.user_id, day.local_date, day.timezone)
    item.result_snapshot_id = day.current_snapshot_id
    await persist_preparation(item)
    items = await project_sessions(day, episodes)
    item.waiting_sessions = {}
    for session in items:
        if item.organize_only:
            break
        if session["state"] in {"available", "stale"}:
            try:
                await request_session_memory(
                    day.user_id,
                    day.local_date,
                    day.timezone,
                    session["session_key"],
                    session["revision"],
                    priority=item.priority,
                )
            except (ValueError, RuntimeError) as exc:
                # Retain a durable, recoverable prerequisite instead of silently
                # dropping a session while its siblings complete.
                item.waiting_sessions[session["session_key"]] = str(exc)[:1000]
    item.state = "waiting" if item.waiting_sessions else "complete"
    if item.waiting_sessions:
        item.attempts = max(0, item.attempts - 1)
    item.completed_revision = item.requested_revision
    await persist_preparation(item)


async def prepare_recent_sessions():
    """Registered recovery entry point; bounded recent history, newest first."""
    # Defer this dependency to break the import cycle through backend.services.source_search
    # -> backend.services.timeline.sessions.
    from backend.services.source_search import db

    intents = await db().recording_organization_intents.find({}).limit(100).to_list()
    for intent in intents:
        owner = await TimelineDay.find_one(
            TimelineDay.user_id == intent["user_id"],
            TimelineDay.local_date == date.fromisoformat(intent["local_date"]),
            TimelineDay.timezone == intent["timezone"],
        )
        if owner and owner.current_snapshot and not owner.pending_publication_id:
            await request_preparation(owner, organize_only=True, priority=100)
    days = (
        await TimelineDay.find(
            {
                "current_snapshot_id": {"$nin": [None, ""]},
                "local_date": {"$gte": utcnow() - timedelta(days=8)},
            }
        )
        .sort("-local_date")
        .limit(100)
        .to_list()
    )
    considered = 0
    for day in days:
        today = utcnow().astimezone(ZoneInfo(day.timezone)).date()
        if (
            today - timedelta(days=6) <= day.local_date <= today
            and not day.pending_publication_id
        ):
            await request_preparation(day)
            considered += 1
    rows = (
        await MemoryReviewProposal.find(
            {
                "$or": [
                    {"state": {"$in": ["queued", "generating"]}},
                    {"state": "failed", "attempts": {"$lt": 3}},
                ],
                "session_key": {"$ne": None},
            }
        )
        .sort([("priority", -1), ("created_at", 1)])
        .limit(50)
        .to_list()
    )
    # Defer this dependency to break the import cycle through backend.controllers ->
    # backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.controllers.queue_controller import _job_is_live

    # Defer this dependency to break the import cycle through backend.workers ->
    # backend.controllers -> backend.controllers.system_controller -> backend.chat_service ->
    # backend.services.chat_sources -> backend.services.timeline.sessions.
    from backend.workers.session_jobs import prepare_sessions_job

    preparations = (
        await SessionPreparation.find(
            {
                "$or": [
                    {"state": {"$in": ["queued", "running", "waiting"]}},
                    {"state": "failed", "attempts": {"$lt": 3}},
                ]
            }
        )
        .sort([("priority", -1), ("updated_at", 1)])
        .limit(30)
        .to_list()
    )
    for item in preparations:
        if item.job_id and await asyncio.to_thread(_job_is_live, item.job_id):
            continue
        if item.attempts >= 3:
            await SessionPreparation.get_pymongo_collection().update_one(
                {"_id": item.id, "state": item.state, "attempts": item.attempts},
                {
                    "$set": {
                        "state": "failed",
                        "error": "Session organization retries exhausted after three attempts",
                    }
                },
            )
            continue
        item.job_id = await asyncio.to_thread(
            _enqueue,
            prepare_sessions_job,
            str(item.id),
            priority=item.priority,
            label="Recover session preparation",
        )
        await item.set({"job_id": item.job_id, "updated_at": utcnow()})
    for proposal in rows:
        if proposal.job_id and await asyncio.to_thread(_job_is_live, proposal.job_id):
            continue
        result = await MemoryReviewProposal.get_pymongo_collection().update_one(
            {
                "_id": proposal.id,
                "state": proposal.state,
                "active": True,
                "attempts": proposal.attempts,
            },
            {
                "$set": {
                    "state": "queued" if proposal.attempts < 3 else "failed",
                    "error": (
                        None
                        if proposal.attempts < 3
                        else "Generation retries exhausted after three attempts"
                    ),
                }
            },
        )
        if result.modified_count or proposal.state == "queued":
            await enqueue_memory(await MemoryReviewProposal.get(proposal.id))
    return {"considered": considered}


async def queue_published_sessions(journal):
    """Publication event; the recent scan recovers an interrupted enqueue."""
    if journal.operation_source == "semantic_group":
        return
    for plan in journal.affected_days:
        # Defer this dependency to break the import cycle through backend.services.source_search
        # -> backend.services.timeline.sessions.
        from backend.services.source_search import db

        intent = await db().recording_organization_intents.find_one(
            {
                "user_id": journal.user_id,
                "local_date": plan.local_date.isoformat(),
                "timezone": plan.timezone,
            }
        )
        today = utcnow().astimezone(ZoneInfo(plan.timezone)).date()
        requested = await SessionPreparation.find_one(
            {
                "user_id": journal.user_id,
                "local_date": plan.local_date,
                "timezone": plan.timezone,
                "priority": {"$gt": 0},
            }
        )
        if intent or requested or today - timedelta(days=6) <= plan.local_date <= today:
            day = await get_day(journal.user_id, plan.local_date, plan.timezone)
            await request_preparation(
                day, organize_only=bool(intent), priority=100 if intent else 0
            )
