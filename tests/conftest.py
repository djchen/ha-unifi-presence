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
        self.update_mock = AsyncMock()

    async def update(self) -> None:
        await self.update_mock()


@pytest.fixture
def mock_coordinator_controller() -> Generator[MagicMock]:
    """Fixture to mock the aiounifi Controller for coordinator tests."""
    controller = _build_controller(
        clients=_MockClientStore(),
        clients_all=_MockClientStore(),
    )

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller",
        return_value=controller,
    ):
        yield controller


@pytest.fixture
def mock_controller() -> MagicMock:
    """Fully-wired mock aiounifi controller for integration tests."""
    clients = MagicMock()
    clients.update = AsyncMock()
    clients.get = MagicMock(return_value=None)
    return _build_controller(clients=clients)


def _build_client_store(items: list[tuple[str, MagicMock]] | None = None) -> MagicMock:
    """Create a dict-like mock client store with async update support."""
    store = MagicMock()
    store.update = AsyncMock()
    store.update_mock = store.update
    item_list = items or []
    store.items.return_value = item_list
    store.__iter__.side_effect = lambda: iter(k for k, _v in item_list)
    store.get = MagicMock(side_effect=dict(item_list).get)
    return store


def make_mock_controller(
    *,
    login_side_effect: Exception | None = None,
    clients_all_items: list[tuple[str, MagicMock]] | None = None,
    clients_items: list[tuple[str, MagicMock]] | None = None,
    sites: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a fully-wired controller mock for flow and integration tests."""
    controller = _build_controller(
        clients=_build_client_store(clients_items),
        clients_all=_build_client_store(clients_all_items),
    )
    controller.login = AsyncMock(side_effect=login_side_effect)
    controller.sites = MagicMock()
    controller.sites.update = AsyncMock()
    controller.sites.values.return_value = sites or []
    return controller


def _build_controller(
    *,
    clients: MagicMock | _MockClientStore,
    clients_all: MagicMock | _MockClientStore | None = None,
) -> MagicMock:
    """Create a fully wired controller mock with shared defaults."""
    controller = MagicMock()
    controller.clients = clients
    if clients_all is not None:
        controller.clients_all = clients_all
    else:
        controller.clients_all = MagicMock()
        controller.clients_all.update = AsyncMock()
        controller.clients_all.get = MagicMock(return_value=None)
    controller.login = AsyncMock()
    controller.messages = MagicMock()
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.connectivity = MagicMock()
    controller.start_websocket = AsyncMock()
    return controller
