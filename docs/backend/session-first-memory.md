# Session-first memory

Timeline retains activity evidence; memory retains useful personal knowledge. A committed semantic group is a session, including a single episode when no supported continuation is known. Chronological display buckets are never memory accounts. Existing human group revisions retain their history.

## Processing order

1. Resolve the exact session revision, including members on another date. Raw episode selection uses the same publication and source-scope validator.
2. Preserve source/device/track identity, captured ranges, content hashes and attribution. Apply durable source exclusions, deferrals and explicit attribution. A clarification is separate user evidence.
3. Exclude routine media and coverage diagnostics from personal claims. Assess unresolved speech only for material questions; independently grounded sources may proceed.
4. Pi investigates bounded source groups through confined, paginated evidence and accepted-vault tools, combines one session account and hands it to a fresh independent Pi reviewer. The reviewer assesses grounding, usefulness and consequential questions, with its own source and vault access. Bounded revisions remain in the harness. Exact quotations, source permissions, exclusions and revision identity are enforced by Chronicle; semantic conclusions remain agent decisions. Relationships retain supporting, duplicate, complementary, unrelated or unresolved capture; overlap alone never authorizes deduplication.
5. Compare the verified account with accepted notes in an isolated vault. The session writer has its own prompt and cannot create Conversation notes. Daily entries are optional. Each proposed note change cites account claims and the precise evidence behind those claims.
6. Empty results complete without approval. Meaningful changes keep immutable generations, individual note approvals and apply-time source/vault freshness fences. Exclusions never silently retract accepted notes.

## Durable work and decisions

`SessionPreparation` owns date organization; `MemoryReviewProposal` owns a session account and its note-change generation. Both dispatch to the existing memory RQ queue. Jobs deduplicate, retain attempts, bound retries and recover after interruption. A large account yields after a bounded work segment and resumes its retained source results in another queued job without spending a failure retry. Generation updates compare state before writing so an excluded or superseded draft cannot be revived by an older worker. A new preparation request arriving during an attempt remains durable subsequent work. Stale current sessions receive new proposals, retaining their old generations. Derived episode summaries are display material and cannot invalidate a source-based account; actual source, role, policy and membership changes still do.

The scheduled window is today and the preceding six local dates. Explicit historical requests persist independently of that window. Recovery scans are bounded; one session's missing prerequisites do not prevent committed sibling sessions from preparing. Progress separates reading source groups, combining accounts, checking claims and drafting notes. No ETA is manufactured.

`Don't remember`, `Later`, attribution and clarification are durable source decisions. Audio exclusions follow immutable capture chunks and intervals through new transcripts and regrouping. A partial exclusion conservatively holds an excerpt that cannot be separated precisely. New independent capture remains eligible. Not an activity remains an episode interpretation correction.

## Interfaces and presentation

`GET /api/timeline/sessions/{date}` returns lightweight revision and progress projections. Source text loads through the revision-fenced `/sources` route on expansion. `/memory`, `/prepare` and `/disposition` dispatch explicit work and decisions. `/review/proposals/{id}/exchanges` loads retained account, claim-check and writer exchanges only for their owner.

Session rows lead with the account and next action. Note changes appear before expanded evidence. Screen OCR, photos, microphone audio and system audio remain distinct. Questions accept direct clarification; detailed source inspection and episode edits remain available. Completed, background and deferred sessions are folded away. Existing paper, ink and orange tokens apply in both themes; amber marks questions and green completed actions.

## Validation

Production API and registered worker entry points are exercised with isolated external dependencies. Coverage includes mixed TV/call evidence, cross-device source identity, retranscription/regroup exclusions, source corrections, cross-midnight ownership, bounded source batches, unknown attribution, empty outcomes, cancelled workers, duplicate requests, retries and concurrent note approval fences.

Machine-local replay artifacts under `untracked/session-memory-20260908/` retain Friday source inputs and exact model exchanges. Vault-writer replay outputs are isolated under `backend/data/session-validation/`. Browser captures and diagnostics are under `artifacts/screenshots/session-memory-20260908/`; these private artifacts are not source assets.

## Pi investigation boundary

`pi_tasks.run_task` is shared by separation, interpretation, session organization, merge synthesis, session accounts, independent review and accepted-context refresh assessment. It exposes only `search_material`, `read_material` and a typed `finish_task`. Evidence and accepted knowledge are immutable, separately scoped stores; no native shell or filesystem tools are available. Prompts state general grounding and uncertainty principles. Investigations choose their own searches and follow references without a prescribed search sequence.

Vault tools issue passage references; the agent selects those references for its context brief and Chronicle retains the exact inspected text. Repeated immutable reads are referenced without duplicating content, with an explicit reread option for recovery after compaction.

Each run retains exact effective prompts, tool schemas, arguments/results, validated output, policy/model settings, retained note passages and reviewer findings. Complete operation records are preserved; redundant cumulative stream snapshots are compacted. A terminal tool closes the harness only after successful validation. Interrupted, invalid or exhausted work stays incomplete and is never cached as an empty result.

Cache freshness follows consulted note hashes and search result fingerprints, including unsuccessful searches. Dependencies accumulate across source subjobs and independent review. Unrelated vault edits can reuse work; changed dependencies invalidate affected results. Context and selection are checked again after inference and before publishing an empty result or starting the note writer. A changed source scope requires rebuilding the account before context assessment. Clarification projection is idempotent.

Expanded session details expose consulted passages, independent review findings and exact tool exchanges. Source-group progress describes completed source work, not a fixed agent investigation sequence. Existing durable job budgets and per-note approval remain authoritative.

## Reading the implementation

`review.generate_memory_review` coordinates four steps: prepare the reviewed account,
draft changes in an isolated vault, validate source freshness, and persist the outcome.
Its ownership lock, cancellation handling and generation fence remain visible there.

`session_accounts` exposes explicit source-account preparation, combination and
revision operations. Validation returns normalized data from a copy; it does not
rewrite the caller's candidate. `pi_tasks.run_task` returns `TaskOutcome(result,
context)`, and account orchestration explicitly carries that context into later work.
Tool handlers keep structured data until the gateway serializes a response; the tool
module owns replay, while the checkpoint module owns durable files and spent budgets.

`SessionWriteInput` carries the reviewed account, context and source references to
`MemoryService.draft_session_memory`. Source permissions are passed to the agent as
data, independently of prompt formatting. `SessionDraftResult` returns provenance and
completion alongside the proposed writes. Session drafting does not create a required
Daily index. A repair must itself complete before its changes can be proposed.

Exact shared evidence is collected before resolving member policies. A reference-only
member excludes that shared evidence until an explicit source decision includes it;
unrelated evidence remains independent. Conflicting attribution stays uncertain.
Historical context comparisons retain inspected passages without presenting them as
complete previous notes or treating an unconsulted note as newly created.
