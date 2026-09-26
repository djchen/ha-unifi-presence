"""Tests for the UniFi Presence device tracker platform."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.device_tracker import SourceType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.coordinator import UnifiPresenceData
from custom_components.unifi_presence.device_tracker import (
    PARALLEL_UPDATES,
    UnifiPresenceTracker,
)

from .conftest import _make_mock_client, add_mock_config_entry, make_mock_controller

MAC = "aa:bb:cc:dd:ee:ff"
PATCH_CREATE_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller_for_params"


def _make_coordinator(data: UnifiPresenceData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.tracked_devices = list(data) if data else []
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry_id"
    coordinator.site_id = "default"
    return coordinator


@pytest.mark.parametrize(
    ("data", "update_success", "name", "connected", "available"),
    [
        pytest.param({MAC: (True, "Dan Phone")}, True, "Dan Phone", True, True, id="home"),
        pytest.param(
            {MAC: (False, "Dan Phone")},
            True,
            "Dan Phone",
            False,
            True,
            id="offline",
        ),
        pytest.param(None, True, MAC, False, False, id="no-data"),
        pytest.param(
            {"11:22:33:44:55:66": (True, "Other Device")},
            True,
            MAC,
            False,
            True,
            id="missing-client",
        ),
        pytest.param(
            {MAC: (False, "Dan Phone")},
            False,
            "Dan Phone",
            False,
            False,
            id="update-failed",
        ),
    ],
)
def test_tracker_runtime_state(
    data: UnifiPresenceData | None,
    update_success: bool,
    name: str,
    connected: bool,
    available: bool,
) -> None:
    """Test tracker state derived from coordinator data and health."""
    coordinator = _make_coordinator(data)
    coordinator.last_update_success = update_success

    tracker = UnifiPresenceTracker(coordinator, MAC)

    assert tracker.name == name
    assert tracker.is_connected is connected
    assert tracker.available is available


def test_tracker_metadata_contract() -> None:
    """Test the static tracker platform and entity metadata contract."""
    coordinator = _make_coordinator({MAC: (True, "Dan Phone")})
    coordinator.site_id = "office"

    tracker = UnifiPresenceTracker(coordinator, MAC)

    assert PARALLEL_UPDATES == 0
    assert tracker.source_type is SourceType.ROUTER
    assert tracker.unique_id == f"office-{MAC}"
    assert tracker.mac_address == MAC
    assert tracker._attr_has_entity_name is True
    assert tracker.entity_registry_enabled_default is True


@pytest.mark.parametrize("device_timing", ["never", "before_setup", "after_setup"])
async def test_trackers_remain_entity_only(
    hass: HomeAssistant, enable_custom_integrations: None, device_timing: str
) -> None:
    """A matching MAC from another integration must not create or link a device."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    other_entry = MockConfigEntry(domain="test")
    other_entry.add_to_hass(hass)
    entry = add_mock_config_entry(hass)
    client = _make_mock_client(MAC, name="Phone", last_seen=int(dt_util.utcnow().timestamp()))
    controller = make_mock_controller(clients_items=[(MAC, client)])

    def register_other_device() -> None:
        device_registry.async_get_or_create(
            config_entry_id=other_entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
            name="Other integration's phone",
            manufacturer="Original manufacturer",
        )

    if device_timing == "before_setup":
        register_other_device()
    original_devices = list(device_registry.devices)

    def assert_entity_only() -> None:
        entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        assert len(entities) == 1
        entity = entities[0]
        assert entity.unique_id == f"{entry.unique_id}-{MAC}"
        assert entity.device_id is None
        assert entity.disabled_by is None
        state = hass.states.get(entity.entity_id)
        assert state is not None
        assert state.state == "home"
        assert state.attributes["mac"] == MAC
        assert state.attributes["source_type"] == "router"
        assert not dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        assert list(device_registry.devices) == original_devices

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert_entity_only()

        if device_timing == "after_setup":
            register_other_device()
            original_devices = list(device_registry.devices)
            await hass.async_block_till_done()
            assert_entity_only()

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert_entity_only()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert list(device_registry.devices) == original_devices


@pytest.mark.parametrize("previous_device_owner", ["other", "unifi_presence"])
async def test_previously_linked_tracker_is_detached_without_removing_devices(
    hass: HomeAssistant, enable_custom_integrations: None, previous_device_owner: str
) -> None:
    """Setup clears an old device link while preserving devices and user metadata."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    other_entry = MockConfigEntry(domain="test")
    other_entry.add_to_hass(hass)
    entry = add_mock_config_entry(hass)
    other_device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Other integration's phone",
    )
    previous_device = other_device
    if previous_device_owner == "unifi_presence":
        previous_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
            name=MAC,
        )
    device_registry.async_update_device(previous_device.id, name_by_user="My phone")
    other_entity = entity_registry.async_get_or_create(
        "sensor", "test", "phone", config_entry=other_entry, device_id=other_device.id
    )
    tracker_entry = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.unique_id}-{MAC}",
        config_entry=entry,
        device_id=previous_device.id,
        suggested_object_id="my_phone",
    )
    entity_registry.async_update_entity(tracker_entry.entity_id, name="My presence")
    original_devices = list(device_registry.devices)
    controller = make_mock_controller()

    with patch(PATCH_CREATE_CONTROLLER, return_value=controller):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        for reload in (False, True):
            if reload:
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
            updated = entity_registry.async_get(tracker_entry.entity_id)
            assert updated is not None
            assert updated.id == tracker_entry.id
            assert updated.unique_id == tracker_entry.unique_id
            assert updated.name == "My presence"
            assert updated.device_id is None
            assert updated.disabled_by is None
            state = hass.states.get(tracker_entry.entity_id)
            assert state is not None
            assert state.state == "not_home"
            assert list(device_registry.devices) == original_devices
            preserved_device = device_registry.async_get(previous_device.id)
            assert preserved_device is not None
            assert preserved_device.name_by_user == "My phone"
            assert entity_registry.async_get(other_entity.entity_id) == other_entity

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert list(device_registry.devices) == original_devices
        preserved_device = device_registry.async_get(previous_device.id)
        assert preserved_device is not None
        assert preserved_device.name_by_user == "My phone"
