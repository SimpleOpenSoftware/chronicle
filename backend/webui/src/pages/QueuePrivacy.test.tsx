// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Queue from './Queue'
import { queueApi } from '../services/api'
const state = vi.hoisted(() => ({ dashboard: {} as any }))
vi.mock('../hooks/useQueue', () => ({ useQueueDashboard: () => state.dashboard }))
vi.mock('../services/api', () => ({ BACKEND_URL: '', queueApi: { getJob: vi.fn() }, conversationsApi: {} }))
const job = { job_id: 'synthetic-job', job_type: 'process_memory_job', status: 'finished', created_at: '2026-01-01T00:00:00Z', meta: {}, data: { description: 'Synthetic detail' } }
const detail = { ...job, description: 'Synthetic detail', result: { text: 'Synthetic private result' } }
let client: QueryClient
function view() { return <QueryClientProvider client={client}><Queue /></QueryClientProvider> }
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  state.dashboard = { data: { jobs: { finished: [job] }, events: [] }, isLoading: false, isFetching: false, error: null }
  vi.mocked(queueApi.getJob).mockResolvedValue({ data: detail } as any)
})
afterEach(() => { cleanup(); client.clear() })
describe('queue privacy refresh', () => {
  it('clears cached jobs and an open detail when a refresh is held', async () => {
    const rendered = render(view())
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    expect(await screen.findByText(/Synthetic private result/)).toBeVisible()
    state.dashboard = { ...state.dashboard, error: { response: { status: 423 } } }
    rendered.rerender(view())
    expect(screen.getByRole('alert')).toHaveTextContent('Queue details are hidden')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('synthetic-job')).not.toBeInTheDocument()
    expect(screen.queryByText(/Synthetic private result/)).not.toBeInTheDocument()
  })
  it('replaces an open detail with current held metadata', async () => {
    const rendered = render(view())
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    await screen.findByText(/Synthetic private result/)
    state.dashboard = { ...state.dashboard, data: { jobs: { finished: [{ ...job, privacy_held: true, description: 'Private or unscreened job details held' }] } } }
    rendered.rerender(view())
    expect(screen.getByRole('dialog')).toHaveTextContent('Private or unscreened job details held')
    expect(screen.queryByText(/Synthetic private result/)).not.toBeInTheDocument()
  })
  it('discards a detail request that finishes after the dashboard was held', async () => {
    let resolve!: (value: any) => void
    vi.mocked(queueApi.getJob).mockReturnValue(new Promise(done => { resolve = done }))
    const rendered = render(view())
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    state.dashboard = { ...state.dashboard, error: { response: { status: 423 } } }
    rendered.rerender(view())
    resolve({ data: detail })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByText(/Synthetic private result/)).not.toBeInTheDocument()
  })
})

it('updates an open event after its payload becomes held', async () => {
  const event = { timestamp: 100, event: 'synthetic.event', user_id: 'synthetic-user', metadata: {}, plugins_subscribed: ['synthetic'], plugins_executed: [{ plugin_id: 'synthetic', success: true, message: 'Synthetic plugin result' }] }
  state.dashboard = { ...state.dashboard, data: { ...state.dashboard.data, events: [event] } }
  const rendered = render(view())
  fireEvent.click(screen.getByRole('button', { name: 'View event details' }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Synthetic plugin result')
  state.dashboard = { ...state.dashboard, data: { ...state.dashboard.data, events: [{ ...event, privacy_held: true, plugins_executed: [] }] } }
  rendered.rerender(view())
  expect(screen.getByRole('dialog')).toHaveTextContent('Private or unscreened event details held')
  expect(screen.queryByText('Synthetic plugin result')).not.toBeInTheDocument()
})

it('shows a held session even when its private job metadata is absent', () => {
  state.dashboard = { ...state.dashboard, data: { jobs: {}, events: [], streaming_status: {
    active_sessions: [{ session_id: 'synthetic-session', client_id: 'Synthetic computer', privacy_held: true, privacy_reason: 'Private or unscreened session details held', identified_speakers: 'Synthetic private speaker' }],
    completed_sessions: [], rq_queues: {}, stream_health: {
      'audio:stream:synthetic-session': { session_id: 'synthetic-session', client_id: 'Synthetic computer', stream_length: 3, total_pending: 0, consumer_groups: [] },
    },
  } } }
  render(view())
  expect(screen.getByText('Synthetic computer')).toBeVisible()
  expect(screen.getByText('Private or unscreened session details held')).toBeVisible()
  expect(screen.queryByText('Synthetic private speaker')).not.toBeInTheDocument()
})
