/* Browser audio-thread mechanics. No network, engagement state, or capture ownership. */
export class PlaybackQueue {
  constructor(rate, capacity = 48000, prebuffer = 2880) {
    this.rate = rate
    this.capacity = capacity
    this.prebuffer = prebuffer
    this.samples = new Float32Array(capacity)
    this.reset()
  }
  reset() {
    this.read = 0
    this.write = 0
    this.fraction = 0
    this.started = false
    this.finished = false
    this.underrunSamples = 0
    this.underrunRun = 0
    this.maxUnderrunSamples = 0
  }
  get rendered() { return this.read }
  get buffered() { return this.write - this.read }
  append(samples) {
    if (samples.length + this.buffered > this.capacity) throw new Error('Playback buffer exceeded two seconds')
    for (let i = 0; i < samples.length; i++) this.samples[(this.write + i) % this.capacity] = samples[i]
    this.write += samples.length
  }
  render(output) {
    output.fill(0)
    if (!this.started && (this.buffered >= this.prebuffer || (this.finished && this.buffered > 0))) this.started = true
    if (!this.started) return
    const step = 24000 / this.rate
    let i = 0
    for (; i < output.length && this.buffered > 0; i++) {
      const a = this.samples[this.read % this.capacity]
      const b = this.buffered > 1 ? this.samples[(this.read + 1) % this.capacity] : a
      output[i] = a + (b - a) * this.fraction
      this.fraction += step
      const advance = Math.min(Math.floor(this.fraction), this.buffered)
      this.read += advance
      this.fraction -= advance
      if (this.buffered === 0) this.fraction = 0
    }
    const missing = this.finished ? 0 : output.length - i
    this.underrunSamples += missing
    this.underrunRun = missing === output.length ? this.underrunRun + missing : missing
    this.maxUnderrunSamples = Math.max(this.maxUnderrunSamples, this.underrunRun)
  }
}

export class CaptureFrames {
  constructor(rate, emit) {
    this.ratio = rate / 16000
    this.emit = emit
    this.frame = new Float32Array(320)
    this.position = 0
    this.weight = 0
    this.sum = 0
  }
  push(input) {
    for (const sample of input) {
      let remaining = 1
      while (remaining > 1e-9) {
        const take = Math.min(remaining, this.ratio - this.weight)
        this.sum += sample * take
        this.weight += take
        remaining -= take
        if (this.weight >= this.ratio - 1e-9) {
          this.frame[this.position++] = this.sum / this.ratio
          this.weight = 0
          this.sum = 0
          if (this.position === 320) {
            this.emit(this.frame)
            this.frame = new Float32Array(320)
            this.position = 0
          }
        }
      }
    }
  }
}

// The guard lets the same DSP run in deterministic tests without emulating a browser.
if (typeof registerProcessor !== 'undefined') {
  class ChroniclePlayback extends AudioWorkletProcessor {
    constructor() {
      super()
      this.queue = new PlaybackQueue(sampleRate)
      this.token = null
      this.terminal = false
      this.reportedStart = false
      this.sinceProgress = 0
      this.renderFrame = currentFrame
      this.port.onmessage = ({ data }) => {
        if (data.kind === 'open') {
          this.queue.reset()
          this.token = data.token
          this.terminal = false
          this.reportedStart = false
          this.sinceProgress = 0
          this.renderFrame = currentFrame
          return
        }
        if (data.token !== this.token || this.terminal) return
        if (data.kind === 'cancel') {
          // Post the actual last rendered cursor before clearing the FIFO.
          this.queue.write = this.queue.read
          this.report('cancelled')
          this.terminal = true
        } else if (data.kind === 'append') {
          try { this.queue.append(data.samples) } catch (error) {
            this.queue.write = this.queue.read
            this.report('failed', String(error))
            this.terminal = true
          }
        } else if (data.kind === 'finish') {
          this.queue.finished = true
        }
      }
    }
    report(state, detail = '') {
      this.port.postMessage({ token: this.token, state, rendered: this.queue.rendered, buffered: this.queue.buffered, detail,
        audioFrame: this.renderFrame, sampleRate, underrunSamples: this.queue.underrunSamples, maxUnderrunSamples: this.queue.maxUnderrunSamples, underrunRunSamples: this.queue.underrunRun })
    }
    process(_inputs, outputs) {
      const output = outputs[0][0]
      if (!this.token || this.terminal) { output.fill(0); return true }
      this.queue.render(output)
      this.renderFrame = currentFrame + output.length
      if (this.queue.started && !this.reportedStart) {
        this.reportedStart = true
        this.report('started')
      }
      this.sinceProgress += output.length
      if (this.sinceProgress >= sampleRate / 10) {
        this.sinceProgress = 0
        this.report('progress')
      }
      if (this.queue.finished && this.queue.buffered === 0) {
        this.report('done')
        this.terminal = true
      }
      return true
    }
  }
  class ChronicleCapture extends AudioWorkletProcessor {
    constructor() {
      super()
      this.frames = new CaptureFrames(sampleRate, frame => this.port.postMessage({ samples: frame,
        audioFrame: currentFrame, sampleRate }, [frame.buffer]))
    }
    process(inputs, outputs) {
      // Keep the graph alive, but never monitor microphone audio to the headphones.
      outputs[0]?.forEach(channel => channel.fill(0))
      if (inputs[0]?.[0]) this.frames.push(inputs[0][0])
      return true
    }
  }
  registerProcessor('chronicle-playback', ChroniclePlayback)
  registerProcessor('chronicle-capture', ChronicleCapture)
}
