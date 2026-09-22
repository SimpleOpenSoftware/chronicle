/** Incremental playback transport; the existing native module owns audio and its clock. */
import type { CaptureBinding, PlaybackOffer, PlaybackMediaPacket, PlaybackFinished, CancelPlayback } from './audioV2';
import { AudioCodec } from './audioV2';
import type { NativePlaybackState, NativeResponseBinding, NativeResponseOffer, NativeResponsePacket, NativeResponseFinish } from '../../modules/chronicle-duplex-audio';

export interface NativePlaybackPort {
  beginResponse(value: NativeResponseOffer): Promise<void>;
  appendResponse(value: NativeResponsePacket): Promise<void>;
  finishResponse(value: NativeResponseFinish): Promise<void>;
  cancelResponse(id: string, generation: number): Promise<void>;
}
interface Response {
  binding: NativeResponseBinding;
  sequence: number;
  rendered: number;
  finishing: boolean;
}
function sameBinding(a: CaptureBinding, b?: CaptureBinding): boolean {
  return a.captureSessionId?.value === b?.captureSessionId?.value && a.voiceSessionId?.value === b?.voiceSessionId?.value && a.captureEpoch === b?.captureEpoch;
}

export class NativePlayback {
  private active: Response | null = null;
  private work: Promise<void> = Promise.resolve();
  private generationFloor = -1;
  private seen = new Set<string>();
  private closed = false;
  constructor(private binding: CaptureBinding, private port: NativePlaybackPort,
    private base64: (bytes: Uint8Array) => string,
    private report: (state: NativePlaybackState) => void,
    private failed: (error: Error) => void) {}

  private reject(response: Response, cause: unknown): void {
    this.failed(cause instanceof Error ? cause : new Error(String(cause)));
    this.report({ ...response.binding, state: 'failed', monotonicTimestampMs: 0,
      renderedSamples: response.rendered, bufferedSamples: 0, errorCode: 'decode_failed' });
    if (this.active === response) this.cancelCurrent();
  }

  private enqueue(response: Response, operation: () => Promise<void>): void {
    this.work = this.work.then(async () => {
      if (this.active !== response || this.closed) return;
      try { await operation(); }
      catch (cause) {
        this.reject(response, cause);
      }
    });
  }
  open(offer: PlaybackOffer): void {
    if (this.closed || !sameBinding(this.binding, offer.binding)) return;
    const generation = Number(offer.generation), id = offer.responseId?.value;
    if (!id || generation < this.generationFloor || this.seen.has(id)) return;
    if (!offer.incremental || offer.audioSpec?.codec !== AudioCodec.OPUS || offer.audioSpec.sampleRateHz !== 24000 ||
      offer.audioSpec.channelCount !== 1 || offer.audioSpec.frameDuration?.seconds !== 0n || offer.audioSpec.frameDuration.nanos !== 20000000 ||
      offer.preSkipSamples > 48000 || !Number.isSafeInteger(generation)) {
      this.failed(new Error('Native playback requires incremental 24 kHz mono Opus.'));
      return;
    }
    this.cancelCurrent();
    if (generation > this.generationFloor) this.seen.clear();
    if (this.seen.size >= 256) { this.failed(new Error('Too many responses in one generation')); return; }
    this.generationFloor = generation;
    this.seen.add(id);
    const response: Response = { binding: { responseId: id, generation, captureEpoch: Number(this.binding.captureEpoch) }, sequence: 0, rendered: 0, finishing: false };
    this.active = response;
    this.enqueue(response, () => this.port.beginResponse({ ...response.binding, preSkipSamples: offer.preSkipSamples }));
  }
  append(packet: PlaybackMediaPacket): void {
    const response = this.active;
    if (!response || response.binding.responseId !== packet.responseId?.value || response.binding.generation !== Number(packet.generation)) return;
    if (response.finishing || packet.sequence !== BigInt(response.sequence) || packet.finalPacket || !packet.opusPayload.length ||
      (response.sequence + 1) * 480 - response.rendered > 48000) {
      this.reject(response, new Error('Invalid playback order or buffer limit'));
      return;
    }
    const value = { ...response.binding, sequence: response.sequence++, opusBase64: this.base64(packet.opusPayload) };
    this.enqueue(response, () => this.port.appendResponse(value));
  }
  finish(value: PlaybackFinished): void {
    const response = this.active;
    if (!response || !sameBinding(this.binding, value.binding) || response.binding.responseId !== value.responseId?.value || response.binding.generation !== Number(value.generation) || response.finishing) return;
    const totalSamples = Number(value.totalSamples);
    if (!Number.isSafeInteger(totalSamples) || totalSamples < 0) { this.reject(response, new Error('Invalid playback finish')); return; }
    response.finishing = true;
    this.enqueue(response, () => this.port.finishResponse({ ...response.binding, totalSamples }));
  }
  progress(event: NativePlaybackState): void {
    if (event.captureEpoch !== Number(this.binding.captureEpoch)) return;
    const response = this.active;
    if (response && event.responseId === response.binding.responseId && event.generation === response.binding.generation) {
      if (event.renderedSamples < response.rendered || event.renderedSamples > response.sequence * 480) { this.reject(response, new Error('Invalid rendered position')); return; }
      response.rendered = event.renderedSamples;
      if (['done', 'cancelled', 'failed'].includes(event.state)) this.active = null;
    }
    this.report(event);
  }
  cancel(value: CancelPlayback): void {
    if (!sameBinding(this.binding, value.binding)) return;
    const generation = Number(value.generation);
    this.generationFloor = Math.max(this.generationFloor, generation);
    if (value.responseId?.value) this.seen.add(value.responseId.value);
    if (this.active && (value.responseId?.value === '*' || value.responseId?.value === this.active.binding.responseId) && generation >= this.active.binding.generation) this.cancelCurrent();
  }
  cancelCurrent(): void {
    const response = this.active;
    this.active = null;
    if (response) this.work = this.work.then(() => this.port.cancelResponse(response.binding.responseId, response.binding.generation)).catch(cause => this.failed(cause));
  }
  close(): void { this.closed = true; this.cancelCurrent(); }
}
