import { Mic, Square, Loader2, Monitor } from 'lucide-react'
import { RecordingContextType } from '../../contexts/RecordingContext'
import { Card } from '../ui'

interface SimplifiedControlsProps {
  recording: RecordingContextType
  memorySpaceId?: string
}

const getStepText = (step: string): string => {
  switch (step) {
    case 'idle': return 'Ready to Record'
    case 'mic': return 'Getting Microphone Access...'
    case 'display-audio': return 'Requesting Tab Audio Access...'
    case 'websocket': return 'Connecting to Server...'
    case 'audio-start': return 'Preparing Recording...'
    case 'streaming': return 'Starting Recording...'
    case 'stopping': return 'Finishing recording…'
    case 'error': return 'Error Occurred'
    default: return 'Processing...'
  }
}

const isProcessing = (step: string): boolean => {
  return ['mic', 'display-audio', 'websocket', 'audio-start', 'streaming', 'stopping'].includes(step)
}

export default function SimplifiedControls({ recording, memorySpaceId }: SimplifiedControlsProps) {
  const processing = isProcessing(recording.currentStep)
  const stopping = recording.currentStep === 'stopping'
  const canStart = recording.canAccessMicrophone && !processing && !recording.isRecording

  const handleClick = () => {
    if (recording.isRecording) {
      recording.stopRecording()
    } else if (canStart) {
      recording.startRecording(memorySpaceId)
    }
  }

  // Button appearance based on state
  const getButtonClasses = (): string => {
    if (recording.isRecording) return 'bg-red-600 hover:bg-red-700'
    if (processing) return 'bg-yellow-600'
    if (recording.currentStep === 'error') return 'bg-red-600 hover:bg-red-700'
    return 'bg-blue-600 hover:bg-blue-700'
  }

  const isDisabled = stopping || (recording.isRecording ? false : (processing || !canStart))

  return (
    <Card raised padded={false} className="p-8 mb-6">
      <div className="text-center">
        {/* Single Toggle Button */}
        <div className="mb-6 flex justify-center">
          <div className="relative">
            {/* Pulsing ring when recording */}
            {recording.isRecording && !stopping && (
              <span className="absolute inset-0 rounded-full bg-red-400 opacity-30 animate-ping" />
            )}
            <button
              aria-label={stopping ? getStepText(recording.currentStep) : recording.isRecording ? 'Stop recording' : processing ? getStepText(recording.currentStep) : 'Start recording'}
              onClick={handleClick}
              disabled={isDisabled}
              className={`relative w-24 h-24 ${getButtonClasses()} text-white rounded-full flex items-center justify-center transition-all duration-200 shadow-lg disabled:opacity-50 disabled:cursor-not-allowed transform hover:scale-105 active:scale-95`}
            >
              {stopping ? (
                <Loader2 className="h-10 w-10 animate-spin" />
              ) : recording.isRecording ? (
                <Square className="h-10 w-10 fill-current" />
              ) : processing ? (
                <Loader2 className="h-10 w-10 animate-spin" />
              ) : recording.audioSource === 'tab' ? (
                <Monitor className="h-10 w-10" />
              ) : (
                <Mic className="h-10 w-10" />
              )}
            </button>
          </div>
        </div>

        {/* Status Text */}
        <div className="space-y-2">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            {stopping ? getStepText(recording.currentStep) : recording.isRecording ? 'Recording in Progress' : getStepText(recording.currentStep)}
          </h2>

          {/* Recording Duration */}
          {recording.isRecording && (
            <p className="text-3xl font-mono text-red-600 dark:text-red-400">
              {recording.formatDuration(recording.recordingDuration)}
            </p>
          )}

          {/* Action Text */}
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {stopping
              ? 'Microphone off. Waiting for the server to finish saving audio…'
              : recording.isRecording
              ? 'Click to stop recording'
              : recording.currentStep === 'idle'
                ? 'Click to start recording'
                : recording.currentStep === 'error'
                  ? 'Click to try again'
                  : 'Please wait while setting up...'}
          </p>

          {/* Error Message */}
          {recording.error && (
            <div className="mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
              <p className="text-sm text-red-700 dark:text-red-300">
                <strong>Error:</strong> {recording.error}
              </p>
            </div>
          )}

          {/* Security Warning */}
          {!recording.canAccessMicrophone && (
            <div className="mt-4 p-3 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg">
              <p className="text-sm text-orange-700 dark:text-orange-300">
                <strong>Secure Access Required:</strong> Microphone access requires HTTPS or localhost
              </p>
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}
