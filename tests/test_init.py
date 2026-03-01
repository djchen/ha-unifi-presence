"""Tests for the UniFi Presence integration setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator
from custom_components.unifi_presence.websocket import UnifiPresenceWebsocket

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller"


@pytest.fixture
def mock_controller() -> MagicMock:
    """Fully-wired mock aiounifi controller suitable for integration setup."""
    clients = MagicMock()
    clients.update = AsyncMock()
    clients.get = MagicMock(return_value=None)

    controller = AsyncMock()
    controller.clients = clients
    controller.login = AsyncMock()
    controller.messages = MagicMock()
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.connectivity = MagicMock()
    controller.start_websocket = AsyncMock()
    return controller


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
