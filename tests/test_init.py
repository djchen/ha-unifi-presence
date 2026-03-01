"""Tests for the UniFi Presence integration setup and unload."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence import async_remove_config_entry_device
from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

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


async def test_async_setup_entry_registers_hub_device(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_setup_entry registers hub device for via_device references."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device_reg = dr.async_get(hass)
    device = device_reg.async_get_device({(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE


async def test_remove_config_entry_device_allows_non_service(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_remove_config_entry_device allows removal of non-service devices."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Create a fake device entry that is NOT the service device
    device_reg = dr.async_get(hass)
    device = device_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "zz:zz:zz:zz:zz:zz")},
    )

    result = await async_remove_config_entry_device(hass, entry, device)
    assert result is True


async def test_remove_config_entry_device_blocks_service_device(
    hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock
) -> None:
    """Test that async_remove_config_entry_device blocks removal of the service device."""
    entry = _make_config_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Get the service device
    device_reg = dr.async_get(hass)
    device = device_reg.async_get_device({(DOMAIN, entry.entry_id)})
    assert device is not None

    result = await async_remove_config_entry_device(hass, entry, device)
    assert result is False


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
