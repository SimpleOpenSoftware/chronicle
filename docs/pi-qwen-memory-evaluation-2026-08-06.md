# Pi + Qwen memory-agent evaluation

Date: 2026-08-06

## Decision

Pi is the recommended first-class local-Qwen backend for Chronicle memory writes and
searches. In the A30 write evaluation it completed every primary write, had the strongest
semantic coverage, used fewer tokens and tool calls, and materially improved tail latency
over Chronicle's direct tool loop. In the corrected, matched-budget retrieval run on the validation host,
Pi was fully correct on five of six questions versus four of six for Direct and recovered
all expected answerable fact atoms. Direct remains useful as an explicit write-recovery
backend and as the faster retrieval diagnostic path.

This is a canary recommendation rather than a universal model ranking. The write suite
had eight cases and the retrieval suite had six questions. Pi's one imperfect retrieval
answer contained an unsupported embellishment, and one query needed bounded evidence-only
synthesis after reaching the search-round cap.

## What was evaluated

- One NVIDIA A30 with 24 GB VRAM for the audited write suite and context profiling.
- The validation host's NVIDIA RTX 4090 for the corrected retrieval comparison and local smoke test.
- `unsloth/Qwen3.6-27B-GGUF:Q4_K_M`, approximately 16.8 GB.
- llama.cpp b10290 at commit `c8e03ce`.
- A text-only 64K context profile with flash attention, Q8 K/V cache, one parallel
  slot, and no multimodal projector.
- Pi 0.83.0 on Node 22.19.0, with thinking disabled for this Qwen chat template.
- Eight audited write cases, each run into an isolated vault.
- Six retrieval questions over immutable copies of a fixed vault, including two cases
  authored as negative controls and one additional question the vault could not answer.
- The final retrieval comparison used the same validation-host model endpoint, temperature 0.2,
  2,048-token output limit, six search rounds, and source vault for both executors. The
  Codex write column below is a historical semantic comparator, not a contemporaneous
  performance run.

Only aggregates are recorded here. No transcript, question, answer, personal fact,
source identifier, note path, or raw vault content is reproduced.

## A30 context findings

| Profile | Observed GPU memory |
|---|---:|
| 32K, F16 K/V | 19,108 MiB |
| 32K, Q8 K/V, projector loaded | 18,234 MiB |
| 64K, Q8 K/V, projector loaded | 19,482 MiB |
| 64K, Q8 K/V, text only | 18,344–18,358 MiB |

The text-only switch recovered roughly 1.1 GiB at 64K. Prompt processing was about
670 tokens/second. Audited multi-round write prompts reached roughly 14.9K tokens, so
the previous 8K context profile was not valid for realistic memory work. The 64K/Q8
profile leaves useful headroom on a 24 GB A30 while preserving full audited prompts.

## Write results

### Structural and operational

| Executor | Primary canonical completion | Final canonical note | Stalled | Deterministic fallback | Median | P95 | Tokens | Tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Direct | 6/8 | 8/8 | 2 | 2 | 113.1 s | 378.7 s | 1.928M | 251 |
| Pi | 8/8 | 8/8 | 0 | 0 | 172.6 s | 269.2 s | 1.630M | 213 |

Neither output vault had a structural invariant violation. Pi used about 15% fewer
tokens and tool calls and reduced P95 latency by about 29%. Its median was slower, and
its total wall time was 5.6% higher because Direct stopped early on two hard cases and
then relied on deterministic source-preserving fallback notes. Pi completed both cases
through the agent.

Tool errors in both runs were retained for audit. They were recoverable search/edit
attempts rather than silent failures; canonical completion and invariant checks were
evaluated independently of the presence of those errors.

### Blinded semantic audit

Counts cover twelve semantic rubric items. A thirteenth item separately checked for
unsupported claims.

| Writer | Full | Partial | Miss | Incorrect | Fabricated | Safety item |
|---|---:|---:|---:|---:|---:|---|
| Direct Qwen | 8 | 2 | 2 | 0 | 0 | Pass |
| Pi + Qwen | 9 | 1 | 2 | 0 | 0 | Pass |
| Historical Codex | 8 | 0 | 4 | 0 | 0 | Pass |

Pi had the best overall coverage. No rubric-level fabrication was found in any vault.
The result is promising but small-sample: it is not evidence that this ranking will hold
for every transcript class.

## Retrieval results

### Corrected matched-budget run on the validation host

| Executor | Completed | Fully correct | Answerable fact coverage | Correct abstentions | Unsupported additions | Wall time |
|---|---:|---:|---:|---:|---:|---:|
| Direct | 6/6 | 4/6 | 40–45% | 3/3 | 2–3 | 77.2 s |
| Pi | 6/6 | 5/6 | 100% | 3/3 | 1 | 145.3 s |

Two independent vault-only graders agreed on the fully-correct counts, all three
abstentions, Pi's complete answerable-fact coverage, and Pi's one unsupported addition.
The range for Direct reflects different but reasonable fact-atom boundaries; it does not
change the ranking. Only the sanitized aggregates are recorded in this report.

Both executors produced six terminal answers with no errors, invalid references, or vault
mutations. Direct completed without warnings. Pi reached the six-round search cap on one
query; the harness surfaced two warnings and then produced a bounded, tools-disabled
answer from already audited note evidence. Pi was slower in the parity run. Its first
request accounted for roughly half the total wall time, so the sample is too small to
separate cold-start noise from executor overhead.

### Why the earlier retrieval pass was discarded

The first A30 pass was not decision-grade: a pause/resume had removed system-installed
`ripgrep`, Pi's terminal failure sentinel counted as an answer, and both loops could lose
evidence gathered on their last allowed tool turn. The implementation now rejects failure
sentinels as answers, hard-caps tool calls, and permits one bounded tools-disabled
synthesis only from audited evidence. The validation-host rerun above had working `rg` and records
the effective Pi temperature in its provenance.

## Private network topology

The intended production data path keeps the raw model API out of Chronicle's ingress:

```text
Tailnet client
  -> Chronicle HTTPS + JWT authentication
  -> memory write/search service
  -> local Pi subprocess
  -> http://llama-cpp-llm:8080/v1 on Chronicle's private container network
```

The validation host now uses that container-DNS endpoint for backend and worker requests. The optional
host diagnostic ports for the LLM and embedding server bind only to `127.0.0.1`; no
Jarvis endpoint exists, and all Jarvis machines are paused. Tailscale is connected, with
neither Serve nor Funnel configured at evaluation time.

Chronicle's Caddy/backend ports currently listen on all validation-host interfaces. That is
not a Tailscale Funnel or automatic public-internet publication, but it permits LAN access
and relies on the host/router boundary. A strict Tailnet-only cutover should bind Chronicle
ingress to loopback and place Tailscale Serve in front after client URLs and certificates
are checked. That live ingress change was intentionally not made during the model test.

## Implemented product changes

- Independent first-class selectors at `memory.agents.write.backend` and
  `memory.agents.search.backend`.
- Write backends: `direct`, `codex`, and `pi`; search backends: `direct` and `pi`.
- Explicit write recovery selection, including `null` to disable recovery.
- Strict rejection of obsolete flat memory-executor configuration rather than silently
  selecting another model or backend.
- Setup-wizard validation and per-model Pi context/output-limit derivation.
- Pi 0.83.0 on pinned Node 22.19.0 in production and development images.
- Ephemeral Pi model configuration sourced from Chronicle's model registry; no Pi login
  or host `~/.pi` state is required.
- No Pi shell, filesystem, session, context-file, skill, template, theme, extension, or
  built-in tools. Only generated canonical vault tools cross a bearer-authenticated
  loopback gateway.
- Read-only Pi search schemas; hard write/search round and call caps; subprocess timeout,
  termination, output parsing, redaction, and partial-mutation audit handling.
- Vault path, symlink, category, and note-schema validation at the canonical tool
  boundary.
- Privacy-preserving write and retrieval benchmark harnesses with source/copy hashing,
  immutable-source checks, private file modes, and runtime provenance (including Pi's
  effective temperature) without API keys or endpoint URLs.
- Code-owned prompt-injection boundaries that label transcript, title, vault context, and
  retrieved note content as untrusted data after configurable prompts are assembled.
- Readiness checks that exercise the configured memory backend, explicit recovery without
  hidden executor substitution, deterministic lossless source fallback after exhausted
  write recovery, and process-cache resets when memory operations are changed.
- Local Langfuse trace trees for write/search, primary/recovery/fallback attempts, Pi and
  Codex subprocess token usage, and canonical vault tools. Manual content capture is
  opt-in; Langfuse export is batched and explicitly flushed before RQ work-horses exit.

## Validation

- 240 focused backend, executor, recovery, vault-safety, readiness, benchmark, and
  memory-telemetry tests
  passed.
- 62 setup-wizard, defaults, and llama.cpp packaging tests passed in the root setup
  environment: 302 focused tests in total.
- `git diff --check`, import sorting, Python byte-compilation, and Podman Compose parsing
  passed.
- A debug OTEL smoke produced the expected nested `memory_write` → `pi_memory_agent` →
  `pi_model_run` / `memory_tool.*` tree, and its metadata-only output contained no
  synthetic private-content sentinel. The local Langfuse 3.221.1 health endpoint was
  healthy.
- A synthetic local Pi write reached primary canonical completion in 16.4 seconds with
  five tool calls, no fallback, no errors, and no vault invariant violations.
- The validation host's `/readiness` endpoint returns `ready`; its model and memory probes connect.
  The broader legacy `/health` endpoint currently reports `critical` because its Redis
  probe attempts a signal operation from a non-main thread. That separate probe defect
  does not indicate a failed model or memory connection.

## Recommendation and rollout

1. Use Pi for local-Qwen memory writes and searches with the 64K/Q8 text-only profile.
2. Keep Direct as an explicit recovery backend initially; monitor how often recovery is
   invoked rather than treating a fallback note as primary success.
3. Canary Pi retrieval on one non-critical vault and retain Direct as a fast diagnostic
   selector while watching cap warnings and unsupported-answer rate.
4. Run Chronicle's setup wizard before rebuilding/restarting Chronicle with Pi selected.
   Existing obsolete memory keys are intentionally rejected, so the validation host will not silently
   switch to Direct or another model.
5. Roll out writes to one non-critical vault first and monitor latency, truncation, tool-error,
   recovery, and invariant metrics before broad ingestion.

The validation host's live llama.cpp profile and private model routing were changed and restarted, with
rollback copies of the prior configuration retained. The Chronicle Pi application code is
merged into `dev`, but the live backend/workers were not rebuilt or restarted as part of
that merge. They continue running the prior process image until the wizard and deliberate
image rebuild/rollout are performed.

## Cost and limitations

The observed final A30 write suite cost ₹38.88. Earlier A30 context and exploratory work
is estimated at about ₹60, for approximately ₹99 total. The corrected retrieval rerun
used the validation host and added no Jarvis compute cost. Cost is approximate because the earlier
exploratory session was not captured as one final billing record.

Limitations include eight write cases, six retrieval questions, one run per case, no
seed-repetition study, unstable tail percentiles at this sample size, one
hardware/model/quantization combination, and a historical Codex comparator evaluated
under a different runtime. The semantic grades are rubric-based judgments, not an
automated ground-truth metric. Text-only memory usage does not represent multimodal
workloads.
