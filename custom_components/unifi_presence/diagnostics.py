"""Diagnostics support for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import UnifiPresenceConfigEntry
from .const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_FALLBACK_POLL_INTERVAL,
)

TO_REDACT = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME}


def _partial_redact_mac(mac: str) -> str:
    """Redact the first 3 octets of a MAC address, keeping the last 3."""
    parts = mac.split(":")
    if len(parts) == 6:
        return f"**:**:**:{parts[3]}:{parts[4]}:{parts[5]}"
    return "**REDACTED**"


def _redact_mac_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a dict with MAC-address keys partially redacted."""
    redacted: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for key, value in data.items():
        redacted_key = _partial_redact_mac(key)
        count = counts.get(redacted_key, 0) + 1
        counts[redacted_key] = count
        unique_key = redacted_key if count == 1 else f"{redacted_key} ({count})"
        redacted[unique_key] = value
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    coordinator_data = coordinator.data if coordinator is not None else None
    tracked_count = len(entry.options.get(CONF_TRACKED_DEVICES, []))
    away_seconds = entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)
    fallback_poll_interval_seconds = entry.options.get(
        CONF_FALLBACK_POLL_INTERVAL,
        DEFAULT_FALLBACK_POLL_INTERVAL,
    )
    websocket_connected = False
    devices_with_active_away_timers = 0

    if coordinator is not None:
        tracked_count = len(coordinator.tracked_devices)
        away_seconds = coordinator.away_seconds
        fallback_poll_interval_seconds = (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        )
        websocket_connected = coordinator.websocket is not None and coordinator.websocket.available
        devices_with_active_away_timers = coordinator.heartbeat_expiry_count

    device_states = (
        {mac: client.is_home for mac, client in coordinator_data.clients.items()}
        if coordinator_data is not None
        else {}
    )

    # Redact options containing MAC addresses
    redacted_options = dict(entry.options)
    if CONF_TRACKED_DEVICES in redacted_options:
        redacted_options[CONF_TRACKED_DEVICES] = [
            _partial_redact_mac(mac) for mac in redacted_options[CONF_TRACKED_DEVICES]
        ]

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": redacted_options,
        },
        "tracked_device_count": tracked_count,
        "device_states": _redact_mac_keys(device_states),
        "away_seconds": away_seconds,
        "fallback_poll_interval_seconds": fallback_poll_interval_seconds,
        "websocket_connected": websocket_connected,
        "devices_with_active_away_timers": devices_with_active_away_timers,
    }
