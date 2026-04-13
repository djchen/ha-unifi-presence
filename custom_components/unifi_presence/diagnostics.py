"""Diagnostics support for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import UnifiPresenceConfigEntry
from .const import (
    CONF_TRACKED_DEVICES,
)
from .helpers import build_entry_runtime_summary, get_entry_runtime_coordinator

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


def _redact_mac_list(macs: list[str]) -> list[str]:
    """Return a copy of a list with MAC addresses partially redacted."""
    return [_partial_redact_mac(m) for m in macs]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = get_entry_runtime_coordinator(entry)
    coordinator_data = coordinator.data if coordinator is not None else None
    summary = build_entry_runtime_summary(entry)

    device_states = coordinator_data.device_states if coordinator_data is not None else {}

    # Redact options containing MAC addresses
    redacted_options = dict(entry.options)
    if CONF_TRACKED_DEVICES in redacted_options:
        redacted_options[CONF_TRACKED_DEVICES] = _redact_mac_list(redacted_options[CONF_TRACKED_DEVICES])

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": redacted_options,
        },
        "tracked_device_count": summary.tracked_device_count,
        "device_states": _redact_mac_keys(device_states),
        "away_seconds": summary.away_seconds,
        "fallback_poll_interval_seconds": summary.fallback_poll_interval_seconds,
        "websocket_connected": summary.websocket_connected,
        "heartbeat_expiry_count": summary.heartbeat_expiry_count,
    }
