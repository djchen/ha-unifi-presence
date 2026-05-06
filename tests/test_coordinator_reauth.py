"""Tests for the UniFi Presence coordinator — reauth and error handling."""

from __future__ import annotations

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

from .conftest import _make_mock_client, make_mock_controller, make_reauth_side_effect


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_on_session_error(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that the coordinator re-authenticates on LoginRequired or Unauthorized."""
    now = int(dt_util.utcnow().timestamp())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients.update_mock.side_effect = make_reauth_side_effect(exception, recover=True)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    # Should have re-authenticated (reset _controller, called create_controller again) and succeeded
    assert data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator._controller is not None


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

    assert data.device_states[mac] is True
    mock_coordinator_controller.clients.update_mock.assert_awaited_once()


async def test_coordinator_initial_timeout_raises_update_failed(
    hass: HomeAssistant,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that timeouts during initial controller creation are transient failures."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    with (
        patch(
            "custom_components.unifi_presence.coordinator.create_controller",
            side_effect=TimeoutError,
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_coordinator_reauth_detaches_replaced_runtime_controller(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test poll-triggered reauth detaches the replaced runtime controller session."""
    now = int(dt_util.utcnow().timestamp())
    owned_session = MagicMock()
    owned_session.closed = False
    owned_session.detach = MagicMock()
    mock_coordinator_controller.clients.update_mock.side_effect = aiounifi.LoginRequired
    mock_coordinator_controller._unifi_presence_owned_session = owned_session

    replacement_controller = make_mock_controller(
        clients_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now))]
    )

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator._controller = mock_coordinator_controller

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=replacement_controller,
    ):
        data = await coordinator._async_update_data()

    assert data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.controller is replacement_controller
    owned_session.detach.assert_called_once_with()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that persistent credential failure after re-auth raises ConfigEntryAuthFailed."""
    mock_coordinator_controller.clients.update_mock.side_effect = make_reauth_side_effect(exception, recover=False)

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_network_failure_raises_update_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that network failure after re-auth raises UpdateFailed."""

    async def _network_fails_after_reauth() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        raise aiounifi.AiounifiException("still down")

    call_count = 0

    mock_coordinator_controller.clients.update_mock.side_effect = _network_fails_after_reauth

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_invalid_json_raises_update_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that truncated JSON after re-auth remains a transient failure."""

    async def _invalid_json_after_reauth() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        raise JSONDecodeError(
            "unexpected end of data",
            '{"meta":{"rc":"ok"},"data":[',
            27,
        )

    call_count = 0

    mock_coordinator_controller.clients.update_mock.side_effect = _invalid_json_after_reauth

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_timeout_raises_update_failed(
    hass: HomeAssistant,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that timeouts during re-auth remain transient failures."""

    async def _timeout_after_reauth() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        raise TimeoutError

    call_count = 0

    mock_coordinator_controller.clients.update_mock.side_effect = _timeout_after_reauth

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_resets_controller_before_retry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that re-auth resets _controller to None before retrying."""
    now = int(dt_util.utcnow().timestamp())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(return_value=client1)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock(side_effect=make_reauth_side_effect(exception, recover=True))

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ) as mock_create:
        coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
        await coordinator._async_update_data()

    # create_controller called twice: once for initial, once after _controller reset to None
    assert mock_create.call_count == 2
    assert coordinator._controller is not None


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_triggers_websocket_reconnect(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test that websocket.restart_with_current_controller() is called after a poll-triggered controller swap."""
    now = int(dt_util.utcnow().timestamp())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(return_value=client1)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock(side_effect=make_reauth_side_effect(exception, recover=True))

    mock_ws = MagicMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
        coordinator.websocket = mock_ws
        await coordinator._async_update_data()

    mock_ws.restart_with_current_controller.assert_called_once()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_retry_refreshes_clients_all_and_preserves_prior_metadata(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    coordinator_config_entry: MagicMock,
    exception: type[Exception],
) -> None:
    """Test reauth retry refreshes clients_all and preserves prior metadata."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    client1 = _make_mock_client(mac, name="Dan Phone", last_seen=now)
    clients = {mac: client1}

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(side_effect=clients.get)
    controller.clients_all = MagicMock()
    controller.clients_all.update = AsyncMock()
    controller.clients_all.get = MagicMock(return_value=None)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
        first_data = await coordinator._async_update_data()
        coordinator.async_set_updated_data(first_data)

        clients.clear()
        controller.clients.update = AsyncMock(side_effect=make_reauth_side_effect(exception, recover=True))
        second_data = await coordinator._async_update_data()

    assert controller.clients_all.update.await_count == 3
    assert second_data.clients[mac].name == "Dan Phone"
