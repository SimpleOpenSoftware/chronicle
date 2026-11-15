// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider, useAuth } from './AuthContext'
import ProtectedRoute from '../components/auth/ProtectedRoute'
import { authApi, browserSession } from '../services/api'

vi.mock('../services/api', () => ({
  authApi: { getMe: vi.fn(), login: vi.fn() },
  browserSession: { refresh: vi.fn(), finishLogout: vi.fn(), logout: vi.fn(), exchange: vi.fn() },
}))
const token = `header.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 86400 }))}.signature`
const user = { id: 'user', email: 'user@example.com', is_superuser: false }

function Dashboard() {
  const { user } = useAuth()
  return <div>Signed in as {user?.email}</div>
}
function mount() {
  return render(<MemoryRouter><AuthProvider><ProtectedRoute><Dashboard /></ProtectedRoute></AuthProvider></MemoryRouter>)
}
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); localStorage.setItem('root_token', token) })
afterEach(cleanup)

it.each([503, undefined])('keeps login during startup failure %s and recovers without credentials', async status => {
  vi.mocked(authApi.getMe).mockRejectedValueOnce({ response: status ? { status } : undefined })
  mount()
  await screen.findByText('Connecting to Chronicle')
  expect(localStorage.getItem('root_token')).toBe(token)
  vi.mocked(authApi.getMe).mockResolvedValue({ data: user } as any)
  fireEvent.click(screen.getByText('Try again now'))
  await screen.findByText('Signed in as user@example.com')
  expect(authApi.login).not.toHaveBeenCalled()
})

it('recovers when connectivity returns', async () => {
  vi.mocked(authApi.getMe).mockRejectedValueOnce(new Error('offline'))
  mount()
  await screen.findByText('Connecting to Chronicle')
  vi.mocked(authApi.getMe).mockResolvedValue({ data: user } as any)
  fireEvent(window, new Event('online'))
  await screen.findByText('Signed in as user@example.com')
})

it('restores from the HttpOnly session when local access-token storage is empty', async () => {
  localStorage.clear()
  vi.mocked(browserSession.refresh).mockImplementation(async () => {
    localStorage.setItem('root_token', token)
    return token
  })
  vi.mocked(authApi.getMe).mockResolvedValue({ data: user } as any)
  mount()
  await screen.findByText('Signed in as user@example.com')
  expect(browserSession.refresh).toHaveBeenCalledTimes(1)
})

it('does not restore a session after an offline logout and page reload', async () => {
  localStorage.removeItem('root_token')
  localStorage.setItem('root_sessionLogout', 'pending')
  vi.mocked(browserSession.finishLogout).mockResolvedValue(undefined)
  mount()
  await waitFor(() => expect(browserSession.finishLogout).toHaveBeenCalled())
  expect(browserSession.refresh).not.toHaveBeenCalled()
  expect(authApi.getMe).not.toHaveBeenCalled()
})

it('ignores an account response that arrives after another tab signs out', async () => {
  let resolve!: (value: any) => void
  vi.mocked(authApi.getMe).mockReturnValue(new Promise(done => { resolve = done }))
  mount()
  await act(async () => {
    localStorage.removeItem('root_token')
    localStorage.setItem('root_sessionLogout', 'done')
    window.dispatchEvent(new StorageEvent('storage', { key: 'root_token' }))
    resolve({ data: user })
  })
  expect(screen.queryByText('Signed in as user@example.com')).toBeNull()
})

it('signs in from a #token handoff and strips it from the address bar', async () => {
  localStorage.clear()
  window.history.replaceState(null, '', '/#token=host-token')
  vi.mocked(browserSession.exchange).mockImplementation(async () => {
    localStorage.setItem('root_token', token)
    return token
  })
  vi.mocked(authApi.getMe).mockResolvedValue({ data: user } as any)
  mount()
  await screen.findByText('Signed in as user@example.com')
  expect(browserSession.exchange).toHaveBeenCalledWith('host-token')
  expect(window.location.hash).toBe('')
  expect(browserSession.refresh).not.toHaveBeenCalled()
})
