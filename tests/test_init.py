"""Tests for the UniFi Presence integration setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence import (
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.unifi_presence.config_flow import _async_remove_deselected_entities
from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_mock_client, _mock_controller

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller_for_params"


def _make_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )
    entry.add_to_hass(hass)
    return entry


async def test_async_setup_entry(hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock) -> None:
    """Test that async_setup_entry creates coordinator, starts WS, and forwards platforms."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, UnifiPresenceCoordinator)
    assert isinstance(entry.runtime_data.websocket, UnifiPresenceWebsocket)


async def test_async_unload_entry(hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock) -> None:
    """Test that async_unload_entry stops WS and unloads platforms."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.websocket is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_async_unload_entry_releases_resources_even_when_platform_unload_fails(hass: HomeAssistant) -> None:
    """Test unload leaves runtime resources intact when platform unload fails."""
    entry = _make_config_entry(hass)
    websocket = MagicMock()
    websocket.stop_and_wait = AsyncMock()
    runtime_data = MagicMock(websocket=websocket)
    runtime_data.async_shutdown = AsyncMock()
    entry.runtime_data = runtime_data

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        unloaded = await async_unload_entry(hass, entry)

    assert unloaded is False
    websocket.stop_and_wait.assert_not_awaited()
    runtime_data.async_shutdown.assert_not_awaited()


async def test_async_unload_entry_shuts_down_coordinator(hass: HomeAssistant) -> None:
    """Test direct unload stops websocket and releases coordinator resources."""
    entry = _make_config_entry(hass)
    websocket = MagicMock()
    websocket.stop_and_wait = AsyncMock()
    runtime_data = MagicMock(websocket=websocket)
    runtime_data.async_shutdown = AsyncMock()
    entry.runtime_data = runtime_data

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        unloaded = await async_unload_entry(hass, entry)

    assert unloaded is True
    websocket.stop_and_wait.assert_awaited_once()
    runtime_data.async_shutdown.assert_awaited_once()


async def test_async_setup_entry_cleans_up_controller_when_platform_setup_fails(
    hass: HomeAssistant,
) -> None:
    """Test setup releases the controller if platform setup fails after refresh."""
    entry = _make_config_entry(hass)
    owned_session = MagicMock()
    owned_session.closed = False
    owned_session.detach = MagicMock()
    controller = MagicMock()
    controller.clients.get = MagicMock(return_value=None)
    controller.clients.update = AsyncMock()
    controller.clients_all.get = MagicMock(return_value=None)
    controller.clients_all.update = AsyncMock()

    async def _first_refresh(coordinator: UnifiPresenceCoordinator) -> None:
        coordinator._controller = controller

    controller._unifi_presence_owned_session = owned_session

    with (
        patch.object(
            UnifiPresenceCoordinator,
            "async_config_entry_first_refresh",
            autospec=True,
            side_effect=_first_refresh,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await async_setup_entry(hass, entry)

    owned_session.detach.assert_called_once_with()


async def test_shutdown_event_stops_websocket(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that shutdown stops the websocket and releases the controller."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ws = entry.runtime_data.websocket
    assert ws is not None
    ws.stop = MagicMock(wraps=ws.stop)
    entry.runtime_data.async_shutdown = AsyncMock()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    ws.stop.assert_called_once()
    entry.runtime_data.async_shutdown.assert_awaited_once()


async def test_unload_unregisters_shutdown_listener(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test normal unload removes the HA stop listener."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data
    ws = coordinator.websocket
    assert ws is not None
    ws.stop = MagicMock(wraps=ws.stop)
    owned_session = MagicMock()
    owned_session.closed = False
    owned_session.detach = MagicMock()
    mock_controller._unifi_presence_owned_session = owned_session

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert ws.stop.call_count == 1
    owned_session.detach.assert_called_once_with()

    ws.stop.reset_mock()
    owned_session.detach.reset_mock()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    ws.stop.assert_not_called()
    owned_session.detach.assert_not_called()


async def test_websocket_starts_after_shutdown_registration(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that websocket startup is deferred until teardown hooks are registered."""
    entry = _make_config_entry(hass)

    forward_complete = False
    shutdown_registered = False
    forward_started_after_shutdown_registration = False
    original_forward = hass.config_entries.async_forward_entry_setups
    original_listen_once = type(hass.bus).async_listen_once

    async def _forward_and_record(*args, **kwargs):
        nonlocal forward_complete, forward_started_after_shutdown_registration
        forward_started_after_shutdown_registration = shutdown_registered
        result = await original_forward(*args, **kwargs)
        forward_complete = True
        return result

    def _async_listen_once_and_record(bus, *args, **kwargs):
        nonlocal shutdown_registered
        if bus is hass.bus and args and args[0] == EVENT_HOMEASSISTANT_STOP:
            shutdown_registered = True
        return original_listen_once(bus, *args, **kwargs)

    def _assert_start_after_setup(_websocket: UnifiPresenceWebsocket) -> None:
        assert forward_complete is True
        assert shutdown_registered is True
        assert forward_started_after_shutdown_registration is True

    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=_forward_and_record),
        patch.object(type(hass.bus), "async_listen_once", new=_async_listen_once_and_record),
        patch.object(UnifiPresenceWebsocket, "start", autospec=True, side_effect=_assert_start_after_setup),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_while_stopping_releases_runtime_without_starting_websocket(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test setup during HA shutdown cleans up and skips WebSocket start."""
    entry = _make_config_entry(hass)
    hass.set_state(CoreState.stopping)

    async def _first_refresh(coordinator: UnifiPresenceCoordinator) -> None:
        coordinator._controller = mock_controller

    forward_setups = AsyncMock()

    try:
        with (
            patch.object(
                UnifiPresenceCoordinator,
                "async_config_entry_first_refresh",
                autospec=True,
                side_effect=_first_refresh,
            ),
            patch.object(hass.config_entries, "async_forward_entry_setups", forward_setups),
            patch.object(UnifiPresenceWebsocket, "start", autospec=True) as start,
        ):
            loaded = await async_setup_entry(hass, entry)
    finally:
        hass.set_state(CoreState.running)

    coordinator = entry.runtime_data
    assert loaded is False
    assert coordinator.controller is None
    assert coordinator.websocket is not None
    assert coordinator.websocket._stopped is True
    forward_setups.assert_not_awaited()
    start.assert_not_called()


async def test_setup_stopping_after_platform_forward_unloads_platforms(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test setup unloads forwarded platforms if HA stops during forwarding."""
    entry = _make_config_entry(hass)

    async def _first_refresh(coordinator: UnifiPresenceCoordinator) -> None:
        coordinator._controller = mock_controller

    async def _forward_and_stop(*args, **kwargs) -> None:
        hass.set_state(CoreState.stopping)

    unload_platforms = AsyncMock(return_value=True)

    try:
        with (
            patch.object(
                UnifiPresenceCoordinator,
                "async_config_entry_first_refresh",
                autospec=True,
                side_effect=_first_refresh,
            ),
            patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=_forward_and_stop)),
            patch.object(hass.config_entries, "async_unload_platforms", unload_platforms),
            patch.object(UnifiPresenceWebsocket, "start", autospec=True) as start,
        ):
            loaded = await async_setup_entry(hass, entry)
    finally:
        hass.set_state(CoreState.running)

    assert loaded is False
    unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
    start.assert_not_called()


async def test_entity_states_reflect_coordinator_data(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    enable_custom_integrations,
    mock_controller: MagicMock,
) -> None:
    """Test that device_tracker entities have correct states after full setup.

    No entity pre-seeding — entities should be enabled by default on a clean install.
    """
    now = int(dt_util.utcnow().timestamp())
    home_client = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone", hostname="dan-phone", ip="192.168.1.100", last_seen=now, is_wired=False
    )
    away_client = _make_mock_client(
        "11:22:33:44:55:66", name="Jane Phone", hostname="jane-phone", ip="192.168.1.101", last_seen=now - 120
    )

    def _get_client(mac: str) -> MagicMock | None:
        clients = {"aa:bb:cc:dd:ee:ff": home_client, "11:22:33:44:55:66": away_client}
        return clients.get(mac)

    mock_controller.clients.get = MagicMock(side_effect=_get_client)

    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    home_state = hass.states.get("device_tracker.dan_phone")
    away_state = hass.states.get("device_tracker.jane_phone")
    assert home_state is not None
    assert away_state is not None

    assert home_state.state == "home"
    assert away_state.state == "not_home"

    assert home_state.attributes["mac"] == "aa:bb:cc:dd:ee:ff"
    assert home_state.attributes["source_type"] == "router"

    assert away_state.attributes["mac"] == "11:22:33:44:55:66"
    assert away_state.attributes["source_type"] == "router"


async def test_offline_tracked_client_entity_is_not_home(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    enable_custom_integrations,
    mock_controller: MagicMock,
) -> None:
    """Test that offline selected tracked clients show as not_home, not unavailable."""
    now = int(dt_util.utcnow().timestamp())
    home_client = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone", hostname="dan-phone", ip="192.168.1.100", last_seen=now
    )

    def _get_client(mac: str) -> MagicMock | None:
        clients = {"aa:bb:cc:dd:ee:ff": home_client}
        return clients.get(mac)

    mock_controller.clients.get = MagicMock(side_effect=_get_client)

    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    missing_entry = entity_registry.async_get_entity_id(
        "device_tracker",
        DOMAIN,
        f"{entry.unique_id}-11:22:33:44:55:66",
    )

    assert missing_entry is not None
    missing_state = hass.states.get(missing_entry)
    assert missing_state is not None
    assert missing_state.state == "not_home"
    assert missing_state.attributes["friendly_name"] == "11:22:33:44:55:66"


async def test_options_flow_removes_explicitly_deselected_entity_registry_entries(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test options flow removes entities for deselected tracked clients."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    kept_entry = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.unique_id}-aa:bb:cc:dd:ee:ff",
        config_entry=entry,
        suggested_object_id="dan_phone",
    )
    removed_entry = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.unique_id}-11:22:33:44:55:66",
        config_entry=entry,
        suggested_object_id="jane_phone",
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
                CONF_AWAY_SECONDS: entry.options[CONF_AWAY_SECONDS],
                CONF_FALLBACK_POLL_INTERVAL: entry.options[CONF_FALLBACK_POLL_INTERVAL],
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entity_registry.async_get(kept_entry.entity_id) is not None
    assert entity_registry.async_get(removed_entry.entity_id) is None
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_options_flow_keeps_still_selected_missing_entity_registry_entries(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test options flow does not remove still-selected missing tracked clients."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    missing_entry = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.unique_id}-11:22:33:44:55:66",
        config_entry=entry,
        suggested_object_id="jane_phone",
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_AWAY_SECONDS: 120,
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
                CONF_FALLBACK_POLL_INTERVAL: entry.options[CONF_FALLBACK_POLL_INTERVAL],
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entity_registry.async_get(missing_entry.entity_id) is not None
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_data_update_does_not_schedule_listener_reload(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test reauth/reconfigure data updates rely on their own reload scheduling."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_USERNAME: "new-admin"},
        )
        await hass.async_block_till_done()

    schedule_reload.assert_not_called()


async def test_reconfigure_schedules_one_reload(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test reconfigure schedules a single reload after updating entry data."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    flow_controller = _mock_controller()
    with (
        patch(
            "custom_components.unifi_presence.config_flow.create_controller_for_params",
            return_value=flow_controller,
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: MOCK_CONFIG_DATA[CONF_HOST],
                CONF_PORT: MOCK_CONFIG_DATA[CONF_PORT],
                CONF_USERNAME: "new-admin",
                CONF_PASSWORD: "new-pass",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_options_flow_saves_loaded_entry_and_reloads(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test options flow saves options and schedules a reload."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
                CONF_AWAY_SECONDS: 120,
                CONF_FALLBACK_POLL_INTERVAL: 600,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_remove_deselected_entities_noop_when_removed_macs_empty(hass: HomeAssistant) -> None:
    """Test empty deselection sets skip entity-registry cleanup work."""
    entry = _make_config_entry(hass)

    with patch("custom_components.unifi_presence.config_flow.er.async_get") as async_get:
        _async_remove_deselected_entities(hass, entry, set())

    async_get.assert_not_called()
