"""Tests for the UniFi Presence device tracker platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.device_tracker import SourceType

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceData
from custom_components.unifi_presence.device_tracker import (
    PARALLEL_UPDATES,
    UnifiPresenceTracker,
)


def _make_coordinator(data: UnifiPresenceData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.tracked_devices = list(data.device_states.keys()) if data else []
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry_id"
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


def test_tracker_has_entity_name() -> None:
    """Test that has_entity_name is True and name is None (inherits device name)."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker._attr_has_entity_name is True
    assert tracker.name is None


def test_tracker_extra_attributes() -> None:
    """Test extra state attributes."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    attrs = tracker.extra_state_attributes
    assert attrs is not None
    assert attrs["is_wired"] is False
    assert attrs["last_seen"] == 1700000000


def test_tracker_device_info() -> None:
    """Test that device_info is populated with correct identifiers and name."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    device_info = tracker._attr_device_info
    assert device_info is not None
    assert (DOMAIN, "aa:bb:cc:dd:ee:ff") in device_info["identifiers"]
    assert device_info["default_name"] == "Device aa:bb:cc"
    assert device_info["default_manufacturer"] == "Ubiquiti Networks"


def test_tracker_device_info_fallback_no_data() -> None:
    """Test that device_info uses MAC as name when coordinator.data is None."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    device_info = tracker._attr_device_info
    assert device_info is not None
    assert device_info["default_name"] == "aa:bb:cc:dd:ee:ff"


def test_tracker_translation_key() -> None:
    """Test that translation_key is set for entity translations."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker._attr_translation_key == "presence"


def test_parallel_updates_is_zero() -> None:
    """Test that PARALLEL_UPDATES is 0 (coordinator handles updates)."""
    assert PARALLEL_UPDATES == 0


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


def test_tracker_is_connected_missing_mac() -> None:
    """Test that is_connected returns False when MAC is not in device_states."""
    # Data exists but does not contain the tracker's MAC
    data = _make_presence_data(home_macs=["11:22:33:44:55:66"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "ff:ff:ff:ff:ff:ff")
    assert tracker.is_connected is False
