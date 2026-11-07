"""Ending a stream whose provider socket already closed.

Deepgram closes an idle stream (NET-0001) after about 10 seconds without audio.
When the session then ends, the CloseStream send hits a dead socket. That must
end the stream cleanly, not raise and leave the stream registered.
"""

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from backend.services.transcription import RegistryStreamingTranscriptionProvider

IDLE_CLOSE = Close(
    1011, "Deepgram did not receive audio data within the timeout window."
)


class ClosedSocket:
    def __init__(self):
        self.closed_called = False

    async def send(self, _payload):
        raise ConnectionClosedError(IDLE_CLOSE, IDLE_CLOSE, rcvd_then_sent=True)

    async def recv(self):
        raise ConnectionClosedError(IDLE_CLOSE, IDLE_CLOSE, rcvd_then_sent=True)

    async def close(self):
        self.closed_called = True


def _provider_with(ws, pending=b""):
    provider = object.__new__(RegistryStreamingTranscriptionProvider)
    provider.model = type(
        "Model", (), {"operations": {"end": {"message": {"type": "CloseStream"}}}}
    )()
    provider._streams = {"s1": {"ws": ws, "pending_audio": bytearray(pending)}}
    return provider


@pytest.mark.parametrize("pending", [b"", b"\x00" * 64])
async def test_end_stream_on_provider_closed_socket_returns_empty_and_unregisters(
    pending,
):
    ws = ClosedSocket()
    provider = _provider_with(ws, pending)

    result = await provider.end_stream("s1")

    assert result == {"text": "", "words": [], "segments": []}
    assert "s1" not in provider._streams
