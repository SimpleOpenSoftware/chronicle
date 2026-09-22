// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ReactNode } from 'react'
import { useSystemData } from './useSystem'
const api = vi.hoisted(() => ({ getHealth: vi.fn(), getReadiness: vi.fn(), getMetrics: vi.fn(), getConfigDiagnostics: vi.fn(), getActiveClients: vi.fn() }))
vi.mock('../services/api', () => ({ systemApi: api }))
beforeEach(() => { for (const fn of Object.values(api)) fn.mockReset().mockResolvedValue({ data: {} }) })
afterEach(cleanup)
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderHook(() => useSystemData(true), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> })
}
it.each([0, 2])('reads the client envelope total_count (%i) rather than treating it as an array', async count => {
  api.getActiveClients.mockResolvedValue({ data: { active_clients: {}, total_count: count } })
  const { result } = setup()
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.activeClientCount).toBe(count)
})
it('preserves unavailable clients independently of a successful dependency health check', async () => {
  api.getActiveClients.mockRejectedValue(new Error('unavailable'))
  api.getHealth.mockResolvedValue({ data: { status: 'healthy', overall_healthy: true, services: {} } })
  const { result } = setup()
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.activeClientCount).toBeNull()
  expect(result.current.data?.healthData?.status).toBe('healthy')
})

it.each(['<!doctype html><html>Vite app</html>', { services: {} }])('rejects a successful HTTP response that is not the system-health contract (%j)', async payload => {
  api.getHealth.mockResolvedValue({ data: payload })
  api.getActiveClients.mockResolvedValue({ data: { active_clients: {}, total_count: 2 } })
  const { result } = setup()
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.healthData).toBeNull()
  expect(result.current.data?.activeClientCount).toBe(2)
})
