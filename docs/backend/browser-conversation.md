# Browser conversation runtime

The desktop Chromium recording page supports an engaged voice conversation while
its existing microphone capture continues. Headphones must be selected explicitly.
Start conversation or a wake activation opens an engagement; subsequent utterances
do not need another wake word. End conversation closes the engagement and its
playback. Stop recording also closes capture. A disconnected engagement is not
automatically resumed or replayed.

## Ownership and data flow

The recording context owns one microphone and one audio-v2 socket. Its capture
worklet continues sending Opus during playback and tool work. Normal ingestion owns
durable capture chunks and archival processing; the voice runtime never writes a
second recording or changes a Conversation's audio claims.

The controller dispatches generated ConversationCommand messages to
`VoiceConversationRuntime`. The existing InteractionStore owns engagement state,
revision checks and atomic state/journal/effect publication. Independent voice
workers execute response, task and control effects outside state transitions.
The existing ResponseCoordinator owns response generations and playback credit.
The browser presents authoritative ConversationState and implements capture,
decoding, bounded rendering and rendered-sample acknowledgements.

Committed utterances use exact dynamic capture intervals. The modular engine
transcribes that interval through the selected batch STT provider immediately,
then streams the `voice_conversation` LLM operation through bounded phrase TTS.
It does not wait for archival transcription to finalize or select another provider
after a failure. Numbers are normalized into spoken English before phrase splitting;
the conversation retains the original answer text.

The optional Realtime engine subscribes independently to canonical audio-v2 frames.
Chronicle remains the turn arbiter: provider automatic turn responses are disabled.
Before committing, the adapter reconciles the exact canonical utterance against its
provider input buffer. It shares the same tools, response delivery and cancellation
contracts. Engine selection is fixed for an engagement and credentials remain on
the backend.

## State, interruption and recovery

Capture binding, engagement ID, state revision and response generation serve
different purposes. Every output operation checks its binding and generation;
End and task cancellation address an exact engagement. A stale control or playback
acknowledgement is rejected without terminating healthy microphone capture.

Speech onset advances the response generation and publishes cancellation atomically,
before final STT. The browser clears queued output and reports the rendered cursor.
Produced audio is not treated as heard audio. Modular history keeps fully heard
phrases and marks an interrupted partial phrase; Realtime context is truncated at
the rendered cursor. Producer completion and device drain are separate events.
Phrase checkpoints precede first/final audio chunks. Crash recovery uses only the
matching response's checkpoints and rendered cursor; a partial phrase gets an
uncertainty marker without guessing which words were heard.

Incremental Opus offers carry codec pre-skip. PlaybackFinished carries the valid
logical sample count so decoder delay and final packet padding cannot lose or add
audible content. The browser prebuffer is 120 ms, browser queue bound is two seconds,
and total application unrendered credit is four seconds. Progress is reported every
100 ms. Phrase audio is limited to 3.82 seconds, reserving 180 ms for packet and
rendering carry. Oversized or stalled providers fail the response explicitly.

Effect leases are renewed and completion is ownership-fenced. Recovery does not
replay an already generating answer. Queued committed input can resume, while
interrupted work gets an explicit status. Response, remote task and control lanes
have independent capacity so slow remote work cannot occupy the cancellation lane.

Idle expiry defaults to 60 seconds when no output or task is active; total engagement
duration defaults to 20 minutes. Further bounds are 100 turns, eight tasks and
512 KiB of history. Reaching an engagement bound ends dialogue, preserving capture
and existing remote task identity.

## Tools and durable evidence

`voice.vault_retrieval_enabled` defaults to false. When enabled,
`search_memories(query)` uses the existing Pi retrieval path with server-bound user
and memory space. Both advertised schemas and gateway execution allow only grep,
glob and read_note. Results distinguish no match, partial evidence and unavailable;
bounded excerpts preserve consulted note paths and revisions. The default deadline
is 15 seconds.

`delegate_to_hermes(request)` uses the existing Hermes run API. The runtime records
submission intent before POST and checkpoints the returned run ID. Ambiguous
submission becomes unknown and is never automatically repeated. End conversation
and speech interruption leave remote work running. Explicit task cancellation
requests remote stop; it does not imply termination or undo an action. Observation
has a persistent ten-minute default deadline. Late completion can update an ended
engagement's journal but cannot revive its speech. The adapter adds no separate
Discord notifications.

Each state revision atomically appends a generated VoiceJournalEntry to Redis.
The monitored journal projector upserts `voice_conversation_journal` in Mongo by
engagement/revision, then acknowledges and removes the projected entry atomically.
Database failures leave pending evidence for retry. The journal includes capture
binding, exact committed turn intervals, task evidence and heard output cursor;
canonical microphone audio remains in the normal capture collections.

## Configuration and validation boundaries

`config/defaults.yml` defines the `voice` settings and the `voice_conversation` and
`voice_realtime` model operations. The modular engine uses the explicitly configured
STT and LLM; TTS uses the existing TTS endpoint. Local output is English. The initial
client requires Chromium audio worklets and WebCodecs; there is no legacy browser
playback route. Native mobile, speaker echo cancellation, music identification and
retrospective audio lookup are separate work.

Tests cover registered WebSocket and worker entry points, state/effect atomicity,
provider failures, generation fencing, bounded audio credit, exact codec sample
accounting, retrieval confinement and independent task lifetime. Automated browser
replay establishes software behavior. Physical headphones, acoustic interruption,
language quality and end-to-end latency require their own measurements before a
deployment readiness claim.

### Concurrent processing observations

`VoiceProcessingUpdate` reports actual backend work independently of the persisted
conversation phase and the browser's audio renderer. Exact committed-turn STT sets
`transcribing`; modular engine provider pulls set `generating_text`, and its current
or prefetched synthesis sets `synthesizing_speech`. These can overlap. Native
speech-to-speech exposes only `generating_response` around provider-event pulls;
it does not invent a separate TTS stage. A finished update clears work flags only,
not audio that the browser still has queued.

A response effect owns one synchronous observer and one publisher task. Observations
replace a single latest snapshot and coalesce at 100 ms. Publishing and terminal
cleanup have a 300 ms deadline and cannot block PCM production. They do not write
interaction revisions, outbox entries, or journals per token or audio frame.

The existing interaction transition admits each response effect with its ID and
transition revision. Its small Redis ownership pointer changes atomically with the
state and outbox only on a new admission or engagement end. Ordinary phrase
checkpoints do not rewrite that pointer. The publisher watches both this pointer
and the client generation, then publishes inside the same transaction. Thus late
work from an interrupted generation or an earlier same-generation tool continuation
cannot publish. The fence does not depend on a retained playback ResponseRecord.

`ConversationState.response_generation` and `response_effect_id` are authoritative
admission. Updates include `effect_id`, that effect's admission `state_revision`, and
an increasing per-effect `sequence`. Clients reject old bindings/generations and
closed effects, and hold at most one newest pending effect snapshot until its ID is
admitted by authoritative state. This handles updates arriving before state and
same-generation tool-result continuations without an unbounded retired-ID set.
An authoritative LISTENING or ENDED state also clears its matching effect's work,
so a dropped best-effort terminal update cannot leave stale activity visible.

These observations are ephemeral, with no latest-status replay cache. A new binding
resets them; durable capture, transcript claims, tool state, and playback accounting
remain owned by their existing boundaries.
