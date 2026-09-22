import { sourceDate } from '../../utils/sourceTime'

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '0s'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.round(seconds % 60)
  if (h > 0) return `${h}h ${m}m`
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

export function formatClock(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

export function formatDate(iso: string | null): string {
  if (!iso) return '—'
  return sourceDate(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })
}

// Processing-status chip for a Data Audit row. Returns null for the normal
// 'completed' case (and legacy null status) so only noteworthy rows are chipped.
export function processingStatusChip(
  status: string | null,
  failureStage: string | null
): { label: string; className: string } | null {
  if (status === 'failed') {
    return {
      label: failureStage === 'summarization' ? 'Summary failed' : 'Transcription failed',
      className: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
    }
  }
  if (status === 'active') {
    return {
      label: 'Processing…',
      className: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
    }
  }
  return null
}

// Color band for a speaker-recognition cosine. This model's range is
// compressed (operating point ~0.5), so the bands are tuned for it rather than
// a naive 0–1 scale. Used for both stored confidence and live suggestions.
export function confidenceBadgeClass(c: number): string {
  if (c >= 0.55) return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
  if (c >= 0.45) return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
  return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
}
