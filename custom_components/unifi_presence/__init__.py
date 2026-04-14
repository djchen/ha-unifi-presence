"""The UniFi Presence integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_TRACKED_DEVICES
from .coordinator import UnifiPresenceCoordinator
from .helpers import normalize_macs, tracker_unique_id
from .websocket import UnifiPresenceWebsocket

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


@callback
def _async_remove_deselected_entities(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
    removed_macs: set[str],
) -> None:
    """Remove entity registry entries for explicitly deselected tracked clients."""
    if not removed_macs:
        return

    entity_registry = er.async_get(hass)
    removed_unique_ids = {tracker_unique_id(entry.runtime_data.site_id, mac) for mac in removed_macs}

    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registry_entry.unique_id in removed_unique_ids:
            entity_registry.async_remove(registry_entry.entity_id)


async def _async_handle_entry_update(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> None:
    """Clean up deselected entities before Home Assistant reloads the entry."""
    coordinator = entry.runtime_data
    removed_macs = set(normalize_macs(coordinator.tracked_devices)) - set(
        normalize_macs(entry.options.get(CONF_TRACKED_DEVICES, []))
    )
    _async_remove_deselected_entities(hass, entry, removed_macs)


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()

        # Start WebSocket for real-time presence updates
        if coordinator.controller is not None:
            websocket = UnifiPresenceWebsocket(
                hass,
                lambda: coordinator.controller,
                coordinator.process_message,
            )
            coordinator.websocket = websocket

        entry.runtime_data = coordinator

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        entry.async_on_unload(entry.add_update_listener(_async_handle_entry_update))

        @callback
        def _async_shutdown(_event: object) -> None:
            """Stop runtime listeners and release owned sessions on shutdown."""
            if coordinator.websocket is not None:
                coordinator.websocket.stop()
            hass.async_create_task(coordinator.async_shutdown())

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown))

        if coordinator.websocket is not None:
            coordinator.websocket.start()
    except Exception:
        if coordinator.websocket is not None:
            coordinator.websocket.stop()
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if coordinator.websocket is not None:
        await coordinator.websocket.stop_and_wait()
    await coordinator.async_shutdown()

    return unload_ok
