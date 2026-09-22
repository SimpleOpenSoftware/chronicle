import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Loader2, Pause, Play, RefreshCw, Volume2 } from 'lucide-react'
import {
  BACKEND_URL,
  dataAuditApi,
  speakerApi,
  SpeakerLabelReviewClip,
  SpeakerLabelReviewMetrics,
  SpeakerLabelReviewVerdict,
} from '../../services/api'
import { getStorageKey } from '../../utils/storage'
import { TITLE_NOT_GENERATED } from '../../lib/constants'
import { Alert, Button, Select, StateBadge } from '../ui'
import { formatClock } from './format'

type Decision = {
  verdict: SpeakerLabelReviewVerdict
  correctedSpeaker?: string
}

const VERDICTS: Array<{ verdict: SpeakerLabelReviewVerdict; label: string }> = [
  { verdict: 'correct', label: 'Correct' },
  { verdict: 'unknown', label: 'Unknown' },
  { verdict: 'background', label: 'Background' },
  { verdict: 'mixed', label: 'Mixed voices' },
  { verdict: 'bad_audio', label: 'Bad audio' },
]

export default function SpeakerLabelReview({
  onPendingChanged,
}: {
  onPendingChanged?: () => void
}) {
  const [clips, setClips] = useState<SpeakerLabelReviewClip[]>([])
  const [reviewedTotal, setReviewedTotal] = useState(0)
  const [candidateClaims, setCandidateClaims] = useState(0)
  const [metrics, setMetrics] = useState<SpeakerLabelReviewMetrics | null>(null)
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [speakers, setSpeakers] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await dataAuditApi.getNextSpeakerLabelReviews(5)
      setClips(response.data.batch)
      setReviewedTotal(response.data.reviewed_total)
      setCandidateClaims(response.data.candidate_claims)
      setDecisions({})
      const metricsResponse = await dataAuditApi.getSpeakerLabelReviewMetrics()
      setMetrics(metricsResponse.data)
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load speaker labels')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    speakerApi
      .getEnrolledSpeakers()
      .then((response) =>
        setSpeakers(
          (response.data.speakers || [])
            .map((speaker: any) => speaker.name)
            .filter(Boolean)
            .sort((a: string, b: string) => a.localeCompare(b))
        )
      )
      .catch(() => undefined)
    return () => audioRef.current?.pause()
  }, [load])

  const stop = () => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlaying(null)
  }

  const play = (clip: SpeakerLabelReviewClip) => {
    if (playing === clip.review_key) {
      stop()
      return
    }
    stop()
    const token = localStorage.getItem(getStorageKey('token')) || ''
    const start = Math.max(0, clip.start - 4)
    const end = clip.end + 4
    const url =
      `${BACKEND_URL}/api/audio/chunks/${clip.conversation_id}` +
      `?start_time=${start.toFixed(2)}&end_time=${end.toFixed(2)}&format=wav&token=${token}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlaying(clip.review_key)
    audio.addEventListener('ended', stop)
    audio.addEventListener('error', () => {
      setError('Audio playback failed')
      stop()
    })
    audio.play().catch(() => stop())
  }

  const choose = (clip: SpeakerLabelReviewClip, verdict: SpeakerLabelReviewVerdict) => {
    setDecisions((current) => ({
      ...current,
      [clip.review_key]: { verdict },
    }))
  }

  const relabel = (clip: SpeakerLabelReviewClip, correctedSpeaker: string) => {
    if (!correctedSpeaker) {
      setDecisions((current) => {
        const next = { ...current }
        delete next[clip.review_key]
        return next
      })
      return
    }
    setDecisions((current) => ({
      ...current,
      [clip.review_key]: { verdict: 'relabel', correctedSpeaker },
    }))
  }

  const submit = async () => {
    const reviewed = clips.filter((clip) => decisions[clip.review_key])
    if (!reviewed.length) return
    setSubmitting(true)
    setError(null)
    try {
      const response = await dataAuditApi.decideSpeakerLabelReviews(
        reviewed.map((clip) => ({
          conversation_id: clip.conversation_id,
          segment_index: clip.segment_index,
          segment_start_time: clip.segment_start_time,
          claimed_speaker: clip.claimed_speaker,
          confidence: clip.confidence,
          selection_lane: clip.selection_lane,
          verdict: decisions[clip.review_key].verdict,
          corrected_speaker: decisions[clip.review_key].correctedSpeaker,
        }))
      )
      if (response.data.errors.length) {
        setError(`${response.data.errors.length} decision(s) could not be saved`)
      } else {
        const savedMessage =
          `Saved ${response.data.recorded} reviews` +
            (response.data.corrections_pending
              ? ` · ${response.data.corrections_pending} corrections ready to apply`
              : '')
        setMessage(savedMessage)
      }
      onPendingChanged?.()
      await load()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to save review batch')
    } finally {
      setSubmitting(false)
    }
  }

  const decidedCount = Object.keys(decisions).length

  return (
    <section className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-gray-100">Review speaker labels</h3>
          <p className="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
            Listen and verify five speaker labels. Enrollment is separate.
          </p>
        </div>
        <div className="text-right text-xs text-gray-500 dark:text-gray-400">
          <div className="font-medium text-gray-700 dark:text-gray-200">{decidedCount}/{clips.length} decided</div>
          <div>{reviewedTotal} previously reviewed</div>
          <div>{candidateClaims} claims remain eligible</div>
        </div>
      </header>

      {metrics && metrics.overall.reviewed > 0 && (
        <div className="border-b border-gray-200 bg-gray-50 px-5 py-4 dark:border-gray-700 dark:bg-gray-900/30">
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
            <div>
              <span className="text-xs text-gray-500">Calibration checks</span>
              <div className="text-2xl font-semibold text-gray-900 dark:text-gray-100">
                {metrics.control.correct} of {metrics.control.evaluable} correct
              </div>
            </div>
            <div className="text-xs text-gray-500">
              {metrics.boundary.evaluable > 0 && metrics.boundary.precision != null
                ? `Uncertain claims: ${metrics.boundary.correct} of ${metrics.boundary.evaluable} correct`
                : ''}
              {metrics.overall.excluded > 0 ? ` · ${metrics.overall.excluded} mixed/bad excluded` : ''}
            </div>
          </div>
          {metrics.speakers.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {metrics.speakers.slice(0, 8).map((speaker) => (
                <StateBadge
                  key={speaker.speaker}
                  tone={speaker.precision != null && speaker.precision < 0.8 ? 'warning' : 'neutral'}
                >
                  {speaker.speaker} · {speaker.precision == null ? '—' : `${Math.round(speaker.precision * 100)}%`} ({speaker.evaluable})
                </StateBadge>
              ))}
            </div>
          )}
        </div>
      )}

      {error && <div className="p-4"><Alert tone="danger">{error}</Alert></div>}
      {message && <div className="p-4"><Alert tone="success">{message}</Alert></div>}

      {loading ? (
        <div className="flex items-center justify-center gap-2 p-12 text-sm text-gray-500">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading identity claims…
        </div>
      ) : clips.length === 0 ? (
        <div className="p-12 text-center">
          <Check className="mx-auto h-7 w-7 text-emerald-600" />
          <p className="mt-2 font-medium text-gray-900 dark:text-gray-100">All available labels reviewed</p>
          <p className="mt-1 text-sm text-gray-500">New identified segments will appear here.</p>
        </div>
      ) : (
        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {clips.map((clip, position) => {
            const decision = decisions[clip.review_key]
            return (
              <article key={clip.review_key} className="px-5 py-5">
                <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
                  <span className="font-mono">{position + 1}/5</span>
                  <span>{clip.conversation_title || TITLE_NOT_GENERATED}</span>
                  <span>·</span>
                  <span>{formatClock(clip.start)}–{formatClock(clip.end)}</span>
                  {clip.confidence != null && <StateBadge tone="neutral">score {clip.confidence.toFixed(3)}</StateBadge>}
                  <StateBadge tone={clip.selection_lane === 'boundary' ? 'warning' : 'neutral'}>
                    {clip.selection_lane === 'boundary' ? 'decision boundary' : 'calibration control'}
                  </StateBadge>
                </div>

                <div className="mt-3 grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
                  <div>
                    <div className="flex items-center gap-3">
                      <button
                        onClick={() => play(clip)}
                        className="flex h-9 w-9 flex-none items-center justify-center rounded-full border border-gray-300 text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                        title="Play this turn with four seconds of surrounding audio"
                      >
                        {playing === clip.review_key ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                      </button>
                      <div>
                        <div className="text-[11px] uppercase tracking-wide text-gray-400">Model claims</div>
                        <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">{clip.claimed_speaker}</div>
                      </div>
                    </div>

                    <div className="mt-3 overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
                      {clip.context.map((context, index) => (
                        <div
                          key={`${context.position}:${context.start}:${index}`}
                          className={`grid grid-cols-[7rem_minmax(0,1fr)] gap-3 px-3 py-2 text-sm ${
                            context.position === 'current'
                              ? 'bg-amber-50 text-gray-900 dark:bg-amber-950/20 dark:text-gray-100'
                              : 'bg-gray-50 text-gray-500 dark:bg-gray-900/30 dark:text-gray-400'
                          }`}
                        >
                          <span className="truncate font-medium">{context.speaker || 'Unknown'}</span>
                          <span>{context.text || '—'}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="min-w-[19rem] space-y-2">
                    <div className="flex flex-wrap gap-1.5">
                      {VERDICTS.map((item) => (
                        <button
                          key={item.verdict}
                          onClick={() => choose(clip, item.verdict)}
                          className={`rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors ${
                            decision?.verdict === item.verdict
                              ? 'border-blue-600 bg-blue-600 text-white'
                              : 'border-gray-300 text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700'
                          }`}
                        >
                          {item.label}
                        </button>
                      ))}
                    </div>
                    <Select
                      value={decision?.verdict === 'relabel' ? decision.correctedSpeaker || '' : ''}
                      onChange={(event) => relabel(clip, event.target.value)}
                      aria-label={`Correct speaker for ${clip.claimed_speaker}`}
                    >
                      <option value="">Choose another known speaker…</option>
                      {speakers.filter((speaker) => speaker !== clip.claimed_speaker).map((speaker) => (
                        <option key={speaker} value={speaker}>{speaker}</option>
                      ))}
                    </Select>
                    {decision && (
                      <div className="flex items-center gap-1 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                        <Check className="h-3.5 w-3.5" /> Verdict recorded for this batch
                      </div>
                    )}
                  </div>
                </div>
              </article>
            )
          })}
        </div>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 bg-gray-50 px-5 py-4 dark:border-gray-700 dark:bg-gray-900/30">
        <p className="text-xs text-gray-500">Undecided rows stay in the queue.</p>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={load} disabled={loading || submitting} icon={<RefreshCw className="h-4 w-4" />}>
            Reload
          </Button>
          <Button variant="primary" onClick={submit} disabled={!decidedCount || submitting} icon={submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Volume2 className="h-4 w-4" />}>
            {submitting ? 'Saving' : `Submit ${decidedCount} & next five`}
          </Button>
        </div>
      </footer>
    </section>
  )
}
