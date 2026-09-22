// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type RecordingContextType, useRecording } from '../contexts/RecordingContext'
import LiveRecord from './LiveRecord'
import { create } from '@bufbuild/protobuf'
import { ConversationPhase, ConversationStateSchema, SpeechEngine, VoiceTaskStatus } from '../protocol/audioV2'

vi.mock('../contexts/RecordingContext', () => ({
  useRecording: vi.fn(),
  isLoopbackDevice: (label: string) => /monitor of/i.test(label),
  isMacOS: false,
}))
vi.mock('../components/audio/SimplifiedControls', () => ({ default: () => null }))
vi.mock('../components/audio/StatusDisplay', () => ({ default: () => null }))
vi.mock('../components/audio/AudioVisualizer', () => ({ default: () => null }))
vi.mock('../components/audio/SimpleDebugPanel', () => ({ default: () => null }))
vi.mock('../components/audio/WakeFeedback', () => ({ default: () => null }))

const requestDeviceAccess = vi.fn(async () => undefined)
const setSelectedDeviceId = vi.fn()

function recording(overrides: Partial<RecordingContextType> = {}): RecordingContextType {
  return {
    isRecording: false,
    audioSource: 'mic',
    setAudioSource: vi.fn(),
    availableDevices: [],
    selectedDeviceId: null,
    setSelectedDeviceId,
    monitorDeviceId: null,
    setMonitorDeviceId: vi.fn(),
    requestDeviceAccess,
    likelyLacksDisplayAudio: false,
    systemAudioLabel: null,
    systemAudioStatus: 'unknown',
    liveTranscript: '',
    currentStep: 'idle',
    headphonesConfirmed: false,
    setHeadphonesConfirmed: vi.fn(),
    voiceEngine: SpeechEngine.MODULAR,
    setVoiceEngine: vi.fn(),
    conversationState: null,
    conversationStarting: false,
    conversationReady: false,
    conversationUnavailableReason: null,
    conversationError: null,
    startConversation: vi.fn(async () => {}),
    endConversation: vi.fn(),
    cancelVoiceTask: vi.fn(),
    analyser: null,
    ...overrides,
  } as RecordingContextType
}

describe('LiveRecord microphone setup', () => {
  beforeEach(() => {
    vi.mocked(useRecording).mockReturnValue(recording())
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('offers microphone selection before a recording has started', () => {
    render(<LiveRecord />)

    expect(screen.getByRole('button', { name: 'Choose microphone…' })).toBeVisible()
    expect(screen.getByText(/Recording will not start/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Choose microphone…' }))
    expect(requestDeviceAccess).toHaveBeenCalledOnce()
  })

  it('shows labeled devices and applies the selection before recording', () => {
    vi.mocked(useRecording).mockReturnValue(recording({
      availableDevices: [
        { deviceId: 'built-in', kind: 'audioinput', label: 'Built-in Microphone', groupId: '', toJSON: vi.fn() },
        { deviceId: 'usb', kind: 'audioinput', label: 'USB Podcast Mic', groupId: '', toJSON: vi.fn() },
      ],
    }))

    render(<LiveRecord />)

    const picker = screen.getByRole('combobox', { name: /Microphone/ })
    expect(picker).toBeEnabled()
    expect(screen.getByRole('option', { name: 'USB Podcast Mic' })).toBeVisible()

    fireEvent.change(picker, { target: { value: 'usb' } })
    expect(setSelectedDeviceId).toHaveBeenCalledWith('usb')
  })

  it('does not show a microphone picker for tab-only capture', () => {
    vi.mocked(useRecording).mockReturnValue(recording({ audioSource: 'tab' }))

    render(<LiveRecord />)

    expect(screen.queryByRole('button', { name: 'Choose microphone…' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /Microphone/ })).not.toBeInTheDocument()
  })
  it('requires headphones before starting and passes the current recording destination', () => {
    const value = recording()
    vi.mocked(useRecording).mockReturnValue(value)
    const rendered = render(<LiveRecord memorySpaceId="space-one" />)
    expect(screen.getByRole('button', { name: 'Start conversation' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /using headphones/ }))
    expect(value.setHeadphonesConfirmed).toHaveBeenCalledWith(true)
    vi.mocked(useRecording).mockReturnValue({ ...value, headphonesConfirmed: true })
    rendered.rerender(<LiveRecord memorySpaceId="space-one" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start conversation' }))
    expect(value.startConversation).toHaveBeenCalledWith('space-one')
  })

  it('separates End conversation and task cancellation from recording', () => {
    const value = recording({
      isRecording: true, currentStep: 'streaming', headphonesConfirmed: true, conversationReady: true,
      conversationState: create(ConversationStateSchema, { phase: ConversationPhase.SPEAKING, engine: SpeechEngine.MODULAR,
        tasks: [{ taskId: 'task-one', toolName: 'delegate_to_hermes', status: VoiceTaskStatus.RUNNING }],
      }),
    })
    vi.mocked(useRecording).mockReturnValue(value)
    render(<LiveRecord />)
    expect(screen.getByRole('combobox', { name: 'Conversation engine' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: /using headphones/ })).toBeDisabled()
    expect(screen.getByText('Responding')).toBeVisible()
    expect(screen.getByText('Hermes')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'End conversation' }))
    expect(value.endConversation).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: 'Stop task' }))
    expect(value.cancelVoiceTask).toHaveBeenCalledWith('task-one')
  })

})
