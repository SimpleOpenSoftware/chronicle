"""One source-scope contract shared by session and individual episode memory.

Visibility, attribution and a person's memory disposition are independent. In
particular, reference media on one track cannot suppress speech on another track.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone

import backend.models.session_memory as session_memory
from backend.constants import is_non_enrollable_speaker
from backend.services.inference_artifacts import canonical_hash

REFERENCE_ROLES = {"media_content", "ambient", "assistant_generated"}
COVERAGE_KINDS = {"audio_span", "capture_gap"}


def attributed_role(ref):
    """Use retained speaker attribution, never input/output direction as identity.

    Transcript intake labels input as uncertain and output as media. Named speaker
    annotations can resolve speech in a personal capture; a meeting's output may
    contain its other participants. Names remain quoted names, not an assumed user.
    """
    if ref.kind != "transcript" or ref.role not in {"uncertain", "media_content"}:
        return ref.role, "evidence"
    direction = ref.metadata.get("direction")
    if ref.role == "media_content" and not (
        direction == "output" and ref.metadata.get("meeting_id")
    ):
        return ref.role, "evidence"
    if direction == "output" and not ref.metadata.get("meeting_id"):
        return ref.role, "evidence"
    speakers = ref.metadata.get("speakers", [])
    if speakers and all(not is_non_enrollable_speaker(speaker) for speaker in speakers):
        return "third_party", "retained_speaker_attribution"
    if direction == "output" and ref.metadata.get("meeting_id"):
        return "uncertain", "meeting_output"
    return ref.role, "evidence"


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def evidence_sources(episodes):
    """Collect exact source references, then resolve their member episode policies."""
    memberships = defaultdict(list)
    for episode in episodes:
        for ref in episode.evidence_refs:
            source = source_in_episode(ref, episode)
            if source is not None:
                memberships[source["key"]].append((source, episode.memory_policy))
    sources = [combine_source_memberships(members) for members in memberships.values()]
    return sorted(sources, key=lambda source: (source["started_at"], source["key"]))


def source_in_episode(ref, episode):
    """Project a reference into its episode interval without applying memory policy."""
    episode_start, episode_end = utc(episode.started_at), utc(episode.ended_at)
    start = max(utc(ref.started_at), episode_start)
    end = min(utc(ref.ended_at or ref.started_at), episode_end)
    if end < start or (start >= episode_end and episode_end > episode_start):
        return None
    identity = {
        "evidence_id": ref.evidence_id,
        "content_hash": ref.content_hash,
        "locator": ref.locator.model_dump(mode="json"),
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
    }
    role, attribution_origin = attributed_role(ref)
    capture_ranges = [
        capture
        for capture in episode.audio_ranges
        if utc(capture.started_at) <= end
        and utc(capture.ended_at) >= start
        and (
            capture.capture_source_id == ref.locator.capture_source_id
            or ref.metadata.get("conversation_id") in capture.conversation_ids
        )
    ]
    source = {
        **identity,
        "key": canonical_hash(identity),
        "kind": ref.kind,
        "role": role,
        "original_role": ref.role,
        "attribution_origin": attribution_origin,
        "capture_chunk_ids": sorted(
            {chunk for capture in capture_ranges for chunk in capture.chunk_ids}
        ),
        "capture_source_ids": sorted(
            {capture.capture_source_id for capture in capture_ranges}
        ),
        "direction": ref.metadata.get("direction", "unknown"),
        "excerpt": ref.excerpt or "",
        "metadata": deepcopy(ref.metadata),
        "episode_keys": [episode.episode_key],
        # Resolved across all memberships before this source leaves evidence_sources.
        "participation": "supporting",
        "disposition": "auto",
    }
    if episode.status == "open":
        source["ongoing"] = True
    return source


def combine_source_memberships(members):
    """Retain all capture references; conflicting attribution remains unresolved."""
    sources = sorted((source for source, _policy in members), key=canonical_hash)
    source = deepcopy(sources[0])
    for field in ("episode_keys", "capture_chunk_ids", "capture_source_ids"):
        source[field] = sorted({value for member in sources for value in member[field]})
    if any(member.get("ongoing") for member in sources):
        source["ongoing"] = True

    attributions = {}
    meanings = set()
    for member in sources:
        attribution = {
            field: member[field]
            for field in (
                "role",
                "original_role",
                "attribution_origin",
                "direction",
                "metadata",
            )
        }
        attributions[canonical_hash(attribution)] = attribution
        meanings.add(
            canonical_hash(
                {
                    "role": member["role"],
                    "direction": member["direction"],
                    "speakers": sorted(member["metadata"].get("speakers", [])),
                }
            )
        )
    if len(attributions) > 1:
        source["metadata"]["attribution_variants"] = [
            attributions[key] for key in sorted(attributions)
        ]
    if len(meanings) > 1:
        source["role"] = "uncertain"
        source["original_role"] = "uncertain"
        source["attribution_origin"] = "conflicting_episode_attribution"
        source["metadata"]["speakers"] = []
        if len({member["direction"] for member in sources}) > 1:
            source["direction"] = "unknown"
    policies = {policy for _source, policy in members}
    source["participation"] = source_participation(source, policies)
    return source


def source_participation(source, policies):
    """An excluded member protects shared evidence until an explicit source decision.

    Opt-in remembers content as attributed. It cannot resolve conflicting speakers
    or turn capture coverage into a personal activity.
    """
    if "reference" in policies:
        return "excluded"
    if source["kind"] in COVERAGE_KINDS:
        return "background"
    if source["role"] == "uncertain":
        return "uncertain"
    if "remember" in policies:
        return "supporting"
    if source["role"] in REFERENCE_ROLES:
        return "background"
    return "supporting"


def same_evidence(decided, source):
    physical = bool(
        set(decided["capture_chunk_ids"]) & set(source["capture_chunk_ids"])
    )
    return physical or (
        decided["evidence_id"] == source["evidence_id"]
        and decided["locator"] == source["locator"]
    )


def covers(decided, source):
    if source["locator"]["modality"] == "photo" and same_evidence(decided, source):
        return True
    return (
        same_evidence(decided, source)
        and utc(decided["started_at"]) <= utc(source["started_at"])
        and utc(decided["ended_at"]) >= utc(source["ended_at"])
    )


def overlaps(decided, source):
    """An unpartitioned excerpt must not leak a previously excluded portion."""
    return (
        same_evidence(decided, source)
        and utc(decided["started_at"]) <= utc(source["ended_at"])
        and utc(decided["ended_at"]) >= utc(source["started_at"])
    )


def apply_dispositions(sources, decisions, excluded_keys=()):
    # Rebuild derived clarification rows when projecting an already-resolved scope.
    # They must not become their own parents or be appended twice on refresh.
    clarification_keys = {
        canonical_hash(f"user-clarification:{decision.id}")
        for decision in decisions
        if decision.action == "clarify"
    }
    sources = [source for source in sources if source["key"] not in clarification_keys]
    for decision in sorted(decisions, key=lambda item: utc(item.created_at)):
        if decision.action != "clarify":
            continue
        matched = [
            s for s in sources if any(overlaps(item, s) for item in decision.sources)
        ]
        if not matched:
            continue
        identity = f"user-clarification:{decision.id}"
        sources.append(
            {
                "key": canonical_hash(identity),
                "evidence_id": identity,
                "content_hash": canonical_hash(decision.clarification),
                "locator": {
                    "capture_source_id": "user",
                    "modality": "context",
                    "track_id": None,
                },
                "started_at": min(s["started_at"] for s in matched),
                "ended_at": max(s["ended_at"] for s in matched),
                "role": "user_statement",
                "original_role": "user_statement",
                "attribution_origin": "user_clarification",
                "kind": "annotation",
                "capture_chunk_ids": [],
                "capture_source_ids": [],
                "direction": "unknown",
                "excerpt": decision.clarification,
                "metadata": {"recorded_at": utc(decision.created_at).isoformat()},
                "episode_keys": sorted(
                    {key for s in matched for key in s["episode_keys"]}
                ),
                "parent_evidence_ids": sorted({s["evidence_id"] for s in matched}),
                "participation": "supporting",
                "disposition": "auto",
            }
        )
    result = []
    for original in sources:
        source = dict(original)
        for decision in sorted(decisions, key=lambda item: utc(item.created_at)):
            if decision.action == "clarify":
                continue
            matched = any(covers(item, source) for item in decision.sources)
            partial_exclusion = decision.action == "exclude" and any(
                overlaps(item, source) for item in decision.sources
            )
            if matched or partial_exclusion:
                if decision.action in {"defer", "resume"}:
                    source["deferred"] = decision.action == "defer"
                    continue
                if decision.action == "attribute":
                    source["role"] = decision.role
                    source["attribution_origin"] = "user_decision"
                    # Attribution resolves who spoke; it does not opt excluded
                    # source material back into memory.
                    if source["participation"] != "excluded":
                        source["participation"] = (
                            "background"
                            if decision.role == "media_content"
                            else "supporting"
                        )
                    continue
                source["disposition"] = decision.action
                if decision.action == "exclude":
                    source["participation"] = "excluded"
                    if not matched:
                        source["scope_note"] = (
                            "Contains previously excluded evidence; this excerpt cannot be separated precisely."
                        )
                elif decision.action == "include":
                    source["participation"] = (
                        "background"
                        if source["kind"] in COVERAGE_KINDS
                        else (
                            "uncertain"
                            if source["role"] == "uncertain"
                            else "supporting"
                        )
                    )
        if source["key"] in excluded_keys:
            source["participation"] = "excluded"
        result.append(source)
    return result


async def source_decisions(user_id, sources, memory_space_id=None):

    ids = sorted({s["evidence_id"] for s in sources})
    chunks = sorted({chunk for s in sources for chunk in s["capture_chunk_ids"]})
    rows = {}
    for start in range(0, len(ids), 500):
        for row in await session_memory.MemorySourceDecision.find(
            {
                "user_id": user_id,
                "memory_space_id": memory_space_id,
                "$or": [
                    {"sources.evidence_id": {"$in": ids[start : start + 500]}},
                    {"sources.parent_evidence_ids": {"$in": ids[start : start + 500]}},
                ],
            }
        ).to_list():
            rows[str(row.id)] = row
    for start in range(0, len(chunks), 500):
        for row in await session_memory.MemorySourceDecision.find(
            {
                "user_id": user_id,
                "memory_space_id": memory_space_id,
                "sources.capture_chunk_ids": {"$in": chunks[start : start + 500]},
            }
        ).to_list():
            rows[str(row.id)] = row
    return list(rows.values())


def scope_hash(sources):
    return canonical_hash(
        [{k: v for k, v in s.items() if k != "deferred"} for s in sources]
    )


def scope_questions(sources):
    return (
        [
            "Some speech cannot yet be attributed. Review the marked source before using its claims."
        ]
        if any(
            s["participation"] == "uncertain" and s["excerpt"].strip() for s in sources
        )
        else []
    )


def memory_sources(sources):
    return [s for s in sources if s["participation"] == "supporting"]


def account_sources(sources):
    """Read unresolved speech to assess usefulness, without authorizing its claims."""
    return [s for s in sources if s["participation"] in {"supporting", "uncertain"}]
