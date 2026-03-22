"""Tests for the UniFi Presence integration setup and unload."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence import async_remove_config_entry_device
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


async def test_remove_config_entry_device_allows_untracked_mac(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_remove_config_entry_device allows removal when MAC is not tracked."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Create a device whose MAC is NOT in the tracked set
    device_reg = dr.async_get(hass)
    device = device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, "22:33:44:55:66:77")},
    )

    result = await async_remove_config_entry_device(hass, entry, device)
    assert result is True


async def test_remove_config_entry_device_blocks_tracked_mac(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_remove_config_entry_device blocks removal when MAC is still tracked."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The conftest MOCK_OPTIONS tracks "aa:bb:cc:dd:ee:ff" — create a device with that MAC
    tracked_mac = next(iter(MOCK_OPTIONS[CONF_TRACKED_DEVICES]))
    device_reg = dr.async_get(hass)
    device = device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, tracked_mac)},
    )

    result = await async_remove_config_entry_device(hass, entry, device)
    assert result is False


async def test_stale_device_cleanup_on_setup(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that stale device entries are removed during setup."""
    entry = _make_config_entry(hass)

    # Pre-create a device with a MAC that is NOT in tracked_devices
    stale_mac = "99:99:99:99:99:99"
    device_reg = dr.async_get(hass)
    device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, stale_mac)},
    )

    # Also pre-create a device with a tracked MAC to ensure it is NOT removed
    tracked_mac = "aa:bb:cc:dd:ee:ff"
    device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, tracked_mac)},
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Stale device should be removed
    stale_device = device_reg.async_get_device(connections={(CONNECTION_NETWORK_MAC, stale_mac)})
    assert stale_device is None

    # Tracked device should still exist
    tracked_device = device_reg.async_get_device(connections={(CONNECTION_NETWORK_MAC, tracked_mac)})
    assert tracked_device is not None


async def test_shutdown_event_stops_websocket(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that firing EVENT_HOMEASSISTANT_STOP calls websocket.stop()."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ws = entry.runtime_data.websocket
    assert ws is not None
    ws.stop = MagicMock(wraps=ws.stop)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    # The _async_shutdown listener should have called ws.stop()
    ws.stop.assert_called_once()


async def test_entity_states_reflect_coordinator_data(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that device_tracker entities have correct states after full setup."""
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

    # Pre-register entities as enabled so the test environment doesn't disable them
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "device_tracker",
        DOMAIN,
        "aa:bb:cc:dd:ee:ff",
        config_entry=entry,
        disabled_by=None,
    )
    entity_reg.async_get_or_create(
        "device_tracker",
        DOMAIN,
        "11:22:33:44:55:66",
        config_entry=entry,
        disabled_by=None,
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # Verify entity states in the HA state machine
    home_state = hass.states.get("device_tracker.unifi_presence_aa_bb_cc_dd_ee_ff")
    away_state = hass.states.get("device_tracker.unifi_presence_11_22_33_44_55_66")
    assert home_state is not None
    assert away_state is not None

    assert home_state.state == "home"
    assert away_state.state == "not_home"

    # Verify attributes flow through
    assert home_state.attributes["ip"] == "192.168.1.100"
    assert home_state.attributes["mac"] == "aa:bb:cc:dd:ee:ff"
    assert home_state.attributes["host_name"] == "dan-phone"
    assert home_state.attributes["is_wired"] is False
    assert home_state.attributes["source_type"] == "router"
