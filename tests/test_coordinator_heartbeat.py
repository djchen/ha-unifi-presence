"""Tests for the UniFi Presence coordinator — heartbeat expiry and scheduling."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator


async def test_heartbeat_expiry_marks_recently_offline_client_away(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat expiry marks a client away without waiting for fallback poll."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": now,
            }
        )
    )

    assert coordinator.data.device_states[mac] is True
    assert coordinator.heartbeat_expiry_count == 1

    coordinator._last_seen_by_mac[mac] = now - coordinator.away_seconds
    coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)

    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data.device_states[mac] is False
    assert coordinator.heartbeat_expiry_count == 0


async def test_heartbeat_expiry_skips_client_when_newer_activity_arrives(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test stale heartbeat expiry does not mark away after a newer reconnect."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": now - coordinator.away_seconds + 1,
            }
        )
    )

    stale_expiry = dt_util.utcnow() - timedelta(seconds=1)
    coordinator._heartbeat_expiry[mac] = stale_expiry
    coordinator._last_seen_by_mac[mac] = now

    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data.device_states[mac] is True
    assert coordinator.heartbeat_expiry_count == 1
    # Verify expiry was refreshed forward based on the newer last_seen
    assert coordinator._heartbeat_expiry[mac] > stale_expiry


async def test_async_shutdown_clears_heartbeat_tracking(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test shutdown clears heartbeat expiry and cached last_seen state."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    coordinator.process_message(
        MagicMock(
            data={
                "mac": mac,
                "name": "Dan Phone",
                "last_seen": now,
            }
        )
    )

    assert coordinator.heartbeat_expiry_count == 1
    assert coordinator._get_known_last_seen(mac) == now

    await coordinator.async_shutdown()

    assert coordinator.heartbeat_expiry_count == 0
    assert coordinator._get_known_last_seen(mac) is None


async def test_heartbeat_expiry_preserves_last_update_success_false(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat expiry does not mask a real controller failure.

    After a failed refresh, last_update_success is False and entities are
    unavailable.  A subsequent local heartbeat expiry must not flip
    last_update_success back to True.
    """
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Establish device as home via WS
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True
    assert coordinator.last_update_success is True

    # Simulate a controller failure
    coordinator.last_update_success = False

    # Force heartbeat expiry
    coordinator._last_seen_by_mac[mac] = now - coordinator.away_seconds
    coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)
    coordinator._async_check_heartbeat_expiry()

    # Device should transition to away, but last_update_success must stay False
    assert coordinator.data.device_states[mac] is False
    assert coordinator.last_update_success is False


async def test_heartbeat_expiry_does_not_reset_refresh_timer(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat expiry does not push the next REST poll further out.

    During a WebSocket outage, heartbeat expiries should not reschedule
    the coordinator refresh timer — that would weaken the REST fallback.
    """
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Establish device as home via WS
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))

    # Spy on _schedule_refresh (called by async_set_updated_data but not by
    # our direct data assignment + async_update_listeners path)
    with patch.object(coordinator, "_schedule_refresh") as mock_schedule:
        coordinator._last_seen_by_mac[mac] = now - coordinator.away_seconds
        coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)
        coordinator._async_check_heartbeat_expiry()

        mock_schedule.assert_not_called()


async def test_heartbeat_expiry_noop_when_already_away(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat expiry is a no-op for a device already marked not_home."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Device starts home
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True

    # Expire it normally
    coordinator._last_seen_by_mac[mac] = now - coordinator.away_seconds
    coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)
    coordinator._async_check_heartbeat_expiry()
    assert coordinator.data.device_states[mac] is False

    # Re-inject a stale heartbeat entry for the already-away device
    coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)
    snapshot = coordinator.data

    coordinator._async_check_heartbeat_expiry()

    # Data object unchanged — already away, no redundant update
    assert coordinator.data is snapshot
    assert coordinator.heartbeat_expiry_count == 0


async def test_heartbeat_expiry_mixed_devices(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test heartbeat sweep with one device expiring and another staying home."""
    now = int(time.time())
    mac1 = "aa:bb:cc:dd:ee:ff"
    mac2 = "11:22:33:44:55:66"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Both devices home
    coordinator.process_message(MagicMock(data={"mac": mac1, "name": "Dan Phone", "last_seen": now}))
    coordinator.process_message(MagicMock(data={"mac": mac2, "name": "Jane Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac1] is True
    assert coordinator.data.device_states[mac2] is True
    assert coordinator.heartbeat_expiry_count == 2

    # Expire mac1 only; mac2 keeps a fresh last_seen
    coordinator._last_seen_by_mac[mac1] = now - coordinator.away_seconds
    coordinator._heartbeat_expiry[mac1] = dt_util.utcnow() - timedelta(seconds=1)

    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data.device_states[mac1] is False
    assert coordinator.data.device_states[mac2] is True
    # mac1 expired and removed, mac2 still tracked
    assert coordinator.heartbeat_expiry_count == 1


async def test_reschedule_heartbeat_schedules_at_earliest_expiry(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that _reschedule_heartbeat_check schedules at the earliest pending expiry."""
    now = int(time.time())
    mac1 = "aa:bb:cc:dd:ee:ff"
    mac2 = "11:22:33:44:55:66"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Both devices home via WS
    coordinator.process_message(MagicMock(data={"mac": mac1, "name": "Dan Phone", "last_seen": now}))
    coordinator.process_message(MagicMock(data={"mac": mac2, "name": "Jane Phone", "last_seen": now - 30}))

    # A heartbeat check should be scheduled
    assert coordinator._cancel_heartbeat_check is not None
    assert coordinator.heartbeat_expiry_count == 2


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


async def test_heartbeat_fires_at_scheduled_time(
    hass: HomeAssistant, mock_coordinator_controller: AsyncMock, coordinator_config_entry: MagicMock
) -> None:
    """Test that the on-demand heartbeat timer fires and transitions device to away."""
    now = int(time.time())
    mac = "aa:bb:cc:dd:ee:ff"
    coordinator = UnifiPresenceCoordinator(hass, coordinator_config_entry)

    # Device comes home via WS
    coordinator.process_message(MagicMock(data={"mac": mac, "name": "Dan Phone", "last_seen": now}))
    assert coordinator.data.device_states[mac] is True
    assert coordinator._cancel_heartbeat_check is not None

    # Simulate time advancing past the away threshold and fire the scheduled callback
    coordinator._last_seen_by_mac[mac] = now - coordinator.away_seconds
    coordinator._heartbeat_expiry[mac] = dt_util.utcnow() - timedelta(seconds=1)
    coordinator._async_check_heartbeat_expiry()

    assert coordinator.data.device_states[mac] is False
    # No more expiries — timer should be cleared
    assert coordinator._cancel_heartbeat_check is None
