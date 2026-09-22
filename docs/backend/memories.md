# Memory Service: The Agentic Markdown Vault

> 📖 **Prerequisite**: Read the [Quick Start Guide](../../quickstart.md) first for system overview.

This document explains how Chronicle stores and retrieves memories.

Chronicle has **one** memory provider: `chronicle`. It is an **agentic Markdown vault** — a directory of Obsidian-style notes that is the single source of truth for memories. There is no separate vector database, no embeddings, and no hybrid search index. Memories are plain Markdown files; writing and reading each have an independently selectable agent backend.

**Code References**:
- **Provider**: `src/backend/services/memory/providers/chronicle.py`
- **Memory agents**: `src/backend/services/memory/agent/` (write agent + read/retrieval agent)
- **Memory extraction job**: runs in the post-conversation RQ chain (`memory_extraction_job`), calls `memory_service.add_memory()`
- **Configuration**: `config/config.yml` (memory + LLM sections) + `src/model_registry.py`

## Overview

```
Conversation transcript
        │
        ▼ memory_extraction_job → memory_service.add_memory()
┌─────────────────────────────┐
│  Write agent (_add_memory_  │   direct / Codex / Pi
│  agent)                     │   • record conversation note
│                             │   • surgically edit People/
│                             │     Topics/Category notes
└──────────────┬──────────────┘
               ▼
   data/conversation_docs/<user_id>/   ← the vault (source of truth)
        Conversations/<id>.md
        People/<name>.md
        Topics/<topic>.md
        <Category>/<name>.md
               ▲
               │ ripgrep (grep / glob / read_note tools)
┌──────────────┴──────────────┐
│  Read agent (_search_vault_ │   direct / Pi
│  grep)                      │   • greps the vault
│                             │   • reads relevant notes
│                             │   • synthesizes an answer
└─────────────────────────────┘
               ▲
   /api/memories/search   and   chat `search_memories` tool
```

## Storage: the vault layout

The vault lives on disk at:

```
data/conversation_docs/<user_id>/
```

It is per-user (keyed by the MongoDB ObjectId `user_id`) and organized into note types:

| Note type | Path | Contents |
|-----------|------|----------|
| **Conversations** | `Conversations/<conversation_id>.md` | One note per conversation — the record of what was discussed. |
| **People** | `People/<name>.md` | A durable semantic profile for a person when captured evidence establishes reusable facts about them. |
| **Topics** | `Topics/<topic>.md` | A note per recurring topic. |
| **Categories** | `<Category>/<name>.md` | Other category notes (e.g. places, projects, preferences). |

These are ordinary Markdown files — readable, editable, and grep-able. Because the vault is the system of record, memories survive as durable text rather than as opaque vector rows.

`People/` is deliberately **not** the enrolled-speaker roster or a count of successful
voice identifications. The speaker service owns enrollment, and active transcript
segments retain every recognized name whether or not a person note exists. Timeline
episodes may carry those names as entities. A memory write creates a person note only
when it has durable person-specific knowledge to put in the note; a routine appearance
or a recognized name alone remains a transcript/timeline fact instead of producing a
thin placeholder profile.

If an Immich photo library is configured (`IMMICH_URL`/`IMMICH_API_KEY`, offered by the setup wizard), the `person_photos` cron job (`services/person_photos.py`) matches each `People/<name>.md` note against Immich's people API, stores the person's face-crop thumbnail content-addressed under the vault's `_media/` directory, and embeds a small photo at the top of the note.

## Agent backends

The write and search paths are selected independently under `memory.agents`:

| Backend | Write | Search | Model/auth source |
|---|---:|---:|---|
| `direct` | Yes | Yes | Built-in tool-calling loop using the model resolved by `llm_operations.memory_write` or `memory_search`. |
| `codex` | Yes | No | Codex CLI and ChatGPT subscription auth from the `CODEX_HOME` mount. |
| `pi` | Yes | Yes | Pi CLI using a Chronicle model-registry entry; local llama.cpp, Ollama, and remote OpenAI-compatible models all use the same path. |

Writes also declare `recovery_backend`. It defaults to `direct`, so a failed Codex or
Pi run gets one direct-agent recovery attempt. When direct is already the primary,
the recovery attempt uses `defaults.fallback_llm`. Set it to `null` to disable agent
recovery. The setup wizard preserves an explicitly configured value on reruns rather
than silently resetting it to `direct`.

Codex subscription authentication is also a readiness requirement when Codex is the
configured primary writer. A recovery backend handles a write attempt that fails after
the service is ready; it does not make an unauthenticated Codex primary ready. Run
`codex login` in the host `CODEX_HOME` before starting that configuration.

Pi is installed in the backend image and runs non-interactively with isolated runtime
configuration. Chronicle resolves each memory operation through its model
registry (`llm_operations` and defaults), including the upstream model ID, URL, and API key. No host-side Pi login,
`~/.pi` directory, auth volume, or hand-written `models.json` is required. The shipped
default is Pi 0.85.1 on Node 22.19.0.

The Pi process is not given Pi's built-in shell or filesystem tools. Chronicle disables
them and loads a generated extension containing only the canonical vault tool schemas;
calls cross a short-lived, bearer-authenticated loopback gateway into `VaultTools`.
The search extension receives only the read-only search schemas.

Inside that boundary, Pi runs reuse the public `createReadTool` and `createEditTool`
factories from the pinned package. A run-owned Node helper computes against an
in-memory note buffer; it receives no real vault path or model credentials. Python
owns path/symlink confinement and the complete Redis-locked read → compute → validate
→ save operation, including source attribution and review publication. Independent
remote read/write callbacks would not preserve that transaction. This factory/custom
operations pattern is supported by [Pi's extension API](https://pi.dev/docs/latest/extensions).

`edit_note` requires each old fragment to occur exactly once before native editing.
There is no fuzzy retry: reread and copy a unique exact anchor. This prevents native
fuzzy matching from normalizing neighboring punctuation on the same line. The native
engine still checks overlap, ambiguity and no-op batches. `edit_section`, person merges,
category operations and validation remain Chronicle domain operations. Direct execution
retains its Python edit engine and reader; it does not require Node or Pi.

`read_note` uses zero-based line offsets (translated to Pi's one-based offsets), defaults
to 200 lines and caps at 2000 lines/8000 source characters. Follow its continuation
instructions. `read_slice(path, char_offset, max_chars)` handles very long individual
lines: offsets count Unicode characters from the start of the decoded whole note,
default size is 2000 and maximum is 8000. Both tools accept `refresh=true` to repeat an
unchanged window in Pi. This minimal inspection tool is the selected shell equivalent;
no Bash capability is enabled.

Both retrieval executors collect distinct read/range windows under canonical note
paths for synthesis and citations, replace repeated windows, and retain up to 64,000
characters per note with the latest observations first. Final synthesis applies its
additional total serialized-byte budget. `.base` views remain readable presentation
references and are excluded from memory evidence. Complete tool observations remain in
the run trace.

The native helper starts lazily, serializes requests, uses a 10-second call deadline
and a separate bounded queue wait, and is killed/reaped after transport failure or run
shutdown (including cancellation). No native computation failure commits a vault edit.
The bridge caps individual JSON messages at 32 MiB. Local tests need the pinned Pi
0.85.1 installation on PATH, or `PI_BINARY` pointing to its executable; the helper is
included in the backend wheel as package data.

Write loops are bounded at 48 model/tool rounds. Pi additionally enforces an atomic
192-call write cap at the gateway. Search is bounded at 6 tool rounds and 24 calls.
When that tool budget is exhausted, the direct backend gets exactly one completion with
no tool schemas so it can synthesize from evidence already in its conversation. Pi gets
one fresh, isolated no-tool process only when it has already read note evidence; that
process receives the selected evidence but no Chronicle extension or Pi built-in tools.
Neither final-synthesis path can perform another vault operation. A
truncated, stalled, timed-out, or process-failed write retains every audited partial
mutation but is not reported as complete: Chronicle invokes the configured recovery
backend even when the partial run already produced a valid conversation note. New
People and Topic notes are also checked at the tool boundary for every canonical
section and aggregation embed, so a smaller local model gets a recoverable tool error
instead of silently leaving a malformed long-lived note. A second deterministic guard
rejects a new Topic when at least two and three quarters of its substantive `About`
bullets are already contained by one peer Topic; the same check is repeated after native
filesystem agents finish.

## Write path: the memory agent

Memory extraction runs as part of the post-conversation RQ pipeline. After a conversation closes, `memory_extraction_job` calls `memory_service.add_memory()`, which invokes the **write agent** (`_add_memory_agent` in `providers/chronicle.py`).

Given the conversation transcript and metadata, the selected write backend:

1. Records the conversation as a new `Conversations/<conversation_id>.md` note.
2. **Surgically edits** existing People / Topics / Category notes — adding or updating facts in place rather than blindly appending — and creates a new semantic note only when the evidence establishes reusable, durable knowledge for it.

This is LLM-driven extraction: the agent decides what is worth remembering and where it belongs in the vault.

### Capture evidence is remembered by the day, not the conversation

Continuous ScreenPipe audio does not take this path. Capture is profiled in windows
capped at two hours; detected Conversation claims prefer quiet seams near 30 minutes but
may stay longer while speech is continuous. Remembering per claim would inherit those
operational bounds, while a Timeline episode already carries the semantic bounds.

`add_day_memory` therefore records one **settled local day** of episodes in a single
write, anchored on `Daily/<local_date>.md` rather than under `Conversations/`, which
stays one note per conversation. Chronicle first writes a concise, deterministic
episode index (range, kind, salience and title); the agent then considers only durable
People/Topic/Category facts. It shares the conversation path's executor selection,
recovery backend, bounded rounds, Langfuse spans, and audit ledger
(`MemoryCause.DAY_EPISODES`).

Person notes deliberately separate durable identity from provenance. `## About` holds
stable/current facts such as relationship, work and enduring preferences; it is not a
dated activity log. Conversation-scoped writes may use `## Mentions` as a compact source
pointer, but settled-day writes cannot modify that section at all: Daily/Timeline owns
chronology. The same proposition therefore cannot be copied into both sections by a day
run. Topic/category `## About` sections likewise describe the recurring thing rather
than repeating each day's episode summary.
The vault owner's own Person note is specifically not a second Daily log: speaking,
building, or testing something on a day does not earn a dated self-mention. The owner
note changes only when the day establishes a durable personal fact, placed in `About`.
Other people's mentions remain sparse relationship/provenance pointers rather than
episode synopses on conversation writes; day writes put a materially clarified durable
relationship or role in `About` and leave `Mentions` unchanged. One-off implementation
phrases and events do not mint Topic notes unless they establish durable state that is
likely to matter across days. A fact belongs to one canonical Topic; a narrower note
cannot substantially repeat the `About` scope of a broader peer.

The day digest contains only bounded episode summaries, entities, attributes, and
role/confidence assertions. Raw transcripts and deterministic `Episodes/*.md` artifacts
remain outside the vault. See [Memory segmentation and storage](memory-segmentation-storage.md)
for the complete evidence-to-vault contract.

The episode index has a deterministic source-preserving representation and does not
depend on the model. Semantic People/Topic/Category extraction still has no safe
fallback: a day whose agent does not deliberately finish stays unwritten and is retried
with its diagnostic rather than pretending the missing judgement succeeded.

A ScreenPipe recording that the timeline agent judged **conversational** — a standup, a 1:1 — is separately promoted back into the Recordings list and search. See [Semantic timeline episodes](timeline-episodes.md#a-conversational-episode-promotes-the-recordings-it-cites).

### Checking the write: deterministic gates, then an optional reviewer

A completed write always passes deterministic gates before the run is accepted. When
`memory.agents.write.review` is enabled, a separate semantic reviewer also checks what
was added; the two checks address different failure classes.

**Structure** is decided by a function. `vault_verify.verify_vault_changes` diffs the
vault against a pre-run snapshot and reports illegal paths, a note missing its canonical
sections or aggregation embed, a newly duplicated `## Section`, a case-only collision, a
day write that minted a `Conversations/` note, and a captured-content note at the vault
root (where only complete category hubs belong). It also treats People `Mentions` as an
immutable section for day runs and rejects newly-created Topics whose factual scope is
mostly already carried by another Topic. The Daily episode index is separately
verified against the active Timeline digest and restored mechanically if the model
touches it. Each `Finding` carries a fix instruction addressed to a model. The same
function is offered to the agent as the `verify_vault` tool so it can self-correct
in-run, and re-run server-side so correctness does not depend on it choosing to.
Deterministic findings that survive the bounded repair pass fail the day and leave it
retryable; they are never merely logged and latched as written. The unattended rebuild
finisher also scans the complete regenerated vault as one final structural gate.

**Redundancy cannot be.** Structural verification passes on a perfectly well-formed
bullet that re-records something the vault already holds — which is exactly how a
DeepSeek V4 Pro day write finished with *Vault verification passed* after restating the
phone stand, the chai, and the air-fryer fries that `People/alex.md` and
`People/blair.md` already carried. Deciding that means reading the surrounding notes
and judging whether two differently worded sentences carry the same fact, so a second
agent does it (`agent/review_agent.py`):

- **read-only** — `grep`/`glob`/`read_note`/`read_slice` and a `report_findings` tool, so a review
  cannot mutate the vault it judges;
- **fresh context** — it sees the source and the lines actually added, never the
  writer's reasoning, so it cannot inherit the writer's conviction that the work was
  done;
- **narrow** — only `redundant` (a note of the same kind already records this) and
  `unsupported` (the source does not say this). Off-vocabulary verdicts are dropped;
  `Daily`, `People`, and `Topics` may cover the same evidence at different semantic
  levels, but a People/Topic bullet that merely rephrases the Daily activity log is
  redundancy;
- **never judging what it cannot see** — the source is bounded at the day digest's own
  budget, and if it still had to be cut, `unsupported` is withdrawn for that run. A
  reviewer shown part of a source cannot tell "the source never said this" from "the
  source said it in the part you were not given", and left to judge anyway it picks the
  former in confident detail: cutting a 39,563-char digest at 24,000 hid a gaming
  session, and a true bullet about it was flagged as invented;
- **advisory** — its findings are the same `Finding` type and flow into the same bounded
  repair pass. A reviewer that fails, stalls, or returns nothing parseable yields no
  findings, because a broken reviewer must never block a good write.

It ends with a **forced verdict**: when the round or tool-call budget runs out, the
search tools are withdrawn and the model is asked to report from what it has already
read. Measured on the live vault, that step is what makes the reviewer usable at all —
the first runs exhausted six rounds with the right answer already written in their own
prose ("no mention of Tokyo … this is unsupported") and returned nothing, because they
never got a round in which to report. With nothing left to call but the verdict, there
is no next search to narrate.

Measured on the live vault with `scripts/probe_write_review.py`, which injects bullets
whose verdict is known into a copy of the real notes: **28/32** over two days and eight
trials on Qwen 3.6 27B. Genuinely-new bullets were left alone 8/8 and invented ones
caught 8/8. All four misses are the same borderline bullet, where the reviewer finds the
overlap and then rules it a *new detail about an already-recorded event* — the exception
its own instructions grant — rather than a duplicate.

The reviewer is not an ontology gate. An exact replay of the bad 2026-06-17 write read
both `Agent Control` and `Policy Store`, spent 41,887 tokens over five rounds/eight tool
calls, and returned no finding even though all four Policy Store facts were contained by
Agent Control. That failure is why Topic-scope containment is enforced deterministically
at the write boundary instead of paying the reviewer and hoping it notices.

Disable per deployment with `memory.agents.write.review: false`; it costs one extra
agent run per write that changed anything (~38s against a ~190s day write here).

## Read path: the retrieval agent

Search is served by the **read agent** (`_search_vault_grep`). Both the direct and Pi
search backends are read-only and operate over the vault with four tools:

- `grep` — full-text ripgrep across the notes
- `glob` — find notes by path/name pattern
- `read_note` — inspect a bounded line window of a specific note
- `read_slice` — inspect a bounded character range, including within a very long line
- `search_images` — rank saved images by what they *look* like, returning `Manual Memories/`
  note paths for `read_note`. Backed by the optional
  [ColPali service](../manual-memories.md#enrichment-and-recovery); when that is
  unreachable it returns a plain sentence saying so rather than raising, and the
  images remain findable by grep over their descriptions.

Given a query, the agent greps the vault, reads the relevant notes, and **synthesizes an answer**. The result returned to the caller is:

- the **synthesized answer** as the top result, plus
- the **notes it read** (cited note paths) as supporting context.

There is no vector similarity score — relevance comes from the agent's reasoning over the text it retrieves.

## Chat integration

Chat is always **agentic / tool-calling**. The chat LLM is given a `search_memories` tool; when it needs context about the user it calls that tool, which runs the same agentic vault search and returns the synthesized answer plus the cited note paths. The chat model then incorporates that into its reply.

## Langfuse tracing

When Chronicle's existing `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, and
`LANGFUSE_SECRET_KEY` variables are configured, memory work is exported to the same
local Langfuse OTLP endpoint as the rest of the pipeline. A write trace shows primary
and recovery attempts, the selected executor, model calls, canonical vault-tool calls,
deterministic fallback, latency, token usage, and completion state. A search trace shows
the executor, rounds, tool calls, notes-read count, cap recovery, warnings, usage, and
whether the final answer was usable. Pi's Node subprocess emits equivalent manual model
usage spans, so it is visible beside Direct and Codex rather than becoming a telemetry
blind spot. Langfuse OTLP export is batched, keeping network export off the vault-tool
mutation path even when an agent emits many tool observations. Chronicle explicitly
flushes the completed trace tree at the common `async_job` boundary before a forked RQ
work-horse exits. The flush belongs to the job wrapper rather than an individual
memory decorator because the final model/agent spans end only moments before RQ calls
`os._exit()`; relying on the batch exporter's timer would selectively lose those late
spans while retaining earlier Timeline calls from the same job.

Chronicle's manual memory spans are metadata-only by default: they retain lengths and
SHA-256 fingerprints but omit transcripts, queries, answers, note paths/bodies, tool
arguments, and raw provider errors. Set `LANGFUSE_MEMORY_CAPTURE_CONTENT=true` only
when Langfuse is trusted and local and that personal content is useful for a bounded
debugging session. Content-bearing fields are length-limited; API keys, model endpoints,
and Pi gateway tokens are never emitted by the memory tracer.

The Direct executor also has native child spans from Chronicle's global OpenInference
OpenAI instrumentation. Those spans have independent privacy controls and include model
inputs/outputs by default. Set both `OPENINFERENCE_HIDE_INPUTS=true` and
`OPENINFERENCE_HIDE_OUTPUTS=true` to redact them; because instrumentation is global,
those settings apply to every OpenAI-client call in Chronicle, not only memory. Pi and
Codex subprocess spans use the memory-specific content toggle above.

## Pi operating memory

Pi's learned operating guidance is private per-user state under
`data/pi_operating_memory/<user_id>/`, outside the semantic Obsidian vault. Production
loads only the active `AGENTS.md` snapshot at the start of a run; Pi still chooses which
vault files to search, read, and edit. Generated guidance is not fixed file routing.

Both the writer and read-only retriever load the same stable snapshot. Their completed
`pi_memory` and `pi_memory_search` inference artifacts feed bounded, content-free outlines
to the optimizer. It runs after 25 new traces and has a daily backstop. In the default
`shadow` mode it may write one AGENTS.md proposal or one inert skill/script candidate per
run, but cannot change active guidance or production code. AGENTS.md proposals require distinct
development and holdout evaluation artifacts, review, and promotion. Promotion refuses
a candidate when active guidance changed since that candidate was generated. Every
activation records the prior text, and a later rollback creates another revision instead
of deleting history. Skill
and script candidates remain inspectable but non-executable.

## API Endpoints

- `GET /api/memories/search?query={query}&limit={limit}` — runs the agentic vault search and returns the synthesized answer plus the notes the read agent consulted.
- `GET /api/memories/people/suggestions` — ranks conservative deterministic duplicate-person candidates for review; it never merges automatically.
- `POST /api/memories/people/identity` — records or clears a symmetric `distinct_from` decision in two People notes, with optional stale-revision protection.
- `POST /api/memories/people/merge/preview` and `POST /api/memories/people/merge` — preview and apply a locked deterministic merge. A `distinct_from` decision blocks preview.
- `GET /api/memories/operating-memory` — returns active Pi guidance, content-free candidate metadata, revision metadata, and optimizer progress for the current user.
- `GET /api/memories/operating-memory/candidates/{id}` — reads one bounded candidate for human inspection.
- `POST /api/memories/operating-memory/candidates/{id}/review` and `/promote` — record an evidence-backed decision, then explicitly activate an approved, non-stale AGENTS.md candidate.
- `POST /api/memories/operating-memory/revisions/{id}/rollback` — restores the state preceding a selected revision while retaining rollback history.
- Other `/api/memories/*` management endpoints operate over the vault notes.

## Vault sync to Obsidian (separate feature)

The vault is designed to be edited and viewed directly. The optional **vault sync** feature (in the cross-platform desktop tray, `extras/chronicle-tray/`) syncs `data/conversation_docs/` to an Obsidian vault via Syncthing, so you can browse and hand-edit your memory notes in Obsidian. Human edits made in Obsidian sync back into the vault. This sync is independent of the memory provider itself — the vault on the backend remains the source of truth.

The optional [Chronicle Companion](../obsidian-companion.md) plugin adds explicit,
deterministic maintenance actions such as merging duplicate people. The UI previews and
confirms the action, while the backend performs the locked mutation; no LLM participates
in execution.

## What was removed

For historical context, the previous architecture used **FalkorDB** hybrid search (vector + BM25 + entity-graph BFS over ConvDoc/ConvChunk/ConvEntity nodes and a knowledge graph), plus alternative providers (OpenMemory MCP, Graphiti) and Qdrant/Mem0 vector storage. **All of these have been removed.** There is now a single `chronicle` provider backed entirely by the Markdown vault; the `falkordb` container and `FALKORDB_*` environment variables no longer exist.

## Configuration

The setup wizard asks for write and search backends separately. This nested structure
is the only supported configuration shape:

```yaml
memory:
  provider: chronicle
  timeout_seconds: 1200
  agents:
    write:
      backend: pi
      recovery_backend: direct
      review: true            # read-only review agent over what the write added
    search:
      backend: pi
  backends:
    direct: {}
    codex:
      model: gpt-5.6-terra
      reasoning_effort: low
      sandbox_mode: workspace-write
      timeout_seconds: 900
      max_used_percent: 80
      limit_id: ""
    pi:
      model: muse-glimmer-llm  # Chronicle model-registry entry, not upstream model ID
      timeout_seconds: 900
      context_window: 131072
      max_tokens: 4096        # capped generally; leaves most context for prompts/tools
      thinking: high

llm_operations:
  memory_write:
    reasoning_effort: high
    max_tokens: 8000
  memory_search:
    reasoning_effort: high
    max_tokens: 8000
```

The Pi model may be any OpenAI-compatible LLM entry in the effective registry formed by
`config/defaults.yml` plus name-based overrides from `config/config.yml`. The wizard
rejects missing entries, embeddings, and non-OpenAI API families. For the local Muse
Glimmer service, setup selects `muse-glimmer-llm`, records llama.cpp's exact upstream
Hugging Face identity, and records the context actually selected for that service. API credentials,
when a selected registry model needs them, continue to come from the model definition's
environment-variable reference.
No vector store, embedding model, or graph database is part of memory storage or
retrieval.

Pi limits are derived per selected model rather than pinning every backend to one
machine's context profile. The wizard uses a declared `context_window` (including the
context written by local llama.cpp setup), otherwise a conservative 32K fallback. New
output limits are one quarter of the context up to 4096 tokens, leaving most of the
window for Pi's system prompt, tool schemas, transcript, and multi-round results.
Existing explicit Pi limits are preserved on rerun.

The built-in Qwen 3.8 27B profile declares a 98,304-token context and uses llama.cpp's
OpenAI-compatible route and Qwen chat-template thinking control. Chronicle's Pi memory
workload is text-only even though the registry model can also serve vision. Keep the
4,096-token output cap so the system prompt, transcript, tool schemas, and multi-round
results retain headroom. Thinking is a per-memory-agent setting, not an assumption made
from the registry model's general capabilities. Increase output limits or enable thinking
only after the isolated vault benchmark passes; a model fitting in GPU memory does not
show that its agent loop completes valid writes.
