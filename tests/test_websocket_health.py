"""Tests for WebSocket health checks and stale-session detection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import (
    STALE_WEBSOCKET_INTERVAL,
    UnifiPresenceWebsocket,
)

from .websocket_helpers import make_websocket


async def test_async_watch_websocket_logs_health(hass: HomeAssistant) -> None:
    """Test that _async_watch_websocket runs without error."""
    ws, controller, _ = make_websocket(hass)
    controller.connectivity.ws_message_received = "2025-01-01T00:00:00Z"

    # Directly invoke the health check callback
    ws._async_watch_websocket(None)

    # No assertion needed — just verify it doesn't raise


async def test_health_check_reauths_when_task_done_while_available(
    hass: HomeAssistant,
) -> None:
    """Test health checks reconnect when the runner is done but still marked available."""
    ws, controller, _ = make_websocket(hass)
    ws.available = True
    finished_task = hass.async_create_task(asyncio.sleep(0))
    await finished_task
    ws.ws_task = finished_task
    controller.connectivity.ws_message_received = datetime.now(UTC)

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._async_watch_websocket(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_health_check_with_none_controller(hass: HomeAssistant) -> None:
    """Test that _async_watch_websocket handles a None controller gracefully."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    # Should not raise
    ws._async_watch_websocket(None)


async def test_health_check_reauths_on_stale_session(hass: HomeAssistant) -> None:
    """Test that the health check reconnects a stale but marked-available session."""
    ws, controller, _ = make_websocket(hass)
    ws.available = True
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False
    controller.connectivity.ws_message_received = datetime.now(UTC) - STALE_WEBSOCKET_INTERVAL - timedelta(seconds=1)

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._async_watch_websocket(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_health_check_reauths_on_stale_startup(hass: HomeAssistant) -> None:
    """Test that the health check reconnects when no message received since a stale startup."""
    ws, controller, _ = make_websocket(hass)
    ws.available = False
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False
    controller.connectivity.ws_message_received = None
    ws._ws_started_at = datetime.now(UTC) - STALE_WEBSOCKET_INTERVAL - timedelta(seconds=1)

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._async_watch_websocket(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_health_check_skips_reauth_on_recent_startup(hass: HomeAssistant) -> None:
    """Test that the health check does NOT reconnect when startup is recent."""
    ws, controller, _ = make_websocket(hass)
    ws.available = True
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False
    controller.connectivity.ws_message_received = None
    ws._ws_started_at = datetime.now(UTC) - timedelta(seconds=30)

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._async_watch_websocket(None)

    mock_schedule_reauth_and_restart.assert_not_called()


async def test_health_check_reauths_on_stale_startup_when_available(hass: HomeAssistant) -> None:
    """Test stale-startup reconnect fires even when available is True (defensive fallback)."""
    ws, controller, _ = make_websocket(hass)
    ws.available = True
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False
    controller.connectivity.ws_message_received = None
    ws._ws_started_at = datetime.now(UTC) - STALE_WEBSOCKET_INTERVAL - timedelta(seconds=1)

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._async_watch_websocket(None)

    mock_schedule_reauth_and_restart.assert_called_once()
