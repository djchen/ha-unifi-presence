"""Tests for the UniFi Presence coordinator — WebSocket process_message."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import _make_mock_client


async def test_process_message_updates_state(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that process_message updates device state on change."""
    now = dt_util.utcnow()
    client1 = _make_mock_client(
        "aa:bb:cc:dd:ee:ff",
        name="Dan Phone",
        last_seen=int((now - timedelta(seconds=120)).timestamp()),
    )
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # First do a fallback poll to populate initial data (device away)
    data = await coordinator._async_update_data()
    assert data.clients["aa:bb:cc:dd:ee:ff"].is_home is False

    # Simulate a WS message that brings the device home
    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    # State should now be home
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].is_home is True
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].name == "Dan Phone"
    assert coordinator.heartbeat_expiry_count == 1


async def test_process_message_updates_offline_client(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that websocket updates transition offline clients to home."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)

    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].is_home is False

    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone",
        "last_seen": int(dt_util.utcnow().timestamp()),
    }
    coordinator.process_message(message)

    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].is_home is True
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].name == "Dan Phone"
    assert coordinator.heartbeat_expiry_count == 1


async def test_process_message_noop_for_equivalent_update(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test repeated equivalent websocket updates do not publish redundant data."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)
    coordinator.async_update_listeners = MagicMock()

    message = MagicMock()
    message.data = {
        "mac": mac,
        "name": "Dan Phone",
        "last_seen": int(now.timestamp()),
    }

    coordinator.process_message(message)

    assert coordinator.data is initial_data
    coordinator.async_update_listeners.assert_not_called()


async def test_process_message_recovers_availability_after_poll_failure(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that a valid push restores availability after a failed REST poll."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)
    coordinator.async_update_listeners = MagicMock()
    coordinator.last_update_success = False

    message = MagicMock()
    message.data = {
        "mac": mac,
        "name": "Dan Phone",
        "last_seen": int(now.timestamp()),
    }

    coordinator.process_message(message)

    assert coordinator.last_update_success is True
    assert coordinator.data is initial_data
    coordinator.async_update_listeners.assert_called_once_with()


async def test_process_message_preserves_metadata_when_offline_client_reappears(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that websocket recovery keeps last-known metadata when payload omits names."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()
    offline_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(offline_data)

    message = MagicMock()
    message.data = {
        "mac": mac,
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    assert coordinator.data.clients[mac].name == "Dan Phone"


async def test_process_message_uses_hostname_when_name_missing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that websocket updates fall back to hostname for display name."""
    now = dt_util.utcnow()
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()

    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "hostname": "dan-phone",
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].name == "dan-phone"


async def test_process_message_ignores_untracked_mac(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that process_message ignores MACs not in tracked set."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()

    original_data = coordinator.data

    message = MagicMock()
    message.data = {
        "mac": "ff:ff:ff:ff:ff:ff",
        "last_seen": int(dt_util.utcnow().timestamp()),
    }
    coordinator.process_message(message)

    # Data should be unchanged
    assert coordinator.data is original_data


async def test_process_message_metadata_only_update_notifies_listeners(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that metadata-only websocket updates refresh entities immediately."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator.async_refresh()

    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].is_home is True
    original_data = coordinator.data
    coordinator.async_update_listeners = MagicMock()

    # Send WS message with same home state but updated name
    message = MagicMock()
    message.data = {
        "mac": "aa:bb:cc:dd:ee:ff",
        "name": "Dan Phone Updated",
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    assert coordinator.data is original_data
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].name == "Dan Phone Updated"
    coordinator.async_update_listeners.assert_called_once_with()


async def test_process_message_for_one_mac_preserves_unrelated_tracked_device_state(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that one tracked-device push update does not alter unrelated tracked devices."""
    now = dt_util.utcnow()
    home_mac = "aa:bb:cc:dd:ee:ff"
    away_mac = "11:22:33:44:55:66"
    mock_coordinator_controller.clients[home_mac] = _make_mock_client(
        home_mac,
        name="Dan Phone",
        last_seen=int(now.timestamp()),
    )
    mock_coordinator_controller.clients[away_mac] = _make_mock_client(
        away_mac,
        name="Jane Phone",
        last_seen=int((now - timedelta(seconds=120)).timestamp()),
    )

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    initial_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(initial_data)

    message = MagicMock()
    message.data = {
        "mac": home_mac,
        "name": "Dan Phone Updated",
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    assert coordinator.data.clients[home_mac].is_home is True
    assert coordinator.data.clients[home_mac].name == "Dan Phone Updated"
    assert coordinator.data.clients[away_mac].is_home is False
    assert coordinator.data.clients[away_mac].name == "Jane Phone"


async def test_process_message_inserts_initial_runtime_state_into_client_cache(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test the first push creates cached runtime state for a newly seen device."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    assert coordinator.data is None

    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": int(now.timestamp())}))

    state = coordinator.data.clients[mac]
    assert state.is_home is True
    assert state.name == "Dan Phone"
    assert state.last_seen_ts == int(now.timestamp())
    assert state.expiry_ts is not None


async def test_process_message_case_insensitive_mac(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that process_message matches upper-case MACs from WS against lower-case tracked set."""
    now = dt_util.utcnow()
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()

    message = MagicMock()
    message.data = {
        "mac": "AA:BB:CC:DD:EE:FF",
        "name": "Dan Phone",
        "last_seen": int(now.timestamp()),
    }
    coordinator.process_message(message)

    # Should match after lowercasing
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].is_home is True


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


async def test_process_message_without_last_seen_uses_cached_timestamp(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test payloads without last_seen reuse the cached timestamp."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": int(now.timestamp())}))

    cached_last_seen = coordinator.data.clients[mac].last_seen_ts
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone"}))

    assert coordinator.data.clients[mac].last_seen_ts == cached_last_seen


async def test_process_message_missing_mac(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that process_message ignores a message with no mac field."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()
    original_data = coordinator.data

    message = MagicMock()
    message.data = {"last_seen": int(dt_util.utcnow().timestamp())}
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
    message.data = {"mac": 123, "last_seen": int(dt_util.utcnow().timestamp())}
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
