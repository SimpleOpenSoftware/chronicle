import * as Application from 'expo-application';
import Constants from 'expo-constants';
import { Platform } from 'react-native';

import { fetchAuthed, deriveBaseUrl } from './auth';
import { getLastConnectedDeviceId, getWebSocketUrl } from '../utils/storage';

export interface ClientDiagnosticReceipt {
  upload_id: string;
  received_at: string;
  size_bytes: number;
  sha256: string;
  platform: string | null;
  app_version: string | null;
  build_version: string | null;
  device_id: string | null;
}

export async function uploadClientDiagnostic(contents: string): Promise<ClientDiagnosticReceipt> {
  if (!contents.trim()) throw new Error('The device log is empty.');

  const configuredUrl = await getWebSocketUrl();
  if (!configuredUrl) throw new Error('Configure and log in to a Chronicle backend first.');

  const deviceId = await getLastConnectedDeviceId();
  const appVersion = Application.nativeApplicationVersion ?? Constants.expoConfig?.version ?? 'unknown';
  const buildVersion = Application.nativeBuildVersion ?? 'unknown';
  const response = await fetchAuthed(`${deriveBaseUrl(configuredUrl)}/api/client-diagnostics`, {
    method: 'POST',
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      'X-Chronicle-Platform': `${Platform.OS} ${Platform.Version}`,
      'X-Chronicle-App-Version': appVersion,
      'X-Chronicle-Build-Version': buildVersion,
      ...(deviceId ? { 'X-Chronicle-Device-ID': deviceId } : {}),
    },
    body: contents,
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Backend rejected the log (${response.status}): ${detail || response.statusText}`);
  }
  return await response.json() as ClientDiagnosticReceipt;
}
