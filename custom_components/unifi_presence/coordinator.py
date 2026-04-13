"""DataUpdateCoordinator for UniFi Presence."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, TypedDict

import aiounifi
from aiounifi.models.message import Message
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    _heartbeat_expiry: dict[str, datetime]
    _last_seen_by_mac: dict[str, int]
    _cancel_heartbeat_check: CALLBACK_TYPE | None

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self._controller: Controller | None = None
        self.websocket: UnifiPresenceWebsocket | None = None

        # Cache options that only change on reload
        raw_tracked: list[str] = config_entry.options.get(CONF_TRACKED_DEVICES, [])
        self._tracked_macs = _normalize_tracked_macs(raw_tracked)
        self._tracked_set: frozenset[str] = frozenset(self._tracked_macs)
        self._away_seconds: int = config_entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)
        self._last_seen_by_mac = {}
        self._heartbeat_expiry = {}
        self._cancel_heartbeat_check = None

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
        if self._cancel_heartbeat_check is not None:
            self._cancel_heartbeat_check()
            self._cancel_heartbeat_check = None

        self._heartbeat_expiry.clear()
        self._last_seen_by_mac.clear()

        await self._async_close_runtime_controller()
        await super().async_shutdown()

    async def _async_close_runtime_controller(self) -> None:
        """Release the current controller session without stopping heartbeat tracking."""
        controller = self._controller
        self._controller = None
        if controller is not None:
            await async_close_controller(controller)

    @property
    def heartbeat_expiry_count(self) -> int:
        """Return the number of tracked heartbeat expiries."""
        return len(self._heartbeat_expiry)

    @callback
    def _set_last_seen(self, mac: str, last_seen: int | float) -> int:
        """Store the newest known last_seen for a client and return it.

        Only updates the cache when the incoming timestamp is strictly newer,
        so stale fallback-poll values and out-of-order WebSocket frames cannot
        move the clock backwards.
        """
        normalized = int(last_seen)
        existing = self._last_seen_by_mac.get(mac)
        if existing is not None and existing >= normalized:
            return existing
        self._last_seen_by_mac[mac] = normalized
        return normalized

    @callback
    def _get_known_last_seen(self, mac: str) -> int | None:
        """Return the newest known last_seen for a client."""
        return self._last_seen_by_mac.get(mac)

    @callback
    def _clear_heartbeat(self, mac: str) -> None:
        """Forget any pending heartbeat expiry for a client."""
        self._heartbeat_expiry.pop(mac, None)

    @callback
    def _reschedule_heartbeat_check(self) -> None:
        """Schedule the next heartbeat check at the earliest pending expiry.

        The event loop only wakes when a tracked device is actually due
        to expire, rather than polling on a fixed interval.
        """
        if self._cancel_heartbeat_check is not None:
            self._cancel_heartbeat_check()
            self._cancel_heartbeat_check = None

        if not self._heartbeat_expiry:
            return

        earliest = min(self._heartbeat_expiry.values())
        self._cancel_heartbeat_check = async_track_point_in_utc_time(
            self.hass,
            self._async_check_heartbeat_expiry,
            earliest,
        )

    @callback
    def _compute_presence_from_last_seen(self, last_seen: int | float) -> tuple[bool, datetime | None]:
        """Return current presence and optional expiry from a last_seen timestamp."""
        normalized_last_seen = int(last_seen)
        now = dt_util.utcnow()
        last_seen_at = dt_util.utc_from_timestamp(normalized_last_seen)
        expiry = last_seen_at + timedelta(seconds=self._away_seconds)
        if now >= expiry:
            return False, None

        return True, expiry

    @callback
    def _apply_presence_observation(
        self,
        mac: str,
        *,
        last_seen: int | float | None,
    ) -> bool:
        """Apply an observed presence update and maintain heartbeat expiry.

        Args:
            mac: Tracked client MAC.
            last_seen: Latest trustworthy timestamp, if any.

        Returns:
            Whether the client should currently be considered home.
        """
        effective_last_seen = (
            self._set_last_seen(mac, last_seen) if last_seen is not None else self._get_known_last_seen(mac)
        )

        if effective_last_seen is None:
            self._clear_heartbeat(mac)
            return False

        is_home, expiry = self._compute_presence_from_last_seen(effective_last_seen)
        if is_home and expiry is not None:
            self._heartbeat_expiry[mac] = expiry
        else:
            self._clear_heartbeat(mac)
        return is_home

    @callback
    def _get_current_info(self, mac: str) -> ClientInfo:
        """Return the current client info for a device, falling back to bare MAC."""
        current_data = self.data
        if current_data is not None and (info := current_data.client_info.get(mac)) is not None:
            return info

        return self._build_client_info(mac)

    @callback
    def _update_single_device_state(self, mac: str, is_home: bool, info: ClientInfo) -> None:
        """Publish a single-device state update when state or metadata changes."""
        current_data = self.data
        previous_info = current_data.client_info.get(mac) if current_data is not None else None
        previous_state = current_data.device_states.get(mac) if current_data is not None else None

        if previous_state == is_home and previous_info == info:
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

    @callback
    def _async_check_heartbeat_expiry(self, *_: datetime) -> None:
        """Expire tracked clients whose away deadline has elapsed."""
        if self.data is None or not self._heartbeat_expiry:
            return

        now = dt_util.utcnow()
        current_states = self.data.device_states
        current_info = self.data.client_info
        changed_macs: list[str] = []

        for mac, expiry in tuple(self._heartbeat_expiry.items()):
            if now < expiry:
                continue

            effective_last_seen = self._get_known_last_seen(mac)
            if effective_last_seen is not None:
                is_home, refreshed_expiry = self._compute_presence_from_last_seen(effective_last_seen)
                if is_home and refreshed_expiry is not None:
                    self._heartbeat_expiry[mac] = refreshed_expiry
                    continue

            self._heartbeat_expiry.pop(mac, None)
            if current_states.get(mac, False):
                changed_macs.append(mac)

        if not changed_macs:
            self._reschedule_heartbeat_check()
            return

        new_states = dict(current_states)
        new_info = dict(current_info)
        for mac in changed_macs:
            new_states[mac] = False
            new_info.setdefault(mac, self._get_current_info(mac))

        # Update data and notify listeners directly instead of calling
        # async_set_updated_data(), which would set last_update_success=True
        # (masking a real controller failure) and reset the refresh timer
        # (delaying the REST fallback recovery path).
        self.data = UnifiPresenceData(
            device_states=new_states,
            client_info=new_info,
        )
        self.async_update_listeners()
        self._reschedule_heartbeat_check()

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

    def _resolve_offline_client_info(
        self,
        mac: str,
        previous_info: dict[str, ClientInfo],
        clients_all: Any,
    ) -> ClientInfo:
        """Resolve display metadata for an offline client.

        Priority order: historical metadata from ``clients_all`` (so that
        renames made in UniFi while a device is offline propagate on the next
        fallback poll), then prior coordinator data (avoids a bare-MAC
        regression when clients_all has no record for the device), then the
        bare MAC address as a last resort.
        """
        historical = clients_all.get(mac)
        if historical is not None and (historical.name or historical.hostname):
            return self._build_client_info(
                mac,
                name=historical.name or "",
                hostname=historical.hostname or "",
            )

        prior = previous_info.get(mac)
        if prior is not None:
            return prior

        return self._build_client_info(mac)

    def process_message(self, message: Message) -> None:
        """Handle a sta:sync WebSocket message for a tracked client."""
        raw: object = message.data
        if not isinstance(raw, dict):
            return

        mac_raw = raw.get("mac")
        if not isinstance(mac_raw, str):
            return

        mac = mac_raw.lower()
        if mac not in self._tracked_set:
            return

        last_seen_raw = raw.get("last_seen")
        if last_seen_raw is None:
            # No timestamp in the payload — fall back to the cached value in
            # _apply_presence_observation rather than treating a bare WS event
            # as proof of current activity.
            last_seen: int | float | None = None
        elif isinstance(last_seen_raw, bool) or not isinstance(last_seen_raw, (int, float)):
            return
        else:
            last_seen = last_seen_raw

        current_data = self.data
        previous_info = current_data.client_info.get(mac) if current_data is not None else None
        name_raw = raw.get("name")
        name = name_raw if isinstance(name_raw, str) else ""
        hostname_raw = raw.get("hostname")
        hostname = hostname_raw if isinstance(hostname_raw, str) else ""
        info = (
            self._build_client_info(mac, name=name, hostname=hostname)
            if name or hostname or previous_info is None
            else previous_info
        )

        is_home = self._apply_presence_observation(mac, last_seen=last_seen)
        self._update_single_device_state(mac, is_home, info)
        self._reschedule_heartbeat_check()

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
        tracked_macs = self._tracked_macs

        try:
            controller = await self._ensure_controller()
            await self._refresh_clients(controller)
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            # Session expired or credentials rejected — force re-auth
            _LOGGER.info("UniFi session expired, re-authenticating")
            await self._async_close_runtime_controller()
            try:
                controller = await self._ensure_controller()
                await self._refresh_clients(controller)
            except (aiounifi.LoginRequired, aiounifi.Unauthorized) as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="credentials_rejected",
                ) from err
            except (TimeoutError, aiounifi.AiounifiException, JSONDecodeError) as err:
                raise self._connect_error() from err

            # Controller was replaced — restart websocket so it binds to the new one
            if self.websocket is not None:
                self.websocket.reconnect()
        except (TimeoutError, aiounifi.AiounifiException, JSONDecodeError) as err:
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
                last_seen = client.last_seen or None
                is_home = self._apply_presence_observation(mac, last_seen=last_seen)
                client_info[mac] = self._build_client_info(
                    mac,
                    name=client.name or "",
                    hostname=client.hostname or "",
                )
            else:
                is_home = self._apply_presence_observation(mac, last_seen=None)
                client_info[mac] = self._resolve_offline_client_info(mac, previous_info, clients_all)

            device_states[mac] = is_home

        self._reschedule_heartbeat_check()

        new_data = UnifiPresenceData(
            device_states=device_states,
            client_info=client_info,
        )

        # Reuse the existing object only when neither presence nor metadata changed.
        if self.data is not None and new_data == self.data:
            return self.data

        return new_data
