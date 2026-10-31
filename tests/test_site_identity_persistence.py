"""Replay durable migration provenance across independent HA storage writes."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_test_home_assistant

from custom_components.unifi_presence import async_setup_entry
from custom_components.unifi_presence.const import CONF_SITE, CONF_SITE_ID_MIGRATION, DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import MOCK_CONFIG_DATA, add_mock_config_entry
from .site_identity_helpers import MAC, NEW_ID, OLD_ID, OTHER_ID, PATCH_CONTROLLER, network_11_controller

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

MARKER = {"old_site_id": OLD_ID, "new_site_id": NEW_ID, CONF_SITE: "default"}


async def _load_persistent_registry(hass: HomeAssistant) -> er.EntityRegistry:
    """Replace the fixture's write-disabled registry with HA's actual store."""
    registry = er.EntityRegistry(hass)
    await registry.async_load()
    hass.data[er.DATA_REGISTRY] = registry
    er.async_get.cache_clear()
    return registry


def _customized_trackers(registry, entry):
    """Seed selected and disabled/absent trackers with user customizations."""
    records = []
    for index, mac in enumerate((MAC, "11:22:33:44:55:66")):
        record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{mac}", config_entry=entry)
        registry.async_update_entity_options(record.entity_id, "device_tracker", {"associated_zone": "zone.home"})
        records.append(
            registry.async_update_entity(
                record.entity_id,
                name=f"My phone {index}",
                icon="mdi:phone",
                area_id="my_area",
                labels={"important"},
                hidden_by=er.RegistryEntryHider.USER,
                disabled_by=er.RegistryEntryDisabler.USER if index else None,
            )
        )
    return records


def _assert_preserved_trackers(registry, entry, records):
    """Require the original entity IDs/metadata with only the new prefix."""
    for before in records:
        after = registry.async_get(before.entity_id)
        assert after is not None
        assert after.id == before.id
        assert after.unique_id == before.unique_id.replace(OLD_ID, NEW_ID)
        for field in ("name", "icon", "area_id", "labels", "options", "disabled_by", "hidden_by"):
            assert getattr(after, field) == getattr(before, field)
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == len(records)


@pytest.mark.parametrize("completed", [0, 1, 2])
async def test_entry_only_save_restarts_preserve_entities(hass: HomeAssistant, hass_storage, completed):
    """The 1s entry save can survive a crash before the 10s registry save."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = await _load_persistent_registry(hass)
    records = _customized_trackers(registry, entry)
    # This is the pre-upgrade registry on disk, not the current in-memory data.
    await registry._store.async_save(registry._data_to_save())
    old_registry = deepcopy(hass_storage[er.STORAGE_KEY])
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.unique_id == NEW_ID
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    saved_entry = hass_storage["core.config_entries"]["data"]["entries"][0]
    assert saved_entry["unique_id"] == NEW_ID
    assert saved_entry["data"][CONF_SITE_ID_MIGRATION] == MARKER
    assert hass_storage[er.STORAGE_KEY] == old_registry
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Include partial/already-complete replay without ever inventing a source
    # from the registry. Each restart loads only what the two stores persisted.
    for saved_record in old_registry["data"]["entities"][:completed]:
        saved_record["unique_id"] = saved_record["unique_id"].replace(OLD_ID, NEW_ID)
    crash_snapshot = deepcopy(hass_storage)
    crash_snapshot[er.STORAGE_KEY] = old_registry
    hass_storage.update(deepcopy(crash_snapshot))
    async with async_test_home_assistant(hass.loop, config_dir=hass.config.config_dir) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        await restarted.config_entries.async_initialize()
        restored_registry = await _load_persistent_registry(restarted)
        restored_entry = restarted.config_entries.async_get_entry(entry.entry_id)
        assert restored_entry is not None
        controller = network_11_controller()
        try:
            with patch(PATCH_CONTROLLER, return_value=controller):
                assert await restarted.config_entries.async_setup(entry.entry_id)
                await restarted.async_block_till_done()
                assert restored_entry.data[CONF_SITE_ID_MIGRATION] == MARKER
                _assert_preserved_trackers(restored_registry, restored_entry, records)
                component = restarted.data["entity_components"]["device_tracker"]
                assert component.get_entity(records[0].entity_id).unique_id == f"{NEW_ID}-{MAC}"
                assert await restarted.config_entries.async_reload(entry.entry_id)
            controller.request.assert_not_awaited()
            assert restored_entry.data[CONF_SITE_ID_MIGRATION] == MARKER
        finally:
            if restored_entry.state is ConfigEntryState.LOADED:
                assert await restarted.config_entries.async_unload(entry.entry_id)
            await restarted.async_stop(force=True)


async def test_registry_first_save_restarts_preserve_entities(hass: HomeAssistant, hass_storage):
    """New registry plus old unmarked entry still requires fresh identity evidence."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = await _load_persistent_registry(hass)
    records = _customized_trackers(registry, entry)
    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    old_entry_store = deepcopy(hass_storage["core.config_entries"])
    with patch(PATCH_CONTROLLER, return_value=network_11_controller()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await registry._store.async_save(registry._data_to_save())
    assert await hass.config_entries.async_unload(entry.entry_id)
    # Model the opposite independent-write outcome: only the registry reached
    # disk. Do not reuse the updated in-memory entry or manufacture provenance.
    hass_storage["core.config_entries"] = old_entry_store
    async with async_test_home_assistant(hass.loop, config_dir=hass.config.config_dir) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        await restarted.config_entries.async_initialize()
        restored_registry = await _load_persistent_registry(restarted)
        restored_entry = restarted.config_entries.async_get_entry(entry.entry_id)
        assert restored_entry is not None
        assert restored_entry.unique_id == OLD_ID
        assert CONF_SITE_ID_MIGRATION not in restored_entry.data
        _assert_preserved_trackers(restored_registry, restored_entry, records)
        controller = network_11_controller()
        try:
            with (
                patch(PATCH_CONTROLLER, return_value=controller),
                patch("custom_components.unifi_presence._LOGGER.info") as recovery_log,
            ):
                assert await restarted.config_entries.async_setup(entry.entry_id)
                await restarted.async_block_till_done()
                assert restored_entry.unique_id == NEW_ID
                assert restored_entry.data[CONF_SITE_ID_MIGRATION] == MARKER
                _assert_preserved_trackers(restored_registry, restored_entry, records)
                component = restarted.data["entity_components"]["device_tracker"]
                assert component.get_entity(records[0].entity_id).unique_id == f"{NEW_ID}-{MAC}"
                recovery_log.assert_called_once_with(
                    "Recovered UniFi site identity from %s to %s; migrated %s tracker registry records",
                    OLD_ID,
                    NEW_ID,
                    0,
                )
                async_fire_time_changed(restarted, dt_util.utcnow() + timedelta(seconds=2))
                await restarted.async_block_till_done()
                persisted_entry = hass_storage["core.config_entries"]["data"]["entries"][0]
                assert persisted_entry["unique_id"] == NEW_ID
                assert persisted_entry["data"][CONF_SITE_ID_MIGRATION] == MARKER
                assert await restarted.config_entries.async_reload(entry.entry_id)
            controller.request.assert_awaited_once()
        finally:
            if restored_entry.state is ConfigEntryState.LOADED:
                assert await restarted.config_entries.async_unload(entry.entry_id)
            await restarted.async_stop(force=True)


@pytest.mark.parametrize("owner_first", [False, True])
async def test_replay_rejects_any_other_uuid_owner(hass: HomeAssistant, owner_first):
    """A recovering entry's own UUID match cannot hide another real owner."""
    assert await async_setup_component(hass, DOMAIN, {})
    if owner_first:
        owner = add_mock_config_entry(hass, unique_id=NEW_ID)
    entry = add_mock_config_entry(hass, unique_id=NEW_ID, data={**MOCK_CONFIG_DATA, CONF_SITE_ID_MIGRATION: MARKER})
    if not owner_first:
        owner = add_mock_config_entry(hass, unique_id=NEW_ID)
    assert hass.config_entries.async_entries(DOMAIN) == ([owner, entry] if owner_first else [entry, owner])
    registry = er.async_get(hass)
    registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    before = dict(registry.entities)
    before_entries = [(item.unique_id, dict(item.data)) for item in (entry, owner)]
    controller = network_11_controller()
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "site_identity_conflict"
    assert dict(registry.entities) == before
    assert [(item.unique_id, dict(item.data)) for item in (entry, owner)] == before_entries
    forward.assert_not_awaited()
    controller.request.assert_not_awaited()


@pytest.mark.parametrize("conflict", ["entity", "later", "normalized"])
async def test_replay_conflicts_fail_closed(hass: HomeAssistant, conflict):
    """Residual preflight checks all records before mutation or entity creation."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = add_mock_config_entry(hass, unique_id=NEW_ID, data={**MOCK_CONFIG_DATA, CONF_SITE_ID_MIGRATION: MARKER})
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    mac = "11:22:33:44:55:66" if conflict == "later" else MAC
    if conflict == "later":
        registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{mac}", config_entry=entry)
    unique_id = f"{OLD_ID}-{MAC.upper()}" if conflict == "normalized" else f"{NEW_ID}-{mac}"
    registry.async_get_or_create("device_tracker", DOMAIN, unique_id, config_entry=entry)
    before = dict(registry.entities)
    controller = network_11_controller()
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.error_reason_translation_key == "site_identity_conflict"
    assert dict(registry.entities) == before
    assert registry.async_get(record.entity_id) == record
    assert entry.unique_id == NEW_ID
    assert entry.data[CONF_SITE_ID_MIGRATION] == MARKER
    forward.assert_not_awaited()
    controller.request.assert_not_awaited()


@pytest.mark.parametrize(
    "marker",
    [
        None,
        [],
        {},
        {**MARKER, "old_site_id": "arbitrary"},
        {**MARKER, "old_site_id": OLD_ID.upper()},
        {**MARKER, "new_site_id": NEW_ID.upper()},
        {**MARKER, CONF_SITE: ""},
        {**MARKER, CONF_SITE: OLD_ID},
    ],
)
async def test_invalid_provenance_fails_closed(hass: HomeAssistant, marker):
    """Do not migrate or create duplicate trackers from malformed stored metadata."""
    entry = add_mock_config_entry(hass, unique_id=NEW_ID, data={**MOCK_CONFIG_DATA, CONF_SITE_ID_MIGRATION: marker})
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    controller = network_11_controller()
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.error_reason_translation_key == "cannot_verify_site_identity"
    assert registry.async_get(record.entity_id) == record
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 1
    forward.assert_not_awaited()
    controller.request.assert_not_awaited()


async def test_old_marker_cannot_redirect_explicit_site_change(hass: HomeAssistant):
    """Reconfigure preserves extra data, but an old marker is bound to its target."""
    data = {**MOCK_CONFIG_DATA, CONF_SITE_ID_MIGRATION: MARKER}
    entry = add_mock_config_entry(hass, unique_id=OTHER_ID, data=data)
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert registry.async_get(record.entity_id) == record
    controller.request.assert_not_awaited()


async def test_marker_survives_request_facing_site_rename(hass: HomeAssistant):
    """A renamed short name does not change the authoritative destination UUID."""
    entry = add_mock_config_entry(
        hass, unique_id=NEW_ID, data={**MOCK_CONFIG_DATA, CONF_SITE: "office", CONF_SITE_ID_MIGRATION: MARKER}
    )
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert registry.async_get(record.entity_id).unique_id == f"{NEW_ID}-{MAC}"
    assert entry.data[CONF_SITE_ID_MIGRATION] == MARKER
    controller.request.assert_not_awaited()


@pytest.mark.parametrize("change", ["data", "identity", "provenance", "stop", "shutdown"])
async def test_replay_rechecks_snapshot_and_lifecycle(hass: HomeAssistant, change):
    """First refresh cannot race settings/shutdown into a residual commit."""
    entry = add_mock_config_entry(
        hass, unique_id=NEW_ID, data={**MOCK_CONFIG_DATA, CONF_SITE_ID_MIGRATION: deepcopy(MARKER)}
    )
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)

    async def refresh(coordinator):
        if change == "data":
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PASSWORD: "changed"})
        elif change == "identity":
            hass.config_entries.async_update_entry(entry, unique_id=OTHER_ID)
        elif change == "provenance":
            entry.data[CONF_SITE_ID_MIGRATION]["old_site_id"] = "5f0123456789abcdef012346"
        elif change == "stop":
            hass.set_state(CoreState.stopping)
        else:
            await coordinator.async_shutdown()

    with (
        patch.object(UnifiPresenceCoordinator, "async_config_entry_first_refresh", refresh),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock) as forward,
    ):
        if change in ("stop", "shutdown"):
            with pytest.raises(asyncio.CancelledError):
                await async_setup_entry(hass, entry)
        else:
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)
    assert registry.async_get(record.entity_id).unique_id == record.unique_id
    forward.assert_not_awaited()
