"""Diagnostics support for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import UnifiPresenceConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


def _partial_redact_mac(mac: str) -> str:
    """Redact the first 3 octets of a MAC address, keeping the last 3."""
    parts = mac.split(":")
    if len(parts) == 6:
        return f"**:**:**:{parts[3]}:{parts[4]}:{parts[5]}"
    return "**REDACTED**"


def _redact_mac_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a dict with MAC-address keys partially redacted."""
    return {_partial_redact_mac(k): v for k, v in data.items()}


def _redact_mac_list(macs: list[str]) -> list[str]:
    """Return a copy of a list with MAC addresses partially redacted."""
    return [_partial_redact_mac(m) for m in macs]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    device_states: dict[str, bool] = {}
    tracked_count = len(coordinator.tracked_devices)
    if coordinator.data is not None:
        device_states = coordinator.data.device_states

    websocket_connected = coordinator.websocket is not None and coordinator.websocket.available

    # Redact options containing MAC addresses
    redacted_options = dict(entry.options)
    if "tracked_devices" in redacted_options:
        redacted_options["tracked_devices"] = _redact_mac_list(redacted_options["tracked_devices"])

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": redacted_options,
        },
        "tracked_device_count": tracked_count,
        "device_states": _redact_mac_keys(device_states),
        "away_seconds": coordinator.away_seconds,
        "fallback_poll_interval_seconds": coordinator.update_interval.total_seconds()
        if coordinator.update_interval
        else None,
        "websocket_connected": websocket_connected,
    }
