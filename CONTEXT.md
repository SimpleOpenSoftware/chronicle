# Chronicle domain context

This file names the concepts agents should use when changing Chronicle. Architectural
decisions live in `docs/adr/`; this is the compact shared vocabulary.

## Core domain

Chronicle is a continuous evidence system that can materialize user-facing semantic
objects. Capture and meaning are different layers.

- **Capture source**: a stable device/channel identity, such as a wearable microphone
  or ScreenPipe input channel.
- **Capture session**: one technical ingest/recovery attempt. Reconnects create new
  sessions; they do not imply new conversations.
- **Audio document**: an immutable, approximately ten-second Opus document owned by a
  user, capture source, and capture session. Its `captured_at` is its wall-clock
  identity.
- **Audio range claim**: ordered audio-document IDs plus exact absolute UTC bounds.
  The first and last documents may be clipped. Claims can share an edge document.
- **Conversation**: a deliberate or detected, user-visible semantic claim over one or
  more audio ranges. It is not an audio persistence container.
- **Processing artifact**: immutable STT or neural diarization evidence over audio
  ranges, with provider/model/configuration provenance and a deterministic retry key.
  Each timed item retains its provider-relative presentation offsets plus one or more
  absolute `audio_spans` identifying the physical range pieces that support it.
- **Transcript revision**: a derived user-facing projection that fuses a Conversation
  claim with transcript and diarization artifacts. Reprocessing produces a revision;
  it does not rewrite raw evidence.
- **Timeline episode**: a revisioned semantic event over multimodal evidence. Its
  audio ranges are authoritative; related Conversation IDs are lineage/navigation.
- **Vault**: the Markdown memory source of truth. It is derived from settled semantic
  inputs and can be regenerated from durable database evidence.
- **Potential memory**: a proposed change derived from one reviewed day and the
  currently accepted Vault. It is not memory and is invisible to retrieval until a
  person accepts it.
- **Review day**: one local day whose Episode account and Potential memories are being
  decided. Review days advance in chronological order.
- **Unexplained evidence**: captured evidence inside a reviewed interval that no active
  Timeline episode accounts for. It is distinct from a capture gap, where no evidence
  exists to interpret.

## Invariants

### Assistant dialogue

- **Dialogue thread**: a continuing exchange between a user and the assistant,
  shared across typed and spoken interaction. It is distinct from a captured Conversation.
- **Utterance**: what the user or assistant communicated. Questions, answers,
  corrections and acknowledgements are meanings of utterances in context.
- **Dialogue task**: work carried through a thread that can wait for input, pause,
  resume or finish independently of a device's voice engagement.
- **Input wait**: a dialogue task awaiting user input after an assistant utterance.
- **Utterance delivery**: evidence of what was displayed or played to a particular
  device, distinct from the assistant's generated content.

### Capture invariants

1. Redis accepting audio never depends on a Conversation existing.
2. Every stored audio document has immutable absolute time and capture identity. An
   audio document never bridges a discontinuity in that capture clock; persistence
   closes the current document before the first post-gap sample.
3. Conversations, episodes, evidence spans, transcripts, and diarization reference
   audio; none owns or reparents it.
4. Playback resolves ordered range claims and clips edge documents precisely.
5. Split, merge, and silence trim edit claims and derived revisions, never audio
   documents or `captured_at`.
6. Provider utterance boundaries are STT evidence, not canonical speaker turns.
7. Pyannote performs neural speaker segmentation. Speaker identification labels those
   turns afterwards; it is not a substitute for segmentation.
8. Pyannote compute windows are bounded at 20 minutes. They constrain peak memory and
   failure scope, not Conversation boundaries. Speaker-count constraints are automatic
   by default and optional when evidence justifies them.
9. An empty pyannote timeline never restores provider utterance boundaries. A marked
   word-timeline fallback may preserve timed ASR coverage, but must own each word exactly
   once and must not claim neural provenance.
10. `Conversation.transcript_versions` is a bounded read cache: retain provider source
   versions and the active derived projection. Full immutable history belongs to
   `ConversationTranscriptRevision`; repeated reprocessing must not grow one MongoDB
   document without bound.
11. Retries converge through deterministic capture, artifact, and segmentation keys.
12. Deleting a semantic claim does not authorize deletion of shared capture evidence.
13. Raw-audio deletion requires a separate explicit retention policy and a complete
    reference check. It is otherwise disabled.
14. Incremental archives contain only changed/new documents and files, plus deletion
    tombstones. An omitted item must be hash-verified in the base chain before restore.
15. A provider interval that crosses audio ranges is never represented by one absolute
    start/end pair. Ordered ranges may have wall-clock gaps or overlap; the interval
    keeps presentation offsets and one physical span per intersected range. Zero-length
    STT points are valid evidence; neural diarization turns must remain positive.
16. `Unknown Speaker N` is a conversation-scoped display description, not a person
    identity. The number distinguishes diarization identities only within that one
    conversation. Global filters, enrollment, memory, and corpus analysis must collapse
    unknowns into a category or key them by conversation plus local label; they must
    never equate matching placeholder text across conversations.
17. A later Review day never observes an unresolved Potential memory. It is derived
    only after every earlier Review day has either changed the accepted Vault or been
    rejected without changing it.

## Important Modules and Interfaces

- Capture Module: `models/audio_capture.py`, `models/audio_chunk.py`,
  `services/audio_stream/producer.py`, `workers/audio_jobs.py`.
- Claim Module: `services/audio_claims.py`. This is the deep Interface for resolving,
  clipping, partitioning, merging, and inversely locating semantic audio.
- Conversation Module: `models/conversation.py` and conversation lifecycle workers.
- Artifact Module: `services/processing_artifacts.py`.
- ScreenPipe Adapter: `services/device_audio_ingest.py`; it persists the complete mixed
  capture before VAD, then materializes only speech-bearing claims.
- Legacy-backup Adapter: `services/legacy_backups.py` plus
  `scripts/import_legacy_backups.py`. It reads historical JSON/WAV once and writes only
  through current capture/claim Interfaces. Obsolete chunk metadata is ignored.
- Archive Module: `services/data_archive.py`.

The Claim Module has high Leverage and should remain deep: callers express a semantic
range operation without knowing Mongo query details or coordinate transforms. Keep
capture/semantic coupling localized at this Seam rather than duplicating it across
controllers and maintenance scripts.

## Conversation materialization

- Deliberate recording/upload: create a visible processing Conversation and claim the
  imported capture.
- Live detected speech: persist capture continuously; create a Conversation only when
  speech detection fires; attach its range when persistence reaches the semantic end.
- ScreenPipe: persist each bounded mixed capture first. Definitive silence creates an
  `AudioEvidenceSpan` but no Conversation. Speech-derived intervals claim the existing
  capture and appear on Recordings.

No placeholder Conversation is required for durability.

## Backups and regeneration

Before destructive corpus work, stop writers, create and verify a new incremental
archive, and keep the prior base chain available. Restore preflight verifies every
omitted base document/file before mutating MongoDB or the filesystem. Incremental
archives cannot be used with replace restore.

Vault regeneration is derived work. Preserve audio, screenshots, conversations,
artifacts, annotations, and timeline evidence; clear/rebuild only the selected derived
stage.

## Service deployment

- **Deployment**: one configured compute capability group and its allowed instances.
- **Instance**: one deployment running on one explicitly registered node.
- **Single owner**: the only node allowed to run a deployment. Failure does not
  transfer ownership.
- **Warm standby**: an allowed, running replica that can serve new requests when
  a preferred instance is unavailable.
- **Deployment authority**: the single owner of the fleet's placement decisions.
- **Activation reservation**: an outstanding permission to activate an instance;
  a placement change must wait for that permission to be returned.
- **Catalog snapshot**: immutable enrolled speaker identities and their embedding
  space. Speaker inference replicas are interchangeable only for the same snapshot.
