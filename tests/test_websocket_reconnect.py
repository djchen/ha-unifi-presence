"""Tests for WebSocket reconnect, reauth, retry scheduling, and error recovery."""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import aiohttp
import aiounifi
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import (
    RETRY_TIMER,
    UnifiPresenceWebsocket,
)

from .websocket_helpers import make_websocket, wait_for_task


async def test_websocket_error_sets_unavailable_and_schedules_reauth_restart(
    hass: HomeAssistant,
) -> None:
    """Test that a WebSocket error marks unavailable and schedules reconnect."""
    ws, _controller, _ = make_websocket(hass, start_websocket_side_effect=aiounifi.WebsocketError("disconnected"))

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        await wait_for_task(ws.ws_task)

    assert ws.available is False
    mock_call_later.assert_called_once()
    assert mock_call_later.call_args[0][1] == RETRY_TIMER


async def test_connector_error_sets_unavailable(hass: HomeAssistant) -> None:
    """Test that a ClientConnectorError marks unavailable."""
    ws, _, _ = make_websocket(
        hass,
        start_websocket_side_effect=aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("refused")
        ),
    )

    with (
        patch(
            "custom_components.unifi_presence.websocket.async_call_later",
            return_value=MagicMock(),
        ),
        patch("custom_components.unifi_presence.websocket._LOGGER") as logger,
    ):
        ws.start()
        await wait_for_task(ws.ws_task)

    assert ws.available is False
    logger.error.assert_any_call("WebSocket connector failed: %s", ANY)

    ws.stop()


async def test_schedule_reauth_and_restart_relogins_and_restarts_ws(hass: HomeAssistant) -> None:
    """Test that _schedule_reauth_and_restart re-authenticates and restarts the WebSocket."""
    ws, controller, _ = make_websocket(hass)
    second_started = asyncio.Event()

    # Make start_websocket block forever so the WS runner stays alive
    hang_forever = asyncio.Event()
    expect_reconnect = False

    async def _block_forever() -> None:
        if expect_reconnect:
            controller.messages.new_data(b"frame")
            second_started.set()
        await hang_forever.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_forever)

    ws.start()
    # Simulate a reconnect
    ws.available = False
    expect_reconnect = True
    ws._schedule_reauth_and_restart()

    await asyncio.wait_for(second_started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    await asyncio.sleep(0)

    controller.login.assert_awaited()
    # Should have restarted WS (available should be True again)
    assert ws.available is True

    await ws.stop_and_wait()


async def test_schedule_reauth_and_restart_reschedules_on_auth_failure(hass: HomeAssistant) -> None:
    """Test that _schedule_reauth_and_restart reschedules itself on login failure."""
    ws, controller, _ = make_websocket(hass)
    controller.login = AsyncMock(side_effect=aiounifi.AiounifiException("auth failed"))

    ws.start()
    ws.available = False

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._schedule_reauth_and_restart()
        await wait_for_task(ws._reconnect_task)

    # Should have scheduled another reconnect
    mock_call_later.assert_called()
    assert ws.available is False

    ws.stop()


async def test_schedule_reauth_and_restart_blocked_after_stop(hass: HomeAssistant) -> None:
    """Test that _schedule_reauth_and_restart is a no-op after stop() has been called."""
    ws, _controller, _ = make_websocket(hass)

    ws.start()
    ws.stop()

    # _schedule_reauth_and_restart should bail out immediately due to _stopped flag
    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._schedule_reauth_and_restart()
        await asyncio.sleep(0)

    # No reconnect should have been scheduled
    mock_call_later.assert_not_called()
    assert ws._reconnect_task is None


async def test_stop_cancels_pending_retry(hass: HomeAssistant) -> None:
    """Test that stop() cancels a pending retry timer."""
    ws, _controller, _ = make_websocket(hass, start_websocket_side_effect=aiounifi.WebsocketError("disconnected"))

    cancel_mock = MagicMock()
    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=cancel_mock,
    ):
        ws.start()
        await wait_for_task(ws.ws_task)

    assert ws._cancel_retry is cancel_mock

    ws.stop()

    cancel_mock.assert_called_once()
    assert ws._cancel_retry is None


async def test_clear_retry_cancels_active_handle(hass: HomeAssistant) -> None:
    """Test _clear_retry() cancels and clears an active retry handle."""
    ws, _, _ = make_websocket(hass)
    cancel_retry = MagicMock()
    ws._cancel_retry = cancel_retry

    ws._clear_retry()

    cancel_retry.assert_called_once()
    assert ws._cancel_retry is None


async def test_schedule_reauth_and_restart_clears_pending_retry_handle(
    hass: HomeAssistant,
) -> None:
    """Test _schedule_reauth_and_restart() cancels a pending retry handle first."""
    ws, controller, _ = make_websocket(hass)
    cancel_retry = MagicMock()
    ws._cancel_retry = cancel_retry
    controller.login = AsyncMock(side_effect=aiounifi.AiounifiException("auth failed"))

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ):
        ws._schedule_reauth_and_restart()
        await wait_for_task(ws._reconnect_task)

    cancel_retry.assert_called_once()
    assert ws._cancel_retry is not cancel_retry

    ws.stop()


async def test_websocket_runner_returns_when_stopped(hass: HomeAssistant) -> None:
    """Test that ws runner exits without scheduling retry when _stopped is True."""
    ws, controller, _ = make_websocket(hass)

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
        await wait_for_task(ws.ws_task)

    # No retry should be scheduled because _stopped was True when runner exited
    mock_call_later.assert_not_called()


async def test_schedule_retry_clears_handle_when_callback_runs(hass: HomeAssistant) -> None:
    """Test that the pending retry handle is cleared before reconnect executes."""
    ws, _, _ = make_websocket(hass)
    scheduled_callback = None

    def _call_later(_hass: HomeAssistant, _delay: float, callback):
        nonlocal scheduled_callback
        scheduled_callback = callback
        return MagicMock()

    with (
        patch("custom_components.unifi_presence.websocket.async_call_later", side_effect=_call_later),
        patch.object(ws, "_schedule_reauth_and_restart") as mock_schedule_reauth_and_restart,
    ):
        ws._schedule_retry()
        assert ws._cancel_retry is not None

        assert scheduled_callback is not None
        scheduled_callback(None)

    assert ws._cancel_retry is None
    mock_schedule_reauth_and_restart.assert_called_once()


async def test_handshake_error_sets_unavailable(hass: HomeAssistant) -> None:
    """Test that a WSServerHandshakeError marks unavailable and schedules reconnect."""
    ws, _controller, _ = make_websocket(
        hass,
        start_websocket_side_effect=aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            message="handshake failed",
            status=403,
            headers=MagicMock(),
        ),
    )

    with (
        patch(
            "custom_components.unifi_presence.websocket.async_call_later",
            return_value=MagicMock(),
        ) as mock_call_later,
        patch("custom_components.unifi_presence.websocket._LOGGER") as logger,
    ):
        ws.start()
        await wait_for_task(ws.ws_task)

    assert ws.available is False
    mock_call_later.assert_called_once()
    logger.error.assert_any_call("WebSocket handshake failed with status %s: %s", 403, ANY)

    ws.stop()


async def test_unexpected_exception_sets_unavailable_and_schedules_reauth_restart(
    hass: HomeAssistant,
) -> None:
    """Test that an unexpected exception in start_websocket is caught and triggers reconnect."""
    ws, _controller, _ = make_websocket(
        hass,
        start_websocket_side_effect=RuntimeError("something unexpected"),
    )

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws.start()
        await wait_for_task(ws.ws_task)

    assert ws.available is False
    mock_call_later.assert_called_once()

    ws.stop()


async def test_schedule_reauth_and_restart_schedules_retry_when_controller_none(hass: HomeAssistant) -> None:
    """Test that _schedule_reauth_and_restart schedules a retry when the controller getter returns None."""
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: None, on_message)

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ) as mock_call_later:
        ws._schedule_reauth_and_restart()
        await wait_for_task(ws._reconnect_task)

    mock_call_later.assert_called_once()
    assert mock_call_later.call_args[0][1] == RETRY_TIMER
