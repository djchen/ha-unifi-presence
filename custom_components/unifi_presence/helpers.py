"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, auto
from json import JSONDecodeError
from typing import Any, cast

import aiohttp
import aiounifi
from aiohttp import CookieJar
from aiounifi.controller import Controller
from aiounifi.models.client import Client
from aiounifi.models.configuration import Configuration
from aiounifi.models.site import Site
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util.ssl import client_context as ha_client_context

from .const import (
    CONF_SITE,
    CONF_SSL_VERIFY,
    DEFAULT_SITE,
)

CONTROLLER_LOGIN_TIMEOUT = 10
NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL = "No longer in UniFi Client Devices"

UNIFI_AUTH_EXCEPTIONS: tuple[type[Exception], ...] = (aiounifi.LoginRequired, aiounifi.Unauthorized)
UNIFI_COMMUNICATION_EXCEPTIONS: tuple[type[Exception], ...] = (
    TimeoutError,
    aiounifi.AiounifiException,
    aiohttp.ClientError,
    JSONDecodeError,
)


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
            ssl_verify=bool(data[CONF_SSL_VERIFY]),
        )


class ClientStoreRefreshFailed(RuntimeError):
    """Raised when client stores cannot be refreshed and no cache can be used."""


class ClientStoreRefreshPolicy(Enum):
    """Client-store refresh policy for runtime and setup discovery."""

    DISCOVERY = auto()
    RUNTIME = auto()


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


def config_entry_site_id(entry: ConfigEntry) -> str:
    """Return the config entry site identifier used for tracker IDs."""
    return entry.unique_id or entry.entry_id


def site_title(site: Site) -> str:
    """Return the user-facing title for a UniFi site."""
    return str(site.description or site.name)


def resolve_client_display_name(
    mac: str,
    *,
    current: Client | None = None,
    historical: Client | None = None,
    websocket_name: str | None = None,
    websocket_hostname: str | None = None,
    previous_name: str | None = None,
) -> str:
    """Resolve a display name from live, historical, cached, then MAC data."""
    if websocket_name:
        return websocket_name
    if websocket_hostname:
        return websocket_hostname

    for client in (current, historical):
        if client is None:
            continue
        name = str(client.name or "")
        hostname = str(client.hostname or "")
        if name or hostname:
            return name or hostname

    if previous_name is not None:
        return previous_name

    return mac


def should_resolve_controller_site(
    params: ControllerConnectionParams,
    *,
    unique_id: str | None,
) -> bool:
    """Return whether a stored site value needs legacy normalization."""
    return (
        bool(params.site)
        and params.site != DEFAULT_SITE
        and (unique_id is None or params.site == unique_id or "_" in unique_id)
    )


async def create_controller_for_params(
    hass: HomeAssistant,
    params: ControllerConnectionParams,
    *,
    unique_id: str | None = None,
    resolve_legacy_site: bool = False,
) -> Controller:
    """Create a controller, resolving legacy stored site values when requested."""
    should_resolve = resolve_legacy_site and should_resolve_controller_site(params, unique_id=unique_id)
    controller = await create_controller(
        hass,
        replace(params, site="") if should_resolve else params,
    )

    if not should_resolve:
        return controller

    try:
        async with asyncio.timeout(CONTROLLER_LOGIN_TIMEOUT):
            await controller.sites.update()

        resolved_site = params.site
        for available_site in controller.sites.values():
            if available_site.site_id in {params.site, unique_id}:
                resolved_site = str(available_site.name)
                break

        controller.connectivity.config.site = resolved_site
    except BaseException:
        await async_close_controller(controller)
        raise

    return controller


async def async_refresh_client_stores(
    controller: Controller,
    *,
    policy: ClientStoreRefreshPolicy,
) -> None:
    """Refresh UniFi client stores while preserving caller-specific policy."""
    historical_result, active_result = await asyncio.gather(
        controller.clients_all.update(),
        controller.clients.update(),
        return_exceptions=True,
    )

    historical_refreshed = _client_store_refresh_succeeded(historical_result)
    active_refreshed = _client_store_refresh_succeeded(active_result)

    if policy is ClientStoreRefreshPolicy.RUNTIME and not active_refreshed:
        assert isinstance(active_result, UNIFI_COMMUNICATION_EXCEPTIONS)
        raise active_result

    if policy is ClientStoreRefreshPolicy.DISCOVERY and not historical_refreshed and not active_refreshed:
        has_cached = any(controller.clients_all) or any(controller.clients)
        if not has_cached:
            msg = "Both active and historical client sources failed"
            raise ClientStoreRefreshFailed(msg)


def _client_store_refresh_succeeded(result: object) -> bool:
    """Return success for expected refresh results and re-raise unexpected errors."""
    if not isinstance(result, BaseException):
        return True

    if isinstance(result, UNIFI_COMMUNICATION_EXCEPTIONS):
        return False

    raise result


def build_client_labels_from_stores(
    historical_clients: Iterable[tuple[str, Client]],
    active_clients: Iterable[tuple[str, Client]],
) -> dict[str, str]:
    """Build picker labels from historical and active UniFi client stores."""
    clients: dict[str, str] = {}
    for store_items in (historical_clients, active_clients):
        for mac, client in store_items:
            mac_lower = normalize_mac(mac)
            name = resolve_client_display_name(mac_lower, current=client)
            clients[mac_lower] = f"{name} ({mac_lower})"

    return clients


async def async_close_controller(controller: Controller) -> None:
    """Detach an aiohttp session owned by this integration.

    Home Assistant client sessions share a connector, so detach() is the
    correct cleanup here: it closes the session wrapper without tearing down
    the shared connector used elsewhere in Home Assistant.
    """
    owned_session = getattr(controller, "_unifi_presence_owned_session", None)
    if owned_session is None or getattr(owned_session, "closed", False):
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
        cast(Any, controller)._unifi_presence_owned_session = session

    login_succeeded = False
    try:
        async with asyncio.timeout(CONTROLLER_LOGIN_TIMEOUT):
            await controller.login()
        login_succeeded = True
    finally:
        if not login_succeeded:
            await async_close_controller(controller)

    return controller
