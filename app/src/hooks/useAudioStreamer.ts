import { create } from '@bufbuild/protobuf';
import NetInfo from '@react-native-community/netinfo';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';

import {
  addPlaybackStateListener,
  addRouteChangeListener,
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
import { refreshToken } from '../services/auth';
import { durableAudioSpool, type SpoolPacket } from '../services/durableAudioSpool';

interface UseAudioStreamerOptions {
  onTokenRefreshed?: (token: string) => void;
  autoReconnectEnabled?: boolean;
}

export interface StreamStartConfig {
  phoneVoice?: {
    captureEpoch: number;
    capabilities: VoiceCapabilities;
    restartCapture: () => Promise<NonNullable<StreamStartConfig['phoneVoice']>>;
    stopCapture: () => Promise<void>;
  };
}

interface UseAudioStreamer {
  isStreaming: boolean;
  isConnecting: boolean;
  error: string | null;
  phonePlaybackState: 'started' | 'done' | 'cancelled' | 'failed' | null;
  startStreaming: (url: string, config?: StreamStartConfig) => Promise<void>;
  getWebSocketReadyState: () => number | undefined;
  stopStreaming: () => Promise<void>;
  sendDurableAudio: (audioBytes: Uint8Array) => void;
  sendInteractiveFrame: (frame: CapturedOpusFrame) => void;
}

const HEARTBEAT_MS = 25_000;
const RECONNECT_BASE_MS = 3_000;
const RECONNECT_MAX_MS = 30_000;

function recoveryBatchId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function socketOptions(urlText: string, phone: boolean) {
  const url = new URL(urlText);
  const bearerToken = url.searchParams.get('token') ?? '';
  const displayName = url.searchParams.get('device_name') ?? (phone ? 'phone-mic' : 'omi');
  url.search = '';
  url.pathname = '/ws/audio';
  return {
    url: url.toString(),
    bearerToken,
    sourceId: displayName,
    displayName,
    deviceKind: phone
      ? (Platform.OS === 'ios' ? DeviceKind.IOS_PHONE : DeviceKind.ANDROID_PHONE)
      : DeviceKind.OMI,
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

export const useAudioStreamer = (options?: UseAudioStreamerOptions): UseAudioStreamer => {
  const [isStreaming, setIsStreaming] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [phonePlaybackState, setPhonePlaybackState] = useState<UseAudioStreamer['phonePlaybackState']>(null);
  const socketRef = useRef<AudioV2Socket | null>(null);
  const configRef = useRef<StreamStartConfig | undefined>(undefined);
  const urlRef = useRef('');
  const stoppedRef = useRef(false);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const liveSequenceRef = useRef(0);
  const liveStartedAtRef = useRef(0);
  const nativeClockOriginRef = useRef<number | null>(null);
  const acceptedRef = useRef(new Map<number, SpoolPacket>());
  const acceptedWaitersRef = useRef(new Map<number, () => void>());
  const connectingRef = useRef<Promise<void> | null>(null);
  const deliveryModeRef = useRef<'idle' | 'recovering' | 'live'>('idle');
  const playbackRef = useRef<{
    responseId: string;
    generation: number;
    packets: string[];
    nextSequence: number;
  } | null>(null);
  const playbackSubscriptionRef = useRef<{ remove: () => void } | null>(null);
  const routeSubscriptionRef = useRef<{ remove: () => void } | null>(null);

  const encodeBase64 = useCallback((bytes: Uint8Array) => {
    let binary = '';
    for (let index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return base64.encode(binary);
  }, []);

  const packetAccepted = useCallback((sequence: number) => {
    const packet = acceptedRef.current.get(sequence);
    if (packet) {
      acceptedRef.current.delete(sequence);
      durableAudioSpool.acknowledge(packet).catch(cause => {
        setError(cause instanceof Error ? cause.message : 'Could not retire audio spool packet');
      });
    }
    acceptedWaitersRef.current.get(sequence)?.();
    acceptedWaitersRef.current.delete(sequence);
  }, []);

  const sendSpoolPacket = useCallback((packet: SpoolPacket, sequence: number, nativeMs?: number) => {
    const socket = socketRef.current;
    if (!socket?.activeBinding) return false;
    acceptedRef.current.set(sequence, packet);
    if (nativeMs !== undefined && nativeClockOriginRef.current === null) {
      nativeClockOriginRef.current = nativeMs;
    }
    socket.sendPacket({
      sequence,
      capturedAtMs: packet.capturedAtMs,
      monotonicOffsetUs: Math.max(
        0,
        Math.round((nativeMs === undefined
          ? packet.capturedAtMs - liveStartedAtRef.current
          : nativeMs - nativeClockOriginRef.current!) * 1000)
      ),
      deviceMonotonicTimestampUs: nativeMs === undefined ? undefined : nativeMs * 1000,
      opus: packet.payload,
    });
    return true;
  }, []);

  const drainRecovery = useCallback(async (
    socket: AudioV2Socket,
    captureEpoch: number,
  ) => {
    let recoverySequence = 0;
    while (true) {
      const packets = await durableAudioSpool.pendingPackets();
      if (!packets.length) return;
      await socket.beginCapture({
        captureEpoch,
        processingProfile: ProcessingProfile.SOURCE_NATIVE,
        dataPurpose: DataPurpose.NORMAL_CAPTURE,
        deliveryClass: DeliveryClass.RECOVERED,
        recoveryBatchId: recoveryBatchId(),
      });
      const acknowledgements = packets.map(packet => {
        const sequence = recoverySequence++;
        return new Promise<void>(resolve => {
          acceptedWaitersRef.current.set(sequence, resolve);
          acceptedRef.current.set(sequence, packet);
          socket.sendPacket({
            sequence,
            capturedAtMs: packet.capturedAtMs,
            monotonicOffsetUs: 0,
            opus: packet.payload,
          });
        });
      });
      await Promise.all(acknowledgements);
      await socket.stopCapture();
    }
  }, []);

  const stopStreaming = useCallback(async () => {
    stoppedRef.current = true;
    deliveryModeRef.current = 'idle';
    if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    reconnectRef.current = null;
    heartbeatRef.current = null;
    try {
      await socketRef.current?.stopCapture();
    } finally {
      socketRef.current?.close();
      socketRef.current = null;
      playbackSubscriptionRef.current?.remove();
      playbackSubscriptionRef.current = null;
      routeSubscriptionRef.current?.remove();
      routeSubscriptionRef.current = null;
      playbackRef.current = null;
      await configRef.current?.phoneVoice?.stopCapture();
      durableAudioSpool.close();
      setIsStreaming(false);
      setIsConnecting(false);
    }
  }, []);

  const startStreaming = useCallback(async (
    url: string,
    config?: StreamStartConfig,
  ): Promise<void> => {
    if (connectingRef.current) return connectingRef.current;
    const operation = (async () => {
      stoppedRef.current = false;
      urlRef.current = url;
      configRef.current = config ?? configRef.current;
      setIsConnecting(true);
      setError(null);
      const phoneVoice = configRef.current?.phoneVoice;
      let parsed = socketOptions(url, Boolean(phoneVoice));
      if (!parsed.bearerToken) {
        const token = await refreshToken();
        if (!token) throw new Error('Audio authentication expired');
        options?.onTokenRefreshed?.(token);
        const refreshed = new URL(url);
        refreshed.searchParams.set('token', token);
        urlRef.current = refreshed.toString();
        parsed = socketOptions(urlRef.current, Boolean(phoneVoice));
      }
      const socket = new AudioV2Socket({
        ...parsed,
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
          setIsStreaming(false);
          if (
            !stoppedRef.current &&
            (options?.autoReconnectEnabled ?? true) &&
            !reconnectRef.current
          ) {
            const delay = Math.min(
              RECONNECT_MAX_MS,
              RECONNECT_BASE_MS * (2 ** reconnectAttemptRef.current++)
            );
            reconnectRef.current = setTimeout(() => {
              reconnectRef.current = null;
              startStreaming(urlRef.current).catch(cause => {
                setError(cause instanceof Error ? cause.message : 'Audio reconnect failed');
              });
            }, delay);
          }
        },
      });
      socketRef.current = socket;
      await socket.connect();
      deliveryModeRef.current = 'recovering';
      await drainRecovery(socket, phoneVoice?.captureEpoch ?? 0);
      liveStartedAtRef.current = Date.now();
      nativeClockOriginRef.current = null;
      liveSequenceRef.current = 0;
      const capabilities = phoneVoice
        ? typedCapabilities(phoneVoice.capabilities)
        : undefined;
      await socket.beginCapture({
        captureEpoch: phoneVoice?.captureEpoch ?? 0,
        processingProfile: phoneVoice
          ? processingProfile(phoneVoice.capabilities)
          : ProcessingProfile.SOURCE_NATIVE,
        dataPurpose: DataPurpose.NORMAL_CAPTURE,
        deliveryClass: DeliveryClass.LIVE,
        capabilities,
      });
      deliveryModeRef.current = 'live';
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
        routeSubscriptionRef.current?.remove();
        routeSubscriptionRef.current = addRouteChangeListener(event => {
          if (event.captureEpoch !== phoneVoice.captureEpoch) return;
          stoppedRef.current = true;
          if (heartbeatRef.current) clearInterval(heartbeatRef.current);
          heartbeatRef.current = null;
          socket.stopCapture()
            .catch(() => undefined)
            .then(() => {
              socket.close();
              return phoneVoice.restartCapture();
            })
            .then(restarted => startStreaming(urlRef.current, { phoneVoice: restarted }))
            .catch(cause => {
              setError(cause instanceof Error ? cause.message : 'Audio route restart failed');
            });
        });
      }
      reconnectAttemptRef.current = 0;
      heartbeatRef.current = setInterval(
        () => socket.heartbeat(performance.now()),
        HEARTBEAT_MS,
      );
      setIsConnecting(false);
      setIsStreaming(true);
    })();
    connectingRef.current = operation;
    try {
      await operation;
    } catch (cause) {
      setIsConnecting(false);
      setIsStreaming(false);
      const message = cause instanceof Error ? cause.message : 'Audio V2 connection failed';
      setError(message);
      socketRef.current?.close();
      socketRef.current = null;
      playbackSubscriptionRef.current?.remove();
      playbackSubscriptionRef.current = null;
      routeSubscriptionRef.current?.remove();
      routeSubscriptionRef.current = null;
      deliveryModeRef.current = 'idle';
      throw cause;
    } finally {
      connectingRef.current = null;
    }
  }, [drainRecovery, encodeBase64, options, packetAccepted]);

  const enqueueLive = useCallback((opus: Uint8Array, capturedAtMs: number, nativeMs?: number) => {
    if (!opus.length) return;
    const packet = durableAudioSpool.append(opus, capturedAtMs);
    if (deliveryModeRef.current === 'live') {
      sendSpoolPacket(packet, liveSequenceRef.current++, nativeMs);
    }
  }, [sendSpoolPacket]);

  const sendDurableAudio = useCallback((audioBytes: Uint8Array) => {
    enqueueLive(audioBytes, Date.now());
  }, [enqueueLive]);

  const sendInteractiveFrame = useCallback((frame: CapturedOpusFrame) => {
    enqueueLive(frame.opus, frame.capturedAtMs, frame.monotonicTimestampMs);
  }, [enqueueLive]);

  useEffect(() => {
    const unsubscribe = NetInfo.addEventListener(state => {
      if (
        state.isConnected &&
        state.isInternetReachable &&
        !stoppedRef.current &&
        socketRef.current?.readyState !== WebSocket.OPEN &&
        urlRef.current
      ) {
        startStreaming(urlRef.current).catch(() => undefined);
      }
    });
    return () => {
      unsubscribe();
      stoppedRef.current = true;
      socketRef.current?.close();
    };
  }, [startStreaming]);

  return {
    isStreaming,
    isConnecting,
    error,
    phonePlaybackState,
    startStreaming,
    getWebSocketReadyState: () => socketRef.current?.readyState,
    stopStreaming,
    sendDurableAudio,
    sendInteractiveFrame,
  };
};
