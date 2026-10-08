"""Tests for the UniFi Presence diagnostics platform."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.diagnostics import (
    _partial_redact_mac,
    _redact_mac_keys,
    async_get_config_entry_diagnostics,
)

from .conftest import MOCK_OPTIONS, add_mock_config_entry


@pytest.fixture
def runtime_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry with runtime data, without starting platforms or WebSocket tasks."""
    entry = add_mock_config_entry(
        hass,
        title="UniFi Presence (192.168.1.1)",
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )

    entry.runtime_data = SimpleNamespace(
        data={mac: (False, mac) for mac in MOCK_OPTIONS["tracked_devices"]},
        tracked_devices=tuple(MOCK_OPTIONS["tracked_devices"]),
        away_seconds=MOCK_OPTIONS["away_seconds"],
        update_interval=timedelta(seconds=MOCK_OPTIONS["fallback_poll_interval"]),
        websocket=SimpleNamespace(available=True),
        heartbeat_expiry_count=0,
    )
    return entry


async def test_diagnostics_redacts_credentials(hass: HomeAssistant, runtime_entry: MockConfigEntry) -> None:
    """Test that diagnostics redacts sensitive credentials and MAC addresses."""
    result = await async_get_config_entry_diagnostics(hass, runtime_entry)

    # Sensitive config entry data should be redacted.
    assert result["config_entry"]["data"]["host"] == "**REDACTED**"
    assert result["config_entry"]["data"]["username"] == "**REDACTED**"
    assert result["config_entry"]["data"]["password"] == "**REDACTED**"

    # Site remains visible in diagnostics data.
    assert result["config_entry"]["data"]["site"] == "default"
    assert result["tracked_device_count"] == 2
    assert result["away_seconds"] == 60
    assert result["fallback_poll_interval_seconds"] == 300
    assert result["websocket_connected"] is True
    assert result["devices_with_active_away_timers"] == 0

    # MAC addresses in options should be partially redacted
    assert result["config_entry"]["options"]["tracked_devices"] == ["**:**:**:dd:ee:ff", "**:**:**:44:55:66"]

    # MAC addresses in device_states keys should be partially redacted
    assert result["device_states"] == {"**:**:**:dd:ee:ff": False, "**:**:**:44:55:66": False}
    # Building diagnostics must not redact the stored entry in place.
    assert runtime_entry.data["host"] == "192.168.1.1"
    assert runtime_entry.options["tracked_devices"] == MOCK_OPTIONS["tracked_devices"]


async def test_diagnostics_websocket_none(hass: HomeAssistant, runtime_entry: MockConfigEntry) -> None:
    """Test that diagnostics reports websocket_connected=False when websocket is None."""
    runtime_entry.runtime_data.websocket = None

    result = await async_get_config_entry_diagnostics(hass, runtime_entry)

    assert result["websocket_connected"] is False


async def test_diagnostics_without_runtime_data(hass: HomeAssistant) -> None:
    """Test that diagnostics falls back to config entry data when runtime_data is unavailable."""
    entry = add_mock_config_entry(
        hass,
        title="UniFi Presence (192.168.1.1)",
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["tracked_device_count"] == len(MOCK_OPTIONS["tracked_devices"])
    assert result["device_states"] == {}
    assert result["away_seconds"] == MOCK_OPTIONS["away_seconds"]
    assert result["fallback_poll_interval_seconds"] == MOCK_OPTIONS["fallback_poll_interval"]
    assert result["websocket_connected"] is False
    assert result["devices_with_active_away_timers"] == 0


def test_partial_redact_mac_standard() -> None:
    """Test partial MAC redaction keeps last 3 octets."""
    assert _partial_redact_mac("aa:bb:cc:dd:ee:ff") == "**:**:**:dd:ee:ff"


def test_partial_redact_mac_malformed() -> None:
    """Test that malformed MAC addresses are fully redacted."""
    assert _partial_redact_mac("not-a-mac") == "**REDACTED**"
    assert _partial_redact_mac("") == "**REDACTED**"


def test_redact_mac_keys() -> None:
    """Test that dict keys containing MACs are partially redacted."""
    data = {"aa:bb:cc:dd:ee:ff": True, "11:22:33:44:55:66": False}
    result = _redact_mac_keys(data)
    assert result == {"**:**:**:dd:ee:ff": True, "**:**:**:44:55:66": False}
    assert data == {"aa:bb:cc:dd:ee:ff": True, "11:22:33:44:55:66": False}


async def test_diagnostics_preserves_mac_suffix_collisions(hass: HomeAssistant, runtime_entry: MockConfigEntry) -> None:
    """Test that diagnostics keep all device states when redacted MAC keys collide."""
    runtime_entry.runtime_data.data = {
        "aa:bb:cc:dd:ee:ff": (True, "Dan Phone"),
        "11:22:33:dd:ee:ff": (False, "Jane Phone"),
    }

    result = await async_get_config_entry_diagnostics(hass, runtime_entry)

    assert result["device_states"] == {"**:**:**:dd:ee:ff": True, "**:**:**:dd:ee:ff (2)": False}
