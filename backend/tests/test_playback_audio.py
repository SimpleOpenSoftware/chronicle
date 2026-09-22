import io
import wave

import opuslib

from backend.services.playback_audio import encode_wav_for_playback


def _wav(*, sample_rate: int, channels: int, frames: int) -> bytes:
    body = io.BytesIO()
    with wave.open(body, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(frames * channels * 2))
    return body.getvalue()


def test_wav_is_normalized_to_decodable_24khz_20ms_raw_opus_packets():
    encoded = encode_wav_for_playback(
        _wav(sample_rate=16_000, channels=2, frames=1_600)
    )
    decoder = opuslib.Decoder(24_000, 1)

    assert encoded.duration_ms == 100
    assert (
        len(encoded.packets) == 6
    )  # Includes a final packet flushing codec lookahead.
    assert all(not packet.startswith(b"OggS") for packet in encoded.packets)
    assert all(len(decoder.decode(packet, 480)) == 960 for packet in encoded.packets)


def test_incremental_opus_preskip_and_flush_preserve_last_speech_samples():
    import numpy as np

    from backend.services.playback_audio import StreamingPlaybackEncoder

    samples = (12000 * np.sin(np.arange(4800) * 2 * np.pi * 523 / 24000)).astype("<i2")
    encoder = StreamingPlaybackEncoder()
    packets = encoder.append(samples.tobytes()) + encoder.finish()
    decoder = opuslib.Decoder(24000, 1)
    decoded = np.frombuffer(
        b"".join(decoder.decode(packet, 480) for packet in packets), dtype="<i2"
    )
    assert encoder.total_samples == len(samples)
    assert len(decoded) >= len(samples) + encoder.pre_skip_samples
    heard = decoded[encoder.pre_skip_samples : encoder.pre_skip_samples + len(samples)]
    assert len(heard) == len(samples)
    assert np.corrcoef(heard[-480:], samples[-480:])[0, 1] > 0.98
