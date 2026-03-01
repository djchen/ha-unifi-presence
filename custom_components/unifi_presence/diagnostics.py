"""Diagnostics support for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import UnifiPresenceConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


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

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "tracked_device_count": tracked_count,
        "device_states": device_states,
        "away_seconds": coordinator.away_seconds,
        "fallback_poll_interval_seconds": coordinator.update_interval.total_seconds(),
        "websocket_connected": websocket_connected,
    }
