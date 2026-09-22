export type VoiceMode = 'duplex_full' | 'duplex_isolated' | 'duplex_half';
export type InputRoute = 'built_in_mic' | 'bluetooth_hfp' | 'wired_mic' | 'usb' | 'unknown';
export type OutputRoute = 'speakerphone' | 'earpiece' | 'headphones' | 'bluetooth_hfp' | 'usb' | 'remote' | 'unknown';

export interface EffectStatus {
  requested: boolean;
  available: boolean;
  enabled: boolean;
}

export interface VoiceCapabilities {
  mode: VoiceMode;
  input_route: InputRoute;
  output_route: OutputRoute;
  native_sample_rate: number;
  incremental_playback: boolean;
  aec: EffectStatus;
  noise_suppression: EffectStatus;
  fallback_reason: 'aec_unavailable' | 'aec_unhealthy' | 'route_not_isolated' |
    'unsupported_route' | 'platform_unavailable' | null;
}
