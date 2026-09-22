import { useEffect, useState } from 'react'
import { Clock, ToggleLeft, ToggleRight, Edit3, Check, X, Play, RefreshCw, Eye } from 'lucide-react'
import cronstrue from 'cronstrue'
import { useCronJobs, useToggleCronJob, useUpdateCronSchedule, useRunCronJob } from '../hooks/useFinetuning'
import { Alert, Button, Card, IconButton, StateBadge, StateTone } from './ui'

// Human-friendly names + what kind of automation each job is. Trigger-now lives
// on the Training page; this card owns only "should it run / when".
const JOB_DISPLAY_NAMES: Record<string, string> = {
  speaker_finetuning: 'Speaker enrollment (auto)',
  asr_finetuning: 'ASR fine-tuning export',
  prompt_optimization: 'Prompt optimization',
  asr_jargon_extraction: 'ASR jargon extraction',
  annotation_suggestions: 'Transcript suggestion detection',
  auto_clean: 'Auto-archive speech-free audio',
}

const JOB_KIND: Record<string, { label: string; tone: StateTone }> = {
  speaker_finetuning: { label: 'Training', tone: 'info' },
  asr_finetuning: { label: 'Training', tone: 'info' },
  prompt_optimization: { label: 'Training', tone: 'info' },
  asr_jargon_extraction: { label: 'Suggestions', tone: 'suggest' },
  annotation_suggestions: { label: 'Suggestions', tone: 'suggest' },
  auto_clean: { label: 'Maintenance', tone: 'neutral' },
}

function humanCron(expr: string): string {
  try {
    return cronstrue.toString(expr)
  } catch {
    return expr
  }
}

function formatTs(iso: string | null): string {
  if (!iso) return 'Never'
  return `${new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' })} IST`
}

export default function AutomationSettings({ isAdmin }: { isAdmin: boolean }) {
  const { data: jobs = [], isLoading, refetch } = useCronJobs()
  const toggleJob = useToggleCronJob()
  const updateSchedule = useUpdateCronSchedule()
  const runJob = useRunCronJob()

  const [editing, setEditing] = useState<string | null>(null)
  const [scheduleInput, setScheduleInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [runningJob, setRunningJob] = useState<string | null>(null)
  // AI-suggestion review auto-open preference (shared key with UserLoopModal).
  const [autoShow, setAutoShow] = useState(() => {
    try { return localStorage.getItem('userloop-auto-show') === 'true' } catch { return false }
  })
  useEffect(() => {
    try { localStorage.setItem('userloop-auto-show', String(autoShow)) } catch {}
  }, [autoShow])

  if (!isAdmin) return null

  const handleRun = async (jobId: string) => {
    setError(null)
    setRunningJob(jobId)
    try {
      await runJob.mutateAsync(jobId)
      refetch()
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to run job')
    } finally {
      setRunningJob(null)
    }
  }

  const handleToggle = async (jobId: string, enabled: boolean) => {
    setError(null)
    try {
      await toggleJob.mutateAsync({ jobId, enabled: !enabled })
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to update job')
    }
  }

  const handleSave = async (jobId: string) => {
    setError(null)
    try {
      await updateSchedule.mutateAsync({ jobId, schedule: scheduleInput })
      setEditing(null)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Invalid cron expression')
    }
  }

  return (
    <Card raised padded={false} className="p-6 lg:col-span-2">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center">
        <Clock className="h-5 w-5 mr-2 text-blue-600" />
        Automation &amp; Schedules
      </h3>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
        Next runs are shown in IST. Enabling or disabling a job takes effect immediately.
      </p>

      {error && (
        <Alert tone="danger" className="mb-3">{error}</Alert>
      )}

      {isLoading ? (
        <div
          className="divide-y divide-gray-100 dark:divide-gray-700"
          role="status"
          aria-label="Loading automation schedules"
          aria-busy="true"
        >
          {[0, 1, 2].map(index => (
            <div key={index} className="flex animate-pulse items-center justify-between gap-4 py-3">
              <div className="min-w-0 flex-1 space-y-2">
                <div className="h-3.5 w-44 rounded bg-gray-200 dark:bg-gray-700" />
                <div className="h-2.5 w-full max-w-md rounded bg-gray-100 dark:bg-gray-700/70" />
                <div className="h-2.5 w-28 rounded bg-gray-100 dark:bg-gray-700/70" />
              </div>
              <div className="h-7 w-16 flex-shrink-0 rounded-md bg-gray-200 dark:bg-gray-700" />
            </div>
          ))}
          <span className="sr-only">Loading automation schedules.</span>
        </div>
      ) : (
        <div className="divide-y divide-gray-100 dark:divide-gray-700">
          {jobs.map((job: any) => {
            const kind = JOB_KIND[job.job_id]
            return (
              <div key={job.job_id} className="py-3 flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {JOB_DISPLAY_NAMES[job.job_id] || job.job_id}
                    </span>
                    {kind && <StateBadge tone={kind.tone}>{kind.label}</StateBadge>}
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{job.description}</p>

                  <p className="mt-1.5 text-xs text-gray-700 dark:text-gray-300">
                    Next: {job.enabled ? (job.next_run ? formatTs(job.next_run) : 'Not scheduled') : 'Paused'}
                  </p>
                  <details className="mt-1.5 text-xs">
                    <summary className="cursor-pointer text-gray-600 dark:text-gray-400">Schedule and last run</summary>
                  <div className="mt-1.5 flex items-center gap-2 text-xs">
                    <Clock className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />
                    {editing === job.job_id ? (
                      <div className="flex items-center gap-1">
                        <span>Cron (UTC)</span>
                        <input
                          type="text"
                          aria-label="Cron schedule (UTC)"
                          value={scheduleInput}
                          onChange={(e) => setScheduleInput(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') handleSave(job.job_id)
                            if (e.key === 'Escape') setEditing(null)
                          }}
                          className="font-mono px-2 py-0.5 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                          autoFocus
                        />
                        <IconButton label="Save schedule" onClick={() => handleSave(job.job_id)}><Check className="h-4 w-4 text-green-500" /></IconButton>
                        <IconButton label="Cancel" onClick={() => setEditing(null)}><X className="h-4 w-4 text-gray-400" /></IconButton>
                      </div>
                    ) : (
                      <>
                        <span className="text-gray-700 dark:text-gray-300">{humanCron(job.schedule)} (UTC)</span>
                        <span className="font-mono text-gray-400">({job.schedule})</span>
                        <IconButton
                          label="Edit schedule"
                          onClick={() => { setEditing(job.job_id); setScheduleInput(job.schedule) }}
                        >
                          <Edit3 className="h-3.5 w-3.5 text-gray-400 hover:text-gray-600" />
                        </IconButton>
                      </>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-gray-400">
                    Last run: {formatTs(job.last_run)}
                    {job.last_error && <span className="text-red-500"> · error: {String(job.last_error).slice(0, 60)}</span>}
                  </div>

                  </details>

                  {/* Suggestion-review controls (this job produces AI suggestions to review). */}
                  {job.job_id === 'annotation_suggestions' && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <button
                        onClick={() => window.dispatchEvent(new Event('open-swipe-ui'))}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-purple-600 text-white text-xs font-medium hover:bg-purple-700"
                      >
                        <Eye className="h-3.5 w-3.5" /> Review suggestions
                      </button>
                      <Button
                        variant="ghost"
                        onClick={() => setAutoShow((v) => !v)}
                        title={autoShow ? 'Auto-opens the review when suggestions exist' : 'Use Review to open manually'}
                        icon={autoShow ? <ToggleRight className="h-4 w-4 text-purple-500" /> : <ToggleLeft className="h-4 w-4 text-gray-400" />}
                      >
                        Auto-open
                      </Button>
                    </div>
                  )}
                </div>

                <div className="flex-shrink-0 flex items-center gap-2 mt-0.5">
                  <Button
                    variant="secondary"
                    onClick={() => handleRun(job.job_id)}
                    disabled={runningJob === job.job_id || job.running}
                    title="Run now"
                    icon={runningJob === job.job_id || job.running
                      ? <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                      : <Play className="h-3.5 w-3.5" />}
                  >
                    Run
                  </Button>
                  <IconButton
                    label={job.enabled ? 'Disable' : 'Enable'}
                    onClick={() => handleToggle(job.job_id, job.enabled)}
                  >
                    {job.enabled
                      ? <ToggleRight className="h-6 w-6 text-green-500" />
                      : <ToggleLeft className="h-6 w-6 text-gray-400" />}
                  </IconButton>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
