"""Tests for the UniFi Presence coordinator — reauth and error handling."""

from json import JSONDecodeError
from unittest.mock import MagicMock, patch

import aiounifi
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import _assert_session_cleanup, _attach_mock_session, _make_mock_client, make_mock_controller


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
    initial_controller = make_mock_controller(
        clients_items=[(mac, _make_mock_client(mac, name="Dan Phone", last_seen=now))]
    )
    owned_session = _attach_mock_session(initial_controller)
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

    assert data[mac] == (True, "Dan Phone")
    assert coordinator.controller is replacement_controller
    assert mock_create.await_count == 2
    _assert_session_cleanup(owned_session)
    replacement_controller.clients.update_mock.assert_awaited_once()
    replacement_controller.clients_all.update_mock.assert_awaited_once()
    websocket.restart_with_current_controller.assert_called_once_with()


async def test_coordinator_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: MagicMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that UpdateFailed is raised on persistent AiounifiException."""
    error = aiounifi.AiounifiException("connection lost")
    mock_coordinator_controller.clients.update_mock.side_effect = error

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()

    assert exc_info.value.__cause__ is error
    mock_coordinator_controller.clients.update_mock.assert_awaited_once_with()


async def test_coordinator_invalid_json_raises_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: MagicMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that truncated JSON responses are treated as transient failures."""
    error = JSONDecodeError(
        "unexpected end of data",
        '{"meta":{"rc":"ok"},"data":[',
        27,
    )
    mock_coordinator_controller.clients.update_mock.side_effect = error

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()

    assert exc_info.value.__cause__ is error
    mock_coordinator_controller.clients.update_mock.assert_awaited_once_with()


async def test_coordinator_best_effort_clients_all_refresh_failure_uses_cached_data(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: MagicMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test clients_all refresh failures stay best-effort during polling."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients_all.update_mock.side_effect = aiounifi.AiounifiException("historical down")
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)
    offline_mac = "11:22:33:44:55:66"
    mock_coordinator_controller.clients_all[offline_mac] = _make_mock_client(offline_mac, name="Jane Phone")

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data == {mac: (True, "Dan Phone"), offline_mac: (False, "Jane Phone")}
    mock_coordinator_controller.clients.update_mock.assert_awaited_once_with()
    mock_coordinator_controller.clients_all.update_mock.assert_awaited_once_with()


async def test_coordinator_initial_timeout_raises_update_failed(
    hass: HomeAssistant,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that timeouts during initial controller creation are transient failures."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    error = TimeoutError()

    with (
        patch(
            "custom_components.unifi_presence.coordinator.create_controller_for_params",
            side_effect=error,
        ) as create_controller,
        pytest.raises(UpdateFailed) as exc_info,
    ):
        await coordinator._async_update_data()

    assert exc_info.value.__cause__ is error
    create_controller.assert_awaited_once()


async def test_coordinator_reauth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: MagicMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that persistent credential failure after re-auth raises ConfigEntryAuthFailed."""
    error = aiounifi.Unauthorized()
    mock_coordinator_controller.clients.update_mock.side_effect = [
        aiounifi.LoginRequired,
        error,
    ]

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(ConfigEntryAuthFailed) as exc_info:
        await coordinator._async_update_data()

    assert exc_info.value.__cause__ is error
    assert mock_coordinator_controller.clients.update_mock.await_count == 2


async def test_coordinator_post_reauth_communication_failure_raises_update_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: MagicMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that communication failures after re-auth raise UpdateFailed."""
    error = aiounifi.AiounifiException("still down")
    mock_coordinator_controller.clients.update_mock.side_effect = [
        aiounifi.LoginRequired,
        error,
    ]

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()

    assert exc_info.value.__cause__ is error
    assert mock_coordinator_controller.clients.update_mock.await_count == 2
