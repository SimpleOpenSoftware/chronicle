"""Read-only accepted context and bounded, advisory refresh assessments."""

import asyncio
from datetime import timedelta
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

import backend.services.privacy as privacy
import backend.services.source_search as source_search
import backend.services.timeline.memory_sources as memory_sources
import backend.services.timeline.pi_tasks as pi_tasks
import backend.services.timeline.review as review
import backend.services.timeline.sessions as sessions
import backend.workers.source_search_jobs as source_search_jobs
from backend.models.timeline import MemoryReviewProposal, utcnow
from backend.services.inference_artifacts import canonical_hash
from backend.services.memory.scope import MemoryScope, MemoryScopeResolver
from backend.services.memory.vault_lock import vault_run_lock
from backend.services.memory.vault_scaffold import is_scaffold_note

VERSION = "accepted-context-pi-v1"


def vault_root(user_id, memory_space_id=None):
    return MemoryScopeResolver().vault_root(MemoryScope(user_id, memory_space_id))


def snapshot(user_id, memory_space_id=None):
    root = vault_root(user_id, memory_space_id)
    with vault_run_lock(user_id):
        return {
            p.relative_to(root).as_posix(): p.read_text()
            for p in root.rglob("*.md")
            if p.is_file()
            and not p.is_symlink()
            and not is_scaffold_note(p, root)
            and p.relative_to(root).parts[0] != "Conversations"
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
        }


async def processing_snapshot(user_id, memory_space_id=None):
    """Return accepted notes eligible for processing under the current policy."""

    policy = await privacy.load_snapshot(user_id)
    excluded = {
        path.casefold()
        for path in await privacy.quarantined_vault_paths(user_id, snapshot=policy)
    }
    notes = await asyncio.to_thread(snapshot, user_id, memory_space_id)
    await privacy.assert_current(user_id, policy)
    return {
        path: text for path, text in notes.items() if path.casefold() not in excluded
    }


async def for_sources(
    user_id, sources, memory_space_id=None, questions=(), timezone_name=None
):
    """Seed only scope; the Pi investigator selects relevant accepted knowledge."""
    notes = await processing_snapshot(user_id, memory_space_id)
    return {
        "policy_version": VERSION,
        "notes": [],
        "lookup_terms": [],
        "unresolved_lookups": [],
        "unresolved_questions": list(questions),
        "scope": {"user_id": user_id, "memory_space_id": memory_space_id},
        "scope_hash": canonical_hash(notes),
        "timezone": timezone_name,
    }


class FullNoteChange(TypedDict):
    """A comparison of complete note contents in two tracked vault snapshots."""

    before: str | None
    after: str | None


class HistoricalNoteContext(TypedDict):
    """Current content with lookup history, not a reconstructed previous note."""

    comparison: Literal["previous_content_unavailable"]
    previously_consulted_passages: list[str]
    previous_note_hashes: list[str]
    after: str | None


AssessmentMaterial = dict[str, FullNoteChange | HistoricalNoteContext]


def _changed_notes(
    before: dict[str, str], current: dict[str, str]
) -> dict[str, FullNoteChange]:
    return {
        path: {"before": before.get(path), "after": current.get(path)}
        for path in sorted(set(before) | set(current))
        if before.get(path) != current.get(path)
    }


def _historical_context_candidates(
    accepted_context: dict, current: dict[str, str]
) -> dict[str, HistoricalNoteContext]:
    """Do not mistake an unconsulted note for a newly created note.

    A saved passage cannot reconstruct its complete prior note. Retain every
    consulted passage as lookup context and compare full-note hashes where known.
    """
    consulted_by_path: dict[str, list[dict]] = {}
    for note in accepted_context.get("notes", []):
        consulted_by_path.setdefault(note["path"], []).append(note)
    candidates = {}
    for path in sorted(set(consulted_by_path) | set(current)):
        consulted = consulted_by_path.get(path, [])
        previous_hashes = sorted(
            {note["hash"] for note in consulted if note.get("hash")}
        )
        current_hash = canonical_hash(current[path]) if path in current else None
        if current_hash in previous_hashes:
            continue
        if path not in current and not previous_hashes:
            continue
        candidates[path] = {
            "comparison": "previous_content_unavailable",
            "previously_consulted_passages": list(
                dict.fromkeys(
                    note["passage"] for note in consulted if note.get("passage")
                )
            ),
            "previous_note_hashes": previous_hashes,
            "after": current.get(path),
        }
    return candidates


class Assessment(BaseModel):
    verdict: str = Field(pattern="^(useful|unrelated|uncertain)$")
    # Execution budgets bound output; explanation length is not a semantic failure.
    reason: str = Field(min_length=1)
    relevant_paths: list[str] = Field(default_factory=list)


async def assess(proposal, changes: AssessmentMaterial):

    runs = []

    async def record(run):
        runs.append(run)

    def validate(result):
        if not set(result.relevant_paths) <= set(changes):
            raise ValueError("Assessment cited a note outside the supplied comparison")

    try:
        await review.validate_selection(proposal)
        sources = memory_sources.account_sources(
            memory_sources.apply_dispositions(
                proposal.source_scope,
                await memory_sources.source_decisions(
                    proposal.user_id, proposal.source_scope, proposal.memory_space_id
                ),
                proposal.excluded_source_keys,
            )
        )
        if memory_sources.scope_hash(sources) != memory_sources.scope_hash(
            memory_sources.account_sources(proposal.source_scope)
        ):
            return {
                "verdict": "uncertain",
                "reason": "The session source selection changed; rebuild its account before assessing new context.",
                "relevant_paths": [],
                "error": "source_scope_changed",
            }
        outcome = await pi_tasks.run_task(
            stage="refresh_assessment",
            instruction=(
                "Assess whether the supplied accepted knowledge would materially improve this session account, "
                "its questions or proposed memories. Explain the expected benefit or lack of relevance, "
                "preserving uncertainty. Historical comparisons contain lookup excerpts, not complete prior "
                "notes; absence from lookup history does not establish that a note is new."
            ),
            payload={
                "account": proposal.account,
                "questions": proposal.questions,
                "prior_context": proposal.accepted_context,
                "changes": changes,
            },
            sources=sources,
            result_type=Assessment,
            user_id=proposal.user_id,
            memory_space_id=proposal.memory_space_id,
            record=record,
            validate=validate,
        )
        await review.validate_selection(proposal)
        return {
            **outcome.result.model_dump(),
            "artifact_hash": runs[-1]["artifact_hash"],
            "operation": runs[-1]["operation"],
        }
    except Exception as exc:
        return {
            "verdict": "uncertain",
            "reason": "The context investigation did not complete.",
            "relevant_paths": list(changes),
            "error": str(exc),
            **(
                {
                    "artifact_hash": runs[-1]["artifact_hash"],
                    "operation": runs[-1]["operation"],
                }
                if runs
                else {}
            ),
        }


async def assess_scope(user_id, memory_space_id=None):
    """Persist the changed-note snapshot before assessing at most five candidates."""

    collection = source_search.db().vault_context_checks
    key = canonical_hash([user_id, memory_space_id])
    current = await processing_snapshot(user_id, memory_space_id)
    # Opening historical material requests an advisory check, never generation.
    requested = (
        await source_search.db()
        .context_assessment_requests.find(
            {"user_id": user_id, "memory_space_id": memory_space_id, "state": "queued"}
        )
        .limit(5)
        .to_list()
    )
    for request in requested:
        proposal = await MemoryReviewProposal.find_one(
            {
                "proposal_id": request["proposal_id"],
                "user_id": user_id,
                "memory_space_id": memory_space_id,
            }
        )
        if proposal and proposal.state not in {
            "queued",
            "generating",
            "regenerating",
            "stale",
            "corrected",
            "excluded",
            "rejected",
            "applying",
        }:
            candidates = _historical_context_candidates(
                proposal.accepted_context, current
            )
            await assess_changes(proposal, candidates)
        await source_search.db().context_assessment_requests.update_one(
            {"_id": request["_id"]}, {"$set": {"state": "complete"}}
        )
    state = await collection.find_one({"_id": key}) or {}
    before = state.get("notes", {})
    changes = _changed_notes(before, current)
    if changes:
        pending = dict(state.get("pending_changes", {}))
        for p, value in changes.items():
            pending[p] = {
                "before": pending.get(p, value)["before"],
                "after": value["after"],
            }
        await collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "user_id": user_id,
                    "memory_space_id": memory_space_id,
                    "notes": current,
                    "pending_changes": pending,
                    "after": None,
                    "state": "queued",
                }
            },
            upsert=True,
        )
        state.update(pending_changes=pending, after=None)
    changes = state.get("pending_changes", {})
    if not changes:
        return len(requested)
    fingerprint = canonical_hash(changes)
    query = {
        "user_id": user_id,
        "memory_space_id": memory_space_id,
        "state": {
            "$nin": [
                "queued",
                "generating",
                "applying",
                "checking",
                "regenerating",
                "excluded",
                "rejected",
                "deferred",
                "stale",
            ]
        },
        "$or": [
            {"local_date": {"$gte": utcnow() - timedelta(days=7)}},
            {
                "priority": {"$gt": 0},
                "state": {
                    "$in": ["pending", "needs_attention", "no_changes", "failed"]
                },
            },
        ],
    }
    if state.get("after"):
        query["_id"] = {"$gt": state["after"]}
    rows = await MemoryReviewProposal.find(query).sort("_id").limit(5).to_list()
    for proposal in rows:
        today = utcnow().astimezone(ZoneInfo(proposal.timezone)).date()
        recent = (
            proposal.local_date is not None
            and today - timedelta(days=6) <= proposal.local_date <= today
        )
        if not recent and not (
            proposal.priority > 0
            and proposal.state in {"pending", "needs_attention", "no_changes", "failed"}
        ):
            await collection.update_one(
                {"_id": key}, {"$set": {"after": proposal.id, "state": "running"}}
            )
            continue
        # Keep deduplication and exact-snapshot checks at the same boundary.
        await assess_changes(proposal, changes)
        await collection.update_one(
            {"_id": key}, {"$set": {"after": proposal.id, "state": "running"}}
        )
    if not rows:
        retry = await MemoryReviewProposal.find_one(
            {
                "user_id": user_id,
                "memory_space_id": memory_space_id,
                "refresh_assessment.change_hash": fingerprint,
                "refresh_assessment.error": {"$exists": True},
                "refresh_assessment.attempts": {"$lt": 3},
            }
        )
        await collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "pending_changes": changes if retry else {},
                    "after": None,
                    "state": "queued" if retry else "complete",
                }
            },
        )
    return len(rows)


async def assess_changes(proposal, changes: AssessmentMaterial):
    if not changes:
        return
    fingerprint = canonical_hash(changes)
    old = proposal.refresh_assessment or {}
    prepared_context = proposal.accepted_context or {}
    context_unchanged = (
        proposal.account is not None
        and prepared_context.get("review", {}).get("verdict") == "ready"
        and prepared_context.get("scope_hash")
        == canonical_hash(
            await asyncio.to_thread(
                snapshot, proposal.user_id, proposal.memory_space_id
            )
        )
    )
    if old.get("change_hash") == fingerprint:
        if context_unchanged and old.get("context_unchanged"):
            return
        if not context_unchanged and (
            not old.get("error") or old.get("attempts", 0) >= 3
        ):
            return
    # Consulted passages are a subset, not the vault snapshot the account saw.
    # Identical complete snapshots establish no new input; real changes still
    # require Pi to assess meaning. Never apply this shortcut to unfinished accounts.
    if context_unchanged:
        result = {
            "verdict": "unrelated",
            "reason": "Accepted knowledge has not changed since this account was prepared; no context refresh is needed.",
            "relevant_paths": [],
            "context_unchanged": True,
        }
        if old.get("error"):
            result["previous_failure"] = {
                key: old[key]
                for key in ("operation", "artifact_hash", "error")
                if key in old
            }
    else:
        result = await assess(proposal, changes)

    await source_search.db().session_context_assessments.update_one(
        {
            "_id": canonical_hash(
                [proposal.proposal_id, proposal.source_scope_hash, fingerprint]
            )
        },
        {
            "$set": {
                "user_id": proposal.user_id,
                "memory_space_id": proposal.memory_space_id,
                "proposal_id": proposal.proposal_id,
                "source_scope_hash": proposal.source_scope_hash,
                "changes": changes,
                "result": result,
                "checked_at": utcnow(),
            }
        },
        upsert=True,
    )
    if (
        result["verdict"] == "unrelated"
        and not context_unchanged
        and old.get("verdict") == "useful"
        and not set(old.get("relevant_paths", [])).intersection(changes)
    ):
        return
    # Do not attach an old assessment to a changed generation or review state.
    await MemoryReviewProposal.get_pymongo_collection().update_one(
        {
            "_id": proposal.id,
            "user_id": proposal.user_id,
            "memory_space_id": proposal.memory_space_id,
            "generation": proposal.generation,
            "source_scope_hash": proposal.source_scope_hash,
            "selection_hash": proposal.selection_hash,
            "excluded_source_keys": proposal.excluded_source_keys,
            "state": proposal.state,
            "active": proposal.active,
            "refresh_assessment": old or None,
        },
        {
            "$set": {
                "refresh_assessment": {
                    **result,
                    "change_hash": fingerprint,
                    "attempts": (
                        old.get("attempts", 0) + 1
                        if old.get("change_hash") == fingerprint
                        else 1
                    ),
                    "checked_at": utcnow().isoformat(),
                }
            }
        },
    )


async def queue_context_assessment(user_id, memory_space_id=None):

    identifier = user_id + "|" + (memory_space_id or "main")
    await asyncio.to_thread(
        sessions._enqueue,
        source_search_jobs.assess_context_job,
        identifier,
        priority=0,
        label="Check whether new memory helps earlier sessions",
    )


async def recover_context_assessments():
    scopes = (
        await MemoryReviewProposal.get_pymongo_collection()
        .aggregate(
            [
                {"$group": {"_id": {"user": "$user_id", "space": "$memory_space_id"}}},
                {"$limit": 100},
            ]
        )
        .to_list()
    )
    for row in scopes:
        await queue_context_assessment(row["_id"]["user"], row["_id"].get("space"))
