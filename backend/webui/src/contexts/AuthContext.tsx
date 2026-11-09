import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { authApi, browserSession, type CurrentUser } from '../services/api'
import { getStorageKey } from '../utils/storage'
import { setDisplayTimezone } from '../utils/datetime'
import { SESSION_CHANGED, savedToken, signedOut, logoutPending } from '../services/browserSession'

interface AuthContextType {
  user: CurrentUser | null
  token: string | null
  login: (email: string, password: string) => Promise<{success: boolean, error?: string, errorType?: string}>
  logout: () => void
  connectionError: boolean
  retryConnection: () => void
  isLoading: boolean
  isAdmin: boolean
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [token, setToken] = useState<string | null>(localStorage.getItem(getStorageKey('token')))
  const [isLoading, setIsLoading] = useState(true)

  const [connectionError, setConnectionError] = useState(false)
  const [retry, setRetry] = useState(0)
  const retryConnection = () => setRetry(value => value + 1)

  // Check if user is admin
  const isAdmin = user?.is_superuser || false

  // Every displayed timestamp follows the user's saved zone (browser zone when unset).
  useEffect(() => setDisplayTimezone(user?.timezone), [user?.timezone])

  useEffect(() => {
    const sync = () => {
      setToken(savedToken())
      if (!savedToken()) setUser(null)
    }
    const syncStorage = (event: StorageEvent) => {
      if (event.key === getStorageKey('token') || event.key === getStorageKey('sessionLogout') || event.key === null) {
        sync()
        retryConnection()
      }
    }
    window.addEventListener(SESSION_CHANGED, sync)
    window.addEventListener('storage', syncStorage)
    window.addEventListener('online', retryConnection)
    return () => {
      window.removeEventListener(SESSION_CHANGED, sync)
      window.removeEventListener('storage', syncStorage)
      window.removeEventListener('online', retryConnection)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const check = async () => {
      try {
        if (signedOut()) {
          setUser(null)
          setConnectionError(false)
          setIsLoading(false)
          if (logoutPending()) await browserSession.finishLogout()
          return
        }
        if (!savedToken()) await browserSession.refresh()
        const response = await authApi.getMe()
        if (cancelled || signedOut()) return
        setUser(response.data)
        setToken(savedToken())
        setConnectionError(false)
        // Renew before expiry, including for non-Axios media/WebSocket consumers.
        const payload = JSON.parse(atob(savedToken()!.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
        const delay = Math.max(1000, Math.min(2147483647, payload.exp * 1000 - Date.now() - 60000))
        timer = setTimeout(async () => {
          try { await browserSession.refresh() } catch {
            if (!cancelled) timer = setTimeout(retryConnection, 5000)
          }
        }, delay)
      } catch (error: any) {
        if (cancelled) return
        if (error.response?.status === 401 && signedOut()) {
          setUser(null)
          setToken(null)
          setConnectionError(false)
        } else {
          // Network errors and 5xx say nothing about the saved credential.
          setConnectionError(!signedOut())
          timer = setTimeout(retryConnection, 5000)
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    void check()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [token, retry])

  const login = async (email: string, password: string): Promise<{success: boolean, error?: string, errorType?: string}> => {
    try {
      const response = await authApi.login(email, password)

      const { access_token } = response.data
      setToken(access_token)
      setConnectionError(false)

      // Get user info
      const userResponse = await authApi.getMe()
      if (signedOut() || savedToken() !== access_token) return { success: false, error: 'Session changed. Please sign in again.' }
      setUser(userResponse.data)

      return { success: true }
    } catch (error: any) {
      console.error('Login failed:', error)

      // Parse structured error response from backend
      let errorMessage = 'Login failed. Please try again.'
      let errorType = 'unknown'

      if (error.response?.data) {
        const errorData = error.response.data
        errorMessage = errorData.detail || errorMessage
        errorType = errorData.error_type || errorType
        if (errorData.detail === 'LOGIN_BAD_CREDENTIALS') {
          errorMessage = 'Invalid email or password'
          errorType = 'authentication_failure'
        }
      } else if (error.code === 'ERR_NETWORK') {
        errorMessage = 'Unable to connect to server. Please check your connection and try again.'
        errorType = 'connection_failure'
      }

      return {
        success: false,
        error: errorMessage,
        errorType: errorType
      }
    }
  }

  const logout = () => {
    // Local sign-out is immediate. Failed revocation is persisted and retried;
    // a reload must never silently restore a session the user signed out of.
    void browserSession.logout().catch(() => retryConnection())
  }

  return (
    <AuthContext.Provider value={{ user, token, login, logout, isLoading, isAdmin, connectionError, retryConnection }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
