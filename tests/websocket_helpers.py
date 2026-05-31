"""Shared helpers for UniFi Presence WebSocket tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import make_mock_controller


def make_websocket(
    hass: HomeAssistant,
    start_websocket_side_effect: Exception | None = None,
) -> tuple[UnifiPresenceWebsocket, MagicMock, MagicMock]:
    """Create a WebSocket manager with a mock controller."""
    controller = make_mock_controller()
    controller.start_websocket = AsyncMock(side_effect=start_websocket_side_effect)

    on_message = MagicMock()

    ws = UnifiPresenceWebsocket(
        hass,
        lambda: controller,
        on_message,
    )
    return ws, controller, on_message


async def wait_for_task(task: asyncio.Task[object] | None, *, timeout: float = 1.0) -> None:
    """Wait for a task to finish without relying on repeated loop yields."""
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


async def wait_for_websocket_start(controller: MagicMock, *, count: int = 1, timeout: float = 1.0) -> None:
    """Wait until the mocked controller websocket runner has started."""
    async with asyncio.timeout(timeout):
        while controller.start_websocket.await_count < count:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
