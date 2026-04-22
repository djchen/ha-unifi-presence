"""Tests for the UniFi Presence config flow — options flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import SelectSelectorMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

from .conftest import (
    DEFAULT_SITE_ID,
    MOCK_CONFIG_DATA,
    MOCK_OPTIONS,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    _get_tracked_device_options,
    _get_tracked_device_selector,
    _make_mock_client,
    _mock_controller,
    _site_arg_from_call,
)

TRANSLATIONS_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "unifi_presence"

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


# ── Options flow ─────────────────────────────────────────────────────────


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
    selector = _get_tracked_device_selector(result)
    assert selector.config["multiple"] is True
    assert selector.config["mode"] == SelectSelectorMode.DROPDOWN

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


async def test_options_flow_fallback_login_normalizes_legacy_stored_site_id(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test options fallback login resolves stored site IDs to the short site name."""
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, "site": OFFICE_SITE_ID},
        unique_id="192.168.1.1_office",
    )

    client_controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Dan Phone"))]
    )

    with (
        patch(
            "custom_components.unifi_presence.config_flow.create_controller_with_resolved_site",
            return_value=(client_controller, "office"),
        ) as create_controller_with_resolved_site,
    ):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert (
        _site_arg_from_call(
            create_controller_with_resolved_site.await_args.args,
            create_controller_with_resolved_site.await_args.kwargs,
        )
        == OFFICE_SITE_ID
    )


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


# ── Translation integrity ────────────────────────────────────────────────


def test_options_flow_no_tracked_devices_translation_keys_exist() -> None:
    """Test the dedicated options validation key exists in both translation files."""
    strings = json.loads((TRANSLATIONS_ROOT / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_ROOT / "translations" / "en.json").read_text())

    assert "no_tracked_devices" in strings["options"]["error"]
    assert "no_tracked_devices" in english["options"]["error"]


def test_removed_translation_keys_stay_absent_and_aligned() -> None:
    """Test stale translation keys were removed from both translation files."""
    strings = json.loads((TRANSLATIONS_ROOT / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_ROOT / "translations" / "en.json").read_text())

    assert "already_configured" not in strings["config"]["error"]
    assert "already_configured" not in english["config"]["error"]
    assert "no_devices" not in strings["options"]["error"]
    assert "no_devices" not in english["options"]["error"]
    assert "entity" not in strings
    assert "entity" not in english


def test_device_picker_copy_mentions_known_clients_and_secure_ssl_default() -> None:
    """Test setup copy matches historical-client behavior and secure SSL defaults."""
    strings = json.loads((TRANSLATIONS_ROOT / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_ROOT / "translations" / "en.json").read_text())

    for doc in (strings, english):
        assert doc["config"]["step"]["devices"]["data"]["tracked_devices"] == "Known devices"
        assert "including previously seen devices" in doc["config"]["step"]["devices"]["description"]
        assert (
            "Leave enabled unless your controller uses a self-signed"
            in doc["config"]["step"]["user"]["data_description"]["ssl_verify"]
        )
        assert (
            doc["options"]["error"]["cannot_discover_devices"]
            == "Could not refresh the client list; previously tracked devices may still be available"
        )
        assert doc["system_health"]["info"]["devices_with_active_away_timers"] == "Devices within away timeout"
