// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DayReviewProjection, TimelineConsolidationProposal, TimelineEpisode, timelineApi } from '../../services/api'
import DayReviewBoard from './DayReviewBoard'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('DayReviewBoard', () => {
  it('renders repeated ribbon intervals without duplicate React keys', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const interval = {
      lane: 'foreground' as const,
      episode_id: 'episode-1',
      started_at: '2026-02-19T10:00:00.000Z',
      ended_at: '2026-02-19T10:10:00.000Z',
    }
    const projection = {
      version: 'test',
      day_started_at: '2026-02-19T00:00:00.000Z',
      day_ended_at: '2026-02-20T00:00:00.000Z',
      episode_count: 1,
      group_count: 1,
      needs_attention_count: 0,
      confirmed_count: 0,
      groups: [{
        group_id: 'group-1',
        started_at: interval.started_at,
        ended_at: interval.ended_at,
        title: 'Repeated evidence interval',
        summary: '',
        semantic: false,
        lane: 'foreground',
        episode_ids: ['episode-1'],
        episode_count: 1,
        conversational_count: 0,
        confirmed_count: 0,
        duration_seconds: 600,
        span_seconds: 600,
        gap_seconds: 0,
        intervals: [interval, { ...interval }],
        entities: [],
        salience: 'routine',
        attention_reasons: [],
        needs_attention: false,
      }],
    } as DayReviewProjection

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DayReviewBoard
          day="2026-02-19"
          timezone="Asia/Kolkata"
          projection={projection}
          episodes={[]}
          snapshot={{ snapshot_state: 'ready', current_snapshot_id: 'a'.repeat(64), reviewed_snapshot_id: null, applied_snapshot_id: null }}
          initialProposal={null}
          labeling={false}
          onToggleEditing={vi.fn()}
          renderEpisode={() => null}
          onSelectGroup={vi.fn()}
        />
      </QueryClientProvider>,
    )

    expect(consoleError.mock.calls.flat().join(' ')).not.toContain(
      'Encountered two children with the same key',
    )
    consoleError.mockRestore()
  })

  it.each([false, true])('queues individual confirmations and handles save failure=%s', async (fail) => {
    let finish!: (value: never) => void
    let reject!: (error: Error) => void
    const pending = new Promise<never>((resolve, fail) => { finish = resolve; reject = fail })
    const resolve = vi.spyOn(timelineApi, 'resolveConsolidation').mockResolvedValue({ data: { groups: [] } } as never).mockReturnValueOnce(pending)
    const makeEpisode = (episodeId: string, title: string, startMinute: number): TimelineEpisode => ({
      episode_id: episodeId,
      episode_key: `${episodeId}-key`,
      revision: 1,
      started_at: `2026-02-19T10:${String(startMinute).padStart(2, '0')}:00.000Z`,
      ended_at: `2026-02-19T10:${String(startMinute + 5).padStart(2, '0')}:00.000Z`,
      kind: 'media',
      title,
      summary: `${title} summary`,
      status: 'provisional',
      confirmed_at: null,
      confirmed_fields: [],
      requires_activity_review: true, memory_eligible: true, memory_policy: 'auto',
      salience: 'routine',
      confidence: 0.9,
      activity_mode: 'foreground',
      entities: [],
      attributes: {},
      assertions: [],
      evidence: [],
      related_episode_ids: [],
      related_conversation_ids: [],
      audio_ranges: [],
      parent_episode_id: null,
      has_thumbnail: false,
    })
    const episodes = [
      makeEpisode('episode-1', 'First suggested episode', 0),
      makeEpisode('episode-2', 'Second suggested episode', 5),
      makeEpisode('episode-3', 'Unrelated episode', 10),
    ]
    const projection = {
      version: 'test',
      day_started_at: '2026-02-19T00:00:00.000Z',
      day_ended_at: '2026-02-20T00:00:00.000Z',
      episode_count: 3,
      group_count: 1,
      needs_attention_count: 0,
      confirmed_count: 0,
      groups: [{
        group_id: 'group-1',
        started_at: episodes[0].started_at,
        ended_at: episodes[2].ended_at,
        title: 'Display session containing all episodes',
        summary: '',
        semantic: false,
        lane: 'conversation',
        episode_ids: episodes.map(episode => episode.episode_id),
        episode_count: 3,
        conversational_count: 3,
        confirmed_count: 0,
        duration_seconds: 900,
        span_seconds: 900,
        gap_seconds: 0,
        intervals: episodes.map(episode => ({ lane: 'conversation' as const, episode_id: episode.episode_id, started_at: episode.started_at, ended_at: episode.ended_at })),
        entities: [],
        salience: 'routine',
        attention_reasons: [],
        needs_attention: false,
      }],
    } satisfies DayReviewProjection
    const proposal = {
      state: 'ready',
      snapshot_id: 'a'.repeat(64),
      model: 'qwen',
      suggestions: [{
        suggestion_id: 'suggestion-1',
        episode_ids: ['episode-1', 'episode-2'],
        title: 'Suggested pair',
        reason: 'These two episodes belong together.',
        confidence: 0.9,
      }, { suggestion_id: 'suggestion-2', episode_ids: ['episode-3'], title: 'Still awaiting review', reason: 'Another proposal', confidence: 0.7 }],
    } satisfies TimelineConsolidationProposal

    vi.spyOn(timelineApi, 'getDay')
      .mockResolvedValueOnce({ data: { current_snapshot_id: 'b'.repeat(64), consolidation: { ...proposal, snapshot_id: 'b'.repeat(64), suggestions: [proposal.suggestions[1]] } } } as never)
      .mockResolvedValueOnce({ data: { current_snapshot_id: 'c'.repeat(64), consolidation: null } } as never)

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DayReviewBoard
          day="2026-02-19"
          timezone="Asia/Kolkata"
          projection={projection}
          episodes={episodes}
          snapshot={{ snapshot_state: 'ready', current_snapshot_id: 'a'.repeat(64), reviewed_snapshot_id: null, applied_snapshot_id: null }}
          initialProposal={proposal}
          labeling
          onToggleEditing={vi.fn()}
          renderEpisode={episode => <article>{episode.title}</article>}
          onSelectGroup={vi.fn()}
        />
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Preview Suggested pair' }))

    const firstCard = document.querySelector('[data-grouping-episode-id="episode-1"]')
    const secondCard = document.querySelector('[data-grouping-episode-id="episode-2"]')
    const unrelatedCard = document.querySelector('[data-grouping-episode-id="episode-3"]')
    expect(firstCard).toHaveAttribute('data-suggestion-preview', 'included')
    expect(secondCard).toHaveAttribute('data-suggestion-preview', 'included')
    expect(firstCard).toHaveClass('ring-2', 'ring-[var(--tape-focus)]')
    expect(secondCard).toHaveClass('ring-2', 'ring-[var(--tape-focus)]')
    expect(unrelatedCard).toHaveAttribute('data-suggestion-preview', 'excluded')
    expect(unrelatedCard).toHaveClass('opacity-35')
    expect(document.querySelector('[data-session-group-id="group-1"]')).not.toHaveClass('ring-1')

    const tapeIntervals = Array.from(document.querySelectorAll<HTMLElement>('[data-tape-key]'))
    const firstTapeIntervals = tapeIntervals.filter(item => item.getAttribute('aria-label')?.includes('first suggested episode'))
    const secondTapeIntervals = tapeIntervals.filter(item => item.getAttribute('aria-label')?.includes('second suggested episode'))
    const unrelatedTapeIntervals = tapeIntervals.filter(item => item.getAttribute('aria-label')?.includes('unrelated episode'))
    expect(firstTapeIntervals, tapeIntervals.map(item => item.getAttribute('aria-label')).join('\n')).not.toHaveLength(0)
    expect(secondTapeIntervals.length).toBeGreaterThan(0)
    expect(firstTapeIntervals.every(item => item.dataset.suggestionPreview === 'included' && item.classList.contains('ring-2'))).toBe(true)
    expect(secondTapeIntervals.every(item => item.dataset.suggestionPreview === 'included' && item.classList.contains('ring-2'))).toBe(true)
    expect(unrelatedTapeIntervals.every(item => item.dataset.suggestionPreview === 'excluded' && item.classList.contains('opacity-20'))).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Hide Suggested pair' }))
    expect(firstCard).not.toHaveAttribute('data-suggestion-preview')
    expect(secondCard).not.toHaveAttribute('data-suggestion-preview')
    expect(unrelatedCard).not.toHaveAttribute('data-suggestion-preview')
    expect(firstCard).not.toHaveClass('ring-2')
    expect(unrelatedCard).not.toHaveClass('opacity-35')
    expect(tapeIntervals.every(item => !item.dataset.suggestionPreview)).toBe(true)
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Accept only this grouping: Suggested pair' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm grouping: Suggested pair' }))
    expect(resolve).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(4000))
    expect(screen.getByRole('button', { name: 'Accept only this grouping: Suggested pair' })).toHaveAttribute('data-confirm-state', 'idle')
    vi.useRealTimers()
    let now = 1000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    fireEvent.click(screen.getByRole('button', { name: 'Accept only this grouping: Suggested pair' }))
    expect(resolve).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Confirm grouping: Suggested pair' }), { key: 'Escape' })
    expect(screen.queryByRole('button', { name: 'Confirm grouping: Suggested pair' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Accept only this grouping: Suggested pair' }))
    now += 500
    fireEvent.click(screen.getByRole('button', { name: 'Confirm grouping: Suggested pair' }))
    await waitFor(() => expect(resolve).toHaveBeenCalledWith('2026-02-19', 'Asia/Kolkata', 'a'.repeat(64), ['suggestion-1'], false))
    const next = screen.getByRole('button', { name: 'Accept only this grouping: Still awaiting review' })
    expect(next).toBeEnabled()
    fireEvent.click(next)
    now += 500
    fireEvent.click(next)
    expect(next).toHaveAttribute('data-confirm-state', 'queued')
    expect(next).toBeDisabled()
    expect(resolve).toHaveBeenCalledTimes(1)
    if (fail) {
      await act(async () => reject(new Error('Snapshot changed')))
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Queue stopped'))
      expect(resolve).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('button', { name: 'Accept only this grouping: Still awaiting review' })).toBeEnabled()
    } else {
      await act(async () => finish({ data: { groups: [] } } as never))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Grouping accepted: Suggested pair' })).toHaveAttribute('data-confirm-state', 'accepted'))
      await waitFor(() => expect(resolve).toHaveBeenNthCalledWith(2, '2026-02-19', 'Asia/Kolkata', 'b'.repeat(64), ['suggestion-2'], false))
      await waitFor(() => expect(screen.queryByText('Still awaiting review', { exact: true })).not.toBeInTheDocument())
      expect(resolve).toHaveBeenCalledTimes(2)
    }
  })
})
