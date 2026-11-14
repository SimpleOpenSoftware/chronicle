"""Trace-driven outer loop for Pi's versioned operating memory."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from backend.config_loader import load_config
from backend.model_registry import check_output_limit
from backend.services.inference_artifacts import (
    canonical_hash,
    load_inference_runs,
    persist_inference_run,
)
from backend.services.observability.system_events import record_event_sync

from .memory_agent import DEFAULT_AGENT_SYSTEM_PROMPT
from .operating_memory import (
    OperatingMemoryStore,
    OperatingMemoryTools,
    optimizer_tool_schemas,
)
from .pi_agent import _invoke_pi, _resolve_pi_config

logger = logging.getLogger("memory_service.agent.operating_memory_optimizer")

_TRACE_MUTATING_TOOLS = frozenset(
    {
        "create_category",
        "edit_note",
        "edit_section",
        "rename_person",
        "write_note",
    }
)
_TRACE_ARGUMENTS = frozenset(
    {
        "path",
        "pattern",
        "query",
        "offset",
        "limit",
        "char_offset",
        "glob",
        "output_mode",
    }
)
_PROMPT_OUTLINE_HEAD = 8
_PROMPT_OUTLINE_TAIL = 12
_OPTIMIZER_MIN_MAX_TOKENS = 8_192

_SYSTEM_PROMPT = """You improve Chronicle's Pi memory agent from its completed
write and retrieval traces.

Your writable state is operating memory, never Chronicle production code or the user's
semantic vault. Optimize for fewer input/output tokens, tool calls, and turns while
preserving verified vault correctness and useful memory updates. There is no human-label
dataset yet, so do not infer quality from low cost alone and do not reward a no-op writer.

Rules:
- Read current operating memory before changing it.
- Change at most one component this run: either AGENTS.md or one skill candidate.
- Generalize only from repeated or clearly evidenced behavior and cite artifact hashes.
- Keep the rationale observational. Do not invent a causal story from a failed outcome:
  claim that the agent searched, read, wrote, verified, or stopped early only when the
  cited trace's explicit tool sequence shows that action. Say that causality is unknown
  when the trace establishes correlation but not cause.
- Give Pi strategies and decision criteria, not a fixed list of vault files to open.
- Do not impose fixed search, read, tool-call, or turn budgets. Pi decides how much
  evidence the current case requires; stop criteria may be conditional and evidence-based.
- Never put numeric limits or one-shot mandates into guidance. Forbidden forms include
  "at most one search", "one negative result is sufficient", "verify once", or a
  mandatory discovery sequence. Pi chooses the files and note families from the task.
- An unchanged call is usually redundant, but do not claim it can never become useful:
  an intervening vault mutation or a concrete unresolved question may justify it.
- Do not require glob, grep, or read before writing, and do not treat one empty search as
  universally definitive. Search scope and the unresolved decision determine sufficiency.
- Write small incremental guidance for the evidenced failure mode. Do not restate the base
  actor workflow, note templates, mutation order, or verification contract.
- Keep trace statistics, artifact details, and observed runtime limits in the candidate
  rationale, not in runtime AGENTS.md. Runtime guidance should stay small and reusable.
- Never disguise a call count as a sufficiency rule (for example, "one scoped search
  answers it"). Define sufficiency by whether the evidence covers the unresolved decision.
- Call the proposal tool promptly after reviewing current operating memory and the trace
  evidence. Do not repeatedly redraft or narrate the proposal instead of recording it.
- Tool-use protocol: Your first response must call `read_operating_memory`. After receiving
  it, your next response must either record one proposal with `propose_agents_memory` /
  `replace_agents_memory`, or explicitly conclude that no change is justified. Do not
  spend a response drafting prose without making the required tool call.
- The base actor contract included in the task is immutable and authoritative. Never
  contradict its note semantics, schemas, edit rules, or verification requirements.
- Preserve useful existing guidance. Current vault evidence always outranks a heuristic.
- A proposed script is inert design material; never claim it is executable or deployed.
- It is valid to make no change when the traces do not justify one.
"""


def _settings() -> dict[str, Any]:
    memory = load_config().get("memory", {}) or {}
    value = memory.get("operating_memory", {}) or {}
    return dict(value) if isinstance(value, dict) else {}


def _optimizer_runtime_config(config: Any) -> Any:
    """Give the meta-optimizer room to reason and still emit its tool call."""

    values = dict(vars(config))
    values["max_tokens"] = check_output_limit(
        values.get("max_output_tokens"),
        max(_OPTIMIZER_MIN_MAX_TOKENS, int(values.get("max_tokens") or 0)),
        setting="operating_memory optimizer minimum max_tokens",
        model=str(values.get("model_entry") or values.get("model") or ""),
    )
    return type(config)(**values)


def _event_outline(stdout: str) -> list[dict[str, Any]]:
    """Extract one record per explicit tool event without replaying private content.

    Pi 0.83 emits cumulative ``message_update`` snapshots. Recursively inspecting those
    snapshots duplicates earlier calls and can fill the optimizer's bounded outline
    before it reaches the end of a run. Only lifecycle-level execution events are
    authoritative here.
    """

    starts: dict[str, dict[str, Any]] = {}
    outline: list[dict[str, Any]] = []
    turn = 0
    # JSONL uses LF framing. Do not use ``splitlines``: it also splits at U+2028,
    # which Pi can legally emit inside a JSON string copied from a transcript.
    for line in stdout.split("\n"):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "turn_start":
            turn += 1
            continue
        if event_type == "tool_execution_start":
            call_id = str(event.get("toolCallId") or "")
            raw_arguments = event.get("args")
            arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
            starts[call_id] = {
                "turn": max(turn, 1),
                "tool": str(event.get("toolName") or "unknown")[:120],
                "arguments_hash": canonical_hash(arguments)[:12],
                "arguments": {
                    key: str(value)[:240]
                    for key, value in arguments.items()
                    if key in _TRACE_ARGUMENTS
                    and isinstance(value, (str, int, float, bool))
                },
            }
            continue
        if event_type != "tool_execution_end":
            continue
        call_id = str(event.get("toolCallId") or "")
        compact = starts.pop(
            call_id,
            {
                "turn": max(turn, 1),
                "tool": str(event.get("toolName") or "unknown")[:120],
                "arguments_hash": canonical_hash({})[:12],
                "arguments": {},
            },
        )
        result = event.get("result")
        output_text = ""
        if isinstance(result, str):
            output_text = result
        elif isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                output_text = "".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict)
                )
        compact.update(
            {
                "is_error": bool(event.get("isError", False))
                or output_text.lstrip().startswith("Error:"),
                "output_chars": len(output_text),
            }
        )
        outline.append(compact)
    if len(outline) <= 80:
        return outline
    return [*outline[:40], *outline[-40:]]


def _trace_signals(
    outline: list[dict[str, Any]], *, total_rounds: Any
) -> dict[str, Any]:
    """Derive auditable efficiency signals from the explicit tool sequence."""

    call_signatures = [
        json.dumps(
            {
                "tool": call.get("tool"),
                "arguments_hash": call.get("arguments_hash"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for call in outline
    ]
    signatures = Counter(call_signatures)
    max_consecutive = 0
    current_consecutive = 0
    previous_signature = None
    for signature in call_signatures:
        if signature == previous_signature:
            current_consecutive += 1
        else:
            current_consecutive = 1
            previous_signature = signature
        max_consecutive = max(max_consecutive, current_consecutive)
    max_cycle_repetitions = 1
    repeated_cycle_length = 0
    for start in range(len(call_signatures)):
        # The outline retains at most 80 calls, so 40 is the longest cycle that
        # can have two complete repetitions and therefore be evidenced at all.
        max_period = min(40, (len(call_signatures) - start) // 2)
        for period in range(1, max_period + 1):
            block = call_signatures[start : start + period]
            repetitions = 1
            while (
                start + (repetitions + 1) * period <= len(call_signatures)
                and call_signatures[
                    start + repetitions * period : start + (repetitions + 1) * period
                ]
                == block
            ):
                repetitions += 1
            if repetitions > max_cycle_repetitions or (
                repetitions == max_cycle_repetitions
                and repetitions > 1
                and (repeated_cycle_length == 0 or period < repeated_cycle_length)
            ):
                max_cycle_repetitions = repetitions
                repeated_cycle_length = period
    mutation_turns = [
        int(call.get("turn") or 0)
        for call in outline
        if call.get("tool") in _TRACE_MUTATING_TOOLS
    ]
    calls_by_turn = Counter(int(call.get("turn") or 0) for call in outline)
    rounds = int(total_rounds) if isinstance(total_rounds, int) else 0
    return {
        "explicit_tool_events": len(outline),
        "read_calls": sum(call.get("tool") == "read_note" for call in outline),
        "search_calls": sum(call.get("tool") in {"glob", "grep"} for call in outline),
        "mutation_calls": len(mutation_turns),
        "tool_error_count": sum(bool(call.get("is_error")) for call in outline),
        "tool_output_chars": sum(
            int(call.get("output_chars") or 0) for call in outline
        ),
        "read_output_chars": sum(
            int(call.get("output_chars") or 0)
            for call in outline
            if call.get("tool") == "read_note"
        ),
        "unique_call_count": len(signatures),
        "repeated_call_count": sum(count - 1 for count in signatures.values()),
        "max_consecutive_identical_calls": max_consecutive,
        "max_repeated_cycle_repetitions": max_cycle_repetitions,
        "repeated_cycle_length": repeated_cycle_length,
        "max_calls_in_single_turn": max(calls_by_turn.values(), default=0),
        "turns_with_parallel_fanout": sum(
            call_count >= 5 for call_count in calls_by_turn.values()
        ),
        "turns_before_first_mutation": (
            max(0, min(mutation_turns) - 1) if mutation_turns else None
        ),
        "turns_after_last_mutation": (
            max(0, rounds - max(mutation_turns)) if mutation_turns else None
        ),
    }


def _trace_summary(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    outline = _event_outline(str(record.get("stdout") or ""))
    prompt_outline_limit = _PROMPT_OUTLINE_HEAD + _PROMPT_OUTLINE_TAIL
    if len(outline) <= prompt_outline_limit:
        prompt_outline = outline
    else:
        prompt_outline = [
            *outline[:_PROMPT_OUTLINE_HEAD],
            *outline[-_PROMPT_OUTLINE_TAIL:],
        ]
    summary = str(result.get("summary") or "")
    signals = _trace_signals(outline, total_rounds=result.get("rounds"))
    write_outcome = record.get("benchmark_write_outcome")
    write_outcome = write_outcome if isinstance(write_outcome, dict) else None
    optimization_feedback = record.get("optimization_feedback")
    optimization_feedback = (
        optimization_feedback if isinstance(optimization_feedback, dict) else None
    )
    reported_tool_calls = result.get("tool_calls")
    if isinstance(reported_tool_calls, int) and not isinstance(
        reported_tool_calls, bool
    ):
        signals["reported_tool_calls"] = reported_tool_calls
        signals["unoutlined_tool_calls"] = max(
            0, reported_tool_calls - signals["explicit_tool_events"]
        )
    return {
        "artifact_hash": record.get("artifact_hash"),
        "recorded_at": record.get("recorded_at"),
        "operation": request.get("operation"),
        "record": request.get("record"),
        "model": request.get("model"),
        "usage": result.get("usage", {}),
        "rounds": result.get("rounds"),
        "tool_calls": result.get("tool_calls"),
        "touched_count": len(result.get("touched") or []),
        "removed_count": len(result.get("removed") or []),
        "verified": result.get("verified"),
        "truncated": result.get("truncated"),
        "errors": (result.get("errors") or [])[:8],
        "warnings": (result.get("warnings") or [])[:8],
        "read_count": len(result.get("read_paths") or []),
        "failure_bucket": result.get("failure_bucket"),
        "judge_correct": result.get("judge_correct"),
        "direct_diagnostic_correct": result.get("direct_diagnostic_correct"),
        "evidence_judge_correct": result.get("evidence_judge_correct"),
        "evidence_missing_count": int(result.get("evidence_missing_count") or 0),
        "benchmark_question_type": request.get("benchmark_question_type"),
        "benchmark_write_outcome": write_outcome,
        "optimization_feedback": optimization_feedback,
        "summary_chars": len(summary),
        "summary_sha256": canonical_hash(summary),
        "efficiency_signals": signals,
        "tool_outline": prompt_outline,
        "tool_outline_retained_events": len(prompt_outline),
        "tool_outline_omitted_events": len(outline) - len(prompt_outline),
    }


def _trace_priority(record: dict[str, Any]) -> tuple[int, int, int, int, int, int, int]:
    """Rank traces by actionable evidence without inspecting semantic content."""

    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    outline = _event_outline(str(record.get("stdout") or ""))
    signals = _trace_signals(outline, total_rounds=result.get("rounds"))
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    warnings = (
        result.get("warnings") if isinstance(result.get("warnings"), list) else []
    )
    write_outcome = record.get("benchmark_write_outcome")
    write_outcome = write_outcome if isinstance(write_outcome, dict) else {}
    bad_write_outcome = bool(write_outcome) and (
        not bool(write_outcome.get("writer_ok"))
        or not bool(write_outcome.get("completed"))
        or not bool(write_outcome.get("agent_completed"))
        or not bool(write_outcome.get("primary_canonical"))
        or bool(write_outcome.get("fallback_written"))
        or not bool(write_outcome.get("final_canonical"))
        or not bool(write_outcome.get("vault_invariants_ok"))
        or int(write_outcome.get("introduced_issue_count") or 0) > 0
    )
    is_writer = request.get("record") != "search"
    no_mutation_writer = is_writer and not (result.get("touched") or [])
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    total_tokens = int(
        float(usage.get("total_tokens") or usage.get("totalTokens") or 0)
    )
    return (
        int(
            bool(errors)
            or bool(warnings)
            or bool(result.get("truncated"))
            or bool(result.get("failure_bucket"))
            or bad_write_outcome
        ),
        int(no_mutation_writer),
        int(signals["max_calls_in_single_turn"]),
        int(signals["max_consecutive_identical_calls"]),
        int(signals["repeated_call_count"]),
        int(result.get("rounds") or 0),
        total_tokens,
    )


def _select_optimizer_records(
    records: list[dict[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    """Select high-signal traces plus recent controls, returned oldest first."""

    if len(records) <= limit:
        return list(reversed(records))
    signal_limit = max(1, limit * 3 // 4)
    ranked_indices = sorted(
        range(len(records)),
        key=lambda index: _trace_priority(records[index]),
        reverse=True,
    )
    selected = set(ranked_indices[:signal_limit])
    for index in range(len(records)):
        if len(selected) >= limit:
            break
        selected.add(index)
    # ``records`` is newest first. Give Pi a chronological batch so it can see
    # whether later behavior improved without receiving semantic transcript text.
    return [records[index] for index in sorted(selected, reverse=True)]


async def _optimize_user(
    user_id: str,
    records: list[dict[str, Any]],
    store: OperatingMemoryStore,
    *,
    component: str | None = None,
    iteration_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if component not in {None, "agents"}:
        raise ValueError("component must be None or 'agents'")
    mode = str(_settings().get("mode") or "shadow").strip().lower()
    if mode not in {"shadow", "active"}:
        raise ValueError("memory.operating_memory.mode must be 'shadow' or 'active'")
    config = _optimizer_runtime_config(
        _resolve_pi_config("memory_harness_optimization")
    )
    trace_summaries = [_trace_summary(record) for record in records]
    mode_instruction = (
        "You are in SHADOW mode. Record an AGENTS.md proposal or inert skill "
        "candidate only; no proposal becomes active production guidance."
        if mode == "shadow"
        else "You are in ACTIVE mode and may update active AGENTS.md guidance."
    )
    iteration_section = ""
    revision_reminder = ""
    if iteration_feedback:
        iteration_section = (
            "A prior shadow candidate was rejected by measured benchmark evidence. "
            "Treat the following as corrective evidence: do not preserve the rejected "
            "guidance wholesale, and do not optimize away the counterexample.\n\n"
            "<iteration_feedback>\n"
            f"{json.dumps(iteration_feedback, ensure_ascii=False, indent=2)}\n"
            "</iteration_feedback>\n\n"
        )
        contract_rejection = iteration_feedback.get("contract_rejection")
        if isinstance(contract_rejection, dict):
            failed_checks = contract_rejection.get("failed_checks")
            passed_checks = contract_rejection.get("passed_checks")
            if isinstance(failed_checks, list) and failed_checks:
                revision_reminder = (
                    "\n\nBefore recording a proposal, compare its literal runtime guidance "
                    "against every independently failed contract check below. The "
                    "proposal must actually satisfy them; a rationale claiming compliance "
                    "does not compensate for contradictory guidance.\n"
                    "<failed_contract_checks>\n"
                    f"{json.dumps(failed_checks, ensure_ascii=False, indent=2)}\n"
                    "</failed_contract_checks>\n"
                    "Make the smallest revision that fixes those failures. Do not add "
                    "new ordering rules or otherwise regress checks that the same review "
                    "already passed.\n"
                    "<already_passing_contract_checks>\n"
                    f"{json.dumps(passed_checks or [], ensure_ascii=False, indent=2)}\n"
                    "</already_passing_contract_checks>"
                )
    prompt = (
        f"{mode_instruction}\n\n"
        + (
            "For this benchmark iteration, evaluate AGENTS.md guidance only. "
            "Do not propose a skill or script.\n\n"
            if component == "agents"
            else ""
        )
        + "The following base actor contract is read-only. Your proposal may make its "
        "execution more economical but must not replace or contradict it.\n\n"
        "<base_actor_contract>\n"
        f"{DEFAULT_AGENT_SYSTEM_PROMPT}\n"
        "</base_actor_contract>\n\n"
        "Analyze these new production Pi traces and improve one operating-memory "
        "component only when evidence warrants it.\n\n"
        + iteration_section
        + json.dumps(trace_summaries, ensure_ascii=False, indent=2)
        + revision_reminder
    )
    tools = OperatingMemoryTools(store, mode=mode)
    schemas = optimizer_tool_schemas(mode)
    if component == "agents":
        schemas = [
            schema
            for schema in schemas
            if schema.get("function", {}).get("name") != "write_skill_candidate"
        ]
    events, gateway = await _invoke_pi(
        store.root,
        prompt=prompt,
        system_prompt=_SYSTEM_PROMPT,
        schemas=schemas,
        config=config,
        max_tool_rounds=3,
        max_tool_calls=4,
        load_vault_skill=False,
        telemetry_attributes={
            "chronicle.memory.pi.phase": "operating_memory_optimizer",
            "chronicle.memory.optimizer.trace_count": len(records),
        },
        tool_handler=tools,
    )
    artifact_hashes = [str(record["artifact_hash"]) for record in records]

    def persist_optimizer_run() -> tuple[str, str]:
        return persist_inference_run(
            operation="pi_operating_memory_optimizer",
            request={
                "user_id": user_id,
                "mode": mode,
                "component": component,
                "source_artifact_hashes": artifact_hashes,
                "iteration_feedback": iteration_feedback,
                "system_prompt": _SYSTEM_PROMPT,
                "prompt": prompt,
                "model": config.model,
                "provider": config.provider,
            },
            stdout=events.stdout,
            stderr=events.stderr,
            result={
                "mode": mode,
                "summary": events.summary,
                "errors": events.errors,
                "usage": events.usage,
                "rounds": events.rounds,
                "tool_calls": max(events.tool_calls, gateway.call_count),
                "touched": tools.touched,
                "truncated": events.truncated,
            },
            metadata={"returncode": events.returncode},
            reusable=False,
        )

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="pi-memory-artifact"
    ) as executor:
        _request_hash, optimizer_hash = await loop.run_in_executor(
            executor, persist_optimizer_run
        )
    processed = not events.errors and not events.truncated and events.returncode == 0
    if processed:
        store.mark_processed(artifact_hashes)
    if tools.touched or events.errors:
        candidate_kind = "candidate generated" if tools.touched else "run failed"
        record_event_sync(
            severity="warning" if events.errors else "info",
            category="memory",
            source="pi_operating_memory_optimizer",
            title=f"Pi operating-memory optimizer {candidate_kind}",
            detail=str(events.errors[0] if events.errors else events.summary)[:2_000],
            user_id=user_id,
            metadata={
                "mode": mode,
                "source_trace_count": len(records),
                "touched": tools.touched,
                "optimizer_artifact_hash": optimizer_hash,
                "error_count": len(events.errors),
            },
        )
    return {
        "user_id": user_id,
        "mode": mode,
        "source_traces": len(records),
        "touched": tools.touched,
        "optimizer_artifact_hash": optimizer_hash,
        "errors": events.errors,
        "processed": processed,
    }


async def run_operating_memory_optimizer(*, force: bool = False) -> dict[str, Any]:
    """Optimize users with enough new traces; ``force`` is the daily backstop."""

    settings = _settings()
    threshold = max(1, int(settings.get("operation_threshold", 25)))
    max_traces = max(1, min(int(settings.get("max_traces_per_user", 20)), 100))
    scan_limit = max(max_traces, int(settings.get("artifact_scan_limit", 2_000)))

    def load_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            load_inference_runs("pi_memory", limit=scan_limit),
            load_inference_runs("pi_memory_search", limit=scan_limit),
        )

    # Use one bounded filesystem task and close its executor before returning.
    # This avoids relying on the process-wide executor used by unrelated jobs.
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="pi-memory-traces"
    ) as executor:
        write_records, search_records = await loop.run_in_executor(
            executor, load_records
        )
    records = sorted(
        [*write_records, *search_records],
        key=lambda record: str(record.get("recorded_at") or ""),
        reverse=True,
    )[:scan_limit]
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        request = record.get("request")
        user_id = request.get("user_id") if isinstance(request, dict) else None
        if isinstance(user_id, str) and user_id:
            by_user[user_id].append(record)

    optimized: list[dict[str, Any]] = []
    skipped = 0
    for user_id, user_records in by_user.items():
        store = OperatingMemoryStore(user_id)
        processed = set(store.load_state().get("processed_artifact_hashes", []))
        new_records = [
            record
            for record in user_records
            if record.get("artifact_hash") not in processed
        ]
        if not new_records or (not force and len(new_records) < threshold):
            skipped += 1
            continue
        selected = _select_optimizer_records(new_records, limit=max_traces)
        with store.optimization_lease() as acquired:
            if not acquired:
                skipped += 1
                continue
            optimized.append(await _optimize_user(user_id, selected, store))
    return {
        "mode": "daily" if force else "operation_threshold",
        "optimized": optimized,
        "optimized_users": len(optimized),
        "skipped_users": skipped,
    }


async def run_operating_memory_threshold_job() -> dict[str, Any]:
    """Hourly poll: run only for users with the configured number of new traces."""

    return await run_operating_memory_optimizer(force=False)


async def run_operating_memory_daily_job() -> dict[str, Any]:
    """Daily backstop: process any new traces even below the threshold."""

    return await run_operating_memory_optimizer(force=True)
