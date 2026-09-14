const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

// Exercise the production hook's capture and playback entry points. Native audio,
// storage and the socket are adapters; no device or running backend is contacted.
let playbackListener;
const sockets = [];
class Socket {
  constructor(options) { this.options = options; this.packets = []; this.acks = []; sockets.push(this); }
  async connect() {}
  async beginCapture() { this.activeBinding = { captureSessionId: { value: 'capture' } }; return this.activeBinding; }
  voiceReady() {}
  sendPacket(packet) { this.packets.push(packet); }
  acknowledgePlayback(...args) { this.acks.push(args); }
  async stopCapture() { this.activeBinding = null; }
  close() {}
}
const protocol = new Proxy({}, { get: (_, key) => new Proxy({}, { get: (_, value) => `${String(key)}.${String(value)}` }) });
const mocks = {
  '@bufbuild/protobuf': { create: (_, values) => values },
  '@react-native-community/netinfo': { addEventListener: () => () => {} },
  react: { useCallback: fn => fn, useEffect: () => {}, useRef: value => ({ current: value }), useState: value => [value, () => {}] },
  'react-native': { Platform: { OS: 'ios' } },
  'react-native-base64': { encode: s => Buffer.from(s).toString('base64') },
  '../../modules/chronicle-duplex-audio': {
    addPlaybackStateListener: fn => { playbackListener = fn; return { remove() {} }; },
    addRouteChangeListener: () => ({ remove() {} }),
    cancelResponse: async () => {}, scheduleResponse: async () => {},
  },
  '../protocol/audioV2': protocol,
  '../protocol/audioV2Socket': { AudioV2Socket: Socket },
  '../services/auth': { getValidToken: async () => 'fixture-token' },
  '../services/phoneAudioDiagnostics': { phoneAudioDiagnostics: new Proxy({}, { get: () => () => {} }) },

};
const sourcePath = path.join(__dirname, '../src/hooks/useAudioStreamer.ts');
const loaded = new Module(sourcePath, module);
loaded.filename = sourcePath;
loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
const requireOriginal = loaded.require.bind(loaded);
loaded.require = request => Object.hasOwn(mocks, request) ? mocks[request] : requireOriginal(request);
loaded._compile(ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), { compilerOptions: {
  esModuleInterop: true, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
} }).outputText, sourcePath);

(async () => {
  const hook = loaded.exports.useAudioStreamer();
  const effect = { requested: true, available: true, enabled: true };
  const config = { kind: 'phone', captureEpoch: 3, capabilities: { mode: 'duplex_full', input_route: 'built_in_mic', output_route: 'speakerphone', native_sample_rate: 48000, aec: effect, noise_suppression: effect }, stopCapture: async () => {} };
  await hook.startStreaming('ws://localhost/ws/audio?token=fixture', config);
  try {
    const frame = (mono, wall) => ({ captureEpoch: 3, capturedAtMs: wall, monotonicTimestampMs: mono, frameDurationMs: 20, opus: new Uint8Array([1, 2, 3]) });
    hook.sendFrame('phone', frame(900000, 1700000000000));
    hook.sendFrame('phone', frame(900020, 1699996400020)); // wall clock jumps back an hour
    hook.sendFrame('phone', frame(900040, 1700003600040)); // wall clock jumps forward
    assert.deepEqual(sockets[0].packets.map(p => p.monotonicOffsetUs), [0, 20000, 40000]);
    assert.deepEqual(sockets[0].packets.map(p => p.deviceMonotonicTimestampUs), [900000000, 900020000, 900040000]);
    playbackListener({ captureEpoch: 3, responseId: 'reply', generation: 1, state: 'started', monotonicTimestampMs: 905000.5 });
    assert.equal(sockets[0].acks[0][3], 905000.5, 'native playback observation must survive the JS bridge');
    playbackListener({ captureEpoch: 2, responseId: 'stale', generation: 1, state: 'started', monotonicTimestampMs: 1 });
    assert.equal(sockets[0].acks.length, 1, 'stale capture epoch must not enter the trace');
  } finally { await hook.stopStreaming(); }
  await hook.startStreaming('ws://localhost/ws/audio?token=fixture', config);
  try {
    hook.sendFrame('phone', { captureEpoch: 3, capturedAtMs: 1700004000000, monotonicTimestampMs: 1000000, frameDurationMs: 20, opus: new Uint8Array([1]) });
    assert.equal(sockets[1].packets[0].monotonicOffsetUs, 0, 'new capture resets only the relative origin');
    assert.equal(sockets[1].packets[0].deviceMonotonicTimestampUs, 1000000000);
  } finally { await hook.stopStreaming(); }
  console.log('voice timing capture/ACK clock tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
