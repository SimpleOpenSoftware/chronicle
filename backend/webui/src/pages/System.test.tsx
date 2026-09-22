// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import System from './System'
const state = vi.hoisted(() => ({ count: null as number | null, health: { status: 'degraded', services: { speaker_recognition: { healthy: false, message: 'Connection Timeout (5s)' } } } as unknown }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }))
vi.mock('../contexts/ThemeContext', () => ({ useTheme: () => ({ isDark: false }) }))
vi.mock('../hooks/useSystem', () => ({
  useSystemData: () => ({ data: { healthData: state.health, activeClientCount: state.count }, isLoading: false, refetch: vi.fn() }),
  useRestartWorkers: () => ({ mutate: vi.fn() }), useRestartBackend: () => ({ mutate: vi.fn() }), useBackendVersion: () => ({ data: null }),
}))
vi.mock('../components/ExternalServices', () => ({ default: () => <h3>Service controls</h3> }))
vi.mock('../components/RemoteControl', () => ({ default: () => null }))
vi.mock('../services/api', () => ({ systemApi: {} }))
beforeEach(() => { state.count = null; state.health = { status: 'degraded', services: { speaker_recognition: { healthy: false, message: 'Connection Timeout (5s)' } } } })
afterEach(cleanup)
const setup = () => render(<MemoryRouter><System /></MemoryRouter>)
it('puts the failing capability before lifecycle controls, with the API degraded state', () => {
  setup()
  const cause = screen.getByRole('listitem')
  expect(cause).toHaveTextContent('Speaker recognition is unavailable.')
  expect(screen.getByText('Connection Timeout (5s)')).not.toBeVisible()
  fireEvent.click(screen.getByText('Technical details'))
  expect(screen.getByText('Connection Timeout (5s)')).toBeVisible()
  expect(cause.compareDocumentPosition(screen.getByText('Service controls')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByText('DEGRADED')).toHaveClass('text-yellow-600')
})
it('keeps an unavailable client count distinct from a verified zero', () => {
  const { rerender } = setup()
  expect(screen.getByText('Connected-client count unavailable.')).toBeVisible()
  state.count = 0
  rerender(<MemoryRouter><System /></MemoryRouter>)
  expect(screen.getByRole('heading', { name: 'Active Clients (0)' })).toBeVisible()
  expect(screen.getByText('0 clients currently connected.')).toBeVisible()
})
it('states that health is unavailable when its request did not return data', () => {
  state.health = null
  setup()
  expect(screen.getByText('System health is unavailable. Refresh to retry.')).toBeVisible()
  expect(screen.queryByText('HEALTHY')).not.toBeInTheDocument()
})
