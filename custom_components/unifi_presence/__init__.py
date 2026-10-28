"""The UniFi Presence integration."""

import asyncio
import logging
from collections.abc import Mapping
from copy import deepcopy

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady

from .const import CONF_SITE, CONF_SITE_ID_MIGRATION, DOMAIN
from .coordinator import UnifiPresenceCoordinator
from .helpers import UNIFI_AUTH_EXCEPTIONS, UNIFI_COMMUNICATION_EXCEPTIONS, SiteSnapshotError
from .site_identity import (
    apply_tracker_migration_plan,
    build_tracker_migration_plan,
    eligible_identity,
    entry_snapshot_matches,
    resolve_network_11_site,
    site_id_has_other_owner,
    tracker_migration_source,
)
from .websocket import UnifiPresenceWebsocket

_LOGGER = logging.getLogger(__name__)

type UnifiPresenceConfigEntry = ConfigEntry[UnifiPresenceCoordinator]

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


async def _async_reconcile_site_identity(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
    coordinator: UnifiPresenceCoordinator,
    original_unique_id: str | None,
    original_data: Mapping[str, object],
) -> None:
    """Fetch current evidence, then validate and commit without intervening awaits."""
    try:
        sites = await coordinator.async_fetch_identity_sites()
    except UNIFI_AUTH_EXCEPTIONS:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="credentials_rejected") from None
    except UNIFI_COMMUNICATION_EXCEPTIONS:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": str(original_data["host"])},
        ) from None
    except SiteSnapshotError:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_verify_site_identity") from None

    # The fetch checks shutdown before returning; keep validation and commit synchronous.
    # Validate even empty/no-candidate responses before loading runtime.
    if not entry_snapshot_matches(hass, entry, original_unique_id, original_data):
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_verify_site_identity")
    candidate = resolve_network_11_site(original_unique_id, original_data.get(CONF_SITE), sites)
    if candidate is None:
        return
    has_other_owner = site_id_has_other_owner(hass, entry, candidate.site_id)
    plan = build_tracker_migration_plan(
        hass, entry, old_site_id=original_unique_id or entry.entry_id, new_site_id=candidate.site_id
    )
    if has_other_owner or plan is None:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="site_identity_conflict")
    apply_tracker_migration_plan(hass, plan)
    # Persist provenance in the SAME entry update as the new identity. Never
    # clear it after an in-memory registry update: that store can save later.
    hass.config_entries.async_update_entry(
        entry,
        unique_id=candidate.site_id,
        data={
            **entry.data,
            CONF_SITE_ID_MIGRATION: {
                "old_site_id": original_unique_id,
                "new_site_id": candidate.site_id,
                CONF_SITE: original_data[CONF_SITE],
            },
        },
    )
    _LOGGER.info(
        "Recovered UniFi site identity from %s to %s; migrated %s tracker registry records",
        original_unique_id,
        candidate.site_id,
        len(plan),
    )


@callback
def _replay_tracker_migration(
    hass: HomeAssistant,
    entry: UnifiPresenceConfigEntry,
    coordinator: UnifiPresenceCoordinator,
    original_unique_id: str | None,
    original_data: Mapping[str, object],
) -> None:
    """Finish a prior committed recovery before any tracker can be constructed."""
    coordinator._raise_if_stopping()
    if hass.is_stopping:
        raise asyncio.CancelledError
    if not entry_snapshot_matches(hass, entry, original_unique_id, original_data):
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_verify_site_identity")
    try:
        old_site_id = tracker_migration_source(original_unique_id, original_data)
    except ValueError as err:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="cannot_verify_site_identity") from err
    if old_site_id is None:
        return
    # The marker validator has required this exact destination to be a UUID.
    assert original_unique_id is not None
    has_other_owner = site_id_has_other_owner(hass, entry, original_unique_id)
    plan = build_tracker_migration_plan(hass, entry, old_site_id=old_site_id, new_site_id=original_unique_id)
    if has_other_owner or plan is None:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="site_identity_conflict")
    apply_tracker_migration_plan(hass, plan)


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Set up UniFi Presence from a config entry."""
    coordinator = UnifiPresenceCoordinator(hass, entry)
    original_unique_id, original_data = entry.unique_id, deepcopy(dict(entry.data))
    verify_identity = eligible_identity(original_unique_id, original_data.get(CONF_SITE))
    websocket: UnifiPresenceWebsocket | None = None
    shutdown_unsub: CALLBACK_TYPE | None = None

    @callback
    def _unsubscribe_shutdown() -> None:
        """Remove the stop listener only if it has not already fired."""
        nonlocal shutdown_unsub
        if shutdown_unsub is not None:
            shutdown_unsub()
            shutdown_unsub = None

    try:
        await coordinator.async_config_entry_first_refresh()

        if CONF_SITE_ID_MIGRATION in original_data:
            _replay_tracker_migration(hass, entry, coordinator, original_unique_id, original_data)

        if verify_identity:
            await _async_reconcile_site_identity(hass, entry, coordinator, original_unique_id, original_data)

        # Create WebSocket for real-time presence updates
        websocket = UnifiPresenceWebsocket(
            hass,
            lambda: coordinator.controller,
            coordinator.process_message,
        )
        coordinator.websocket = websocket

        entry.runtime_data = coordinator

        if bool(hass.is_stopping):
            await coordinator.async_shutdown()
            return False

        async def _async_shutdown(_event: object) -> None:
            """Stop runtime listeners and release owned sessions on shutdown."""
            nonlocal shutdown_unsub
            shutdown_unsub = None  # The one-shot listener has removed itself.
            await coordinator.async_shutdown()

        shutdown_unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)
        entry.async_on_unload(_unsubscribe_shutdown)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if bool(hass.is_stopping):
            _unsubscribe_shutdown()
            await coordinator.async_shutdown()
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            return False

        websocket.start()
    except Exception:
        _unsubscribe_shutdown()
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPresenceConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    await coordinator.async_shutdown()

    return True
