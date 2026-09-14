const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(sourcePath, mocks) {
  const source = fs.readFileSync(sourcePath, 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
    fileName: sourcePath,
  });
  const loaded = new Module(sourcePath, module);
  loaded.filename = sourcePath;
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = request => mocks[request] ?? originalRequire(request);
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const writes = [];
const sourcePath = path.join(__dirname, '../src/services/phoneAudioSelfTest.ts');
const { runPhoneAudioDiagnosticSuite } = loadTypeScript(sourcePath, {
  '../../modules/chronicle-duplex-audio': {},
  '../protocol/audioV2': {
    DataPurpose: { ANNOTATION: 2 },
    DeliveryClass: { RECOVERED: 2 },
    DeviceKind: { IOS_PHONE: 1 },
    ProcessingProfile: { SOURCE_NATIVE: 2 },
  },
  '../protocol/audioV2Socket': { AudioV2Socket: class {} },
  '@/utils/logger': {
    logInfo: (tag, message) => writes.push({ level: 'info', tag, message }),
    logWarn: (tag, message) => writes.push({ level: 'warn', tag, message }),
    logError: (tag, message) => writes.push({ level: 'error', tag, message }),
  },
  'react-native-base64': {
    decode: value => Buffer.from(value, 'base64').toString('binary'),
  },
  'react-native': { Platform: { OS: 'ios' } },
});

const profiles = [];
let frameListener = () => undefined;
let nativeListener = () => undefined;
let routeListener = () => undefined;
let stopCalls = 0;
let sentPackets = 0;
let closed = false;
const progress = [];

const dependencies = {
  now: (() => {
    let value = 1_000_000;
    return () => value += 25;
  })(),
  sleep: async () => undefined,
  addOpusFrameListener(listener) {
    frameListener = listener;
    return { remove: () => { frameListener = () => undefined; } };
  },
  addCaptureDiagnosticListener(listener) {
    nativeListener = listener;
    return { remove: () => { nativeListener = () => undefined; } };
  },
  addRouteChangeListener(listener) {
    routeListener = listener;
    return { remove: () => { routeListener = () => undefined; } };
  },
  async startVoiceSession(options) {
    profiles.push(options.diagnosticProfile);
    nativeListener({
      captureEpoch: options.captureEpoch,
      stage: 'system_change',
      monotonicTimestampMs: 123,
      detail: 'engine_reset ignored=true',
    });
    routeListener({
      captureEpoch: options.captureEpoch,
      reason: 'engine_reset',
      capabilities: {
        mode: 'duplex_full',
        input_route: 'built_in_mic',
        output_route: 'speakerphone',
        native_sample_rate: 48_000,
        aec: { requested: true, available: true, enabled: true },
        noise_suppression: { requested: true, available: true, enabled: true },
        fallback_reason: null,
      },
    });
    if (options.diagnosticProfile === 'voice_processing_hold') {
      for (let index = 0; index < 25; index += 1) {
        frameListener({
          captureEpoch: options.captureEpoch,
          capturedAtMs: 1_700_000_000_000 + (index * 20),
          monotonicTimestampMs: 1000 + (index * 20),
          sampleRate: 16_000,
          channels: 1,
          frameDurationMs: 20,
          audioLevel: 0.25,
          opusBase64: Buffer.from([0xf8, 0xff, 0xfe]).toString('base64'),
        });
      }
    }
    return {
      mode: 'duplex_full',
      input_route: 'built_in_mic',
      output_route: 'speakerphone',
      native_sample_rate: 48_000,
      aec: { requested: true, available: true, enabled: true },
      noise_suppression: { requested: true, available: true, enabled: true },
      fallback_reason: null,
    };
  },
  async getVoiceSessionDiagnostics() {
    const profile = profiles.at(-1);
    const frames = profile === 'voice_processing_hold' ? 25 : 0;
    return {
      diagnosticProfile: profile,
      captureEpoch: profiles.length,
      engineRunning: frames > 0,
      sessionRunning: frames > 0,
      tapInstalled: true,
      tapFrameCount: frames,
      convertedFrameCount: frames * 320,
      opusPacketCount: frames,
      opusByteCount: frames * 3,
      peakAudioLevel: frames ? 0.25 : 0,
      systemChangeCount: 1,
      lastSystemChangeReason: 'engine_reset',
      watchdogEvaluationCount: 1,
      voiceProcessingEnabled: profile !== 'plain_capture_hold',
      audioSessionCategory: 'AVAudioSessionCategoryPlayAndRecord',
      audioSessionMode: 'AVAudioSessionModeVoiceChat',
      audioSessionSampleRate: 48_000,
      audioSessionIOBufferDurationMs: 20,
      inputFormat: '48000Hz/1ch/float32/noninterleaved',
      outputFormat: '48000Hz/2ch/float32/noninterleaved',
    };
  },
  async stopVoiceSession() {
    stopCalls += 1;
    return { restorationSucceeded: true, failureCode: null };
  },
  createSocket(options) {
    return {
      async connect() {
        options.onDiagnostic?.({ stage: 'transport_open' });
        options.onDiagnostic?.({ stage: 'client_hello_sent' });
        options.onDiagnostic?.({ stage: 'server_hello_received' });
      },
      async beginCapture() {
        return {
          captureSessionId: { value: 'capture-diagnostic-full-id' },
          captureEpoch: 0n,
        };
      },
      sendPacket(packet) {
        sentPackets += 1;
        options.onPacketAccepted?.(packet.sequence);
      },
      async stopCapture() {},
      close() { closed = true; },
    };
  },
};

(async () => {
  const result = await runPhoneAudioDiagnosticSuite({
    backendUrl: 'wss://chronicle.example/ws/audio?token=must-not-log',
    jwtToken: 'jwt-must-not-log',
    onProgress: value => progress.push(value),
  }, dependencies);

  assert.deepEqual(profiles, [
    'production',
    'voice_processing_hold',
    'plain_capture_hold',
    'system_tap_format_hold',
  ], 'the bounded matrix must distinguish reset handling, VoiceProcessingIO, and tap format');
  assert.equal(stopCalls, 4, 'every native probe must restore the audio session');
  assert.equal(sentPackets, 25, 'the backend probe must send a bounded half-second payload');
  assert.equal(closed, true, 'the backend diagnostic socket must always close');
  assert.equal(result.nativeProbes.filter(probe => probe.status === 'pass').length, 1);
  assert.equal(result.networkProbe.status, 'pass');
  assert.equal(result.networkProbe.captureSessionId, 'capture-diagnostic-full-id');
  assert.equal(result.status, 'pass');
  assert.ok(progress.some(value => value.phase === 'native'));
  assert.ok(progress.some(value => value.phase === 'network'));
  assert.equal(progress.at(-1).phase, 'complete');

  const logText = writes.map(({ tag, message }) => `${tag} ${message}`).join('\n');
  assert.match(logText, /profile=production status=fail/);
  assert.match(logText, /profile=voice_processing_hold status=pass/);
  assert.match(logText, /capture_session_id=capture-diagnostic-full-id/);
  assert.match(logText, /payload_source=native_mic packets_sent=25 packets_acked=25/);
  assert.match(logText, /event=suite_complete status=pass/);
  assert.doesNotMatch(logText, /jwt-must-not-log|must-not-log/, 'diagnostics must never log credentials');
  assert.doesNotMatch(logText, /opusBase64/, 'diagnostics must never log captured audio payloads');

  const settingsSource = fs.readFileSync(path.join(__dirname, '../app/settings.tsx'), 'utf8');
  const sectionSource = fs.readFileSync(
    path.join(__dirname, '../src/components/PhoneAudioDiagnosticsSection.tsx'),
    'utf8',
  );
  assert.match(settingsSource, /<PhoneAudioDiagnosticsSection/);
  assert.match(sectionSource, /Run full audio check/);
  assert.match(sectionSource, /Open Device Log/);
  assert.match(sectionSource, /25 packet acknowledgements/);

  console.log('phone audio self-test passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
