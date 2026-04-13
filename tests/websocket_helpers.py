"""Shared helpers for UniFi Presence WebSocket tests."""

from __future__ import annotations

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
    controller.start_websocket = AsyncMock(side_effect=start_websocket_side_effect)
    controller.login = AsyncMock()

    on_message = MagicMock()

    ws = UnifiPresenceWebsocket(
        hass,
        lambda: controller,
        on_message,
    )
    return ws, controller, on_message
