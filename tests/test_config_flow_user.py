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

from custom_components.unifi_presence.config_flow import UnifiPresenceConfigFlow
from custom_components.unifi_presence.const import (
    CONF_SITE,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import (
    DEFAULT_SITE_ID,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    USER_STEP_INPUT,
    _get_tracked_device_options,
    _get_tracked_device_selector,
    _make_mock_client,
    _make_mock_site,
    _mock_controller,
    add_mock_config_entry,
    async_configure_flow_step,
    async_run_user_step,
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


# ── User step: authentication errors ─────────────────────────────────────


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """Test that the user step shows the credential form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.parametrize(
    ("side_effect", "expected_error"),
    [
        (aiounifi.LoginRequired, "invalid_auth"),
        (aiounifi.Unauthorized, "invalid_auth"),
        (aiounifi.AiounifiException, "cannot_connect"),
        (aiohttp.ClientError("offline"), "cannot_connect"),
        (TimeoutError, "cannot_connect"),
        (Exception("boom"), "unknown"),
    ],
)
async def test_user_step_controller_errors(
    hass: HomeAssistant,
    side_effect: object,
    expected_error: str,
) -> None:
    """Test controller creation errors map to the expected user-step form error."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=side_effect):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


# ── User step: client discovery ──────────────────────────────────────────


async def test_user_step_client_fetch_failure_shows_discovery_error(hass: HomeAssistant) -> None:
    """Test that setup shows a discovery error if both client sources fail after login."""
    site_controller = _mock_controller(clients_all_items=[])
    client_controller = _mock_controller(clients_all_items=[])
    client_controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical fetch failed"))
    client_controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"
    assert result["description_placeholders"] == {"site": "Home"}
    assert result["errors"] == {"base": "cannot_discover_devices"}


async def test_user_step_success_goes_to_devices(hass: HomeAssistant) -> None:
    """Test successful login proceeds to device selection."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


async def test_user_step_single_site_fetches_clients_with_selected_site(hass: HomeAssistant) -> None:
    """Test single-site setup fetches clients with the selected UniFi site."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    site_controller = _mock_controller()
    client_controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]) as create_controller:
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert create_controller.await_count == 2
    assert create_controller.await_args_list[0].args[1].site == ""
    assert create_controller.await_args_list[1].args[1].site == "default"


async def test_user_step_single_site_retries_client_discovery_on_resubmit(hass: HomeAssistant) -> None:
    """Test single-site client discovery errors are retried on the next submit."""
    site_controller = _mock_controller(clients_all_items=[])
    failed_client_controller = _mock_controller(clients_all_items=[])
    failed_client_controller.clients_all.update = AsyncMock(
        side_effect=aiounifi.AiounifiException("historical clients unavailable")
    )
    failed_client_controller.clients.update = AsyncMock(
        side_effect=aiounifi.AiounifiException("active clients unavailable")
    )

    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    retry_controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(
        PATCH_CREATE_CONTROLLER,
        side_effect=[site_controller, failed_client_controller, retry_controller],
    ) as create_controller:
        result = await async_run_user_step(hass, USER_STEP_INPUT)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "single_site_retry"
        assert result["errors"] == {"base": "cannot_discover_devices"}

        result = await async_configure_flow_step(hass, result, {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert create_controller.await_count == 3


async def test_user_step_no_clients_available_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts with the clearer no-clients reason."""
    site_controller = _mock_controller()
    client_controller = _mock_controller(clients_all_items=[])

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_clients_available"


async def test_user_step_site_fetch_failure_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that failing to fetch sites returns to the user step with an error."""
    controller = _mock_controller()
    controller.sites.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_no_sites_available_aborts(hass: HomeAssistant) -> None:
    """Test that setup aborts when the account has no accessible UniFi sites."""
    controller = _mock_controller(sites=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_user_step_both_updates_fail_but_cached_data_proceeds(hass: HomeAssistant) -> None:
    """Test that setup proceeds when both update() calls fail but stores have cached data."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Cached Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    controller.clients_all.update = AsyncMock(side_effect=aiounifi.AiounifiException("historical fetch failed"))
    controller.clients.update = AsyncMock(side_effect=aiounifi.AiounifiException("active fetch failed"))

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)

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
        site = args[1].site
        if site == "default":
            raise aiounifi.Unauthorized
        return controller

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_create_controller_side_effect) as mock_create_controller:
        result = await async_run_user_step(hass, USER_STEP_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert not any(call.args[1].site == "default" for call in mock_create_controller.call_args_list)


@pytest.mark.parametrize("site_value", ["missing-site", 123])
async def test_site_selection_invalid_site_value_shows_user_error(
    hass: HomeAssistant,
    site_value: object,
) -> None:
    """Test invalid site selections return a user-facing validation error."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_sites = {
        DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
        OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
    }

    result = await flow.async_step_site({CONF_SITE: site_value})

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {"site": OFFICE_SITE_ID})
        result = await async_configure_flow_step(hass, result, {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]})

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]})

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home (192.168.1.1)"
    assert result["data"][CONF_HOST] == "192.168.1.1"
    assert result["data"]["site"] == "default"
    assert result["result"].unique_id == DEFAULT_SITE_ID
    assert "aa:bb:cc:dd:ee:ff" in result["options"][CONF_TRACKED_DEVICES]


async def test_devices_step_no_devices(hass: HomeAssistant) -> None:
    """Test that submitting with no devices shows an error."""
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


async def test_devices_step_without_available_clients_aborts(hass: HomeAssistant) -> None:
    """Test the devices step aborts when no clients are loaded."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass

    result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_clients_available"


async def test_already_configured_abort(hass: HomeAssistant) -> None:
    """Test that duplicate site setup aborts even with a different host alias."""
    config_entry = add_mock_config_entry(hass)
    assert config_entry.unique_id == DEFAULT_SITE_ID
    client1 = _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone")
    controller = _mock_controller(clients_all_items=[("aa:bb:cc:dd:ee:ff", client1)])
    alias_config = {**USER_STEP_INPUT, CONF_HOST: "controller.example.com"}

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, alias_config)

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
        result = await async_run_user_step(hass, USER_STEP_INPUT)

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
    flow._async_load_selected_site_clients = AsyncMock(return_value="cannot_discover_devices")
    flow.context = {}
    flow._site_id = DEFAULT_SITE_ID

    result = await flow.async_step_single_site_retry({})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "single_site_retry"
    assert result["errors"] == {"base": "cannot_discover_devices"}


async def test_finish_multi_site_user_selection_returns_site_error(hass: HomeAssistant) -> None:
    """Test multi-site client discovery errors return to the site form."""
    site_controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, TimeoutError]):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {CONF_SITE: DEFAULT_SITE_ID})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_finish_multi_site_user_selection_aborts_without_clients(hass: HomeAssistant) -> None:
    """Test multi-site setup aborts when client discovery succeeds but finds nothing."""
    site_controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )
    client_controller = _mock_controller(clients_all_items=[], clients_items=[])

    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        result = await async_configure_flow_step(hass, result, {CONF_SITE: DEFAULT_SITE_ID})

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
