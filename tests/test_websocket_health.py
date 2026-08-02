"""Tests for WebSocket watchdog and stale-session detection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from .websocket_helpers import make_websocket, wait_for_websocket_start


@pytest.mark.parametrize(
    ("available", "task_done"),
    [(True, True), (True, False), (False, False)],
    ids=("task-done-while-available", "stale-session", "stale-startup"),
)
async def test_watchdog_reauths_on_expiry(hass: HomeAssistant, available: bool, task_done: bool) -> None:
    """Test watchdog reconnects unhealthy sessions."""
    ws, _controller, _ = make_websocket(hass)
    ws.available = available
    if task_done:
        finished_task = hass.async_create_task(asyncio.sleep(0))
        await finished_task
        ws.ws_task = finished_task
    else:
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
