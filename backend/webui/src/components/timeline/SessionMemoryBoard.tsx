import AskAboutSource from "../AskAboutSource"
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ChevronDown, Image, Monitor, Mic, Volume2, Check, Loader2 } from 'lucide-react'
import { MemorySession, SessionSource, timelineApi } from '../../services/api'
import { Button } from '../ui'
import { CandidateChanges } from './ReviewDesk'

const busyStates = ['queued', 'generating', 'checking', 'applying', 'regenerating']
const quietStates = ['excluded', 'deferred', 'no_changes', 'applied', 'rejected']
const clock = (value: string, timezone: string) => new Date(value).toLocaleTimeString('en-IN', { timeZone: timezone, hour: 'numeric', minute: '2-digit' })
function sessionTime(session: MemorySession, timezone: string) {
  const options = { timeZone: timezone, day: 'numeric', month: 'short' } as const
  const startDay = new Date(session.started_at).toLocaleDateString('en-IN', options)
  const endDay = new Date(session.ended_at).toLocaleDateString('en-IN', options)
  return startDay === endDay ? `${clock(session.started_at, timezone)}–${clock(session.ended_at, timezone)}` : `${startDay}, ${clock(session.started_at, timezone)}–${endDay}, ${clock(session.ended_at, timezone)}`
}
function errorMessage(error: unknown) {
  const e = error as { response?: { data?: { detail?: string } }; message?: string }
  return e?.response?.data?.detail || e?.message || 'Could not save this decision.'
}
export function sourceLabel(source: SessionSource) {
  if (source.kind === 'annotation') return 'Your clarification'
  if (source.locator.modality === 'photo' || source.kind === 'immich') return 'Photo'
  if (source.locator.modality === 'screen') {
    if (source.metadata?.text_source === 'accessibility') return 'Screen accessibility text'
    if (source.metadata?.text_source === 'ocr') return 'Screen OCR'
    return 'Screen text'
  }
  if (source.direction === 'output') return 'System audio'
  if (source.direction === 'input') return 'Microphone audio'
  if (source.kind === 'transcript') return 'Transcript'
  return source.kind === 'capture_gap' ? 'Capture gap' : 'Source evidence'
}
function SourceIcon({ source }: { source: SessionSource }) {
  const label = sourceLabel(source)
  const Icon = label === 'Photo' ? Image : source.locator.modality === 'screen' ? Monitor : label === 'System audio' ? Volume2 : Mic
  return <Icon aria-hidden className="h-4 w-4 shrink-0" />
}
function stateLabel(session: MemorySession) {
  if (session.state === 'paused') return session.failure_kind === 'repeated_tool_call' ? 'Investigation stopped repeating a tool call' : 'Investigation limit reached'
  if (session.state === 'queued' && session.failure_kind === 'time_slice') return 'Continuing saved investigation'
  if (session.state === 'needs_attention') return `${session.questions.length} question${session.questions.length === 1 ? '' : 's'}`
  if (session.state === 'pending') return `${session.change_count} proposed change${session.change_count === 1 ? '' : 's'}`
  if (session.state === 'generating') return ({ account: 'Investigating session', combining: 'Combining the session account', checking_claims: 'Reviewing the account and questions', revising: 'Revising the account from feedback', memory: 'Drafting useful changes' } as Record<string, string>)[session.stage || 'account'] || 'Preparing memory'
  return ({ available: 'Ready to prepare', waiting: 'Waiting for evidence', no_changes: 'No useful changes', needs_attention: 'One question', excluded: 'Excluded from memory', deferred: 'For later', applied: 'Memory saved', failed: 'Preparation failed', queued: 'Queued', stale: 'Sources changed', correction_required: 'Memory correction needed' } as Record<string, string>)[session.state] || session.state.replace(/_/g, ' ')
}

function activityLabel(session: MemorySession) {
  if (session.stage === 'memory') return null
  const activity = session.investigation_activity
  if (!activity?.event) return null
  if (activity.event === 'compaction_start') return 'Compacting context'
  if (activity.event === 'compaction_end') return 'Context compacted'
  if (activity.tool === 'finish_task') return 'Validating the investigation result'
  if (activity.tool === 'revise_result') return 'Correcting the investigation draft'
  if (activity.tool === 'search_material') return activity.store === 'vault' ? 'Searching accepted notes' : 'Finding source passages'
  if (activity.tool === 'read_materials') return 'Reading source passages'
  if (activity.tool === 'read_material') return activity.store === 'vault' ? 'Consulting accepted notes' : 'Reading evidence'
  return activity.checkpoint_saved ? 'Investigation checkpoint saved' : 'Considering the evidence'
}

function SessionRow({ session, day, timezone }: { session: MemorySession; day: string; timezone: string }) {
  const client = useQueryClient()
  const questions = ['stale', 'failed', 'paused'].includes(session.state) ? [] : session.questions
  const [params] = useSearchParams()
  const focused = params.get('session') === session.session_key
  const [open, setOpen] = useState(focused)
  useEffect(() => { if (focused) { setOpen(true); document.querySelector(`[data-session-key="${session.session_key}"]`)?.scrollIntoView({ block: 'start' }) } }, [focused, session.session_key])
  const [reviewing, setReviewing] = useState(false)
  const [inspecting, setInspecting] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [notice, setNotice] = useState('')
  const [clarification, setClarification] = useState('')
  useEffect(() => setSelected(new Set()), [session.scope_hash])
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['timeline-sessions', day, timezone] }),
      client.invalidateQueries({ queryKey: ['timeline-memory-selections', day, timezone] }),
    ])
  }
  const decide = useMutation({
    mutationFn: ({ action, keys, role, clarification: answer }: { action: 'exclude' | 'defer' | 'resume' | 'include' | 'attribute' | 'clarify'; keys: string[]; role?: 'user_statement' | 'third_party' | 'media_content'; clarification?: string }) => timelineApi.decideSessionMemory(day, timezone, session, action, keys, role, answer),
    retry: (count, error) => count < 2 && (error as { response?: { status?: number } })?.response?.status === 503,
    retryDelay: 2000,
    onMutate: () => setNotice(''),
    onSuccess: async response => {
      setNotice(response.data.correction_required ? 'This evidence already supports saved memory. Review a correction before changing those notes.' : 'Decision saved. Source recordings remain available.')
      setSelected(new Set())
      setClarification('')
      await refresh()
    },
  })
  const generate = useMutation({ mutationFn: () => timelineApi.generateSessionMemory(day, timezone, session), onSuccess: refresh })
  const restart = useMutation({ mutationFn: () => timelineApi.regenerateMemoryProposal(session.proposal_id!), onSuccess: refresh })
  const correction = useMutation({ mutationFn: () => timelineApi.correctMemoryProposal(session.proposal_id!), onSuccess: refresh })
  const proposals = useQuery({
    queryKey: ['timeline-memory-selections', day, timezone],
    queryFn: async () => (await timelineApi.getMemorySelections(day, timezone)).data,
    enabled: reviewing,
    refetchInterval: reviewing ? 3000 : false,
  })
  const inference = useQuery({ queryKey: ['session-memory-exchanges', session.proposal_id], queryFn: async () => (await timelineApi.getMemoryExchanges(session.proposal_id!)).data, enabled: inspecting && !!session.proposal_id })
  const proposal = proposals.data?.proposals.find(p => p.proposal_id === session.proposal_id)
  const busy = busyStates.includes(session.state)
  const saving = decide.isPending || generate.isPending || restart.isPending || correction.isPending
  const evidence = useQuery({ queryKey: ['session-sources', session.session_key, session.revision, session.scope_hash], queryFn: async () => (await timelineApi.getSessionSources(day, timezone, session)).data, enabled: open, staleTime: 60000 })
  const sourcesForDisplay = evidence.data?.scope_hash === session.scope_hash ? evidence.data.sources : []
  const allKeys = session.source_keys
  const beginReview = () => { setOpen(true); setReviewing(true) }
  const start = new Date(session.started_at).getTime()
  const duration = Math.max(1, new Date(session.ended_at).getTime() - start)
  const tracks = new Map<string, SessionSource[]>()
  for (const source of sourcesForDisplay) {
    const key = `${source.locator.capture_source_id}:${source.locator.track_id}:${sourceLabel(source)}:${source.participation}`
    tracks.set(key, [...(tracks.get(key) || []), source])
  }
  return <article data-session-key={session.session_key} className="border-b border-[var(--tape-line)] last:border-0" aria-label={`Session: ${session.title}`}>
    <div className="flex flex-col gap-3 py-4 sm:flex-row sm:items-start">
      <button className="flex min-w-0 flex-1 gap-3 text-left focus-visible:outline-[var(--tape-focus)]" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronDown className={`mt-1 h-4 w-4 shrink-0 transition-transform ${open ? '' : '-rotate-90'}`} />
        <span className="min-w-0">
          <span className="block text-xs text-gray-600 dark:text-gray-400">{sessionTime(session, timezone)} · {session.origin === 'human' ? 'Reviewed session' : 'Organized automatically'}</span>
          <span className="mt-1 block break-words text-base font-semibold">{session.title}</span>
          {session.refresh_assessment?.verdict === 'useful' && <span className="mt-1 block text-xs text-amber-800 dark:text-amber-300" title={session.refresh_assessment.reason}>New context available · review suggested refreshes above</span>}
          <span style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }} className="mt-1 text-sm text-gray-600 dark:text-gray-300">{session.summary}</span>
          <span className={`mt-2 flex items-center gap-1.5 text-xs ${questions.length ? 'text-amber-800 dark:text-amber-300' : 'text-gray-600 dark:text-gray-400'}`} aria-live="polite">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : session.state === 'applied' ? <Check className="h-3.5 w-3.5 text-green-700 dark:text-green-400" /> : null}
            {stateLabel(session)}
          </span>
          {session.waiting_reason && <span className="mt-1 block text-xs text-gray-600 dark:text-gray-400">{session.waiting_reason}</span>}
        </span>
      </button>
      <div className="flex shrink-0 flex-wrap gap-2 pl-7 sm:max-w-60 sm:justify-end sm:pl-0">
        <AskAboutSource source={{ kind: "session", key: session.session_key, local_date: day, timezone }} />
        {session.state === 'pending' ? <Button size="sm" variant="primary" onClick={beginReview}>Review changes</Button>
          : session.state === 'correction_required' ? <Button size="sm" variant="primary" disabled={saving} onClick={() => correction.mutate()}>Review correction</Button>
          : session.state !== 'stale' && session.state !== 'failed' && questions.length ? <Button size="sm" variant="primary" onClick={() => setOpen(true)}>Resolve question</Button>
          : session.state === 'paused' ? <>{session.proposal_id && <Button size="sm" variant="primary" disabled={saving} onClick={() => restart.mutate()}>{restart.isPending ? 'Queuing…' : 'Restart investigation'}</Button>}<Button size="sm" variant="secondary" onClick={() => { setOpen(true); setInspecting(true) }}>Inspect saved work</Button></>
          : !busy && !['excluded', 'deferred', 'applied', 'no_changes', 'waiting'].includes(session.state) ? <Button size="sm" variant="primary" disabled={saving} onClick={() => generate.mutate()}>{generate.isPending ? 'Queuing…' : session.state === 'failed' ? 'Retry preparation' : session.state === 'stale' ? 'Refresh draft' : 'Generate memory'}</Button> : null}
        {!['excluded', 'deferred'].includes(session.state) && allKeys.length > 0 && <Button size="sm" variant="ghost" disabled={saving || session.state === 'applying'} onClick={() => decide.mutate({ action: 'exclude', keys: allKeys })}>Don’t remember</Button>}
      </div>
    </div>
    {session.total_sources > 0 && (busy || session.state === 'paused') && <div className="mb-3 pl-7"><p className="mb-1 text-xs text-gray-600 dark:text-gray-400">{session.completed_sources} of {session.total_sources} source groups complete · {({ account: 'Building the session account', combining: 'Combining source accounts', checking_claims: 'Independent account review', memory: 'Preparing note changes' } as Record<string, string>)[session.stage || 'account']}</p><div role="progressbar" aria-label="Source groups complete" aria-valuemin={0} aria-valuemax={session.total_sources} aria-valuenow={session.completed_sources} className="h-1.5 overflow-hidden rounded bg-[var(--tape-line)]"><div className="h-full" style={{ backgroundColor: "var(--tape-focus)", width: `${Math.min(100, session.completed_sources / session.total_sources * 100)}%` }} /></div></div>}
    {busy && activityLabel(session) && <p className="mb-3 pl-7 text-xs text-gray-600 dark:text-gray-400" role="status">{activityLabel(session)} · last activity {clock(session.investigation_activity!.updated_at, 'Asia/Kolkata')} IST</p>}
    {session.error && session.state !== 'generating' && <div className="mb-3 space-y-2 text-sm">
      <p role={session.state === 'failed' ? 'alert' : 'status'} className={session.state === 'failed' ? 'text-red-700 dark:text-red-300' : session.state === 'paused' ? 'text-amber-800 dark:text-amber-300' : 'text-gray-600 dark:text-gray-300'}>{session.state === 'paused' ? (session.failure_kind === 'repeated_tool_call' ? 'Repeated identical tool calls stopped this investigation. Saved source reads and drafts remain inspectable. Restart begins a fresh attempt; no automatic retry is scheduled.' : 'The investigation reached its work limit. Restart to begin a new attempt with completed source groups retained; no automatic retry is scheduled. Earlier exchanges remain available below.') : session.state === 'failed' ? 'Preparation stopped before a reviewed draft was completed. Retry preparation, or inspect the details below.' : session.state === 'stale' ? 'This draft needs refreshing against the current sources before it can be approved.' : session.failure_kind === 'time_slice' ? 'Saved work will continue in the queue.' : session.error}</p>
      {session.state === 'failed' && <details><summary className="cursor-pointer text-xs text-[var(--tape-focus)]">Technical details</summary><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--tape-paper-raised)] p-2 text-xs">{session.error}</pre></details>}
    </div>}
    {decide.isPending && !notice && <p role="status" className="mb-3 text-sm text-gray-600 dark:text-gray-300">{decide.failureCount ? 'Timeline is updating. Retrying your answer…' : 'Saving your decision…'}</p>}
    {(decide.error || generate.error || restart.error || correction.error) && <p role="alert" className="mb-3 break-words text-sm text-red-700 dark:text-red-300">{errorMessage(decide.error || generate.error || restart.error || correction.error)}</p>}
    {notice && <p role="status" className="mb-3 text-xs text-gray-600 dark:text-gray-300">{notice}</p>}
    {open && <div className="mb-4 space-y-4 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-3 sm:ml-7 sm:p-4">
      {session.investigation_activity?.event && <details className="text-xs text-gray-600 dark:text-gray-400"><summary className="cursor-pointer text-[var(--tape-focus)]">Investigation activity</summary><p className="mt-2">{session.investigation_activity.tool_calls} tool calls · {session.investigation_activity.rounds} model rounds · Pi {session.investigation_activity.runtime_version}</p><p>{session.investigation_activity.checkpoint_saved ? 'Native session checkpoint saved' : 'Awaiting first complete checkpoint'}{session.investigation_activity.resumed ? ' · resumed saved work' : ''}</p><p>Last activity {clock(session.investigation_activity.updated_at, 'Asia/Kolkata')} IST</p></details>}
      {proposal && reviewing && <CandidateChanges proposal={proposal} day={day} timezone={timezone} />}
      {questions.map(question => <p key={question} className="rounded-md bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950/30 dark:text-amber-200">{question}</p>)}
      {questions.length > 0 && <div className="space-y-2"><label className="block text-sm font-medium" htmlFor={`clarification-${session.session_key}`}>Add the missing context</label><textarea id={`clarification-${session.session_key}`} className="w-full rounded border border-[var(--tape-line)] bg-[var(--tape-paper)] p-2 text-sm" rows={2} maxLength={2000} value={clarification} onChange={event => setClarification(event.target.value)} placeholder="Your answer is retained as your own statement alongside the original sources." /><Button size="sm" variant="primary" disabled={saving || !clarification.trim()} onClick={() => decide.mutate({ action: 'clarify', keys: selected.size ? [...selected] : allKeys, clarification: clarification.trim() })}>Save clarification</Button></div>}
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold">Evidence behind this session</h3><span className="text-xs text-gray-600 dark:text-gray-400">Sources remain separate and inspectable</span></div>
      {evidence.isLoading && <p role="status" className="text-xs">Loading source evidence…</p>}
      {evidence.isError && <p role="alert" className="text-xs text-red-700 dark:text-red-300">Could not load source evidence. Close and reopen this session to retry.</p>}
      <div className="space-y-2">
        {[...tracks.entries()].map(([track, sources]) => <details key={track} className="rounded border border-[var(--tape-line)] p-2" open={sources.some(s => s.participation === 'uncertain')}>
          <summary className="cursor-pointer text-xs"><strong>{sourceLabel(sources[0])}</strong> · <span className="break-all">{sources[0].locator.track_id || sources[0].locator.capture_source_id}</span> · {sources.length} excerpt{sources.length === 1 ? '' : 's'} · {sources[0].participation}</summary>
          <div className="relative my-2 h-2 overflow-hidden rounded bg-[var(--tape-chip)]" aria-label={`${sourceLabel(sources[0])} evidence intervals`}>
            {sources.map(s => <span key={s.key} className={`absolute h-full rounded ${s.participation === 'supporting' ? 'bg-[var(--tape-focus)]' : 'bg-[var(--tape-line)]'}`} style={{ left: `${Math.max(0, (new Date(s.started_at).getTime() - start) / duration * 100)}%`, width: `${Math.max(0.25, (new Date(s.ended_at).getTime() - new Date(s.started_at).getTime()) / duration * 100)}%` }} />)}
          </div>
          <Button size="sm" variant="ghost" onClick={() => setSelected(current => new Set([...current, ...sources.map(s => s.key)]))}>Select this track</Button>
          <div className="mt-2 max-h-80 space-y-2 overflow-y-auto">
        {sources.map(source => <div key={source.key} className="min-w-0 border-b border-[var(--tape-line)] pb-2 last:border-0">
          <div className="flex items-start gap-2">
            <input className="mt-1 shrink-0" type="checkbox" aria-label={`Select ${sourceLabel(source)} ${source.locator.capture_source_id}`} checked={selected.has(source.key)} onChange={() => setSelected(current => { const next = new Set(current); next.has(source.key) ? next.delete(source.key) : next.add(source.key); return next })} />
            <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2 text-xs"><SourceIcon source={source} /><strong>{sourceLabel(source)}</strong><span className="break-all text-gray-600 dark:text-gray-400">{source.locator.capture_source_id}{source.locator.track_id ? ` · ${source.locator.track_id}` : ''}</span><span className="rounded bg-[var(--tape-chip)] px-1.5 py-0.5">{source.participation === 'supporting' ? 'Used for the account' : source.participation === 'background' ? 'Background context' : source.participation === 'uncertain' ? 'Attribution uncertain' : 'Excluded'}</span></div>
              <div className="relative my-2 h-1.5 overflow-hidden rounded bg-[var(--tape-chip)]" aria-hidden><div className={`absolute h-full rounded ${source.participation === 'supporting' ? 'bg-[var(--tape-focus)]' : 'bg-[var(--tape-line)]'}`} style={{ left: `${Math.max(0, Math.min(100, (new Date(source.started_at).getTime() - start) / duration * 100))}%`, width: `${Math.max(0.5, Math.min(100, (new Date(source.ended_at).getTime() - new Date(source.started_at).getTime()) / duration * 100))}%` }} /></div>
              {source.participation === 'uncertain' && <div className="mb-2 flex flex-wrap gap-2"><Button size="sm" variant="secondary" disabled={saving} onClick={() => decide.mutate({ action: 'attribute', keys: [source.key], role: 'user_statement' })}>I said this</Button><Button size="sm" variant="secondary" disabled={saving} onClick={() => decide.mutate({ action: 'attribute', keys: [source.key], role: 'third_party' })}>Another person speaking</Button><Button size="sm" variant="ghost" disabled={saving} onClick={() => decide.mutate({ action: 'attribute', keys: [source.key], role: 'media_content' })}>Media dialogue</Button></div>}
              <details className="text-xs"><summary className="cursor-pointer text-[var(--tape-focus)]">Inspect source · {clock(source.started_at, timezone)}–{clock(source.ended_at, timezone)} · {source.role.replace(/_/g, ' ')}</summary><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-words font-sans">{source.excerpt || 'No text excerpt. Open its episode to inspect the original evidence.'}</pre><div className="mt-2 flex flex-wrap gap-3">{source.episode_keys.map(key => <Link key={key} className="text-[var(--tape-focus)] underline" to={`/timeline/key/${key}`}>Open episode evidence</Link>)}</div></details>
            </div>
          </div>
        </div>)}
          </div>
        </details>)}
      </div>
      <div className={`${selected.size ? "sticky bottom-0 z-10" : ""} flex flex-wrap gap-2 border-t border-[var(--tape-line)] bg-[var(--tape-paper-raised)] py-2`}>
        <Button size="sm" variant="secondary" disabled={!selected.size || saving} onClick={() => decide.mutate({ action: 'exclude', keys: [...selected] })}>Don’t remember selected sources</Button>
        <Button size="sm" variant="ghost" disabled={!selected.size || saving} onClick={() => decide.mutate({ action: 'include', keys: [...selected] })}>Use selected sources</Button>
        <Button size="sm" variant="ghost" disabled={!allKeys.length || saving} onClick={() => decide.mutate({ action: session.state === 'deferred' ? 'resume' : 'defer', keys: allKeys })}>{session.state === 'deferred' ? 'Resume review' : 'Later'}</Button>
      </div>
      {reviewing && proposals.isLoading && <p role="status">Loading note changes…</p>}
      {reviewing && proposals.isError && <p role="alert">Could not load the note changes.</p>}
      {session.proposal_id && <button className="text-xs text-[var(--tape-focus)] underline" aria-expanded={inspecting} onClick={() => setInspecting(!inspecting)}>Inspect model input and output</button>}
      {inspecting && inference.isLoading && <p role="status" className="text-xs">Loading retained model exchanges…</p>}
      {inspecting && inference.isError && <p role="alert" className="text-xs">Could not load model exchanges.</p>}
      {inspecting && inference.data && <div className="min-w-0 space-y-2 text-xs">
        {inference.data.accepted_context && <section aria-label="Accepted knowledge consulted" className="space-y-3 rounded border border-[var(--tape-line)] bg-[var(--tape-paper)] p-3">
          <h3 className="font-semibold">Accepted knowledge consulted</h3>
          {inference.data.accepted_context.review && <p className="text-[var(--tape-ink)]"><span className="font-medium">Independent review: </span>{inference.data.accepted_context.review.reason}</p>}
          {[...(inference.data.accepted_context.notes || []), ...(inference.data.accepted_context.review_context?.notes || [])].filter((note, index, rows) => rows.findIndex(other => other.path === note.path && other.passage === note.passage) === index).map((note, index) => <details key={index}><summary className="cursor-pointer break-all text-[var(--tape-focus)]">{note.path}</summary><blockquote className="mt-2 whitespace-pre-wrap break-words border-l border-[var(--tape-line)] pl-3 text-[var(--tape-ink)]">{note.passage}</blockquote></details>)}
          {!!inference.data.accepted_context.unresolved_lookups?.length && <details><summary className="cursor-pointer">Searches without matches</summary><ul className="mt-2 list-inside list-disc break-words">{inference.data.accepted_context.unresolved_lookups.map((lookup, index) => <li key={index}>{lookup || 'Vault inventory'}</li>)}</ul></details>}
        </section>}
        {inference.data.inference_runs.map((run, index) => <details key={index} className="rounded border border-[var(--tape-line)] p-2">
          <summary className="cursor-pointer">{run.operation === 'pi_session_review' ? 'Independent Pi review' : run.operation === 'pi_session_account' ? 'Pi investigation' : run.operation.replace(/_/g, ' ')} {index + 1} · {run.error ? 'Incomplete' : 'Complete'}{run.cached ? ' · Reused retained result' : ''}</summary>
          {run.error && <p className="my-2 break-words text-red-700 dark:text-red-300">{run.error}</p>}
          <p className="mt-2 font-semibold">Exact model input</p><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(run.model_input ?? (Array.isArray(run.exchanges) && run.exchanges.length ? run.exchanges : run.request), null, 2)}</pre>
          {!!run.tool_calls?.length && <details className="mt-2"><summary className="cursor-pointer text-[var(--tape-focus)]">Evidence and vault tool calls · {run.tool_calls.length}</summary><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(run.tool_calls, null, 2)}</pre></details>}
          <p className="mt-2 font-semibold">Raw output</p><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words">{run.output}</pre>
          {run.result != null && <details className="mt-2"><summary className="cursor-pointer text-[var(--tape-focus)]">Validated result used by the next stage</summary><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(run.result, null, 2)}</pre></details>}
        </details>)}
        {inference.data.writer_exchanges.map((run, index) => <details key={`writer-${index}`} className="rounded border border-[var(--tape-line)] p-2"><summary className="cursor-pointer">Memory writer exchange {index + 1} · {run.operation}</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(run.request, null, 2)}</pre><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words">{run.stdout}</pre>{run.stderr && <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words">{run.stderr}</pre>}</details>)}
        <details><summary className="cursor-pointer">Session account supplied to the memory writer</summary><pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words">{inference.data.source_digest}</pre></details>
      </div>}

    </div>}
  </article>
}

export default function SessionMemoryBoard({ day, timezone, snapshotId }: { day: string; timezone: string; snapshotId: string }) {
  const client = useQueryClient()
  const [params] = useSearchParams()
  const query = useQuery({ queryKey: ['timeline-sessions', day, timezone], queryFn: async () => (await timelineApi.getSessions(day, timezone)).data, refetchInterval: 5000 })
  const prepare = useMutation({ mutationFn: () => timelineApi.prepareSessions(day, timezone, snapshotId), onSuccess: () => client.invalidateQueries({ queryKey: ['timeline-sessions', day, timezone] }) })
  const sessions = query.data?.sessions || []
  const [groupingDetails, setGroupingDetails] = useState(false)
  const groupingRuns = useQuery({ queryKey: ['session-organization-exchanges', day, timezone], queryFn: async () => (await timelineApi.getSessionOrganizationExchanges(day, timezone)).data, enabled: groupingDetails })
  const preparation = query.data?.preparation
  const active = sessions.filter(s => !quietStates.includes(s.state))
  const quiet = sessions.filter(s => quietStates.includes(s.state))
  return <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-3 text-[var(--tape-ink)] sm:p-5" aria-label="Sessions and memory">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--tape-line)] pb-4"><div><h2 className="text-lg font-semibold">Sessions & memory</h2><p className="mt-1 max-w-2xl text-sm text-gray-600 dark:text-gray-300">Review what is worth keeping. The evidence stays available underneath.</p><p className="mt-2 text-xs text-gray-600 dark:text-gray-400">{sessions.filter(s => s.state === 'pending').length} draft{sessions.filter(s => s.state === 'pending').length === 1 ? '' : 's'} · {sessions.filter(s => !['stale', 'failed'].includes(s.state) && s.questions.length > 0).length} session{sessions.filter(s => !['stale', 'failed'].includes(s.state) && s.questions.length > 0).length === 1 ? '' : 's'} with questions</p></div><Button size="sm" variant="secondary" disabled={prepare.isPending} onClick={() => prepare.mutate()}>{prepare.isPending ? 'Queuing…' : 'Prepare this day'}</Button></div>
    {query.isLoading && <p role="status" className="py-4">Loading sessions…</p>}
    {(query.isError || prepare.isError) && <p role="alert" className="py-3 text-sm text-red-700 dark:text-red-300">{errorMessage(query.error || prepare.error)}</p>}
    {prepare.isSuccess && <p role="status" className="mt-3 text-xs">Preparation requested. Sessions will update as their jobs complete.</p>}
    {preparation && ['queued', 'running', 'waiting', 'failed'].includes(preparation.state) && <div className="my-3 rounded border border-[var(--tape-line)] p-3 text-sm" role={preparation.state === 'failed' ? 'alert' : 'status'}>
      <p className="font-medium">{({ queued: 'Session organization queued', running: 'Organizing session evidence', waiting: 'Waiting for affected evidence', failed: 'Session organization failed' } as Record<string, string>)[preparation.state]}</p>
      <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">{preparation.completed_queries} source-group queries completed · Attempt {preparation.attempts} of 3</p>
      {preparation.error && <p className="mt-2 break-words text-red-700 dark:text-red-300">{preparation.error}</p>}
      {Array.from(new Set(Object.values(preparation.waiting_sessions))).map(reason => <p key={reason} className="mt-2 text-xs">{reason}</p>)}
    </div>}
    {!!preparation?.completed_queries && <div className="my-3 text-xs"><button className="text-[var(--tape-focus)] underline" aria-expanded={groupingDetails} onClick={() => setGroupingDetails(!groupingDetails)}>Inspect grouping input and output</button>{groupingDetails && <div className="mt-2 space-y-2">{groupingRuns.isLoading && <p role="status">Loading retained grouping exchanges…</p>}{groupingRuns.isError && <p role="alert">Could not load grouping exchanges.</p>}{groupingRuns.data?.runs.map((run, index) => <details key={index} className="rounded border border-[var(--tape-line)] p-2"><summary className="cursor-pointer">Grouping query {index + 1}{run.stderr ? ' · Validation failed' : ''}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(run.metadata?.model_input ?? run.request, null, 2)}</pre>{!!run.metadata?.tool_calls?.length && <details><summary className="cursor-pointer">Evidence and vault tool calls</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(run.metadata.tool_calls, null, 2)}</pre></details>}<pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words">{run.stdout}</pre>{run.stderr && <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words">{run.stderr}</pre>}</details>)}</div>}</div>}
    {active.map(session => <SessionRow key={`${session.session_key}:${session.revision}`} session={session} day={session.owner_local_date} timezone={timezone} />)}
    {!active.length && query.isSuccess && <p className="py-4 text-sm text-gray-600 dark:text-gray-300">No memory decisions waiting here.</p>}
    {!!quiet.length && <details open={quiet.some(session => session.session_key === params.get("session"))} className="mt-2 border-t border-[var(--tape-line)] pt-3"><summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-300">Completed, background & later · {quiet.length} sessions</summary>{quiet.map(session => <SessionRow key={`${session.session_key}:${session.revision}`} session={session} day={session.owner_local_date} timezone={timezone} />)}</details>}
  </section>
}
