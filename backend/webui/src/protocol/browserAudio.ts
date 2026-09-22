import { type PlaybackRenderer, type RenderProgress } from './incrementalAudioPlayer'
import { type CaptureClock } from './voiceTimingTrace'

const loaded = new WeakMap<AudioContext, Promise<void>>()
export function loadVoiceWorklet(context: AudioContext): Promise<void> {
  let promise = loaded.get(context)
  if (!promise) {
    promise = context.audioWorklet.addModule(new URL('./voiceAudio.worklet.js', import.meta.url))
    loaded.set(context, promise)
  }
  return promise
}

export function conversationSupportError(): string | null {
  if (!globalThis.isSecureContext) return 'Conversation needs HTTPS or localhost.'
  if (!(globalThis as any).AudioEncoder || !(globalThis as any).AudioDecoder || !(globalThis as any).AudioData) {
    return 'Conversation needs a desktop Chromium browser with WebCodecs audio support.'
  }
  if (!globalThis.AudioWorkletNode || !globalThis.AudioContext) return 'This browser does not support AudioWorklet playback.'
  return null
}

export async function checkOpusSupport(): Promise<void> {
  const encoder = (globalThis as any).AudioEncoder
  const decoder = (globalThis as any).AudioDecoder
  const [input, output] = await Promise.all([
    encoder.isConfigSupported({ codec: 'opus', sampleRate: 16000, numberOfChannels: 1, bitrate: 24000 }),
    decoder.isConfigSupported({ codec: 'opus', sampleRate: 24000, numberOfChannels: 1 }),
  ])
  if (!input.supported || !output.supported) throw new Error('This browser cannot encode and play the required Opus audio.')
}

export class WorkletPlaybackRenderer implements PlaybackRenderer {
  private constructor(private node: AudioWorkletNode) {}

  static async create(context: AudioContext): Promise<WorkletPlaybackRenderer> {
    await loadVoiceWorklet(context)
    const node = new AudioWorkletNode(context, 'chronicle-playback', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] })
    node.connect(context.destination)
    return new WorkletPlaybackRenderer(node)
  }
  onProgress: (progress: RenderProgress) => void = () => {}
  open(token: string): void {
    this.node.port.onmessage = ({ data }) => this.onProgress(data)
    this.node.port.postMessage({ kind: 'open', token })
  }
  append(token: string, samples: Float32Array): void { this.node.port.postMessage({ kind: 'append', token, samples }, [samples.buffer]) }
  finish(token: string): void { this.node.port.postMessage({ kind: 'finish', token }) }
  cancel(token: string): void { this.node.port.postMessage({ kind: 'cancel', token }) }
  close(): void { this.node.port.onmessage = null; this.node.port.close(); this.node.disconnect() }
}

export async function createCaptureWorklet(context: AudioContext, onFrame: (samples: Float32Array, clock: CaptureClock) => void): Promise<AudioWorkletNode> {
  await loadVoiceWorklet(context)
  const node = new AudioWorkletNode(context, 'chronicle-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit' })
  node.port.onmessage = ({ data }) => onFrame(data.samples, { audioFrame: data.audioFrame, sampleRate: data.sampleRate })
  return node
}
