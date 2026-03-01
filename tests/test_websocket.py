"""Tests for the UniFi Presence WebSocket lifecycle manager."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import aiounifi
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import (
    RETRY_TIMER,
    UnifiPresenceWebsocket,
)


def _make_websocket(
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
        controller,
        "test-signal",
        on_message,
    )
    return ws, controller, on_message


async def test_start_subscribes_and_creates_task(hass: HomeAssistant) -> None:
    """Test that start() subscribes to messages and creates a WS task."""
    ws, controller, _ = _make_websocket(hass)

    ws.start()

    controller.messages.subscribe.assert_called_once()
    assert ws.ws_task is not None
    assert ws.available is True

    ws.stop()


async def test_stop_cancels_task_and_unsubscribes(hass: HomeAssistant) -> None:
    """Test that stop() cancels the WS task and unsubscribes."""
    ws, controller, _ = _make_websocket(hass)

    ws.start()
    assert ws.ws_task is not None

    unsub = controller.messages.subscribe.return_value
    ws.stop()

    unsub.assert_called_once()
    assert ws.available is False
    assert ws._stopped is True


async def test_stop_and_wait(hass: HomeAssistant) -> None:
    """Test that stop_and_wait awaits the WS task."""
    ws, _, _ = _make_websocket(hass)

    ws.start()
    await ws.stop_and_wait()

    assert ws._unsub_messages is None


async def test_websocket_error_sets_unavailable_and_schedules_reconnect(
    hass: HomeAssistant,
) -> None:
    """Test that a WebSocket error marks unavailable and schedules reconnect."""
    ws, _controller, _ = _make_websocket(hass, start_websocket_side_effect=aiounifi.WebsocketError("disconnected"))

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        # Let the task run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert ws.available is False
    mock_call_later.assert_called_once()
    assert mock_call_later.call_args[0][1] == RETRY_TIMER


async def test_connector_error_sets_unavailable(hass: HomeAssistant) -> None:
    """Test that a ClientConnectorError marks unavailable."""
    ws, _, _ = _make_websocket(
        hass,
        start_websocket_side_effect=aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("refused")
        ),
    )

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ):
        ws.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert ws.available is False

    ws.stop()


async def test_reconnect_relogins_and_restarts_ws(hass: HomeAssistant) -> None:
    """Test that _reconnect re-authenticates and restarts the WebSocket."""
    ws, controller, _ = _make_websocket(hass)

    # Make start_websocket block forever so the WS runner stays alive
    hang_forever = asyncio.Event()

    async def _block_forever() -> None:
        await hang_forever.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_forever)

    ws.start()
    # Simulate a reconnect
    ws.available = False
    ws._reconnect(log=True)

    # Let the reconnect coroutine and nested _start_websocket task run
    for _ in range(5):
        await asyncio.sleep(0)

    controller.login.assert_awaited()
    # Should have restarted WS (available should be True again)
    assert ws.available is True

    ws.stop()


async def test_reconnect_reschedules_on_auth_failure(hass: HomeAssistant) -> None:
    """Test that _reconnect reschedules itself on login failure."""
    ws, controller, _ = _make_websocket(hass)
    controller.login = AsyncMock(side_effect=aiounifi.AiounifiException("auth failed"))

    ws.start()
    ws.available = False

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._reconnect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Should have scheduled another reconnect
    mock_call_later.assert_called()
    assert ws.available is False

    ws.stop()


async def test_reconnect_blocked_after_stop(hass: HomeAssistant) -> None:
    """Test that _reconnect is a no-op after stop() has been called."""
    ws, _controller, _ = _make_websocket(hass)

    ws.start()
    ws.stop()

    # _reconnect should bail out immediately due to _stopped flag
    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._reconnect(log=True)
        await asyncio.sleep(0)

    # No reconnect should have been scheduled
    mock_call_later.assert_not_called()
    assert ws._reconnect_task is None


async def test_stop_cancels_pending_retry(hass: HomeAssistant) -> None:
    """Test that stop() cancels a pending retry timer."""
    ws, _controller, _ = _make_websocket(hass, start_websocket_side_effect=aiounifi.WebsocketError("disconnected"))

    cancel_mock = MagicMock()
    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=cancel_mock,
    ):
        ws.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert ws._cancel_retry is cancel_mock

    ws.stop()

    cancel_mock.assert_called_once()
    assert ws._cancel_retry is None


async def test_message_handler_forwards_to_callback(hass: HomeAssistant) -> None:
    """Test that the subscribe callback forwards messages to on_message."""
    ws, controller, on_message = _make_websocket(hass)

    ws.start()

    # Capture the handler passed to subscribe
    subscribe_call = controller.messages.subscribe.call_args
    handler = subscribe_call[0][0]

    # Invoke the handler with a mock message
    mock_msg = MagicMock()
    handler(mock_msg)

    on_message.assert_called_once_with(mock_msg)

    ws.stop()


async def test_stop_and_wait_timeout_logs_warning(
    hass: HomeAssistant, caplog: logging.Handler
) -> None:
    """Test that stop_and_wait logs a warning when the WS task won't finish."""
    ws, controller, _ = _make_websocket(hass)

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
        caplog.at_level(logging.WARNING),
    ):
        await ws.stop_and_wait()

    assert "did not complete in time" in caplog.text

    # The task is still pending — clean up
    hang.set()
    await asyncio.sleep(0)


async def test_websocket_runner_returns_when_stopped(hass: HomeAssistant) -> None:
    """Test that ws runner exits without scheduling retry when _stopped is True."""
    ws, controller, _ = _make_websocket(hass)

    # Make start_websocket raise after setting _stopped, simulating stop() being
    # called while the websocket runner is active.
    async def _raise_after_stop() -> None:
        ws._stopped = True
        raise aiounifi.WebsocketError("disconnected")

    controller.start_websocket = AsyncMock(side_effect=_raise_after_stop)

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # No retry should be scheduled because _stopped was True when runner exited
    mock_call_later.assert_not_called()


async def test_handshake_error_sets_unavailable(hass: HomeAssistant) -> None:
    """Test that a WSServerHandshakeError marks unavailable and schedules reconnect."""
    ws, _controller, _ = _make_websocket(
        hass,
        start_websocket_side_effect=aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            message="handshake failed",
            status=403,
            headers=MagicMock(),
        ),
    )

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert ws.available is False
    mock_call_later.assert_called_once()

    ws.stop()
