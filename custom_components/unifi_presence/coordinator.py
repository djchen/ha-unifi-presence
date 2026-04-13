"""DataUpdateCoordinator for UniFi Presence."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from json import JSONDecodeError
from typing import TYPE_CHECKING, TypedDict, cast

import aiounifi
from aiounifi.models.message import Message
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_FALLBACK_POLL_INTERVAL,
    DEFAULT_SITE,
    DOMAIN,
)
from .helpers import (
    ClientLike,
    ControllerConnectionParams,
    async_close_controller,
    create_controller,
    normalize_mac,
    normalize_macs,
    resolve_controller_site,
)

if TYPE_CHECKING:
    from aiounifi.controller import Controller

    from .websocket import UnifiPresenceWebsocket

_LOGGER = logging.getLogger(__name__)


class ClientInfo(TypedDict):
    """Typed dictionary describing a single UniFi client."""

    name: str
    mac: str


@dataclass(slots=True)
class UnifiPresenceData:
    """Container for coordinator data."""

    device_states: dict[str, bool]
    client_info: dict[str, ClientInfo]


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

        self._tracked_macs = normalize_macs(config_entry.options.get(CONF_TRACKED_DEVICES, []))
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
            # Suppress entity writes when the snapshot is identical to the
            # previous one.  State transitions come from WebSocket pushes and
            # heartbeat expiry; the fallback poll only needs to write when
            # something actually changed.
            always_update=False,
        )

    @property
    def tracked_devices(self) -> tuple[str, ...]:
        """Return the tuple of tracked MAC addresses."""
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

        Only stores the value when it is newer than any previously recorded
        timestamp. This ensures a stale fallback-poll value cannot overwrite a
        more recent WebSocket observation.
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

        By scheduling a point-in-time callback the event loop is woken
        precisely when the first device should transition to ``not_home``,
        avoiding unnecessary periodic polling.
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
            mac: Normalized MAC address of the client.
            last_seen: Unix timestamp from a WebSocket message or REST poll,
                or ``None`` when no fresh timestamp is available (offline
                client during a poll).

        Returns:
            ``True`` if the client is considered home, ``False`` otherwise.
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

    @staticmethod
    def _build_client_info(
        mac: str,
        *,
        name: str = "",
        hostname: str = "",
    ) -> ClientInfo:
        """Build a normalized client_info dict."""
        return {"name": name or hostname or mac, "mac": mac}

    @callback
    def _get_current_info(self, mac: str) -> ClientInfo:
        """Return the current client info for a device, falling back to bare MAC."""
        current_data = self.data
        if current_data is not None and (info := current_data.client_info.get(mac)) is not None:
            return info

        return self._build_client_info(mac)

    @staticmethod
    def _client_name_parts(client: ClientLike | None) -> tuple[str, str]:
        """Return normalized client name and hostname values."""
        if client is None:
            return "", ""

        return str(client.name or ""), str(client.hostname or "")

    def _build_client_info_from_client(
        self,
        mac: str,
        client: ClientLike | None,
        *,
        previous_info: ClientInfo | None = None,
    ) -> ClientInfo:
        """Build client metadata from live or historical UniFi data."""
        name, hostname = self._client_name_parts(client)
        if name or hostname or previous_info is None:
            return self._build_client_info(mac, name=name, hostname=hostname)

        return previous_info

    def _resolve_offline_client_info(
        self,
        mac: str,
        previous_info: dict[str, ClientInfo],
        clients_all: dict[str, ClientLike],
    ) -> ClientInfo:
        """Resolve display metadata for an offline client."""
        historical = clients_all.get(mac)
        if historical is not None:
            name, hostname = self._client_name_parts(historical)
            if name or hostname:
                return self._build_client_info(mac, name=name, hostname=hostname)

        prior = previous_info.get(mac)
        if prior is not None:
            return prior

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

        self.async_set_updated_data(UnifiPresenceData(device_states=new_states, client_info=new_info))

    @callback
    def _publish_local_state_change(self, new_data: UnifiPresenceData) -> None:
        """Publish local state changes without altering refresh success bookkeeping.

        Unlike ``async_set_updated_data()``, this does not reset the refresh
        timer or flip ``last_update_success`` to ``True``. That distinction
        matters for heartbeat-only transitions: a device going ``not_home``
        due to expiry should not look like a successful REST poll.
        """
        self.data = new_data
        self.async_update_listeners()

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

        self._publish_local_state_change(UnifiPresenceData(device_states=new_states, client_info=new_info))
        self._reschedule_heartbeat_check()

    async def _ensure_controller(self) -> Controller:
        """Create or re-authenticate the controller connection."""
        if self._controller is not None:
            return self._controller

        data = self.config_entry.data
        params = ControllerConnectionParams.from_mapping(data)
        resolved_site = await resolve_controller_site(
            self.hass,
            params,
            unique_id=self.config_entry.unique_id,
        )
        params = replace(params, site=resolved_site)
        self._controller = await create_controller(
            self.hass,
            params,
        )
        return self._controller

    def process_message(self, message: Message) -> None:
        """Handle a sta:sync WebSocket message for a tracked client."""
        raw: object = message.data
        if not isinstance(raw, dict):
            return

        mac_raw = raw.get("mac")
        if not isinstance(mac_raw, str):
            return

        mac = normalize_mac(mac_raw)
        if mac not in self._tracked_set:
            return

        last_seen_raw = raw.get("last_seen")
        if last_seen_raw is None:
            last_seen: int | float | None = None
        elif isinstance(last_seen_raw, bool) or not isinstance(last_seen_raw, (int, float)):
            return
        else:
            last_seen = last_seen_raw

        current_data = self.data
        previous_info = current_data.client_info.get(mac) if current_data is not None else None
        info = self._build_client_info_from_client(
            mac,
            cast(ClientLike, _MessageClientView(raw)),
            previous_info=previous_info,
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
        """Best-effort refresh of historical clients, then required refresh of active clients."""
        try:
            await controller.clients_all.update()
        except Exception:
            _LOGGER.debug("Best-effort clients_all refresh failed; using cached data")

        await controller.clients.update()

    async def _async_refresh_with_reauth(self) -> Controller:
        """Refresh client stores, re-authenticating once on session expiry."""
        try:
            controller = await self._ensure_controller()
            await self._refresh_clients(controller)
            return controller
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
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

            if self.websocket is not None:
                self.websocket.restart_with_current_controller()

            return controller
        except (TimeoutError, aiounifi.AiounifiException, JSONDecodeError) as err:
            raise self._connect_error() from err

    def _build_snapshot(
        self,
        controller: Controller,
    ) -> UnifiPresenceData:
        """Build the current coordinator snapshot from controller client stores."""
        clients = cast(dict[str, ClientLike], controller.clients)
        clients_all = cast(dict[str, ClientLike], controller.clients_all)
        device_states: dict[str, bool] = {}
        client_info: dict[str, ClientInfo] = {}
        previous_info = self.data.client_info if self.data is not None else {}

        for mac in self._tracked_macs:
            client = clients.get(mac)
            if client is not None:
                last_seen = client.last_seen or None
                is_home = self._apply_presence_observation(mac, last_seen=last_seen)
                client_info[mac] = self._build_client_info_from_client(mac, client)
            else:
                is_home = self._apply_presence_observation(mac, last_seen=None)
                client_info[mac] = self._resolve_offline_client_info(mac, previous_info, clients_all)

            device_states[mac] = is_home

        return UnifiPresenceData(device_states=device_states, client_info=client_info)

    def _reuse_existing_snapshot_if_unchanged(self, new_data: UnifiPresenceData) -> UnifiPresenceData:
        """Return the existing data object when neither state nor metadata changed."""
        if self.data is not None and new_data == self.data:
            return self.data

        return new_data

    async def _async_update_data(self) -> UnifiPresenceData:
        """Fallback REST poll — fetch data from the UniFi controller."""
        controller = await self._async_refresh_with_reauth()
        _LOGGER.debug("Fallback poll for tracked device(s)")

        new_data = self._build_snapshot(controller)
        self._reschedule_heartbeat_check()
        return self._reuse_existing_snapshot_if_unchanged(new_data)


class _MessageClientView:
    """Adapter exposing message payload fields through the ClientLike protocol."""

    def __init__(self, raw: dict[str, object]) -> None:
        """Initialize the message view."""
        name_raw = raw.get("name")
        hostname_raw = raw.get("hostname")
        self.name = name_raw if isinstance(name_raw, str) else None
        self.hostname = hostname_raw if isinstance(hostname_raw, str) else None
        self.last_seen = None
