// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import SystemEvents from './SystemEvents'
const state = vi.hoisted(() => ({ summary: undefined as any, filter: vi.fn() }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }))
vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries: vi.fn() }) }))
vi.mock('../services/api', () => ({ systemEventsApi: {} }))
vi.mock('../hooks/useSystemEvents', () => ({
  useSystemEvents: (filter: unknown) => { state.filter(filter); return { data: { events: [] }, isLoading: false, refetch: vi.fn() } },
  useSystemEventsSummary: () => ({ data: state.summary }),
}))
afterEach(() => { cleanup(); state.summary = undefined; state.filter.mockClear() })
it('does not turn a loading summary into zero incidents or a success fallback banner', () => {
  render(<SystemEvents />)
  expect(screen.getAllByText('—')).toHaveLength(6)
  expect(screen.queryByRole('region', { name: 'Memory fallback events' })).not.toBeInTheDocument()
})
it('keeps actionable fallback evidence and inspection while hiding technical details until requested', () => {
  state.summary = { total: 3, unacked: 3, by_severity: { warning: 3 }, by_source: {}, memory_fallbacks: { occurrences: 3, affected_conversations: 1, by_reason: { timeout: 3 }, agent_paths: [] } }
  render(<SystemEvents />)
  expect(screen.getByText('timeout ×3')).not.toBeVisible()
  fireEvent.click(screen.getByText('Fallback details'))
  expect(screen.getByText('timeout ×3')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect memory events' }))
  expect(state.filter).toHaveBeenLastCalledWith(expect.objectContaining({ category: 'memory' }))
  expect(state.filter.mock.lastCall?.[0]).not.toHaveProperty('acked')
})
