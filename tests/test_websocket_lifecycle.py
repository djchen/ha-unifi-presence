"""Tests for WebSocket lifecycle: start, stop, subscribe, availability."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .websocket_helpers import make_websocket, wait_for_task, wait_for_websocket_start


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
    await wait_for_websocket_start(controller)

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


async def test_websocket_becomes_available_after_runner_confirms_session(hass: HomeAssistant) -> None:
    """Test that availability flips only after the first inbound WebSocket frame."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    frame_received = asyncio.Event()

    async def _start_websocket() -> None:
        controller.messages.new_data(b"frame")
        frame_received.set()
        await hang.wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    assert ws.available is False
    await asyncio.wait_for(frame_received.wait(), timeout=1)

    assert ws.available is True

    ws.stop()


async def test_websocket_startup_without_messages_stays_unavailable(hass: HomeAssistant) -> None:
    """Test that startup does not mark available until a frame is received."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    ws.start()
    await wait_for_websocket_start(controller)
    await asyncio.sleep(0.02)

    assert ws.available is False

    ws.stop()


async def test_websocket_runner_restores_new_data_after_exit(hass: HomeAssistant) -> None:
    """Test the temporary WebSocket health wrapper is restored on runner exit."""
    ws, controller, _ = make_websocket(hass, start_websocket_side_effect=RuntimeError("boom"))
    original_new_data = controller.messages.new_data

    with patch(
        "custom_components.unifi_presence.websocket.async_call_later",
        return_value=MagicMock(),
    ):
        ws.start()
        await wait_for_task(ws.ws_task)

    assert controller.messages.new_data is original_new_data

    ws.stop()


async def test_websocket_runner_restores_new_data_after_cancel(hass: HomeAssistant) -> None:
    """Test the temporary WebSocket health wrapper is restored after cancellation."""
    ws, controller, _ = make_websocket(hass)
    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)
    original_new_data = controller.messages.new_data

    ws.start()
    await wait_for_websocket_start(controller)

    assert controller.messages.new_data is not original_new_data

    await ws.stop_and_wait()

    assert controller.messages.new_data is original_new_data


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
        await wait_for_task(ws.ws_task)

    # Should not schedule a retry — just returns
    mock_call_later.assert_not_called()


async def test_start_websocket_runner_skips_when_stopped(hass: HomeAssistant) -> None:
    """Test the runner is not created after the manager has stopped."""
    ws, controller, _ = make_websocket(hass)
    ws._stopped = True

    ws._start_websocket_runner()

    assert ws.ws_task is None
    controller.start_websocket.assert_not_called()


async def test_async_cancel_and_wait_ignores_none_task(hass: HomeAssistant) -> None:
    """Test task cancellation helper handles a missing task."""
    ws, _, _ = make_websocket(hass)

    await ws._async_cancel_and_wait(None)


async def test_async_cancel_and_wait_skips_awaiting_current_task(hass: HomeAssistant) -> None:
    """Test self-cancellation does not await the current task."""
    ws, _, _ = make_websocket(hass)
    current_task = MagicMock()

    with patch("custom_components.unifi_presence.websocket.asyncio.current_task", return_value=current_task):
        await ws._async_cancel_and_wait(current_task)

    current_task.cancel.assert_called_once_with()


async def test_schedule_retry_noop_when_retry_already_pending(hass: HomeAssistant) -> None:
    """Test duplicate retry scheduling is ignored while a handle exists."""
    ws, _, _ = make_websocket(hass)
    existing_handle = MagicMock()
    ws._cancel_retry = existing_handle

    with patch("custom_components.unifi_presence.websocket.async_call_later") as async_call_later:
        ws._schedule_retry()

    async_call_later.assert_not_called()
    assert ws._cancel_retry is existing_handle


async def test_mark_connected_noop_when_already_available(hass: HomeAssistant) -> None:
    """Test repeated health marks do not churn retry state."""
    ws, _, _ = make_websocket(hass)
    ws.available = True
    ws._retry_delay = 99
    ws._cancel_retry = MagicMock()

    ws._mark_connected()

    assert ws.available is True
    assert ws._retry_delay == 99


async def test_async_restart_runner_noop_when_stop_occurs_after_cancel(hass: HomeAssistant) -> None:
    """Test restart does not resubscribe after stopping during cancellation."""
    ws, _, _ = make_websocket(hass)

    async def _cancel_and_stop(_task: asyncio.Task[None] | None) -> None:
        ws._stopped = True

    ws.ws_task = hass.async_create_task(asyncio.sleep(0))

    with (
        patch.object(ws, "_async_cancel_and_wait", side_effect=_cancel_and_stop) as cancel_and_wait,
        patch.object(ws, "_replace_message_subscription") as replace_subscription,
        patch.object(ws, "_start_websocket_runner") as start_runner,
    ):
        await ws._async_restart_runner()

    cancel_and_wait.assert_awaited_once()
    replace_subscription.assert_not_called()
    start_runner.assert_not_called()
