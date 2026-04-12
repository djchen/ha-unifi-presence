"""Tests for the UniFi Presence integration setup and unload."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence import _async_remove_deselected_entities, async_setup_entry, async_unload_entry
from custom_components.unifi_presence.const import CONF_TRACKED_DEVICES, DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_mock_client

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller"


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


async def test_async_setup_entry_no_websocket_when_controller_none(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_setup_entry skips WebSocket when controller is None after first refresh."""
    entry = _make_config_entry(hass)

    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller),
        patch.object(UnifiPresenceCoordinator, "controller", new_callable=lambda: property(lambda self: None)),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, UnifiPresenceCoordinator)
    assert entry.runtime_data.websocket is None


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


async def test_async_unload_entry_keeps_websocket_running_when_platform_unload_fails(hass: HomeAssistant) -> None:
    """Test unload does not tear down the websocket before a failed platform unload."""
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


async def test_async_unload_entry_releases_controller_after_successful_platform_unload(hass: HomeAssistant) -> None:
    """Test unload releases the runtime controller after platform unload succeeds."""
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
    controller._unifi_presence_owned_session = owned_session
    controller.clients.get = MagicMock(return_value=None)
    controller.clients.update = AsyncMock()
    controller.clients_all.get = MagicMock(return_value=None)
    controller.clients_all.update = AsyncMock()

    async def _first_refresh(coordinator: UnifiPresenceCoordinator) -> None:
        coordinator._controller = controller

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


async def test_websocket_starts_after_shutdown_registration(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that websocket startup is deferred until teardown hooks are registered."""
    entry = _make_config_entry(hass)

    forward_complete = False
    shutdown_registered = False
    original_forward = hass.config_entries.async_forward_entry_setups
    original_async_on_unload = entry.async_on_unload

    async def _forward_and_record(*args, **kwargs):
        nonlocal forward_complete
        result = await original_forward(*args, **kwargs)
        forward_complete = True
        return result

    def _async_on_unload_and_record(*args, **kwargs):
        nonlocal shutdown_registered
        shutdown_registered = True
        return original_async_on_unload(*args, **kwargs)

    def _assert_start_after_setup(_websocket: UnifiPresenceWebsocket) -> None:
        assert forward_complete is True
        assert shutdown_registered is True

    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=_forward_and_record),
        patch.object(entry, "async_on_unload", side_effect=_async_on_unload_and_record),
        patch.object(UnifiPresenceWebsocket, "start", autospec=True, side_effect=_assert_start_after_setup),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_entity_states_reflect_coordinator_data(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that device_tracker entities have correct states after full setup.

    No entity pre-seeding — entities should be enabled by default on a clean install.
    """
    now = int(time.time())
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
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that offline selected tracked clients show as not_home, not unavailable."""
    now = int(time.time())
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


async def test_options_update_removes_explicitly_deselected_entity_registry_entries(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test options updates remove entities for deselected tracked clients."""
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

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    await hass.async_block_till_done()

    assert entity_registry.async_get(kept_entry.entity_id) is not None
    assert entity_registry.async_get(removed_entry.entity_id) is None


async def test_options_update_keeps_still_selected_missing_entity_registry_entries(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test options updates do not remove still-selected missing tracked clients."""
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

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]},
    )
    await hass.async_block_till_done()

    assert entity_registry.async_get(missing_entry.entity_id) is not None


async def test_remove_deselected_entities_noop_when_removed_macs_empty(hass: HomeAssistant) -> None:
    """Test empty deselection sets skip entity-registry cleanup work."""
    entry = _make_config_entry(hass)

    with patch("custom_components.unifi_presence.er.async_get") as async_get:
        _async_remove_deselected_entities(hass, entry, set())

    async_get.assert_not_called()
