"""Tests for WebSocket watchdog and stale-session detection."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from .websocket_helpers import block_websocket, make_websocket


@pytest.mark.parametrize(
    ("available", "task_done"),
    [(True, True), (True, False), (False, False)],
    ids=("task-done-while-available", "stale-session", "stale-startup"),
)
async def test_watchdog_reauths_on_expiry(hass: HomeAssistant, available: bool, task_done: bool) -> None:
    """Test watchdog reconnects unhealthy sessions."""
    ws, _controller, _ = make_websocket(hass)
    ws.available = available
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = task_done

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
    started = block_websocket(controller)

    ws.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    initial_handle = ws._cancel_watchdog
    assert initial_handle is not None

    controller.messages.new_data(b"frame")

    assert ws.available is True
    assert ws._cancel_watchdog is not None
    assert ws._cancel_watchdog is not initial_handle

    await ws.stop_and_wait()
