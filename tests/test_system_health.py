"""Tests for UniFi Presence system health."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.system_health import (
    async_register,
    system_health_info,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller"


async def test_system_health_info_reports_loaded_entry(
    hass: HomeAssistant,
    enable_custom_integrations,
    mock_controller: MagicMock,
) -> None:
    """Test system health summarizes the loaded integration state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )
    entry.add_to_hass(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=mock_controller):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entry.runtime_data.websocket.available = True

    result = await system_health_info(hass)

    assert result == {
        "config_entry_count": 1,
        "loaded_entry_count": 1,
        "coordinator_available_count": 1,
        "heartbeat_expiry_count": 0,
        "websocket_connected_count": 1,
        "tracked_device_count": 2,
        "controllers": "192.168.1.1 (default)",
    }


async def test_system_health_info_reports_unloaded_entry(hass: HomeAssistant) -> None:
    """Test system health falls back to stored config when entry is not loaded."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )
    entry.add_to_hass(hass)

    result = await system_health_info(hass)

    assert result == {
        "config_entry_count": 1,
        "loaded_entry_count": 0,
        "coordinator_available_count": 0,
        "heartbeat_expiry_count": 0,
        "websocket_connected_count": 0,
        "tracked_device_count": 2,
        "controllers": "192.168.1.1 (default)",
    }


def test_async_register_registers_callback(hass: HomeAssistant) -> None:
    """Test the system health platform registers its callback."""
    registration = MagicMock()

    async_register(hass, registration)

    registration.async_register_info.assert_called_once_with(system_health_info)
