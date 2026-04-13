"""Tests for the UniFi Presence device tracker platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.device_tracker import SourceType

from custom_components.unifi_presence.coordinator import UnifiPresenceData
from custom_components.unifi_presence.device_tracker import (
    PARALLEL_UPDATES,
    UnifiPresenceTracker,
)


def _make_coordinator(data: UnifiPresenceData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.tracked_devices = list(data.device_states.keys()) if data else []
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry_id"
    coordinator.site_id = "default"
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
            "mac": mac,
        }

    for mac in away_macs or []:
        states[mac] = False
        info[mac] = {
            "name": mac,
            "mac": mac,
        }

    return UnifiPresenceData(
        device_states=states,
        client_info=info,
    )


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
    """Test the unique ID includes the site ID and MAC address."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.unique_id == "default-aa:bb:cc:dd:ee:ff"


def test_tracker_unique_id_differs_by_site() -> None:
    """Test that the same MAC on different sites gets distinct unique IDs."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    default_coordinator = _make_coordinator(data)
    office_coordinator = _make_coordinator(data)
    office_coordinator.site_id = "office"

    default_tracker = UnifiPresenceTracker(default_coordinator, "aa:bb:cc:dd:ee:ff")
    office_tracker = UnifiPresenceTracker(office_coordinator, "aa:bb:cc:dd:ee:ff")

    assert default_tracker.unique_id == "default-aa:bb:cc:dd:ee:ff"
    assert office_tracker.unique_id == "office-aa:bb:cc:dd:ee:ff"


def test_tracker_mac_address() -> None:
    """Test that mac_address returns the tracked MAC."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.mac_address == "aa:bb:cc:dd:ee:ff"


def test_tracker_has_entity_name() -> None:
    """Test that trackers follow entity-name based naming."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker._attr_has_entity_name is True
    assert tracker.name == "Device aa:bb:cc"


def test_tracker_name_prefers_runtime_name() -> None:
    """Test that tracker name uses the resolved runtime name."""
    data = UnifiPresenceData(
        device_states={"aa:bb:cc:dd:ee:ff": True},
        client_info={
            "aa:bb:cc:dd:ee:ff": {
                "name": "Dan Phone",
                "mac": "aa:bb:cc:dd:ee:ff",
            }
        },
    )
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")

    assert tracker.name == "Dan Phone"


def test_tracker_entity_registry_enabled_default() -> None:
    """Test that entities are enabled by default (overrides ScannerEntity default)."""
    data = _make_presence_data(home_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.entity_registry_enabled_default is True


def test_tracker_name_falls_back_to_mac_without_runtime_data() -> None:
    """Test that name falls back to MAC when coordinator has no data."""
    coordinator = _make_coordinator(None)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")
    assert tracker.name == "aa:bb:cc:dd:ee:ff"


def test_parallel_updates_is_zero() -> None:
    """Test that PARALLEL_UPDATES is 0 (coordinator handles updates)."""
    assert PARALLEL_UPDATES == 0


def test_tracker_is_connected_missing_mac() -> None:
    """Test that is_connected returns False when MAC is not in device_states."""
    # Data exists but does not contain the tracker's MAC
    data = _make_presence_data(home_macs=["11:22:33:44:55:66"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "ff:ff:ff:ff:ff:ff")
    assert tracker.is_connected is False


def test_tracker_available_true_when_offline() -> None:
    """Test that offline tracked clients remain available (with not_home state)."""
    data = _make_presence_data(
        away_macs=["aa:bb:cc:dd:ee:ff"],
    )
    data.client_info["aa:bb:cc:dd:ee:ff"]["name"] = "Dan Phone"
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")

    assert tracker.available is True
    assert tracker.is_connected is False
    assert tracker.name == "Dan Phone"


def test_tracker_available_true_when_away_but_present() -> None:
    """Test that away tracked clients stay available when still present."""
    data = _make_presence_data(away_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")

    assert tracker.available is True


def test_tracker_available_false_when_coordinator_update_failed() -> None:
    """Test that coordinator update failures still make trackers unavailable."""
    data = _make_presence_data(away_macs=["aa:bb:cc:dd:ee:ff"])
    coordinator = _make_coordinator(data)
    coordinator.last_update_success = False

    tracker = UnifiPresenceTracker(coordinator, "aa:bb:cc:dd:ee:ff")

    assert tracker.available is False
