"""Stream OMI/Neo raw Opus through Chronicle's generated audio V2 client."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Callable
from typing import AsyncGenerator

import websockets
from audio_contract.v2 import audio_pb2
from chronicle_client import ClientConfig
from chronicle_client.audio_v2 import AudioV2Client

from .output_route import HostOutputPolicy

logger = logging.getLogger(__name__)
_config = ClientConfig.from_env()
backend_url = _config.backend_url
websocket_uri = f"{_config.backend_ws_url}/ws/audio"
CHRONICLE_API_KEY = _config.api_key
VERIFY_SSL = _config.verify_ssl

_active_client: AudioV2Client | None = None
_BACKOFF_INITIAL = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX = 30.0
_MIN_HEALTHY_DURATION = 30.0


async def send_button_event(button_state: str) -> None:
    """Send a generated button control over the active audio V2 connection."""

    if _active_client is None:
        logger.debug(
            "No active audio V2 client; dropping button event %s", button_state
        )
        return
    state = {
        "SINGLE_PRESS": audio_pb2.BUTTON_STATE_SINGLE_PRESS,
        "DOUBLE_PRESS": audio_pb2.BUTTON_STATE_DOUBLE_PRESS,
        "LONG_PRESS": audio_pb2.BUTTON_STATE_LONG_PRESS,
    }.get(button_state)
    if state is None:
        raise ValueError(f"unknown button state: {button_state}")
    await _active_client.send_button(state)


def _ssl_context() -> ssl.SSLContext | None:
    if not backend_url.startswith("https"):
        return None
    context = ssl.create_default_context()
    if not VERIFY_SSL:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


async def stream_to_backend(
    stream: AsyncGenerator[bytes, None],
    device_name: str = "wearable",
    speaker=None,
    output_policy: HostOutputPolicy | str = HostOutputPolicy.AUTO,
    on_voice_status: Callable[[str], None] | None = None,
    initial_capture_epoch: int = 0,
    on_capture_epoch: Callable[[int], None] | None = None,
) -> None:
    """Stream one BLE source with reconnects and a fresh binding per socket."""

    del speaker, output_policy
    global _active_client
    status = on_voice_status or (lambda _status: None)
    capture_epoch = initial_capture_epoch
    backoff = _BACKOFF_INITIAL
    iterator = stream.__aiter__()
    pending: bytes | None = None

    while True:
        client = AudioV2Client(
            websocket_url=websocket_uri,
            bearer_token=CHRONICLE_API_KEY,
            source_id=device_name,
            display_name=device_name,
            device_kind=(
                audio_pb2.DEVICE_KIND_NEO
                if "neo" in device_name.lower()
                else audio_pb2.DEVICE_KIND_OMI
            ),
            uplink_frame_duration_ms=60,
            ssl_context=_ssl_context(),
        )
        connected_at = None
        try:
            await client.connect()
            capture_epoch += 1
            if on_capture_epoch is not None:
                on_capture_epoch(capture_epoch)
            await client.start_capture(capture_epoch=capture_epoch)
            _active_client = client
            connected_at = asyncio.get_running_loop().time()
            status(f"{device_name} → Chronicle audio V2")

            while True:
                packet = pending
                pending = None
                if packet is None:
                    try:
                        packet = await iterator.__anext__()
                    except StopAsyncIteration:
                        await client.close()
                        _active_client = None
                        return
                try:
                    await client.send_opus(packet)
                except (websockets.WebSocketException, OSError):
                    pending = packet
                    raise
                if (
                    backoff != _BACKOFF_INITIAL
                    and connected_at is not None
                    and asyncio.get_running_loop().time() - connected_at
                    >= _MIN_HEALTHY_DURATION
                ):
                    backoff = _BACKOFF_INITIAL
        except (websockets.WebSocketException, OSError, ConnectionError) as error:
            _active_client = None
            await client.close()
            logger.warning(
                "Audio V2 socket dropped (%s); reconnecting in %.0fs", error, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
