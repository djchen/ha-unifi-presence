"""Tests for the UniFi Presence config flow — reauth flow."""

from __future__ import annotations

from unittest.mock import patch

import aiounifi
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import (
    CONF_SITE,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import (
    MOCK_CONFIG_DATA,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    _mock_controller,
    add_mock_config_entry,
    async_run_reauth_confirm_step,
    make_reauth_confirm_input,
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Standard config entry added to hass."""
    return add_mock_config_entry(hass)


# ── Reauth flow ──────────────────────────────────────────────────────────


async def test_reauth_shows_form(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test that the reauth flow shows the credential form."""
    result = await config_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["site"] == "Home"
    assert result["description_placeholders"]["host"] == MOCK_CONFIG_DATA["host"]
    assert "username" in result["data_schema"].schema
    assert "password" in result["data_schema"].schema


async def test_reauth_shows_site_and_host_for_same_controller_different_site(hass: HomeAssistant) -> None:
    """Test that reauth identifies the entry by both site and host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office (192.168.1.1)",
        data={**MOCK_CONFIG_DATA, "site": "office"},
        unique_id=OFFICE_SITE_ID,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["site"] == "Office"
    assert result["description_placeholders"]["host"] == MOCK_CONFIG_DATA["host"]


async def test_reauth_success(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test successful reauthentication updates credentials and reloads."""
    controller = _mock_controller()

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reauth_confirm_step(
            hass,
            config_entry,
            make_reauth_confirm_input(username="new_admin", password="new_pass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["username"] == "new_admin"
    assert config_entry.data["password"] == "new_pass"


async def test_reauth_invalid_auth_shows_form_error(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Test an authentication failure is shown on the reauth form."""
    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiounifi.LoginRequired("bad credentials")):
        result = await async_run_reauth_confirm_step(hass, config_entry, make_reauth_confirm_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_normalizes_legacy_stored_site_id_before_login(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test reauth resolves legacy stored site IDs to the short site name."""
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, "site": OFFICE_SITE_ID},
        unique_id="192.168.1.1_office",
    )

    reauth_controller = _mock_controller()

    with (
        patch(
            "custom_components.unifi_presence.config_flow.create_controller_for_params",
            return_value=reauth_controller,
        ) as create_controller_for_params,
    ):
        result = await async_run_reauth_confirm_step(
            hass,
            config_entry,
            make_reauth_confirm_input(username="new_admin", password="new_pass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert create_controller_for_params.await_args.args[1].site == OFFICE_SITE_ID
    assert create_controller_for_params.await_args.kwargs["resolve_legacy_site"] is True


async def test_reauth_missing_unique_id_uses_stored_short_site_directly(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test missing-unique-id reauth preserves direct short-site login."""
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_SITE: "office"},
        unique_id=None,
    )

    controller = _mock_controller()

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller) as create_controller:
        result = await async_run_reauth_confirm_step(
            hass,
            config_entry,
            make_reauth_confirm_input(username="new_admin", password="new_pass"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert create_controller.await_args.args[1].site == "office"
    assert create_controller.await_args.kwargs["resolve_legacy_site"] is False
