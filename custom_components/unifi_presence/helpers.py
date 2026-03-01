"""Shared helpers for UniFi Presence integration."""

from __future__ import annotations

import aiounifi
from aiounifi.models.configuration import Configuration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


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
    session = async_get_clientsession(hass, verify_ssl=ssl_verify)
    config = Configuration(
        session,
        host=host,
        port=port,
        username=username,
        password=password,
        site=site,
        ssl_context=ssl_verify,  # bool: False=skip, True=verify
    )
    controller = aiounifi.Controller(config)
    await controller.login()
    return controller
