import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Archive, AudioLines, BookOpen, Check, CircleDot, Eye, FileDiff, Image, Loader2, LockKeyhole, MessageCircle, Mic, MonitorUp, Play, Plus, RefreshCw, Save, Search, Send, Wifi } from 'lucide-react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import LiveRecord from './LiveRecord'
import { spaceStateLabel, spaceSyncLabel } from '../utils/memorySpaceStatus'
import { TITLE_NOT_GENERATED } from '../lib/constants'
import { sourceDate } from '../utils/sourceTime'
import { Button, Input, Textarea, computeWordDiff, WordDiff } from '../components/ui'
import { memorySpacesApi, type MemorySpace, type SpaceMergeProposal, type SpaceNoteReviewFrame } from '../services/api'

const TABS = [
  { id: 'record', label: 'Record', icon: Mic },
  { id: 'notes', label: 'Notes', icon: BookOpen },
  { id: 'chat', label: 'Chat', icon: MessageCircle },
  { id: 'merge', label: 'Merge', icon: FileDiff },
] as const

type TabId = typeof TABS[number]['id']

function errorDetail(error: unknown) {
  const candidate = error as { response?: { data?: { detail?: string } }; message?: string }
  return candidate?.response?.data?.detail || candidate?.message || 'Something went wrong'
}

function ScopeStrip({ name, state, syncState }: { name: string; state: MemorySpace['state']; syncState: MemorySpace['sync_state'] }) {
  return (
    <div className="sticky top-0 z-20 -mx-4 mb-6 border-y border-[#cfc3aa] bg-[#f4efdf]/95 px-4 py-2.5 backdrop-blur dark:border-stone-700 dark:bg-[#211f1a]/95 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-stone-700 dark:text-stone-300">
        <span className="flex items-center gap-2 font-semibold"><BookOpen className="h-4 w-4 text-emerald-800 dark:text-emerald-300" />{name}</span>
        <span className="flex items-center gap-1.5"><LockKeyhole className="h-3.5 w-3.5" />Main sealed</span>
        <span className="flex items-center gap-1.5"><Wifi className="h-3.5 w-3.5" />{spaceSyncLabel[syncState]}</span>
        <span className="flex items-center gap-1.5"><CircleDot className="h-3.5 w-3.5 text-red-700 dark:text-red-400" />{state === 'active' ? 'Recordings land here' : state === 'archived' ? 'Read-only' : 'Edits paused'}</span>
        <span className="ml-auto uppercase tracking-[0.15em] text-stone-500">{spaceStateLabel[state]}</span>
      </div>
    </div>
  )
}

function SpaceRecordingAudio({ conversationId, title }: { conversationId: string; title: string }) {
  const [requested, setRequested] = useState(false)
  const [audioUrl, setAudioUrl] = useState<string>()
  const audio = useQuery({
    queryKey: ['memory-space-recording-audio', conversationId],
    queryFn: () => memorySpacesApi.recordingAudio(conversationId).then(response => response.data),
    enabled: requested,
    staleTime: Infinity,
  })

  useEffect(() => {
    if (!audio.data) return
    const next = URL.createObjectURL(audio.data)
    setAudioUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [audio.data])

  return (
    <div className="flex min-h-10 flex-wrap items-center gap-3 border-l border-stone-300 pl-3 dark:border-stone-700">
      <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-stone-500">
        <AudioLines className="h-3.5 w-3.5" />Captured audio
      </span>
      {!requested && (
        <Button size="sm" variant="secondary" icon={<Play className="h-3.5 w-3.5" />} onClick={() => setRequested(true)}>
          Load recording
        </Button>
      )}
      {requested && audio.isLoading && <span className="flex items-center gap-1.5 text-xs text-stone-500"><Loader2 className="h-3.5 w-3.5 animate-spin" />Preparing playback…</span>}
      {audioUrl && <audio controls preload="metadata" src={audioUrl} aria-label={`Playback for ${title}`} className="h-9 min-w-0 flex-1" />}
      {audio.isError && <p className="text-xs text-red-700 dark:text-red-300">Could not load this recording. {errorDetail(audio.error)}</p>}
    </div>
  )
}

function NotesTab({ spaceId, archived }: { spaceId: string; archived: boolean }) {
  const queryClient = useQueryClient()
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [draftPath, setDraftPath] = useState('')
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const notes = useQuery({
    queryKey: ['memory-space-notes', spaceId],
    queryFn: () => memorySpacesApi.notes(spaceId).then(response => response.data),
  })
  const selectedNote = notes.data?.find(note => note.note_path === selectedPath)
  useEffect(() => {
    if (selectedNote) {
      setDraftPath(selectedNote.note_path)
      setDraft(selectedNote.content)
    }
  }, [selectedNote])
  const save = useMutation({
    mutationFn: () => memorySpacesApi.writeNote(spaceId, draftPath.trim(), draft),
    onSuccess: async () => {
      setSelectedPath(draftPath.trim())
      await queryClient.invalidateQueries({ queryKey: ['memory-space-notes', spaceId] })
    },
  })
  const filtered = (notes.data ?? []).filter(note => note.note_path.toLowerCase().includes(search.toLowerCase()))

  const newNote = () => {
    setSelectedPath(null)
    setDraftPath('Notes/Untitled.md')
    setDraft('# Untitled\n\n')
  }

  return (
    <div className="grid min-h-[34rem] gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <aside className="border-r border-stone-300 pr-5 dark:border-stone-700">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-stone-400" />
            <Input className="pl-8" value={search} onChange={event => setSearch(event.target.value)} placeholder="Filter notes" />
          </div>
          {!archived && <Button size="sm" variant="secondary" onClick={newNote} aria-label="New note"><Plus className="h-4 w-4" /></Button>}
        </div>
        {notes.isLoading && <p className="py-3 text-xs text-stone-500">Loading notes…</p>}
        {notes.isError && <p role="alert" className="py-3 text-xs text-red-700 dark:text-red-300">Could not load notes. <button className="underline" onClick={() => notes.refetch()}>Retry</button></p>}
        {notes.isSuccess && !filtered.length && <p className="py-3 text-xs text-stone-500">{search ? 'No notes match this filter.' : 'No notes yet.'}</p>}
        <div className="mt-3 divide-y divide-stone-200 dark:divide-stone-800">
          {filtered.map(note => (
            <button
              key={note.note_path}
              onClick={() => setSelectedPath(note.note_path)}
              className={`block w-full break-all py-2.5 text-left font-mono text-xs ${selectedPath === note.note_path ? 'text-emerald-800 dark:text-emerald-300' : 'text-stone-600 hover:text-stone-950 dark:text-stone-400 dark:hover:text-stone-100'}`}
            >
              {note.note_path}
            </button>
          ))}
        </div>
      </aside>
      <section>
        {draftPath ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Input value={draftPath} onChange={event => setDraftPath(event.target.value)} disabled={!!selectedNote || archived} className="min-w-64 flex-1 font-mono text-xs" />
              {!archived && <Button size="sm" icon={save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} disabled={!draftPath.trim() || save.isPending} onClick={() => save.mutate()}>Save</Button>}
            </div>
            <Textarea value={draft} onChange={event => setDraft(event.target.value)} disabled={archived} className="min-h-[30rem] resize-y font-mono text-sm leading-6" />
            {save.isError && <p className="text-xs text-red-700 dark:text-red-300">{errorDetail(save.error)}</p>}
          </div>
        ) : (
          <div className="flex min-h-[28rem] items-center justify-center text-center text-sm text-stone-500">
            <div><BookOpen className="mx-auto mb-3 h-7 w-7" />Choose a note{archived ? '.' : ' or begin a new page.'}</div>
          </div>
        )}
      </section>
    </div>
  )
}

function ChatTab({ spaceId, archived }: { spaceId: string; archived: boolean }) {
  const [sessionId, setSessionId] = useState<string>()
  const [message, setMessage] = useState('')
  const [turns, setTurns] = useState<Array<{ role: 'user' | 'assistant'; content: string }>>([])
  const chat = useMutation({
    mutationFn: (text: string) => memorySpacesApi.chat(spaceId, text, sessionId),
    onMutate: text => {
      setTurns(current => [...current, { role: 'user', content: text }])
      setMessage('')
    },
    onSuccess: response => {
      setSessionId(response.data.session_id)
      setTurns(current => [...current, { role: 'assistant', content: response.data.content }])
    },
  })
  return (
    <div className="mx-auto flex min-h-[34rem] max-w-3xl flex-col">
      <div className="flex-1 space-y-5 py-4">
        {!turns.length && <p className="border-l-2 border-[#9b8b69] pl-4 text-sm leading-6 text-stone-600 dark:text-stone-400">Uses only this notebook, including notes copied from Main.</p>}
        {turns.map((turn, index) => (
          <div key={index} className={turn.role === 'user' ? 'ml-auto max-w-[85%] border-r-2 border-stone-400 pr-4 text-right' : 'max-w-[90%] border-l-2 border-emerald-700 pl-4'}>
            <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-stone-400">{turn.role}</p>
            <p className="whitespace-pre-wrap text-sm leading-6 text-stone-800 dark:text-stone-200">{turn.content}</p>
          </div>
        ))}
        {chat.isPending && <p className="flex items-center gap-2 text-sm text-stone-500"><Loader2 className="h-4 w-4 animate-spin" />Thinking inside this space…</p>}
      </div>
      <form
        className="flex gap-2 border-t border-stone-300 pt-4 dark:border-stone-700"
        onSubmit={event => {
          event.preventDefault()
          if (message.trim() && !chat.isPending) chat.mutate(message.trim())
        }}
      >
        <Textarea value={message} onChange={event => setMessage(event.target.value)} disabled={archived} rows={2} placeholder={archived ? 'Reopen this space to continue chatting' : 'Think with this notebook…'} className="min-h-14 flex-1 resize-none" />
        <Button type="submit" disabled={archived || !message.trim() || chat.isPending} aria-label="Send"><Send className="h-4 w-4" /></Button>
      </form>
      {chat.isError && <p className="mt-2 text-xs text-red-700 dark:text-red-300">{errorDetail(chat.error)}</p>}
    </div>
  )
}

function ReviewFrameImage({ spaceId, conversationId, frame }: { spaceId: string; conversationId: string; frame: SpaceNoteReviewFrame }) {
  const [url, setUrl] = useState<string>()
  const image = useQuery({
    queryKey: ['memory-space-note-review-frame', conversationId, frame.key],
    queryFn: () => memorySpacesApi.noteReviewFrame(spaceId, conversationId, frame.source_id, frame.frame_id).then(response => response.data),
  })
  useEffect(() => {
    if (!image.data) return
    const next = URL.createObjectURL(image.data)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [image.data])
  if (image.isError) return <p className="py-4 text-xs text-red-700 dark:text-red-300">Could not load this screen frame.</p>
  if (!url) return <div className="flex aspect-video items-center justify-center bg-stone-200 text-stone-500 dark:bg-stone-900"><Loader2 className="h-4 w-4 animate-spin" /></div>
  return <img src={url} alt={`Screen context frame ${frame.frame_id}`} className="aspect-video w-full object-cover" />
}

function NoteExtractionReview({ spaceId, conversationId, archived }: { spaceId: string; conversationId: string; archived: boolean }) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const review = useQuery({
    queryKey: ['memory-space-note-review', spaceId, conversationId],
    queryFn: () => memorySpacesApi.noteReview(spaceId, conversationId).then(response => response.data),
    refetchInterval: query => ['context_requested', 'extracting'].includes(query.state.data?.review_state ?? '') ? 3_000 : false,
  })
  useEffect(() => {
    if (review.data?.selected_frame_keys?.length) setSelected(new Set(review.data.selected_frame_keys))
  }, [review.data?.selected_frame_keys])
  const request = useMutation({
    mutationFn: (sourceId: string) => memorySpacesApi.requestNoteContext(spaceId, conversationId, sourceId),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['memory-space-note-review', spaceId, conversationId] }),
  })
  const extract = useMutation({
    mutationFn: () => memorySpacesApi.extractReviewedNote(spaceId, conversationId, [...selected]),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['memory-space-note-review', spaceId, conversationId] }),
        queryClient.invalidateQueries({ queryKey: ['memory-space-recordings', spaceId] }),
        queryClient.invalidateQueries({ queryKey: ['memory-space-notes', spaceId] }),
      ])
    },
  })
  if (review.isLoading) return <div className="py-4 text-xs text-stone-500">Loading transcript checkpoint…</div>
  if (review.isError) return <p role="alert" className="py-4 text-xs text-red-700 dark:text-red-300">Could not load this transcript checkpoint. <button className="underline" onClick={() => review.refetch()}>Retry</button></p>
  if (!review.data) return null
  const data = review.data
  const sourceName = new Map(data.sources.map(source => [source.source_id, source.name]))
  const extracted = data.review_state === 'extracted'
  const working = data.review_state === 'extracting'

  return (
    <section className="border-l-2 border-[#9b8b69] pl-4 sm:pl-5" aria-label="Note extraction review">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-stone-500">Transcript → screen evidence → note</p>
          <h3 className="mt-1 text-sm font-semibold">{data.title || 'Untitled recording'}</h3>
        </div>
        <span className={`text-xs ${extracted ? 'text-emerald-700 dark:text-emerald-300' : 'text-stone-500'}`}>
          {extracted ? 'Note extracted' : working ? 'Extracting with selected evidence…' : 'Awaiting review'}
        </span>
      </div>
      <details className="mt-3" open={!extracted}>
        <summary className="cursor-pointer text-xs font-medium text-stone-600 dark:text-stone-300">Read transcript</summary>
        <p className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap border-l border-stone-300 pl-3 text-xs leading-5 text-stone-600 dark:border-stone-700 dark:text-stone-300">{data.transcript || 'Transcript is still processing.'}</p>
      </details>

      {!extracted && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="mr-1 flex items-center gap-1.5 text-xs font-medium text-stone-600 dark:text-stone-300"><MonitorUp className="h-3.5 w-3.5" />Pull screen context</span>
            {data.sources.map(source => (
              <Button key={source.source_id} size="sm" variant="secondary" disabled={archived || request.isPending || working} onClick={() => request.mutate(source.source_id)}>
                {source.name}{source.status !== 'online' ? ` · ${source.status}` : ''}
              </Button>
            ))}
            {!data.sources.length && <span className="text-xs text-amber-700 dark:text-amber-300">No paired ScreenPipe source is available.</span>}
            {data.review_state === 'context_requested' && <span className="flex items-center gap-1 text-xs text-stone-500"><Loader2 className="h-3.5 w-3.5 animate-spin" />Waiting for the source to return frames…</span>}
          </div>

          {!!data.frames.length && (
            <div className="mt-4">
              <div className="mb-2 flex items-center justify-between gap-3"><span className="flex items-center gap-1.5 text-xs font-medium text-stone-600 dark:text-stone-300"><Image className="h-3.5 w-3.5" />Choose what the note extractor can see</span><span className="text-[10px] uppercase tracking-wider text-stone-400">{selected.size} selected</span></div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {data.frames.map(frame => {
                  const checked = selected.has(frame.key)
                  return (
                    <label key={frame.key} className={`cursor-pointer overflow-hidden border ${checked ? 'border-emerald-700 ring-1 ring-emerald-700' : 'border-stone-300 dark:border-stone-700'}`}>
                      <ReviewFrameImage spaceId={spaceId} conversationId={conversationId} frame={frame} />
                      <span className="flex items-center gap-2 px-2.5 py-2 text-[10px] text-stone-500">
                        <input type="checkbox" checked={checked} onChange={() => setSelected(current => {
                          const next = new Set(current)
                          next.has(frame.key) ? next.delete(frame.key) : next.add(frame.key)
                          return next
                        })} />
                        <span className="min-w-0 truncate">{sourceName.get(frame.source_id) || frame.source_id}</span>
                        <span className="ml-auto tabular-nums">{frame.captured_at ? new Date(frame.captured_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : `#${frame.frame_id}`}</span>
                      </span>
                    </label>
                  )
                })}
              </div>
              <p className="mt-2 flex items-start gap-1.5 text-[11px] leading-4 text-stone-500"><Eye className="mt-0.5 h-3 w-3 shrink-0" />Only checked frames are sent as pixels to the note-extraction LLM. Main remains sealed.</p>
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button size="sm" disabled={archived || working || extract.isPending || !data.transcript} icon={(working || extract.isPending) ? <Loader2 className="h-4 w-4 animate-spin" /> : <BookOpen className="h-4 w-4" />} onClick={() => extract.mutate()}>
              {selected.size ? `Extract note with ${selected.size} image${selected.size === 1 ? '' : 's'}` : 'Extract transcript only'}
            </Button>
            {!!selected.size && <button className="text-xs text-stone-500 underline-offset-2 hover:underline" onClick={() => setSelected(new Set())}>Clear selection</button>}
          </div>
        </>
      )}
      {(request.isError || extract.isError || data.review_error) && <p className="mt-3 text-xs text-red-700 dark:text-red-300">{request.isError ? errorDetail(request.error) : extract.isError ? errorDetail(extract.error) : data.review_error}</p>}
      {data.context_description && <details className="mt-3"><summary className="cursor-pointer text-[11px] text-stone-500">Visual grounding used</summary><p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-stone-600 dark:text-stone-300">{data.context_description}</p></details>}
    </section>
  )
}

function MergeTab({ spaceId, spaceState }: { spaceId: string; spaceState: string }) {
  const queryClient = useQueryClient()
  const archived = spaceState === 'archived'
  const [proposal, setProposal] = useState<SpaceMergeProposal | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [recoveringProposal, setRecoveringProposal] = useState(false)
  const latestProposal = useQuery({
    queryKey: ['memory-space-latest-merge', spaceId],
    queryFn: () => memorySpacesApi.latestMergeProposal(spaceId).then(response => response.data),
    enabled: spaceState === 'merging' || archived || recoveringProposal,
    refetchInterval: query => recoveringProposal && !query.state.data ? 2_000 : false,
  })
  useEffect(() => {
    if (!proposal && latestProposal.data) {
      setProposal(latestProposal.data)
      setRecoveringProposal(false)
      if (latestProposal.data.state === 'pending') {
        setSelected(new Set(latestProposal.data.changes.filter(change => !change.conflict).map(change => change.change_id)))
      }
    }
  }, [latestProposal.data, proposal])
  const deferredEvents = useQuery({
    queryKey: ['memory-space-deferred-events', spaceId],
    queryFn: () => memorySpacesApi.deferredEvents(spaceId).then(response => response.data),
    enabled: proposal?.state === 'applied',
  })
  const retryEvent = useMutation({
    mutationFn: (eventId: string) => memorySpacesApi.retryDeferredEvent(spaceId, eventId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['memory-space-deferred-events', spaceId] })
    },
  })
  const prepare = useMutation({
    mutationFn: (acknowledge: boolean) => memorySpacesApi.prepareMerge(spaceId, acknowledge),
    onMutate: () => {
      setRecoveringProposal(true)
    },
    onSuccess: response => {
      setRecoveringProposal(false)
      setProposal(response.data)
      setSelected(new Set(response.data.changes.filter(change => !change.conflict).map(change => change.change_id)))
    },
    onError: (error: unknown) => {
      const response = (error as { response?: unknown })?.response
      if (response) setRecoveringProposal(false)
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ['memory-space', spaceId] })
    },
  })
  const resolve = useMutation({
    mutationFn: (accepted?: string[]) => memorySpacesApi.resolveMerge(
      proposal!.proposal_id,
      accepted ?? [...selected],
    ),
    onSuccess: async response => {
      setProposal(response.data)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['memory-space', spaceId] }),
        queryClient.invalidateQueries({ queryKey: ['memory-spaces'] }),
      ])
    },
  })
  const cancel = useMutation({
    mutationFn: () => memorySpacesApi.cancelMerge(proposal!.proposal_id),
    onSuccess: async () => {
      queryClient.setQueryData(['memory-space-latest-merge', spaceId], null)
      setProposal(null)
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['memory-space', spaceId] }),
        queryClient.invalidateQueries({ queryKey: ['memory-spaces'] }),
      ])
      queryClient.removeQueries({ queryKey: ['memory-space-latest-merge', spaceId] })
    },
  })
  if (latestProposal.isError && !proposal) {
    return <p role="alert" className="py-12 text-sm text-red-700 dark:text-red-300">Could not load the publication ledger. <button className="underline" onClick={() => latestProposal.refetch()}>Retry</button></p>
  }
  if (latestProposal.isLoading && !proposal) {
    return <p className="flex items-center justify-center gap-2 py-12 text-sm text-stone-500"><Loader2 className="h-4 w-4 animate-spin" />Loading publication ledger…</p>
  }
  if (archived && !proposal) {
    return <div className="py-12 text-center text-sm text-stone-500"><Archive className="mx-auto mb-3 h-7 w-7" />This cycle is archived. Reopen it to begin a fresh checkpoint.</div>
  }
  if (!proposal) {
    const detail = prepare.isError ? errorDetail(prepare.error) : ''
    const needsAck = /offline|stale/i.test(detail)
    return (
      <div className="mx-auto max-w-2xl py-10">
        <h2 className="text-xl font-semibold text-stone-900 dark:text-stone-100">Publication ledger</h2>
        <p className="mt-3 text-sm leading-6 text-stone-600 dark:text-stone-400">Preparation pauses notebook edits. Review the proposed changes before publishing to Main.</p>
        <Button className="mt-6" icon={recoveringProposal ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDiff className="h-4 w-4" />} disabled={prepare.isPending || recoveringProposal} onClick={() => prepare.mutate(false)}>Prepare merge</Button>
        {recoveringProposal && <p className="mt-3 text-xs text-stone-500">Reviewing the staged vault… This can take a few minutes; the ledger will appear automatically.</p>}
        {prepare.isError && !recoveringProposal && (
          <div className="mt-4 border-l-2 border-amber-600 pl-4 text-sm text-amber-800 dark:text-amber-300">
            <p>{detail}</p>
            {needsAck && <Button className="mt-3" size="sm" variant="secondary" onClick={() => prepare.mutate(true)}>Acknowledge and continue</Button>}
          </div>
        )}
      </div>
    )
  }
  if (proposal.state === 'applied') {
    const failedEvents = (deferredEvents.data ?? []).filter(event => event.state === 'failed')
    return (
      <div className="py-12 text-center">
        <Check className="mx-auto h-8 w-8 text-emerald-700" />
        <h2 className="mt-3 text-lg font-semibold">Published and archived</h2>
        <p className="mt-1 text-sm text-stone-500">Accepted notes and their supporting recordings were released to Main. Rejected material remains here.</p>
        {!!failedEvents.length && (
          <div className="mx-auto mt-7 max-w-2xl border-l-2 border-amber-600 pl-4 text-left">
            <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-300">{failedEvents.length} automation{failedEvents.length === 1 ? '' : 's'} need attention</h3>
            <div className="mt-2 divide-y divide-stone-300 dark:divide-stone-700">
              {failedEvents.map(event => (
                <div key={event.event_id} className="flex items-center justify-between gap-4 py-3">
                  <span className="min-w-0 text-xs text-stone-600 dark:text-stone-400">
                    <span className="block font-semibold text-stone-800 dark:text-stone-200">{event.event_type}</span>
                    <span className="block truncate">{event.error || 'Delivery failed'} · attempt {event.attempts}</span>
                  </span>
                  <Button size="sm" variant="secondary" disabled={retryEvent.isPending} onClick={() => retryEvent.mutate(event.event_id)}>Retry</Button>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    )
  }
  if (proposal.state !== 'pending') {
    return (
      <div className="mx-auto max-w-2xl py-10">
        <h2 className="text-xl font-semibold">Publication ledger</h2>
        <p className="mt-3 text-sm text-stone-500">This proposal is {proposal.state}. Return to editing to sync or revise the notebook, then prepare a fresh ledger.</p>
        <Button className="mt-6" variant="secondary" disabled={cancel.isPending} icon={cancel.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />} onClick={() => cancel.mutate()}>Return to editing</Button>
        {cancel.isError && <p className="mt-3 text-sm text-red-700 dark:text-red-300">{errorDetail(cancel.error)}</p>}
      </div>
    )
  }
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-stone-300 pb-4 dark:border-stone-700">
        <div><h2 className="text-xl font-semibold">Publication ledger</h2><p className="mt-1 text-sm text-stone-500">{proposal.changes.length} note changes · {proposal.deferred_event_count} deferred automations</p></div>
        <Button disabled={resolve.isPending} icon={resolve.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} onClick={() => resolve.mutate(undefined)}>Publish {selected.size} selected</Button>
      </div>
      {proposal.changes.map(change => {
        const diff = computeWordDiff(change.before_text ?? '', change.after_text ?? '')
        const checked = selected.has(change.change_id)
        return (
          <article key={change.change_id} className="border-b border-stone-300 pb-5 dark:border-stone-700">
            <label className={`flex gap-3 ${change.conflict ? 'cursor-not-allowed opacity-70' : 'cursor-pointer'}`}>
              <input type="checkbox" className="mt-1 h-4 w-4 rounded text-emerald-700" disabled={!!change.conflict} checked={checked} onChange={() => setSelected(current => {
                const next = new Set(current)
                next.has(change.change_id) ? next.delete(change.change_id) : next.add(change.change_id)
                return next
              })} />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2"><span className="break-all font-mono text-xs font-semibold">{change.note_path}</span><span className="text-[10px] uppercase tracking-wider text-stone-500">{change.operation}</span></span>
                {!!change.source_refs.length && <span className="mt-1 block text-xs text-stone-500">Sources: {change.source_refs.map(ref => `${ref.kind} ${ref.source_id.slice(0, 12)}`).join(', ')}</span>}
                {change.conflict && <span className="mt-2 flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5" />{change.conflict}</span>}
              </span>
            </label>
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap bg-[#eee9dc] p-3 text-xs leading-5 text-stone-700 dark:bg-stone-950 dark:text-stone-300">{change.before_text == null ? <em className="text-stone-400">New note</em> : <WordDiff tokens={diff.beforeTokens} />}</pre>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap bg-[#f2eee2] p-3 text-xs leading-5 text-stone-700 dark:bg-[#171b17] dark:text-stone-300">{change.after_text == null ? <em className="text-stone-400">Delete note</em> : <WordDiff tokens={diff.afterTokens} />}</pre>
            </div>
          </article>
        )
      })}
      {!proposal.changes.length && <p className="py-8 text-center text-sm text-stone-500">No workspace edits differ from the checkpoint. Publishing will archive the cycle without changing Main.</p>}
      <div className="flex flex-wrap gap-2"><Button disabled={resolve.isPending || cancel.isPending} onClick={() => resolve.mutate(undefined)}>Finish merge</Button><Button variant="secondary" disabled={resolve.isPending || cancel.isPending} onClick={() => resolve.mutate([])}>Reject all and archive</Button><Button variant="secondary" disabled={resolve.isPending || cancel.isPending} onClick={() => cancel.mutate()}>Return to editing</Button></div>
      {resolve.isError && <p className="text-sm text-red-700 dark:text-red-300">{errorDetail(resolve.error)} The selected batch was not applied; regenerate against current Main.</p>}
      {cancel.isError && <p className="text-sm text-red-700 dark:text-red-300">{errorDetail(cancel.error)}</p>}
    </div>
  )
}

export default function MemorySpaceWorkspace() {
  const { spaceId, tab } = useParams<{ spaceId: string; tab?: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const activeTab = (TABS.some(item => item.id === tab) ? tab : 'record') as TabId
  const space = useQuery({
    queryKey: ['memory-space', spaceId],
    queryFn: () => memorySpacesApi.get(spaceId!).then(response => response.data),
    enabled: !!spaceId,
  })
  const recordings = useQuery({
    queryKey: ['memory-space-recordings', spaceId],
    queryFn: () => memorySpacesApi.recordings(spaceId!).then(response => response.data),
    enabled: !!spaceId && activeTab === 'record',
    refetchInterval: activeTab === 'record' ? 10_000 : false,
  })
  const reopen = useMutation({
    mutationFn: () => memorySpacesApi.reopen(spaceId!),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['memory-space', spaceId] })
      navigate(`/spaces/${spaceId}/record`)
    },
  })

  if (!spaceId) return <Navigate to="/spaces" replace />
  if (space.isLoading) return <p className="py-16 text-center text-sm text-stone-500">Opening sealed notebook…</p>
  if (space.isError) return <div role="alert" className="space-y-3 py-12 text-center text-sm text-red-700 dark:text-red-300"><p>{(space.error as { response?: { status?: number } }).response?.status === 404 ? 'Memory space not found.' : 'Could not load this memory space.'}</p><Button variant="secondary" onClick={() => space.refetch()}>Retry</Button></div>
  if (!space.data) return null
  const archived = space.data.state === 'archived'
  const recordingRows = (recordings.data as { conversations?: Array<{ conversation_id: string; title?: string; created_at?: string; processing_status?: string; memory_review_state?: string }> } | undefined)?.conversations ?? []

  return (
    <main className="-m-4 min-h-[calc(100vh-6rem)] bg-[#f7f3e8] p-4 text-stone-900 dark:bg-[#191814] dark:text-stone-100 sm:-m-6 sm:p-6 lg:-m-8 lg:p-8">
      <ScopeStrip name={space.data.name} state={space.data.state} syncState={space.data.sync_state} />
      <header className="mx-auto max-w-6xl">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div><Link to="/spaces" className="text-xs text-stone-500 hover:text-stone-900 dark:hover:text-stone-100">← All spaces</Link><h1 className="mt-2 text-3xl font-semibold tracking-tight">{space.data.name}</h1></div>
          {archived && <Button variant="secondary" icon={reopen.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />} disabled={reopen.isPending} onClick={() => reopen.mutate()}>Reopen new cycle</Button>}
        </div>
        <nav className="mt-7 flex gap-6 overflow-x-auto border-b border-stone-300 dark:border-stone-700" aria-label="Memory space">
          {TABS.map(item => {
            const Icon = item.icon
            return <Link key={item.id} to={`/spaces/${spaceId}/${item.id}`} className={`flex items-center gap-1.5 border-b-2 px-1 pb-3 text-sm font-medium ${activeTab === item.id ? 'border-emerald-800 text-emerald-900 dark:border-emerald-300 dark:text-emerald-200' : 'border-transparent text-stone-500 hover:text-stone-900 dark:hover:text-stone-100'}`}><Icon className="h-4 w-4" />{item.label}</Link>
          })}
        </nav>
      </header>

      <div className="mx-auto mt-6 max-w-6xl">
        {archived && activeTab !== 'merge' && <div className="mb-5 border-l-2 border-slate-500 pl-4 text-sm text-stone-600 dark:text-stone-400">Archived spaces are read-only. Reopen a new cycle to resume editing and sync.</div>}
        {activeTab === 'record' && (
          <div className="space-y-8">
            {!archived && <LiveRecord memorySpaceId={spaceId} destinationLabel={space.data.name} embedded />}
            <section className="border-t border-stone-300 pt-5 dark:border-stone-700">
              <h2 className="text-sm font-semibold uppercase tracking-[0.14em] text-stone-500">Recordings in this space</h2>
              <div className="mt-3 divide-y divide-stone-200 dark:divide-stone-800">
                {recordings.isLoading && <p className="py-5 text-sm text-stone-500">Loading recordings…</p>}
                {recordings.isError && <p role="alert" className="py-5 text-sm text-red-700 dark:text-red-300">Could not load recordings. <button className="underline" onClick={() => recordings.refetch()}>Retry</button></p>}
                {recordings.isSuccess && !recordingRows.length && <p className="py-5 text-sm text-stone-500">No recordings yet.</p>}
                {recordingRows.map(recording => (
                  <div key={recording.conversation_id} className="space-y-4 py-4">
                    <div className="flex items-center justify-between gap-3"><span><span className="block text-sm font-medium">{recording.title && recording.title !== TITLE_NOT_GENERATED ? recording.title : 'Untitled recording'}</span><span className="text-xs text-stone-500">{recording.created_at ? sourceDate(recording.created_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' }) : ''}</span></span><span className="text-xs text-stone-500">{recording.processing_status ? `Processing: ${recording.processing_status.replace(/_/g, ' ')}` : 'Processing status unavailable'}</span></div>
                    <SpaceRecordingAudio conversationId={recording.conversation_id} title={recording.title || 'recording'} />
                    {recording.memory_review_state && recording.memory_review_state !== 'automatic' && <NoteExtractionReview spaceId={spaceId} conversationId={recording.conversation_id} archived={archived} />}
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
        {activeTab === 'notes' && <NotesTab spaceId={spaceId} archived={archived} />}
        {activeTab === 'chat' && <ChatTab spaceId={spaceId} archived={archived} />}
        {activeTab === 'merge' && <MergeTab spaceId={spaceId} spaceState={space.data.state} />}
      </div>
    </main>
  )
}
