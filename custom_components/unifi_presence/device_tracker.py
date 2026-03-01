"""Device tracker platform for UniFi Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UnifiPresenceConfigEntry
from .coordinator import UnifiPresenceCoordinator


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

    def __init__(
        self,
        coordinator: UnifiPresenceCoordinator,
        mac: str,
    ) -> None:
        """Initialize the tracker entity."""
        super().__init__(coordinator)
        self._mac = mac

    @property
    def _client_info(self) -> dict[str, Any] | None:
        """Return the client info dict for this MAC, or None."""
        data = self.coordinator.data
        if data is None:
            return None
        return data.client_info.get(self._mac)

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        info = self._client_info
        if info is not None:
            name = info.get("name", self._mac)
            if name != self._mac:
                return name
        return self._mac

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected (home)."""
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.device_states.get(self._mac, False)

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac

    @property
    def ip_address(self) -> str | None:
        """Return the IP address of the device."""
        info = self._client_info
        if info is not None:
            ip = info.get("ip", "")
            return ip if ip else None
        return None

    @property
    def hostname(self) -> str | None:
        """Return the hostname of the device."""
        info = self._client_info
        if info is not None:
            hostname = info.get("hostname", "")
            return hostname if hostname else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool] | None:
        """Return extra state attributes."""
        info = self._client_info
        if info is not None:
            return {
                "is_wired": info.get("is_wired", False),
                "last_seen": info.get("last_seen", 0),
            }
        return None
