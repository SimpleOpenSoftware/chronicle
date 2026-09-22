"""Recorder for the memory vault audit ledger, plus the provenance taxonomy.

Memory is a per-user markdown vault that is overwritten in place rather than
versioned, so instead of keeping prior copies we record *which notes changed,
when, and why* into the ``memory_audit`` collection
(see :class:`backend.models.memory_audit.MemoryAuditEntry`).

Provenance is modelled along two **independent** axes so the ledger can answer
"who caused this and why" honestly:

* :class:`MemoryCause` — *why* the memory changed (a new conversation, a manual
  replay, a transcript/speaker reprocess, an applied annotation, an inbound
  Obsidian edit, a bulk delete). This is descriptive metadata for the ledger.
* :class:`UpdateStrategy` — *how* the provider updates the vault (a full
  re-extraction vs. a targeted speaker-diff update). This is pure control flow
  for the memory job; it is recorded for transparency but never used as a label.

Keeping these apart is deliberate: e.g. "speaker reprocess" and "diarization
annotation applied" are the *same* operation (speaker attribution changed) and
both use :attr:`UpdateStrategy.SPEAKER_DIFF`, yet they carry different causes.
The display label and ``actor`` are derived from these via the helpers below, so
the WebUI does not have to special-case magic strings.

Cause/strategy are known by the memory job, not the provider, so they are passed
down through a contextvar set with :func:`memory_provenance` around the provider
call — the provider runs in the same coroutine, so the values are visible
without threading them through the shared ``add_memory`` signature.

Only the chronicle provider owns a vault, so it is the only caller. Recording is
best-effort: a ledger write must never break memory processing.
"""

import contextlib
import contextvars
import difflib
import hashlib
import logging
from enum import Enum
from typing import Any, Iterator, NamedTuple, Optional

from bson import ObjectId
from opentelemetry import trace
from pymongo.errors import DuplicateKeyError

from backend.models.memory_audit import MemoryAuditEntry

logger = logging.getLogger("memory_service.audit")


class MemoryCause(str, Enum):
    """*Why* a memory vault change happened (descriptive provenance)."""

    AUTO_EXTRACTION = "auto_extraction"  # automatic post-conversation pipeline
    DAY_EPISODES = "day_episodes"  # settled-day timeline episodes (capture evidence)
    MEMORY_REPLAY = "memory_replay"  # manual re-extract, same inputs
    CHAT_REVIEW = "chat_review"  # explicitly approved whole-chat note changes
    MEMORY_REBUILD = "memory_rebuild"  # clean vault replay from durable transcripts
    TRANSCRIPT_REPROCESS = "transcript_reprocess"  # re-ran ASR
    SPEAKER_REPROCESS = "speaker_reprocess"  # re-ran diarization
    ANNOTATION_APPLY = "annotation_apply"  # user applied annotation corrections
    OBSIDIAN_SYNC = "obsidian_sync"  # inbound human edit via Syncthing
    OBSIDIAN_ACTION = "obsidian_action"  # explicit semantic action from Obsidian
    DELETE_ALL = "delete_all"  # bulk vault wipe


class UpdateStrategy(str, Enum):
    """*How* the provider updates the vault (control flow, not a label)."""

    FULL = "full"  # full re-extraction via add_memory
    SPEAKER_DIFF = "speaker_diff"  # targeted diff update via reprocess_memory


# Coarse UI grouping used for icon/colour/filtering. The precise cause is carried
# separately by the human-readable label, so several causes share one kind.
_CAUSE_KIND = {
    MemoryCause.AUTO_EXTRACTION: "extraction",
    MemoryCause.DAY_EPISODES: "extraction",
    MemoryCause.MEMORY_REPLAY: "reprocess",
    MemoryCause.CHAT_REVIEW: "extraction",
    MemoryCause.MEMORY_REBUILD: "reprocess",
    MemoryCause.TRANSCRIPT_REPROCESS: "reprocess",
    MemoryCause.SPEAKER_REPROCESS: "reprocess",
    MemoryCause.ANNOTATION_APPLY: "reprocess",
    MemoryCause.OBSIDIAN_SYNC: "human",
    MemoryCause.OBSIDIAN_ACTION: "human",
    MemoryCause.DELETE_ALL: "bulk",
}

_CAUSE_LABEL = {
    MemoryCause.AUTO_EXTRACTION: "AI extraction",
    MemoryCause.DAY_EPISODES: "Day episodes",
    MemoryCause.MEMORY_REPLAY: "Memory replay",
    MemoryCause.CHAT_REVIEW: "Reviewed chat",
    MemoryCause.MEMORY_REBUILD: "Memory rebuild",
    MemoryCause.TRANSCRIPT_REPROCESS: "Transcript reprocess",
    MemoryCause.SPEAKER_REPROCESS: "Speaker reprocess",
    MemoryCause.ANNOTATION_APPLY: "Annotation applied",
    MemoryCause.OBSIDIAN_SYNC: "Human · Obsidian",
    MemoryCause.OBSIDIAN_ACTION: "Obsidian action",
    MemoryCause.DELETE_ALL: "Bulk delete",
}


def _as_cause(cause: Optional[str]) -> Optional[MemoryCause]:
    try:
        return MemoryCause(cause) if cause is not None else None
    except ValueError:
        return None


def source_kind_for(
    cause: Optional[str], agent_mode: bool, operation: Optional[str]
) -> str:
    """Coarse provenance bucket for UI colour/icon/filtering.

    Order matters: a vault-wide delete and the autonomous memory agent are
    classified by *how* they ran regardless of the nominal cause.
    """
    if operation == "delete_all":
        return "bulk"
    if agent_mode:
        return "agent"
    c = _as_cause(cause)
    if c is None:
        return "other"
    return _CAUSE_KIND.get(c, "other")


def source_label_for(
    cause: Optional[str], agent_mode: bool, operation: Optional[str]
) -> str:
    """Human-readable provenance label shown in the ledger chip."""
    if operation == "delete_all":
        return "Bulk delete"
    if agent_mode:
        return "Memory agent"
    c = _as_cause(cause)
    if c is None:
        return cause or "system"
    return _CAUSE_LABEL.get(c, c.value)


def actor_for(cause: Optional[str], agent_mode: bool, operation: Optional[str]) -> str:
    """Who caused the change: system | user | human_external | agent."""
    if agent_mode:
        return "agent"
    c = _as_cause(cause)
    if c in (MemoryCause.OBSIDIAN_SYNC, MemoryCause.OBSIDIAN_ACTION):
        return "human_external"
    if c == MemoryCause.AUTO_EXTRACTION:
        return "system"
    # Every manual reprocess/replay/annotation/delete is a user action.
    return "user"


class _Provenance(NamedTuple):
    cause: Optional[str]
    strategy: Optional[str]
    source_type: Optional[str]
    source_id: Optional[str]
    source_conversation_ids: tuple[str, ...]
    source_episode_ids: tuple[str, ...]
    timeline_run_id: Optional[str]


_current_provenance: contextvars.ContextVar[_Provenance] = contextvars.ContextVar(
    "memory_audit_provenance",
    default=_Provenance(None, None, None, None, (), (), None),
)

_audit_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "memory_audit_suppressed", default=False
)


@contextlib.contextmanager
def suppress_memory_audit() -> Iterator[None]:
    """Suppress ledger writes while an agent edits an isolated review vault."""

    token = _audit_suppressed.set(True)
    try:
        yield
    finally:
        _audit_suppressed.reset(token)


@contextlib.contextmanager
def memory_provenance(
    cause: Optional[str],
    strategy: Optional[str] = None,
    *,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    source_conversation_ids: tuple[str, ...] = (),
    source_episode_ids: tuple[str, ...] = (),
    timeline_run_id: Optional[str] = None,
) -> Iterator[None]:
    """Set typed provenance recorded by vault changes within this block.

    ``cause`` and ``strategy`` may be :class:`MemoryCause`/:class:`UpdateStrategy`
    members or their string values; both are normalised to plain strings.
    """
    token = _current_provenance.set(
        _Provenance(
            cause.value if isinstance(cause, Enum) else cause,
            strategy.value if isinstance(strategy, Enum) else strategy,
            source_type,
            source_id,
            tuple(dict.fromkeys(str(item) for item in source_conversation_ids if item)),
            tuple(dict.fromkeys(str(item) for item in source_episode_ids if item)),
            timeline_run_id,
        )
    )
    try:
        yield
    finally:
        _current_provenance.reset(token)


def _sha256(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_delta_summary(before: Optional[str], after: Optional[str]) -> Optional[str]:
    """A short, human-readable description of how a note changed."""
    if before is None and after is not None:
        return f"created ({len(after.splitlines())} lines)"
    if after is None and before is not None:
        return "deleted"
    if before is None or after is None:
        return None
    if before == after:
        return "no change"
    added = removed = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return f"+{added}/-{removed} lines"


def _active_trace_context() -> dict[str, str]:
    """IDs that join an exact vault path to its Langfuse/OpenTelemetry trace."""

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "otel_trace_id": f"{context.trace_id:032x}",
        "otel_span_id": f"{context.span_id:016x}",
    }


async def record_vault_change(
    *,
    user_id: str,
    memory_space_id: Optional[str] = None,
    operation: str,
    conversation_id: Optional[str] = None,
    note_path: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    agent_mode: bool = False,
    provider: str = "chronicle",
    summary: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    strict: bool = False,
    **extra: Any,
) -> None:
    """Append one entry to the memory vault audit ledger (best-effort).

    ``before``/``after`` are note contents. ``before`` is used only to derive the
    before-hash and line delta; ``after`` is hashed *and* stored as ``after_text``
    so a before→after diff can later be reconstructed from the note's history.
    Pass ``after=None`` for deletions.
    """
    if _audit_suppressed.get():
        return
    provenance = _current_provenance.get()
    provenance_extra: dict[str, Any] = {}
    if provenance.source_type:
        provenance_extra["source_type"] = provenance.source_type
    if provenance.source_id:
        provenance_extra["source_id"] = provenance.source_id
    if provenance.source_conversation_ids:
        provenance_extra["source_conversation_ids"] = list(
            provenance.source_conversation_ids
        )
    if provenance.source_episode_ids:
        provenance_extra["source_episode_ids"] = list(provenance.source_episode_ids)
    if provenance.timeline_run_id:
        provenance_extra["timeline_run_id"] = provenance.timeline_run_id

    # A local date is not a Conversation id. Day writes retain the legacy source_id
    # passed by VaultTools only in typed metadata so the indexed conversation field
    # remains semantically honest.
    audit_conversation_id = (
        None if provenance.source_type == "timeline_day" else conversation_id
    )
    try:
        entry = MemoryAuditEntry(
            user_id=str(user_id),
            memory_space_id=memory_space_id,
            conversation_id=audit_conversation_id,
            operation=operation,
            note_path=note_path,
            cause=provenance.cause,
            strategy=provenance.strategy,
            provider=provider,
            agent_mode=agent_mode,
            before_hash=_sha256(before),
            after_hash=_sha256(after),
            after_bytes=len(after.encode("utf-8")) if after is not None else None,
            after_text=after,
            summary=summary or _line_delta_summary(before, after),
            extra={
                **dict(extra),
                **provenance_extra,
                **_active_trace_context(),
            },
        )
        if idempotency_key:
            entry.id = ObjectId(
                hashlib.sha256(f"{user_id}:{idempotency_key}".encode()).hexdigest()[:24]
            )
        try:
            await entry.insert()
        except DuplicateKeyError:
            if not idempotency_key:
                raise
            existing = await MemoryAuditEntry.get(entry.id)
            if (
                existing is None
                or existing.before_hash != entry.before_hash
                or existing.after_hash != entry.after_hash
                or existing.note_path != entry.note_path
            ):
                raise RuntimeError("Idempotent audit identity collision")
        try:
            # Defer this dependency to break the import cycle through
            # backend.services.timeline.accepted_context -> backend.services.memory ->
            # backend.services.memory.service_factory -> backend.services.memory.providers.chronicle
            # -> backend.services.memory.audit.
            from backend.services.timeline.accepted_context import (
                queue_context_assessment,
            )

            await queue_context_assessment(str(user_id), memory_space_id)
        except Exception:
            logger.warning(
                "Context assessment enqueue deferred to recovery", exc_info=True
            )
    except Exception as e:  # noqa: BLE001 — ordinary writes retain best-effort audit
        if strict:
            raise
        logger.warning(
            "Failed to record vault audit (%s %s for %s): %s",
            operation,
            note_path,
            user_id,
            e,
        )
