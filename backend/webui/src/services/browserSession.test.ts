// @vitest-environment jsdom
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createBrowserSession, savedToken, signedOut, logoutPending, takeHandoffToken } from './browserSession'

const success = (config: InternalAxiosRequestConfig, data: unknown = {}) => ({ config, data, status: 200, statusText: 'OK', headers: {} })
const reject = (config: InternalAxiosRequestConfig, status: number) => Promise.reject(new AxiosError('Request failed', '', config, null, { ...success(config), status }))

beforeEach(() => { localStorage.clear() })

function setup(handler: (config: InternalAxiosRequestConfig) => Promise<any>) {
  axios.defaults.adapter = handler
  const api = axios.create()
  api.interceptors.request.use(config => {
    const token = savedToken()
    if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  })
  return { api, session: createBrowserSession(api, '') }
}

describe('browser session transport', () => {
  it('renews once for concurrent expired requests and replays each with the new token', async () => {
    localStorage.setItem('root_token', 'expired')
    const refresh = vi.fn()
    const { api } = setup(async config => {
      if (config.url === '/auth/session/refresh') {
        refresh()
        expect(config.withCredentials).toBe(true)
        expect(config.headers['X-Chronicle-Session']).toBe('1')
        await new Promise(resolve => setTimeout(resolve, 10))
        return success(config, { access_token: 'renewed' })
      }
      return config.headers.Authorization === 'Bearer renewed' ? success(config) : reject(config, 401)
    })
    await Promise.all([api.get('/users/me'), api.get('/api/conversations')])
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(savedToken()).toBe('renewed')
  })

  it.each([503, 0])('preserves credentials if renewal fails with %s', async status => {
    localStorage.setItem('root_token', 'expired')
    const { api } = setup(async config => {
      if (config.url === '/auth/session/refresh') {
        if (!status) throw new AxiosError('Network error', 'ERR_NETWORK', config)
        return reject(config, status)
      }
      return reject(config, 401)
    })
    await expect(api.get('/users/me')).rejects.toThrow()
    expect(savedToken()).toBe('expired')
    expect(signedOut()).toBe(false)
  })

  it('clears credentials only after the refresh endpoint confirms expiry', async () => {
    localStorage.setItem('root_token', 'expired')
    const { api } = setup(config => reject(config, 401))
    await expect(api.get('/users/me')).rejects.toThrow()
    expect(savedToken()).toBeNull()
    expect(signedOut()).toBe(true)
  })

  it('does not log out or loop when a specific endpoint rejects a renewed token', async () => {
    localStorage.setItem('root_token', 'expired')
    const handler = vi.fn(async config => config.url === '/auth/session/refresh' ? success(config, { access_token: 'renewed' }) : reject(config, 401))
    const { api } = setup(handler)
    await expect(api.get('/api/restricted')).rejects.toThrow()
    expect(handler).toHaveBeenCalledTimes(3)
    expect(savedToken()).toBe('renewed')
  })

  it('cannot restore login from a renewal that completes after logout', async () => {
    localStorage.setItem('root_token', 'existing')
    let release!: () => void
    const { session } = setup(async config => {
      if (config.url === '/auth/session/refresh') {
        await new Promise<void>(resolve => { release = resolve })
        return success(config, { access_token: 'late' })
      }
      return success(config)
    })
    const pending = session.refresh()
    const rejected = expect(pending).rejects.toThrow('Session changed')
    await vi.waitFor(() => expect(release).toBeDefined())
    await session.logout()
    release()
    await rejected
    expect(savedToken()).toBeNull()
    expect(signedOut()).toBe(true)
  })

  it('persists offline logout and revokes it before another login', async () => {
    localStorage.setItem('root_token', 'existing')
    let offline = true
    const calls: string[] = []
    const { session } = setup(async config => {
      calls.push(config.url!)
      if (offline) throw new AxiosError('Offline', 'ERR_NETWORK', config)
      return success(config, { access_token: 'new-login' })
    })
    await expect(session.logout()).rejects.toThrow()
    expect(logoutPending()).toBe(true)
    expect(savedToken()).toBeNull()
    await expect(session.refresh()).rejects.toThrow('Signed out')
    offline = false
    await session.login('user@example.com', 'password')
    expect(calls).toEqual(['/auth/session/logout', '/auth/session/logout', '/auth/session/login'])
    expect(savedToken()).toBe('new-login')
    expect(signedOut()).toBe(false)
  })
})

describe('sign-in handoff', () => {
  it('exchanges a host token for a session and clears an earlier sign-out', async () => {
    localStorage.setItem('root_sessionLogout', 'done')
    const { session } = setup(async config => {
      expect(config.url).toBe('/auth/session/exchange')
      expect(config.withCredentials).toBe(true)
      expect(config.headers['X-Chronicle-Session']).toBe('1')
      expect(config.headers.Authorization).toBe('Bearer host-token')
      return success(config, { access_token: 'chronicle-token' })
    })
    await expect(session.exchange('host-token')).resolves.toBe('chronicle-token')
    expect(savedToken()).toBe('chronicle-token')
    expect(signedOut()).toBe(false)
  })

  it('keeps the existing state when the host token is rejected', async () => {
    localStorage.setItem('root_token', 'existing')
    const { session } = setup(async config => reject(config, 401))
    await expect(session.exchange('bad')).rejects.toBeTruthy()
    expect(savedToken()).toBe('existing')
  })

  it('takes the token from the fragment and removes it from the address bar', () => {
    window.history.replaceState(null, '', '/conversations?x=1#token=abc&tab=2')
    expect(takeHandoffToken()).toBe('abc')
    expect(window.location.pathname + window.location.search + window.location.hash).toBe('/conversations?x=1#tab=2')
    expect(takeHandoffToken()).toBeNull()
  })
})
