"""DataUpdateCoordinator for UniFi Presence."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiounifi
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_SITE,
    CONF_SSL_VERIFY,
    CONF_TRACKED_DEVICES,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_FALLBACK_POLL_INTERVAL,
    DEFAULT_SITE,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
)
from .helpers import create_controller

if TYPE_CHECKING:
    from .websocket import UnifiPresenceWebsocket

_LOGGER = logging.getLogger(__name__)


class UnifiPresenceData:
    """Container for coordinator data."""

    __slots__ = ("client_info", "device_states")

    def __init__(
        self,
        device_states: dict[str, bool],
        client_info: dict[str, dict[str, Any]],
    ) -> None:
        """Initialize.

        Args:
            device_states: MAC address -> is_home (True = home, False = not_home).
            client_info: MAC address -> extra client attributes (name, hostname, ip, etc.).
        """
        self.device_states = device_states
        self.client_info = client_info


class UnifiPresenceCoordinator(DataUpdateCoordinator[UnifiPresenceData]):
    """Coordinator for UniFi client presence via WebSocket + fallback poll."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self._controller: aiounifi.Controller | None = None
        self.websocket: UnifiPresenceWebsocket | None = None

        # Cache options that only change on reload
        raw_tracked: list[str] = config_entry.options.get(CONF_TRACKED_DEVICES, [])
        self._tracked_macs: tuple[str, ...] = tuple(m.lower() for m in raw_tracked)
        self._tracked_set: frozenset[str] = frozenset(self._tracked_macs)
        self._away_seconds: int = config_entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)

        fallback_interval = config_entry.options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=fallback_interval),
            config_entry=config_entry,
        )

    @property
    def signal_reachable(self) -> str:
        """Return the dispatcher signal for WebSocket reachability changes."""
        return f"{DOMAIN}-reachable-{self.config_entry.entry_id}"

    @property
    def tracked_devices(self) -> tuple[str, ...]:
        """Return the tuple of tracked MAC addresses (pre-lowercased)."""
        return self._tracked_macs

    @property
    def away_seconds(self) -> int:
        """Return the away threshold in seconds."""
        return self._away_seconds

    @property
    def controller(self) -> aiounifi.Controller | None:
        """Return the cached controller, if available."""
        return self._controller

    async def _ensure_controller(self) -> aiounifi.Controller:
        """Create or re-authenticate the controller connection."""
        if self._controller is not None:
            return self._controller

        data = self.config_entry.data
        self._controller = await create_controller(
            self.hass,
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            site=data.get(CONF_SITE, DEFAULT_SITE),
            ssl_verify=data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
        )
        return self._controller

    @staticmethod
    def _build_client_info(
        mac: str,
        *,
        name: str = "",
        hostname: str = "",
        ip: str = "",
        is_wired: bool = False,
        last_seen: int = 0,
    ) -> dict[str, Any]:
        """Build a normalised client_info dict."""
        return {
            "name": name or hostname or mac,
            "hostname": hostname or "",
            "ip": ip or "",
            "mac": mac,
            "is_wired": is_wired,
            "last_seen": last_seen or 0,
        }

    def process_message(self, message: Any) -> None:
        """Handle a sta:sync WebSocket message for a tracked client."""
        raw = message.data
        mac = raw.get("mac", "").lower()
        if mac not in self._tracked_set:
            return

        now = int(time.time())
        last_seen = raw.get("last_seen") or 0
        is_home = (now - last_seen) < self._away_seconds

        # Check if state actually changed
        if self.data is not None:
            old_home = self.data.device_states.get(mac)
            if old_home == is_home:
                # No state change — silently update client_info for freshness
                self.data.client_info[mac] = self._build_client_info(
                    mac,
                    name=raw.get("name", ""),
                    hostname=raw.get("hostname", ""),
                    ip=raw.get("ip", ""),
                    is_wired=raw.get("is_wired", False),
                    last_seen=last_seen,
                )
                return

        # State changed — rebuild and push
        new_states = dict(self.data.device_states) if self.data else {}
        new_states[mac] = is_home

        new_info = dict(self.data.client_info) if self.data else {}
        new_info[mac] = self._build_client_info(
            mac,
            name=raw.get("name", ""),
            hostname=raw.get("hostname", ""),
            ip=raw.get("ip", ""),
            is_wired=raw.get("is_wired", False),
            last_seen=last_seen,
        )

        _LOGGER.debug(
            "Device %s (%s) %s: %s",
            new_info[mac]["name"],
            mac,
            "initial state" if self.data is None else "state changed",
            "home" if is_home else "away",
        )

        self.async_set_updated_data(UnifiPresenceData(device_states=new_states, client_info=new_info))

    async def _async_update_data(self) -> UnifiPresenceData:
        """Fallback REST poll — fetch data from the UniFi controller."""
        now = int(time.time())
        tracked_macs = self._tracked_macs
        away_threshold = self._away_seconds

        try:
            controller = await self._ensure_controller()
            await controller.clients.update()
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            # Session expired or credentials rejected — force re-auth
            _LOGGER.info("UniFi session expired, re-authenticating")
            self._controller = None
            try:
                controller = await self._ensure_controller()
                await controller.clients.update()
            except (aiounifi.LoginRequired, aiounifi.Unauthorized) as err:
                raise ConfigEntryAuthFailed(f"Credentials rejected by UniFi controller: {err}") from err
            except aiounifi.AiounifiException as err:
                raise UpdateFailed(f"Could not fetch clients after re-auth: {err}") from err
        except aiounifi.AiounifiException as err:
            raise UpdateFailed(f"Error communicating with UniFi controller: {err}") from err

        _LOGGER.debug("Fallback poll for tracked device(s)")

        # Look up only tracked MACs directly — avoids copying the full client dict
        clients = controller.clients
        device_states: dict[str, bool] = {}
        client_info: dict[str, dict[str, Any]] = {}

        for mac in tracked_macs:
            client = clients.get(mac)

            if client is not None:
                last_seen = client.last_seen or 0
                is_home = (now - last_seen) < away_threshold
                client_info[mac] = self._build_client_info(
                    mac,
                    name=client.name or "",
                    hostname=client.hostname or "",
                    ip=client.ip or "",
                    is_wired=client.is_wired,
                    last_seen=last_seen,
                )
            else:
                is_home = False
                client_info[mac] = self._build_client_info(mac)

            device_states[mac] = is_home

        new_data = UnifiPresenceData(
            device_states=device_states,
            client_info=client_info,
        )

        # If device states haven't changed, keep the existing data object to
        # avoid unnecessary entity writes and just refresh client_info in-place.
        if self.data is not None and new_data.device_states == self.data.device_states:
            self.data.client_info.update(client_info)
            return self.data

        return new_data
