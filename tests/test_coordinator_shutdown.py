"""Deterministic shutdown barriers for controller and store ownership."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import (
    _assert_session_cleanup,
    _attach_mock_session,
    _make_mock_client,
    make_mock_controller,
)

PATCH_CREATE = "custom_components.unifi_presence.coordinator.create_controller_for_params"


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> AsyncGenerator[UnifiPresenceCoordinator]:
    """Provide runtime cleanup even if an assertion fails."""
    runtime = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    yield runtime
    await runtime.async_shutdown()


def assert_stopped(coordinator: UnifiPresenceCoordinator) -> None:
    """Verify successful cleanup left no runtime resource or scheduling handle."""
    assert coordinator._stopping
    assert coordinator._shutdown_task.done()
    assert coordinator.controller is None
    assert coordinator._client_states == {}
    assert coordinator._cancel_heartbeat_check is None
    assert coordinator._scheduled_heartbeat_ts is None
    assert coordinator._unsub_refresh is None
    assert coordinator._debounced_refresh._timer_task is None
    assert coordinator.update_interval is None
    assert not coordinator._client_store_lock.locked()


@pytest.mark.parametrize("replacement", [False, True])
async def test_shutdown_disposes_late_controller_before_completion(
    hass: HomeAssistant, coordinator: UnifiPresenceCoordinator, replacement: bool
) -> None:
    """Initial and reauth factories cannot install a controller after stopping."""
    original = make_mock_controller()
    original_session = _attach_mock_session(original)
    if replacement:
        coordinator._controller = original
        original.clients.update_mock.side_effect = aiounifi.LoginRequired
    late = make_mock_controller()
    late_session = _attach_mock_session(late)
    creating = asyncio.Event()
    release = asyncio.Event()
    disposing = asyncio.Event()
    release_disposal = asyncio.Event()

    async def create(*args, **kwargs):
        creating.set()
        await release.wait()
        return late

    async def close(controller):
        assert controller is late
        disposing.set()
        await release_disposal.wait()
        late_session.detach()

    with patch(PATCH_CREATE, side_effect=create):
        refresh = hass.async_create_task(coordinator.async_refresh())
        await creating.wait()
        with patch("custom_components.unifi_presence.coordinator.async_close_controller", side_effect=close):
            shutdown = hass.async_create_task(coordinator.async_shutdown())
            assert coordinator._stopping
            assert not shutdown.done()
            late_session.detach.assert_not_called()
            release.set()
            await disposing.wait()
            assert coordinator.controller is None
            assert not shutdown.done()
            release_disposal.set()
            await refresh
            await shutdown

    assert coordinator.data is None
    late.clients.update_mock.assert_not_called()
    _assert_session_cleanup(late_session)
    _assert_session_cleanup(original_session, owned=replacement)
    assert_stopped(coordinator)


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("blocked_store", ["clients", "clients_all"])
async def test_shutdown_drains_refresh_without_publication(
    hass: HomeAssistant,
    coordinator: UnifiPresenceCoordinator,
    scheduled: bool,
    blocked_store: str,
) -> None:
    """Public and interval refreshes keep the last snapshot and never rearm timers."""
    controller = make_mock_controller()
    session = _attach_mock_session(controller)
    coordinator._controller = controller
    mac = coordinator.tracked_devices[0]
    now = int(dt_util.utcnow().timestamp())
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Before", "last_seen": now}))
    previous = coordinator.data
    controller.clients[mac] = _make_mock_client(mac, name="After", last_seen=now)
    listener = MagicMock()
    coordinator.async_add_listener(listener)
    started = asyncio.Event()
    release = asyncio.Event()

    async def update():
        started.set()
        await release.wait()
        assert not session.closed

    with patch.object(getattr(controller, blocked_store), "update_mock", side_effect=update):
        if scheduled:
            coordinator._async_unsub_refresh()
        refresh = hass.async_create_task(
            coordinator._handle_refresh_interval() if scheduled else coordinator.async_refresh()
        )
        await started.wait()
        shutdown = hass.async_create_task(coordinator.async_shutdown())
        assert not shutdown.done()
        session.detach.assert_not_called()
        assert coordinator._unsub_refresh is None
        assert coordinator._cancel_heartbeat_check is None
        release.set()
        await refresh
        await shutdown

    listener.assert_not_called()
    assert coordinator.data is previous
    assert isinstance(coordinator.last_exception, asyncio.CancelledError)
    _assert_session_cleanup(session)
    assert_stopped(coordinator)


@pytest.mark.parametrize("discovery_first", [False, True])
@pytest.mark.parametrize("auth_error", [False, True])
async def test_shutdown_fences_store_lock_waiters(
    hass: HomeAssistant, coordinator: UnifiPresenceCoordinator, discovery_first: bool, auth_error: bool
) -> None:
    """Queued discovery/polling cannot borrow or replace the retiring controller."""
    controller = make_mock_controller()
    coordinator._controller = controller
    session = _attach_mock_session(controller)
    coordinator.websocket = MagicMock(stop_and_wait=AsyncMock())
    started = asyncio.Event()
    release = asyncio.Event()

    async def update():
        started.set()
        await release.wait()
        if auth_error:
            raise aiounifi.LoginRequired

    async def discovery():
        # An in-flight discovery auth failure still follows its existing flow policy.
        try:
            return await coordinator.async_fetch_available_clients()
        except aiounifi.LoginRequired:
            assert discovery_first
            assert auth_error
            return None

    with (
        patch.object(controller.clients, "update_mock", side_effect=update) as active_update,
        patch(PATCH_CREATE) as create,
    ):
        holder = hass.async_create_task(discovery() if discovery_first else coordinator.async_refresh())
        await started.wait()
        waiter = hass.async_create_task(coordinator.async_refresh() if discovery_first else discovery())
        assert coordinator._client_store_lock.locked()
        shutdown = hass.async_create_task(coordinator.async_shutdown())
        assert not shutdown.done()
        session.detach.assert_not_called()
        release.set()
        await holder
        await waiter
        await shutdown

    active_update.assert_awaited_once()
    controller.clients_all.update_mock.assert_awaited_once()
    coordinator.websocket.restart_with_current_controller.assert_not_called()
    create.assert_not_called()
    _assert_session_cleanup(session)
    assert_stopped(coordinator)


async def test_concurrent_shutdown_shields_cleanup_from_cancelled_waiter(
    hass: HomeAssistant, coordinator: UnifiPresenceCoordinator
) -> None:
    """All callers join one operation, even when one caller is cancelled."""
    controller = make_mock_controller()
    coordinator._controller = controller
    session = _attach_mock_session(controller)
    async with coordinator._client_store_lock:
        first = hass.async_create_task(coordinator.async_shutdown())
        shared = coordinator._shutdown_task
        second = hass.async_create_task(coordinator.async_shutdown())
        assert coordinator._shutdown_task is shared
        assert not first.done()
        assert not second.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not shared.cancelling()
        assert not second.done()
        session.detach.assert_not_called()

    await second
    await coordinator.async_shutdown()
    _assert_session_cleanup(session)
    assert_stopped(coordinator)


async def test_stopping_rejects_late_callbacks_and_runtime_work(
    hass: HomeAssistant, coordinator: UnifiPresenceCoordinator
) -> None:
    """Callbacks already queued at shutdown cannot write state or start I/O."""
    controller = make_mock_controller()
    coordinator._controller = controller
    coordinator.websocket = UnifiPresenceWebsocket(hass, lambda: coordinator.controller, coordinator.process_message)
    mac = coordinator.tracked_devices[0]
    now = int(dt_util.utcnow().timestamp())
    coordinator.process_message(MagicMock(data={"mac": mac, "last_seen": now}))
    previous = coordinator.data
    listener = MagicMock()
    coordinator.async_add_listener(listener)
    async with coordinator._client_store_lock:
        shutdown = hass.async_create_task(coordinator.async_shutdown())
        coordinator.process_message(MagicMock(data={"mac": mac, "name": "Late", "last_seen": now + 300}))
        coordinator._async_check_heartbeat_expiry()
        coordinator._reschedule_heartbeat_check()
        coordinator.async_update_listeners()
        await coordinator.async_refresh()
        await coordinator.async_request_refresh()
        assert await coordinator.async_fetch_available_clients() is None
        with pytest.raises(asyncio.CancelledError):
            await coordinator._async_update_data()
        with pytest.raises(asyncio.CancelledError):
            await coordinator._ensure_controller()
        coordinator.websocket.restart_with_current_controller()
        coordinator.websocket._schedule_reauth_and_restart()
        coordinator.websocket._schedule_retry()
        coordinator.websocket._start_reconnect_task(AsyncMock())
        assert coordinator.data is previous
        assert coordinator._get_known_last_seen(mac) == now
        listener.assert_not_called()
    await shutdown

    controller.clients.update_mock.assert_not_called()
    controller.clients_all.update_mock.assert_not_called()
    controller.login.assert_not_called()
    assert coordinator.websocket._cancel_retry is None
    assert coordinator.websocket._cancel_watchdog is None
    assert_stopped(coordinator)


async def test_shutdown_propagates_cleanup_failure_without_detaching(
    coordinator: UnifiPresenceCoordinator,
) -> None:
    """A failed dependency drain must not masquerade as successful cleanup."""
    controller = make_mock_controller()
    coordinator._controller = controller
    session = _attach_mock_session(controller)
    coordinator.websocket = MagicMock(stop_and_wait=AsyncMock(side_effect=RuntimeError("cleanup failed")))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await coordinator.async_shutdown()
    session.detach.assert_not_called()
    assert coordinator.controller is controller
    coordinator.websocket.stop_and_wait.assert_awaited_once()
    # The retained failure is intentional; do not rethrow it from fixture teardown.
    coordinator.websocket = None
    coordinator._shutdown_task = None
    await coordinator.async_shutdown()
