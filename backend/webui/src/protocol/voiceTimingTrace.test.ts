// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { VoiceTimingTrace } from './voiceTimingTrace'
import { PlaybackQueue } from './voiceAudio.worklet.js'

describe('bounded browser voice timing evidence', () => {
  afterEach(() => vi.useRealTimers())

  it('keeps ordered bounded history, clock anchors and immutable terminal snapshots', async () => {
    vi.useFakeTimers()
    let now = 100
    const publish = vi.fn()
    const trace = new VoiceTimingTrace(3, () => now++, publish)
    trace.bind('capture', 'voice', '42')
    trace.record('playback_open', { token: 'response:1' })
    trace.record('decoded', { token: 'response:1', canonicalSamples: 480 })
    trace.record('render_progress', { token: 'response:1', state: 'done', renderedSamples: 480,
      audioFrame: 88200, sampleRate: 44100 })
    trace.checkpoint('response_done:response:1')
    trace.checkpoint('response_done:response:1')
    trace.record('unrelated_later_event')
    await vi.runOnlyPendingTimersAsync()
    expect(publish).toHaveBeenCalledOnce()
    const report = JSON.parse(publish.mock.calls[0][0])
    expect(report.droppedPoints).toBe(1)
    expect(report.points.map((p: any) => p.stage)).toEqual(['playback_open', 'decoded', 'render_progress'])
    expect(report.firstPoint.stage).toBe('capture_bound')
    expect(report.lastPoint.stage).toBe('render_progress')
    expect(report.responseAnchors['response:1'].playback_open.monotonicMs).toBe(102)
    trace.close(); trace.close()
    await vi.runOnlyPendingTimersAsync()
    expect(publish).toHaveBeenCalledTimes(2)
    expect(JSON.parse(publish.mock.calls[1][0]).checkpoint).toBe('capture_closed')
  })

  it('bounds serialized UTF-8 bytes and isolates diagnostic upload failures', async () => {
    vi.useFakeTimers()
    const publish = vi.fn((_body: string) => { throw new Error('offline') })
    const trace = new VoiceTimingTrace(4096, () => 1, publish)
    for (let i = 0; i < 4096; i++) trace.record('capture_sent', { sequence: i, diagnosticFixture: 'अ'.repeat(512) })
    trace.close()
    await expect(vi.runOnlyPendingTimersAsync()).resolves.not.toThrow()
    const body = publish.mock.calls[0][0]
    expect(new TextEncoder().encode(body).length).toBeLessThanOrEqual(2_000_000)
    expect(JSON.parse(body).droppedPoints).toBeGreaterThan(0)
  })

  it('counts missing queued output separately from startup and natural PCM silence', () => {
    const queue = new PlaybackQueue(48000, 48000, 1)
    queue.render(new Float32Array(480))
    expect(queue.underrunSamples).toBe(0)
    queue.append(new Float32Array(480).fill(0.5))
    queue.render(new Float32Array(960))
    queue.render(new Float32Array(480))
    expect(queue.underrunSamples).toBe(480)
    queue.append(new Float32Array(240))
    queue.render(new Float32Array(480))
    expect(queue.underrunSamples).toBe(480)
    queue.render(new Float32Array(256)); queue.render(new Float32Array(256))
    expect(queue.maxUnderrunSamples).toBe(512)
    queue.reset()
    expect(queue.underrunSamples).toBe(0)
    expect(queue.maxUnderrunSamples).toBe(0)
  })
})
