"""Tests for the UniFi Presence device tracker platform."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.device_tracker import SourceType

from custom_components.unifi_presence.coordinator import UnifiPresenceData
from custom_components.unifi_presence.device_tracker import (
    PARALLEL_UPDATES,
    UnifiPresenceTracker,
)

MAC = "aa:bb:cc:dd:ee:ff"


def _make_coordinator(data: UnifiPresenceData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.tracked_devices = list(data) if data else []
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry_id"
    coordinator.site_id = "default"
    return coordinator


@pytest.mark.parametrize(
    ("data", "update_success", "name", "connected", "available"),
    [
        pytest.param({MAC: (True, "Dan Phone")}, True, "Dan Phone", True, True, id="home"),
        pytest.param(
            {MAC: (False, "Dan Phone")},
            True,
            "Dan Phone",
            False,
            True,
            id="offline",
        ),
        pytest.param(None, True, MAC, False, False, id="no-data"),
        pytest.param(
            {"11:22:33:44:55:66": (True, "Other Device")},
            True,
            MAC,
            False,
            True,
            id="missing-client",
        ),
        pytest.param(
            {MAC: (False, "Dan Phone")},
            False,
            "Dan Phone",
            False,
            False,
            id="update-failed",
        ),
    ],
)
def test_tracker_runtime_state(
    data: UnifiPresenceData | None,
    update_success: bool,
    name: str,
    connected: bool,
    available: bool,
) -> None:
    """Test tracker state derived from coordinator data and health."""
    coordinator = _make_coordinator(data)
    coordinator.last_update_success = update_success

    tracker = UnifiPresenceTracker(coordinator, MAC)

    assert tracker.name == name
    assert tracker.is_connected is connected
    assert tracker.available is available


def test_tracker_metadata_contract() -> None:
    """Test the static tracker platform and entity metadata contract."""
    coordinator = _make_coordinator({MAC: (True, "Dan Phone")})
    coordinator.site_id = "office"

    tracker = UnifiPresenceTracker(coordinator, MAC)

    assert PARALLEL_UPDATES == 0
    assert tracker.source_type is SourceType.ROUTER
    assert tracker.unique_id == f"office-{MAC}"
    assert tracker.mac_address == MAC
    assert tracker._attr_has_entity_name is True
    assert tracker.entity_registry_enabled_default is True
