"""Shared fixtures for UniFi Presence tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_SITE,
    CONF_SSL_VERIFY,
    CONF_TRACKED_DEVICES,
)

MOCK_CONFIG_DATA = {
    CONF_HOST: "192.168.1.1",
    CONF_PORT: 443,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "password",
    CONF_SITE: "default",
    CONF_SSL_VERIFY: False,
}

MOCK_OPTIONS = {
    CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
    CONF_AWAY_SECONDS: 60,
    CONF_FALLBACK_POLL_INTERVAL: 300,
}


def _make_mock_client(
    mac: str,
    name: str = "",
    hostname: str = "",
    ip: str = "",
    last_seen: int = 0,
    is_wired: bool = False,
) -> MagicMock:
    """Create a mock aiounifi client object."""
    client = MagicMock()
    client.mac = mac
    client.name = name
    client.hostname = hostname
    client.ip = ip
    client.last_seen = last_seen
    client.is_wired = is_wired
    return client


class _MockClientStore(dict):
    """Dict-like store for mock clients that also has an async update method."""

    def __init__(self) -> None:
        super().__init__()
        self.update_async = AsyncMock()

    async def update(self) -> None:
        await self.update_async()


@pytest.fixture
def mock_coordinator_controller() -> Generator[AsyncMock]:
    """Fixture to mock the aiounifi Controller for coordinator tests."""
    controller = AsyncMock()
    controller.clients = _MockClientStore()
    controller.login = AsyncMock()

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        yield controller
