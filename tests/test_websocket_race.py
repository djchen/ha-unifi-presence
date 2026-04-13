"""Tests for WebSocket race conditions and task serialization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .websocket_helpers import make_websocket


async def test_restart_with_current_controller_waits_for_previous_runner_cleanup(
    hass: HomeAssistant,
) -> None:
    """Test restart_with_current_controller() serializes replacement startup behind runner cancellation."""
    ws, controller, _ = make_websocket(hass)
    first_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    second_started = asyncio.Event()
    call_count = 0

    async def _start_websocket() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
                raise

        second_started.set()
        await asyncio.Event().wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    await first_started.wait()

    ws.restart_with_current_controller()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert second_started.is_set() is False

    release_cleanup.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)

    await ws.stop_and_wait()


async def test_schedule_reauth_and_restart_after_login_waits_for_runner_cleanup(
    hass: HomeAssistant,
) -> None:
    """Test _schedule_reauth_and_restart() waits for the old runner to finish before restarting."""
    ws, controller, _ = make_websocket(hass)
    first_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    second_started = asyncio.Event()
    call_count = 0

    async def _start_websocket() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
                raise

        second_started.set()
        await asyncio.Event().wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    await first_started.wait()

    ws._schedule_reauth_and_restart()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert second_started.is_set() is False

    release_cleanup.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)

    await ws.stop_and_wait()


async def test_restart_with_current_controller_resubscribes_and_restarts(hass: HomeAssistant) -> None:
    """Test restart_with_current_controller() re-subscribes to the new controller."""
    # Build the first controller and start the websocket against it
    old_controller = AsyncMock()
    old_controller.messages = MagicMock()
    old_controller.messages.subscribe = MagicMock(return_value=MagicMock())
    old_controller.login = AsyncMock()

    hang = asyncio.Event()
    old_controller.start_websocket = AsyncMock(side_effect=hang.wait)

    # Mutable holder so the lambda can be swapped to a new controller
    current = {"api": old_controller}
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: current["api"], on_message)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        await asyncio.sleep(0)

    first_task = ws.ws_task

    # Build a second controller to simulate the coordinator swapping it
    new_controller = AsyncMock()
    new_controller.messages = MagicMock()
    new_controller.messages.subscribe = MagicMock(return_value=MagicMock())
    new_controller.login = AsyncMock()
    new_hang = asyncio.Event()
    new_controller.start_websocket = AsyncMock(side_effect=new_hang.wait)

    current["api"] = new_controller

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.restart_with_current_controller()
        for _ in range(5):
            await asyncio.sleep(0)

    # Should have subscribed on the *new* controller and created a new task
    new_controller.messages.subscribe.assert_called_once()
    assert ws.ws_task is not first_task

    ws.stop()


async def test_restart_with_current_controller_noop_after_stop(hass: HomeAssistant) -> None:
    """Test restart_with_current_controller() is a no-op after stop()."""
    ws, controller, _ = make_websocket(hass)

    ws.start()
    ws.stop()

    controller.messages.subscribe.reset_mock()
    ws.restart_with_current_controller()

    controller.messages.subscribe.assert_not_called()
    assert ws.ws_task is None


async def test_restart_with_current_controller_cancels_inflight_reconnect_task(
    hass: HomeAssistant,
) -> None:
    """Test restart_with_current_controller() cancels an in-flight reconnect task."""
    ws, controller, _ = make_websocket(hass)

    hang = asyncio.Event()
    controller.start_websocket = AsyncMock(side_effect=hang.wait)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        await asyncio.sleep(0)

    # Simulate an in-flight _reconnect_task (e.g. from a prior health-check reconnect)
    stale_task = MagicMock()
    stale_task.cancel = MagicMock()
    ws._reconnect_task = stale_task

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.restart_with_current_controller()
        for _ in range(5):
            await asyncio.sleep(0)

    # The stale _reconnect_task should have been cancelled and cleared
    stale_task.cancel.assert_called_once()
    # After reconnect(), _reconnect_task should be None (not the stale one)
    assert ws._reconnect_task is None

    ws.stop()


async def test_restart_with_current_controller_old_task_does_not_clear_new_reconnect_task(
    hass: HomeAssistant,
) -> None:
    """Test an older restart task cannot clear a newer reconnect task reference."""
    ws, controller, _ = make_websocket(hass)
    release_restart = asyncio.Event()
    restart_started = asyncio.Event()

    async def _block_restart() -> None:
        restart_started.set()
        await release_restart.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_restart)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        await asyncio.sleep(0)

    ws.restart_with_current_controller()
    first_task = ws._reconnect_task
    assert first_task is not None
    await restart_started.wait()

    replacement_task = hass.async_create_background_task(asyncio.sleep(3600), name="replacement_reconnect")
    ws._reconnect_task = replacement_task

    release_restart.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ws._reconnect_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    await ws.stop_and_wait()


async def test_schedule_reauth_and_restart_after_login_old_task_does_not_clear_new_reconnect_task(
    hass: HomeAssistant,
) -> None:
    """Test an older _schedule_reauth_and_restart task cannot clear a newer reconnect task reference."""
    ws, controller, _ = make_websocket(hass)
    release_restart = asyncio.Event()
    restart_started = asyncio.Event()

    async def _block_restart() -> None:
        restart_started.set()
        await release_restart.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_restart)

    with patch("custom_components.unifi_presence.websocket.WEBSOCKET_READY_TIMEOUT", 0):
        ws.start()
        await asyncio.sleep(0)

    ws._schedule_reauth_and_restart()
    first_task = ws._reconnect_task
    assert first_task is not None
    await restart_started.wait()

    replacement_task = hass.async_create_background_task(asyncio.sleep(3600), name="replacement_reconnect")
    ws._reconnect_task = replacement_task

    release_restart.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ws._reconnect_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    await ws.stop_and_wait()
