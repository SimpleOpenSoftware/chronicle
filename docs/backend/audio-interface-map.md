# Chronicle audio interface map

This is the migration ledger for the breaking audio-v2 cutover. `contracts/audio/v2`
owns every cross-language message shape. A boundary is complete only after its public
test and a deployed trace both exist.

| ID | Producer → consumer | Current wire | V2 contract | Status | Evidence |
|---|---|---|---|---|---|
| WS-CONTROL | first-party clients → backend | removed | generated `ClientControl` JSON | green deployed | `test_audio_protocol_v2.py`; live hello/start/stop trace |
| WS-MEDIA | first-party clients → backend | removed | atomic binary `MediaEnvelope` | green deployed | `test_audio_v2_ingress.py`; 55/55 live packet ACKs |
| CONNECTION | socket → client/session lifecycle | cleanup by stable client id | exact `ConnectionId` lease | green | `test_client_connection_lease.py` |
| IOS-CAPTURE | AVAudioEngine → app transport | PCM base64 map | native Opus `CaptureMediaPacket` | amber: source complete, native build pending | Expo typecheck; TestFlight/Xcode compile required |
| ANDROID-CAPTURE | AudioRecord → app transport | PCM base64 map | native Opus `CaptureMediaPacket` | amber: source complete, Gradle/device pending | MediaCodec Opus adapter; Expo typecheck |
| WEB-CAPTURE | Web Audio → backend | paired-header PCM | WebCodecs raw Opus packets | green source/build; browser E2E pending | `RecordingContext.test.tsx`; WebUI production build |
| OMI-NEO | BLE → app/tray → backend | raw Opus via Wyoming | Opus packet adapter | green app + tray adapters; physical device pending | `AudioV2Socket`; shared Python `AudioV2Client` test |
| HAVPE | firmware → relay → backend | device-local JSONL/PCM → relay V2 adapter | generated V2 at backend boundary | green source; physical pending | PCM normalizer/raw-Opus round trip; typed button/playback adapter |
| RECOVERY | phone spool → backend | inferred bare packets | typed recovered packets | green app + ingress + persistence | typed packet ACK; `test_audio_durability.py` |
| DURABLE-REDIS | ingress → persistence | string fields/magic end marker | `CaptureStreamEvent` binary | green V2 producer + persistence consumer | `test_audio_v2_streams.py`, `test_audio_durability.py` |
| REALTIME-REDIS | ingress → ASR/wake/turns | wildcard string-field streams | typed `CanonicalPcmFrame` | green producer + all three consumers | backend streaming tests; wakeword consumer tests |
| MONGO-AUDIO | persistence → claims | fixed Opus chunks | retained; stricter domain types | green deployed | live completed session + 1 canonical 1.1 s chunk |
| STREAMING-ASR | PCM → provider → consumers | dict/JSON words | typed `TranscriptionEvent` | pending | — |
| WAKE | PCM → detector → backend | JSON event | typed `WakeDetection` | pending | — |
| TURN | committed audio → plugin execution | dict fields/blocking dispatch | typed queued turn | pending | — |
| TTS-DOWNLINK | TTS → response → client | JSON header + WAV | Opus playback packets | green backend; amber native build | `test_playback_audio.py`, `test_audio_v2_downlink.py`, Expo typecheck |
| SCREENPIPE | collector → device input | multipart + generic metadata | typed finite ingest descriptor | pending | — |
| FILE-IMPORT | UI/API → processing | controller-specific forms | `FiniteAudioIngestPort` | pending | — |
| ASR-BATCH | backend → ASR service | multipart + dict response | typed request/result | pending | — |
| SPEAKER | backend → speaker service | multipart + untyped response | typed request/result | pending | — |
| MEDIA-HTTP | storage → review/playback | repeated string query formats | representation enum + typed metadata | pending | — |

## Fixed invariants

- Live uplink is 16 kHz mono raw Opus in 20 ms packets.
- Live downlink is 24 kHz mono raw Opus in 20 ms packets.
- PCM S16LE is internal only.
- Recovered packets never enter a live wake, turn, or action path.
- A WebSocket media frame contains its own binding, clock, sequence, and payload.
- Cross-process messages contain no generic maps, `Any`, `Struct`, or stringified JSON.
- Historical Mongo Opus chunks and absolute capture clocks remain unchanged.

## Worklog

### 2026-08-29

- Added the Protobuf v2 source and generated Python/TypeScript bindings.
- Added strict control/media codecs and schema guards.
- Added `/ws/audio` with `chronicle.audio.v2` negotiation and typed hello/start/stop/media handling.
- Added exact physical-connection cleanup so a stale socket cannot remove its replacement.
- Replaced codec-dictionary provenance inference at the capture lifecycle seam with
  `CaptureStartProvenance`; V2 now preserves `source_native` and annotation claims.
- Added separate generated-message Redis lanes. Live capture opens durable and
  realtime consumer groups before acknowledgement; recovered capture creates only
  the durable stream.
- Verified the shared TypeScript package through the Expo app typecheck and WebUI
  production build; verified 34 affected backend lifecycle tests.
- Added contract-versioned job ownership so a V2 session has exactly one Mongo
  persistence consumer and never dual-writes through the legacy WAL.
- Migrated Mongo persistence to decode generated `CaptureStreamEvent` entries. A
  recovered V2 frame now commits with its absolute captured clock and is ACKed only
  after the canonical `AudioChunkDocument` insert.
- Migrated streaming ASR, wake detection, and active-turn segmentation to the three
  V2 realtime consumer groups; removed the extra `voice:frames:*` publication from
  V2 ingress.
- Added `data_purpose` to every canonical frame after the pending-recovery test
  proved a restarted consumer cannot rely on replaying an already-ACKed open event.
- Added generated packet-acceptance acknowledgements and made the Expo durable spool
  retire data only after the backend has accepted it into the Redis durability lane.
- Replaced the first-party Expo transport with one `AudioV2Socket`: recovery drains
  durable-only before live capture; phone and OMI Opus enter the same packet adapter.
- Moved iOS uplink encoding into AVFoundation and Android uplink encoding into
  MediaCodec, eliminating PCM/base64 from the phone-to-JavaScript boundary.
- Replaced response JSON plus side-band WAV storage with generated
  `DeviceDownlinkEvent` messages. TTS WAV is normalized once to 24 kHz mono raw Opus;
  the socket forwards typed offers, atomic media packets, cancellation, and receives
  typed physical-playback acknowledgements.
- Added native raw-Opus playback adapters for iOS and Android. The TypeScript path is
  checked; platform compilers and physical route/AEC behavior remain explicit gates.
- Replaced WebUI PCM-on-wire capture with a generated V2 session and WebCodecs Opus
  encoder. The browser graph may use float PCM internally, but every network frame is
  now an atomic, bound 16 kHz/20 ms raw-Opus `MediaEnvelope`.
- Added a shared Python `AudioV2Client` under `chronicle-client` so tray/wearable and
  relay adapters share connection, binding, sequence, clock, and encoding logic.
- Migrated the local OMI/Neo wearable client to the shared V2 adapter, including
  generated button events and packet-preserving reconnect behavior. Removed the
  public `/ws?codec=...` backend route; `/ws/audio` is now the only audio socket.
- Ran the broad backend suite and isolated its remaining audio failures. The mobile
  spool invariant itself remains sound: V2 binds every acceptance to a backend
  `CaptureBinding` plus packet sequence, while the spool-file identity stays local.
  Replaced the stale source-text assertion with a generated-schema contract test.
- Updated first-party endpoint defaults and the live recorder diagnostics to name
  `/ws/audio`, Chronicle audio v2, and raw Opus; no active UI now advertises the
  removed `/ws` Wyoming transport.
- Re-ran the V2 capture, durability, downlink, response, voice-session, and committed
  turn slice: 69 backend tests pass. WebUI production build and Expo TypeScript check
  also pass; iOS autolinking finds the native duplex module.
- Migrated HAVPE's backend-facing boundary to the shared Python `AudioV2Client`.
  Device-local PCM is normalized and encoded into 16 kHz/20 ms raw Opus; button
  events are generated controls; 24 kHz Opus downlink is decoded and staged for the
  ESPHome media player with physical started/done/cancelled/failed acknowledgements.
- Removed HAVPE's protocol-v1 relay/playback implementation and its obsolete test
  suite. The replacement adapter and shared Python client pass three focused tests,
  including a real libopus encode/decode round trip.
- Extracted capture lifecycle from the old WebSocket implementation, then deleted
  the 2,900-line Wyoming controller, protocol-v1 domain parser, legacy voice-frame
  stream, backend-local duplicate client, and protocol-shaped tests. Backend test
  collection now reaches 1,649 tests with no removed-module import failures (the
  base environment still needs the optional Hypothesis dependency for one module).
- Full backend verification is green: **1,518 passed, 134 skipped, 0 failed** in
  28.63 seconds. Skips are the existing Mongo-at-27018 integration gates; pytest also
  reports one pre-existing unclosed test Mongo client warning after completion.
- Mobile verification is green for TypeScript, durable spool, wearable activation,
  theme policy, and iOS module autolinking. The WebUI audio-v2 tests pass and the
  production build passes. The full WebUI suite has 34 passing tests and two
  unrelated Timeline copy/state failures in `Timeline.test.tsx`.
- **21:03 IST:** restarted the bind-mounted production backend and worker containers;
  `/health` reports the backend, Mongo, Redis, and all 14 workers healthy. A bounded
  authenticated deployment probe negotiated `chronicle.audio.v2`, started a
  source-native annotation capture, received **55/55** generated packet-acceptance
  controls, and stopped cleanly. Mongo contains the completed capture session
  `a421c9-audiov2dep-3971e53a6a254ceab71bb28de68a1bfd` and one canonical 1.1-second
  `audio_chunks` document with captured time 21:03:55 IST. The deleted `/ws` endpoint
  rejects a WebSocket upgrade with HTTP 403.
- **21:00 IST operational finding:** Kraken's root filesystem is 100% utilized with
  about 370 MB free. ScreenPipe's finite-ingest path is actively logging `ENOSPC`
  while writing large temporary WAVs. This did not prevent the small V2 durability
  probe, but it is a separate production retention/capacity incident and makes larger
  capture/build operations unsafe until space is reclaimed or temp storage moves to
  `/mnt/wsl/data`.
- **21:12 IST:** isolated the cutover from the heavily dirty `dev` checkout on
  `feature/audio-v2-cutover`, committed it as `4a73a5ff`, and pushed only runtime
  audio code, generated contracts, clients, tests, and canonical docs. Local wake-word
  datasets/models/reports and this worklog were deliberately excluded. Push-triggered
  TestFlight run **#38 / 33260967365** started on GitHub's macOS 26 runner; repository
  checkout, Node 22, and Xcode 26.2 selection are green while the build continues.
- **21:15 IST:** TestFlight's clean macOS checkout passed `npm ci`, wearable activation,
  TypeScript, durable-spool, theme, and iOS Expo-module autolinking gates. Xcode is now
  compiling the local App Store IPA; submission remains the next automatic step.
- **21:21 IST:** Xcode 26.2 successfully compiled and signed the App Store IPA from
  commit `4a73a5ff`. The workflow advanced to `eas submit --wait`; Apple/TestFlight
  upload and processing acknowledgement are in progress.
- **21:24 IST:** TestFlight run **#38 / 33260967365 completed successfully**. Apple
  accepted the locally compiled IPA through `eas submit --wait`; the audio-v2 build is
  now in TestFlight's normal processing/distribution path and ready for the user's
  install as soon as it appears in the TestFlight app.
- **22:49 IST:** diagnosed a live WebUI packaging regression after the Audio V2
  cutover. The running image predated `@chronicle/audio-contracts`; its build context
  was limited to `backend/webui`, and Compose still mounted the deleted
  voice-protocol-v1 contract. Changed the WebUI image to build from the repository
  root, copy/install the generated Audio V2 file dependency, and mount that same
  contract at runtime. Added a Compose regression gate covering both the build context
  and removal of the stale protocol-v1 mount.
- **22:55 IST:** rebuilt and deployed only `advanced_webui-dev_1`. The corrected image
  installed 445 packages, Vite started in 191 ms, and live requests transformed
  `src/protocol/audioV2.ts`, the contract index, the 102 KB generated protobuf module,
  and the codec module without an import-analysis error. All eight focused WebUI
  Compose-image regression tests pass.
- **23:12 IST:** reproduced the WebUI recorder hanging on “Initializing audio
  session.” The live socket authenticated, but WebUI sent capture epoch 1 with the
  `SOURCE_NATIVE` profile; the backend model requires epoch zero and closed the socket.
  The client then cleared its unresolved `captureStarted` promise without rejecting
  it, leaving the UI stuck forever. WebUI source-native start now owns epoch zero,
  every pending control has a 10-second deadline, and socket close/error/server-error
  rejects pending controls. The backend now rejects invalid source-native epochs at
  the protocol boundary instead of leaking a Pydantic exception from persistence.
  Four WebUI lifecycle tests and five backend Audio V2 ingress tests pass; the live
  Vite module contains the fix and the restarted backend reports HTTP 200 health.
- **23:28 IST:** replaced the Audio V2 ingress dependency on the OMI packet decoder
  with a contract-owned raw-Opus decoder. Chromium legitimately emits a three-byte
  Opus silence frame; the OMI decoder treated every payload of three bytes or fewer
  as a missing wearable header and returned no PCM. A real Chromium/WebCodecs run
  then achieved **807/807 packet acknowledgements**, clean `captureStopped`, two
  canonical Mongo chunks, and no browser error. Raw three-byte Opus is now a codec
  regression fixture in both the backend contract and wearable SDK suites.
- **23:35 IST:** found a second cutover omission: protocol v1 forwarded Redis
  `transcription:interim:{capture_session_id}` messages, while Audio V2 left an
  unused `interim_task` and no transcript event despite the UI advertising live
  text. Added a generated, capture-bound `TranscriptUpdate` server control, a
  subscription barrier before transcription jobs start, WebUI handling, and backend
  plus React boundary tests. A live controlled event crossed Redis, backend,
  Protobuf JSON, WebSocket, and rendered in the browser.
- **23:45 IST:** corrected the streaming provider path against Smallest.ai's current
  realtime contract (`/waves/v1/stt/live?model=pulse`, `close_stream`) and coalesced
  Audio V2's 640-byte/20 ms PCM frames into 3.2 KB provider writes. Previously every
  frame incurred a 50 ms receive poll, throttling a 32 KB/s stream below real time.
  A direct probe using Smallest's public official sample returned six provider
  events. The final deployed Chromium acceptance returned **625/625 packet ACKs,
  10 real transcript updates, one clean capture stop, zero console errors, and zero
  page errors**. The reusable gate is
  `backend/webui/scripts/live_audio_v2_e2e.py`; it requires an explicitly
  supplied non-sensitive 16 kHz mono WAV and asserts transport, transcript rendering,
  and stop lifecycle together.
- **30 Aug, 00:36 IST:** traced the latest apparently healthy WebUI captures beyond
  transport. Mongo held three canonical chunks and Smallest emitted `Hey Hermes`, but
  Redis group `wakeword-v2` had **0 consumers and 1,007 messages of lag**. The running
  wake-word image/process still used the retired stream contract, while `/health`
  accepted any HTTP 200 from that service. Added a typed health-contract check for
  group `wakeword-v2` and pattern `audio:v2:realtime:*`; a legacy service now makes
  Chronicle health report wake-word unhealthy.
- **30 Aug, 00:40 IST:** rebuilt and deployed the wake-word image as
  `493f96634517…`. The clean build also exposed and fixed two reproducibility gaps:
  its protobuf constraint conflicted with the current Pipecat dependency, and its
  Docker ignore rules excluded the shared Audio V2 contract. The deployed service
  loaded both `hey_hermes` and bare `hermes` CUDA models and Chronicle `/health`
  reports the exact V2 group and stream pattern.
- **30 Aug, 00:43 IST:** a real Chromium replay of a held wake-positive fixture sent
  and received **644/644** packet acknowledgements, rendered four transcript updates,
  stopped cleanly, and detected `hey_hermes` at **0.996245** with verifier score
  **0.998610**. Capture session:
  `a421c9-webui-reco-27a64439…`; Mongo persisted two chunks / 12.88 seconds. A longer
  replay also exercised the actual production chain through wake detection, command
  ASR, intent routing, Hermes dispatch, and `acted` ledger state. The probe endpoint
  remains available when a side-effect-free detector assertion is desired.
- **30 Aug, 00:47 IST:** fixed a second asynchronous boundary found by the replay:
  speech materialization could race with `stop_capture`, then fail because the
  capture session was already terminal and no longer allowed an active-conversation
  pointer. Terminal capture sessions now permit creation of their detected
  Conversation without reopening or mutating the pointer. A dedicated regression
  invokes the real materialization/orchestration seam after capture completion.
  Separately, a drained/reclaimed Redis stream can delete its consumer group while
  the wake consumer is blocked; `NOGROUP` is now a normal end-of-stream condition and
  is covered by a consumer regression instead of surfacing as a failed background
  task.
- **30 Aug, 00:57 IST:** the reusable browser gate now snapshots Conversations before
  capture and requires a newly visible row after capture; the public API deliberately
  does not expose technical capture-session IDs, so its earlier string-correlation
  assertion was invalid. Final live run: capture
  `a421c9-webui-reco-fddffa031adf4481adddc18e5201b3cd`, **605/605** packets accepted,
  **10** transcript updates, clean `captureStopped`, clean socket close, and new
  Conversation `4da9bc3e-857f-4fc3-854f-f87a13ed29cf`. Verification is green for
  33 focused backend integration tests, all 22 wake-service tests, the clean-image
  context test, all 6 WebUI recorder lifecycle tests, and the production WebUI build.
  The terminal-session and wake health-contract regressions are also invoked by the
  ordinary Robot `integration/full_duplex_cutover_tests.robot` suite, while the
  Playwright replay remains the deployment acceptance gate spanning the real browser,
  codec, socket, Redis, workers, Mongo, and public API.
  Post-conversation short/detailed summaries currently fail independently because
  the configured OpenRouter key has reached its monthly limit; this does not block
  capture, transcripts, Conversation persistence, wake detection, or Hermes dispatch.

## Remaining verification gates

- Record one physical iPhone capture/recovery/playback trace and one physical
  OMI/Neo or HAVPE capture trace through ingress, Redis, Mongo, inference, and action.
- Restore or replace the exhausted OpenRouter allowance, then verify short summary,
  detailed summary, and memory extraction on one of the E2E-created Conversations.

### 30 Aug production error-storm closure

- **11:00 IST:** the System Events page was receiving roughly **192 error documents
  per 15 minutes** from one live WebUI capture. Two independent causes were present:
  every detected Conversation expanded an exhausted OpenRouter 403 into title, short
  summary, and detailed-summary retries plus duplicate log/job/pipeline events; six
  `open_conversation_job` runs also rejected Smallest word ends extending 0.1–0.25
  seconds beyond the final durable audio sample.
- **11:08 IST:** deployed two entry-point fixes to the bind-mounted backend and worker
  fleet. Transcript artifact persistence now reconciles only sub-frame provider
  boundary error, clipping at most **500 ms** while retaining untouched relative
  provider timestamps in `raw_response`; the generic audio-claim mapper remains strict
  and a one-second overrun is rejected. Registered title/summary jobs classify
  OpenAI-compatible permission/quota denial as `retryable: false` and finish with an
  explicit provider-denied result instead of spending the RQ retry budget.
- **11:10 IST:** real Chromium acceptance passed with capture
  `a421c9-webui-reco-74e87cd76e6042ea86c783a9486136a3`: **602/602** Opus packets
  acknowledged, **11** transcript updates, clean stop/close, and Conversation
  `5d4e9062-17ad-4ac8-9e89-14ed0e9c76e8`. The Conversation reached `completed`, owns
  two canonical chunks plus transcript artifact `cbe2d10f-…`, has no failure
  breadcrumb, and produced **zero error System Events during or after replay**.
- Full backend verification, with Hypothesis supplied ephemerally, is green:
  **1,544 passed, 137 skipped, 0 failed** in 29.49 seconds. The first plain invocation
  stopped at collection because Hypothesis is optional/missing from the base venv;
  the complete rerun did not alter dependencies. Pytest still reports the pre-existing
  unclosed Mongo client warning from Timeline dispatch tests.
- **11:26 IST:** cut Chronicle off OpenRouter after its exhausted allowance continued
  surfacing historical/live errors. `defaults.llm`, `defaults.fast_llm`, and
  `defaults.fallback_llm` now all resolve to Kraken's local `qwen3.8-llm`; the two
  explicit overrides (`memory_write` and `timeline_consolidation`) were moved to the
  same model. Backend and worker runtime inspection confirms every registered LLM
  operation resolves to provider `llamacpp` at `http://llama-cpp-llm:8080/v1`.
  The loaded server model is `unsloth/Qwen3.8-27B-GGUF` (27.3B parameters, Q4_K,
  98,304-token runtime context). A real `plugin_assistant` operation executed inside
  the deployed worker returned `LOCAL_OK`; health is green and no new OpenRouter or
  quota events appeared after cutover. Focused routing/config verification:
  **62 passed**.
- **11:29 IST:** censused and re-triggered exactly the **25 non-deleted
  Conversations** whose title/summary pipeline carried an OpenRouter monthly-limit
  failure. Deleted 75 stale deterministic summary job records, then enqueued 25 fresh
  ordered title → short summary → detailed summary bundles with recovery provenance
  `openrouter_quota_recovery_20260830`; speaker, memory, and event jobs were untouched.
  Local Qwen repaired **25/25 titles and 25/25 short summaries** with zero failed
  recovery jobs and zero new error/OpenRouter System Events. Detailed summaries are
  draining serially on the local GPU (three completed at the last checkpoint, one
  running, 21 queued); they remain deliberately serialized to avoid competing VRAM
  peaks.

### Voice timing observations

- Live phone packets and canonical PCM frames can carry native device monotonic
  timestamps independently of session-relative offsets and wall time.
- Turn segmentation retains first/last voiced-frame timestamps separately from the
  committed interval's pre-roll and endpoint silence.
- Generated `InteractionTimingEvent` observations join STT, response delivery and
  device playback through the existing durable interaction-event consumer.
- See [voice-latency.md](voice-latency.md) for measurement semantics, missing-event
  handling and the required physical-device validation gate.
