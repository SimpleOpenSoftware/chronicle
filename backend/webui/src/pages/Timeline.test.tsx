// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../contexts/AuthContext'
import { authApi, TimelineDay, timelineApi } from '../services/api'
import Timeline from './Timeline'

const todayInKolkata = () => {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const value = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}

const TEST_DAY = todayInKolkata()
const TEST_SNAPSHOT = 'abcdef0'.padEnd(64, '1')

const emptyDay = {
  date: TEST_DAY,
  timezone: 'Asia/Calcutta',
  current_snapshot_id: null,
  reviewed_snapshot_id: null,
  applied_snapshot_id: null,
  snapshot_state: 'dirty',
  coverage: { unassigned_intervals: [] },
  analysis: null,
  consolidation: null,
  semantic_groups: [],
  review_decision_count: 0,
  review_projection: {
    version: 'test',
    day_started_at: '2026-08-27T18:30:00.000Z',
    day_ended_at: '2026-08-28T18:30:00.000Z',
    episode_count: 0,
    group_count: 0,
    needs_attention_count: 0,
    confirmed_count: 0,
    groups: [],
  },
  review: {
    state: 'episodes_pending',
    review_snapshot_id: null,
    episodes_reviewed_at: null,
    resolved_at: null,
    outcome: null,
    error: null,
    proposal: null,
  },
  reconciliation: { ranges: [] },
  episodes: [],
} satisfies TimelineDay

const reviewableDay = {
  ...emptyDay,
  current_snapshot_id: TEST_SNAPSHOT,
  snapshot_state: 'ready',
  review_projection: {
    ...emptyDay.review_projection,
    episode_count: 1,
    group_count: 1,
    groups: [{
      group_id: 'session-1',
      started_at: '2026-08-28T09:00:00.000Z',
      ended_at: '2026-08-28T09:30:00.000Z',
      title: 'Planning Chronicle work',
      summary: 'Reviewed the implementation plan.',
      semantic: false,
      lane: 'conversation' as const,
      episode_ids: ['episode-1'],
      episode_count: 1,
      conversational_count: 1,
      confirmed_count: 0,
      duration_seconds: 1800,
      span_seconds: 1800,
      gap_seconds: 0,
      intervals: [{ lane: 'conversation' as const, episode_id: 'episode-1', started_at: '2026-08-28T09:00:00.000Z', ended_at: '2026-08-28T09:30:00.000Z' }],
      entities: [],
      salience: 'routine' as const,
      attention_reasons: [],
      needs_attention: false,
    }],
  },
  episodes: [{
    episode_id: 'episode-1',
    episode_key: 'episode-key-1',
    revision: 3,
    started_at: '2026-08-28T09:00:00.000Z',
    ended_at: '2026-08-28T09:30:00.000Z',
    kind: 'conversation',
    title: 'Planning Chronicle work',
    summary: 'Reviewed the implementation plan.',
    status: 'settled' as const,
    confirmed_at: null,
    confirmed_fields: [],
    requires_activity_review: true, memory_eligible: true, memory_policy: 'auto' as const,
    salience: 'routine' as const,
    confidence: 0.91,
    activity_mode: 'foreground' as const,
    entities: [],
    attributes: {},
    assertions: [],
    evidence: [],
    related_episode_ids: [],
    related_conversation_ids: [],
    audio_ranges: [],
    parent_episode_id: null,
    has_thumbnail: false,
  }],
} satisfies TimelineDay

function renderTimeline(
  analysis: TimelineDay['analysis'],
  dayData: TimelineDay = emptyDay,
  refreshedDay?: TimelineDay,
  useRefreshedDay: () => boolean = () => false,
  requestState = 'blocked',
) {
  localStorage.setItem('root_token', 'test-token')
  vi.spyOn(authApi, 'getMe').mockResolvedValue({ data: {
    id: 'user-1',
    email: 'user@example.com',
    display_name: 'User',
    assistant_name: null,
    timezone: 'Asia/Calcutta',
    is_superuser: true,
  } } as never)
  const getDay = vi.spyOn(timelineApi, 'getDay')
  if (refreshedDay) {
    getDay.mockImplementation(async () => ({
      data: useRefreshedDay() ? refreshedDay : { ...dayData, analysis },
    } as never))
  } else {
    getDay.mockResolvedValue({ data: { ...dayData, analysis } } as never)
  }
  vi.spyOn(timelineApi, 'getReviewQueue').mockResolvedValue({ data: { items: [{
    date: '2026-02-19',
    state: 'episodes_pending',
    outcome: null,
    episode_count: 32,
    unexplained_count: 0,
    capture_gap_count: 0,
    proposal: null,
  }] } } as never)
  const reconciliation = {
    request_id: 'request-one',
    date: TEST_DAY,
    timezone: 'Asia/Calcutta',
    state: requestState,
    reason: 'no_immich_evidence',
    target_asset_count: 0,
    latest_eligible_asset_date: null,
    checked_at: '2026-08-28T00:00:00.000Z',
    evidence_cutoff: '2026-08-28T00:00:00.000Z',
    notification_id: 'notice-one',
    notification_status: 'queued',
    job_id: null,
    dirty_range_id: null,
    run_id: null,
    last_error: null,
    created_at: '2026-08-28T00:00:00.000Z',
    updated_at: '2026-08-28T00:00:00.000Z',
  }
  const reconcile = vi.spyOn(timelineApi, 'reconcileDay').mockResolvedValue({ data: reconciliation } as never)
  vi.spyOn(timelineApi, 'getReconciliation').mockResolvedValue({ data: reconciliation } as never)

  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter initialEntries={[`/timeline?date=${TEST_DAY}`]}>
          <Timeline />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return reconcile
}

beforeEach(() => {
  vi.spyOn(timelineApi, 'getSessions').mockResolvedValue({ data: { sessions: [] } } as never)
  vi.spyOn(timelineApi, 'getMemorySelections').mockResolvedValue({ data: { proposals: [], outcomes: {} } } as never)
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('Timeline next action', () => {
  it('keeps later evidence visible and actionable after a point-in-time run completes', async () => {
    localStorage.setItem(`chronicle.timeline.reconciliation:Asia/Calcutta:${TEST_DAY}`, 'request-one')
    const reconcile = renderTimeline(null, {
      ...reviewableDay,
      reconciliation: { ranges: [{
        dirty_range_id: 'later-evidence',
        started_at: '2026-08-28T07:03:00.000Z',
        ended_at: '2026-08-28T08:15:00.000Z',
        state: 'pending',
        trigger_reasons: ['transcript_revision'],
        attempts: 0,
        error: null,
        resolution_history: [],
      }] },
    }, undefined, undefined, 'completed')

    expect(await screen.findByText(/Reconciliation completed through/)).toBeVisible()
    expect(await screen.findByText(/Evidence still needs reconciliation/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Reconcile available evidence' }))
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta'))
  })

  it('offers one reconciliation action when a completed empty day receives new evidence', async () => {
    const reconcile = renderTimeline({ state: 'complete' } as never, {
      ...emptyDay,
      reconciliation: { ranges: [{
        dirty_range_id: 'new-evidence', started_at: '2026-08-28T07:03:00Z', ended_at: '2026-08-28T08:15:00Z',
        state: 'pending', trigger_reasons: ['transcript_revision'], attempts: 0, error: null, resolution_history: [],
      }] },
    })
    expect(await screen.findByText('Captured evidence is awaiting reconciliation.')).toBeVisible()
    expect(screen.queryByText('Individual episodes & structure tools')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reconcile available evidence' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reconcile this day' }))
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta'))
  })

  it('refreshes a completed timeline to discover newly dirty evidence', async () => {
    vi.useFakeTimers()
    try {
      let arrived = false
      const refreshedDay: TimelineDay = {
        ...reviewableDay,
        reconciliation: { ranges: [{
          dirty_range_id: 'late-upload',
          started_at: '2026-08-28T07:03:00.000Z',
          ended_at: '2026-08-28T08:15:00.000Z',
          state: 'pending',
          trigger_reasons: ['transcript_revision'],
          attempts: 0,
          error: null,
          resolution_history: [],
        }] },
      }
      const reconcile = renderTimeline(null, reviewableDay, refreshedDay, () => arrived)
      await act(async () => { await vi.advanceTimersByTimeAsync(100) })
      expect(screen.queryByText(/Evidence still needs reconciliation/)).not.toBeInTheDocument()
      arrived = true
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      expect(screen.getByText(/Evidence still needs reconciliation/)).toBeVisible()
      expect(reconcile).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('turns an unprocessed empty day into an explicit review or reconciliation choice', async () => {
    const reconcile = renderTimeline(null)

    expect(await screen.findByRole('heading', { name: 'Today has no processed episodes yet.' })).toBeVisible()
    expect(screen.getByRole('link', { name: /Continue review/i })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(screen.getByText('Next: Feb 19 · Review episodes')).toBeVisible()
    expect(screen.queryByText('Episodes await review')).not.toBeInTheDocument()
    expect(reconcile).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile this day' }))
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta'))
  })

  it('restores durable reconciliation status after a reload', async () => {
    localStorage.setItem(
      `chronicle.timeline.reconciliation:Asia/Calcutta:${TEST_DAY}`,
      'request-one',
    )
    renderTimeline(null)

    await waitFor(() => {
      expect(timelineApi.getReconciliation).toHaveBeenCalledWith('request-one')
    })
    expect(await screen.findByText('Reconciliation blocked')).toBeVisible()
    expect(screen.getByText('Backup reminder: queued')).toBeVisible()
  })

  it('lets the user intentionally continue without Immich evidence', async () => {
    localStorage.setItem(
      `chronicle.timeline.reconciliation:Asia/Calcutta:${TEST_DAY}`,
      'request-one',
    )
    const reconcile = renderTimeline(null)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue without Immich' }))

    await waitFor(() => {
      expect(reconcile).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta', true)
    })
  })

  it('keeps the Timeline review cursor actionable while today is processing', async () => {
    const reconcile = renderTimeline({
      run_id: 'run-1',
      state: 'pending',
      attempts: 0,
      retry_after: null,
      error: null,
      created_at: '2026-08-28T00:00:00.000Z',
      completed_at: null,
    })

    expect(await screen.findByRole('heading', { name: 'Today’s episodes are still processing.' })).toBeVisible()
    expect(screen.getByRole('link', { name: /Continue review/i })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(screen.queryByRole('button', { name: 'Reconcile this day' })).not.toBeInTheDocument()
    expect(reconcile).not.toHaveBeenCalled()
  })

  it('finishes episode review on Timeline without jumping to Memory Ledger', async () => {
    const finalize = vi.spyOn(timelineApi, 'finalizeEpisodes').mockResolvedValue({ data: { ...reviewableDay.review!, state: 'memory_queued' } } as never)
    renderTimeline(null, reviewableDay)

    if (!(await screen.findByText('Individual episodes & structure tools')).closest('details')?.open) fireEvent.click(screen.getByText('Individual episodes & structure tools'))
    expect(await screen.findByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    expect(screen.getByText('memory-eligible').parentElement).toHaveTextContent('1 memory-eligible')
    expect(screen.queryByRole('link', { name: /Memory queued|Extracting potential memory/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Finish structural review' }))
    await waitFor(() => expect(finalize).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta', TEST_SNAPSHOT))
  })

  it('records a reason for a failed range, refreshes review state, then finalizes', async () => {
    const failedRangeDay: TimelineDay = {
      ...reviewableDay,
      reconciliation: {
        ranges: [{
          dirty_range_id: 'range-one',
          started_at: '2026-08-28T09:10:00.000Z',
          ended_at: '2026-08-28T09:20:00.000Z',
          state: 'failed',
          trigger_reasons: ['transcript_revision'],
          attempts: 5,
          error: 'exhausted interpretation retries',
          resolution_history: [],
        }],
      },
    }
    const refreshedDay: TimelineDay = {
      ...reviewableDay,
      reconciliation: { ranges: [] },
    }
    const dismiss = vi.spyOn(timelineApi, 'dismissFailedRange').mockResolvedValue({ data: {
      ...failedRangeDay.reconciliation.ranges[0],
      state: 'dismissed',
      resolution_history: [{
        resolution_id: 'resolution-one',
        action: 'dismissed',
        actor_user_id: 'user-1',
        reason: 'Reviewed the transcript gap',
        prior_state: 'failed',
        created_at: '2026-08-28T10:00:00.000Z',
      }],
    } } as never)
    const finalize = vi.spyOn(timelineApi, 'finalizeEpisodes').mockResolvedValue({ data: { ...reviewableDay.review!, state: 'memory_queued' } } as never)
    renderTimeline(null, failedRangeDay, refreshedDay, () => dismiss.mock.calls.length > 0)

    expect(await screen.findByRole('heading', { name: 'Resolve failed reconciliation' })).toBeVisible()
    const queueCallsBeforeDismissal = vi.mocked(timelineApi.getReviewQueue).mock.calls.length
    const dismissButton = screen.getByRole('button', { name: /Dismiss failed range/ })
    expect(dismissButton).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Reason for failed range/), {
      target: { value: '  Reviewed the transcript gap  ' },
    })
    fireEvent.click(dismissButton)

    await waitFor(() => expect(dismiss).toHaveBeenCalledWith('range-one', 'Reviewed the transcript gap'))
    if (!(await screen.findByText('Individual episodes & structure tools')).closest('details')?.open) fireEvent.click(screen.getByText('Individual episodes & structure tools'))
    expect(await screen.findByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    await waitFor(() => expect(timelineApi.getReviewQueue).toHaveBeenCalledTimes(queueCallsBeforeDismissal + 1))

    fireEvent.click(screen.getByRole('button', { name: 'Finish structural review' }))
    await waitFor(() => expect(finalize).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta', TEST_SNAPSHOT))
  })

  it('offers failed-range dismissal when every episode was rejected and no review exists', async () => {
    const failedEmptyDay: TimelineDay = {
      ...emptyDay,
      current_snapshot_id: TEST_SNAPSHOT,
      snapshot_state: 'ready',
      review: null,
      reconciliation: {
        ranges: [{
          dirty_range_id: 'all-rejected-range',
          started_at: '2026-08-28T11:00:00.000Z',
          ended_at: '2026-08-28T11:30:00.000Z',
          state: 'failed',
          trigger_reasons: ['interpretation_rejected'],
          attempts: 5,
          error: 'all episode hypotheses were rejected',
          resolution_history: [],
        }],
      },
      episodes: [],
    }
    const refreshedEmptyDay: TimelineDay = {
      ...failedEmptyDay,
      reconciliation: { ranges: [] },
    }
    const dismiss = vi.spyOn(timelineApi, 'dismissFailedRange').mockResolvedValue({ data: {
      ...failedEmptyDay.reconciliation.ranges[0],
      state: 'dismissed',
      resolution_history: [{
        resolution_id: 'all-rejected-resolution',
        action: 'dismissed',
        actor_user_id: 'user-1',
        reason: 'Reviewed the rejected hypotheses',
        prior_state: 'failed',
        created_at: '2026-08-28T12:00:00.000Z',
      }],
    } } as never)
    renderTimeline(
      null,
      failedEmptyDay,
      refreshedEmptyDay,
      () => dismiss.mock.calls.length > 0,
    )

    expect(await screen.findByRole('heading', { name: 'Resolve failed reconciliation' })).toBeVisible()
    expect(screen.getByText('0 episodes')).toBeVisible()
    const queueCallsBeforeDismissal = vi.mocked(timelineApi.getReviewQueue).mock.calls.length
    fireEvent.change(screen.getByLabelText(/Reason for failed range/), {
      target: { value: 'Reviewed the rejected hypotheses' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Dismiss failed range/ }))

    await waitFor(() => expect(dismiss).toHaveBeenCalledWith(
      'all-rejected-range',
      'Reviewed the rejected hypotheses',
    ))
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Resolve failed reconciliation' })).not.toBeInTheDocument())
    expect(screen.getByRole('heading', { name: 'Today has no processed episodes yet.' })).toBeVisible()
    await waitFor(() => expect(timelineApi.getReviewQueue).toHaveBeenCalledTimes(queueCallsBeforeDismissal + 1))
  })

  it('does not ask for structure confirmation of API-classified reference evidence', async () => {
    const day: TimelineDay = {
      ...reviewableDay,
      episodes: [{ ...reviewableDay.episodes[0], status: 'provisional',
        requires_activity_review: false, memory_eligible: false }],
    }
    renderTimeline(null, day)
    if (!(await screen.findByText('Individual episodes & structure tools')).closest('details')?.open) fireEvent.click(screen.getByText('Individual episodes & structure tools'))
    expect(await screen.findByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /Confirm session/ })).not.toBeInTheDocument()
  })

  it('confirms an exact provisional revision, follows its successor snapshot, then finalizes', async () => {
    const provisionalDay: TimelineDay = {
      ...reviewableDay,
      episodes: [{
        ...reviewableDay.episodes[0],
        status: 'provisional',
        revision: 3,
        confirmed_fields: [],
      }],
    }
    const successorSnapshot = 'bcdef01'.padEnd(64, '2')
    const successorDay: TimelineDay = {
      ...reviewableDay,
      current_snapshot_id: successorSnapshot,
      episodes: [{
        ...reviewableDay.episodes[0],
        episode_id: 'episode-successor',
        revision: 4,
        status: 'provisional',
        confirmed_fields: ['started_at', 'ended_at', 'evidence_refs'],
      }],
    }
    const confirm = vi.spyOn(timelineApi, 'confirmSessionStructures').mockResolvedValue({ data: { episodes: successorDay.episodes } } as never)
    const finalize = vi.spyOn(timelineApi, 'finalizeEpisodes').mockResolvedValue({ data: { ...reviewableDay.review!, state: 'memory_queued' } } as never)
    renderTimeline(null, provisionalDay, successorDay, () => confirm.mock.calls.length > 0)

    if (!(await screen.findByText('Individual episodes & structure tools')).closest('details')?.open) fireEvent.click(screen.getByText('Individual episodes & structure tools'))
    expect(await screen.findByRole('heading', { name: 'Review your sessions' })).toBeVisible()
    const queueCallsBeforeConfirmation = vi.mocked(timelineApi.getReviewQueue).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Confirm session (1)' }))

    await waitFor(() => expect(confirm).toHaveBeenCalledWith(
      TEST_DAY,
      'Asia/Calcutta',
      TEST_SNAPSHOT,
      [{ episode_key: 'episode-key-1', revision: 3 }],
    ))
    if (!(await screen.findByText('Individual episodes & structure tools')).closest('details')?.open) fireEvent.click(screen.getByText('Individual episodes & structure tools'))
    expect(await screen.findByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    await waitFor(() => expect(timelineApi.getReviewQueue).toHaveBeenCalledTimes(queueCallsBeforeConfirmation + 1))

    fireEvent.click(screen.getByRole('button', { name: 'Finish structural review' }))
    await waitFor(() => expect(finalize).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta', successorSnapshot))
  })
})
