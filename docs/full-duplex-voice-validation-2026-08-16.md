# Full-Duplex Voice Cutover Validation — 2026-08-16

## Verdict

**AVAILABLE AUTOMATED GATES GREEN — NOT CUTOVER-READY.**

The implementation candidate is commit
`02b7a3b89d22b097935fb60fb86dec59f4d918a0` on
`feature/full-duplex-voice-cutover`. This report is the only change after that
candidate revision.

The Linux/WSL2 host used for validation has no local Apple or Android native build
toolchain and no required physical phones or acoustic replay rig. EAS subsequently
compiled the production iOS archive and submitted build 59 to App Store Connect, but
the standalone Swift tests, Android unit/instrumentation tests, physical-route tests,
and automated acoustic gates were not run. The branch must not be merged, deployed,
released, or described as cutover-ready until those gates pass and this report is
updated with their results.

No production deployment, maintenance window, archive operation, provenance
backfill apply, app release, live Swiggy request, or payment was performed.

## Candidate and environment

| Item | Value |
| --- | --- |
| Branch | `feature/full-duplex-voice-cutover` |
| Implementation candidate | `02b7a3b89d22b097935fb60fb86dec59f4d918a0` |
| Base lineage | `dev`, with curated source changes committed at `210e0c73` |
| Host | Linux validation host |
| OS | Linux 6.6.87.2 WSL2, x86_64 |
| Date | 2026-08-16 UTC |
| Test container engine | Podman |
| Isolated MongoDB host port | 27019 |

The isolated port was required because an unrelated user-owned test MongoDB was
already bound to the repository default port 27018. The unrelated container and the
original dirty checkout were not modified.

## Passing gates

### Backend and contracts

| Command | Result |
| --- | --- |
| `uv run --group test --with ../../extras/chronicle-setup pytest -q --disable-warnings` from `backend` | PASS, 1,489 collected, exit 0 |
| Configured wakeword service pytest suite | PASS, 11 tests |
| Focused duplex property, recovery, response-fence, and Swiggy tests | PASS, 26 tests |
| `python3 scripts/check_import_placement.py` | PASS |

The backend suite covers strict protocol-v1 contracts and golden fixtures, capture
provenance and guarded backfill, voice/capture session binding, resume-token use,
generation fencing, response coordination, committed interval routing, active-turn
recovery, effect-fence behavior, and fake-Swiggy interaction processing.

### Production-entry integration gate

The final isolated stack was started from a clean test-container lifecycle with:

```bash
CONTAINER_ENGINE=podman \
TEST_MONGODB_PORT=27019 \
MONGODB_URI=mongodb://localhost:27019 \
make test PROFILE=mock
```

Result: **PASS — 188 total, 174 passed, 0 failed, 14 profile-declared skips.**

The same ordinary Robot integration tree now includes
`integration/full_duplex_cutover_tests.robot`; it is not a separate optional suite.
Its focused final rerun passed 3/3. It executes the backend contracts inside the real
backend test container and exercises:

- a simulated protocol-v1 phone, bound binary playback, ACKs, cancellation,
  barge-in, stale delivery rejection, route change, resume, duplicate events, and
  worker recovery;
- committed turn routing, stream recovery, response generations, one-response and
  no-stale-playback properties, and the non-idempotent effect fence;
- the complete fake-Swiggy multi-item flow through UPI QR generation, without
  payment.

Robot dry-run validation passed for all 30 selected suites, and repository Robot tag
validation passed for all 34 suites.

### App orchestration

| Command | Result |
| --- | --- |
| `npm run typecheck` | PASS |
| `npm run test:voice-protocol` | PASS |
| `npm run test:phone-duplex` | PASS |
| `npm run check:theme` | PASS |
| `npx expo-modules-autolinking verify` | Duplex module found; PASS with the repository's pre-existing duplicate `@expo/log-box` warning |

### Post-audit TestFlight corrections — 2026-08-17

A max-effort pre-upload audit found that `response.cancel` correctly carries the new
superseding generation while both native engines required equality with the older
playing generation. That would have silently ignored ordinary barge-in cancellation.
The candidate now applies the shared rule on iOS and Android: a matching response ID
(or the native wildcard used during shutdown) is cancelled when the cancellation
generation is at least the playing generation; stale or mismatched cancellation is
ignored. Production native code calls the same Swift/Kotlin policy covered by the new
tests, and the JavaScript orchestration test now uses generation 1 audio followed by
generation 2 cancellation.

The audit also found an unimplemented iOS `processing` background mode. It was removed;
the resolved public Expo configuration now reports only `UIBackgroundModes=["audio"]`.
This is asserted by the phone-duplex orchestration test.

Portable post-fix checks passed:

- `npm run typecheck`
- `npm run test:voice-protocol`
- `npm run test:phone-duplex`
- `npm run check:theme`
- `npx --no-install expo config --type public --json`
- `npx --no-install expo-modules-autolinking verify --platform ios --verbose`

`app/modules/chronicle-duplex-audio/ios/Package.swift` now makes the production-used
Swift cancellation/state policy tests directly runnable with `swift test`. They remain
unexecuted on this Linux host and are a required native-toolchain gate. The Android policy tests
likewise remain unexecuted until an Android toolchain is available.

The executable `scripts/ios-testflight-handoff.sh` fetches an exact remote SHA into
a new detached worktree, runs the portable checks and Swift package tests, and prints
the explicit TestFlight workflow-dispatch command. It does not contain credentials or
upload a build by itself.

The first TestFlight workflow attempt, GitHub Actions run `32039648755`, successfully
validated App Store Connect credentials, created EAS build
`c694b727-545a-4370-9b3f-fb5aa44d16d2`, and allocated build number 57. Native
compilation then failed because the engine observer used the nonexistent
`AVAudioEngine.configurationChangeNotification` member. Candidate `678e9027` replaces
it with Apple's documented `NSNotification.Name.AVAudioEngineConfigurationChange`.
Build 57 was not submitted to TestFlight.

The replacement workflow, GitHub Actions run
[`32040033093`](https://github.com/SimpleOpenSoftware/chronicle/actions/runs/32040033093),
completed successfully. EAS build
[`5b286316-68e4-48f8-8f53-ff773329a3ab`](https://expo.dev/accounts/cupbearer5517/projects/friend-lite-app/builds/5b286316-68e4-48f8-8f53-ff773329a3ab)
compiled app version 1.12.0, build number 58, from workflow head
`80a64ef73ec63d1433f46de310f505bbca519378`. That revision differs from implementation
candidate `678e9027` only by this validation report. EAS submission
[`7360a0f0-298c-417c-86a2-e2a0414e7672`](https://expo.dev/accounts/cupbearer5517/projects/friend-lite-app/submissions/7360a0f0-298c-417c-86a2-e2a0414e7672)
then uploaded the binary successfully to App Store Connect. Apple reported that the
binary was processing; processing and TestFlight installation are not evidence for
the outstanding physical-device or acoustic gates.

Candidate `02b7a3b8` adds one user-facing status line under the phone audio meter,
driven by the existing native playback lifecycle: `Listening`, `Chronicle speaking`,
`Speech interrupted`, `Response complete`, or `Playback failed`. It exposes no route,
AEC, or duplex terminology. The status mapping was added through the public app test
entry point, followed by passing reruns of `npm run test:phone-duplex`,
`npm run test:voice-protocol`, `npm run typecheck`, and `npm run check:theme`.

GitHub Actions run
[`32043466407`](https://github.com/SimpleOpenSoftware/chronicle/actions/runs/32043466407)
completed successfully from exact candidate `02b7a3b8`. EAS build
[`beb6d39c-2f78-4b80-ae3c-ede9963b4a30`](https://expo.dev/accounts/cupbearer5517/projects/friend-lite-app/builds/beb6d39c-2f78-4b80-ae3c-ede9963b4a30)
compiled app version 1.12.0, build number 59. EAS submission
[`89b237b2-55bf-4260-ad50-b58c1e604a88`](https://expo.dev/accounts/cupbearer5517/projects/friend-lite-app/submissions/89b237b2-55bf-4260-ad50-b58c1e604a88)
uploaded it successfully to App Store Connect, where Apple began processing it.

## Attempts and resolved failures

The first mock-profile run exposed 12 failures. They were resolved before the final
gate:

- data-audit split/merge assertions counted a shared boundary chunk twice; the audit
  now compares the unique child range union;
- silent-ingress tests still expected provisional Conversations and legacy chunk
  ownership; they now assert capture-owned chunks and speech-driven Conversation
  creation;
- MongoDB helpers queried the removed chunk `conversation_id`; they now resolve
  ordered chunk IDs from Conversation audio-range claims and assert required capture
  provenance;
- terminal close-event cases used a 30-second wait while the persisted terminal job
  was correctly dispatched just after that boundary; the real-entry tests now allow
  90 seconds.

A rerun against a reused stack encountered stale in-memory client-ID collisions. It
was discarded as non-authoritative; the backend test stack was stopped and the final
full mock gate was run from a fresh lifecycle, producing the 174/0/14 result above.

A direct `.venv/bin/pytest -q` diagnostic invocation was interrupted before producing
a result. The required `uv run --group test --with ../../extras/chronicle-setup ...`
entry point subsequently completed successfully twice; the quiet final invocation is
the result recorded above.

## Unavailable required gates

Native tools `xcodebuild`, `swift`, `gradle`, `adb`, `emulator`, and `sdkmanager` were
not present on the validation host. The cloud iOS archive compile and App Store Connect
upload passed as recorded above; local test execution remains outstanding.

| Required gate | Status | Required evidence before cutover |
| --- | --- | --- |
| iOS production archive compile | PASS | EAS build 58 compiled and uploaded to App Store Connect |
| iOS Swift/XCTest behavior suites | NOT RUN | Engine state, route/interruption/reset, resampling, epoch rejection, and native cancellation ACK |
| Android unit/instrumentation | NOT RUN | Focus/mode/device restoration, AEC/NS, recorder/player failures, route changes, and cancellation ACK |
| iPhone speakerphone | NOT RUN | 20 no-user trials, 20 interruption trials, route-change checks |
| iPhone HFP/AirPods | NOT RUN | Same matrix |
| iPhone wired headphones | NOT RUN | Same matrix |
| Pixel API 31+ speakerphone | NOT RUN | Same matrix |
| Pixel Bluetooth communication audio | NOT RUN | Same matrix |
| Pixel wired headphones | NOT RUN | Same matrix |
| Samsung API 31+ speakerphone | NOT RUN | Same matrix |
| Samsung Bluetooth communication audio | NOT RUN | Same matrix |
| Samsung wired headphones | NOT RUN | Same matrix |
| Android unavailable-AEC fallback | NOT RUN | Native half-duplex behavior and restoration |
| Automated acoustic replay | NOT RUN | At least 500 no-user trials; at least 100 interruptions per route class; zero committed echo turns; stop-latency p95 at most 700 ms; at least 95% opening-word retention |

Each physical-device row must record the exact hardware model, OS version, route,
app/backend revision, failures, fixes, and final rerun. The required acceptance target
per device/route remains zero false committed turns, Conversations, or interruptions
across 20 no-user TTS trials; at least 19 of 20 deliberate interruptions retaining the
opening word and stopping within 700 ms; and no overlapping or stale playback during
route changes.

## Cutover hold

The one-shot cutover procedure in
`docs/full-duplex-voice-modes-plan-2026-08-16.md` remains a future operator action.
Keep this branch out of `dev` and production until every unavailable gate above is
replaced with passing evidence. The guarded provenance command may be exercised
against a disposable archive restore in dry-run and apply modes, but must not be run
against live Chronicle data until the maintenance window, verified archive, and
restore verification are complete.
