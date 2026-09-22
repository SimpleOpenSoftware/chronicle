import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Zap, RefreshCw, AlertTriangle, Play, Mic, FileAudio, Sparkles, Clock,
  CheckCircle2, CircleDashed, ArrowUpRight,
} from 'lucide-react'
import { useFinetuningStatus, useCronJobs, useRunCronJob, useDeleteOrphanedAnnotations, useRetryFailedAnnotations, useDeleteFailedAnnotations } from '../hooks/useFinetuning'
import { useExternalServices } from '../hooks/useSystem'
import { useAuth } from '../contexts/AuthContext'
import { sourceDate } from '../utils/sourceTime'
import { Button, Alert } from '../components/ui'

interface AnnotationTypeCounts {
  total: number
  pending: number
  applied: number
  trained: number
  orphaned: number
  failed: number
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return 'No recorded run'
  return sourceDate(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })
}

// One card per model we can teach. Speaker recognition is instant kNN enrollment
// (managed in Data Audit); ASR and prompts are real batch training triggered here.
interface ModelTarget {
  key: 'speaker' | 'asr' | 'prompts'
  label: string
  blurb: string
  icon: any
  /** Applied correction counts; these are not model-readiness or deployment evidence. */
  correctionTypes: string[]
  /** null = no batch trigger here (link out instead). */
  cronJobId: string | null
  runVerb: string
}

const MODEL_TARGETS: ModelTarget[] = [
  {
    key: 'speaker',
    label: 'Speaker recognition',
    blurb: 'Review speaker corrections for voice enrollment.',
    icon: Mic,
    correctionTypes: ['diarization'],
    cronJobId: null, // instant kNN — enrolled deliberately in Data Audit
    runVerb: 'Enroll',
  },
  {
    key: 'asr',
    label: 'ASR training',
    blurb: 'Export corrected transcripts and speaker labels to the configured ASR service.',
    icon: FileAudio,
    correctionTypes: ['transcript', 'diarization'],
    cronJobId: 'asr_finetuning',
    runVerb: 'Run ASR export',
  },
  {
    key: 'prompts',
    label: 'LLM prompts',
    blurb: 'Use title and memory corrections in prompt optimization.',
    icon: Sparkles,
    correctionTypes: ['title', 'memory'],
    cronJobId: 'prompt_optimization',
    runVerb: 'Optimize prompts',
  },
]

const TYPE_LABEL: Record<string, string> = {
  diarization: 'speaker relabels',
  transcript: 'transcript corrections',
  speech_suggestion_correction: 'suggestion-correction triples',
  timing: 'timing edits',
  insert: 'inserts',
  deletion: 'deletions',
  title: 'title edits',
  memory: 'memory edits',
}

export default function Finetuning() {
  const { isAdmin } = useAuth()
  const { data: externalServices } = useExternalServices(isAdmin, false)
  const { data: status = null, isLoading: statusLoading, error: statusError, refetch: refetchStatus } = useFinetuningStatus()
  const { data: cronJobs = [], isLoading: cronLoading, error: cronError, refetch: refetchCron } = useCronJobs()
  const runJob = useRunCronJob()
  const retryFailed = useRetryFailedAnnotations()
  const deleteFailed = useDeleteFailedAnnotations()
  const deleteOrphaned = useDeleteOrphanedAnnotations()

  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)
  const [runningKey, setRunningKey] = useState<string | null>(null)
  const [cleaningType, setCleaningType] = useState<string | null>(null)

  const loading = statusLoading || cronLoading
  const counts = (status?.annotation_counts || {}) as Record<string, AnnotationTypeCounts>

  const cronFor = (jobId: string | null) => (jobId ? cronJobs.find((j: any) => j.job_id === jobId) : undefined)

  const handleRun = async (t: ModelTarget) => {
    if (!t.cronJobId) return
    setError(null)
    setSuccessMessage(null)
    setRunningKey(t.key)
    try {
      const data = await runJob.mutateAsync(t.cronJobId)
      if (data.error) setError(`${t.label}: ${data.error}`)
      else if (data.message) setSuccessMessage(`${t.label}: ${data.message}`)
      else setSuccessMessage(`${t.label}: job completed. Check the run result in Queue & Events.`)
      refetchStatus(); refetchCron()
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Run failed')
    } finally {
      setRunningKey(null)
    }
  }

  const handleRetryFailed = async () => {
    setError(null); setSuccessMessage(null)
    try {
      const data = await retryFailed.mutateAsync('diarization')
      setSuccessMessage(`Reset ${data.reset_count ?? 0} failed annotations — retried on the next run`)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to reset annotations')
    }
  }

  const handleDiscardFailed = async () => {
    if (!window.confirm('Discard all failed annotations? This permanently deletes annotations that keep failing to train.')) return
    setError(null); setSuccessMessage(null)
    try {
      const data = await deleteFailed.mutateAsync('diarization')
      setSuccessMessage(`Discarded ${data.deleted_count ?? 0} failed annotations`)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to discard annotations')
    }
  }

  const handleCleanOrphaned = async (annotationType: string) => {
    setCleaningType(annotationType)
    setError(null); setSuccessMessage(null)
    try {
      const data = await deleteOrphaned.mutateAsync(annotationType)
      setSuccessMessage(data.deleted_count > 0 ? `Cleaned ${data.deleted_count} orphaned ${annotationType} annotations` : 'No orphaned annotations found')
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to clean orphaned annotations')
    } finally {
      setCleaningType(null)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600">Loading...</span>
      </div>
    )
  }

  const totalOrphaned = Object.values(counts).reduce((s, c) => s + (c.orphaned || 0), 0)
  const failedCount = status?.failed_annotation_count || 0
  const speakerHealthUrl = externalServices?.services
    ?.find(service => service.name === 'speaker-recognition')
    ?.ui_url?.replace(/\/$/, '')

  return (
    <div className="max-w-4xl">
      {/* Header */}
      <div className="flex justify-between items-start mb-6">
        <div className="flex items-center space-x-2">
          <Zap className="h-6 w-6 text-blue-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Training</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">Corrections and training jobs.</p>
          </div>
        </div>
        <Button variant="secondary" size="md" onClick={() => { refetchStatus(); refetchCron() }} icon={<RefreshCw className="h-4 w-4" />}>Refresh</Button>
      </div>

      {error && (
        <Alert tone="danger" className="mb-4" icon={<AlertTriangle className="h-5 w-5 flex-shrink-0" />}>{error}</Alert>
      )}
      {successMessage && (
        <Alert tone="success" className="mb-4" icon={<CheckCircle2 className="h-5 w-5 flex-shrink-0" />}>{successMessage}</Alert>
      )}

      {(statusError || cronError) && <Alert role="alert" tone="danger" className="mb-4">Training status could not be loaded. Refresh to try again.</Alert>}

      {/* Model cards */}
      {!statusError && !cronError && <div className="space-y-4">
        {MODEL_TARGETS.map((t) => {
          const breakdown = t.correctionTypes.map((ty) => ({ ty, count: counts[ty]?.applied || 0 }))
          const applied = breakdown.reduce((s, b) => s + b.count, 0)
          const cron = cronFor(t.cronJobId)
          const Icon = t.icon
          const running = runningKey === t.key || cron?.running
          return (
            <div key={t.key} className="bg-white dark:bg-gray-800 rounded-xl shadow p-6 border border-gray-200 dark:border-gray-700">
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-3 min-w-0">
                  <div className="p-2.5 rounded-lg bg-blue-50 dark:bg-blue-900/30"><Icon className="h-6 w-6 text-blue-600 dark:text-blue-400" /></div>
                  <div className="min-w-0">
                    <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{t.label}</h3>
                    <p className="text-sm text-gray-500 dark:text-gray-400">{t.blurb}</p>
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
                      {breakdown.map((b) => (
                        <span key={b.ty} className="text-xs text-gray-600 dark:text-gray-300">
                          <span className={`font-semibold ${b.count ? 'text-gray-900 dark:text-gray-100' : 'text-gray-400'}`}>{b.count}</span> {TYPE_LABEL[b.ty] || b.ty}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="text-right flex-shrink-0">
                  <div className="text-3xl font-bold text-gray-900 dark:text-gray-100">{applied}</div>
                  <div className="text-[11px] uppercase tracking-wide text-gray-400">applied corrections</div>
                </div>
              </div>

              {t.key === 'speaker' && (
                <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-gray-600 dark:text-gray-300">
                  <span>Enrollment updates voice identification.</span>
                  {speakerHealthUrl && (
                    <a
                      href={`${speakerHealthUrl}/enrollment-health`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 font-medium text-blue-700 dark:text-blue-300 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded-sm"
                    >
                      Check enrollment health <ArrowUpRight className="h-3.5 w-3.5" />
                    </a>
                  )}
                </div>
              )}

              <div className="mt-4 pt-4 border-t border-gray-100 dark:border-gray-700 flex items-center justify-between">
                <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                  {cron?.enabled
                    ? <span className="inline-flex items-center gap-1 text-green-700 dark:text-green-400"><CheckCircle2 className="h-3.5 w-3.5" /> Scheduled</span>
                    : <span className="inline-flex items-center gap-1"><CircleDashed className="h-3.5 w-3.5" /> {t.cronJobId ? 'Manual' : 'Human review'}</span>}
                  {t.cronJobId && <span className="inline-flex items-center gap-1"><Clock className="h-3.5 w-3.5" /> {formatTimestamp(cron?.last_run)}</span>}

                  {cron?.last_error && <span className="text-red-500 truncate max-w-[180px]" title={cron.last_error}>error</span>}
                </div>
                {t.cronJobId ? (
                  <Button
                    variant="primary"
                    size="md"
                    onClick={() => handleRun(t)}
                    disabled={!!running || !cron}
                    icon={running ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  >
                    {running ? 'Running…' : t.runVerb}
                  </Button>
                ) : (
                  <Link
                    to="/data-audit?view=enroll"
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-blue-300 dark:border-blue-700 text-blue-700 dark:text-blue-300 text-sm font-medium hover:bg-blue-50 dark:hover:bg-blue-900/20"
                  >
                    Review enrollment <ArrowUpRight className="h-4 w-4" />
                  </Link>
                )}
              </div>
            </div>
          )
        })}
      </div>}

      {/* Maintenance — failed/orphaned annotation recovery (collapsed) */}
      {(failedCount > 0 || totalOrphaned > 0) && (
        <details className="mt-6 group">
          <summary className="cursor-pointer text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 select-none">
            Maintenance — {failedCount > 0 && `${failedCount} failed`}{failedCount > 0 && totalOrphaned > 0 && ', '}{totalOrphaned > 0 && `${totalOrphaned} orphaned`} annotation{failedCount + totalOrphaned === 1 ? '' : 's'}
          </summary>
          <div className="mt-3 space-y-4">
            {failedCount > 0 && (
              <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-semibold text-red-700 dark:text-red-300">{failedCount} annotation{failedCount === 1 ? '' : 's'} failed to train</span>
                  <div className="flex space-x-2">
                    <Button variant="primary" onClick={handleRetryFailed} disabled={retryFailed.isPending || deleteFailed.isPending}>{retryFailed.isPending ? 'Retrying…' : 'Retry'}</Button>
                    <Button variant="danger" onClick={handleDiscardFailed} disabled={retryFailed.isPending || deleteFailed.isPending}>{deleteFailed.isPending ? 'Discarding…' : 'Discard'}</Button>
                  </div>
                </div>
                {status?.failed_annotation_errors?.length > 0 && (
                  <ul className="text-xs text-red-600 dark:text-red-400 list-disc list-inside space-y-0.5">
                    {status.failed_annotation_errors.map((e: string, i: number) => <li key={i} className="truncate" title={e}>{e}</li>)}
                  </ul>
                )}
              </div>
            )}
            {totalOrphaned > 0 && (
              <div className="p-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-lg space-y-2">
                <p className="text-sm text-amber-800 dark:text-amber-300">These annotations reference conversations that no longer exist.</p>
                {Object.entries(counts).map(([key, c]) => c.orphaned > 0 && (
                  <div key={key} className="flex items-center justify-between">
                    <span className="text-sm text-gray-700 dark:text-gray-300">{key}: {c.orphaned} orphaned</span>
                    <button onClick={() => handleCleanOrphaned(key)} disabled={cleaningType === key} className="px-3 py-1 bg-amber-600 text-white text-xs rounded hover:bg-amber-700 disabled:bg-gray-300">{cleaningType === key ? 'Cleaning…' : 'Clean up'}</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  )
}
