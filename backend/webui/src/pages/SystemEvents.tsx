import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, AlertOctagon, Info, RefreshCw, ChevronRight, ChevronDown,
  Check, ExternalLink, ShieldAlert, Copy, type LucideIcon,
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useSystemEvents, useSystemEventsSummary } from '../hooks/useSystemEvents'
import {
  systemEventsApi,
  type SystemEvent,
  type SystemEventsFilter,
  type SystemEventsSummary,
} from '../services/api'
import { Button, Alert, Checkbox } from '../components/ui'

// ---- Severity + category styling ------------------------------------------

type Severity = SystemEvent['severity']

const SEVERITY_STYLE: Record<Severity, { chip: string; Icon: LucideIcon; text: string }> = {
  critical: { chip: 'text-red-700 bg-red-100 dark:bg-red-900/40 dark:text-red-300', Icon: AlertOctagon, text: 'text-red-600 dark:text-red-400' },
  error: { chip: 'text-red-700 bg-red-50 dark:bg-red-900/30 dark:text-red-300', Icon: AlertTriangle, text: 'text-red-600 dark:text-red-400' },
  warning: { chip: 'text-amber-700 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-300', Icon: AlertTriangle, text: 'text-amber-600 dark:text-amber-400' },
  info: { chip: 'text-blue-700 bg-blue-50 dark:bg-blue-900/30 dark:text-blue-300', Icon: Info, text: 'text-blue-600 dark:text-blue-400' },
}

const CATEGORY_CHIP: Record<string, string> = {
  service: 'text-purple-700 bg-purple-50 dark:bg-purple-900/30 dark:text-purple-300',
  client: 'text-cyan-700 bg-cyan-50 dark:bg-cyan-900/30 dark:text-cyan-300',
  pipeline: 'text-indigo-700 bg-indigo-50 dark:bg-indigo-900/30 dark:text-indigo-300',
  job: 'text-orange-700 bg-orange-50 dark:bg-orange-900/30 dark:text-orange-300',
  memory: 'text-amber-800 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-300',
  plugin: 'text-pink-700 bg-pink-50 dark:bg-pink-900/30 dark:text-pink-300',
  config: 'text-yellow-700 bg-yellow-50 dark:bg-yellow-900/30 dark:text-yellow-300',
  api: 'text-teal-700 bg-teal-50 dark:bg-teal-900/30 dark:text-teal-300',
  log: 'text-gray-600 bg-gray-100 dark:bg-gray-700 dark:text-gray-300',
}

const SEVERITIES: Severity[] = ['critical', 'error', 'warning', 'info']
const CATEGORIES = ['service', 'client', 'pipeline', 'job', 'memory', 'plugin', 'config', 'api', 'log']
const SYSTEM_EVENTS_TIME_ZONE = 'Asia/Kolkata'
const systemEventsTimeFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: SYSTEM_EVENTS_TIME_ZONE,
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return `${systemEventsTimeFormatter.format(date)} IST`
}

// ---- One event row ---------------------------------------------------------

function EventRow({
  event, onAck, selected, onToggleSelect,
}: {
  event: SystemEvent
  onAck: (id: string) => void
  selected: boolean
  onToggleSelect: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const sev = SEVERITY_STYLE[event.severity] ?? SEVERITY_STYLE.info
  const SevIcon = sev.Icon
  const expandable = !!(event.detail || event.traceback || Object.keys(event.metadata || {}).length)
  const detailsId = `system-event-details-${event.id}`

  return (
    <div className={`rounded-lg border overflow-hidden ${event.acked ? 'border-gray-200 opacity-60 dark:border-gray-700' : selected ? 'border-green-400 dark:border-green-600' : 'border-gray-200 dark:border-gray-700'}`}>
      <div
        className={`flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left ${
          expandable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/50' : ''
        }`}
      >
        {!event.acked && (
          <input
            type="checkbox"
            checked={selected}
            onClick={e => e.stopPropagation()}
            onChange={() => onToggleSelect(event.id)}
            className="flex-shrink-0 rounded"
            title="Select for bulk acknowledge"
          />
        )}

        {expandable ? (
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            aria-expanded={open}
            aria-controls={detailsId}
            className="flex min-w-0 flex-1 items-center gap-3 text-left"
            title={open ? 'Collapse event details' : 'Expand event details'}
          >
            <span className="w-4 flex-shrink-0 text-gray-400">
              {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </span>
            <span className={`inline-flex flex-shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${sev.chip}`}>
              <SevIcon className="h-3.5 w-3.5" />
              {event.severity}
            </span>
            <span className={`flex-shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${CATEGORY_CHIP[event.category] || CATEGORY_CHIP.log}`}>
              {event.category}
            </span>
            <span className="min-w-0 flex-1 truncate text-sm text-gray-800 dark:text-gray-200" title={event.title}>
              {event.title}
            </span>
          </button>
        ) : (
          <>
            <span className="w-4 flex-shrink-0" />
            <span className={`inline-flex flex-shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${sev.chip}`}>
              <SevIcon className="h-3.5 w-3.5" />
              {event.severity}
            </span>
            <span className={`flex-shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${CATEGORY_CHIP[event.category] || CATEGORY_CHIP.log}`}>
              {event.category}
            </span>
            <span className="min-w-0 flex-1 truncate text-sm text-gray-800 dark:text-gray-200" title={event.title}>
              {event.title}
            </span>
          </>
        )}

        {event.count > 1 && (
          <span className="flex-shrink-0 rounded-full bg-gray-200 px-1.5 py-0.5 text-xs font-semibold text-gray-700 dark:bg-gray-600 dark:text-gray-200" title="Times this event recurred">
            ×{event.count}
          </span>
        )}

        <span className="flex-shrink-0 hidden md:inline max-w-[12rem] truncate font-mono text-xs text-gray-400 dark:text-gray-500" title={event.source}>
          {event.source}
        </span>

        {event.client_id && (
          <span className="flex-shrink-0 hidden lg:inline font-mono text-xs text-cyan-600 dark:text-cyan-400" title="client_id">
            {event.client_id}
          </span>
        )}

        {event.conversation_id && (
          <Link
            to={`/recordings/${event.conversation_id}`}
            onClick={e => e.stopPropagation()}
            className="flex-shrink-0 hidden md:inline-flex items-center gap-0.5 text-xs text-blue-600 hover:underline dark:text-blue-400"
            title="Open related conversation"
          >
            conversation <ExternalLink className="h-3 w-3" />
          </Link>
        )}

        <span className="flex-shrink-0 w-40 text-right text-xs text-gray-500 dark:text-gray-400">
          {formatTime(event.last_seen_at || event.created_at)}
        </span>

        {!event.acked && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onAck(event.id) }}
            className="flex-shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-gray-500 hover:bg-green-50 hover:text-green-700 dark:text-gray-400 dark:hover:bg-green-900/30 dark:hover:text-green-300"
            title="Acknowledge"
          >
            <Check className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {open && expandable && (
        <div id={detailsId} className="border-t border-gray-100 px-4 py-3 dark:border-gray-700/60">
          {event.detail && (
            <pre className="mb-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
              {event.detail}
            </pre>
          )}
          {event.traceback && (
            <pre className="mb-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-900/20 dark:text-red-300">
              {event.traceback}
            </pre>
          )}
          {Object.keys(event.metadata || {}).length > 0 && (
            <div className="flex flex-wrap gap-2 text-xs text-gray-500 dark:text-gray-400">
              {Object.entries(event.metadata).map(([k, v]) => (
                <span key={k} className="rounded bg-gray-100 px-1.5 py-0.5 font-mono dark:bg-gray-700">
                  {k}={String(v)}
                </span>
              ))}
            </div>
          )}
          <div className="mt-2 text-xs text-gray-400 dark:text-gray-500">
            First seen {formatTime(event.created_at)}
            {event.user_id ? ` · user ${event.user_id}` : ''}
          </div>
        </div>
      )}
    </div>
  )
}

// ---- Summary strip ---------------------------------------------------------

function SummaryStrip({ summary }: { summary?: SystemEventsSummary }) {
  const bySev = summary?.by_severity ?? {}
  const cards: { label: string; value: number | string; cls: string }[] = [
    { label: 'Total (window)', value: summary?.total ?? '—', cls: 'text-gray-900 dark:text-gray-100' },
    { label: 'Critical', value: summary ? bySev.critical ?? 0 : '—', cls: 'text-red-600 dark:text-red-400' },
    { label: 'Error', value: summary ? bySev.error ?? 0 : '—', cls: 'text-red-500 dark:text-red-400' },
    { label: 'Warning', value: summary ? bySev.warning ?? 0 : '—', cls: 'text-amber-600 dark:text-amber-400' },
    { label: 'Info', value: summary ? bySev.info ?? 0 : '—', cls: 'text-blue-600 dark:text-blue-400' },
    { label: 'Unacknowledged', value: summary?.unacked ?? '—', cls: 'text-orange-600 dark:text-orange-400' },
  ]
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {cards.map(c => (
        <div key={c.label} className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
          <div className={`text-2xl font-bold ${c.cls}`}>{c.value}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400">{c.label}</div>
        </div>
      ))}
    </div>
  )
}

function metricLabel(value: string): string {
  return value.replace(/_/g, ' ')
}

function MemoryFallbackBand({
  summary,
  onInspect,
}: {
  summary?: SystemEventsSummary
  onInspect: () => void
}) {
  const stats = summary?.memory_fallbacks
  if (!stats || stats.occurrences === 0) return null

  return (
    <section className="rounded-lg border border-amber-200 bg-amber-50/70 px-4 py-3 dark:border-amber-800/70 dark:bg-amber-950/20" aria-label="Memory fallback events">
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">Memory writes used fallback</div>
          <div className="mt-0.5 text-xs text-gray-600 dark:text-gray-400">
            {stats.occurrences} writes across {stats.affected_conversations} conversations
          </div>
        </div>
        <button type="button" onClick={onInspect} className="rounded-md px-2.5 py-1.5 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-300 dark:text-amber-300 dark:ring-amber-700">
          Inspect memory events
        </button>
      </div>
      <details className="mt-2 text-xs text-gray-600 dark:text-gray-400">
        <summary className="cursor-pointer">Fallback details</summary>
        <ul className="mt-2 space-y-1">
          {Object.entries(stats.by_reason).map(([reason, count]) => <li key={reason}>{metricLabel(reason)} ×{count}</li>)}
          {stats.agent_paths.map(path => <li key={`${path.primary_backend}:${path.recovery_backend}`}>{path.primary_backend} → {path.recovery_backend === 'none' ? 'no recovery' : path.recovery_backend} → deterministic ×{path.occurrences}</li>)}
        </ul>
        {stats.latest_at && <div className="mt-1">Latest {formatTime(stats.latest_at)}</div>}
      </details>
    </section>
  )
}

// ---- Page ------------------------------------------------------------------

const WINDOWS = [
  { value: 1, label: 'Last 1h' },
  { value: 6, label: 'Last 6h' },
  { value: 24, label: 'Last 24h' },
  { value: 24 * 7, label: 'Last 7d' },
  { value: 24 * 30, label: 'Last 30d' },
]

type SimilarityField = 'severity' | 'category' | 'source' | 'client_id' | 'user_id'

const SIMILARITY_FIELDS: SimilarityField[] = [
  'severity',
  'category',
  'source',
  'client_id',
  'user_id',
]

function findSimilarEvents(events: SystemEvent[], selectedIds: Set<string>): SystemEvent[] {
  const selectedEvents = events.filter(event => selectedIds.has(event.id))
  if (selectedEvents.length === 0) return []

  const first = selectedEvents[0]
  const hasCommonTitle = selectedEvents.every(event => event.title === first.title)
  if (hasCommonTitle) {
    return events.filter(event => !event.acked && event.title === first.title)
  }

  const commonFields = SIMILARITY_FIELDS.filter(field => {
    const value = first[field]
    return value != null && value !== '' && selectedEvents.every(event => event[field] === value)
  })
  if (commonFields.length === 0) return []

  return events.filter(event =>
    !event.acked && commonFields.every(field => event[field] === first[field])
  )
}

function describeSimilarSelection(events: SystemEvent[], selectedIds: Set<string>): string {
  const selectedEvents = events.filter(event => selectedIds.has(event.id))
  if (selectedEvents.length === 0) return ''

  const first = selectedEvents[0]
  if (selectedEvents.every(event => event.title === first.title)) {
    return `Matches exact title: ${first.title}`
  }

  const commonFields = SIMILARITY_FIELDS.filter(field => {
    const value = first[field]
    return value != null && value !== '' && selectedEvents.every(event => event[field] === value)
  })
  if (commonFields.length === 0) {
    return 'Unavailable: the selected events have different titles and no shared severity, category, source, client, or user.'
  }

  return `Matches shared ${commonFields.join(', ')}.`
}

export default function SystemEvents() {
  const { isAdmin } = useAuth()
  const queryClient = useQueryClient()

  const [severity, setSeverity] = useState('')
  const [category, setCategory] = useState('')
  const [source, setSource] = useState('')
  const [clientId, setClientId] = useState('')
  const [showAcked, setShowAcked] = useState(false)
  const [windowHours, setWindowHours] = useState(24)
  const [limit, setLimit] = useState(200)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [copied, setCopied] = useState(false)

  const filter: SystemEventsFilter = {
    ...(severity ? { severity } : {}),
    ...(category ? { category } : {}),
    ...(source ? { source } : {}),
    ...(clientId.trim() ? { client_id: clientId.trim() } : {}),
    ...(showAcked ? {} : { acked: false }),
    since_hours: windowHours,
    limit,
  }

  const { data, isLoading, error, refetch, isFetching } = useSystemEvents(filter, isAdmin)
  const { data: summary } = useSystemEventsSummary(windowHours, isAdmin)

  if (!isAdmin) {
    return (
      <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center dark:border-gray-700">
        <ShieldAlert className="mx-auto mb-3 h-8 w-8 text-gray-400" />
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Access Restricted</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400">System events are visible to administrators only.</p>
      </div>
    )
  }

  const events = data?.events ?? []
  const sources = Object.keys(summary?.by_source ?? {})
  const unackedVisibleIds = events.filter(e => !e.acked).map(e => e.id)
  const allVisibleSelected = unackedVisibleIds.length > 0 && unackedVisibleIds.every(id => selected.has(id))
  const similarEvents = findSimilarEvents(events, selected)
  const similarEventIds = similarEvents.map(event => event.id)
  const hasNewSimilarEvents = similarEventIds.some(id => !selected.has(id))
  const similarSelectionDescription = describeSimilarSelection(events, selected)
  const similarSelectionTitle = hasNewSimilarEvents
    ? similarSelectionDescription
    : `${similarSelectionDescription} ${similarEventIds.length > 0 ? 'All visible matches are already selected.' : ''}`.trim()

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['system-events'] })
    queryClient.invalidateQueries({ queryKey: ['system-events-summary'] })
  }

  const onAck = (id: string) => {
    systemEventsApi.ack(id).then(refresh)
  }

  const toggleSelect = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAllVisible = () => {
    setSelected(prev => {
      if (unackedVisibleIds.every(id => prev.has(id))) {
        const next = new Set(prev)
        unackedVisibleIds.forEach(id => next.delete(id))
        return next
      }
      return new Set([...prev, ...unackedVisibleIds])
    })
  }

  const selectSimilar = () => {
    setSelected(prev => new Set([...prev, ...similarEventIds]))
  }

  const onAckSelected = () => {
    const ids = [...selected]
    if (ids.length === 0) return
    systemEventsApi.ackSelected(ids).then(() => {
      setSelected(new Set())
      refresh()
    })
  }

  const formatEvent = (e: SystemEvent): string => {
    const ts = e.last_seen_at || e.created_at || ''
    const lines = [
      `[${e.severity.toUpperCase()}] ${e.title}${e.count > 1 ? ` (×${e.count})` : ''}`,
      `category: ${e.category} | source: ${e.source}${ts ? ` | ${formatTime(ts)}` : ''}`,
    ]
    if (e.client_id) lines.push(`client_id: ${e.client_id}`)
    if (e.conversation_id) lines.push(`conversation_id: ${e.conversation_id}`)
    if (e.detail) lines.push('', e.detail)
    if (e.traceback) lines.push('', e.traceback)
    return lines.join('\n')
  }

  const onCopySelected = () => {
    const chosen = events.filter(e => selected.has(e.id))
    if (chosen.length === 0) return
    const text = chosen.map(formatEvent).join('\n\n' + '─'.repeat(60) + '\n\n')
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const onAckAll = () => {
    if (!window.confirm('Acknowledge all unacknowledged events matching the current filters?')) return
    const { acked: _acked, limit: _limit, offset: _offset, ...ackParams } = filter
    systemEventsApi.ackAll(ackParams).then(refresh)
  }

  const onClearAcked = () => {
    if (!window.confirm('Delete all acknowledged events?')) return
    systemEventsApi.clear(true).then(refresh)
  }

  return (
    <div>
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center space-x-2">
          <AlertTriangle className="h-6 w-6 text-red-600 flex-shrink-0" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">System Events</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {selected.size > 0 && (
            <>
              <Button
                variant="primary"
                size="md"
                onClick={onCopySelected}
                title="Copy the selected events to the clipboard"
                icon={copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
              >
                {copied ? 'Copied!' : `Copy events (${selected.size})`}
              </Button>
              <button
                onClick={onAckSelected}
                className="flex items-center gap-2 rounded-md bg-green-600 px-3 py-2 text-sm font-medium text-white hover:bg-green-700"
                title="Acknowledge the selected events"
              >
                <Check className="h-4 w-4" />
                Acknowledge selected ({selected.size})
              </button>
            </>
          )}
          <button
            onClick={onAckAll}
            disabled={(summary?.unacked ?? 0) === 0}
            className="flex items-center gap-2 rounded-md bg-green-50 px-3 py-2 text-sm font-medium text-green-700 hover:bg-green-100 disabled:opacity-50 disabled:hover:bg-green-50 dark:bg-green-900/30 dark:text-green-300 dark:hover:bg-green-900/50"
            title="Mark all matching events as acknowledged; this does not resolve an active failure"
          >
            <Check className="h-4 w-4" />
            Acknowledge all
          </button>
          <Button variant="secondary" size="md" onClick={onClearAcked}>
            Clear acknowledged
          </Button>
          <Button
            variant="secondary"
            size="md"
            onClick={() => refetch()}
            disabled={isFetching}
            icon={<RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />}
          >
            Refresh
          </Button>
        </div>
      </div>

      <p className="mb-5 text-sm text-gray-500 dark:text-gray-400">
        Events from the last 30 days. Times in IST. Unacknowledged counts include historical events, not just current failures.
      </p>

      {error && (
        <Alert tone="danger" className="mb-4">
          {(error as Error).message || 'Failed to load system events.'}
        </Alert>
      )}

      <div className="mb-5 space-y-3">
        <SummaryStrip summary={summary} />
        <MemoryFallbackBand
          summary={summary}
          onInspect={() => {
            setCategory('memory')
            setShowAcked(true)
          }}
        />
      </div>

      {/* Controls */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select value={severity} onChange={e => setSeverity(e.target.value)} className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All severities</option>
          {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>

        <select value={category} onChange={e => setCategory(e.target.value)} className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All categories</option>
          {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>

        <select value={source} onChange={e => setSource(e.target.value)} className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All sources</option>
          {sources.map(s => <option key={s} value={s}>{s}</option>)}
        </select>

        <input
          type="text"
          value={clientId}
          onChange={e => setClientId(e.target.value)}
          placeholder="client_id…"
          className="w-40 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        />

        <select value={windowHours} onChange={e => setWindowHours(Number(e.target.value))} className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          {WINDOWS.map(w => <option key={w.value} value={w.value}>{w.label}</option>)}
        </select>

        <select value={limit} onChange={e => setLimit(Number(e.target.value))} className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          {[50, 100, 200, 500].map(n => <option key={n} value={n}>Last {n}</option>)}
        </select>

        <Checkbox label="Show acknowledged" checked={showAcked} onChange={e => setShowAcked(e.target.checked)} />

        <Checkbox
          label="Select all visible"
          title="Select all unacknowledged events shown below"
          checked={allVisibleSelected}
          disabled={unackedVisibleIds.length === 0}
          onChange={toggleSelectAllVisible}
        />

        {selected.size > 0 && (
          <span title={similarSelectionTitle}>
            <button
              type="button"
              onClick={selectSimilar}
              disabled={!hasNewSimilarEvents}
              className="rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              aria-label={`Select similar. ${similarSelectionTitle}`}
            >
              Select similar (experimental)
            </button>
          </span>
        )}
      </div>

      {/* List */}
      {isLoading ? (
        <div className="flex items-center justify-center h-40">
          <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-blue-600" />
        </div>
      ) : events.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
          No events match these filters.
        </div>
      ) : (
        <div className="space-y-2">
          {events.map(ev => (
            <EventRow
              key={ev.id}
              event={ev}
              onAck={onAck}
              selected={selected.has(ev.id)}
              onToggleSelect={toggleSelect}
            />
          ))}
          {data && data.total > events.length && (
            <div className="pt-2 text-center text-xs text-gray-400 dark:text-gray-500">
              Showing {events.length} of {data.total}. Narrow filters or raise the limit to see more.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
