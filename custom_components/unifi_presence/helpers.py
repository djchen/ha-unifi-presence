"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiounifi
from aiohttp import CookieJar
from aiounifi.models.configuration import Configuration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util.ssl import client_context as ha_client_context

if TYPE_CHECKING:
    from aiounifi.controller import Controller


async def create_controller(
    hass: HomeAssistant,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    ssl_verify: bool,
) -> Controller:
    """Create, authenticate, and return an aiounifi Controller."""
    if ssl_verify:
        session = async_get_clientsession(hass)
        ssl_context = ha_client_context()
    else:
        session = async_create_clientsession(hass, verify_ssl=False, cookie_jar=CookieJar(unsafe=True))
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
    controller = aiounifi.Controller(config)  # type: ignore[attr-defined]
    async with asyncio.timeout(10):
        await controller.login()
    return controller
