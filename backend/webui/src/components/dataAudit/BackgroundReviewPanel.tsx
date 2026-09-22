import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, Pause, Play, Radio } from 'lucide-react'
import { BACKEND_URL, BackgroundAccuracyReport, BackgroundCleanupReport, BackgroundCluster, BackgroundClusterLane, BackgroundDecisionHistoryItem, BackgroundSurface, BatchProgress, dataAuditApi } from '../../services/api'
import { useJobPolling } from '../../hooks/useJobPolling'
import { getStorageKey } from '../../utils/storage'
import { TITLE_NOT_GENERATED } from '../../lib/constants'
import { formatClock } from './format'

type Decision = 'noise' | 'background_speech' | 'not_background'
type ReviewDecision = Decision | 'mixed' | 'dismissed'

// Which mining lane to draw review clusters from. "harvest" clips sit near a
// confirmed background exemplar (batch-confirmable); "novel" clips resemble
// nothing labelled yet — new sources, the highest-value annotations for
// cross-source recall.
const LANE_OPTIONS: Array<{ value: 'all' | BackgroundClusterLane; label: string; hint: string }> = [
  { value: 'all', label: 'All', hint: 'Default order: near-certain matches first' },
  { value: 'harvest', label: 'Near matches', hint: 'Close to a confirmed background exemplar — quick batch confirms' },
  { value: 'novel', label: 'New sources', hint: 'Unlike everything labelled so far — these teach the system the most' },
  { value: 'similar', label: 'Clusters', hint: 'Repeated within-conversation audio without a strong match either way' },
]

// Review dial: widens/narrows the surfacing thresholds server-side. Only
// changes what is offered for review — production suppression stays at its
// own (benchmark-tuned) thresholds.
const SURFACE_OPTIONS: Array<{ value: BackgroundSurface; label: string; hint: string }> = [
  { value: 'less', label: 'Show less', hint: 'Only near-certain candidates and larger clusters' },
  { value: 'default', label: 'Default', hint: 'Balanced queue — the production default' },
  { value: 'more', label: 'Show more', hint: 'Wider thresholds and smaller clusters — more borderline material to review' },
]

const decisionLabel = (decision: ReviewDecision | 'skip') => ({
  noise: 'noise',
  // Source-first vocabulary: the question is WHAT produced the audio
  // (content vs a real person in the room), not its role in one conversation.
  background_speech: 'content (media)',
  not_background: 'real people',
  mixed: 'mixed clip labels',
  dismissed: 'dismissed cluster',
  skip: 'skipped',
}[decision])

export default function BackgroundReviewPanel() {
  const [open, setOpen] = useState(false)
  const [clusters, setClusters] = useState<BackgroundCluster[]>([])
  const [indexed, setIndexed] = useState(0)
  const [remaining, setRemaining] = useState(0)
  const [bucketSizes, setBucketSizes] = useState({ noise: 0, background_speech: 0 })
  const [reviewFocus, setReviewFocus] = useState<'bootstrap' | 'hard_speech' | 'discovery'>('bootstrap')
  const [progress, setProgress] = useState<BatchProgress | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [deciding, setDeciding] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const [decisionHistory, setDecisionHistory] = useState<BackgroundDecisionHistoryItem[]>([])
  const [showDecisionHistory, setShowDecisionHistory] = useState(false)
  const [expandedDecisionId, setExpandedDecisionId] = useState<string | null>(null)
  const [mixedMode, setMixedMode] = useState(false)
  const [sampleDecisions, setSampleDecisions] = useState<Record<string, Decision>>({})
  const [lastResult, setLastResult] = useState<{
    reviewId: string
    reviewed: number
    exemplarsAdded: number
    duplicatesCovered: number
    decision: ReviewDecision
  } | null>(null)
  const [report, setReport] = useState<BackgroundCleanupReport | null>(null)
  const [accuracyReport, setAccuracyReport] = useState<BackgroundAccuracyReport | null>(null)
  const [reportVisible, setReportVisible] = useState(false)
  const [reportLoading, setReportLoading] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<{ conversations_updated: number; segments_changed: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [playingKey, setPlayingKey] = useState<string | null>(null)
  const [lane, setLane] = useState<'all' | BackgroundClusterLane>('all')
  const [laneCounts, setLaneCounts] = useState<Partial<Record<BackgroundClusterLane, number>>>({})
  const [surface, setSurface] = useState<BackgroundSurface>(() => {
    const stored = localStorage.getItem(getStorageKey('bgReviewSurface'))
    return stored === 'less' || stored === 'more' ? stored : 'default'
  })
  const [queueSummary, setQueueSummary] = useState<{ unreviewed: number; quick_confirms: number; uncertain: number } | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const { pollJob } = useJobPolling()

  const loadClusters = useCallback(async () => {
    const response = await dataAuditApi.getBackgroundClusters(6, 5, lane === 'all' ? undefined : lane, surface)
    setClusters(response.data.clusters)
    setIndexed(response.data.indexed)
    setRemaining(response.data.remaining)
    setBucketSizes(response.data.bucket_sizes)
    setReviewFocus(response.data.review_focus || 'bootstrap')
    setLaneCounts(response.data.lane_counts || {})
    setQueueSummary(response.data.queue_summary || null)
  }, [lane, surface])

  const loadDecisionHistory = useCallback(async () => {
    const response = await dataAuditApi.getBackgroundDecisionHistory()
    setDecisionHistory(response.data.decisions)
  }, [])

  const watchJob = useCallback(async (jobId: string) => {
    setIndexing(true)
    const result = await pollJob(jobId, (_status, nextProgress) => {
      if (nextProgress) setProgress(nextProgress)
    })
    setIndexing(false)
    if (result === 'finished') {
      setProgress(null)
      await loadClusters()
    } else {
      setError('Corpus sampling failed')
    }
  }, [loadClusters, pollJob])

  useEffect(() => {
    if (!open) return
    void loadDecisionHistory().catch(() => undefined)
    dataAuditApi.getLatestBackgroundDecision().then(({ data }) => {
      if (!data.review_id || !data.decision) return
      setLastResult({
        reviewId: data.review_id,
        reviewed: data.reviewed || 0,
        exemplarsAdded: 0,
        duplicatesCovered: 0,
        decision: data.decision === 'skip' ? 'not_background' : data.decision,
      })
    }).catch(() => undefined)
    dataAuditApi.getBackgroundIndex().then(({ data }) => {
      if (data.job_id && ['queued', 'started', 'deferred', 'scheduled'].includes(data.status || '')) {
        void watchJob(data.job_id)
      } else if (data.indexed > 0) {
        void loadClusters()
      }
    }).catch(() => setError('Could not load background corpus status'))
  }, [loadClusters, loadDecisionHistory, open, watchJob])

  const startIndex = async () => {
    setError(null)
    setProgress({ percent: 0, message: 'Preparing corpus…' })
    try {
      const response = await dataAuditApi.startBackgroundIndex()
      await watchJob(response.data.job_id)
    } catch {
      setIndexing(false)
      setError('Could not start corpus sampling')
    }
  }

  const stopAudio = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingKey(null)
  }, [])

  const play = (sample: { clip_key: string; conversation_id: string; start: number; end: number }) => {
    if (playingKey === sample.clip_key) return stopAudio()
    stopAudio()
    const token = localStorage.getItem(getStorageKey('token')) || ''
    const url = `${BACKEND_URL}/api/audio/chunks/${sample.conversation_id}?start_time=${sample.start.toFixed(2)}&end_time=${sample.end.toFixed(2)}&format=wav&token=${token}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingKey(sample.clip_key)
    audio.addEventListener('ended', stopAudio)
    audio.addEventListener('error', stopAudio)
    void audio.play().catch(stopAudio)
  }

  const decide = async (cluster: BackgroundCluster, decision: ReviewDecision, clipDecisions: Record<string, Decision> = {}) => {
    setDeciding(true)
    setError(null)
    stopAudio()
    try {
      const response = await dataAuditApi.decideBackgroundCluster(cluster, decision, clipDecisions)
      setLastResult({
        reviewId: response.data.review_id,
        reviewed: response.data.reviewed,
        exemplarsAdded: response.data.exemplars_added,
        duplicatesCovered: response.data.duplicates_covered,
        decision: response.data.decision,
      })
      setMixedMode(false)
      setSampleDecisions({})
      await loadDecisionHistory()
      await loadClusters()
    } catch {
      setError('Could not save this cluster decision')
    } finally {
      setDeciding(false)
    }
  }

  const skipForNow = () => {
    stopAudio()
    setLastResult(null)
    setMixedMode(false)
    setSampleDecisions({})
    setClusters((current) => current.length > 1 ? [...current.slice(1), current[0]] : current)
  }

  const editDecision = async (reviewId: string, decision: ReviewDecision) => {
    if (decision === 'mixed') return
    setUndoing(true)
    setError(null)
    try {
      await dataAuditApi.editBackgroundDecision(reviewId, decision)
      setAccuracyReport(null)
      setReport(null)
      setReportVisible(false)
      await loadDecisionHistory()
      await loadClusters()
    } catch {
      setError('Could not change this annotation')
    } finally {
      setUndoing(false)
    }
  }

  const removeDecision = async (reviewId: string) => {
    setUndoing(true)
    setError(null)
    try {
      await dataAuditApi.undoBackgroundDecision(reviewId)
      const latest = await dataAuditApi.getLatestBackgroundDecision()
      if (latest.data.review_id && latest.data.decision) {
        setLastResult({
          reviewId: latest.data.review_id,
          reviewed: latest.data.reviewed || 0,
          exemplarsAdded: 0,
          duplicatesCovered: 0,
          decision: latest.data.decision === 'skip' ? 'not_background' : latest.data.decision,
        })
      } else setLastResult(null)
      setAccuracyReport(null)
      setReport(null)
      setReportVisible(false)
      await loadDecisionHistory()
      await loadClusters()
    } catch {
      setError('Could not remove this annotation')
    } finally {
      setUndoing(false)
    }
  }

  const evaluateAnnotations = async () => {
    setReportLoading(true)
    setError(null)
    try {
      const [accuracy, cleanup] = await Promise.all([
        dataAuditApi.getBackgroundAccuracyReport(),
        dataAuditApi.getBackgroundCleanupReport(),
      ])
      setAccuracyReport(accuracy.data)
      setReport(cleanup.data)
      setReportVisible(true)
    } catch {
      setError('Could not evaluate background annotations')
    } finally {
      setReportLoading(false)
    }
  }

  const applyCleanup = async () => {
    if (!report?.report_id) return
    setApplying(true)
    setError(null)
    try {
      const response = await dataAuditApi.applyBackgroundCleanup(report.report_id)
      const terminal = await pollJob(response.data.job_id, (_status, nextProgress) => {
        if (nextProgress) setProgress(nextProgress)
      })
      if (terminal !== 'finished') throw new Error('cleanup failed')
      const result = await dataAuditApi.getJobResult<{ conversations_updated: number; segments_changed: number }>(response.data.job_id)
      if (result.data.result) setApplyResult(result.data.result)
    } catch {
      setError('Could not apply background cleanup')
    } finally {
      setApplying(false)
      setProgress(null)
    }
  }

  const cluster = clusters[0]
  const percent = progress?.percent ?? 0

  return (
    <section className="mb-4 rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
      <button onClick={() => setOpen((value) => !value)} className="flex w-full items-center justify-between px-4 py-3 text-left">
        <span className="flex items-center gap-2 font-medium text-gray-800 dark:text-gray-100">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <Radio className="h-4 w-4 text-gray-500" />
          Background sample corpus
        </span>
        {indexed > 0 && <span className="text-xs text-gray-400">{remaining} clips left · {bucketSizes.noise} noise · {bucketSizes.background_speech} background speech</span>}
      </button>

      {open && <div className="border-t border-gray-100 px-4 pb-4 pt-3 dark:border-gray-700">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-200">Review similar audio together.</p>
            <p className="mt-1 text-xs text-gray-400">Content is played media; real people are speaking live.</p>
          </div>
          <div className="flex items-center gap-2">
            {decisionHistory.length > 0 && <button onClick={() => setShowDecisionHistory((value) => !value)} className="rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700">Annotation history ({decisionHistory.length})</button>}
            <button onClick={startIndex} disabled={indexing} className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700">
              {indexing && <Loader2 className="h-4 w-4 animate-spin" />}
              {indexing ? 'Sampling full corpus…' : indexed ? 'Update corpus sample' : 'Sample full corpus'}
            </button>
          </div>
        </div>

        {indexing && <div className="mb-4" aria-live="polite">
          <div className="mb-1 flex justify-between text-xs text-gray-500"><span>{progress?.message || 'Sampling corpus…'}</span><span>{percent}%</span></div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700"><div className="h-full bg-gray-500 transition-[width] duration-200" style={{ width: `${percent}%` }} /></div>
        </div>}
        {error && <p className="mb-3 text-xs text-red-500">{error}</p>}
        {lastResult && <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400" aria-live="polite">
          <span>{lastResult.decision === 'dismissed' ? `Dismissed a ${lastResult.reviewed}-clip cluster` : `Reviewed ${lastResult.reviewed} clips as ${decisionLabel(lastResult.decision)}`}
            {lastResult.exemplarsAdded > 0 ? ` · added ${lastResult.exemplarsAdded} representative samples` : ''}
            {lastResult.duplicatesCovered > 0 ? ` · covered ${lastResult.duplicatesCovered} duplicate clips` : ''}.
          </span>
        </div>}
        {decisionHistory.length > 0 && <div className="mb-3">
          {showDecisionHistory && <ul className="mt-2 max-h-96 divide-y divide-gray-100 overflow-y-auto border-y border-gray-100 dark:divide-gray-700 dark:border-gray-700">
            {decisionHistory.map((item) => <li key={item.review_id} className="py-1 text-xs">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setExpandedDecisionId((current) => current === item.review_id ? null : item.review_id)}
                  aria-expanded={expandedDecisionId === item.review_id}
                  className="flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  {expandedDecisionId === item.review_id ? <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-gray-400" /> : <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-gray-400" />}
                  <span className="w-32 flex-shrink-0 font-medium text-gray-600 dark:text-gray-300">{decisionLabel(item.decision)}</span>
                  <span className="min-w-0 flex-1 truncate text-gray-400">{item.clips_affected} clips · {new Date(item.reviewed_at).toLocaleString()}</span>
                </button>
                {item.decision !== 'mixed' && <select
                  value={item.decision}
                  disabled={undoing}
                  onChange={(e) => { if (e.target.value !== item.decision) editDecision(item.review_id, e.target.value as ReviewDecision) }}
                  title="Change this verdict — references move to match"
                  className="appearance-none rounded border border-gray-200 bg-transparent px-1.5 py-0.5 text-xs text-gray-600 disabled:opacity-50 [color-scheme:light] dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:[color-scheme:dark]"
                >
                  <option value="background_speech">content (media)</option>
                  <option value="not_background">real people</option>
                  <option value="noise">noise</option>
                  <option value="dismissed">dismissed</option>
                </select>}
                <button onClick={() => removeDecision(item.review_id)} disabled={undoing} className="px-2 py-1 font-medium text-gray-600 hover:text-red-600 disabled:opacity-50 dark:text-gray-300">Remove</button>
              </div>
              {expandedDecisionId === item.review_id && <div className="mb-2 ml-6 rounded border border-gray-100 bg-gray-50/60 dark:border-gray-700 dark:bg-gray-900/20">
                {item.samples.map((sample) => <div key={sample.clip_key} className="flex items-start gap-2 border-b border-gray-100 px-2 py-2 last:border-b-0 dark:border-gray-700">
                  <button onClick={() => play(sample)} aria-label={playingKey === sample.clip_key ? 'Pause clip' : 'Play clip'} className="mt-0.5 rounded p-1 hover:bg-gray-200 dark:hover:bg-gray-700">
                    {playingKey === sample.clip_key ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 text-gray-400">
                      <span className="font-medium text-gray-600 dark:text-gray-300">{sample.conversation_title || TITLE_NOT_GENERATED}</span>
                      <span className="tabular-nums">{formatClock(sample.start)}–{formatClock(sample.end)}</span>
                      <span>{sample.current_label || 'Unknown speaker'}</span>
                      {sample.decision && <span>labelled {decisionLabel(sample.decision)}</span>}
                    </div>
                    <p className="mt-0.5 text-gray-600 dark:text-gray-300">{sample.text || 'No decipherable speech'}</p>
                  </div>
                </div>)}
                {item.samples.length === 0 && <p className="px-3 py-2 text-gray-400">The source clips are no longer in the sampled corpus.</p>}
                {item.samples_reconstructed && item.samples.length > 0 && <p className="border-t border-gray-100 px-3 py-1.5 text-[11px] text-gray-400 dark:border-gray-700">Representative clips reconstructed from the stored cluster.</p>}
              </div>}
            </li>)}
          </ul>}
        </div>}

        {!indexing && indexed > 0 && <div className="mb-4 border-y border-gray-100 py-3 dark:border-gray-700">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-gray-800 dark:text-gray-100">Foreground / background accuracy</p>
              <p className="mt-0.5 text-xs text-gray-400">Measure whether your annotations improved held-out F1, then preview transcript impact.</p>
            </div>
            <div className="flex items-center gap-2">
              {(report || accuracyReport) && (
                <button
                  onClick={() => setReportVisible((visible) => !visible)}
                  disabled={reportLoading || applying}
                  className="rounded px-3 py-1.5 text-sm text-gray-500 hover:bg-gray-50 hover:text-gray-700 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                >
                  {reportVisible ? 'Hide report' : 'Show report'}
                </button>
              )}
              <button onClick={evaluateAnnotations} disabled={reportLoading || applying} className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700">
                {reportLoading && <Loader2 className="h-4 w-4 animate-spin" />}
                {reportLoading ? 'Evaluating…' : report || accuracyReport ? 'Refresh report' : 'Evaluate annotations'}
              </button>
            </div>
          </div>
          {reportVisible && accuracyReport && !accuracyReport.ready && <p className="mt-3 text-xs text-gray-500">{accuracyReport.reason}</p>}
          {reportVisible && accuracyReport?.ready && accuracyReport.baseline && accuracyReport.adapted && <div className="mt-3">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{(accuracyReport.baseline.f1 * 100).toFixed(1)}%</div><div className="text-xs text-gray-400">baseline F1</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{(accuracyReport.adapted.f1 * 100).toFixed(1)}%</div><div className="text-xs text-gray-400">adapted F1</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{(Number(accuracyReport.f1_change || 0) * 100) >= 0 ? '+' : ''}{(Number(accuracyReport.f1_change || 0) * 100).toFixed(1)} pts</div><div className="text-xs text-gray-400">F1 change</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{accuracyReport.reviewed_samples}</div><div className="text-xs text-gray-400">reviewed samples</div></div>
            </div>
            <div className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
              <div className="rounded border border-gray-200 p-2 dark:border-gray-700"><span className="text-gray-500">Baseline confusion</span><div className="mt-1 tabular-nums text-gray-700 dark:text-gray-200">TP {accuracyReport.baseline.confusion.tp} · FP {accuracyReport.baseline.confusion.fp} · FN {accuracyReport.baseline.confusion.fn} · TN {accuracyReport.baseline.confusion.tn}</div></div>
              <div className="rounded border border-gray-200 p-2 dark:border-gray-700"><span className="text-gray-500">Adapted confusion</span><div className="mt-1 tabular-nums text-gray-700 dark:text-gray-200">TP {accuracyReport.adapted.confusion.tp} · FP {accuracyReport.adapted.confusion.fp} · FN {accuracyReport.adapted.confusion.fn} · TN {accuracyReport.adapted.confusion.tn}</div></div>
            </div>
            {(accuracyReport.learning_curve?.length || 0) > 1 && <div className="mt-3">
              <div className="mb-1 flex justify-between text-xs text-gray-400"><span>F1 learning curve</span><span>0 → {accuracyReport.learning_curve!.length - 1} cluster annotations</span></div>
              <div className="flex h-16 items-end gap-1 border-b border-gray-200 dark:border-gray-700" role="img" aria-label="F1 score after each annotation iteration">
                {accuracyReport.learning_curve?.map((point) => <div key={point.annotations} className="min-w-1 flex-1 bg-gray-400 dark:bg-gray-500" style={{ height: `${Math.max(2, point.f1 * 100)}%` }} title={`${point.annotations} annotations: ${(point.f1 * 100).toFixed(1)}% F1 on ${point.samples} held-out samples`} />)}
              </div>
            </div>}
            <p className="mt-2 text-xs text-gray-400">{accuracyReport.method}; duplicate clips are excluded across folds.{accuracyReport.reconstructed_review_samples ? ' Older reviewed samples were reconstructed from their clusters.' : ''}</p>
            {!accuracyReport.background_speech_samples && <p className="mt-2 text-xs font-medium text-gray-600 dark:text-gray-300">Hard case not measured: no Background Speech positives were reviewed. This 100% result only measures transcript gaps versus speech, not TV/distant speech versus foreground.</p>}
          </div>}
          {reportVisible && report && !report.ready && <p className="mt-3 text-xs text-gray-500">{report.reason}</p>}
          {reportVisible && report?.ready && <div className="mt-3">
            <p className="mb-2 text-xs font-medium text-gray-600 dark:text-gray-300">Projected transcript impact</p>
            {report.recommendation && <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">{report.recommendation}</p>}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{report.high_confidence}</div><div className="text-xs text-gray-400">high-confidence changes</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{report.ambiguous}</div><div className="text-xs text-gray-400">left unchanged</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{report.conversations_affected}</div><div className="text-xs text-gray-400">conversations affected</div></div>
              <div><div className="text-lg tabular-nums text-gray-900 dark:text-white">{report.reference_counts?.noise || 0} / {report.reference_counts?.background_speech || 0}</div><div className="text-xs text-gray-400">noise / background references</div></div>
            </div>
            {(report.high_samples?.length || 0) > 0 && <ul className="mt-3 divide-y divide-gray-100 border-y border-gray-100 dark:divide-gray-700 dark:border-gray-700">
              {report.high_samples?.slice(0, 5).map((sample) => <li key={sample.clip_key} className="flex items-center gap-3 py-2 text-xs">
                <button onClick={() => play(sample)} className="rounded p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700">{playingKey === sample.clip_key ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}</button>
                <span className="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-300">{sample.text}</span>
                <span className="text-gray-400">{sample.current_label || 'Unknown'} → {sample.proposed_label}</span>
                <span className="w-12 text-right tabular-nums text-gray-400">{Math.round(sample.background_score * 100)}%</span>
              </li>)}
            </ul>}
            <div className="mt-3 flex items-center justify-between gap-3">
              <p className="text-xs text-gray-400">Only high-confidence changes are applied. Original transcript versions remain available.</p>
              <button onClick={applyCleanup} disabled={applying || !report.high_confidence} className="inline-flex flex-shrink-0 items-center gap-1.5 rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-gray-700 dark:text-gray-100">
                {applying && <Loader2 className="h-4 w-4 animate-spin" />}{applying ? 'Applying…' : `Apply ${report.high_confidence} changes`}
              </button>
            </div>
            {applying && progress && <p className="mt-2 text-xs text-gray-400">{progress.message}</p>}
            {applyResult && <p className="mt-2 text-xs text-gray-500">Applied {applyResult.segments_changed} segment changes across {applyResult.conversations_updated} conversations. Memories were not reprocessed.</p>}
          </div>}
        </div>}

        {!indexing && !cluster && indexed === 0 && <div className="rounded border border-dashed border-gray-200 px-3 py-7 text-center text-sm text-gray-400 dark:border-gray-700">Sample the corpus to find repeated background audio.</div>}

        {!indexing && indexed > 0 && queueSummary && queueSummary.unreviewed > 0 && <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
          {queueSummary.unreviewed} cluster{queueSummary.unreviewed === 1 ? '' : 's'} unreviewed ·{' '}
          <span title="The system already believes these are background — reviewing is a one-click sign-off, not a judgment call">{queueSummary.quick_confirms} quick confirm{queueSummary.quick_confirms === 1 ? '' : 's'}</span> ·{' '}
          <span className="font-medium text-gray-600 dark:text-gray-300" title="Genuinely uncertain — this is the number that shrinks as the system learns">{queueSummary.uncertain} genuinely uncertain</span>
        </p>}
        {!indexing && indexed > 0 && <div className="mb-3 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-gray-400">Queue:</span>
          {LANE_OPTIONS.map((option) => {
            const count = option.value === 'all'
              ? Object.values(laneCounts).reduce((sum, n) => sum + (n || 0), 0)
              : laneCounts[option.value] || 0
            return <button
              key={option.value}
              onClick={() => { stopAudio(); setMixedMode(false); setSampleDecisions({}); setLane(option.value) }}
              title={option.hint}
              className={`rounded-full border px-2.5 py-1 ${lane === option.value ? 'border-gray-700 bg-gray-700 text-white dark:border-gray-300 dark:bg-gray-300 dark:text-gray-900' : 'border-gray-200 text-gray-500 hover:border-gray-400 dark:border-gray-600 dark:text-gray-300 dark:hover:border-gray-400'}`}
            >{option.label}{count ? ` (${count})` : ''}</button>
          })}
          <span className="ml-3 text-gray-400">Show:</span>
          {SURFACE_OPTIONS.map((option) => <button
            key={option.value}
            onClick={() => {
              stopAudio(); setMixedMode(false); setSampleDecisions({})
              localStorage.setItem(getStorageKey('bgReviewSurface'), option.value)
              setSurface(option.value)
            }}
            title={option.hint}
            className={`rounded-full border px-2.5 py-1 ${surface === option.value ? 'border-gray-700 bg-gray-700 text-white dark:border-gray-300 dark:bg-gray-300 dark:text-gray-900' : 'border-gray-200 text-gray-500 hover:border-gray-400 dark:border-gray-600 dark:text-gray-300 dark:hover:border-gray-400'}`}
          >{option.label}</button>)}
        </div>}

        {!indexing && !cluster && indexed > 0 && lane !== 'all' && <div className="rounded border border-dashed border-gray-200 px-3 py-7 text-center text-sm text-gray-500 dark:border-gray-700">Nothing waiting in this queue right now — switch back to All.</div>}
        {!indexing && !cluster && indexed > 0 && lane === 'all' && <div className="rounded border border-dashed border-gray-200 px-3 py-7 text-center text-sm text-gray-500 dark:border-gray-700">No reviewable clusters remain{surface !== 'more' ? ' at this setting — try Show more, or update' : '. Update'} the corpus sample after adding conversations.</div>}

        {cluster && <div className="border-t border-gray-100 pt-3 dark:border-gray-700">
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <div>
              <span className="text-sm font-medium text-gray-800 dark:text-gray-100">{reviewFocus === 'discovery' ? 'Review a likely background candidate' : reviewFocus === 'hard_speech' ? 'Review a hard speech boundary' : 'Review this cluster'}</span>
              {cluster.mined === 'harvest' && <span className="ml-2 rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300" title="Close to a confirmed background exemplar">near match</span>}
              {cluster.mined === 'novel' && <span className="ml-2 rounded bg-sky-50 px-1.5 py-0.5 text-[11px] text-sky-700 dark:bg-sky-950/50 dark:text-sky-300" title="Unlike everything labelled so far — a first verdict here teaches the system the most">new source</span>}
              <span className="ml-2 text-xs text-gray-400">{cluster.size} similar clips in {cluster.conversation_title || cluster.conversation_id.slice(0, 8)}</span>
            </div>
            <span className="text-xs text-gray-400">{clusters.length} clusters ready</span>
          </div>
          <ul className="mb-3 divide-y divide-gray-100 border-y border-gray-100 dark:divide-gray-700 dark:border-gray-700">
            {cluster.samples.map((sample) => <li key={sample.clip_key} className="flex flex-wrap items-center gap-3 py-2 text-xs">
              <button onClick={() => play(sample)} className="rounded p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700" title="Play sample">{playingKey === sample.clip_key ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}</button>
              <span className="w-24 flex-shrink-0 tabular-nums text-gray-400">{formatClock(sample.start)}–{formatClock(sample.end)}</span>
              <span className="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-300">{sample.text || <em className="text-gray-400">No transcript</em>}</span>
              {sample.review_role === 'edge' && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500 dark:bg-gray-700 dark:text-gray-300" title="One of the least similar clips in this cluster">Edge case</span>}
              {sample.current_label && <span className="max-w-32 truncate text-gray-400">currently {sample.current_label}</span>}
              {mixedMode && <div className="ml-9 flex w-full flex-wrap gap-1.5 sm:ml-0 sm:w-auto">
                {(['noise', 'background_speech', 'not_background'] as Decision[]).map((choice) => <button
                  key={choice}
                  onClick={() => setSampleDecisions((current) => ({ ...current, [sample.clip_key]: choice }))}
                  className={`rounded border px-2 py-1 text-[11px] ${sampleDecisions[sample.clip_key] === choice ? 'border-gray-700 bg-gray-700 text-white dark:border-gray-200 dark:bg-gray-200 dark:text-gray-900' : 'border-gray-200 text-gray-500 hover:border-gray-400 dark:border-gray-600 dark:text-gray-300'}`}
                >{choice === 'background_speech' ? 'Content (media)' : choice === 'not_background' ? 'Real people' : 'Noise'}</button>)}
              </div>}
            </li>)}
          </ul>
          {reviewFocus === 'discovery' && <p className="mb-3 text-xs text-gray-400">Suggested because this cluster is acoustically unlike your confirmed foreground{(cluster.mean_background_similarity || 0) > 0 ? ' and resembles confirmed background speech' : ''}.</p>}
          <div className="flex flex-wrap items-center gap-2">
            {!mixedMode && <>
              {cluster.candidate_type === 'noise' && <button disabled={deciding} onClick={() => decide(cluster, 'noise')} className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-gray-700 dark:text-gray-100">Noise</button>}
              {cluster.candidate_type === 'background_speech' && <button disabled={deciding} onClick={() => decide(cluster, 'background_speech')} className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-gray-700 dark:text-gray-100">Content (media)</button>}
              <button disabled={deciding} onClick={() => decide(cluster, 'not_background')} className="px-2 py-1.5 text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50 dark:text-gray-300 dark:hover:text-white">Real people</button>
              {/* STT hallucinates words onto taps/clatter, so "speech" clusters
                  still need a whole-cluster noise verdict. */}
              {cluster.candidate_type === 'background_speech' && <button disabled={deciding} onClick={() => decide(cluster, 'noise')} className="px-2 py-1.5 text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50 dark:text-gray-300 dark:hover:text-white" title="Not speech at all — taps, clatter, hallucinated transcript">Noise</button>}
              <button disabled={deciding} onClick={() => setMixedMode(true)} className="px-2 py-1.5 text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50 dark:text-gray-300 dark:hover:text-white">Mixed clips</button>
              <button disabled={deciding} onClick={() => decide(cluster, 'dismissed')} className="px-2 py-1.5 text-sm text-gray-400 hover:text-gray-700 disabled:opacity-50 dark:hover:text-gray-200" title="Persistently remove this poor-quality cluster without using it for training">Dismiss cluster</button>
              <button disabled={deciding || clusters.length < 2} onClick={skipForNow} className="px-2 py-1.5 text-sm text-gray-400 hover:text-gray-700 disabled:opacity-40 dark:hover:text-gray-200" title={clusters.length < 2 ? 'No other cluster is ready' : 'Show another cluster without recording a decision'}>Show later</button>
              <span className="ml-auto text-xs text-gray-400">Cluster labels propagate to all {cluster.size} similar clips</span>
            </>}
            {mixedMode && <>
              <button
                disabled={deciding || cluster.samples.some((sample) => !sampleDecisions[sample.clip_key])}
                onClick={() => decide(cluster, 'mixed', sampleDecisions)}
                className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-40 dark:bg-gray-700 dark:text-gray-100"
              >Save {Object.keys(sampleDecisions).length} clip labels</button>
              <button disabled={deciding} onClick={() => { setMixedMode(false); setSampleDecisions({}) }} className="px-2 py-1.5 text-sm text-gray-500">Cancel</button>
              <span className="ml-auto text-xs text-gray-400">Label every shown clip; unseen members will be reclustered</span>
            </>}
          </div>
        </div>}
      </div>}
    </section>
  )
}
