"""Setup-only identity migration through real HA lifecycle and runtime locking."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import aiounifi
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.unifi_presence.const import CONF_SITE, CONF_SITE_ID_MIGRATION, DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceCoordinator

from .conftest import (
    MOCK_CONFIG_DATA,
    _assert_session_cleanup,
    _attach_mock_session,
    add_mock_config_entry,
)
from .site_identity_helpers import MAC, NEW_ID, OLD_ID, PATCH_CONTROLLER, network_11_controller

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_real_reload_preserves_tracker_identity_and_metadata(hass: HomeAssistant):
    """Pre-upgrade entities survive with registry IDs and user customizations intact."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()
    controller.request.return_value = {"data": [{"_id": OLD_ID, "name": "renamed"}]}
    registry = er.async_get(hass)

    def assert_committed(*_args: object) -> None:
        assert entry.unique_id == NEW_ID
        for record in (before, retained):
            assert registry.async_get(record.entity_id).unique_id == record.unique_id.replace(OLD_ID, NEW_ID)

    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch("custom_components.unifi_presence._LOGGER.info", side_effect=assert_committed) as recovery_log,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = registry.async_get_entity_id("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}")
        assert entity_id is not None
        registry.async_update_entity_options(entity_id, "device_tracker", {"associated_zone": "zone.home"})
        before = registry.async_update_entity(entity_id, name="My phone", icon="mdi:phone", labels={"important"})
        retained = registry.async_get_or_create(
            "device_tracker", DOMAIN, f"{OLD_ID}-11:22:33:44:55:66", config_entry=entry
        )
        retained = registry.async_update_entity(
            retained.entity_id, disabled_by=er.RegistryEntryDisabler.USER, hidden_by=er.RegistryEntryHider.USER
        )
        original = deepcopy((dict(entry.data), dict(entry.options), entry.title, entry.entry_id, entry.version))
        controller.request.return_value = {"data": [{"_id": NEW_ID, "name": "default"}]}
        # Firmware traffic alone must not migrate an already loaded tracker.
        await entry.runtime_data.async_refresh()
        assert entry.unique_id == OLD_ID
        assert registry.async_get(entity_id).unique_id == f"{OLD_ID}-{MAC}"
        assert controller.request.await_count == 1
        old_runtime = entry.runtime_data
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not old_runtime
        assert old_runtime._stopping
        assert entry.unique_id == NEW_ID
        assert entry.runtime_data.site_id == NEW_ID
        after = registry.async_get(entity_id)
        assert after.id == before.id
        assert after.unique_id == f"{NEW_ID}-{MAC}"
        assert (after.name, after.icon, after.labels, after.options) == (
            before.name,
            before.icon,
            before.labels,
            before.options,
        )
        assert registry.async_get(retained.entity_id).unique_id.startswith(NEW_ID)
        assert registry.async_get(retained.entity_id).disabled_by is retained.disabled_by
        assert registry.async_get(retained.entity_id).hidden_by is retained.hidden_by
        assert dict(entry.data) == {
            **original[0],
            CONF_SITE_ID_MIGRATION: {"old_site_id": OLD_ID, "new_site_id": NEW_ID, CONF_SITE: "default"},
        }
        assert (dict(entry.options), entry.title, entry.entry_id, entry.version) == original[1:]
        component = hass.data["entity_components"]["device_tracker"]
        tracker = component.get_entity(entity_id)
        assert tracker.unique_id == f"{NEW_ID}-{MAC}"
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 2
        assert controller.request.await_count == 2
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert controller.request.await_count == 2
    recovery_log.assert_called_once_with(
        "Recovered UniFi site identity from %s to %s; migrated %s tracker registry records",
        OLD_ID,
        NEW_ID,
        2,
    )


@pytest.mark.parametrize("unique_id", [NEW_ID, "arbitrary", "host_default", None])
async def test_ineligible_setup_never_fetches_identity(hass: HomeAssistant, unique_id):
    """Ordinary and already migrated setups gain no site-list dependency."""
    entry = add_mock_config_entry(hass, unique_id=unique_id)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    controller.request.assert_not_awaited()


@pytest.mark.parametrize("stored_site", [None, "", OLD_ID, NEW_ID])
async def test_missing_short_name_never_fetches_identity(hass: HomeAssistant, stored_site):
    """Do not fabricate the default short-name recovery anchor."""
    data = dict(MOCK_CONFIG_DATA)
    if stored_site is None:
        data.pop(CONF_SITE)
    else:
        data[CONF_SITE] = stored_site
    entry = add_mock_config_entry(hass, unique_id=OLD_ID, data=data)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
    controller.request.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "state", "key"),
    [
        (aiounifi.Unauthorized(), ConfigEntryState.SETUP_ERROR, "credentials_rejected"),
        (aiounifi.LoginRequired(), ConfigEntryState.SETUP_ERROR, "credentials_rejected"),
        (TimeoutError(), ConfigEntryState.SETUP_RETRY, "cannot_connect"),
        (aiounifi.AiounifiException(), ConfigEntryState.SETUP_RETRY, "cannot_connect"),
        (KeyError("programming"), ConfigEntryState.SETUP_ERROR, None),
    ],
)
async def test_fetch_failure_cleanup(hass: HomeAssistant, failure, state, key):
    """Expected failures classify narrowly; every failed setup releases its session."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()
    controller.request.side_effect = failure
    session = _attach_mock_session(controller)
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is state
    assert entry.error_reason_translation_key == key
    assert entry.unique_id == OLD_ID
    forward.assert_not_awaited()
    controller.start_websocket.assert_not_awaited()
    _assert_session_cleanup(session)


@pytest.mark.parametrize("response", [{}, {"data": [{"_id": NEW_ID, "name": ""}]}, {"meta": None}])
async def test_invalid_snapshot_retries(hass: HomeAssistant, response):
    """Malformed snapshots never establish absence and cannot migrate identity."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()
    controller.request.return_value = response
    session = _attach_mock_session(controller)
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "cannot_verify_site_identity"
    assert entry.unique_id == OLD_ID
    _assert_session_cleanup(session)


@pytest.mark.parametrize("conflict", ["entry", "entity", "later", "normalized"])
async def test_setup_conflicts_are_mutation_free_and_recoverable(hass: HomeAssistant, conflict):
    """Final ownership and whole-registry preflight prevent all partial writes."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    blocker = None
    if conflict == "entry":
        owner = add_mock_config_entry(hass, unique_id=NEW_ID)
    elif conflict == "normalized":
        blocker = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC.upper()}", config_entry=entry)
    else:
        target_mac = "11:22:33:44:55:66" if conflict == "later" else MAC
        if conflict == "later":
            registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{target_mac}", config_entry=entry)
        blocker = registry.async_get_or_create("device_tracker", DOMAIN, f"{NEW_ID}-{target_mac}", config_entry=entry)
    before = dict(registry.entities)
    controller = network_11_controller()
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch("custom_components.unifi_presence._LOGGER.info") as recovery_log,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert entry.error_reason_translation_key == "site_identity_conflict"
        assert entry.unique_id == OLD_ID
        assert dict(registry.entities) == before
        recovery_log.assert_not_called()
        if blocker:
            registry.async_remove(blocker.entity_id)
        else:
            assert await hass.config_entries.async_remove(owner.entry_id)
        assert await hass.config_entries.async_reload(entry.entry_id)
        recovery_log.assert_called_once()
    assert entry.unique_id == NEW_ID
    assert registry.async_get(record.entity_id).unique_id == f"{NEW_ID}-{MAC}"


@pytest.mark.parametrize(("stage", "candidate"), [("refresh", False), ("identity", True), ("identity", False)])
async def test_changed_entry_snapshot_retries_without_runtime(hass: HomeAssistant, stage, candidate):
    """Snapshot starts before first refresh, and no-candidate results are also checked."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()

    async def change(*_args):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PASSWORD: "changed"})
        return {"data": [{"_id": NEW_ID, "name": "default"}] if candidate else []}

    if stage == "refresh":
        controller.clients.update_mock.side_effect = change
        controller.request.return_value = {"data": []}
    else:
        controller.request.side_effect = change
    with patch(PATCH_CONTROLLER, return_value=controller):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.unique_id == OLD_ID
    assert entry.data[CONF_PASSWORD] == "changed"
    assert entry.error_reason_translation_key == "cannot_verify_site_identity"
    controller.start_websocket.assert_not_awaited()


@pytest.mark.parametrize("action", ["cancel", "stop", "shutdown"])
async def test_cancel_stop_and_shutdown_during_identity_fetch(hass: HomeAssistant, action):
    """The new await participates in HA cancellation and coordinator drain cleanup."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    controller = network_11_controller()
    session = _attach_mock_session(controller)
    started, release = asyncio.Event(), asyncio.Event()
    coordinators = []
    original_fetch = UnifiPresenceCoordinator.async_fetch_identity_sites

    async def capture(coordinator):
        coordinators.append(coordinator)
        return await original_fetch(coordinator)

    async def fetch(_request):
        started.set()
        await release.wait()
        return {"data": [{"_id": NEW_ID, "name": "default"}]}

    controller.request.side_effect = fetch
    shutdown = None
    with (
        patch(PATCH_CONTROLLER, return_value=controller),
        patch.object(UnifiPresenceCoordinator, "async_fetch_identity_sites", capture),
    ):
        task = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await started.wait()
            coordinator = coordinators[0]
            assert coordinator._client_store_lock.locked()
            if action == "cancel":
                task.cancel()
            elif action == "stop":
                hass.set_state(CoreState.stopping)
            else:
                shutdown = hass.async_create_task(coordinator.async_shutdown())
                # Eager HA task sets stopping before waiting for the held lock.
                assert coordinator._stopping
                assert not shutdown.done()
            release.set()
            if action == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                # HA treats a raised CancelledError without task cancellation as
                # a setup error, but still runs the registered unload callbacks.
                assert not await task
            if shutdown:
                await shutdown
            assert not coordinator._client_store_lock.locked()
            assert entry.unique_id == OLD_ID
            controller.start_websocket.assert_not_awaited()
            _assert_session_cleanup(session)
        finally:
            release.set()
            await asyncio.gather(task, *([shutdown] if shutdown else []), return_exceptions=True)


async def test_platform_failure_keeps_commit_and_retry_is_idempotent(hass: HomeAssistant):
    """A later platform exception must not roll back coherent migrated identity."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-{MAC}", config_entry=entry)
    controller = network_11_controller()
    with patch(PATCH_CONTROLLER, return_value=controller):
        with patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("platform")):
            assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.unique_id == NEW_ID
        assert registry.async_get(record.entity_id).unique_id == f"{NEW_ID}-{MAC}"
        assert await hass.config_entries.async_reload(entry.entry_id)
    controller.request.assert_awaited_once()


async def test_two_old_entries_cannot_claim_one_uuid(hass: HomeAssistant):
    """A UUID claimed during validation is rejected by the final synchronous check."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    competitor = add_mock_config_entry(hass, unique_id="5f0123456789abcdef012346")
    first, second = network_11_controller(), network_11_controller()
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(_request):
        started.set()
        await release.wait()
        return {"data": [{"_id": NEW_ID, "name": "default"}]}

    first.request.side_effect = blocked
    with patch(PATCH_CONTROLLER, side_effect=[first, second]):
        task = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await started.wait()
            assert await hass.config_entries.async_setup(competitor.entry_id)
            release.set()
            assert not await task
            assert competitor.unique_id == NEW_ID
            assert entry.unique_id == OLD_ID
            assert entry.error_reason_translation_key == "site_identity_conflict"
        finally:
            release.set()
            await task


@pytest.mark.parametrize("stop", ["already_stopped", "ha_stopping", "waiting_for_lock", "ha_stopping_while_waiting"])
async def test_identity_fetch_honors_stop_before_request(hass: HomeAssistant, stop):
    """Stop checks cover work queued behind the lifetime lock as well as new work."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    coordinator = UnifiPresenceCoordinator(hass, entry)
    controller = network_11_controller()
    coordinator._controller = controller
    session = _attach_mock_session(controller)
    try:
        if stop == "already_stopped":
            await coordinator.async_shutdown()
        elif stop == "ha_stopping":
            hass.set_state(CoreState.stopping)
        if stop in ("waiting_for_lock", "ha_stopping_while_waiting"):
            waiting = asyncio.Event()
            lock = coordinator._client_store_lock
            acquire = lock.acquire
            await lock.acquire()

            async def observe_acquire():
                waiting.set()
                return await acquire()

            with patch.object(lock, "acquire", side_effect=observe_acquire):
                fetch = hass.async_create_task(coordinator.async_fetch_identity_sites())
                await waiting.wait()
                shutdown = None
                if stop == "waiting_for_lock":
                    shutdown = hass.async_create_task(coordinator.async_shutdown())
                    assert coordinator._stopping
                else:
                    hass.set_state(CoreState.stopping)
                lock.release()
                with pytest.raises(asyncio.CancelledError):
                    await fetch
                if shutdown:
                    await shutdown
        else:
            with pytest.raises(asyncio.CancelledError):
                await coordinator.async_fetch_identity_sites()
        controller.request.assert_not_awaited()
    finally:
        await coordinator.async_shutdown()
    _assert_session_cleanup(session)
