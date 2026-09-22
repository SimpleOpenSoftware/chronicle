import * as FileSystem from 'expo-file-system/legacy';
import * as Application from 'expo-application';
import * as Updates from 'expo-updates';
import { Platform } from 'react-native';
import Constants from 'expo-constants';

const LOG_DIR = `${FileSystem.documentDirectory}chronicle-logs/`;
const LOG_FILE = `${LOG_DIR}chronicle-log.txt`;
const LOG_FILE_OLD = `${LOG_DIR}chronicle-log.old.txt`;
const MAX_LOG_BYTES = 1_000_000;

type Level = 'INFO' | 'WARN' | 'ERROR' | 'FATAL';

let initialized = false;
let writeQueue: Promise<void> = Promise.resolve();
let sessionId = '';

function ts(): string {
  return new Date().toISOString();
}

function formatLine(level: Level, tag: string, msg: string): string {
  return `${ts()} [${level}] [${tag}] ${msg}\n`;
}

async function rotateIfNeeded(): Promise<void> {
  try {
    const info = await FileSystem.getInfoAsync(LOG_FILE);
    if (info.exists && !info.isDirectory && (info.size ?? 0) > MAX_LOG_BYTES) {
      const oldInfo = await FileSystem.getInfoAsync(LOG_FILE_OLD);
      if (oldInfo.exists) {
        await FileSystem.deleteAsync(LOG_FILE_OLD, { idempotent: true });
      }
      await FileSystem.moveAsync({ from: LOG_FILE, to: LOG_FILE_OLD });
    }
  } catch {
    // Best-effort rotation; never block logging
  }
}

function enqueueWrite(line: string): void {
  writeQueue = writeQueue
    .then(async () => {
      await rotateIfNeeded();
      const info = await FileSystem.getInfoAsync(LOG_FILE);
      if (info.exists) {
        const existing = await FileSystem.readAsStringAsync(LOG_FILE);
        await FileSystem.writeAsStringAsync(LOG_FILE, existing + line);
      } else {
        await FileSystem.writeAsStringAsync(LOG_FILE, line);
      }
    })
    .catch((err) => {
      // Last-resort fallback so we don't kill the promise chain
      console.warn('[logger] write failed', err);
    });
}

export function log(level: Level, tag: string, msg: string): void {
  const line = formatLine(level, tag, msg);
  // Always mirror to console so Metro/devtools see it too
  if (level === 'ERROR' || level === 'FATAL') console.error(line.trim());
  else if (level === 'WARN') console.warn(line.trim());
  else console.log(line.trim());
  if (!initialized) return;
  enqueueWrite(line);
}

export const logInfo = (tag: string, msg: string) => log('INFO', tag, msg);
export const logWarn = (tag: string, msg: string) => log('WARN', tag, msg);
export const logError = (tag: string, msg: string) => log('ERROR', tag, msg);

export function getLogPath(): string {
  return LOG_FILE;
}

export function getOldLogPath(): string {
  return LOG_FILE_OLD;
}

async function ensureDir(): Promise<void> {
  const info = await FileSystem.getInfoAsync(LOG_DIR);
  if (!info.exists) {
    await FileSystem.makeDirectoryAsync(LOG_DIR, { intermediates: true });
  }
}

function describeUpdatesState(): string {
  try {
    const parts: string[] = [
      `isEmbeddedLaunch=${Updates.isEmbeddedLaunch}`,
      `updateId=${Updates.updateId ?? 'null'}`,
      `channel=${Updates.channel ?? 'null'}`,
      `runtimeVersion=${Updates.runtimeVersion ?? 'null'}`,
      `createdAt=${Updates.createdAt ? Updates.createdAt.toISOString() : 'null'}`,
    ];
    return parts.join(' ');
  } catch (err) {
    return `describeUpdatesState error: ${String(err)}`;
  }
}

function installGlobalErrorHandler(): void {
  const g: any = global as any;
  const prev = g.ErrorUtils?.getGlobalHandler?.();
  g.ErrorUtils?.setGlobalHandler?.((error: Error, isFatal?: boolean) => {
    try {
      const msg = `${isFatal ? 'FATAL' : 'NON-FATAL'} uncaught JS error: ${error?.name}: ${error?.message}\nstack: ${error?.stack ?? 'no stack'}`;
      log(isFatal ? 'FATAL' : 'ERROR', 'GlobalError', msg);
    } catch {
      // swallow — nothing we can do here
    }
    if (prev) prev(error, isFatal);
  });

  const rejectionTracking = (g as any).HermesInternal;
  // Unhandled promise rejection — RN exposes via process in newer versions
  if (typeof g.addEventListener === 'function') {
    g.addEventListener('unhandledrejection', (ev: any) => {
      try {
        const reason = ev?.reason;
        const msg = `Unhandled promise rejection: ${reason?.message ?? String(reason)}\nstack: ${reason?.stack ?? 'no stack'}`;
        log('ERROR', 'UnhandledRejection', msg);
      } catch {
        // ignore
      }
    });
  }
}

export async function initLogger(): Promise<void> {
  if (initialized) return;
  try {
    await ensureDir();
    initialized = true;
    sessionId = Math.random().toString(36).slice(2, 10);
    const header = [
      '',
      '==================== NEW SESSION ====================',
      `sessionId=${sessionId}`,
      `time=${ts()}`,
      `platform=${Platform.OS} ${Platform.Version}`,
      `appVersion=${Application.nativeApplicationVersion ?? 'unknown'}`,
      `nativeAppVersion=${Application.nativeApplicationVersion ?? 'unknown'}`,
      `nativeBuildVersion=${Application.nativeBuildVersion ?? 'unknown'}`,
      `applicationId=${Application.applicationId ?? 'unknown'}`,
      `applicationName=${Application.applicationName ?? 'unknown'}`,
      `configuredAppVersion=${Constants.expoConfig?.version ?? 'unknown'}`,
      `executionEnvironment=${Constants.executionEnvironment ?? 'unknown'}`,
      `updates: ${describeUpdatesState()}`,
      '=====================================================',
      '',
    ].join('\n');
    enqueueWrite(header);
    installGlobalErrorHandler();
  } catch (err) {
    console.warn('[logger] init failed', err);
  }
}

export async function readLog(): Promise<string> {
  try {
    await writeQueue;
    const info = await FileSystem.getInfoAsync(LOG_FILE);
    if (!info.exists) return '';
    return await FileSystem.readAsStringAsync(LOG_FILE);
  } catch (err) {
    return `failed to read log: ${String(err)}`;
  }
}

export async function readLogBundle(): Promise<string> {
  try {
    await writeQueue;
    const sections: string[] = [];
    for (const [label, path] of [
      ['previous', LOG_FILE_OLD],
      ['current', LOG_FILE],
    ] as const) {
      const info = await FileSystem.getInfoAsync(path);
      if (info.exists && !info.isDirectory) {
        const contents = await FileSystem.readAsStringAsync(path);
        sections.push(`==================== ${label.toUpperCase()} LOG ====================\n${contents}`);
      }
    }
    return sections.join('\n');
  } catch (err) {
    return `failed to read log bundle: ${String(err)}`;
  }
}

export async function clearLog(): Promise<void> {
  try {
    await FileSystem.deleteAsync(LOG_FILE, { idempotent: true });
    await FileSystem.deleteAsync(LOG_FILE_OLD, { idempotent: true });
  } catch (err) {
    console.warn('[logger] clear failed', err);
  }
}
