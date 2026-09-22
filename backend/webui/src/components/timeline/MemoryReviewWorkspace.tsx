import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CalendarDays, ExternalLink } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTimelineTimezone } from '../../hooks/useTimelineTimezone'
import { timelineApi } from '../../services/api'
import { Button } from '../ui'
import ReviewDesk from './ReviewDesk'
import { dateFromSearch, localDate } from './timelineNavigation'

function shortDate(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** URL-addressable home for selective episode and memory decisions. */
export default function MemoryReviewWorkspace() {
  const [searchParams, setSearchParams] = useSearchParams()
  const {
    timezone, browserTimezone, storedTimezone, shouldOfferBrowserTimezone,
    saveBrowserTimezone, savingBrowserTimezone,
  } = useTimelineTimezone()
  const today = localDate(new Date(), timezone)
  const requestedDay = dateFromSearch(`?${searchParams.toString()}`, '')
  const queue = useQuery({
    queryKey: ['timeline-review-queue', timezone],
    queryFn: async () => (await timelineApi.getReviewQueue(timezone)).data.items,
  })
  const lastReviewed = queue.data?.[0]?.date
  const day = requestedDay || lastReviewed || today

  useEffect(() => {
    if (requestedDay || !queue.isSuccess) return
    const next = new URLSearchParams(searchParams)
    next.set('view', 'review')
    next.set('date', day)
    setSearchParams(next, { replace: true })
  }, [day, queue.isSuccess, requestedDay, searchParams, setSearchParams])

  const selectedDay = useQuery({
    queryKey: ['semantic-timeline', day, timezone],
    queryFn: async () => (await timelineApi.getDay(day, timezone)).data,
  })

  const selectDay = (nextDay: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('view', 'review')
    next.set('date', nextDay)
    setSearchParams(next)
  }
  const unassigned = selectedDay.data?.coverage?.unassigned_intervals || []
  const classified = unassigned.some(interval => interval.cause)
  const unexplainedCount = classified
    ? unassigned.filter(interval => interval.cause === 'unexplained').length
    : unassigned.length
  const captureGapCount = classified
    ? unassigned.filter(interval => interval.cause === 'no_capture').length
    : 0
  const unreconciledCount = selectedDay.data?.reconciliation?.ranges.length || 0

  return (
    <div className="space-y-4">
      <div className="flex flex-col justify-between gap-3 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-3 sm:flex-row sm:items-center">
        <div className="flex items-center gap-3">
          <CalendarDays className="h-5 w-5 text-[var(--tape-media)]" aria-hidden="true" />
          <label className="text-xs font-semibold uppercase tracking-[0.12em] text-gray-500 dark:text-gray-400">
            Review day
            <input
              type="date"
              value={day}
              onChange={event => selectDay(event.target.value)}
              className="ml-2 min-h-9 rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-2.5 py-1.5 text-sm font-medium normal-case tracking-normal text-gray-900 outline-none focus:ring-2 focus:ring-[var(--tape-focus)] dark:text-gray-100"
            />
          </label>
        </div>
        <Link
          to={`/timeline?date=${day}`}
          aria-label={`Open Timeline for ${shortDate(day)}`}
          className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--tape-focus)] hover:underline"
        >
          Open this day in Timeline <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
        </Link>
      </div>

      {shouldOfferBrowserTimezone && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2 text-xs text-gray-600 dark:text-gray-300">
          <span>{storedTimezone ? `Times are shown in ${storedTimezone}; this browser reports ${browserTimezone}.` : `Using browser timezone: ${browserTimezone}.`}</span>
          <Button variant="ghost" size="sm" onClick={saveBrowserTimezone} disabled={savingBrowserTimezone}>
            {storedTimezone ? 'Use browser timezone' : 'Save browser timezone'}
          </Button>
        </div>
      )}

      {selectedDay.isLoading ? (
        <div className="flex h-40 items-center justify-center text-sm text-gray-500 dark:text-gray-400">Loading review day…</div>
      ) : selectedDay.isError ? (
        <div className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          Could not load this review day. <Button variant="secondary" size="sm" onClick={() => selectedDay.refetch()}>Retry</Button>
        </div>
      ) : (
        <ReviewDesk
          day={day}
          timezone={timezone}
          review={selectedDay.data?.review}
          snapshotId={selectedDay.data?.current_snapshot_id}
          unexplainedCount={unexplainedCount}
          captureGapCount={captureGapCount}
          unreconciledCount={unreconciledCount}
          onSelectDay={selectDay}
        />
      )}
    </div>
  )
}
