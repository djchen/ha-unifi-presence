"""Shared helpers for UniFi Presence WebSocket tests."""

import asyncio
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import make_mock_controller


def make_websocket(
    hass: HomeAssistant,
    start_websocket_side_effect: Exception | None = None,
) -> tuple[UnifiPresenceWebsocket, MagicMock, MagicMock]:
    """Create a WebSocket manager with a mock controller."""
    controller = make_mock_controller()
    controller.start_websocket.side_effect = start_websocket_side_effect

    on_message = MagicMock()

    ws = UnifiPresenceWebsocket(
        hass,
        lambda: controller,
        on_message,
    )
    return ws, controller, on_message


def block_websocket(controller: MagicMock) -> asyncio.Event:
    """Block the mocked WebSocket runner, returning its startup event."""
    started = asyncio.Event()

    async def _start_websocket() -> None:
        started.set()
        await asyncio.Event().wait()

    controller.start_websocket.side_effect = _start_websocket
    return started


async def wait_for_task(task: asyncio.Task[object] | None, *, timeout: float = 1.0) -> None:
    """Wait for a task to finish without relying on repeated loop yields."""
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
