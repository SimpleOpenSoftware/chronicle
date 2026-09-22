import { createContext, useContext, useState, useRef, useCallback, useEffect, useMemo, ReactNode } from 'react'
import { api, BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'
import { useAuth } from './AuthContext'
import { setActiveWakeClientId } from '../hooks/useWakeFeedback'
import { WebAudioV2Session } from '../protocol/webAudioV2Session'
import { create } from '@bufbuild/protobuf'
import { CaptureCapabilitiesSchema, ConversationPhase, DuplexMode, EffectStatusSchema, InputRoute, OutputRoute, SpeechEngine, type ConversationState } from '../protocol/audioV2'
import { checkOpusSupport, conversationSupportError, createCaptureWorklet, WorkletPlaybackRenderer } from '../protocol/browserAudio'
import { type PlaybackActivity } from '../protocol/incrementalAudioPlayer'
import { type VoiceProcessingUpdate } from '../protocol/audioV2'
import { VoiceTimingTrace } from '../protocol/voiceTimingTrace'

const log = import.meta.env.DEV ? console.log.bind(console) : () => {}

// Firefox currently ignores `audio: true` in getDisplayMedia — its share picker has
// no "Share audio" option (bugzilla #1541425).
// This is a HINT for UI copy only: never gate capture on it. We always try the picker
// and check whether an audio track actually came back, so the browser decides — a UA
// gate would lock out Chromium forks that sniff as Firefox, and would keep blocking
// Firefox on the day #1541425 ships.
export const likelyLacksDisplayAudio = /firefox/i.test(navigator.userAgent)

// macOS has no built-in loopback input at all: Linux gets PipeWire/PulseAudio
// "Monitor of …" devices for free, but on macOS the user must install a virtual
// audio driver (BlackHole, Loopback, Soundflower) before system audio is capturable.
export const isMacOS = /mac/i.test(navigator.userAgent)

// Inputs that carry system audio rather than a real microphone. Linux names them
// "Monitor of …"; macOS/Windows virtual drivers use their own product names.
export const isLoopbackDevice = (label: string) =>
  /monitor of|loopback|blackhole|soundflower|stereo mix|what ?u ?hear/i.test(label)

export type RecordingStep = 'idle' | 'mic' | 'display-audio' | 'websocket' | 'audio-start' | 'streaming' | 'stopping' | 'error'
export type AudioSource = 'mic' | 'meeting' | 'tab'

export interface DebugStats {
  chunksSent: number
  messagesReceived: number
  lastError: string | null
  lastErrorTime: Date | null
  sessionStartTime: Date | null
  connectionAttempts: number
}

export interface RecordingContextType {
  // Current state
  currentStep: RecordingStep
  isRecording: boolean
  recordingDuration: number
  error: string | null
  liveTranscript: string

  // Actions
  startRecording: (memorySpaceId?: string) => Promise<void>
  stopRecording: () => void
  audioSource: AudioSource
  setAudioSource: (source: AudioSource) => void

  // Conversation shares this recording owner; ending it never stops capture.
  headphonesConfirmed: boolean
  setHeadphonesConfirmed: (confirmed: boolean) => void
  voiceEngine: SpeechEngine
  setVoiceEngine: (engine: SpeechEngine) => void
  voiceProcessing: VoiceProcessingUpdate | null
  playbackActivity: PlaybackActivity
  conversationState: ConversationState | null
  conversationError: string | null
  conversationStarting: boolean
  conversationReady: boolean
  conversationUnavailableReason: string | null
  startConversation: (memorySpaceId?: string, threadId?: string) => Promise<void>
  endConversation: () => void
  cancelVoiceTask: (taskId: string) => void

  // Microphone selection
  availableDevices: MediaDeviceInfo[]
  selectedDeviceId: string | null
  setSelectedDeviceId: (id: string | null) => void

  // System-audio loopback device (Firefox meeting/tab mode)
  monitorDeviceId: string | null
  setMonitorDeviceId: (id: string | null) => void
  requestDeviceAccess: () => Promise<void>

  // What the system-audio capture is actually doing (meeting/tab mode)
  systemAudioLabel: string | null
  systemAudioStatus: 'unknown' | 'active' | 'silent'

  // For components
  analyser: AnalyserNode | null
  debugStats: DebugStats

  // Utilities
  formatDuration: (seconds: number) => string
  canAccessMicrophone: boolean
  likelyLacksDisplayAudio: boolean
}

const RecordingContext = createContext<RecordingContextType | undefined>(undefined)

export function RecordingProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()

  // Basic state
  const [currentStep, setCurrentStep] = useState<RecordingStep>('idle')
  const [isRecording, setIsRecording] = useState(false)
  const [recordingDuration, setRecordingDuration] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [liveTranscript, setLiveTranscript] = useState('')
  const [analyserState, setAnalyserState] = useState<AnalyserNode | null>(null)
  const [audioSource, setAudioSource] = useState<AudioSource>('mic')
  const [headphonesConfirmed, setHeadphonesConfirmed] = useState(false)
  const [voiceEngine, setVoiceEngine] = useState(SpeechEngine.MODULAR)
  const [voiceProcessing, setVoiceProcessing] = useState<VoiceProcessingUpdate | null>(null)
  const [playbackActivity, setPlaybackActivity] = useState<PlaybackActivity>('idle')
  const [conversationState, setConversationState] = useState<ConversationState | null>(null)
  const [conversationError, setConversationError] = useState<string | null>(null)
  const [conversationStarting, setConversationStarting] = useState(false)
  const [conversationReady, setConversationReady] = useState(false)
  const conversationUnavailableReason = conversationSupportError()

  // Microphone selection
  const [availableDevices, setAvailableDevices] = useState<MediaDeviceInfo[]>([])
  const [selectedDeviceId, setSelectedDeviceIdState] = useState<string | null>(
    () => localStorage.getItem(getStorageKey('microphoneDeviceId'))
  )
  const setSelectedDeviceId = useCallback((id: string | null) => {
    setSelectedDeviceIdState(id)
    if (id) {
      localStorage.setItem(getStorageKey('microphoneDeviceId'), id)
    } else {
      localStorage.removeItem(getStorageKey('microphoneDeviceId'))
    }
  }, [])
  // System-audio loopback device for Firefox meeting/tab mode ("Monitor of …").
  // Persisted: auto-detect can't know which sink the user actually listens through
  // (each output has its own monitor), so remember the last working choice.
  const [monitorDeviceId, setMonitorDeviceIdState] = useState<string | null>(
    () => localStorage.getItem(getStorageKey('monitorDeviceId'))
  )
  const setMonitorDeviceId = useCallback((id: string | null) => {
    setMonitorDeviceIdState(id)
    if (id) {
      localStorage.setItem(getStorageKey('monitorDeviceId'), id)
    } else {
      localStorage.removeItem(getStorageKey('monitorDeviceId'))
    }
  }, [])
  // Diagnostics for the system-audio capture: which device, and is it delivering signal
  const [systemAudioLabel, setSystemAudioLabel] = useState<string | null>(null)
  const [systemAudioStatus, setSystemAudioStatus] = useState<'unknown' | 'active' | 'silent'>('unknown')

  // Debug stats
  const [debugStats, setDebugStats] = useState<DebugStats>({
    chunksSent: 0,
    messagesReceived: 0,
    lastError: null,
    lastErrorTime: null,
    sessionStartTime: null,
    connectionAttempts: 0
  })

  // Refs for direct access
  const audioSessionRef = useRef<WebAudioV2Session | null>(null)
  const voiceTimingRef = useRef<VoiceTimingTrace | null>(null)
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const processorRef = useRef<AudioWorkletNode | null>(null)
  const displayStreamRef = useRef<MediaStream | null>(null)
  const durationIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const systemAudioWatchRef = useRef<ReturnType<typeof setInterval>>()
  const startingRef = useRef(false)
  const stoppingRef = useRef(false)
  const chunkCountRef = useRef(0)
  const audioProcessingStartedRef = useRef(false)

  // Check if we're on localhost or using HTTPS
  const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  const isHttps = window.location.protocol === 'https:'

  // DEVELOPMENT ONLY: Allow specific IP addresses (remove in production!)
  const devAllowedHosts = import.meta.env.MODE === 'development'
    ? ['192.168.1.100', '10.0.0.100'] // Add your Docker host IPs here
    : []
  const isDevelopmentHost = devAllowedHosts.includes(window.location.hostname)

  const canAccessMicrophone = isLocalhost || isHttps || isDevelopmentHost

  // Enumerate audio input devices
  const refreshDevices = useCallback(async () => {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices()
      const audioInputs = devices.filter(d => d.kind === 'audioinput')
      setAvailableDevices(audioInputs)
    } catch (e) {
      console.warn('Failed to enumerate audio devices:', e)
    }
  }, [])

  // Device labels are hidden until an audio permission is granted — run a
  // throwaway capture so "Monitor of …" entries become selectable up front.
  const requestDeviceAccess = useCallback(async () => {
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true })
      probe.getTracks().forEach(t => t.stop())
    } catch (e) {
      console.warn('Device access probe failed:', e)
    }
    await refreshDevices()
  }, [refreshDevices])

  // Initial device enumeration + listen for device changes
  useEffect(() => {
    refreshDevices()
    navigator.mediaDevices.addEventListener('devicechange', refreshDevices)
    return () => navigator.mediaDevices.removeEventListener('devicechange', refreshDevices)
  }, [refreshDevices])

  // Format duration helper
  const formatDuration = useCallback((seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }, [])

  const cleanupCapture = useCallback(() => {
    log('Cleaning up audio capture resources')
    // Stop audio processing
    audioProcessingStartedRef.current = false

    // Clean up media stream
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }

    // Clean up display stream (meeting mode)
    if (displayStreamRef.current) {
      displayStreamRef.current.getTracks().forEach(track => track.stop())
      displayStreamRef.current = null
    }

    processorRef.current?.port.close()
    processorRef.current?.disconnect()

    // Clean up audio context
    if (audioContextRef.current?.state !== 'closed') {
      audioContextRef.current?.close()
    }
    audioContextRef.current = null
    analyserRef.current = null
    setAnalyserState(null)
    processorRef.current = null

    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current)
      durationIntervalRef.current = undefined
    }

    if (systemAudioWatchRef.current) {
      clearInterval(systemAudioWatchRef.current)
      systemAudioWatchRef.current = undefined
    }
    setSystemAudioLabel(null)
    setSystemAudioStatus('unknown')

    // Reset counters
    chunkCountRef.current = 0
  }, [])

  const cleanupTransport = useCallback(() => {
    log('Cleaning up audio-v2 transport')
    setActiveWakeClientId(null)
    audioSessionRef.current?.dispose()
    audioSessionRef.current = null
    setVoiceProcessing(null)
    setPlaybackActivity('idle')
    setConversationReady(false)
    setConversationStarting(false)
  }, [])

  const cleanup = useCallback(() => {
    cleanupCapture()
    cleanupTransport()
  }, [cleanupCapture, cleanupTransport])

  const handleAudioSessionFailure = useCallback((failure: Error) => {
    audioProcessingStartedRef.current = false
    const session = audioSessionRef.current
    audioSessionRef.current = null
    session?.dispose()
    setActiveWakeClientId(null)
    setVoiceProcessing(null)
    setPlaybackActivity('idle')
    setConversationReady(false)
    setConversationStarting(false)
    setConversationState(null)
    setError(failure.message)
    setCurrentStep('error')
    setIsRecording(false)
    setDebugStats(prev => ({
      ...prev,
      lastError: failure.message,
      lastErrorTime: new Date(),
    }))
    cleanupCapture()
  }, [cleanupCapture])

  // Step 1: Get microphone access
  const getMicrophoneAccess = useCallback(async (): Promise<MediaStream> => {
    log('Step 1: Requesting microphone access')

    if (!canAccessMicrophone) {
      throw new Error('Microphone access requires HTTPS or localhost')
    }

    // In meeting mode, disable echo cancellation so speaker/tab audio
    // isn't subtracted from the mic signal
    const disableProcessing = audioSource === 'meeting'
    const audioConstraints: MediaTrackConstraints = {
      channelCount: 1,
      echoCancellation: !disableProcessing,
      noiseSuppression: !disableProcessing,
      autoGainControl: true,
    }
    if (selectedDeviceId) {
      audioConstraints.deviceId = { exact: selectedDeviceId }
    }

    const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints })

    mediaStreamRef.current = stream

    // Re-enumerate to get labels after permission grant
    refreshDevices()

    // Track when mic permission is revoked
    stream.getTracks().forEach(track => {
      track.onended = () => {
        log('Microphone track ended (permission revoked or device disconnected)')
        if (audioProcessingStartedRef.current || startingRef.current) {
          setError('Microphone disconnected or permission revoked')
          setCurrentStep('error')
          cleanup()
          setIsRecording(false)
        }
      }
    })

    log('Microphone access granted')
    return stream
  }, [canAccessMicrophone, selectedDeviceId, isRecording, cleanup, refreshDevices, audioSource])

  // Step 1b: Get display/tab audio (meeting mode only)
  // Returns null (rather than throwing) when the picker yields no audio, so the
  // caller can fall back to a loopback input instead of dead-ending the recording.
  const getDisplayAudio = useCallback(async (): Promise<MediaStream | null> => {
    log('Step 1b: Requesting display/tab audio')

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,   // Required for picker to show
        audio: true,   // Request audio track
      })
    } catch (e) {
      // Picker cancelled, or the browser exposes no display capture at all.
      log('getDisplayMedia unavailable or dismissed:', e)
      return null
    }

    // Stop video track — we only need audio. Chrome keeps audio alive.
    stream.getVideoTracks().forEach(t => t.stop())

    // Firefox lands here: it grants the share but silently drops `audio: true`.
    if (stream.getAudioTracks().length === 0) {
      stream.getTracks().forEach(t => t.stop())
      log('Share produced no audio track')
      return null
    }

    displayStreamRef.current = stream
    setSystemAudioLabel(stream.getAudioTracks()[0].label || 'Shared tab audio')

    // Handle user clicking "Stop sharing" in browser chrome
    stream.getAudioTracks()[0].onended = () => {
      log('Display audio track ended (user stopped sharing)')
      displayStreamRef.current = null
    }

    log('Display audio access granted')
    return stream
  }, [])

  // Step 1b (Firefox fallback): capture system audio from a PipeWire/PulseAudio
  // "Monitor of …" loopback input via a second getUserMedia call, since Firefox's
  // getDisplayMedia can't deliver audio. Mixed into the graph exactly like the
  // Chromium display stream. No auto-detect: only the OS knows which output sink
  // audio is routed to, so the user must pick the matching monitor explicitly.
  const getMonitorAudio = useCallback(async (): Promise<MediaStream> => {
    log('Step 1b (Firefox): Capturing system audio via monitor device')

    if (!monitorDeviceId) {
      // Reached only after the share picker already failed to produce audio.
      throw new Error(
        isMacOS
          ? 'No system audio captured. Share a browser tab (not a window or whole screen) with "Share tab audio" ' +
            'ticked — macOS blocks window and screen audio at the OS level. Firefox can\'t share tab audio at all ' +
            '(bugzilla #1541425), so there use a Chromium browser, or install a loopback driver such as BlackHole ' +
            'and pick it in the "System audio" dropdown.'
          : 'No system audio captured. Share a browser tab with "Share tab audio" ticked (window and screen shares ' +
            'never carry audio), or pick the "Monitor of …" entry matching the output you\'re listening through ' +
            'in the "System audio" dropdown.'
      )
    }

    const devices = await navigator.mediaDevices.enumerateDevices()
    const target = devices.find(d => d.kind === 'audioinput' && d.deviceId === monitorDeviceId)
    if (!target) {
      throw new Error(
        'The selected system-audio device is no longer available (output disconnected, or the browser reset ' +
        'device IDs — grant persistent microphone permission to prevent that). Re-select a loopback device ' +
        'in the System audio dropdown.'
      )
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: { exact: target.deviceId },
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    })

    displayStreamRef.current = stream
    setSystemAudioLabel(target.label)
    stream.getAudioTracks()[0].onended = () => {
      log('Monitor audio track ended')
      displayStreamRef.current = null
    }

    log('Monitor audio capture started:', target.label)
    return stream
  }, [monitorDeviceId])

  // Step 2: Connect and bind the generated audio V2 session.
  const connectAudioSession = useCallback(async (): Promise<WebAudioV2Session> => {
    const token = localStorage.getItem(getStorageKey('token'))
    if (!token) throw new Error('No authentication token found')
    const base = BACKEND_URL && BACKEND_URL.startsWith('http')
      ? new URL(BACKEND_URL)
      : new URL(BACKEND_URL || '/', window.location.origin)
    base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
    base.pathname = `${base.pathname.replace(/\/$/, '')}/ws/audio`
    base.search = ''
    const voiceEnabled = headphonesConfirmed && audioSource === 'mic'
    const context = audioContextRef.current!
    const settings = mediaStreamRef.current?.getAudioTracks()[0]?.getSettings?.()
    const renderer = voiceEnabled ? await WorkletPlaybackRenderer.create(context) : undefined
    const timing = voiceEnabled ? new VoiceTimingTrace(4096, () => performance.now(), body => {
      // Best-effort, authenticated metadata upload; never delays or fails capture.
      void api.post('/api/client-diagnostics', body, { timeout: 5000, headers: {
        'Content-Type': 'text/plain; charset=utf-8', 'X-Chronicle-Platform': 'web',
      } }).catch(() => undefined)
    }) : undefined
    voiceTimingRef.current = timing ?? null
    timing?.observeContext(context)
    const capabilities = voiceEnabled ? create(CaptureCapabilitiesSchema, {
      duplexMode: DuplexMode.ISOLATED,
      // Browsers do not reliably expose physical routes. Headphones are a user assertion.
      inputRoute: InputRoute.UNKNOWN,
      outputRoute: OutputRoute.HEADPHONES,
      nativeSampleRateHz: settings?.sampleRate ?? context.sampleRate,
      incrementalPlayback: true,
      acousticEchoCancellation: create(EffectStatusSchema, { requested: true, available: settings?.echoCancellation === true, enabled: settings?.echoCancellation === true }),
      noiseSuppression: create(EffectStatusSchema, { requested: true, available: settings?.noiseSuppression === true, enabled: settings?.noiseSuppression === true }),
    }) : undefined
    const session = new WebAudioV2Session(
      base.toString(),
      token,
      clientId => setActiveWakeClientId(clientId || null),
      text => setLiveTranscript(text),
      handleAudioSessionFailure,
      renderer && capabilities ? {
        renderer, capabilities, timing,
        onProcessing: setVoiceProcessing,
        onPlaybackActivity: setPlaybackActivity,
        onState: state => {
          setConversationState(state)
          if (state.engine !== SpeechEngine.UNSPECIFIED && state.phase !== ConversationPhase.ENDED && state.phase !== ConversationPhase.UNSPECIFIED) setVoiceEngine(state.engine)
          setConversationStarting(false)
          setConversationError(null)
        },
        onPlaybackError: failure => { setConversationStarting(false); setVoiceProcessing(null); setPlaybackActivity('idle'); setConversationError(failure.message) },
      } : undefined,
    )
    audioSessionRef.current = session
    await session.connect()
    setDebugStats(prev => ({ ...prev, connectionAttempts: prev.connectionAttempts + 1, sessionStartTime: new Date() }))
    return session
  }, [handleAudioSessionFailure, headphonesConfirmed, audioSource])

  // Step 3: Open a source-native capture under the V2 binding.
  const startAudioSession = useCallback(async (session: WebAudioV2Session, memorySpaceId?: string): Promise<void> => {
    await session.start(memorySpaceId)
    setConversationReady(headphonesConfirmed && audioSource === 'mic')
  }, [headphonesConfirmed, audioSource])

  // Step 4: Start audio streaming
  const startAudioStreaming = useCallback(async (micStream: MediaStream | null, session: WebAudioV2Session): Promise<void> => {
    log('Step 4: Starting audio streaming')

    // Reuse the AudioContext created in startRecording
    const audioContext = audioContextRef.current!
    const analyser = audioContext.createAnalyser()
    analyser.fftSize = 256

    log('Audio context state:', audioContext.state, 'Sample rate:', audioContext.sampleRate)

    // Resume audio context if suspended (required by some browsers)
    if (audioContext.state === 'suspended') {
      log('Resuming suspended audio context...')
      await audioContext.resume()
      log('Audio context resumed, new state:', audioContext.state)
    }
    analyserRef.current = analyser
    setAnalyserState(analyser)

    const processor = await createCaptureWorklet(audioContext, (samples, clock) => {
      if (!audioProcessingStartedRef.current) return
      try {
        voiceTimingRef.current?.capture(samples.length, clock)
        session.push(samples)
        chunkCountRef.current++
        if (chunkCountRef.current % 25 === 0) setDebugStats(prev => ({ ...prev, chunksSent: chunkCountRef.current }))
      } catch (cause) {
        handleAudioSessionFailure(cause instanceof Error ? cause : new Error('Opus encode failed'))
      }
    })

    // Connect mic source if available
    if (micStream) {
      const micSource = audioContext.createMediaStreamSource(micStream)
      micSource.connect(analyser)
      micSource.connect(processor)
    }

    // Mix in display/tab audio if available
    // NOTE: We do NOT connect display audio to audioContext.destination —
    // the source tab already plays the audio, so replaying it here would cause echo.
    if (displayStreamRef.current && displayStreamRef.current.getAudioTracks().length > 0) {
      const displaySource = audioContext.createMediaStreamSource(displayStreamRef.current)
      displaySource.connect(processor)
      // Use display audio for visualization when no mic
      if (!micStream) {
        displaySource.connect(analyser)
      }
      log('Display audio connected to recording pipeline')

      // Watch the system-audio level so a silent capture (wrong monitor device,
      // idle output sink) is surfaced in the UI instead of discovered post-hoc.
      const sysAnalyser = audioContext.createAnalyser()
      sysAnalyser.fftSize = 2048
      displaySource.connect(sysAnalyser)
      const levelBuf = new Float32Array(sysAnalyser.fftSize)
      let silentTicks = 0
      systemAudioWatchRef.current = setInterval(() => {
        sysAnalyser.getFloatTimeDomainData(levelBuf)
        let peak = 0
        for (let i = 0; i < levelBuf.length; i++) {
          const v = Math.abs(levelBuf[i])
          if (v > peak) peak = v
        }
        if (peak > 0.003) {
          silentTicks = 0
          setSystemAudioStatus('active')
        } else if (++silentTicks >= 4) {
          setSystemAudioStatus('silent')
        }
      }, 1000)
    }

    processor.connect(audioContext.destination)

    processorRef.current = processor
    audioProcessingStartedRef.current = true

    log('Audio streaming started')
  }, [handleAudioSessionFailure])

  // Main start recording function - sequential flow
  const startRecording = useCallback(async (memorySpaceId?: string) => {
    if (startingRef.current || stoppingRef.current || audioProcessingStartedRef.current) return
    startingRef.current = true
    const needsMic = audioSource !== 'tab'
    const needsDisplayAudio = audioSource !== 'mic'

    try {
      // A stopped capture may still be draining a response on its old socket.
      // Starting a fresh capture explicitly retires that transport first.
      cleanupTransport()
      setError(null)
      setLiveTranscript('')
      setConversationState(null)
      setConversationError(null)
      if (!globalThis.AudioWorkletNode) throw new Error('This browser needs AudioWorklet support to record audio.')
      if (headphonesConfirmed && audioSource === 'mic' && conversationUnavailableReason) throw new Error(conversationUnavailableReason)
      // Unlock playback within the user gesture, before permission/network awaits.
      const audioContext = new AudioContext()
      audioContextRef.current = audioContext
      const unlocked = audioContext.resume()
      if (headphonesConfirmed && audioSource === 'mic') await checkOpusSupport()
      await unlocked

      // Step 1: Get microphone access (skip for tab-only)
      let micStream: MediaStream | null = null
      if (needsMic) {
        setCurrentStep('mic')
        micStream = await getMicrophoneAccess()
      }

      // Step 1b: Get display/tab audio if needed. Try the share picker first and
      // fall back to a loopback input only if it produced no audio track — feature
      // detection, not UA sniffing, so Chromium keeps its one-click tab share.
      // Skip the picker outright when the user has already chosen a loopback device.
      if (needsDisplayAudio) {
        setCurrentStep('display-audio')
        const shared = monitorDeviceId ? null : await getDisplayAudio()
        if (!shared) {
          await getMonitorAudio()
        }
      }

      setCurrentStep('websocket')
      // Step 2: Connect WebSocket (includes stabilization delay)
      const session = await connectAudioSession()

      setCurrentStep('audio-start')
      // Step 3: Send audio-start message (uses audioContextRef for sample rate)
      await startAudioSession(session, memorySpaceId)

      setCurrentStep('streaming')
      // Step 4: Start audio streaming (reuses existing AudioContext)
      await startAudioStreaming(micStream, session)

      // All steps complete - mark as recording
      setIsRecording(true)
      setRecordingDuration(0)

      // Start duration timer
      durationIntervalRef.current = setInterval(() => {
        setRecordingDuration(prev => prev + 1)
      }, 1000)

      log('Recording started successfully!')

    } catch (error) {
      console.error('❌ Recording failed:', error)
      setCurrentStep('error')
      setError(error instanceof Error ? error.message : 'Recording failed')
      setDebugStats(prev => ({
        ...prev,
        lastError: error instanceof Error ? error.message : 'Recording failed',
        lastErrorTime: new Date()
      }))
      cleanup()
    } finally { startingRef.current = false }
  }, [headphonesConfirmed, conversationUnavailableReason, getMicrophoneAccess, getDisplayAudio, getMonitorAudio, monitorDeviceId, audioSource, connectAudioSession, startAudioSession, startAudioStreaming, cleanup, cleanupTransport])

  // Stop recording function
  const stopRecording = useCallback(() => {
    if (!isRecording || stoppingRef.current) return
    stoppingRef.current = true

    log('Stopping recording')
    setCurrentStep('stopping')

    // Stop audio processing
    audioProcessingStartedRef.current = false

    const session = audioSessionRef.current
    const stopped = session?.stop()
    cleanupCapture()
    setActiveWakeClientId(null)
    setVoiceProcessing(null)
    setPlaybackActivity('idle')
    setConversationReady(false)
    setConversationStarting(false)
    setConversationState(null)

    // Keep the busy state and unload guard until buffered audio has drained and
    // the server acknowledges captureStopped. The microphone is already off.
    void Promise.resolve(stopped).then(() => {
      setCurrentStep('idle')
      log('Recording stopped')
    }).catch(error => {
      setError(error instanceof Error ? error.message : 'Audio V2 stop failed')
      setCurrentStep('error')
    }).finally(() => {
      if (audioSessionRef.current === session) audioSessionRef.current = null
      stoppingRef.current = false
      setIsRecording(false)
      setRecordingDuration(0)
    })
  }, [isRecording, cleanupCapture])

  const startConversation = useCallback(async (memorySpaceId?: string, threadId?: string) => {
    if (stoppingRef.current) return
    setConversationError(null)
    if (!headphonesConfirmed || audioSource !== 'mic') {
      setConversationError('Confirm headphones and select microphone capture before starting a conversation.')
      return
    }
    if (conversationUnavailableReason) { setConversationError(conversationUnavailableReason); return }
    try {
      if (!audioSessionRef.current) await startRecording(memorySpaceId)
      const session = audioSessionRef.current
      if (!session) return
      await audioContextRef.current?.resume()
      setConversationStarting(true)
      session.startConversation(voiceEngine, threadId)
    } catch (cause) {
      setConversationStarting(false)
      setConversationError(cause instanceof Error ? cause.message : 'Conversation could not start')
    }
  }, [headphonesConfirmed, audioSource, conversationUnavailableReason, startRecording, voiceEngine])

  const endConversation = useCallback(() => {
    try { audioSessionRef.current?.endConversation() }
    catch (cause) { setConversationError(cause instanceof Error ? cause.message : 'Conversation could not end') }
  }, [])
  const cancelVoiceTask = useCallback((taskId: string) => {
    try { audioSessionRef.current?.cancelTask(taskId) }
    catch (cause) { setConversationError(cause instanceof Error ? cause.message : 'Task cancellation could not be sent') }
  }, [])

  // Stop recording when user logs out
  useEffect(() => {
    if (!user && isRecording) {
      log('User logged out, stopping recording')
      stopRecording()
      cleanupTransport()
    }
  }, [user, isRecording, stopRecording, cleanupTransport])

  // Warn user before closing tab during recording
  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (isRecording) {
        event.preventDefault()
        event.returnValue = 'Recording in progress. Are you sure you want to leave?'
        return event.returnValue
      }
    }

    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [isRecording])

  // The global provider survives navigation; actual owner disposal must release audio.
  useEffect(() => () => cleanup(), [cleanup])

  const contextValue = useMemo<RecordingContextType>(() => ({
    currentStep,
    isRecording,
    recordingDuration,
    error,
    liveTranscript,
    startRecording,
    stopRecording,
    audioSource,
    setAudioSource,
    headphonesConfirmed, setHeadphonesConfirmed, voiceEngine, setVoiceEngine,
    voiceProcessing, playbackActivity, conversationState, conversationError, conversationStarting, conversationReady, conversationUnavailableReason,
    startConversation, endConversation, cancelVoiceTask,
    availableDevices,
    selectedDeviceId,
    setSelectedDeviceId,
    monitorDeviceId,
    setMonitorDeviceId,
    requestDeviceAccess,
    systemAudioLabel,
    systemAudioStatus,
    analyser: analyserState,
    debugStats,
    formatDuration,
    canAccessMicrophone,
    likelyLacksDisplayAudio
  }), [
    currentStep, isRecording, recordingDuration, error, liveTranscript,
    startRecording, stopRecording,
    audioSource, setAudioSource,
    headphonesConfirmed, voiceEngine, voiceProcessing, playbackActivity, conversationState, conversationError, conversationStarting, conversationReady, conversationUnavailableReason,
    startConversation, endConversation, cancelVoiceTask,
    availableDevices, selectedDeviceId, setSelectedDeviceId,
    monitorDeviceId, setMonitorDeviceId, requestDeviceAccess, systemAudioLabel, systemAudioStatus,
    analyserState, debugStats, formatDuration, canAccessMicrophone
  ])

  return (
    <RecordingContext.Provider value={contextValue}>
      {children}
    </RecordingContext.Provider>
  )
}

export function useRecording() {
  const context = useContext(RecordingContext)
  if (context === undefined) {
    throw new Error('useRecording must be used within a RecordingProvider')
  }
  return context
}
