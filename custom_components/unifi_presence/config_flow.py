"""Config flow for UniFi Presence integration."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Literal, SupportsInt, cast

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from aiounifi.models.site import Site
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AWAY_SECONDS,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_SITE,
    CONF_SSL_VERIFY,
    CONF_TRACKED_DEVICES,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_FALLBACK_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SITE,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
)
from .helpers import (
    NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL,
    UNIFI_AUTH_EXCEPTIONS,
    UNIFI_COMMUNICATION_EXCEPTIONS,
    ClientStoreRefreshPolicy,
    ControllerConnectionParams,
    async_close_controller,
    async_refresh_client_stores,
    build_client_labels_from_stores,
    config_entry_site_id,
    create_controller_for_params,
    normalize_mac,
    normalize_macs,
    site_title,
    tracker_unique_id,
)

if TYPE_CHECKING:
    from aiounifi.controller import Controller

_LOGGER = logging.getLogger(__name__)

type FlowErrorKey = Literal[
    "invalid_auth",
    "cannot_connect",
    "cannot_discover_devices",
    "unknown",
]


def _as_int(value: object) -> int:
    """Return an int from a validated flow value."""
    return int(cast(SupportsInt | str, value))


def _build_credentials_schema(defaults: Mapping[str, object] | None = None) -> vol.Schema:
    """Build the initial or defaults-aware reconfigure credential schema."""
    host = vol.Required(CONF_HOST) if defaults is None else vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))
    port = vol.Required(CONF_PORT, default=DEFAULT_PORT if defaults is None else defaults.get(CONF_PORT, DEFAULT_PORT))
    username = (
        vol.Required(CONF_USERNAME)
        if defaults is None
        else vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, ""))
    )
    ssl_verify = vol.Optional(
        CONF_SSL_VERIFY,
        default=DEFAULT_SSL_VERIFY if defaults is None else defaults[CONF_SSL_VERIFY],
    )
    return vol.Schema(
        {
            host: str,
            port: cv.port,
            username: str,
            vol.Required(CONF_PASSWORD): str,
            ssl_verify: bool,
        }
    )


def _build_tracked_device_selector(client_options: Mapping[str, str]) -> SelectSelector:
    """Build a searchable selector for tracked-device choices."""
    options = [SelectOptionDict(value=mac, label=label) for mac, label in client_options.items()]
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            multiple=True,
            custom_value=False,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _find_reconfigure_site(
    available_sites: Mapping[str, Site],
    *,
    entry_unique_id: str | None,
    stored_site: object,
) -> Site | None:
    """Return the already-configured site from the fetched site list."""
    if isinstance(entry_unique_id, str) and (site := available_sites.get(entry_unique_id)) is not None:
        return site

    if not isinstance(stored_site, str):
        return None

    for site in available_sites.values():
        if stored_site in {site.site_id, site.name}:
            return site

    return None


def _is_legacy_or_missing_site_identity(entry_unique_id: str | None) -> bool:
    """Return whether an entry still lacks a stable site_id identity.

    Legacy entries used ``{host}_{site_name}`` as unique_id which contains
    underscores.  Modern entries use the bare UniFi site_id, a 24-character
    hex string (MongoDB ObjectId) that never contains underscores.
    """
    return entry_unique_id is None or "_" in entry_unique_id


@callback
def _async_remove_deselected_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    removed_macs: set[str],
) -> None:
    """Remove entity registry entries for explicitly deselected tracked clients."""
    if not removed_macs:
        return

    entity_registry = er.async_get(hass)
    site_id = config_entry_site_id(entry)
    removed_unique_ids = {tracker_unique_id(site_id, mac) for mac in removed_macs}

    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registry_entry.unique_id in removed_unique_ids:
            entity_registry.async_remove(registry_entry.entity_id)


@callback
def _async_migrate_tracker_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    old_site_id: str,
    new_site_id: str,
) -> None:
    """Migrate tracker entity unique IDs when a legacy entry gains a site_id."""
    if old_site_id == new_site_id:
        return

    entity_registry = er.async_get(hass)
    legacy_prefix = f"{old_site_id}-"

    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if not registry_entry.unique_id.startswith(legacy_prefix):
            continue

        mac = normalize_mac(registry_entry.unique_id.removeprefix(legacy_prefix))
        if not mac:
            continue

        entity_registry.async_update_entity(
            registry_entry.entity_id,
            new_unique_id=tracker_unique_id(new_site_id, mac),
        )


def _build_client_options(
    available_clients: Mapping[str, str],
    current_tracked: Iterable[str] = (),
) -> dict[str, str]:
    """Build ordered client labels for tracked client selection."""
    normalized_available = {normalize_mac(mac): label for mac, label in available_clients.items()}
    normalized_tracked = list(normalize_macs(current_tracked))

    preserved_missing = sorted(mac for mac in normalized_tracked if mac not in normalized_available)
    current_clients = sorted(normalized_available.items(), key=lambda item: item[1].lower())

    client_options: dict[str, str] = {}

    for mac in preserved_missing:
        client_options[mac] = f"{mac} ({NO_LONGER_IN_UNIFI_CLIENT_DEVICES_LABEL})"

    for mac, label in current_clients:
        client_options[mac] = label

    return client_options


async def _fetch_all_clients(controller: Controller) -> dict[str, str]:
    """Fetch all known clients from the UniFi controller."""
    await async_refresh_client_stores(
        controller,
        policy=ClientStoreRefreshPolicy.DISCOVERY,
    )
    return build_client_labels_from_stores(
        controller.clients_all.items(),
        controller.clients.items(),
    )


class UnifiPresenceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Presence."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._username: str = ""
        self._password: str = ""
        self._site: str = DEFAULT_SITE
        self._ssl_verify: bool = DEFAULT_SSL_VERIFY
        self._site_id: str = ""
        self._site_title: str = ""
        self._available_sites: dict[str, Site] = {}
        self._available_clients: dict[str, str] = {}

    def _current_connection_params(self, *, site: str | None = None) -> ControllerConnectionParams:
        """Return the current flow connection parameters."""
        return ControllerConnectionParams(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            site=self._site if site is None else site,
            ssl_verify=self._ssl_verify,
        )

    def _store_connection_input(
        self,
        user_input: Mapping[str, object],
        *,
        ssl_verify_default: bool,
    ) -> None:
        """Persist connection settings from a submitted form."""
        self._host = str(user_input[CONF_HOST])
        self._port = _as_int(user_input[CONF_PORT])
        self._username = str(user_input[CONF_USERNAME])
        self._password = str(user_input[CONF_PASSWORD])
        self._ssl_verify = bool(user_input.get(CONF_SSL_VERIFY, ssl_verify_default))

    def _set_selected_site(self, site: Site) -> None:
        """Persist the currently selected site metadata on the flow."""
        self._site_id = site.site_id
        self._site = site.name
        self._site_title = site_title(site)

    async def _async_validate_login(
        self,
        *,
        params: ControllerConnectionParams,
        log_context: str,
        unique_id: str | None = None,
        resolve_legacy_site: bool = False,
    ) -> tuple[Controller | None, FlowErrorKey | None]:
        """Attempt controller login and return (controller, error_key)."""
        try:
            controller = await create_controller_for_params(
                self.hass,
                params,
                unique_id=unique_id,
                resolve_legacy_site=resolve_legacy_site,
            )
        except UNIFI_AUTH_EXCEPTIONS:
            return None, "invalid_auth"
        except UNIFI_COMMUNICATION_EXCEPTIONS:
            return None, "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception during %s", log_context)
            return None, "unknown"

        return controller, None

    async def _async_discover_clients_from_controller(self, controller: Controller) -> FlowErrorKey | None:
        """Fetch available clients for the currently selected site."""
        try:
            self._available_clients = await _fetch_all_clients(controller)
        except Exception:
            _LOGGER.exception("Failed to fetch client list")
            self._available_clients = {}
            return "cannot_discover_devices"

        return None

    async def _async_load_sites_for_current_connection(
        self,
        *,
        log_context: str,
        site: str,
    ) -> FlowErrorKey | None:
        """Validate credentials and populate accessible sites."""
        controller, error = await self._async_validate_login(
            params=self._current_connection_params(site=site),
            log_context=log_context,
        )
        if error is not None:
            return error

        assert controller is not None

        try:
            await controller.sites.update()
            self._available_sites = {site.site_id: site for site in controller.sites.values()}
            self._available_clients = {}
        except Exception:
            _LOGGER.exception("Failed to fetch site list")
            return "cannot_connect"
        finally:
            await async_close_controller(controller)

        return None

    async def _async_load_selected_site_clients(self, *, log_context: str) -> FlowErrorKey | None:
        """Validate the selected site and refresh its client list."""
        self._available_clients = {}

        controller, error = await self._async_validate_login(
            params=self._current_connection_params(),
            log_context=log_context,
        )
        if error is not None:
            return error

        assert controller is not None
        try:
            return await self._async_discover_clients_from_controller(controller)
        finally:
            await async_close_controller(controller)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> UnifiPresenceOptionsFlow:
        """Get the options flow for this handler."""
        return UnifiPresenceOptionsFlow()

    def _show_site_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """Show the site selection form."""
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE): vol.In(
                        {site_id: site_title(site) for site_id, site in self._available_sites.items()}
                    )
                }
            ),
            errors=errors,
        )

    def _show_single_site_retry_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """Show the single-site retry form after client discovery fails."""
        return self.async_show_form(
            step_id="single_site_retry",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"site": self._site_title or self._site},
        )

    def _show_reconfigure_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """Show the reconfigure form using the latest submitted values when available."""
        current_data = self._get_reconfigure_entry().data
        schema_defaults: Mapping[str, object]
        if self._host:
            schema_defaults = {
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_USERNAME: self._username,
                CONF_SSL_VERIFY: self._ssl_verify,
            }
        else:
            schema_defaults = current_data

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_credentials_schema(schema_defaults),
            errors=errors,
        )

    async def _async_finish_reconfigure(self, site: Site) -> ConfigFlowResult:
        """Validate and save the already-configured site during reconfigure."""
        reconfigure_entry = self._get_reconfigure_entry()
        controller, error = await self._async_validate_login(
            params=self._current_connection_params(),
            log_context="UniFi reconfigure site validation",
        )
        if error is not None:
            return self._show_reconfigure_form(errors={"base": error})

        assert controller is not None
        try:
            if (client_error := await self._async_discover_clients_from_controller(controller)) is not None:
                return self._show_reconfigure_form(errors={"base": client_error})
        finally:
            await async_close_controller(controller)

        _async_migrate_tracker_unique_ids(
            self.hass,
            reconfigure_entry,
            old_site_id=reconfigure_entry.unique_id or reconfigure_entry.entry_id,
            new_site_id=self._site_id,
        )

        return self.async_update_reload_and_abort(
            reconfigure_entry,
            unique_id=self._site_id,
            title=f"{site_title(site)} ({self._host})",
            data_updates={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SITE: self._site,
                CONF_SSL_VERIFY: self._ssl_verify,
            },
        )

    async def _async_finish_user_site_selection(self) -> ConfigFlowResult:
        """Complete site selection for the initial user flow."""
        await self.async_set_unique_id(self._site_id)
        self._abort_if_unique_id_configured()

        is_single_site = len(self._available_sites) == 1
        error = await self._async_load_selected_site_clients(log_context="UniFi site client discovery")
        if error is not None:
            if is_single_site:
                return self._show_single_site_retry_form(errors={"base": error})
            return self._show_site_form(errors={"base": error})

        if not self._available_clients:
            return self.async_abort(reason="no_clients_available")

        return await self.async_step_devices()

    async def async_step_user(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle the initial step: UniFi controller credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._store_connection_input(user_input, ssl_verify_default=DEFAULT_SSL_VERIFY)

            error = await self._async_load_sites_for_current_connection(log_context="UniFi login", site="")
            if error is not None:
                errors["base"] = error
            elif not self._available_sites:
                return self.async_abort(reason="no_sites_available")
            else:
                return await self.async_step_site()

        return self.async_show_form(step_id="user", data_schema=_build_credentials_schema(), errors=errors)

    async def async_step_site(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle UniFi site selection."""
        if not self._available_sites:
            return self.async_abort(reason="no_sites_available")

        if user_input is None:
            if len(self._available_sites) != 1:
                return self._show_site_form()
            self._set_selected_site(next(iter(self._available_sites.values())))
            return await self._async_finish_user_site_selection()

        selected_site = user_input.get(CONF_SITE)
        site = self._available_sites.get(selected_site) if isinstance(selected_site, str) else None
        if site is None:
            return self._show_site_form(errors={"base": "invalid_site"})

        self._set_selected_site(site)
        return await self._async_finish_user_site_selection()

    async def async_step_single_site_retry(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Retry client discovery when only one site is available."""
        if not self._available_sites:
            return self.async_abort(reason="no_sites_available")

        if len(self._available_sites) != 1:
            return self._show_site_form()

        self._set_selected_site(next(iter(self._available_sites.values())))

        if user_input is None:
            return self._show_single_site_retry_form()

        return await self._async_finish_user_site_selection()

    async def async_step_reauth(self, entry_data: Mapping[str, object]) -> ConfigFlowResult:
        """Handle reauthentication triggered by ConfigEntryAuthFailed."""
        self._host = str(entry_data[CONF_HOST])
        self._port = _as_int(entry_data[CONF_PORT])
        self._site = str(entry_data.get(CONF_SITE, DEFAULT_SITE))
        self._ssl_verify = bool(entry_data[CONF_SSL_VERIFY])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle reauthentication confirmation dialog."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            controller, error = await self._async_validate_login(
                params=ControllerConnectionParams(
                    host=self._host,
                    port=self._port,
                    username=str(user_input[CONF_USERNAME]),
                    password=str(user_input[CONF_PASSWORD]),
                    site=self._site,
                    ssl_verify=self._ssl_verify,
                ),
                log_context="UniFi re-authentication",
                unique_id=reauth_entry.unique_id,
                resolve_legacy_site=reauth_entry.unique_id is not None,
            )
            if error is not None:
                errors["base"] = error
            else:
                assert controller is not None
                await async_close_controller(controller)
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_USERNAME: str(user_input[CONF_USERNAME]),
                        CONF_PASSWORD: str(user_input[CONF_PASSWORD]),
                    },
                )

        entry_title = self._get_reauth_entry().title
        suffix = f" ({self._host})"
        site_label = entry_title.removesuffix(suffix) if entry_title.endswith(suffix) else entry_title or self._site

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"site": site_label, "host": self._host},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of controller credentials."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()
        current_data = reconfigure_entry.data

        if user_input is not None:
            self._store_connection_input(
                user_input,
                ssl_verify_default=bool(current_data[CONF_SSL_VERIFY]),
            )
            self._site = str(current_data.get(CONF_SITE, DEFAULT_SITE))

            error = await self._async_load_sites_for_current_connection(
                log_context="UniFi reconfigure",
                site=self._site,
            )
            if error is not None:
                errors["base"] = error
            elif not self._available_sites:
                return self.async_abort(reason="no_sites_available")
            else:
                current_site = _find_reconfigure_site(
                    self._available_sites,
                    entry_unique_id=reconfigure_entry.unique_id,
                    stored_site=current_data.get(CONF_SITE),
                )
                if current_site is None:
                    if (
                        _is_legacy_or_missing_site_identity(reconfigure_entry.unique_id)
                        and len(self._available_sites) == 1
                    ):
                        current_site = next(iter(self._available_sites.values()))
                    else:
                        return self.async_abort(reason="different_site_selected")

                self._set_selected_site(current_site)
                return await self._async_finish_reconfigure(current_site)

        return self._show_reconfigure_form(errors=errors)

    async def async_step_devices(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle device selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tracked = list(normalize_macs(cast(list[str], user_input.get(CONF_TRACKED_DEVICES, []))))
            if not tracked:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title=f"{self._site_title} ({self._host})",
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_SITE: self._site,
                        CONF_SSL_VERIFY: self._ssl_verify,
                    },
                    options={
                        CONF_TRACKED_DEVICES: tracked,
                        CONF_AWAY_SECONDS: DEFAULT_AWAY_SECONDS,
                        CONF_FALLBACK_POLL_INTERVAL: DEFAULT_FALLBACK_POLL_INTERVAL,
                    },
                )

        if not self._available_clients:
            return self.async_abort(reason="no_clients_available")

        client_options = _build_client_options(self._available_clients)
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACKED_DEVICES, default=[]): _build_tracked_device_selector(client_options),
                }
            ),
            errors=errors,
            description_placeholders={"client_count": str(len(client_options))},
        )


class UnifiPresenceOptionsFlow(OptionsFlowWithReload):
    """Handle options for UniFi Presence."""

    async def _async_fetch_available_clients(self) -> tuple[dict[str, str], bool]:
        """Fetch available clients for the options flow."""
        controller: Controller | None = None
        close_controller = False
        try:
            if self.config_entry.state is ConfigEntryState.LOADED:
                coordinator = getattr(self.config_entry, "runtime_data", None)
                if coordinator is not None and getattr(coordinator, "controller", None) is not None:
                    controller = cast("Controller | None", coordinator.controller)

            if controller is None:
                data = self.config_entry.data
                controller = await create_controller_for_params(
                    self.hass,
                    ControllerConnectionParams.from_mapping(data),
                    unique_id=self.config_entry.unique_id,
                    resolve_legacy_site=True,
                )
                close_controller = True

            return await _fetch_all_clients(controller), False
        except Exception:
            _LOGGER.warning("Could not fetch UniFi clients for options flow")
            return {}, True
        finally:
            if close_controller and controller is not None:
                await async_close_controller(controller)

    async def async_step_init(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        current_options = self.config_entry.options
        current_tracked = list(normalize_macs(current_options.get(CONF_TRACKED_DEVICES, [])))

        if user_input is not None:
            tracked = list(normalize_macs(cast(list[str], user_input.get(CONF_TRACKED_DEVICES, []))))
            if not tracked:
                errors["base"] = "no_tracked_devices"
            else:
                removed_macs = set(current_tracked) - set(tracked)
                _async_remove_deselected_entities(self.hass, self.config_entry, removed_macs)
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_TRACKED_DEVICES: tracked,
                        CONF_AWAY_SECONDS: _as_int(user_input.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS)),
                        CONF_FALLBACK_POLL_INTERVAL: _as_int(
                            user_input.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL)
                        ),
                    },
                )

        available_clients, discovery_failed = await self._async_fetch_available_clients()
        client_options = _build_client_options(available_clients, current_tracked)

        if not client_options:
            return self.async_abort(reason="cannot_discover_devices" if discovery_failed else "no_devices_discovered")

        schema_fields: dict[object, object] = {
            vol.Optional(CONF_TRACKED_DEVICES, default=current_tracked): _build_tracked_device_selector(client_options),
            vol.Optional(
                CONF_AWAY_SECONDS,
                default=current_options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS),
            ): vol.All(int, vol.Range(min=1)),
            vol.Optional(
                CONF_FALLBACK_POLL_INTERVAL,
                default=current_options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL),
            ): vol.All(int, vol.Range(min=60)),
        }

        if discovery_failed and "base" not in errors:
            errors["base"] = "cannot_discover_devices"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )
