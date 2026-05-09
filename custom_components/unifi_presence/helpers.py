"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from aiohttp import CookieJar
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
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


def format_current_client_label(name: str, mac: str) -> str:
    """Return the user-facing label for a current UniFi client."""
    normalized_mac = normalize_mac(mac)
    return f"{name} ({normalized_mac})"


def format_missing_client_label(mac: str) -> str:
    """Return the label used for tracked clients no longer listed by UniFi."""
    normalized_mac = normalize_mac(mac)
    return f"{normalized_mac} ({NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL})"


def should_resolve_controller_site(
    params: ControllerConnectionParams,
    *,
    unique_id: str | None,
) -> bool:
    """Return whether a stored site value needs legacy normalization."""
    return params.site != DEFAULT_SITE and (unique_id is None or params.site == unique_id or "_" in unique_id)


async def create_controller_with_resolved_site(
    hass: HomeAssistant,
    params: ControllerConnectionParams,
    *,
    unique_id: str | None,
) -> tuple[Controller, str]:
    """Create a controller and normalize any legacy stored site value in-place."""
    controller = await create_controller(
        hass,
        replace(
            params,
            site="" if should_resolve_controller_site(params, unique_id=unique_id) else params.site,
        ),
    )

    try:
        if not should_resolve_controller_site(params, unique_id=unique_id):
            resolved_site = params.site
        else:
            async with asyncio.timeout(CONTROLLER_LOGIN_TIMEOUT):
                await controller.sites.update()

            resolved_site = params.site
            for available_site in controller.sites.values():
                if available_site.site_id in {params.site, unique_id}:
                    resolved_site = str(available_site.name)
                    break
    except Exception:
        await async_close_controller(controller)
        raise

    controller.connectivity.config.site = resolved_site
    return controller, resolved_site


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
