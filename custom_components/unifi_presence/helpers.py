"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Protocol, cast
from weakref import WeakKeyDictionary

from aiohttp import ClientSession, CookieJar
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util.ssl import client_context as ha_client_context

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
)

CONTROLLER_LOGIN_TIMEOUT = 10
NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL = "No longer in UniFi Client Devices"


class SiteLike(Protocol):
    """Subset of UniFi site fields used by this integration."""

    site_id: str
    name: str
    description: str | None


class ClientLike(Protocol):
    """Subset of UniFi client fields used by this integration."""

    name: str | None
    hostname: str | None
    last_seen: int | float | None


class WebsocketLike(Protocol):
    """Subset of WebSocket fields used by runtime summary."""

    available: bool


class RuntimeCoordinatorLike(Protocol):
    """Subset of coordinator fields shared by diagnostics/system health."""

    tracked_devices: tuple[str, ...]
    away_seconds: int
    update_interval: timedelta | None
    heartbeat_expiry_count: int
    last_update_success: bool
    data: Any
    controller: object
    websocket: WebsocketLike | None


@dataclass(slots=True, frozen=True)
class ControllerConnectionParams:
    """Connection parameters for opening a UniFi controller session."""

    host: str
    port: int
    username: str
    password: str
    site: str
    ssl_verify: bool

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        site: str | None = None,
    ) -> ControllerConnectionParams:
        """Build typed connection params from config-entry style data."""
        return cls(
            host=str(data[CONF_HOST]),
            port=int(data[CONF_PORT]),
            username=str(data[CONF_USERNAME]),
            password=str(data[CONF_PASSWORD]),
            site=str(data.get(CONF_SITE, DEFAULT_SITE) if site is None else site),
            ssl_verify=bool(data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)),
        )


@dataclass(slots=True, frozen=True)
class EntryRuntimeSummary:
    """Shared runtime summary derived from a config entry and coordinator."""

    tracked_macs: tuple[str, ...]
    away_seconds: int
    fallback_poll_interval_seconds: float | None
    websocket_connected: bool
    heartbeat_expiry_count: int
    last_update_success: bool | None

    @property
    def tracked_device_count(self) -> int:
        """Return the normalized tracked-device count."""
        return len(self.tracked_macs)


_OWNED_SESSIONS: WeakKeyDictionary[Controller, ClientSession] = WeakKeyDictionary()


def normalize_mac(mac: str) -> str:
    """Return a normalized MAC string for storage and comparisons."""
    return mac.strip().lower()


def normalize_macs(macs: Iterable[str]) -> tuple[str, ...]:
    """Return trimmed, lowercased, deduplicated MACs preserving order."""
    normalized: list[str] = []
    seen: set[str] = set()

    for mac in macs:
        normalized_mac = normalize_mac(mac)
        if not normalized_mac or normalized_mac in seen:
            continue

        seen.add(normalized_mac)
        normalized.append(normalized_mac)

    return tuple(normalized)


def tracker_unique_id(site_id: str, mac: str) -> str:
    """Return the site-scoped unique ID for a tracked client."""
    return f"{site_id}-{normalize_mac(mac)}"


def site_title(site: SiteLike) -> str:
    """Return the user-facing title for a UniFi site."""
    return str(site.description or site.name)


def format_config_entry_title(site_title_value: str, host: str) -> str:
    """Return the Home Assistant config entry title for a site."""
    return f"{site_title_value} ({host})"


def format_site_config_entry_title(site: SiteLike, host: str) -> str:
    """Return the Home Assistant config entry title for a site object."""
    return format_config_entry_title(site_title(site), host)


def format_current_client_label(name: str, mac: str) -> str:
    """Return the user-facing label for a current UniFi client."""
    normalized_mac = normalize_mac(mac)
    return f"{name} ({normalized_mac})"


def format_missing_client_label(mac: str) -> str:
    """Return the label used for tracked clients no longer listed by UniFi."""
    normalized_mac = normalize_mac(mac)
    return f"{normalized_mac} ({NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL})"


def get_entry_runtime_coordinator(entry: ConfigEntry[Any]) -> RuntimeCoordinatorLike | None:
    """Return typed runtime data for a loaded UniFi Presence entry, if any."""
    return cast(RuntimeCoordinatorLike | None, getattr(entry, "runtime_data", None))


def build_entry_runtime_summary(entry: ConfigEntry[Any]) -> EntryRuntimeSummary:
    """Build a shared runtime summary for diagnostics and system health."""
    coordinator = get_entry_runtime_coordinator(entry)
    tracked_macs = normalize_macs(entry.options.get(CONF_TRACKED_DEVICES, []))
    away_seconds = int(entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS))
    fallback_poll_interval_seconds: float | None = float(
        entry.options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL)
    )
    websocket_connected = False
    heartbeat_expiry_count = 0
    last_update_success: bool | None = None

    if coordinator is not None:
        tracked_macs = coordinator.tracked_devices
        away_seconds = coordinator.away_seconds
        fallback_poll_interval_seconds = (
            coordinator.update_interval.total_seconds() if coordinator.update_interval is not None else None
        )
        websocket_connected = coordinator.websocket is not None and coordinator.websocket.available
        heartbeat_expiry_count = coordinator.heartbeat_expiry_count
        last_update_success = coordinator.last_update_success

    return EntryRuntimeSummary(
        tracked_macs=tracked_macs,
        away_seconds=away_seconds,
        fallback_poll_interval_seconds=fallback_poll_interval_seconds,
        websocket_connected=websocket_connected,
        heartbeat_expiry_count=heartbeat_expiry_count,
        last_update_success=last_update_success,
    )


async def resolve_controller_site(
    hass: HomeAssistant,
    params: ControllerConnectionParams,
    *,
    unique_id: str | None,
) -> str:
    """Resolve a config entry's stored site to the short UniFi site name.

    Legacy entries may store a site ID instead of the short site name required by
    site-scoped controller requests. If the stored value matches an accessible
    site's ``site_id``, return that site's short name. Otherwise return the
    stored value unchanged.
    """
    if params.site == DEFAULT_SITE:
        return params.site

    if unique_id is not None and params.site != unique_id and "_" not in unique_id:
        return params.site

    controller = await create_controller(
        hass,
        replace(params, site=""),
    )
    try:
        await controller.sites.update()

        for available_site in controller.sites.values():
            if available_site.site_id in {params.site, unique_id}:
                return str(available_site.name)
    finally:
        await async_close_controller(controller)

    return params.site


async def async_close_controller(controller: Controller) -> None:
    """Detach an aiohttp session owned by this integration.

    Home Assistant client sessions share a connector, so detach() is the
    correct cleanup here: it closes the session wrapper without tearing down
    the shared connector used elsewhere in Home Assistant.
    """
    owned_session = _OWNED_SESSIONS.pop(controller, None)
    if owned_session is None or owned_session.closed:
        return

    owned_session.detach()


async def create_controller(
    hass: HomeAssistant,
    params: ControllerConnectionParams,
) -> Controller:
    """Create, authenticate, and return an aiounifi Controller."""
    if params.ssl_verify:
        session = async_get_clientsession(hass)
        ssl_context = ha_client_context()
    else:
        session = async_create_clientsession(
            hass,
            verify_ssl=False,
            auto_cleanup=False,
            cookie_jar=CookieJar(unsafe=True),
        )
        ssl_context = None

    config = Configuration(
        session,
        host=params.host,
        port=params.port,
        username=params.username,
        password=params.password,
        site=params.site,
        ssl_context=ssl_context if ssl_context is not None else False,
    )
    controller = Controller(config)
    if not params.ssl_verify:
        _OWNED_SESSIONS[controller] = session

    login_succeeded = False
    try:
        async with asyncio.timeout(CONTROLLER_LOGIN_TIMEOUT):
            await controller.login()
        login_succeeded = True
    finally:
        if not login_succeeded:
            await async_close_controller(controller)

    return controller
