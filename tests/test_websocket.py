"""Tests for the UniFi Presence WebSocket lifecycle manager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import aiounifi
import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import (
    RETRY_TIMER,
    STALE_WEBSOCKET_INTERVAL,
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
        lambda: controller,
        on_message,
    )
    return ws, controller, on_message


async def test_start_subscribes_and_creates_task(hass: HomeAssistant) -> None:
    """Test that start() subscribes to messages and creates a WS task."""
    ws, controller, _ = _make_websocket(hass)

    # Make start_websocket block so the task stays alive during assertions
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    ws.start()

    controller.messages.subscribe.assert_called_once()
    assert ws.ws_task is not None
    assert ws.available is False

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
        for _ in range(5):
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
        for _ in range(5):
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

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        # Simulate a reconnect
        ws.available = False
        ws._reconnect()

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
        for _ in range(5):
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
        ws._reconnect()
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
        for _ in range(5):
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


async def test_stop_and_wait_timeout_logs_warning(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
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
        caplog.at_level("WARNING"),
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
        for _ in range(5):
            await asyncio.sleep(0)

    # No retry should be scheduled because _stopped was True when runner exited
    mock_call_later.assert_not_called()


async def test_async_watch_websocket_logs_health(hass: HomeAssistant) -> None:
    """Test that _async_watch_websocket runs without error."""
    ws, controller, _ = _make_websocket(hass)
    controller.connectivity.ws_message_received = "2025-01-01T00:00:00Z"

    # Directly invoke the health check callback
    ws._async_watch_websocket(None)

    # No assertion needed — just verify it doesn't raise


async def test_websocket_becomes_available_after_runner_confirms_session(hass: HomeAssistant) -> None:
    """Test that availability flips only after the runner confirms a live session."""
    ws, controller, _ = _make_websocket(hass)
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


async def test_schedule_retry_clears_handle_when_callback_runs(hass: HomeAssistant) -> None:
    """Test that the pending retry handle is cleared before reconnect executes."""
    ws, _, _ = _make_websocket(hass)
    scheduled_callback = None

    def _call_later(_hass: HomeAssistant, _delay: float, callback):
        nonlocal scheduled_callback
        scheduled_callback = callback
        return MagicMock()

    with (
        patch("custom_components.unifi_presence.websocket.async_call_later", side_effect=_call_later),
        patch.object(ws, "_reconnect") as mock_reconnect,
    ):
        ws._schedule_retry()
        assert ws._cancel_retry is not None

        assert scheduled_callback is not None
        scheduled_callback(None)

    assert ws._cancel_retry is None
    mock_reconnect.assert_called_once()


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
        for _ in range(5):
            await asyncio.sleep(0)

    assert ws.available is False
    mock_call_later.assert_called_once()

    ws.stop()


async def test_unexpected_exception_sets_unavailable_and_schedules_reconnect(hass: HomeAssistant) -> None:
    """Test that an unexpected exception in start_websocket is caught and triggers reconnect."""
    ws, _controller, _ = _make_websocket(
        hass,
        start_websocket_side_effect=RuntimeError("something unexpected"),
    )

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        for _ in range(5):
            await asyncio.sleep(0)

    assert ws.available is False
    mock_call_later.assert_called_once()

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
        ws._start_websocket()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Should not schedule a retry — just returns
    mock_call_later.assert_not_called()


async def test_reconnect_schedules_retry_when_controller_none(hass: HomeAssistant) -> None:
    """Test that _reconnect schedules a retry when the controller getter returns None."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._reconnect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    mock_call_later.assert_called_once()
    assert mock_call_later.call_args[0][1] == RETRY_TIMER


async def test_health_check_with_none_controller(hass: HomeAssistant) -> None:
    """Test that _async_watch_websocket handles a None controller gracefully."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    # Should not raise
    ws._async_watch_websocket(None)


async def test_async_watch_websocket_reconnects_stale_session(hass: HomeAssistant) -> None:
    """Test that the health check reconnects a stale but marked-available session."""
    ws, controller, _ = _make_websocket(hass)
    ws.available = True
    ws.ws_task = MagicMock()
    ws.ws_task.done.return_value = False
    controller.connectivity.ws_message_received = datetime.now(UTC) - STALE_WEBSOCKET_INTERVAL - timedelta(seconds=1)

    with patch.object(ws, "_reconnect") as mock_reconnect:
        ws._async_watch_websocket(None)

    mock_reconnect.assert_called_once()


async def test_reconnect_public_resubscribes_and_restarts(hass: HomeAssistant) -> None:
    """Test that the public reconnect() re-subscribes and restarts the websocket."""
    ws, controller, _ = _make_websocket(hass)

    # Make start_websocket block so the task stays alive
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        await asyncio.sleep(0)

    first_subscribe_count = controller.messages.subscribe.call_count
    first_task = ws.ws_task

    # Simulate coordinator replacing the controller and calling reconnect()
    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.reconnect()
        for _ in range(5):
            await asyncio.sleep(0)

    # Should have re-subscribed and created a new task
    assert controller.messages.subscribe.call_count > first_subscribe_count
    assert ws.ws_task is not first_task

    ws.stop()


async def test_reconnect_public_noop_after_stop(hass: HomeAssistant) -> None:
    """Test that the public reconnect() is a no-op after stop()."""
    ws, controller, _ = _make_websocket(hass)

    ws.start()
    ws.stop()

    controller.messages.subscribe.reset_mock()
    ws.reconnect()

    controller.messages.subscribe.assert_not_called()
    assert ws.ws_task is None
