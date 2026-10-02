"""Tests for the UniFi Presence config flow — reconfigure flow."""

import asyncio
from collections.abc import AsyncGenerator
from copy import deepcopy
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiounifi
import pytest
import voluptuous as vol
from homeassistant import config_entries
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
from custom_components.unifi_presence.helpers import ControllerConnectionParams

from .conftest import (
    DEFAULT_SITE_ID,
    MOCK_CONFIG_DATA,
    MOCK_OPTIONS,
    OFFICE_SITE_ID,
    PATCH_CREATE_CONTROLLER,
    _assert_session_cleanup,
    _attach_mock_session,
    _make_mock_client,
    _make_mock_site,
    _mock_controller,
    add_mock_config_entry,
    async_run_reconfigure_step,
    make_reconfigure_input,
)

pytestmark = pytest.mark.usefixtures("_bypass_setup")


@pytest.fixture
async def unchanged_reconfigure_flow(hass: HomeAssistant) -> AsyncGenerator[UnifiPresenceConfigFlow]:
    """Guard migration and persistence using a legacy entry that would need migration."""
    entry = add_mock_config_entry(hass, unique_id="192.168.1.1_default", options=deepcopy(MOCK_OPTIONS))
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        "192.168.1.1_default-aa:bb:cc:dd:ee:ff",
        config_entry=entry,
        suggested_object_id="legacy_phone",
    )
    original_entry = deepcopy((dict(entry.data), dict(entry.options), entry.title, entry.unique_id))
    original_entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow.context = {"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    with (
        patch(
            "custom_components.unifi_presence.config_flow._async_migrate_tracker_unique_ids",
            wraps=_async_migrate_tracker_unique_ids,
        ) as migrate,
        patch.object(flow, "async_update_reload_and_abort", wraps=flow.async_update_reload_and_abort) as persist,
    ):
        yield flow
        await hass.async_block_till_done()
        assert (dict(entry.data), dict(entry.options), entry.title, entry.unique_id) == original_entry
        assert er.async_entries_for_config_entry(registry, entry.entry_id) == original_entities
        migrate.assert_not_called()
        persist.assert_not_called()


def _assert_reconfigure_error(result: dict[str, Any], expected_error: str) -> None:
    """Check error routing and submitted defaults without exposing either password."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": expected_error}
    fields = {str(key): key for key in result["data_schema"].schema}
    for name, value in {"host": "10.0.0.1", "port": 8443, "username": "newadmin", "ssl_verify": False}.items():
        assert fields[name].default() == value
    assert isinstance(fields["password"], vol.Required)
    assert fields["password"].default is vol.UNDEFINED


def _validation_input() -> dict[str, object]:
    """Return distinct submitted values for selected-site validation tests."""
    return make_reconfigure_input(host="10.0.0.1", port=8443, username="newadmin", password="newpass", ssl_verify=False)


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


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.parametrize("ssl_verify", [False, True], ids=["owned", "shared"])
async def test_reconfigure_flow_success(hass: HomeAssistant, populated: bool, ssl_verify: bool) -> None:
    """Reconfigure accepts empty/populated discovery and preserves all tracked clients."""
    entry = add_mock_config_entry(hass, options=deepcopy(MOCK_OPTIONS))
    original_options = deepcopy(dict(entry.options))
    registry = er.async_get(hass)
    entities = [
        registry.async_get_or_create("device_tracker", DOMAIN, f"{DEFAULT_SITE_ID}-{mac}", config_entry=entry)
        for mac in entry.options[CONF_TRACKED_DEVICES]
    ]

    new_data = make_reconfigure_input(
        host="10.0.0.1",
        port=8443,
        username="newadmin",
        password="newpass",
        ssl_verify=ssl_verify,
    )

    site_controller = _mock_controller()
    mac = "aa:bb:cc:dd:ee:ff"
    client_controller = _mock_controller(
        clients_all_items=[(mac, _make_mock_client(mac, name="Phone"))] if populated else []
    )
    site_session = _attach_mock_session(site_controller, owned=not ssl_verify)
    client_session = _attach_mock_session(client_controller, owned=not ssl_verify)
    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]) as create_controller:
        result = await async_run_reconfigure_step(hass, entry, new_data)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "10.0.0.1"
    assert entry.data["port"] == 8443
    assert entry.data["site"] == "default"
    assert entry.data["username"] == "newadmin"
    assert entry.data["password"] == "newpass"
    assert entry.data["ssl_verify"] is ssl_verify
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.title == "Home (10.0.0.1)"
    assert dict(entry.options) == original_options
    assert [registry.async_get(entity.entity_id) for entity in entities] == entities
    assert create_controller.await_count == 2
    for acquisition in create_controller.await_args_list:
        assert acquisition.args == (hass, ControllerConnectionParams(**new_data, site="default"))
        assert acquisition.kwargs == {"unique_id": None, "resolve_legacy_site": False}
    site_controller.sites.update.assert_awaited_once_with()
    site_controller.clients.update_mock.assert_not_awaited()
    site_controller.clients_all.update_mock.assert_not_awaited()
    client_controller.sites.update.assert_not_awaited()
    client_controller.clients.update_mock.assert_awaited_once_with()
    client_controller.clients_all.update_mock.assert_awaited_once_with()
    _assert_session_cleanup(site_session, owned=not ssl_verify)
    _assert_session_cleanup(client_session, owned=not ssl_verify)


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
        site = args[1].site
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
    assert first_call.args[1].site == "office"


# ── Reconfigure flow: site handling ──────────────────────────────────────


async def test_reconfigure_flow_uses_existing_site_without_showing_picker(hass: HomeAssistant) -> None:
    """Test that reconfigure validates the existing site without exposing a picker."""
    entry = add_mock_config_entry(hass)

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
    entry = add_mock_config_entry(hass)
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
    entry = add_mock_config_entry(hass)
    controller = _mock_controller()
    controller.sites.update = AsyncMock(side_effect=aiounifi.AiounifiException)

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_no_sites_available_aborts(hass: HomeAssistant) -> None:
    """Test that reconfigure aborts when the account has no accessible UniFi sites."""
    entry = add_mock_config_entry(hass)
    controller = _mock_controller(sites=[])

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites_available"


async def test_reconfigure_flow_same_site_requires_site_access(
    unchanged_reconfigure_flow: UnifiPresenceConfigFlow,
) -> None:
    """Test that reconfigure validates site-scoped client access before saving."""
    controller = _mock_controller()
    site_controller = _mock_controller(clients_all_items=[], clients_items=[])
    site_controller.clients.update_mock.side_effect = aiounifi.AiounifiException
    site_controller.clients_all.update_mock.side_effect = aiounifi.AiounifiException
    site_session = _attach_mock_session(controller)
    client_session = _attach_mock_session(site_controller)

    with patch(
        PATCH_CREATE_CONTROLLER,
        side_effect=[controller, site_controller],
    ):
        result = await unchanged_reconfigure_flow.async_step_reconfigure(_validation_input())

    _assert_reconfigure_error(result, "cannot_discover_devices")
    site_controller.clients.update_mock.assert_awaited_once_with()
    site_controller.clients_all.update_mock.assert_awaited_once_with()
    _assert_session_cleanup(site_session)
    _assert_session_cleanup(client_session)


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (aiounifi.LoginRequired(), "invalid_auth"),
        (aiounifi.AiounifiException(), "cannot_connect"),
        (RuntimeError("unexpected acquisition failure"), "unknown"),
    ],
)
async def test_reconfigure_flow_second_login_failure_returns_to_reconfigure_form(
    unchanged_reconfigure_flow: UnifiPresenceConfigFlow,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_error: str,
) -> None:
    """Test reconfigure shows a form error when existing-site validation fails."""
    site_list_controller = _mock_controller(
        sites=[
            _make_mock_site(DEFAULT_SITE_ID, "default", "Home"),
            _make_mock_site(OFFICE_SITE_ID, "office", "Office"),
        ]
    )
    site_session = _attach_mock_session(site_list_controller)

    with (
        patch(PATCH_CREATE_CONTROLLER, side_effect=[site_list_controller, error]) as create_controller,
        patch.object(
            unchanged_reconfigure_flow,
            "_async_discover_clients_from_controller",
            wraps=unchanged_reconfigure_flow._async_discover_clients_from_controller,
        ) as discover,
    ):
        result = await unchanged_reconfigure_flow.async_step_reconfigure(_validation_input())

    _assert_reconfigure_error(result, expected_error)
    assert create_controller.await_count == 2
    discover.assert_not_awaited()
    _assert_session_cleanup(site_session)
    if expected_error == "unknown":
        assert "Unexpected exception during UniFi reconfigure site validation" in caplog.text


async def test_reconfigure_selected_site_acquisition_cancellation(
    unchanged_reconfigure_flow: UnifiPresenceConfigFlow,
) -> None:
    """Cancellation during the second acquisition propagates without changing the entry."""
    controller = _mock_controller()
    session = _attach_mock_session(controller)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _acquire(*args: Any, **kwargs: Any) -> MagicMock:
        if not session.closed:
            return controller
        started.set()
        await release.wait()
        pytest.fail("Selected-site acquisition should have been cancelled")

    with patch(PATCH_CREATE_CONTROLLER, side_effect=_acquire) as create_controller:
        task = asyncio.create_task(unchanged_reconfigure_flow.async_step_reconfigure(_validation_input()))
        try:
            async with asyncio.timeout(5):
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            task.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(task, return_exceptions=True)
    assert create_controller.await_count == 2
    _assert_session_cleanup(session)


async def test_reconfigure_selected_site_discovery_cancellation(
    unchanged_reconfigure_flow: UnifiPresenceConfigFlow,
) -> None:
    """Cancel real concurrent discovery, awaiting both stores and owned-session cleanup."""
    site_controller = _mock_controller()
    client_controller = _mock_controller()
    site_session = _attach_mock_session(site_controller)
    client_session = _attach_mock_session(client_controller)
    started = [asyncio.Event(), asyncio.Event()]
    cancelled = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def _refresh(index: int) -> None:
        started[index].set()
        try:
            await release.wait()
        finally:
            cancelled[index].set()

    client_controller.clients_all.update_mock.side_effect = partial(_refresh, 0)
    client_controller.clients.update_mock.side_effect = partial(_refresh, 1)
    with patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]):
        task = asyncio.create_task(unchanged_reconfigure_flow.async_step_reconfigure(_validation_input()))
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*(event.wait() for event in started))
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            task.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(task, return_exceptions=True)

    assert all(event.is_set() for event in cancelled)
    client_controller.clients_all.update_mock.assert_awaited_once_with()
    client_controller.clients.update_mock.assert_awaited_once_with()
    _assert_session_cleanup(site_session)
    _assert_session_cleanup(client_session)


async def test_reconfigure_selected_site_cleanup_exception(
    unchanged_reconfigure_flow: UnifiPresenceConfigFlow,
) -> None:
    """A cleanup exception propagates before tracker migration or persistence."""
    site_controller = _mock_controller()
    client_controller = _mock_controller()
    site_session = _attach_mock_session(site_controller)
    client_session = _attach_mock_session(client_controller)
    error = RuntimeError("session detachment failed")
    client_session.detach.side_effect = error
    with (
        patch(PATCH_CREATE_CONTROLLER, side_effect=[site_controller, client_controller]),
        pytest.raises(RuntimeError, match="session detachment failed") as raised,
    ):
        await unchanged_reconfigure_flow.async_step_reconfigure(_validation_input())
    assert raised.value is error
    _assert_session_cleanup(site_session)
    client_session.detach.assert_called_once_with()
    assert client_session.closed is False
    client_session.close.assert_not_called()
    client_session.shared_connector.close.assert_not_called()


async def test_load_selected_site_clients_returns_login_error(hass: HomeAssistant) -> None:
    """Test selected-site refresh returns a login error without discovery."""
    flow = UnifiPresenceConfigFlow()
    flow.hass = hass
    flow._available_clients = {"aa:bb:cc:dd:ee:ff": "Stale Phone"}

    async def _acquire(**kwargs: Any) -> tuple[None, str]:
        assert flow._available_clients == {}
        return None, "cannot_connect"

    flow._async_validate_login = AsyncMock(side_effect=_acquire)
    flow._async_discover_clients_from_controller = AsyncMock()

    assert await flow._async_load_selected_site_clients(log_context="UniFi site client discovery") == "cannot_connect"
    assert flow._available_clients == {}
    flow._async_validate_login.assert_awaited_once()
    flow._async_discover_clients_from_controller.assert_not_awaited()


# ── Reconfigure flow: legacy / migration ─────────────────────────────────


@pytest.mark.parametrize("initial_unique_id", [None, "192.168.1.1_default"])
async def test_reconfigure_flow_matches_stored_site_for_legacy_or_missing_unique_id(
    hass: HomeAssistant, initial_unique_id: str | None
) -> None:
    """Test reconfigure accepts the existing site when unique_id is missing or legacy."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={**MOCK_CONFIG_DATA, "site": DEFAULT_SITE_ID},
        unique_id=initial_unique_id,
        options={CONF_TRACKED_DEVICES: ["aa:bb:cc:dd:ee:ff"]},
    )
    entry.add_to_hass(hass)

    controller = _mock_controller()
    client_controller = _mock_controller()
    new_data = make_reconfigure_input(username="newadmin", password="newpass", ssl_verify=False)
    with patch(PATCH_CREATE_CONTROLLER, side_effect=[controller, client_controller]) as create_controller:
        result = await async_run_reconfigure_step(
            hass,
            entry,
            new_data,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == DEFAULT_SITE_ID
    assert entry.data["password"] == "newpass"
    assert entry.data["site"] == "default"
    assert create_controller.await_count == 2
    for acquisition, site in zip(create_controller.await_args_list, [DEFAULT_SITE_ID, "default"], strict=True):
        assert acquisition.args == (hass, ControllerConnectionParams(**new_data, site=site))
        assert acquisition.kwargs == {"unique_id": None, "resolve_legacy_site": False}
    controller.sites.update.assert_awaited_once_with()
    controller.clients.update_mock.assert_not_awaited()
    controller.clients_all.update_mock.assert_not_awaited()
    client_controller.sites.update.assert_not_awaited()
    client_controller.clients.update_mock.assert_awaited_once_with()
    client_controller.clients_all.update_mock.assert_awaited_once_with()


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
    client_controller = _mock_controller()
    site_session = _attach_mock_session(controller)
    client_session = _attach_mock_session(client_controller)
    operations = MagicMock()
    with (
        patch(PATCH_CREATE_CONTROLLER, side_effect=[controller, client_controller]) as acquire,
        patch(
            "custom_components.unifi_presence.config_flow._async_migrate_tracker_unique_ids",
            wraps=_async_migrate_tracker_unique_ids,
        ) as migrate,
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as persist,
    ):
        for name, mock in [
            ("acquire", acquire),
            ("site_cleanup", site_session.detach),
            ("client_cleanup", client_session.detach),
            ("migration", migrate),
            ("persistence", persist),
        ]:
            operations.attach_mock(mock, name)
        result = await async_run_reconfigure_step(
            hass,
            entry,
            make_reconfigure_input(username="newadmin", password="newpass"),
        )
        assert [call[0] for call in operations.mock_calls] == [
            "acquire",
            "site_cleanup",
            "acquire",
            "client_cleanup",
            "migration",
            "persistence",
        ]
        await hass.async_block_till_done()

    _assert_session_cleanup(site_session)
    _assert_session_cleanup(client_session)
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
    entry = add_mock_config_entry(hass)
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
