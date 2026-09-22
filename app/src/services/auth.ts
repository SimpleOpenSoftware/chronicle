// auth.ts — central authentication / token manager.
//
// Single source of truth for obtaining a *valid* JWT. JWTs are short-lived
// (1h), so "we have a token" is not the same as "we are logged in". Everything
// that needs auth should go through getValidToken()/fetchAuthed() so a token
// that has expired (or a 401) is silently refreshed from stored credentials —
// the user only sees a real failure if re-login itself fails.
//
// Framework-agnostic (no React) so hooks, screens and background tasks can all
// share it.

import {
  getAuthEmail,
  getAuthPassword,
  getWebSocketUrl,
  getJwtToken,
  saveAuthEmail,
  saveAuthPassword,
  saveJwtToken,
  clearToken,
  clearAuthData,
} from '../utils/storage';

/** Convert a ws(s):// or http(s):// URL (optionally ending in /ws) to an HTTP base URL. */
export const deriveBaseUrl = (url: string): string => {
  return url
    .replace('ws://', 'http://')
    .replace('wss://', 'https://')
    .split('/ws')[0];
};

/** Best-effort decode of a JWT's `exp` (epoch seconds). Returns null if undecodable. */
const getTokenExpiry = (token: string): number | null => {
  try {
    const payload = token.split('.')[1];
    if (!payload) return null;
    // base64url → base64
    const base64 = payload.replace(/-/g, '+').replace(/_/g, '/');
    const json = typeof atob === 'function' ? atob(base64) : null;
    if (!json) return null;
    const claims = JSON.parse(json);
    return typeof claims.exp === 'number' ? claims.exp : null;
  } catch {
    return null;
  }
};

/** True if the token is missing or within `skewSeconds` of expiry. */
export const isTokenExpired = (token: string | null, skewSeconds = 60): boolean => {
  if (!token) return true;
  const exp = getTokenExpiry(token);
  if (exp === null) return false; // can't tell → assume usable, rely on 401 retry
  return Date.now() / 1000 >= exp - skewSeconds;
};

type TokenListener = (token: string) => void;
const tokenListeners = new Set<TokenListener>();

export const onTokenRefreshed = (listener: TokenListener): (() => void) => {
  tokenListeners.add(listener);
  return () => tokenListeners.delete(listener);
};

// De-dupe concurrent refreshes: many callers may hit a 401 at once.
let refreshInFlight: Promise<string | null> | null = null;

/**
 * Authenticate with the backend and persist email/password/token.
 * Returns the new token. Throws on failure (used by the interactive login form).
 */
export const login = async (
  email: string,
  password: string,
  backendUrl: string
): Promise<string> => {
  const baseUrl = deriveBaseUrl(backendUrl);
  const loginUrl = `${baseUrl}/auth/jwt/login`;
  const body = `username=${encodeURIComponent(email)}&password=${encodeURIComponent(password)}`;

  const response = await fetch(loginUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`Login failed: ${response.status} ${response.statusText} ${text}`.trim());
  }

  const data = await response.json();
  const token = data.access_token;
  if (!token) throw new Error('No access token received from server');

  await saveAuthEmail(email);
  await saveAuthPassword(password);
  await saveJwtToken(token);
  tokenListeners.forEach(listener => listener(token));
  return token;
};

/**
 * Silently re-authenticate using stored credentials. Returns the new token, or
 * null if credentials are missing or re-login fails. Concurrent callers share a
 * single in-flight request.
 */
export const refreshToken = async (): Promise<string | null> => {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const email = await getAuthEmail();
      const password = await getAuthPassword();
      const wsUrl = await getWebSocketUrl();
      if (!email || !password || !wsUrl) {
        console.log('[Auth] Cannot refresh: missing stored credentials');
        return null;
      }
      const token = await login(email, password, wsUrl);
      console.log('[Auth] Token refreshed');
      return token;
    } catch (e) {
      console.warn('[Auth] Token refresh failed:', e);
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
};

/**
 * Return a token that is valid right now: the stored token if it's not near
 * expiry, otherwise a freshly refreshed one. Returns null only if we have no
 * way to authenticate.
 */
export const getValidToken = async (): Promise<string | null> => {
  const token = await getJwtToken();
  if (token && !isTokenExpired(token)) return token;
  return await refreshToken();
};

/**
 * fetch() wrapper that attaches a valid bearer token and transparently retries
 * once on a 401 after refreshing. Use for ALL authenticated API calls so the UI
 * never has to think about token expiry.
 */
export const fetchAuthed = async (
  input: string,
  init: RequestInit = {}
): Promise<Response> => {
  const withAuth = async (token: string | null): Promise<Response> => {
    const headers = new Headers(init.headers || {});
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return fetch(input, { ...init, headers });
  };

  let token = await getValidToken();
  let response = await withAuth(token);

  if (response.status === 401) {
    // Token rejected despite our expiry check — force a refresh and retry once.
    token = await refreshToken();
    if (token) {
      response = await withAuth(token);
    }
  }

  return response;
};

/** Log out: drop the token but keep email + password for one-tap re-login. */
export const logout = async (): Promise<void> => {
  await clearToken();
};

/** Forget account: clear email, password and token. */
export const forgetAccount = async (): Promise<void> => {
  await clearAuthData();
};
