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

    @callback
    def _unsubscribe_shutdown() -> None:
        """Remove the stop listener only if it has not already fired."""
        nonlocal shutdown_unsub
        if shutdown_unsub is not None:
            shutdown_unsub()
            shutdown_unsub = None

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
            await coordinator.async_shutdown()
            return False

        async def _async_shutdown(_event: object) -> None:
            """Stop runtime listeners and release owned sessions on shutdown."""
            nonlocal shutdown_unsub
            shutdown_unsub = None  # The one-shot listener has removed itself.
            await coordinator.async_shutdown()

        shutdown_unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)
        entry.async_on_unload(_unsubscribe_shutdown)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if bool(hass.is_stopping):
            _unsubscribe_shutdown()
            await coordinator.async_shutdown()
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            return False

        websocket.start()
    except Exception:
        _unsubscribe_shutdown()
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    await coordinator.async_shutdown()

    return True
