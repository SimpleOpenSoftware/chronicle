import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Play, Scissors, X } from 'lucide-react'
import { AuditConversation } from '../../services/api'
import { formatDate, formatDuration, processingStatusChip } from './format'
import PreviewStrip from './PreviewStrip'
import SegmentTriage from './SegmentTriage'
import BackgroundReview from './BackgroundReview'
import { MetadataChip, StateBadge } from '../ui'

type SortKey = 'title' | 'created_at' | 'duration_seconds' | 'speech_fraction' | 'archive_reason'
type SortDir = 'asc' | 'desc'

// Default direction on first click: newest/longest/most-speech first, text A→Z.
const DEFAULT_DIR: Record<SortKey, SortDir> = {
  title: 'asc',
  created_at: 'desc',
  duration_seconds: 'desc',
  speech_fraction: 'desc',
  archive_reason: 'asc',
}

const SORT_STORAGE_KEY = 'data_audit_sort'

function loadSort(): { key: SortKey; dir: SortDir } {
  try {
    const raw = sessionStorage.getItem(SORT_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed.key in DEFAULT_DIR && (parsed.dir === 'asc' || parsed.dir === 'desc')) {
        return parsed
      }
    }
  } catch {
    // ignore malformed storage
  }
  return { key: 'created_at', dir: 'desc' }
}

function compareRows(a: AuditConversation, b: AuditConversation, key: SortKey): number {
  // Nulls always sort last regardless of direction (handled by caller).
  switch (key) {
    case 'title':
      return (a.title || a.conversation_id).localeCompare(b.title || b.conversation_id)
    case 'created_at':
      return (a.created_at || '').localeCompare(b.created_at || '')
    case 'duration_seconds':
      return a.duration_seconds - b.duration_seconds
    case 'speech_fraction':
      return (a.speech_fraction ?? 0) - (b.speech_fraction ?? 0)
    case 'archive_reason':
      return (a.archive_reason || '').localeCompare(b.archive_reason || '')
  }
}

interface Props {
  rows: AuditConversation[]
  loading: boolean
  archivedView: boolean
  selected: Set<string>
  onToggleSelect: (id: string) => void
  onSelectMany: (ids: string[], value: boolean) => void
  onToggleSelectAll: () => void
  onSplit: (row: AuditConversation) => void
  // Bump when a triage decision is made/undone so the toolbar's count refreshes.
  onTriageChanged?: () => void
  // Stored confidence below this counts as a weak match (folded into triage review).
  marginalThreshold?: number
}

export default function AuditTable({
  rows,
  loading,
  archivedView,
  selected,
  onToggleSelect,
  onSelectMany,
  onToggleSelectAll,
  onSplit,
  onTriageChanged,
  marginalThreshold,
}: Props) {
  const allSelected = rows.length > 0 && selected.size === rows.length
  // Sort survives navigating to a conversation detail page and back.
  const [sortKey, setSortKey] = useState<SortKey>(() => loadSort().key)
  const [sortDir, setSortDir] = useState<SortDir>(() => loadSort().dir)

  useEffect(() => {
    try {
      sessionStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ key: sortKey, dir: sortDir }))
    } catch {
      // ignore storage quota/availability errors
    }
  }, [sortKey, sortDir])

  // Inline preview strip — one row's preview open at a time.
  const [previewId, setPreviewId] = useState<string | null>(null)

  const togglePreview = (conversationId: string) => {
    setPreviewId((current) => (current === conversationId ? null : conversationId))
  }

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir(DEFAULT_DIR[key])
    }
  }

  const sortedRows = useMemo(() => {
    const isNull = (r: AuditConversation) =>
      sortKey === 'speech_fraction' && r.speech_fraction === null
    return [...rows].sort((a, b) => {
      // Unanalyzed rows (no speech %) stay at the bottom in either direction.
      if (isNull(a) !== isNull(b)) return isNull(a) ? 1 : -1
      const cmp = compareRows(a, b, sortKey)
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [rows, sortKey, sortDir])

  // Shift-click range selection anchor: the last row whose checkbox was
  // toggled individually. Ranges follow the current sorted display order.
  const anchorRef = useRef<string | null>(null)

  const handleRowSelect = (e: React.MouseEvent, id: string) => {
    const willSelect = !selected.has(id)
    if (e.shiftKey && anchorRef.current && anchorRef.current !== id) {
      const ids = sortedRows.map((r) => r.conversation_id)
      const a = ids.indexOf(anchorRef.current)
      const b = ids.indexOf(id)
      if (a !== -1 && b !== -1) {
        // Set the whole anchor→row range to the clicked row's new state.
        // The anchor stays put so the range can be re-extended.
        const [lo, hi] = a < b ? [a, b] : [b, a]
        onSelectMany(ids.slice(lo, hi + 1), willSelect)
        return
      }
    }
    onToggleSelect(id)
    anchorRef.current = id
  }

  // Cmd/Ctrl-click toggles, Shift-click range-selects — anywhere on the row.
  // Plain clicks (links, play, Split…) are untouched, as are modifier clicks
  // on interactive elements (e.g. cmd-click a link still opens a new tab).
  const handleRowClick = (e: React.MouseEvent, id: string) => {
    if (!e.shiftKey && !e.metaKey && !e.ctrlKey) return
    if ((e.target as HTMLElement).closest('a, button, input')) return
    e.preventDefault()
    handleRowSelect(e, id)
  }

  const SortHeader = ({ label, sort }: { label: string; sort: SortKey }) => (
    <th className="px-3 py-2">
      <button
        onClick={() => handleSort(sort)}
        className="flex items-center space-x-1 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
        title={`Sort by ${label.toLowerCase()}`}
      >
        <span>{label}</span>
        {sortKey === sort ? (
          sortDir === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />
        ) : (
          <ArrowUpDown className="h-3 w-3 opacity-40" />
        )}
      </button>
    </th>
  )

  return (
    <div className="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
      <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
        <thead className="bg-gray-50 dark:bg-gray-900/40">
          <tr className="text-left text-gray-500 dark:text-gray-400">
            {!archivedView && (
              <th className="px-3 py-2">
                <input type="checkbox" checked={allSelected} onChange={onToggleSelectAll} />
              </th>
            )}
            <SortHeader label="Conversation" sort="title" />
            <SortHeader label="Date" sort="created_at" />
            <SortHeader label="Duration" sort="duration_seconds" />
            {!archivedView ? (
              <>
                <SortHeader label="Speech %" sort="speech_fraction" />
                <th className="px-3 py-2">Speakers</th>
                <th className="px-3 py-2">Actions</th>
              </>
            ) : (
              <SortHeader label="Reason" sort="archive_reason" />
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.length === 0 && (
            <tr>
              <td colSpan={archivedView ? 4 : 7} className="px-3 py-8 text-center text-gray-400">
                {loading ? 'Loading…' : 'No conversations match the current filters.'}
              </td>
            </tr>
          )}
          {sortedRows.map((r) => (
            <Fragment key={r.conversation_id}>
            <tr
              className={`text-gray-700 dark:text-gray-200 ${
                !archivedView && selected.has(r.conversation_id)
                  ? 'bg-blue-50/60 dark:bg-blue-900/20'
                  : ''
              }`}
              onClick={!archivedView ? (e) => handleRowClick(e, r.conversation_id) : undefined}
              onMouseDown={
                // Shift-clicking a range shouldn't drag-select the row text.
                !archivedView ? (e) => e.shiftKey && e.preventDefault() : undefined
              }
            >
              {!archivedView && (
                <td className="px-3 py-2">
                  <input
                    type="checkbox"
                    checked={selected.has(r.conversation_id)}
                    onChange={() => {}}
                    onClick={(e) => handleRowSelect(e, r.conversation_id)}
                  />
                </td>
              )}
              <td className="px-3 py-2 max-w-xs">
                <div className="flex items-center space-x-2">
                  {!archivedView && r.duration_seconds > 0 && (
                    <button
                      onClick={() => togglePreview(r.conversation_id)}
                      className={`flex-shrink-0 p-1.5 rounded-full transition-colors ${
                        previewId === r.conversation_id
                          ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200'
                          : 'text-gray-400 hover:text-blue-600 hover:bg-gray-100 dark:hover:bg-gray-700'
                      }`}
                      title={previewId === r.conversation_id ? 'Close preview' : 'Preview speech'}
                    >
                      {previewId === r.conversation_id ? (
                        <X className="h-4 w-4" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                    </button>
                  )}
                  <div className="min-w-0">
                    <Link
                      to={`/data-audit/recordings/${r.conversation_id}`}
                      state={{ from: '/data-audit' }}
                      className="block truncate text-blue-600 dark:text-blue-400 hover:underline"
                    >
                      {r.title || r.conversation_id.slice(0, 8)}
                    </Link>
                    <div className="flex items-center space-x-1.5 text-xs text-gray-400">
                      <span>{r.client_id}</span>
                      {r.derived_operation && (
                        <MetadataChip>{r.derived_operation}</MetadataChip>
                      )}
                      {r.last_export && (
                        <MetadataChip
                          title={`Shipped in ${r.last_export.export_id} (${formatDate(r.last_export.created_at)}) — filter with Export history to skip these`}
                        >
                          exported
                        </MetadataChip>
                      )}
                      {(() => {
                        const chip = processingStatusChip(r.processing_status, r.failure_stage)
                        return chip ? (
                          <span className={`px-1.5 py-0.5 rounded ${chip.className}`}>
                            {chip.label}
                          </span>
                        ) : null
                      })()}
                    </div>
                  </div>
                </div>
              </td>
              <td className="px-3 py-2 whitespace-nowrap">{formatDate(r.created_at)}</td>
              <td className="px-3 py-2 whitespace-nowrap">{formatDuration(r.duration_seconds)}</td>
              {!archivedView ? (
                <>
                  <td className="px-3 py-2">
                    {r.speech_fraction !== null ? (
                      `${Math.round(r.speech_fraction * 100)}%`
                    ) : (
                      <span className="text-gray-400" title="Not analyzed yet">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {r.speakers.length === 0 && <span className="text-gray-400 text-xs">none</span>}
                      {r.speakers.map((s) => (
                        <MetadataChip key={s}>{s}</MetadataChip>
                      ))}
                      {r.unknown_speech_segments > 0 && (
                        <StateBadge
                          tone="warning"
                          title="Speech segments not matched to an enrolled speaker — open the preview to triage them"
                        >
                          {r.unknown_speech_segments} to review
                        </StateBadge>
                      )}
                      {r.marginal_identified_segments > 0 && (
                        <span
                          className="px-1.5 py-0.5 rounded text-xs bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300"
                          title="Identified as an enrolled speaker but at low confidence (near the match threshold) — likely wrong (e.g. noise labeled as the nearest speaker). Open the preview to review."
                        >
                          {r.marginal_identified_segments} low-confidence
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {r.duration_seconds > 0 && (
                      <button
                        onClick={() => onSplit(r)}
                        className="flex items-center space-x-1 px-2 py-1 rounded text-xs border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                        title="Split this conversation at long silence gaps"
                      >
                        <Scissors className="h-3.5 w-3.5" />
                        <span>Split…</span>
                      </button>
                    )}
                  </td>
                </>
              ) : (
                <td className="px-3 py-2">
                  <MetadataChip>{r.archive_reason || 'archived'}</MetadataChip>
                </td>
              )}
            </tr>
            {previewId === r.conversation_id && (
              <tr>
                <td colSpan={archivedView ? 4 : 7} className="px-3 py-2 bg-gray-50/50 dark:bg-gray-900/20">
                  <PreviewStrip
                    conversationId={r.conversation_id}
                    durationSeconds={r.duration_seconds}
                    onClose={() => setPreviewId(null)}
                    autoPlay
                    speakers={r.speakers}
                  />
                  <SegmentTriage
                    conversationId={r.conversation_id}
                    onDecisionsChanged={onTriageChanged}
                    marginalThreshold={marginalThreshold}
                  />
                  <div className="mt-3">
                    <BackgroundReview
                      conversationId={r.conversation_id}
                      onDecisionsChanged={onTriageChanged}
                    />
                  </div>
                </td>
              </tr>
            )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}
