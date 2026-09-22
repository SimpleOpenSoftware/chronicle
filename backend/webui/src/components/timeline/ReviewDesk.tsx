import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, FileDiff, Loader2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { TimelineDayReview, MemoryReviewProposal, timelineApi } from '../../services/api'
import { Button, Card, computeWordDiff, WordDiff } from '../ui'

export function CandidateChanges({ proposal, day, timezone }: { proposal: MemoryReviewProposal; day: string; timezone: string }) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())

  useEffect(() => setSelected(new Set()), [proposal?.proposal_id])

  const resolve = useMutation({
    mutationFn: (accepted: string[]) => timelineApi.resolveMemoryProposal(proposal.proposal_id, proposal.generation, accepted),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-sessions', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['recording-context'] }),
      ])
    },
  })
  const regenerate = useMutation({
    mutationFn: () => timelineApi.regenerateMemoryProposal(proposal!.proposal_id),
    onSuccess: async () => {
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-sessions', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['recording-context'] }),
      ])
    },
  })

  if (proposal.state !== 'pending') return null
  const changes = proposal.changes || []
  const allSelected = selected.size === changes.length
  const error = resolve.error as { response?: { data?: { detail?: string } }; message?: string } | null

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Potential memory changes</h4>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Sources and accepted notes are checked again before applying changes.
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setSelected(allSelected ? new Set() : new Set(changes.map(change => change.change_id)))}
        >
          {allSelected ? 'Clear selection' : 'Select all'}
        </Button>
      </div>

      {changes.map(change => {
        const checked = selected.has(change.change_id)
        const diff = computeWordDiff(change.before_text ?? '', change.after_text ?? '')
        return (
          <article key={change.change_id} className={`rounded-lg border ${checked ? 'border-[var(--tape-focus)] bg-[var(--tape-selected)]' : 'border-[var(--tape-line)] bg-[var(--tape-paper-raised)]'}`}>
            <label className="flex cursor-pointer items-start gap-3 p-3">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => setSelected(current => {
                  const next = new Set(current)
                  next.has(change.change_id) ? next.delete(change.change_id) : next.add(change.change_id)
                  return next
                })}
                className="mt-1 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <FileDiff className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono text-xs font-semibold text-gray-800 dark:text-gray-200">{change.note_path}</span>
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-gray-500 dark:bg-gray-800 dark:text-gray-400">{change.operation}</span>
                </span>
                <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">{change.summary}</span>
                {!change.note_path.startsWith('Daily/') && !!change.source_episode_keys.length && (
                  <span className="mt-2 flex flex-wrap gap-1.5">
                    {change.source_episode_keys.map((episodeKey, index) => (
                      <Link
                        key={episodeKey}
                        to={`/timeline/key/${encodeURIComponent(episodeKey)}`}
                        onClick={event => event.stopPropagation()}
                        className="rounded-full bg-[var(--tape-chip)] px-2 py-0.5 text-[11px] font-medium text-[var(--tape-focus)] hover:underline"
                        title={`Open source episode ${episodeKey}`}
                      >
                        Source episode{change.source_episode_keys.length > 1 ? ` ${index + 1}` : ''}
                      </Link>
                    ))}
                  </span>
                )}
              </span>
            </label>
            {change.before_text == null && change.after_text && <p className="whitespace-pre-wrap break-words px-3 pb-3 text-sm leading-relaxed text-gray-700 dark:text-gray-200">{change.after_text.replace(/^---\n[\s\S]*?\n---\n/, '').split(/\n## (?:Conversations|Mentions)/)[0].replace(/^## .+\n/, '').trim().slice(0, 1000)}</p>}
            {!!change.source_evidence_keys?.length && <details className="px-3 pb-2"><summary className="cursor-pointer text-xs text-[var(--tape-focus)]">Supporting source quotations</summary><div className="mt-2 space-y-2">{proposal.account?.claims.filter(claim => claim.source_keys.some(key => change.source_evidence_keys?.includes(key))).map((claim, index) => <div key={index} className="rounded bg-[var(--tape-chip)] p-2 text-xs text-gray-900 dark:text-gray-100"><p>{claim.text}</p>{claim.citations.map((citation, i) => <blockquote key={i} className="mt-1 border-l-2 border-[var(--tape-line)] pl-2 text-gray-600 dark:text-gray-300">{citation.quote}</blockquote>)}</div>)}</div></details>}
            <details className="border-t border-gray-100 px-3 py-2 dark:border-gray-800">
              <summary className="cursor-pointer text-xs font-medium text-gray-600 dark:text-gray-300">Inspect highlighted changes</summary>
              <div className="mt-2 grid gap-2 lg:grid-cols-2">
                <div>
                  <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                    <span>Before</span>
                    {change.before_text != null && <span className="normal-case tracking-normal text-red-600 dark:text-red-400">Removed</span>}
                  </div>
                  <pre
                    aria-label="Before text; removed words are highlighted and struck through"
                    className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs leading-5 text-gray-700 dark:bg-gray-950 dark:text-gray-300"
                  >
                    {change.before_text == null
                      ? <span className="italic text-gray-400">New note — no previous content</span>
                      : <WordDiff tokens={diff.beforeTokens} />}
                  </pre>
                </div>
                <div>
                  <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                    <span>Proposed</span>
                    {change.after_text != null && <span className="normal-case tracking-normal text-green-700 dark:text-green-400">Added</span>}
                  </div>
                  <pre
                    aria-label="Proposed text; added words are highlighted"
                    className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs leading-5 text-gray-700 dark:bg-gray-950 dark:text-gray-300"
                  >
                    {change.after_text == null
                      ? <span className="italic text-gray-400">Delete note — no proposed content</span>
                      : <WordDiff tokens={diff.afterTokens} />}
                  </pre>
                </div>
              </div>
            </details>
          </article>
        )
      })}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          variant="primary"
          disabled={!selected.size || resolve.isPending}
          onClick={() => resolve.mutate([...selected])}
          icon={resolve.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
        >
          Apply {selected.size || ''} selected changes
        </Button>
        <Button variant="secondary" size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate([])}>
          {changes.length ? 'Reject all changes' : 'Confirm no changes'}
        </Button>
        <Button variant="secondary" size="sm" disabled={resolve.isPending || regenerate.isPending} onClick={() => regenerate.mutate()}>
          {regenerate.isPending ? 'Regenerating…' : 'Regenerate from current vault'}
        </Button>
        {selected.size > 0 && selected.size < changes.length && (
          <span className="text-xs text-gray-500 dark:text-gray-400">The other {changes.length - selected.size} changes will be rejected.</span>
        )}
      </div>
      {resolve.isError && (
        <p className="text-xs text-red-600 dark:text-red-400">{error?.response?.data?.detail || error?.message || 'Could not resolve this proposal.'} Regenerate the proposal to compare against the current vault.</p>
      )}
      {regenerate.isError && <p className="text-xs text-red-600 dark:text-red-400">Could not regenerate this proposal. {(regenerate.error as Error)?.message}</p>}
    </div>
  )
}

export default function ReviewDesk({ day, timezone }: {
  day: string; timezone: string; review?: TimelineDayReview | null; snapshotId?: string | null;
  unexplainedCount?: number; captureGapCount?: number; unreconciledCount?: number;
  onSelectDay: (day: string) => void;
}) {
  const queryClient = useQueryClient()
  const selections = useQuery({
    queryKey: ['timeline-memory-selections', day, timezone],
    queryFn: async () => (await timelineApi.getMemorySelections(day, timezone)).data,
    refetchInterval: query => query.state.data?.proposals.some(p => ['queued', 'generating', 'checking', 'applying', 'regenerating'].includes(p.state)) ? 2000 : false,
  })
  const regenerate = useMutation({
    mutationFn: (id: string) => timelineApi.regenerateMemoryProposal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] }),
  })
  const correct = useMutation({
    mutationFn: (id: string) => timelineApi.correctMemoryProposal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] }),
  })
  return <Card className="space-y-4 text-gray-900 dark:text-gray-100">
    <div>
      <h2 className="text-sm font-semibold">Review date · {day}</h2>
      <Link className="mt-2 inline-block text-sm text-[var(--tape-focus)] hover:underline" to={`/timeline?date=${day}`}>Select episodes in Timeline</Link>
    </div>
    {selections.isLoading && <p role="status">Loading proposals…</p>}
    {selections.isError && <p role="alert">Could not load memory proposals. {(selections.error as Error).message}</p>}
    {selections.data?.proposals.length === 0 && <p className="text-sm text-gray-500">No memory selections for this day yet.</p>}
    {selections.data?.proposals.map(proposal => <section key={proposal.proposal_id} className="border-t border-[var(--tape-line)] pt-3">
      <h3 className="text-sm font-semibold">{proposal.selected_episodes.length} source episode{proposal.selected_episodes.length === 1 ? '' : 's'} · Proposal day {proposal.local_date}</h3>
      <p className="mt-1 text-xs text-gray-500" aria-live="polite">{proposal.state === 'checking' ? 'Checking changes in the accepted vault…' : proposal.state.replace(/_/g, ' ')} · revision {proposal.generation}</p>
      {proposal.freshness && <p className="mt-1 text-xs">{proposal.freshness.reason}</p>}
      {proposal.replacement_proposal_id && <p className="mt-1 text-xs">Replaced by a newer proposal below.</p>}
      {proposal.error && <p role="alert" className="mt-1 text-xs text-red-700 dark:text-red-300">{proposal.error}</p>}
      <CandidateChanges proposal={proposal} day={day} timezone={timezone} />
      {proposal.state === 'correction_required' && <Button size="sm" onClick={() => correct.mutate(proposal.proposal_id)} disabled={correct.isPending}>Review correction from current evidence</Button>}
      {proposal.state === 'failed' && <Button size="sm" onClick={() => regenerate.mutate(proposal.proposal_id)} disabled={regenerate.isPending}>Retry generation</Button>}
      {['applied', 'rejected', 'no_changes'].includes(proposal.state) && <p className="mt-2 text-xs">{proposal.accepted_change_ids.length} changes accepted · {proposal.rejected_change_ids.length} rejected. Unselected episodes remain available.</p>}
      {proposal.changes?.length && proposal.state !== 'pending' ? <details className="mt-2 text-xs"><summary>Previous diff and decisions</summary>{proposal.changes.map(c => <div key={c.change_id} className="mt-2"><strong>{c.note_path}</strong><pre className="max-h-48 overflow-auto whitespace-pre-wrap">{c.after_text ?? '(deleted)'}</pre></div>)}</details> : null}
    </section>)}
    {correct.isError && <p role="alert">Could not prepare correction. {(correct.error as Error).message}</p>}
    {regenerate.isError && <p role="alert">Could not regenerate. {(regenerate.error as Error).message}</p>}
  </Card>
}
