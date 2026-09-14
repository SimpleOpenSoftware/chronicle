import { useCallback } from 'react';
import { Alert } from 'react-native';
import { BleAudioCodec, OmiConnection } from 'friend-lite-react-native';
import type { AppSettings } from './useAppSettings';
import type { AudioStreamSource } from './useAudioStreamer';
import type { PhoneCaptureSession } from './usePhoneAudioRecorder';
import type { CapturedOpusFrame } from '../protocol/capturedOpusFrame';
import { phoneAudioDiagnostics } from '../services/phoneAudioDiagnostics';

interface OrchestratorParams {
  omiConnection: OmiConnection;
  deviceConnection: {
    connectedDeviceId: string | null;
  };
  audioStreamer: {
    startStreaming: (url: string, source: AudioStreamSource) => Promise<void>;
    stopStreaming: () => Promise<void>;
    sendFrame: (source: 'phone' | 'wearable', frame: CapturedOpusFrame) => void;
  };
  phoneAudioRecorder: {
    isRecording: boolean;
    startRecording: (
      onData: (frame: CapturedOpusFrame) => Promise<void>
    ) => Promise<PhoneCaptureSession>;
  };
  originalStartAudioListener: (onAudioData: (bytes: Uint8Array) => void) => Promise<void>;
  originalStopAudioListener: () => Promise<void>;
  settings: Pick<AppSettings, 'webSocketUrl'>;
}

export interface AudioOrchestrator {
  isPhoneAudioMode: boolean;
  handleStartAudioListeningAndStreaming: () => Promise<void>;
  handleStopAudioListeningAndStreaming: () => Promise<void>;
  handleTogglePhoneAudio: () => Promise<void>;
}

export const useAudioStreamingOrchestrator = ({
  omiConnection,
  deviceConnection,
  audioStreamer,
  phoneAudioRecorder,
  originalStartAudioListener,
  originalStopAudioListener,
  settings,
}: OrchestratorParams): AudioOrchestrator => {
  const buildAudioWebSocketUrl = useCallback((baseUrl: string): string => {
    let url = baseUrl.trim();
    url = url.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
    const parsed = new URL(url);
    parsed.pathname = '/ws/audio';
    parsed.search = '';
    return parsed.toString();
  }, []);

  const handleStartAudioListeningAndStreaming = useCallback(async () => {
    if (!settings.webSocketUrl?.trim()) {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }
    if (!omiConnection.isConnected() || !deviceConnection.connectedDeviceId) {
      Alert.alert('Device Not Connected', 'Please connect to an OMI device first.');
      return;
    }

    try {
      const codec = await omiConnection.getAudioCodec();
      if (codec !== BleAudioCodec.OPUS) {
        throw new Error(`Wearable must stream Opus; device reported ${codec}`);
      }
      const sourceId = deviceConnection.connectedDeviceId;
      const finalUrl = buildAudioWebSocketUrl(settings.webSocketUrl);
      await originalStartAudioListener(async (audioBytes) => {
        if (audioBytes.length > 0) {
          audioStreamer.sendFrame('wearable', {
            captureEpoch: 0,
            capturedAtMs: Date.now(),
            monotonicTimestampMs: performance.now(),
            frameDurationMs: 60,
            opus: audioBytes,
          });
        }
      });
      await audioStreamer.startStreaming(finalUrl, { kind: 'wearable', sourceId });
    } catch (error) {
      Alert.alert('Error', 'Could not start audio listening or streaming.');
      await originalStopAudioListener();
      await audioStreamer.stopStreaming();
    }
  }, [originalStartAudioListener, audioStreamer, settings.webSocketUrl, omiConnection, deviceConnection.connectedDeviceId, buildAudioWebSocketUrl]);

  const handleStopAudioListeningAndStreaming = useCallback(async () => {
    await originalStopAudioListener();
    await audioStreamer.stopStreaming();
  }, [originalStopAudioListener, audioStreamer]);

  const handleStartPhoneAudioStreaming = useCallback(async () => {
    if (!settings.webSocketUrl?.trim()) {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }

    try {
      const finalUrl = buildAudioWebSocketUrl(settings.webSocketUrl);
      const capture = await phoneAudioRecorder.startRecording(async (frame) => {
        if (frame.opus.length === 0) return;
        audioStreamer.sendFrame('phone', frame);
      });
      await audioStreamer.startStreaming(finalUrl, { kind: 'phone', ...capture });
    } catch (error) {
      phoneAudioDiagnostics.failure('orchestrator_start', error);
      Alert.alert('Error', 'Could not start phone audio streaming.');
      await audioStreamer.stopStreaming();
    }
  }, [audioStreamer, phoneAudioRecorder, settings.webSocketUrl, buildAudioWebSocketUrl]);

  const handleStopPhoneAudioStreaming = useCallback(async () => {
    await audioStreamer.stopStreaming();
    phoneAudioDiagnostics.stopped();
  }, [audioStreamer]);

  const handleTogglePhoneAudio = useCallback(async () => {
    if (phoneAudioRecorder.isRecording) {
      await handleStopPhoneAudioStreaming();
    } else {
      phoneAudioDiagnostics.beginAttempt();
      await handleStartPhoneAudioStreaming();
    }
  }, [phoneAudioRecorder.isRecording, handleStartPhoneAudioStreaming, handleStopPhoneAudioStreaming]);

  return {
    isPhoneAudioMode: phoneAudioRecorder.isRecording,
    handleStartAudioListeningAndStreaming,
    handleStopAudioListeningAndStreaming,
    handleTogglePhoneAudio,
  };
};
