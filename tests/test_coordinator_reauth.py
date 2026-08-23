"""Tests for the UniFi Presence coordinator — reauth and error handling."""

from json import JSONDecodeError
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import _make_mock_client, make_mock_controller


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_successful_reauth_lifecycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test controller replacement and state recovery after session expiry."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    owned_session = MagicMock(closed=False)
    initial_controller = make_mock_controller(
        clients_items=[(mac, _make_mock_client(mac, name="Dan Phone", last_seen=now))]
    )
    initial_controller._unifi_presence_owned_session = owned_session
    replacement_controller = make_mock_controller(clients_items=[(mac, _make_mock_client(mac, last_seen=now))])
    websocket = MagicMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller_for_params",
        side_effect=[initial_controller, replacement_controller],
    ) as mock_create:
        coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
        first_data = await coordinator._async_update_data()
        coordinator.async_set_updated_data(first_data)

        initial_controller.clients.update_mock.side_effect = exception
        coordinator.websocket = websocket
        data = await coordinator._async_update_data()

    assert data[mac][0] is True
    assert data[mac][1] == "Dan Phone"
    assert coordinator.controller is replacement_controller
    assert mock_create.await_count == 2
    owned_session.detach.assert_called_once_with()
    replacement_controller.clients.update_mock.assert_awaited_once()
    replacement_controller.clients_all.update_mock.assert_awaited_once()
    websocket.restart_with_current_controller.assert_called_once_with()


async def test_coordinator_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that UpdateFailed is raised on persistent AiounifiException."""
    mock_coordinator_controller.clients.update_mock.side_effect = aiounifi.AiounifiException("connection lost")

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_invalid_json_raises_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that truncated JSON responses are treated as transient failures."""
    mock_coordinator_controller.clients.update_mock.side_effect = JSONDecodeError(
        "unexpected end of data",
        '{"meta":{"rc":"ok"},"data":[',
        27,
    )

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_best_effort_clients_all_refresh_failure_uses_cached_data(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test clients_all refresh failures stay best-effort during polling."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients.update_mock = AsyncMock()
    mock_coordinator_controller.clients_all.update_mock.side_effect = aiounifi.AiounifiException("historical down")
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data[mac][0] is True
    mock_coordinator_controller.clients.update_mock.assert_awaited_once()


async def test_coordinator_initial_timeout_raises_update_failed(
    hass: HomeAssistant,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that timeouts during initial controller creation are transient failures."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    with (
        patch(
            "custom_components.unifi_presence.coordinator.create_controller_for_params",
            side_effect=TimeoutError,
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_coordinator_reauth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that persistent credential failure after re-auth raises ConfigEntryAuthFailed."""
    mock_coordinator_controller.clients.update_mock.side_effect = [
        aiounifi.LoginRequired,
        aiounifi.Unauthorized,
    ]

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_post_reauth_communication_failure_raises_update_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that communication failures after re-auth raise UpdateFailed."""
    mock_coordinator_controller.clients.update_mock.side_effect = [
        aiounifi.LoginRequired,
        aiounifi.AiounifiException("still down"),
    ]

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
