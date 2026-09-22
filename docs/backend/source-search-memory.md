# Find source material and prepare reviewed memory

Recordings, episodes and meaningful sessions share `/api/search`. The Recordings page defaults to Everything (all three source types), supports selecting title, summary, transcript, speakers and IDs, and keeps query/fields/page in the URL. Typing waits 800 ms; obsolete HTTP requests are cancelled and their results cannot replace newer searches. Matching requires every word across selected fields, supports prefixes from three characters and one insertion/deletion/substitution for words of five or more characters. IDs match literal fragments. Two-character terms such as AI match exact words.

`source_search` is a disposable, revisioned Mongo projection. Current active transcripts supply indexed tokens and bounded passages. Indexed candidates are verified against current ownership, memory space, deletion and transcript revision; episode hits validate exact current snapshot membership; session hits also validate current group membership. Superseded episodes and pending publications are withheld. Search never executes user regex. A durable worker retains its cursor after each source, resumes after interruption, bounds each run and stops a failing source after three attempts. Indexing status is explicit and retryable. Source-save/publication hooks update the projection; the recovery scan catches missed events. A projection-version change starts a fresh resumable scan, without a migration script.

## Search scope vocabulary

- A recording is a user-facing audio selection and its active transcript. It may be deliberately recorded or promoted from detected speech; raw capture evidence remains separate.
- An episode is a bounded Timeline activity interpreted from audio and other evidence. Its published revision has its own title, summary and evidence references.
- A session is a meaningful group of related episodes, possibly crossing dates. A singleton episode is a session when no continuation is established. Prepared undated recording sessions are also searchable without inventing an event date.
- Technical audio capture sessions describe ingest/recovery attempts and are not a source-search scope.

Everything returns distinct, labeled recording, episode and session results. Each result opens its own detail or Timeline session destination. The narrower scopes restrict results, not the meaning of these entities. Search sorting is relevance-based; recording-list sort and starred filtering apply only while browsing. Unknown-speaker hiding is a transcript display option, never a corpus identity filter.

## Recording navigation

The recording page's Timeline & memory panel distinguishes event time from upload time:

- Known capture dates offer Organize this day through the durable reconciliation queue. A persistent organization intent tells session preparation to organize without generating memories for the entire historical day. Linked sessions have titles, times, source passages and a direct Timeline link.
- Unknown event dates offer Prepare undated session. `UndatedSession` retains immutable revisions of a recording selection with exact transcript revision, source identity and audio references. It has no fabricated date or episode confirmation.
- Generating an undated session uses the same account builder, scope validator, review worker, immutable proposal history and per-note approval as dated sessions. Source exclusions and clarifications persist independently of a draft. A source change fences stale work.
- The writer receives the freshly scoped account and citations. Storage timestamps are omitted for undated accounts; processing dates are allowed only for note metadata. Exact physical audio coordinates remain in retained source provenance.

## Accepted context and useful refreshes

Before account construction, retrieve bounded accepted note passages for included or uncertain sources. Excluded/background sources do not participate in context lookup. Retain note paths, hashes, lookup terms, unsuccessful lookups and material questions. Context and policy travel in inference inputs/cache keys; the worker checks for relevant context changes before drafting and the existing approval checker checks accepted-vault freshness before applying.

Accepted edits, additions and deletions queue a deduplicated context-assessment job. A bounded recovery scan compares accepted vault snapshots. Candidate sessions use note dependencies, lookup terms, entities and questions. The recent seven local dates and explicitly selected unresolved history are assessed automatically; opening older material requests an advisory check. No assessment regenerates memory or writes accepted notes.

Assessments retain exact changed content, inference artifacts and one of useful/unrelated/uncertain. Provider failures remain uncertain and have bounded retries. An unrelated later edit does not erase an outstanding useful suggestion. The UI offers New context available and Review suggested refreshes; selecting a session creates a new draft generation. Accepted notes require a correction proposal with their original history preserved.

## Validation boundary

Automated coverage exercises API serialization and owner scoping, real registered worker entry points, word/typo search, stale source checks, resumable indexing, undated selection and approval, dated organization-only requests, persistent exclusions, failed lookups, old-session assessment, bounded assessment retries and manual refresh selection. Expanded-corpus latency measurements and screenshot manifests are machine-local under `untracked/source-memory-20260909/`.

The live introduction is an undated session with proposed People notes. Approving those notes remains a user decision. Verifying useful Friday suggestions against those newly accepted facts and refreshing Friday are subsequent approval-dependent checks. Fixture screenshots cover progress, failure, material questions, exclusion and suggested refreshes; they are labeled separately from live search and introduction approval screenshots.
