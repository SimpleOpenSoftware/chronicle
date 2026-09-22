import { useState } from 'react'
import { Radio, Settings, Monitor, Mic, Headphones } from 'lucide-react'
import { useRecording, isLoopbackDevice, isMacOS } from '../contexts/RecordingContext'
import { Button } from '../components/ui'
import SimplifiedControls from '../components/audio/SimplifiedControls'
import AudioVisualizer from '../components/audio/AudioVisualizer'
import WakeFeedback from '../components/audio/WakeFeedback'
import DialogueTasks from '../components/DialogueTasks'
import { ConversationPhase, SpeechEngine, VoiceTaskStatus } from '../protocol/audioV2'

export default function LiveRecord({
  memorySpaceId,
  destinationLabel = 'Main',
  embedded = false,
}: {
  memorySpaceId?: string
  destinationLabel?: string
  embedded?: boolean
} = {}) {
  const params = new URLSearchParams(window.location.search)
  const threadId = params.get("thread") || undefined
  memorySpaceId = memorySpaceId || params.get("memory_space_id") || undefined
  const recording = useRecording()
  const [isLoadingMicrophones, setIsLoadingMicrophones] = useState(false)
  const microphoneDevices = recording.availableDevices.filter(
    device => recording.audioSource === 'mic' || !isLoopbackDevice(device.label)
  )
  const microphoneLabelsKnown = microphoneDevices.some(device => device.label)
  const voice = recording.conversationState
  const engaged = Boolean(voice && voice.phase !== ConversationPhase.ENDED && voice.phase !== ConversationPhase.UNSPECIFIED)
  const voiceLabel = recording.conversationStarting ? 'Starting conversation…' : voice ? {
    [ConversationPhase.UNSPECIFIED]: 'Ready to talk',
    [ConversationPhase.LISTENING]: 'Listening',
    [ConversationPhase.THINKING]: 'Thinking',
    [ConversationPhase.SPEAKING]: 'Responding',
    [ConversationPhase.ENDED]: 'Conversation ended',
  }[voice.phase] : 'Ready to talk'
  const activities = engaged && !recording.conversationError ? [
    recording.voiceProcessing?.transcribing && 'Transcribing',
    recording.voiceProcessing?.generatingText && 'Generating reply',
    recording.voiceProcessing?.synthesizingSpeech && 'Generating speech (TTS)',
    recording.voiceProcessing?.generatingResponse && 'Generating response',
    recording.playbackActivity && ({ preparing: 'Preparing audio', playing: 'Playing', buffering: 'Buffering', idle: '' }[recording.playbackActivity]),
  ].filter((label): label is string => Boolean(label)) : []
  const recordingBusy = !['idle', 'error', 'streaming'].includes(recording.currentStep ?? 'idle')


  const loadMicrophones = async () => {
    setIsLoadingMicrophones(true)
    try {
      await recording.requestDeviceAccess()
    } finally {
      setIsLoadingMicrophones(false)
    }
  }

  return (
    <div>
      {/* Header */}
      <div className={`flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between ${embedded ? 'mb-4' : 'mb-6'}`}>
        <div className="flex items-center space-x-2">
          <Radio className="h-6 w-6 text-blue-600 flex-shrink-0" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {embedded ? `Record into ${destinationLabel}` : 'Live Audio Recording'}
          </h1>
        </div>

      </div>

      {/* Audio Source Toggle */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className={`inline-flex rounded-lg border border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-800 p-0.5 ${recording.isRecording ? 'opacity-50 pointer-events-none' : ''}`}>
          <button
            aria-pressed={recording.audioSource === 'mic'}
            onClick={() => recording.setAudioSource('mic')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'mic'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <span>Mic</span>
          </button>
          <button
            aria-pressed={recording.audioSource === 'meeting'}
            onClick={() => recording.setAudioSource('meeting')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'meeting'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <Monitor className="h-3.5 w-3.5" />
            <span>Meeting</span>
          </button>
          <button
            aria-pressed={recording.audioSource === 'tab'}
            onClick={() => recording.setAudioSource('tab')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'tab'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Monitor className="h-3.5 w-3.5" />
            <span>Tab</span>
          </button>
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {recording.audioSource === 'mic'
            ? 'Microphone only'
            : recording.audioSource === 'meeting'
              ? recording.monitorDeviceId
                ? 'Mic + system audio (from the loopback device below)'
                : 'Mic + tab audio (you\'ll be asked to select a tab)'
              : recording.monitorDeviceId
                ? 'System audio only (from the loopback device below)'
                : 'Browser tab audio only (no microphone)'}
        </span>
      </div>

      {/* Optional loopback capture. Chromium shares tab audio straight from the picker,
          so this stays hidden there; Firefox ignores `audio: true` (bugzilla #1541425) and
          needs a loopback input — a PipeWire/PulseAudio monitor on Linux, or a driver the
          user installed on macOS, which has no built-in equivalent. Choosing one here
          skips the share picker entirely and captures the whole output instead of one tab. */}
      {recording.audioSource !== 'mic' && (() => {
        // Labels are blank until an audio permission is granted, so an empty list
        // means "not probed yet" — not "this machine has no loopback device".
        const labelsKnown = recording.availableDevices.some(d => d.label)
        const loopbacks = recording.availableDevices.filter(d => isLoopbackDevice(d.label))
        // A macOS Aggregate Device can be named anything, so once we know the labels
        // and found no known driver, offer every input rather than a dead end.
        const options = loopbacks.length > 0
          ? loopbacks
          : (isMacOS && labelsKnown ? recording.availableDevices : [])
        // On Chromium the share picker handles this, so stay out of the way unless
        // the browser needs a loopback or the user already has one to choose from.
        const relevant = recording.likelyLacksDisplayAudio || recording.monitorDeviceId || loopbacks.length > 0
        if (!relevant) return null
        return (
          <div className="mb-4 space-y-2">
            <div className="flex items-center gap-2">
              <Monitor className="h-4 w-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
              <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-shrink-0">
                System audio:
              </label>
              {options.length > 0 ? (
                <select
                  value={recording.monitorDeviceId ?? ''}
                  onChange={(e) => recording.setMonitorDeviceId(e.target.value || null)}
                  disabled={recording.isRecording}
                  className={`
                    flex-1 min-w-0 text-sm px-2 py-1.5 rounded-lg border
                    bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100
                    border-gray-300 dark:border-gray-600
                    ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                  `}
                >
                  <option value="">
                    {isMacOS ? 'Choose loopback input (e.g. BlackHole)' : 'Choose "Monitor of …" output device'}
                  </option>
                  {options.map((device) => (
                    <option key={device.deviceId} value={device.deviceId}>
                      {device.label}
                    </option>
                  ))}
                </select>
              ) : labelsKnown ? (
                <span className="text-sm text-orange-600 dark:text-orange-400">
                  No loopback input found on this machine.
                </span>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => recording.requestDeviceAccess()}>
                  Load audio devices…
                </Button>
              )}
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {recording.monitorDeviceId
                ? 'Records all audio from the selected output.'
                : recording.likelyLacksDisplayAudio
                  ? 'This browser needs a loopback audio input. You can also use a Chromium browser to share tab audio.'
                  : 'Leave unset to choose a browser tab.'}
            </p>
          </div>
        )
      })()}

      {/* Microphone setup stays visible before recording. Browsers hide device labels
          until permission is granted, so the first action probes and immediately
          releases the default input; it does not start a Chronicle recording. */}
      {recording.audioSource !== 'tab' && (
        <div className="mb-4 space-y-1.5">
          <div className="flex items-center gap-2">
            <Settings className="h-4 w-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
            <label
              htmlFor="recording-microphone"
              className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-shrink-0"
            >
              Microphone:
            </label>
            {microphoneLabelsKnown ? (
              <select
                id="recording-microphone"
                value={recording.selectedDeviceId ?? ''}
                onChange={(e) => recording.setSelectedDeviceId(e.target.value || null)}
                disabled={recording.isRecording}
                className={`
                  flex-1 min-w-0 text-sm px-2 py-1.5 rounded-lg border
                  bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100
                  border-gray-300 dark:border-gray-600
                  ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                `}
              >
                <option value="">System Default</option>
                {microphoneDevices.map((device) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label}
                  </option>
                ))}
              </select>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                onClick={loadMicrophones}
                disabled={recording.isRecording || isLoadingMicrophones}
              >
                {isLoadingMicrophones ? 'Loading microphones…' : 'Choose microphone…'}
              </Button>
            )}
          </div>
          {!microphoneLabelsKnown && (
            <p className="pl-6 text-xs text-gray-500 dark:text-gray-400">
              Allow access to choose a microphone. Recording will not start.
            </p>
          )}
        </div>
      )}

      <div className="mb-3 text-sm text-[var(--tape-activity)]">
        Recording destination: <strong className="text-[var(--tape-ink)]">{destinationLabel}</strong>
      </div>
      <SimplifiedControls recording={recording} memorySpaceId={memorySpaceId} />

      {recording.audioSource === 'mic' && (
        <section aria-label="Voice conversation" className="mb-6 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-gray-900 dark:text-gray-100">
              <Headphones className="h-4 w-4 text-blue-600" aria-hidden="true" />
              <h2 className="font-medium">Talk to Chronicle</h2>
              <span role="status" aria-live="polite" className="text-sm text-gray-500 dark:text-gray-400">{voiceLabel}</span>
            </div>
            {engaged || recording.conversationStarting ? (
              <Button variant="secondary" size="sm" onClick={recording.endConversation}>End conversation</Button>
            ) : (
              <Button size="sm" onClick={() => void (threadId ? recording.startConversation(memorySpaceId, threadId) : recording.startConversation(memorySpaceId))}
                disabled={!recording.headphonesConfirmed || Boolean(recording.conversationUnavailableReason) || recordingBusy || (recording.isRecording && !recording.conversationReady)}>
                Start conversation
              </Button>
            )}
          </div>
          {(voice?.threadId || threadId) && <div className="text-sm">
            <a className="underline" href={`/chat?session=${encodeURIComponent(voice?.threadId || threadId!)}`}>Open this dialogue in Chat</a>
            {engaged && voice?.threadId && <DialogueTasks sessionId={voice.threadId} />}
          </div>}
          {activities.length > 0 && (
            <div role="status" aria-label="Conversation activity" aria-live="polite" aria-atomic="true"
              className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-600 dark:text-gray-300">
              {activities.map((label, index) => <span key={label} className="inline-flex items-center gap-3">
                {index > 0 && <span aria-hidden="true" className="text-gray-400">·</span>}<span>{label}</span>
              </span>)}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-3 text-sm">
            <label className="flex items-center gap-2 text-gray-700 dark:text-gray-300">
              <input type="checkbox" checked={Boolean(recording.headphonesConfirmed)}
                disabled={recording.isRecording || recordingBusy}
                onChange={event => recording.setHeadphonesConfirmed(event.target.checked)} />
              I’m using headphones
            </label>
            <label className="flex items-center gap-2 text-gray-700 dark:text-gray-300">
              Conversation engine
              <select value={recording.voiceEngine ?? SpeechEngine.MODULAR}
                disabled={engaged || recording.conversationStarting || recordingBusy}
                onChange={event => recording.setVoiceEngine(Number(event.target.value) as SpeechEngine)}
                className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1.5 text-gray-900 dark:text-gray-100">
                <option value={SpeechEngine.MODULAR}>Local Qwen + Kokoro</option>
                <option value={SpeechEngine.REALTIME}>Online realtime</option>
              </select>
            </label>
          </div>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {recording.conversationUnavailableReason || (
              recording.isRecording && !recording.conversationReady
                ? 'To enable conversation, stop recording and confirm headphones before starting again.'
                : engaged
                  ? 'Speak naturally, or interrupt an answer. Ending the conversation keeps recording on.'
                  : 'Start here with the selected engine, or say “Hey Hermes” to use the local engine while recording with headphones. Follow-up turns need no wake word.'
            )}
          </p>
          {recording.conversationError && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{recording.conversationError}</p>}
          {voice?.detail && voice.detail !== 'user_ended' && (
            <p className="text-sm text-gray-600 dark:text-gray-400">{voice.detail}</p>
          )}
          {voice?.responseText && <p className="whitespace-pre-wrap text-gray-900 dark:text-gray-100">{voice.responseText}</p>}
          {Boolean(voice?.tasks.length) && (
            <ul aria-label="Conversation tasks" className="space-y-2 border-t border-gray-200 dark:border-gray-700 pt-3">
              {voice!.tasks.map(task => {
                const canCancel = task.status === VoiceTaskStatus.QUEUED || task.status === VoiceTaskStatus.RUNNING
                const taskLabel = task.toolName === 'search_memories' ? 'Searching memories' : 'Hermes'
                const status = {
                  [VoiceTaskStatus.UNSPECIFIED]: 'Waiting', [VoiceTaskStatus.QUEUED]: 'Queued',
                  [VoiceTaskStatus.RUNNING]: 'Working', [VoiceTaskStatus.COMPLETED]: 'Complete',
                  [VoiceTaskStatus.FAILED]: 'Failed', [VoiceTaskStatus.CANCEL_REQUESTED]: 'Stop requested',
                  [VoiceTaskStatus.CANCELLED]: 'Stopped', [VoiceTaskStatus.UNKNOWN]: 'Outcome unknown',
                }[task.status]
                return <li key={task.taskId} className="flex items-center justify-between gap-3 text-sm">
                  <div><span className="font-medium text-gray-800 dark:text-gray-200">{taskLabel}</span>{' '}
                    <span className="text-gray-500 dark:text-gray-400">{status}{task.detail ? ` · ${task.detail}` : ''}</span></div>
                  {canCancel && <Button variant="secondary" size="sm" onClick={() => recording.cancelVoiceTask(task.taskId)}>Stop task</Button>}
                </li>
              })}
            </ul>
          )}
          {voice?.vaultRetrievalEnabled && <p className="text-xs text-gray-500 dark:text-gray-400">Memory search is available for this conversation.</p>}
        </section>
      )}

      {/* System-audio capture health (meeting/tab mode) */}
      {recording.isRecording && recording.audioSource !== 'mic' && (
        <div className={`mb-6 -mt-2 p-3 rounded-lg border text-sm ${
          recording.systemAudioStatus === 'silent'
            ? 'bg-orange-50 dark:bg-orange-900/20 border-orange-200 dark:border-orange-800 text-orange-700 dark:text-orange-300'
            : 'bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400'
        }`}>
          <span className="font-medium">System audio:</span>{' '}
          {recording.systemAudioLabel ?? 'not captured'}
          {recording.systemAudioStatus === 'active' && (
            <span className="text-green-600 dark:text-green-400"> — receiving audio ✓</span>
          )}
          {recording.systemAudioStatus === 'silent' && (
            <span>
              {' '}— <strong>no signal detected.</strong> Check the selected audio output if sound is playing.
            </span>
          )}
        </div>
      )}

      {recording.isRecording && (
        <AudioVisualizer isRecording analyser={recording.analyser} />
      )}

      {/* Live streaming transcript - real-time text from the streaming STT provider */}
      {(recording.isRecording || recording.liveTranscript) && (
        <div className="mt-4 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-2">
            <span className="relative flex h-2.5 w-2.5">
              {recording.isRecording && (
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
              )}
              <span className={`relative inline-flex rounded-full h-2.5 w-2.5 ${recording.isRecording ? 'bg-green-500' : 'bg-gray-400'}`}></span>
            </span>
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Live Transcript
            </h3>
            <span className="text-xs text-gray-400">(streaming)</span>
          </div>
          <p className="text-gray-900 dark:text-gray-100 whitespace-pre-wrap min-h-[1.5rem]">
            {recording.liveTranscript || (
              <span className="text-gray-400 italic">
                {recording.isRecording ? 'Listening…' : ''}
              </span>
            )}
          </p>
        </div>
      )}

      {/* Wake-word feedback - pulses on arm/end-of-turn + shows recognized command */}
      <WakeFeedback />

    </div>
  )
}
