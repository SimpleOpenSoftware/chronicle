// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import Network from './Network'
import type { DeviceInputSource } from '../services/api'
vi.mock('../components/ServiceDeployments', () => ({ default: () => <div>Service placement</div> }))
const state = vi.hoisted(() => ({ error: false, sourceError: false, sources: [] as DeviceInputSource[] }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }))
vi.mock('../services/api', () => ({ systemApi: {}, clientsApi: {}, deviceInputApi: {} }))
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }), useMutation: () => ({}),
  useQuery: ({ queryKey }: { queryKey: string[] }) => queryKey[0] === 'device-input-sources'
    ? { data: state.sourceError ? undefined : state.sources, isError: state.sourceError, isLoading: false }
    : { data: state.error ? undefined : { tailscale_available: true, advertising: [], discovered_services: [{ name: 'chronicle-backend', url: 'http://backend:8000', reachable: false, labels: { health: 'healthy' }, host: 'test-node' }], connected_devices: [] }, isError: state.error, isLoading: false, refetch: vi.fn() },
}))
beforeEach(() => { state.error = false; state.sourceError = false; state.sources = [] })
afterEach(cleanup)
it('presents source-reported health separately from a failed backend reachability probe', () => {
  render(<Network />)
  expect(screen.getByText('Node: healthy')).toBeVisible()
  expect(screen.getByText('Backend probe: unreachable')).toBeVisible()
  expect(screen.queryByText('Status Legend')).not.toBeInTheDocument()
})
it('does not claim missing Tailscale or no paired sources when those requests fail', () => {
  state.error = true; state.sourceError = true
  render(<Network />)
  expect(screen.getByText('Capture sources could not be loaded.')).toBeVisible()
  expect(screen.getByText('Network discovery is unavailable. Scan again to retry.')).toBeVisible()
  expect(screen.queryByText('No capture sources paired.')).not.toBeInTheDocument()
  expect(screen.queryByText('Tailscale Not Detected')).not.toBeInTheDocument()
})

it('shows screening backlog without equating an online collector with verified coverage', () => {
  state.sources = [{
    source_id: 'synthetic-source', name: 'Test display', provider: 'screenpipe', platform: 'test',
    status: 'online', last_seen_at: null, capabilities: [], privacy_enabled_from: '2026-09-16T00:00:00Z',
    health: { privacy_screening: { state: 'unavailable', pending_jobs: 120, failed_jobs: 3,
      completed_jobs: 8, frame_read_ms: 1250, inference_ms: 100, cache_hits: 4,
      pending_live_jobs: 2, pending_background_jobs: 118, oldest_live_pending_seconds: 90,
      last_failure: 'Synthetic text that must not be rendered' } },
  }]
  render(<Network />)
  expect(screen.getByText('120 screening jobs waiting (3 retrying).')).toBeVisible()
  expect(screen.getByText('Live captures: 2 waiting · History and rechecks: 118 waiting.')).toBeVisible()
  expect(screen.getByText('Oldest queued live capture: 90 seconds.')).toBeVisible()
  expect(screen.getByText('8 completed since worker restart.')).toBeVisible()
  expect(screen.getByText('Screening or screen coverage is unavailable; unresolved captures stay held.')).toBeVisible()
  expect(screen.getByText('Latest screening timings')).toBeVisible()
  expect(screen.queryByText('Synthetic text that must not be rendered')).not.toBeInTheDocument()
})

it('distinguishes a waiting privacy hold from an unprotected source', () => {
  state.sources = [{
    source_id: 'synthetic-source', name: 'Test display', provider: 'screenpipe', platform: 'test',
    status: 'online', last_seen_at: null, capabilities: [], health: {},
    privacy_enabled_from: null, privacy_waiting_from: '2026-09-16T00:00:00Z',
  }]
  render(<Network />)
  expect(screen.getByText('Waiting for local screening · processing held')).toBeVisible()
  expect(screen.queryByText('Privacy screening not active · source unprotected')).not.toBeInTheDocument()
  expect(screen.queryByText('Privacy screening enabled · unresolved captures held')).not.toBeInTheDocument()
})

it('does not imply a hold before screening setup is requested', () => {
  state.sources = [{
    source_id: 'synthetic-source', name: 'Test display', provider: 'screenpipe', platform: 'test',
    status: 'online', last_seen_at: null, capabilities: [], health: {},
    privacy_enabled_from: null, privacy_waiting_from: null,
  }]
  render(<Network />)
  expect(screen.getByText('Privacy screening not active · source unprotected')).toBeVisible()
})
