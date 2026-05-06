"""Tests for the UniFi Presence coordinator — identity, naming, timestamp ordering, and controller lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.const import CONF_SITE, CONF_TRACKED_DEVICES
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_mock_client

# ── Naming fallback tests ────────────────────────────────────────────────


async def test_coordinator_uses_hostname_when_name_missing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that hostname is used as the runtime display name fallback."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", hostname="dan-phone", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data.clients["aa:bb:cc:dd:ee:ff"].name == "dan-phone"


async def test_coordinator_uses_mac_when_name_and_hostname_missing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that MAC remains the last-resort tracker name fallback."""
    now = dt_util.utcnow()
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", last_seen=int(now.timestamp()))
    mock_coordinator_controller.clients["aa:bb:cc:dd:ee:ff"] = client1

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    data = await coordinator._async_update_data()

    assert data.clients["aa:bb:cc:dd:ee:ff"].name == "aa:bb:cc:dd:ee:ff"


async def test_coordinator_normalizes_tracked_macs(hass: HomeAssistant, coordinator_config_entry: MagicMock) -> None:
    """Test tracked MAC options are trimmed, deduplicated, and lowercased."""
    coordinator_config_entry.options = {
        **MOCK_OPTIONS,
        CONF_TRACKED_DEVICES: [
            " AA:BB:CC:DD:EE:FF ",
            "",
            "aa:bb:cc:dd:ee:ff",
            "11:22:33:44:55:66",
            "  ",
        ],
    }

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    assert coordinator.tracked_devices == (
        "aa:bb:cc:dd:ee:ff",
        "11:22:33:44:55:66",
    )


# ── Site ID / controller lifecycle ───────────────────────────────────────


async def test_coordinator_site_id_uses_entry_id_when_unique_id_missing(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> None:
    """Test tracker ID fallback stays stable when unique_id is unavailable."""
    coordinator_config_entry.unique_id = None

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    assert coordinator.site_id == "test_entry_id"


async def test_coordinator_site_id_falls_back_to_default_site_when_identity_missing(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> None:
    """Test tracker IDs fall back to the default site when no identity exists."""
    coordinator_config_entry.unique_id = None
    coordinator_config_entry.entry_id = None

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    assert coordinator.site_id == "default"


async def test_ensure_controller_reuses_existing_controller(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> None:
    """Test that _ensure_controller returns cached controller without re-creating it."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    existing_controller = AsyncMock()
    coordinator._controller = existing_controller

    with patch("custom_components.unifi_presence.coordinator.create_controller") as create_controller:
        controller = await coordinator._ensure_controller()

    assert controller is existing_controller
    create_controller.assert_not_called()


async def test_ensure_controller_normalizes_legacy_stored_site_id(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> None:
    """Test runtime controller setup resolves legacy stored site IDs to site names."""
    coordinator_config_entry.data = {**MOCK_CONFIG_DATA, CONF_SITE: "site-office-id"}
    coordinator_config_entry.unique_id = "192.168.1.1_office"

    runtime_controller = AsyncMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller_with_resolved_site",
        return_value=(runtime_controller, "office"),
    ) as create_controller_with_resolved_site:
        coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
        controller = await coordinator._ensure_controller()

    assert controller is runtime_controller
    assert create_controller_with_resolved_site.await_args.args[1].site == "site-office-id"


async def test_coordinator_async_shutdown_detaches_owned_runtime_session(
    hass: HomeAssistant, coordinator_config_entry: MagicMock
) -> None:
    """Test coordinator shutdown detaches an owned runtime session."""
    owned_session = MagicMock()
    owned_session.closed = False
    owned_session.detach = MagicMock()
    controller = MagicMock()
    controller._unifi_presence_owned_session = owned_session
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator._controller = controller

    await coordinator.async_shutdown()

    assert coordinator.controller is None
    owned_session.detach.assert_called_once_with()


async def test_update_single_device_state_adds_new_runtime_state_and_notifies_on_first_public_state(
    hass: HomeAssistant,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test first-time cache insertion publishes the new public device state."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)
    coordinator.last_update_success = False
    coordinator.async_update_listeners = MagicMock()

    coordinator._update_single_device_state(
        "aa:bb:cc:dd:ee:ff",
        is_home=False,
        name="Dan Phone",
        last_seen_ts=None,
        expiry_ts=None,
    )

    assert coordinator.data is not None
    assert coordinator.data.clients["aa:bb:cc:dd:ee:ff"].name == "Dan Phone"
    assert coordinator.last_update_success is True
    coordinator.async_update_listeners.assert_called_once_with()


async def test_controller_property(hass: HomeAssistant, coordinator_config_entry: MagicMock) -> None:
    """Test that the public controller property returns the cached controller."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    assert coordinator.controller is None

    mock_ctrl = MagicMock()
    coordinator._controller = mock_ctrl
    assert coordinator.controller is mock_ctrl


# ── Timestamp ordering / _set_last_seen ──────────────────────────────────


async def test_set_last_seen_rejects_stale_timestamp(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that _set_last_seen keeps the newer cached value when a stale timestamp arrives."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Seed with a recent timestamp
    result1 = coordinator._set_last_seen(mac, now)
    assert result1 == now
    assert coordinator._get_known_last_seen(mac) == now

    # Attempt to overwrite with an older timestamp
    stale = now - 300
    result2 = coordinator._set_last_seen(mac, stale)

    # Should return the existing (newer) value, cache unchanged
    assert result2 == now
    assert coordinator._get_known_last_seen(mac) == now


async def test_out_of_order_ws_does_not_regress_presence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that out-of-order WebSocket frames cannot move last_seen backwards."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # First WS message: device seen now -> home
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True
    assert coordinator._get_known_last_seen(mac) == now

    # Delayed/reordered WS message: stale timestamp from before the away threshold
    stale = now - coordinator.away_seconds - 10
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": stale}))

    # Should still be home — the stale timestamp must not overwrite the newer one
    assert coordinator.data.device_states[mac] is True
    assert coordinator._get_known_last_seen(mac) == now


async def test_stale_poll_does_not_regress_ws_last_seen(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that a REST poll with an older last_seen does not overwrite a newer WS value."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # WS message: device seen now
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True

    # REST poll returns an older last_seen (e.g. stale cache on controller)
    stale = now - 10
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=stale)
    data = await coordinator._async_update_data()

    # The newer WS timestamp must be preserved
    assert data.device_states[mac] is True
    assert coordinator._get_known_last_seen(mac) == now


async def test_poll_with_missing_last_seen_uses_cached(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that a poll with None/0 last_seen falls back to cached value."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"

    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # WS message: device seen now
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True

    # REST poll returns last_seen=0 (missing/null from controller)
    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=0)
    data = await coordinator._async_update_data()

    # Should still be home — 0 is treated as None and the cached timestamp is used
    assert data.device_states[mac] is True
    assert coordinator._get_known_last_seen(mac) == now


async def test_newer_but_expired_last_seen_updates_cache_and_marks_away(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that a newer timestamp that's still past the away threshold is accepted and marks away."""
    now = int(dt_util.utcnow().timestamp())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # WS message: device seen a while ago but within threshold
    initial_ts = now - 30
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": initial_ts}))
    assert coordinator.data.device_states[mac] is True

    # REST poll returns a newer timestamp, but it's past the away threshold
    newer_but_expired = now - coordinator.away_seconds - 5
    # This is only newer if initial_ts < newer_but_expired
    # initial_ts = now - 30, newer_but_expired = now - 65 → NOT newer
    # So let's use a scenario where the initial was even older
    old_ts = now - coordinator.away_seconds - 100
    coordinator.data.clients[mac].last_seen_ts = old_ts  # simulate very old cache

    mock_coordinator_controller.clients[mac] = _make_mock_client(mac, name="Dan Phone", last_seen=newer_but_expired)
    data = await coordinator._async_update_data()

    # The newer timestamp should be accepted (it's > old_ts)
    assert coordinator._get_known_last_seen(mac) == newer_but_expired
    # But it's still past the away threshold, so device is away
    assert data.device_states[mac] is False
