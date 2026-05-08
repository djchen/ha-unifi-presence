"""The UniFi Presence integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_FALLBACK_POLL_INTERVAL,
)
from .coordinator import UnifiPresenceCoordinator
from .helpers import normalize_macs, tracker_unique_id
from .websocket import UnifiPresenceWebsocket

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


def _is_hass_stopping(hass: HomeAssistant) -> bool:
    """Return whether Home Assistant is stopping."""
    return hass.is_stopping


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
    """Clean up deselected entities and reload after options changes."""
    coordinator = entry.runtime_data
    tracked_devices = normalize_macs(entry.options.get(CONF_TRACKED_DEVICES, []))
    removed_macs = set(normalize_macs(coordinator.tracked_devices)) - set(tracked_devices)
    _async_remove_deselected_entities(hass, entry, removed_macs)

    fallback_interval = entry.options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL)
    runtime_interval = (
        int(coordinator.update_interval.total_seconds())
        if coordinator.update_interval is not None
        else DEFAULT_FALLBACK_POLL_INTERVAL
    )
    if (
        tracked_devices == coordinator.tracked_devices
        and entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS) == coordinator.away_seconds
        and fallback_interval == runtime_interval
    ):
        return

    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    shutdown_unsub: CALLBACK_TYPE | None = None
    shutdown_handled = False
    try:
        await coordinator.async_config_entry_first_refresh()

        # Start WebSocket for real-time presence updates
        coordinator.websocket = UnifiPresenceWebsocket(
            hass,
            lambda: coordinator.controller,
            coordinator.process_message,
        )

        entry.runtime_data = coordinator

        if _is_hass_stopping(hass):
            coordinator.websocket.stop()
            await coordinator.async_shutdown()
            return False

        @callback
        def _async_shutdown(_event: object) -> None:
            """Stop runtime listeners and release owned sessions on shutdown."""
            nonlocal shutdown_handled
            shutdown_handled = True
            if coordinator.websocket is not None:
                coordinator.websocket.stop()
            hass.async_create_task(coordinator.async_shutdown())

        shutdown_unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if _is_hass_stopping(hass):
            if not shutdown_handled:
                shutdown_unsub()
                if coordinator.websocket is not None:
                    coordinator.websocket.stop()
                await coordinator.async_shutdown()
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            return False

        entry.async_on_unload(shutdown_unsub)
        entry.async_on_unload(entry.add_update_listener(_async_handle_entry_update))
        coordinator.websocket.start()
    except Exception:
        if shutdown_unsub is not None and not shutdown_handled:
            shutdown_unsub()
        if not shutdown_handled:
            if coordinator.websocket is not None:
                coordinator.websocket.stop()
            await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    if coordinator.websocket is not None:
        await coordinator.websocket.stop_and_wait()

    return True
