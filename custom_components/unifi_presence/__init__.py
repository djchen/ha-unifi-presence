"""The UniFi Presence integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .coordinator import UnifiPresenceCoordinator
from .websocket import UnifiPresenceWebsocket

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Start WebSocket for real-time presence updates
    if coordinator.controller is not None:
        websocket = UnifiPresenceWebsocket(
            hass,
            lambda: coordinator.controller,
            coordinator.signal_reachable,
            coordinator.process_message,
        )
        websocket.start()
        coordinator.websocket = websocket

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Remove stale device entries for MACs no longer tracked (e.g. after options change)
    tracked_set = frozenset(coordinator.tracked_devices)
    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        device_macs = {mac for conn_type, mac in device_entry.connections if conn_type == CONNECTION_NETWORK_MAC}
        if not device_macs & tracked_set:
            device_registry.async_update_device(device_entry.id, remove_config_entry_id=entry.entry_id)

    @callback
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
    """Allow removal of a device only if it is no longer tracked."""
    coordinator = config_entry.runtime_data
    tracked = frozenset(coordinator.tracked_devices)
    return not any(
        mac for conn_type, mac in device_entry.connections if conn_type == CONNECTION_NETWORK_MAC and mac in tracked
    )
