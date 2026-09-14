import { create } from '@bufbuild/protobuf';
import { useCallback, useRef, useState } from 'react';
import { Platform } from 'react-native';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';

import {
  addPlaybackStateListener,
  cancelResponse,
  scheduleResponse,
} from '../../modules/chronicle-duplex-audio';

import {
  CaptureCapabilitiesSchema,
  DataPurpose,
  DeliveryClass,
  DeviceKind,
  DuplexMode,
  EffectStatusSchema,
  InputRoute,
  OutputRoute,
  PlaybackState,
  ProcessingProfile,
} from '../protocol/audioV2';
import { AudioV2Socket } from '../protocol/audioV2Socket';
import type { CapturedOpusFrame } from '../protocol/capturedOpusFrame';
import type { VoiceCapabilities } from '../protocol/audioCapabilities';
import type { PhoneCaptureSession } from './usePhoneAudioRecorder';
import { getValidToken } from '../services/auth';
import { phoneAudioDiagnostics } from '../services/phoneAudioDiagnostics';

export type AudioStreamSource =
  | {
    kind: 'wearable';
    sourceId: string;
  }
  | ({ kind: 'phone' } & PhoneCaptureSession);

interface UseAudioStreamer {
  isStreaming: boolean;
  isConnecting: boolean;
  error: string | null;
  phonePlaybackState: 'started' | 'done' | 'cancelled' | 'failed' | null;
  startStreaming: (url: string, source: AudioStreamSource) => Promise<void>;
  stopStreaming: () => Promise<void>;
  sendFrame: (source: AudioStreamSource['kind'], frame: CapturedOpusFrame) => void;
}

const HEARTBEAT_MS = 25_000;

function socketSource(source: AudioStreamSource) {
  if (source.kind === 'wearable') {
    return {
      sourceId: source.sourceId,
      displayName: 'wearable',
      deviceKind: DeviceKind.OMI,
      uplinkFrameDurationMs: 60 as const,
    };
  }
  return {
    sourceId: 'phone-mic',
    displayName: 'phone-mic',
    deviceKind: Platform.OS === 'ios' ? DeviceKind.IOS_PHONE : DeviceKind.ANDROID_PHONE,
    uplinkFrameDurationMs: 20 as const,
  };
}

const inputRoutes: Record<VoiceCapabilities['input_route'], InputRoute> = {
  built_in_mic: InputRoute.BUILT_IN_MIC,
  bluetooth_hfp: InputRoute.BLUETOOTH_HFP,
  wired_mic: InputRoute.WIRED_MIC,
  usb: InputRoute.USB,
  unknown: InputRoute.REMOTE,
};

const outputRoutes: Record<VoiceCapabilities['output_route'], OutputRoute> = {
  speakerphone: OutputRoute.SPEAKERPHONE,
  earpiece: OutputRoute.EARPIECE,
  headphones: OutputRoute.HEADPHONES,
  bluetooth_hfp: OutputRoute.BLUETOOTH_HFP,
  usb: OutputRoute.USB,
  remote: OutputRoute.REMOTE,
  unknown: OutputRoute.REMOTE,
};

function typedCapabilities(capabilities: VoiceCapabilities) {
  const effect = (value: VoiceCapabilities['aec']) => create(EffectStatusSchema, value);
  return create(CaptureCapabilitiesSchema, {
    duplexMode: capabilities.mode === 'duplex_full'
      ? DuplexMode.FULL
      : capabilities.mode === 'duplex_isolated'
        ? DuplexMode.ISOLATED
        : DuplexMode.HALF,
    inputRoute: inputRoutes[capabilities.input_route],
    outputRoute: outputRoutes[capabilities.output_route],
    nativeSampleRateHz: capabilities.native_sample_rate,
    acousticEchoCancellation: effect(capabilities.aec),
    noiseSuppression: effect(capabilities.noise_suppression),
  });
}

function processingProfile(capabilities: VoiceCapabilities): ProcessingProfile {
  if (capabilities.mode === 'duplex_full') return ProcessingProfile.DUPLEX_AEC;
  if (capabilities.mode === 'duplex_isolated') return ProcessingProfile.DUPLEX_ISOLATED;
  return ProcessingProfile.HALF_DUPLEX;
}

export const useAudioStreamer = (): UseAudioStreamer => {
  const [isStreaming, setIsStreaming] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [phonePlaybackState, setPhonePlaybackState] = useState<UseAudioStreamer['phonePlaybackState']>(null);
  const socketRef = useRef<AudioV2Socket | null>(null);
  const sourceRef = useRef<AudioStreamSource | null>(null);
  const stoppedRef = useRef(false);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const liveSequenceRef = useRef(0);
  const liveMonotonicOriginRef = useRef<number | null>(null);
  const playbackRef = useRef<{
    responseId: string;
    generation: number;
    packets: string[];
    nextSequence: number;
  } | null>(null);
  const playbackSubscriptionRef = useRef<{ remove: () => void } | null>(null);

  const encodeBase64 = useCallback((bytes: Uint8Array) => {
    let binary = '';
    for (let index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return base64.encode(binary);
  }, []);

  const packetAccepted = useCallback((sequence: number) => {
    phoneAudioDiagnostics.packetAccepted(sequence);
  }, []);

  const stopStreaming = useCallback(async () => {
    stoppedRef.current = true;
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    heartbeatRef.current = null;
    const socket = socketRef.current;
    try {
      if (sourceRef.current?.kind === 'phone') {
        await sourceRef.current.stopCapture();
      }
      await socket?.stopCapture();
    } finally {
      socket?.close();
      socketRef.current = null;
      playbackSubscriptionRef.current?.remove();
      playbackSubscriptionRef.current = null;
      playbackRef.current = null;
      sourceRef.current = null;
      setIsStreaming(false);
      setIsConnecting(false);
    }
  }, []);

  const startStreaming = useCallback(async (
    url: string,
    source: AudioStreamSource,
  ): Promise<void> => {
    stoppedRef.current = false;
    sourceRef.current = source;
    setIsConnecting(true);
    setError(null);
    try {
      const phoneVoice = source.kind === 'phone' ? source : null;
      const token = await getValidToken();
      if (!token) throw new Error('Audio authentication expired');
      const socket = new AudioV2Socket({
        url,
        bearerToken: token,
        ...socketSource(source),
        onPacketAccepted: packetAccepted,
        onControl: control => {
          if (control.event.case === 'playbackOffer') {
            const offer = control.event.value;
            playbackRef.current = {
              responseId: offer.responseId?.value ?? '',
              generation: Number(offer.generation),
              packets: [],
              nextSequence: 0,
            };
          } else if (control.event.case === 'cancelPlayback') {
            const cancellation = control.event.value;
            cancelResponse(
              cancellation.responseId?.value ?? '',
              Number(cancellation.generation),
            ).catch(() => undefined);
          }
        },
        onPlaybackPacket: packet => {
          const playback = playbackRef.current;
          if (
            !playback ||
            playback.responseId !== packet.responseId?.value ||
            playback.generation !== Number(packet.generation) ||
            playback.nextSequence !== Number(packet.sequence)
          ) {
            setError('Rejected an out-of-order audio V2 playback packet');
            return;
          }
          playback.packets.push(encodeBase64(packet.opusPayload));
          playback.nextSequence += 1;
          if (packet.finalPacket && phoneVoice) {
            playbackRef.current = null;
            scheduleResponse({
              responseId: playback.responseId,
              generation: playback.generation,
              captureEpoch: phoneVoice.captureEpoch,
              opusPacketsBase64: playback.packets,
            }).catch(cause => {
              setError(cause instanceof Error ? cause.message : 'Opus playback failed');
              socket.acknowledgePlayback(
                playback.responseId,
                playback.generation,
                PlaybackState.FAILED,
                performance.now(),
              );
            });
          }
        },
        onClosed: () => {
          if (phoneVoice) phoneAudioDiagnostics.socketClosed(stoppedRef.current);
          setIsStreaming(false);
          if (!stoppedRef.current) setError('Audio connection closed');
        },
        onDiagnostic: event => {
          if (phoneVoice) phoneAudioDiagnostics.socketStage(event.stage, event.detail);
        },
      });
      socketRef.current = socket;
      if (phoneVoice) phoneAudioDiagnostics.socketConnecting();
      await socket.connect();
      if (phoneVoice) phoneAudioDiagnostics.socketOpen();
      liveMonotonicOriginRef.current = null;
      liveSequenceRef.current = 0;
      const capabilities = phoneVoice
        ? typedCapabilities(phoneVoice.capabilities)
        : undefined;
      const binding = await socket.beginCapture({
        captureEpoch: phoneVoice?.captureEpoch ?? 0,
        processingProfile: phoneVoice
          ? processingProfile(phoneVoice.capabilities)
          : ProcessingProfile.SOURCE_NATIVE,
        dataPurpose: DataPurpose.NORMAL_CAPTURE,
        deliveryClass: DeliveryClass.LIVE,
        capabilities,
      });
      if (phoneVoice) {
        phoneAudioDiagnostics.captureStarted(binding.captureSessionId?.value ?? '');
      }
      if (capabilities) socket.voiceReady(capabilities);
      if (phoneVoice) {
        playbackSubscriptionRef.current?.remove();
        playbackSubscriptionRef.current = addPlaybackStateListener(event => {
          if (event.captureEpoch !== phoneVoice.captureEpoch) return;
          setPhonePlaybackState(event.state);
          const state = {
            started: PlaybackState.STARTED,
            done: PlaybackState.DONE,
            cancelled: PlaybackState.CANCELLED,
            failed: PlaybackState.FAILED,
          }[event.state];
          socket.acknowledgePlayback(
            event.responseId,
            event.generation,
            state,
            event.monotonicTimestampMs,
          );
        });
      }
      heartbeatRef.current = setInterval(
        () => socket.heartbeat(performance.now()),
        HEARTBEAT_MS,
      );
      setIsConnecting(false);
      setIsStreaming(true);
    } catch (cause) {
      if (source.kind === 'phone') phoneAudioDiagnostics.failure('websocket_start', cause);
      setIsConnecting(false);
      setIsStreaming(false);
      const message = cause instanceof Error ? cause.message : 'Audio V2 connection failed';
      setError(message);
      socketRef.current?.close();
      socketRef.current = null;
      playbackSubscriptionRef.current?.remove();
      playbackSubscriptionRef.current = null;
      throw cause;
    }
  }, [encodeBase64, packetAccepted]);

  const sendFrame = useCallback((
    source: AudioStreamSource['kind'],
    frame: CapturedOpusFrame,
  ) => {
    if (stoppedRef.current || !frame.opus.length) return;
    const activeSource = sourceRef.current;
    const socket = socketRef.current;
    if (!socket?.activeBinding || activeSource?.kind !== source) return;
    if (source === 'phone') {
      if (activeSource.kind !== 'phone' || frame.captureEpoch !== activeSource.captureEpoch) return;
      phoneAudioDiagnostics.frameSent(frame.opus.length);
    }
    liveMonotonicOriginRef.current ??= frame.monotonicTimestampMs;
    socket.sendPacket({
      sequence: liveSequenceRef.current++,
      capturedAtMs: frame.capturedAtMs,
      monotonicOffsetUs: Math.max(
        0,
        Math.round(
          (frame.monotonicTimestampMs - liveMonotonicOriginRef.current) * 1000,
        ),
      ),
      deviceMonotonicTimestampUs: frame.monotonicTimestampMs * 1000,
      opus: frame.opus,
    });
  }, []);

  return {
    isStreaming,
    isConnecting,
    error,
    phonePlaybackState,
    startStreaming,
    stopStreaming,
    sendFrame,
  };
};
