"""Shared fixtures and helpers for UniFi Presence tests."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_SITE,
    CONF_SSL_VERIFY,
    CONF_TRACKED_DEVICES,
)

# ── Shared constants ─────────────────────────────────────────────────────

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

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.config_flow.create_controller"
DEFAULT_SITE_ID = "site-default-id"
OFFICE_SITE_ID = "site-office-id"
USER_STEP_INPUT = {k: v for k, v in MOCK_CONFIG_DATA.items() if k != CONF_SITE}


# ── Shared mock factories ────────────────────────────────────────────────


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


def _make_mock_site(site_id: str, name: str, description: str = "") -> MagicMock:
    """Create a mock aiounifi site object."""
    site = MagicMock()
    site.site_id = site_id
    site.name = name
    site.description = description
    return site


class _MockClientStore(dict):
    """Dict-like store for mock clients that also has an async update method."""

    def __init__(self) -> None:
        super().__init__()
        self.update_mock = AsyncMock()

    async def update(self) -> None:
        await self.update_mock()


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


def _mock_controller(
    login_side_effect: Exception | None = None,
    clients_all_items: list[Any] | None = None,
    clients_items: list[Any] | None = None,
    sites: list[Any] | None = None,
) -> MagicMock:
    """Create a mock aiounifi Controller with a default site."""
    return make_mock_controller(
        login_side_effect=login_side_effect,
        clients_all_items=clients_all_items,
        clients_items=clients_items,
        sites=sites if sites is not None else [_make_mock_site(DEFAULT_SITE_ID, "default", "Home")],
    )


# ── Shared config-flow helpers ───────────────────────────────────────────


def _site_arg_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Return the ``site`` argument from a mocked create_controller() call.

    The production signature is ``create_controller(hass, params)`` where
    *params* is a ``ControllerConnectionParams`` dataclass.
    """
    if "params" in kwargs:
        return str(kwargs["params"].site)

    # Positional: create_controller(hass, params)
    return str(args[1].site)


def _get_tracked_device_options(result: dict[str, Any]) -> dict[str, str]:
    """Return the tracked device selector options from a flow result."""
    schema = result["data_schema"].schema
    tracked_key = next(key for key in schema if str(key) == CONF_TRACKED_DEVICES)
    return schema[tracked_key].options


# ── Shared coordinator helpers ───────────────────────────────────────────


def make_reauth_side_effect(
    exception: type[Exception],
    *,
    recover: bool = True,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Return an async update side effect that raises *exception* on first call.

    If *recover* is True, the second call succeeds.  Otherwise it raises again.
    """
    call_count = 0

    async def _side_effect() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exception
        if not recover:
            raise exception

    return _side_effect


# ── Shared fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def _bypass_setup(hass: HomeAssistant, enable_custom_integrations: None) -> Generator[None]:
    """Enable custom integrations and prevent actual setup after config flow."""
    with patch(
        "custom_components.unifi_presence.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.fixture
def coordinator_config_entry(hass: HomeAssistant) -> MagicMock:
    """Create a mock config entry for coordinator tests."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = MOCK_CONFIG_DATA
    entry.options = MOCK_OPTIONS
    return entry


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
