"""Network 11 policy, response-only dependency contracts, and registry preflight."""

from copy import deepcopy
from json import JSONDecodeError
from unittest.mock import AsyncMock, Mock, patch

import aiounifi
import orjson
import pytest
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from aiounifi.models.site import Site
from attr import fields
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.helpers import (
    SiteSnapshotError,
    async_fetch_site_snapshot,
    site_title,
)
from custom_components.unifi_presence.site_identity import (
    apply_tracker_migration_plan,
    build_tracker_migration_plan,
    eligible_identity,
    is_object_id,
    is_uuid,
    resolve_network_11_site,
)

from .conftest import add_mock_config_entry, make_mock_controller
from .site_identity_helpers import NEW_ID, OLD_ID, OTHER_ID


def site(site_id=NEW_ID, name="default", **extra):
    """Build a real detached site model."""
    return Site({"_id": site_id, "name": name, "desc": "", **extra})


@pytest.mark.parametrize("name", ["default", "office", " office "])
def test_resolve_transition(name):
    """Exact surviving short names resolve without normalization."""
    candidate = site(name=name)
    assert resolve_network_11_site(OLD_ID, name, [candidate]) is candidate


@pytest.mark.parametrize(
    ("old", "name", "sites"),
    [
        (OLD_ID, "default", [site(OLD_ID, "renamed"), site()]),
        (OLD_ID, "default", [site(), site(OTHER_ID)]),
        (OLD_ID, "default", [site(name="office", desc="default")]),
        (OLD_ID, "default", []),
        (OLD_ID, "default", [site("5f0123456789abcdef012346")]),
        (OLD_ID, "default", [site(NEW_ID.upper())]),
        (OLD_ID, "default", [site("arbitrary")]),
        (OLD_ID, "default", [site(name=" default ")]),
        *[
            (old, "default", [site()])
            for old in (None, "", "host_default", "arbitrary", NEW_ID, OLD_ID.upper(), OLD_ID + "\n")
        ],
        *[(OLD_ID, name, [site()]) for name in (None, "", OLD_ID, NEW_ID, 1)],
    ],
)
def test_reject_ineligible_transition(old, name, sites):
    """No broad missing-ID repair, description match, or single-site fallback."""
    assert resolve_network_11_site(old, name, sites) is None


def test_predicates():
    """ID formats are lowercase full matches, with no version constraint."""
    assert is_object_id(OLD_ID)
    assert is_uuid(NEW_ID)
    assert not is_uuid(NEW_ID + "\n")
    assert not is_object_id(7)
    assert eligible_identity(OLD_ID, "default")


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"data": None},
        {"data": {}},
        {"data": [None]},
        {"data": [{}]},
        {"data": [{"_id": "", "name": "default"}]},
        {"data": [{"_id": 1, "name": "default"}]},
        {"data": [{"_id": NEW_ID, "name": ""}]},
        {"data": [{"_id": NEW_ID, "name": 1}]},
        {"data": [{"_id": NEW_ID, "name": "default", "desc": 1}]},
        {"data": [{"_id": NEW_ID, "name": "default"}] * 2},
    ],
)
async def test_snapshot_rejects_malformed_response(response):
    """Malformed records cannot silently establish absence or uniqueness."""
    controller = make_mock_controller()
    controller.request.return_value = response
    with pytest.raises(SiteSnapshotError):
        await async_fetch_site_snapshot(controller)


@pytest.mark.parametrize("description", [None, "", "Office"])
async def test_snapshot_detaches_rows(description):
    """Optional descriptions safely fall back without mutating the source."""
    row = {"_id": "arbitrary", "name": "office", "desc": description}
    controller = make_mock_controller()
    controller.request.return_value = {"data": [row]}
    result = await async_fetch_site_snapshot(controller)
    assert site_title(result[0]) == (description or "office")
    row["name"] = "changed"
    assert result[0].name == "office"
    assert row["desc"] is description


@pytest.mark.parametrize("unifi_os", [False, True])
async def test_real_request_snapshot_ignores_accumulated_cache(unifi_os):
    """Real Sites upserts, while public global requests return response-only evidence."""
    controller = Controller(
        Configuration(Mock(), host="controller", username="user", password="password", site="office")
    )
    controller.connectivity.is_unifi_os = unifi_os
    response = Mock(content_type="application/json")
    old = orjson.dumps({"data": [{"_id": OLD_ID, "name": "office"}]})
    new = orjson.dumps({"data": [{"_id": NEW_ID, "name": "office"}]})
    with patch.object(
        controller.connectivity, "_request", AsyncMock(side_effect=[(response, old), (response, new), (response, new)])
    ) as request:
        await controller.sites.update()
        await controller.sites.update()
        assert {s.site_id for s in controller.sites.values()} == {OLD_ID, NEW_ID}
        snapshot = await async_fetch_site_snapshot(controller)
    assert [s.site_id for s in snapshot] == [NEW_ID]
    assert resolve_network_11_site(OLD_ID, "office", snapshot) is snapshot[0]
    assert site_title(snapshot[0]) == "office"
    assert controller.connectivity.config.site == "office"
    path = "/proxy/network/api/self/sites" if unifi_os else "/api/self/sites"
    assert all(call.args[1].endswith(path) for call in request.call_args_list)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b'{"meta":null,"data":[]}', SiteSnapshotError),
        (b'{"meta":{},"data":[]}', SiteSnapshotError),
        (b"null", SiteSnapshotError),
        (b"{", JSONDecodeError),
        (b'{"meta":{"rc":"error","msg":"api.err.Invalid"}}', aiounifi.Unauthorized),
        (b'{"meta":{"rc":"error","msg":"api.err.LoginRequired"}}', aiounifi.LoginRequired),
    ],
)
async def test_real_decoder_error_classification(raw, error):
    """Only decoder structural failures are translated by the adapter."""
    controller = Controller(Configuration(Mock(), host="controller", username="user", password="password"))
    with (
        patch.object(
            controller.connectivity, "_request", AsyncMock(return_value=(Mock(content_type="application/json"), raw))
        ),
        pytest.raises(error),
    ):
        await async_fetch_site_snapshot(controller)


@pytest.mark.parametrize("error", [KeyError("implementation"), TypeError("implementation")])
async def test_request_programming_errors_propagate(error):
    """Structural catches must not extend to the whole public request."""
    controller = make_mock_controller()
    controller.request.side_effect = error
    with pytest.raises(type(error), match="implementation"):
        await async_fetch_site_snapshot(controller)


async def test_empty_and_ambiguous_snapshots():
    """Explicit empty data is valid and duplicate names remain visible."""
    controller = make_mock_controller()
    controller.request.return_value = {"data": []}
    assert await async_fetch_site_snapshot(controller) == ()
    controller.request.return_value = {
        "data": [{"_id": NEW_ID, "name": "default"}, {"_id": OTHER_ID, "name": "default"}]
    }
    assert resolve_network_11_site(OLD_ID, "default", await async_fetch_site_snapshot(controller)) is None


async def test_preflight_ignores_empty_suffix_and_other_scopes(hass: HomeAssistant):
    """Extraction retains the old prefix/MAC and actual registry uniqueness semantics."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}- ", config_entry=entry)
    record = registry.async_get_or_create("device_tracker", DOMAIN, f"{OLD_ID}-mac", config_entry=entry)
    registry.async_get_or_create("sensor", DOMAIN, f"{NEW_ID}-mac", config_entry=entry)
    registry.async_get_or_create("device_tracker", "other", f"{NEW_ID}-mac", config_entry=entry)
    assert build_tracker_migration_plan(hass, entry, old_site_id=OLD_ID, new_site_id=NEW_ID) == [
        (record.entity_id, f"{NEW_ID}-mac")
    ]


async def test_read_only_preflight_and_metadata(hass: HomeAssistant):
    """Every entry-owned record migrates with only registry identity metadata changed."""
    entry = add_mock_config_entry(hass, unique_id=OLD_ID)
    registry = er.async_get(hass)
    records = []
    for index, domain in enumerate(("device_tracker", "sensor", "device_tracker")):
        record = registry.async_get_or_create(
            domain, DOMAIN, f"{OLD_ID}- AA:BB:CC:DD:EE:{index:02X} ", config_entry=entry
        )
        registry.async_update_entity_options(record.entity_id, "device_tracker", {"associated_zone": "zone.office"})
        record = registry.async_update_entity(
            record.entity_id,
            name="My name",
            icon="mdi:phone",
            area_id="area",
            labels={"label"},
            hidden_by=er.RegistryEntryHider.USER,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        records.append(record)
    before = deepcopy(dict(registry.entities))
    with patch.object(registry, "async_update_entity", wraps=registry.async_update_entity) as update:
        plan = build_tracker_migration_plan(hass, entry, old_site_id=OLD_ID, new_site_id=NEW_ID)
        assert plan is not None
        assert len(plan) == 3
        update.assert_not_called()
        assert dict(registry.entities) == before
        apply_tracker_migration_plan(hass, plan)
    for record in records:
        current = registry.async_get(record.entity_id)
        assert current.id == record.id
        assert current.unique_id.startswith(NEW_ID)
        for field in fields(er.RegistryEntry):
            if field.name not in {"unique_id", "previous_unique_id", "modified_at", "_cache"}:
                assert getattr(current, field.name) == getattr(record, field.name)
