"""Tests for the UniFi Presence config flow — user step, site selection, and device selection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import aiounifi
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import SelectSelectorMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.config_flow import UnifiPresenceConfigFlow
from custom_components.unifi_presence.const import (
    CONF_SITE,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import (
    DEFAULT_SITE_ID,
    MOCK_CONFIG_DATA,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    USER_STEP_INPUT,
    _get_tracked_device_options,
    _get_tracked_device_selector,
    _make_mock_client,
    _make_mock_site,
    _mock_controller,
    _site_arg_from_call,
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


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


# ── User step: authentication errors ─────────────────────────────────────


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


async def test_user_step_client_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that aiohttp transport errors show a connectivity error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiohttp.ClientError("offline")):
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


# ── User step: client discovery ──────────────────────────────────────────


async def test_user_step_client_fetch_failure_shows_discovery_error(hass: HomeAssistant) -> None:
    """Test that setup shows a discovery error if both client sources fail after login."""
    controller = _mock_controller(clients_all_items=[])
    controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical fetch failed"))
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"
    assert result["description_placeholders"] == {"site": "Home"}
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
    selector = _get_tracked_device_selector(result)
    assert selector.config["multiple"] is True
    assert selector.config["mode"] == SelectSelectorMode.DROPDOWN
    assert result["description_placeholders"] == {"client_count": "1"}
    assert _get_tracked_device_options(result) == {"aa:bb:cc:dd:ee:ff": "Dan Phone (aa:bb:cc:dd:ee:ff)"}


async def test_user_step_historical_client_failure_uses_active_clients(
    hass: HomeAssistant,
) -> None:
    """Test that setup still proceeds when historical client refresh fails but active succeeds."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical clients unavailable"))

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


async def test_user_step_single_site_reuses_site_fetch_for_client_discovery(hass: HomeAssistant) -> None:
    """Test single-site setup does not perform a second controller login."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller) as create_controller:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    create_controller.assert_called_once()


async def test_user_step_single_site_retries_client_discovery_on_resubmit(hass: HomeAssistant) -> None:
    """Test single-site client discovery errors are retried on the next submit."""
    controller = _mock_controller(clients_all_items=[])
    controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical clients unavailable"))
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active clients unavailable"))

    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    retry_controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[controller, retry_controller]) as create_controller:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "single_site_retry"
        assert result["errors"] == {"base": "cannot_discover_devices"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert create_controller.await_count == 2


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


async def test_user_step_both_updates_fail_but_cached_data_proceeds(hass: HomeAssistant) -> None:
    """Test that setup proceeds when both update() calls fail but stores have cached data."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Cached Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical fetch failed"))
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=USER_STEP_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


# ── Site selection ───────────────────────────────────────────────────────


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
        site = _site_arg_from_call(args, kwargs)
        if site == "default":
            raise aiounifi.Unauthorized
        return controller

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_create_controller_side_effect) as mock_create_controller:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert not any(
        _site_arg_from_call(call.args, call.kwargs) == "default" for call in mock_create_controller.call_args_list
    )


async def test_site_selection_stale_site_id_shows_user_error(hass: HomeAssistant) -> None:
    """Test stale site selections return to the site step with an explicit error."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {
        DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
        OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
    }

    result = await flow.async_step_site({CONF_SITE: "missing-site"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "invalid_site"}


async def test_site_selection_malformed_site_value_shows_user_error(hass: HomeAssistant) -> None:
    """Test malformed site selections return a user-facing validation error."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {
        DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
        OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
    }

    result = await flow.async_step_site({CONF_SITE: 123})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "invalid_site"}


async def test_site_selection_without_available_sites_aborts(hass: HomeAssistant) -> None:
    """Test the site step aborts when the available site list is empty."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass

    result = await flow.async_step_site()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_site_selection_reconfigure_target_uses_reconfigure_handler(hass: HomeAssistant) -> None:
    """Test valid site submission uses the reconfigure completion path."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._site_step_target = "reconfigure"
    flow._available_sites = {DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home")}
    flow._async_finish_reconfigure_site_selection = AsyncMock(
        return_value={"type": FlowResultType.ABORT, "reason": "reconfigure_successful"}
    )

    result = await flow.async_step_site({CONF_SITE: DEFAULT_SITE_ID})

    assert result == {"type": FlowResultType.ABORT, "reason": "reconfigure_successful"}


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
    assert result["title"] == "Office (192.168.1.1)"
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
    assert result["title"] == "office (192.168.1.1)"
    assert result["data"]["site"] == "office"
    assert result["result"].unique_id == OFFICE_SITE_ID


# ── Device selection ─────────────────────────────────────────────────────


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
    assert result["title"] == "Home (192.168.1.1)"
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
    assert result["title"] == f"Home ({host})"
    assert result["data"][CONF_HOST] == host
    assert result["result"].unique_id == expected_unique_id


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


async def test_devices_step_without_available_clients_aborts(hass: HomeAssistant) -> None:
    """Test the devices step aborts when no clients are loaded."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass

    result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_clients_available"


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


# ── Client merge / edge-case tests ──────────────────────────────────────


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
    options = _get_tracked_device_options(result)
    assert "aa:bb:cc:dd:ee:ff" in options
    assert "Current Name" in options["aa:bb:cc:dd:ee:ff"]


async def test_finish_single_site_user_selection_keeps_retry_form_on_repeat_failure(hass: HomeAssistant) -> None:
    """Test repeated single-site discovery failures keep the dedicated retry form."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home")}
    flow._site = "default"
    flow._site_title = "Home"
    flow._single_site_discovery_error = "cannot_discover_devices"
    flow._async_load_selected_site_clients = AsyncMock(return_value="cannot_discover_devices")

    result = await flow._async_finish_single_site_user_selection()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"
    assert result["errors"] == {"base": "cannot_discover_devices"}


async def test_finish_multi_site_user_selection_returns_site_error(hass: HomeAssistant) -> None:
    """Test multi-site client discovery errors return to the site form."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {
        DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
        OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
    }
    flow._async_load_selected_site_clients = AsyncMock(return_value="cannot_connect")

    result = await flow._async_finish_multi_site_user_selection()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_finish_multi_site_user_selection_aborts_without_clients(hass: HomeAssistant) -> None:
    """Test multi-site setup aborts when client discovery succeeds but finds nothing."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_clients = {}
    flow._async_load_selected_site_clients = AsyncMock(return_value=None)

    result = await flow._async_finish_multi_site_user_selection()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_clients_available"


async def test_single_site_retry_without_sites_aborts(hass: HomeAssistant) -> None:
    """Test single-site retry aborts when the flow no longer has site data."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass

    result = await flow.async_step_single_site_retry({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_single_site_retry_with_multiple_sites_returns_site_step(hass: HomeAssistant) -> None:
    """Test single-site retry falls back to the site step when multiple sites exist."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {
        DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
        OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
    }

    result = await flow.async_step_single_site_retry({})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"


async def test_single_site_retry_without_input_shows_retry_form(hass: HomeAssistant) -> None:
    """Test single-site retry shows the retry form before resubmission."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home")}
    flow._site = "default"
    flow._site_title = "Home"

    result = await flow.async_step_single_site_retry()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"


async def test_single_site_retry_keeps_form_when_retry_fails(hass: HomeAssistant) -> None:
    """Test single-site retry keeps the retry form when the next refresh still fails."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home")}
    flow._site = "default"
    flow._site_title = "Home"
    flow._async_load_selected_site_clients = AsyncMock(return_value="cannot_discover_devices")

    result = await flow.async_step_single_site_retry({})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"
    assert result["errors"] == {"base": "cannot_discover_devices"}
