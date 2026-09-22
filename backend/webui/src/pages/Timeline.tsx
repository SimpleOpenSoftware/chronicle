import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, Bookmark, CalendarDays, ChevronLeft, ChevronRight,
  Combine, Image, MoreHorizontal, RefreshCw, ScrollText,
} from 'lucide-react'
import ReconciliationProgress from '../components/timeline/ReconciliationProgress'
import EpisodeCard from '../components/timeline/EpisodeCard'
import EpisodeLabelBar from '../components/timeline/EpisodeLabelBar'
import DayReviewBoard from '../components/timeline/DayReviewBoard'
import PrivacyIntervals from '../components/timeline/PrivacyIntervals'
import MemorySelectionPanel from '../components/timeline/MemorySelectionPanel'
import SessionMemoryBoard from '../components/timeline/SessionMemoryBoard'
import ContextRefreshes from '../components/ContextRefreshes'
import PhotoExplorationPanel from '../components/timeline/PhotoExplorationPanel'
import EpisodeReviewCheckpoint from '../components/timeline/EpisodeReviewCheckpoint'
import { TapeCoverageInterval } from '../components/timeline/EvidenceTape'
import { isSemanticMemoryEligible } from '../components/timeline/episodePresentation'
import { EmptyDayHandoff, ReviewBacklogMenu } from '../components/timeline/ReviewCursor'
import { dateFromSearch, localDate, shiftDate } from '../components/timeline/timelineNavigation'
import { useTimelineTimezone } from '../hooks/useTimelineTimezone'
import {
  ManualMemory, TimelineEpisode, TimelineEpisodeUpdate, TimelineReconciliationRequest,
  deviceInputApi, manualMemoriesApi, timelineApi,
} from '../services/api'
import { Button } from '../components/ui'

function analysisMessage(state?: string, retryAfter?: string | null) {
  if (state === 'pending' || state === 'preparing') return 'Timeline analysis is queued.'
  if (state === 'running') return 'Reading the day’s evidence and forming episodes.'
  if (state === 'validating') return 'Checking episode boundaries and evidence citations.'
  if (state === 'quota_deferred') return `Analysis is waiting for Codex capacity${retryAfter ? ` until ${new Date(retryAfter).toLocaleString()}` : ''}.`
  if (state === 'awaiting_evidence') return 'No usable evidence has arrived for this day yet.'
  return null
}

function readinessMessage(request?: TimelineReconciliationRequest) {
  if (!request) return null
  const { reason, target_asset_count: count, latest_eligible_asset_date: watermark } = request
  if (reason === 'assets_on_day') return `${count} eligible Immich asset${count === 1 ? '' : 's'} found for this day.`
  if (reason === 'later_asset_watermark') return `Immich contains an eligible asset captured on ${watermark}, after this day.`
  if (reason === 'no_immich_evidence') return 'No eligible Immich evidence is available for this interval yet. Open Immich, back up photos up to the cutoff below, then check again.'
  if (reason === 'user_bypassed_immich') return 'Continuing without Immich evidence at your request.'
  if (reason === 'immich_unconfigured') return 'Immich is not configured for Chronicle, so reconciliation cannot start.'
  return 'Chronicle could not reach Immich. Reconciliation did not start.'
}

function visualEvidenceMessage(request?: TimelineReconciliationRequest) {
  const visual = request?.immich_visual
  if (!visual || visual.state === 'not_needed') return null
  if (visual.state === 'pending') return 'Photo metadata is available; visual exploration is queued.'
  if (visual.state === 'running') return 'Chronicle is sampling photo grids and investigating follow-up questions.'
  const useful = `${visual.helpful_count} useful for reconstructing the day`
  if (visual.state === 'failed') return `Photo review failed for ${visual.candidate_count} selected photos.`
  if (visual.state === 'partial') return `${visual.analyzed_count} photos described (${useful}); ${visual.failed_count} failed.`
  return `${visual.analyzed_count} of ${visual.candidate_count} photos inspected; ${visual.uninspected_count} remain uninspected. ${useful}.`
}

function reviewLabel(state?: string) {
  if (state === 'memory_pending') return 'Memory decision ready'
  if (state === 'failed') return 'Memory review needs attention'
  return null
}

const STRUCTURAL_CONFIRMATION_FIELDS = ['started_at', 'ended_at', 'evidence_refs']

function hasStableEpisodeStructure(episode: TimelineEpisode) {
  return episode.status === 'settled'
    || STRUCTURAL_CONFIRMATION_FIELDS.every(field => episode.confirmed_fields.includes(field))
}

function clockTime(value: string) {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function ManualMemoryPreview({ memory }: { memory: ManualMemory }) {
  const attachment = memory.attachments[0]
  const thumbnail = useQuery({
    queryKey: ['manual-memory-thumbnail', memory.memory_id, attachment?.attachment_id],
    queryFn: async () => (await manualMemoriesApi.getThumbnail(memory.memory_id, attachment.attachment_id)).data,
    enabled: Boolean(attachment),
    staleTime: Infinity,
  })
  const url = useMemo(() => thumbnail.data ? URL.createObjectURL(thumbnail.data) : null, [thumbnail.data])
  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url)
  }, [url])
  if (thumbnail.isLoading) return <div className="aspect-[4/3] animate-pulse bg-gray-100 dark:bg-gray-800" />
  if (!url) return <div className="flex aspect-[4/3] items-center justify-center bg-gray-100 text-gray-400 dark:bg-gray-800"><Image className="h-7 w-7" /></div>
  return <img src={url} alt="" className="aspect-[4/3] w-full bg-gray-100 object-contain dark:bg-gray-800" />
}

function ManualMemories({ items }: { items: ManualMemory[] }) {
  return (
    <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
      <h3 className="font-medium text-gray-900 dark:text-gray-100">Manual memories</h3>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Explicitly saved material for this account, independent of timeline analysis.</p>
      {items.length ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {items.map(item => {
            const description = item.attachments.find(attachment => attachment.description)?.description || ''
            return (
              <article key={item.memory_id} className="overflow-hidden rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)]">
                <ManualMemoryPreview memory={item} />
                <div className="p-3">
                  <p className="line-clamp-2 text-sm text-gray-800 dark:text-gray-200">{item.note || description || 'Manual memory.'}</p>
                  <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">{new Date(item.shared_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</p>
                </div>
              </article>
            )
          })}
        </div>
      ) : <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">No manual memories saved yet.</p>}
    </section>
  )
}

function CoverageInspector({ coverage }: { coverage: TapeCoverageInterval[] }) {
  if (!coverage.length) return null
  return (
    <details className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5">
      <summary className="cursor-pointer text-sm font-semibold text-gray-800 marker:text-gray-400 dark:text-gray-200">
        Inspect {coverage.length} coverage interval{coverage.length === 1 ? '' : 's'}
      </summary>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {coverage.map((interval, index) => (
          <div key={`${interval.kind}:${interval.started_at}:${interval.ended_at}:${index}`} className="rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-2 text-xs">
            <p className="font-semibold text-gray-800 dark:text-gray-200">{interval.label}</p>
            <p className="mt-0.5 text-gray-500 dark:text-gray-400">{clockTime(interval.started_at)}–{clockTime(interval.ended_at)}</p>
          </div>
        ))}
      </div>
    </details>
  )
}

export default function Timeline() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const {
    timezone, browserTimezone, storedTimezone, shouldOfferBrowserTimezone,
    saveBrowserTimezone, savingBrowserTimezone,
  } = useTimelineTimezone()
  const today = localDate(new Date(), timezone)
  const day = dateFromSearch(`?${searchParams.toString()}`, today)
  const [showRaw, setShowRaw] = useState(false)
  const [showManualMemories, setShowManualMemories] = useState(false)
  const [labeling, setLabeling] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [reconciliationRequestId, setReconciliationRequestId] = useState<string | null>(null)
  const reconciliationStorageKey = `chronicle.timeline.reconciliation:${timezone}:${day}`

  useEffect(() => {
    setReconciliationRequestId(window.localStorage.getItem(reconciliationStorageKey))
  }, [reconciliationStorageKey])

  const setDay = (nextDay: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('date', nextDay)
    setSearchParams(next)
    setSelected(new Set())
    setLabeling(false)
    setReconciliationRequestId(null)
  }

  const timeline = useQuery({
    queryKey: ['semantic-timeline', day, timezone],
    queryFn: async () => (await timelineApi.getDay(day, timezone)).data,
    refetchInterval: query => {
      if (['queued', 'running'].includes(query.state.data?.latest_reconciliation?.state || '')) return 5_000
      const state = query.state.data?.analysis?.state
      const consolidationState = query.state.data?.consolidation?.state
      if (consolidationState === 'queued' || consolidationState === 'generating') return 5_000
      // A completed run is a checkpoint. Late uploads and later capture can dirty
      // this date without changing its analysis state (including historical dates).
      return state && !['complete', 'failed', 'awaiting_evidence'].includes(state) ? 10_000 : 30_000
    },
  })
  const reviewQueue = useQuery({
    queryKey: ['timeline-review-queue', timezone],
    queryFn: async () => (await timelineApi.getReviewQueue(timezone)).data.items,
    refetchInterval: query => query.state.data?.some(item => ['memory_queued', 'memory_generating', 'memory_applying'].includes(item.state)) ? 5_000 : false,
  })
  const projectionStart = timeline.data?.review_projection?.day_started_at
  const projectionEnd = timeline.data?.review_projection?.day_ended_at
  const raw = useQuery({
    queryKey: ['raw-device-timeline', day, timezone],
    queryFn: async () => (await deviceInputApi.getTimeline(projectionStart!, projectionEnd!)).data.items,
    enabled: showRaw && Boolean(projectionStart && projectionEnd),
  })
  const manualMemories = useQuery({
    queryKey: ['manual-memories'],
    queryFn: async () => (await manualMemoriesApi.list()).data.items,
    enabled: showManualMemories,
  })
  const reconcile = useMutation({
    mutationFn: async () => (await timelineApi.reconcileDay(day, timezone)).data,
    onSuccess: request => {
      window.localStorage.setItem(reconciliationStorageKey, request.request_id)
      setReconciliationRequestId(request.request_id)
    },
  })
  const bypassImmich = useMutation({
    mutationFn: async () => (await timelineApi.reconcileDay(day, timezone, true)).data,
    onSuccess: request => {
      window.localStorage.setItem(reconciliationStorageKey, request.request_id)
      setReconciliationRequestId(request.request_id)
    },
  })
  const reconciliation = useQuery({
    queryKey: ['timeline-reconciliation', reconciliationRequestId],
    queryFn: async () => (await timelineApi.getReconciliation(reconciliationRequestId!)).data,
    enabled: Boolean(reconciliationRequestId),
    refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state || '') ? 5_000 : false,
  })
  const reconciliationStatus = [timeline.data?.latest_reconciliation, reconciliation.data, reconcile.data]
    .filter((request): request is TimelineReconciliationRequest => Boolean(request))
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || Date.parse(b.updated_at) - Date.parse(a.updated_at))[0]
  useEffect(() => {
    if (reconciliationStatus?.state !== 'completed') return
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
      queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
    ])
  }, [day, queryClient, reconciliationStatus?.request_id, reconciliationStatus?.state, timezone])
  const refreshDay = () => {
    setSelected(new Set())
    return queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] })
  }
  const refreshDayAndQueue = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
    queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
  ])
  const adjust = useMutation({ mutationFn: ({ episodeId, changes }: { episodeId: string; changes: TimelineEpisodeUpdate }) => timelineApi.updateEpisode(episodeId, changes), onSuccess: refreshDay })
  const split = useMutation({ mutationFn: ({ episodeId, at }: { episodeId: string; at: string }) => timelineApi.splitEpisode(episodeId, at), onSuccess: refreshDay })
  const group = useMutation({ mutationFn: (episodeIds: string[]) => timelineApi.groupEpisodes(day, timezone, timeline.data!.current_snapshot_id!, episodeIds), onSuccess: refreshDay })
  const remove = useMutation({ mutationFn: (episodeId: string) => timelineApi.deleteEpisode(episodeId), onSuccess: refreshDay })
  const dismissFailedRange = useMutation({
    mutationFn: ({ dirtyRangeId, reason }: { dirtyRangeId: string; reason: string }) => timelineApi.dismissFailedRange(dirtyRangeId, reason),
    onSuccess: refreshDayAndQueue,
  })
  const rejectActivity = useMutation({
    mutationFn: (episode: TimelineEpisode) => timelineApi.rejectActivity(episode, timeline.data!.current_snapshot_id!, day, timezone),
    onSuccess: refreshDayAndQueue,
    onError: refreshDayAndQueue,
  })
  const confirmStructure = useMutation({
    mutationFn: ({ episodes }: { sessionId: string; episodes: TimelineEpisode[] }) => timelineApi.confirmSessionStructures(
      day, timezone, timeline.data!.current_snapshot_id!, episodes.map(episode => ({ episode_key: episode.episode_key, revision: episode.revision })),
    ),
    onSuccess: refreshDayAndQueue,
    onError: refreshDayAndQueue,
  })

  const finalizeEpisodes = useMutation({
    mutationFn: () => timelineApi.finalizeEpisodes(day, timezone, timeline.data!.current_snapshot_id!),
    onSuccess: async () => {
      setLabeling(false)
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
      ])
    },
  })
  const mutating = adjust.isPending || split.isPending || group.isPending || remove.isPending
  const mutationError = [adjust, split, group, remove].find(mutation => mutation.error)?.error

  const episodes = timeline.data?.episodes || []
  const memoryEligibleCount = episodes.filter(isSemanticMemoryEligible).length
  const referenceOnlyCount = episodes.length - memoryEligibleCount
  const status = timeline.data?.analysis
  const unaccounted = timeline.data?.coverage?.unassigned_intervals || []
  const classified = unaccounted.some(interval => interval.cause)
  const unexplained = classified ? unaccounted.filter(item => item.cause === 'unexplained') : unaccounted
  const uncaptured = classified ? unaccounted.filter(item => item.cause === 'no_capture') : []
  const unreconciled = timeline.data?.reconciliation?.ranges || []
  const failedRanges = unreconciled.filter(range => range.state === 'failed')
  const unstableEpisodes = episodes.filter(episode =>
    episode.requires_activity_review
    && (episode.status === 'open' || episode.status === 'provisional')
    && !hasStableEpisodeStructure(episode))
  const progressMessage = analysisMessage(status?.state, status?.retry_after)
  const processing = !!status && ['pending', 'preparing', 'running', 'validating', 'quota_deferred'].includes(status.state)
  const currentMemoryReviewLabel = reviewLabel(timeline.data?.review?.state)
  const recordingIntervals = timeline.data?.coverage?.recording_intervals
  const coverage = useMemo<TapeCoverageInterval[]>(() => [
    ...(recordingIntervals || []).map(item => ({
      started_at: item.started_at, ended_at: item.ended_at, kind: 'recording' as const,
      label: `Recording · no speech detected · ${item.source}`,
      detail: `${Math.round(item.covered_seconds / 60)} min recorded; ${item.acoustic_active_seconds.toFixed(1)} s above the sound-activity threshold.${item.missing_seconds > 0 ? ` ${item.missing_seconds} s not covered by source timestamps within this span; exact gap positions are unavailable.` : ' No missing recording coverage.'} Paused speech or playback is not a recording gap.`,
    })),
    ...unexplained.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'unexplained' as const, label: item.reason || 'Captured but unexplained' })),
    ...uncaptured.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'no_capture' as const, label: item.reason || 'No capture' })),
    ...unreconciled.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'unreconciled' as const, label: `Awaiting reconciliation · ${item.state}` })),
  ], [unexplained, uncaptured, unreconciled, recordingIntervals])
  const reviewGrouping = () => {
    setLabeling(true)
    setSelected(new Set())
    requestAnimationFrame(() => document.querySelector<HTMLElement>('#suggested-grouping')?.scrollIntoView?.({ block: 'center', behavior: 'smooth' }))
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-gray-900 dark:text-gray-100"><CalendarDays className="h-6 w-6 text-[var(--tape-media)]" /> Timeline</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Sessions and episodes from your captured evidence.</p>
        </div>
        <div className="flex items-end gap-1.5">
          <Button variant="ghost" size="sm" aria-label="Previous day" onClick={() => setDay(shiftDate(day, -1))}><ChevronLeft className="h-4 w-4" /></Button>
          <label className="flex flex-col gap-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">
            Date
            <input type="date" value={day} onChange={event => setDay(event.target.value)} className="min-h-9 rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-2.5 py-1.5 text-sm font-medium normal-case tracking-normal text-gray-900 outline-none focus:ring-2 focus:ring-[var(--tape-focus)] dark:text-gray-100" />
          </label>
          <Button variant="ghost" size="sm" aria-label="Next day" onClick={() => setDay(shiftDate(day, 1))}><ChevronRight className="h-4 w-4" /></Button>
        </div>
      </header>

      {shouldOfferBrowserTimezone && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2 text-xs text-gray-600 dark:text-gray-300">
          <span>{storedTimezone ? `Times are shown in ${storedTimezone}; this browser reports ${browserTimezone}.` : `Using browser timezone: ${browserTimezone}.`}</span>
          <Button variant="ghost" size="sm" onClick={saveBrowserTimezone} disabled={savingBrowserTimezone}>{storedTimezone ? 'Use browser timezone' : 'Save browser timezone'}</Button>
        </div>
      )}

      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5 text-xs text-gray-600 dark:text-gray-300">
        {timeline.isFetching && <RefreshCw className="h-4 w-4 animate-spin text-gray-400" />}
        <span className="font-semibold text-gray-800 dark:text-gray-200">{episodes.length} episodes</span>
        {timeline.data?.coverage?.window_count != null && <span>· {timeline.data.coverage.window_count} evidence windows</span>}
        {!!coverage.length && <span className="flex items-center gap-1 text-amber-800 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5" />{coverage.length} coverage intervals</span>}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1">
          {currentMemoryReviewLabel && <Link to={`/memory-ledger?view=review&date=${day}`} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-semibold text-[var(--tape-focus)] hover:bg-[var(--tape-chip)]"><ScrollText className="h-3.5 w-3.5" />{currentMemoryReviewLabel}</Link>}
          <ReviewBacklogMenu items={reviewQueue.data || []} day={day} />
          <details className="relative">
            <summary className="flex cursor-pointer list-none items-center gap-1 rounded-md px-2 py-1 font-semibold text-gray-700 hover:bg-[var(--tape-chip)] dark:text-gray-200"><MoreHorizontal className="h-4 w-4" /> Day tools</summary>
            <div className="absolute right-0 z-30 mt-1 w-48 space-y-1 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-1.5 shadow-lg">
              <button type="button" onClick={() => setShowManualMemories(value => !value)} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)]"><Bookmark className="h-4 w-4" />{showManualMemories ? 'Hide' : 'Show'} manual memories</button>
              <button type="button" onClick={() => setShowRaw(value => !value)} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)]"><ScanLineIcon />{showRaw ? 'Hide' : 'Show'} raw capture</button>
              <button type="button" onClick={() => reconcile.mutate()} disabled={reconcile.isPending || ['queued', 'running'].includes(reconciliationStatus?.state || '')} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)] disabled:opacity-40"><RefreshCw className="h-4 w-4" />Reconcile day</button>
            </div>
          </details>
        </div>
      </section>

      {progressMessage && episodes.length > 0 && <div className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5 text-sm text-gray-600 dark:text-gray-300">{progressMessage}</div>}
      {reconciliationStatus?.progress && ['queued', 'running'].includes(reconciliationStatus.state) && <ReconciliationProgress progress={reconciliationStatus.progress} status={reconciliationStatus.state} />}
      {reconciliationStatus && (
        <div className={`rounded-lg border px-3 py-3 text-sm ${reconciliationStatus.state === 'blocked' || reconciliationStatus.state === 'failed' ? 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200' : 'border-[var(--tape-line)] bg-[var(--tape-paper)] text-gray-700 dark:text-gray-200'}`}>
          <div className="flex flex-wrap items-center gap-2">
            {(reconciliationStatus.state === 'queued' || reconciliationStatus.state === 'running') && <RefreshCw className="h-4 w-4 animate-spin" />}
            {(reconciliationStatus.state === 'blocked' || reconciliationStatus.state === 'failed') && <AlertTriangle className="h-4 w-4" />}
            <span className="font-semibold">Reconciliation {reconciliationStatus.state}{reconciliationStatus.state === 'completed' && ` through ${new Date(reconciliationStatus.evidence_cutoff).toLocaleString('en-IN', { timeZone: timezone })} (${timezone})`}</span>
          </div>
          {reconciliationStatus.state !== 'completed' && <p className="mt-1.5">{readinessMessage(reconciliationStatus)}</p>}
          <p className="mt-1 text-xs">{reconciliationStatus.state === 'completed' ? 'Later evidence is not included.' : `Evidence cutoff: ${new Date(reconciliationStatus.evidence_cutoff).toLocaleString('en-IN', { timeZone: timezone })} (${timezone}).`}</p>
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer">Evidence details</summary>
            <p className="mt-2">Checked {new Date(reconciliationStatus.checked_at).toLocaleString('en-IN', { timeZone: timezone })} ({timezone})</p>
          {visualEvidenceMessage(reconciliationStatus) && <p className="mt-1 text-xs opacity-80">{visualEvidenceMessage(reconciliationStatus)}</p>}
          {reconciliationStatus.immich_evidence && reconciliationStatus.immich_evidence.evidence_count > 0 && (
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer font-medium">
                {reconciliationStatus.immich_evidence.evidence_count} Immich photos considered across {reconciliationStatus.immich_evidence.window_count} evidence windows
              </summary>
              <div className="mt-1 grid gap-1 opacity-80">
                {reconciliationStatus.immich_evidence.windows.map(window => (
                  <span key={`${window.started_at}-${window.ended_at}`}>
                    {new Date(window.started_at).toLocaleTimeString('en-IN', { timeZone: timezone, hour: '2-digit', minute: '2-digit' })}–{new Date(window.ended_at).toLocaleTimeString('en-IN', { timeZone: timezone, hour: '2-digit', minute: '2-digit' })}: {window.asset_count} photo{window.asset_count === 1 ? '' : 's'}, {window.helpful_asset_count} useful
                  </span>
                ))}
              </div>
            </details>
          )}
          {reconciliationStatus.immich_visual?.artifact_id && <PhotoExplorationPanel requestId={reconciliationStatus.request_id} />}
          </details>
          {reconciliationStatus.state === 'blocked' && reconciliationStatus.notification_id && <p className="mt-1 text-xs opacity-75">Backup reminder: {reconciliationStatus.notification_status || 'queued'}</p>}
          {reconciliationStatus.last_error && <p className="mt-1 text-xs text-red-700 dark:text-red-300">{reconciliationStatus.last_error}</p>}
          {(reconciliationStatus.state === 'blocked' || reconciliationStatus.state === 'failed') && (
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" onClick={() => reconcile.mutate()} disabled={reconcile.isPending || bypassImmich.isPending}>{reconciliationStatus.state === 'blocked' ? 'Check again' : 'Retry reconciliation'}</Button>
              {reconciliationStatus.state === 'blocked' && reconciliationStatus.reason === 'no_immich_evidence' && <Button size="sm" variant="secondary" onClick={() => bypassImmich.mutate()} disabled={reconcile.isPending || bypassImmich.isPending}>Continue without Immich</Button>}
            </div>
          )}
        </div>
      )}
      {status?.state === 'failed' && !reconciliationStatus && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <AlertTriangle className="h-4 w-4" /><span className="min-w-0 flex-1">The previous analysis failed. {status.error}</span><Button size="sm" variant="danger" onClick={() => reconcile.mutate()}>Reconcile day</Button>
        </div>
      )}
      {timeline.isError && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <AlertTriangle className="h-4 w-4" />
          <span className="min-w-0 flex-1">Could not load this day. {(timeline.error as Error).message}</span>
          <Button size="sm" variant="danger" onClick={() => timeline.refetch()}>Retry</Button>
        </div>
      )}
      {episodes.length > 0 && unreconciled.some(range => range.state === 'pending') && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <div>
            <p className="font-semibold">Evidence still needs reconciliation.</p>
            <p className="mt-1 text-xs">New or updated evidence is available for this day.</p>
          </div>
          <Button size="sm" onClick={() => reconcile.mutate()} disabled={reconcile.isPending || bypassImmich.isPending || ['queued', 'running'].includes(reconciliationStatus?.state || '')}>Reconcile available evidence</Button>
        </div>
      )}
      <CoverageInspector coverage={coverage} />
      <PrivacyIntervals intervals={timeline.data?.coverage?.privacy_intervals || []} timezone={timezone} />
      <ContextRefreshes day={day} />
      {episodes.length > 0 && timeline.data?.current_snapshot_id && <SessionMemoryBoard day={day} timezone={timezone} snapshotId={timeline.data.current_snapshot_id} />}
      {(episodes.length > 0 || failedRanges.length > 0) && <details open={failedRanges.length > 0 || undefined} className="rounded-lg border border-[var(--tape-line)] p-3 text-[var(--tape-ink)]">
        <summary className="cursor-pointer text-sm">Individual episodes & structure tools</summary>
      {episodes.length > 0 && <MemorySelectionPanel day={day} timezone={timezone} snapshotId={timeline.data?.current_snapshot_id} episodes={episodes} />}
      {((timeline.data?.review && episodes.length > 0) || failedRanges.length > 0) && (
        <EpisodeReviewCheckpoint
          day={day}
          timezone={timezone}
          review={timeline.data?.review || null}
          episodeCount={episodes.length}
          eligibleCount={memoryEligibleCount}
          referenceOnlyCount={referenceOnlyCount}
          unreconciledRanges={unreconciled}
          unstableEpisodes={unstableEpisodes}
          consolidation={timeline.data?.consolidation || null}
          finalizing={finalizeEpisodes.isPending}
          dismissingRangeId={dismissFailedRange.isPending ? dismissFailedRange.variables?.dirtyRangeId : null}
          rejectingEpisodeId={rejectActivity.isPending ? rejectActivity.variables?.episode_id : null}
          rejectionError={rejectActivity.error as Error | null}
          onNotActivity={episode => rejectActivity.mutate(episode)}
          confirmingSessionId={confirmStructure.isPending ? confirmStructure.variables?.sessionId : null}
          projection={timeline.data!.review_projection}
          episodes={episodes}
          error={finalizeEpisodes.error as Error | null}
          dismissalError={dismissFailedRange.error as Error | null}
          confirmationError={confirmStructure.error as Error | null}
          onReviewGrouping={reviewGrouping}
          onDismissRange={(dirtyRangeId, reason) => dismissFailedRange.mutate({ dirtyRangeId, reason })}
          onConfirmStructures={(sessionId, episodes) => confirmStructure.mutate({ sessionId, episodes })}
          onEditEpisode={episode => {
            setLabeling(true)
            requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-episode-id="${CSS.escape(episode.episode_id)}"]`)?.scrollIntoView?.({ block: 'center', behavior: 'smooth' }))
          }}
          onFinish={() => timeline.data?.current_snapshot_id && finalizeEpisodes.mutate()}
        />
      )}
      </details>}
      {showManualMemories && (manualMemories.isLoading ? <div className="rounded-lg border border-[var(--tape-line)] p-4 text-sm text-gray-500">Loading manual memories…</div> : <ManualMemories items={manualMemories.data || []} />)}

      {labeling && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-[var(--tape-focus)] bg-[var(--tape-selected)] p-3 text-sm">
          <span className="text-gray-700 dark:text-gray-200">{selected.size ? `${selected.size} selected` : 'Select two or more episodes to group, or correct one below.'}</span>
          <Button size="sm" disabled={selected.size < 2 || mutating} onClick={() => group.mutate([...selected])} icon={<Combine className="h-4 w-4" />}>Group selected</Button>
          {!!selected.size && <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>}
          {!!mutationError && <p className="w-full text-xs text-red-700 dark:text-red-300">{(mutationError as { message?: string }).message || 'That edit was rejected.'}</p>}
        </div>
      )}

      {timeline.data?.review_projection && episodes.length ? (
        <details open={labeling || undefined} className="rounded-lg border border-[var(--tape-line)] p-3 text-[var(--tape-ink)]">
        <summary className="cursor-pointer text-sm">Inspect episode timeline & grouping</summary>
        <DayReviewBoard
          day={day}
          timezone={timezone}
          projection={timeline.data.review_projection}
          episodes={episodes}
          coverage={coverage}
          initialProposal={timeline.data.consolidation}
          snapshot={{
            snapshot_state: timeline.data.snapshot_state,
            current_snapshot_id: timeline.data.current_snapshot_id,
            reviewed_snapshot_id: timeline.data.reviewed_snapshot_id,
            applied_snapshot_id: timeline.data.applied_snapshot_id,
          }}
          labeling={labeling}
          onToggleEditing={() => { setLabeling(value => !value); setSelected(new Set()) }}
          onSelectGroup={episodeIds => setSelected(new Set(episodeIds))}
          renderEpisode={episode => (
            <div key={episode.episode_id}>
              <EpisodeCard episode={episode} nested={episode.activity_mode === 'background' || !!episode.parent_episode_id} />
              <EpisodeLabelBar
                episode={episode}
                selected={selected.has(episode.episode_id)}
                busy={mutating}
                nested={episode.activity_mode === 'background' || !!episode.parent_episode_id}
                onToggleSelected={() => setSelected(current => {
                  const next = new Set(current)
                  next.has(episode.episode_id) ? next.delete(episode.episode_id) : next.add(episode.episode_id)
                  return next
                })}
                onAdjust={changes => adjust.mutate({ episodeId: episode.episode_id, changes })}
                onSplit={at => split.mutate({ episodeId: episode.episode_id, at })}
                onDelete={() => remove.mutate(episode.episode_id)}
              />
            </div>
          )}
        />
        </details>
      ) : !timeline.isLoading && !timeline.isError ? (
        <EmptyDayHandoff
          items={reviewQueue.data || []}
          title={processing
            ? day === today ? 'Today’s episodes are still processing.' : 'This day’s episodes are still processing.'
            : status?.state === 'awaiting_evidence'
            ? 'No usable evidence for analysis yet.'
            : status?.state === 'complete'
              ? 'Analysis found no episodes for this day.'
              : status?.state === 'failed'
                ? 'This day’s analysis needs attention.'
              : day === today ? 'Today has no processed episodes yet.' : 'This day has no processed episodes yet.'}
          description={processing
            ? progressMessage || 'Analysis is in progress.'
            : unreconciled.some(range => range.state === 'pending')
            ? 'Captured evidence is awaiting reconciliation.'
            : status?.state === 'awaiting_evidence'
            ? 'Captured material may still be arriving or processing.'
            : status?.state === 'complete'
              ? 'Analysis completed without producing an episode.'
              : status?.state === 'failed'
                ? 'Retry this day or continue an earlier review.'
              : 'Reconcile this day or continue an earlier review.'}
          canAnalyze={!status || status.state === 'complete' || (status.state === 'awaiting_evidence' && unreconciled.some(range => range.state === 'pending'))}
          analyzing={reconcile.isPending || ['queued', 'running'].includes(reconciliationStatus?.state || '')}
          analyzeLabel={reconciliationStatus?.state === 'blocked' ? 'Check Immich again' : 'Reconcile this day'}
          onAnalyze={() => reconcile.mutate()}
        />
      ) : null}

      {showRaw && (
        <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
          <h3 className="font-medium text-gray-900 dark:text-gray-100">Raw capture diagnostics</h3>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Transport and observation rows used to build evidence, not the semantic timeline.</p>
          {raw.isLoading && <p className="mt-3 text-sm text-gray-500">Loading raw capture…</p>}
          {raw.data && <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">{raw.data.length} raw items · {raw.data.filter(item => item.kind === 'audio').length} audio chunks · {raw.data.filter(item => item.kind !== 'audio').length} visual/context items</p>}
        </section>
      )}
    </div>
  )
}

function ScanLineIcon() {
  return <span className="inline-flex h-4 w-4 items-center justify-center text-[10px] font-bold" aria-hidden="true">|||</span>
}
