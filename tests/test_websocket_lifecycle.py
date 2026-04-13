"""Tests for WebSocket lifecycle: start, stop, subscribe, availability."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .websocket_helpers import make_websocket


async def test_start_subscribes_and_creates_task(hass: HomeAssistant) -> None:
    """Test that start() subscribes to messages and creates a WS task."""
    ws, controller, _ = make_websocket(hass)

    # Make start_websocket block so the task stays alive during assertions
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    ws.start()

    controller.messages.subscribe.assert_called_once()
    assert ws.ws_task is not None
    assert ws.available is False

    await ws.stop_and_wait()


async def test_start_websocket_is_idempotent_while_runner_active(hass: HomeAssistant) -> None:
    """Test _start_websocket_runner() does not create a second runner while active."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    ws._start_websocket_runner()
    first_task = ws.ws_task

    ws._start_websocket_runner()
    await asyncio.sleep(0)

    assert ws.ws_task is first_task
    controller.start_websocket.assert_awaited_once()

    await ws.stop_and_wait()


async def test_stop_cancels_task_and_unsubscribes(hass: HomeAssistant) -> None:
    """Test that stop() cancels the WS task and unsubscribes."""
    ws, controller, _ = make_websocket(hass)

    ws.start()
    assert ws.ws_task is not None

    unsub = controller.messages.subscribe.return_value
    await ws.stop_and_wait()

    unsub.assert_called_once()
    assert ws.available is False
    assert ws._stopped is True


async def test_stop_and_wait(hass: HomeAssistant) -> None:
    """Test that stop_and_wait awaits the WS task."""
    ws, _, _ = make_websocket(hass)

    ws.start()
    await ws.stop_and_wait()

    assert ws._unsub_messages is None


async def test_stop_and_wait_timeout_logs_warning(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """Test that stop_and_wait logs a warning when the WS task won't finish."""
    ws, controller, _ = make_websocket(hass)

    # Make start_websocket block forever
    hang = asyncio.Event()

    async def _block_forever() -> None:
        await hang.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_forever)

    ws.start()

    # Patch asyncio.wait to simulate timeout (return the task as pending)
    real_task = ws.ws_task
    with (
        patch("custom_components.unifi_presence.websocket.asyncio.wait", return_value=(set(), {real_task})),
        caplog.at_level("WARNING"),
    ):
        await ws.stop_and_wait()

    assert "did not complete in time" in caplog.text

    # The task is still pending — clean up
    hang.set()
    await asyncio.sleep(0)


async def test_message_handler_forwards_to_callback(hass: HomeAssistant) -> None:
    """Test that the subscribe callback forwards messages to on_message."""
    ws, controller, on_message = make_websocket(hass)

    ws.start()

    # Capture the handler passed to subscribe
    subscribe_call = controller.messages.subscribe.call_args
    handler = subscribe_call[0][0]

    # Invoke the handler with a mock message
    mock_msg = MagicMock()
    handler(mock_msg)

    on_message.assert_called_once_with(mock_msg)

    ws.stop()


async def test_set_available_noop_when_value_unchanged(hass: HomeAssistant) -> None:
    """Test _set_available() leaves state untouched on no-op updates."""
    ws, _, _ = make_websocket(hass)
    ws.available = True

    ws._set_available(True)

    assert ws.available is True


async def test_websocket_becomes_available_after_runner_confirms_session(hass: HomeAssistant) -> None:
    """Test that availability flips only after the runner confirms a live session."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0.01):
        ws.start()
        await asyncio.sleep(0)
        assert ws.available is False

        await asyncio.sleep(0.02)
        await asyncio.sleep(0)

    assert ws.available is True

    ws.stop()


async def test_start_with_none_controller_skips_subscribe(hass: HomeAssistant) -> None:
    """Test that start() handles a None controller gracefully."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    ws.start()

    # No subscription should have been created
    assert ws._unsub_messages is None

    ws.stop()


async def test_websocket_runner_returns_when_controller_none(hass: HomeAssistant) -> None:
    """Test that the WS runner exits early when the controller getter returns None."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._start_websocket_runner()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Should not schedule a retry — just returns
    mock_call_later.assert_not_called()
