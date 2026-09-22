import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowUp,
  Check,
  AudioLines,
  Eraser,
  Loader2,
  Mic,
  Users,
  Pause,
  Play,
  RefreshCw,
  Search,
  Scissors,
  SkipForward,
  Trash2,
  X,
} from 'lucide-react'
import {
  BACKEND_URL,
  dataAuditApi,
  GuidedEnrollmentClip,
  GuidedEnrollmentGalleryResponse,
  GuidedEnrollmentSuggestResponse,
  GuidedEnrollmentSession,
  speakerApi,
  SpeakerBenchmarkReport,
  SpeakerGalleryBaseline,
} from '../../services/api'
import { getStorageKey } from '../../utils/storage'
import { formatClock } from './format'
import SpeakerInlineInput from '../SpeakerInlineInput'
import { Region, WaveformRegionEditor } from '../audio/WaveformRegionEditor'
import { useJobPolling } from '../../hooks/useJobPolling'
import { Button, StateBadge } from '../ui'

type EnrolledSpeaker = { speaker_id: string; name: string }
const PRIVATE_BENCHMARK_MESSAGE = 'This benchmark is held because its source privacy needs review. Rebuild it from allowed recordings.'

type Decision = {
  kind: 'accept' | 'reject' | 'skip' | 'bad_clip' | 'multiple_speakers' | 'another_speaker'
  actualSpeaker?: string
}

function submissionErrorMessage(error: any): string {
  const status = error?.response?.status
  const data = error?.response?.data
  const prefix = status ? `Submission failed (HTTP ${status})` : 'Submission failed'

  if (Array.isArray(data?.detail)) {
    const details = data.detail
      .slice(0, 3)
      .map((item: any) => {
        const location = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : ''
        return `${location ? `${location}: ` : ''}${item.msg || 'Invalid value'}`
      })
    const remaining = data.detail.length - details.length
    return `${prefix}: ${details.join('; ')}${remaining > 0 ? `; and ${remaining} more` : ''}`
  }
  if (typeof data?.detail === 'string') return `${prefix}: ${data.detail}`
  if (typeof data?.error === 'string') return `${prefix}: ${data.error}`
  if (typeof error?.message === 'string') return `${prefix}: ${error.message}`
  return prefix
}

function qualityDelta(before: any, after: any, novelty: number | null): string {
  if (!before || !after) return ''
  const cohesion =
    before.median_self != null && after.median_self != null
      ? `cohesion ${before.median_self.toFixed(3)} → ${after.median_self.toFixed(3)}`
      : 'cohesion unavailable'
  const outliers = `outliers ${before.n_flagged}/${before.n_clips} → ${after.n_flagged}/${after.n_clips}`
  const coverage = novelty != null ? `accepted novelty ${(novelty * 100).toFixed(0)}%` : null
  return ` · ${cohesion} · ${outliers}${coverage ? ` · ${coverage}` : ''}`
}

function EnrollmentTrend({ sessions }: { sessions: GuidedEnrollmentSession[] }) {
  const points = [...sessions]
    .reverse()
    .filter((session) => session.health_after?.median_self != null)
    .map((session) => ({
      clips: session.health_after!.n_clips,
      cohesion: session.health_after!.median_self!,
    }))
  if (points.length < 2) return null

  const width = 640
  const height = 150
  const pad = 28
  const minClips = Math.min(...points.map((point) => point.clips))
  const maxClips = Math.max(...points.map((point) => point.clips))
  const rawMin = Math.min(...points.map((point) => point.cohesion))
  const rawMax = Math.max(...points.map((point) => point.cohesion))
  const minCohesion = Math.max(0, rawMin - 0.015)
  const maxCohesion = Math.min(1, rawMax + 0.015)
  const x = (value: number) =>
    pad + ((value - minClips) / Math.max(1, maxClips - minClips)) * (width - pad * 2)
  const y = (value: number) =>
    height - pad - ((value - minCohesion) / Math.max(0.001, maxCohesion - minCohesion)) * (height - pad * 2)
  const line = points.map((point) => `${x(point.clips)},${y(point.cohesion)}`).join(' ')

  return (
    <div className="mb-4">
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <h3 className="text-xs font-medium text-gray-700 dark:text-gray-200">Observed gallery cohesion</h3>
        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
          <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
          Higher is better
        </span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-[150px] border border-gray-200 dark:border-gray-700 rounded bg-gray-50 dark:bg-gray-900/30" role="img" aria-label="Gallery cohesion by clip count">
        <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} stroke="currentColor" className="text-gray-300 dark:text-gray-600" />
        <line x1={pad} y1={pad} x2={pad} y2={height - pad} stroke="currentColor" className="text-gray-300 dark:text-gray-600" />
        <polyline points={line} fill="none" stroke="#c2551f" strokeWidth="2.5" />
        {points.map((point) => (
          <g key={`${point.clips}:${point.cohesion}`}>
            <circle cx={x(point.clips)} cy={y(point.cohesion)} r="4" fill="#c2551f" />
            <text x={x(point.clips)} y={y(point.cohesion) - 8} textAnchor="middle" fontSize="10" fill="currentColor">{point.cohesion.toFixed(3)}</text>
          </g>
        ))}
        <text x={width / 2} y={height - 7} textAnchor="middle" fontSize="10" fill="currentColor">gallery clips</text>
        <text x={pad} y={height - 12} textAnchor="middle" fontSize="10" fill="currentColor">{minClips}</text>
        <text x={width - pad} y={height - 12} textAnchor="middle" fontSize="10" fill="currentColor">{maxClips}</text>
      </svg>
    </div>
  )
}

export function BenchmarkPanel({ speakerName }: { speakerName: string }) {
  const [report, setReport] = useState<SpeakerBenchmarkReport | null>(null)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState('')
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null)
  const [baseline, setBaseline] = useState<SpeakerGalleryBaseline | null>(null)
  const { pollJob } = useJobPolling()

  const loadLatest = useCallback(async () => {
    try {
      const response = await dataAuditApi.getLatestSpeakerBenchmark()
      setReport(response.data.report)
      setBenchmarkError(null)
    } catch (error: any) {
      setReport(null)
      setBenchmarkError(error?.response?.status === 423
        ? PRIVATE_BENCHMARK_MESSAGE
        : 'Failed to load the latest benchmark')
      throw error
    }
  }, [])

  useEffect(() => {
    loadLatest().catch(() => undefined)
    dataAuditApi.getSpeakerGalleryBaseline().then((response) => setBaseline(response.data)).catch(() => undefined)
  }, [loadLatest])

  const run = async () => {
    setRunning(true)
    setBenchmarkError(null)
    setProgress('Queued')
    try {
      const response = await dataAuditApi.runSpeakerBenchmark()
      const status = await pollJob(response.data.job_id, (_status, batch) => {
        setProgress(batch?.message || _status)
      })
      if (status !== 'finished') throw new Error('Benchmark job failed; inspect Queue & Events for details')
      await loadLatest()
      setProgress('')
    } catch (error: any) {
      if (error?.response?.status === 423) {
        setReport(null)
        setBenchmarkError(PRIVATE_BENCHMARK_MESSAGE)
      } else setBenchmarkError(submissionErrorMessage(error))
    } finally {
      setRunning(false)
      setProgress('')
    }
  }

  const latest = report?.learning_curve[report.learning_curve.length - 1]
  const speakerBaseline = baseline?.speakers.find(
    (item) => item.name.toLocaleLowerCase() === speakerName.toLocaleLowerCase(),
  )

  return (
    <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div>
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{speakerName} gallery baseline</h2>
        {speakerBaseline ? (
          <div className="grid grid-cols-3 gap-3 mt-2 text-xs">
            <div><span className="text-gray-500">Clips</span><div className="font-semibold text-gray-900 dark:text-gray-100">{speakerBaseline.baseline.n_clips} → {speakerBaseline.current?.n_clips ?? '—'}</div></div>
            <div><span className="text-gray-500">Cohesion</span><div className="font-semibold text-gray-900 dark:text-gray-100">{speakerBaseline.baseline.median_self?.toFixed(3) ?? '—'} → {speakerBaseline.current?.median_self?.toFixed(3) ?? '—'}</div></div>
            <div><span className="text-gray-500">Outliers</span><div className="font-semibold text-gray-900 dark:text-gray-100">{speakerBaseline.baseline.n_flagged} → {speakerBaseline.current?.n_flagged ?? '—'}</div></div>
          </div>
        ) : (
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">No pre-enhancement baseline is available for this speaker.</p>
        )}
        {speakerBaseline && baseline?.cutoff && (
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">Baseline reconstructed from enrolled clips at {new Date(baseline.cutoff).toLocaleString()}</p>
        )}
      </div>

      <details className="border-t border-gray-200 dark:border-gray-700 pt-3">
        <summary className="cursor-pointer text-xs font-medium text-gray-700 dark:text-gray-200">
          Overall recognition benchmark{benchmarkError === PRIVATE_BENCHMARK_MESSAGE ? ' · Held for privacy review' : ''}{latest?.top1_accuracy_mean != null ? ` · Top-1 ${Math.round(latest.top1_accuracy_mean * 100)}%` : ''}{latest?.eer_mean != null ? ` · EER ${(latest.eer_mean * 100).toFixed(1)}%` : ''}
        </summary>
        <div className="space-y-3 pt-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-gray-500 dark:text-gray-400">Five folds grouped by conversation; cached embeddings; live galleries unchanged.</p>
            <Button
              variant="primary"
              onClick={run}
              disabled={running}
              icon={running ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            >
              {running ? 'Benchmarking' : report ? 'Run again' : 'Run benchmark'}
            </Button>
          </div>
          {progress && <p className="text-xs text-blue-700 dark:text-blue-300">{progress}</p>}
          {benchmarkError && <p className="text-xs text-red-600 dark:text-red-400">{benchmarkError}</p>}
          {report && (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
                <div><span className="text-gray-500">Top-1</span><div className="font-semibold text-gray-900 dark:text-gray-100">{latest?.top1_accuracy_mean != null ? `${Math.round(latest.top1_accuracy_mean * 100)}%` : 'Insufficient data'}</div></div>
                <div><span className="text-gray-500">Macro recall</span><div className="font-semibold text-gray-900 dark:text-gray-100">{latest?.macro_recall_mean != null ? `${Math.round(latest.macro_recall_mean * 100)}%` : '—'}</div></div>
                <div><span className="text-gray-500">False accepts</span><div className="font-semibold text-gray-900 dark:text-gray-100">{latest?.false_accept_rate_mean != null ? `${(latest.false_accept_rate_mean * 100).toFixed(1)}%` : '—'}</div></div>
                <div><span className="text-gray-500">EER</span><div className="font-semibold text-gray-900 dark:text-gray-100">{latest?.eer_mean != null ? `${(latest.eer_mean * 100).toFixed(1)}%` : '—'}</div></div>
                <div><span className="text-gray-500">Dataset</span><div className="font-semibold text-gray-900 dark:text-gray-100">{report.dataset.embedded_clips} clips · {report.dataset.speakers} speakers</div></div>
              </div>
              <div className="space-y-1.5">
                {report.learning_curve.map((point) => (
                  <div key={point.fraction} className="grid grid-cols-[3rem_1fr_3.5rem] items-center gap-2 text-xs">
                    <span className="text-gray-500">{Math.round(point.fraction * 100)}%</span>
                    <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden"><div className="h-full bg-blue-600" style={{ width: `${(point.top1_accuracy_mean || 0) * 100}%` }} /></div>
                    <span className="text-right font-mono text-gray-700 dark:text-gray-200">{point.top1_accuracy_mean != null ? `${Math.round(point.top1_accuracy_mean * 100)}%` : '—'}</span>
                  </div>
                ))}
              </div>
              <p className="text-[11px] text-gray-500 dark:text-gray-400">{report.protocol} · {report.conversation_groups} conversations · threshold {report.threshold.toFixed(2)} · {report.embedding_model} · {new Date(report.created_at).toLocaleString()}</p>
            </>
          )}
        </div>
      </details>
    </section>
  )
}

function flagBadge(clip: GuidedEnrollmentGalleryResponse['clips'][number]) {
  if (clip.flags.includes('mislabel'))
    return (
      <StateBadge tone="danger">
        sounds like {clip.suggested?.name || 'another speaker'}
      </StateBadge>
    )
  if (clip.flags.includes('junk'))
    return <StateBadge tone="danger">junk</StateBadge>
  if (clip.flags.includes('weak'))
    return <StateBadge tone="warning">weak match</StateBadge>
  return null
}

/**
 * The clips currently backing a speaker's voiceprint. Play each by ear, remove
 * bad ones (quarantined server-side, centroid recomputed), or clean the whole
 * profile: forget every review decision, session snapshot, and corpus match
 * recorded under the name — so after a mislabeled run the speaker can be
 * re-enrolled from scratch and previously reviewed clips are suggested again.
 */
function GallerySection({
  speakerName,
  refreshKey,
  onReset,
}: {
  speakerName: string
  refreshKey: number
  onReset: (galleryPurged: boolean, message: string) => void
}) {
  const [gallery, setGallery] = useState<GuidedEnrollmentGalleryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<number | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)
  const [playingId, setPlayingId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [confirmingReset, setConfirmingReset] = useState(false)
  const [purgeGallery, setPurgeGallery] = useState(false)
  const [resetting, setResetting] = useState(false)

  const token = () => localStorage.getItem(getStorageKey('token')) || ''

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setPendingDelete(null)
    try {
      const res = await dataAuditApi.getEnrollmentGallery(speakerName)
      setGallery(res.data)
    } catch (e: any) {
      setGallery(null)
      setError(e?.response?.data?.error || 'Failed to load enrolled clips')
    } finally {
      setLoading(false)
    }
  }, [speakerName])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  const stop = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingId(null)
  }, [])

  useEffect(() => stop, [stop])

  const play = (segmentId: number) => {
    if (playingId === segmentId) {
      stop()
      return
    }
    stop()
    const audio = new Audio(
      `${BACKEND_URL}/api/data-audit/enrollment/guided/gallery/segments/${segmentId}/audio?token=${token()}`
    )
    audioRef.current = audio
    setPlayingId(segmentId)
    audio.addEventListener('ended', () => stop())
    audio.addEventListener('error', () => stop())
    audio.play().catch(() => stop())
  }

  const removeClip = async (segmentId: number) => {
    setDeleting(segmentId)
    setError(null)
    try {
      if (playingId === segmentId) stop()
      await dataAuditApi.deleteEnrollmentGalleryClip(speakerName, segmentId)
      setPendingDelete(null)
      await load()
    } catch (e: any) {
      setError(submissionErrorMessage(e))
    } finally {
      setDeleting(null)
    }
  }

  const reset = async () => {
    setResetting(true)
    setError(null)
    try {
      const res = await dataAuditApi.resetGuidedEnrollment(speakerName, purgeGallery)
      const d = res.data.deleted
      setConfirmingReset(false)
      setPurgeGallery(false)
      onReset(
        res.data.gallery_deleted,
        `Cleaned ${speakerName}: forgot ${d.reviews} review decisions, ${d.sessions} sessions, ` +
          `${d.discovery_matches} corpus matches` +
          (res.data.gallery_deleted ? ' — voiceprint deleted from the speaker service' : '')
      )
      if (!res.data.gallery_deleted) await load()
    } catch (e: any) {
      setError(submissionErrorMessage(e))
    } finally {
      setResetting(false)
    }
  }

  return (
    <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          Enrolled clips
          {gallery && (
            <span className="ml-2 font-normal text-xs text-gray-500 dark:text-gray-400">
              {gallery.clips.length} clips
              {gallery.median_self != null && ` · cohesion ${gallery.median_self.toFixed(3)}`}
              {gallery.verdict && ` · ${gallery.verdict}`}
            </span>
          )}
        </h2>
        <button
          onClick={() => load()}
          disabled={loading}
          className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 disabled:opacity-50"
        >
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          Reload
        </button>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {gallery && gallery.clips.length === 0 && !loading && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          No per-clip records for this voiceprint. Older enrollments may need the
          segment backfill on the speaker service before they can be managed here.
        </p>
      )}

      {gallery && gallery.clips.length > 0 && (
        <ul className="space-y-1.5">
          {gallery.clips.map((clip) => (
            <li
              key={clip.segment_id}
              className="flex flex-wrap items-center gap-2 rounded border border-gray-200 dark:border-gray-700 px-3 py-2"
            >
              <button
                onClick={() => play(clip.segment_id)}
                className="shrink-0 p-1.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200"
                title="Play clip"
              >
                {playingId === clip.segment_id ? (
                  <Pause className="h-4 w-4" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
              </button>
              <span className="text-xs text-gray-700 dark:text-gray-200 truncate max-w-[16rem]" title={clip.filename}>
                {clip.filename}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400">{clip.duration.toFixed(1)}s</span>
              {clip.self_score != null && (
                <span
                  className="text-xs font-mono text-gray-600 dark:text-gray-300"
                  title="Similarity to the rest of this speaker's gallery"
                >
                  self {clip.self_score.toFixed(3)}
                </span>
              )}
              {flagBadge(clip)}
              <div className="ml-auto flex items-center gap-1.5">
                {pendingDelete === clip.segment_id ? (
                  <>
                    <button
                      onClick={() => removeClip(clip.segment_id)}
                      disabled={deleting === clip.segment_id}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-red-600 text-white disabled:opacity-50"
                    >
                      {deleting === clip.segment_id && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                      Remove from voiceprint
                    </button>
                    <button
                      onClick={() => setPendingDelete(null)}
                      className="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setPendingDelete(clip.segment_id)}
                    className="p-1.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400"
                    title="Remove this clip from the voiceprint (recoverable — audio is quarantined)"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
        {!confirmingReset ? (
          <button
            onClick={() => setConfirmingReset(true)}
            className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded border border-red-300 dark:border-red-800 text-red-700 dark:text-red-300"
          >
            <Eraser className="h-3.5 w-3.5" />
            Clean profile…
          </button>
        ) : (
          <div className="space-y-2 rounded border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/15 p-3">
            <p className="text-xs text-gray-700 dark:text-gray-200">
              Forget everything recorded for “{speakerName}”: review decisions, enrollment
              session history, and corpus-discovery matches. Previously reviewed clips become
              suggestible again — use this after a mislabeled run to start the profile over.
            </p>
            <label className="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-200">
              <input
                type="checkbox"
                checked={purgeGallery}
                onChange={(e) => setPurgeGallery(e.target.checked)}
              />
              Also delete the voiceprint and its enrollment audio from the speaker service
            </label>
            <div className="flex items-center gap-2">
              <button
                onClick={reset}
                disabled={resetting}
                className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded bg-red-600 text-white disabled:opacity-50"
              >
                {resetting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Clean profile
              </button>
              <button
                onClick={() => {
                  setConfirmingReset(false)
                  setPurgeGallery(false)
                }}
                className="text-xs px-2.5 py-1.5 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

/**
 * Guided speaker enrollment. Pick an enrolled speaker and the backend scans the
 * corpus for the clips whose confirmation would improve that speaker's
 * voiceprint the most (new acoustic conditions for the gallery, matches near
 * the decision boundary, longer clips — max 2 per conversation so enrollment
 * spans sessions). Review each clip by ear, accept/reject, submit, repeat.
 * Decisions are recorded server-side, so a decided clip is never re-suggested.
 */
export default function GuidedEnrollment() {
  const [speakers, setSpeakers] = useState<EnrolledSpeaker[]>([])
  const [speaker, setSpeaker] = useState('')
  const [speakerSearch, setSpeakerSearch] = useState('')

  const [suggestion, setSuggestion] = useState<GuidedEnrollmentSuggestResponse | null>(null)
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [discoveryProgress, setDiscoveryProgress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<string | null>(null)
  const [history, setHistory] = useState<GuidedEnrollmentSession[]>([])
  const [galleryVersion, setGalleryVersion] = useState(0)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [trimmedRegions, setTrimmedRegions] = useState<Record<string, Region>>({})

  const [playingKey, setPlayingKey] = useState<string | null>(null)
  const [playheadTime, setPlayheadTime] = useState<number | null>(null)
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [sortOrder, setSortOrder] = useState<'informative' | 'confidence'>('informative')
  const [mining, setMining] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const playheadFrameRef = useRef<number | null>(null)
  const miningInputRef = useRef<HTMLInputElement | null>(null)
  const selectedSpeakerRef = useRef('')
  const { pollJob } = useJobPolling()

  const token = () => localStorage.getItem(getStorageKey('token')) || ''
  const clipKey = (c: GuidedEnrollmentClip) => `${c.conversation_id}:${c.start}`

  useEffect(() => {
    if (speakers.length) return
    speakerApi
      .getEnrolledSpeakers()
      .then((res) => {
        const enrolled = (res.data.speakers || [])
          .filter((s: any) => s.name)
          .map((s: any) => ({ speaker_id: s.id || s.speaker_id || s.name, name: s.name }))
          .sort((a: EnrolledSpeaker, b: EnrolledSpeaker) => a.name.localeCompare(b.name))
        setSpeakers(enrolled)
      })
      .catch(() => setError('Failed to load enrolled speakers'))
  }, [speakers.length])

  const stopAudio = useCallback(() => {
    if (playheadFrameRef.current != null) cancelAnimationFrame(playheadFrameRef.current)
    playheadFrameRef.current = null
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingKey(null)
    setPlayheadTime(null)
  }, [])

  const trackPlayhead = (audio: HTMLAudioElement, absoluteStart: number) => {
    const update = () => {
      if (audioRef.current !== audio || audio.paused || audio.ended) return
      setPlayheadTime(absoluteStart + audio.currentTime)
      playheadFrameRef.current = requestAnimationFrame(update)
    }
    setPlayheadTime(absoluteStart)
    playheadFrameRef.current = requestAnimationFrame(update)
  }

  useEffect(() => stopAudio, [stopAudio])

  const loadHistory = useCallback(async (name: string) => {
    try {
      const res = await dataAuditApi.guidedEnrollmentHistory(name)
      setHistory(res.data.sessions)
    } catch {
      setHistory([])
    }
  }, [])

  const playClip = (clip: GuidedEnrollmentClip) => {
    const key = clipKey(clip)
    if (playingKey === key) {
      stopAudio()
      return
    }
    stopAudio()
    const region = trimmedRegions[key] || { start: clip.start, end: clip.end }
    const url =
      `${BACKEND_URL}/api/audio/chunks/${clip.conversation_id}` +
      `?start_time=${region.start.toFixed(2)}&end_time=${region.end.toFixed(2)}&format=wav&token=${token()}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingKey(key)
    audio.addEventListener('ended', () => stopAudio())
    audio.addEventListener('error', () => stopAudio())
    audio.play().then(() => trackPlayhead(audio, region.start)).catch(() => stopAudio())
  }

  const playRegion = (clip: GuidedEnrollmentClip, region: Region) => {
    const key = clipKey(clip)
    setTrimmedRegions((current) => ({ ...current, [key]: region }))
    stopAudio()
    const url =
      `${BACKEND_URL}/api/audio/chunks/${clip.conversation_id}` +
      `?start_time=${region.start.toFixed(2)}&end_time=${region.end.toFixed(2)}&format=wav&token=${token()}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingKey(key)
    audio.addEventListener('ended', () => stopAudio())
    audio.addEventListener('error', () => stopAudio())
    audio.play().then(() => trackPlayhead(audio, region.start)).catch(() => stopAudio())
  }

  const suggest = useCallback(
    async (name: string, order?: 'informative' | 'confidence') => {
      stopAudio()
      setLoading(true)
      setError(null)
      setSuggestion(null)
      setDecisions({})
      setTrimmedRegions({})
      setEditingKey(null)
      try {
        const res = await dataAuditApi.guidedEnrollmentSuggest(name, order ?? sortOrder)
        setSuggestion(res.data)
        if (!res.data.batch.length) {
          setLastResult(
            res.data.discovery_indexed
              ? 'The indexed corpus has no unreviewed clips that plausibly match this speaker.'
              : res.data.pool_remaining > 0
              ? 'No clips passed the information gate this round — the remaining pool is likely other speakers or already covered.'
              : 'No label-derived clips found. Search corpus speech to discover missed or incorrectly labeled clips.'
          )
        }
      } catch (e: any) {
        setError(e?.response?.data?.error || 'Failed to fetch suggestions')
      } finally {
        setLoading(false)
      }
    },
    [stopAudio, sortOrder]
  )

  const submit = async () => {
    if (!suggestion) return
    const decided = suggestion.batch.filter((c) => decisions[clipKey(c)])
    if (!decided.length) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await dataAuditApi.guidedEnrollmentDecide(
        suggestion.speaker.speaker_name,
        decided.map((c) => ({
          conversation_id: c.conversation_id,
          start: (trimmedRegions[clipKey(c)] || c).start,
          end: (trimmedRegions[clipKey(c)] || c).end,
          original_start: c.start,
          original_end: c.end,
          decision: decisions[clipKey(c)].kind,
          actual_speaker: decisions[clipKey(c)].actualSpeaker,
          scores: c.scores,
        }))
      )
      const g = res.data.speaker
      setLastResult(
        `Enrolled ${res.data.enrolled}, rejected ${res.data.rejected}, bad clips ${res.data.bad_clips}, multiple speakers ${res.data.multiple_speakers}, skipped ${res.data.skipped}` +
          (res.data.errors.length
            ? `, ${res.data.errors.length} failed: ${res.data.errors
                .slice(0, 2)
                .map((item) => item.error)
                .join('; ')}`
            : '') +
          (res.data.health_before && res.data.health_after
            ? ` — gallery ${res.data.health_before.n_clips} → ${res.data.health_after.n_clips} clips`
            : g?.n_clips != null
              ? ` — gallery now ${g.n_clips} clips`
              : '') +
          qualityDelta(
            res.data.health_before,
            res.data.health_after,
            res.data.coverage.accepted_novelty_mean
          ) +
          (res.data.benchmark_job_id ? ' · cross-validation queued' : '')
      )
      // Every decided clip is now excluded server-side; fetch the next batch.
      setGalleryVersion((v) => v + 1)
      await suggest(suggestion.speaker.speaker_name)
      await loadHistory(suggestion.speaker.speaker_name)
      if (res.data.discovery_job_id) {
        setDiscoveryProgress('Updating corpus matches for the new gallery')
        void followDiscoveryJob(
          suggestion.speaker.speaker_name,
          res.data.discovery_job_id,
        )
      }
    } catch (e: any) {
      setError(submissionErrorMessage(e))
      setSubmitting(false)
      return
    }
    setSubmitting(false)
  }

  const freshBatch = async () => {
    if (!suggestion) return
    setSubmitting(true)
    setError(null)
    try {
      await dataAuditApi.guidedEnrollmentDecide(
        suggestion.speaker.speaker_name,
        suggestion.batch.map((c) => ({
          conversation_id: c.conversation_id,
          start: (trimmedRegions[clipKey(c)] || c).start,
          end: (trimmedRegions[clipKey(c)] || c).end,
          original_start: c.start,
          original_end: c.end,
          decision: decisions[clipKey(c)]?.kind || 'skip',
          actual_speaker: decisions[clipKey(c)]?.actualSpeaker,
          scores: c.scores,
        }))
      )
      await suggest(suggestion.speaker.speaker_name)
    } catch (e: any) {
      setError(submissionErrorMessage(e))
    } finally {
      setSubmitting(false)
    }
  }

  const followDiscoveryJob = async (name: string, jobId: string) => {
    setDiscovering(true)
    setError(null)
    try {
      const status = await pollJob(jobId, (_status, batch) => {
        setDiscoveryProgress(batch?.message || _status)
      })
      if (status !== 'finished') throw new Error('Corpus discovery failed; inspect Queue & Events for details')
      setDiscoveryProgress('')
      if (selectedSpeakerRef.current === name) await suggest(name)
    } catch (e: any) {
      setError(submissionErrorMessage(e))
    } finally {
      setDiscovering(false)
    }
  }

  const discoverCorpus = async (name: string) => {
    setDiscoveryProgress('Queued')
    try {
      const response = await dataAuditApi.discoverSpeakerCorpus(name, includeDeleted)
      await followDiscoveryJob(name, response.data.job_id)
    } catch (e: any) {
      setError(submissionErrorMessage(e))
      setDiscovering(false)
    }
  }

  const mineFiles = async (name: string, files: File[]) => {
    if (!files.length) return
    setMining(true)
    setError(null)
    setDiscoveryProgress(`Uploading ${files.length} file(s) for mining…`)
    try {
      const res = await dataAuditApi.mineCorpusAudio(name, files)
      const d = res.data
      const failNote = d.failed.length ? `, ${d.failed.length} failed` : ''
      if (!d.transcription_available) {
        setError(
          `Ingested ${d.ingested} file(s)${failNote}, but no batch transcription provider is ` +
            'configured — mined audio cannot be segmented until transcription runs.'
        )
        setDiscoveryProgress('')
        return
      }
      setLastResult(
        `Mining ${d.ingested} file(s) for ${name}${failNote} — transcribing, then scanning for matches.`
      )
      if (d.discovery_job_id) {
        setDiscoveryProgress('Waiting for transcription, then scanning corpus')
        void followDiscoveryJob(name, d.discovery_job_id)
      } else {
        setDiscoveryProgress('')
      }
    } catch (e: any) {
      setError(submissionErrorMessage(e))
      setDiscoveryProgress('')
    } finally {
      setMining(false)
      if (miningInputRef.current) miningInputRef.current.value = ''
    }
  }

  const attachOrCreateDiscovery = async (name: string) => {
    try {
      const response = await dataAuditApi.getSpeakerCorpusDiscovery(name)
      const { job_id: jobId, status, matched_segments: matchedSegments } = response.data
      if (jobId && ['queued', 'started', 'deferred', 'scheduled'].includes(status || '')) {
        setDiscoveryProgress('Reattaching to corpus search')
        await followDiscoveryJob(name, jobId)
      } else if (matchedSegments === 0) {
        await discoverCorpus(name)
      }
    } catch (e: any) {
      setError(submissionErrorMessage(e))
    }
  }

  const decidedCount = suggestion
    ? suggestion.batch.filter((c) => decisions[clipKey(c)]).length
    : 0

  return (
    <div className="space-y-5">
      <section className="space-y-3">
        <div className="flex items-center gap-2">
          <Mic className="h-5 w-5 text-blue-600" />
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Speaker enrollment</h1>
        </div>
        <div className="max-w-md">
          <SpeakerInlineInput
            value={speakerSearch}
            onChange={setSpeakerSearch}
            onSelect={(name) => {
              selectedSpeakerRef.current = name
              setSpeaker(name)
              setSpeakerSearch(name)
              suggest(name)
              loadHistory(name)
              attachOrCreateDiscovery(name)
            }}
            enrolledSpeakers={speakers}
            placeholder="Search for a speaker to enhance..."
            allowCreate={false}
          />
        </div>
        {!speaker && lastResult && (
          <p className="text-sm text-green-700 dark:text-green-400">{lastResult}</p>
        )}
      </section>

      {speaker && (
        <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              onClick={() => speaker && suggest(speaker)}
              disabled={!speaker || loading || submitting}
              icon={
                loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )
              }
            >
              Fresh batch
            </Button>
            <select
              value={sortOrder}
              onChange={(e) => {
                const next = e.target.value as 'informative' | 'confidence'
                setSortOrder(next)
                if (speaker) suggest(speaker, next)
              }}
              disabled={loading || submitting}
              className="text-sm px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 disabled:opacity-50"
              title="Most informative surfaces clips the model learns most from (often uncertain, lower-similarity ones); best match first surfaces the highest-similarity clips"
            >
              <option value="informative">Sort: most informative</option>
              <option value="confidence">Sort: best match first</option>
            </select>
            <Button
              variant="secondary"
              onClick={() => discoverCorpus(speaker)}
              disabled={discovering}
              title="Refresh the reusable speech-embedding index and rescore it against this gallery"
              icon={discovering ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            >
              {discovering ? 'Searching corpus' : 'Refresh corpus'}
            </Button>
            <label
              className="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300"
              title="Also scan speech from soft-deleted conversations whose audio is still stored"
            >
              <input
                type="checkbox"
                checked={includeDeleted}
                onChange={(e) => setIncludeDeleted(e.target.checked)}
              />
              include deleted
            </label>
            <input
              ref={miningInputRef}
              type="file"
              accept="audio/*,video/*,.wav,.mp3,.m4a,.ogg,.opus,.flac"
              multiple
              className="hidden"
              onChange={(e) => mineFiles(speaker, Array.from(e.target.files || []))}
            />
            <Button
              variant="secondary"
              onClick={() => miningInputRef.current?.click()}
              disabled={mining || discovering}
              title="Upload unlabelled audio (recordings, exports) and mine it for this speaker's voice. Files are kept out of memory processing."
              icon={mining ? <Loader2 className="h-4 w-4 animate-spin" /> : <AudioLines className="h-4 w-4" />}
            >
              {mining ? 'Uploading…' : 'Mine audio files'}
            </Button>
            {suggestion && (
              <span className="text-xs text-gray-500 dark:text-gray-400">
                gallery: {suggestion.speaker.n_clips ?? '?'} clips
                {suggestion.speaker.total_duration_s != null &&
                  ` / ${Math.round(suggestion.speaker.total_duration_s)}s`}
                {' · '}
                {suggestion.reviewed_total} reviewed · ~{suggestion.pool_remaining} in pool
                {suggestion.discovery_indexed && ` · ${suggestion.discovery_candidates} indexed segments`}
              </span>
            )}
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}
          {lastResult && !loading && (
            <p className="text-sm text-green-700 dark:text-green-400">{lastResult}</p>
          )}
          {loading && (
            <p className="text-sm text-gray-500">
              Scanning the corpus and scoring candidate clips…
            </p>
          )}
          {discoveryProgress && (
            <p className="text-xs text-blue-700 dark:text-blue-300">{discoveryProgress}</p>
          )}

          {suggestion && suggestion.batch.length > 0 && (
            <>
              <ul className="space-y-2">
                {suggestion.batch.map((clip) => {
                  const key = clipKey(clip)
                  const decision = decisions[key]
                  const region = trimmedRegions[key] || { start: clip.start, end: clip.end }
                  return (
                    <li
                      key={key}
                      className={`rounded border p-3 space-y-1.5 ${
                        decision
                          ? decision.kind === 'accept'
                            ? 'border-green-400 bg-green-50 dark:bg-green-900/20'
                            : decision.kind === 'another_speaker'
                            ? 'border-purple-400 bg-purple-50 dark:bg-purple-900/20'
                            : decision.kind === 'reject'
                            ? 'border-red-300 bg-red-50 dark:bg-red-900/20'
                            : 'border-gray-300 bg-gray-50 dark:border-gray-600 dark:bg-gray-800'
                          : 'border-gray-200 dark:border-gray-700'
                      }`}
                    >
                      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <button
                            onClick={() => playClip(clip)}
                            className="shrink-0 p-1.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200"
                            title="Play clip"
                          >
                            {playingKey === key ? (
                              <Pause className="h-4 w-4" />
                            ) : (
                              <Play className="h-4 w-4" />
                            )}
                          </button>
                          <span className="shrink-0 text-xs text-gray-500 dark:text-gray-400">
                            {formatClock(region.start)}–{formatClock(region.end)} ·{' '}
                            {(region.end - region.start).toFixed(1)}s
                            {trimmedRegions[key] && ' · trimmed'}
                          </span>
                          <span className="truncate text-xs text-gray-500 dark:text-gray-400">
                            {clip.conversation_title || clip.conversation_id.slice(0, 8)}
                            {clip.conversation_date && ` · ${clip.conversation_date.slice(0, 10)}`}
                          </span>
                        </div>
                        <div className="flex items-center justify-end gap-2 sm:ml-auto">
                          <span className="text-xs font-mono text-gray-600 dark:text-gray-300">
                            match {clip.scores.sim_centroid.toFixed(2)}
                          </span>
                          <div className="flex gap-1">
                          <button
                            onClick={() => setEditingKey(editingKey === key ? null : key)}
                            className={`p-1.5 rounded ${editingKey === key ? 'bg-emerald-600 text-white' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'}`}
                            title="Trim this enrollment clip"
                          >
                            <Scissors className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() =>
                              setDecisions((d) => ({ ...d, [key]: { kind: 'accept' } }))
                            }
                            className={`p-1.5 rounded ${
                              decision?.kind === 'accept'
                                ? 'bg-green-600 text-white'
                                : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
                            }`}
                            title={`Yes — this is ${suggestion.speaker.speaker_name}`}
                          >
                            <Check className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() =>
                              setDecisions((d) => ({ ...d, [key]: { kind: 'reject' } }))
                            }
                            className={`p-1.5 rounded ${
                              decision?.kind === 'reject'
                                ? 'bg-red-600 text-white'
                                : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
                            }`}
                            title="No — not this speaker"
                          >
                            <X className="h-4 w-4" />
                          </button>
                          </div>
                        </div>
                      </div>
                      {editingKey === key && clip.conversation_duration > 0 && (
                        <WaveformRegionEditor
                          conversationId={clip.conversation_id}
                          duration={clip.conversation_duration}
                          initialRegion={region}
                          pickerMode
                          onChange={(next) => {
                            if (next) setTrimmedRegions((current) => ({ ...current, [key]: next }))
                          }}
                          onCancel={() => setEditingKey(null)}
                          onPlay={(next) => playRegion(clip, next)}
                          playheadTime={playingKey === key ? playheadTime : null}
                          height={88}
                        />
                      )}
                      <div className="flex flex-wrap items-center gap-2 sm:pl-9">
                        <span className="w-full text-xs text-gray-500 dark:text-gray-400 sm:w-auto">Actually:</span>
                        <div className="w-full sm:w-52">
                          <SpeakerInlineInput
                            value={decision?.actualSpeaker || ''}
                            onChange={(name) => setDecisions((d) => ({ ...d, [key]: { kind: 'another_speaker', actualSpeaker: name } }))}
                            onSelect={(name) => setDecisions((d) => ({ ...d, [key]: { kind: 'another_speaker', actualSpeaker: name } }))}
                            enrolledSpeakers={speakers.filter((item) => item.name !== speaker)}
                            placeholder="Search speakers..."
                            allowCreate={false}
                          />
                        </div>
                        {decision?.kind === 'another_speaker' && decision.actualSpeaker && (
                          <span className="text-xs px-2 py-0.5 rounded bg-purple-600 text-white">
                            relabel → {decision.actualSpeaker}
                          </span>
                        )}
                        <button onClick={() => setDecisions((d) => ({ ...d, [key]: { kind: 'skip' } }))} className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${decision?.kind === 'skip' ? 'bg-gray-600 text-white' : 'border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'}`}><SkipForward className="h-3.5 w-3.5" />Skip</button>
                        <button onClick={() => setDecisions((d) => ({ ...d, [key]: { kind: 'multiple_speakers' } }))} className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${decision?.kind === 'multiple_speakers' ? 'bg-amber-600 text-white' : 'border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'}`} title="Several people are talking in this clip; do not enroll or label it"><Users className="h-3.5 w-3.5" />Multiple speakers</button>
                        <button onClick={() => setDecisions((d) => ({ ...d, [key]: { kind: 'bad_clip' } }))} className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${decision?.kind === 'bad_clip' ? 'bg-amber-600 text-white' : 'border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'}`} title="Poor enrollment audio, such as mostly noise or badly bounded speech"><AudioLines className="h-3.5 w-3.5" />Bad clip</button>
                      </div>
                      {clip.text && (
                        <p className="text-sm text-gray-800 dark:text-gray-200 line-clamp-2">
                          “{clip.text}”
                        </p>
                      )}
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {clip.current_label
                          ? `currently labeled ${clip.current_label}`
                          : 'currently unlabeled'}
                        {clip.reasons.length > 0 && ` · ${clip.reasons.join(' · ')}`}
                      </p>
                    </li>
                  )
                })}
              </ul>
              <Button
                variant="primary"
                onClick={submit}
                disabled={submitting || decidedCount === 0}
                icon={submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
              >
                Submit {decidedCount}/{suggestion.batch.length} & next batch
              </Button>
              <Button
                variant="secondary"
                onClick={freshBatch}
                disabled={submitting}
                className="ml-2"
                icon={<RefreshCw className="h-4 w-4" />}
              >
                Skip remaining & fresh batch
              </Button>
            </>
          )}

          <GallerySection
            speakerName={speaker}
            refreshKey={galleryVersion}
            onReset={(galleryPurged, message) => {
              setLastResult(message)
              setError(null)
              if (galleryPurged) {
                stopAudio()
                selectedSpeakerRef.current = ''
                setSpeaker('')
                setSpeakerSearch('')
                setSuggestion(null)
                setDecisions({})
                setHistory([])
                setSpeakers([]) // triggers a refetch of the enrolled-speaker list
              } else {
                suggest(speaker)
                loadHistory(speaker)
              }
            }}
          />

          {suggestion && <BenchmarkPanel speakerName={speaker} />}

          {history.length > 0 && (
            <section className="border-t border-gray-200 dark:border-gray-700 pt-4">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-2">Enrollment history</h2>
              <div className="grid sm:grid-cols-3 gap-2 mb-4 text-xs">
                <div className="border border-gray-200 dark:border-gray-700 rounded p-2">
                  <div className="text-gray-500 dark:text-gray-400">Clean speech quantity</div>
                  <div className="font-medium text-gray-900 dark:text-gray-100">
                    {(suggestion?.speaker.total_duration_s ?? 0) >= 60 ? 'Sufficient' : 'Still building'}
                    {suggestion?.speaker.total_duration_s != null && ` · ${Math.round(suggestion.speaker.total_duration_s)}s`}
                  </div>
                  <div className="text-gray-500 dark:text-gray-400">Research gains usually flatten after 30–60s</div>
                </div>
                <div className="border border-gray-200 dark:border-gray-700 rounded p-2">
                  <div className="text-gray-500 dark:text-gray-400">Gallery cleanliness</div>
                  <div className="font-medium text-gray-900 dark:text-gray-100">{history[0].health_after?.n_flagged ?? '—'} outliers</div>
                  <div className="text-gray-500 dark:text-gray-400">Cohesion {history[0].health_after?.median_self?.toFixed(3) ?? 'unavailable'}</div>
                </div>
                <div className="border border-gray-200 dark:border-gray-700 rounded p-2">
                  <div className="text-gray-500 dark:text-gray-400">Recognition performance</div>
                  <div className="font-medium text-amber-700 dark:text-amber-300">Not benchmarked</div>
                  <div className="text-gray-500 dark:text-gray-400">Requires held-out identification clips</div>
                </div>
              </div>
              <EnrollmentTrend sessions={history} />
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <tr><th className="py-2 pr-3">Date</th><th className="py-2 pr-3">Added</th><th className="py-2 pr-3">Gallery</th><th className="py-2 pr-3">Cohesion</th><th className="py-2 pr-3">Outliers</th><th className="py-2">Novelty</th></tr>
                  </thead>
                  <tbody>
                    {history.map((session, index) => {
                      const before = session.health_before
                      const after = session.health_after
                      return (
                        <tr key={`${session.created_at}:${index}`} className="border-b border-gray-100 dark:border-gray-700/60 text-gray-700 dark:text-gray-200">
                          <td className="py-2 pr-3 whitespace-nowrap">{new Date(session.created_at).toLocaleString()}</td>
                          <td className="py-2 pr-3">{session.decisions.enrolled}</td>
                          <td className="py-2 pr-3 whitespace-nowrap">{before && after ? `${before.n_clips} → ${after.n_clips}` : after?.n_clips ?? '—'}</td>
                          <td className="py-2 pr-3 whitespace-nowrap">{before?.median_self != null && after?.median_self != null ? `${before.median_self.toFixed(3)} → ${after.median_self.toFixed(3)}` : '—'}</td>
                          <td className="py-2 pr-3 whitespace-nowrap">{before && after ? `${before.n_flagged}/${before.n_clips} → ${after.n_flagged}/${after.n_clips}` : '—'}</td>
                          <td className="py-2">{session.coverage.accepted_novelty_mean != null ? `${Math.round(session.coverage.accepted_novelty_mean * 100)}%` : '—'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </section>
      )}
    </div>
  )
}
