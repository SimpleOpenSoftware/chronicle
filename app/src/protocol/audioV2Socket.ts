import { create } from '@bufbuild/protobuf';
import { DurationSchema } from '@bufbuild/protobuf/wkt';

import {
  AudioCodec,
  AudioSpecSchema,
  CaptureBindingSchema,
  CaptureMediaPacketSchema,
  CaptureSourceIdSchema,
  ClientHelloSchema,
  ClientControlSchema,
  DataPurpose,
  DeliveryClass,
  DeviceKind,
  EventIdSchema,
  HeartbeatSchema,
  MediaEnvelopeSchema,
  PlaybackAcknowledgementSchema,
  PlaybackState, ConversationAction, ConversationCommandSchema, SpeechEngine,
  ProcessingProfile,
  ResponseIdSchema,
  StartCaptureSchema,
  StopCaptureSchema,
  StopReason,
  VoiceReadySchema,
  type CaptureBinding,
  type CaptureCapabilities,
  type ClientControl,
  type PlaybackMediaPacket,
  type ServerControl,
  decodeMediaEnvelope,
  decodeServerControl,
  encodeClientControl,
  encodeMediaEnvelope,
  timestampFromUnixMs,
} from './audioV2';

export const AUDIO_V2_SUBPROTOCOL = 'chronicle.audio.v2';

export interface CapturePacket {
  sequence: number;
  capturedAtMs: number;
  monotonicOffsetUs: number;
  deviceMonotonicTimestampUs?: number;
  opus: Uint8Array;
}

export interface StartCaptureOptions {
  captureEpoch: number;
  processingProfile: ProcessingProfile;
  dataPurpose?: DataPurpose;
  deliveryClass: DeliveryClass;
  recoveryBatchId?: string;
  capabilities?: CaptureCapabilities;
}

export interface AudioV2SocketDiagnostic {
  stage:
    | 'socket_created'
    | 'transport_open'
    | 'client_hello_sent'
    | 'server_hello_received'
    | 'capture_start_sent'
    | 'capture_started_received'
    | 'capture_stop_sent'
    | 'capture_stopped_received'
    | 'server_error'
    | 'transport_error'
    | 'transport_closed'
    | 'control_decode_failed';
  detail?: string;
}

export interface AudioV2SocketOptions {
  url: string;
  bearerToken: string;
  sourceId: string;
  displayName: string;
  deviceKind: DeviceKind;
  uplinkFrameDurationMs: 20 | 60;
  onPacketAccepted?: (sequence: number) => void;
  onPlaybackPacket?: (packet: PlaybackMediaPacket) => void;
  onControl?: (control: ServerControl) => void;
  onClosed?: () => void;
  onDiagnostic?: (event: AudioV2SocketDiagnostic) => void;
  webSocketFactory?: (url: string, protocols: string | string[]) => WebSocket;
}

type AwaitedEvent = 'hello' | 'captureStarted' | 'captureStopped';

interface RuntimeCrypto {
  randomUUID?: () => string;
}

let fallbackEventIdSequence = 0;

function fallbackEventId(): string {
  fallbackEventIdSequence = (fallbackEventIdSequence + 1) % 0x1_0000;
  const random = Array.from(
    { length: 19 },
    () => Math.floor(Math.random() * 16).toString(16),
  ).join('');
  const sequence = fallbackEventIdSequence.toString(16).padStart(4, '0');
  const timestamp = Date.now().toString(16).padStart(12, '0').slice(-12);
  const variant = (8 + Math.floor(Math.random() * 4)).toString(16);
  return `${random.slice(0, 8)}-${sequence}-4${random.slice(8, 11)}-${variant}${random.slice(11, 14)}-${timestamp}`;
}

export function createClientEventIdValue(
  runtimeCrypto: RuntimeCrypto | null | undefined = (globalThis as { crypto?: RuntimeCrypto }).crypto,
): string {
  return runtimeCrypto?.randomUUID ? runtimeCrypto.randomUUID() : fallbackEventId();
}

function eventId() {
  return create(EventIdSchema, { value: createClientEventIdValue() });
}

function uplinkSpec(frameDurationMs: 20 | 60) {
  return create(AudioSpecSchema, {
    codec: AudioCodec.OPUS,
    sampleRateHz: 16_000,
    channelCount: 1,
    frameDuration: create(DurationSchema, { nanos: frameDurationMs * 1_000_000 }),
    bitrateBps: 24_000,
  });
}

function downlinkSpec() {
  return create(AudioSpecSchema, {
    codec: AudioCodec.OPUS,
    sampleRateHz: 24_000,
    channelCount: 1,
    frameDuration: create(DurationSchema, { nanos: 20_000_000 }),
    bitrateBps: 24_000,
  });
}

export class AudioV2Socket {
  private readonly options: AudioV2SocketOptions;
  private socket: WebSocket | null = null;
  private binding: CaptureBinding | null = null;
  private waiters = new Map<AwaitedEvent, {
    resolve: (control: ServerControl) => void;
    reject: (error: Error) => void;
    timeout: ReturnType<typeof setTimeout>;
  }>();

  constructor(options: AudioV2SocketOptions) {
    this.options = options;
  }

  get readyState(): number | undefined {
    return this.socket?.readyState;
  }

  get activeBinding(): CaptureBinding | null {
    return this.binding;
  }

  async connect(): Promise<void> {
    if (this.socket) throw new Error('audio-v2 socket already exists');
    const factory = this.options.webSocketFactory ?? ((url, protocols) => new WebSocket(url, protocols));
    const socket = factory(this.options.url, AUDIO_V2_SUBPROTOCOL);
    this.diagnostic('socket_created');
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    const hello = this.waitFor('hello');
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => {
        this.diagnostic('transport_open');
        try {
          this.sendControl({
            case: 'hello',
            value: create(ClientHelloSchema, {
              bearerToken: this.options.bearerToken,
              sourceId: create(CaptureSourceIdSchema, { value: this.options.sourceId }),
              deviceKind: this.options.deviceKind,
              displayName: this.options.displayName,
              supportedUplink: [uplinkSpec(this.options.uplinkFrameDurationMs)],
              supportedDownlink: [downlinkSpec()],
            }),
          });
          this.diagnostic('client_hello_sent');
          resolve();
        } catch (error) {
          reject(error instanceof Error ? error : new Error(String(error)));
        }
      };
      socket.onerror = () => {
        this.diagnostic('transport_error');
        reject(new Error('audio-v2 WebSocket failed'));
      };
      socket.onclose = event => {
        this.diagnostic(
          'transport_closed',
          `code=${event.code} clean=${event.wasClean} reason=${event.reason.slice(0, 160)}`,
        );
        this.rejectWaiters(new Error('audio-v2 WebSocket closed'));
        this.options.onClosed?.();
      };
      socket.onmessage = event => this.receive(event.data);
    });
    await hello;
  }

  async startCapture(options: StartCaptureOptions): Promise<CaptureBinding> {
    if (this.binding) throw new Error('capture already active');
    const started = this.waitFor('captureStarted');
    this.sendControl({
      case: 'startCapture',
      value: create(StartCaptureSchema, {
        captureEpoch: BigInt(options.captureEpoch),
        processingProfile: options.processingProfile,
        dataPurpose: options.dataPurpose ?? DataPurpose.NORMAL_CAPTURE,
        deliveryClass: options.deliveryClass,
        audioSpec: uplinkSpec(this.options.uplinkFrameDurationMs),
        capabilities: options.capabilities,
        recoveryBatchId: options.recoveryBatchId ?? '',
      }),
    });
    this.diagnostic('capture_start_sent');
    const control = await started;
    if (control.event.case !== 'captureStarted') {
      throw new Error('expected capture_started');
    }
    const binding = control.event.value.binding;
    if (!binding?.captureSessionId?.value) throw new Error('capture start has no binding');
    this.binding = binding;
    this.diagnostic('capture_started_received');
    return binding;
  }

  sendPacket(packet: CapturePacket): void {
    if (!this.binding) throw new Error('capture is not active');
    const envelope = create(MediaEnvelopeSchema, {
      media: {
        case: 'capture',
        value: create(CaptureMediaPacketSchema, {
          binding: create(CaptureBindingSchema, this.binding),
          sequence: BigInt(packet.sequence),
          capturedAt: timestampFromUnixMs(packet.capturedAtMs),
          monotonicOffsetUs: BigInt(packet.monotonicOffsetUs),
          deviceMonotonicTimestampUs: packet.deviceMonotonicTimestampUs === undefined
            ? undefined : BigInt(Math.round(packet.deviceMonotonicTimestampUs)),
          deliveryClass: this.currentDeliveryClass,
          opusPayload: packet.opus,
        }),
      },
    });
    this.requireOpen().send(encodeMediaEnvelope(envelope));
  }

  private currentDeliveryClass = DeliveryClass.UNSPECIFIED;

  async beginCapture(options: StartCaptureOptions): Promise<CaptureBinding> {
    this.currentDeliveryClass = options.deliveryClass;
    try {
      return await this.startCapture(options);
    } catch (error) {
      this.currentDeliveryClass = DeliveryClass.UNSPECIFIED;
      throw error;
    }
  }

  async stopCapture(reason = StopReason.USER_REQUESTED): Promise<void> {
    if (!this.binding) return;
    const binding = this.binding;
    const stopped = this.waitFor('captureStopped');
    this.sendControl({
      case: 'stopCapture',
      value: create(StopCaptureSchema, { binding, reason }),
    });
    this.diagnostic('capture_stop_sent');
    await stopped;
    this.diagnostic('capture_stopped_received');
    this.binding = null;
    this.currentDeliveryClass = DeliveryClass.UNSPECIFIED;
  }

  voiceReady(capabilities: CaptureCapabilities): void {
    if (!this.binding?.voiceSessionId?.value) {
      throw new Error('voice-ready requires an interactive capture binding');
    }
    this.sendControl({
      case: 'voiceReady',
      value: create(VoiceReadySchema, {
        binding: this.binding,
        capabilities,
      }),
    });
  }

  acknowledgePlayback(
    responseId: string,
    generation: number,
    state: PlaybackState,
    monotonicTimestampMs: number,
    renderedSamples = 0, bufferedSamples = 0,
  ): void {
    if (!this.binding) throw new Error('playback acknowledgement requires capture');
    this.sendControl({
      case: 'playbackAcknowledgement',
      value: create(PlaybackAcknowledgementSchema, {
        binding: this.binding,
        responseId: create(ResponseIdSchema, { value: responseId }),
        generation: BigInt(generation),
        state, renderedSamples: BigInt(renderedSamples), bufferedSamples: BigInt(bufferedSamples),
        monotonicTimestampUs: BigInt(Math.round(monotonicTimestampMs * 1000)),
      }),
    });
  }

  conversation(action: ConversationAction, threadId = "", interactionId = "", taskId = "", taskRevision = 0): void {
    if (!this.binding) throw new Error("Start phone capture before voice dialogue");
    this.sendControl({ case: "conversationCommand", value: create(ConversationCommandSchema, {
      binding: this.binding, action, threadId, interactionId, taskId, taskRevision: BigInt(taskRevision), engine: SpeechEngine.MODULAR,
    }) });
  }

  heartbeat(monotonicTimestampMs: number): void {
    this.sendControl({
      case: 'heartbeat',
      value: create(HeartbeatSchema, {
        monotonicTimestampUs: BigInt(Math.round(monotonicTimestampMs * 1000)),
      }),
    });
  }

  close(): void {
    this.rejectWaiters(new Error('audio-v2 socket closed'));
    this.socket?.close(1000, 'client-stop');
    this.socket = null;
    this.binding = null;
  }

  private sendControl(event: ClientControl['event']): void {
    const control = create(ClientControlSchema, {
      eventId: eventId(),
      sentAt: timestampFromUnixMs(Date.now()),
      event,
    });
    this.requireOpen().send(encodeClientControl(control));
  }

  private receive(payload: string | ArrayBuffer | Blob): void {
    if (typeof payload === 'string') {
      let control: ServerControl;
      try {
        control = decodeServerControl(payload);
      } catch (error) {
        this.diagnostic(
          'control_decode_failed',
          error instanceof Error ? error.message.slice(0, 160) : String(error).slice(0, 160),
        );
        this.rejectWaiters(new Error('audio-v2 server control could not be decoded'));
        return;
      }
      const event = control.event.case;
      if (event === 'error') {
        this.diagnostic('server_error', control.event.value.detail.slice(0, 160));
        this.rejectWaiters(new Error(control.event.value.detail));
        return;
      }
      if (event === 'hello') this.diagnostic('server_hello_received');
      if (event === 'captureStarted') this.binding = control.event.value.binding ?? null;
      if (event === 'capturePacketAccepted') {
        this.options.onPacketAccepted?.(Number(control.event.value.sequence));
      }
      const waiter = event ? this.waiters.get(event as AwaitedEvent) : undefined;
      if (waiter) {
        this.waiters.delete(event as AwaitedEvent);
        clearTimeout(waiter.timeout);
        waiter.resolve(control);
      }
      this.options.onControl?.(control);
      return;
    }
    if (payload instanceof Blob) {
      payload.arrayBuffer().then(value => this.receive(value));
      return;
    }
    const media = decodeMediaEnvelope(new Uint8Array(payload));
    if (media.media.case === 'playback') {
      this.options.onPlaybackPacket?.(media.media.value);
    }
  }

  private waitFor(event: AwaitedEvent): Promise<ServerControl> {
    if (this.waiters.has(event)) throw new Error(`already waiting for ${event}`);
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(event);
        reject(new Error(`audio-v2 timed out waiting for ${event}`));
      }, 10_000);
      this.waiters.set(event, { resolve, reject, timeout });
    });
  }

  private rejectWaiters(error: Error): void {
    this.waiters.forEach(waiter => {
      clearTimeout(waiter.timeout);
      waiter.reject(error);
    });
    this.waiters.clear();
  }

  private diagnostic(stage: AudioV2SocketDiagnostic['stage'], detail?: string): void {
    this.options.onDiagnostic?.({ stage, detail });
  }

  private requireOpen(): WebSocket {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error('audio-v2 WebSocket is not open');
    }
    return this.socket;
  }
}

export const ambientProfile = ProcessingProfile.SOURCE_NATIVE;
