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
  loaded.require = (request) => mocks[request] ?? originalRequire(request);
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const application = {
  applicationId: 'com.chronicle.app',
  applicationName: 'Chronicle',
  nativeApplicationVersion: '1.15.0',
  nativeBuildVersion: '79',
};
const constants = {
  expoConfig: { version: 'wrong-config-version' },
  executionEnvironment: 'standalone',
};
const updates = {
  isEmbeddedLaunch: true,
  updateId: 'update-123',
  channel: 'testflight',
  runtimeVersion: '1.15.0',
  createdAt: new Date('2026-09-06T18:25:35Z'),
};

async function testLogHeader() {
  let stored = '';
  const loggerPath = path.join(__dirname, '../src/utils/logger.ts');
  const logger = loadTypeScript(loggerPath, {
    'expo-application': application,
    'expo-constants': { __esModule: true, default: constants },
    'expo-updates': updates,
    'react-native': { Platform: { OS: 'ios', Version: '26.6.1' } },
    'expo-file-system/legacy': {
      documentDirectory: 'memory://',
      getInfoAsync: async () => ({ exists: stored.length > 0, isDirectory: false, size: stored.length }),
      makeDirectoryAsync: async () => undefined,
      readAsStringAsync: async () => stored,
      writeAsStringAsync: async (_path, value) => { stored = value; },
      deleteAsync: async () => { stored = ''; },
      moveAsync: async () => undefined,
    },
  });

  await logger.initLogger();
  const text = await logger.readLog();
  assert.match(text, /appVersion=1\.15\.0/);
  assert.match(text, /nativeBuildVersion=79/);
  assert.match(text, /applicationId=com\.chronicle\.app/);
  assert.match(text, /applicationName=Chronicle/);
  assert.match(text, /executionEnvironment=standalone/);
  assert.match(text, /updateId=update-123/);
  assert.match(text, /channel=testflight/);
  assert.match(text, /runtimeVersion=1\.15\.0/);
  assert.match(text, /sessionId=[a-z0-9]{8}/);
  assert.doesNotMatch(text, /Version=unknown/);
}

async function testUploadHeaders() {
  let request;
  const clientPath = path.join(__dirname, '../src/services/clientDiagnostics.ts');
  const client = loadTypeScript(clientPath, {
    'expo-application': application,
    'expo-constants': { __esModule: true, default: constants },
    'react-native': { Platform: { OS: 'ios', Version: '26.6.1' } },
    './auth': {
      deriveBaseUrl: () => 'https://chronicle.example',
      fetchAuthed: async (url, init) => {
        request = { url, init };
        return {
          ok: true,
          json: async () => ({ app_version: '1.15.0', build_version: '79' }),
        };
      },
    },
    '../utils/storage': {
      getLastConnectedDeviceId: async () => null,
      getWebSocketUrl: async () => 'wss://chronicle.example/ws/audio',
    },
  });

  await client.uploadClientDiagnostic('diagnostic body');
  assert.equal(request.init.headers['X-Chronicle-App-Version'], '1.15.0');
  assert.equal(request.init.headers['X-Chronicle-Build-Version'], '79');
  assert.notEqual(request.init.headers['X-Chronicle-Build-Version'], 'unknown');
}

Promise.all([testLogHeader(), testUploadHeaders()])
  .then(() => console.log('client diagnostic metadata tests passed'))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
