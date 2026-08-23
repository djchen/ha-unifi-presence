"""Tests for the UniFi Presence coordinator — REST polling and fallback behaviour."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.const import CONF_FALLBACK_POLL_INTERVAL
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import MOCK_OPTIONS, _make_mock_client


async def test_coordinator_fetches_clients(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that the coordinator fetches and processes client data."""
    now = dt_util.utcnow()
    client1 = _make_mock_client(
        "aa:bb:cc:dd:ee:ff",
        name="Dan Phone",
        ip="192.168.1.100",
        last_seen=int(now.timestamp()),
    )
    client2 = _make_mock_client(
        "11:22:33:44:55:66",
        name="Jane Phone",
        ip="192.168.1.101",
        last_seen=int((now - timedelta(seconds=120)).timestamp()),
    )
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1
    mock_coordinator_controller.clients["11:22:33:44:55:66"] = client2

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert isinstance(data, dict)
    # Client 1 seen just now -> home
    assert data["aa:bb:cc:dd:ee:ff"][0] is True
    # Client 2 seen 120s ago with 60s threshold -> not_home
    assert data["11:22:33:44:55:66"][0] is False


async def test_coordinator_marks_unknown_device_not_home(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that a tracked device not in active clients is marked not_home."""
    mock_coordinator_controller.clients.clear()

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data["aa:bb:cc:dd:ee:ff"][0] is False
    assert data["11:22:33:44:55:66"][0] is False


async def test_coordinator_offline_client_falls_back_to_mac_without_cached_metadata(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that unknown offline clients fall back to their MAC address."""
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients.clear()

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data[mac] == (False, mac)
    mock_coordinator_controller.clients_all.update_mock.assert_awaited_once()


async def test_fallback_poll_refreshes_only_active_clients(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test fallback polling refreshes active and historical client stores."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    await coordinator._async_update_data()

    mock_coordinator_controller.clients.update_mock.assert_awaited_once()
    mock_coordinator_controller.clients_all.update_mock.assert_awaited_once()


async def test_offline_tracked_client_uses_clients_all_name_when_available(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that offline tracked clients use refreshed historical UniFi metadata."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac, name="Dan's Renamed Phone")

    data = await coordinator._async_update_data()

    assert data[mac] == (True, "Dan's Renamed Phone")
    assert mock_coordinator_controller.clients_all.update_mock.await_count == 2


async def test_offline_tracked_client_keeps_cached_metadata_when_clients_all_stub_is_blank(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that empty historical stubs do not overwrite richer cached metadata."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac)

    data = await coordinator._async_update_data()

    assert data[mac] == (True, "Dan Phone")
    assert mock_coordinator_controller.clients_all.update_mock.await_count == 2


async def test_offline_tracked_client_uses_clients_all_name_on_cold_start(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that cold-start offline clients recover their UniFi display name."""
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients.clear()
    mock_coordinator_controller.clients_all[mac] = _make_mock_client(mac, name="Dan Phone")

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data[mac] == (False, "Dan Phone")
    mock_coordinator_controller.clients_all.update_mock.assert_awaited_once()


async def test_active_client_with_blank_metadata_preserves_previous_info(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test active clients keep prior metadata when UniFi omits names temporarily."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, last_seen=int(now.timestamp()))
    data = await coordinator._async_update_data()

    assert data[mac] == (True, "Dan Phone")


async def test_coordinator_fallback_interval(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that update_interval uses the configured fallback poll interval."""
    coordinator_config_entry.options = {**MOCK_OPTIONS, CONF_FALLBACK_POLL_INTERVAL: 600}

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    assert coordinator.update_interval.total_seconds() == 600


async def test_fallback_poll_keeps_recently_missing_client_home_until_heartbeat_expires(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test missing active clients stay home while cached last_seen is still fresh."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=int(now.timestamp()))

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    first_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first_data)

    mock_coordinator_controller.clients.clear()

    second_data = await coordinator._async_update_data()

    assert second_data[mac][0] is True
    assert coordinator.heartbeat_expiry_count == 1


async def test_fallback_poll_returns_detached_equal_snapshot(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test an unchanged fallback poll returns a fresh, equal public snapshot."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    data1 = await coordinator._async_update_data()
    # Simulate what DataUpdateCoordinator does after _async_update_data returns
    coordinator.async_set_updated_data(data1)

    data2 = await coordinator._async_update_data()

    assert data2 == data1
    assert data2 is not data1


async def test_async_refresh_skips_listener_update_when_state_unchanged(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that unchanged fallback polls do not notify listeners."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator.async_update_listeners = MagicMock()

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1


async def test_async_refresh_notifies_listeners_on_metadata_only_change(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that fallback polls notify listeners when only metadata changes."""
    now = dt_util.utcnow()
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=int(now.timestamp())
    )

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator.async_update_listeners = MagicMock()

    await coordinator.async_refresh()
    assert coordinator.async_update_listeners.call_count == 1

    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = _make_mock_client(
        "aa:bb:cc:dd:ee:ff", name="Dan Phone Updated", last_seen=int(now.timestamp())
    )

    await coordinator.async_refresh()

    assert coordinator.async_update_listeners.call_count == 2
    assert coordinator.data["aa:bb:cc:dd:ee:ff"][1] == "Dan Phone Updated"


async def test_fallback_poll_returns_new_data_on_state_change(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that fallback poll returns new data when device state changes between polls."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    data1 = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data1)
    assert data1["aa:bb:cc:dd:ee:ff"][0] is True

    # Simulate device going away: remove from active clients and age out
    # the cached timestamp past the away threshold
    mock_coordinator_controller.clients.clear()
    coordinator._client_states["aa:bb:cc:dd:ee:ff"].last_seen_ts = int((now - timedelta(seconds=120)).timestamp())

    data2 = await coordinator._async_update_data()

    assert data2["aa:bb:cc:dd:ee:ff"][0] is False
