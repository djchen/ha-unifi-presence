"""Tests for the UniFi Presence config flow."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
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
TRANSLATIONS_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "unifi_presence"
DEFAULT_SITE_ID = "site-default-id"
OFFICE_SITE_ID = "site-office-id"
USER_STEP_INPUT = {k: v for k, v in MOCK_CONFIG_DATA.items() if k != "site"}


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Standard config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def options_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry with full options added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
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
    sites: list[Any] | None = None,
) -> MagicMock:
    """Create a mock aiounifi Controller."""
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=login_side_effect)
    controller.start_websocket = AsyncMock()
    controller.clients_all = MagicMock()
    controller.clients_all.update = AsyncMock()
    controller.clients_all.items.return_value = clients_all_items or []
    controller.clients_all.__iter__ = lambda self: iter(k for k, _v in (clients_all_items or []))
    controller.clients = MagicMock()
    controller.clients.update = AsyncMock()
    controller.clients.items.return_value = clients_items or []
    controller.clients.__iter__ = lambda self: iter(k for k, _v in (clients_items or []))
    controller.sites = MagicMock()
    controller.sites.update = AsyncMock()
    controller.sites.values.return_value = (
        sites if sites is not None else [_make_mock_site(DEFAULT_SITE_ID, "default", "Home")]
    )
    controller.messages.subscribe = MagicMock(return_value=MagicMock())
    controller.connectivity = MagicMock()
    return controller


def _make_mock_site(site_id: str, name: str, description: str = "") -> MagicMock:
    """Create a mock aiounifi site object."""
    site = MagicMock()
    site.site_id = site_id
    site.name = name
    site.description = description
    return site


def _get_tracked_device_options(result: dict[str, Any]) -> dict[str, str]:
    """Return the tracked device selector options from a flow result."""
    schema = result["data_schema"].schema
    tracked_key = next(key for key in schema if str(key) == CONF_TRACKED_DEVICES)
    return schema[tracked_key].options


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
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_unauthorized(hass: HomeAssistant) -> None:
    """Test that Unauthorized (api.err.Invalid) shows an auth error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.Unauthorized):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(hass: HomeAssistant) -> None:
    """Test that connection errors show an error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.AiounifiException):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_timeout_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that controller login timeouts show a connectivity error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=TimeoutError):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_unknown_error(hass: HomeAssistant) -> None:
    """Test that unexpected errors surface as unknown."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("boom")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_step_client_fetch_failure_shows_discovery_error(hass: HomeAssistant) -> None:
    """Test that setup shows a discovery error if both client sources fail after login."""
    controller = _mock_controller(clients_all_items=[])
    controller.clients_all.update = AsyncMock(side_effect=Exception("historical fetch failed"))
    controller.clients.update = AsyncMock(side_effect=Exception("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "cannot_discover_devices"}


async def test_user_step_success_goes_to_devices(hass: HomeAssistant) -> None:
    """Test successful login proceeds to device selection."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert _get_tracked_device_options(result) == {"aa:bb:cc:dd:ee:ff": "Dan Phone (aa:bb:cc:dd:ee:ff)"}


async def test_user_step_multiple_sites_shows_friendly_selector(hass: HomeAssistant) -> None:
    """Test multi-site setup shows human-friendly site labels."""
    controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    site_field = next(iter(result["data_schema"].schema))
    assert result["data_schema"].schema[site_field].container == {
        DEFAULT_SITE_ID: "Home",
        OFFICE_SITE_ID: "Office",
    }


async def test_user_step_site_picker_does_not_assume_default_site(hass: HomeAssistant) -> None:
    """Test setup can reach site selection without default-site access."""
    controller = _mock_controller(
        sites=[
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
            _make_mock_site("site-guest-id", "guest", "Guest"),
        ]
    )

    def _create_controller_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        site = args[5] if len(args) > 5 else kwargs["site"]
        if site == "default":
            raise aiounifi.Unauthorized
        return controller

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_create_controller_side_effect) as mock_create_controller:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert not any(call.args[5] == "default" for call in mock_create_controller.call_args_list)


async def test_site_selection_stores_short_name_and_site_id(hass: HomeAssistant) -> None:
    """Test setup stores the UniFi site short name but keys the entry by site_id."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)],
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ],
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=USER_STEP_INPUT)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"site": OFFICE_SITE_ID})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Office"
    assert result["data"]["site"] == "office"
    assert result["result"].unique_id == OFFICE_SITE_ID


async def test_devices_step_uses_site_name_when_description_missing(hass: HomeAssistant) -> None:
    """Test site titles fall back to the site short name."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)],
        sites=[_make_mock_site(OFFICE_SITE_ID, "office", "")],
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=USER_STEP_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "office"
    assert result["data"]["site"] == "office"
    assert result["result"].unique_id == OFFICE_SITE_ID


async def test_user_step_historical_client_failure_uses_active_clients(
    hass: HomeAssistant,
) -> None:
    """Test that setup still proceeds when historical client refresh fails but active succeeds."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients_all.update = AsyncMock(side_effect=Exception("historical clients unavailable"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


async def test_user_step_active_client_refresh_failure_uses_historical_clients(
    hass: HomeAssistant,
) -> None:
    """Test that setup still proceeds when active client refresh fails."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active clients unavailable"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
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
            user_input=USER_STEP_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"],
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_HOST] == "192.168.1.1"
    assert result["data"]["site"] == "default"
    assert result["result"].unique_id == DEFAULT_SITE_ID
    assert "aa:bb:cc:dd:ee:ff" in result["options"][CONF_TRACKED_DEVICES]


@pytest.mark.parametrize(
    ("host", "expected_unique_id"),
    [
        ("192.168.1.1", DEFAULT_SITE_ID),
        ("::1", DEFAULT_SITE_ID),
        ("fd12:3456:789a::1", DEFAULT_SITE_ID),
        ("unifi.local", DEFAULT_SITE_ID),
        ("controller.example.com", DEFAULT_SITE_ID),
    ],
)
async def test_devices_step_creates_entry_host_variants(
    hass: HomeAssistant, host: str, expected_unique_id: str
) -> None:
    """Test that setup identity is based on site_id, not host."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    config_data = {**USER_STEP_INPUT, CONF_HOST: host}

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
    assert result["title"] == "Home"
    assert result["data"][CONF_HOST] == host
    assert result["result"].unique_id == expected_unique_id


async def test_user_step_no_clients_available_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts with the clearer no-clients reason."""
    controller = _mock_controller(clients_all_items=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_clients_available"


async def test_user_step_site_fetch_failure_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that failing to fetch sites returns to the user step with an error."""
    controller = _mock_controller()
    controller.sites.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_no_sites_available_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts when the account has no accessible UniFi sites."""
    controller = _mock_controller(sites=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_devices_step_no_devices(hass: HomeAssistant) -> None:
    """Test that submitting with no devices shows an error."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


async def test_already_configured_abort(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that duplicate site setup aborts even with a different host alias."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    alias_config = {**USER_STEP_INPUT, CONF_HOST: "controller.example.com"}

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=alias_config,
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


async def test_options_flow_preserves_missing_clients_with_expected_labels_and_order(hass: HomeAssistant) -> None:
    """Test missing tracked clients stay selectable and sort ahead of current clients."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
        options={
            CONF_TRACKED_DEVICES: ["cc:cc:cc:cc:cc:cc", "aa:aa:aa:aa:aa:aa"],
            CONF_AWAY_SECONDS: 60,
            CONF_FALLBACK_POLL_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)
    controller = _mock_controller(
        clients_all_items=[
            ("22:22:22:22:22:22", _make_mock_client("22:22:22:22:22:22", name="Alpha Phone")),
            ("11:11:11:11:11:11", _make_mock_client("11:11:11:11:11:11", name="Beta Phone")),
        ]
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    options = _get_tracked_device_options(result)
    assert list(options)[:2] == ["aa:aa:aa:aa:aa:aa", "cc:cc:cc:cc:cc:cc"]
    assert list(options)[2:] == ["22:22:22:22:22:22", "11:11:11:11:11:11"]
    assert options["aa:aa:aa:aa:aa:aa"] == "aa:aa:aa:aa:aa:aa (No longer in UniFi Client Devices)"
    assert options["cc:cc:cc:cc:cc:cc"] == "cc:cc:cc:cc:cc:cc (No longer in UniFi Client Devices)"
    assert options["22:22:22:22:22:22"] == "Alpha Phone (22:22:22:22:22:22)"
    assert options["11:11:11:11:11:11"] == "Beta Phone (11:11:11:11:11:11)"


async def test_options_flow_current_labels_always_append_mac(hass: HomeAssistant) -> None:
    """Test current client labels always append MACs in the options flow."""
    controller = _mock_controller(
        clients_all_items=[
            ("aa:aa:aa:aa:aa:aa", _make_mock_client("aa:aa:aa:aa:aa:aa", name="Dan Phone")),
            ("bb:bb:bb:bb:bb:bb", _make_mock_client("bb:bb:bb:bb:bb:bb", name="Dan Phone")),
            ("cc:cc:cc:cc:cc:cc", _make_mock_client("cc:cc:cc:cc:cc:cc", name="Zoe Phone")),
        ]
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
        options={CONF_TRACKED_DEVICES: ["aa:aa:aa:aa:aa:aa"]},
    )
    entry.add_to_hass(hass)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    options = _get_tracked_device_options(result)
    assert options == {
        "aa:aa:aa:aa:aa:aa": "Dan Phone (aa:aa:aa:aa:aa:aa)",
        "bb:bb:bb:bb:bb:bb": "Dan Phone (bb:bb:bb:bb:bb:bb)",
        "cc:cc:cc:cc:cc:cc": "Zoe Phone (cc:cc:cc:cc:cc:cc)",
    }


async def test_options_flow_keeps_missing_selected_clients_configured(hass: HomeAssistant) -> None:
    """Test a missing client remains configured when still selected in options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
        options={
            CONF_TRACKED_DEVICES: ["aa:aa:aa:aa:aa:aa"],
            CONF_AWAY_SECONDS: 60,
            CONF_FALLBACK_POLL_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)

    controller = _mock_controller(
        clients_all_items=[("bb:bb:bb:bb:bb:bb", _make_mock_client("bb:bb:bb:bb:bb:bb", name="Other Phone"))]
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: ["aa:aa:aa:aa:aa:aa"],
                CONF_AWAY_SECONDS: 120,
                CONF_FALLBACK_POLL_INTERVAL: 600,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TRACKED_DEVICES] == ["aa:aa:aa:aa:aa:aa"]


def _make_reconfigure_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a standard entry for reconfigure flow tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=DEFAULT_SITE_ID,
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
    assert entry.data["site"] == "default"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.data["ssl_verify"] is True
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.title == "Home"


async def test_reconfigure_flow_uses_existing_site_for_site_scoped_account(hass: HomeAssistant) -> None:
    """Test reconfigure does not require access to the default site."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office",
        data={**MOCK_CONFIG_DATA, "site": "office"},
        unique_id=OFFICE_SITE_ID,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.runtime_data = None
    entry.add_to_hass(hass)

    controller = _mock_controller(sites=[_make_mock_site(OFFICE_SITE_ID, "office", "Office")])

    def _create_controller_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        site = args[5] if len(args) > 5 else kwargs["site"]
        if site == "default":
            raise aiounifi.Unauthorized
        return controller

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_create_controller_side_effect) as mock_create_controller:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "10.0.0.1",
                "port": 8443,
                "username": "officeadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["site"] == "office"
    assert entry.unique_id == OFFICE_SITE_ID
    assert mock_create_controller.call_args_list[0].args[5] == "office"


@pytest.mark.parametrize(
    ("new_host", "expected_unique_id"),
    [
        ("10.0.0.1", DEFAULT_SITE_ID),
        ("::1", DEFAULT_SITE_ID),
        ("fd12:3456:789a::1", DEFAULT_SITE_ID),
        ("unifi.local", DEFAULT_SITE_ID),
        ("controller.example.com", DEFAULT_SITE_ID),
    ],
)
async def test_reconfigure_flow_success_host_variants(
    hass: HomeAssistant, new_host: str, expected_unique_id: str
) -> None:
    """Test that reconfigure keeps the same site identity across host changes."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": new_host,
                "port": 8443,
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == new_host
    assert entry.unique_id == expected_unique_id
    assert entry.title == "Home"


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


async def test_reconfigure_flow_timeout_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that reconfigure surfaces login timeouts as cannot_connect."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=TimeoutError):
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
    """Test that reconfigure aborts when the selected site differs."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "10.0.0.1",
                "port": MOCK_CONFIG_DATA["port"],
                "username": "newadmin",
                "password": "newpass",
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "site"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"site": OFFICE_SITE_ID},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "different_site_selected"


async def test_reconfigure_flow_site_fetch_failure_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that reconfigure returns an error if the site list cannot be loaded."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller()
    controller.sites.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
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
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_no_sites_available_aborts(hass: HomeAssistant) -> None:
    """Test that reconfigure aborts when the account has no accessible UniFi sites."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller(sites=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
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

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_reconfigure_flow_same_site_requires_site_access(hass: HomeAssistant) -> None:
    """Test that reconfigure validates site-scoped client access before saving."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller()
    site_controller = _mock_controller(clients_all_items=[], clients_items=[])
    site_controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException)
    site_controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(
        PATCH_CREATE_CONTROLLER,
        side_effect=[controller, site_controller],
    ):
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
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "cannot_discover_devices"}


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
    assert result["errors"] == {"base": "no_tracked_devices"}


def test_options_flow_no_tracked_devices_translation_keys_exist() -> None:
    """Test the dedicated options validation key exists in both translation files."""
    strings = json.loads((TRANSLATIONS_ROOT / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_ROOT / "translations" / "en.json").read_text())

    assert "no_tracked_devices" in strings["options"]["error"]
    assert "no_tracked_devices" in english["options"]["error"]


async def test_reconfigure_flow_same_host_site_changes_credentials(hass: HomeAssistant) -> None:
    """Test reconfigure with the same site updates credentials successfully."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.1.1",
                "port": 443,
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.unique_id == DEFAULT_SITE_ID


@pytest.mark.parametrize("initial_unique_id", [None, "192.168.1.1_default"])
async def test_reconfigure_flow_matches_stored_site_for_legacy_or_missing_unique_id(
    hass: HomeAssistant, initial_unique_id: str | None
) -> None:
    """Test reconfigure accepts the existing site when unique_id is missing or legacy."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id=initial_unique_id,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "host": "192.168.1.1",
                "port": 443,
                "username": "newadmin",
                "password": "newpass",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.data["password"] == "newpass"


async def test_reauth_confirm_timeout_shows_cannot_connect(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that reauth surfaces login timeouts as cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": config_entry.entry_id},
        data=config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(PATCH_CREATE_CONTROLLER, side_effect=TimeoutError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "username": "admin",
                "password": "new-pass",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


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


async def test_options_flow_active_client_refresh_failure_uses_historical_clients(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test options flow still shows devices when active refresh fails."""
    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active clients unavailable"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_flow_handles_client_fetch_error(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test options flow stays editable and surfaces discovery errors."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("offline")):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["errors"] == {"base": "cannot_discover_devices"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_AWAY_SECONDS: 90,
                CONF_FALLBACK_POLL_INTERVAL: 600,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TRACKED_DEVICES] == ["aa:bb:cc:dd:ee:ff"]


async def test_options_flow_discovery_failure_preserves_validation_error(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test submit validation errors are not masked by discovery failures."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("offline")):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["errors"] == {"base": "cannot_discover_devices"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_TRACKED_DEVICES: [],
                CONF_AWAY_SECONDS: 90,
                CONF_FALLBACK_POLL_INTERVAL: 600,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "no_tracked_devices"}


async def test_options_flow_discovery_failure_without_tracked_devices_aborts(
    hass: HomeAssistant,
) -> None:
    """Test options flow aborts on discovery failure when no tracked devices exist."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options={CONF_TRACKED_DEVICES: []},
    )
    entry.add_to_hass(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=Exception("offline")):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_discover_devices"


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


# ── Client merge / edge-case tests ───────────────────────────────────────


async def test_fetch_all_clients_active_wins_on_key_collision(hass: HomeAssistant) -> None:
    """Test that active client name takes precedence over historical on same MAC."""
    historical = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Old Name")
    active = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Current Name")

    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", historical)],
        clients_items=[("aa:bb:cc:dd:ee:ff", active)],
    )

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"

    # The multi-select should show the active name, not the historical one
    schema = result["data_schema"].schema
    tracked_key = next(k for k in schema if str(k) == CONF_TRACKED_DEVICES)
    options = schema[tracked_key].options
    assert "aa:bb:cc:dd:ee:ff" in options
    assert "Current Name" in options["aa:bb:cc:dd:ee:ff"]


async def test_options_flow_empty_clients_and_empty_tracked_aborts(
    hass: HomeAssistant,
) -> None:
    """Test that options flow aborts when no clients and no tracked MACs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Presence (192.168.1.1)",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options={CONF_TRACKED_DEVICES: [], **{k: v for k, v in MOCK_OPTIONS.items() if k != CONF_TRACKED_DEVICES}},
    )
    entry.add_to_hass(hass)

    # Client discovery returns nothing, and there are no currently tracked MACs
    controller = _mock_controller(clients_all_items=[], clients_items=[])
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_discovered"


async def test_user_step_both_updates_fail_but_cached_data_proceeds(hass: HomeAssistant) -> None:
    """Test that setup proceeds when both update() calls fail but stores have cached data."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Cached Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients_all.update = AsyncMock(side_effect=Exception("historical fetch failed"))
    controller.clients.update = AsyncMock(side_effect=Exception("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
