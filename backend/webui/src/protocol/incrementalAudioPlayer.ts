import { create } from '@bufbuild/protobuf'
import { type VoiceTimingTrace } from './voiceTimingTrace'
import {
  AudioCodec, PlaybackAcknowledgementSchema, PlaybackState, ProtocolErrorCode,
  type CaptureBinding, type CancelPlayback, type PlaybackAcknowledgement,
  type PlaybackFinished, type PlaybackMediaPacket, type PlaybackOffer,
} from './audioV2'

export type PlaybackActivity = 'idle' | 'preparing' | 'playing' | 'buffering'

export interface RenderProgress {
  token: string
  state: 'started' | 'progress' | 'done' | 'cancelled' | 'failed'
  rendered: number
  buffered: number
  detail?: string
  audioFrame?: number
  sampleRate?: number
  underrunSamples?: number
  maxUnderrunSamples?: number
  underrunRunSamples?: number
}
export interface PlaybackRenderer {
  onProgress: (progress: RenderProgress) => void
  open(token: string): void
  append(token: string, samples: Float32Array): void
  finish(token: string): void
  cancel(token: string): void
  close(): void
}
export interface OpusDecoder {
  decode(payload: Uint8Array, timestampUs: number): void
  flush(): Promise<void>
  close(): void
}
export type DecoderFactory = (onSamples: (samples: Float32Array, clock?: { nativeDecodedSampleRateHz: number; nativeDecodedFrames: number; decodedTimestampUs: number }) => void, onError: (error: Error) => void) => OpusDecoder

export function sameCaptureBinding(a?: CaptureBinding, b?: CaptureBinding): boolean {
  return Boolean(a && b && a.captureSessionId?.value &&
    a.captureSessionId.value === b.captureSessionId?.value &&
    a.voiceSessionId?.value === b.voiceSessionId?.value && a.captureEpoch === b.captureEpoch)
}

export const webCodecsOpusDecoder: DecoderFactory = (output, error) => {
  // Opus is stateful: one decoder per response, never one per packet.
  const Decoder = (globalThis as any).AudioDecoder
  const Chunk = (globalThis as any).EncodedAudioChunk
  let closed = false
  const decoder = new Decoder({
    output: (data: any) => {
      try {
        if (closed) return
        if (![24000, 48000].includes(data.sampleRate) || data.numberOfChannels !== 1) {
          throw new Error(`Unexpected decoded audio format: ${data.sampleRate} Hz / ${data.numberOfChannels} channels`)
        }
        const samples = new Float32Array(data.numberOfFrames)
        const clock = { nativeDecodedSampleRateHz: data.sampleRate, nativeDecodedFrames: data.numberOfFrames, decodedTimestampUs: data.timestamp }
        data.copyTo(samples, { planeIndex: 0, format: 'f32-planar' })
        // Chromium decodes raw Opus at48k even when configured24k. Keep the
        // renderer timeline and all acknowledgements in canonical24k samples.
        if (data.sampleRate === 48000) {
          if (samples.length % 2 !== 0) throw new Error('Odd-sized48k Opus output')
          const canonical = new Float32Array(samples.length / 2)
          for (let i = 0; i < canonical.length; i++) canonical[i] = (samples[i * 2] + samples[i * 2 + 1]) / 2
          output(canonical, clock)
        } else output(samples, clock)
      } catch (cause) { error(cause instanceof Error ? cause : new Error(String(cause))) }
      finally { data.close() }
    },
    error,
  })
  decoder.configure({ codec: 'opus', sampleRate: 24000, numberOfChannels: 1 })
  return {
    decode: (data, timestamp) => decoder.decode(new Chunk({ type: 'key', timestamp, duration: 20000, data })),
    flush: () => decoder.flush(),
    close: () => { closed = true; if (decoder.state !== 'closed') decoder.close() },
  }
}

interface Response {
  offer: PlaybackOffer
  token: string
  decoder: OpusDecoder
  sequence: bigint
  submitted: number
  decoded: number
  skipRemaining: number
  delivered: number
  tail: Float32Array | null
  rendered: number
  finishing: boolean
  failed: boolean
}

/** Output-only adapter: cancellation never touches capture, its encoder, or its socket. */
export class IncrementalAudioPlayer {
  private active: Response | null = null
  private retiring = new Map<string, Response>()
  private highestGeneration = -1n
  private seenResponseIds = new Set<string>()
  private generationFloor = -1n
  private cancelledTokens = new Set<string>()
  private closed = false
  private activity: PlaybackActivity = 'idle'

  constructor(
    private binding: CaptureBinding,
    private renderer: PlaybackRenderer,
    private acknowledge: (ack: PlaybackAcknowledgement) => void,
    private onError: (error: Error) => void,
    private decoderFactory: DecoderFactory = webCodecsOpusDecoder,
    private timing?: VoiceTimingTrace,
    private onActivity?: (activity: PlaybackActivity) => void,
  ) {
    renderer.onProgress = progress => this.progress(progress)
  }

  open(offer: PlaybackOffer): void {
    if (this.closed || !sameCaptureBinding(this.binding, offer.binding) || offer.generation < this.highestGeneration ||
        (offer.generation === this.highestGeneration && (this.active !== null || this.seenResponseIds.has(offer.responseId?.value ?? '') || this.seenResponseIds.size >= 256)) || offer.generation < this.generationFloor || this.cancelledTokens.has(`${offer.responseId?.value}:${offer.generation}`)) return
    if (offer.generation > this.highestGeneration) this.seenResponseIds.clear()
    this.highestGeneration = offer.generation
    this.seenResponseIds.add(offer.responseId?.value ?? '')
    this.cancelCurrent()
    if (!offer.incremental || !offer.responseId?.value || !Number.isSafeInteger(offer.preSkipSamples) || offer.preSkipSamples < 0 || offer.preSkipSamples > 48000 || offer.audioSpec?.codec !== AudioCodec.OPUS ||
      offer.audioSpec.sampleRateHz !== 24000 || offer.audioSpec.channelCount !== 1 ||
      offer.audioSpec.frameDuration?.nanos !== 20000000 || Number(offer.audioSpec.frameDuration.seconds) !== 0) {
      this.acknowledge(create(PlaybackAcknowledgementSchema, {
        binding: this.binding, responseId: offer.responseId, generation: offer.generation,
        state: PlaybackState.FAILED, errorCode: ProtocolErrorCode.UNSUPPORTED_AUDIO_FORMAT,
      }))
      this.onError(new Error('The browser requires incremental 24 kHz mono Opus playback.'))
      return
    }
    const response = {
      offer, token: `${offer.responseId.value}:${offer.generation}`, sequence: 0n,
      submitted: 0, decoded: 0, skipRemaining: offer.preSkipSamples, delivered: 0, tail: null, rendered: 0, finishing: false, failed: false,
    } as Response
    this.active = response
    this.setActivity('preparing')
    this.timing?.record('playback_open', { token: response.token, preSkipSamples: offer.preSkipSamples })
    this.renderer.open(response.token)
    try {
      response.decoder = this.decoderFactory((samples, clock) => {
        if (this.active !== response || response.failed) return
        this.timing?.record('decoded', { token: response.token, canonicalSamples: samples.length, ...clock })
        // Decoder output is already normalized to canonical24k. Opus lookahead
        // is transport delay, not heard speech or a rendered-cursor advance.
        if (response.skipRemaining > 0) {
          const skip = Math.min(response.skipRemaining, samples.length)
          response.skipRemaining -= skip
          samples = samples.slice(skip)
          if (!samples.length) return
        }
        response.decoded += samples.length
        if (response.decoded - response.rendered > 48000) {
          this.fail(response, new Error('Playback buffer exceeded two seconds'))
          return
        }
        // Retain only the final packet until its valid sample count is known.
        // PlaybackFinished excludes the Opus encoder's final-frame zero padding.
        if (response.tail) {
          response.delivered += response.tail.length
          this.renderer.append(response.token, response.tail)
          this.timing?.record('worklet_append', { token: response.token, deliveredSamples: response.delivered })
        }
        response.tail = samples
      }, error => this.fail(response, error))
    } catch (error) { this.fail(response, error instanceof Error ? error : new Error(String(error))) }
  }

  append(packet: PlaybackMediaPacket): void {
    const response = this.active
    if (!response || response.offer.responseId?.value !== packet.responseId?.value || response.offer.generation !== packet.generation) return
    if (packet.sequence !== response.sequence || response.finishing || !packet.opusPayload.length || packet.finalPacket) {
      this.fail(response, new Error('Invalid incremental playback packet order or termination'))
      return
    }
    // Bound decoder input as well as PCM/worklet queues. Each packet is 20 ms.
    if (response.submitted + 480 - response.rendered > 48000) {
      this.fail(response, new Error('Playback producer exceeded the browser buffer limit'))
      return
    }
    response.submitted += 480
    response.sequence++
    this.timing?.record('decode_submitted', { token: response.token, sequence: Number(packet.sequence), payloadBytes: packet.opusPayload.length })
    try { response.decoder.decode(packet.opusPayload, Number(packet.sequence) * 20000) }
    catch (error) { this.fail(response, error instanceof Error ? error : new Error(String(error))) }
  }

  async finish(finished: PlaybackFinished): Promise<void> {
    const response = this.active
    if (!response || !sameCaptureBinding(this.binding, finished.binding) ||
      response.offer.responseId?.value !== finished.responseId?.value || response.offer.generation !== finished.generation || response.finishing) return
    response.finishing = true
    this.timing?.record('producer_finished', { token: response.token, totalSamples: Number(finished.totalSamples) })
    try {
      await response.decoder.flush()
      if (this.active !== response) return
      const total = Number(finished.totalSamples)
      if (response.skipRemaining > 0 || !Number.isSafeInteger(total) || total < response.delivered || total > response.decoded || response.decoded - total >= 480) {
        throw new Error('Playback length does not match producer completion')
      }
      if (response.tail && total > response.delivered) this.renderer.append(response.token, response.tail.slice(0, total - response.delivered))
      response.delivered = total
      response.tail = null
      this.renderer.finish(response.token)
    } catch (error) {
      if (this.active === response) this.fail(response, error instanceof Error ? error : new Error(String(error)))
    }
  }

  cancel(cancel: CancelPlayback): void {
    if (!sameCaptureBinding(this.binding, cancel.binding)) return
    this.generationFloor = cancel.generation > this.generationFloor ? cancel.generation : this.generationFloor
    this.cancelledTokens.add(`${cancel.responseId?.value}:${cancel.generation}`)
    if (this.cancelledTokens.size > 256) this.cancelledTokens.delete(this.cancelledTokens.values().next().value!)
    const response = this.active
    if (response && response.offer.responseId?.value === cancel.responseId?.value && response.offer.generation <= cancel.generation) this.cancelCurrent()
  }

  advanceGeneration(generation: bigint): void {
    if (generation <= this.generationFloor) return
    this.generationFloor = generation
    if (this.active && this.active.offer.generation < generation) this.cancelCurrent()
  }

  cancelCurrent(): void {
    const response = this.active
    if (!response) return
    this.setActivity('idle')
    this.active = null // Fence decoder callbacks synchronously before posting to the audio thread.
    response.decoder?.close()
    this.retiring.set(response.token, response)
    this.renderer.cancel(response.token)
  }

  close(): void {
    if (this.closed) return
    this.cancelCurrent()
    this.closed = true
    this.retiring.clear()
    this.renderer.close()
  }

  private fail(response: Response, error: Error): void {
    if (this.active !== response) return
    response.failed = true
    this.onError(error)
    this.cancelCurrent()
  }

  private setActivity(activity: PlaybackActivity): void {
    if (this.activity === activity) return
    this.activity = activity
    this.onActivity?.(activity)
  }

  private progress(progress: RenderProgress): void {
    if (this.closed) return
    const response = this.active?.token === progress.token ? this.active : this.retiring.get(progress.token)
    if (!response) return
    const retired = this.active !== response
    if (retired && progress.state !== 'cancelled' && progress.state !== 'failed') return
    if (!Number.isSafeInteger(progress.rendered) || progress.rendered < response.rendered || progress.rendered > response.decoded) return
    const advanced = progress.rendered > response.rendered
    response.rendered = progress.rendered
    this.timing?.record('render_progress', { token: progress.token, state: progress.state, renderedSamples: progress.rendered,
      bufferedSamples: progress.buffered, audioFrame: progress.audioFrame, sampleRate: progress.sampleRate,
      underrunSamples: progress.underrunSamples, maxUnderrunSamples: progress.maxUnderrunSamples })
    const terminal = ['done', 'cancelled', 'failed'].includes(progress.state)
    if (!retired) {
      if (terminal) this.setActivity('idle')
      // At most one semantic update per 100ms Worklet report. Brief packet gaps
      // stay quiet; Buffering means 200ms of continuous missing queued samples.
      else if ((progress.sampleRate ?? 0) > 0 && (progress.underrunRunSamples ?? 0) >= progress.sampleRate! * 0.2) this.setActivity('buffering')
      else if (progress.state === 'started' || advanced) this.setActivity('playing')
    }
    // Prebuffer reports are local observations, not valid PROGRESS transitions
    // while the server is still OFFERED. Terminal outcomes remain admissible.
    if (progress.state === 'progress' && progress.rendered === 0) return
    this.acknowledge(create(PlaybackAcknowledgementSchema, {
      binding: this.binding,
      responseId: response.offer.responseId,
      generation: response.offer.generation,
      state: response.failed ? PlaybackState.FAILED : {
        started: PlaybackState.STARTED, progress: PlaybackState.PROGRESS, done: PlaybackState.DONE,
        cancelled: PlaybackState.CANCELLED, failed: PlaybackState.FAILED,
      }[progress.state],
      monotonicTimestampUs: BigInt(Math.round(performance.now() * 1000)),
      renderedSamples: BigInt(progress.rendered),
      bufferedSamples: BigInt(Math.max(0, progress.buffered)),
      errorCode: response.failed || progress.state === 'failed' ? ProtocolErrorCode.INVALID_MEDIA : ProtocolErrorCode.UNSPECIFIED,
    }))
    if (terminal) {
      this.timing?.checkpoint(`response_${progress.state}:${response.token}`)
      response.decoder?.close()
      if (this.active === response) this.active = null
      this.retiring.delete(response.token)
      if (progress.state === 'failed' && !response.failed) this.onError(new Error(progress.detail || 'Audio rendering failed'))
    }
  }
}
