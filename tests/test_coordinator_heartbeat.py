"""Tests for the UniFi Presence coordinator — heartbeat expiry and scheduling."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator


async def test_heartbeat_expiry_marks_recently_offline_client_away(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test heartbeat expiry marks a client away without waiting for fallback poll."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": int(now.timestamp()),
            }
        )
    )

    assert coordinator.data[mac][0] is True
    snapshot = coordinator.data
    assert coordinator.heartbeat_expiry_count == 1

    freezer.tick(timedelta(seconds=coordinator.away_seconds + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.data[mac][0] is False
    assert snapshot[mac][0] is True
    assert coordinator.heartbeat_expiry_count == 0
    assert coordinator._cancel_heartbeat_check is None


async def test_heartbeat_expiry_skips_client_when_newer_activity_arrives(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test stale heartbeat expiry does not mark away after a newer reconnect."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": int((now - timedelta(seconds=coordinator.away_seconds - 1)).timestamp()),
            }
        )
    )

    stale_expiry = int((dt_util.utcnow() - timedelta(seconds=1)).timestamp())
    coordinator._client_states[mac].expiry_ts = stale_expiry
    coordinator._client_states[mac].last_seen_ts = int(now.timestamp())

    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data[mac][0] is True
    assert coordinator.heartbeat_expiry_count == 1
    # Verify expiry was refreshed forward based on the newer last_seen
    assert coordinator._client_states[mac].expiry_ts is not None
    assert coordinator._client_states[mac].expiry_ts > stale_expiry


async def test_async_shutdown_clears_heartbeat_tracking(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test shutdown clears heartbeat expiry and cached last_seen state."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": int(now.timestamp()),
            }
        )
    )

    assert coordinator.heartbeat_expiry_count == 1
    assert coordinator._get_known_last_seen(mac) == int(now.timestamp())

    await coordinator.async_shutdown()

    assert coordinator.heartbeat_expiry_count == 0
    assert coordinator._get_known_last_seen(mac) is None


async def test_heartbeat_expiry_preserves_last_update_success_false(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test heartbeat expiry does not mask a real controller failure.

    After a failed refresh, last_update_success is False and entities are
    unavailable.  A subsequent local heartbeat expiry must not flip
    last_update_success back to True.
    """
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Establish device as home via WS
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": int(now.timestamp())}))
    assert coordinator.data[mac][0] is True
    assert coordinator.last_update_success is True

    # Simulate a controller failure
    coordinator.last_update_success = False

    # Force heartbeat expiry
    freezer.tick(timedelta(seconds=coordinator.away_seconds + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Device should transition to away, but last_update_success must stay False
    assert coordinator.data[mac][0] is False
    assert coordinator.last_update_success is False


async def test_heartbeat_expiry_does_not_reset_refresh_timer(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test heartbeat expiry does not push the next REST poll further out.

    During a WebSocket outage, heartbeat expiries should not reschedule
    the coordinator refresh timer — that would weaken the REST fallback.
    """
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Establish device as home via WS
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": int(now.timestamp())}))

    # Spy on _schedule_refresh (called by async_set_updated_data but not by
    # our direct data assignment + async_update_listeners path)
    with patch.object(coordinator, "_schedule_refresh") as mock_schedule:
        freezer.tick(timedelta(seconds=coordinator.away_seconds + 1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        mock_schedule.assert_not_called()


async def test_heartbeat_expiry_noop_when_already_away(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test heartbeat expiry is a no-op for a device already marked not_home."""
    now = dt_util.utcnow()
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Device starts home
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": int(now.timestamp())}))
    assert coordinator.data[mac][0] is True

    # Expire it normally
    freezer.tick(timedelta(seconds=coordinator.away_seconds + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data[mac][0] is False

    # Re-inject a stale heartbeat entry for the already-away device
    coordinator._client_states[mac].expiry_ts = int((dt_util.utcnow() - timedelta(seconds=1)).timestamp())
    snapshot = coordinator.data

    coordinator._async_check_heartbeat_expiry()

    # Data object unchanged — already away, no redundant update
    assert coordinator.data is snapshot
    assert coordinator.heartbeat_expiry_count == 0


async def test_heartbeat_expiry_mixed_devices(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test heartbeat sweep with one device expiring and another staying home."""
    now = dt_util.utcnow()
    mac1 = "aa:bb:cc:dd:ee:ff"
    mac2 = "11:22:33:44:55:66"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Both devices home
    coordinator.process_message(MagicMock(data={"mac": mac1, "name": "Dan Phone", "last_seen": int(now.timestamp())}))
    coordinator.process_message(MagicMock(data={"mac": mac2, "name": "Jane Phone", "last_seen": int(now.timestamp())}))
    assert coordinator.data[mac1][0] is True
    assert coordinator.data[mac2][0] is True
    assert coordinator.heartbeat_expiry_count == 2

    # Expire mac1 only; mac2 gets a fresh websocket message first.
    freezer.tick(timedelta(seconds=coordinator.away_seconds - 1))
    coordinator.process_message(
        MagicMock(data={"mac": mac2, "name": "Jane Phone", "last_seen": int(dt_util.utcnow().timestamp())})
    )
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.data[mac1][0] is False
    assert coordinator.data[mac2][0] is True
    # mac1 expired and removed, mac2 still tracked
    assert coordinator.heartbeat_expiry_count == 1


async def test_reschedule_heartbeat_schedules_at_earliest_expiry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test that _reschedule_heartbeat_check schedules at the earliest pending expiry."""
    now = dt_util.utcnow()
    mac1 = "aa:bb:cc:dd:ee:ff"
    mac2 = "11:22:33:44:55:66"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Both devices home via WS
    coordinator.process_message(MagicMock(data={"mac": mac1, "name": "Dan Phone", "last_seen": int(now.timestamp())}))
    coordinator.process_message(
        MagicMock(data={"mac": mac2, "name": "Jane Phone", "last_seen": int((now - timedelta(seconds=30)).timestamp())})
    )

    # A heartbeat check should be scheduled
    assert coordinator._cancel_heartbeat_check is not None
    assert coordinator.heartbeat_expiry_count == 2


async def test_reschedule_heartbeat_keeps_existing_timer_when_earliest_expiry_unchanged(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_coordinator_controller: AsyncMock,
    coordinator_config_entry: MagicMock,
) -> None:
    """Test later heartbeat updates do not churn the scheduled timer."""
    now = dt_util.utcnow()
    earlier_mac = "aa:bb:cc:dd:ee:ff"
    later_mac = "11:22:33:44:55:66"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(data={"mac": earlier_mac, "name": "Dan Phone", "last_seen": int(now.timestamp())})
    )
    coordinator.process_message(
        MagicMock(data={"mac": later_mac, "name": "Jane Phone", "last_seen": int(now.timestamp())})
    )

    initial_handle = coordinator._cancel_heartbeat_check
    assert initial_handle is not None

    freezer.tick(timedelta(seconds=5))
    coordinator.process_message(
        MagicMock(data={"mac": later_mac, "name": "Jane Phone", "last_seen": int(dt_util.utcnow().timestamp())})
    )

    assert coordinator._cancel_heartbeat_check is initial_handle


async def test_reschedule_heartbeat_clears_when_no_expiries(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that _reschedule_heartbeat_check cancels the timer when no expiries remain."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # No devices tracked as home
    assert coordinator._cancel_heartbeat_check is None
    assert coordinator.heartbeat_expiry_count == 0

    # Manually call reschedule — should remain None
    coordinator._reschedule_heartbeat_check()
    assert coordinator._cancel_heartbeat_check is None


async def test_heartbeat_expiry_noop_without_tracked_state(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat expiry checks return immediately with no tracked state."""
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data is None
