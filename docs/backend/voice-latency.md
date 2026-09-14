# Voice interaction timing

The Wake-Word Lab's **Voice interaction timing** panel reports recent addressed
voice turns. `GET /api/wakeword/latency?limit=20&client_id=…` returns the same data,
scoped to the authenticated user's full ID, including for administrators.

## What the numbers mean

For one uninterrupted turn, speaking + waiting + reply playback = total elapsed:

- Speaking: first to last VAD-positive captured frame, including internal pauses.
- Waiting: last speech frame to the first reply playback observation.
- Reply playback: first playback observation to final playback completion.
- Total: first speech frame to completed reply playback.

An example is 3 seconds speaking + 2 seconds waiting + 5 seconds playback = 10
seconds. Armed/thinking tones and unaddressed ambient speech are excluded.
Playback includes internal pauses, stalls, and any leading/trailing TTS silence.
These are estimates of audible boundaries, not a calibrated microphone measurement.

The breakdown reports separate monotonic spans for streaming transcript wait,
exact-range batch STT, total command transcript resolution, routing/plugins, Hermes
request, notification, interaction-mode handler, TTS, Opus encoding and downlink.
STT total contains STT wait/batch; routing contains agent/notification. Nested spans
must not be summed. The streaming wait is the **post-commit wait for usable final
words**, not the streaming provider's internal compute time. Running spans remain
visible before they complete; errors retain their failed span.

Speech-end to TTS request, speech-end to audio ready, and endpoint/ingress estimates
use cross-host wall-clock alignment. They are explicitly marked clock estimates;
there is no claim of calibrated synchronization or complete attribution of every
millisecond within waiting. Network, device decoding and buffering remain visible
in the total wait even where their individual subspans are not available.

## Clock and identity contract

`CaptureMediaPacket.device_monotonic_timestamp_us` and the corresponding
`CanonicalPcmFrame` field preserve the native device clock. The active-turn segmenter
retains first/last voiced-frame positions separately from pre-roll and endpoint
silence. Live phone packets use a native monotonic origin for relative offsets.
Recovered spool packets are persistence-only and make no live timing claim.

Playback acknowledgements carry native monotonic timestamps. Their source timestamps
are preserved independently from backend receipt time. Reports subtract device
clocks only when audio-session/epoch identity agrees; process spans use their own
monotonic clock. Wall time aligns the waterfall approximately and is displayed in IST.

On iOS, capture uses the tap's AVAudioTime host timestamp; playback onset is estimated
from the first buffer's `.dataPlayedBack` callback minus that buffer's duration.
Completion timestamps are captured before crossing the control queue. Apple documents
that `.dataPlayedBack` includes downstream processing and device latency:
[AVAudioPlayerNode completion](https://developer.apple.com/documentation/avfaudio/avaudioplayernodecompletioncallbacktype/dataplayedback).
On Android, capture keeps encoder input presentation time, started waits for playback
head movement, and done waits for written frames to drain, with a bounded timeout:
[AudioTrack](https://developer.android.com/reference/android/media/AudioTrack).
Callback jitter, VAD frame resolution and audio-route behavior still need physical
validation; these events do not certify the exact first/last phoneme at the listener.

## Persistence and reports

`services/voice_latency.py` owns the producer interface, validation and reduction.
Generated `InteractionTimingEvent` messages enter the existing
`wakeword:interaction-events` stream and its interaction-ledger consumer. The worker
initializes `voice_interaction_events` indexes, persists before acknowledgement and
periodically retries pending deliveries. Event IDs make persistence idempotent.
The collection has 30-day TTL retention based on server insertion time.

Turn identity comprises user, device, capture session/epoch, turn ID and revision; response ID and
generation preserve separate response attempts. Events contain no transcript, reply
text, audio or credentials. The wake activation ledger remains separate evidence of
wake detection; its older ACK-based latency report is not the audible turn report.

Missing events are not zeros. Conflicting clock order is invalid. Failed or cancelled
responses remain visible; a cancelled/incomplete attempt cannot claim a completed
answer. The endpoint limits report count to 100 and each trace to 1,000 events;
truncation is explicit. Summary p50/p95/p99 wait values state their completed sample
count alongside all/failed counts, over the returned recent sample within 30 days.

## Verification and rollout

Tests cross capture normalization, active-turn publication, committed STT resolution,
response coordination, the registered ledger consumer and worker startup, the HTTP
route, report reduction and UI. The controlled 3+2+5 fixture verifies that delayed
backend ACK receipt does not inflate device-clock durations. Other cases cover
missing clocks, invalid ordering, duplicate acknowledgements, multiple responses,
failures, cancellation, tone exclusion and user isolation.

Roll out backend/worker, wake-service generated contracts and the rebuilt mobile app
together. Native compiler/device validation is required for both platforms. Finish
acceptance with a real spoken interaction and an external recording on one acoustic
clock, comparing software timestamps against sound, including the intended output
route (speaker, wired or Bluetooth). Source tests and UI fixtures alone are not a
physical end-to-end latency benchmark.
