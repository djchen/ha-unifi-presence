"""Tests for the UniFi Presence coordinator."""

from __future__ import annotations

import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.unifi_presence.const import CONF_FALLBACK_POLL_INTERVAL
from custom_components.unifi_presence.coordinator import (
    UnifiPresenceCoordinator,
    UnifiPresenceData,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_mock_client


def _make_reauth_side_effect(
    exception: type[Exception],
    *,
    recover: bool = True,
) -> Callable[[], None]:
    """Return an async update side effect that raises exception on first call.

    If recover=True, the second call succeeds. If recover=False, it raises again.
    """
    call_count = 0

    async def _side_effect() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        if not recover:
            raise exception

    return _side_effect


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = MOCK_CONFIG_DATA
    entry.options = MOCK_OPTIONS
    return entry


async def test_coordinator_fetches_clients(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that the coordinator fetches and processes client data."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", ip="192.168.1.100", last_seen=now)
    client2 = _make_mock_client("11:22:33:44:55:66", name="Jane Phone", ip="192.168.1.101", last_seen=now - 120)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1
    mock_coordinator_controller.clients["11:22:33:44:55:66"] = client2

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert isinstance(data, UnifiPresenceData)
    # Client 1 seen just now -> home
    assert data.device_states["aa:bb:cc:dd:ee:ff"] is True
    # Client 2 seen 120s ago with 60s threshold -> not_home
    assert data.device_states["11:22:33:44:55:66"] is False


async def test_coordinator_marks_unknown_device_not_home(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that a tracked device not in active clients is marked not_home."""
    mock_coordinator_controller.clients.clear()

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.device_states["aa:bb:cc:dd:ee:ff"] is False
    assert data.device_states["11:22:33:44:55:66"] is False


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_on_session_error(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that the coordinator re-authenticates on LoginRequired or Unauthorized."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients.update_async.side_effect = _make_reauth_side_effect(exception, recover=True)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    # Should have re-authenticated (reset _controller, called create_controller again) and succeeded
    assert data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator._controller is not None


async def test_coordinator_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that UpdateFailed is raised on persistent AiounifiException."""
    mock_coordinator_controller.clients.update_async.side_effect = aiounifi.AiounifiException("connection lost")

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_fallback_interval(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that update_interval uses the configured fallback poll interval."""
    config_entry.options = {**MOCK_OPTIONS, CONF_FALLBACK_POLL_INTERVAL: 600}

    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    assert coordinator.update_interval.total_seconds() == 600


async def test_ensure_controller_reuses_existing_controller(hass: HomeAssistant, config_entry: MagicMock) -> None:
    """Test that _ensure_controller returns cached controller without re-creating it."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    existing_controller = AsyncMock()
    coordinator._controller = existing_controller

    with patch("custom_components.unifi_presence.coordinator.create_controller") as create_controller:
        controller = await coordinator._ensure_controller()

    assert controller is existing_controller
    create_controller.assert_not_called()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that persistent credential failure after re-auth raises ConfigEntryAuthFailed."""
    mock_coordinator_controller.clients.update_async.side_effect = _make_reauth_side_effect(exception, recover=False)

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_network_failure_raises_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that network failure after re-auth raises UpdateFailed."""

    async def _network_fails_after_reauth() -> None:
        _network_fails_after_reauth.count = getattr(_network_fails_after_reauth, "count", 0) + 1
        if _network_fails_after_reauth.count == 1:
            raise exception
        raise aiounifi.AiounifiException("still down")

    mock_coordinator_controller.clients.update_async.side_effect = _network_fails_after_reauth

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_process_message_updates_state(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message updates device state on change."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now - 120)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    # First do a fallback poll to populate initial data (device away)
    data = await coordinator._async_update_data()
    assert data.device_states["aa:bb:cc:dd:ee:ff"] is False

    # Simulate a WS message that brings the device home
    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "hostname": "dan-phone",
        "ip": "192.168.1.100",
        "is_wired": False,
        "last_seen": now,
    }
    coordinator.process_message(message)

    # State should now be home
    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["ip"] == "192.168.1.100"


async def test_process_message_ignores_untracked_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message ignores MACs not in tracked set."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()

    original_data = coordinator.data

    message = MagicMock()
    message.data = {
        "mac": "ff:ff:ff:ff:ff:ff",
        "last_seen": int(time.time()),
    }
    coordinator.process_message(message)

    # Data should be unchanged
    assert coordinator.data is original_data


async def test_process_message_no_state_change_updates_info_silently(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message updates client_info without triggering state change."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", ip="192.168.1.100", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()
    # Simulate what DataUpdateCoordinator does after _async_update_data returns
    coordinator.async_set_updated_data(data)

    assert data.device_states["aa:bb:cc:dd:ee:ff"] is True
    original_data = coordinator.data

    # Send WS message with same home state but different IP
    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "hostname": "dan-phone",
        "ip": "192.168.1.200",
        "is_wired": False,
        "last_seen": now,
    }
    coordinator.process_message(message)

    # Data object should be the same (no async_set_updated_data called for state change)
    assert coordinator.data is original_data
    # But client_info should be updated in-place
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["ip"] == "192.168.1.200"


async def test_fallback_poll_diff_returns_existing_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that fallback poll returns existing data when state unchanged."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    data1 = await coordinator._async_update_data()
    # Simulate what DataUpdateCoordinator does after _async_update_data returns
    coordinator.async_set_updated_data(data1)

    data2 = await coordinator._async_update_data()

    # Same state -> should return the same object
    assert data2 is data1


async def test_signal_reachable_property(hass: HomeAssistant, config_entry: MagicMock) -> None:
    """Test that signal_reachable returns a unique signal per entry."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    assert "unifi_presence-reachable-" in coordinator.signal_reachable
    assert config_entry.entry_id in coordinator.signal_reachable


async def test_process_message_when_data_is_none(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message works when self.data is None (first WS message before poll)."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    # data is None before first poll
    assert coordinator.data is None

    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "hostname": "dan-phone",
        "ip": "192.168.1.100",
        "is_wired": False,
        "last_seen": now,
    }
    coordinator.process_message(message)

    # Should have created data with the device home
    assert coordinator.data is not None
    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["ip"] == "192.168.1.100"


async def test_process_message_case_insensitive_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message matches upper-case MACs from WS against lower-case tracked set."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()

    message = MagicMock()
    message.data = {
        "mac": "AA:BB:CC:DD:EE:FF",
        "name": "Dan Phone",
        "last_seen": now,
    }
    coordinator.process_message(message)

    # Should match after lowercasing
    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True


async def test_fallback_poll_returns_new_data_on_state_change(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that fallback poll returns new data when device state changes between polls."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    data1 = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data1)
    assert data1.device_states["aa:bb:cc:dd:ee:ff"] is True

    # Simulate device going away
    client1_away = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now - 120)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1_away

    data2 = await coordinator._async_update_data()

    # State changed -> should return a new data object
    assert data2 is not data1
    assert data2.device_states["aa:bb:cc:dd:ee:ff"] is False


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_resets_controller_before_retry(
    hass: HomeAssistant, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that re-auth resets _controller to None before retrying."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(return_value=client1)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock(side_effect=_make_reauth_side_effect(exception, recover=True))

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ) as mock_create:
        coordinator = UnifiPresenceCoordinator(hass, config_entry)
        await coordinator._async_update_data()

    # create_controller called twice: once for initial, once after _controller reset to None
    assert mock_create.call_count == 2
    assert coordinator._controller is not None


async def test_controller_property(hass: HomeAssistant, config_entry: MagicMock) -> None:
    """Test that the public controller property returns the cached controller."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    assert coordinator.controller is None

    mock_ctrl = MagicMock()
    coordinator._controller = mock_ctrl
    assert coordinator.controller is mock_ctrl
