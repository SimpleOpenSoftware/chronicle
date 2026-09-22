// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { chatApi } from '../services/api'
import { useChatSessions } from './useChat'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 0 } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

it('surfaces a timeout without automatically starting another chat scan, and allows retry', async () => {
  const get = vi.spyOn(chatApi, 'getSessions').mockRejectedValueOnce(new Error('timeout'))
    .mockResolvedValue({ data: [] } as never)
  const { result } = renderHook(() => useChatSessions(), { wrapper })
  await waitFor(() => expect(result.current.isError).toBe(true))
  expect(get).toHaveBeenCalledTimes(1)
  await act(async () => { await result.current.refetch() })
  await waitFor(() => expect(result.current.data).toEqual([]))
  expect(get).toHaveBeenCalledTimes(2)
})

it('aborts an obsolete chat request when its Memory Space changes or the view unmounts', async () => {
  const signals: AbortSignal[] = []
  vi.spyOn(chatApi, 'getSessions').mockImplementation((_limit, _space, signal) => {
    signals.push(signal!)
    return new Promise(() => {})
  })
  const { rerender, unmount } = renderHook(({ space }) => useChatSessions(space), {
    wrapper, initialProps: { space: 'one' },
  })
  await waitFor(() => expect(signals).toHaveLength(1))
  rerender({ space: 'two' })
  await waitFor(() => expect(signals).toHaveLength(2))
  expect(signals[0].aborted).toBe(true)
  expect(signals[1].aborted).toBe(false)
  unmount()
  expect(signals[1].aborted).toBe(true)
})
