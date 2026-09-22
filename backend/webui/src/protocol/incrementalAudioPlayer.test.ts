import { create } from '@bufbuild/protobuf'
import { DurationSchema } from '@bufbuild/protobuf/wkt'
import { describe, expect, it, vi } from 'vitest'
import { AudioCodec, AudioSpecSchema, CaptureBindingSchema, CancelPlaybackSchema, PlaybackFinishedSchema, PlaybackMediaPacketSchema, PlaybackOfferSchema, ResponseIdSchema, PlaybackState, type PlaybackAcknowledgement } from './audioV2'
import { IncrementalAudioPlayer, webCodecsOpusDecoder, type DecoderFactory, type PlaybackRenderer, type RenderProgress } from './incrementalAudioPlayer'
import { CaptureFrames, PlaybackQueue } from './voiceAudio.worklet.js'

const binding = create(CaptureBindingSchema, { captureSessionId: { value: 'capture' }, voiceSessionId: { value: 'voice' }, captureEpoch: 42n })
const offer = (generation = 1n) => create(PlaybackOfferSchema, {
  binding, responseId: { value: `response-${generation}` }, generation, incremental: true,
  audioSpec: create(AudioSpecSchema, { codec: AudioCodec.OPUS, sampleRateHz: 24000, channelCount: 1, frameDuration: create(DurationSchema, { nanos: 20000000 }) }),
})
const packet = (sequence: number, generation = 1n) => create(PlaybackMediaPacketSchema, {
  responseId: { value: `response-${generation}` }, generation, sequence: BigInt(sequence), opusPayload: new Uint8Array([1]),
})
class Renderer implements PlaybackRenderer {
  onProgress: (progress: RenderProgress) => void = () => {}
  queue = new PlaybackQueue(48000)
  token = ''
  open = vi.fn((token: string) => { this.token = token; this.queue.reset() })
  append = vi.fn((_token: string, samples: Float32Array) => this.queue.append(samples))
  finish = vi.fn(() => { this.queue.finished = true })
  cancel = vi.fn((token: string) => {
    const rendered = this.queue.rendered
    this.queue.reset()
    this.onProgress({ token, state: 'cancelled', rendered, buffered: 0 })
  })
  close = vi.fn()
  render(samples = 128) {
    const output = new Float32Array(samples)
    this.queue.render(output)
    this.onProgress({ token: this.token, state: this.queue.finished && this.queue.buffered === 0 ? 'done' : 'progress', rendered: this.queue.rendered, buffered: this.queue.buffered })
    return output
  }
}
function setup(onActivity = vi.fn()) {
  const renderer = new Renderer()
  const acks: PlaybackAcknowledgement[] = []
  const onError = vi.fn()
  const decoders: { output: (samples: Float32Array) => void, close: ReturnType<typeof vi.fn> }[] = []
  const factory: DecoderFactory = output => {
    const close = vi.fn()
    decoders.push({ output, close })
    return { decode: () => output(new Float32Array(480).fill(0.5)), flush: async () => {}, close }
  }
  const player = new IncrementalAudioPlayer(binding, renderer, ack => acks.push(ack), onError, factory, undefined, onActivity)
  return { renderer, acks, onError, decoders, player, onActivity }
}
const last = <T,>(items: T[]) => items[items.length - 1]

describe('incremental browser response adapter and audio-thread DSP', () => {
  it('reports semantic playback activity from actual rendering, with a 200ms starvation threshold', () => {
    const { player, renderer, onActivity } = setup()
    player.open(offer())
    expect(onActivity).toHaveBeenLastCalledWith('preparing')
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 0, buffered: 0 })
    expect(onActivity).toHaveBeenCalledTimes(1)
    for (let i = 0; i < 8; i++) player.append(packet(i))
    renderer.onProgress({ token: 'response-1:1', state: 'started', rendered: 240, buffered: 3000 })
    expect(onActivity).toHaveBeenLastCalledWith('playing')
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 240, buffered: 0, sampleRate: 48000, underrunRunSamples: 4800 })
    expect(onActivity).toHaveBeenCalledTimes(2)
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 240, buffered: 0, sampleRate: 48000, underrunRunSamples: 9600 })
    expect(onActivity).toHaveBeenLastCalledWith('buffering')
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 480, buffered: 1500, sampleRate: 48000, underrunRunSamples: 0 })
    expect(onActivity).toHaveBeenLastCalledWith('playing')
    player.cancelCurrent()
    expect(onActivity).toHaveBeenLastCalledWith('idle')
    const calls = onActivity.mock.calls.length
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 480, buffered: 0, sampleRate: 48000, underrunRunSamples: 48000 })
    expect(onActivity).toHaveBeenCalledTimes(calls)
  })
  it('keeps zero-render prebuffer progress local while allowing cancellation before playback starts', () => {
    const { player, renderer, acks, onActivity } = setup()
    player.open(offer())
    renderer.onProgress({ token: 'response-1:1', state: 'progress', rendered: 0, buffered: 0 })
    expect(acks).toHaveLength(0)
    expect(onActivity).toHaveBeenLastCalledWith('preparing')
    player.cancelCurrent()
    expect(acks).toHaveLength(1)
    expect(acks[0].state).toBe(PlaybackState.CANCELLED)
    expect(acks[0].renderedSamples).toBe(0n)
  })
  it('accepts a fresh sequential response in the same generation only after terminal playback', async () => {
    const { player, renderer, decoders } = setup()
    player.open(offer())
    const continuation = create(PlaybackOfferSchema, { ...offer(), responseId: create(ResponseIdSchema, { value: 'continuation' }) })
    player.open(continuation)
    expect(renderer.open).toHaveBeenCalledOnce() // concurrent same-generation replacement is forbidden
    player.append(packet(0))
    await player.finish(create(PlaybackFinishedSchema, { binding, responseId: { value: 'response-1' }, generation: 1n, totalSamples: 480n }))
    renderer.render(960)
    player.open(continuation)
    expect(renderer.open).toHaveBeenCalledTimes(2)
    expect(decoders).toHaveLength(2)
    player.open(offer())
    player.append(packet(1)) // retired response packet must not enter the new decoder
    expect(renderer.open).toHaveBeenCalledTimes(2)
    expect(renderer.append).toHaveBeenCalledTimes(1)
    player.cancelCurrent()
    player.open(continuation) // cancelled response identity cannot be replayed
    expect(renderer.open).toHaveBeenCalledTimes(2)
  })
  it('plays before producer completion with one decoder across frames', () => {
    const { player, renderer, decoders } = setup()
    player.open(offer())
    for (let i = 0; i < 8; i++) player.append(packet(i))
    expect(renderer.finish).not.toHaveBeenCalled()
    expect(renderer.render().some(v => v !== 0)).toBe(true)
    expect(decoders).toHaveLength(1)
  })
  it('trims the final padded Opus frame before it reaches playback', async () => {
    const { player, renderer, acks, onError } = setup()
    player.open(offer())
    player.append(packet(0))
    expect(renderer.append).not.toHaveBeenCalled()
    await player.finish(create(PlaybackFinishedSchema, { binding, responseId: { value: 'response-1' }, generation: 1n, totalSamples: 317n }))
    expect(renderer.append.mock.calls[0][1]).toHaveLength(317)
    renderer.render(960)
    expect(last(acks)?.state).toBe(PlaybackState.DONE)
    expect(last(acks)?.renderedSamples).toBe(317n)
    expect(last(acks)?.bufferedSamples).toBe(0n)
    expect(onError).not.toHaveBeenCalled()
  })
  it('excludes declared canonical Opus lookahead from rendered samples and trims the padded tail', async () => {
    const { player, renderer, acks, onError } = setup()
    player.open(create(PlaybackOfferSchema, { ...offer(), preSkipSamples: 156 }))
    player.append(packet(0))
    player.append(packet(1))
    expect(renderer.append.mock.calls[0][1]).toHaveLength(324)
    await player.finish(create(PlaybackFinishedSchema, { binding, responseId: { value: 'response-1' }, generation: 1n, totalSamples: 700n }))
    expect(renderer.append.mock.calls[1][1]).toHaveLength(376)
    renderer.render(1600)
    expect(last(acks)?.renderedSamples).toBe(700n)
    expect(last(acks)?.state).toBe(PlaybackState.DONE)
    expect(onError).not.toHaveBeenCalled()
  })
  it('cancels with the rendered cursor and fences late decoder callbacks and offers', () => {
    const { player, renderer, decoders, acks } = setup()
    player.open(offer())
    for (let i = 0; i < 8; i++) player.append(packet(i))
    renderer.render(480)
    player.cancel(create(CancelPlaybackSchema, { binding, responseId: { value: 'response-1' }, generation: 2n }))
    expect(last(acks)?.state).toBe(PlaybackState.CANCELLED)
    expect(last(acks)?.generation).toBe(1n)
    expect(last(acks)?.renderedSamples).toBe(240n)
    expect(last(acks)?.bufferedSamples).toBe(0n)
    const count = renderer.append.mock.calls.length
    decoders[0].output(new Float32Array(480))
    player.append(packet(8))
    player.open(offer(1n))
    expect(renderer.append).toHaveBeenCalledTimes(count)
    expect(renderer.open).toHaveBeenCalledOnce()
    expect(decoders[0].close).toHaveBeenCalled()
    player.open(offer(2n))
    expect(renderer.open).toHaveBeenCalledTimes(2)
  })
  it('rejects stale bindings and out-of-order packets', () => {
    const { player, renderer, onError, acks } = setup()
    player.open(create(PlaybackOfferSchema, { ...offer(), binding: { ...binding, captureEpoch: 41n } }))
    expect(renderer.open).not.toHaveBeenCalled()
    player.open(offer())
    player.append(packet(1))
    expect(onError).toHaveBeenCalledOnce()
    expect(last(acks)?.state).toBe(PlaybackState.FAILED)
  })
  it('bounds the decoder and posted audio queues when rendering stops', () => {
    const { player, onError, acks } = setup()
    player.open(offer())
    for (let i = 0; i < 101; i++) player.append(packet(i))
    expect(onError).toHaveBeenCalledOnce()
    expect(last(acks)?.state).toBe(PlaybackState.FAILED)
  })
  it('does not finish a cancelled response after delayed decoder flush', async () => {
    const renderer = new Renderer()
    let flush!: () => void
    const finished = new Promise<void>(resolve => { flush = resolve })
    const player = new IncrementalAudioPlayer(binding, renderer, () => {}, () => {}, output => ({ decode: () => output(new Float32Array(480)), flush: () => finished, close: () => {} }))
    player.open(offer())
    player.append(packet(0))
    const finishing = player.finish(create(PlaybackFinishedSchema, { binding, responseId: { value: 'response-1' }, generation: 1n, totalSamples: 480n }))
    player.cancelCurrent()
    flush()
    await finishing
    expect(renderer.finish).not.toHaveBeenCalled()
  })
  it.each([44100, 48000, 16000])('resamples %i Hz capture into contiguous20ms frames', rate => {
    const frames: Float32Array[] = []
    const capture = new CaptureFrames(rate, (frame: Float32Array) => frames.push(frame))
    for (let offset = 0; offset < rate; offset += 128) capture.push(new Float32Array(Math.min(128, rate - offset)).fill(0.25))
    expect(frames).toHaveLength(50)
    expect(frames.every(frame => frame.length === 320 && frame.every(v => Math.abs(v - 0.25) < 1e-6))).toBe(true)
  })
  it.each([44100, 48000, 16000])('counts only actual24k rendered samples at %i Hz', rate => {
    const queue = new PlaybackQueue(rate)
    queue.append(new Float32Array(24000).fill(0.25))
    queue.finished = true
    for (let i = 0; i < Math.ceil(rate / 128) + 1; i++) queue.render(new Float32Array(128))
    expect(queue.rendered).toBe(24000)
    expect(queue.buffered).toBe(0)
    queue.render(new Float32Array(128))
    expect(queue.rendered).toBe(24000)
  })
  it('normalizes Chromium48k decoded Opus to canonical24k and closes AudioData', async () => {
    const decoded: Float32Array[] = []
    const close = vi.fn()
    const error = vi.fn()
    vi.stubGlobal('AudioDecoder', class {
      state = 'configured'
      constructor(private callbacks: { output: (data: unknown) => void }) {}
      configure() {}
      decode() { this.callbacks.output({ sampleRate: 48000, numberOfChannels: 1, numberOfFrames: 960, copyTo: (target: Float32Array) => target.fill(0.4), close }) }
      async flush() {}
      close() { this.state = 'closed' }
    })
    vi.stubGlobal('EncodedAudioChunk', class {})
    try {
      const decoder = webCodecsOpusDecoder(frame => decoded.push(frame), error)
      decoder.decode(new Uint8Array([1]), 0)
      await decoder.flush()
      decoder.close()
      expect(decoded[0]).toHaveLength(480)
      expect(decoded[0][0]).toBeCloseTo(0.4)
      expect(close).toHaveBeenCalledOnce()
      expect(error).not.toHaveBeenCalled()
    } finally { vi.unstubAllGlobals() }
  })

})
