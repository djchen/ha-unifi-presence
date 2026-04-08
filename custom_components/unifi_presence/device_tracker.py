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

        self._attr_unique_id = f"{coordinator.site_id}-{mac}"
        self._attr_has_entity_name = True

    @property
    def name(self) -> str:
        """Return the display name from coordinator client_info, falling back to MAC."""
        data = self.coordinator.data
        if data is None:
            return self._mac

        info = data.client_info.get(self._mac)
        return info["name"] if info else self._mac

    @property
    def unique_id(self) -> str:
        """Return the site-scoped unique ID for this tracker."""
        return str(self._attr_unique_id)

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected (home)."""
        data = self.coordinator.data
        if data is None:
            return False

        return data.device_states.get(self._mac, False)

    @property
    def available(self) -> bool:
        """Return whether the tracked client is currently available."""
        if not super().available:
            return False
        return self._mac not in self.coordinator.data.missing_macs

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac
