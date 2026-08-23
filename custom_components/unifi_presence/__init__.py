"""The UniFi Presence integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .coordinator import UnifiPresenceCoordinator
from .websocket import UnifiPresenceWebsocket

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    websocket: UnifiPresenceWebsocket | None = None
    shutdown_unsub: CALLBACK_TYPE | None = None
    shutdown_handled = False
    try:
        await coordinator.async_config_entry_first_refresh()

        # Create WebSocket for real-time presence updates
        websocket = UnifiPresenceWebsocket(
            hass,
            lambda: coordinator.controller,
            coordinator.process_message,
        )
        coordinator.websocket = websocket

        entry.runtime_data = coordinator

        if bool(hass.is_stopping):
            websocket.stop()
            await coordinator.async_shutdown()
            return False

        @callback
        def _async_shutdown(_event: object) -> None:
            """Stop runtime listeners and release owned sessions on shutdown."""
            nonlocal shutdown_handled
            shutdown_handled = True
            websocket.stop()
            hass.async_create_task(coordinator.async_shutdown())

        shutdown_unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if bool(hass.is_stopping):
            if not shutdown_handled:
                shutdown_unsub()
                websocket.stop()
                await coordinator.async_shutdown()
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            return False

        entry.async_on_unload(shutdown_unsub)
        websocket.start()
    except Exception:
        if shutdown_unsub is not None and not shutdown_handled:
            shutdown_unsub()
        if not shutdown_handled:
            if websocket is not None:
                websocket.stop()
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

    await coordinator.async_shutdown()

    return True
