"""Robot Framework adapter for Chronicle's audio-v2 WebSocket contract."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import opuslib

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "extras/chronicle-client"),
    str(ROOT / "backend/src"),
]

from audio_contract.v2 import audio_pb2
from chronicle_client.audio_v2 import AudioV2Client


def _websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/ws/audio", "", "", ""))


@dataclass
class _Session:
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread
    client: AudioV2Client
    chunks_sent: int = 0
    audio_stopped: bool = False


class StreamManager:
    """Keep long-running audio-v2 sockets available to synchronous Robot keywords."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    @staticmethod
    def _submit(session: _Session, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, session.loop).result(
            timeout=15
        )

    def start_stream(
        self,
        base_url: str,
        token: str,
        device_name: str = "robot-test",
        recording_mode: str = "streaming",
    ) -> str:
        del recording_mode
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        client = AudioV2Client(
            websocket_url=_websocket_url(base_url),
            bearer_token=token,
            source_id=device_name,
            display_name=device_name,
            device_kind=audio_pb2.DEVICE_KIND_PROBE,
            uplink_frame_duration_ms=20,
        )
        session = _Session(loop=loop, thread=thread, client=client)
        stream_id = str(uuid.uuid4())
        self._sessions[stream_id] = session

        async def open_capture() -> None:
            await client.connect()
            await client.start_capture(capture_epoch=0)

        try:
            self._submit(session, open_capture())
        except Exception:
            self._shutdown(stream_id)
            raise
        return stream_id

    def send_chunks_from_file(
        self,
        stream_id: str,
        wav_path: str,
        num_chunks: Optional[int] = None,
        chunk_duration_ms: int = 100,
        realtime_pacing: bool = False,
    ) -> int:
        del chunk_duration_ms
        session = self._require(stream_id)
        if session.audio_stopped:
            raise RuntimeError("capture is already stopped")
        with wave.open(wav_path, "rb") as reader:
            shape = (
                reader.getframerate(),
                reader.getnchannels(),
                reader.getsampwidth(),
            )
            if shape != (16_000, 1, 2):
                raise ValueError(
                    "audio-v2 Robot fixtures must be 16 kHz mono PCM16 WAV"
                )
            encoder = opuslib.Encoder(16_000, 1, opuslib.APPLICATION_AUDIO)
            encoder.bitrate = 24_000
            sent = 0
            while num_chunks is None or sent < int(num_chunks):
                pcm = reader.readframes(320)
                if not pcm:
                    break
                pcm = pcm.ljust(640, b"\0")
                self._submit(
                    session, session.client.send_opus(encoder.encode(pcm, 320))
                )
                sent += 1
                session.chunks_sent += 1
                if realtime_pacing:
                    time.sleep(0.02)
        return sent

    def send_audio_stop(self, stream_id: str) -> None:
        session = self._require(stream_id)
        self._submit(session, session.client.stop_capture())
        session.audio_stopped = True

    def stop_stream(self, stream_id: str) -> int:
        session = self._require(stream_id)
        if not session.audio_stopped:
            self.send_audio_stop(stream_id)
        total = session.chunks_sent
        self._submit(session, session.client.close())
        self._shutdown(stream_id)
        return total

    def close_stream_without_stop(self, stream_id: str) -> int:
        session = self._require(stream_id)

        async def abrupt_close() -> None:
            session.client.binding = None
            await session.client.close()

        total = session.chunks_sent
        self._submit(session, abrupt_close())
        self._shutdown(stream_id)
        return total

    def send_button_event(self, stream_id: str, button_state: str) -> None:
        state = {
            "SINGLE_PRESS": audio_pb2.BUTTON_STATE_SINGLE_PRESS,
            "DOUBLE_PRESS": audio_pb2.BUTTON_STATE_DOUBLE_PRESS,
            "LONG_PRESS": audio_pb2.BUTTON_STATE_LONG_PRESS,
        }.get(button_state.upper())
        if state is None:
            raise ValueError(f"unsupported button state: {button_state}")
        session = self._require(stream_id)
        self._submit(session, session.client.send_button(state))

    def cleanup_all(self) -> None:
        for stream_id in list(self._sessions):
            try:
                self.stop_stream(stream_id)
            except Exception:
                self._shutdown(stream_id)

    def _require(self, stream_id: str) -> _Session:
        try:
            return self._sessions[stream_id]
        except KeyError as error:
            raise ValueError(f"Stream {stream_id} not found") from error

    def _shutdown(self, stream_id: str) -> None:
        session = self._sessions.pop(stream_id, None)
        if session is None:
            return
        session.loop.call_soon_threadsafe(session.loop.stop)
        session.thread.join(timeout=5)
        session.loop.close()


_manager = StreamManager()


def stream_audio_file(
    base_url: str,
    token: str,
    wav_path: str,
    device_name: str = "robot-test",
    recording_mode: str = "streaming",
    use_wyoming: bool | None = None,
) -> int:
    if use_wyoming:
        raise ValueError("Wyoming audio transport was removed; use Chronicle audio v2")
    stream_id = _manager.start_stream(base_url, token, device_name, recording_mode)
    _manager.send_chunks_from_file(stream_id, wav_path)
    return _manager.stop_stream(stream_id)


def start_audio_stream(
    base_url: str,
    token: str,
    device_name: str = "robot-test",
    recording_mode: str = "streaming",
) -> str:
    return _manager.start_stream(base_url, token, device_name, recording_mode)


def send_audio_chunks(
    stream_id: str,
    wav_path: str,
    num_chunks: Optional[int] = None,
    chunk_duration_ms: int = 100,
    realtime_pacing: bool = False,
) -> int:
    return _manager.send_chunks_from_file(
        stream_id, wav_path, num_chunks, chunk_duration_ms, realtime_pacing
    )


def send_audio_stop_event(stream_id: str) -> None:
    _manager.send_audio_stop(stream_id)


def stop_audio_stream(stream_id: str) -> int:
    return _manager.stop_stream(stream_id)


def close_audio_stream_without_stop(stream_id: str) -> int:
    return _manager.close_stream_without_stop(stream_id)


def send_button_event(stream_id: str, button_state: str = "SINGLE_PRESS") -> None:
    _manager.send_button_event(stream_id, button_state)


def cleanup_all_streams() -> None:
    _manager.cleanup_all()


def get_audio_stream_client(
    base_url: str, token: str, device_name: str = "robot-test"
) -> AudioV2Client:
    return AudioV2Client(
        websocket_url=_websocket_url(base_url),
        bearer_token=token,
        source_id=device_name,
        display_name=device_name,
        device_kind=audio_pb2.DEVICE_KIND_PROBE,
        uplink_frame_duration_ms=20,
    )
