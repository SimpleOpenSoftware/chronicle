# Cohesive Chat

Chat follows one flow: ask, attach conversations, inspect sources, then review and save.
New sessions carry `interaction_version: 2`. Older sessions remain readable, including retained citations and runs, but reject message, attachment, extraction, deletion and run-deletion writes. Starting a new chat does not copy historical content.

## Conversations and evidence

`SourceResults` shares search state and result rendering between Chat and Recordings; each page owns its own navigation. Empty queries return recent authorized sources. Chat accepts up to ten unique recording, session or episode references. Attachment updates use the same renewable interaction lock as replies, persist a context-change marker, and affect subsequent turns. Omitted updates preserve attachments; an empty list removes them.

`chat_sources` resolves canonical evidence within the user's destination. Search excerpts never become evidence. Unavailable attachments block the turn. `chat_context.ChatContext` divides a shared 32,000-character initial budget fairly, redistributing unused allocation, and requires an attached source identifier for bounded additional reads. Passage IDs are namespaced by source. Answers retain source references, revisions, coverage and exact cited excerpts independently of later attachment changes.

The vault provider's `retrieve_for_chat` returns `VaultRetrieval`: a synthesized answer, actual supporting notes, coverage and run identity. The synthesized answer is never counted as a note. Chat's public request has no memory count or execution-budget control. Attached conversations establish what happened; vault retrieval and earlier discussion are distinguishable background, and suggestions are not commitments.

One Sources disclosure groups conversation passages and vault notes per answer. Message history and streaming completions return the same persisted typed evidence. Source links retain playback offsets. View run exposes execution details separately.

## Reviewed saving

Session-scoped save proposals snapshot every message through the latest completed assistant turn, retaining roles, evidence and run references. The writer works on an isolated copy of the destination vault. Proposal generation and inspection do not write accepted notes or emit accepted-memory audit records.

`memory.note_review` shares snapshotting, note diffs, path checks, freshness hashes, vault locking, atomic writes and application journaling with Timeline review. `chat_review` owns chat-specific orchestration. The exact generation and selected change IDs bind approval. Changed notes reject application and require a refreshed preview. Repeated approval does not rewrite notes. An interrupted application retries the same selection and journal; a partially applied proposal cannot be discarded or replaced.

The durable `chat_save_proposals` ledger records queued, generating, pending, applying, applied, discarded and failed states. The registered `chat_note_review` cron resumes interrupted jobs, while each attempt has a linked chat run. Accepted writes use idempotent per-note audit records; downstream events become eligible only after application. A Memory Space's events remain scoped through the existing deferred-event workflow.

API operations under `/api/chat/sessions/{session_id}`:

- Create or update with plural `sources`; read resolved context from `/sources`.
- Generate a whole-chat proposal with `POST /save-proposals`.
- Reload its state with `GET /save-proposals/latest`.
- Approve, discard or retry with `POST /save-proposals/{proposal_id}/{action}`, passing its exact `generation` and `selected_change_ids`.

All operations enforce session ownership and Memory Space boundaries. No migration or compatibility write path is provided.

## Verification

Backend coverage lives in `test_cohesive_chat.py`, `test_chat_sources.py`, `test_chat_runs.py`, `test_chat_turn_progress.py`, `test_selective_memory_review.py`, `test_source_search_flow.py` and the memory audit tests. These exercise real entry points with external dependencies faked, including proposal generation, application recovery, audit failures and queue isolation. UI coverage in Chat, SourceSearch and Recordings tests covers recent search, drafts, citations, read-only history, unavailable attachments, streamed errors and selective approval.

Live acceptance uses a disposable Memory Space, two clearly synthetic conversations, an actual model answer, persisted evidence and an explicitly approved test save. Authenticated screenshots and test artifacts stay machine-local.

## Execution lineage

Every new chat turn creates a durable `chat_runs` record before resolving source evidence. Both user and assistant messages link to its `run_id`; failed turns remain in the chat's Run history even when no answer exists. Runs record success, failure, incomplete output, or cancellation. Running attempts renew a 90-second lease; an expired lease is displayed as interrupted rather than assumed successful. This also makes process termination visible when no finalizer could run.

`services/chat_runs.py` owns run recording. It records source snapshots, ordered rounds, model attempts and tool calls with parent IDs. Complete prepared model requests, assembled responses (including provider termination and reported usage), raw/effective tool arguments and results use the existing content-addressed inference store under `chat_run_<run_id>`, with reuse disabled. Direct nested vault retrieval shares the owning tool's context; provider-owned artifacts such as Pi retrieval are linked and copied into the run, preserving their original compaction metadata. A retained compacted provider artifact is not an exact unabridged provider transcript. No deterministic replay is promised.

The source snapshot and prepared requests distinguish all available evidence from the passages actually shown to the model. Source citations continue to use the existing answer evidence panel. Optional OpenTelemetry spans carry run/step IDs and outcomes; local durable history does not require an exporter. Recording failures or finalizer timeouts explicitly mark a run's details incomplete. Compression and filesystem persistence run off the event loop. Streaming generators close in the request context; bounded, shielded finalization preserves partial output on disconnect.

Completion is emitted only after successful answer persistence and final-round recording. A provider token cutoff is an incomplete run, not a complete saved answer. The UI offers View run, expandable exact retained inputs/results, and JSON export on desktop and phone. Run reads, exports and deletion require access to the owning chat and Memory Space. Payloads include personal context and remain local by default. History is retained until explicitly deleted, or until its chat is deleted; active runs cannot be deleted. Previously existing provider archives retain their own lifecycle. No past executions are reconstructed or backfilled.

Run validation: `tests/test_chat_runs.py` exercises the production generator, streaming/nonstreaming termination, cancellation at a token and after completion, disabled/failed detail storage, timed-out finalization, nested retrieval, concurrent context isolation, source snapshots, ownership, Memory Space access and deletion. Chat UI tests cover failed-run readback, answer links, and draft preservation.
