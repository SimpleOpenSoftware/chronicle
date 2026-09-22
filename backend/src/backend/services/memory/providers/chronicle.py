"""Chronicle memory service — agentic Markdown vault.

This module provides the core MemoryService class that maintains a personal,
Obsidian-style Markdown vault (``data/conversation_docs/<user>/``) as the single
source of truth for a user's memory:

- **Write** — a tool-calling memory agent records each conversation and surgically
  edits the People/Topic/Category notes it touches (``_add_memory_agent``).
- **Read** — a read-only retrieval agent drives ripgrep over the vault, reads the
  relevant notes, and synthesises an answer (``_search_vault_grep``).

The vault is the only store; there is no separate search index. All knowledge about
how the vault is shaped lives in the memory agent's prompts (see ``..agent``).
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import backend.services.chat_runs as chat_runs
import backend.services.privacy as privacy

# The deterministic Daily ``## Episodes`` index is a projection of active timeline
# episodes, shared with the incremental projection layer so a settled-day write and a
# rolling refresh install identical content. Aliased under the historical private names
# because callers (and scripts) import them from this module.
from backend.services.observability.system_events import record_event_sync
from backend.services.timeline.projection import (
    ensure_day_episode_index as _ensure_day_episode_index,
)
from backend.services.timeline.projection import (
    render_day_episode_index as _render_day_episode_index,
)
from backend.services.timeline.projection import (
    replace_h2_section as _replace_h2_section,
)

from ..audit import record_vault_change
from ..base import (
    DayWriteOutcome,
    MemoryEntry,
    MemoryServiceBase,
    VaultSearchUnavailable,
)
from ..config import MemoryConfig
from ..conversation_note import (
    ConversationNoteError,
    canonicalize_conversation_note,
    write_source_fallback_conversation_note,
)
from ..scope import MemoryScope, MemoryScopeResolver
from ..session_write import SessionDraftResult, SessionWriteInput
from ..telemetry import (
    memory_attempt,
    memory_span,
    set_observation_io,
    set_safe_span_attributes,
    text_payload,
)
from ..vault_manager import ConvDocVaultManager
from ..vault_scaffold import is_scaffold_note, seed_vault_scaffold
from ..vault_verify import (
    render_findings,
    verify_day_episode_ranges,
    verify_vault_changes,
)

memory_logger = logging.getLogger("memory_service")

# Deterministic vault invariants are completion gates, not advisory review notes. The
# semantic reviewer also returns findings (``redundant``/``unsupported``), but those are
# model judgements and remain warnings. If one of these structural rules survives the
# repair pass, the day must stay retryable instead of being latched as written with a
# malformed vault.
_BLOCKING_VAULT_RULES = frozenset(
    {
        "record_missing",
        "forbidden_folder",
        "immutable_section",
        "illegal_path",
        "root_note_role",
        "not_a_person",
        "duplicate_section",
        "empty_semantic_note",
        "note_schema",
        "new_category",
        "topic_scope_overlap",
        "case_collision",
        "episode_ranges",
    }
)


def _safe_exception_diagnostic(exc: BaseException) -> str:
    """Return a useful diagnostic without persisting arbitrary provider text."""

    return type(exc).__name__


def _repair_guidance(findings: List[Any]) -> str:
    """The repair instruction for one set of findings.

    Two clauses are conditional because each is false for some finding it might
    accompany. "The day is already recorded, do not add new content" is wrong when the
    finding IS that the record note is missing — it forbids the very fix being asked
    for. And a write agent told to record things does not read "fix this" as "delete
    this", so a redundancy has to name its own remedy.
    """

    rules = {getattr(f, "rule", "") for f in findings}
    lines = ["REPAIR ONLY. Fix exactly these problems and nothing else."]
    if not rules.intersection({"record_missing", "episode_ranges"}):
        lines.append("The day is already recorded; do not add new content.")
    if "episode_ranges" in rules:
        lines.append(
            "An `episode_ranges` finding is fixed by REPLACING only the Daily note's "
            "`## Episodes` section with exactly one chronological bullet per supplied "
            "episode. Begin every bullet with its source range verbatim; remove stale "
            "and duplicate bullets, and preserve all other sections."
        )
    if "redundant" in rules:
        lines.append(
            "A `redundant` finding is fixed by DELETING the line it names — the fact is "
            "already recorded, so there is nothing to rewrite or merge."
        )
    if "unsupported" in rules:
        lines.append(
            "An `unsupported` finding is fixed by DELETING the line it names — do not "
            "look for a source that justifies it."
        )
    lines.append("Then call verify_vault to confirm:")
    return " ".join(lines) + "\n" + render_findings(findings)


def _session_write_completed(result, verify_expected: bool) -> bool:
    """Distinguish deliberate edits/no-op from a model stopping mid-investigation."""
    if result.truncated or result.stalled or (verify_expected and not result.verified):
        return False
    if result.touched:
        return True
    return not result.errors and bool(result.summary.strip())


class MemoryService(MemoryServiceBase):
    """Memory service backed by an agentic Markdown vault (the ground truth).

    Each conversation is recorded into the vault by the memory agent, which also
    updates the person/topic/category notes it mentions. Retrieval is an agentic
    ripgrep over those notes that returns a synthesised answer plus the notes it read.
    """

    @property
    def provider_identifier(self) -> str:
        return "chronicle"

    def __init__(self, config: MemoryConfig):
        super().__init__()
        self.config = config
        self.vault = ConvDocVaultManager()
        self.scope_resolver = MemoryScopeResolver(self.vault._base_dir.parent)
        self.last_day_source_episode_keys_by_path: dict[str, list[str]] = {}
        self.last_day_source_evidence_keys_by_path: dict[str, list[str]] = {}
        self.last_day_inference_artifacts: list[dict] = []

    def _scope_root(self, user_id: str, memory_space_id: Optional[str]) -> Path:
        if not memory_space_id:
            return self.vault.user_root(str(user_id))
        return self.scope_resolver.vault_root(
            MemoryScope(str(user_id), memory_space_id)
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._validate_configured_backends()
        # Vault-only: the agent generates + writes notes via its own tool-calling
        # LLM, and search greps the vault. No external index to connect to.
        self._initialized = True
        memory_logger.info("✅ Chronicle memory service initialized (agentic vault).")

    def _validate_configured_backends(self) -> None:
        """Resolve executors and model contracts before reporting readiness."""
        # A missing Pi/Codex runtime must not silently turn an experiment into a
        # different backend. Recovery remains available for a runtime that disappears
        # after this check, inside `_run_agent_with_note_guarantee`.
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        write_class = self._write_agent_class(write_backend)
        if write_backend == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import validate_pi_executor_config

            validate_pi_executor_config("memory_write")
        elif write_backend == "codex":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.codex_agent import validate_codex_executor_config

            validate_codex_executor_config()

        recovery_backend = getattr(self.config, "write_recovery_backend", "direct")
        if recovery_backend:
            recovery_backend = recovery_backend.lower()
            recovery_class = self._write_agent_class(recovery_backend)
            if recovery_backend == "pi":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.pi_agent import validate_pi_executor_config

                validate_pi_executor_config(
                    "memory_write", force_fallback=recovery_class is write_class
                )
            elif recovery_backend == "codex":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.codex_agent import validate_codex_executor_config

                validate_codex_executor_config()

        search_backend = (
            getattr(self.config, "search_agent_backend", "direct") or "direct"
        ).lower()
        if search_backend == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import validate_pi_executor_config

            validate_pi_executor_config("memory_search")
        elif search_backend != "direct":
            raise ValueError(f"Unsupported memory search backend: {search_backend}")

    def _write_agent_class(self, backend: Optional[str] = None):
        """Resolve one configured write backend to its agent implementation.

        Configuration errors are deliberately loud. The explicitly configured
        recovery backend handles failed note creation; availability is not a reason
        to change the primary backend implicitly.
        """
        # Lazy import: circular dependency (agent → memory_agent → llm_client →
        # services.memory.config → service_factory → this module)
        from ..agent import MemoryAgent

        name = (
            backend or getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        if name == "direct":
            return MemoryAgent
        if name == "codex":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.codex_agent import CodexMemoryAgent, codex_executor_available

            available, detail = codex_executor_available()
            if available:
                return CodexMemoryAgent
            raise RuntimeError(
                "memory write backend 'codex' is unavailable: " f"{detail}"
            )
        if name == "pi":
            # Lazy: ..agent imports llm_client, which imports this package's config back.
            from ..agent.pi_agent import PiMemoryAgent, pi_executor_available

            available, detail = pi_executor_available()
            if available:
                return PiMemoryAgent
            raise RuntimeError("memory write backend 'pi' is unavailable: " f"{detail}")
        raise ValueError(f"Unsupported memory write backend: {name}")

    def _recovery_agent_class(self):
        backend = getattr(self.config, "write_recovery_backend", "direct")
        return self._write_agent_class(backend) if backend else None

    def _write_agent_instance(self, agent_class, user_root: Path, **kwargs: Any):
        """Instantiate a writer with Pi-only, evidence-gated runtime safeguards."""

        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.pi_agent import PiMemoryAgent

        if agent_class is PiMemoryAgent:
            # Pi has already chosen and completed its edits once the required source
            # note exists, at least one mutation landed, and verify_vault passes. End
            # at that deterministic success boundary instead of paying for another
            # free-text turn that can itself consume the response cap.
            kwargs.setdefault("terminate_on_verified", True)
            limit = getattr(
                self.config,
                "write_max_consecutive_identical_tool_calls",
                None,
            )
            if limit is not None:
                kwargs["max_identical_tool_calls"] = limit
        return agent_class(user_root, **kwargs)

    async def _review_write(
        self,
        user_root: Path,
        *,
        source: str,
        before: dict,
        touched: List[str],
        record: str,
    ) -> List[Any]:
        """Findings from the read-only review agent, or none if it could not judge.

        Always the direct executor, whichever backend did the writing: the review is
        worth more for being independent of the writer, and it needs only the read-only
        tools every configured model can drive.
        """
        if not touched or not getattr(self.config, "review_writes", True):
            return []
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.review_agent import review_vault_write

        t0 = time.perf_counter()
        with memory_attempt("review"):
            review = await review_vault_write(
                user_root,
                source=source,
                before=before,
                touched=touched,
                record=record,
            )
        # Always logged: this costs a whole agent run, so a silent one is a cost with no
        # visible counterpart — and "found nothing" and "never reached a verdict" have
        # to be distinguishable from outside.
        memory_logger.info(
            "🔍 Write review (%s): %s over %d note(s), %d finding(s) in "
            "%d round(s)/%d tool call(s) (%.1fs)%s%s",
            record,
            "verdict" if review.reported else "NO VERDICT",
            len(touched),
            len(review.findings),
            review.rounds,
            review.tool_calls,
            time.perf_counter() - t0,
            ("; " + "; ".join(review.warnings)) if review.warnings else "",
            (
                ("; " + "; ".join(f"{f.path} [{f.rule}]" for f in review.findings))
                if review.findings
                else ""
            ),
        )
        return list(review.findings)

    # =========================================================================
    # ADD MEMORY
    # =========================================================================

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
        *,
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
        source_people: Optional[List[str]] = None,
        source_images: Optional[List[Tuple[str, bytes]]] = None,
        memory_space_id: Optional[str] = None,
        admitted_space_write: bool = False,
    ) -> Tuple[bool, List[str]]:
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        with memory_span(
            "memory_write",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": source_id,
                "session.id": source_id,
                "langfuse.session.id": source_id,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.client_id": client_id,
                "chronicle.pipeline.stage": "memory_write",
                "chronicle.memory.operation": "write",
                "chronicle.memory.primary_backend": write_backend,
                "chronicle.memory.recovery_backend": recovery_backend or "none",
                "chronicle.memory.transcript_chars": len(transcript or ""),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": source_id,
                    "transcript": text_payload(transcript),
                    "title": text_payload(source_title),
                    "duration_minutes": source_duration_minutes,
                },
            )
            await self._ensure_initialized()
            if memory_space_id:
                await self.scope_resolver.require_space(
                    MemoryScope(str(user_id), memory_space_id),
                    writable=True,
                    allow_merging=admitted_space_write,
                )
            success, touched = await self._add_memory_agent(
                transcript,
                source_id,
                user_id,
                source_date=source_date,
                source_duration_minutes=source_duration_minutes,
                source_title=source_title,
                source_people=source_people,
                source_images=source_images,
                memory_space_id=memory_space_id,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": success,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"success": success, "touched_count": len(touched)},
            )
            return success, touched

    async def _add_memory_agent(
        self,
        transcript: str,
        source_id: str,
        user_id: str,
        *,
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
        source_people: Optional[List[str]] = None,
        source_images: Optional[List[Tuple[str, bytes]]] = None,
        memory_space_id: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Write path via the tool-calling memory agent.

        The agent creates the conversation note and surgically edits person/topic
        notes in the user's vault. Returns ``(success, touched_paths)`` — the touched
        vault-relative note paths stand in for the chunk/memory ids the older index
        path returned, so the existing job bookkeeping (counts, versions) works unchanged.
        """
        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        t0 = time.perf_counter()
        user_root = self._scope_root(user_id, memory_space_id)
        # Concurrent memory jobs for the same user may run on different RQ workers;
        # each individual vault mutation is serialised inside VaultTools via the
        # per-user vault_note_lock (lock-write-unlock, never across LLM calls).
        seed_vault_scaffold(user_root)  # idempotent: .base + hub notes
        existing_before = self._vault_note_set(user_root)
        result = await self._run_agent_with_note_guarantee(
            None,
            user_root,
            transcript,
            source_id,
            source_date=source_date,
            source_duration_minutes=source_duration_minutes,
            source_title=source_title,
            source_people=source_people,
            source_images=source_images,
        )
        if (result.truncated or result.stalled) and not result.touched:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ add_memory(agent) %s: aborted on %s after %d rounds (%d tool "
                "calls) — nothing recorded (%.2fs)",
                source_id,
                reason,
                result.rounds,
                result.tool_calls,
                time.perf_counter() - t0,
            )
            return False, []
        await self._record_agent_touches(
            user_id,
            source_id,
            user_root,
            result.touched,
            existing_before,
            removed=result.removed,
            memory_space_id=memory_space_id,
        )
        expected_note = user_root / "Conversations" / f"{Path(source_id).name}.md"
        if not expected_note.is_file():
            memory_logger.error(
                "❌ add_memory(agent) %s: required conversation note was not created",
                source_id,
            )
            return False, result.touched
        if result.truncated or result.stalled:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ add_memory(agent) %s: source and partial mutations were preserved, "
                "but no agent completed deliberately (%s)",
                source_id,
                reason,
            )
            return False, result.touched
        memory_logger.info(
            "✅ add_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    # =========================================================================
    # ADD DAY MEMORY
    # =========================================================================

    async def draft_session_memory(
        self, source: SessionWriteInput, user_id: str
    ) -> SessionDraftResult:
        """Draft a reviewed session in this service's isolated vault.

        No activity index is written. A completed no-op is valid; an interrupted
        investigation is not. The caller owns proposal publication and approval.
        """
        # Defer this dependency to break the import cycle through backend.services.memory ->
        # backend.services.memory.service_factory -> backend.services.memory.providers.chronicle.
        from ..agent.memory_agent import VERIFY_CAPABLE_BACKENDS

        await self._ensure_initialized()
        root = self.vault.user_root(user_id)
        seed_vault_scaffold(root)
        before = self._vault_note_set(root)
        draft = SessionDraftResult(outcome="failed")
        text = source.render()
        verify_expected = (
            self.config.write_agent_backend in VERIFY_CAPABLE_BACKENDS
            and (self.config.write_recovery_backend or "direct")
            in VERIFY_CAPABLE_BACKENDS
        )
        result = None
        with memory_span(
            "memory_write_session",
            attributes={
                "chronicle.memory.operation": "write_session",
                "chronicle.memory.session_key": source.session_key,
                "chronicle.memory.source_run_id": source.proposal_id,
                "chronicle.memory.source_episode_ids": list(source.episode_ids),
                "chronicle.memory.source_conversation_ids": list(
                    source.conversation_ids
                ),
                "chronicle.memory.local_date": source.event_date or "undated",
                "chronicle.user_id": user_id,
            },
        ) as span:
            set_observation_io(span, input={"session": text_payload(text)})
            for attempt in ("primary", "recovery"):
                if attempt == "recovery" and not self.config.write_recovery_backend:
                    break
                result = await self._run_session_attempt(
                    root,
                    source,
                    attempt,
                    "Finish only the supported useful changes; no Daily entry is required.",
                )
                if result is not None:
                    draft.retain_attempt(result)
                    if _session_write_completed(result, verify_expected):
                        break

            findings = await self._session_draft_findings(
                root, before, text, draft.touched
            )
            if findings and result is not None:
                repair = await self._run_session_attempt(
                    root,
                    source,
                    "verify_repair",
                    _repair_guidance(findings),
                )
                if repair is not None:
                    draft.retain_attempt(repair)
                # The latest attempt owns completion. A failed repair cannot hide
                # behind earlier success, and a completed repair can finish recovery.
                result = repair
                findings = await self._session_draft_findings(
                    root, before, text, draft.touched
                )

            await self._record_agent_touches(
                user_id,
                source.session_key,
                root,
                draft.touched,
                before,
                removed=result.removed if result else None,
            )
            blocking = [f for f in findings if f.rule in _BLOCKING_VAULT_RULES]
            if blocking or result is None:
                draft.outcome = "failed"
            elif result.truncated or result.stalled:
                draft.outcome = "partial" if not findings else "failed"
            elif _session_write_completed(result, verify_expected):
                draft.outcome = "complete"
            else:
                draft.outcome = "failed"
            if findings:
                memory_logger.warning(
                    "Session %s has unresolved vault findings: %s",
                    source.session_key,
                    "; ".join(f"{f.path} [{f.rule}]" for f in findings),
                )
            set_observation_io(
                span, output={"outcome": draft.outcome, "touched": draft.touched}
            )
            memory_logger.info("Session %s draft %s", source.session_key, draft.outcome)
            return draft

    async def _run_session_attempt(self, root, source, attempt, guidance):
        with memory_attempt(attempt):
            try:
                agent_class = (
                    self._recovery_agent_class()
                    if attempt == "recovery"
                    else self._write_agent_class()
                )
                if agent_class is None:
                    return None
                return await self._write_agent_instance(agent_class, root).run(
                    source.render(),
                    source.event_date or "undated",
                    date=source.source_date,
                    guidance=guidance,
                    record="session",
                    source_permissions=source.permissions,
                )
            except Exception as exc:
                memory_logger.warning(
                    "Session %s %s failed (%s)",
                    source.session_key,
                    attempt,
                    _safe_exception_diagnostic(exc),
                )
                return None

    async def _session_draft_findings(self, root, before, source, touched):
        # Defer this dependency to break the import cycle through backend.services.memory ->
        # backend.services.memory.service_factory -> backend.services.memory.providers.chronicle.
        from ..agent.memory_agent import (
            allow_new_categories,
            forbidden_folders,
            immutable_sections,
            required_notes,
        )

        findings = verify_vault_changes(
            root,
            before,
            required=required_notes("session", ""),
            forbidden_folders=forbidden_folders("session"),
            immutable_sections=immutable_sections("session"),
            forbid_new_categories=not allow_new_categories("session"),
        )
        findings.extend(
            await self._review_write(
                root,
                source=source,
                before=before,
                touched=touched,
                record="session",
            )
        )
        return findings

    async def add_day_memory(
        self,
        day_digest: str,
        local_date: str,
        user_id: str,
        *,
        day_index_digest: str,
        source_date: Optional[str] = None,
        source_run_id: Optional[str] = None,
        source_episode_ids: Optional[List[str]] = None,
        source_conversation_ids: Optional[List[str]] = None,
    ) -> Tuple[DayWriteOutcome, List[str]]:
        """Record one settled local day of timeline episodes into the vault.

        The conversation path (``add_memory``) is the wrong unit for capture evidence:
        ScreenPipe audio is cut into bounded compute spans, so one meeting can span
        several recordings. An episode already carries the right bounds, so the day of
        episodes — not the recordings under it — is what gets remembered.

        The record lands in ``Daily/<local_date>.md`` rather than under
        ``Conversations/``, which stays one note per conversation. Durable
        People/Topic/Category edits are unchanged.
        """
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        episode_ids = list(dict.fromkeys(source_episode_ids or []))
        conversation_ids = list(dict.fromkeys(source_conversation_ids or []))
        session_id = (
            f"timeline-day:{user_id}:{local_date}:{source_run_id or 'unversioned'}"
        )
        span_attributes = {
            "openinference.span.kind": "CHAIN",
            "gen_ai.operation.name": "invoke_agent",
            "session.id": session_id,
            "langfuse.session.id": session_id,
            "chronicle.user_id": str(user_id),
            "langfuse.user.id": str(user_id),
            "chronicle.pipeline.stage": "memory_write",
            "chronicle.memory.operation": "write_day",
            "chronicle.memory.local_date": local_date,
            "chronicle.memory.primary_backend": write_backend,
            "chronicle.memory.recovery_backend": recovery_backend or "none",
            "chronicle.memory.transcript_chars": len(day_digest or ""),
            "chronicle.memory.day_index_chars": len(day_index_digest or ""),
        }
        if source_run_id:
            span_attributes["chronicle.memory.source_run_id"] = source_run_id
        if episode_ids:
            span_attributes["chronicle.memory.source_episode_ids"] = episode_ids
        if conversation_ids:
            span_attributes["chronicle.memory.source_conversation_ids"] = (
                conversation_ids
            )
        with memory_span(
            "memory_write_day",
            attributes=span_attributes,
        ) as span:
            set_observation_io(
                span,
                input={
                    "local_date": local_date,
                    "transcript": text_payload(day_digest),
                    "source_run_id": source_run_id,
                    "source_episode_ids": episode_ids,
                    "source_conversation_ids": conversation_ids,
                },
            )
            await self._ensure_initialized()
            outcome, touched = await self._add_day_memory_agent(
                day_digest,
                local_date,
                user_id,
                day_index_digest=day_index_digest,
                source_date=source_date,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.outcome": outcome.value,
                    "chronicle.memory.success": outcome is DayWriteOutcome.COMPLETE,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"outcome": outcome.value, "touched_count": len(touched)},
            )
            return outcome, touched

    async def _add_day_memory_agent(
        self,
        day_digest: str,
        local_date: str,
        user_id: str,
        *,
        day_index_digest: Optional[str] = None,
        source_date: Optional[str] = None,
    ) -> Tuple[DayWriteOutcome, List[str]]:
        """Write path for a settled day, with one recovery attempt.

        Deliberately simpler than ``_run_agent_with_note_guarantee``: that guarantee is
        built around a conversation note whose id the caller owns, and its
        source-preserving fallback writes a ``Conversations/`` note that would be wrong
        here. Chronicle owns the deterministic Daily index; the model owns only the
        semantic cross-day notes. If that judgement does not deliberately complete,
        the caller records the day as failed so it can be retried.
        """
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import (
            VERIFY_CAPABLE_BACKENDS,
            allow_new_categories,
            day_note_path,
            forbidden_folders,
            immutable_sections,
            required_notes,
        )

        record = "day"
        verify_expected = (
            self.config.write_agent_backend in VERIFY_CAPABLE_BACKENDS
            and (self.config.write_recovery_backend or "direct")
            in VERIFY_CAPABLE_BACKENDS
        )
        provenance: dict[str, set[str]] = {}
        self.last_day_source_episode_keys_by_path = {}
        self.last_day_source_evidence_keys_by_path = {}
        self.last_day_inference_artifacts = []

        def retain_provenance(agent_result) -> None:
            if agent_result is None:
                return
            self.last_day_inference_artifacts.extend(agent_result.inference_artifacts)
            for path, keys in agent_result.source_evidence_keys_by_path.items():
                self.last_day_source_evidence_keys_by_path[path] = sorted(
                    set(self.last_day_source_evidence_keys_by_path.get(path, []))
                    | set(keys)
                )
            for path, keys in agent_result.source_episode_keys_by_path.items():
                provenance.setdefault(path, set()).update(keys)
            self.last_day_source_episode_keys_by_path = {
                path: sorted(keys) for path, keys in provenance.items()
            }

        t0 = time.perf_counter()
        trusted_date = source_date or datetime.now(timezone.utc).isoformat()
        user_root = self.vault.user_root(user_id)
        seed_vault_scaffold(user_root)  # idempotent: .base + hub notes
        existing_before = self._vault_note_set(user_root)
        day_rel = day_note_path(local_date)
        system_touched: list[str] = []
        index_digest = day_index_digest or day_digest

        # The episode index is source-backed presentation, not a semantic judgement.
        # Install it before the agent runs so the model spends its context and tool
        # budget only on genuinely durable People/Topic/category facts. This also
        # prevents a large first-time day from truncating while serialising an index
        # Chronicle deterministically replaces after the run anyway.
        if _ensure_day_episode_index(user_root / day_rel, local_date, index_digest):
            system_touched.append(day_rel)

        # A reference-only day (for example, passive media) still gets Chronicle's
        # deterministic Daily index, but there is deliberately nothing to send to the
        # semantic memory agent.
        if not day_digest or len(day_digest.strip()) < 10:
            memory_logger.info(
                "Recorded reference-only day index for %s; skipping semantic memory",
                local_date,
            )
            return DayWriteOutcome.COMPLETE, system_touched

        def day_range_findings():
            return verify_day_episode_ranges(user_root / day_rel, index_digest)

        def day_index_recorded() -> bool:
            """Did Chronicle install the exact active Timeline index for this day?"""

            return (user_root / day_rel).is_file() and not day_range_findings()

        def deliberate_no_op(agent_result) -> bool:
            """Did the agent finish cleanly and decide nothing needed recording.

            A distinct outcome from failing to write. A day whose note already holds
            the episodes — every date carries 65-177 entries from the retired
            per-observation curation — is *correctly* left alone by an agent told not
            to duplicate what a note already has. Treating that as incomplete ran the
            recovery backend for nothing and then reported the day failed, so it was
            retried until it settled as skipped: two full agent runs per attempt to
            reach a conclusion the first run had already reached on purpose.
            """

            return (
                agent_result is not None
                and not agent_result.truncated
                and not agent_result.stalled
                and not agent_result.errors
                and bool((agent_result.summary or "").strip())
                and not agent_result.touched
                # A non-empty summary is not a conclusion. Both Qwen3.6 and DeepSeek V4
                # Pro end runs by narrating the next step as prose instead of emitting
                # the call — "Let me check the later parts of the day note for evening
                # episodes" — which reads as a clean finish with nothing to record. An
                # agent that genuinely decided the day needs nothing called verify_vault
                # first, as it is told to; one that stopped mid-thought did not.
                and (agent_result.verified or not verify_expected)
            )

        def semantic_write_completed(agent_result) -> bool:
            """Did the semantic agent deliberately finish its assigned vault work?"""

            return (
                agent_result is not None
                and not agent_result.truncated
                and not agent_result.stalled
                and (agent_result.verified or not verify_expected)
                and (bool(agent_result.touched) or deliberate_no_op(agent_result))
            )

        result = None
        for attempt, guidance in (
            ("primary", ""),
            (
                "recovery",
                "RECOVERY REQUIREMENT: the previous attempt did not finish recording "
                f"this day. You MUST write the day note at exactly "
                f"{day_note_path(local_date)}. Inspect what is already there and add "
                "only what is missing; do not duplicate facts already recorded.",
            ),
        ):
            agent_class = (
                self._write_agent_class()
                if attempt == "primary"
                else self._recovery_agent_class()
            )
            if agent_class is None:
                break
            with memory_attempt(attempt):
                try:
                    result = await self._write_agent_instance(
                        agent_class, user_root
                    ).run(
                        day_digest,
                        local_date,
                        date=trusted_date,
                        guidance=guidance,
                        record=record,
                    )
                    retain_provenance(result)
                except Exception as exc:  # noqa: BLE001 - recovery/caller handles it
                    diagnostic = _safe_exception_diagnostic(exc)
                    memory_logger.error(
                        "Day memory %s backend failed for %s (%s)",
                        attempt,
                        local_date,
                        diagnostic,
                    )
                    result = None
                    continue
            if semantic_write_completed(result):
                # A deliberate no-op ends the run here: there is nothing for the
                # recovery backend to recover.
                break

        # The agent is told to call verify_vault itself, but correctness must not depend
        # on it choosing to. Re-run the same checks here and give anything left back as
        # guidance for one repair pass — the findings name the note and the fix.
        #
        # Chronicle owns the day note, so no record note is required from the semantic
        # agent. It may legitimately touch only People/Topic/category notes, or verify
        # the vault and make no edits when the day contains no durable information.
        def _required() -> tuple[str, ...]:
            return required_notes(record, local_date)

        # Enforce ownership after the agent too. If it ignored the prompt and rewrote
        # the index, restore the concise source-backed form without spending another
        # model round.
        if _ensure_day_episode_index(user_root / day_rel, local_date, index_digest):
            system_touched.append(day_rel)

        findings = verify_vault_changes(
            user_root,
            existing_before,
            required=_required(),
            forbidden_folders=forbidden_folders(record),
            immutable_sections=immutable_sections(record),
            forbid_new_categories=not allow_new_categories(record),
        )
        findings.extend(day_range_findings())
        # Structural checks pass on a well-formed duplicate. A second, read-only agent
        # reads what was added against the notes around it and reports what the vault
        # already knew — the one failure a rule cannot decide.
        findings.extend(
            await self._review_write(
                user_root,
                source=day_digest,
                before=existing_before,
                touched=list(result.touched) if result else [],
                record=record,
            )
        )
        repair_class = self._write_agent_class()
        if findings and result is not None and repair_class is not None:
            memory_logger.info(
                "🧹 Day %s: %d vault problem(s) left after the write; repairing",
                local_date,
                len(findings),
            )
            with memory_attempt("verify_repair"):
                try:
                    repair = await self._write_agent_instance(
                        repair_class, user_root
                    ).run(
                        day_digest,
                        local_date,
                        date=trusted_date,
                        guidance=_repair_guidance(findings),
                        record=record,
                    )
                except Exception as exc:  # noqa: BLE001 - a failed repair is not fatal
                    memory_logger.warning(
                        "Day %s verify repair failed (%s)",
                        local_date,
                        _safe_exception_diagnostic(exc),
                    )
                else:
                    retain_provenance(repair)
                    result.touched = list(
                        dict.fromkeys([*result.touched, *repair.touched])
                    )
            if _ensure_day_episode_index(user_root / day_rel, local_date, index_digest):
                system_touched.append(day_rel)
            findings = verify_vault_changes(
                user_root,
                existing_before,
                required=_required(),
                forbidden_folders=forbidden_folders(record),
                immutable_sections=immutable_sections(record),
                forbid_new_categories=not allow_new_categories(record),
            )
            findings.extend(day_range_findings())
            # Re-review too: the repair edited notes, and only a second read can say
            # whether it removed the duplication or reworded it.
            findings.extend(
                await self._review_write(
                    user_root,
                    source=day_digest,
                    before=existing_before,
                    touched=list(result.touched) if result else [],
                    record=record,
                )
            )
        if findings:
            # Reviewer judgements are advisory. Deterministic structural findings are
            # gated below after touches have been audited, so a malformed day remains
            # retryable rather than being silently latched as written.
            memory_logger.warning(
                "🧹 Day %s recorded with %d unresolved vault problem(s): %s",
                local_date,
                len(findings),
                "; ".join(f"{f.path} [{f.rule}]" for f in findings),
            )

        touched = list(
            dict.fromkeys([*system_touched, *(list(result.touched) if result else [])])
        )
        await self._record_agent_touches(
            user_id,
            local_date,
            user_root,
            touched,
            existing_before,
            removed=result.removed if result else None,
        )
        blocking_findings = [
            finding for finding in findings if finding.rule in _BLOCKING_VAULT_RULES
        ]
        if blocking_findings:
            memory_logger.error(
                "❌ add_day_memory %s: deterministic vault invariants remain after "
                "repair: %s",
                local_date,
                "; ".join(
                    f"{finding.path} [{finding.rule}]" for finding in blocking_findings
                ),
            )
            return DayWriteOutcome.FAILED, touched
        if result is None:
            # Every backend raised. The note-exists check below cannot catch this: a
            # Daily note for this date may already exist from an earlier write, so its
            # presence says nothing about whether *this* run recorded anything. Without
            # this the day latches as `written` with an empty vault and is never retried.
            memory_logger.error(
                "❌ add_day_memory %s: no write backend completed (%.2fs)",
                local_date,
                time.perf_counter() - t0,
            )
            return DayWriteOutcome.FAILED, touched
        if deliberate_no_op(result):
            memory_logger.info(
                "🗓️ add_day_memory %s: agent recorded nothing — it judged the day "
                "already covered (%.2fs): %s",
                local_date,
                time.perf_counter() - t0,
                (result.summary or "").strip()[:300],
            )
            return DayWriteOutcome.COMPLETE, touched
        if not day_index_recorded():
            memory_logger.error(
                "❌ add_day_memory %s: the canonical day index %s was not recorded "
                "(touched %d other note(s)) (%.2fs)",
                local_date,
                day_rel,
                len(touched),
                time.perf_counter() - t0,
            )
            return DayWriteOutcome.FAILED, touched
        if result is not None and (result.truncated or result.stalled):
            diagnostics = "; ".join(result.errors) or "no diagnostic reported"
            if day_index_recorded() and not findings:
                # Structurally clean, but the run was cut off: it may have stopped
                # before any of its durable People/Topic edits, and nothing here can
                # tell. Neither complete nor failed — see ``DayWriteOutcome.PARTIAL``.
                memory_logger.error(
                    "⚠️ add_day_memory %s recorded a partial day: the agent was "
                    "truncated/stalled but deterministic vault verification is clean "
                    "(rounds=%d tools=%d; %.2fs): %s",
                    local_date,
                    result.rounds,
                    result.tool_calls,
                    time.perf_counter() - t0,
                    diagnostics,
                )
                return DayWriteOutcome.PARTIAL, touched
            memory_logger.error(
                "❌ add_day_memory %s: partial mutations preserved, but no agent "
                "completed deliberately (rounds=%d tools=%d; %.2fs): %s",
                local_date,
                result.rounds,
                result.tool_calls,
                time.perf_counter() - t0,
                diagnostics,
            )
            return DayWriteOutcome.FAILED, touched
        if verify_expected and not result.verified:
            memory_logger.error(
                "❌ add_day_memory %s: semantic agent ended without verify_vault "
                "after primary and recovery attempts (%.2fs)",
                local_date,
                time.perf_counter() - t0,
            )
            return DayWriteOutcome.FAILED, touched
        if result.errors:
            # The required day note and its audited mutations completed, so these are
            # non-fatal diagnostics rather than a reason to discard a real write. They
            # still belong at warning level with their cause: an ``errors=1`` success
            # line cannot distinguish recovered Pi transport noise from a compromised
            # tool call and therefore looks falsely healthy on the System Errors page.
            memory_logger.warning(
                "⚠️ add_day_memory %s completed with %d non-fatal agent "
                "diagnostic(s): %s",
                local_date,
                len(result.errors),
                "; ".join(result.errors),
            )
        memory_logger.info(
            "✅ add_day_memory %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs)",
            local_date,
            len(touched),
            result.rounds if result else 0,
            result.tool_calls if result else 0,
            len(result.errors) if result else 0,
            time.perf_counter() - t0,
        )
        return DayWriteOutcome.COMPLETE, touched

    async def _run_agent_with_note_guarantee(
        self,
        agent_class,
        user_root: Path,
        transcript: str,
        source_id: str,
        *,
        guidance: str = "",
        source_date: Optional[str] = None,
        source_duration_minutes: Optional[float] = None,
        source_title: Optional[str] = None,
        source_people: Optional[List[str]] = None,
        source_images: Optional[List[Tuple[str, bytes]]] = None,
    ):
        """Recover when the primary fails or its conversation note is invalid."""
        trusted_date = source_date or datetime.now(timezone.utc).isoformat()
        primary_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        configured_recovery_backend = getattr(
            self.config, "write_recovery_backend", None
        )
        recovery_backend = (
            configured_recovery_backend.lower()
            if configured_recovery_backend
            else "none"
        )
        before_primary = self._vault_note_set(user_root)
        with memory_attempt("primary"):
            try:
                # Resolve inside the guarantee so a runtime that disappears after
                # service initialization (binary/auth removed, invalidated mount, etc.)
                # still takes the explicitly configured recovery path.
                if agent_class is None:
                    agent_class = self._write_agent_class()
                result = await self._write_agent_instance(agent_class, user_root).run(
                    transcript,
                    source_id,
                    date=trusted_date,
                    duration_minutes=source_duration_minutes,
                    title=source_title,
                    guidance=guidance,
                    images=source_images,
                )
            except Exception as exc:  # noqa: BLE001 - recovery backend handles it
                # Lazy import: circular dependency (agent → memory_agent →
                # llm_client → back into providers), same as _write_agent_class.
                from ..agent.memory_agent import MemoryAgentResult

                diagnostic = _safe_exception_diagnostic(exc)
                memory_logger.error(
                    "Primary memory write backend failed for %s (%s); trying the "
                    "configured recovery path",
                    source_id,
                    diagnostic,
                )
                after_primary = self._vault_note_set(user_root)
                touched = sorted(
                    path
                    for path, content in after_primary.items()
                    if before_primary.get(path) != content
                )
                removed = [
                    {"old_path": path, "new_path": "", "before": content}
                    for path, content in sorted(before_primary.items())
                    if path not in after_primary
                ]
                result = MemoryAgentResult(
                    conversation_id=source_id,
                    rounds=0,
                    touched=touched,
                    summary="",
                    removed=removed,
                    errors=[f"primary write backend failed ({diagnostic})"],
                    truncated=True,
                )
        note_name = Path(source_id).name
        expected_note = user_root / "Conversations" / f"{note_name}.md"
        primary_note_valid = self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        )
        primary_incomplete = bool(result.truncated or result.stalled)
        if primary_note_valid and not primary_incomplete:
            result.touched = list(
                dict.fromkeys((*result.touched, f"Conversations/{note_name}.md"))
            )
            return result

        recovery_resolution_error = None
        with memory_attempt("recovery"):
            try:
                recovery_class = self._recovery_agent_class()
            except Exception as exc:  # noqa: BLE001 - deterministic fallback guaranteed
                recovery_class = None
                recovery_resolution_error = _safe_exception_diagnostic(exc)
                memory_logger.error(
                    "Memory recovery backend could not be resolved for %s (%s); "
                    "writing the source-preserving conversation note",
                    source_id,
                    recovery_resolution_error,
                )
        failure_description = (
            "stopped before deliberate completion"
            if primary_incomplete and primary_note_valid
            else f"did not create a valid Conversations/{note_name}.md note"
        )
        memory_logger.warning(
            "Memory write backend %s%s",
            failure_description,
            (
                "; trying the configured recovery backend"
                if recovery_class
                else "; using the source-preserving fallback path"
            ),
        )
        if primary_incomplete and primary_note_valid:
            recovery_requirement = (
                "RECOVERY REQUIREMENT: the previous attempt stopped before deliberate "
                "completion. Inspect the existing conversation and linked notes, then "
                "finish recording any transcript facts it missed. Do not duplicate facts."
            )
        else:
            recovery_requirement = (
                "RECOVERY REQUIREMENT: the previous attempt did not create the required "
                f"conversation note. You MUST write it at exactly "
                f"Conversations/{note_name}.md using conversation_id {source_id}. Do "
                "not alter or abbreviate the ID. The Summary and Key Facts sections "
                "MUST contain substantive text. For a short or low-information "
                "transcript, summarize the exact utterance rather than leaving either "
                "section blank."
            )
        recovery_guidance = (
            f"{guidance}\n\n" if guidance else ""
        ) + recovery_requirement
        # The recovery pass is best-effort: it may fail outright (no
        # defaults.fallback_llm configured, fallback unreachable, ...). The note
        # guarantee must survive that — degrade to an empty recovery result and
        # let the source-preserving fallback note below do its job, rather than
        # failing the whole memory job with nothing recorded.
        with memory_attempt("recovery"):
            if recovery_class is None:
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.memory_agent import MemoryAgentResult

                recovery = MemoryAgentResult(
                    conversation_id=source_id,
                    rounds=0,
                    touched=[],
                    summary="",
                    errors=(
                        [f"recovery backend unavailable: {recovery_resolution_error}"]
                        if recovery_resolution_error is not None
                        else []
                    ),
                )
            else:
                try:
                    recovery = await self._write_agent_instance(
                        recovery_class,
                        user_root,
                        # Reusing the direct runtime means retrying through its configured
                        # fallback model. Switching runtimes already supplies an independent
                        # recovery path, so use that runtime's primary model.
                        force_fallback=recovery_class is agent_class,
                    ).run(
                        transcript,
                        source_id,
                        date=trusted_date,
                        duration_minutes=source_duration_minutes,
                        title=source_title,
                        guidance=recovery_guidance,
                        images=source_images,
                    )
                except Exception as exc:  # noqa: BLE001 - never lose the note
                    # Lazy import: circular dependency (agent → memory_agent →
                    # llm_client → providers), same as _write_agent_class above.
                    from ..agent.memory_agent import MemoryAgentResult

                    diagnostic = _safe_exception_diagnostic(exc)
                    memory_logger.error(
                        "Memory-agent recovery pass failed for %s (%s); falling back "
                        "to the source-preserving conversation note",
                        source_id,
                        diagnostic,
                    )
                    recovery = MemoryAgentResult(
                        conversation_id=source_id,
                        rounds=0,
                        touched=[],
                        summary="",
                        errors=[f"recovery pass failed ({diagnostic})"],
                        truncated=True,
                    )
        recovery.rounds += result.rounds
        recovery.tool_calls += result.tool_calls
        recovery.touched = list(dict.fromkeys((*result.touched, *recovery.touched)))
        recovery.removed = [*result.removed, *recovery.removed]
        recovery.errors = [*result.errors, *recovery.errors]
        if recovery_class is None:
            # No agent completed the interrupted work. The deterministic note path
            # preserves the source but must not make an incomplete primary look like
            # a deliberate semantic completion.
            recovery.truncated = recovery.truncated or result.truncated
            recovery.stalled = recovery.stalled or result.stalled
        for key, value in result.usage.items():
            recovery.usage[key] = recovery.usage.get(key, 0) + value
        recovery_valid = self._canonicalize_conversation_note(
            expected_note,
            source_id,
            trusted_date,
            source_duration_minutes,
            source_title,
        )
        recovery_incomplete = bool(recovery.truncated or recovery.stalled)
        if not recovery_valid or recovery_incomplete:
            fallback_reasons = []
            if not recovery_valid:
                fallback_reasons.append("invalid_note")
            if recovery_incomplete:
                fallback_reasons.append("incomplete_agent")
            memory_logger.warning(
                "Memory-agent attempts did not produce a complete valid note for %s; "
                "writing the deterministic source-preserving fallback",
                source_id,
            )
            with memory_attempt("fallback"):
                with memory_span(
                    "memory_write.source_fallback",
                    attributes={
                        "openinference.span.kind": "CHAIN",
                        "chronicle.memory.operation": "write_source_fallback",
                        "chronicle.memory.attempt": "fallback",
                        "chronicle.memory.fallback_type": (
                            "deterministic_source_preserving_note"
                        ),
                        "chronicle.memory.fallback_reasons": fallback_reasons,
                        "chronicle.memory.primary_backend": primary_backend,
                        "chronicle.memory.recovery_backend": recovery_backend,
                        "gen_ai.conversation.id": source_id,
                    },
                ) as span:
                    set_observation_io(
                        span,
                        input={
                            "conversation_id": source_id,
                            "transcript": text_payload(transcript),
                            "title": text_payload(source_title),
                        },
                    )
                    write_source_fallback_conversation_note(
                        expected_note,
                        transcript=transcript,
                        conversation_id=source_id,
                        date=trusted_date,
                        duration_minutes=source_duration_minutes,
                        title=source_title,
                        source_people=source_people or (),
                    )
                    record_event_sync(
                        severity="warning",
                        category="memory",
                        source="memory.provider.chronicle",
                        title=f"Deterministic memory fallback used: {source_id}",
                        detail=(
                            "The memory agent did not produce a complete valid note. "
                            "Chronicle wrote the deterministic source-preserving "
                            "conversation note."
                        ),
                        user_id=user_root.name,
                        conversation_id=source_id,
                        metadata={
                            "fallback_type": "deterministic_source_preserving_note",
                            "note_path": f"Conversations/{note_name}.md",
                            "reasons": fallback_reasons,
                            "primary_backend": primary_backend,
                            "recovery_backend": recovery_backend,
                            "agent_truncated": bool(recovery.truncated),
                            "agent_stalled": bool(recovery.stalled),
                            "agent_error_count": len(recovery.errors),
                            "rounds": recovery.rounds,
                            "tool_calls": recovery.tool_calls,
                        },
                    )
                    set_safe_span_attributes(
                        span,
                        {
                            "chronicle.memory.success": True,
                            "chronicle.memory.touched_count": 1,
                        },
                    )
                    set_observation_io(
                        span,
                        output={"written": True, "touched_count": 1},
                    )
            recovery.touched = list(
                dict.fromkeys((*recovery.touched, f"Conversations/{note_name}.md"))
            )
        return recovery

    @staticmethod
    def _canonicalize_conversation_note(
        path: Path,
        source_id: str,
        source_date: str,
        source_duration_minutes: Optional[float],
        source_title: Optional[str],
    ) -> bool:
        if not path.is_file():
            return False
        try:
            canonicalize_conversation_note(
                path,
                conversation_id=source_id,
                date=source_date,
                duration_minutes=source_duration_minutes,
                title=source_title,
            )
            return True
        except ConversationNoteError as exc:
            memory_logger.warning("Invalid conversation note %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return False

    async def _reprocess_memory_agent(
        self,
        transcript: str,
        source_id: str,
        user_id: str,
        transcript_diff: Optional[list],
        *,
        memory_space_id: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Reprocess path.

        Deletes the stale conversation note so the agent re-records it cleanly, then runs
        the agent. When the reprocess came from speaker re-identification we hand the agent
        the old→new speaker map as guidance so it can ``rename_person`` (which rewrites all
        backlinks) instead of leaving orphaned ``[[Speaker 0]]`` notes. Person/topic notes
        are kept and surgically updated — only the conversation note is regenerated.
        """
        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        user_root = self._scope_root(user_id, memory_space_id)
        guidance = self._speaker_rename_guidance(transcript_diff)

        t0 = time.perf_counter()
        # Per-user serialisation happens per-mutation inside VaultTools (see
        # _add_memory_agent).
        seed_vault_scaffold(user_root)
        existing_before = self._vault_note_set(user_root)

        # Remove the old conversation note (agent writes Conversations/<id>.md and
        # write_note refuses to clobber). Person/topic notes are preserved.
        conv_note = user_root / "Conversations" / f"{Path(source_id).name}.md"
        if conv_note.exists():
            conv_note.unlink()

        result = await self._run_agent_with_note_guarantee(
            None,
            user_root,
            transcript,
            source_id,
            guidance=guidance,
        )
        if result.truncated and not result.touched:
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: aborted on truncated LLM response after "
                "%d rounds (%d tool calls) — nothing recorded (%.2fs)",
                source_id,
                result.rounds,
                result.tool_calls,
                time.perf_counter() - t0,
            )
            return False, []
        await self._record_agent_touches(
            user_id,
            source_id,
            user_root,
            result.touched,
            existing_before,
            removed=result.removed,
            memory_space_id=memory_space_id,
        )
        if not conv_note.is_file():
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: required conversation note was not created",
                source_id,
            )
            return False, result.touched
        if result.truncated or result.stalled:
            reason = (
                "truncated LLM response" if result.truncated else "stalled retry loop"
            )
            memory_logger.error(
                "❌ reprocess_memory(agent) %s: source and partial mutations were "
                "preserved, but no agent completed deliberately (%s)",
                source_id,
                reason,
            )
            return False, result.touched
        memory_logger.info(
            "✅ reprocess_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    def _vault_note_set(self, user_root: Path) -> dict[str, str]:
        """Snapshot vault-relative note contents for create/update audit records."""
        if not user_root.exists():
            return {}
        snapshot: dict[str, str] = {}
        for path in user_root.rglob("*.md"):
            try:
                snapshot[path.relative_to(user_root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
        return snapshot

    async def _record_agent_touches(
        self,
        user_id: str,
        source_id: str,
        user_root: Path,
        touched: Iterable[str],
        existing_before: dict[str, str],
        removed: Optional[Iterable[dict]] = None,
        memory_space_id: Optional[str] = None,
    ) -> None:
        """Record one audit-ledger entry per note the memory agent changed.

        ``removed`` are notes retired by a rename/merge (``VaultTools.removed``): each
        is logged as a ``rename`` entry carrying the pre-removal content, so a note
        disappearing from the vault is never invisible in the ledger (the gap that made
        a rename look like an unexplained clobber followed later by a fresh ``create``).
        """
        for entry in removed or ():
            await record_vault_change(
                user_id=user_id,
                conversation_id=source_id,
                operation="rename",
                note_path=entry.get("old_path"),
                before=entry.get("before"),
                after=None,
                agent_mode=True,
                summary=f"renamed/merged into {entry.get('new_path')}",
                new_path=entry.get("new_path"),
                memory_space_id=memory_space_id,
            )
        for rel in sorted(touched):
            try:
                after: Optional[str] = (user_root / rel).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — note may have been renamed away
                after = None
            is_new = rel not in existing_before
            await record_vault_change(
                user_id=user_id,
                conversation_id=source_id,
                operation="create" if is_new else "update",
                note_path=rel,
                before=existing_before.get(rel),
                after=after,
                agent_mode=True,
                summary=(
                    None
                    if is_new
                    else (
                        f"updated ({len(after.splitlines())} lines)"
                        if after is not None
                        else "updated"
                    )
                ),
                memory_space_id=memory_space_id,
            )

    # Diarization placeholders ("Speaker 0", "Unknown Speaker 1") — the only labels a
    # conversation-scoped diff may globally rename. A person note under a real name
    # aggregates facts from many conversations, so renaming it from one conversation's
    # relabel merges the wrong person's whole history (alex.md -> roshan.md, 2026-07-17).
    _PLACEHOLDER_SPEAKER_RE = re.compile(
        r"^(unknown\s+)?speaker[\s_]*\d+$", re.IGNORECASE
    )

    @classmethod
    def _speaker_rename_guidance(cls, transcript_diff: Optional[list]) -> str:
        """Turn a speaker diff into an instruction to rename the matching person notes."""
        if not transcript_diff:
            return ""
        renames: dict[str, str] = {}
        relabels: dict[str, str] = {}
        for ch in transcript_diff:
            if isinstance(ch, dict) and ch.get("type") == "speaker_change":
                old, new = ch.get("old_speaker"), ch.get("new_speaker")
                if old and new and old != new:
                    if cls._PLACEHOLDER_SPEAKER_RE.match(old.strip()):
                        renames[old] = new
                    else:
                        relabels[old] = new
        parts: list[str] = []
        if renames:
            pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in renames.items())
            parts.append(
                f"Placeholder speakers were identified: {pairs}. For each, if a "
                "People/<placeholder>.md note exists, call rename_person(old, new) FIRST "
                "— it renames the note and rewrites every [[wikilink]] across the vault — "
                "then record the conversation and update the renamed person notes. Do not "
                "leave notes under placeholder speaker labels."
            )
        if relabels:
            pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in relabels.items())
            parts.append(
                f"Attribution changed between named people IN THIS CONVERSATION ONLY: "
                f"{pairs}. Fix the conversation note and move only facts sourced from "
                "this conversation between the affected person notes. Do NOT call "
                "rename_person for these — both notes describe real people whose facts "
                "come from many other conversations."
            )
        if not parts:
            return ""
        return "This is a REPROCESS after speaker re-identification. " + " ".join(parts)

    # =========================================================================
    # SEARCH
    # =========================================================================

    async def search_memories(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        score_threshold: float = 0.0,
        *,
        memory_space_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()
        if memory_space_id:
            await self.scope_resolver.require_space(
                MemoryScope(str(user_id), memory_space_id)
            )
        return await self._search_vault_grep(
            query, user_id, limit, memory_space_id=memory_space_id
        )

    async def retrieve_for_chat(
        self,
        query: str,
        user_id: str,
        *,
        memory_space_id=None,
        notes_only: bool = False,
    ):
        """Return vault evidence without the indexed-memory result adapter."""
        if not self._initialized:
            await self.initialize()
        if memory_space_id:
            await self.scope_resolver.require_space(
                MemoryScope(str(user_id), memory_space_id)
            )

        # Defer this dependency to break the import cycle through backend.services.chat_context ->
        # backend.services.chat_sources -> backend.services.memory ->
        # backend.services.memory.service_factory -> backend.services.memory.providers.chronicle.
        from backend.services.chat_context import VaultNoteEvidence, VaultRetrieval

        # Defer this dependency to break the import cycle through backend.services.memory ->
        # backend.services.memory.service_factory -> backend.services.memory.providers.chronicle.
        from ..agent.memory_agent import is_search_failure_answer

        result, _ = await self._run_search_agent(
            query, user_id, 20, memory_space_id=memory_space_id, notes_only=notes_only
        )
        if not result.answer.strip() or is_search_failure_answer(result.answer):
            raise VaultSearchUnavailable(
                "The vault retrieval agent did not produce a usable answer"
            )
        notes = []
        for note in result.notes:
            text = note["content"]
            path = note["path"]
            revision = hashlib.sha256(text.encode()).hexdigest()
            notes.append(
                VaultNoteEvidence(
                    id="V"
                    + hashlib.sha256((path + revision).encode()).hexdigest()[:12],
                    path=path,
                    title=Path(path).stem,
                    text=text[:8000],
                    revision=revision,
                    coverage=(
                        "Consulted excerpt"
                        if len(text) <= 8000
                        else "First 8,000 characters of consulted excerpt"
                    ),
                )
            )
        return VaultRetrieval(
            answer=result.answer,
            notes=notes,
            coverage=(
                "Retrieval incomplete"
                if result.truncated or result.errors
                else "Notes consulted by vault retrieval; not an exhaustive vault inventory"
            ),
            run_id=chat_runs.current_run().id if chat_runs.current_run() else None,
        )

    async def _search_vault_grep(
        self,
        query: str,
        user_id: str,
        limit: int,
        *,
        memory_space_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Read path: a read-only retrieval agent drives ripgrep over the vault.

        Modelled on Claude Code — the LLM formulates ripgrep patterns (no query
        preprocessing), reads the relevant notes, and synthesises an answer. We return
        one MemoryEntry per note the agent read (capped), with the synthesised answer
        as the top entry so chat gets both the conclusion and the supporting notes.
        """
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import is_search_failure_answer

        result, backend = await self._run_search_agent(
            query, user_id, limit, memory_space_id=memory_space_id
        )

        if result.errors:
            # Log the errors themselves, not just a count. A count tells you a
            # search degraded but not which tool call failed or why, which makes
            # a flaky retrieval agent undiagnosable from logs alone.
            memory_logger.warning(
                "Vault search backend=%s completed with %d error(s) (user: %s): %s",
                backend,
                len(result.errors),
                user_id,
                "; ".join(result.errors),
            )
        if result.warnings:
            memory_logger.info(
                "Vault search backend=%s completed with %d warning(s) (user: %s)",
                backend,
                len(result.warnings),
                user_id,
            )

        # Terminal sentinels are internal audit state, not user memory. Returning one
        # as a score-1 answer (or returning notes from an empty synthesis) contaminates
        # chat context with a failed search. Explicit evidence-based abstentions remain
        # ordinary nonempty answers and still flow through.
        if not result.answer.strip() or is_search_failure_answer(result.answer):
            memory_logger.warning(
                "Vault search backend=%s returned no usable answer (user: %s)",
                backend,
                user_id,
            )
            # Raise rather than return []. An empty list is indistinguishable from
            # a vault that genuinely holds nothing, and callers that assume the
            # latter go on to tell the user their vault is empty.
            raise VaultSearchUnavailable(
                f"The {backend} retrieval agent did not produce a usable answer"
                + (f" ({len(result.errors)} tool error(s))" if result.errors else "")
            )

        results: List[MemoryEntry] = []
        if result.answer:
            results.append(
                MemoryEntry(
                    id=f"search:{user_id}",
                    content=result.answer,
                    metadata={
                        "user_id": user_id,
                        "memory_space_id": memory_space_id,
                        "kind": "vault_search_answer",
                    },
                    score=1.0,
                    created_at=None,
                )
            )
        for note in result.notes[: max(0, limit - len(results))]:
            path = note["path"]
            conv_id = ""
            if path.startswith("Conversations/"):
                conv_id = path.split("/", 1)[1].rsplit(".md", 1)[0]
            results.append(
                MemoryEntry(
                    id=path,
                    content=note["content"][:1500],
                    metadata={
                        "user_id": user_id,
                        "memory_space_id": memory_space_id,
                        "note": path,
                        "conversation_id": conv_id,
                        "kind": "vault_note",
                    },
                    score=0.9,
                    created_at=None,
                )
            )
        memory_logger.info(
            f"🔍 vault search: '{query}' -> {len(result.notes)} note(s) read, "
            f"{result.rounds} round(s), backend={backend} (user: {user_id})"
        )
        return results[:limit]

    async def _run_search_agent(
        self,
        query: str,
        user_id: str,
        limit: int,
        *,
        memory_space_id: Optional[str] = None,
        notes_only: bool = False,
    ):
        """Run and trace one configured retrieval backend without exposing note text."""
        # Lazy: ..agent imports llm_client, which imports this package's config back.
        from ..agent.memory_agent import is_search_failure_answer

        backend = (
            getattr(self.config, "search_agent_backend", "direct") or "direct"
        ).lower()
        if notes_only and backend != "pi":
            raise VaultSearchUnavailable(
                "Notes-only voice retrieval requires the configured Pi search backend"
            )
        with memory_span(
            "memory_search",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "chronicle.pipeline.stage": "memory_search",
                "chronicle.memory.operation": "search",
                "chronicle.memory.backend": backend,
                "chronicle.memory.limit": limit,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.memory.query_chars": len(query),
            },
        ) as span:
            set_observation_io(
                span,
                input={"query": text_payload(query), "limit": limit},
            )
            if backend == "direct":
                # Lazy import: circular dependency (agent → memory_agent →
                # llm_client → services.memory.config → service_factory → here)
                from ..agent import search_vault

                result = await search_vault(
                    query,
                    self._scope_root(user_id, memory_space_id),
                    operation="memory_search",
                    user_id=user_id,
                )
            elif backend == "pi":
                # Lazy: ..agent imports llm_client, which imports this package's config back.
                from ..agent.pi_agent import search_vault_with_pi

                result = await search_vault_with_pi(
                    query,
                    self._scope_root(user_id, memory_space_id),
                    operation="memory_search",
                    user_id=user_id,
                    **({"notes_only": True} if notes_only else {}),
                )
            else:
                raise ValueError(f"Unsupported memory search backend: {backend}")

            usable_answer = bool(
                result.answer.strip()
            ) and not is_search_failure_answer(result.answer)
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": usable_answer and not result.truncated,
                    "chronicle.memory.usable_answer": usable_answer,
                    "chronicle.memory.rounds": result.rounds,
                    "chronicle.memory.tool_calls": result.tool_calls,
                    "chronicle.memory.notes_read_count": len(result.notes),
                    "chronicle.memory.error_count": len(result.errors),
                    "chronicle.memory.warning_count": len(result.warnings),
                    "chronicle.memory.final_synthesis_used": result.final_synthesis_used,
                    "chronicle.memory.truncated": result.truncated,
                    **{
                        f"chronicle.memory.usage.{key}": value
                        for key, value in result.usage.items()
                    },
                },
            )
            set_observation_io(
                span,
                output={
                    "answer": text_payload(result.answer),
                    "usable_answer": usable_answer,
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "notes_read_count": len(result.notes),
                    "error_count": len(result.errors),
                    "warning_count": len(result.warnings),
                    "final_synthesis_used": result.final_synthesis_used,
                    "truncated": result.truncated,
                },
            )
            return result, backend

    # =========================================================================
    # CRUD
    # =========================================================================

    def _vault_entry_from_path(
        self,
        user_id: str,
        path: Path,
        root: Path,
        content_limit: Optional[int] = None,
        memory_space_id: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """Build a MemoryEntry from one vault note (id = vault-relative path)."""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        if content_limit is not None:
            content = content[:content_limit]
        rel = path.relative_to(root).as_posix()
        conv_id = (
            rel[len("Conversations/") : -3] if rel.startswith("Conversations/") else ""
        )
        return MemoryEntry(
            id=rel,
            content=content,
            metadata={
                "user_id": user_id,
                "memory_space_id": memory_space_id,
                "note": rel,
                "conversation_id": conv_id,
                "kind": "vault_note",
            },
            created_at=None,
        )

    def _vault_entries(
        self,
        user_id: str,
        limit: Optional[int] = None,
        *,
        memory_space_id: Optional[str] = None,
        excluded_paths: frozenset[str] = frozenset(),
    ) -> List[MemoryEntry]:
        """Enumerate a user's vault notes as MemoryEntry objects (newest first).

        One entry per note, recursive over Conversations/People/Topics.
        """
        root = self._scope_root(user_id, memory_space_id)
        if not root.exists():
            return []
        paths = sorted(
            (
                p
                for p in root.rglob("*.md")
                if not is_scaffold_note(p, root)
                and p.relative_to(root).as_posix().casefold() not in excluded_paths
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if limit is not None:
            paths = paths[:limit]
        entries: List[MemoryEntry] = []
        for p in paths:
            entry = self._vault_entry_from_path(
                user_id,
                p,
                root,
                content_limit=1500,
                memory_space_id=memory_space_id,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    async def get_all_memories(
        self,
        user_id: str,
        limit: int = 100,
        *,
        memory_space_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()
        if memory_space_id:
            await self.scope_resolver.require_space(
                MemoryScope(str(user_id), memory_space_id)
            )

        excluded = frozenset(
            p.casefold() for p in await privacy.quarantined_vault_paths(user_id)
        )
        return self._vault_entries(
            user_id, limit, memory_space_id=memory_space_id, excluded_paths=excluded
        )

    async def count_memories(
        self, user_id: str, *, memory_space_id: Optional[str] = None
    ) -> Optional[int]:
        if not self._initialized:
            await self.initialize()
        return len(self._vault_entries(user_id, memory_space_id=memory_space_id))

    async def get_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        *,
        memory_space_id: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            memory_logger.error(
                "get_memory called without user_id; the vault is per-user"
            )
            return None

        # Memory ids are vault-relative note paths (see add_memory).
        from backend.services.privacy import quarantined_vault_paths

        if memory_id.casefold() in {
            p.casefold() for p in await quarantined_vault_paths(user_id)
        }:
            return None
        root = self._scope_root(user_id, memory_space_id)
        fp = root / memory_id
        if not fp.is_file():
            return None
        return self._vault_entry_from_path(
            user_id, fp, root, memory_space_id=memory_space_id
        )

    async def get_memories_by_source(
        self,
        user_id: str,
        source_id: str,
        limit: int = 100,
        *,
        memory_space_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Return the conversation note for ``source_id`` (the vault's per-source record)."""
        if not self._initialized:
            await self.initialize()

        root = self._scope_root(user_id, memory_space_id)
        conv_note = root / "Conversations" / f"{Path(source_id).name}.md"

        if conv_note.relative_to(root).as_posix().casefold() in {
            p.casefold() for p in await privacy.quarantined_vault_paths(user_id)
        }:
            return []
        if not conv_note.is_file():
            return []
        entry = self._vault_entry_from_path(
            user_id, conv_note, root, memory_space_id=memory_space_id
        )
        return [entry] if entry is not None else []

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Notes are edited by the memory agent (or by the user directly in the vault),
        not through this API."""
        memory_logger.warning(
            f"update_memory called for {memory_id} but vault notes are edited via the "
            "memory agent or directly in the vault. Use reprocess_memory to regenerate."
        )
        return False

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        *,
        memory_space_id: Optional[str] = None,
    ) -> bool:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            memory_logger.error(
                "delete_memory called without user_id; the vault is per-user"
            )
            return False

        # Memory ids are vault-relative note paths.
        if memory_space_id:
            await self.scope_resolver.require_space(
                MemoryScope(str(user_id), memory_space_id), writable=True
            )
        root = self._scope_root(user_id, memory_space_id)
        fp = root / memory_id
        try:
            if not fp.is_file():
                return False
            fp.unlink()
            await record_vault_change(
                user_id=user_id,
                operation="delete",
                note_path=Path(memory_id).as_posix(),
                agent_mode=False,
                summary=f"deleted {memory_id}",
                memory_space_id=memory_space_id,
            )
            memory_logger.info(f"🗑️ Deleted memory note {memory_id}")
            return True
        except Exception as e:
            memory_logger.error(f"Delete memory failed: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        if not self._initialized:
            await self.initialize()

        count = self.vault.delete_all_docs(user_id)  # vault is the only store
        await record_vault_change(
            user_id=user_id,
            operation="delete_all",
            agent_mode=False,
            summary=f"deleted {count} notes",
            count=count,
        )
        return count

    async def reprocess_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        transcript_diff: Optional[list] = None,
        previous_transcript: Optional[str] = None,
        memory_space_id: Optional[str] = None,
        admitted_space_write: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Delete the stale conversation note and re-record from the transcript."""
        write_backend = (
            getattr(self.config, "write_agent_backend", "direct") or "direct"
        ).lower()
        recovery_backend = getattr(self.config, "write_recovery_backend", None)
        with memory_span(
            "memory_write",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": source_id,
                "session.id": source_id,
                "langfuse.session.id": source_id,
                "chronicle.user_id": str(user_id),
                "langfuse.user.id": str(user_id),
                "chronicle.client_id": client_id,
                "chronicle.pipeline.stage": "memory_write",
                "chronicle.memory.operation": "reprocess",
                "chronicle.memory.primary_backend": write_backend,
                "chronicle.memory.recovery_backend": recovery_backend or "none",
                "chronicle.memory.transcript_chars": len(transcript or ""),
                "chronicle.memory.reprocess": True,
                "chronicle.memory.transcript_diff_count": len(transcript_diff or []),
            },
        ) as span:
            set_observation_io(
                span,
                input={
                    "conversation_id": source_id,
                    "transcript": text_payload(transcript),
                    "previous_transcript": text_payload(previous_transcript),
                    "transcript_diff_count": len(transcript_diff or []),
                },
            )
            await self._ensure_initialized()
            if memory_space_id:
                await self.scope_resolver.require_space(
                    MemoryScope(str(user_id), memory_space_id),
                    writable=True,
                    allow_merging=admitted_space_write,
                )
            success, touched = await self._reprocess_memory_agent(
                transcript,
                source_id,
                user_id,
                transcript_diff,
                memory_space_id=memory_space_id,
            )
            set_safe_span_attributes(
                span,
                {
                    "chronicle.memory.success": success,
                    "chronicle.memory.touched_count": len(touched),
                },
            )
            set_observation_io(
                span,
                output={"success": success, "touched_count": len(touched)},
            )
            return success, touched

    async def test_connection(self) -> bool:
        try:
            self._validate_configured_backends()
            return True
        except (RuntimeError, ValueError):
            return False

    def shutdown(self) -> None:
        self._initialized = False
        memory_logger.info("Memory service shut down")
