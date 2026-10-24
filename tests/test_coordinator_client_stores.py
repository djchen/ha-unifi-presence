"""Regression tests for serialized polling and runtime options discovery."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from aiounifi.controller import Controller
from aiounifi.models.api import ApiRequest
from aiounifi.models.configuration import Configuration
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.helpers import resolve_client_display_name

from .conftest import (
    MOCK_OPTIONS,
    PATCH_CREATE_CONTROLLER,
    _assert_session_cleanup,
    _attach_mock_session,
    _get_tracked_device_options,
    add_mock_config_entry,
)

MAC = "aa:bb:cc:dd:ee:ff"


def _real_controller() -> Controller:
    """Build a real controller and stores without opening a network session."""
    controller = Controller(Configuration(MagicMock(), host="unifi.test", username="user", password="password"))
    _attach_mock_session(controller)
    return controller


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> AsyncGenerator[UnifiPresenceCoordinator]:
    """Provide a loaded entry with a real runtime controller and cleanup."""
    entry = add_mock_config_entry(hass, options=MOCK_OPTIONS)
    runtime = UnifiPresenceCoordinator(hass, entry)
    runtime._controller = _real_controller()
    entry.runtime_data = runtime
    entry.mock_state(hass, ConfigEntryState.LOADED)
    try:
        yield runtime
    finally:
        await runtime.async_shutdown()


@pytest.mark.usefixtures("_bypass_setup", "freezer")
@pytest.mark.parametrize("options_first", [True, False])
async def test_options_and_poll_serialize_refresh_and_reads(  # noqa: PLR0915 - Keep event ordering together.
    hass: HomeAssistant,
    coordinator: UnifiPresenceCoordinator,
    options_first: bool,
) -> None:
    """Neither operation can overwrite stores before the first consumes them."""
    controller = coordinator.controller
    assert controller is not None
    now = int(dt_util.utcnow().timestamp())
    coordinator._set_last_seen(MAC, now - 120)
    first_active_started = asyncio.Event()
    first_history_started = asyncio.Event()
    active_processed = asyncio.Event()
    release_active = asyncio.Event()
    release_history = asyncio.Event()
    second_started = asyncio.Event()
    calls = {"active": 0, "history": 0}
    poll_reads: list[str] = []
    discovery_reads: list[str] = []

    def poll_name(mac: str, **kwargs: Any) -> str:
        assert coordinator._client_store_lock.locked()
        poll_reads.append(mac)
        return resolve_client_display_name(mac, **kwargs)

    def discovery_name(mac: str, **kwargs: Any) -> str:
        assert coordinator._client_store_lock.locked()
        discovery_reads.append(mac)
        return resolve_client_display_name(mac, **kwargs)

    async def request(api_request: ApiRequest) -> dict[str, object]:
        active = api_request is controller.clients.api_request
        source = "active" if active else "history"
        calls[source] += 1
        first = calls[source] == 1
        if first:
            (first_active_started if active else first_history_started).set()
            await (release_active if active else release_history).wait()
        elif options_first:
            assert discovery_reads == [MAC]
        else:
            assert poll_reads == list(coordinator.tracked_devices)
            assert coordinator._get_known_last_seen(MAC) == now
        if not active:
            return {"data": []}
        discovery = first == options_first
        return {
            "data": [
                {"mac": MAC, "name": "Discovery" if discovery else "Poll", "last_seen": now - 120 if discovery else now}
            ]
        }

    controller.clients.subscribe(lambda *_: active_processed.set())

    async def options() -> dict[str, Any]:
        return await hass.config_entries.options.async_init(coordinator.config_entry.entry_id)

    discover_clients = coordinator.async_fetch_available_clients

    async def discovery() -> dict[str, str] | None:
        if not options_first:
            # Signal at the real discovery entry point, after HA flow preparation.
            second_started.set()
        return await discover_clients()

    async def second_operation() -> object:
        if options_first:
            second_started.set()
        return await (coordinator.async_refresh() if options_first else options())

    with (
        patch.object(controller, "request", side_effect=request),
        patch("custom_components.unifi_presence.coordinator.resolve_client_display_name", side_effect=poll_name),
        patch("custom_components.unifi_presence.helpers.resolve_client_display_name", side_effect=discovery_name),
        patch.object(coordinator, "async_fetch_available_clients", side_effect=discovery),
        patch(PATCH_CREATE_CONTROLLER) as fallback_login,
    ):
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            first = tasks.create_task(options() if options_first else coordinator.async_refresh())
            await first_active_started.wait()
            await first_history_started.wait()
            second = tasks.create_task(second_operation())
            await second_started.wait()
            assert calls == {"active": 1, "history": 1}
            release_active.set()
            await active_processed.wait()
            # The active store is already changed, but history and consumers are pending.
            assert calls == {"active": 1, "history": 1}
            assert poll_reads == discovery_reads == []
            release_history.set()

    result = first.result() if options_first else second.result()
    assert result["type"] is FlowResultType.FORM
    assert _get_tracked_device_options(result)[MAC] == f"Discovery ({MAC})"
    fallback_login.assert_not_called()
    assert calls == {"active": 2, "history": 2}
    assert coordinator.data[MAC] == (True, "Poll")
    assert coordinator._get_known_last_seen(MAC) == now
    _assert_session_cleanup(controller._unifi_presence_owned_session, owned=False)


@pytest.mark.usefixtures("freezer")
async def test_discovery_waiting_for_reauth_uses_replacement(
    coordinator: UnifiPresenceCoordinator,
) -> None:
    """Queued discovery selects the replacement only after acquiring the lock."""
    original = coordinator.controller
    assert original is not None
    replacement = _real_controller()
    auth_started = asyncio.Event()
    release_auth = asyncio.Event()
    discovery_started = asyncio.Event()
    now = int(dt_util.utcnow().timestamp())
    coordinator.websocket = MagicMock(stop_and_wait=AsyncMock())

    async def expired_request(api_request: ApiRequest) -> dict[str, object]:
        if api_request is original.clients.api_request:
            auth_started.set()
            await release_auth.wait()
            raise aiounifi.LoginRequired
        return {"data": []}

    async def discovery() -> dict[str, str] | None:
        discovery_started.set()
        return await coordinator.async_fetch_available_clients()

    with (
        patch.object(original, "request", side_effect=expired_request) as original_request,
        patch.object(
            replacement, "request", return_value={"data": [{"mac": MAC, "name": "New", "last_seen": now}]}
        ) as replacement_request,
        patch(
            "custom_components.unifi_presence.coordinator.create_controller_for_params", return_value=replacement
        ) as create_controller,
    ):
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            tasks.create_task(coordinator.async_refresh())
            await auth_started.wait()
            queued = tasks.create_task(discovery())
            await discovery_started.wait()
            assert original_request.await_count == 2
            replacement_request.assert_not_called()
            release_auth.set()

    assert queued.result() == {MAC: f"New ({MAC})"}
    assert coordinator.controller is replacement
    assert original_request.await_count == 2
    assert replacement_request.await_count == 4
    create_controller.assert_awaited_once()
    coordinator.websocket.restart_with_current_controller.assert_called_once_with()
    _assert_session_cleanup(original._unifi_presence_owned_session)
    _assert_session_cleanup(replacement._unifi_presence_owned_session, owned=False)


@pytest.mark.usefixtures("freezer")
async def test_websocket_observation_during_locked_refresh_stays_monotonic(
    coordinator: UnifiPresenceCoordinator,
) -> None:
    """WebSocket pushes remain synchronous and newer than a delayed REST response."""
    controller = coordinator.controller
    assert controller is not None
    started = asyncio.Event()
    release = asyncio.Event()
    now = int(dt_util.utcnow().timestamp())

    async def request(api_request: ApiRequest) -> dict[str, object]:
        if api_request is controller.clients.api_request:
            started.set()
            await release.wait()
            return {"data": [{"mac": MAC, "last_seen": now - 120}]}
        return {"data": []}

    with patch.object(controller, "request", side_effect=request):
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            tasks.create_task(coordinator.async_refresh())
            await started.wait()
            coordinator.process_message(MagicMock(data={"mac": MAC, "last_seen": now, "name": "Push"}))
            assert coordinator.data[MAC] == (True, "Push")
            release.set()

    assert coordinator.data[MAC] == (True, "Push")
    assert coordinator._get_known_last_seen(MAC) == now


@pytest.mark.parametrize("discovery", [True, False])
@pytest.mark.parametrize("error", [TimeoutError, ValueError])
async def test_store_lock_released_after_error(
    coordinator: UnifiPresenceCoordinator, discovery: bool, error: type[Exception]
) -> None:
    """Expected and unexpected refresh errors release the lock for later work."""
    controller = coordinator.controller
    assert controller is not None
    expected = (RuntimeError if discovery else UpdateFailed) if error is TimeoutError else error
    with patch.object(controller, "request", side_effect=error), pytest.raises(expected):
        await (coordinator.async_fetch_available_clients() if discovery else coordinator._async_update_data())

    with patch.object(controller, "request", return_value={"data": []}):
        async with asyncio.timeout(5):
            assert await coordinator.async_fetch_available_clients() == {}
            await coordinator._async_update_data()


@pytest.mark.parametrize("discovery", [True, False])
@pytest.mark.parametrize("cancel_waiter", [True, False])
async def test_store_lock_cancellation(
    coordinator: UnifiPresenceCoordinator, discovery: bool, cancel_waiter: bool
) -> None:
    """Cancellation while holding or waiting for the lock does not strand it."""
    controller = coordinator.controller
    assert controller is not None
    started = asyncio.Event()
    release = asyncio.Event()
    waiter_started = asyncio.Event()

    async def request(_: ApiRequest) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"data": []}

    async def operation() -> object:
        waiter_started.set()
        return await (coordinator.async_fetch_available_clients() if discovery else coordinator._async_update_data())

    with patch.object(controller, "request", side_effect=request) as requests:
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            if cancel_waiter:
                holder_operation = (
                    coordinator._async_update_data() if discovery else coordinator.async_fetch_available_clients()
                )
            else:
                holder_operation = operation()
            holder = tasks.create_task(holder_operation)
            await started.wait()
            cancelled = tasks.create_task(operation()) if cancel_waiter else holder
            await waiter_started.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            if cancel_waiter:
                assert coordinator._client_store_lock.locked()
                assert requests.await_count == 2
            release.set()

        async with asyncio.timeout(5):
            assert await coordinator.async_fetch_available_clients() == {}
            await coordinator._async_update_data()

    _assert_session_cleanup(controller._unifi_presence_owned_session, owned=False)


@pytest.mark.parametrize("cached", [True, False])
async def test_runtime_discovery_tolerates_refresh_failures(
    coordinator: UnifiPresenceCoordinator, cached: bool
) -> None:
    """Discovery accepts either refreshed history or cache without publishing state."""
    controller = coordinator.controller
    assert controller is not None
    if cached:
        controller.clients_all.process_item({"mac": MAC, "name": "History"})

    async def request(api_request: ApiRequest) -> dict[str, object]:
        if cached or api_request is controller.clients.api_request:
            raise TimeoutError
        return {"data": [{"mac": MAC, "name": "History"}]}

    with (
        patch.object(controller, "request", side_effect=request),
        patch.object(coordinator, "async_update_listeners") as publish,
        patch.object(coordinator, "_reschedule_heartbeat_check") as schedule,
    ):
        assert await coordinator.async_fetch_available_clients() == {MAC: f"History ({MAC})"}

    assert coordinator.data is None
    publish.assert_not_called()
    schedule.assert_not_called()
    _assert_session_cleanup(controller._unifi_presence_owned_session, owned=False)


async def test_runtime_discovery_blank_active_overrides_historical_label(
    coordinator: UnifiPresenceCoordinator,
) -> None:
    """Blank active metadata overrides historical names for discovery only."""
    controller = coordinator.controller
    assert controller is not None

    async def request(api_request: ApiRequest) -> dict[str, object]:
        name = "" if api_request is controller.clients.api_request else "Historical"
        return {"data": [{"mac": MAC, "name": name}]}

    with patch.object(controller, "request", side_effect=request):
        assert await coordinator.async_fetch_available_clients() == {MAC: f"{MAC} ({MAC})"}
        assert (await coordinator._async_update_data())[MAC] == (False, MAC)


@pytest.mark.usefixtures("_bypass_setup")
async def test_runtime_discovery_distinguishes_empty_from_unavailable(
    hass: HomeAssistant, coordinator: UnifiPresenceCoordinator
) -> None:
    """An empty result must not trigger fallback; no controller must not log in."""
    controller = coordinator.controller
    assert controller is not None
    with (
        patch.object(controller, "request", return_value={"data": []}),
        patch(PATCH_CREATE_CONTROLLER) as fallback,
    ):
        result = await hass.config_entries.options.async_init(coordinator.config_entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {}
        fallback.assert_not_called()

    await coordinator._async_close_runtime_controller()
    with patch("custom_components.unifi_presence.coordinator.create_controller_for_params") as create:
        assert await coordinator.async_fetch_available_clients() is None
        create.assert_not_called()


async def test_poll_after_shutdown_aborts_before_refresh(coordinator: UnifiPresenceCoordinator) -> None:
    """Stopped runtimes reject polling while discovery still signals unavailability."""
    controller = coordinator.controller
    await coordinator.async_shutdown()

    with patch.object(coordinator, "_async_refresh_with_reauth", return_value=controller) as refresh:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._async_update_data()

        assert not coordinator._client_store_lock.locked()
        assert await coordinator.async_fetch_available_clients() is None
        refresh.assert_not_called()

    assert coordinator.controller is None
    assert coordinator._client_states == {}
    assert coordinator._cancel_heartbeat_check is None
