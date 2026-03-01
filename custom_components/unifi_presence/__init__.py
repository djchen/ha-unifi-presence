"""The UniFi Presence integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import UnifiPresenceCoordinator
from .websocket import UnifiPresenceWebsocket

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Start WebSocket for real-time presence updates
    controller = coordinator.controller
    if controller is not None:
        websocket = UnifiPresenceWebsocket(
            hass,
            controller,
            coordinator.signal_reachable,
            coordinator.process_message,
        )
        websocket.start()
        coordinator.websocket = websocket

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    if coordinator.websocket is not None:
        await coordinator.websocket.stop_and_wait()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
