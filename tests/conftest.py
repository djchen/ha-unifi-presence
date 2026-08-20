"""Shared fixtures and helpers for UniFi Presence tests."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Callable, Generator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import SelectSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_SITE,
    CONF_SSL_VERIFY,
    CONF_TRACKED_DEVICES,
    DOMAIN,
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

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.config_flow.create_controller_for_params"
DEFAULT_SITE_ID = "site-default-id"
OFFICE_SITE_ID = "site-office-id"
USER_STEP_INPUT = {k: v for k, v in MOCK_CONFIG_DATA.items() if k != CONF_SITE}
RECONFIGURE_STEP_INPUT = {
    CONF_HOST: MOCK_CONFIG_DATA[CONF_HOST],
    CONF_PORT: MOCK_CONFIG_DATA[CONF_PORT],
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "new-pass",
}
REAUTH_CONFIRM_INPUT = {
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "password",
}


# ── Shared mock factories ────────────────────────────────────────────────


def _make_mock_client(
    mac: str,
    name: str = "",
    hostname: str = "",
    ip: str = "",
    last_seen: int = 0,
    is_wired: bool = False,
) -> SimpleNamespace:
    """Create an aiounifi-like client record."""
    return SimpleNamespace(
        mac=mac,
        name=name,
        hostname=hostname,
        ip=ip,
        last_seen=last_seen,
        is_wired=is_wired,
    )


def _make_mock_site(site_id: str, name: str, description: str = "") -> SimpleNamespace:
    """Create an aiounifi-like site record."""
    return SimpleNamespace(site_id=site_id, name=name, description=description)


class _MockClientStore(dict[str, Any]):
    """Dict-like store for mock clients that also has an async update method."""

    def __init__(self, items: list[tuple[str, Any]] | None = None) -> None:
        super().__init__(items or [])
        self.update_mock = AsyncMock()

    async def update(self) -> None:
        await self.update_mock()


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
        controller.clients_all = _MockClientStore()
    controller.login = AsyncMock()
    controller.messages = MagicMock()
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.messages.new_data = MagicMock()
    controller.connectivity = MagicMock()
    controller.connectivity.ws_message_received = None
    controller.start_websocket = AsyncMock()
    return controller


def make_mock_controller(
    *,
    clients_all_items: list[tuple[str, Any]] | None = None,
    clients_items: list[tuple[str, Any]] | None = None,
    sites: list[Any] | None = None,
) -> MagicMock:
    """Create a fully-wired controller mock for flow and integration tests."""
    controller = _build_controller(
        clients=_MockClientStore(clients_items),
        clients_all=_MockClientStore(clients_all_items),
    )
    controller.sites = MagicMock()
    controller.sites.update = AsyncMock()
    controller.sites.values.return_value = (
        sites if sites is not None else [_make_mock_site(DEFAULT_SITE_ID, "default", "Home")]
    )
    return controller


_mock_controller = make_mock_controller


# ── Shared config-flow helpers ───────────────────────────────────────────


def _get_tracked_device_selector(result: dict[str, Any]) -> SelectSelector:
    """Return the tracked device selector from a flow result."""
    schema = result["data_schema"].schema
    tracked_key = next(key for key in schema if str(key) == CONF_TRACKED_DEVICES)
    selector = schema[tracked_key]
    assert isinstance(selector, SelectSelector)
    return selector


def _get_tracked_device_options(result: dict[str, Any]) -> dict[str, str]:
    """Return the tracked device selector options from a flow result."""
    selector = _get_tracked_device_selector(result)
    options = cast(list[dict[str, str]], selector.config["options"])
    return {option["value"]: option["label"] for option in options}


def make_reconfigure_input(**overrides: object) -> dict[str, object]:
    """Return a standard reconfigure payload with optional overrides."""
    return {**RECONFIGURE_STEP_INPUT, **overrides}


def make_reauth_confirm_input(**overrides: object) -> dict[str, object]:
    """Return a standard reauth-confirm payload with optional overrides."""
    return {**REAUTH_CONFIRM_INPUT, **overrides}


async def async_configure_flow_step(
    hass: HomeAssistant,
    result: dict[str, Any],
    user_input: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Submit user input to the current config flow step."""
    return cast(
        dict[str, Any],
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input=user_input),
    )


async def async_run_user_step(
    hass: HomeAssistant,
    user_input: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Start the user flow and submit the first credential step."""
    result = cast(
        dict[str, Any],
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}),
    )
    return await async_configure_flow_step(hass, result, user_input)


async def async_run_reconfigure_step(
    hass: HomeAssistant,
    entry: Any,
    user_input: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Start a reconfigure flow and submit the credentials step."""
    return await async_configure_flow_step(hass, await entry.start_reconfigure_flow(hass), user_input)


async def async_run_reauth_confirm_step(
    hass: HomeAssistant,
    entry: Any,
    user_input: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Start a reauth flow and submit the confirmation step."""
    return await async_configure_flow_step(hass, await entry.start_reauth_flow(hass), user_input)


def add_mock_config_entry(
    hass: HomeAssistant,
    *,
    title: str = "Home",
    data: dict[str, object] | None = None,
    unique_id: str | None = DEFAULT_SITE_ID,
    options: dict[str, object] | None = None,
) -> MockConfigEntry:
    """Create and add a standard mock config entry to Home Assistant."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data=cast(dict[str, Any], MOCK_CONFIG_DATA if data is None else data),
        unique_id=unique_id,
        options=cast(
            dict[str, Any],
            {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]} if options is None else options,
        ),
    )
    entry.add_to_hass(hass)
    return entry


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
async def coordinator_config_entry(hass: HomeAssistant) -> AsyncGenerator[MagicMock]:
    """Create a mock config entry for coordinator tests."""
    unload_callbacks: list[Callable[[], object]] = []

    def _async_on_unload(callback: Callable[[], object]) -> Callable[[], object]:
        unload_callbacks.append(callback)
        return callback

    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = MOCK_CONFIG_DATA
    entry.options = MOCK_OPTIONS
    entry.async_on_unload = MagicMock(side_effect=_async_on_unload)

    yield entry

    for unload_callback in reversed(unload_callbacks):
        result = unload_callback()
        if inspect.isawaitable(result):
            await result


@pytest.fixture
def mock_coordinator_controller() -> Generator[MagicMock]:
    """Fixture to mock the aiounifi Controller for coordinator tests."""
    controller = _build_controller(
        clients=_MockClientStore(),
        clients_all=_MockClientStore(),
    )

    with patch(
        "custom_components.unifi_presence.coordinator.create_controller_for_params",
        return_value=controller,
    ):
        yield controller


@pytest.fixture
def mock_controller() -> MagicMock:
    """Fully-wired mock aiounifi controller for integration tests."""
    clients = _MockClientStore()
    return _build_controller(clients=clients)
