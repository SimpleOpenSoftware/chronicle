"""HAVPE device-local transport to Chronicle audio-v2 adapter."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import websockets
from audio_contract.v2 import audio_pb2
from audio_v2_adapter import (
    HavpePlayback,
    forward_device_capture,
    forward_device_events,
)
from chronicle_client import ClientConfig, acheck_credentials
from chronicle_client.audio_v2 import AudioV2Client
from device_controller import DeviceController

logger = logging.getLogger(__name__)


@dataclass
class RelayConfig:
    backend_url: str
    backend_ws_url: str
    api_key: str
    device_name: str
    esphome_device_ip: str = ""
    device_idle_timeout: float = 300.0

    @classmethod
    def from_env(cls) -> "RelayConfig":
        client = ClientConfig.from_env(default_device_name="havpe")
        return cls(
            backend_url=client.backend_url,
            backend_ws_url=client.backend_ws_url,
            api_key=client.api_key,
            device_name=client.device_name,
            esphome_device_ip=os.getenv("ESPHOME_DEVICE_IP", ""),
            device_idle_timeout=float(os.getenv("DEVICE_IDLE_TIMEOUT", "300")),
        )


async def _maintain_esphome_api(
    device: DeviceController, device_ip: str, *, interval: float = 15.0
) -> None:
    while True:
        await asyncio.sleep(interval)
        if device.connected:
            continue
        try:
            connected = await asyncio.wait_for(device.connect(device_ip), timeout=5.0)
        except asyncio.TimeoutError:
            connected = False
        if connected:
            logger.info("ESPHome API reconnected at %s", device_ip)


async def run_device_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: RelayConfig,
    *,
    on_audio_chunk: Callable[[bytes, int], None] | None = None,
    on_audio_event: Callable[[str, dict], None] | None = None,
    on_session_start: Callable[[str], None] | None = None,
    on_session_end: Callable[[], None] | None = None,
    on_auth_failure: Callable[[], None] | None = None,
) -> None:
    """Keep one physical device alive across transient audio-v2 reconnects."""

    peer = writer.get_extra_info("peername")
    address = f"{peer[0]}:{peer[1]}" if peer else "unknown"
    device_ip = peer[0] if peer else "127.0.0.1"
    if on_session_start:
        on_session_start(address)

    if not await acheck_credentials(config.api_key, config.backend_url):
        if on_auth_failure:
            on_auth_failure()
        writer.close()
        await writer.wait_closed()
        return

    device = DeviceController()
    esphome_ip = config.esphome_device_ip or device_ip
    try:
        await asyncio.wait_for(device.connect(esphome_ip), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("ESPHome API unavailable; capture continues without controls")
    keepalive = asyncio.create_task(
        _maintain_esphome_api(device, esphome_ip), name="esphome-api-keepalive"
    )

    backoff = 1.0
    capture_epoch = 0
    tasks: list[asyncio.Task] = []
    try:
        while True:
            client = AudioV2Client(
                websocket_url=f"{config.backend_ws_url.rstrip('/')}/ws/audio",
                bearer_token=config.api_key,
                source_id=config.device_name,
                display_name=config.device_name,
                device_kind=audio_pb2.DEVICE_KIND_HAVPE,
                uplink_frame_duration_ms=20,
            )
            playback = HavpePlayback(client, device)
            client.on_control = playback.control
            client.on_playback = playback.media
            connected_at = 0.0
            try:
                await client.connect()
                connected_at = asyncio.get_running_loop().time()
                capture_epoch += 1
                logger.info("Chronicle audio-v2 connected for %s", address)
                capture = asyncio.create_task(
                    forward_device_capture(
                        reader,
                        client,
                        capture_epoch=capture_epoch,
                        idle_timeout=config.device_idle_timeout,
                        interactive=device.supports_audio_v2_playback(),
                        on_audio_chunk=on_audio_chunk,
                        on_audio_event=on_audio_event,
                    ),
                    name="havpe-capture-v2",
                )
                tasks = [capture]
                if device.connected:
                    tasks.append(
                        asyncio.create_task(
                            forward_device_events(device, client),
                            name="havpe-controls-v2",
                        )
                    )
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if capture in done and capture.exception() is None:
                    return
                for task in done:
                    error = task.exception()
                    if error is not None:
                        raise error
            except (websockets.WebSocketException, OSError, ConnectionError) as error:
                logger.warning(
                    "Audio-v2 dropped (%s); retrying in %.0fs", error, backoff
                )
            finally:
                await client.close()
            if connected_at and asyncio.get_running_loop().time() - connected_at >= 30:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        logger.info("HAVPE device disconnected or became idle")
    except Exception:
        logger.exception("HAVPE session failed")
    finally:
        keepalive.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(keepalive, *tasks, return_exceptions=True)
        await device.disconnect()
        writer.close()
        await writer.wait_closed()
        if on_session_end:
            on_session_end()
        logger.info("HAVPE session ended")
