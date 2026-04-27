"""DataUpdateCoordinator for UniFi Presence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from json import JSONDecodeError
from typing import TYPE_CHECKING, TypedDict, cast

import aiohttp
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
    create_controller_with_resolved_site,
    normalize_mac,
    normalize_macs,
    should_resolve_controller_site,
)

if TYPE_CHECKING:
    from aiounifi.controller import Controller

    from .websocket import UnifiPresenceWebsocket

_LOGGER = logging.getLogger(__name__)


class ClientInfo(TypedDict):
    """Typed dictionary describing a single UniFi client."""

    name: str


@dataclass(slots=True)
class TrackedClientState:
    """Unified runtime state for a tracked UniFi client."""

    is_home: bool
    name: str
    last_seen_ts: int | None
    expiry_ts: int | None


@dataclass(eq=False, slots=True)
class UnifiPresenceData:
    """Container for coordinator data."""

    __hash__ = object.__hash__

    clients: dict[str, TrackedClientState]

    def __eq__(self, other: object) -> bool:
        """Compare only externally visible state and metadata.

        Heartbeat bookkeeping timestamps change frequently and should not cause
        fallback polls to publish redundant entity updates.
        """
        if not isinstance(other, UnifiPresenceData):
            return NotImplemented

        if self.clients.keys() != other.clients.keys():
            return False

        return all(
            state.is_home == other.clients[mac].is_home and state.name == other.clients[mac].name
            for mac, state in self.clients.items()
        )

    @property
    def device_states(self) -> dict[str, bool]:
        """Return per-device presence state for diagnostics and tests."""
        return {mac: client.is_home for mac, client in self.clients.items()}

    @property
    def client_info(self) -> dict[str, ClientInfo]:
        """Return per-device metadata for diagnostics and tests."""
        return {mac: {"name": client.name} for mac, client in self.clients.items()}


class UnifiPresenceCoordinator(DataUpdateCoordinator[UnifiPresenceData]):
    """Coordinator for UniFi client presence via WebSocket + fallback poll."""

    config_entry: ConfigEntry
    _client_states: dict[str, TrackedClientState]
    _cancel_heartbeat_check: CALLBACK_TYPE | None
    _scheduled_heartbeat_ts: int | None

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self._controller: Controller | None = None
        self.websocket: UnifiPresenceWebsocket | None = None

        self._tracked_macs = normalize_macs(config_entry.options.get(CONF_TRACKED_DEVICES, []))
        self._tracked_set: frozenset[str] = frozenset(self._tracked_macs)
        self._away_seconds: int = config_entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)
        self._client_states = {}
        self._cancel_heartbeat_check = None
        self._scheduled_heartbeat_ts = None

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
        self._scheduled_heartbeat_ts = None

        self._client_states.clear()

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
        return sum(1 for state in self._client_states.values() if state.expiry_ts is not None)

    @callback
    def _set_last_seen(self, mac: str, last_seen: int | float) -> int:
        """Store the newest known last_seen for a client and return it.

        Only stores the value when it is newer than any previously recorded
        timestamp. This ensures a stale fallback-poll value cannot overwrite a
        more recent WebSocket observation.
        """
        normalized = int(last_seen)
        existing = self._client_states.get(mac)
        if existing is not None and existing.last_seen_ts is not None and existing.last_seen_ts >= normalized:
            return existing.last_seen_ts

        if existing is None:
            self._client_states[mac] = TrackedClientState(
                is_home=False,
                name=mac,
                last_seen_ts=normalized,
                expiry_ts=None,
            )
        else:
            existing.last_seen_ts = normalized

        return normalized

    @callback
    def _get_known_last_seen(self, mac: str) -> int | None:
        """Return the newest known last_seen for a client."""
        state = self._client_states.get(mac)
        return state.last_seen_ts if state is not None else None

    @callback
    def _reschedule_heartbeat_check(self) -> None:
        """Schedule the next heartbeat check at the earliest pending expiry.

        By scheduling a point-in-time callback the event loop is woken
        precisely when the first device should transition to ``not_home``,
        avoiding unnecessary periodic polling.
        """
        earliest: int | None = None
        for state in self._client_states.values():
            expiry_ts = state.expiry_ts
            if expiry_ts is None:
                continue
            if earliest is None or expiry_ts < earliest:
                earliest = expiry_ts

        if earliest is None:
            if self._cancel_heartbeat_check is not None:
                self._cancel_heartbeat_check()
                self._cancel_heartbeat_check = None
            self._scheduled_heartbeat_ts = None
            return

        if self._scheduled_heartbeat_ts == earliest and self._cancel_heartbeat_check is not None:
            return

        if self._cancel_heartbeat_check is not None:
            self._cancel_heartbeat_check()

        self._scheduled_heartbeat_ts = earliest
        self._cancel_heartbeat_check = async_track_point_in_utc_time(
            self.hass,
            self._async_check_heartbeat_expiry,
            dt_util.utc_from_timestamp(earliest),
        )

    @callback
    def _compute_presence_from_last_seen(self, last_seen: int | float) -> tuple[bool, int | None]:
        """Return current presence and optional expiry from a last_seen timestamp."""
        normalized_last_seen = int(last_seen)
        now_ts = int(dt_util.utcnow().timestamp())
        expiry_ts = normalized_last_seen + self._away_seconds
        if now_ts >= expiry_ts:
            return False, None

        return True, expiry_ts

    @callback
    def _apply_presence_observation(
        self,
        mac: str,
        *,
        last_seen: int | float | None,
    ) -> tuple[bool, int | None, int | None]:
        """Apply an observed presence update and maintain heartbeat expiry.

        Args:
            mac: Normalized MAC address of the client.
            last_seen: Unix timestamp from a WebSocket message or REST poll,
                or ``None`` when no fresh timestamp is available (offline
                client during a poll).

        Returns:
            Presence, effective last_seen, and optional expiry timestamp.
        """
        effective_last_seen = (
            self._set_last_seen(mac, last_seen) if last_seen is not None else self._get_known_last_seen(mac)
        )
        if effective_last_seen is None:
            return False, None, None

        is_home, expiry_ts = self._compute_presence_from_last_seen(effective_last_seen)
        return is_home, effective_last_seen, expiry_ts if is_home else None

    @staticmethod
    def _build_client_info(
        mac: str,
        *,
        name: str = "",
        hostname: str = "",
    ) -> ClientInfo:
        """Build a normalized client_info dict."""
        return {"name": name or hostname or mac}

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
    ) -> str:
        """Build a display name from live UniFi data and cached metadata."""
        name, hostname = self._client_name_parts(client)
        if name or hostname or previous_info is None:
            return self._build_client_info(mac, name=name, hostname=hostname)["name"]

        return previous_info["name"]

    def _resolve_offline_client_name(
        self,
        mac: str,
        previous_info: dict[str, ClientInfo],
        clients_all: dict[str, ClientLike],
    ) -> str:
        """Resolve display metadata for an offline client."""
        historical = clients_all.get(mac)
        if historical is not None:
            name, hostname = self._client_name_parts(historical)
            if name or hostname:
                return self._build_client_info(mac, name=name, hostname=hostname)["name"]

        prior = previous_info.get(mac)
        if prior is not None:
            return prior["name"]

        return self._build_client_info(mac)["name"]

    @callback
    def _update_single_device_state(
        self,
        mac: str,
        *,
        is_home: bool,
        name: str,
        last_seen_ts: int | None,
        expiry_ts: int | None,
    ) -> None:
        """Publish a single-device state update when state or metadata changes."""
        previous = self._client_states.get(mac)
        current_data = cast(UnifiPresenceData | None, self.data)
        recovered = not self.last_update_success
        updated_state = TrackedClientState(
            is_home=is_home,
            name=name,
            last_seen_ts=last_seen_ts,
            expiry_ts=expiry_ts,
        )
        if previous == updated_state:
            if current_data is None:
                self.data = UnifiPresenceData(clients=self._client_states)
            elif current_data.clients is not self._client_states:
                current_data.clients = self._client_states
            if not recovered:
                return

            self.last_update_success = True
            self.async_update_listeners()
            return

        public_changed = previous is None or previous.is_home != is_home or previous.name != name
        if previous is None:
            self._client_states[mac] = updated_state
        else:
            previous.is_home = is_home
            previous.name = name
            previous.last_seen_ts = last_seen_ts
            previous.expiry_ts = expiry_ts
        if current_data is None:
            self.data = UnifiPresenceData(clients=self._client_states)
        elif current_data.clients is not self._client_states:
            current_data.clients = self._client_states
        self.last_update_success = True
        if not public_changed and not recovered:
            return

        _LOGGER.debug(
            "Device %s (%s) %s: %s",
            name,
            mac,
            "initial state" if previous is None else "state changed",
            "home" if is_home else "away",
        )

        self.async_update_listeners()

    @callback
    def _publish_local_state_change(self) -> None:
        """Publish local state changes without altering refresh success bookkeeping.

        Unlike ``async_set_updated_data()``, this does not reset the refresh
        timer or flip ``last_update_success`` to ``True``. That distinction
        matters for heartbeat-only transitions: a device going ``not_home``
        due to expiry should not look like a successful REST poll.
        """
        current_data = cast(UnifiPresenceData | None, self.data)
        if current_data is None:
            self.data = UnifiPresenceData(clients=self._client_states)
        elif current_data.clients is not self._client_states:
            current_data.clients = self._client_states
        self.async_update_listeners()

    @callback
    def _async_check_heartbeat_expiry(self, *_: datetime) -> None:
        """Expire tracked clients whose away deadline has elapsed."""
        self._cancel_heartbeat_check = None
        self._scheduled_heartbeat_ts = None
        if not self._client_states:
            return

        now_ts = int(dt_util.utcnow().timestamp())
        changed_macs: list[str] = []

        for mac, state in self._client_states.items():
            expiry_ts = state.expiry_ts
            if expiry_ts is None or now_ts < expiry_ts:
                continue

            if state.last_seen_ts is not None:
                is_home, refreshed_expiry_ts = self._compute_presence_from_last_seen(state.last_seen_ts)
                if is_home and refreshed_expiry_ts is not None:
                    state.expiry_ts = refreshed_expiry_ts
                    continue

            state.expiry_ts = None
            if state.is_home:
                state.is_home = False
                changed_macs.append(mac)

        if not changed_macs:
            self._reschedule_heartbeat_check()
            return

        self._publish_local_state_change()
        self._reschedule_heartbeat_check()

    async def _ensure_controller(self) -> Controller:
        """Create or re-authenticate the controller connection."""
        if self._controller is not None:
            return self._controller

        data = self.config_entry.data
        params = ControllerConnectionParams.from_mapping(data)
        if should_resolve_controller_site(params, unique_id=self.config_entry.unique_id):
            self._controller, _ = await create_controller_with_resolved_site(
                self.hass,
                params,
                unique_id=self.config_entry.unique_id,
            )
        else:
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

        previous_state = self._client_states.get(mac)
        previous_info: ClientInfo | None = {"name": previous_state.name} if previous_state is not None else None
        name = self._build_client_info_from_client(
            mac,
            cast(ClientLike, _MessageClientView(raw)),
            previous_info=previous_info,
        )

        is_home, effective_last_seen, expiry_ts = self._apply_presence_observation(mac, last_seen=last_seen)
        self._update_single_device_state(
            mac,
            is_home=is_home,
            name=name,
            last_seen_ts=effective_last_seen,
            expiry_ts=expiry_ts,
        )
        self._reschedule_heartbeat_check()

    async def _async_refresh_with_reauth(self) -> Controller:
        """Refresh client stores, re-authenticating once on session expiry."""
        try:
            controller = await self._ensure_controller()
            try:
                await controller.clients_all.update()
            except TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError:
                _LOGGER.debug("Best-effort clients_all refresh failed; using cached data")
            await controller.clients.update()
            return controller
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            _LOGGER.info("UniFi session expired, re-authenticating")
            await self._async_close_runtime_controller()
            try:
                controller = await self._ensure_controller()
                try:
                    await controller.clients_all.update()
                except TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError:
                    _LOGGER.debug("Best-effort clients_all refresh failed; using cached data")
                await controller.clients.update()
            except (aiounifi.LoginRequired, aiounifi.Unauthorized) as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="credentials_rejected",
                ) from err
            except (TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError) as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect",
                    translation_placeholders={"host": self.config_entry.data[CONF_HOST]},
                ) from err

            if self.websocket is not None:
                self.websocket.restart_with_current_controller()

            return controller
        except (TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"host": self.config_entry.data[CONF_HOST]},
            ) from err

    async def _async_update_data(self) -> UnifiPresenceData:
        """Fallback REST poll — fetch data from the UniFi controller."""
        controller = await self._async_refresh_with_reauth()
        _LOGGER.debug("Fallback poll for tracked device(s)")

        clients = cast(dict[str, ClientLike], controller.clients)
        clients_all = cast(dict[str, ClientLike], controller.clients_all)
        previous_info = self.data.client_info if self.data is not None else {}
        new_states: dict[str, TrackedClientState] = {}

        for mac in self._tracked_macs:
            client = clients.get(mac)
            if client is not None:
                last_seen = client.last_seen or None
                name = self._build_client_info_from_client(
                    mac,
                    client,
                    previous_info=previous_info.get(mac),
                )
            else:
                last_seen = None
                name = self._resolve_offline_client_name(mac, previous_info, clients_all)

            is_home, effective_last_seen, expiry_ts = self._apply_presence_observation(mac, last_seen=last_seen)
            new_states[mac] = TrackedClientState(
                is_home=is_home,
                name=name,
                last_seen_ts=effective_last_seen,
                expiry_ts=expiry_ts,
            )

        self._client_states = new_states
        new_data = UnifiPresenceData(clients=new_states)
        self._reschedule_heartbeat_check()
        if self.data is not None and new_data == self.data:
            self.data.clients = new_data.clients
            self._client_states = new_data.clients
            return self.data

        return new_data


class _MessageClientView:
    """Adapter exposing message payload fields through the ClientLike protocol."""

    def __init__(self, raw: dict[str, object]) -> None:
        """Initialize the message view."""
        name_raw = raw.get("name")
        hostname_raw = raw.get("hostname")
        self.name = name_raw if isinstance(name_raw, str) else None
        self.hostname = hostname_raw if isinstance(hostname_raw, str) else None
        self.last_seen = None
