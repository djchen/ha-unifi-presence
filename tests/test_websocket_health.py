"""Tests for WebSocket watchdog and stale-session detection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from .websocket_helpers import make_websocket, wait_for_websocket_start


async def test_watchdog_reauths_when_task_done_while_available(hass: HomeAssistant) -> None:
    """Test watchdog reconnects when the runner is already done."""
    ws, _controller, _ = make_websocket(hass)
    ws.available = True
    finished_task = hass.async_create_task(asyncio.sleep(0))
    await finished_task
    ws.ws_task = finished_task

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._handle_watchdog_expiry(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_watchdog_reauths_on_stale_session(hass: HomeAssistant) -> None:
    """Test watchdog reconnects a stale but marked-available session."""
    ws, _controller, _ = make_websocket(hass)
    ws.available = True
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._handle_watchdog_expiry(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_watchdog_reauths_on_stale_startup(hass: HomeAssistant) -> None:
    """Test watchdog reconnects when no inbound frame arrives after startup."""
    ws, _controller, _ = make_websocket(hass)
    ws.available = False
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False

    with patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart:
        ws._handle_watchdog_expiry(None)

    mock_schedule_reauth_and_restart.assert_called_once()


async def test_arm_watchdog_skips_when_stopped(hass: HomeAssistant) -> None:
    """Test watchdog is not armed once the manager has been stopped."""
    ws, _controller, _ = make_websocket(hass)
    ws._stopped = True

    ws._arm_watchdog()

    assert ws._cancel_watchdog is None


async def test_watchdog_expiry_noop_when_stopped(hass: HomeAssistant) -> None:
    """Test watchdog expiry does nothing after shutdown."""
    ws, _controller, _ = make_websocket(hass)
    ws._stopped = True

    with patch.object(ws, "_schedule_reauth_and_restart") as reconnect:
        ws._handle_watchdog_expiry(None)

    reconnect.assert_not_called()


async def test_inbound_frame_resets_watchdog_deadline(hass: HomeAssistant) -> None:
    """Test each inbound frame replaces the watchdog timer and marks the socket healthy."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    ws.start()
    await wait_for_websocket_start(controller)
    initial_handle = ws._cancel_watchdog
    assert initial_handle is not None

    controller.messages.new_data(b"frame")

    assert ws.available is True
    assert ws._cancel_watchdog is not None
    assert ws._cancel_watchdog is not initial_handle

    ws.stop()
