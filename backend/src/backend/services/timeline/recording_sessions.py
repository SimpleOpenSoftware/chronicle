"""Resolve recordings to committed activities, with a genuinely undated option."""

import asyncio
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

import backend.services.memory.scope as scope
import backend.services.privacy as privacy
import backend.services.timeline.consolidation as consolidation
import backend.services.timeline.review as review
import backend.services.timeline.sessions as sessions
from backend.models.conversation import Conversation
from backend.models.session_memory import UndatedSession
from backend.models.timeline import MemoryReviewProposal, TimelineDay
from backend.redis_keys import timeline_publication_lock
from backend.services.inference_artifacts import canonical_hash
from backend.services.recording_purpose import personal_recording_filter
from backend.services.redis_lock import distributed_lock

from .evidence import _transcript_item
from .memory_sources import (
    apply_dispositions,
    attributed_role,
    scope_hash,
    source_decisions,
)


async def owned_recording(user_id, identifier, memory_space_id=None):
    row = await Conversation.find_one(
        {
            "conversation_id": identifier,
            "user_id": user_id,
            "memory_space_id": memory_space_id,
            "deleted": False,
            **personal_recording_filter(),
        }
    )
    if row is None:
        raise LookupError("Recording not found")
    return row


def recording_hash(row):
    return canonical_hash(
        {
            "revision": row.active_transcript_version,
            "transcript": row.transcript,
            "segments": [s.model_dump(mode="json") for s in row.segments],
            "ranges": [
                r.model_dump(mode="json", exclude={"range_id"})
                for r in row.audio_ranges
            ],
            "excluded": row.memory_excluded,
        }
    )


def recording_sources(row):
    if not row.audio_ranges or not row.transcript:
        raise ValueError(
            "This recording needs retained audio references and a transcript"
        )
    bounds = (
        min(r.started_at for r in row.audio_ranges),
        max(r.ended_at for r in row.audio_ranges),
    )
    ref = _transcript_item(row, bounds)
    role, origin = attributed_role(ref)
    # These bounds locate stored audio. They do not claim a known event date.
    identity = {
        "evidence_id": ref.evidence_id,
        "content_hash": recording_hash(row),
        "locator": ref.locator.model_dump(),
        "started_at": bounds[0].isoformat(),
        "ended_at": bounds[1].isoformat(),
    }
    return [
        {
            **identity,
            "key": canonical_hash(identity),
            "kind": "transcript",
            "role": role,
            "original_role": ref.role,
            "attribution_origin": origin,
            "direction": ref.metadata["direction"],
            "event_date": None,
            "time_basis": "unknown",
            "excerpt": "\n".join(f"{s.speaker}: {s.text}" for s in row.segments)
            or row.transcript,
            "metadata": {**ref.metadata, "event_date": None, "time_basis": "unknown"},
            "capture_chunk_ids": sorted(
                {i for r in row.audio_ranges for i in r.chunk_ids}
            ),
            "capture_source_ids": sorted(
                {r.capture_source_id for r in row.audio_ranges}
            ),
            "episode_keys": [],
            "participation": (
                "excluded"
                if row.memory_excluded
                else (
                    "uncertain"
                    if role == "uncertain"
                    else "background" if role == "media_content" else "supporting"
                )
            ),
            "disposition": "exclude" if row.memory_excluded else "auto",
        }
    ]


async def prepare_undated(user_id, identifier, memory_space_id=None):
    row = await owned_recording(user_id, identifier, memory_space_id)
    if any(r.time_basis != "unknown" for r in row.audio_ranges):
        raise ValueError("This recording has a capture date; organize its day first")
    key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"undated:{user_id}:{memory_space_id}:{identifier}"
        )
    )
    async with distributed_lock(
        f"undated-session:{key}", timeout=60, blocking_timeout=5
    ):
        current = (
            await UndatedSession.find({"session_key": key})
            .sort("-revision")
            .first_or_none()
        )
        digest = recording_hash(row)
        if current and current.source_hash == digest:
            return current
        sources = recording_sources(row)
        session = UndatedSession(
            user_id=user_id,
            memory_space_id=memory_space_id,
            session_key=key,
            revision=current.revision + 1 if current else 1,
            recording_id=identifier,
            recording_revision=row.active_transcript_version,
            source_hash=digest,
            title=(row.title or "Undated recording")[:160],
            sources=sources,
        )
        await session.insert()
        await index_undated(session)
        return session


async def index_undated(session, *, visibility=None):
    # Defer this dependency to break the import cycle through backend.services.source_search
    # -> backend.services.timeline.recording_sessions.
    from backend.services.source_search import (
        VERSION,
        db,
        passages,
        publish_projection,
        searchable,
    )

    visibility = visibility or privacy.ConversationPrivacyFilter()
    if not await visibility.filter([{"conversation_id": session.recording_id}]):
        await db().source_search.delete_one({"_id": "session:" + session.session_key})
        return False
    try:
        await owned_recording(
            session.user_id, session.recording_id, session.memory_space_id
        )
    except LookupError:
        await db().source_search.delete_one({"_id": "session:" + session.session_key})
        return None
    await visibility.assert_current()

    fields = {
        "id": session.session_key,
        "title": session.title,
        "summary": "Undated session",
        "speakers": " ".join(
            s for r in session.sources for s in r["metadata"].get("speakers", [])
        ),
        "transcript": " ".join(r["excerpt"] for r in session.sources),
    }
    data = {
        "_id": "session:" + session.session_key,
        "kind": "session",
        "key": session.session_key,
        "user_id": session.user_id,
        "memory_space_id": session.memory_space_id,
        "revision": session.revision,
        "undated": True,
        "recording_id": session.recording_id,
        "fields": {k: v for k, v in fields.items() if k != "transcript"},
        "terms": await asyncio.to_thread(searchable, fields),
        "passages": passages(fields["transcript"]),
        "title": session.title,
        "summary": "Recording date unknown",
        "started_at": None,
        "updated_at": session.created_at,
        "url": f"/recordings/{session.recording_id}?session={session.session_key}",
        "source_hash": session.source_hash,
        "version": VERSION,
    }
    await publish_projection(data, visibility)
    return True


async def validate_undated(proposal):

    if proposal.memory_space_id:

        try:
            await scope.MemoryScopeResolver().require_space(
                scope.MemoryScope(proposal.user_id, proposal.memory_space_id),
                writable=True,
            )
        except scope.MemoryScopeError as exc:
            raise review.SelectionChanged(str(exc)) from exc

    session = await UndatedSession.find_one(
        {
            "session_key": proposal.session_key,
            "revision": proposal.session_revision,
            "user_id": proposal.user_id,
            "memory_space_id": proposal.memory_space_id,
        }
    )
    if session is None:
        raise review.SelectionChanged("Undated session revision is unavailable")
    try:
        recording = await owned_recording(
            proposal.user_id, session.recording_id, proposal.memory_space_id
        )
    except LookupError as exc:
        raise review.SelectionChanged(str(exc)) from exc
    if recording_hash(recording) != session.source_hash:
        raise review.SelectionChanged(
            "Recording evidence changed; prepare its current revision"
        )
    sources = apply_dispositions(
        session.sources,
        await source_decisions(
            proposal.user_id, session.sources, proposal.memory_space_id
        ),
        proposal.excluded_source_keys,
    )
    if proposal.source_scope_hash and scope_hash(sources) != proposal.source_scope_hash:
        raise review.SelectionChanged("Source decisions changed")
    return sources


async def generate_undated(
    user_id,
    identifier,
    revision,
    *,
    excluded_keys=(),
    memory_space_id=None,
    correction_of=None,
):

    session = await UndatedSession.find_one(
        {
            "session_key": identifier,
            "revision": revision,
            "user_id": user_id,
            "memory_space_id": memory_space_id,
        }
    )
    if session is None:
        raise LookupError("Session not found")
    await owned_recording(user_id, session.recording_id, memory_space_id)
    if not set(excluded_keys) <= {s["key"] for s in session.sources}:
        raise ValueError("Exclusion is outside the selected session")
    async with distributed_lock(
        timeline_publication_lock(user_id), timeout=60, blocking_timeout=5
    ):
        token = f"undated:{identifier}:{revision}"
        existing = (
            await MemoryReviewProposal.find(
                {"user_id": user_id, "selected_tokens": token}
            )
            .sort("-created_at")
            .first_or_none()
        )
        if (
            existing
            and existing.active
            and existing.state not in {"failed", "stale"}
            and set(existing.excluded_source_keys) == set(excluded_keys)
        ):
            await validate_undated(existing)
            if correction_of and correction_of not in existing.correction_of:
                raise ValueError(
                    "Resolve the current draft before preparing a correction"
                )
            return existing
        if existing and existing.active:
            await existing.set({"active": False})
        proposal = MemoryReviewProposal(
            request_id=str(uuid.uuid4()),
            user_id=user_id,
            memory_space_id=memory_space_id,
            local_date=None,
            timezone="Asia/Kolkata",
            source_kind="undated",
            recording_id=session.recording_id,
            snapshot_id=session.source_hash,
            selected_episodes=[],
            selected_tokens=[token],
            selection_hash=session.source_hash,
            session_key=identifier,
            session_revision=revision,
            excluded_source_keys=list(excluded_keys),
            priority=100,
        )
        proposal.source_scope = await validate_undated(proposal)
        proposal.source_scope_hash = scope_hash(proposal.source_scope)
        proposal.correction_of = [correction_of] if correction_of else []
        await proposal.insert()
        await sessions.enqueue_memory(proposal)
        return proposal


async def recording_context(user_id, identifier, timezone_name, memory_space_id=None):

    row = await owned_recording(user_id, identifier, memory_space_id)
    days = set()
    for span in row.audio_ranges:
        if span.time_basis != "unknown":
            first = span.started_at.astimezone(ZoneInfo(timezone_name)).date()
            last = (
                (span.ended_at - timedelta(microseconds=1))
                .astimezone(ZoneInfo(timezone_name))
                .date()
            )
            days.update(
                first + timedelta(days=i) for i in range((last - first).days + 1)
            )
    linked = []
    for local_date in sorted(days):
        day = await TimelineDay.find_one(
            TimelineDay.user_id == user_id,
            TimelineDay.local_date == local_date,
            TimelineDay.timezone == timezone_name,
        )
        if day and day.current_snapshot and not day.pending_publication_id:
            episodes = await consolidation.snapshot_episodes(day)
            chunks = {i for r in row.audio_ranges for i in r.chunk_ids}
            keys = {
                e.episode_key
                for e in episodes
                if any(
                    set(span.chunk_ids).intersection(r.chunk_ids)
                    and max(sessions.utc(span.started_at), sessions.utc(r.started_at))
                    < min(sessions.utc(span.ended_at), sessions.utc(r.ended_at))
                    for span in row.audio_ranges
                    for r in e.audio_ranges
                )
            }
            for s in await sessions.project_sessions(
                day, episodes, include_sources=True
            ):
                if keys.intersection(r["episode_key"] for r in s["episodes"]):
                    s["supporting_passages"] = [
                        source.get("excerpt", "")[:400]
                        for source in s["sources"]
                        if identifier
                        == source.get("metadata", {}).get("conversation_id")
                        or chunks.intersection(source.get("capture_chunk_ids", []))
                    ][:3]
                    s["sources"] = []
                    linked.append(s)
    linked = list({(s["session_key"], s["revision"]): s for s in linked}.values())
    undated = (
        await UndatedSession.find(
            {
                "user_id": user_id,
                "recording_id": identifier,
                "memory_space_id": memory_space_id,
            }
        )
        .sort("-revision")
        .first_or_none()
    )
    proposal = (
        await MemoryReviewProposal.find(
            {"user_id": user_id, "session_key": undated.session_key}
        )
        .sort("-created_at")
        .first_or_none()
        if undated
        else None
    )
    # Defer this dependency to break the import cycle through backend.services.source_search
    # -> backend.services.timeline.recording_sessions.
    from backend.services.source_search import db

    requests = (
        await db()
        .recording_organization_intents.find(
            {"user_id": user_id, "recording_id": identifier}, {"_id": 0}
        )
        .to_list()
    )
    undated_data = undated.model_dump(mode="json", exclude={"id"}) if undated else None
    if undated_data:
        undated_data["sources"] = apply_dispositions(
            undated.sources,
            await source_decisions(user_id, undated.sources, memory_space_id),
        )
        undated_data["stale"] = recording_hash(row) != undated.source_hash
    return {
        "organization_requests": requests,
        "recording_id": identifier,
        "event_date_known": bool(days),
        "dates": [d.isoformat() for d in sorted(days)],
        "uploaded_at": row.created_at,
        "sessions": linked,
        "undated_session": undated_data,
        "proposal": (
            proposal.model_dump(mode="json", exclude={"id"}) if proposal else None
        ),
    }
