"""The UniFi Presence integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
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

    def _async_shutdown(_event: object) -> None:
        """Stop WebSocket on Home Assistant shutdown."""
        if coordinator.websocket is not None:
            coordinator.websocket.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    if coordinator.websocket is not None:
        await coordinator.websocket.stop_and_wait()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: UnifiPresenceConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal of a device if it is no longer tracked."""
    coordinator = config_entry.runtime_data
    tracked = frozenset(coordinator.tracked_devices)

    # Allow removal only if the device MAC is not in the tracked set
    return not any(
        identifier for identifier in device_entry.identifiers if identifier[0] == DOMAIN and identifier[1] in tracked
    )
