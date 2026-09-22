# Shared dialogue

A Chat session is a dialogue thread. Its existing message collection is the only
transcript. `Utterance` projects immutable user or assistant communication; tool
calls, run outcomes, source evidence and playback receipts remain separate.
`Conversation` continues to mean captured evidence.

The domain is in `services/dialogue/models.py`: strict frozen Pydantic values,
discriminated sources and continuations, one foreground wait and at most eight
unfinished tasks. Ordinary Chat requires no task. Explicit choices target a task
revision; free text uses validated interpretation. Pausing retains a wait, cancelling
dismisses it, and time-sensitive waits expire. An aside admits one durable return
prompt. Historical Chat stays read-only through its existing version check.

## Persistence and execution

`dialogue_state` stores a thread's tasks, foreground pointer, audio lease, bounded
command receipts and pending effects in one Mongo document. Revision-checked
replacement admits state and effects atomically on standalone Mongo. No transaction
or migration is required. The interaction worker's registered lifecycle runs the
recovery sweep. Waiting holds neither a model call nor a worker execution slot.

Effect identities derive from task, revision and operation. Executions renew fenced
leases. An expired execution is uncertain, never an unconditional retry. Hermes
reconciles its recorded remote identity; other uncertain external outcomes become
stale. Terminal snapshots archive idempotently in `dialogue_tasks`. A failed or lost
Hermes submission is never silently resubmitted. Redis retains capture, transport,
response generation and playback authority; it is not the dialogue database.

Chat runs are execution attempts. A task can span runs and input methods. The Chat
review path excludes interrupted assistant fragments from completed-answer selection.
Ownership, Memory Space, source evidence and privacy checks apply on admission and
resume; provider work is fenced against privacy changes.

## Adapters

- Chat exposes `/api/chat/sessions/{id}/dialogue` and targeted task `commands`.
  Existing streams include dialogue snapshots and messages include typed utterances.
- Engaged modular and Realtime voice bind explicitly to a Chat thread. Ending voice
  releases its audio lease and preserves capture and tasks. Reconnection does not
  initiate speech. One foreground audio device owns a thread at a time.
- Committed wake input is admitted once. Home Assistant and Hermes wake handlers
  hand work to dialogue, and Instamart's mode processor retains only a thread pointer.
  Plugin executors retain entity resolution, cart review and checkout rules. Passive
  transcript fragments do not authorize actions.
- Hermes's run snapshots expose pending clarification and approval requests. Replies
  carry request ID, revision and response identity. Approvals also bind the exact
  reviewed operation. Restart loss, expiration and cancellation remain explicit.
- Generated Audio V2 includes thread binding and pause/resume/cancel controls. Native
  playback handles incremental Opus with bounded buffering, pre-skip, finish trimming,
  rendered-position acknowledgements and cancellation without stopping capture.

Generated assistant text and delivered speech differ. Voice journals reference
shared utterances and retain execution/delivery evidence. Context uses acknowledged
heard content for voice output. A playback offer alone is not proof of hearing.
Original English, Hindi and Hinglish text is preserved; pronunciation normalization
is separate. Kokoro selects English/Hindi pipelines per request with shared weights
and validated voices. Supported ASR providers receive active-wait hints through the
existing capability and transcription cache contract.

## Verification and release gates

Run dialogue, cohesive Chat/run/review, voice engine/runtime/Realtime/journal,
interaction, Instamart and playback suites through their production entry points.
Use external fakes for approvals, purchases and device playback. Hermes must use
its own `scripts/run_tests.sh`. Web and native TypeScript/build gates are separate
from service health and from physical iOS/Android acoustic acceptance.

Release snapshots include the integrated checkout's required product code. Keep
live web, backend, workers, plugins, generated contracts and speech coordinated;
retain prior mount/image snapshots for rollback. Local research and test artifacts
are not release content. A successful native build does not establish microphone,
interruption or actual speaker behavior on a physical device.
