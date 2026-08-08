"""Tests for the UniFi Presence config flow — reconfigure flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import aiounifi
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.config_flow import (
    UnifiPresenceConfigFlow,
    _async_migrate_tracker_unique_ids,
    _find_reconfigure_site,
)
from custom_components.unifi_presence.const import (
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import (
    DEFAULT_SITE_ID,
    MOCK_CONFIG_DATA,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    _make_mock_site,
    _mock_controller,
    _site_arg_from_call,
    add_mock_config_entry,
    async_run_reconfigure_step,
    make_reconfigure_input,
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


def _make_reconfigure_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a standard entry for reconfigure flow tests."""
    return add_mock_config_entry(hass)


@pytest.mark.parametrize(
    ("sites", "stored_site"),
    [
        ({DEFAULT_SITE_ID: _make_mock_site(DEFAULT_SITE_ID, "default", "Home")}, 123),
        ({OFFICE_SITE_ID: _make_mock_site(OFFICE_SITE_ID, "office", "Office")}, "default"),
    ],
)
def test_find_reconfigure_site_returns_none_for_invalid_current_site(
    sites: dict[str, MagicMock],
    stored_site: object,
) -> None:
    """Test reconfigure site lookup returns None for invalid stored/current sites."""
    assert _find_reconfigure_site(sites, entry_unique_id=None, stored_site=stored_site) is None


# ── Reconfigure flow: success paths ──────────────────────────────────────


async def test_reconfigure_flow_success(hass: HomeAssistant) -> None:
    """Test that reconfigure flow updates credentials and reloads."""
    entry = _make_reconfigure_entry(hass)

    new_data = make_reconfigure_input(
        host="10.0.0.1",
        port=8443,
        username="newadmin",
        password="newpass",
        ssl_verify=True,
    )

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, new_data)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "10.0.0.1"
    assert entry.data["port"] == 8443
    assert entry.data["site"] == "default"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.data["ssl_verify"] is True
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.title == "Home (10.0.0.1)"


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
        site = _site_arg_from_call(args, kwargs)
        if site == "default":
            raise aiounifi.Unauthorized
        return controller

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_create_controller_side_effect) as mock_create_controller:
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(host="10.0.0.1", port=8443, username="officeadmin", password="newpass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["site"] == "office"
    assert entry.unique_id == OFFICE_SITE_ID
    first_call = mock_create_controller.call_args_list[0]
    assert _site_arg_from_call(first_call.args, first_call.kwargs) == "office"


# ── Reconfigure flow: error paths ────────────────────────────────────────


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
async def test_reconfigure_flow_controller_errors(
    hass: HomeAssistant,
    side_effect: object,
    expected_error: str,
) -> None:
    """Test reconfigure controller errors map to the expected form error."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=side_effect):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


# ── Reconfigure flow: site handling ──────────────────────────────────────


async def test_reconfigure_flow_uses_existing_site_without_showing_picker(hass: HomeAssistant) -> None:
    """Test that reconfigure validates the existing site without exposing a picker."""
    entry = _make_reconfigure_entry(hass)

    controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(host="10.0.0.1", username="newadmin", password="newpass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID


async def test_reconfigure_flow_aborts_when_existing_site_is_no_longer_accessible(hass: HomeAssistant) -> None:
    """Test reconfigure aborts if the updated credentials cannot access the current site."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller(sites=[_make_mock_site(OFFICE_SITE_ID, "office", "Office")])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "different_site_selected"


async def test_reconfigure_flow_site_fetch_failure_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that reconfigure returns an error if the site list cannot be loaded."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller()
    controller.sites.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_no_sites_available_aborts(hass: HomeAssistant) -> None:
    """Test that reconfigure aborts when the account has no accessible UniFi sites."""
    entry = _make_reconfigure_entry(hass)
    controller = _mock_controller(sites=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

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
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_discover_devices"}


async def test_reconfigure_flow_second_login_failure_returns_to_reconfigure_form(hass: HomeAssistant) -> None:
    """Test reconfigure shows a form error when existing-site validation fails."""
    entry = _make_reconfigure_entry(hass)
    site_list_controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )

    with patch(
        PATCH_CREATE_CONTROLLER,
        side_effect=[site_list_controller, aiounifi.LoginRequired],
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_load_selected_site_clients_returns_login_error(hass: HomeAssistant) -> None:
    """Test selected-site refresh returns a login error without discovery."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._async_validate_login = AsyncMock(return_value=(None, "cannot_connect"))

    assert await flow._async_load_selected_site_clients(log_context="UniFi site client discovery") == "cannot_connect"


# ── Reconfigure flow: legacy / migration ─────────────────────────────────


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
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(username="newadmin", password="newpass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.data["password"] == "newpass"


async def test_reconfigure_flow_migrates_legacy_tracker_entity_unique_ids(hass: HomeAssistant) -> None:
    """Test reconfigure updates entity-registry tracker IDs for legacy entries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=MOCK_CONFIG_DATA,
        unique_id="192.168.1.1_default",
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    entity_registry = er.async_get(hass)
    legacy_entity = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        "192.168.1.1_default-aa:bb:cc:dd:ee:ff",
        config_entry=entry,
        suggested_object_id="dan_phone",
    )

    controller = _mock_controller()
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(username="newadmin", password="newpass"),
        )

    migrated_entity = entity_registry.async_get(legacy_entity.entity_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert migrated_entity is not None
    assert migrated_entity.unique_id == f"{DEFAULT_SITE_ID}-aa:bb:cc:dd:ee:ff"
    assert (
        entity_registry.async_get_entity_id(
            "device_tracker",
            DOMAIN,
            "192.168.1.1_default-aa:bb:cc:dd:ee:ff",
        )
        is None
    )


async def test_migrate_tracker_unique_ids_skips_non_matching_entities(hass: HomeAssistant) -> None:
    """Test tracker migration leaves unrelated entity IDs unchanged."""
    entry = _make_reconfigure_entry(hass)
    entity_registry = er.async_get(hass)
    entity = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{DEFAULT_SITE_ID}-11:22:33:44:55:66",
        config_entry=entry,
        suggested_object_id="other_phone",
    )

    _async_migrate_tracker_unique_ids(
        hass,
        entry,
        old_site_id="192.168.1.1_default",
        new_site_id=DEFAULT_SITE_ID,
    )

    unchanged = entity_registry.async_get(entity.entity_id)
    assert unchanged is not None
    assert unchanged.unique_id == f"{DEFAULT_SITE_ID}-11:22:33:44:55:66"


@pytest.mark.parametrize("initial_unique_id", [None, "192.168.1.1_default"])
async def test_reconfigure_flow_recovers_legacy_site_identity_when_single_site_is_accessible(
    hass: HomeAssistant, initial_unique_id: str | None
) -> None:
    """Test reconfigure recovers legacy entries when exactly one site is accessible."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={**MOCK_CONFIG_DATA, "site": "stale-site-token"},
        unique_id=initial_unique_id,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    controller = _mock_controller(sites=[_make_mock_site(DEFAULT_SITE_ID, "default", "Home")])
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(host="10.0.0.1", username="newadmin", password="newpass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.data["site"] == "default"


@pytest.mark.parametrize("initial_unique_id", [None, "192.168.1.1_default"])
async def test_reconfigure_flow_does_not_guess_legacy_site_identity_with_multiple_sites(
    hass: HomeAssistant, initial_unique_id: str | None
) -> None:
    """Test reconfigure keeps aborting when multiple sites fit a legacy recovery."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={**MOCK_CONFIG_DATA, "site": "stale-site-token"},
        unique_id=initial_unique_id,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(host="10.0.0.1", username="newadmin", password="newpass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "different_site_selected"
