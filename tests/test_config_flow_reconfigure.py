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
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


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


# ── Reconfigure flow: success paths ──────────────────────────────────────


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
    first_call = mock_create_controller.call_args_list[0]
    assert _site_arg_from_call(first_call.args, first_call.kwargs) == "office"


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
    assert entry.title == f"Home ({new_host})"


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


# ── Reconfigure flow: error paths ────────────────────────────────────────


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


async def test_reconfigure_flow_client_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """Test that aiohttp transport errors surface as cannot_connect."""
    entry = _make_reconfigure_entry(hass)

    with patch(PATCH_CREATE_CONTROLLER, side_effect=aiohttp.ClientError("offline")):
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

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID


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
    assert result["errors"] == {"base": "invalid_auth"}


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
