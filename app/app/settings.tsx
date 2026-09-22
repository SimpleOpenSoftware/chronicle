import React from 'react';

import AuthSection from '@/components/AuthSection';
import BackendStatus from '@/components/BackendStatus';
import NetworkOverview from '@/components/NetworkOverview';
import NotificationsSection from '@/components/NotificationsSection';
import PhoneAudioDiagnosticsSection from '@/components/PhoneAudioDiagnosticsSection';
import SystemAdminControls from '@/components/SystemAdminControls';
import { Screen, SectionLabel } from '@/components/ui';
import { useSharedAppSettings } from '@/contexts/AppSettingsContext';

export default function SettingsScreen() {
  const settings = useSharedAppSettings();

  return (
    <Screen scroll keyboardAvoiding>
      <SectionLabel>Connection</SectionLabel>
      <BackendStatus
        backendUrl={settings.webSocketUrl}
        onBackendUrlChange={settings.handleSetAndSaveWebSocketUrl}
        jwtToken={settings.jwtToken}
      />
      <AuthSection
        backendUrl={settings.webSocketUrl}
        isAuthenticated={settings.isAuthenticated}
        currentUserEmail={settings.currentUserEmail}
        onAuthStatusChange={settings.handleAuthStatusChange}
      />
      <NotificationsSection
        backendUrl={settings.webSocketUrl}
        authenticated={settings.isAuthenticated}
      />

      <SectionLabel>Diagnostics</SectionLabel>
      <PhoneAudioDiagnosticsSection
        backendUrl={settings.webSocketUrl}
        jwtToken={settings.jwtToken}
      />

      <SectionLabel>Administration</SectionLabel>
      <SystemAdminControls backendUrl={settings.webSocketUrl} jwtToken={settings.jwtToken} />
      <NetworkOverview backendUrl={settings.webSocketUrl} />
    </Screen>
  );
}
