"""Real flow-manager and deferred-reload contracts for Network 11 migration."""

from unittest.mock import patch

import aiounifi
import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState, OperationNotAllowed
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_test_home_assistant

from custom_components.unifi_presence.const import CONF_SITE_ID_MIGRATION, CONF_SSL_VERIFY, CONF_TRACKED_DEVICES, DOMAIN

from .conftest import (
    MOCK_CONFIG_DATA,
    PATCH_CREATE_CONTROLLER,
    USER_STEP_INPUT,
    _make_mock_client,
    _make_mock_site,
    add_mock_config_entry,
    assert_no_reconfigure_mutations,
    async_configure_flow_step,
    async_run_reauth_confirm_step,
    async_run_reconfigure_step,
    async_run_user_step,
    make_mock_controller,
    make_reconfigure_input,
)
from .site_identity_helpers import MAC, NEW_ID, OLD_ID, OTHER_ID, PATCH_CONTROLLER, network_11_controller

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def flow_controller(*, multi=False):
    """Return current response sites and discoverable clients."""
    sites = [_make_mock_site(NEW_ID, "default")]
    if multi:
        sites.append(_make_mock_site(OTHER_ID, "office"))
    return make_mock_controller(sites=sites, clients_items=[(MAC, _make_mock_client(MAC))])


async def test_reconfigure_saves_settings_without_identity_commit(hass: HomeAssistant):
    """The confirmation is settings-first; only subsequent setup commits identity."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule,
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input(ssl_verify=True))
    assert result["reason"] == "settings_saved_reload_scheduled"
    assert entry.unique_id == OLD_ID
    assert registry.async_get(record.entity_id) is record
    assert entry.data[CONF_PASSWORD] == "new-pass"
    assert entry.data[CONF_SSL_VERIFY] is True
    schedule.assert_called_once_with(entry.entry_id)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.unique_id == NEW_ID
    assert registry.async_get(record.entity_id).id == record.id


@pytest.mark.parametrize("overrides", [{CONF_HOST: "alias"}, {CONF_PORT: 8443}])
async def test_reconfigure_changed_endpoint_rejected(hass: HomeAssistant, overrides):
    """Only exact stored endpoint values permit the restricted heuristic."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    with patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()), assert_no_reconfigure_mutations(hass, entry):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input(**overrides))
    assert result["reason"] == "different_site_selected"


@pytest.mark.parametrize("conflict", ["entry", "entity"])
async def test_reconfigure_read_only_conflicts_save_nothing(hass: HomeAssistant, conflict):
    """A known conflict cannot even save connection settings."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    if conflict == "entry":
        add_mock_config_entry(hass, unique_id=NEW_ID)
    else:
        registry.async_get_or_create("device_tracker", DOMAIN, f"{NEW_ID}-{MAC}", config_entry=entry)
    with patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()), assert_no_reconfigure_mutations(hass, entry):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
    assert result["reason"] == "site_identity_conflict"


@pytest.mark.parametrize("stage", ["sites", "clients"])
@pytest.mark.parametrize("failure", [False, True])
async def test_reconfigure_rejects_changed_configuration(hass: HomeAssistant, stage, failure):
    """Each submitted attempt owns a copied snapshot across all validation awaits."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = flow_controller()

    async def change(*_args):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PASSWORD: "concurrent"})
        if failure:
            raise TimeoutError
        return {"data": [{"_id": NEW_ID, "name": "default"}]}

    if stage == "sites":
        controller.request.side_effect = change
    else:
        controller.clients.update_mock.side_effect = change
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
    assert result["reason"] == "configuration_changed"
    assert entry.unique_id == OLD_ID
    assert entry.data[CONF_PASSWORD] == "concurrent"
    reload.assert_not_called()


async def test_reconfigure_retry_does_not_reuse_deferred_branch(hass: HomeAssistant):
    """A failed discovery and changed endpoint cannot leak a previous candidate."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = flow_controller()
    controller.clients.update_mock.side_effect = RuntimeError("discovery")
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
        assert result["errors"] == {"base": "cannot_discover_devices"}
        controller.clients.update_mock.side_effect = None
        result = await async_configure_flow_step(hass, result, make_reconfigure_input(host="other"))
    assert result["reason"] == "different_site_selected"
    assert entry.unique_id == OLD_ID
    assert entry.data[CONF_PASSWORD] != "new-pass"


@pytest.mark.parametrize("unload_error", [False, True])
async def test_failed_real_unload_never_commits_and_requires_restart(hass: HomeAssistant, unload_error):
    """HA FAILED_UNLOAD cannot be recovered by a second reload; live IDs stay old."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()
    controller.request.return_value = {"data": [{"_id": OLD_ID, "name": "default"}]}
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}")
        tracker = hass.data["entity_components"]["device_tracker"].get_entity(entity_id)
        controller.request.reset_mock()
        with (
            patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()),
            patch.object(
                hass.config_entries,
                "async_unload_platforms",
                return_value=False,
                side_effect=RuntimeError("unload") if unload_error else None,
            ),
        ):
            result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
            await hass.async_block_till_done()
        assert result["reason"] == "settings_saved_reload_scheduled"
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert not coordinator._stopping
        assert entry.unique_id == coordinator.site_id == OLD_ID
        assert registry.async_get(entity_id).unique_id == tracker.unique_id == f"{OLD_ID}-{MAC}"
        controller.request.assert_not_awaited()
        with pytest.raises(OperationNotAllowed):
            await hass.config_entries.async_reload(entry.entry_id)
        with patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()):
            duplicate = await async_run_user_step(hass, USER_STEP_INPUT)
        assert duplicate["reason"] == "site_identity_pending"
        assert hass.config_entries.async_get_entry(entry.entry_id) is entry
        # Surviving runtime recreation intentionally reads the saved credentials.
        await coordinator._async_close_runtime_controller()
        with patch(PATCH_CONTROLLER, return_value=controller) as create:
            await coordinator.async_refresh()
        assert create.call_args.args[1].password == "new-pass"
        assert entry.unique_id == OLD_ID
        # A FAILED_UNLOAD entry is not part of the fixture's normal unload set.
        await coordinator.async_shutdown()

        # Restart-equivalent: a fresh HA instance receives the same stored entry
        # and immutable registry records, not a reset of FAILED_UNLOAD in-place.
        records = list(er.async_entries_for_config_entry(registry, entry.entry_id))
        async with async_test_home_assistant() as restarted:
            restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
            restored = MockConfigEntry(
                domain=DOMAIN,
                entry_id=entry.entry_id,
                unique_id=entry.unique_id,
                data=dict(entry.data),
                options=dict(entry.options),
                title=entry.title,
            )
            restored.add_to_hass(restarted)
            restored_registry = er.async_get(restarted)
            for record in records:
                restored_registry.entities[record.entity_id] = record
            controller.request.return_value = {"data": [{"_id": NEW_ID, "name": "default"}]}
            assert await restarted.config_entries.async_setup(restored.entry_id)
            assert restored.unique_id == NEW_ID
            assert restored_registry.async_get(entity_id).id == registry.async_get(entity_id).id
            assert restored_registry.async_get(entity_id).unique_id == f"{NEW_ID}-{MAC}"
            assert await restarted.config_entries.async_unload(restored.entry_id)
            await restarted.async_stop(force=True)


async def test_disabled_entry_defers_until_enabled(hass: HomeAssistant):
    """Saving settings for a disabled entry does not run identity verification."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    controller = network_11_controller()
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()),
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
        await hass.async_block_till_done()
        assert result["reason"] == "settings_saved_reload_scheduled"
        assert entry.unique_id == OLD_ID
        controller.request.assert_not_awaited()
        duplicate = await async_run_user_step(hass, USER_STEP_INPUT)
        assert duplicate["reason"] == "site_identity_pending"
        await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
        await hass.async_block_till_done()
    assert entry.unique_id == NEW_ID
    controller.request.assert_awaited_once()


async def test_options_deselection_before_migration_replay(hass: HomeAssistant):
    """Explicit deselection removes old-ID records before setup can replay them."""
    other_mac = "11:22:33:44:55:66"
    entry = add_mock_config_entry(
        hass,
        unique_id=NEW_ID,
        data={
            **MOCK_CONFIG_DATA,
            CONF_SITE_ID_MIGRATION: {"old_site_id": OLD_ID, "new_site_id": NEW_ID, "site": "default"},
        },
        options={CONF_TRACKED_DEVICES: [MAC, other_mac]},
    )
    registry = er.async_get(hass)
    kept, removed = [
        registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{mac}", config_entry=entry)
        for mac in (MAC, other_mac)
    ]
    controller = network_11_controller()
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_TRACKED_DEVICES: [MAC]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert registry.async_get(removed.entity_id) is None
    assert registry.async_get(kept.entity_id).unique_id == f"{OLD_ID}-{MAC}"

    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(removed.entity_id) is None
    assert registry.async_get(kept.entity_id).unique_id == f"{NEW_ID}-{MAC}"


@pytest.mark.parametrize("fresh", [OTHER_ID, None, "conflict", "active_failure"])
async def test_scheduled_setup_uses_fresh_evidence_and_prerequisites(hass: HomeAssistant, fresh):
    """Flow preflight reserves nothing and successful discovery cannot guarantee setup."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    discovery = flow_controller()
    if fresh == "active_failure":
        discovery.clients_all[MAC] = _make_mock_client(MAC)
        discovery.clients.update_mock.side_effect = aiounifi.AiounifiException()
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=discovery),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
    assert result["reason"] == "settings_saved_reload_scheduled"
    controller = network_11_controller()
    if fresh == "conflict":
        add_mock_config_entry(hass, unique_id=NEW_ID)
    elif fresh == "active_failure":
        controller.clients.update_mock.side_effect = aiounifi.AiounifiException()
    else:
        controller.request.return_value = {"data": [{"_id": fresh, "name": "default"}] if fresh else []}
    with patch(PATCH_CONTROLLER, return_value=controller):
        success = await hass.config_entries.async_reload(entry.entry_id)
    assert entry.data[CONF_PASSWORD] == "new-pass"
    if fresh in ("conflict", "active_failure"):
        assert not success
        assert entry.unique_id == OLD_ID
        if fresh == "active_failure":
            controller.request.assert_not_awaited()
    else:
        assert success
        assert entry.unique_id == (fresh or OLD_ID)


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("timing", ["initial", "during_devices", "migrated_during_devices"])
async def test_new_flow_preserves_pending_entry(hass: HomeAssistant, multi, timing):
    """The real flow manager must not replace/remove a pending or migrated entry."""
    controller = flow_controller(multi=multi)
    entry = add_mock_config_entry(hass, unique_id=OLD_ID) if timing == "initial" else None
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        if multi:
            result = await async_configure_flow_step(hass, result, {"site": NEW_ID})
        if timing != "initial":
            assert result["step_id"] == "devices"
            entry = add_mock_config_entry(hass, unique_id=NEW_ID if timing == "migrated_during_devices" else OLD_ID)
            result = await async_configure_flow_step(hass, result, {CONF_TRACKED_DEVICES: [MAC]})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == (
        "already_configured" if timing == "migrated_during_devices" else "site_identity_pending"
    )
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data[CONF_PASSWORD] == "password"


@pytest.mark.parametrize("data", [{CONF_HOST: "alias"}, {CONF_PORT: 8443}, {"site": OLD_ID}, {"site": "office"}])
async def test_pending_guard_does_not_block_unidentifiable_entries(hass: HomeAssistant, data):
    """Same default names on unrelated endpoints are not controller identity."""
    add_mock_config_entry(hass, unique_id=OLD_ID, data={**MOCK_CONFIG_DATA, **data})
    with patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
    assert result["step_id"] == "devices"


async def test_successful_reauth_schedules_one_reload_then_migrates(hass: HomeAssistant):
    """Reauth itself remains credential-only; setup owns reconciliation."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    original_options = dict(entry.options)
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=flow_controller()),
        patch.object(
            hass.config_entries, "async_schedule_reload", wraps=hass.config_entries.async_schedule_reload
        ) as schedule,
        patch(PATCH_CONTROLLER, return_value=network_11_controller()),
    ):
        result = await async_run_reauth_confirm_step(hass, entry, {"username": "new", "password": "new"})
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    schedule.assert_called_once_with(entry.entry_id)
    assert entry.unique_id == NEW_ID
    assert dict(entry.options) == original_options


@pytest.mark.parametrize("error", [aiounifi.Unauthorized(), ValueError("malformed"), RuntimeError("unexpected")])
async def test_site_fetch_form_errors_are_sanitized(hass: HomeAssistant, error):
    """Site-list failures retain form behavior without logging raw exceptions/payloads."""
    controller = flow_controller()
    controller.request.side_effect = error
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
    assert result["errors"] == {
        "base": "invalid_auth" if isinstance(error, aiounifi.Unauthorized) else "cannot_connect"
    }


@pytest.mark.parametrize("data", [{}, {"data": [{"_id": NEW_ID, "name": "default", "desc": 1}]}])
async def test_malformed_site_fetch_is_form_error(hass: HomeAssistant, data):
    """Decoded shape validation is shared with setup."""
    controller = flow_controller()
    controller.request.return_value = data
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("description", ["absent", None])
async def test_optional_description_renders_in_actual_site_picker(hass: HomeAssistant, description):
    """Real Site models with optional display metadata remain usable by forms."""
    controller = flow_controller(multi=True)
    rows = [{"_id": NEW_ID, "name": "default"}, {"_id": OTHER_ID, "name": "office"}]
    if description is None:
        rows[0]["desc"] = None
    controller.request.return_value = {"data": rows}
    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        result = await async_run_user_step(hass, USER_STEP_INPUT)
        assert result["step_id"] == "site"
        result = await async_configure_flow_step(hass, result, {"site": NEW_ID})
        assert result["step_id"] == "devices"


async def test_uuid_claim_during_reconfigure_validation(hass: HomeAssistant):
    """The form reruns final ownership checks after its temporary controller closes."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = flow_controller()

    async def claim():
        add_mock_config_entry(hass, unique_id=NEW_ID)

    controller.clients.update_mock.side_effect = claim
    with (
        patch(PATCH_CREATE_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await async_run_reconfigure_step(hass, entry, make_reconfigure_input())
    assert result["reason"] == "site_identity_conflict"
    assert entry.unique_id == OLD_ID
    assert entry.data[CONF_PASSWORD] != "new-pass"
    reload.assert_not_called()


async def test_options_use_migrated_namespace_and_only_explicit_deselection_removes(hass: HomeAssistant):
    """Options never reconcile identity, and preserved registry records stay addressable."""
    other_mac = "11:22:33:44:55:66"
    entry = add_mock_config_entry(hass, unique_id=OLD_ID, options={CONF_TRACKED_DEVICES: [MAC, other_mac]})
    controller = network_11_controller()
    registry = er.async_get(hass)
    retained = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-77:88:99:aa:bb:cc", config_entry=entry)
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
        other_entity_id = registry.async_get_entity_id("device_tracker", DOMAIN, f"{NEW_ID}-{other_mac}")
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        assert registry.async_get(retained.entity_id).unique_id.startswith(NEW_ID)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_TRACKED_DEVICES: [MAC]})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert registry.async_get(other_entity_id) is None
    assert registry.async_get(retained.entity_id).unique_id.startswith(NEW_ID)
    controller.request.assert_awaited_once()
