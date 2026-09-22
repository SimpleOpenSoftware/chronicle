import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { TimelineEpisode, timelineApi } from '../../services/api'
import { Button } from '../ui'

export default function MemorySelectionPanel({ day, timezone, snapshotId, episodes }: {
  day: string; timezone: string; snapshotId: string | null | undefined; episodes: TimelineEpisode[]
}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  useEffect(() => setSelected(new Set()), [day, snapshotId])
  const selections = useQuery({
    queryKey: ['timeline-memory-selections', day, timezone],
    queryFn: async () => (await timelineApi.getMemorySelections(day, timezone)).data,
    refetchInterval: query => query.state.data?.proposals.some(p => ['queued', 'generating', 'checking', 'applying', 'regenerating'].includes(p.state)) ? 2000 : false,
  })
  const submit = useMutation({
    mutationFn: (exclude: boolean) => {
      const refs = episodes.filter(e => selected.has(`${e.episode_key}:${e.revision}`)).map(e => ({ episode_key: e.episode_key, revision: e.revision }))
      return exclude
        ? timelineApi.excludeMemorySelection(day, timezone, snapshotId!, refs)
        : timelineApi.createMemorySelection(day, timezone, snapshotId!, refs)
    },
    onSuccess: async () => {
      setSelected(new Set())
      await queryClient.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] })
    },
  })
  const error = submit.error as { response?: { data?: { detail?: string } }; message?: string } | null
  return <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-3 text-[var(--tape-ink)]" aria-label="Select episodes for memory">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div><h2 className="text-sm font-semibold">Remember selected episodes</h2><p className="mt-1 text-xs text-gray-500 dark:text-gray-300">Review what matters now. Everything else stays available for later.</p></div>
      <Link to={`/memory-ledger?view=review&date=${day}`} className="text-sm font-semibold text-[var(--tape-focus)] hover:underline">Review memory proposals</Link>
    </div>
    <details className="mt-3" open={selected.size > 0 || undefined}>
      <summary className="cursor-pointer text-sm">Choose from {episodes.length} episodes</summary>
      <div className="mt-2 max-h-96 space-y-1 overflow-auto">
        {episodes.map(episode => {
          const token = `${episode.episode_key}:${episode.revision}`
          const outcome = selections.data?.outcomes[token]
          const busy = selections.data?.proposals.some(p => p.active && p.selected_episodes.some(r => `${r.episode_key}:${r.revision}` === token))
          return <label key={token} className="flex items-start gap-2 rounded p-2 hover:bg-[var(--tape-paper-raised)]">
            <input type="checkbox" className="mt-1" disabled={busy || submit.isPending} checked={selected.has(token)} onChange={() => setSelected(value => { const next = new Set(value); next.has(token) ? next.delete(token) : next.add(token); return next })} />
            <span className="min-w-0 text-sm"><span className="font-medium">{episode.title}</span><span className="mt-0.5 block text-xs text-gray-500 dark:text-gray-300">{new Date(episode.started_at).toLocaleString('en-IN', { timeZone: timezone, dateStyle: 'medium', timeStyle: 'short' })} · {outcome?.state.replace(/_/g, ' ') || 'undecided'}{episode.memory_policy === 'reference' ? ' · reference only' : ''}{outcome && ` · ${outcome.accepted_changes} accepted, ${outcome.rejected_changes} rejected${outcome.daily_recorded ? ' · in Daily' : ''}`}</span></span>
          </label>
        })}
      </div>
    </details>
    <div className="mt-3 flex flex-wrap gap-2">
      <Button size="sm" disabled={!selected.size || !snapshotId || submit.isPending} onClick={() => submit.mutate(false)}>{submit.isPending ? 'Submitting…' : `Review ${selected.size || ''} selected episodes`}</Button>
      <Button size="sm" variant="secondary" disabled={!selected.size || !snapshotId || submit.isPending} onClick={() => submit.mutate(true)}>Exclude selected from memory</Button>
    </div>
    {submit.isSuccess && <p role="status" className="mt-2 text-xs">{submit.variables ? 'Selected episodes excluded. Other episodes remain undecided.' : 'Selection saved. Review its proposal before anything enters the vault.'}</p>}
    {error && <p role="alert" className="mt-2 text-xs text-red-700 dark:text-red-300">{error.response?.data?.detail || error.message}</p>}
    {selections.isError && <p role="alert" className="mt-2 text-xs text-red-700">Could not load episode memory decisions.</p>}
  </section>
}
