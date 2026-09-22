"""Normalize synthesized WAV audio into Chronicle V2 raw Opus packets."""

from __future__ import annotations

import audioop
import io
import wave
from dataclasses import dataclass

import opuslib

DOWNLINK_SAMPLE_RATE_HZ = 24_000
DOWNLINK_CHANNELS = 1
DOWNLINK_FRAME_MS = 20
DOWNLINK_FRAME_SAMPLES = DOWNLINK_SAMPLE_RATE_HZ * DOWNLINK_FRAME_MS // 1_000
DOWNLINK_FRAME_BYTES = DOWNLINK_FRAME_SAMPLES * 2
DOWNLINK_BITRATE_BPS = 24_000


@dataclass(frozen=True)
class EncodedPlayback:
    packets: tuple[bytes, ...]
    duration_ms: int


def normalize_wav_for_playback(wav_body: bytes) -> bytes:
    """Normalize one WAV to signed PCM16LE mono 24 kHz."""

    try:
        with wave.open(io.BytesIO(wav_body), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            pcm = reader.readframes(frame_count)
    except (EOFError, wave.Error) as error:
        raise ValueError("coordinated response must be a valid WAV") from error
    if channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
        raise ValueError("response WAV must be mono/stereo integer PCM")
    if sample_rate <= 0 or frame_count <= 0:
        raise ValueError("coordinated response WAV must contain audio frames")

    if sample_width == 1:
        pcm = audioop.bias(pcm, 1, -128)
    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
    if sample_width != 2:
        pcm = audioop.lin2lin(pcm, sample_width, 2)
    if sample_rate != DOWNLINK_SAMPLE_RATE_HZ:
        pcm, _state = audioop.ratecv(
            pcm, 2, DOWNLINK_CHANNELS, sample_rate, DOWNLINK_SAMPLE_RATE_HZ, None
        )

    return pcm


class StreamingPlaybackEncoder:
    """One Opus encoder per response; preserve partial frames across producer chunks."""

    def __init__(self):
        self.encoder = opuslib.Encoder(
            DOWNLINK_SAMPLE_RATE_HZ, DOWNLINK_CHANNELS, "audio"
        )
        self.encoder.bitrate = DOWNLINK_BITRATE_BPS
        self.pre_skip_samples = self.encoder.lookahead
        self.pending = bytearray()
        self.total_samples = 0
        self.finished = False

    def append(self, pcm: bytes) -> tuple[bytes, ...]:
        if self.finished:
            raise ValueError("encoder is finished")
        self.pending.extend(pcm)
        packets = []
        while len(self.pending) >= DOWNLINK_FRAME_BYTES:
            frame = bytes(self.pending[:DOWNLINK_FRAME_BYTES])
            del self.pending[:DOWNLINK_FRAME_BYTES]
            packets.append(self.encoder.encode(frame, DOWNLINK_FRAME_SAMPLES))
            self.total_samples += DOWNLINK_FRAME_SAMPLES
        return tuple(packets)

    def finish(self) -> tuple[bytes, ...]:
        if self.finished:
            raise ValueError("encoder is finished")
        if len(self.pending) % 2:
            raise ValueError("PCM stream ends in a partial sample")
        self.finished = True
        self.total_samples += len(self.pending) // 2
        if not self.total_samples:
            return ()
        # Flush the codec's lookahead too: trimming to the logical input length
        # without pre-skip/flush replaces the speech tail with initial codec delay.
        tail = bytes(self.pending) + bytes(self.pre_skip_samples * 2)
        self.pending.clear()
        return tuple(
            self.encoder.encode(
                tail[offset : offset + DOWNLINK_FRAME_BYTES].ljust(
                    DOWNLINK_FRAME_BYTES, b"\0"
                ),
                DOWNLINK_FRAME_SAMPLES,
            )
            for offset in range(0, len(tail), DOWNLINK_FRAME_BYTES)
        )


def encode_wav_for_playback(wav_body: bytes) -> EncodedPlayback:
    """Return 24 kHz mono, 20 ms raw Opus packets for one valid WAV body."""
    pcm = normalize_wav_for_playback(wav_body)
    encoder = StreamingPlaybackEncoder()
    packets = encoder.append(pcm) + encoder.finish()
    return EncodedPlayback(
        packets=packets,
        duration_ms=max(1, round(len(pcm) * 1_000 / 2 / DOWNLINK_SAMPLE_RATE_HZ)),
    )
