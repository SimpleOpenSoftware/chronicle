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

let permission = { granted: false, status: 'undetermined' };
let lastResponse = null;
let openError = null;
const requests = [];
const opened = [];
const alerts = [];
let pushTokenListener = null;
let nativeTokenFetches = 0;
const notifications = {
  PermissionStatus: { DENIED: 'denied' },
  AndroidImportance: { HIGH: 4, DEFAULT: 3 },
  getPermissionsAsync: async () => permission,
  requestPermissionsAsync: async () => permission,
  getExpoPushTokenAsync: async ({ projectId, devicePushToken }) => {
    assert.equal(projectId, 'project-one');
    // Expo requests a native token if none is supplied; iOS emits the listener
    // again when that request completes. Bound the buggy loop in this fixture.
    if (!devicePushToken && pushTokenListener && ++nativeTokenFetches < 5) {
      pushTokenListener({ type: 'ios', data: 'rotated' });
    }
    return { data: 'ExpoPushToken[abcdefghijklmnopqrstuvwxyz]' };
  },
  setNotificationHandler() {},
  setNotificationChannelAsync: async () => {},
  setNotificationCategoryAsync: async () => {},
  getLastNotificationResponseAsync: async () => lastResponse,
  clearLastNotificationResponseAsync: async () => { lastResponse = null; },
  addNotificationResponseReceivedListener: () => ({ remove() {} }),
  addPushTokenListener: listener => {
    pushTokenListener = listener;
    return { remove() { pushTokenListener = null; } };
  },
};
const linking = {
  openURL: async url => {
    opened.push(url);
    if (openError) throw openError;
  },
};

const sourcePath = path.join(__dirname, '../src/services/pushNotifications.ts');
const push = loadTypeScript(sourcePath, {
  'expo-constants': {
    __esModule: true,
    default: {
      easConfig: { projectId: 'project-one' },
      expoConfig: { version: '1.0.0' },
      nativeBuildVersion: '1',
    },
  },
  'expo-linking': linking,
  'expo-notifications': notifications,
  'react-native': {
    Platform: { OS: 'ios' },
    Alert: { alert: (...args) => alerts.push(args) },
  },
  './auth': {
    deriveBaseUrl: value => value.replace('wss://', 'https://').split('/ws')[0],
    fetchAuthed: async (...args) => {
      requests.push(args);
      return { ok: true, status: 200 };
    },
  },
  '../utils/storage': {
    getOrCreateInstallationId: async () => 'installation-one',
  },
});

(async () => {
  permission = { granted: false, status: 'denied' };
  assert.equal(await push.enablePushNotifications('wss://chronicle/ws/audio'), 'denied');
  assert.equal(requests.length, 0, 'denied permission leaves the app usable and registers nothing');

  permission = { granted: true, status: 'granted' };
  assert.equal(await push.enablePushNotifications('wss://chronicle/ws/audio'), 'granted');
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], 'https://chronicle/api/notifications/devices/installation-one');
  assert.equal(JSON.parse(requests[0][1].body).platform, 'ios');

  await push.refreshPushRegistration('wss://chronicle/ws/audio');
  assert.equal(requests.length, 2, 'authenticated launch refreshes the registration');
  const stopTokenListener = push.listenForPushTokenChanges('wss://chronicle/ws/audio');
  pushTokenListener({ type: 'ios', data: 'rotated' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nativeTokenFetches, 0, 'rotation must use the supplied native token without requesting another');
  assert.equal(requests.length, 3, 'native token changes refresh the Expo registration');
  stopTokenListener();

  await push.unregisterPushDevice('wss://chronicle/ws/audio');
  assert.equal(requests.length, 4);
  assert.equal(requests[3][1].method, 'DELETE', 'logout unregisters before clearing auth');

  lastResponse = {
    notification: { request: { content: { data: { action: 'open_immich' } } } },
  };
  const stop = await push.startNotificationTapHandling();
  assert.deepEqual(opened, ['immich://']);
  assert.equal(lastResponse, null, 'cold-start response is consumed once');
  stop();

  openError = new Error('not installed');
  lastResponse = {
    notification: { request: { content: { data: { action: 'open_immich' } } } },
  };
  await push.startNotificationTapHandling();
  assert.equal(alerts[0][0], 'Could not open Immich');

  const source = fs.readFileSync(sourcePath, 'utf8');
  assert.equal(source.includes('audioV2'), false, 'push behavior stays outside Audio V2');
  assert.equal(source.includes('expo-image-picker'), false, 'push behavior never requests photo access');

  console.log('push notification contract tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
