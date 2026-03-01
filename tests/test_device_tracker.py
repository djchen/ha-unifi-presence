"""Tests for the UniFi Presence device tracker platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.device_tracker import SourceType

from custom_components.unifi_presence.coordinator import UnifiPresenceData
from custom_components.unifi_presence.device_tracker import UnifiPresenceTracker


def _make_coordinator(data: UnifiPresenceData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.tracked_devices = list(data.device_states.keys()) if data else []
    return coordinator


def _make_presence_data(
    home_macs: list[str] | None = None,
    away_macs: list[str] | None = None,
) -> UnifiPresenceData:
    """Create test presence data."""
    states: dict[str, bool] = {}
    info: dict[str, dict] = {}

    for mac in home_macs or []:
        states[mac] = True
        info[mac] = {
            "name": f"Device {mac[:8]}",
            "hostname": f"host-{mac[:8]}",
            "ip": "192.168.1.100",
            "mac": mac,
            "is_wired": False,
            "last_seen": 1700000000,
        }

    for mac in away_macs or []:
        states[mac] = False
        info[mac] = {
            "name": mac,
            "hostname": "",
            "ip": "",
            "mac": mac,
            "is_wired": False,
            "last_seen": 0,
        }

    return UnifiPresenceData(device_states=states, client_info=info)


def test_tracker_is_connected_when_home() -> None:
    """Test that is_connected returns True when device is home."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.is_connected is True


def test_tracker_not_connected_when_away() -> None:
    """Test that is_connected returns False when device is away."""
    data = _make_presence_data(away_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.is_connected is False


def test_tracker_not_connected_when_no_data() -> None:
    """Test that is_connected returns False when coordinator has no data."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.is_connected is False


def test_tracker_source_type() -> None:
    """Test that source type is ROUTER."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.source_type is SourceType.ROUTER


def test_tracker_unique_id() -> None:
    """Test the unique ID format (ScannerEntity uses mac_address as unique_id)."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.unique_id == "aa:bb:cc:dd:ee:ff"


def test_tracker_mac_address() -> None:
    """Test that mac_address returns the tracked MAC."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.mac_address == "aa:bb:cc:dd:ee:ff"


def test_tracker_ip_address() -> None:
    """Test that ip_address returns the client IP when available."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.ip_address == "192.168.1.100"


def test_tracker_ip_address_none_when_empty() -> None:
    """Test that ip_address returns None when not available."""
    data = _make_presence_data(away_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.ip_address is None


def test_tracker_ip_address_none_no_data() -> None:
    """Test that ip_address returns None when coordinator.data is None."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.ip_address is None


def test_tracker_hostname() -> None:
    """Test that hostname returns the client hostname."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.hostname == "host-aa:bb:cc"


def test_tracker_name_from_client_info() -> None:
    """Test that name comes from client info when available."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.name == "Device aa:bb:cc"


def test_tracker_extra_attributes() -> None:
    """Test extra state attributes."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    attrs = tracker.extra_state_attributes
    assert attrs is not None
    assert attrs["is_wired"] is False
    assert attrs["last_seen"] == 1700000000


def test_tracker_name_fallback_no_data() -> None:
    """Test that name returns MAC when coordinator.data is None."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.name == "aa:bb:cc:dd:ee:ff"


def test_tracker_hostname_none_no_data() -> None:
    """Test that hostname returns None when coordinator.data is None."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.hostname is None


def test_tracker_attrs_none_no_data() -> None:
    """Test that extra_state_attributes returns None when coordinator.data is None."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.extra_state_attributes is None
