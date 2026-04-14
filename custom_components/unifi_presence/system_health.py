"""System health support for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback

from .const import CONF_SITE, CONF_TRACKED_DEVICES, DEFAULT_SITE, DOMAIN


@callback
def async_register(
    hass: HomeAssistant,
    register: system_health.SystemHealthRegistration,
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return info for the system health page."""
    config_entries = hass.config_entries.async_entries(DOMAIN)

    controller_targets: list[str] = []
    loaded_entries = 0
    coordinator_available = 0
    tracked_devices = 0
    websocket_connected = 0
    heartbeat_expiry = 0

    for entry in config_entries:
        site = entry.data.get(CONF_SITE, DEFAULT_SITE)
        controller_targets.append(f"{entry.data[CONF_HOST]} ({site})")
        tracked_devices += len(entry.options.get(CONF_TRACKED_DEVICES, []))

        if entry.state is not ConfigEntryState.LOADED:
            continue

        loaded_entries += 1
        coordinator = entry.runtime_data

        if coordinator.last_update_success:
            coordinator_available += 1

        if coordinator.websocket is not None and coordinator.websocket.available:
            websocket_connected += 1

        heartbeat_expiry += coordinator.heartbeat_expiry_count

    return {
        "config_entry_count": len(config_entries),
        "loaded_entry_count": loaded_entries,
        "coordinator_available_count": coordinator_available,
        "websocket_connected_count": websocket_connected,
        "heartbeat_expiry_count": heartbeat_expiry,
        "tracked_device_count": tracked_devices,
        "controllers": ", ".join(controller_targets) if controller_targets else "none",
    }
