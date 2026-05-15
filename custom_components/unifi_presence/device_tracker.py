"""Device tracker platform for UniFi Presence."""

from __future__ import annotations

from typing import cast

from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UnifiPresenceConfigEntry
from .coordinator import UnifiPresenceCoordinator, UnifiPresenceData
from .helpers import tracker_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: UnifiPresenceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
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

        self._attr_unique_id = tracker_unique_id(coordinator.site_id, mac)
        self._attr_has_entity_name = True

    @property
    def name(self) -> str:
        """Return the display name from coordinator state, falling back to MAC."""
        data = cast(UnifiPresenceData | None, self.coordinator.data)
        if data is None:
            return self._mac

        client = data.clients.get(self._mac)
        return client.name if client else self._mac

    @property
    def unique_id(self) -> str | None:
        """Return the site-scoped ID instead of ScannerEntity's MAC-only ID."""
        return self._attr_unique_id

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected (home)."""
        data = cast(UnifiPresenceData | None, self.coordinator.data)
        if data is None:
            return False

        client = data.clients.get(self._mac)
        return client.is_home if client else False

    @property
    def available(self) -> bool:
        """Return whether the tracked client is currently available."""
        data = cast(UnifiPresenceData | None, self.coordinator.data)
        return super().available and data is not None

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac
