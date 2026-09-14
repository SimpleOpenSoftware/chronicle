import Constants from 'expo-constants';
import * as Linking from 'expo-linking';
import * as Notifications from 'expo-notifications';
import { Alert, Platform } from 'react-native';

import { deriveBaseUrl, fetchAuthed } from './auth';
import { getOrCreateInstallationId } from '../utils/storage';

export type NotificationPermissionState = 'granted' | 'denied' | 'undetermined' | 'unsupported';

const projectId = Constants.easConfig?.projectId ?? Constants.expoConfig?.extra?.eas?.projectId;

export const notificationPermissionState = async (): Promise<NotificationPermissionState> => {
  if (Platform.OS === 'web') return 'unsupported';
  const permission = await Notifications.getPermissionsAsync();
  if (permission.granted) return 'granted';
  return permission.status === Notifications.PermissionStatus.DENIED ? 'denied' : 'undetermined';
};

export const configureNotificationPresentation = async (): Promise<void> => {
  if (Platform.OS === 'web') return;
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync('priority', {
      name: 'Priority',
      importance: Notifications.AndroidImportance.HIGH,
      sound: 'default',
    });
    await Notifications.setNotificationChannelAsync('agent', {
      name: 'Agent',
      importance: Notifications.AndroidImportance.DEFAULT,
    });
  } else if (Platform.OS === 'ios') {
    await Promise.all([
      Notifications.setNotificationCategoryAsync('priority', []),
      Notifications.setNotificationCategoryAsync('agent', []),
    ]);
  }
};

const registerToken = async (
  backendUrl: string,
  devicePushToken?: Notifications.DevicePushToken,
): Promise<void> => {
  if (!projectId) throw new Error('This build has no EAS project ID.');
  const token = await Notifications.getExpoPushTokenAsync({ projectId, devicePushToken });
  const installationId = await getOrCreateInstallationId();
  const response = await fetchAuthed(
    `${deriveBaseUrl(backendUrl)}/api/notifications/devices/${encodeURIComponent(installationId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expo_push_token: token.data,
        platform: Platform.OS,
        app_version: Constants.expoConfig?.version ?? null,
        build_version: Constants.nativeBuildVersion ?? null,
      }),
    },
  );
  if (!response.ok) throw new Error(`Notification registration failed (${response.status}).`);
};

export const enablePushNotifications = async (backendUrl: string): Promise<NotificationPermissionState> => {
  if (Platform.OS === 'web') return 'unsupported';
  const permission = await Notifications.requestPermissionsAsync();
  if (!permission.granted) return 'denied';
  await configureNotificationPresentation();
  await registerToken(backendUrl);
  return 'granted';
};

/** Refresh silently only after the user has already granted OS permission. */
export const refreshPushRegistration = async (backendUrl: string): Promise<void> => {
  if (await notificationPermissionState() !== 'granted') return;
  await configureNotificationPresentation();
  await registerToken(backendUrl);
};

export const unregisterPushDevice = async (backendUrl: string): Promise<void> => {
  const installationId = await getOrCreateInstallationId();
  const response = await fetchAuthed(
    `${deriveBaseUrl(backendUrl)}/api/notifications/devices/${encodeURIComponent(installationId)}`,
    { method: 'DELETE' },
  );
  if (!response.ok && response.status !== 404) {
    throw new Error(`Notification unregister failed (${response.status}).`);
  }
};

const handleNotificationData = async (data: Record<string, unknown>): Promise<void> => {
  if (data.action === 'open_immich') {
    try {
      await Linking.openURL('immich://');
    } catch {
      Alert.alert('Could not open Immich', 'Install Immich or open it manually, then return to Chronicle and check again.');
    }
    return;
  }
  if (data.action === 'open_chronicle_route' && typeof data.route === 'string') {
    const route = { timeline: 'timeline', settings: 'settings', memory_ledger: 'memory-ledger' }[data.route];
    if (route) await Linking.openURL(`chronicle://${route}`);
  }
};

export const startNotificationTapHandling = async (): Promise<() => void> => {
  if (Platform.OS === 'web') return () => {};
  const last = await Notifications.getLastNotificationResponseAsync();
  if (last) {
    await handleNotificationData(last.notification.request.content.data ?? {});
    await Notifications.clearLastNotificationResponseAsync();
  }
  const subscription = Notifications.addNotificationResponseReceivedListener(response => {
    void handleNotificationData(response.notification.request.content.data ?? {});
  });
  return () => subscription.remove();
};

/** Native-token rotation means the Expo token must be fetched and registered again. */
export const listenForPushTokenChanges = (backendUrl: string): (() => void) => {
  if (Platform.OS === 'web') return () => {};
  const subscription = Notifications.addPushTokenListener(devicePushToken => {
    void registerToken(backendUrl, devicePushToken).catch(error => {
      console.warn('[Notifications] token refresh failed:', error);
    });
  });
  return () => subscription.remove();
};
