"""Device tracker platform for UniFi Presence."""

from __future__ import annotations

from homeassistant.components.device_tracker import ScannerEntity, SourceType  # type: ignore[attr-defined]
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UnifiPresenceConfigEntry
from .coordinator import UnifiPresenceCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: UnifiPresenceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device tracker entities from a config entry."""
    coordinator = config_entry.runtime_data

    entities = [UnifiPresenceTracker(coordinator, mac) for mac in coordinator.tracked_devices]

    async_add_entities(entities)


class UnifiPresenceTracker(CoordinatorEntity[UnifiPresenceCoordinator], ScannerEntity):
    """Represent a tracked UniFi client as a device tracker entity."""

    _attr_source_type = SourceType.ROUTER

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Enable entities by default.

        ScannerEntity disables entities when no device entry exists, but this
        integration intentionally does not create per-client device entries
        (matching the official HA UniFi integration pattern).
        """
        return True

    def __init__(
        self,
        coordinator: UnifiPresenceCoordinator,
        mac: str,
    ) -> None:
        """Initialize the tracker entity."""
        super().__init__(coordinator)
        self._mac = mac

        self._attr_unique_id = mac
        self._attr_has_entity_name = False

    @property
    def name(self) -> str:
        """Return the display name from coordinator client_info, falling back to MAC."""
        if self.coordinator.data is not None:
            info = self.coordinator.data.client_info.get(self._mac)
            if info:
                return info["name"]
        return self._mac

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected (home)."""
        if self.coordinator.data is None:
            return False  # type: ignore[unreachable]
        return self.coordinator.data.device_states.get(self._mac, False)

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac
