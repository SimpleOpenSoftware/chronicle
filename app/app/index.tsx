import React, { useRef, useCallback, useEffect, useState } from 'react';
import { Text, View, SafeAreaView, ScrollView, Platform, FlatList, ActivityIndicator, Alert, Switch, TouchableOpacity, KeyboardAvoidingView, StyleSheet, RefreshControl } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import { Link } from 'expo-router';
import Constants from 'expo-constants';
import { useTheme, type Theme } from '@/theme';
import { Button, Card, InlineAlert, SectionLabel, StatusDot, toneDotColor } from '@/components/ui';

// Hooks
import { useBluetoothManager } from '@/hooks/useBluetoothManager';
import { useDeviceScanning } from '@/hooks/useDeviceScanning';
import { useDeviceConnection } from '@/hooks/useDeviceConnection';
import { useSharedAppSettings } from '@/contexts/AppSettingsContext';
import { useAutoReconnect } from '@/hooks/useAutoReconnect';
import { useAudioStreamingOrchestrator } from '@/hooks/useAudioStreamingOrchestrator';
import { useAudioListener } from '@/hooks/useAudioListener';
import { useAudioStreamer } from '@/hooks/useAudioStreamer';
import { usePhoneAudioRecorder } from '@/hooks/usePhoneAudioRecorder';
import { useBatteryMonitor } from '@/hooks/useBatteryMonitor';
import { useBackendHealth, isNotConfigured } from '@/hooks/useBackendHealth';
import { saveLastConnectedDeviceId } from '@/utils/storage';

// Components
import BluetoothStatusBanner from '@/components/BluetoothStatusBanner';
import ScanControls from '@/components/ScanControls';
import DeviceListItem from '@/components/DeviceListItem';
import DeviceDetails from '@/components/DeviceDetails';
import PhoneAudioButton from '@/components/PhoneAudioButton';

export default function App() {
  const t = useTheme();
  const s = createStyles(t);
  const omiConnection = useRef(new OmiConnection()).current;
  const [showOnlyOmi, setShowOnlyOmi] = useState(false);
  const [activeTab, setActiveTab] = useState<'backend' | 'connection'>('backend');

  // Bluetooth
  const { bleManager, bluetoothState, permissionGranted, requestBluetoothPermission, isPermissionsLoading } = useBluetoothManager();

  // Settings
  const settings = useSharedAppSettings();

  // Live backend reachability (Connection Doctor), re-probed on pull-to-refresh.
  const { healthStatus, checkBackendHealth } = useBackendHealth(settings.webSocketUrl, settings.jwtToken);
  const [refreshing, setRefreshing] = useState(false);

  // Audio
  const audioStreamer = useAudioStreamer();
  const phoneAudioRecorder = usePhoneAudioRecorder();

  const { isListeningAudio: isOmiAudioListenerActive, audioPacketsReceived, startAudioListener: originalStartAudioListener, stopAudioListener: originalStopAudioListener, isRetrying: isAudioListenerRetrying, retryAttempts: audioListenerRetryAttempts } = useAudioListener(omiConnection, () => !!deviceConnection.connectedDeviceId);

  // Refs for disconnect cleanup
  const isOmiAudioListenerActiveRef = useRef(isOmiAudioListenerActive);
  // Track if audio pipeline was active before BLE disconnect (for auto-restart on reconnect)
  const wasStreamingBeforeDisconnectRef = useRef(false);
  useEffect(() => { isOmiAudioListenerActiveRef.current = isOmiAudioListenerActive; }, [isOmiAudioListenerActive]);

  // Refs to break the declaration-order cycle:
  // onDeviceConnect/onDeviceDisconnect need orchestrator + autoReconnect,
  // but deviceConnection (which needs those callbacks) must be declared
  // before orchestrator and autoReconnect.
  type OrchestratorHandle = ReturnType<typeof useAudioStreamingOrchestrator>;
  type AutoReconnectHandle = ReturnType<typeof useAutoReconnect>;
  const orchestratorRef = useRef<OrchestratorHandle | null>(null);
  const autoReconnectRef = useRef<AutoReconnectHandle | null>(null);

  // Device callbacks
  const onDeviceConnect = useCallback(async () => {
    const deviceIdToSave = omiConnection.connectedDeviceId;
    if (deviceIdToSave) {
      await saveLastConnectedDeviceId(deviceIdToSave);
      autoReconnectRef.current?.setLastKnownDeviceId(deviceIdToSave);
      autoReconnectRef.current?.setTriedAutoReconnectForCurrentId(false);
    }

    // Auto-restart audio pipeline if it was active before BLE disconnect
    if (wasStreamingBeforeDisconnectRef.current) {
      wasStreamingBeforeDisconnectRef.current = false;
      console.log('[App] BLE reconnected — auto-restarting audio pipeline');
      // Short delay to let BLE connection stabilize
      setTimeout(() => {
        orchestratorRef.current?.handleStartAudioListeningAndStreaming().catch(err => {
          console.error('[App] Failed to auto-restart audio pipeline:', err);
        });
      }, 1000);
    }
  }, [omiConnection]);

  const onDeviceDisconnect = useCallback(async () => {
    // BLE disconnect only owns the wearable pipeline. Phone capture is independent.
    if (isOmiAudioListenerActiveRef.current) {
      wasStreamingBeforeDisconnectRef.current = true;
      await originalStopAudioListener();
      await audioStreamer.stopStreaming();
    }
  }, [originalStopAudioListener, audioStreamer.stopStreaming]);

  const deviceConnection = useDeviceConnection(omiConnection, bleManager, onDeviceDisconnect, onDeviceConnect);

  // Battery monitor
  const batteryMonitor = useBatteryMonitor({
    connectedDeviceId: deviceConnection.connectedDeviceId,
    getBatteryLevel: deviceConnection.getRawBatteryLevel,
    onConnectionLost: deviceConnection.disconnectFromDevice,
  });

  // Auto-reconnect
  const autoReconnect = useAutoReconnect({
    bluetoothState,
    permissionGranted,
    deviceConnection,
    scanning: false,
    autoReconnectEnabled: settings.autoReconnectEnabled,
  });

  // Scanning
  const { devices: scannedDevices, scanning, startScan, stopScan: stopDeviceScanAction } = useDeviceScanning(bleManager, omiConnection, permissionGranted, bluetoothState === BluetoothState.PoweredOn, requestBluetoothPermission);

  // Audio orchestrator
  const orchestrator = useAudioStreamingOrchestrator({
    omiConnection,
    deviceConnection,
    audioStreamer,
    phoneAudioRecorder,
    originalStartAudioListener,
    originalStopAudioListener,
    settings,
  });

  // Keep forward-declared refs in sync so device callbacks can call through.
  orchestratorRef.current = orchestrator;
  autoReconnectRef.current = autoReconnect;

  // Cleanup
  const cleanupRefs = useRef({ omiConnection, bleManager, disconnectFromDevice: deviceConnection.disconnectFromDevice, stopAudioStreaming: audioStreamer.stopStreaming, stopPhoneAudio: phoneAudioRecorder.stopRecording });
  useEffect(() => { cleanupRefs.current = { omiConnection, bleManager, disconnectFromDevice: deviceConnection.disconnectFromDevice, stopAudioStreaming: audioStreamer.stopStreaming, stopPhoneAudio: phoneAudioRecorder.stopRecording }; });
  useEffect(() => {
    return () => {
      const refs = cleanupRefs.current;
      if (refs.omiConnection.isConnected()) refs.disconnectFromDevice().catch(() => {});
      if (refs.bleManager) refs.bleManager.destroy();
      refs.stopAudioStreaming();
      refs.stopPhoneAudio().catch(() => {});
    };
  }, []);

  const canScan = React.useMemo(() => (
    permissionGranted && bluetoothState === BluetoothState.PoweredOn &&
    !autoReconnect.isAttemptingAutoReconnect && !autoReconnect.isRetryingConnection &&
    !deviceConnection.isConnecting &&
    !deviceConnection.connectedDeviceId &&
    (autoReconnect.triedAutoReconnectForCurrentId || !autoReconnect.lastKnownDeviceId)
  ), [permissionGranted, bluetoothState, autoReconnect.isAttemptingAutoReconnect, autoReconnect.isRetryingConnection, deviceConnection.isConnecting, deviceConnection.connectedDeviceId, autoReconnect.triedAutoReconnectForCurrentId, autoReconnect.lastKnownDeviceId]);

  const filteredDevices = React.useMemo(() => {
    if (!showOnlyOmi) return scannedDevices;
    return scannedDevices.filter(d => {
      const name = d.name?.toLowerCase() || '';
      return name.includes('omi') || name.includes('friend') || name.includes('neo') || name.includes('elato');
    });
  }, [scannedDevices, showOnlyOmi]);

  // Pull-to-refresh: re-probe backend reachability and refresh live device state.
  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await Promise.all([
        checkBackendHealth(false),
        deviceConnection.connectedDeviceId ? batteryMonitor.refreshBattery() : Promise.resolve(),
      ]);
    } finally {
      setRefreshing(false);
    }
  }, [checkBackendHealth, deviceConnection.connectedDeviceId, batteryMonitor.refreshBattery]);

  const bluetoothReady = bluetoothState === BluetoothState.PoweredOn && permissionGranted;
  // A fresh install points at localhost (the phone itself), which can never be a
  // real backend — treat that (and empty) as "not paired yet" so the setup card
  // and health pill reflect reality.
  const backendConfigured = !isNotConfigured(settings.webSocketUrl);
  // The pill reflects the live probe, not just config: a confirmed-bad probe
  // (offline / unreachable / down / unhealthy) turns it red; pending or healthy
  // probes fall back to the config+bluetooth view.
  const backendDown = ['offline', 'backend_down', 'unreachable', 'unhealthy'].includes(healthStatus.status);
  const isOperational = bluetoothReady && backendConfigured && !backendDown;
  const healthLabel = backendDown
    ? (healthStatus.status === 'offline' ? "You're Offline" : 'Backend Unreachable')
    : isOperational ? 'System Operational' : 'Action Needed';
  const healthTone: 'danger' | 'success' | 'warning' = backendDown ? 'danger' : isOperational ? 'success' : 'warning';
  const batteryDisplay = deviceConnection.connectedDeviceId
    ? batteryMonitor.batteryLevel >= 0 ? `${batteryMonitor.batteryLevel}%` : '...'
    : '--';
  const streamDisplay = audioStreamer.isStreaming
    ? 'Streaming'
    : (phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode)
      ? 'Phone Mic'
      : 'Idle';

  // Loading / auto-reconnect screens
  if (isPermissionsLoading && bluetoothState === BluetoothState.Unknown) {
    return (
      <View style={s.centeredMessageContainer}>
        <ActivityIndicator size="large" color={t.color.accent.base} />
        <Text style={s.centeredMessageText}>
          {autoReconnect.isAttemptingAutoReconnect
            ? `Reconnecting to ${autoReconnect.lastKnownDeviceId?.substring(0, 10)}...`
            : 'Initializing Bluetooth...'}
        </Text>
      </View>
    );
  }

  return (
    <SafeAreaView style={s.container}>
      <View style={s.pulseBackground} />
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}>
        <ScrollView
          contentContainerStyle={s.content}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={t.color.accent.base}
              colors={[t.color.accent.base]}
              progressBackgroundColor={t.color.surface.raised}
            />
          }
        >
          <Card>
            <View style={s.titleRow}>
              <View style={s.brandRow}>
                <Text style={s.title}>Chronicle</Text>
                <Text style={s.versionText}>v{Constants.expoConfig?.version ?? ''}</Text>
              </View>
              <View style={s.headerActions}>
                <Link href="/diagnostics" asChild>
                  <TouchableOpacity style={s.diagButton}>
                    <Text style={s.diagButtonText}>Diagnostics</Text>
                  </TouchableOpacity>
                </Link>
                <Link href="/settings" asChild>
                  <TouchableOpacity style={s.gearButton} accessibilityLabel="Settings">
                    <Text style={s.gearButtonText}>⚙</Text>
                  </TouchableOpacity>
                </Link>
              </View>
            </View>

            <View style={[s.healthPill, { borderColor: toneDotColor(t, healthTone) }]}>
              <StatusDot tone={healthTone} size={8} style={s.healthDot} />
              <Text style={[s.healthText, { color: t.color.status[healthTone].fg }]}>{healthLabel}</Text>
            </View>
          </Card>

          <Card title={activeTab === 'backend' ? 'Backend Dashboard' : 'Connection Center'}>
            <Text style={s.heroSubtitle}>
              {activeTab === 'backend'
                ? 'Control center for backend, audio streaming, and wakeword behavior.'
                : 'Manage Bluetooth pairing, reconnect flow, and device audio routing.'}
            </Text>
            <View style={s.heroMetricsRow}>
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{streamDisplay}</Text>
                <Text style={s.metricLabel}>Audio State</Text>
              </View>
              <View style={s.metricDivider} />
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{batteryDisplay}</Text>
                <Text style={s.metricLabel}>Battery</Text>
              </View>
              <View style={s.metricDivider} />
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{deviceConnection.connectedDeviceId ? 'Connected' : 'Idle'}</Text>
                <Text style={s.metricLabel}>Device</Text>
              </View>
            </View>
          </Card>

          {activeTab === 'backend' && (
            <>
              {(!backendConfigured || !settings.isAuthenticated) && (
                <Link href="/settings" asChild>
                  <TouchableOpacity style={s.setupCard}>
                    <Text style={s.setupTitle}>
                      {!backendConfigured ? 'Connect to your backend' : 'Sign in to your backend'}
                    </Text>
                    <Text style={s.setupSubtitle}>
                      {!backendConfigured
                        ? 'Scan the QR code from your Chronicle dashboard to pair this app.'
                        : 'Log in to access Chronicle backend features.'}
                    </Text>
                    <Text style={s.setupCta}>Open Settings ⚙</Text>
                  </TouchableOpacity>
                </Link>
              )}

              <SectionLabel>Audio Deck</SectionLabel>
              <PhoneAudioButton
                isRecording={phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode}
                isInitializing={phoneAudioRecorder.isInitializing}
                isDisabled={!!deviceConnection.connectedDeviceId || deviceConnection.isConnecting}
                audioLevel={phoneAudioRecorder.audioLevel}
                error={phoneAudioRecorder.error}
                playbackState={audioStreamer.phonePlaybackState}
                onPress={orchestrator.handleTogglePhoneAudio}
              />
            </>
          )}

          {activeTab === 'connection' && (
            <>
              <SectionLabel>Bluetooth</SectionLabel>
              <BluetoothStatusBanner bluetoothState={bluetoothState} isPermissionsLoading={isPermissionsLoading} permissionGranted={permissionGranted} onRequestPermission={requestBluetoothPermission} />
              <ScanControls scanning={scanning} onScanPress={startScan} onStopScanPress={stopDeviceScanAction} canScan={canScan} />

              {(autoReconnect.isAttemptingAutoReconnect || autoReconnect.isRetryingConnection) && (
                <View style={s.retryBanner}>
                  <ActivityIndicator size="small" color={t.color.status.warning.base} />
                  <Text style={s.retryBannerText}>
                    {autoReconnect.isRetryingConnection
                      ? `Reconnecting in ${autoReconnect.retryBackoffSeconds}s... (attempt ${autoReconnect.connectionRetryCount})`
                      : `Reconnecting to ${autoReconnect.lastKnownDeviceId?.substring(0, 10) ?? 'device'}...`}
                  </Text>
                  <Button
                    variant="danger"
                    size="sm"
                    onPress={autoReconnect.handleCancelAutoReconnect}
                  >
                    Cancel
                  </Button>
                </View>
              )}

              {!settings.isAuthenticated && (
                <InlineAlert tone="warning" style={s.authWarning}>
                  Login is required for Chronicle backend features. Simple backend can be used without authentication.
                </InlineAlert>
              )}

              {scannedDevices.length > 0 && !deviceConnection.connectedDeviceId && !autoReconnect.isAttemptingAutoReconnect && (
                <Card title="Found Devices">
                  <View style={s.filterContainer}>
                    <Text style={s.filterText}>Show only OMI/Friend/Neo/Elato</Text>
                    <Switch
                      trackColor={{ false: t.color.disabled, true: t.color.accent.base }}
                      thumbColor={showOnlyOmi ? t.color.status.warning.base : t.color.surface.raised}
                      onValueChange={setShowOnlyOmi}
                      value={showOnlyOmi}
                    />
                  </View>
                  {filteredDevices.length > 0 ? (
                    <FlatList
                      data={filteredDevices}
                      renderItem={({ item }) => (
                        <DeviceListItem device={item} onConnect={deviceConnection.connectToDevice} onDisconnect={deviceConnection.disconnectFromDevice} isConnecting={deviceConnection.isConnecting} connectedDeviceId={deviceConnection.connectedDeviceId} />
                      )}
                      keyExtractor={(item) => item.id}
                      style={{ maxHeight: 200 }}
                    />
                  ) : (
                    <View style={s.noDevicesContainer}>
                      <Text style={s.noDevicesText}>
                        {showOnlyOmi ? `No OMI/Friend/Neo/Elato devices found. ${scannedDevices.length} other device(s) hidden by filter.` : 'No devices found.'}
                      </Text>
                    </View>
                  )}
                </Card>
              )}

              {deviceConnection.connectedDeviceId && filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId) && (
                <Card title="Connected Device">
                  <DeviceListItem
                    device={filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId)!}
                    onConnect={() => {}}
                    onDisconnect={async () => {
                      await saveLastConnectedDeviceId(null);
                      autoReconnect.setLastKnownDeviceId(null);
                      autoReconnect.setTriedAutoReconnectForCurrentId(true);
                      try { await deviceConnection.disconnectFromDevice(); } catch { Alert.alert('Error', 'Failed to disconnect.'); }
                    }}
                    isConnecting={deviceConnection.isConnecting}
                    connectedDeviceId={deviceConnection.connectedDeviceId}
                  />
                </Card>
              )}

              {deviceConnection.connectedDeviceId && !filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId) && (
                <Card>
                  <View style={s.disconnectContainer}>
                    <Text style={s.connectedText}>Connected to: {deviceConnection.connectedDeviceId.substring(0, 15)}...</Text>
                    <Button
                      variant="danger"
                      size="sm"
                      onPress={async () => {
                        await saveLastConnectedDeviceId(null);
                        autoReconnect.setLastKnownDeviceId(null);
                        autoReconnect.setTriedAutoReconnectForCurrentId(true);
                        try { await deviceConnection.disconnectFromDevice(); } catch { Alert.alert('Error', 'Failed to disconnect.'); }
                      }}
                      disabled={deviceConnection.isConnecting}
                    >
                      {deviceConnection.isConnecting ? 'Disconnecting...' : 'Disconnect'}
                    </Button>
                  </View>
                </Card>
              )}

              {deviceConnection.connectedDeviceId && (
                <DeviceDetails
                  connectedDeviceId={deviceConnection.connectedDeviceId}
                  onGetAudioCodec={deviceConnection.getAudioCodec}
                  currentCodec={deviceConnection.currentCodec}
                  batteryLevel={batteryMonitor.batteryLevel}
                  isLowBattery={batteryMonitor.isLowBattery}
                  onRefreshBattery={batteryMonitor.refreshBattery}
                  isListeningAudio={isOmiAudioListenerActive}
                  onStartAudioListener={orchestrator.handleStartAudioListeningAndStreaming}
                  onStopAudioListener={orchestrator.handleStopAudioListeningAndStreaming}
                  audioPacketsReceived={audioPacketsReceived}
                  webSocketUrl={settings.webSocketUrl}
                  onSetWebSocketUrl={settings.handleSetAndSaveWebSocketUrl}
                  isAudioStreaming={audioStreamer.isStreaming}
                  isConnectingAudioStreamer={audioStreamer.isConnecting}
                  audioStreamerError={audioStreamer.error}
                  userId={settings.userId}
                  onSetUserId={settings.handleSetAndSaveUserId}
                  isAudioListenerRetrying={isAudioListenerRetrying}
                  audioListenerRetryAttempts={audioListenerRetryAttempts}
                  autoReconnectEnabled={settings.autoReconnectEnabled}
                  onToggleAutoReconnect={settings.handleToggleAutoReconnect}
                />
              )}
            </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
      <View style={s.bottomNav}>
        <Button
          variant={activeTab === 'connection' ? 'primary' : 'ghost'}
          size="md"
          style={s.navItem}
          onPress={() => setActiveTab('connection')}
        >
          Connection
        </Button>
        <Button
          variant={activeTab === 'backend' ? 'primary' : 'ghost'}
          size="md"
          style={s.navItem}
          onPress={() => setActiveTab('backend')}
        >
          Backend
        </Button>
      </View>
    </SafeAreaView>
  );
}

const createStyles = (t: Theme) => StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: t.color.surface.page,
  },
  pulseBackground: {
    position: 'absolute',
    width: 440,
    height: 440,
    borderRadius: 220,
    backgroundColor: t.color.accent.base,
    opacity: 0.05,
    top: -140,
    right: -140,
  },
  content: {
    padding: t.space[4],
    paddingTop: Platform.OS === 'android' ? t.space[8] : t.space[3],
    paddingBottom: 110,
  },
  titleRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: t.space[3],
  },
  brandRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
  },
  title: {
    fontFamily: t.font.sans,
    ...t.type['2xl'],
    fontWeight: t.weight.bold,
    color: t.color.text.primary,
  },
  versionText: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.muted,
    marginLeft: t.space[2],
  },
  headerActions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: t.space[2],
  },
  diagButton: {
    paddingVertical: t.space[1.5],
    paddingHorizontal: t.space[3],
    borderRadius: t.radius.lg,
    backgroundColor: t.color.surface.sunken,
    borderWidth: t.borderWidth,
    borderColor: t.color.border.base,
  },
  diagButtonText: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.accent.fg,
    fontWeight: t.weight.bold,
  },
  gearButton: {
    width: 34,
    height: 34,
    borderRadius: t.radius.lg,
    backgroundColor: t.color.surface.sunken,
    borderWidth: t.borderWidth,
    borderColor: t.color.border.base,
    alignItems: 'center',
    justifyContent: 'center',
  },
  gearButtonText: {
    fontFamily: t.font.sans,
    ...t.type.base,
    color: t.color.accent.fg,
    fontWeight: t.weight.bold,
  },
  setupCard: {
    marginBottom: t.space[5],
    padding: t.space[4],
    borderRadius: t.radius.xl,
    backgroundColor: t.color.surface.raised,
    borderWidth: t.borderWidth,
    borderColor: t.color.accent.base,
  },
  setupTitle: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.bold,
    color: t.color.text.primary,
  },
  setupSubtitle: {
    marginTop: t.space[1.5],
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.secondary,
  },
  setupCta: {
    marginTop: t.space[3],
    fontFamily: t.font.sans,
    ...t.type.sm,
    fontWeight: t.weight.bold,
    color: t.color.accent.fg,
  },
  healthPill: {
    alignSelf: 'flex-start',
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: t.borderWidth,
    borderRadius: t.radius.full,
    paddingHorizontal: t.space[3],
    paddingVertical: t.space[1],
  },
  healthDot: {
    marginRight: t.space[2],
  },
  healthText: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    fontWeight: t.weight.bold,
  },
  heroSubtitle: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.secondary,
  },
  heroMetricsRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: t.space[4],
    paddingTop: t.space[3],
    borderTopWidth: t.borderWidth,
    borderTopColor: t.color.border.subtle,
  },
  metricBlock: {
    flex: 1,
    alignItems: 'center',
  },
  metricValue: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    fontWeight: t.weight.bold,
    color: t.color.text.primary,
  },
  metricLabel: {
    marginTop: t.space[1],
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.muted,
    textTransform: 'uppercase',
    letterSpacing: t.tracking.wide,
  },
  metricDivider: {
    width: t.borderWidth,
    height: 28,
    backgroundColor: t.color.border.subtle,
    marginHorizontal: t.space[1],
  },
  filterContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: t.space[3],
  },
  filterText: {
    marginRight: t.space[2],
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.primary,
    flexShrink: 1,
  },
  centeredMessageContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: t.space[5],
    backgroundColor: t.color.surface.page,
  },
  centeredMessageText: {
    marginTop: t.space[3],
    fontFamily: t.font.sans,
    ...t.type.base,
    color: t.color.text.secondary,
    textAlign: 'center',
  },
  disconnectContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: t.space[1],
  },
  connectedText: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.primary,
    flex: 1,
    marginRight: t.space[3],
  },
  noDevicesContainer: {
    padding: t.space[5],
    alignItems: 'center',
  },
  noDevicesText: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.muted,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  retryBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: t.space[3],
    marginBottom: t.space[4],
    backgroundColor: t.color.surface.raised,
    borderRadius: t.radius.lg,
    borderWidth: t.borderWidth,
    borderColor: t.color.status.warning.base,
  },
  retryBannerText: {
    flex: 1,
    marginLeft: t.space[3],
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.status.warning.fg,
    fontWeight: t.weight.medium,
  },
  authWarning: {
    marginBottom: t.space[5],
  },
  bottomNav: {
    position: 'absolute',
    left: t.space[4],
    right: t.space[4],
    bottom: t.space[4],
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: t.space[2],
    backgroundColor: t.color.surface.raised,
    borderWidth: t.borderWidth,
    borderColor: t.color.border.base,
    borderRadius: t.radius.xl,
    padding: t.space[2],
  },
  navItem: {
    flex: 1,
  },
});
