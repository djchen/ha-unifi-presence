"""Tests for WebSocket race conditions and task serialization."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import make_mock_controller
from .websocket_helpers import block_websocket, make_websocket, wait_for_task


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
    await asyncio.wait_for(first_started.wait(), timeout=1)

    getattr(ws, restart_entry_point)()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    assert second_started.is_set() is False

    release_cleanup.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)

    await ws.stop_and_wait()


async def test_restart_with_current_controller_resubscribes_and_restarts(hass: HomeAssistant) -> None:
    """Test restart_with_current_controller() re-subscribes to the new controller."""
    # Build the first controller and start the websocket against it
    old_controller = make_mock_controller()
    old_started = block_websocket(old_controller)

    # Mutable holder so the lambda can be swapped to a new controller
    current = {"api": old_controller}
    on_message = MagicMock()
    ws = UnifiPresenceWebsocket(hass, lambda: current["api"], on_message)

    ws.start()
    await asyncio.wait_for(old_started.wait(), timeout=1)

    first_task = ws.ws_task
    assert first_task is not None

    # Build a second controller to simulate the coordinator swapping it
    new_controller = make_mock_controller()
    new_started = block_websocket(new_controller)

    current["api"] = new_controller

    ws.restart_with_current_controller()
    await asyncio.wait_for(new_started.wait(), timeout=1)

    # Should have subscribed on the *new* controller and created a new task
    old_controller.messages.subscribe.return_value.assert_called_once()
    new_controller.messages.subscribe.assert_called_once()
    assert first_task.done()
    assert ws.ws_task is not first_task

    await ws.stop_and_wait()


async def test_restart_with_current_controller_noop_after_stop(hass: HomeAssistant) -> None:
    """Test restart_with_current_controller() is a no-op after stop()."""
    ws, controller, _ = make_websocket(hass)

    ws.start()
    await ws.stop_and_wait()

    controller.messages.subscribe.reset_mock()
    ws.restart_with_current_controller()

    controller.messages.subscribe.assert_not_called()
    assert ws.ws_task is None


async def test_restart_with_current_controller_cancels_inflight_reconnect_task(
    hass: HomeAssistant,
) -> None:
    """Test restart_with_current_controller() cancels an in-flight reconnect task."""
    ws, controller, _ = make_websocket(hass)
    started = block_websocket(controller)

    ws.start()
    await asyncio.wait_for(started.wait(), timeout=1)

    # Simulate an in-flight _reconnect_task (e.g. from a prior health-check reconnect)
    stale_task = MagicMock()
    stale_task.cancel = MagicMock()
    ws._reconnect_task = stale_task

    started.clear()
    ws.restart_with_current_controller()
    await wait_for_task(ws._reconnect_task)
    await asyncio.wait_for(started.wait(), timeout=1)

    # The stale _reconnect_task should have been cancelled and cleared
    stale_task.cancel.assert_called_once()
    # After reconnect(), _reconnect_task should be None (not the stale one)
    assert ws._reconnect_task is None

    await ws.stop_and_wait()


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
    runner_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _block_runner_cleanup() -> None:
        runner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            raise

    controller.start_websocket = AsyncMock(side_effect=_block_runner_cleanup)

    ws.start()
    await asyncio.wait_for(runner_started.wait(), timeout=1)

    getattr(ws, restart_entry_point)()
    first_task = ws._reconnect_task
    assert first_task is not None
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    replacement_task = hass.async_create_background_task(asyncio.Event().wait(), name="replacement_reconnect")
    ws._reconnect_task = replacement_task

    release_cleanup.set()
    await wait_for_task(first_task)

    assert ws._reconnect_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    await ws.stop_and_wait()


async def test_runner_ignores_cleanup_from_stale_task_reference(hass: HomeAssistant) -> None:
    """Test a stale runner finishing does not affect a newer ws_task reference."""
    ws, controller, _ = make_websocket(hass)
    runner_started = asyncio.Event()
    release_runner = asyncio.Event()

    async def _start_websocket() -> None:
        runner_started.set()
        await release_runner.wait()

    controller.start_websocket = AsyncMock(side_effect=_start_websocket)

    ws.start()
    await asyncio.wait_for(runner_started.wait(), timeout=1)
    old_task = ws.ws_task
    assert old_task is not None
    replacement_task = hass.async_create_background_task(asyncio.Event().wait(), name="replacement_ws_task")
    ws.ws_task = replacement_task

    release_runner.set()
    await wait_for_task(old_task)

    assert ws.ws_task is replacement_task

    replacement_task.cancel()
    with suppress(asyncio.CancelledError):
        await replacement_task

    await ws.stop_and_wait()
