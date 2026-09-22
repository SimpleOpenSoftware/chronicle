import { useCallback, useEffect, useState } from 'react'
import { Sparkles, Archive as ArchiveIcon, AlertTriangle } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { dataAuditApi, AuditConversation } from '../services/api'
import { useJobPolling } from '../hooks/useJobPolling'
import AuditFilterBar from '../components/dataAudit/AuditFilterBar'
import {
  AUDIT_FILTERS,
  defaultFilterValues,
} from '../components/dataAudit/filters'
import AuditToolbar from '../components/dataAudit/AuditToolbar'
import AuditTable from '../components/dataAudit/AuditTable'
import SpeakerConfidencePanel from '../components/dataAudit/SpeakerConfidencePanel'
import DriftPanel from '../components/dataAudit/DriftPanel'
import BackgroundReviewPanel from '../components/dataAudit/BackgroundReviewPanel'
import SplitConversationModal from '../components/dataAudit/SplitConversationModal'
import MergePreviewModal from '../components/dataAudit/MergePreviewModal'
import ExportModal from '../components/dataAudit/ExportModal'
import GuidedEnrollment from '../components/dataAudit/GuidedEnrollment'
import UnknownSpeakerDiscovery from '../components/dataAudit/UnknownSpeakerDiscovery'
import SpeakerLabelReview from '../components/dataAudit/SpeakerLabelReview'
import EnrollmentCandidates from '../components/finetuning/EnrollmentCandidates'
import { Alert, Button, Tabs } from '../components/ui'

// Data Audit is the single home for curation. A task hub picks the active flow:
// audit conversations, enroll speakers (queue + guided enhance), or classify
// background/role. Each flow reuses its existing panel(s). The Wake-Word Lab is
// the one hub tile that leaves the page — it's a full sub-view at /wakeword-lab
// rather than an inline flow, so it links out instead of switching curationView.
type CurationView = 'conversations' | 'speaker-review' | 'enroll' | 'background'

// Persist filter inputs across navigation (e.g. opening a conversation detail
// page and clicking back) so the user doesn't lose their filters.
// v2: generic per-filter values keyed by AUDIT_FILTERS registry keys.
const FILTERS_STORAGE_KEY = 'data_audit_filters_v3'
const ARCHIVED_VIEW_STORAGE_KEY = 'data_audit_archived_view'
const SELECTION_STORAGE_KEY = 'data_audit_selection'
// Persist the in-flight analyze job id so navigating away (e.g. into a
// conversation and back) re-attaches to the running job's progress instead of
// losing it. Backend exposes live progress via GET /queue/jobs/{id}/status for
// the life of the RQ job record.
const ANALYZE_JOB_STORAGE_KEY = 'data_audit_analyze_job'

function loadSelection(): Set<string> {
  try {
    const raw = sessionStorage.getItem(SELECTION_STORAGE_KEY)
    if (raw) return new Set(JSON.parse(raw))
  } catch {
    // ignore malformed storage
  }
  return new Set()
}

function loadFilters(datasetId: string | null): Record<string, unknown> {
  const defaults = defaultFilterValues()
  if (datasetId) return { ...defaults, dataset: datasetId }
  try {
    const raw = sessionStorage.getItem(FILTERS_STORAGE_KEY)
    if (raw) return { ...defaults, ...JSON.parse(raw) }
  } catch {
    // ignore malformed storage
  }
  return defaults
}

function loadArchivedView(datasetId: string | null): boolean {
  if (datasetId) return false
  try {
    return sessionStorage.getItem(ARCHIVED_VIEW_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

export default function DataAudit() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialDatasetId = searchParams.get('dataset')
  // Filter values keyed by AUDIT_FILTERS registry keys — initialized from
  // sessionStorage (lazy) so they survive navigating to a conversation detail
  // page and back.
  const [filters, setFilters] = useState<Record<string, unknown>>(() =>
    loadFilters(initialDatasetId)
  )
  const [archivedOnly, setArchivedOnly] = useState(() =>
    loadArchivedView(initialDatasetId)
  )
  // Which curation flow is active (task hub). Conversation audit is the default.
  const requestedView = searchParams.get('view')
  const curationView: CurationView = requestedView === 'enroll' || requestedView === 'speaker-review' || requestedView === 'background' ? requestedView : 'conversations'
  const setCurationView = (view: string) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      if (view === 'conversations') next.delete('view')
      else next.set('view', view)
      return next
    })
  }

  // Data
  const [speakers, setSpeakers] = useState<string[]>([])
  const [datasets, setDatasets] = useState<string[]>([])
  const [rows, setRows] = useState<AuditConversation[]>([])
  const [total, setTotal] = useState(0)
  const [scanCapped, setScanCapped] = useState(false)
  // similarity_threshold + margin: a stored confidence below this is a weak
  // ("low-confidence") match the triage panel folds into its review bucket.
  const [marginalThreshold, setMarginalThreshold] = useState<number | undefined>(undefined)
  // null until the first listing response tells us how many conversations
  // still lack cached VAD analysis (0 disables the Analyze button).
  const [unanalyzedCount, setUnanalyzedCount] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<string>>(() => loadSelection())

  // Status
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Speaker triage: pending decisions across all conversations + apply state.
  const [triagePending, setTriagePending] = useState({ pending_count: 0, conversation_count: 0 })
  const [applyingTriage, setApplyingTriage] = useState(false)

  // Modals
  const [splitTarget, setSplitTarget] = useState<AuditConversation | null>(null)
  const [mergeTargets, setMergeTargets] = useState<AuditConversation[] | null>(null)
  const [exportOpen, setExportOpen] = useState(false)
  const { pollJob } = useJobPolling()

  const loadConversations = useCallback(async (overrideFilters?: Record<string, unknown>) => {
    setLoading(true)
    setError(null)
    try {
      // Each registry filter contributes its own query params.
      const values = overrideFilters ?? filters
      const params: Record<string, unknown> = {
        archived_only: archivedOnly,
        limit: 200,
        offset: 0,
      }
      for (const def of AUDIT_FILTERS) {
        Object.assign(params, def.toParams(values[def.key] ?? def.defaultValue))
      }
      const res = await dataAuditApi.getConversations(params)
      setRows(res.data.conversations)
      setTotal(res.data.total)
      setScanCapped(res.data.scan_capped)
      if (res.data.similarity_threshold != null) {
        setMarginalThreshold(res.data.similarity_threshold + (res.data.marginal_margin ?? 0))
      }
      setUnanalyzedCount(res.data.unanalyzed_count)
      // Speaker filter options come from the same scan, so the list only
      // contains speakers actually present in the current view.
      setSpeakers(res.data.speakers || [])
      setDatasets(res.data.datasets || [])
      // Prune (not clear) the selection: keep selected rows that are still
      // visible so navigation/refresh doesn't lose a curation in progress.
      const visible = new Set(res.data.conversations.map((c) => c.conversation_id))
      setSelected((prev) => new Set([...prev].filter((id) => visible.has(id))))
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }, [filters, archivedOnly])

  useEffect(() => {
    const dataset = typeof filters.dataset === 'string' ? filters.dataset.trim() : ''
    if ((searchParams.get('dataset') || '') === dataset) return
    const next = new URLSearchParams(searchParams)
    if (dataset) next.set('dataset', dataset)
    else next.delete('dataset')
    setSearchParams(next, { replace: true })
  }, [filters.dataset, searchParams, setSearchParams])

  // Persist selection whenever it changes.
  useEffect(() => {
    try {
      sessionStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify([...selected]))
    } catch {
      // ignore storage quota/availability errors
    }
  }, [selected])

  // Persist filter inputs whenever they change.
  useEffect(() => {
    try {
      sessionStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters))
      sessionStorage.setItem(ARCHIVED_VIEW_STORAGE_KEY, String(archivedOnly))
    } catch {
      // ignore storage quota/availability errors
    }
  }, [filters, archivedOnly])

  useEffect(() => {
    loadConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archivedOnly])

  // Poll a (possibly already-running) analyze job to completion, driving the
  // progress message. Shared by a fresh Analyze click and by re-attachment on
  // mount. Clears the stored job id once terminal.
  const attachToAnalyzeJob = useCallback(
    async (jobId: string) => {
      setAnalyzing(true)
      setError(null)
      try {
        const status = await pollJob(jobId, (s, progress) =>
          setMessage(
            progress?.message
              ? `Analyzing audio… ${progress.message}`
              : progress?.total != null
                ? `Analyzing audio… ${progress.done ?? 0}/${progress.total}`
                : `Analyzing audio… (${s})`
          )
        )
        setAnalyzing(false)
        try {
          sessionStorage.removeItem(ANALYZE_JOB_STORAGE_KEY)
        } catch {
          // ignore storage availability errors
        }
        setMessage(status === 'finished' ? 'Analysis complete' : 'Analysis failed')
        if (status === 'finished') loadConversations()
      } catch (e: any) {
        setAnalyzing(false)
        try {
          sessionStorage.removeItem(ANALYZE_JOB_STORAGE_KEY)
        } catch {
          // ignore storage availability errors
        }
        setError(e?.response?.data?.error || 'Analysis failed')
      }
    },
    [pollJob, loadConversations]
  )

  const runAnalysis = async () => {
    setAnalyzing(true)
    setError(null)
    setMessage('Queued audio analysis…')
    try {
      const res = await dataAuditApi.analyze(undefined, false)
      const jobId = res.data.job_id
      if (!jobId) {
        setAnalyzing(false)
        setMessage('Analysis started')
        return
      }
      try {
        sessionStorage.setItem(ANALYZE_JOB_STORAGE_KEY, jobId)
      } catch {
        // ignore storage availability errors
      }
      await attachToAnalyzeJob(jobId)
    } catch (e: any) {
      setAnalyzing(false)
      setError(e?.response?.data?.error || 'Failed to start analysis')
    }
  }

  // On mount, re-attach to an analyze job started before navigating away so its
  // progress keeps showing instead of silently running in the background.
  useEffect(() => {
    let storedJob: string | null = null
    try {
      storedJob = sessionStorage.getItem(ANALYZE_JOB_STORAGE_KEY)
    } catch {
      // ignore storage availability errors
    }
    if (storedJob) {
      setMessage('Reattaching to running analysis…')
      attachToAnalyzeJob(storedJob)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Bulk set used by shift-click range selection in the table.
  const selectMany = (ids: string[], value: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (value ? next.add(id) : next.delete(id)))
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selected.size === rows.length) setSelected(new Set())
    else setSelected(new Set(rows.map((r) => r.conversation_id)))
  }

  const refreshTriagePending = useCallback(async () => {
    try {
      const res = await dataAuditApi.getTriagePending()
      setTriagePending(res.data)
    } catch {
      // best-effort count; ignore
    }
  }, [])

  // Load the pending-decision count on mount.
  useEffect(() => {
    refreshTriagePending()
  }, [refreshTriagePending])

  const applyTriage = async () => {
    setApplyingTriage(true)
    setError(null)
    try {
      const res = await dataAuditApi.applyTriage()
      const { applied_count, conversation_count } = res.data
      setMessage(
        `Updated ${applied_count}/${conversation_count} recordings. Memory processing queued; enrollment is separate.`
      )
      await refreshTriagePending()
      await loadConversations()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to apply triage')
    } finally {
      setApplyingTriage(false)
    }
  }

  const selectedRows = rows.filter((r) => selected.has(r.conversation_id))
  // Merge needs 2+ conversations from the same device; the server is
  // authoritative on adjacency (filtered-out conversations may sit between).
  const mergeEligible =
    selectedRows.length >= 2 && new Set(selectedRows.map((r) => r.client_id)).size === 1

  const onOperationDone = (msg: string) => {
    setMessage(msg)
    loadConversations()
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Sparkles className="h-6 w-6 text-blue-600" />
        <div className="flex-1 min-w-[240px]">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Data Audit</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Review audio and speaker labels.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs
          value={curationView}
          onChange={setCurationView}
          tabs={[
            { value: 'conversations', label: 'Recordings' },
            { value: 'speaker-review', label: 'Speaker labels' },
            { value: 'enroll', label: 'Enrollment' },
            { value: 'background', label: 'Background & role' },
          ]}
        />
        <Link to="/wakeword-lab" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">Wake-Word Lab</Link>
      </div>

      {/* Messages (shared across flows) */}
      {message && <Alert tone="info">{message}</Alert>}
      {error && (
        <Alert tone="danger" icon={<AlertTriangle className="h-4 w-4" />}>
          {error}
        </Alert>
      )}

      {/* ── Enroll speakers flow: queue (deliberate, gated) + guided enhance ── */}
      {curationView === 'enroll' && (
        <div className="space-y-6">
          <SpeakerConfidencePanel />
          <UnknownSpeakerDiscovery />
          <EnrollmentCandidates />
          <GuidedEnrollment />
          <DriftPanel />
        </div>
      )}

      {curationView === 'speaker-review' && (
        <div className="space-y-4">
          <SpeakerLabelReview onPendingChanged={refreshTriagePending} />
          {triagePending.pending_count > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200 bg-white px-5 py-4 dark:border-gray-700 dark:bg-gray-800">
              <p className="text-sm text-gray-600 dark:text-gray-300">
                {triagePending.pending_count} correction(s) pending across {triagePending.conversation_count} conversation(s).
              </p>
              <Button variant="primary" onClick={applyTriage} disabled={applyingTriage}>
                {applyingTriage ? 'Applying corrections…' : 'Apply pending corrections'}
              </Button>
            </div>
          )}
        </div>
      )}

      {/* Background & role */}
      {curationView === 'background' && <BackgroundReviewPanel />}

      {/* ── Conversation audit flow ────────────────────────────────────────── */}
      {curationView === 'conversations' && (
        <>
          {/* View toggle */}
          <Tabs
            variant="underline"
            value={archivedOnly ? 'archived' : 'conversations'}
            onChange={(v) => setArchivedOnly(v === 'archived')}
            tabs={[
              { value: 'conversations', label: 'Current' },
              { value: 'archived', label: 'Historical audio stubs', icon: <ArchiveIcon className="h-4 w-4" /> },
            ]}
          />

          {/* Filters (hidden in archived view) */}
          {!archivedOnly && (
            <AuditFilterBar
              filters={filters}
              onChangeFilter={(key, value) => setFilters((prev) => ({ ...prev, [key]: value }))}
              onResetFilter={(key) => {
                // Compute next state synchronously so the refetch can't see a
                // stale filters snapshot.
                const next = {
                  ...filters,
                  [key]: AUDIT_FILTERS.find((d) => d.key === key)?.defaultValue,
                }
                setFilters(next)
                loadConversations(next)
              }}
              onToggleFilter={(key, value) => {
                // Set + refetch synchronously so the single click takes effect now.
                const next = { ...filters, [key]: value }
                setFilters(next)
                loadConversations(next)
              }}
              onApply={() => loadConversations()}
              ctx={{ speakers, datasets }}
              loading={loading}
            />
          )}

          {scanCapped && !archivedOnly && (
            <Alert tone="warning" icon={<AlertTriangle className="h-4 w-4" />}>
              Showing a capped working set — narrow filters or archive in batches to see the rest.
            </Alert>
          )}

          {/* Toolbar */}
          {!archivedOnly && (
            <AuditToolbar
              total={total}
              selectedCount={selected.size}
              mergeEligible={mergeEligible}
              unanalyzedCount={unanalyzedCount}
              analyzing={analyzing}
              triagePendingCount={triagePending.pending_count}
              triageConversationCount={triagePending.conversation_count}
              applyingTriage={applyingTriage}
              onApplyTriage={applyTriage}
              onAnalyze={runAnalysis}
              onMerge={() => setMergeTargets(selectedRows)}
              onExport={() => setExportOpen(true)}
            />
          )}

          {/* Table */}
          <AuditTable
            rows={rows}
            loading={loading}
            archivedView={archivedOnly}
            selected={selected}
            onToggleSelect={toggleSelect}
            onSelectMany={selectMany}
            onToggleSelectAll={toggleSelectAll}
            onSplit={(row) => setSplitTarget(row)}
            onTriageChanged={refreshTriagePending}
            marginalThreshold={marginalThreshold}
          />

          {!archivedOnly && (
            <p className="text-xs text-gray-400">
              Tip: run <strong>Analyze audio</strong> first to populate speech metrics. Conversations
              showing “—” haven’t been analyzed yet and won’t match a speech-fraction filter.
            </p>
          )}
        </>
      )}

      {/* Modals */}
      {splitTarget && (
        <SplitConversationModal
          conversation={splitTarget}
          onClose={() => setSplitTarget(null)}
          onDone={onOperationDone}
        />
      )}
      {mergeTargets && (
        <MergePreviewModal
          conversations={mergeTargets}
          onClose={() => setMergeTargets(null)}
          onDone={onOperationDone}
        />
      )}
      {exportOpen && (
        <ExportModal selected={selectedRows} onClose={() => setExportOpen(false)} />
      )}

    </div>
  )
}
