"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import asyncio

import aiounifi
from aiohttp import CookieJar
from aiounifi.models.configuration import Configuration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)


async def create_controller(
    hass: HomeAssistant,
    host: str,
    port: int,
    username: str,
    password: str,
    site: str,
    ssl_verify: bool,
) -> aiounifi.Controller:
    """Create, authenticate, and return an aiounifi Controller."""
    if ssl_verify:
        session = async_get_clientsession(hass)
    else:
        session = async_create_clientsession(hass, verify_ssl=False, cookie_jar=CookieJar(unsafe=True))
    config = Configuration(
        session,
        host=host,
        port=port,
        username=username,
        password=password,
        site=site,
        ssl_context=ssl_verify,
    )
    controller = aiounifi.Controller(config)
    async with asyncio.timeout(10):
        await controller.login()
    return controller
