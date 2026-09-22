/** Bounded browser timing evidence. Never stores audio, text, credentials or device labels. */
export interface CaptureClock { audioFrame: number; sampleRate: number }
type Fields = Record<string, string | number | boolean | undefined>
export interface VoiceTimingPoint { stage: string; monotonicMs: number; fields: Fields }

export class VoiceTimingTrace {
  private points: VoiceTimingPoint[] = []
  private cursor = 0
  private dropped = 0
  private lastCaptureReport = -Infinity
  private capturedFrames = 0
  private capturedSamples = 0
  private encodedFrames = 0
  private submittedFrames = 0
  private binding: Fields = {}
  private clockTimer?: ReturnType<typeof setInterval>
  private stopClock?: () => void
  private closed = false
  private checkpoints = new Set<string>()
  private firstPoint?: VoiceTimingPoint
  private lastPoint?: VoiceTimingPoint
  private responseAnchors = new Map<string, Record<string, VoiceTimingPoint>>()
  readonly startedAtUnixMs: number
  readonly startedAtMonotonicMs: number

  constructor(private capacity = 4096, private now = () => performance.now(), private publish?: (body: string) => void) {
    this.startedAtUnixMs = Date.now()
    this.startedAtMonotonicMs = now()
  }

  record(stage: string, fields: Fields = {}): void {
    const point = { stage, monotonicMs: this.now(), fields: { ...fields } }
    this.firstPoint ??= point
    this.lastPoint = point
    if (typeof fields.token === 'string') {
      let anchors = this.responseAnchors.get(fields.token)
      if (!anchors) {
        anchors = {}
        this.responseAnchors.set(fields.token, anchors)
        if (this.responseAnchors.size > 16) this.responseAnchors.delete(this.responseAnchors.keys().next().value!)
      }
      const key = stage === 'render_progress' ? `${stage}_${fields.state}` : stage
      anchors[key] ??= point
      anchors.last = point
    }
    if (this.points.length < this.capacity) this.points.push(point)
    else { this.points[this.cursor] = point; this.cursor = (this.cursor + 1) % this.capacity; this.dropped++ }
  }

  bind(captureSessionId: string, voiceSessionId: string, captureEpoch: string): void {
    this.binding = { captureSessionId, voiceSessionId, captureEpoch }
    this.record('capture_bound')
  }

  capture(samples: number, clock?: CaptureClock): void {
    this.capturedFrames++
    this.capturedSamples += samples
    if (this.now() - this.lastCaptureReport < 500) return
    this.lastCaptureReport = this.now()
    this.record('capture_callback', { capturedFrames: this.capturedFrames, capturedSamples: this.capturedSamples,
      submittedFrames: this.submittedFrames, encodedFrames: this.encodedFrames, ...clock })
  }

  submitted(): void { this.submittedFrames++ }
  encoded(sequence: number, encoderQueue: number, socketBufferedBytes: number): void {
    this.encodedFrames++
    // Preserve transport cadence without producing 50 diagnostic entries per second.
    if (sequence % 25 === 0) this.record('capture_sent', { sequence, encoderQueue, socketBufferedBytes, encodedFrames: this.encodedFrames })
  }

  observeContext(context: AudioContext): void {
    const sample = () => {
      try {
        const output = context.getOutputTimestamp?.()
        this.record('audio_clock', { audioTimeSeconds: context.currentTime, sampleRate: context.sampleRate,
          state: context.state, baseLatencySeconds: context.baseLatency, outputLatencySeconds: context.outputLatency,
          outputContextTimeSeconds: output?.contextTime, outputPerformanceTimeMs: output?.performanceTime })
      } catch { this.record('audio_clock_unavailable', { state: context.state }) }
    }
    sample()
    context.addEventListener('statechange', sample)
    this.stopClock = () => context.removeEventListener('statechange', sample)
    this.clockTimer = setInterval(sample, 500)
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    if (this.clockTimer) clearInterval(this.clockTimer)
    this.clockTimer = undefined
    this.stopClock?.()
    this.stopClock = undefined
    this.record('trace_closed', { capturedFrames: this.capturedFrames, capturedSamples: this.capturedSamples,
      submittedFrames: this.submittedFrames, encodedFrames: this.encodedFrames })
    this.checkpoint('capture_closed')
  }

  checkpoint(key: string): void {
    if (this.checkpoints.has(key)) return
    this.checkpoints.add(key)
    if (this.checkpoints.size > 256) this.checkpoints.delete(this.checkpoints.values().next().value!)
    const report = { ...this.snapshot(), checkpoint: key }
    // Serialize off the decoder/ACK callback, only at terminal checkpoints.
    setTimeout(() => {
      let body = JSON.stringify(report)
      // Leave room below the endpoint's 2.1 MB UTF-8 cap.
      while (new TextEncoder().encode(body).length > 2_000_000 && report.points.length) {
        const count = Math.max(1, Math.ceil(report.points.length / 4))
        report.points.splice(0, count)
        report.droppedPoints += count
        body = JSON.stringify(report)
      }
      try { this.publish?.(body) } catch { /* Diagnostic failure never interrupts capture. */ }
    }, 0)
  }

  snapshot() {
    const points = this.dropped ? [...this.points.slice(this.cursor), ...this.points.slice(0, this.cursor)] : [...this.points]
    return { version: 1, browser: navigator.userAgent.slice(0, 2048), startedAtUnixMs: this.startedAtUnixMs,
      startedAtMonotonicMs: this.startedAtMonotonicMs, clockNote: 'monotonicMs is browser callback receipt; audioFrame/sampleRate is the Worklet source clock (capture quantum start; playback rendered quantum end). getOutputTimestamp pairs outputContextTimeSeconds with outputPerformanceTimeMs when supported; these estimate hardware output scheduling, not physical acoustic measurement. Browser/server wall clocks are not synchronized by this report.',
      binding: this.binding, firstPoint: this.firstPoint, lastPoint: this.lastPoint,
      responseAnchors: Object.fromEntries([...this.responseAnchors].map(([token, anchors]) => [token, { ...anchors }])),
      capacity: this.capacity, droppedPoints: this.dropped, points }
  }
}
