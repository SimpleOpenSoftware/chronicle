import { Platform } from 'react-native';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';

import {
  addCaptureDiagnosticListener,
  addOpusFrameListener,
  addRouteChangeListener,
  getVoiceSessionDiagnostics,
  startVoiceSession,
  stopVoiceSession,
  type NativeCaptureDiagnostic,
  type NativeOpusFrame,
  type NativeRouteChange,
  type NativeStopResult,
  type NativeVoiceSessionDiagnostics,
  type StartVoiceSessionOptions,
} from '../../modules/chronicle-duplex-audio';
import {
  DataPurpose,
  DeliveryClass,
  DeviceKind,
  ProcessingProfile,
} from '../protocol/audioV2';
import {
  AudioV2Socket,
  type AudioV2SocketDiagnostic,
  type AudioV2SocketOptions,
  type CapturePacket,
  type StartCaptureOptions,
} from '../protocol/audioV2Socket';
import { logError, logInfo, logWarn } from '@/utils/logger';

type DiagnosticProfile = NonNullable<StartVoiceSessionOptions['diagnosticProfile']>;
type DiagnosticStatus = 'pass' | 'fail' | 'skipped';

const PROFILES: DiagnosticProfile[] = [
  'production',
  'voice_processing_hold',
  'plain_capture_hold',
  'system_tap_format_hold',
];
const PROBE_DURATION_MS = 2_250;
const PROBE_SETTLE_MS = 250;
const NETWORK_PACKET_COUNT = 25;
const NETWORK_TIMEOUT_MS = 10_000;
const SYNTHETIC_OPUS_SILENCE = new Uint8Array([0xf8, 0xff, 0xfe]);

interface Subscription {
  remove(): void;
}

interface DiagnosticSocket {
  connect(): Promise<void>;
  beginCapture(options: StartCaptureOptions): Promise<{
    captureSessionId?: { value?: string };
  }>;
  sendPacket(packet: CapturePacket): void;
  stopCapture(): Promise<void>;
  close(): void;
}

export interface PhoneAudioDiagnosticDependencies {
  now(): number;
  sleep(milliseconds: number): Promise<void>;
  addOpusFrameListener(listener: (event: NativeOpusFrame) => void): Subscription;
  addCaptureDiagnosticListener(listener: (event: NativeCaptureDiagnostic) => void): Subscription;
  addRouteChangeListener(listener: (event: NativeRouteChange) => void): Subscription;
  startVoiceSession(options: StartVoiceSessionOptions): Promise<VoiceCapabilitiesResult>;
  getVoiceSessionDiagnostics(): Promise<NativeVoiceSessionDiagnostics>;
  stopVoiceSession(): Promise<NativeStopResult>;
  createSocket(options: AudioV2SocketOptions): DiagnosticSocket;
}

type VoiceCapabilitiesResult = Awaited<ReturnType<typeof startVoiceSession>>;

export interface PhoneAudioDiagnosticProgress {
  phase: 'native' | 'network' | 'complete';
  label: string;
  current: number;
  total: number;
}

export interface NativeProbeResult {
  profile: DiagnosticProfile;
  status: Exclude<DiagnosticStatus, 'skipped'>;
  elapsedMs: number;
  frameCount: number;
  failure: string | null;
  snapshot: NativeVoiceSessionDiagnostics | null;
}

export interface NetworkProbeResult {
  status: DiagnosticStatus;
  elapsedMs: number;
  phase: string;
  payloadSource: 'native_mic' | 'synthetic_silence' | 'none';
  packetsSent: number;
  packetsAcked: number;
  captureSessionId: string | null;
  failure: string | null;
}

export interface PhoneAudioDiagnosticRunResult {
  runId: string;
  status: 'pass' | 'partial' | 'fail';
  elapsedMs: number;
  nativeProbes: NativeProbeResult[];
  networkProbe: NetworkProbeResult;
}

export interface PhoneAudioDiagnosticRunOptions {
  backendUrl: string;
  jwtToken: string | null;
  onProgress?: (progress: PhoneAudioDiagnosticProgress) => void;
}

interface CapturedPacket {
  capturedAtMs: number;
  monotonicTimestampMs: number;
  opus: Uint8Array;
}

const defaultDependencies: PhoneAudioDiagnosticDependencies = {
  now: Date.now,
  sleep: milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
  addOpusFrameListener,
  addCaptureDiagnosticListener,
  addRouteChangeListener,
  startVoiceSession,
  getVoiceSessionDiagnostics,
  stopVoiceSession,
  createSocket: options => new AudioV2Socket(options),
};

function safeText(value: unknown, maximum = 300): string {
  return String(value)
    .replace(/([?&](?:token|access_token)=)[^&\s]+/gi, '$1<REDACTED>')
    .replace(/Bearer\s+[^\s]+/gi, 'Bearer <REDACTED>')
    .replace(/[\r\n]+/g, ' ')
    .slice(0, maximum);
}

function decodeBase64(value: string): Uint8Array {
  const binary = base64.decode(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function write(
  runId: string,
  level: 'info' | 'warn' | 'error',
  event: string,
  details = '',
): void {
  const message = `run_id=${runId} event=${event}${details ? ` ${details}` : ''}`;
  if (level === 'error') logError('PhoneAudioSelfTest', message);
  else if (level === 'warn') logWarn('PhoneAudioSelfTest', message);
  else logInfo('PhoneAudioSelfTest', message);
}

async function withTimeout<T>(
  operation: Promise<T>,
  milliseconds: number,
  label: string,
): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | null = null;
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => reject(new Error(`${label} timed out after ${milliseconds}ms`)), milliseconds);
  });
  try {
    return await Promise.race([operation, deadline]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function backendSocketUrl(value: string): string {
  const url = new URL(value.trim());
  if (url.protocol === 'http:') url.protocol = 'ws:';
  if (url.protocol === 'https:') url.protocol = 'wss:';
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    throw new Error('Chronicle backend URL must use HTTP(S) or WS(S)');
  }
  url.pathname = '/ws/audio';
  url.search = '';
  url.hash = '';
  return url.toString();
}

function packetsForNetwork(nativePackets: CapturedPacket[], nowMs: number): {
  source: NetworkProbeResult['payloadSource'];
  packets: CapturePacket[];
} {
  const source = nativePackets.length ? 'native_mic' : 'synthetic_silence';
  const firstMonotonic = nativePackets[0]?.monotonicTimestampMs ?? 0;
  const packets = Array.from({ length: NETWORK_PACKET_COUNT }, (_, sequence) => {
    const native = nativePackets[sequence % Math.max(1, nativePackets.length)];
    return {
      sequence,
      capturedAtMs: nowMs + (sequence * 20),
      monotonicOffsetUs: native
        ? Math.max(0, Math.round((native.monotonicTimestampMs - firstMonotonic) * 1_000))
        : sequence * 20_000,
      opus: native?.opus ?? SYNTHETIC_OPUS_SILENCE,
    };
  });
  return { source, packets };
}

async function runNetworkProbe(
  runId: string,
  options: PhoneAudioDiagnosticRunOptions,
  nativePackets: CapturedPacket[],
  dependencies: PhoneAudioDiagnosticDependencies,
): Promise<NetworkProbeResult> {
  const startedAt = dependencies.now();
  if (!options.backendUrl.trim() || !options.jwtToken) {
    write(runId, 'warn', 'network_skipped', 'reason=backend_or_auth_not_configured');
    return {
      status: 'skipped',
      elapsedMs: 0,
      phase: 'configuration',
      payloadSource: 'none',
      packetsSent: 0,
      packetsAcked: 0,
      captureSessionId: null,
      failure: 'Backend URL or authentication is not configured',
    };
  }

  let phase = 'construct';
  let packetsSent = 0;
  let captureSessionId: string | null = null;
  const acked = new Set<number>();
  let finishAcknowledgements: () => void = () => {};
  const acknowledgements = new Promise<void>(resolve => {
    finishAcknowledgements = resolve;
  });
  let socket: DiagnosticSocket | null = null;
  try {
    const url = backendSocketUrl(options.backendUrl);
    const payload = packetsForNetwork(nativePackets, dependencies.now());
    write(
      runId,
      'info',
      'network_started',
      `endpoint=${new URL(url).host} payload_source=${payload.source} packet_target=${payload.packets.length}`,
    );
    socket = dependencies.createSocket({
      url,
      bearerToken: options.jwtToken,
      sourceId: 'phone-audio-diagnostics',
      displayName: 'phone-audio-diagnostics',
      deviceKind: Platform.OS === 'ios' ? DeviceKind.IOS_PHONE : DeviceKind.ANDROID_PHONE,
      uplinkFrameDurationMs: 20,
      onPacketAccepted: sequence => {
        acked.add(sequence);
        if (acked.size >= payload.packets.length) finishAcknowledgements();
      },
      onDiagnostic: (event: AudioV2SocketDiagnostic) => {
        phase = event.stage;
        write(
          runId,
          event.stage.endsWith('error') || event.stage.endsWith('failed') ? 'warn' : 'info',
          'network_phase',
          `phase=${event.stage}${event.detail ? ` detail=${safeText(event.detail, 180)}` : ''}`,
        );
      },
    });

    phase = 'connect';
    await withTimeout(socket.connect(), NETWORK_TIMEOUT_MS, 'WebSocket hello');
    phase = 'begin_capture';
    const binding = await withTimeout(socket.beginCapture({
      captureEpoch: 0,
      processingProfile: ProcessingProfile.SOURCE_NATIVE,
      dataPurpose: DataPurpose.ANNOTATION,
      deliveryClass: DeliveryClass.RECOVERED,
      recoveryBatchId: `phone-diag-${runId}`,
    }), NETWORK_TIMEOUT_MS, 'backend capture start');
    captureSessionId = binding.captureSessionId?.value ?? null;
    if (!captureSessionId) throw new Error('backend returned no capture session ID');
    write(runId, 'info', 'network_capture_bound', `capture_session_id=${captureSessionId}`);

    phase = 'send_packets';
    for (const packet of payload.packets) {
      socket.sendPacket(packet);
      packetsSent += 1;
    }
    await withTimeout(acknowledgements, NETWORK_TIMEOUT_MS, 'packet acknowledgements');
    phase = 'stop_capture';
    await withTimeout(socket.stopCapture(), NETWORK_TIMEOUT_MS, 'backend capture stop');

    const result: NetworkProbeResult = {
      status: acked.size === packetsSent ? 'pass' : 'fail',
      elapsedMs: Math.max(0, dependencies.now() - startedAt),
      phase: 'complete',
      payloadSource: payload.source,
      packetsSent,
      packetsAcked: acked.size,
      captureSessionId,
      failure: null,
    };
    write(
      runId,
      result.status === 'pass' ? 'info' : 'error',
      'network_result',
      `status=${result.status} capture_session_id=${captureSessionId} payload_source=${payload.source} packets_sent=${packetsSent} packets_acked=${acked.size} elapsed_ms=${result.elapsedMs}`,
    );
    return result;
  } catch (cause) {
    const failure = safeText(cause instanceof Error ? cause.message : cause);
    const result: NetworkProbeResult = {
      status: 'fail',
      elapsedMs: Math.max(0, dependencies.now() - startedAt),
      phase,
      payloadSource: nativePackets.length ? 'native_mic' : 'synthetic_silence',
      packetsSent,
      packetsAcked: acked.size,
      captureSessionId,
      failure,
    };
    write(
      runId,
      'error',
      'network_result',
      `status=fail phase=${phase} capture_session_id=${captureSessionId ?? 'none'} packets_sent=${packetsSent} packets_acked=${acked.size} error=${failure}`,
    );
    return result;
  } finally {
    socket?.close();
  }
}

export async function runPhoneAudioDiagnosticSuite(
  options: PhoneAudioDiagnosticRunOptions,
  dependencies: PhoneAudioDiagnosticDependencies = defaultDependencies,
): Promise<PhoneAudioDiagnosticRunResult> {
  const suiteStartedAt = dependencies.now();
  const runId = `${Math.floor(suiteStartedAt).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const baseEpoch = Math.max(1, Math.floor(suiteStartedAt % 2_000_000_000));
  const nativeProbes: NativeProbeResult[] = [];
  let activeEpoch = -1;
  let activePackets: CapturedPacket[] = [];
  let bestPackets: CapturedPacket[] = [];

  write(
    runId,
    'info',
    'suite_started',
    `platform=${Platform.OS} native_profiles=${PROFILES.join(',')} probe_duration_ms=${PROBE_DURATION_MS} network_packets=${NETWORK_PACKET_COUNT}`,
  );

  const frameSubscription = dependencies.addOpusFrameListener(frame => {
    if (frame.captureEpoch !== activeEpoch || activePackets.length >= NETWORK_PACKET_COUNT) return;
    try {
      const opus = decodeBase64(frame.opusBase64);
      if (!opus.length) return;
      activePackets.push({
        capturedAtMs: frame.capturedAtMs,
        monotonicTimestampMs: frame.monotonicTimestampMs,
        opus,
      });
    } catch (cause) {
      write(runId, 'warn', 'native_frame_decode_failed', `error=${safeText(cause)}`);
    }
  });
  const nativeSubscription = dependencies.addCaptureDiagnosticListener(event => {
    if (event.captureEpoch !== activeEpoch) return;
    write(
      runId,
      event.stage.endsWith('_failed') ? 'warn' : 'info',
      'native_stage',
      [
        `profile=${PROFILES[nativeProbes.length] ?? 'unknown'}`,
        `stage=${event.stage}`,
        event.frameCount === undefined ? '' : `frames=${event.frameCount}`,
        event.byteCount === undefined ? '' : `bytes=${event.byteCount}`,
        event.detail ? `detail=${safeText(event.detail, 220)}` : '',
      ].filter(Boolean).join(' '),
    );
  });
  const routeSubscription = dependencies.addRouteChangeListener(event => {
    if (event.captureEpoch !== activeEpoch) return;
    write(
      runId,
      'info',
      'native_route_event',
      `profile=${PROFILES[nativeProbes.length] ?? 'unknown'} reason=${event.reason} input=${event.capabilities.input_route} output=${event.capabilities.output_route} mode=${event.capabilities.mode}`,
    );
  });

  try {
    for (const [index, profile] of PROFILES.entries()) {
      options.onProgress?.({
        phase: 'native',
        label: `Testing ${profile.replace(/_/g, ' ')}`,
        current: index + 1,
        total: PROFILES.length,
      });
      activeEpoch = baseEpoch + index;
      activePackets = [];
      const probeStartedAt = dependencies.now();
      let snapshot: NativeVoiceSessionDiagnostics | null = null;
      let failure: string | null = null;
      let capabilities: VoiceCapabilitiesResult | null = null;
      write(runId, 'info', 'native_probe_started', `profile=${profile} capture_epoch=${activeEpoch}`);
      try {
        capabilities = await dependencies.startVoiceSession({
          captureEpoch: activeEpoch,
          diagnosticProfile: profile,
        });
        write(
          runId,
          'info',
          'native_probe_capabilities',
          `profile=${profile} mode=${capabilities.mode} input=${capabilities.input_route} output=${capabilities.output_route} sample_rate=${capabilities.native_sample_rate} aec=${capabilities.aec.enabled} noise_suppression=${capabilities.noise_suppression.enabled} fallback=${capabilities.fallback_reason ?? 'none'}`,
        );
        await dependencies.sleep(PROBE_DURATION_MS);
        snapshot = await dependencies.getVoiceSessionDiagnostics();
      } catch (cause) {
        failure = safeText(cause instanceof Error ? cause.message : cause);
        try {
          snapshot = await dependencies.getVoiceSessionDiagnostics();
        } catch {
          // The start failure remains the useful signal.
        }
      } finally {
        try {
          const restoration = await dependencies.stopVoiceSession();
          write(
            runId,
            restoration.restorationSucceeded ? 'info' : 'warn',
            'native_probe_cleanup',
            `profile=${profile} restoration_succeeded=${restoration.restorationSucceeded} failure_code=${restoration.failureCode ?? 'none'}`,
          );
        } catch (cause) {
          write(runId, 'warn', 'native_probe_cleanup', `profile=${profile} error=${safeText(cause)}`);
        }
      }

      const frameCount = Math.max(activePackets.length, snapshot?.opusPacketCount ?? 0);
      if (!failure && frameCount === 0) failure = 'no_opus_frames';
      const result: NativeProbeResult = {
        profile,
        status: failure ? 'fail' : 'pass',
        elapsedMs: Math.max(0, dependencies.now() - probeStartedAt),
        frameCount,
        failure,
        snapshot,
      };
      nativeProbes.push(result);
      if (result.status === 'pass' && activePackets.length > bestPackets.length) {
        bestPackets = [...activePackets];
      }
      write(
        runId,
        result.status === 'pass' ? 'info' : 'warn',
        'native_probe_result',
        `profile=${profile} status=${result.status} elapsed_ms=${result.elapsedMs} js_frames=${activePackets.length} failure=${failure ?? 'none'} snapshot=${JSON.stringify(snapshot)}`,
      );
      await dependencies.sleep(PROBE_SETTLE_MS);
    }
  } finally {
    activeEpoch = -1;
    frameSubscription.remove();
    nativeSubscription.remove();
    routeSubscription.remove();
  }

  options.onProgress?.({ phase: 'network', label: 'Testing Chronicle backend', current: 1, total: 1 });
  const networkProbe = await runNetworkProbe(runId, options, bestPackets, dependencies);
  const nativePassCount = nativeProbes.filter(probe => probe.status === 'pass').length;
  const status: PhoneAudioDiagnosticRunResult['status'] = nativePassCount > 0 && networkProbe.status === 'pass'
    ? 'pass'
    : nativePassCount === 0 && networkProbe.status === 'fail'
      ? 'fail'
      : 'partial';
  const result: PhoneAudioDiagnosticRunResult = {
    runId,
    status,
    elapsedMs: Math.max(0, dependencies.now() - suiteStartedAt),
    nativeProbes,
    networkProbe,
  };
  write(
    runId,
    status === 'pass' ? 'info' : status === 'partial' ? 'warn' : 'error',
    'suite_complete',
    `status=${status} native_passed=${nativePassCount}/${nativeProbes.length} network_status=${networkProbe.status} elapsed_ms=${result.elapsedMs}`,
  );
  options.onProgress?.({ phase: 'complete', label: 'Diagnostic run complete', current: 1, total: 1 });
  return result;
}
