import type { VoiceCapabilities } from '../protocol/audioCapabilities';
import type { NativeCaptureDiagnostic } from '../../modules/chronicle-duplex-audio';
import { logError, logInfo, logWarn } from '@/utils/logger';

type DiagnosticLevel = 'info' | 'warn' | 'error';
type Clock = () => number;

interface NativeFrameDiagnostic {
  captureEpoch: number;
  opusBytes: number;
  audioLevel: number;
}

type CapabilityDiagnostic = Pick<
  VoiceCapabilities,
  'mode' | 'input_route' | 'output_route' | 'native_sample_rate'
>;

const shortId = (value: string): string => value ? `${value.slice(0, 8)}…` : 'none';
const safeLevel = (value: number): number => Math.min(1, Math.max(0, value || 0));
const redactSecrets = (value: string): string => value
  .replace(/([?&](?:token|access_token)=)[^&\s]+/gi, '$1<REDACTED>')
  .replace(/Bearer\s+[^\s]+/gi, 'Bearer <REDACTED>');

export class PhoneAudioDiagnostics {
  private attempt = 0;
  private active = false;
  private startedAtMs = 0;
  private milestones = new Set<string>();
  private nativeFrames = 0;
  private sentFrames = 0;
  private ackedPackets = 0;
  private lastAudioLevel = 0;

  constructor(private readonly now: Clock = Date.now) {}

  private write(level: DiagnosticLevel, event: string, details = ''): void {
    const message = `${event} attempt=${this.attempt}${details ? ` ${details}` : ''}`;
    if (level === 'error') logError('PhoneAudio', message);
    else if (level === 'warn') logWarn('PhoneAudio', message);
    else logInfo('PhoneAudio', message);
  }

  private once(level: DiagnosticLevel, event: string, details = ''): void {
    if (this.milestones.has(event)) return;
    this.milestones.add(event);
    this.write(level, event, details);
  }

  beginAttempt(): void {
    this.attempt += 1;
    this.active = true;
    this.startedAtMs = this.now();
    this.milestones.clear();
    this.nativeFrames = 0;
    this.sentFrames = 0;
    this.ackedPackets = 0;
    this.lastAudioLevel = 0;
    this.write('info', 'button_pressed');
  }

  listenerInstalled(captureEpoch: number): void {
    this.once('info', 'native_listener_installed', `capture_epoch=${captureEpoch}`);
  }

  engineStarted(captureEpoch: number, capabilities: CapabilityDiagnostic): void {
    this.once(
      'info',
      'native_engine_started',
      [
        `capture_epoch=${captureEpoch}`,
        `mode=${capabilities.mode}`,
        `input=${capabilities.input_route}`,
        `output=${capabilities.output_route}`,
        `sample_rate=${capabilities.native_sample_rate}`,
      ].join(' '),
    );
  }

  nativeFrame(frame: NativeFrameDiagnostic): void {
    if (!this.active) return;
    this.nativeFrames += 1;
    this.lastAudioLevel = safeLevel(frame.audioLevel);
    this.once(
      'info',
      'native_first_frame',
      `capture_epoch=${frame.captureEpoch} opus_bytes=${frame.opusBytes} audio_level=${this.lastAudioLevel.toFixed(3)}`,
    );
  }

  nativeStage(event: NativeCaptureDiagnostic): void {
    if (!this.active) return;
    const detail = redactSecrets(event.detail ?? '').slice(0, 240);
    this.once(
      event.stage.endsWith('_failed') ? 'error' : 'info',
      `native_${event.stage}`,
      [
        `capture_epoch=${event.captureEpoch}`,
        event.frameCount === undefined ? '' : `frames=${event.frameCount}`,
        event.byteCount === undefined ? '' : `bytes=${event.byteCount}`,
        detail ? `detail=${detail}` : '',
      ].filter(Boolean).join(' '),
    );
  }

  audioLevelActive(audioLevel: number): void {
    this.once('info', 'audio_level_active', `audio_level=${safeLevel(audioLevel).toFixed(3)}`);
  }

  invalidNativeFrame(reason: string): void {
    this.once('warn', 'native_frame_rejected', `reason=${reason}`);
  }

  socketConnecting(): void {
    this.once('info', 'websocket_connecting');
  }

  socketStage(stage: string, detail?: string): void {
    if (!this.active) return;
    const safeDetail = redactSecrets(detail ?? '').replace(/[\r\n]+/g, ' ').slice(0, 180);
    this.once(
      stage.endsWith('error') || stage.endsWith('failed') ? 'warn' : 'info',
      `websocket_${stage}`,
      safeDetail ? `detail=${safeDetail}` : '',
    );
  }

  socketOpen(): void {
    this.once('info', 'websocket_open');
  }

  socketClosed(expected: boolean): void {
    if (!this.active) return;
    this.write(expected ? 'info' : 'warn', 'websocket_closed', `expected=${expected}`);
  }

  captureStarted(captureSessionId: string): void {
    this.once('info', 'backend_capture_started', `capture_id=${shortId(captureSessionId)}`);
  }

  frameSent(opusBytes: number): void {
    if (!this.active) return;
    this.sentFrames += 1;
    this.once('info', 'first_frame_sent', `opus_bytes=${opusBytes}`);
  }

  packetAccepted(sequence: number): void {
    if (!this.active || !this.milestones.has('backend_capture_started')) return;
    this.ackedPackets += 1;
    this.once('info', 'first_packet_accepted', `sequence=${sequence}`);
  }

  timeout(reason: string): void {
    if (!this.active) return;
    this.write('warn', reason, this.snapshot());
  }

  failure(stage: string, cause: unknown): void {
    const message = redactSecrets(cause instanceof Error ? cause.message : String(cause));
    this.write('error', 'failed', `stage=${stage} error=${message.slice(0, 300)} ${this.snapshot()}`);
  }

  stopped(reason = 'user'): void {
    if (!this.active) return;
    this.write('info', 'stopped', `reason=${reason} ${this.snapshot()}`);
    this.active = false;
  }

  private snapshot(): string {
    return [
      `elapsed_ms=${Math.max(0, this.now() - this.startedAtMs)}`,
      `native_frames=${this.nativeFrames}`,
      `sent_frames=${this.sentFrames}`,
      `acked_packets=${this.ackedPackets}`,
      `last_audio_level=${this.lastAudioLevel.toFixed(3)}`,
    ].join(' ');
  }
}

export const phoneAudioDiagnostics = new PhoneAudioDiagnostics();
