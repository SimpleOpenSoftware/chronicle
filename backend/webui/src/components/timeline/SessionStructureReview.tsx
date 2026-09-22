import { useRef, useState } from 'react'
import { AlertTriangle, Check, ChevronLeft, ChevronRight, Loader2, Monitor, Image, MessageSquare, Pencil } from 'lucide-react'
import { DayReviewProjection, TimelineEpisode } from '../../services/api'
import { Button } from '../ui'
import { EpisodeThumbnail } from './EpisodeCard'
import { episodeDisplayTitle } from './episodePresentation'

const time = (value: string, timezone: string) => new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: timezone })

const timeRange = (start: string, end: string, timezone: string) => {
  const crossDate = new Date(start).toLocaleDateString('en-CA', { timeZone: timezone }) !== new Date(end).toLocaleDateString('en-CA', { timeZone: timezone })
  const label = (value: string) => `${time(value, timezone)}${crossDate ? ` (${new Date(value).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: timezone })})` : ''}`
  return `${label(start)}–${label(end)}`
}

export default function SessionStructureReview({ projection, episodes, unstableEpisodes, timezone, confirmingSessionId, onConfirm, onEdit, onNotActivity, rejectingEpisodeId, rejectionError }: {
  projection: DayReviewProjection
  episodes: TimelineEpisode[]
  unstableEpisodes: TimelineEpisode[]
  timezone: string
  confirmingSessionId?: string | null
  onConfirm: (sessionId: string, episodes: TimelineEpisode[]) => void
  onEdit: (episode: TimelineEpisode) => void
  onNotActivity: (episode: TimelineEpisode) => void
  rejectingEpisodeId?: string | null
  rejectionError?: Error | null
}) {
  const [rejectingKey, setRejectingKey] = useState<string | null>(null)
  const reviewBusy = Boolean(confirmingSessionId || rejectingEpisodeId)
  const reviewRef = useRef<HTMLDivElement>(null)
  const pending = new Set(unstableEpisodes.map(episode => episode.episode_id))
  const byId = new Map(episodes.map(episode => [episode.episode_id, episode]))
  const sessions = projection.groups.filter(group => group.episode_ids.some(id => pending.has(id)))
  const [openId, setOpenId] = useState<string | null>(null)
  const [evidenceTab, setEvidenceTab] = useState<'screen' | 'photo' | 'transcript'>('screen')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const open = sessions.some(group => group.group_id === openId) ? openId : sessions[0]?.group_id
  const sessionIndex = sessions.findIndex(group => group.group_id === open)
  const chooseSession = (id: string) => {
    setOpenId(id)
    setSelectedKey(null)
    setRejectingKey(null)
    setSelectedEvidenceId(null)
    setEvidenceTab('screen')
    reviewRef.current?.scrollIntoView?.({ block: 'start' })
  }

  return <div ref={reviewRef} className="mt-4 border-t border-[var(--tape-line)] pt-4 text-[var(--tape-ink)]">
    <div className="mb-4 flex items-center gap-2">
      <label className="min-w-0 flex-1">
        <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">{sessions.length} sessions left · {unstableEpisodes.length} episodes to check</span>
        <select aria-label="Choose session to review" value={open || ''} onChange={event => chooseSession(event.target.value)} className="min-h-10 w-full min-w-0 rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 text-sm focus-visible:outline-[var(--tape-focus)]">
          {sessions.map((group, index) => <option key={group.group_id} value={group.group_id}>{index + 1}. {time(group.started_at, timezone)} · {group.title}</option>)}
        </select>
      </label>
      <div className="flex shrink-0 gap-1 self-end">
        <Button className="min-h-10" aria-label="Previous session" disabled={sessionIndex <= 0} onClick={() => chooseSession(sessions[sessionIndex - 1].group_id)} icon={<ChevronLeft className="h-4 w-4" />} />
        <Button className="min-h-10" aria-label="Next session" disabled={sessionIndex >= sessions.length - 1} onClick={() => chooseSession(sessions[sessionIndex + 1].group_id)} icon={<ChevronRight className="h-4 w-4" />} />
      </div>
    </div>
    {sessions.filter(group => group.group_id === open).map(group => {
      const members = group.episode_ids.flatMap(id => byId.get(id) || [])
      const unsettled = members.filter(episode => pending.has(episode.episode_id))
      const selected = members.find(episode => episode.episode_key === selectedKey) || unsettled[0] || members[0]
      const busy = confirmingSessionId === group.group_id
      const start = Date.parse(group.started_at), span = Math.max(1, Date.parse(group.ended_at) - start)
      const overlaps = members.some((episode, index) => members.slice(index + 1).some(other => Date.parse(episode.started_at) < Date.parse(other.ended_at) && Date.parse(other.started_at) < Date.parse(episode.ended_at)))
      const captureOnly = members.some(episode => !episode.evidence.length || episode.evidence.every(item => ['audio_span', 'capture_gap'].includes(item.kind)))
      const titleWords = selected.title.toLowerCase().split(/\W+/).filter(word => word.length > 3)
      const relevance = (excerpt: string) => titleWords.filter(word => excerpt.toLowerCase().includes(word)).length
      const evidence = selected.evidence.filter(item => item.excerpt && ['transcript', 'observation', 'immich'].includes(item.kind)).sort((a, b) => relevance(b.excerpt!) - relevance(a.excerpt!))
      const screens = evidence.filter(item => item.kind === 'observation').sort((a, b) => relevance(String(b.metadata?.app_name || '')) - relevance(String(a.metadata?.app_name || '')))
      const photos = evidence.filter(item => item.kind === 'immich')
      const transcript = evidence.find(item => item.kind === 'transcript')
      const available = { screen: screens.length > 0, photo: photos.length > 0, transcript: Boolean(transcript) }
      const tab = available[evidenceTab] ? evidenceTab : screens.length ? 'screen' : photos.length ? 'photo' : 'transcript'
      const visualEvidence = tab === 'photo' ? photos : screens
      const selectedVisual = visualEvidence.find(item => item.evidence_id === selectedEvidenceId) || visualEvidence[0]
      const excerpt = tab === 'transcript' ? transcript : selectedVisual
      const imageEvidence = selectedVisual?.metadata?.thumbnail_available && /^observation:[a-f0-9]{24}$/.test(selectedVisual.evidence_id) ? selectedVisual : undefined
      const imageSourceId = imageEvidence?.evidence_id.split(':')[1]
      const tracks = [...new Set(selected.evidence.map(item => [item.locator?.capture_source_id || item.source_id, item.locator?.track_id].filter(Boolean).join(' / ')).filter(Boolean))]
      const hasCaptureGap = members.some(episode => episode.evidence.some(item => item.kind === 'capture_gap'))
      const gapItems = [...new Map(members.flatMap(episode => episode.evidence).filter(item => item.kind === 'capture_gap').map(item => [item.evidence_id, item])).values()]
      const missingSeconds = gapItems.reduce((sum, item) => sum + Number(item.metadata?.missing_seconds || 0), 0)
      const notices = [overlaps && 'Overlapping intervals', group.gap_seconds >= 60 && `${Math.round(group.gap_seconds / 60)} min unclaimed`, captureOnly && 'Limited evidence', hasCaptureGap && 'Recording gaps'].filter(Boolean)
      return <section key={group.group_id} aria-label={`Review session: ${group.title}`}>
        <header className="pb-3">
          <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
            <span>Session {sessionIndex + 1} of {sessions.length}</span>
            <span className="inline-flex items-center gap-1">{group.semantic && <Check className="h-3 w-3" />}{group.semantic ? 'Accepted grouping' : 'Chronological session'}</span>
          </div>
          <h3 className="text-base font-semibold leading-6 sm:text-lg">{group.title}</h3>
          <p className="mt-1 text-sm tabular-nums text-gray-600 dark:text-gray-300">{timeRange(group.started_at, group.ended_at, timezone)}</p>
        </header>
        {notices.length > 0 && <details className="group mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
          <summary className="flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden"><ChevronRight className="h-3.5 w-3.5 shrink-0 group-open:rotate-90" /><AlertTriangle className="h-3.5 w-3.5 shrink-0" /><span>{notices.join(' · ')}</span></summary>
          <div className="mt-2 space-y-1 leading-5">
            {overlaps && <p>Check whether overlapping recordings describe the same activity across devices.</p>}
            {group.gap_seconds >= 60 && <p>{Math.round(group.gap_seconds / 60)} minutes have no claimed captured interval. The tape preserves these gaps.</p>}
            {captureOnly && <p>Recording coverage alone does not establish an activity. Some episodes have no transcript or screen evidence.</p>}
            {hasCaptureGap && <p>{missingSeconds > 0 ? `${missingSeconds} seconds are not covered by source timestamps within the attached recording spans. Exact gap positions are unavailable. ` : 'The sources report missing recording coverage. '}Pausing speech or playback is not a gap if recording continues.</p>}
          </div>
        </details>}
        <div className="grid min-w-0 rounded-lg border border-[var(--tape-line)] lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div className="min-w-0 rounded-t-lg bg-[var(--tape-paper)] p-3 lg:rounded-l-lg lg:rounded-tr-none">
            <div className="mb-3 flex items-baseline justify-between gap-2"><h4 className="text-xs font-semibold">Episode tape <span className="ml-1 font-normal text-gray-500 dark:text-gray-400">{members.length} intervals</span></h4><span className="text-[11px] text-gray-500 dark:text-gray-400">Select to inspect</span></div>
            <div className="mb-2 flex justify-between text-[11px] tabular-nums text-gray-500 dark:text-gray-400"><span>{time(group.started_at, timezone)}</span><span>{time(group.ended_at, timezone)}</span></div>
            <div className="max-h-48 space-y-1 overflow-y-auto pr-1 lg:max-h-80" aria-label={`Episode intervals for ${group.title}`}>
                {members.map((episode, index) => {
                  const active = selected.episode_key === episode.episode_key
                  return <button key={episode.episode_key} type="button" aria-pressed={active} onClick={() => { setSelectedKey(episode.episode_key); setRejectingKey(null); setSelectedEvidenceId(null); setEvidenceTab('screen') }} className={`block w-full rounded-md border p-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--tape-focus)] ${active ? 'border-[var(--tape-focus)] bg-[var(--tape-selected)]' : 'border-transparent hover:bg-[var(--tape-paper)]'}`}>
                    <span className="flex items-start gap-2 text-xs"><span className="text-gray-500 dark:text-gray-400">{index + 1}</span><span className="min-w-0 flex-1 truncate" title={episodeDisplayTitle(episode)}>{episodeDisplayTitle(episode)}</span>{!episode.requires_activity_review && <span className="shrink-0 text-[10px] text-gray-500 dark:text-gray-400">Reference only</span>}{episode.requires_activity_review && !pending.has(episode.episode_id) && <Check aria-label="Already reviewed" className="h-3.5 w-3.5 shrink-0 text-green-700" />}</span>
                    <span className="mt-1 block text-[11px] text-gray-500 dark:text-gray-400">{timeRange(episode.started_at, episode.ended_at, timezone)} · {episode.activity_mode}</span>
                    <span className="relative mt-2 block h-2 rounded bg-[var(--tape-chip)]">
                      {group.intervals.filter(interval => interval.episode_id === episode.episode_id).map((interval, occurrence) => <span key={occurrence} className="absolute h-2 min-w-[3px] rounded" style={{ left: `${Math.max(0, (Date.parse(interval.started_at) - start) / span * 100)}%`, width: `${Math.max(0, (Date.parse(interval.ended_at) - Date.parse(interval.started_at)) / span * 100)}%`, backgroundColor: `var(--tape-${interval.lane === 'conversation' ? 'conversation' : interval.lane === 'background' ? 'background' : 'activity'})` }} />)}
                    </span>
                  </button>
                })}
              </div>
            </div>
          <div key={selected.episode_id} className="min-w-0 border-t border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-4 lg:rounded-r-lg lg:border-l lg:border-t-0" aria-label="Selected episode evidence">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Evidence · Episode {members.indexOf(selected) + 1}</p>
            <h4 className="mt-1 break-words text-sm font-semibold leading-5">{episodeDisplayTitle(selected)}</h4>
            <p className="mt-1 text-xs tabular-nums text-gray-500 dark:text-gray-400">{timeRange(selected.started_at, selected.ended_at, timezone)}</p>
            <div className="mt-3 max-h-80 overflow-y-auto pr-1">
              <p className="text-xs leading-5 text-gray-700 dark:text-gray-200">{selected.summary ? `${selected.summary.slice(0, 220)}${selected.summary.length > 220 ? '…' : ''}` : 'No episode summary is available.'}</p>
              {selected.summary?.length > 220 && <details className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">Full summary</summary><p>{selected.summary}</p></details>}
              <div className="mt-3 flex flex-wrap gap-1 border-b border-[var(--tape-line)] pb-2" aria-label="Evidence type">
                {screens.length > 0 && <Button variant={tab === 'screen' ? 'secondary' : 'ghost'} aria-pressed={tab === 'screen'} onClick={() => setEvidenceTab('screen')} icon={<Monitor className="h-3.5 w-3.5" />}>Screen OCR</Button>}
                {photos.length > 0 && <Button variant={tab === 'photo' ? 'secondary' : 'ghost'} aria-pressed={tab === 'photo'} onClick={() => setEvidenceTab('photo')} icon={<Image className="h-3.5 w-3.5" />}>Photo</Button>}
                {transcript && <Button variant={tab === 'transcript' ? 'secondary' : 'ghost'} aria-pressed={tab === 'transcript'} onClick={() => setEvidenceTab('transcript')} icon={<MessageSquare className="h-3.5 w-3.5" />}>Transcript</Button>}
              </div>
              {tab !== 'transcript' && <>
                {visualEvidence.length > 1 && <select aria-label={tab === 'photo' ? 'Photo source' : 'Screen OCR source'} value={selectedVisual?.evidence_id} onChange={event => setSelectedEvidenceId(event.target.value)} className="mt-2 min-h-9 w-full min-w-0 rounded border border-[var(--tape-line)] bg-[var(--tape-paper)] px-2 text-xs">
                  {visualEvidence.map(item => <option key={item.evidence_id} value={item.evidence_id}>{item.kind === 'immich' ? 'Photo' : 'Screen OCR'} · {time(item.started_at, timezone)} · {String(item.metadata?.window_name || item.metadata?.app_name || item.excerpt?.slice(0, 80) || item.kind)}</option>)}
                </select>}
                <div className="[&_img]:mt-2 [&_img]:max-h-44"><EpisodeThumbnail episode={selected} sourceItemId={imageSourceId} /></div>
                {(selected.has_thumbnail || imageSourceId) && <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{imageSourceId ? 'Screen capture' : 'Episode preview'}{imageEvidence ? ` · ${time(imageEvidence.started_at, timezone)}` : ''}</p>}
              </>}
              {excerpt ? <div key={excerpt.evidence_id} className="mt-3 text-xs leading-5">
                <p className="font-medium text-gray-500 dark:text-gray-400">{tab === 'transcript' ? 'Transcript sample' : tab === 'photo' ? 'Photo description' : 'Screen OCR · text read from the screen'} · {time(excerpt.started_at, timezone)}</p>
                <p className="mt-1 break-words">{excerpt.excerpt!.replace(/\s+/g, ' ').slice(0, 180)}{excerpt.excerpt!.length > 180 ? '…' : ''}</p>
                <details className="mt-2 text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">Raw text & source</summary><p className="my-2 break-words text-[11px]">{excerpt.locator?.track_id || excerpt.locator?.capture_source_id || excerpt.source_id}</p><p className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words">{excerpt.excerpt}</p></details>
              </div> : <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">No transcript or screen text is attached. Recording coverage alone does not verify this activity.</p>}
              <details className="mt-3 border-t border-[var(--tape-line)] pt-2 text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">All sources & revision · {selected.evidence.length} items</summary><ul className="mt-2 space-y-1 break-words">{tracks.map(track => <li key={track}>{track}</li>)}</ul><p className="mt-2">Revision {selected.revision} · {!selected.requires_activity_review ? 'Reference only · no activity confirmation needed' : pending.has(selected.episode_id) ? 'Awaiting review' : 'Structure reviewed'}</p></details>
            </div>
          </div>
        </div>
        {group.summary && <details className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">Why these episodes were grouped</summary><p className="mt-2">{group.summary}</p></details>}
        <div role="group" aria-label="Session review actions" className="sticky bottom-0 z-10 -mx-3 mt-3 grid grid-cols-2 items-center gap-2 border-t border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-3 sm:-mx-4 sm:flex sm:flex-wrap sm:justify-between sm:px-4">
          {rejectingKey === selected.episode_key ? <div className="col-span-2 w-full rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200" role="group" aria-label="Confirm not an activity">
            <p className="font-semibold">Remove episode {members.indexOf(selected) + 1} from the timeline?</p>
            <p className="mt-1 leading-5">Recordings stay. This episode will not be recreated from the same evidence. New evidence can still be reviewed.</p>
            <div className="mt-2 flex gap-2"><Button variant="danger" disabled={reviewBusy} onClick={() => onNotActivity(selected)}>{rejectingEpisodeId === selected.episode_id ? 'Removing…' : 'Remove episode'}</Button><Button variant="ghost" disabled={reviewBusy} onClick={() => setRejectingKey(null)}>Cancel</Button></div>
            {rejectionError && <p role="alert" className="mt-2">Could not remove this episode. {rejectionError.message}</p>}
          </div> : <div className="col-span-2 flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" className="min-h-10" title="Adjust times, split, or remove the selected episode" disabled={reviewBusy} onClick={() => onEdit(selected)} icon={<Pencil className="h-3.5 w-3.5" />}>Edit episode {members.indexOf(selected) + 1}</Button>
            <Button variant="ghost" className="min-h-10" disabled={reviewBusy} onClick={() => setRejectingKey(selected.episode_key)}>Not an activity</Button>
          </div>}
          <div className="contents sm:flex sm:items-center sm:gap-2">
            {sessions.length > 1 && <Button variant="ghost" className="min-h-10 justify-self-end" onClick={() => chooseSession(sessions[(sessionIndex + 1) % sessions.length].group_id)}>Later</Button>}
            <Button size="sm" variant="primary" className="col-span-2 min-h-10 w-full sm:w-auto" disabled={reviewBusy || rejectingKey === selected.episode_key} onClick={() => onConfirm(group.group_id, unsettled)} icon={busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}>{busy ? 'Saving review…' : `Confirm session (${unsettled.length})`}</Button>
          </div>
          <p className="col-span-2 w-full text-center text-[11px] sm:text-right text-gray-500 dark:text-gray-400">Confirms {unsettled.length} episode{unsettled.length === 1 ? '' : 's'} · Memory is a separate step</p>
        </div>
      </section>
    })}
  </div>
}
