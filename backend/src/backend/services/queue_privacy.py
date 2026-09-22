"""Privacy at queue monitoring boundaries; raw RQ data is not a safe preview."""

from __future__ import annotations

import asyncio

from backend.services import privacy

# These registered workers own a conversation supplied as their first argument.
# Unknown positional contracts are not guessed from arbitrary text or job IDs.
_CONVERSATION_FIRST = {
    "backend.workers.transcription_jobs.transcribe_full_audio_job",
    "backend.workers.memory_jobs.process_memory_job",
    "backend.workers.speaker_jobs.recognise_speakers_job",
}
_IDS = {"conversation_id", "recording_id"}
_ID_LISTS = {
    "conversation_ids",
    "related_conversation_ids",
    "evidence_conversation_ids",
}
_CONTEXT = {
    "proposal_id",
    "episode_id",
    "episode_key",
    "session_key",
    "note_path",
    "path",
    "episode_ids",
    "episode_keys",
    "source_episode_keys",
    "note_paths",
    "accepted_note_paths",
}
_SOURCE = {
    "source_id",
    "capture_source_id",
    "client_id",
    "source_ids",
    "started_at",
    "ended_at",
    "captured_at",
    "created_at",
    "audio_ranges",
    "evidence_refs",
    "locator",
}
_LABEL = "Private or unscreened job details held"
_SAFE = {
    "job_id",
    "id",
    "job_type",
    "func_name",
    "queue",
    "status",
    "priority",
    "created_at",
    "started_at",
    "ended_at",
    "completed_at",
    "retry_count",
    "max_retries",
    "progress_percent",
    "timestamp",
    "event_type",
    "type",
    "event",
}


def _evidence(row):
    references, context, sources = set(), [], []

    def visit(value):
        if isinstance(value, dict):
            item = {
                key: child
                for key, child in value.items()
                if key in _IDS | _ID_LISTS | _CONTEXT
            }
            if item:
                context.append(item)
            for key in _IDS:
                if isinstance(value.get(key), str):
                    references.add(value[key])
            for key in _ID_LISTS:
                if isinstance(value.get(key), (list, tuple)):
                    references.update(v for v in value[key] if isinstance(v, str))
            if any(
                value.get(k)
                for k in ("source_id", "capture_source_id", "client_id", "source_ids")
            ) and any(
                value.get(k) for k in ("started_at", "captured_at", "created_at")
            ):
                source = {key: child for key, child in value.items() if key in _SOURCE}
                if source.get("capture_source_id") and not source.get("source_id"):
                    source["source_id"] = source["capture_source_id"]
                sources.append(source)
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for key in (
        "args",
        "kwargs",
        "meta",
        "metadata",
        "plugins_executed",
        "result",
        "data",
        "_privacy_payload",
    ):
        visit(row.get(key))
    proof = row.get("_privacy_payload") or row
    args = proof.get("args") or ()
    if (
        proof.get("func_name") in _CONVERSATION_FIRST
        and args
        and isinstance(args[0], str)
    ):
        references.add(args[0])
        context.append({"conversation_id": args[0]})
    return references, context, sources


def _held(row, *, event=False):
    result = {key: value for key, value in row.items() if key in _SAFE}
    result.update(privacy_held=True, description=_LABEL, data={"description": _LABEL})
    if not event:
        result.update(
            args=[],
            kwargs={},
            meta={},
            result=None,
            error_message=None,
            progress_message=_LABEL,
        )
    else:
        result.update(metadata={}, plugins_executed=[], plugins_subscribed=[])
    return result


class QueuePrivacyFilter:
    """One retained policy boundary for a complete queue response."""

    def __init__(self):
        self.visibility = privacy.ConversationPrivacyFilter()
        self.has_visible_payload = False

    async def project(self, rows, *, default_owner=None, event=False):
        extracted = await asyncio.to_thread(lambda: [_evidence(row) for row in rows])
        all_ids = {identifier for refs, _, _ in extracted for identifier in refs}
        allowed = {
            row["conversation_id"]
            for row in await self.visibility.filter(
                [{"conversation_id": identifier} for identifier in all_ids]
            )
        }
        projected = []
        for row, (refs, context, sources) in zip(rows, extracted):
            proof = row.get("_privacy_payload") or row
            kwargs, meta = proof.get("kwargs") or {}, proof.get("meta") or {}
            owner = (
                row.get("user_id")
                or kwargs.get("user_id")
                or kwargs.get("requested_by")
                or meta.get("user_id")
            )
            owner = str(owner or default_owner) if owner or default_owner else None
            held = bool(refs - allowed) or not (refs or sources)
            try:
                if owner and not held:
                    if owner not in self.visibility.snapshots:
                        self.visibility.snapshots[owner] = await privacy.load_snapshot(
                            owner
                        )
                    snapshot = self.visibility.snapshots[owner]
                    await privacy.guard_payload(
                        owner, context + sources, snapshot=snapshot
                    )
                elif not refs:
                    held = True
                # Resolve direct source evidence by its capture owner, not the observer.
                source_ids = {
                    str(
                        s.get("source_id")
                        or s.get("capture_source_id")
                        or s.get("client_id")
                    ).split(":", 1)[0]
                    for s in sources
                    if s.get("source_id")
                    or s.get("capture_source_id")
                    or s.get("client_id")
                }
                source_ids.update(
                    str(identifier).split(":", 1)[0]
                    for source in sources
                    for identifier in source.get("source_ids", [])
                )
                if source_ids:
                    owners = (
                        await privacy.database()
                        .capture_sources.find(
                            {"source_id": {"$in": list(source_ids)}},
                            {"user_id": 1, "source_id": 1},
                        )
                        .to_list(length=None)
                    )
                    if source_ids - {s["source_id"] for s in owners}:
                        held = True
                    for source_owner in {str(s["user_id"]) for s in owners}:
                        if source_owner not in self.visibility.snapshots:
                            self.visibility.snapshots[source_owner] = (
                                await privacy.load_snapshot(source_owner)
                            )
                        snapshot = self.visibility.snapshots[source_owner]
                        if any(
                            not snapshot.permits_record(source) for source in sources
                        ):
                            held = True
            except (privacy.PrivacyHeld, ValueError, TypeError, KeyError):
                held = True
            projected.append(
                _held(row, event=event)
                if held
                else {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("_privacy_")
                }
            )
            self.has_visible_payload |= not held
        await self.assert_current()
        return projected

    async def assert_current(self):
        if self.has_visible_payload:
            await self.visibility.assert_current()
