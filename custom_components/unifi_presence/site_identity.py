"""Restricted Network 11 identity policy and synchronous registry migration."""

import re
from collections.abc import Mapping, Sequence

from aiounifi.models.site import Site
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_SITE, CONF_SITE_ID_MIGRATION, DOMAIN
from .helpers import normalize_mac, tracker_unique_id


def is_object_id(value: object) -> bool:
    """Recognize canonical, lowercase MongoDB ObjectIds only."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{24}", value) is not None


def is_uuid(value: object) -> bool:
    """Recognize lowercase UUID-shaped IDs without imposing a UUID version."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is not None


def eligible_identity(unique_id: object, stored_site: object) -> bool:
    """Require an actual ObjectId and surviving raw short-name evidence."""
    return (
        is_object_id(unique_id)
        and isinstance(stored_site, str)
        and bool(stored_site)
        and not is_object_id(stored_site)
        and not is_uuid(stored_site)
    )


def resolve_network_11_site(unique_id: object, stored_site: object, sites: Sequence[Site]) -> Site | None:
    """Follow only an absent ObjectId to one UUID with the unchanged short name."""
    if not eligible_identity(unique_id, stored_site) or any(site.site_id == unique_id for site in sites):
        return None
    matches = [site for site in sites if site.name == stored_site]
    return matches[0] if len(matches) == 1 and is_uuid(matches[0].site_id) else None


def tracker_migration_source(unique_id: object, data: Mapping[str, object]) -> str | None:
    """Read durable recovery provenance, never infer it from registry contents.

    Keep the marker even after replay: HA saves config entries and the registry
    independently. Bind it to the exact destination so settings preserved by
    reconfigure/options cannot replay it against a different site. The short
    name is historical provenance, not current identity: reconfigure may rename
    the same UUID's request-facing site without invalidating recovery.
    Invalid provenance fails closed; valid but no longer applicable provenance
    is inert (an explicit site change must not redirect the old prefix).
    """
    if CONF_SITE_ID_MIGRATION not in data:
        return None
    marker = data[CONF_SITE_ID_MIGRATION]
    if (
        not isinstance(marker, Mapping)
        or not eligible_identity(marker.get("old_site_id"), marker.get(CONF_SITE))
        or not is_uuid(marker.get("new_site_id"))
    ):
        raise ValueError("Invalid Network 11 tracker migration provenance")
    if marker["new_site_id"] != unique_id:
        return None
    return str(marker["old_site_id"])


def same_endpoint(data: Mapping[str, object], host: str, port: int) -> bool:
    """Compare stored endpoint values exactly; do not equate aliases."""
    return data.get(CONF_HOST) == host and data.get(CONF_PORT) == port


def entry_snapshot_matches(
    hass: HomeAssistant, entry: ConfigEntry, unique_id: str | None, data: Mapping[str, object]
) -> bool:
    """Reject removed entries and settings changed across validation awaits."""
    return (
        hass.config_entries.async_get_entry(entry.entry_id) is entry
        and entry.unique_id == unique_id
        and entry.data == data
    )


@callback
def site_id_has_other_owner(hass: HomeAssistant, entry: ConfigEntry, site_id: str) -> bool:
    """Reject every competing owner, not only the first indexed UUID match."""
    return any(other is not entry and other.unique_id == site_id for other in hass.config_entries.async_entries(DOMAIN))


@callback
def build_tracker_migration_plan(
    hass: HomeAssistant, entry: ConfigEntry, *, old_site_id: str, new_site_id: str
) -> list[tuple[str, str]] | None:
    """Preflight every entry-owned prefixed record without writing anything."""
    if old_site_id == new_site_id:
        return []
    registry = er.async_get(hass)
    prefix = f"{old_site_id}-"
    plan: list[tuple[str, str]] = []
    targets: set[tuple[str, str, str]] = set()
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not record.unique_id.startswith(prefix):
            continue
        mac = normalize_mac(record.unique_id.removeprefix(prefix))
        if not mac:
            continue
        unique_id = tracker_unique_id(new_site_id, mac)
        target = (record.domain, record.platform, unique_id)
        existing = registry.async_get_entity_id(*target)
        if target in targets or existing not in (None, record.entity_id):
            return None
        targets.add(target)
        plan.append((record.entity_id, unique_id))
    return plan


@callback
def apply_tracker_migration_plan(hass: HomeAssistant, plan: list[tuple[str, str]]) -> None:
    """Apply a freshly preflighted plan with no intervening await."""
    registry = er.async_get(hass)
    for entity_id, unique_id in plan:
        registry.async_update_entity(entity_id, new_unique_id=unique_id)
