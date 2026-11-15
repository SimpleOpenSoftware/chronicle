import axios, { type AxiosInstance } from 'axios'
import { getStorageKey } from '../utils/storage'

export const SESSION_CHANGED = 'chronicle-session-changed'
const tokenKey = getStorageKey('token')
const logoutKey = getStorageKey('sessionLogout')
export const savedToken = () => localStorage.getItem(tokenKey)
export const signedOut = () => localStorage.getItem(logoutKey) !== null
export const logoutPending = () => localStorage.getItem(logoutKey) === 'pending'

export function createBrowserSession(api: AxiosInstance, baseURL: string) {
  const transport = axios.create({
    baseURL, timeout: 15000, withCredentials: true,
    headers: { 'X-Chronicle-Session': '1' },
  })
  let generation = 0
  let renewing: Promise<string> | null = null
  let revoking: Promise<void> | null = null
  const notify = () => window.dispatchEvent(new Event(SESSION_CHANGED))
  const storeToken = (token: string) => {
    localStorage.setItem(tokenKey, token)
    notify()
  }
  const expire = () => {
    generation++
    localStorage.removeItem(tokenKey)
    localStorage.setItem(logoutKey, 'done')
    notify()
  }
  const refresh = (): Promise<string> => {
    if (signedOut()) return Promise.reject(new Error('Signed out'))
    if (!renewing) {
      const started = generation
      const originalToken = savedToken()
      renewing = transport.post<{ access_token: string }>('/auth/session/refresh')
        .then(({ data }) => {
          if (started !== generation || signedOut()) throw new Error('Session changed')
          // Another tab may have logged in while this request was in flight.
          if (savedToken() !== originalToken && savedToken()) return savedToken()!
          storeToken(data.access_token)
          return data.access_token
        }).catch(error => {
          if (started === generation && savedToken() === originalToken && error.response?.status === 401) expire()
          throw error
        }).finally(() => { renewing = null })
    }
    return renewing
  }
  const finishLogout = async () => {
    if (!logoutPending()) return
    if (!revoking) {
      revoking = transport.post('/auth/session/logout').then(() => {
        if (logoutPending()) localStorage.setItem(logoutKey, 'done')
      }).finally(() => { revoking = null })
    }
    await revoking
  }
  const logout = async () => {
    generation++
    localStorage.setItem(logoutKey, 'pending')
    localStorage.removeItem(tokenKey)
    notify()
    await finishLogout()
  }
  const login = async (email: string, password: string) => {
    // An offline logout must be revoked before this browser starts a new session.
    await finishLogout()
    const started = ++generation
    const form = new FormData()
    form.append('username', email)
    form.append('password', password)
    const response = await transport.post<{ access_token: string; token_type: string }>('/auth/session/login', form)
    if (started !== generation) throw new Error('Session changed')
    localStorage.removeItem(logoutKey)
    storeToken(response.data.access_token)
    return response
  }

  // Single sign-on handoff: trade a host-minted JWT for a normal renewable session.
  // Opening a handoff link is an explicit sign-in, so it overrides an earlier sign-out.
  const exchange = async (hostToken: string) => {
    const started = ++generation
    const response = await transport.post<{ access_token: string }>('/auth/session/exchange', null, {
      headers: { Authorization: `Bearer ${hostToken}` },
    })
    if (started !== generation) throw new Error('Session changed')
    localStorage.removeItem(logoutKey)
    storeToken(response.data.access_token)
    return response.data.access_token
  }

  api.interceptors.response.use(response => response, async error => {
    const request = error.config
    if (error.response?.status !== 401 || !request || request._sessionRetried || signedOut()) throw error
    // Authentication endpoints must report their own errors without renewal loops.
    if (request.url?.startsWith('/auth/')) throw error
    request._sessionRetried = true
    const current = savedToken()
    const token = current && request.headers?.Authorization !== `Bearer ${current}`
      ? current : await refresh()
    request.headers.Authorization = `Bearer ${token}`
    return api.request(request)
  })

  return { login, logout, refresh, finishLogout, expire, exchange }
}

/**
 * Take a handoff token from `#token=…` and remove it from the address bar, so it
 * is not kept in history or copied with the URL. Returns null when there is none.
 */
export function takeHandoffToken(location: Location = window.location, history: History = window.history): string | null {
  const params = new URLSearchParams(location.hash.replace(/^#/, ''))
  const token = params.get('token')
  if (!token) return null
  params.delete('token')
  const rest = params.toString()
  history.replaceState(history.state, '', `${location.pathname}${location.search}${rest ? `#${rest}` : ''}`)
  return token
}
