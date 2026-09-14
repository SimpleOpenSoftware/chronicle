const assert = require('node:assert/strict');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');
const fs = require('node:fs');

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
  loaded.require = (request) => mocks[request] ?? originalRequire(request);
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const ProcessingProfile = {
  SOURCE_NATIVE: 1,
  DUPLEX_AEC: 2,
  DUPLEX_ISOLATED: 3,
  HALF_DUPLEX: 4,
};
const DeliveryClass = { LIVE: 1, RECOVERED: 2 };
const beginCaptureCalls = [];
const sentPackets = [];
const socketBearerTokens = [];
const socketFrameDurations = [];
const diagnostics = [];
let backendStops = 0;
let socketCloses = 0;
let duringBackendStop = () => {};

class MockAudioV2Socket {
  constructor(options) {
    this.options = options;
    this.activeBinding = null;
    socketBearerTokens.push(options.bearerToken);
    socketFrameDurations.push(options.uplinkFrameDurationMs);
  }

  async connect() {}

  async beginCapture(options) {
    beginCaptureCalls.push(options);
    this.activeBinding = {
      captureSessionId: { value: `capture-${beginCaptureCalls.length}` },
      voiceSessionId: { value: options.deliveryClass === DeliveryClass.LIVE ? 'voice-live' : '' },
      captureEpoch: options.captureEpoch,
    };
    return this.activeBinding;
  }

  sendPacket(packet) {
    sentPackets.push(packet);
    this.options.onPacketAccepted(packet.sequence);
  }

  async stopCapture() {
    backendStops += 1;
    await duringBackendStop();
    this.activeBinding = null;
  }

  voiceReady() {}
  heartbeat() {}
  acknowledgePlayback() {}
  close() {
    socketCloses += 1;
    this.activeBinding = null;
  }
}

const noDiagnostics = new Proxy({
  frameSent: bytes => diagnostics.push(['sent', bytes]),
  packetAccepted: sequence => diagnostics.push(['accepted', sequence]),
}, { get: (target, property) => target[property] ?? (() => {}) });

// React Native's JavaScript performance clock and the native audio clock are
// monotonic, but their origins are not part of the bridge contract. Keep them
// deliberately different so this integration catches cross-clock subtraction.
Object.defineProperty(global, 'performance', {
  configurable: true,
  value: { now: () => 50_000 },
});

const sourcePath = path.join(__dirname, '../src/hooks/useAudioStreamer.ts');
const { useAudioStreamer } = loadTypeScript(sourcePath, {
  '@bufbuild/protobuf': { create: (_schema, value) => value },
  react: {
    useCallback: callback => callback,
    useRef: value => ({ current: value }),
    useState: value => [value, () => {}],
  },
  'react-native': { Platform: { OS: 'ios' } },
  'react-native-base64': { encode: value => value },
  '../../modules/chronicle-duplex-audio': {
    addPlaybackStateListener: () => ({ remove() {} }),
    cancelResponse: async () => {},
    scheduleResponse: async () => {},
  },
  '../protocol/audioV2': {
    CaptureCapabilitiesSchema: {},
    DataPurpose: { NORMAL_CAPTURE: 1 },
    DeliveryClass,
    DeviceKind: { IOS_PHONE: 1, ANDROID_PHONE: 2, OMI: 3 },
    DuplexMode: { FULL: 1, ISOLATED: 2, HALF: 3 },
    EffectStatusSchema: {},
    InputRoute: { BUILT_IN_MIC: 1, BLUETOOTH_HFP: 2, WIRED_MIC: 3, USB: 4, REMOTE: 5 },
    OutputRoute: { SPEAKERPHONE: 1, EARPIECE: 2, HEADPHONES: 3, BLUETOOTH_HFP: 4, USB: 5, REMOTE: 6 },
    PlaybackState: { STARTED: 1, DONE: 2, CANCELLED: 3, FAILED: 4 },
    ProcessingProfile,
  },
  '../protocol/audioV2Socket': { AudioV2Socket: MockAudioV2Socket },
  '../services/auth': { getValidToken: async () => 'fresh-token' },
  '../services/phoneAudioDiagnostics': { phoneAudioDiagnostics: noDiagnostics },
});

(async () => {
  let nativeStops = 0;
  const phoneVoice = {
    captureEpoch: 7,
    capabilities: {
      mode: 'duplex_full',
      input_route: 'built_in_mic',
      output_route: 'speakerphone',
      native_sample_rate: 48_000,
      aec: { requested: true, available: true, enabled: true },
      noise_suppression: { requested: true, available: true, enabled: true },
    },
    stopCapture: async () => { nativeStops += 1; },
  };
  const streamer = useAudioStreamer();

  await streamer.startStreaming(
    'wss://chronicle.invalid/ws/audio',
    { kind: 'phone', ...phoneVoice },
  );

  assert.deepEqual(socketBearerTokens, ['fresh-token'], 'audio must use the managed token source');
  assert.deepEqual(socketFrameDurations, [20], 'phone capture must declare 20 ms Opus');
  assert.equal(beginCaptureCalls.length, 1, 'one button press must create exactly one backend capture');
  assert.equal(beginCaptureCalls[0].deliveryClass, DeliveryClass.LIVE);
  assert.equal(beginCaptureCalls[0].captureEpoch, 7);
  assert.equal(beginCaptureCalls[0].processingProfile, ProcessingProfile.DUPLEX_AEC);
  assert.equal(beginCaptureCalls[0].recoveryBatchId, undefined, 'the clean path has no recovery capture');

  const nativeNow = 1_000;
  streamer.sendFrame('phone', {
    captureEpoch: 6,
    capturedAtMs: 1_780_000_000_000,
    monotonicTimestampMs: nativeNow,
    frameDurationMs: 20,
    opus: new Uint8Array([9]),
  });
  streamer.sendFrame('phone', {
    captureEpoch: 7,
    capturedAtMs: 1_780_000_000_020,
    monotonicTimestampMs: nativeNow + 20,
    frameDurationMs: 20,
    opus: new Uint8Array([1, 2, 3]),
  });
  streamer.sendFrame('phone', {
    captureEpoch: 7,
    capturedAtMs: 1_780_000_000_040,
    monotonicTimestampMs: nativeNow + 40,
    frameDurationMs: 20,
    opus: new Uint8Array([4, 5, 6]),
  });
  assert.equal(sentPackets.length, 2, 'only the active native epoch may send');
  assert.equal(sentPackets[0].sequence, 0);
  assert.equal(sentPackets[0].capturedAtMs, 1_780_000_000_020);
  assert.deepEqual(Array.from(sentPackets[0].opus), [1, 2, 3]);
  assert.deepEqual(
    sentPackets.map(packet => packet.monotonicOffsetUs),
    [0, 20_000],
    'live coordinates must use the native frame clock with its own origin',
  );
  assert.deepEqual(diagnostics, [
    ['sent', 3], ['accepted', 0],
    ['sent', 3], ['accepted', 1],
  ]);

  duringBackendStop = async () => {
    const before = sentPackets.length;
    streamer.sendFrame('phone', {
      captureEpoch: 7,
      capturedAtMs: 1_780_000_000_060,
      monotonicTimestampMs: nativeNow + 60,
      opus: new Uint8Array([4]),
    });
    assert.equal(sentPackets.length, before, 'queued native callbacks must not send after stop');
    assert.equal(nativeStops, 1, 'microphone must stop before waiting for the backend');
  };
  await streamer.stopStreaming();
  duringBackendStop = () => {};
  assert.equal(backendStops, 1, 'the capture has one stop owner');
  assert.equal(nativeStops, 1, 'stopping the stream also stops the native phone session');
  assert.equal(socketCloses, 1);

  const wearableStreamer = useAudioStreamer();
  await wearableStreamer.startStreaming(
    'wss://chronicle.invalid/ws/audio',
    { kind: 'wearable', sourceId: 'neo-1' },
  );
  assert.deepEqual(socketFrameDurations, [20, 60], 'wearable capture must declare 60 ms Opus');
  assert.equal(beginCaptureCalls.length, 2, 'wearable also uses one live capture');
  assert.equal(beginCaptureCalls[1].deliveryClass, DeliveryClass.LIVE);
  await wearableStreamer.stopStreaming();

  for (const failureAt of ['native', 'backend']) {
    const failingStreamer = useAudioStreamer();
    const expected = new Error(`${failureAt} stop failed`);
    await failingStreamer.startStreaming('wss://chronicle.invalid/ws/audio', {
      kind: 'phone',
      ...phoneVoice,
      stopCapture: async () => {
        if (failureAt === 'native') throw expected;
      },
    });
    const closesBefore = socketCloses;
    duringBackendStop = async () => { throw expected; };
    await assert.rejects(failingStreamer.stopStreaming(), error => error === expected);
    assert.equal(socketCloses, closesBefore + 1, 'stop failure must still close the transport');
    const packetsBefore = sentPackets.length;
    failingStreamer.sendFrame('phone', {
      captureEpoch: 7, capturedAtMs: 1_780_000_000_060,
      monotonicTimestampMs: nativeNow + 80, opus: new Uint8Array([5]),
    });
    assert.equal(sentPackets.length, packetsBefore, 'failed stop must leave capture inactive');
  }

  console.log('phone audio streaming tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
