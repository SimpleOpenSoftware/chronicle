"""Revision-aware recording/session retrieval over a disposable Mongo projection.

The index is populated by bounded, resumable jobs. Queries never load transcript
versions/word timings and never run user-supplied regular expressions.
"""

import asyncio
import datetime as datetime_module
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import backend.models.timeline as timeline
import backend.services.timeline.consolidation as consolidation
import backend.services.timeline.memory_sources as memory_sources
import backend.services.timeline.recording_sessions as recording_sessions
import backend.services.timeline.sessions as sessions_module
import backend.workers.source_search_jobs as source_search_jobs
from backend.models.conversation import Conversation
from backend.services import privacy
from backend.services.inference_artifacts import canonical_hash
from backend.services.recording_purpose import (
    is_personal_recording,
    personal_recording_filter,
)

VERSION = 4
FIELDS = {"id", "title", "summary", "speakers", "transcript"}


def db():
    return Conversation.get_pymongo_collection().database


def words(text):
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


def signatures(word):
    yield "w:" + word
    if len(word) >= 5:
        for i in range(len(word)):
            yield "d:" + word[:i] + word[i + 1 :]


def one_edit(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
    return a[i:] == b[i + 1 :]


def strength(term, token):
    if term == token:
        return 3
    if len(term) >= 3 and token.startswith(term):
        return 2
    if len(term) >= 5 and one_edit(term, token):
        return 1
    return 0


def searchable(fields):
    tokens = {w for key, value in fields.items() if key != "id" for w in words(value)}
    keys = set()
    for word in tokens:
        keys.update(signatures(word))
        # Symmetric deletes find substitution, insertion and deletion candidates.
        if len(word) >= 4:
            keys.add("d:" + word)
        keys.update("p:" + word[:i] for i in range(3, min(len(word), 40) + 1))
    identity = fields.get("id", "").casefold()
    keys.update("i:" + identity[i : i + 3] for i in range(max(0, len(identity) - 2)))
    return sorted(keys)


def query_keys(term):
    keys = set(signatures(term))
    if len(term) >= 3:
        keys.add("p:" + term)
        keys.add("i:" + term[:3])
    if len(term) >= 5:
        keys.add("d:" + term)
    return sorted(keys)


def query_condition(term):
    word_keys = [key for key in query_keys(term) if not key.startswith("i:")]
    alternatives = [{"terms": {"$in": word_keys}}]
    if len(term) >= 3:
        alternatives.append(
            {
                "terms": {
                    "$all": sorted(
                        {"i:" + term[i : i + 3] for i in range(len(term) - 2)}
                    )
                }
            }
        )
    return {"$or": alternatives}


def passages(text, segments=()):
    result = []
    rows = segments or [{"text": text}]
    for row in rows:
        content = row.get("text") or ""
        for offset in range(0, len(content), 800):
            item = {"text": content[offset : offset + 800]}
            start, end = row.get("start"), row.get("end")
            if (
                isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and 0 <= start < end
            ):
                item.update(start=start, end=end)
            result.append(item)
    return result


def recording_fields(row):
    active = next(
        (
            v
            for v in row.get("transcript_versions", [])
            if v["version_id"] == row.get("active_transcript_version")
        ),
        {},
    )
    segments = active.get("segments", [])
    text = active.get("transcript") or " ".join(s.get("text", "") for s in segments)
    fields = {
        "id": row["conversation_id"],
        "title": row.get("title") or "Untitled recording",
        "summary": " ".join(
            filter(None, [row.get("summary"), row.get("detailed_summary")])
        ),
        "speakers": " ".join(sorted({s.get("speaker", "") for s in segments})),
        "transcript": text,
    }
    return fields, segments


def recording_projection(row):
    fields, segments = recording_fields(row)
    text = fields["transcript"]
    ranges = row.get("audio_ranges", [])
    dated = [r for r in ranges if r.get("time_basis") != "unknown"]
    return {
        "_id": "recording:" + row["conversation_id"],
        "kind": "recording",
        "key": row["conversation_id"],
        "user_id": row["user_id"],
        "memory_space_id": row.get("memory_space_id"),
        "revision": row.get("active_transcript_version"),
        "fields": {k: v for k, v in fields.items() if k != "transcript"},
        "terms": searchable(fields),
        "passages": passages(text, segments),
        "title": fields["title"],
        "summary": (row.get("summary") or "")[:500],
        "started_at": min((r["started_at"] for r in dated), default=None),
        "uploaded_at": row.get("created_at"),
        "updated_at": row.get("created_at"),
        "duration": row.get("audio_total_duration", 0),
        "url": "/recordings/" + row["conversation_id"],
        "source_hash": canonical_hash(fields),
        "version": VERSION,
    }


def match(row, query, selected):
    terms = words(query)
    fields = {k: v for k, v in row["fields"].items() if k in selected}
    if "transcript" in selected:
        fields["transcript"] = " ".join(p["text"] for p in row.get("passages", []))
    if "id" in selected and query.casefold() in fields.get("id", "").casefold():
        return (10000 if query.casefold() == fields["id"].casefold() else 9000), []
    vocabulary = set(words(" ".join(v for k, v in fields.items() if k != "id")))
    scores = [
        (
            3
            if t in fields.get("id", "").casefold()
            else max((strength(t, w) for w in vocabulary), default=0)
        )
        for t in terms
    ]
    if not scores or not all(scores):
        return None
    exact = sum(s == 3 for s in scores)
    phrase = any(query.casefold() in v.casefold() for v in fields.values())
    title = sum(t in words(fields.get("title", "")) for t in terms)
    hits = [w for w in vocabulary if any(strength(t, w) for t in terms)]
    return int(phrase) * 1000 + exact * 50 + sum(scores) * 5 + title, hits


async def ensure_indexes():
    await db().source_search.create_index(
        [("user_id", 1), ("memory_space_id", 1), ("terms", 1)]
    )
    await db().source_search.create_index([("user_id", 1), ("kind", 1)])


def privacy_evidence(records):
    """Retain only source/time provenance, never excerpts or display metadata."""
    result = []
    for record in records:
        row = record if isinstance(record, dict) else vars(record)
        evidence = {
            key: row[key]
            for key in (
                "source_id",
                "source_ids",
                "client_id",
                "started_at",
                "ended_at",
                "created_at",
                "captured_at",
            )
            if key in row
        }
        references = []
        for ref in list(row.get("audio_ranges") or []) + list(
            row.get("evidence_refs") or []
        ):
            ref = ref if isinstance(ref, dict) else vars(ref)
            locator = ref.get("locator") or {}
            locator = locator if isinstance(locator, dict) else vars(locator)
            references.append(
                {
                    "source_id": ref.get("capture_source_id")
                    or ref.get("source_id")
                    or locator.get("capture_source_id"),
                    "started_at": ref.get("started_at"),
                    "ended_at": ref.get("ended_at"),
                }
            )
        evidence["evidence_refs"] = references
        result.append(evidence)
    return result


def projection_recordings(row):
    identifiers = set(row.get("recording_revisions") or {})
    if row["kind"] == "recording":
        identifiers.add(row["key"])
    elif row.get("recording_id"):
        identifiers.add(row["recording_id"])
    return identifiers


async def projection_visible(row, visibility, *, allowed_recordings=None):
    owner = str(row["user_id"])
    if owner not in visibility.snapshots:
        visibility.snapshots[owner] = await privacy.load_snapshot(owner)
    identifiers = projection_recordings(row)
    if identifiers:
        if allowed_recordings is None:
            references = [{"conversation_id": key} for key in identifiers]
            allowed_recordings = {
                r["conversation_id"] for r in await visibility.filter(references)
            }
        if not identifiers <= allowed_recordings:
            return False
    evidence = row.get("privacy_evidence") or []
    snapshot = visibility.snapshots[owner]
    if not identifiers and not evidence and snapshot.sources:
        return False
    if any(not snapshot.permits_record(ref) for ref in evidence):
        return False
    return True


async def publish_projection(row, visibility):
    # May run inside Conversation.save's existing publication lock. Do not
    # acquire it recursively; remove an in-flight projection if its policy moved.
    try:
        await visibility.assert_current()
        await db().source_search.replace_one({"_id": row["_id"]}, row, upsert=True)
        await visibility.assert_current()
    except privacy.PrivacyHeld:
        await db().source_search.delete_one({"_id": row["_id"]})
        raise


async def index_recording(identifier, *, visibility=None):
    row = await db().conversations.find_one(
        {"conversation_id": identifier},
        {"transcript_versions.words": 0, "transcript_versions.segments.words": 0},
    )
    visibility = visibility or privacy.ConversationPrivacyFilter()
    eligible = row is not None and not row.get("deleted") and is_personal_recording(row)
    allowed = eligible and bool(await visibility.filter([row]))
    if not allowed:
        await db().source_search.delete_one({"_id": "recording:" + identifier})
        await db().source_search.delete_many(
            {"kind": "session", "recording_id": identifier}
        )
        return False if eligible else None
    await visibility.assert_current()
    projected = await asyncio.to_thread(recording_projection, row)
    await publish_projection(projected, visibility)
    return True


async def index_day(day, *, visibility=None):

    if day.pending_publication_id or not day.current_snapshot:
        return
    visibility = visibility or privacy.ConversationPrivacyFilter()
    if str(day.user_id) not in visibility.snapshots:
        visibility.snapshots[str(day.user_id)] = await privacy.load_snapshot(
            day.user_id
        )
    try:
        episodes = await consolidation.snapshot_episodes(day)
    except privacy.PrivacyHeld:
        # This day's aggregate cannot be rebuilt from its held snapshot. Remove
        # its disposable projections and let recovery advance to independent
        # days; later scans can rebuild it after evidence becomes eligible.
        await db().source_search.delete_many(
            {
                "user_id": day.user_id,
                "memory_space_id": None,
                "owner_date": day.local_date.isoformat(),
                "timezone": day.timezone,
                "kind": {"$in": ["episode", "session"]},
            }
        )
        return False
    sessions = await sessions_module.resolved_sessions(day, episodes)
    sources_to_index = [
        ("session", owner, group.group_key, group, members)
        for owner, group, members in sessions
    ]
    sources_to_index.extend(
        ("episode", day, episode.episode_id, episode, [episode])
        for episode in episodes
        if episode.status != "superseded"
    )
    held = False
    for kind, owner, key, group, members in sources_to_index:
        evidence = privacy_evidence(members)
        references = {
            identifier
            for member in members
            for identifier in member.related_conversation_ids
        }
        check = {
            "user_id": day.user_id,
            "kind": kind,
            "key": key,
            "privacy_evidence": evidence,
            "recording_revisions": dict.fromkeys(references),
        }
        if not await projection_visible(check, visibility):
            await db().source_search.delete_one({"_id": kind + ":" + key})
            held = True
            continue
        await visibility.assert_current()
        sources = memory_sources.evidence_sources(members)
        ids = references | {
            s.get("metadata", {}).get("conversation_id")
            for s in sources
            if s["kind"] == "transcript"
        }
        recordings = (
            await db()
            .conversations.find(
                {
                    "conversation_id": {"$in": list(ids - {None})},
                    "user_id": day.user_id,
                    "memory_space_id": None,
                    "deleted": False,
                    **personal_recording_filter(),
                },
                {
                    "transcript_versions.words": 0,
                    "transcript_versions.segments.words": 0,
                },
            )
            .to_list()
        )
        if ids - {None} - {r["conversation_id"] for r in recordings}:
            await db().source_search.delete_one({"_id": kind + ":" + key})
            held = True
            continue
        if len(await visibility.filter(recordings)) != len(recordings):
            await db().source_search.delete_one({"_id": kind + ":" + key})
            held = True
            continue
        await visibility.assert_current()
        transcripts = {
            r["conversation_id"]: (
                r["active_transcript_version"],
                " ".join(words(recording_fields(r)[0]["transcript"])),
            )
            for r in recordings
        }
        current_sources = [
            s
            for s in sources
            if s["kind"] == "transcript"
            and s.get("metadata", {}).get("conversation_id") in transcripts
            and " ".join(
                words(
                    re.sub(r"^[^:\n]{1,100}:\s*", "", s["excerpt"], flags=re.MULTILINE)
                )
            )
            in transcripts[s["metadata"]["conversation_id"]][1]
        ]
        fields = {
            "id": key,
            "title": group.title,
            "summary": group.summary,
            "speakers": " ".join(
                s for r in sources for s in r.get("metadata", {}).get("speakers", [])
            ),
            "transcript": " ".join(s["excerpt"] for s in current_sources),
        }
        row = {
            "_id": kind + ":" + key,
            "key": key,
            "kind": kind,
            "user_id": day.user_id,
            "memory_space_id": None,
            "revision": group.revision,
            "owner_date": owner.local_date.isoformat(),
            "timezone": owner.timezone,
            "fields": {k: v for k, v in fields.items() if k != "transcript"},
            "terms": await asyncio.to_thread(searchable, fields),
            "passages": passages(fields["transcript"]),
            "title": group.title,
            "summary": group.summary[:500],
            "started_at": group.started_at,
            "updated_at": owner.revised_at,
            "duration": (group.ended_at - group.started_at).total_seconds(),
            "url": (
                "/timeline/" + key
                if kind == "episode"
                else "/timeline?"
                + urlencode({"date": owner.local_date.isoformat(), "session": key})
            ),
            "source_hash": canonical_hash(fields),
            "recording_revisions": {
                key: value[0] for key, value in transcripts.items()
            },
            "privacy_evidence": evidence,
            "version": VERSION,
        }
        await publish_projection(row, visibility)
    await visibility.assert_current()
    return not held


async def search(
    user_id, query, *, kinds, fields, limit=20, offset=0, memory_space_id=None
):
    terms = words(query)
    if (query.strip() and not terms) or len(terms) > 12 or len(query) > 200:
        return {"items": [], "total": 0, "indexing": await index_status(user_id)}
    criteria = {
        "user_id": user_id,
        "memory_space_id": memory_space_id,
        "kind": {"$in": kinds},
        "version": VERSION,
        **({"$and": [query_condition(t) for t in terms]} if terms else {}),
    }
    rows = await db().source_search.find(criteria).to_list()
    visibility = privacy.ConversationPrivacyFilter()
    references = [
        {"conversation_id": identifier}
        for identifier in {
            identifier for row in rows for identifier in projection_recordings(row)
        }
    ]
    permitted = {row["conversation_id"] for row in await visibility.filter(references)}
    visible = []
    for row in rows:
        if await projection_visible(row, visibility, allowed_recordings=permitted):
            visible.append(row)
    await visibility.assert_current()
    rows = visible
    ranked = []
    session_cache = {}
    matched = await asyncio.to_thread(
        lambda: [
            (row, found)
            for row in rows
            if (found := (match(row, query, fields) if terms else (0, [])))
        ]
    )
    identifiers = list(
        {
            row["key"] if row["kind"] == "recording" else row["recording_id"]
            for row, _ in matched
            if row["kind"] == "recording" or row.get("undated")
        }
    )
    recordings = {}
    for i in range(0, len(identifiers), 500):
        current = (
            await db()
            .conversations.find(
                {
                    "conversation_id": {"$in": identifiers[i : i + 500]},
                    "user_id": user_id,
                    "memory_space_id": memory_space_id,
                    "deleted": False,
                    **personal_recording_filter(),
                },
                {
                    "transcript_versions.words": 0,
                    "transcript_versions.segments.words": 0,
                },
            )
            .to_list()
        )
        recordings.update({r["conversation_id"]: r for r in current})
    for i, (row, found) in enumerate(matched):
        if i % 100 == 0:
            await asyncio.sleep(0)
            await visibility.assert_current()
        if await current_hit(
            row,
            user_id,
            memory_space_id,
            session_cache,
            recordings,
            visibility=visibility,
        ):
            ranked.append(
                (
                    found[0],
                    str(
                        row.get("updated_at")
                        if terms
                        else row.get("started_at") or row.get("uploaded_at") or ""
                    ),
                    row,
                    found[1],
                )
            )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]["_id"]), reverse=True)
    items = []
    for score, _, row, hits in ranked[offset : offset + limit]:
        # An index hit is only a locator; current source state authorizes disclosure.
        if row["kind"] == "recording":
            current = await db().conversations.find_one(
                {
                    "conversation_id": row["key"],
                    "user_id": user_id,
                    "memory_space_id": memory_space_id,
                    "deleted": False,
                    **personal_recording_filter(),
                },
                {"active_transcript_version": 1},
            )
            if (
                current is None
                or current.get("active_transcript_version") != row["revision"]
            ):
                await index_recording(row["key"])
                continue
        excerpt = max(
            row.get("passages", []),
            key=lambda p: sum(
                any(strength(t, w) for w in words(p["text"])) for t in terms
            ),
            default={"text": row["summary"]},
        )
        item = {
            k: row.get(k)
            for k in (
                "kind",
                "key",
                "revision",
                "title",
                "summary",
                "started_at",
                "uploaded_at",
                "duration",
                "url",
                "owner_date",
                "timezone",
            )
        }
        recording = recordings.get(
            row["key"] if row["kind"] == "recording" else row.get("recording_id"), {}
        )
        _, segments = recording_fields(recording) if recording else ({}, [])
        participants = sorted(
            {
                name
                for segment in segments
                if (name := segment.get("identified_as"))
                and not name.casefold().startswith("unknown speaker")
            }
        )
        item.update(
            participants=participants,
            excerpt=excerpt["text"],
            highlights=hits,
            match_start=excerpt.get("start"),
            match_end=excerpt.get("end"),
        )
        items.append(item)
    indexing = await index_status(user_id)
    await visibility.assert_current()
    return {
        "items": items,
        "total": len(ranked),
        "offset": offset,
        "limit": limit,
        "indexing": indexing,
    }


async def current_hit(
    row, user_id, space, session_cache, recordings, *, visibility=None
):
    """A search projection cannot authorize obsolete or reassigned source text."""
    # search() has already filtered the whole batch before ranking. Standalone
    # callers establish that same boundary here.
    if visibility is None:
        visibility = privacy.ConversationPrivacyFilter()
        if not await projection_visible(row, visibility):
            return False
        await visibility.assert_current()
    if row["kind"] == "recording" or row.get("undated"):
        identifier = row["key"] if row["kind"] == "recording" else row["recording_id"]
        current = recordings.get(identifier)
        if current is None:
            await db().source_search.delete_one({"_id": row["_id"]})
            return False
        if row["kind"] == "recording":
            if (
                canonical_hash(recording_fields(current)[0]) != row["source_hash"]
                or current.get("active_transcript_version") != row["revision"]
            ):
                await index_recording(identifier)
                return False
            return True

        full = await Conversation.find_one(
            {
                "conversation_id": identifier,
                "user_id": user_id,
                "memory_space_id": space,
                "deleted": False,
                **personal_recording_filter(),
            }
        )
        return (
            full is not None
            and recording_sessions.recording_hash(full) == row["source_hash"]
        )
    revisions = row["recording_revisions"]
    if revisions:
        current = (
            await db()
            .conversations.find(
                {
                    "conversation_id": {"$in": list(revisions)},
                    "user_id": user_id,
                    "memory_space_id": space,
                    "deleted": False,
                    **personal_recording_filter(),
                },
                {"conversation_id": 1, "active_transcript_version": 1},
            )
            .to_list()
        )
        if {
            r["conversation_id"]: r.get("active_transcript_version") for r in current
        } != revisions:
            return False

    key = (row["owner_date"], row["timezone"])
    if key not in session_cache:
        day = await timeline.TimelineDay.find_one(
            timeline.TimelineDay.user_id == user_id,
            timeline.TimelineDay.local_date
            == datetime_module.date.fromisoformat(key[0]),
            timeline.TimelineDay.timezone == key[1],
        )
        current = {}
        if day is not None and not day.pending_publication_id and day.current_snapshot:
            episodes = await consolidation.snapshot_episodes(day)
            current.update(
                {
                    ("episode", e.episode_id): e.revision
                    for e in episodes
                    if e.status != "superseded"
                }
            )
            current.update(
                {
                    ("session", g.group_key): g.revision
                    for _, g, _ in await sessions_module.resolved_sessions(
                        day, episodes
                    )
                }
            )
        session_cache[key] = current
    return session_cache[key].get((row["kind"], row["key"])) == row["revision"]


async def index_status(user_id):
    state = await db().source_search_jobs.find_one({"_id": "recovery"}, {"_id": 0})
    if state and state.get("version") != VERSION:
        return {"state": "pending", "completed": 0, "initialized": False}
    return (
        {
            k: state[k]
            for k in (
                "state",
                "completed",
                "initialized",
                "error",
                "updated_at",
                "attempts",
                "privacy_held",
                "privacy_waiting",
            )
            if k in state
        }
        if state
        else {"state": "pending", "completed": 0}
    )


async def recover_search_index():
    """Registered cron only queues work; scanning/indexing belongs to a worker."""

    await asyncio.to_thread(
        sessions_module._enqueue,
        source_search_jobs.index_sources_job,
        "recovery",
        priority=0,
        label="Update recording and session search",
    )
