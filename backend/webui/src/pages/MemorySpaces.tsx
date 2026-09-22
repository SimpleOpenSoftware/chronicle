import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, BookOpen, ChevronRight, Link2, Loader2, Plus } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'

import { Button, Input } from '../components/ui'
import { memorySpacesApi } from '../services/api'
import { spaceStateLabel, spaceSyncLabel } from '../utils/memorySpaceStatus'

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export default function MemorySpaces() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [noteQuery, setNoteQuery] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const spaces = useQuery({
    queryKey: ['memory-spaces'],
    queryFn: () => memorySpacesApi.list().then(response => response.data),
  })
  const mainNotes = useQuery({
    queryKey: ['memory-space-main-notes', noteQuery],
    queryFn: () => memorySpacesApi.mainNotes(noteQuery).then(response => response.data),
    enabled: creating,
  })
  const preview = useQuery({
    queryKey: ['memory-space-seed-preview', [...selected].sort()],
    queryFn: () => memorySpacesApi.previewSeed([...selected]).then(response => response.data),
    enabled: creating && selected.size > 0,
  })
  const createSpace = useMutation({
    mutationFn: () => memorySpacesApi.create(name.trim(), [...selected]),
    onSuccess: async response => {
      await queryClient.invalidateQueries({ queryKey: ['memory-spaces'] })
      navigate(`/spaces/${response.data.space_id}/record`)
    },
  })

  const suggestions = useMemo(
    () => (preview.data?.suggestions ?? []).filter((item: { note_path: string }) => !selected.has(item.note_path)),
    [preview.data, selected],
  )

  return (
    <main className="mx-auto max-w-5xl space-y-8">
      <header className="border-b border-stone-300 pb-5 dark:border-stone-700">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-800 dark:text-emerald-300">Private working notebooks</p>
            <h1 className="mt-1 text-3xl font-semibold tracking-tight text-stone-900 dark:text-stone-100">Memory Spaces</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-stone-600 dark:text-stone-400">
              A separate notebook. Publish selected changes to Main when you are ready.
            </p>
          </div>
          <Button icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(value => !value)}>
            {creating ? 'Cancel' : 'New space'}
          </Button>
        </div>
      </header>

      {creating && (
        <section className="space-y-5 border-b border-stone-300 pb-8 dark:border-stone-700" aria-label="Create memory space">
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem]">
            <div className="space-y-4">
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-stone-800 dark:text-stone-200">Notebook name</span>
                <Input value={name} onChange={event => setName(event.target.value)} placeholder="New product brainstorm" autoFocus />
              </label>
              <div>
                <div className="mb-2 flex items-end justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-semibold text-stone-900 dark:text-stone-100">Bring notes from Main <span className="font-normal text-stone-500">— optional</span></h2>
                    <p className="text-xs text-stone-500 dark:text-stone-400">Only notes you check are copied.</p>
                  </div>
                </div>
                <Input value={noteQuery} onChange={event => setNoteQuery(event.target.value)} placeholder="Search Main notes" />
                <div className="mt-2 max-h-72 divide-y divide-stone-200 overflow-auto border-y border-stone-200 dark:divide-stone-800 dark:border-stone-800">
                  {mainNotes.isLoading && <p className="py-4 text-sm text-stone-500">Loading Main notes…</p>}
                  {mainNotes.isError && <p role="alert" className="py-4 text-sm text-red-700 dark:text-red-300">Could not load Main notes. <button type="button" className="underline" onClick={() => mainNotes.refetch()}>Retry</button></p>}
                  {mainNotes.isSuccess && !mainNotes.data.length && <p className="py-4 text-sm text-stone-500">{noteQuery ? 'No notes match this search.' : 'No Main notes available.'}</p>}
                  {mainNotes.data?.map(note => (
                    <label key={note.note_path} className="flex cursor-pointer gap-3 py-3">
                      <input
                        type="checkbox"
                        checked={selected.has(note.note_path)}
                        onChange={() => setSelected(current => {
                          const next = new Set(current)
                          next.has(note.note_path) ? next.delete(note.note_path) : next.add(note.note_path)
                          return next
                        })}
                        className="mt-1 h-4 w-4 rounded border-stone-400 text-emerald-700 focus:ring-emerald-700"
                      />
                      <span className="min-w-0">
                        <span className="block break-all font-mono text-xs font-semibold text-stone-800 dark:text-stone-200">{note.note_path}</span>

                      </span>
                    </label>
                  ))}
                </div>
              </div>
            </div>

            <aside className="border-l border-stone-300 pl-5 dark:border-stone-700">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-stone-500">Starting contents</p>
              <p className="mt-3 text-3xl font-semibold text-stone-900 dark:text-stone-100">{selected.size}</p>
              <p className="text-sm text-stone-500">selected notes · {formatBytes(preview.data?.total_bytes ?? 0)}</p>
              {!selected.size && <p className="mt-4 text-sm leading-6 text-stone-600 dark:text-stone-400">No notes will be copied from Main.</p>}
              {!!suggestions.length && (
                <div className="mt-5">
                  <p className="flex items-center gap-1.5 text-xs font-semibold text-stone-700 dark:text-stone-300"><Link2 className="h-3.5 w-3.5" /> Linked notes</p>
                  <div className="mt-2 space-y-2">
                    {suggestions.map((item: { note_path: string; byte_size: number }) => (
                      <button
                        key={item.note_path}
                        onClick={() => setSelected(current => new Set([...current, item.note_path]))}
                        className="block w-full border-l-2 border-emerald-700 py-1 pl-2 text-left font-mono text-[11px] text-stone-600 hover:text-stone-950 dark:text-stone-400 dark:hover:text-stone-100"
                      >
                        + {item.note_path}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <Button
                className="mt-6 w-full"
                disabled={!name.trim() || createSpace.isPending}
                icon={createSpace.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <BookOpen className="h-4 w-4" />}
                onClick={() => createSpace.mutate()}
              >
                Create sealed space
              </Button>
              {createSpace.isError && <p className="mt-2 text-xs text-red-700 dark:text-red-300">{(createSpace.error as Error).message}</p>}
            </aside>
          </div>
        </section>
      )}

      <section aria-label="Your memory spaces">
        {spaces.isLoading && <p className="py-8 text-sm text-stone-500">Opening the notebook shelf…</p>}
        {spaces.isError && <p role="alert" className="py-8 text-sm text-red-700 dark:text-red-300">Could not load memory spaces. <button className="underline" onClick={() => spaces.refetch()}>Retry</button></p>}
        {spaces.isSuccess && !spaces.data.length && (
          <div className="py-14 text-center">
            <BookOpen className="mx-auto h-8 w-8 text-stone-400" />
            <p className="mt-3 text-sm text-stone-600 dark:text-stone-400">No spaces yet. Start blank, or bring only the Main notes you need.</p>
          </div>
        )}
        <div className="divide-y divide-stone-300 border-y border-stone-300 dark:divide-stone-700 dark:border-stone-700">
          {spaces.data?.map(space => (
            <Link key={space.space_id} to={`/spaces/${space.space_id}/record`} className="group flex items-center gap-4 py-5">
              <span className="flex h-10 w-10 items-center justify-center rounded-full bg-[#e8e0cf] text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                {space.state === 'archived' ? <Archive className="h-4 w-4" /> : <BookOpen className="h-4 w-4" />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-base font-semibold text-stone-900 dark:text-stone-100">{space.name}</span>
                <span className="mt-1 block text-xs text-stone-500 dark:text-stone-400">
                  {spaceStateLabel[space.state]} · {spaceSyncLabel[space.sync_state]}
                </span>
              </span>
              <ChevronRight className="h-5 w-5 text-stone-400 transition-transform group-hover:translate-x-1" />
            </Link>
          ))}
        </div>
      </section>
    </main>
  )
}
