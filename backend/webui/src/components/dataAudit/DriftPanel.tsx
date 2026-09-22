import { useCallback, useState } from 'react'
import { ChevronDown, ChevronRight, DatabaseZap, RefreshCw, Waypoints } from 'lucide-react'
import { conversationsApi, dataAuditApi } from '../../services/api'

interface Transition { from: string | null; to: string | null; count: number }
interface DriftConversation {
  conversation_id: string
  title: string
  speech_segments: number
  drifted_segments: number
  transitions: Transition[]
  processed_at: string | null
}
interface DriftReport {
  drifted: DriftConversation[]
  total_drifted: number
  conversations_scanned: number
  no_centroid_data: number
  not_analyzable: number
  similarity_threshold: number | null
  cached?: boolean
  computed_at?: string
}
interface BackfillResult {
  backfilled: number
  skipped: number
  failed: number
  marked_unanalyzable: number
}

const lbl = (s: string | null) => s || 'Unknown'

/**
 * Identify "drift conversations" — past conversations whose speaker labels would change
 * if reprocessed against the CURRENT voiceprint gallery (e.g. after cleaning enrollment).
 *
 * Re-identifies each conversation's stored per-cluster centroids vs the live gallery
 * (pure vector math, no GPU) and ranks by how many segment labels would flip. Lets you
 * reprocess just the drifted ones. Lazy: fetches only when first expanded.
 */
export default function DriftPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<DriftReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [scanProgress, setScanProgress] = useState<{ percent: number; message: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [reprocessed, setReprocessed] = useState<Set<string>>(new Set())
  const [backfilling, setBackfilling] = useState(false)
  const [backfillProgress, setBackfillProgress] = useState<string | null>(null)
  const [backfillResult, setBackfillResult] = useState<BackfillResult | null>(null)

  const load = useCallback(async (force = false) => {
    setLoading(true)
    setScanProgress(null)
    setError(null)
    try {
      const queued = await conversationsApi.scanDrift(force)
      if (queued.data.report) {
        // Cache hit — nothing the report depends on has changed since last scan.
        setData(queued.data.report as DriftReport)
        setLoading(false)
        return
      }
      const jobId = queued.data.job_id as string
      while (true) {
        const statusResponse = await dataAuditApi.getJobStatus(jobId)
        const { status, batch_progress: progress } = statusResponse.data
        if (progress?.message) {
          setScanProgress({ percent: progress.percent ?? 0, message: progress.message })
        } else if (status === 'queued') {
          setScanProgress({ percent: 0, message: 'Waiting for a worker…' })
        }
        if (status === 'finished') {
          const resultResponse = await dataAuditApi.getJobResult<DriftReport>(jobId)
          setData(resultResponse.data.result)
          break
        }
        if (status === 'failed' || status === 'canceled' || status === 'stopped') {
          throw new Error(`Drift scan ${status}`)
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
      }
    } catch {
      setError('Failed to load drift analysis')
    } finally {
      setLoading(false)
      setScanProgress(null)
    }
  }, [])

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && !data && !loading) load()
  }

  const reprocess = async (id: string) => {
    setBusy(id)
    try {
      await conversationsApi.reprocessSpeakers(id)
      setReprocessed((prev) => new Set(prev).add(id))
    } catch {
      setError(`Failed to queue reprocess for ${id.slice(0, 8)}`)
    } finally {
      setBusy(null)
    }
  }

  const backfill = async () => {
    setBackfilling(true)
    setBackfillProgress('Queueing cluster-embedding backfill…')
    setBackfillResult(null)
    setError(null)
    try {
      const queued = await conversationsApi.backfillDriftClusterEmbeddings()
      const jobId = queued.data.job_id
      while (true) {
        const statusResponse = await dataAuditApi.getJobStatus(jobId)
        const { status, batch_progress: progress } = statusResponse.data
        setBackfillProgress(
          progress?.message || (status === 'queued' ? 'Waiting for a worker…' : 'Backfilling…')
        )
        if (status === 'finished') {
          const resultResponse = await dataAuditApi.getJobResult<BackfillResult>(jobId)
          setBackfillResult(resultResponse.data.result)
          await load()
          break
        }
        if (status === 'failed' || status === 'canceled' || status === 'stopped') {
          throw new Error(`Backfill job ${status}`)
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1500))
      }
    } catch {
      setError('Cluster-embedding backfill failed')
    } finally {
      setBackfilling(false)
      setBackfillProgress(null)
    }
  }

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        onClick={toggle}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left"
      >
        <div className="flex items-center space-x-2">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <Waypoints className="h-4 w-4 text-blue-600" />
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
            Identify drift conversations
          </span>
        </div>
        {data && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {data.total_drifted} would change
            {data.no_centroid_data > 0 && ` · ${data.no_centroid_data} need backfill`}
          </span>
        )}
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-200 dark:border-gray-700 pt-3">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Conversations whose speaker labels would change if reprocessed against the current
            voiceprints — run this after cleaning enrollment, then reprocess the ones that drifted.
          </p>
          <div className="flex items-center gap-3">
            <button
              onClick={() => load(true)}
              disabled={loading}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
              {loading ? 'Analyzing…' : 'Re-analyze'}
            </button>
            {loading && scanProgress && (
              <div className="flex items-center gap-2 flex-1 min-w-0">
                <div className="w-32 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-blue-600 rounded-full transition-all"
                    style={{ width: `${scanProgress.percent}%` }}
                  />
                </div>
                <span className="text-xs text-gray-500 truncate">{scanProgress.message}</span>
              </div>
            )}
            {!loading && data && (
              <span className="text-xs text-gray-500">
                scanned {data.conversations_scanned}
                {data.similarity_threshold != null && ` · threshold ${data.similarity_threshold.toFixed(2)}`}
                {data.cached && data.computed_at &&
                  ` · cached from ${new Date(data.computed_at).toLocaleString()}`}
              </span>
            )}
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          {data && data.no_centroid_data > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-amber-600 dark:text-amber-400">
              <span>
                {data.no_centroid_data} conversations have no stored cluster embeddings yet and
                can't be analyzed.
              </span>
              <button
                onClick={backfill}
                disabled={backfilling}
                className="inline-flex items-center gap-1.5 rounded bg-amber-100 px-2.5 py-1 font-medium text-amber-800 hover:bg-amber-200 disabled:opacity-50 dark:bg-amber-900/40 dark:text-amber-200 dark:hover:bg-amber-900/60"
              >
                <DatabaseZap className={`h-3 w-3 ${backfilling ? 'animate-pulse' : ''}`} />
                {backfilling ? 'Backfilling…' : `Backfill ${data.no_centroid_data}`}
              </button>
              {backfillProgress && <span>{backfillProgress}</span>}
            </div>
          )}

          {data && data.not_analyzable > 0 && (
            <p className="text-[11px] text-gray-400 dark:text-gray-500">
              {data.not_analyzable} conversations excluded — no usable speech audio
              (silent or audio not stored).
            </p>
          )}

          {backfillResult && (
            <p className={`text-[11px] ${backfillResult.failed ? 'text-amber-600 dark:text-amber-400' : 'text-green-600 dark:text-green-400'}`}>
              Backfill complete: {backfillResult.backfilled} added, {backfillResult.skipped} skipped,
              {' '}{backfillResult.failed} failed
              {backfillResult.marked_unanalyzable > 0 &&
                `, ${backfillResult.marked_unanalyzable} marked not-analyzable`}
              . Drift analysis refreshed.
            </p>
          )}

          {data &&
            data.total_drifted === 0 &&
            data.conversations_scanned > data.no_centroid_data + data.not_analyzable &&
            !loading && (
              <p className="text-sm text-green-600 dark:text-green-400">
                No drift — every analyzable conversation still matches the current gallery.
              </p>
            )}

          {data && data.total_drifted > 0 && (
            <div className="overflow-x-auto">
              <table className="min-w-full text-xs">
                <thead className="text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <tr>
                    <th className="text-left py-1 pr-3">Conversation</th>
                    <th className="text-right px-2">drifted / speech</th>
                    <th className="text-left px-3">changes</th>
                    <th className="text-right pl-2">action</th>
                  </tr>
                </thead>
                <tbody>
                  {data.drifted.map((c) => (
                    <tr key={c.conversation_id} className="border-b border-gray-100 dark:border-gray-800 align-top">
                      <td className="py-1.5 pr-3">
                        <a
                          href={`/data-audit/recordings/${c.conversation_id}`}
                          className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                        >
                          {c.title}
                        </a>
                        {c.processed_at && (
                          <div className="text-[10px] text-gray-400">
                            processed {new Date(c.processed_at).toLocaleDateString()}
                          </div>
                        )}
                      </td>
                      <td className="text-right px-2 tabular-nums">
                        <b className="text-orange-600 dark:text-orange-400">{c.drifted_segments}</b>
                        <span className="text-gray-400"> / {c.speech_segments}</span>
                      </td>
                      <td className="px-3">
                        {c.transitions.slice(0, 4).map((t, i) => (
                          <span key={i} className="inline-block mr-2 whitespace-nowrap">
                            <span className="text-gray-500">{lbl(t.from)}</span>
                            <span className="text-gray-400"> → </span>
                            <span className="text-gray-800 dark:text-gray-200">{lbl(t.to)}</span>
                            <span className="text-gray-400"> ×{t.count}</span>
                          </span>
                        ))}
                        {c.transitions.length > 4 && (
                          <span className="text-gray-400">+{c.transitions.length - 4} more</span>
                        )}
                      </td>
                      <td className="text-right pl-2">
                        {reprocessed.has(c.conversation_id) ? (
                          <span className="text-green-600 dark:text-green-400">queued ✓</span>
                        ) : (
                          <button
                            onClick={() => reprocess(c.conversation_id)}
                            disabled={busy === c.conversation_id}
                            className="px-2 py-1 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                          >
                            {busy === c.conversation_id ? '…' : 'Reprocess'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
