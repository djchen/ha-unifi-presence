"""Tests for UniFi Presence system health."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.system_health import (
    async_register,
    system_health_info,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, add_mock_config_entry


async def test_system_health_info_reports_loaded_entry(hass: HomeAssistant) -> None:
    """Test system health summarizes the loaded integration state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
        options=MOCK_OPTIONS,
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = SimpleNamespace(last_update_success=True, websocket=SimpleNamespace(available=True))

    # Exercise aggregation without starting unrelated platforms and background tasks.
    with patch.object(hass.config_entries, "async_entries", return_value=[entry]) as get_entries:
        result = await system_health_info(hass)

    get_entries.assert_called_once_with(DOMAIN)

    assert result == {
        "config_entry_count": 1,
        "loaded_entry_count": 1,
        "coordinator_available_count": 1,
        "websocket_connected_count": 1,
        "tracked_device_count": 2,
        "controllers": "192.168.1.1 (default)",
    }


async def test_system_health_info_reports_unloaded_entry(hass: HomeAssistant) -> None:
    """Test system health falls back to stored config when entry is not loaded."""
    add_mock_config_entry(
        hass,
        title="UniFi Presence (192.168.1.1)",
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )

    result = await system_health_info(hass)

    assert result == {
        "config_entry_count": 1,
        "loaded_entry_count": 0,
        "coordinator_available_count": 0,
        "websocket_connected_count": 0,
        "tracked_device_count": 2,
        "controllers": "192.168.1.1 (default)",
    }


def test_async_register_registers_callback(hass: HomeAssistant) -> None:
    """Test the system health platform registers its callback."""
    registration = MagicMock()

    async_register(hass, registration)

    registration.async_register_info.assert_called_once_with(system_health_info)
