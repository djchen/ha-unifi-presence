"""DataUpdateCoordinator for UniFi Presence."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict

import aiounifi
from aiounifi.models.message import Message
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
from .helpers import async_close_controller, create_controller, resolve_controller_site

if TYPE_CHECKING:
    from aiounifi.controller import Controller

    from .websocket import UnifiPresenceWebsocket

_LOGGER = logging.getLogger(__name__)


def _current_epoch_seconds() -> int:
    """Return the current Unix timestamp in seconds."""
    return int(time.time())


def _normalize_tracked_macs(raw_tracked: list[str]) -> tuple[str, ...]:
    """Normalize tracked MACs by trimming blanks, lowercasing, and deduplicating."""
    normalized: list[str] = []
    seen: set[str] = set()

    for mac in raw_tracked:
        normalized_mac = mac.strip().lower()
        if not normalized_mac or normalized_mac in seen:
            continue

        seen.add(normalized_mac)
        normalized.append(normalized_mac)

    return tuple(normalized)


class ClientInfo(TypedDict):
    """Typed dictionary describing a single UniFi client."""

    name: str
    mac: str


class UnifiPresenceData:
    """Container for coordinator data."""

    __slots__ = ("client_info", "device_states")
    __hash__ = None  # type: ignore[assignment]

    def __init__(
        self,
        device_states: dict[str, bool],
        client_info: dict[str, ClientInfo],
    ) -> None:
        """Initialize.

        Args:
            device_states: MAC address -> is_home (True = home, False = not_home).
            client_info: MAC address -> client metadata used by entities.
        """
        self.device_states = device_states
        self.client_info = client_info

    def __eq__(self, other: object) -> bool:
        """Return whether two coordinator payloads are equivalent."""
        if not isinstance(other, UnifiPresenceData):
            return NotImplemented

        return self.device_states == other.device_states and self.client_info == other.client_info


class UnifiPresenceCoordinator(DataUpdateCoordinator[UnifiPresenceData]):
    """Coordinator for UniFi client presence via WebSocket + fallback poll."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self._controller: Controller | None = None
        self.websocket: UnifiPresenceWebsocket | None = None

        # Cache options that only change on reload
        raw_tracked: list[str] = config_entry.options.get(CONF_TRACKED_DEVICES, [])
        self._tracked_macs = _normalize_tracked_macs(raw_tracked)
        self._tracked_set: frozenset[str] = frozenset(self._tracked_macs)
        self._away_seconds: int = config_entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)

        fallback_interval = config_entry.options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=fallback_interval),
            config_entry=config_entry,
            # We return the existing data object when presence state is unchanged,
            # so listener updates can be suppressed on no-op fallback polls.
            always_update=False,
        )

    @property
    def tracked_devices(self) -> tuple[str, ...]:
        """Return the tuple of tracked MAC addresses (pre-lowercased)."""
        return self._tracked_macs

    @property
    def site_id(self) -> str:
        """Return the config entry site identifier used for tracker IDs."""
        unique_id = self.config_entry.unique_id
        if isinstance(unique_id, str) and unique_id:
            return unique_id

        entry_id = self.config_entry.entry_id
        if isinstance(entry_id, str) and entry_id:
            return entry_id

        return DEFAULT_SITE

    @property
    def away_seconds(self) -> int:
        """Return the away threshold in seconds."""
        return self._away_seconds

    @property
    def controller(self) -> Controller | None:
        """Return the cached controller, if available."""
        return self._controller

    async def async_shutdown(self) -> None:
        """Release the current controller session, if owned by this integration."""
        controller = self._controller
        self._controller = None
        if controller is not None:
            await async_close_controller(controller)

    async def _ensure_controller(self) -> Controller:
        """Create or re-authenticate the controller connection."""
        if self._controller is not None:
            return self._controller

        data = self.config_entry.data
        site = await resolve_controller_site(
            self.hass,
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            site=data.get(CONF_SITE, DEFAULT_SITE),
            ssl_verify=data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
            unique_id=self.config_entry.unique_id,
        )
        self._controller = await create_controller(
            self.hass,
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            site=site,
            ssl_verify=data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
        )
        return self._controller

    @staticmethod
    def _build_client_info(
        mac: str,
        *,
        name: str = "",
        hostname: str = "",
    ) -> ClientInfo:
        """Build a normalised client_info dict."""
        return {
            "name": name or hostname or mac,
            "mac": mac,
        }

    def process_message(self, message: Message) -> None:
        """Handle a sta:sync WebSocket message for a tracked client."""
        raw: object = message.data
        if not isinstance(raw, dict):
            return
        mac = raw.get("mac", "").lower()
        if mac not in self._tracked_set:
            return

        now = _current_epoch_seconds()
        last_seen = raw.get("last_seen") or 0
        is_home = (now - last_seen) < self._away_seconds

        current_data = self.data
        previous_info = current_data.client_info.get(mac) if current_data is not None else None
        name = raw.get("name", "")
        hostname = raw.get("hostname", "")
        info = (
            self._build_client_info(mac, name=name, hostname=hostname)
            if name or hostname or previous_info is None
            else previous_info
        )

        if current_data is not None:
            old_home = current_data.device_states.get(mac)
            if old_home == is_home and previous_info == info:
                return

        new_states = dict(current_data.device_states) if current_data is not None else {}
        new_states[mac] = is_home

        new_info = dict(current_data.client_info) if current_data is not None else {}
        new_info[mac] = info

        _LOGGER.debug(
            "Device %s (%s) %s: %s",
            info["name"],
            mac,
            "initial state" if current_data is None else "state changed",
            "home" if is_home else "away",
        )

        self.async_set_updated_data(
            UnifiPresenceData(
                device_states=new_states,
                client_info=new_info,
            )
        )

    def _connect_error(self) -> UpdateFailed:
        """Build an UpdateFailed for connection errors."""
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": self.config_entry.data[CONF_HOST]},
        )

    async def _refresh_clients(self, controller: Controller) -> None:
        """Best-effort refresh of historical clients, then required refresh of active clients.

        The historical store (clients_all) retains cached data from prior
        successful refreshes, so its failure is non-fatal.  The active store
        (clients) is required — its failure propagates.
        """
        try:
            await controller.clients_all.update()
        except Exception:
            _LOGGER.debug("Best-effort clients_all refresh failed; using cached data")

        await controller.clients.update()

    async def _async_update_data(self) -> UnifiPresenceData:
        """Fallback REST poll — fetch data from the UniFi controller."""
        now = _current_epoch_seconds()
        tracked_macs = self._tracked_macs
        away_threshold = self._away_seconds

        try:
            controller = await self._ensure_controller()
            await self._refresh_clients(controller)
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            # Session expired or credentials rejected — force re-auth
            _LOGGER.info("UniFi session expired, re-authenticating")
            await self.async_shutdown()
            try:
                controller = await self._ensure_controller()
                await self._refresh_clients(controller)
            except (aiounifi.LoginRequired, aiounifi.Unauthorized) as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="credentials_rejected",
                ) from err
            except (TimeoutError, aiounifi.AiounifiException) as err:
                raise self._connect_error() from err

            # Controller was replaced — restart websocket so it binds to the new one
            if self.websocket is not None:
                self.websocket.reconnect()
        except (TimeoutError, aiounifi.AiounifiException) as err:
            raise self._connect_error() from err

        _LOGGER.debug("Fallback poll for tracked device(s)")

        # Look up only tracked MACs directly — avoids copying the full client dict
        clients = controller.clients
        clients_all = controller.clients_all
        device_states: dict[str, bool] = {}
        client_info: dict[str, ClientInfo] = {}
        previous_info = self.data.client_info if self.data is not None else {}

        for mac in tracked_macs:
            client = clients.get(mac)

            if client is not None:
                last_seen = client.last_seen or 0
                is_home = (now - last_seen) < away_threshold
                client_info[mac] = self._build_client_info(
                    mac,
                    name=client.name or "",
                    hostname=client.hostname or "",
                )
            else:
                is_home = False
                # Prefer historical metadata from clients_all, then prior
                # coordinator data, then bare MAC as last resort.
                historical = clients_all.get(mac)
                if historical is not None and (historical.name or historical.hostname):
                    client_info[mac] = self._build_client_info(
                        mac,
                        name=historical.name or "",
                        hostname=historical.hostname or "",
                    )
                else:
                    client_info[mac] = previous_info.get(mac, self._build_client_info(mac))

            device_states[mac] = is_home

        new_data = UnifiPresenceData(
            device_states=device_states,
            client_info=client_info,
        )

        # Reuse the existing object only when neither presence nor metadata changed.
        if self.data is not None and new_data == self.data:
            return self.data

        return new_data
