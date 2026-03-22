"""Tests for the UniFi Presence config flow."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS, _make_mock_client

PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.config_flow.create_controller"


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Standard config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def options_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry with full options added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options=MOCK_OPTIONS,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _bypass_setup(hass: HomeAssistant, enable_custom_integrations) -> Generator[None]:
    """Enable custom integrations and prevent actual setup after config flow."""
    with patch(
        "custom_components.unifi_presence.async_setup_entry",
        return_value=True,
    ):
        yield


def _mock_controller(
    login_side_effect: Exception | None = None,
    clients_all_items: list[Any] | None = None,
    clients_items: list[Any] | None = None,
) -> MagicMock:
    """Create a mock aiounifi Controller."""
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=login_side_effect)
    controller.start_websocket = AsyncMock()
    controller.clients_all = MagicMock()
    controller.clients_all.update = AsyncMock()
    controller.clients_all.items.return_value = clients_all_items or []
    controller.clients = MagicMock()
    controller.clients.update = AsyncMock()
    controller.clients.items.return_value = clients_items or []
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.connectivity = MagicMock()
    return controller


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """Test that the user step shows the credential form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_invalid_auth(hass: HomeAssistant) -> None:
    """Test that invalid credentials show an error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.LoginRequired):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_unauthorized(hass: HomeAssistant) -> None:
    """Test that Unauthorized (api.err.Invalid) shows an auth error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.Unauthorized):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(hass: HomeAssistant) -> None:
    """Test that connection errors show an error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.AiounifiException):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_unknown_error(hass: HomeAssistant) -> None:
    """Test that unexpected errors surface as unknown."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("boom")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_step_client_fetch_failure_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts if client discovery fails after login."""
    controller = _mock_controller(clients_all_items=[])
    controller.clients_all.update = AsyncMock(side_effect=Exception("fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_discovered"


async def test_user_step_success_goes_to_devices(hass: HomeAssistant) -> None:
    """Test successful login proceeds to device selection."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


async def test_devices_step_creates_entry(hass: HomeAssistant) -> None:
    """Test that selecting devices creates a config entry."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "UniFi Presence (192.168.1.1)"
    assert result["data"][CONF_HOST] == "192.168.1.1"
    assert "aa:bb:cc:dd:ee:ff" in result["options"][CONF_TRACKED_DEVICES]


@pytest.mark.parametrize(
    ("host", "site", "expected_unique_id"),
    [
        ("192.168.1.1", "default", "192.168.1.1_default"),
        ("::1", "default", "::1_default"),
        ("fd12:3456:789a::1", "mysite", "fd12:3456:789a::1_mysite"),
        ("unifi.local", "default", "unifi.local_default"),
        ("controller.example.com", "office", "controller.example.com_office"),
    ],
)
async def test_devices_step_creates_entry_host_variants(
    hass: HomeAssistant, host: str, site: str, expected_unique_id: str
) -> None:
    """Test that setup works with IPv4, IPv6, and hostname host values."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    config_data = {**MOCK_CONFIG_DATA, CONF_HOST: host, "site": site}

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=config_data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"UniFi Presence ({host})"
    assert result["data"][CONF_HOST] == host
    assert result["result"].unique_id == expected_unique_id


async def test_user_step_no_devices_discovered_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts if UniFi returns no clients to choose from."""
    controller = _mock_controller(clients_all_items=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_discovered"


async def test_devices_step_no_devices(hass: HomeAssistant) -> None:
    """Test that submitting with no devices shows an error."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


async def test_already_configured_abort(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that duplicate host_site unique_id aborts."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_CONFIG_DATA,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, options_entry: MockConfigEntry) -> None:
    """Test that options flow shows form with current values and saves new options."""
    mock_coordinator = MagicMock()
    mock_coordinator.controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )
    options_entry.runtime_data = mock_coordinator
    options_entry.mock_state(hass, ConfigEntryState.LOADED)

    result = await hass.config_entries.options.async_init(options_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
            CONF_AWAY_SECONDS: 120,
            CONF_FALLBACK_POLL_INTERVAL: 600,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AWAY_SECONDS] == 120
    assert result["data"][CONF_FALLBACK_POLL_INTERVAL] == 600
    assert result["data"][CONF_TRACKED_DEVICES] == ["aa:bb:cc:dd:ee:ff"]


def _make_reconfigure_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a standard entry for reconfigure flow tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_flow_success(hass: HomeAssistant) -> None:
    """Test that reconfigure flow updates credentials and reloads."""
    entry = _make_reconfigure_entry(hass)

    new_data = {
        "host": "10.0.0.1",
        "port": 8443,
        "site": "office",
        "username": "newadmin",
        "password": "newpass",
        "ssl_verify": True,
    }

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=new_data,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "10.0.0.1"
    assert entry.data["port"] == 8443
    assert entry.data["site"] == "office"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.data["ssl_verify"] is True
    assert entry.unique_id == "10.0.0.1_office"
    assert entry.title == "UniFi Presence (10.0.0.1)"


@pytest.mark.parametrize(
    ("new_host", "new_site", "expected_unique_id"),
    [
        ("10.0.0.1", "office", "10.0.0.1_office"),
        ("::1", "default", "::1_default"),
        ("fd12:3456:789a::1", "mysite", "fd12:3456:789a::1_mysite"),
        ("unifi.local", "default", "unifi.local_default"),
        ("controller.example.com", "office", "controller.example.com_office"),
    ],
)
async def test_reconfigure_flow_success_host_variants(
    hass: HomeAssistant, new_host: str, new_site: str, expected_unique_id: str
) -> None:
    """Test that reconfigure works with IPv4, IPv6, and hostname host values."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": new_host,
                "port": 8443,
                "site": new_site,
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == new_host
    assert entry.unique_id == expected_unique_id
    assert entry.title == f"UniFi Presence ({new_host})"


async def test_reconfigure_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Test that reconfigure flow shows error on invalid credentials."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.LoginRequired):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": MOCK_CONFIG_DATA["host"],
                "port": MOCK_CONFIG_DATA["port"],
                "username": "bad-user",
                "password": "bad-pass",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_flow_cannot_connect(hass: HomeAssistant) -> None:
    """Test that reconfigure flow shows cannot_connect on connection errors."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.AiounifiException):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": MOCK_CONFIG_DATA["host"],
                "port": MOCK_CONFIG_DATA["port"],
                "username": "admin",
                "password": "new-pass",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_unknown_error(hass: HomeAssistant) -> None:
    """Test that reconfigure flow surfaces unexpected errors as unknown."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("boom")):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": MOCK_CONFIG_DATA["host"],
                "port": MOCK_CONFIG_DATA["port"],
                "username": "admin",
                "password": "new-pass",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure_flow_already_configured(hass: HomeAssistant) -> None:
    """Test that reconfigure fails if the new host/site is already configured."""
    entry = _make_reconfigure_entry(hass)

    duplicate_data = {
        **MOCK_CONFIG_DATA,
        "host": "10.0.0.1",
        "site": "office",
    }
    duplicate = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (10.0.0.1)",
        data=duplicate_data,
        unique_id="10.0.0.1_office",
        options={CONF_TRACKED_DEVICES: ["11:22:33:44:55:66"]},
    )
    duplicate.add_to_hass(hass)

    with patch(PATCH_CREATE_CONTROLLER) as create_controller:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "10.0.0.1",
                "port": MOCK_CONFIG_DATA["port"],
                "site": "office",
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_configured"}
    create_controller.assert_not_called()


async def test_options_flow_without_runtime_data_uses_login(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test options flow falls back to creating a controller when runtime_data is unavailable."""
    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller) as create_controller:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    create_controller.assert_called_once()


async def test_options_flow_rejects_empty_tracked_devices(hass: HomeAssistant, options_entry: MockConfigEntry) -> None:
    """Test that options flow shows error when submitting with no tracked devices."""
    mock_coordinator = MagicMock()
    mock_coordinator.controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )
    options_entry.runtime_data = mock_coordinator
    options_entry.mock_state(hass, ConfigEntryState.LOADED)

    result = await hass.config_entries.options.async_init(options_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_TRACKED_DEVICES: [],
            CONF_AWAY_SECONDS: 60,
            CONF_FALLBACK_POLL_INTERVAL: 300,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


async def test_reconfigure_flow_same_host_site_changes_credentials(hass: HomeAssistant) -> None:
    """Test reconfigure with same host/site but different username/password succeeds."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.1.1",
                "port": 443,
                "site": "default",
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.unique_id == "192.168.1.1_default"


async def test_options_flow_runtime_data_no_controller_falls_back(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test options flow falls back to login when runtime_data exists but controller is None."""
    mock_coordinator = MagicMock()
    mock_coordinator.controller = None
    config_entry.runtime_data = mock_coordinator
    config_entry.mock_state(hass, ConfigEntryState.LOADED)

    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller) as create_ctrl:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    create_ctrl.assert_called_once()


async def test_options_flow_handles_client_fetch_error(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test options flow still saves non-device options when client fetch fails."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("offline")):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_AWAY_SECONDS: 90,
            CONF_FALLBACK_POLL_INTERVAL: 600,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TRACKED_DEVICES] == ["aa:bb:cc:dd:ee:ff"]


# ── Reauthentication flow tests ──────────────────────────────────────────


async def test_reauth_shows_form(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that the reauth flow shows the credential form."""
    result = await config_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert "username" in result["data_schema"].schema
    assert "password" in result["data_schema"].schema


async def test_reauth_success(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test successful reauthentication updates credentials and reloads."""
    controller = _mock_controller()

    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"username": "new_admin", "password": "new_pass"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["username"] == "new_admin"
    assert config_entry.data["password"] == "new_pass"


async def test_reauth_invalid_auth(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that invalid credentials show an error in the reauth form."""
    result = await config_entry.start_reauth_flow(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.LoginRequired("bad")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"username": "admin", "password": "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_cannot_connect(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that connection failure shows an error in the reauth form."""
    result = await config_entry.start_reauth_flow(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.AiounifiException("fail")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"username": "admin", "password": "password"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_unknown_error(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that an unexpected error shows an error in the reauth form."""
    result = await config_entry.start_reauth_flow(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"username": "admin", "password": "password"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
