// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../contexts/AuthContext'
import { authApi, memoryApi, timelineApi } from '../services/api'
import MemoryLedger from './MemoryLedger'

beforeEach(() => {
  vi.spyOn(timelineApi, 'getMemorySelections').mockResolvedValue({ data: { proposals: [], outcomes: {} } } as never)
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('Memory Ledger review workspace', () => {
  it('opens a URL-addressable review day without silently changing timezone', async () => {
    localStorage.setItem('root_token', 'test-token')
    vi.spyOn(authApi, 'getMe').mockResolvedValue({ data: {
      id: 'user-1',
      email: 'user@example.com',
      display_name: 'User',
      assistant_name: null,
      timezone: 'Asia/Calcutta',
      is_superuser: true,
    } } as never)
    vi.spyOn(memoryApi, 'getAudit').mockResolvedValue({ data: { entries: [], total: 0 } } as never)
    vi.spyOn(timelineApi, 'getReviewQueue').mockResolvedValue({ data: { items: [] } } as never)
    vi.spyOn(timelineApi, 'getDay').mockResolvedValue({ data: {
      date: '2026-02-19',
      timezone: 'Asia/Calcutta',
      coverage: { unassigned_intervals: [] },
      analysis: null,
      consolidation: null,
      semantic_groups: [],
      review_decision_count: 0,
      review_projection: { version: 'test', day_started_at: '2026-02-18T18:30:00.000Z', day_ended_at: '2026-02-19T18:30:00.000Z', episode_count: 0, group_count: 0, needs_attention_count: 0, confirmed_count: 0, groups: [] },
      review: null,
      reconciliation: { ranges: [] },
      episodes: [],
    } } as never)
    const setTimezone = vi.spyOn(timelineApi, 'setTimezone').mockResolvedValue({ data: { timezone: 'UTC' } } as never)

    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/memory-ledger?view=review&date=2026-02-19']}>
            <MemoryLedger />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('tab', { name: 'Review queue' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('link', { name: 'Open Timeline for Feb 19' })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(setTimezone).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Use browser timezone' }))
    await waitFor(() => expect(setTimezone).toHaveBeenCalledWith(Intl.DateTimeFormat().resolvedOptions().timeZone))
  })
})

describe('Memory Ledger history provenance and failures', () => {
  function renderHistory() {
    localStorage.setItem('root_token', 'test-token')
    vi.spyOn(authApi, 'getMe').mockResolvedValue({ data: { id: 'user-1', email: 'user@example.com', is_superuser: true } } as never)
    return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider><MemoryRouter initialEntries={['/memory-ledger?view=history']}><MemoryLedger /></MemoryRouter></AuthProvider>
    </QueryClientProvider>)
  }

  it('provides all episode sources and the recording independently of diff availability', async () => {
    vi.spyOn(memoryApi, 'getAudit').mockResolvedValue({ data: { entries: [{
      id: 'change-1', note_path: 'People/Ada.md', operation: 'update', source_kind: 'extraction', source_label: 'Day episodes',
      extra: { relevant_episode_keys: ['episode-key-a', 'episode-key-b'] }, conversation_id: 'recording-one', has_diff: false,
    }] } } as never)
    renderHistory()
    fireEvent.click(await screen.findByText('Sources · 1 recording · 2 episodes'))
    expect(screen.getByRole('link', { name: 'Recording' })).toHaveAttribute('href', '/recordings/recording-one')
    expect(screen.getByRole('link', { name: 'Episode 1' })).toHaveAttribute('href', '/timeline/key/episode-key-a')
    expect(screen.getByRole('link', { name: 'Episode 2' })).toHaveAttribute('href', '/timeline/key/episode-key-b')
  })

  it('does not display an empty ledger after the history request fails', async () => {
    vi.spyOn(memoryApi, 'getAudit').mockRejectedValue(new Error('History unavailable'))
    renderHistory()
    expect(await screen.findByText('History unavailable')).toBeVisible()
    expect(screen.queryByText('No vault changes were returned.')).not.toBeInTheDocument()
  })
})
