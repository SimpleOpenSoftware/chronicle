import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  ScrollText, RefreshCw, ChevronRight, ChevronDown, ExternalLink,
  Sparkles, Bot, PenLine, Trash2, FileText, type LucideIcon,
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useMemoryLedger, useMemoryAuditDiff } from '../hooks/useMemoryLedger'
import type { MemoryAuditEntry } from '../services/api'
import MemoryReviewWorkspace from '../components/timeline/MemoryReviewWorkspace'
import { Button, MetadataChip, StateBadge, Tabs } from '../components/ui'

// ---- Provenance classification -------------------------------------------
// The ledger answers "who changed this memory and why". The backend owns the
// taxonomy (cause × actor → source_kind/source_label, see services/memory/
// audit.py); the UI maps the coarse `source_kind` to a distinguishing icon and
// prints the precise `source_label` (e.g. "Speaker reprocess" vs "Transcript
// reprocess" both share the reprocess family). The chip stays muted — the icon
// and label carry the category; provenance is metadata, not a state signal.

type SourceKind = MemoryAuditEntry['source_kind']

interface SourceMeta {
  kind: SourceKind
  label: string
  Icon: LucideIcon
}

const KIND_ICON: Record<SourceKind, LucideIcon> = {
  extraction: Sparkles,
  reprocess: RefreshCw,
  human: PenLine,
  agent: Bot,
  bulk: Trash2,
  other: FileText,
}

function classifySource(e: MemoryAuditEntry): SourceMeta {
  const kind = e.source_kind ?? 'other'
  return { kind, label: e.source_label || 'system', Icon: KIND_ICON[kind] ?? FileText }
}

// Deletions are the safety-relevant operation to scan for in an audit log, so
// they keep a restrained danger tint; every other operation is plain metadata.
const isDestructiveOp = (op: string) => op === 'delete' || op === 'delete_all'

const SOURCE_FILTERS: { value: SourceKind | 'all'; label: string }[] = [
  { value: 'all', label: 'All sources' },
  { value: 'extraction', label: 'AI extraction' },
  { value: 'agent', label: 'Memory agent' },
  { value: 'reprocess', label: 'Reprocess' },
  { value: 'human', label: 'Human (Obsidian)' },
  { value: 'bulk', label: 'Bulk delete' },
]

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function formatBytes(n: number | null): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

// ---- Diff rendering --------------------------------------------------------

function DiffView({ entryId }: { entryId: string }) {
  const { data, isLoading, error } = useMemoryAuditDiff(entryId)

  if (isLoading) return <div className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">Loading changes…</div>
  if (error) return <div className="px-4 py-3 text-sm text-red-600 dark:text-red-400">Failed to load diff.</div>
  if (!data) return null
  if (!data.diff_available)
    return <div className="px-4 py-3 text-sm italic text-gray-500 dark:text-gray-400">{data.reason || 'No content recorded for this change.'}</div>
  if (!data.diff.trim())
    return <div className="px-4 py-3 text-sm italic text-gray-500 dark:text-gray-400">No textual change.</div>

  const lines = data.diff.split('\n')
  return (
    <pre className="m-3 overflow-x-auto rounded-md border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed dark:border-gray-700 dark:bg-gray-900">
      {lines.map((ln, i) => {
        let cls = 'text-gray-600 dark:text-gray-400'
        if (ln.startsWith('+++') || ln.startsWith('---')) cls = 'text-gray-400 dark:text-gray-500'
        else if (ln.startsWith('@@')) cls = 'text-cyan-600 dark:text-cyan-400'
        else if (ln.startsWith('+')) cls = 'text-green-700 bg-green-50 dark:bg-green-900/20 dark:text-green-300'
        else if (ln.startsWith('-')) cls = 'text-red-700 bg-red-50 dark:bg-red-900/20 dark:text-red-300'
        return <div key={i} className={`whitespace-pre-wrap break-words font-mono ${cls}`}>{ln || ' '}</div>
      })}
    </pre>
  )
}

// ---- One ledger row --------------------------------------------------------

function LedgerRow({ entry }: { entry: MemoryAuditEntry }) {
  const [open, setOpen] = useState(false)
  const src = classifySource(entry)
  const expandable = entry.has_diff
  const SrcIcon = src.Icon
  const sourceEpisodeKeys = Array.isArray(entry.extra?.relevant_episode_keys)
    ? entry.extra.relevant_episode_keys.filter((key): key is string => typeof key === 'string' && !!key)
    : []

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <button
        type="button"
        onClick={() => expandable && setOpen(o => !o)}
        className={`flex w-full items-center gap-3 px-3 py-2.5 text-left ${
          expandable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer' : 'cursor-default'
        }`}
      >
        <span className="flex-shrink-0 w-4 text-gray-400">
          {expandable ? (open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />) : null}
        </span>

        <MetadataChip className="flex-shrink-0 gap-1">
          <SrcIcon className="h-3.5 w-3.5" />
          {src.label}
        </MetadataChip>

        {isDestructiveOp(entry.operation) ? (
          <StateBadge tone="danger" className="flex-shrink-0">{entry.operation}</StateBadge>
        ) : (
          <MetadataChip className="flex-shrink-0">{entry.operation}</MetadataChip>
        )}

        <span className="min-w-0 flex-1 truncate font-mono text-sm text-gray-800 dark:text-gray-200" title={entry.note_path || ''}>
          {entry.note_path || (entry.operation === 'delete_all' ? `entire vault${entry.extra?.count ? ` (${String(entry.extra.count)} notes)` : ''}` : '—')}
        </span>

        {entry.summary && (
          <span className="flex-shrink-0 hidden sm:inline text-xs text-gray-500 dark:text-gray-400">{entry.summary}</span>
        )}
        {entry.after_bytes != null && (
          <span className="flex-shrink-0 hidden md:inline text-xs text-gray-400 dark:text-gray-500">{formatBytes(entry.after_bytes)}</span>
        )}

        <span className="flex-shrink-0 w-40 text-right text-xs text-gray-500 dark:text-gray-400">{formatTime(entry.created_at)}</span>
      </button>

      {(entry.conversation_id || sourceEpisodeKeys.length > 0) && (
        <details className="px-3 pb-2 text-xs text-gray-600 dark:text-gray-300">
          <summary className="cursor-pointer">Sources · {[entry.conversation_id ? '1 recording' : '', sourceEpisodeKeys.length ? `${sourceEpisodeKeys.length} episode${sourceEpisodeKeys.length === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')}</summary>
          <div className="mt-2 flex flex-wrap gap-3">
            {entry.conversation_id && <Link to={`/recordings/${entry.conversation_id}`} className="inline-flex items-center gap-1 text-[var(--tape-focus)] hover:underline">Recording <ExternalLink className="h-3 w-3" /></Link>}
            {sourceEpisodeKeys.map((key, index) => <Link key={key} to={`/timeline/key/${encodeURIComponent(key)}`} className="inline-flex items-center gap-1 text-[var(--tape-focus)] hover:underline">Episode {index + 1} <ExternalLink className="h-3 w-3" /></Link>)}
          </div>
        </details>
      )}
      {open && expandable && <DiffView entryId={entry.id} />}
    </div>
  )
}

// ---- Summary strip ---------------------------------------------------------

function SummaryStrip({ entries }: { entries: MemoryAuditEntry[] }) {
  const counts = useMemo(() => {
    const c: Record<SourceKind, number> = { extraction: 0, reprocess: 0, human: 0, agent: 0, bulk: 0, other: 0 }
    for (const e of entries) c[classifySource(e).kind]++
    return c
  }, [entries])

  const cards: { label: string; value: number }[] = [
    { label: 'Loaded changes', value: entries.length },
    { label: 'AI extraction', value: counts.extraction },
    { label: 'Memory agent', value: counts.agent },
    { label: 'Reprocess', value: counts.reprocess },
    { label: 'Human (Obsidian)', value: counts.human },
    { label: 'Bulk delete', value: counts.bulk },
  ]

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {cards.map(c => (
        <div key={c.label} className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
          <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{c.value}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400">{c.label}</div>
        </div>
      ))}
    </div>
  )
}

// ---- Workspace -------------------------------------------------------------

export default function MemoryLedger() {
  const [searchParams, setSearchParams] = useSearchParams()
  const view = searchParams.get('view') === 'history' ? 'history' : 'review'
  const setView = (nextView: 'review' | 'history') => {
    const next = new URLSearchParams(searchParams)
    next.set('view', nextView)
    setSearchParams(next)
  }

  return (
    <div className="space-y-5">
      <header>
        <div className="flex items-center gap-2">
          <ScrollText className="h-6 w-6 text-[var(--tape-media)]" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Memory Ledger</h1>
        </div>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {view === 'review'
            ? 'Review proposed changes before they reach your memory vault.'
            : 'Inspect the durable history of changes to the memory vault.'}
        </p>
      </header>
      <Tabs
        variant="underline"
        value={view}
        onChange={setView}
        tabs={[
          { value: 'review', label: 'Review queue' },
          { value: 'history', label: 'Change history' },
        ]}
      />
      {view === 'review' ? <MemoryReviewWorkspace /> : <ChangeHistory />}
    </div>
  )
}

// ---- Change history --------------------------------------------------------

function ChangeHistory() {
  const { isAdmin } = useAuth()

  const [limit, setLimit] = useState(200)
  const [userIdFilter, setUserIdFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState<SourceKind | 'all'>('all')
  const [operationFilter, setOperationFilter] = useState<string>('all')
  const [noteSearch, setNoteSearch] = useState('')
  const [viewMode, setViewMode] = useState<'timeline' | 'by-note'>('timeline')

  const { data, isLoading, error, refetch, isFetching } = useMemoryLedger({
    limit,
    ...(isAdmin && userIdFilter.trim() ? { user_id: userIdFilter.trim() } : {}),
  })

  const entries = data?.entries ?? []

  const filtered = useMemo(() => {
    const q = noteSearch.trim().toLowerCase()
    return entries.filter(e => {
      if (sourceFilter !== 'all' && classifySource(e).kind !== sourceFilter) return false
      if (operationFilter !== 'all' && e.operation !== operationFilter) return false
      if (q && !(e.note_path || '').toLowerCase().includes(q)) return false
      return true
    })
  }, [entries, sourceFilter, operationFilter, noteSearch])

  // By-note grouping. Entries arrive newest-first, so Map insertion order is
  // "most recently changed note first", and each note keeps its own history.
  const groups = useMemo(() => {
    const m = new Map<string, MemoryAuditEntry[]>()
    for (const e of filtered) {
      const key = e.note_path || '(no note)'
      const arr = m.get(key)
      if (arr) arr.push(e)
      else m.set(key, [e])
    }
    return Array.from(m.entries())
  }, [filtered])

  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          icon={<RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />}
        >
          Refresh
        </Button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-300">
          {(error as Error).message || 'Failed to load the memory ledger.'}
        </div>
      )}

      <div className="mb-5">
        <SummaryStrip entries={entries} />
      </div>

      {/* Controls */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="inline-flex rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden">
          {(['timeline', 'by-note'] as const).map(m => (
            <button
              key={m}
              onClick={() => setViewMode(m)}
              className={`px-3 py-1.5 text-sm font-medium ${
                viewMode === m
                  ? 'bg-blue-100 text-blue-900 dark:bg-blue-900 dark:text-blue-100'
                  : 'bg-white text-gray-600 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
              }`}
            >
              {m === 'timeline' ? 'Chronological' : 'By note'}
            </button>
          ))}
        </div>

        <select
          aria-label="Change source"
          value={sourceFilter}
          onChange={e => setSourceFilter(e.target.value as SourceKind | 'all')}
          className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        >
          {SOURCE_FILTERS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>

        <select
          aria-label="Change operation"
          value={operationFilter}
          onChange={e => setOperationFilter(e.target.value)}
          className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        >
          {['all', 'create', 'update', 'delete', 'rename', 'delete_all'].map(o => (
            <option key={o} value={o}>{o === 'all' ? 'All operations' : o}</option>
          ))}
        </select>

        <input
          type="text"
          value={noteSearch}
          onChange={e => setNoteSearch(e.target.value)}
          placeholder="Filter by note path…"
          className="min-w-[12rem] flex-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        />

        <select
          aria-label="History window"
          value={limit}
          onChange={e => setLimit(Number(e.target.value))}
          className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        >
          {[50, 100, 200, 500, 1000].map(n => <option key={n} value={n}>Last {n}</option>)}
        </select>

        {isAdmin && (
          <input
            type="text"
            value={userIdFilter}
            onChange={e => setUserIdFilter(e.target.value)}
            placeholder="user_id (admin)"
            className="w-44 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
          />
        )}
      </div>

      {!isLoading && !error && <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">Showing {filtered.length} of {entries.length} loaded changes. Filters apply to the latest {limit} changes.</p>}
      {/* List */}
      {isLoading ? (
        <div className="flex items-center justify-center h-40">
          <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-blue-600" />
        </div>
      ) : error ? null : filtered.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
          {entries.length > 0 ? 'No loaded changes match these filters.' : 'No vault changes were returned.'}
        </div>
      ) : viewMode === 'timeline' ? (
        <div className="space-y-2">
          {filtered.map(e => <LedgerRow key={e.id} entry={e} />)}
        </div>
      ) : (
        <div className="space-y-4">
          {groups.map(([notePath, items]) => (
            <NoteGroup key={notePath} notePath={notePath} items={items} />
          ))}
        </div>
      )}
    </div>
  )
}

function NoteGroup({ notePath, items }: { notePath: string; items: MemoryAuditEntry[] }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50"
      >
        {open ? <ChevronDown className="h-4 w-4 text-gray-400" /> : <ChevronRight className="h-4 w-4 text-gray-400" />}
        <FileText className="h-4 w-4 text-gray-400" />
        <span className="min-w-0 flex-1 truncate font-mono text-sm font-medium text-gray-800 dark:text-gray-200">{notePath}</span>
        <MetadataChip className="flex-shrink-0">
          {items.length} {items.length === 1 ? 'change' : 'changes'}
        </MetadataChip>
      </button>
      {open && (
        <div className="space-y-2 p-2 pt-0">
          {items.map(e => <LedgerRow key={e.id} entry={e} />)}
        </div>
      )}
    </div>
  )
}
