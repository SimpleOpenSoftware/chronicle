import { useNavigate, useLocation } from 'react-router-dom'
import { Radio, Square } from 'lucide-react'
import { useRecording } from '../../contexts/RecordingContext'
import { useWakeFeedback } from '../../hooks/useWakeFeedback'

export default function GlobalRecordingIndicator() {
  const navigate = useNavigate()
  const location = useLocation()
  const { isRecording, currentStep, recordingDuration, stopRecording, formatDuration } = useRecording()
  const stopping = currentStep === 'stopping'
  const { phase } = useWakeFeedback()

  // Don't show if not recording
  if (!isRecording) return null

  // Don't show on the Live Record page (it has its own UI)
  if (location.pathname === '/live-record') return null

  // While the wake word is active (armed -> end-of-turn) the whole indicator
  // turns amber — the same color as the "wake word detected" message — then
  // snaps back to red at end of turn. This is visible from any page.
  const listening = phase === 'listening'
  // While a follow-up window is open the next utterance is taken as a follow-up
  // (no wake word) — show it in sky blue, distinct from amber (armed) and red.
  const followup = phase === 'followup'

  // Color tokens swap as one set so the pill, dot, text and buttons stay coherent.
  const c = listening
    ? {
        wrap: 'bg-amber-50 dark:bg-amber-900/30 border-amber-300 dark:border-amber-700',
        ping: 'bg-amber-400',
        dot: 'bg-amber-500',
        time: 'text-amber-700 dark:text-amber-300',
        mode: 'text-amber-600 dark:text-amber-400',
        navHover: 'hover:bg-amber-100 dark:hover:bg-amber-800/50 text-amber-600 dark:text-amber-400',
        stop: 'bg-amber-600 hover:bg-amber-700',
      }
    : followup
    ? {
        wrap: 'bg-sky-50 dark:bg-sky-900/30 border-sky-300 dark:border-sky-700',
        ping: 'bg-sky-400',
        dot: 'bg-sky-500',
        time: 'text-sky-700 dark:text-sky-300',
        mode: 'text-sky-600 dark:text-sky-400',
        navHover: 'hover:bg-sky-100 dark:hover:bg-sky-800/50 text-sky-600 dark:text-sky-400',
        stop: 'bg-sky-600 hover:bg-sky-700',
      }
    : {
        wrap: 'bg-red-50 dark:bg-red-900/30 border-red-200 dark:border-red-800',
        ping: 'bg-red-400',
        dot: 'bg-red-500',
        time: 'text-red-700 dark:text-red-300',
        mode: 'text-red-600 dark:text-red-400',
        navHover: 'hover:bg-red-100 dark:hover:bg-red-800/50 text-red-600 dark:text-red-400',
        stop: 'bg-red-600 hover:bg-red-700',
      }

  return (
    <div className={`flex items-center gap-3 px-3 py-1.5 border rounded-lg transition-colors duration-300 ${c.wrap}`}>
      {/* Pulsing dot — amber while listening, red otherwise */}
      <div className="relative flex items-center" title={stopping ? 'Microphone off — finishing recording' : listening ? 'Wake word detected — listening…' : followup ? 'Listening for follow-up… (no wake word needed)' : 'Recording'}>
        {!stopping && <span className={`absolute inline-flex h-3 w-3 rounded-full opacity-75 animate-ping ${c.ping}`} />}
        <span className={`relative inline-flex h-3 w-3 rounded-full ${c.dot}`} />
      </div>

      {/* Recording info */}
      <div className="flex items-center gap-2 text-sm">
        <span className={`font-medium ${c.time}`}>
          {formatDuration(recordingDuration)}
        </span>
        <span className={`flex items-center gap-1 ${c.mode}`}>
          {stopping ? <span>Finishing recording…</span> : listening ? (
            <>
              <Radio className="h-3 w-3" />
              <span>Listening</span>
            </>
          ) : followup ? (
            <>
              <Radio className="h-3 w-3" />
              <span>Follow-up</span>
            </>
          ) : (
            <>
              <Radio className="h-3 w-3" />
              <span>Recording</span>
            </>
          )}
        </span>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-1.5 ml-1">
        {/* Navigate to Live Record */}
        <button
          onClick={() => navigate('/live-record')}
          className={`p-1.5 rounded transition-colors ${c.navHover}`}
          title="Go to Live Record"
        >
          <Radio className="h-4 w-4" />
        </button>

        {/* Stop button */}
        <button
          onClick={stopRecording}
          disabled={stopping}
          className={`p-1.5 rounded transition-colors text-white ${c.stop}`}
          title={stopping ? 'Finishing recording' : 'Stop Recording'}
        >
          <Square className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
