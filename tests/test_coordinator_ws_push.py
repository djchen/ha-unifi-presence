"""Tests for the UniFi Presence coordinator — WebSocket process_message."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import _make_mock_client


async def test_process_message_updates_state(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message updates device state on change."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now - 120)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

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
    assert coordinator.heartbeat_expiry_count == 1


async def test_process_message_updates_offline_client(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that websocket updates transition offline clients to home."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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
    assert coordinator.heartbeat_expiry_count == 1


async def test_process_message_noop_for_equivalent_update(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test repeated equivalent websocket updates do not publish redundant data."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)
    coordinator.async_update_listeners = MagicMock()

    message = MagicMock()
    message.data = {
        "mac": mac,
        "name": "Dan Phone",
        "last_seen": now,
    }

    coordinator.process_message(message)

    assert coordinator.data is initial_data
    coordinator.async_update_listeners.assert_not_called()


async def test_process_message_preserves_metadata_when_offline_client_reappears(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that websocket recovery keeps last-known metadata when payload omits names."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=now)

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that websocket updates fall back to hostname for display name."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores MACs not in tracked set."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that metadata-only websocket updates refresh entities immediately."""
    now = int(time.time())
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=now)
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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


async def test_process_message_when_data_is_none(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message works when self.data is None (first WS message before poll)."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

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
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message matches upper-case MACs from WS against lower-case tracked set."""
    now = int(time.time())
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
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


# ── Malformed message tests ──────────────────────────────────────────────


async def test_process_message_none_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with None data."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = None
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_missing_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with no mac field."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"last_seen": int(time.time())}
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_non_dict_data(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with non-dict data."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = "not a dict"
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_non_string_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores payloads with non-string MACs."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"mac": 123, "last_seen": int(time.time())}
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_non_numeric_last_seen(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores payloads with non-numeric timestamps."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"mac": "aa:bb:cc:dd:ee:ff", "last_seen": "now"}
    coordinator.process_message(message)

    assert coordinator.data is original_data


async def test_process_message_bool_last_seen(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores bool last_seen values."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"mac": "aa:bb:cc:dd:ee:ff", "last_seen": True}
    coordinator.process_message(message)

    assert coordinator.data is original_data
