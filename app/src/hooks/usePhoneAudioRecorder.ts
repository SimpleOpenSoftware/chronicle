import { useCallback, useEffect, useRef, useState } from 'react';
import { PermissionsAndroid, Platform } from 'react-native';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';

import {
  addCaptureDiagnosticListener,
  addOpusFrameListener,
  startVoiceSession,
  stopVoiceSession,
} from '../../modules/chronicle-duplex-audio';
import type { VoiceCapabilities } from '../protocol/audioCapabilities';
import {
  capturedOpusFrameFromNative,
  type CapturedOpusFrame,
} from '../protocol/capturedOpusFrame';
import { phoneAudioDiagnostics } from '../services/phoneAudioDiagnostics';

const FIRST_FRAME_TIMEOUT_MS = 3_000;
const AUDIO_LEVEL_TIMEOUT_MS = 5_000;
const ACTIVE_AUDIO_LEVEL = 0.01;

export interface PhoneCaptureSession {
  captureEpoch: number;
  capabilities: VoiceCapabilities;
  stopCapture: () => Promise<void>;
}

interface UsePhoneAudioRecorder {
  isRecording: boolean;
  isInitializing: boolean;
  error: string | null;
  audioLevel: number;
  startRecording: (
    onAudioData: (frame: CapturedOpusFrame) => void
  ) => Promise<PhoneCaptureSession>;
  stopRecording: () => Promise<void>;
}

function decodeBase64(value: string): Uint8Array {
  const binary = base64.decode(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export const usePhoneAudioRecorder = (): UsePhoneAudioRecorder => {
  const [isRecording, setIsRecording] = useState(false);
  const [isInitializing, setIsInitializing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audioLevel, setAudioLevel] = useState(0);
  const mountedRef = useRef(true);
  const captureEpochRef = useRef(0);
  const frameSubscriptionRef = useRef<{ remove: () => void } | null>(null);
  const diagnosticSubscriptionRef = useRef<{ remove: () => void } | null>(null);
  const onAudioDataRef = useRef<((frame: CapturedOpusFrame) => void) | null>(null);
  const firstFrameSeenRef = useRef(false);
  const firstFrameTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const audioLevelActiveRef = useRef(false);
  const audioLevelTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markCaptureStopped = useCallback(() => {
    if (firstFrameTimeoutRef.current) clearTimeout(firstFrameTimeoutRef.current);
    firstFrameTimeoutRef.current = null;
    if (audioLevelTimeoutRef.current) clearTimeout(audioLevelTimeoutRef.current);
    audioLevelTimeoutRef.current = null;
    frameSubscriptionRef.current?.remove();
    frameSubscriptionRef.current = null;
    diagnosticSubscriptionRef.current?.remove();
    diagnosticSubscriptionRef.current = null;
    onAudioDataRef.current = null;
    if (mountedRef.current) {
      setIsRecording(false);
      setIsInitializing(false);
      setAudioLevel(0);
    }
  }, []);

  const stopRecording = useCallback(async () => {
    try {
      await stopVoiceSession();
    } finally {
      markCaptureStopped();
    }
  }, [markCaptureStopped]);

  const startNativeCapture = useCallback(async (): Promise<PhoneCaptureSession> => {
    if (firstFrameTimeoutRef.current) clearTimeout(firstFrameTimeoutRef.current);
    firstFrameTimeoutRef.current = null;
    if (audioLevelTimeoutRef.current) clearTimeout(audioLevelTimeoutRef.current);
    audioLevelTimeoutRef.current = null;
    const captureEpoch = captureEpochRef.current + 1;
    captureEpochRef.current = captureEpoch;
    const capabilities = await startVoiceSession({ captureEpoch });
    phoneAudioDiagnostics.engineStarted(captureEpoch, capabilities);
    if (!firstFrameSeenRef.current) {
      firstFrameTimeoutRef.current = setTimeout(() => {
        phoneAudioDiagnostics.timeout('native_frame_timeout');
        if (mountedRef.current) {
          setError('Microphone started, but Chronicle received no audio frames.');
        }
      }, FIRST_FRAME_TIMEOUT_MS);
    }
    if (!audioLevelActiveRef.current) {
      audioLevelTimeoutRef.current = setTimeout(() => {
        phoneAudioDiagnostics.timeout('audio_level_stalled');
      }, AUDIO_LEVEL_TIMEOUT_MS);
    }
    return {
      captureEpoch,
      capabilities,
      stopCapture: stopRecording,
    };
  }, [stopRecording]);

  const startRecording = useCallback(async (
    onAudioData: (frame: CapturedOpusFrame) => void
  ): Promise<PhoneCaptureSession> => {
    if (isRecording) await stopRecording();
    if (mountedRef.current) {
      setIsInitializing(true);
      setError(null);
    }
    if (Platform.OS === 'android') {
      const permission = await PermissionsAndroid.request(
        PermissionsAndroid.PERMISSIONS.RECORD_AUDIO
      );
      if (permission !== PermissionsAndroid.RESULTS.GRANTED) {
        throw new Error('Microphone permission denied');
      }
    }

    try {
      firstFrameSeenRef.current = false;
      audioLevelActiveRef.current = false;
      onAudioDataRef.current = onAudioData;
      phoneAudioDiagnostics.listenerInstalled(captureEpochRef.current + 1);
      diagnosticSubscriptionRef.current = addCaptureDiagnosticListener((event) => {
        if (!mountedRef.current || event.captureEpoch !== captureEpochRef.current) return;
        phoneAudioDiagnostics.nativeStage(event);
      });
      frameSubscriptionRef.current = addOpusFrameListener((frame) => {
        if (!mountedRef.current || frame.captureEpoch !== captureEpochRef.current) return;
        let captured: CapturedOpusFrame;
        try {
          captured = capturedOpusFrameFromNative(frame, decodeBase64);
        } catch (cause) {
          phoneAudioDiagnostics.invalidNativeFrame(
            cause instanceof Error ? cause.message : 'invalid_native_frame',
          );
          return;
        }
        const level = Math.min(1, Math.max(0, frame.audioLevel || 0));
        phoneAudioDiagnostics.nativeFrame({
          captureEpoch: frame.captureEpoch,
          opusBytes: captured.opus.length,
          audioLevel: level,
        });
        if (!firstFrameSeenRef.current) {
          firstFrameSeenRef.current = true;
          if (firstFrameTimeoutRef.current) clearTimeout(firstFrameTimeoutRef.current);
          firstFrameTimeoutRef.current = null;
          setError(null);
        }
        if (!audioLevelActiveRef.current && level >= ACTIVE_AUDIO_LEVEL) {
          audioLevelActiveRef.current = true;
          if (audioLevelTimeoutRef.current) clearTimeout(audioLevelTimeoutRef.current);
          audioLevelTimeoutRef.current = null;
          phoneAudioDiagnostics.audioLevelActive(level);
        }
        setAudioLevel(previous => previous === 0 ? level : (previous * 0.65) + (level * 0.35));
        onAudioDataRef.current?.(captured);
      });
      const capture = await startNativeCapture();
      if (mountedRef.current) {
        setIsRecording(true);
        setIsInitializing(false);
      }
      return capture;
    } catch (cause) {
      phoneAudioDiagnostics.failure('native_capture_start', cause);
      frameSubscriptionRef.current?.remove();
      frameSubscriptionRef.current = null;
      diagnosticSubscriptionRef.current?.remove();
      diagnosticSubscriptionRef.current = null;
      const message = cause instanceof Error ? cause.message : 'Failed to start duplex audio';
      if (mountedRef.current) {
        setError(message);
        setIsInitializing(false);
        setIsRecording(false);
      }
      throw cause;
    }
  }, [isRecording, startNativeCapture, stopRecording]);

  useEffect(() => () => {
    mountedRef.current = false;
    frameSubscriptionRef.current?.remove();
    frameSubscriptionRef.current = null;
    diagnosticSubscriptionRef.current?.remove();
    diagnosticSubscriptionRef.current = null;
    if (firstFrameTimeoutRef.current) clearTimeout(firstFrameTimeoutRef.current);
    if (audioLevelTimeoutRef.current) clearTimeout(audioLevelTimeoutRef.current);
    stopVoiceSession().catch(() => undefined);
  }, []);

  return {
    isRecording,
    isInitializing,
    error,
    audioLevel,
    startRecording,
    stopRecording,
  };
};
