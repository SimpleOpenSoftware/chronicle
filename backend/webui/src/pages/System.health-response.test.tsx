// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import System from './System'
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }))
vi.mock('../contexts/ThemeContext', () => ({ useTheme: () => ({ isDark: false }) }))
vi.mock('../components/ExternalServices', () => ({ default: () => null }))
vi.mock('../components/RemoteControl', () => ({ default: () => null }))
vi.mock('../services/api', () => ({ systemApi: {
  getHealth: vi.fn().mockResolvedValue({ data: '<!doctype html><html>Vite app</html>' }),
  getReadiness: vi.fn().mockResolvedValue({ data: null }),
  getMetrics: vi.fn().mockResolvedValue({ data: null }),
  getConfigDiagnostics: vi.fn().mockResolvedValue({ data: null }),
  getActiveClients: vi.fn().mockResolvedValue({ data: { active_clients: {}, total_count: 0 } }),
  getVersion: vi.fn().mockResolvedValue({ data: null }),
} }))
afterEach(cleanup)
it('renders unavailable health instead of crashing when the actual query receives gateway HTML with HTTP 200', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><System /></MemoryRouter></QueryClientProvider>)
  expect(await screen.findByText('System health is unavailable. Refresh to retry.')).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Active Clients (0)' })).toBeVisible()
  expect(screen.queryByText('HEALTHY')).not.toBeInTheDocument()
})
