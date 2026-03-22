"""Tests for the UniFi Presence diagnostics platform."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.diagnostics import (
    _partial_redact_mac,
    _redact_mac_keys,
    _redact_mac_list,
    async_get_config_entry_diagnostics,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller"


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, enable_custom_integrations, mock_controller: MagicMock) -> MockConfigEntry:
    """Config entry fully set up in hass for diagnostics tests."""
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

    return entry


async def test_diagnostics_redacts_credentials(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """Test that diagnostics redacts sensitive credentials and MAC addresses."""
    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    # Credentials should be redacted
    assert result["config_entry"]["data"]["username"] == "**REDACTED**"
    assert result["config_entry"]["data"]["password"] == "**REDACTED**"
    # Non-sensitive data should be present
    assert result["config_entry"]["data"]["host"] == "192.168.1.1"
    assert result["tracked_device_count"] == 2
    assert "device_states" in result
    assert "away_seconds" in result
    assert "fallback_poll_interval_seconds" in result
    assert "websocket_connected" in result

    # MAC addresses in options should be partially redacted
    tracked = result["config_entry"]["options"]["tracked_devices"]
    for mac in tracked:
        assert mac.startswith("**:**:**:")

    # MAC addresses in device_states keys should be partially redacted
    for key in result["device_states"]:
        assert key.startswith("**:**:**:")


async def test_diagnostics_websocket_none(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """Test that diagnostics reports websocket_connected=False when websocket is None."""
    loaded_entry.runtime_data.websocket = None

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert result["websocket_connected"] is False


def test_partial_redact_mac_standard() -> None:
    """Test partial MAC redaction keeps last 3 octets."""
    assert _partial_redact_mac("aa:bb:cc:dd:ee:ff") == "**:**:**:dd:ee:ff"


def test_partial_redact_mac_malformed() -> None:
    """Test that malformed MAC addresses are fully redacted."""
    assert _partial_redact_mac("not-a-mac") == "**REDACTED**"
    assert _partial_redact_mac("") == "**REDACTED**"


def test_redact_mac_keys() -> None:
    """Test that dict keys containing MACs are partially redacted."""
    data = {"aa:bb:cc:dd:ee:ff": True, "11:22:33:44:55:66": False}
    result = _redact_mac_keys(data)
    assert "**:**:**:dd:ee:ff" in result
    assert "**:**:**:44:55:66" in result
    assert "aa:bb:cc:dd:ee:ff" not in result


def test_redact_mac_list() -> None:
    """Test that MAC lists are partially redacted."""
    macs = ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]
    result = _redact_mac_list(macs)
    assert result == ["**:**:**:dd:ee:ff", "**:**:**:44:55:66"]
