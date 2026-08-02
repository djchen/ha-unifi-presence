"""Tests for WebSocket race conditions and task serialization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .websocket_helpers import make_websocket, wait_for_task, wait_for_websocket_start


@pytest.mark.parametrize(
    "restart_entry_point",
    ["restart_with_current_controller", "_schedule_reauth_and_restart"],
    ids=("current-controller-restart", "reauth-and-restart"),
)
async def test_restart_waits_for_previous_runner_cleanup(hass: HomeAssistant, restart_entry_point: str) -> None:
    """Test restarts serialize replacement startup behind runner cancellation."""
    ws, controller, _ = make_websocket(hass)
    first_started = asyncio.Event()
    cleanup_started = asyncio.Event()
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
                cleanup_started.set()
                await release_cleanup.wait()
                raise

        second_started.set()
        await asyncio.Event().wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    await first_started.wait()

    getattr(ws, restart_entry_point)()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

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

    ws.start()
    await wait_for_websocket_start(old_controller)

    first_task = ws.ws_task

    # Build a second controller to simulate the coordinator swapping it
    new_controller = AsyncMock()
    new_controller.messages = MagicMock()
    new_controller.messages.subscribe = MagicMock(return_value=MagicMock())
    new_controller.login = AsyncMock()
    new_hang = asyncio.Event()
    new_controller.start_websocket = AsyncMock(side_effect=new_hang.wait)

    current["api"] = new_controller

    ws.restart_with_current_controller()
    await wait_for_websocket_start(new_controller)

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

    ws.start()
    await wait_for_websocket_start(controller)

    # Simulate an in-flight _reconnect_task (e.g. from a prior health-check reconnect)
    stale_task = MagicMock()
    stale_task.cancel = MagicMock()
    ws._reconnect_task = stale_task

    ws.restart_with_current_controller()
    await wait_for_websocket_start(controller, count=2)

    # The stale _reconnect_task should have been cancelled and cleared
    stale_task.cancel.assert_called_once()
    # After reconnect(), _reconnect_task should be None (not the stale one)
    assert ws._reconnect_task is None

    ws.stop()


@pytest.mark.parametrize(
    "restart_entry_point",
    ["restart_with_current_controller", "_schedule_reauth_and_restart"],
    ids=("current-controller-restart", "reauth-and-restart"),
)
async def test_old_restart_task_does_not_clear_new_reconnect_task(
    hass: HomeAssistant, restart_entry_point: str
) -> None:
    """Test an older restart task cannot clear a newer reconnect task reference."""
    ws, controller, _ = make_websocket(hass)
    release_restart = asyncio.Event()
    restart_started = asyncio.Event()

    async def _block_restart() -> None:
        restart_started.set()
        await release_restart.wait()

    controller.start_websocket = AsyncMock(side_effect=_block_restart)

    ws.start()
    await wait_for_websocket_start(controller)

    getattr(ws, restart_entry_point)()
    first_task = ws._reconnect_task
    assert first_task is not None
    await restart_started.wait()

    replacement_task = hass.async_create_background_task(asyncio.sleep(3600), name="replacement_reconnect")
    ws._reconnect_task = replacement_task

    release_restart.set()
    await wait_for_task(first_task)

    assert ws._reconnect_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    await ws.stop_and_wait()


async def test_runner_ignores_cleanup_from_stale_task_reference(hass: HomeAssistant) -> None:
    """Test a stale runner finishing does not affect a newer ws_task reference."""
    ws, controller, _ = make_websocket(hass)
    release_runner = asyncio.Event()

    async def _start_websocket() -> None:
        await release_runner.wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    old_task = ws.ws_task
    assert old_task is not None
    replacement_task = hass.async_create_background_task(asyncio.sleep(3600), name="replacement_ws_task")
    ws.ws_task = replacement_task

    release_runner.set()
    await wait_for_task(old_task)

    assert ws.ws_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    ws.stop()
