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
  PlaybackState,
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

interface AudioV2SocketOptions {
  url: string;
  bearerToken: string;
  sourceId: string;
  displayName: string;
  deviceKind: DeviceKind;
  onPacketAccepted?: (sequence: number) => void;
  onPlaybackPacket?: (packet: PlaybackMediaPacket) => void;
  onControl?: (control: ServerControl) => void;
  onClosed?: () => void;
  webSocketFactory?: (url: string, protocols: string | string[]) => WebSocket;
}

type AwaitedEvent = 'hello' | 'captureStarted' | 'captureStopped';

function eventId() {
  return create(EventIdSchema, { value: crypto.randomUUID() });
}

function uplinkSpec() {
  return create(AudioSpecSchema, {
    codec: AudioCodec.OPUS,
    sampleRateHz: 16_000,
    channelCount: 1,
    frameDuration: create(DurationSchema, { nanos: 20_000_000 }),
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
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    const hello = this.waitFor('hello');
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => {
        this.sendControl({
          case: 'hello',
          value: create(ClientHelloSchema, {
            bearerToken: this.options.bearerToken,
            sourceId: create(CaptureSourceIdSchema, { value: this.options.sourceId }),
            deviceKind: this.options.deviceKind,
            displayName: this.options.displayName,
            supportedUplink: [uplinkSpec()],
            supportedDownlink: [downlinkSpec()],
          }),
        });
        resolve();
      };
      socket.onerror = () => reject(new Error('audio-v2 WebSocket failed'));
      socket.onclose = () => {
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
        audioSpec: uplinkSpec(),
        capabilities: options.capabilities,
        recoveryBatchId: options.recoveryBatchId ?? '',
      }),
    });
    const control = await started;
    if (control.event.case !== 'captureStarted') {
      throw new Error('expected capture_started');
    }
    const binding = control.event.value.binding;
    if (!binding?.captureSessionId?.value) throw new Error('capture start has no binding');
    this.binding = binding;
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
    await stopped;
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
  ): void {
    if (!this.binding) throw new Error('playback acknowledgement requires capture');
    this.sendControl({
      case: 'playbackAcknowledgement',
      value: create(PlaybackAcknowledgementSchema, {
        binding: this.binding,
        responseId: create(ResponseIdSchema, { value: responseId }),
        generation: BigInt(generation),
        state,
        monotonicTimestampUs: BigInt(Math.round(monotonicTimestampMs * 1000)),
      }),
    });
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
      const control = decodeServerControl(payload);
      const event = control.event.case;
      if (event === 'error') {
        this.rejectWaiters(new Error(control.event.value.detail));
        return;
      }
      if (event === 'captureStarted') this.binding = control.event.value.binding ?? null;
      if (event === 'capturePacketAccepted') {
        this.options.onPacketAccepted?.(Number(control.event.value.sequence));
      }
      const waiter = event ? this.waiters.get(event as AwaitedEvent) : undefined;
      if (waiter) {
        this.waiters.delete(event as AwaitedEvent);
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
    return new Promise((resolve, reject) => this.waiters.set(event, { resolve, reject }));
  }

  private rejectWaiters(error: Error): void {
    this.waiters.forEach(waiter => waiter.reject(error));
    this.waiters.clear();
  }

  private requireOpen(): WebSocket {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error('audio-v2 WebSocket is not open');
    }
    return this.socket;
  }
}

export const ambientProfile = ProcessingProfile.SOURCE_NATIVE;
