import { useState, useEffect, useCallback } from 'react';
import {
  saveWebSocketUrl,
  getWebSocketUrl,
  saveUserId,
  getUserId,
  getAuthEmail,
  getJwtToken,
  getAutoReconnectEnabled,
  saveAutoReconnectEnabled,
} from '../utils/storage';
import { recoverBackendUrl } from '../services/serviceManager';
import { httpUrlToWebSocketUrl } from '../utils/urlConversion';
import { onTokenRefreshed } from '../services/auth';

const DEFAULT_WS_URL = 'ws://localhost:8000/ws/audio';

export interface AppSettings {
  webSocketUrl: string;
  userId: string;
  isAuthenticated: boolean;
  currentUserEmail: string | null;
  jwtToken: string | null;
  autoReconnectEnabled: boolean;
  handleSetAndSaveWebSocketUrl: (url: string) => Promise<void>;
  handleSetAndSaveUserId: (id: string) => Promise<void>;
  handleAuthStatusChange: (authenticated: boolean, email: string | null, token: string | null) => void;
  handleToggleAutoReconnect: () => void;
}

export const useAppSettings = (): AppSettings => {
  const [webSocketUrl, setWebSocketUrl] = useState<string>('');
  const [userId, setUserId] = useState<string>('');
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [currentUserEmail, setCurrentUserEmail] = useState<string | null>(null);
  const [jwtToken, setJwtToken] = useState<string | null>(null);
  const [autoReconnectEnabled, setAutoReconnectEnabled] = useState<boolean>(true);

  useEffect(() => onTokenRefreshed(setJwtToken), []);

  useEffect(() => {
    const loadSettings = async () => {
      const storedWsUrl = await getWebSocketUrl();
      const hasRealUrl = !!storedWsUrl && storedWsUrl !== DEFAULT_WS_URL;
      if (hasRealUrl) {
        setWebSocketUrl(storedWsUrl as string);
      } else {
        setWebSocketUrl(DEFAULT_WS_URL);
        await saveWebSocketUrl(DEFAULT_WS_URL);
        // Backend URL is missing/placeholder — try to recover it from stored
        // service-manager credentials (the SM host is the backend host). This is
        // a no-op (returns fast) when no SM URL is stored. Non-blocking so app
        // startup isn't delayed by network probes.
        recoverBackendUrl()
          .then(async recovered => {
            if (recovered) {
              const wsUrl = httpUrlToWebSocketUrl(recovered);
              setWebSocketUrl(wsUrl);
              await saveWebSocketUrl(wsUrl);
              console.log('[AppSettings] Recovered backend URL from service-manager creds');
            }
          })
          .catch(() => {});
      }

      const storedUserId = await getUserId();
      if (storedUserId) setUserId(storedUserId);

      const storedEmail = await getAuthEmail();
      const storedToken = await getJwtToken();
      if (storedEmail && storedToken) {
        setCurrentUserEmail(storedEmail);
        setJwtToken(storedToken);
        setIsAuthenticated(true);
      }

      setAutoReconnectEnabled(await getAutoReconnectEnabled());
    };
    loadSettings();
  }, []);

  const handleSetAndSaveWebSocketUrl = useCallback(async (url: string) => {
    setWebSocketUrl(url);
    await saveWebSocketUrl(url);
  }, []);

  const handleSetAndSaveUserId = useCallback(async (id: string) => {
    setUserId(id);
    await saveUserId(id || null);
  }, []);

  const handleAuthStatusChange = useCallback((authenticated: boolean, email: string | null, token: string | null) => {
    setIsAuthenticated(authenticated);
    setCurrentUserEmail(email);
    setJwtToken(token);
  }, []);

  const handleToggleAutoReconnect = useCallback(() => {
    setAutoReconnectEnabled(prev => {
      const next = !prev;
      saveAutoReconnectEnabled(next);
      return next;
    });
  }, []);

  return {
    webSocketUrl,
    userId,
    isAuthenticated,
    currentUserEmail,
    jwtToken,
    autoReconnectEnabled,
    handleSetAndSaveWebSocketUrl,
    handleSetAndSaveUserId,
    handleAuthStatusChange,
    handleToggleAutoReconnect,
  };
};
