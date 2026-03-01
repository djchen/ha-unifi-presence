"""Tests for the UniFi Presence diagnostics platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.diagnostics import async_get_config_entry_diagnostics

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller"


@pytest.fixture
def mock_controller() -> MagicMock:
    """Fully-wired mock aiounifi controller for diagnostics tests."""
    clients = MagicMock()
    clients.update = AsyncMock()
    clients.get = MagicMock(return_value=None)

    controller = MagicMock()
    controller.clients = clients
    controller.login = AsyncMock()
    controller.messages = MagicMock()
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.connectivity = MagicMock()
    controller.start_websocket = AsyncMock()
    return controller


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
    """Test that diagnostics redacts sensitive credentials."""
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


async def test_diagnostics_websocket_none(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """Test that diagnostics reports websocket_connected=False when websocket is None."""
    loaded_entry.runtime_data.websocket = None

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert result["websocket_connected"] is False
