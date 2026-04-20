"""Shared helpers for UniFi Presence WebSocket tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket


def make_websocket(
    hass: HomeAssistant,
    start_websocket_side_effect: Exception | None = None,
) -> tuple[UnifiPresenceWebsocket, AsyncMock, MagicMock]:
    """Create a WebSocket manager with a mock controller."""
    controller = AsyncMock()
    controller.messages = MagicMock()
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.messages.new_data = MagicMock()
    controller.start_websocket = AsyncMock(side_effect=start_websocket_side_effect)
    controller.login = AsyncMock()
    controller.connectivity = MagicMock()
    controller.connectivity.ws_message_received = None

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
