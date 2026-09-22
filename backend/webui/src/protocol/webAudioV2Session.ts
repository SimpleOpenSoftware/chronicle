import { create } from '@bufbuild/protobuf'
import { DurationSchema } from '@bufbuild/protobuf/wkt'

import {
  AudioCodec,
  AudioSpecSchema,
  CaptureBindingSchema,
  ConversationAction,
  ConversationCommandSchema,
  ConversationPhase,
  SpeechEngine,
  VoiceReadySchema,
  decodeMediaEnvelope,
  type CaptureCapabilities,
  type ConversationState,
  type VoiceProcessingUpdate,
  CaptureMediaPacketSchema,
  CaptureSourceIdSchema,
  ClientControlSchema,
  ClientHelloSchema,
  DataPurpose,
  DeliveryClass,
  DeviceKind,
  EventIdSchema,
  MediaEnvelopeSchema,
  MemorySpaceIdSchema,
  ProcessingProfile,
  ProtocolErrorCode,
  StartCaptureSchema,
  StopCaptureSchema,
  StopReason,
  decodeServerControl,
  encodeClientControl,
  encodeMediaEnvelope,
  timestampFromUnixMs,
  type CaptureBinding,
} from './audioV2'

import { IncrementalAudioPlayer, sameCaptureBinding, type PlaybackRenderer, type PlaybackActivity } from './incrementalAudioPlayer'
import { type VoiceTimingTrace } from './voiceTimingTrace'

export interface BrowserVoiceOptions {
  capabilities: CaptureCapabilities
  renderer: PlaybackRenderer
  onState: (state: ConversationState) => void
  onPlaybackError: (error: Error) => void
  timing?: VoiceTimingTrace
  onProcessing?: (update: VoiceProcessingUpdate | null) => void
  onPlaybackActivity?: (activity: PlaybackActivity) => void
}

const FRAME_SAMPLES = 320
const FRAME_DURATION_US = 20_000
const CONTROL_TIMEOUT_MS = 10_000

interface ControlWaiter {
  resolve: (value: any) => void
  reject: (error: Error) => void
  timeout: ReturnType<typeof setTimeout>
}

function id() {
  return create(EventIdSchema, { value: crypto.randomUUID() })
}

function spec(sampleRateHz = 16_000) {
  return create(AudioSpecSchema, {
    codec: AudioCodec.OPUS,
    sampleRateHz,
    channelCount: 1,
    frameDuration: create(DurationSchema, { nanos: 20_000_000 }),
    bitrateBps: 24_000,
  })
}

export class WebAudioV2Session {
  private socket: WebSocket | null = null
  private binding: CaptureBinding | null = null
  private encoder: any = null
  private pending = new Float32Array(0)
  private sequence = 0
  private frameTimestampUs = 0
  private capturedAtOriginMs = 0
  private waiters = new Map<string, ControlWaiter>()
  private sentEvents = new Map<string, { kind: string, action?: ConversationAction }>()
  private fatalErrorReported = false
  private closingNormally = false
  private disposed = false
  private player: IncrementalAudioPlayer | null = null
  private stopPromise: Promise<void> | null = null
  private state: ConversationState | null = null
  private pastInteractions = new Set<string>()
  private processingGeneration = -1n
  private processingSequence = -1n
  private processing: VoiceProcessingUpdate | null = null
  private processingEffectId = ''
  private processingFinished = false
  private pendingProcessing: VoiceProcessingUpdate | null = null
  private playoutAdmitted = false
  private awaitingEndAcknowledgement = false
  private endedInteractionId: string | null = null

  constructor(
    private readonly url: string,
    private readonly bearerToken: string,
    private readonly onClientId: (clientId: string) => void,
    private readonly onTranscript: (text: string, isFinal: boolean) => void,
    private readonly onFatalError: (error: Error) => void,
    private readonly voice?: BrowserVoiceOptions,
  ) {}

  async connect(): Promise<void> {
    if (this.disposed || this.socket) throw new Error('Audio V2 session cannot be connected twice')
    const AudioEncoderCtor = (globalThis as any).AudioEncoder
    const AudioDataCtor = (globalThis as any).AudioData
    if (!AudioEncoderCtor || !AudioDataCtor) {
      throw new Error('This browser does not provide the WebCodecs Opus encoder')
    }
    const socket = new WebSocket(this.url, 'chronicle.audio.v2')
    this.socket = socket
    socket.binaryType = 'arraybuffer'
    const hello = this.waitFor('hello')
    socket.onmessage = event => {
      if (this.disposed) return
      const receivedAtMs = performance.now()
      try {
        if (event.data instanceof ArrayBuffer) {
          const envelope = decodeMediaEnvelope(new Uint8Array(event.data))
          if (envelope.media.case === 'playback') this.voice?.timing?.record('playback_received', {
            receivedAtMs, token: `${envelope.media.value.responseId?.value}:${envelope.media.value.generation}`,
            sequence: Number(envelope.media.value.sequence), admitted: this.playoutAdmitted })
          if (envelope.media.case === 'playback' && this.playoutAdmitted) this.player?.append(envelope.media.value)
          return
        }
        if (typeof event.data !== 'string') throw new Error('Unsupported Audio V2 WebSocket message')
        const control = decodeServerControl(event.data)
        const kind = control.event.case
        if (kind !== 'transcriptUpdate' && (kind !== 'capturePacketAccepted' || control.event.value.sequence % 25n === 0n)) {
          this.voice?.timing?.record('control_received', { kind, receivedAtMs, serverEventId: control.eventId?.value,
            serverSentAtUnixMs: control.sentAt ? Number(control.sentAt.seconds) * 1000 + control.sentAt.nanos / 1e6 : undefined,
            acceptedSequence: kind === 'capturePacketAccepted' ? Number(control.event.value.sequence) : undefined,
            interactionId: kind === 'conversationState' ? control.event.value.interactionId : undefined,
            phase: kind === 'conversationState' ? control.event.value.phase : undefined })
        }
        if (kind === 'error') {
          const rejection = control.event.value
          const rejected = this.sentEvents.get(rejection.rejectedEventId?.value ?? '')
          if (rejection.code === ProtocolErrorCode.INVALID_TRANSITION &&
              (rejected?.kind === 'conversationCommand' || rejected?.kind === 'playbackAcknowledgement')) {
            // A cancellation can overtake a rendered-cursor ACK. It must never
            // destroy the independent microphone recording.
            if (rejected.kind === 'conversationCommand') {
              if (rejected.action === ConversationAction.END) this.awaitingEndAcknowledgement = false
              this.voice?.onPlaybackError(new Error(rejection.detail || 'Conversation command rejected'))
              if (rejected.action !== ConversationAction.SNAPSHOT) this.conversationCommand(ConversationAction.SNAPSHOT)
            }
            return
          }
          this.fail(new Error(rejection.detail || 'Audio V2 server rejected the request'))
          return
        }
        if (kind === 'hello') this.onClientId(control.event.value.clientId?.value ?? '')
        if (control.event.case === 'playbackOffer' && this.playoutAdmitted) this.player?.open(control.event.value)
        if (control.event.case === 'playbackFinished') void this.player?.finish(control.event.value)
        if (control.event.case === 'cancelPlayback') {
          const cancel = control.event.value
          if (sameCaptureBinding(this.binding ?? undefined, cancel.binding)) {
            if (cancel.generation > this.processingGeneration) {
              this.processingGeneration = cancel.generation
              this.processingSequence = -1n
              this.setProcessing(null)
            } else if (this.processing?.responseId?.value === cancel.responseId?.value) {
              this.processingFinished = true
              this.setProcessing(null)
            }
            this.player?.cancel(cancel)
          }
        }
        if (control.event.case === 'voiceProcessingUpdate') this.applyProcessing(control.event.value)
        if (control.event.case === 'conversationState') this.applyState(control.event.value)
        if (kind === 'transcriptUpdate') {
          const update = control.event.value
          if (update.text) this.onTranscript(update.text, update.isFinal)
        }
        const waiter = kind === undefined ? undefined : this.waiters.get(kind)
        if (waiter && kind !== undefined) {
          clearTimeout(waiter.timeout)
          this.waiters.delete(kind)
          waiter.resolve(control)
        }
      } catch (error) {
        this.fail(error instanceof Error ? error : new Error('Invalid Audio V2 server control'))
      }
    }
    const opened = this.waitFor('socketOpen')
    socket.onopen = () => {
      const waiter = this.waiters.get('socketOpen')
      if (!waiter) return
      clearTimeout(waiter.timeout)
      this.waiters.delete('socketOpen')
      waiter.resolve(undefined)
    }
    socket.onerror = () => {
      if (!this.disposed) this.fail(new Error('Audio V2 WebSocket failed'))
    }
    socket.onclose = event => {
      if (this.disposed || (this.closingNormally && event.code === 1000)) return
      const suffix = event.reason ? `: ${event.reason}` : ''
      this.fail(new Error(`Audio V2 WebSocket closed${suffix}`))
    }
    await opened
    this.send('hello', create(ClientHelloSchema, {
      bearerToken: this.bearerToken,
      sourceId: create(CaptureSourceIdSchema, { value: 'webui-recorder' }),
      deviceKind: DeviceKind.WEB_BROWSER,
      displayName: 'webui-recorder',
      supportedUplink: [spec()],
      supportedDownlink: this.voice ? [spec(24000)] : [],
    }))
    await hello
  }

  async start(memorySpaceId?: string): Promise<void> {
    const started = this.waitFor('captureStarted')
    this.send('startCapture', create(StartCaptureSchema, {
      // SOURCE_NATIVE is a direct capture stream, not a recoverable phone spool.
      // Its backend provenance invariant requires epoch zero.
      captureEpoch: this.voice ? BigInt(Date.now()) : 0n,
      processingProfile: this.voice ? ProcessingProfile.DUPLEX_ISOLATED : ProcessingProfile.SOURCE_NATIVE,
      capabilities: this.voice?.capabilities,
      dataPurpose: DataPurpose.NORMAL_CAPTURE,
      deliveryClass: DeliveryClass.LIVE,
      audioSpec: spec(),
      memorySpaceId: memorySpaceId
        ? create(MemorySpaceIdSchema, { value: memorySpaceId })
        : undefined,
    }))
    const control = await started
    this.binding = control.event.value.binding
    if (!this.binding) throw new Error('Capture started without a binding')
    this.voice?.timing?.bind(this.binding.captureSessionId?.value ?? '', this.binding.voiceSessionId?.value ?? '', String(this.binding.captureEpoch))
    if (this.voice) {
      if (!this.binding.voiceSessionId?.value) throw new Error('Interactive capture did not receive a voice session')
      this.player = new IncrementalAudioPlayer(this.binding, this.voice.renderer, ack => {
        if (this.socket?.readyState === WebSocket.OPEN) this.send('playbackAcknowledgement', ack)
      }, this.voice.onPlaybackError, undefined, this.voice.timing, this.voice.onPlaybackActivity)
      this.send('voiceReady', create(VoiceReadySchema, { binding: this.binding, capabilities: this.voice.capabilities }))
      this.conversationCommand(ConversationAction.SNAPSHOT)
    }
    this.sequence = 0
    this.frameTimestampUs = 0
    this.capturedAtOriginMs = Date.now()
    const Encoder = (globalThis as any).AudioEncoder
    this.encoder = new Encoder({
      output: (chunk: any) => this.sendEncoded(chunk),
      error: (error: Error) => this.fail(error),
    })
    this.encoder.configure({
      codec: 'opus',
      sampleRate: 16_000,
      numberOfChannels: 1,
      bitrate: 24_000,
      opus: { frameDuration: FRAME_DURATION_US },
    })
  }

  push(samples: Float32Array): void {
    if (!this.encoder || !this.binding || this.stopPromise || this.fatalErrorReported) return
    if (this.encoder.encodeQueueSize > 100 || (this.socket?.bufferedAmount ?? 0) > 256000) {
      this.fail(new Error('Capture connection cannot keep up with live audio'))
      return
    }
    const joined = new Float32Array(this.pending.length + samples.length)
    joined.set(this.pending)
    joined.set(samples, this.pending.length)
    let offset = 0
    const AudioDataCtor = (globalThis as any).AudioData
    while (joined.length - offset >= FRAME_SAMPLES) {
      const frame = joined.slice(offset, offset + FRAME_SAMPLES)
      const data = new AudioDataCtor({
        format: 'f32-planar',
        sampleRate: 16_000,
        numberOfFrames: FRAME_SAMPLES,
        numberOfChannels: 1,
        timestamp: this.frameTimestampUs,
        data: frame,
      })
      this.encoder.encode(data)
      this.voice?.timing?.submitted()
      data.close()
      this.frameTimestampUs += FRAME_DURATION_US
      offset += FRAME_SAMPLES
    }
    this.pending = joined.slice(offset)
  }

  startConversation(engine = SpeechEngine.MODULAR, threadId = ""): void {
    this.conversationCommand(ConversationAction.START, engine, "", threadId)
  }

  endConversation(): void {
    // A server cancellation cannot retract an offer already in the socket queue.
    // Reopen only once a new engagement has authoritatively entered LISTENING.
    this.playoutAdmitted = false
    this.setProcessing(null)
    this.pendingProcessing = null
    this.endedInteractionId = this.state?.interactionId || null
    this.awaitingEndAcknowledgement = this.state?.phase !== ConversationPhase.ENDED
    this.player?.cancelCurrent()
    this.conversationCommand(ConversationAction.END)
  }

  cancelTask(taskId: string): void {
    this.conversationCommand(ConversationAction.CANCEL_TASK, SpeechEngine.UNSPECIFIED, taskId)
  }

  stop(): Promise<void> {
    if (!this.stopPromise) this.stopPromise = this.stopBoundCapture()
    return this.stopPromise
  }

  /** Immediate teardown for owner disposal and failed setup; no second stop handshake. */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.pendingProcessing = null
    this.setProcessing(null)
    this.playoutAdmitted = false
    this.closingNormally = true
    this.rejectAll(new Error('Audio V2 session disposed'))
    this.player?.close()
    this.player = null
    this.voice?.renderer.close()
    if (this.encoder && this.encoder.state !== 'closed') this.encoder.close()
    this.encoder = null
    this.socket?.close(1000, 'capture-complete')
    this.socket = null
    this.binding = null
    this.pending = new Float32Array(0)
    this.sentEvents.clear()
    this.voice?.timing?.close()
  }

  private async stopBoundCapture(): Promise<void> {
    if (!this.socket) return
    try {
      if (this.voice && this.binding) this.endConversation()
      if (this.encoder) {
        await this.encoder.flush()
        this.encoder.close()
        this.encoder = null
      }
      if (this.binding) {
        const stopped = this.waitFor('captureStopped')
        this.send('stopCapture', create(StopCaptureSchema, { binding: this.binding, reason: StopReason.USER_REQUESTED }))
        await stopped
      }
    } finally { this.dispose() }
  }

  private conversationCommand(action: ConversationAction, engine = SpeechEngine.UNSPECIFIED, taskId = '', threadId = '', taskRevision = 0): void {
    if (!this.binding || !this.voice) throw new Error('Start a microphone recording with headphones to use conversation.')
    this.send('conversationCommand', create(ConversationCommandSchema, {
      binding: this.binding, action, engine, taskId, threadId, taskRevision: BigInt(taskRevision),
      interactionId: action === ConversationAction.END || action === ConversationAction.CANCEL_TASK ? this.state?.interactionId ?? '' : '',
    }))
  }

  private applyState(state: ConversationState): void {
    if (!sameCaptureBinding(this.binding ?? undefined, state.binding)) return
    if (state.phase === ConversationPhase.ENDED && state.interactionId === this.endedInteractionId) this.awaitingEndAcknowledgement = false
    if (this.state?.interactionId === state.interactionId && state.revision <= this.state.revision) return
    if (this.state?.interactionId !== state.interactionId) {
      if (this.pastInteractions.has(state.interactionId) || (!state.interactionId && this.state?.interactionId)) return
      if (this.state?.interactionId) this.pastInteractions.add(this.state.interactionId)
    }
    if (this.state?.interactionId !== state.interactionId) {
      this.processingGeneration = -1n
      this.processingEffectId = ''
      this.pendingProcessing = null
      this.setProcessing(null)
    }
    if (state.responseGeneration >= this.processingGeneration &&
        (state.responseGeneration > this.processingGeneration || state.responseEffectId !== this.processingEffectId)) {
      this.processingGeneration = state.responseGeneration
      this.processingEffectId = state.responseEffectId
      this.processingSequence = -1n
      this.processingFinished = false
      this.player?.advanceGeneration(state.responseGeneration)
      this.setProcessing(null)
    }
    if (state.phase === ConversationPhase.LISTENING && state.responseGeneration >= this.processingGeneration &&
        state.responseEffectId === this.processingEffectId) {
      this.processingFinished = true
      this.setProcessing(null)
    }
    this.state = state
    if (state.phase === ConversationPhase.ENDED) {
      this.playoutAdmitted = false
      this.setProcessing(null)
      this.awaitingEndAcknowledgement = false
      this.endedInteractionId = state.interactionId || null
      this.player?.cancelCurrent()
    } else if (this.awaitingEndAcknowledgement) {
      // End may have been clicked before the first engagement state arrived.
      this.endedInteractionId ??= state.interactionId || null
    } else if (state.phase === ConversationPhase.LISTENING && state.interactionId && state.interactionId !== this.endedInteractionId) {
      this.playoutAdmitted = true
    }
    const pending = this.pendingProcessing
    if (pending && pending.stateRevision <= state.revision) {
      this.pendingProcessing = null
      this.applyProcessing(pending)
    }
    this.voice?.onState(state)
  }

  private setProcessing(update: VoiceProcessingUpdate | null): void {
    const before = this.processing
    this.processing = update
    // Wire sequences/cursors do not cause React renders: only visible activities.
    if (Boolean(before?.transcribing) === Boolean(update?.transcribing) &&
        Boolean(before?.generatingText) === Boolean(update?.generatingText) &&
        Boolean(before?.synthesizingSpeech) === Boolean(update?.synthesizingSpeech) &&
        Boolean(before?.generatingResponse) === Boolean(update?.generatingResponse)) return
    this.voice?.onProcessing?.(update)
  }

  private applyProcessing(update: VoiceProcessingUpdate): void {
    if (!this.playoutAdmitted || !sameCaptureBinding(this.binding ?? undefined, update.binding) ||
        update.interactionId !== this.state?.interactionId || update.generation < this.processingGeneration || !update.effectId) return
    if (update.generation !== this.processingGeneration || update.effectId !== this.processingEffectId || update.stateRevision > this.state.revision) {
      // Cross-channel publication may arrive before admission. One bounded latest
      // snapshot waits for authoritative state; arrival order never admits work.
      if (update.stateRevision <= this.state.revision) return
      const pending = this.pendingProcessing
      if (!pending || update.stateRevision > pending.stateRevision ||
          (update.stateRevision === pending.stateRevision && update.effectId === pending.effectId && update.sequence > pending.sequence)) this.pendingProcessing = update
      return
    }
    if (this.processingFinished || update.sequence <= this.processingSequence) return
    this.processingSequence = update.sequence
    if (update.finished) this.processingFinished = true
    this.setProcessing(update.finished ? null : update)
  }

  private sendEncoded(chunk: any): void {
    if (!this.binding || !this.socket || this.socket.readyState !== WebSocket.OPEN) return
    const payload = new Uint8Array(chunk.byteLength)
    chunk.copyTo(payload)
    const sequence = this.sequence++
    this.socket.send(encodeMediaEnvelope(create(MediaEnvelopeSchema, {
      media: {
        case: 'capture',
        value: create(CaptureMediaPacketSchema, {
          binding: create(CaptureBindingSchema, this.binding),
          sequence: BigInt(sequence),
          capturedAt: timestampFromUnixMs(this.capturedAtOriginMs + sequence * 20),
          monotonicOffsetUs: BigInt(sequence * FRAME_DURATION_US),
          deliveryClass: DeliveryClass.LIVE,
          opusPayload: payload,
        }),
      },
    })))
    this.voice?.timing?.encoded(sequence, this.encoder?.encodeQueueSize ?? 0, this.socket.bufferedAmount)
  }

  private send(caseName: any, value: any): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error('Audio V2 WebSocket is not open')
    }
    const eventId = id()
    this.voice?.timing?.record('control_sent', { kind: caseName, eventId: eventId.value,
      socketBufferedBytes: this.socket.bufferedAmount, state: caseName === 'playbackAcknowledgement' ? value.state : undefined,
      token: caseName === 'playbackAcknowledgement' ? `${value.responseId?.value}:${value.generation}` : undefined,
      renderedSamples: caseName === 'playbackAcknowledgement' ? Number(value.renderedSamples) : undefined })
    this.sentEvents.set(eventId.value, { kind: caseName, action: caseName === 'conversationCommand' ? value.action : undefined })
    if (this.sentEvents.size > 1024) this.sentEvents.delete(this.sentEvents.keys().next().value!)
    this.socket.send(encodeClientControl(create(ClientControlSchema, {
      eventId,
      sentAt: timestampFromUnixMs(Date.now()),
      event: { case: caseName, value } as any,
    })))
  }

  private waitFor(kind: string): Promise<any> {
    const promise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(kind)
        reject(new Error(`Timed out waiting for Audio V2 ${kind}`))
      }, CONTROL_TIMEOUT_MS)
      this.waiters.set(kind, { resolve, reject, timeout })
    })
    // An earlier handshake can fail before its caller awaits this response.
    void promise.catch(() => undefined)
    return promise
  }

  private rejectAll(error: Error): void {
    for (const [kind, waiter] of this.waiters) {
      clearTimeout(waiter.timeout)
      waiter.reject(new Error(`${error.message} before ${kind}`))
    }
    this.waiters.clear()
  }

  private fail(error: Error): void {
    this.rejectAll(error)
    if (this.fatalErrorReported) return
    this.fatalErrorReported = true
    this.dispose()
    this.onFatalError(error)
  }
}
