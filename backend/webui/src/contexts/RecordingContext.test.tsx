// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { create, fromJsonString } from '@bufbuild/protobuf'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../services/api'
import { ClientControlSchema, decodeMediaEnvelope, encodeMediaEnvelope, MediaEnvelopeSchema } from '../protocol/audioV2'
import { RecordingProvider, type RecordingContextType, useRecording } from './RecordingContext'
import LiveRecord from '../pages/LiveRecord'
import SimplifiedControls from '../components/audio/SimplifiedControls'

vi.mock('../components/audio/AudioVisualizer', () => ({ default: () => null }))
vi.mock('../components/audio/WakeFeedback', () => ({ default: () => null }))
vi.mock('./AuthContext', () => ({ useAuth: () => ({ user: { id: 'user-1' } }) }))
vi.mock('../services/api', () => ({ BACKEND_URL: '', api: { post: vi.fn(async () => ({ data: { upload_id: 'timing-test' } })) } }))
vi.mock('../hooks/useWakeFeedback', () => ({ setActiveWakeClientId: vi.fn() }))

class FakeWebSocket {
  static OPEN = 1
  static instances: FakeWebSocket[] = []
  static closeOnStart = false
  static rejectFirstMedia = false
  static transcriptAfterStart: string | null = null
  static deferStop = false
  pendingStopAcknowledgement: (() => void) | null = null
  private rejectedMedia = false
  readyState = FakeWebSocket.OPEN
  binaryType: BinaryType = 'blob'
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(public url: string, public protocols?: string | string[]) {
    FakeWebSocket.instances.push(this)
    queueMicrotask(() => this.onopen?.())
  }

  send(value: unknown) {
    this.sent.push(value)
    if (typeof value !== 'string') {
      if (FakeWebSocket.rejectFirstMedia && !this.rejectedMedia) {
        this.rejectedMedia = true
        const envelope = {
          event_id: { value: crypto.randomUUID() },
          sent_at: new Date().toISOString(),
          error: { code: 'PROTOCOL_ERROR_CODE_INVALID_MEDIA', detail: 'browser packet rejected' },
        }
        queueMicrotask(() => {
          this.onmessage?.({ data: JSON.stringify(envelope) } as MessageEvent)
          this.readyState = 3
          this.onclose?.({ code: 1008, reason: 'invalid audio-v2 message' } as CloseEvent)
        })
      }
      return
    }
    const control = JSON.parse(value)
    const envelope = {
      event_id: { value: crypto.randomUUID() },
      sent_at: new Date().toISOString(),
    }
    if (control.hello) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        hello: { client_id: { value: 'client-1' }, connection_id: { value: 'connection-1' } },
      }) } as MessageEvent))
    } else if (control.start_capture) {
      if (FakeWebSocket.closeOnStart) {
        queueMicrotask(() => this.onclose?.({ code: 1011, reason: 'start rejected' } as CloseEvent))
        return
      }
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        capture_started: {
          binding: {
            capture_session_id: { value: 'capture-1' },
            voice_session_id: { value: control.start_capture.capabilities ? 'voice-1' : '' },
            capture_epoch: control.start_capture.capture_epoch,
          },
          audio_spec: control.start_capture.audio_spec,
        },
      }) } as MessageEvent))
      if (FakeWebSocket.transcriptAfterStart) {
        queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
          ...envelope,
          transcript_update: {
            binding: {
              capture_session_id: { value: 'capture-1' },
              voice_session_id: { value: control.start_capture.capabilities ? 'voice-1' : '' },
              capture_epoch: control.start_capture.capture_epoch,
            },
            text: FakeWebSocket.transcriptAfterStart,
            is_final: false,
            confidence: 0.9,
          },
        }) } as MessageEvent))
      }
    } else if (control.conversation_command) {
      const command = control.conversation_command
      const started = command.action === 'CONVERSATION_ACTION_START'
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        conversation_state: {
          binding: command.binding,
          interaction_id: command.action === 'CONVERSATION_ACTION_SNAPSHOT' ? '' : 'interaction-1',
          revision: started ? '1' : '2',
          phase: started ? 'CONVERSATION_PHASE_LISTENING' : 'CONVERSATION_PHASE_ENDED',
          engine: 'SPEECH_ENGINE_MODULAR',
        },
      }) } as MessageEvent))
    } else if (control.stop_capture) {
      const acknowledge = () => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        capture_stopped: { binding: control.stop_capture.binding },
      }) } as MessageEvent)
      if (FakeWebSocket.deferStop) this.pendingStopAcknowledgement = acknowledge
      else queueMicrotask(acknowledge)
    }
  }
  close() { this.readyState = 3 }
}

class FakeAudioContext {
  sampleRate = 16000
  currentTime = 1
  state: AudioContextState = 'running'
  addEventListener = vi.fn()
  removeEventListener = vi.fn()
  destination = {} as AudioDestinationNode
  audioWorklet = { addModule: vi.fn(async () => undefined) }
  processor: FakeAudioWorklet | null = null

  createAnalyser() {
    return {
      fftSize: 0,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getFloatTimeDomainData: vi.fn(),
    } as unknown as AnalyserNode
  }
  createMediaStreamSource() {
    return { connect: vi.fn(), disconnect: vi.fn() } as unknown as MediaStreamAudioSourceNode
  }
  resume = vi.fn(async () => undefined)
  close = vi.fn(async () => undefined)
}

class FakeAudioWorklet {
  static playback: FakeAudioWorklet | null = null
  port = { onmessage: null as ((event: { data: any }) => void) | null, postMessage: vi.fn(), close: vi.fn() }
  connect = vi.fn()
  disconnect = vi.fn()
  constructor(_context: unknown, name: string) {
    if (name === 'chronicle-capture') thisState.processor = this
    if (name === 'chronicle-playback') FakeAudioWorklet.playback = this
  }
}

let thisState: FakeAudioContext
let recording: RecordingContextType | null = null
let microphoneTrackStop: ReturnType<typeof vi.fn>

function Harness({ controls = false, live = false }: { controls?: boolean, live?: boolean }) {
  const value = useRecording()
  useEffect(() => { recording = value }, [value])
  return live ? <LiveRecord /> : controls ? <SimplifiedControls recording={value} /> : null
}

describe('RecordingProvider audio V2 interface', () => {
  afterEach(async () => {
    cleanup()
    if (vi.isFakeTimers()) await vi.runOnlyPendingTimersAsync()
    else await new Promise(resolve => setTimeout(resolve, 0))
    vi.useRealTimers()
  })
  beforeEach(() => {
    vi.mocked(api.post).mockClear()
    vi.mocked(api.post).mockResolvedValue({ data: { upload_id: 'timing-test' } })
    recording = null
    FakeWebSocket.instances = []
    FakeWebSocket.closeOnStart = false
    FakeWebSocket.rejectFirstMedia = false
    FakeWebSocket.transcriptAfterStart = null
    FakeWebSocket.deferStop = false
    localStorage.clear()
    localStorage.setItem('root_token', 'test-token')
    thisState = new FakeAudioContext()
    microphoneTrackStop = vi.fn()
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorklet)
    vi.stubGlobal('isSecureContext', true)
    vi.stubGlobal('AudioDecoder', class {
      static async isConfigSupported() { return { supported: true } }
      configure() {}
      close() {}
    })
    vi.stubGlobal('AudioData', class {
      constructor(_options: unknown) {}
      close() {}
    })
    vi.stubGlobal('AudioEncoder', class {
      static async isConfigSupported() { return { supported: true } }
      private output: (chunk: unknown) => void
      constructor(options: { output: (chunk: unknown) => void }) { this.output = options.output }
      configure() {}
      encode() {
        this.output({
          byteLength: 4,
          copyTo: (target: Uint8Array) => target.set([1, 2, 3, 4]),
        })
      }
      async flush() {}
      close() {}
    })
    vi.stubGlobal('AudioContext', function AudioContext() { return thisState })
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        enumerateDevices: vi.fn(async () => []),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        getUserMedia: vi.fn(async () => ({
          getTracks: () => [{ stop: microphoneTrackStop }],
          getAudioTracks: () => [{}],
        })),
      },
    })
  })

  it('shows concurrent preparation and rendered playback, fences cancellation, and ignores slow diagnostic uploads', async () => {
    vi.mocked(api.post).mockImplementation(() => new Promise(() => {}))
    vi.stubGlobal('EncodedAudioChunk', class { constructor(_value: unknown) {} })
    vi.stubGlobal('AudioDecoder', class {
      static async isConfigSupported() { return { supported: true } }
      state = 'configured'
      constructor(private callbacks: { output: (data: unknown) => void }) {}
      configure() {}
      decode() { this.callbacks.output({ sampleRate: 24000, numberOfChannels: 1, numberOfFrames: 480, timestamp: 0,
        copyTo: (out: Float32Array) => out.fill(0.2), close() {} }) }
      close() { this.state = 'closed' }
    })
    render(<RecordingProvider><Harness live /></RecordingProvider>)
    act(() => recording!.setHeadphonesConfirmed(true))
    await act(async () => { await recording!.startConversation() })
    const socket = FakeWebSocket.instances[0]
    const binding = JSON.parse(socket.sent.find((s): s is string => typeof s === 'string' && Boolean(JSON.parse(s).voice_ready))!).voice_ready.binding
    const receive = (event: object) => act(() => socket.onmessage?.({ data: JSON.stringify({ event_id: { value: crypto.randomUUID() }, sent_at: new Date().toISOString(), ...event }) } as MessageEvent))
    const processing = (sequence: number, fields = {}, generation = 1) => receive({ voice_processing_update: {
      binding, interaction_id: 'interaction-1', generation: String(generation), effect_id: `effect-${generation}`, state_revision: generation === 1 ? '2' : '4', sequence: String(sequence), ...fields,
    } })
    receive({ conversation_state: { binding, interaction_id: 'interaction-1', revision: '2', response_generation: '1', response_effect_id: 'effect-1', phase: 'CONVERSATION_PHASE_THINKING' } })
    processing(1, { transcribing: true })
    expect(screen.getByText('Transcribing')).toBeTruthy()
    processing(2, { generating_text: true, synthesizing_speech: true })
    expect(screen.getByText('Generating reply')).toBeTruthy()
    expect(screen.getByText('Generating speech (TTS)')).toBeTruthy()
    expect(screen.queryByText('Transcribing')).toBeNull()
    receive({ playback_offer: { binding, response_id: { value: 'reply-1' }, generation: '1', incremental: true,
      audio_spec: { codec: 'AUDIO_CODEC_OPUS', sample_rate_hz: 24000, channel_count: 1, frame_duration: '0.020s' } } })
    expect(screen.getByText('Preparing audio')).toBeTruthy()
    expect(screen.queryByText('Playing')).toBeNull()
    for (let sequence = 0; sequence < 8; sequence++) act(() => socket.onmessage?.({ data: encodeMediaEnvelope(create(MediaEnvelopeSchema, { media: { case: 'playback', value: { responseId: { value: 'reply-1' }, generation: 1n, sequence: BigInt(sequence), opusPayload: new Uint8Array([1]) } } })).buffer } as MessageEvent))
    const progress = (fields: object) => act(() => FakeAudioWorklet.playback!.port.onmessage?.({ data: { token: 'reply-1:1', state: 'progress', rendered: 240, buffered: 3000, ...fields } }))
    progress({ state: 'started' })
    expect(screen.getByText('Playing')).toBeTruthy()
    expect(screen.getByText('Generating speech (TTS)')).toBeTruthy()
    processing(3, { finished: true })
    expect(screen.queryByText('Generating speech (TTS)')).toBeNull()
    expect(screen.getByText('Playing')).toBeTruthy()
    progress({ buffered: 0, sampleRate: 48000, underrunRunSamples: 9600 })
    expect(screen.getByText('Buffering')).toBeTruthy()
    progress({ rendered: 480, underrunRunSamples: 0 })
    expect(screen.getByText('Playing')).toBeTruthy()
    processing(4, { generating_text: true }) // terminal generation cannot reopen
    expect(screen.queryByText('Generating reply')).toBeNull()
    receive({ conversation_state: { binding, interaction_id: 'interaction-1', revision: '3', response_generation: '2', response_effect_id: 'effect-1', phase: 'CONVERSATION_PHASE_LISTENING' } })
    expect(screen.queryByRole('status', { name: 'Conversation activity' })).toBeNull()
    progress({ rendered: 600 }) // cancelled renderer cannot relight status
    progress({ state: 'cancelled', rendered: 480, buffered: 0 })
    processing(5, { generating_text: true })
    expect(screen.queryByRole('status', { name: 'Conversation activity' })).toBeNull()
    receive({ conversation_state: { binding, interaction_id: 'interaction-1', revision: '4', response_generation: '2', response_effect_id: 'effect-2', phase: 'CONVERSATION_PHASE_THINKING' } })
    processing(1, { transcribing: true }, 2)
    expect(screen.getByText('Transcribing')).toBeTruthy()
    act(() => recording!.endConversation())
    processing(2, { generating_text: true }, 2)
    expect(screen.queryByRole('status', { name: 'Conversation activity' })).toBeNull()
    await waitFor(() => expect(api.post).toHaveBeenCalled())
    expect(recording!.isRecording).toBe(true)
    expect(microphoneTrackStop).not.toHaveBeenCalled()
    expect(socket.readyState).toBe(FakeWebSocket.OPEN)
    await act(async () => recording!.stopRecording())
    await waitFor(() => expect(recording!.isRecording).toBe(false))
  })

  it('restores the last selected microphone and clears it with System Default', () => {
    localStorage.setItem('root_microphoneDeviceId', 'usb-microphone')

    render(<RecordingProvider><Harness /></RecordingProvider>)

    expect(recording!.selectedDeviceId).toBe('usb-microphone')

    act(() => recording!.setSelectedDeviceId('desk-microphone'))

    expect(localStorage.getItem('root_microphoneDeviceId')).toBe('desk-microphone')

    act(() => recording!.setSelectedDeviceId(null))

    expect(recording!.selectedDeviceId).toBeNull()
    expect(localStorage.getItem('root_microphoneDeviceId')).toBeNull()
  })

  it('encodes microphone input into atomic raw-Opus V2 packets', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })

    const input = new Float32Array(512)
    await act(async () => {
      thisState.processor!.port.onmessage!({ data: { samples: input, audioFrame: 128, sampleRate: 16000 } })
    })

    const socket = FakeWebSocket.instances[0]
    expect(thisState.audioWorklet.addModule).toHaveBeenCalledOnce()
    const headers = socket.sent
      .filter((value): value is string => typeof value === 'string')
      .map(value => JSON.parse(value))
    expect(headers.some(value => value.hello)).toBe(true)
    expect(headers.some(value => value.start_capture)).toBe(true)
    const startPayload = socket.sent.find(
      (value): value is string => typeof value === 'string' && Boolean(JSON.parse(value).start_capture),
    )!
    const startControl = fromJsonString(ClientControlSchema, startPayload)
    expect(startControl.event.case).toBe('startCapture')
    if (startControl.event.case !== 'startCapture') throw new Error('expected startCapture')
    expect(startControl.event.value.captureEpoch).toBe(0n)
    const packets = socket.sent.filter((value): value is Uint8Array => value instanceof Uint8Array)
    expect(packets).toHaveLength(1)
    const envelope = decodeMediaEnvelope(packets[0]!)
    expect(envelope.media.case).toBe('capture')
    if (envelope.media.case !== 'capture') throw new Error('expected capture packet')
    expect(envelope.media.value.opusPayload).toEqual(new Uint8Array([1, 2, 3, 4]))
    expect(socket.protocols).toBe('chronicle.audio.v2')
    expect(socket.readyState).toBe(FakeWebSocket.OPEN)
  })

  it('fails instead of hanging when the socket closes before capture-started', async () => {
    FakeWebSocket.closeOnStart = true
    render(<RecordingProvider><Harness /></RecordingProvider>)

    let outcome: 'settled' | 'timeout'
    await act(async () => {
      outcome = await Promise.race([
        recording!.startRecording().then(() => 'settled' as const),
        new Promise<'timeout'>(resolve => setTimeout(() => resolve('timeout'), 100)),
      ])
    })

    expect(outcome!).toBe('settled')
    expect(recording!.currentStep).toBe('error')
    expect(recording!.error).toContain('WebSocket closed')
    expect(recording!.error).toContain('before captureStarted')
  })

  it('surfaces an asynchronous media rejection after capture started', async () => {
    FakeWebSocket.rejectFirstMedia = true
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })

    await act(async () => {
      thisState.processor!.port.onmessage!({ data: { samples: new Float32Array(512), audioFrame: 128, sampleRate: 16000 } })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(recording!.isRecording).toBe(false)
    expect(recording!.currentStep).toBe('error')
    expect(recording!.error).toContain('browser packet rejected')
  })

  it('retires the socket as well as capture when local encoding throws', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })
    const socket = FakeWebSocket.instances[0]
    vi.stubGlobal('AudioData', class {
      constructor() { throw new Error('local encoder input failed') }
    })
    await act(async () => {
      thisState.processor!.port.onmessage!({ data: { samples: new Float32Array(320), audioFrame: 128, sampleRate: 16000 } })
    })
    expect(recording!.isRecording).toBe(false)
    expect(recording!.error).toBe('local encoder input failed')
    expect(socket.readyState).toBe(3)
    expect(microphoneTrackStop).toHaveBeenCalledOnce()
  })

  it('renders typed Audio V2 transcript updates in recording state', async () => {
    FakeWebSocket.transcriptAfterStart = 'typed live transcript'
    render(<RecordingProvider><Harness /></RecordingProvider>)

    await act(async () => {
      await recording!.startRecording()
      await Promise.resolve()
    })

    expect(recording!.liveTranscript).toBe('typed live transcript')
  })

  it('stops capture through the bound V2 control before closing transport', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })

    const socket = FakeWebSocket.instances[0]
    vi.useFakeTimers()
    act(() => recording!.stopRecording())

    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    const headers = socket.sent
      .filter((value): value is string => typeof value === 'string')
      .map(value => JSON.parse(value))
    expect(headers.some(value => value.stop_capture)).toBe(true)
    expect(microphoneTrackStop).toHaveBeenCalledOnce()
    expect(thisState.close).toHaveBeenCalledOnce()
    expect(socket.readyState).toBe(3)
    vi.useRealTimers()
  })
  it('keeps finishing controls and unload protection until the real stop acknowledgement', async () => {
    FakeWebSocket.deferStop = true
    render(<RecordingProvider><Harness controls /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })
    const socket = FakeWebSocket.instances[0]
    await act(async () => { recording!.stopRecording() })

    expect(microphoneTrackStop).toHaveBeenCalledOnce()
    expect(thisState.close).toHaveBeenCalledOnce()
    expect(recording!.currentStep).toBe('stopping')
    expect(recording!.isRecording).toBe(true)
    expect((screen.getByRole('button', { name: 'Finishing recording…' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('button', { name: 'Start recording' })).toBeNull()
    const unloading = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(unloading)
    expect(unloading.defaultPrevented).toBe(true)

    await act(async () => {
      recording!.stopRecording()
      await recording!.startRecording()
      await recording!.startConversation()
    })
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(socket.sent.filter(value => typeof value === 'string' && JSON.parse(value).stop_capture)).toHaveLength(1)
    expect(socket.readyState).toBe(FakeWebSocket.OPEN)

    await act(async () => { socket.pendingStopAcknowledgement!() })
    expect(recording!.currentStep).toBe('idle')
    expect(recording!.isRecording).toBe(false)
    expect(socket.readyState).toBe(3)
    expect((screen.getByRole('button', { name: 'Start recording' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('settles a rejected stop as an error instead of claiming audio is saved', async () => {
    FakeWebSocket.deferStop = true
    render(<RecordingProvider><Harness controls /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })
    const socket = FakeWebSocket.instances[0]
    await act(async () => { recording!.stopRecording() })
    await act(async () => { socket.onclose?.({ code: 1011, reason: 'stop failed' } as CloseEvent) })
    expect(recording!.currentStep).toBe('error')
    expect(recording!.isRecording).toBe(false)
    expect(recording!.error).toContain('stop failed')
    expect(screen.queryByRole('button', { name: 'Finishing recording…' })).toBeNull()
    expect((screen.getByRole('button', { name: 'Start recording' }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('engages and ends on the same capture and keeps sending microphone packets', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    act(() => recording!.setHeadphonesConfirmed(true))
    await act(async () => { await recording!.startConversation('space-private') })
    const socket = FakeWebSocket.instances[0]
    expect(recording!.isRecording).toBe(true)
    expect(recording!.conversationReady).toBe(true)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
    const controls = () => socket.sent.filter((v): v is string => typeof v === 'string').map(v => JSON.parse(v))
    const start = controls().find(v => v.start_capture).start_capture
    expect(start.processing_profile).toBe('PROCESSING_PROFILE_DUPLEX_ISOLATED')
    expect(start.capabilities.incremental_playback).toBe(true)
    expect(start.capabilities.input_route).toBe('INPUT_ROUTE_UNKNOWN')
    expect(start.memory_space_id.value).toBe('space-private')
    expect(controls().some(v => v.voice_ready)).toBe(true)
    await act(async () => {
      thisState.processor!.port.onmessage!({ data: { samples: new Float32Array(320), audioFrame: 128, sampleRate: 16000 } })
      recording!.endConversation()
      await Promise.resolve()
      thisState.processor!.port.onmessage!({ data: { samples: new Float32Array(320), audioFrame: 128, sampleRate: 16000 } })
    })
    expect(recording!.isRecording).toBe(true)
    expect(microphoneTrackStop).not.toHaveBeenCalled()
    expect(controls().filter(v => v.start_capture)).toHaveLength(1)
    expect(controls().some(v => v.stop_capture)).toBe(false)
    const frames = socket.sent.filter((v): v is Uint8Array => v instanceof Uint8Array).map(decodeMediaEnvelope)
    expect(frames).toHaveLength(2)
    expect(frames.map(v => v.media.case === 'capture' && v.media.value.sequence)).toEqual([0n, 1n])
    await act(async () => { recording!.stopRecording(); await Promise.resolve(); await Promise.resolve() })
    expect(controls().some(v => v.stop_capture)).toBe(true)
  })

  it('does not enable playback or acquire a second microphone without headphone confirmation', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startConversation() })
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    expect(recording!.conversationError).toContain('Confirm headphones')
  })

  it('uploads one metadata report after the capture stop acknowledgement, even if upload fails', async () => {
    vi.mocked(api.post).mockRejectedValue(new Error('diagnostic service offline'))
    FakeWebSocket.deferStop = true
    render(<RecordingProvider><Harness /></RecordingProvider>)
    act(() => recording!.setHeadphonesConfirmed(true))
    await act(async () => { await recording!.startConversation() })
    await act(async () => {
      thisState.processor!.port.onmessage!({ data: { samples: new Float32Array(320), audioFrame: 16000, sampleRate: 16000 } })
      recording!.stopRecording(); recording!.stopRecording()
    })
    expect(api.post).not.toHaveBeenCalled()
    expect(recording!.isRecording).toBe(true)
    await act(async () => { FakeWebSocket.instances[0].pendingStopAcknowledgement?.() })
    await waitFor(() => expect(api.post).toHaveBeenCalledOnce())
    const [url, body, options] = vi.mocked(api.post).mock.calls[0]
    expect(url).toBe('/api/client-diagnostics')
    expect(options?.timeout).toBe(5000)
    const report = JSON.parse(body as string)
    expect(report.checkpoint).toBe('capture_closed')
    expect(report.binding.captureSessionId).toBe('capture-1')
    expect(report.points.find((point: any) => point.stage === 'capture_callback').fields.audioFrame).toBe(16000)
    expect(body).not.toContain('test-token')
    expect(report.lastPoint.fields.encodedFrames).toBe(1)
    expect(recording!.isRecording).toBe(false)
    expect(recording!.error).toBeNull()
  })

  it('autosaves each response terminal once without stopping capture, then saves capture stop separately', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    act(() => recording!.setHeadphonesConfirmed(true))
    await act(async () => { await recording!.startConversation() })
    const socket = FakeWebSocket.instances[0]
    const controls = socket.sent.filter((v): v is string => typeof v === 'string').map(v => JSON.parse(v))
    const binding = controls.find(c => c.voice_ready).voice_ready.binding
    await act(async () => {
      socket.onmessage?.({ data: JSON.stringify({ event_id: { value: 'offer-event' }, sent_at: new Date().toISOString(),
        playback_offer: { binding, response_id: { value: 'response-timing' }, generation: '1', incremental: true,
          audio_spec: { codec: 'AUDIO_CODEC_OPUS', sample_rate_hz: 24000, channel_count: 1, frame_duration: '0.020s' } },
      }) } as MessageEvent)
      recording!.endConversation()
      const progress = { token: 'response-timing:1', state: 'cancelled', rendered: 0, buffered: 0,
        audioFrame: 88200, sampleRate: 44100, underrunSamples: 0, maxUnderrunSamples: 0 }
      FakeAudioWorklet.playback!.port.onmessage?.({ data: progress })
      FakeAudioWorklet.playback!.port.onmessage?.({ data: progress })
    })
    await waitFor(() => expect(api.post).toHaveBeenCalledOnce())
    const first = JSON.parse(vi.mocked(api.post).mock.calls[0][1] as string)
    expect(first.checkpoint).toBe('response_cancelled:response-timing:1')
    expect(first.responseAnchors['response-timing:1'].render_progress_cancelled.fields.audioFrame).toBe(88200)
    expect(recording!.isRecording).toBe(true)
    expect(microphoneTrackStop).not.toHaveBeenCalled()
    await act(async () => { recording!.stopRecording() })
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
    expect(JSON.parse(vi.mocked(api.post).mock.calls[1][1] as string).checkpoint).toBe('capture_closed')
  })

})
