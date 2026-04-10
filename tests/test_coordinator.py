"""Tests for the UniFi Presence coordinator."""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any
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
) -> Callable[[], Coroutine[Any, Any, None]]:
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


async def test_coordinator_uses_hostname_when_name_missing(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that hostname is used as the runtime display name fallback."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", hostname="dan-phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "dan-phone"


async def test_coordinator_uses_mac_when_name_and_hostname_missing(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that MAC remains the last-resort tracker name fallback."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "aa:bb:cc:dd:ee:ff"


async def test_coordinator_marks_unknown_device_not_home(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that a tracked device not in active clients is marked not_home."""
    mock_coordinator_controller.clients.clear()

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.device_states["aa:bb:cc:dd:ee:ff"] is False
    assert data.device_states["11:22:33:44:55:66"] is False


async def test_coordinator_preserves_metadata_for_offline_tracked_client_via_clients_all(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that offline clients get metadata from the historical clients_all store."""
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients.clear()
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac, name="Dan Phone")

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.device_states[mac] is False
    assert data.client_info[mac]["name"] == "Dan Phone"


async def test_clients_all_failure_uses_cached_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that clients_all.update() failure still uses cached historical data."""
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients.clear()
    # Pre-populate clients_all cache, then make update fail on next call
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac, name="Dan Phone")
    mock_coordinator_controller.clients_all.update_async.side_effect = Exception("network")

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    data = await coordinator._async_update_data()

    assert data.device_states[mac] is False
    # Metadata should still come from the cached clients_all dict
    assert data.client_info[mac]["name"] == "Dan Phone"


async def test_clients_all_stub_falls_back_to_previous_metadata(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that empty historical stubs do not overwrite richer prior metadata."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac)

    data = await coordinator._async_update_data()

    assert data.device_states[mac] is False
    assert data.client_info[mac]["name"] == "Dan Phone"


async def test_coordinator_site_id_uses_entry_id_when_unique_id_missing(
    hass: HomeAssistant, config_entry: MagicMock
) -> None:
    """Test tracker ID fallback stays stable when unique_id is unavailable."""
    config_entry.unique_id = None

    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    assert coordinator.site_id == "test_entry_id"


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


async def test_coordinator_initial_timeout_raises_update_failed(
    hass: HomeAssistant,
    config_entry: MagicMock,
) -> None:
    """Test that timeouts during initial controller creation are transient failures."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    with (
        patch(
            "custom_components.unifi_presence.coordinator.create_controller",
            side_effect=TimeoutError,
        ),
        pytest.raises(UpdateFailed),
    ):
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
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        raise aiounifi.AiounifiException("still down")

    call_count = 0

    mock_coordinator_controller.clients.update_async.side_effect = _network_fails_after_reauth

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_coordinator_reauth_timeout_raises_update_failed(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that timeouts during re-auth remain transient failures."""

    async def _timeout_after_reauth() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        raise TimeoutError

    call_count = 0

    mock_coordinator_controller.clients.update_async.side_effect = _timeout_after_reauth

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
        "last_seen": now,
    }
    coordinator.process_message(message)

    # State should now be home
    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "Dan Phone"


async def test_process_message_updates_offline_client(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that websocket updates transition offline clients to home."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)

    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is False

    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "last_seen": int(time.time()),
    }
    coordinator.process_message(message)

    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "Dan Phone"


async def test_process_message_preserves_metadata_when_offline_client_reappears(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that websocket recovery keeps last-known metadata when payload omits names."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()
    offline_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(offline_data)

    message = MagicMock()
    message.data = {
        "mac": mac,
        "last_seen": now,
    }
    coordinator.process_message(message)

    assert coordinator.data.client_info[mac]["name"] == "Dan Phone"


async def test_process_message_uses_hostname_when_name_missing(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that websocket updates fall back to hostname for display name."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()

    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "hostname": "dan-phone",
        "last_seen": now,
    }
    coordinator.process_message(message)

    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "dan-phone"


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


async def test_process_message_metadata_only_update_notifies_listeners(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that metadata-only websocket updates refresh entities immediately."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator.async_refresh()

    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    original_data = coordinator.data
    coordinator.async_update_listeners = MagicMock()

    # Send WS message with same home state but updated name
    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone Updated",
        "last_seen": now,
    }
    coordinator.process_message(message)

    assert coordinator.data is not original_data
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "Dan Phone Updated"
    assert coordinator.async_update_listeners.call_count == 1


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


async def test_async_refresh_skips_listener_update_when_state_unchanged(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that unchanged fallback polls do not notify listeners."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    coordinator.async_update_listeners = MagicMock()

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1


async def test_async_refresh_notifies_listeners_on_metadata_only_change(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that fallback polls notify listeners when only metadata changes."""
    now = int(time.time())
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now
    )

    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    coordinator.async_update_listeners = MagicMock()

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1

    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone Updated", last_seen=now
    )

    await coordinator.async_refresh()

    assert coordinator.async_update_listeners.call_count == 2
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "Dan Phone Updated"


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
        "last_seen": now,
    }
    coordinator.process_message(message)

    # Should have created data with the device home
    assert coordinator.data is not None
    assert coordinator.data.device_states["aa:bb:cc:dd:ee:ff"] is True
    assert coordinator.data.client_info["aa:bb:cc:dd:ee:ff"]["name"] == "Dan Phone"


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


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_triggers_websocket_reconnect(
    hass: HomeAssistant, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test that websocket.reconnect() is called after a poll-triggered controller swap."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(return_value=client1)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock(side_effect=_make_reauth_side_effect(exception, recover=True))

    mock_ws = MagicMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        coordinator = UnifiPresenceCoordinator(hass, config_entry)
        coordinator.websocket = mock_ws
        await coordinator._async_update_data()

    mock_ws.reconnect.assert_called_once()


async def test_controller_property(hass: HomeAssistant, config_entry: MagicMock) -> None:
    """Test that the public controller property returns the cached controller."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)

    assert coordinator.controller is None

    mock_ctrl = MagicMock()
    coordinator._controller = mock_ctrl
    assert coordinator.controller is mock_ctrl


# ── Malformed message tests ──────────────────────────────────────────────


async def test_process_message_none_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with None data."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = None
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_missing_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with no mac field."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"last_seen": int(time.time())}
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_non_dict_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with non-dict data."""
    coordinator = UnifiPresenceCoordinator(hass, config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = "not a dict"
    coordinator.process_message(message)

    assert coordinator.data is original_data


@pytest.mark.parametrize("exception", [aiounifi.LoginRequired, aiounifi.Unauthorized])
async def test_reauth_retry_calls_clients_all_and_preserves_prior_metadata(
    hass: HomeAssistant, config_entry: MagicMock, exception: type[Exception]
) -> None:
    """Test reauth retry refreshes clients_all and offline metadata comes from prior data."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    client1 = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    controller = AsyncMock()
    controller.clients = MagicMock()
    controller.clients.get = MagicMock(return_value=client1)
    controller.clients_all = MagicMock()
    controller.clients_all.update = AsyncMock()
    controller.clients_all.get = MagicMock(return_value=None)
    controller.login = AsyncMock()
    controller.clients.update = AsyncMock(side_effect=_make_reauth_side_effect(exception, recover=True))

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        coordinator = UnifiPresenceCoordinator(hass, config_entry)
        await coordinator._async_update_data()

    # clients_all.update should have been called on both the initial and retry paths
    assert controller.clients_all.update.await_count == 2
