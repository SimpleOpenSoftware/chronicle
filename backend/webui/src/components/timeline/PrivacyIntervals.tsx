import { useMutation, useQueryClient } from '@tanstack/react-query'
import { LockKeyhole } from 'lucide-react'
import { deviceInputApi, PrivacyInterval } from '../../services/api'
import { Button } from '../ui'

export default function PrivacyIntervals({ intervals, timezone }: { intervals: PrivacyInterval[]; timezone: string }) {
  const queries = useQueryClient()
  const decision = useMutation({
    mutationFn: ({ interval, choice }: { interval: PrivacyInterval; choice: 'allowed' | 'excluded' }) =>
      deviceInputApi.overridePrivacy(interval, choice),
    onSuccess: () => Promise.all([
      queries.invalidateQueries({ queryKey: ['semantic-timeline'] }),
      queries.invalidateQueries({ queryKey: ['raw-device-timeline'] }),
      queries.invalidateQueries({ queryKey: ['recordings'] }),
      queries.invalidateQueries({ queryKey: ['source-search'] }),
      queries.invalidateQueries({ queryKey: ['queue'] }),
    ]),
  })
  if (!intervals.length) return null
  const clock = (value: string) => new Date(value).toLocaleTimeString([], {
    timeZone: timezone, hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
  return <section aria-label="Private capture intervals" className="divide-y divide-[var(--tape-line)] rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] text-[var(--tape-ink)]">
    {intervals.map(interval => <div key={`${interval.source_id}:${interval.started_at}`} className="grid grid-cols-[1rem_minmax(0,1fr)] items-start gap-x-3 gap-y-2 px-3 py-2 text-sm sm:flex sm:items-center">
      <LockKeyhole size={16} className="mt-0.5 shrink-0 sm:mt-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">{interval.label}</p>
        <p className="text-xs opacity-70">{clock(interval.started_at)}–{clock(interval.ended_at)} · {interval.source_name}</p>
        {interval.reason && <p className="mt-1 text-xs opacity-70">{interval.reason}</p>}
      </div>
      <div className="col-start-2 flex flex-wrap gap-2 sm:shrink-0">
        <Button className="min-h-10 sm:min-h-0" variant="secondary" disabled={decision.isPending} onClick={() => decision.mutate({ interval, choice: 'allowed' })}>Allow processing</Button>
        <Button className="min-h-10 sm:min-h-0" variant="secondary" disabled={decision.isPending} onClick={() => decision.mutate({ interval, choice: 'excluded' })}>Keep excluded</Button>
      </div>
    </div>)}
    {decision.isError && <p role="alert" className="px-3 py-2 text-sm text-red-700 dark:text-red-300">Could not save this decision. Reload the timeline to check for changes.</p>}
  </section>
}
