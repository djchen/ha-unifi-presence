"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio

from aiohttp import CookieJar
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util.ssl import client_context as ha_client_context

from .const import DEFAULT_SITE

_OWNED_SESSION_ATTR = "_unifi_presence_owned_session"


def format_config_entry_title(site_title: str, host: str) -> str:
    """Return the Home Assistant config entry title for a site."""
    return f"{site_title} ({host})"


async def resolve_controller_site(
    hass: HomeAssistant,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    ssl_verify: bool,
    unique_id: str | None,
) -> str:
    """Resolve a config entry's stored site to the short UniFi site name.

    Legacy entries may store a site ID instead of the short site name required by
    site-scoped controller requests. If the stored value matches an accessible
    site's ``site_id``, return that site's short name. Otherwise return the
    stored value unchanged.
    """
    if site == DEFAULT_SITE:
        return site

    if unique_id is not None and site != unique_id and "_" not in unique_id:
        return site

    controller = await create_controller(
        hass,
        host,
        port,
        username,
        password,
        "",
        ssl_verify,
        transient=True,
    )
    try:
        await controller.sites.update()

        for available_site in controller.sites.values():
            if available_site.site_id in {site, unique_id}:
                return str(available_site.name)
    finally:
        await async_close_controller(controller)

    return site


async def async_close_controller(controller: Controller) -> None:
    """Close a transient controller session owned by this integration."""
    owned_session = getattr(controller, _OWNED_SESSION_ATTR, None)
    if owned_session is None or getattr(owned_session, "closed", False):
        return

    await owned_session.close()


async def create_controller(
    hass: HomeAssistant,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    ssl_verify: bool,
    transient: bool = False,
) -> Controller:
    """Create, authenticate, and return an aiounifi Controller."""
    if ssl_verify:
        session = async_get_clientsession(hass)
        ssl_context = ha_client_context()
    else:
        session = async_create_clientsession(
            hass,
            verify_ssl=False,
            auto_cleanup=not transient,
            cookie_jar=CookieJar(unsafe=True),
        )
        ssl_context = None
    config = Configuration(
        session,
        host=host,
        port=port,
        username=username,
        password=password,
        site=site,
        ssl_context=ssl_context if ssl_context is not None else False,
    )
    controller = Controller(config)
    if transient and not ssl_verify:
        setattr(controller, _OWNED_SESSION_ATTR, session)

    try:
        async with asyncio.timeout(10):
            await controller.login()
    except Exception:
        await async_close_controller(controller)
        raise

    return controller
