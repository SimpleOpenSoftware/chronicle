// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WebAudioV2Session } from './webAudioV2Session'
import { create } from '@bufbuild/protobuf'
import { CaptureCapabilitiesSchema } from './audioV2'

class Socket {
  static OPEN = 1
  static instances: Socket[] = []
  readyState = 1
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number, reason: string }) => void) | null = null
  constructor() { Socket.instances.push(this) }
  send(value: unknown) { this.sent.push(value) }
  close = vi.fn(() => { this.readyState = 3 })
}

function setup() {
  Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('AudioEncoder', class { configure() {}; close() {} })
  vi.stubGlobal('AudioData', class {})
  const client = vi.fn()
  const transcript = vi.fn()
  const fatal = vi.fn()
  const session = new WebAudioV2Session('ws://localhost/ws/audio', 'test-token', client, transcript, fatal)
  return { session, client, transcript, fatal }
}

afterEach(() => vi.unstubAllGlobals())

describe('browser audio session owner lifecycle', () => {
  it('settles an unopened connection when its owner disposes it', async () => {
    const { session, fatal } = setup()
    const connection = session.connect()
    const rejected = expect(connection).rejects.toThrow('disposed before socketOpen')
    session.dispose()
    await rejected
    expect(Socket.instances[0].close).toHaveBeenCalledOnce()
    expect(fatal).not.toHaveBeenCalled()
    session.dispose()
    expect(Socket.instances[0].close).toHaveBeenCalledOnce()
  })

  it('settles an opened connection awaiting hello on disposal and ignores late callbacks', async () => {
    const { session, client, transcript, fatal } = setup()
    const connection = session.connect()
    Socket.instances[0].onopen!()
    await Promise.resolve()
    const rejected = expect(connection).rejects.toThrow('disposed before hello')
    session.dispose()
    await rejected
    Socket.instances[0].onmessage!({ data: JSON.stringify({ hello: { client_id: { value: 'retired-client' } } }) })
    Socket.instances[0].onerror!()
    Socket.instances[0].onclose!({ code: 1006, reason: 'late disconnect' })
    expect(client).not.toHaveBeenCalled()
    expect(transcript).not.toHaveBeenCalled()
    expect(fatal).not.toHaveBeenCalled()
  })

  it('does not acquire a second socket when connect is invoked twice', async () => {
    const { session } = setup()
    const connection = session.connect()
    await expect(session.connect()).rejects.toThrow('cannot be connected twice')
    expect(Socket.instances).toHaveLength(1)
    const rejected = expect(connection).rejects.toThrow('disposed')
    session.dispose()
    await rejected
    await expect(session.connect()).rejects.toThrow('cannot be connected twice')
  })

  it('fences processing by binding, interaction, generation and sequence without per-packet UI churn', async () => {
    setup()
    const onProcessing = vi.fn()
    const renderer = { onProgress: () => {}, open: vi.fn(), append: vi.fn(), finish: vi.fn(), cancel: vi.fn(), close: vi.fn() }
    const session = new WebAudioV2Session('ws://localhost/ws/audio', 'test-token', vi.fn(), vi.fn(), vi.fn(), {
      capabilities: create(CaptureCapabilitiesSchema), renderer, onState: vi.fn(), onPlaybackError: vi.fn(), onProcessing,
    })
    const connected = session.connect(); const socket = Socket.instances[0]
    const receive = (message: object) => socket.onmessage!({ data: JSON.stringify({ event_id: { value: crypto.randomUUID() }, sent_at: new Date().toISOString(), ...message }) })
    socket.onopen!(); await Promise.resolve(); receive({ hello: { client_id: { value: 'client' } } }); await connected
    const started = session.start()
    const binding = { capture_session_id: { value: 'capture' }, voice_session_id: { value: 'voice' }, capture_epoch: '7' }
    receive({ capture_started: { binding } }); await started
    const state = (revision: number, generation: number, effect = '', phase = 'THINKING') => receive({ conversation_state: { binding, interaction_id: 'engaged', revision: String(revision), response_generation: String(generation), response_effect_id: effect, phase: `CONVERSATION_PHASE_${phase}` } })
    const update = (sequence: number, generation: number, effect: string, revision: number, extra = {}) => receive({ voice_processing_update: { binding, interaction_id: 'engaged', generation: String(generation), effect_id: effect, state_revision: String(revision), sequence: String(sequence), transcribing: true, ...extra } })
    state(1, 0, '', 'LISTENING')
    update(1, 1, 'effect-a', 2); expect(onProcessing).not.toHaveBeenCalled() // bounded pending before admission
    state(2, 1, 'effect-a'); expect(onProcessing).toHaveBeenCalledOnce()
    update(2, 1, 'effect-a', 2); expect(onProcessing).toHaveBeenCalledOnce() // only sequence changed
    update(1, 1, 'effect-a', 2, { generating_text: true }); expect(onProcessing).toHaveBeenCalledOnce()
    update(3, 1, 'effect-a', 2, { binding: { ...binding, capture_epoch: '6' } }); expect(onProcessing).toHaveBeenCalledOnce()
    update(3, 1, 'effect-a', 2, { interaction_id: 'retired' }); expect(onProcessing).toHaveBeenCalledOnce()
    update(1, 1, 'effect-b', 3, { transcribing: false, generating_text: true })
    expect(onProcessing).toHaveBeenCalledOnce() // same generation does not admit a new effect by arrival order
    state(3, 1, 'effect-b')
    expect(onProcessing).toHaveBeenLastCalledWith(expect.objectContaining({ generatingText: true }))
    state(4, 1, 'effect-b', 'LISTENING') // terminal processing publish was lost
    expect(onProcessing).toHaveBeenLastCalledWith(null)
    const closedCount = onProcessing.mock.calls.length
    update(9, 1, 'effect-b', 3); expect(onProcessing).toHaveBeenCalledTimes(closedCount)
    update(1, 2, 'effect-c', 5); expect(onProcessing).toHaveBeenCalledTimes(closedCount)
    state(4, 1, 'effect-b', 'LISTENING') // old state cannot erase pending newer effect
    state(5, 2, 'effect-c')
    expect(onProcessing).toHaveBeenLastCalledWith(expect.objectContaining({ transcribing: true }))
    receive({ cancel_playback: { binding, response_id: { value: 'old-response' }, generation: '3' } })
    expect(onProcessing).toHaveBeenLastCalledWith(null)
    update(1, 3, 'effect-d', 6)
    state(6, 3, 'effect-d') // replacement generation remains admissible after cancellation
    expect(onProcessing).toHaveBeenLastCalledWith(expect.objectContaining({ transcribing: true }))
    update(2, 3, 'effect-d', 6, { finished: true }); expect(onProcessing).toHaveBeenLastCalledWith(null)
    const count = onProcessing.mock.calls.length
    update(3, 3, 'effect-d', 6); expect(onProcessing).toHaveBeenCalledTimes(count)
    session.endConversation(); update(1, 4, 'effect-e', 7); expect(onProcessing).toHaveBeenCalledTimes(count)
    session.dispose()
  })

  it('fences late output after End until a new authoritative engagement is listening', async () => {
    setup()
    vi.stubGlobal('AudioDecoder', class { configure() {}; close() {} })
    const renderer = { onProgress: () => {}, open: vi.fn(), append: vi.fn(), finish: vi.fn(), cancel: vi.fn(), close: vi.fn() }
    const onState = vi.fn()
    const fatal = vi.fn()
    const conversationError = vi.fn()
    const session = new WebAudioV2Session('ws://localhost/ws/audio', 'test-token', vi.fn(), vi.fn(), fatal, {
      capabilities: create(CaptureCapabilitiesSchema), renderer, onState, onPlaybackError: conversationError,
    })
    const connected = session.connect()
    const socket = Socket.instances[0]
    const receive = (message: object) => socket.onmessage!({ data: JSON.stringify({ event_id: { value: crypto.randomUUID() }, sent_at: new Date().toISOString(), ...message }) })
    socket.onopen!()
    await Promise.resolve()
    receive({ hello: { client_id: { value: 'client' } } })
    await connected
    const started = session.start()
    const binding = { capture_session_id: { value: 'capture' }, voice_session_id: { value: 'voice' }, capture_epoch: '7' }
    receive({ capture_started: { binding } })
    await started
    const state = (interaction_id: string, revision: number, phase: string, bound = binding) => receive({ conversation_state: { binding: bound, interaction_id, revision: String(revision), phase: `CONVERSATION_PHASE_${phase}` } })
    const offer = (generation: number) => receive({ playback_offer: {
      binding, response_id: { value: `response-${generation}` }, generation: String(generation), incremental: true,
      audio_spec: { codec: 'AUDIO_CODEC_OPUS', sample_rate_hz: 24000, channel_count: 1, frame_duration: '0.020s' },
    } })
    state('engagement-1', 1, 'LISTENING')
    offer(1)
    expect(renderer.open).toHaveBeenCalledOnce()
    session.endConversation()
    const controls = () => socket.sent.filter((value): value is string => typeof value === 'string').map(value => JSON.parse(value))
    const end = controls().filter(value => value.conversation_command).slice(-1)[0]
    expect(end.conversation_command.interaction_id).toBe('engagement-1')
    ;(renderer.onProgress as (event: object) => void)({ token: 'response-1:1', state: 'cancelled', rendered: 0, buffered: 0 })
    const ack = controls().filter(value => value.playback_acknowledgement).slice(-1)[0]
    receive({ error: { code: 'PROTOCOL_ERROR_CODE_INVALID_TRANSITION', rejected_event_id: ack.event_id, detail: 'response already cancelled' } })
    expect(fatal).not.toHaveBeenCalled()
    expect(socket.readyState).toBe(Socket.OPEN)
    offer(2)
    state('engagement-1', 2, 'LISTENING')
    offer(3)
    expect(renderer.open).toHaveBeenCalledOnce()
    expect(renderer.cancel).toHaveBeenCalledOnce()
    state('engagement-1', 3, 'ENDED')
    session.cancelTask('task-after-end')
    const cancelTask = controls().filter(value => value.conversation_command).slice(-1)[0]
    expect(cancelTask.conversation_command.interaction_id).toBe('engagement-1')
    state('engagement-1', 2, 'LISTENING') // old revision cannot rearm output
    offer(4)
    session.startConversation()
    const start = controls().filter(value => value.conversation_command).slice(-1)[0]
    expect(start.conversation_command.interaction_id).toBeUndefined()
    receive({ error: { code: 'PROTOCOL_ERROR_CODE_INVALID_TRANSITION', rejected_event_id: start.event_id, detail: 'voice engine unavailable' } })
    expect(conversationError).toHaveBeenCalledOnce()
    expect(fatal).not.toHaveBeenCalled()
    expect(socket.readyState).toBe(Socket.OPEN)
    offer(5) // sending START alone does not rearm output
    state('engagement-2', 1, 'LISTENING', { ...binding, capture_epoch: '6' })
    offer(6) // stale binding cannot rearm output
    expect(renderer.open).toHaveBeenCalledOnce()
    state('engagement-2', 1, 'LISTENING')
    offer(7)
    expect(renderer.open).toHaveBeenCalledTimes(2)
    state('engagement-1', 4, 'LISTENING') // retired interaction cannot replace current state
    expect(onState.mock.calls[onState.mock.calls.length - 1]?.[0].interactionId).toBe('engagement-2')
    expect(Socket.instances).toHaveLength(1)
    expect(socket.readyState).toBe(Socket.OPEN)
    session.dispose()
  })
})
