"""Config flow for UniFi Presence integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from json import JSONDecodeError
from typing import TYPE_CHECKING, Literal, SupportsInt, cast

import aiohttp
import aiounifi
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
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
    DEFAULT_SETUP_SSL_VERIFY,
    DEFAULT_SITE,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
)
from .helpers import (
    ClientLike,
    ControllerConnectionParams,
    SiteLike,
    async_close_controller,
    create_controller,
    format_config_entry_title,
    format_current_client_label,
    format_missing_client_label,
    format_site_config_entry_title,
    normalize_mac,
    normalize_macs,
    resolve_controller_site,
    site_title,
    tracker_unique_id,
)

if TYPE_CHECKING:
    from aiounifi.controller import Controller

_LOGGER = logging.getLogger(__name__)

type SiteStepTarget = Literal["user", "reconfigure"]
type FlowErrorKey = Literal[
    "invalid_auth",
    "cannot_connect",
    "cannot_discover_devices",
    "invalid_site",
    "no_devices",
    "no_tracked_devices",
    "unknown",
]


def _as_int(value: object) -> int:
    """Return an int from a validated flow value."""
    return int(cast(SupportsInt | str, value))


def _build_user_schema() -> vol.Schema:
    """Build the credential form schema for initial setup."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_SSL_VERIFY, default=DEFAULT_SETUP_SSL_VERIFY): bool,
        }
    )


def _build_reconfigure_schema(current_data: Mapping[str, object]) -> vol.Schema:
    """Build the credential form schema for reconfigure."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=current_data.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=current_data.get(CONF_PORT, DEFAULT_PORT)): cv.port,
            vol.Required(CONF_USERNAME, default=current_data.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_SSL_VERIFY, default=current_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)): bool,
        }
    )


def _build_reauth_schema() -> vol.Schema:
    """Build the credential form schema for reauthentication."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _build_site_schema(available_sites: Mapping[str, SiteLike]) -> vol.Schema:
    """Build the site selection schema."""
    return vol.Schema(
        {vol.Required(CONF_SITE): vol.In({site_id: site_title(site) for site_id, site in available_sites.items()})}
    )


def _build_device_selection_schema(
    client_options: Mapping[str, str],
    *,
    default_selected: list[str] | None = None,
) -> vol.Schema:
    """Build the tracked-device multi-select schema."""
    return vol.Schema(
        {
            vol.Optional(CONF_TRACKED_DEVICES, default=default_selected or []): _build_tracked_device_selector(
                client_options
            ),
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


def _get_selected_site(available_sites: Mapping[str, SiteLike], selected_site: object) -> SiteLike | None:
    """Return the selected site object when the flow input is valid."""
    if not isinstance(selected_site, str):
        return None

    return available_sites.get(selected_site)


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

    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        # Extract the MAC from the last segment of "{site_id}-{mac}".
        # rsplit with maxsplit=1 handles site IDs containing dashes.
        # Entities whose unique_id lacks a dash are safely skipped because
        # the reconstructed legacy_unique_id will never match.
        for mac in normalize_macs([registry_entry.unique_id.rsplit("-", maxsplit=1)[-1]]):
            legacy_unique_id = tracker_unique_id(old_site_id, mac)
            if registry_entry.unique_id != legacy_unique_id:
                continue

            entity_registry.async_update_entity(
                registry_entry.entity_id,
                new_unique_id=tracker_unique_id(new_site_id, mac),
            )


def _build_options_client_labels(available_clients: Mapping[str, str], current_tracked: list[str]) -> dict[str, str]:
    """Build ordered option labels for tracked client selection."""
    normalized_available = {normalize_mac(mac): label for mac, label in available_clients.items()}
    normalized_tracked = list(normalize_macs(current_tracked))

    preserved_missing = sorted(mac for mac in normalized_tracked if mac not in normalized_available)
    current_clients = sorted(normalized_available.items(), key=lambda item: item[1].lower())

    client_options: dict[str, str] = {}

    for mac in preserved_missing:
        client_options[mac] = format_missing_client_label(mac)

    for mac, label in current_clients:
        client_options[mac] = label

    return client_options


async def _fetch_sites(controller: Controller) -> dict[str, SiteLike]:
    """Fetch sites from the UniFi controller keyed by site_id."""
    await controller.sites.update()
    return {site.site_id: cast(SiteLike, site) for site in controller.sites.values()}


async def _fetch_all_clients(controller: Controller) -> dict[str, str]:
    """Fetch all known clients from the UniFi controller."""
    historical_refreshed = False
    try:
        await controller.clients_all.update()
        historical_refreshed = True
    except TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError:
        _LOGGER.debug("Failed to refresh historical UniFi clients")

    active_refreshed = False
    try:
        await controller.clients.update()
        active_refreshed = True
    except TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError:
        _LOGGER.debug("Failed to refresh active UniFi clients")

    if not historical_refreshed and not active_refreshed:
        has_cached = any(controller.clients_all) or any(controller.clients)
        if not has_cached:
            msg = "Both active and historical client sources failed"
            raise RuntimeError(msg)

    clients: dict[str, str] = {}
    for store in (controller.clients_all, controller.clients):
        for mac, client in store.items():
            client_obj = cast(ClientLike, client)
            mac_lower = normalize_mac(mac)
            name = client_obj.name or client_obj.hostname or mac_lower
            clients[mac_lower] = format_current_client_label(str(name), mac_lower)

    return clients


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
        self._ssl_verify: bool = DEFAULT_SETUP_SSL_VERIFY
        self._site_id: str = ""
        self._site_title: str = ""
        self._available_sites: dict[str, SiteLike] = {}
        self._available_clients: dict[str, str] = {}
        self._site_step_target: SiteStepTarget = "user"
        self._single_site_discovery_error: FlowErrorKey | None = None

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

    def _set_selected_site(self, site: SiteLike) -> None:
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
    ) -> tuple[Controller | None, FlowErrorKey | None]:
        """Attempt controller login and return (controller, error_key)."""
        try:
            resolved_site = params.site
            if unique_id is not None:
                resolved_site = await resolve_controller_site(
                    self.hass,
                    params,
                    unique_id=unique_id,
                )

            controller = await create_controller(
                self.hass,
                replace(params, site=resolved_site),
            )
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            return None, "invalid_auth"
        except TimeoutError, aiounifi.AiounifiException, aiohttp.ClientError, JSONDecodeError:
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

    async def _async_load_sites_for_current_connection(self, *, log_context: str) -> FlowErrorKey | None:
        """Validate credentials and populate accessible sites."""
        controller, error = await self._async_validate_login(
            params=self._current_connection_params(site="" if self._site_step_target == "user" else self._site),
            log_context=log_context,
        )
        if error is not None:
            return error

        assert controller is not None

        try:
            self._available_sites = await _fetch_sites(controller)
            self._available_clients = {}
            self._single_site_discovery_error = None

            if self._site_step_target == "user" and len(self._available_sites) == 1:
                site = next(iter(self._available_sites.values()))
                self._set_selected_site(site)
                self._single_site_discovery_error = await self._async_discover_clients_from_controller(controller)
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
            data_schema=_build_site_schema(self._available_sites),
            errors=errors,
        )

    async def _async_finish_reconfigure_site_selection(self, site: SiteLike) -> ConfigFlowResult:
        """Validate and save a selected site during reconfigure."""
        reconfigure_entry = self._get_reconfigure_entry()
        stored_site = reconfigure_entry.data.get(CONF_SITE)
        same_site = self._site_id == reconfigure_entry.unique_id or (
            isinstance(stored_site, str) and stored_site in {self._site_id, self._site}
        )
        if not same_site:
            return self.async_abort(reason="different_site_selected")

        controller, error = await self._async_validate_login(
            params=self._current_connection_params(),
            log_context="UniFi reconfigure site validation",
        )
        if error is not None:
            return self._show_site_form(errors={"base": error})

        assert controller is not None
        try:
            if (client_error := await self._async_discover_clients_from_controller(controller)) is not None:
                return self._show_site_form(errors={"base": client_error})
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
            title=format_site_config_entry_title(site, self._host),
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

        if len(self._available_sites) == 1:
            return await self._async_finish_single_site_user_selection()

        return await self._async_finish_multi_site_user_selection()

    async def _async_finish_single_site_user_selection(self) -> ConfigFlowResult:
        """Complete user setup when only one site is available."""
        if self._single_site_discovery_error is not None:
            self._single_site_discovery_error = await self._async_load_selected_site_clients(
                log_context="UniFi site client discovery",
            )
            if self._single_site_discovery_error is not None:
                return self._show_site_form(errors={"base": self._single_site_discovery_error})

        if not self._available_clients:
            return self.async_abort(reason="no_clients_available")

        return await self.async_step_devices()

    async def _async_finish_multi_site_user_selection(self) -> ConfigFlowResult:
        """Complete user setup after explicit multi-site selection."""
        error = await self._async_load_selected_site_clients(log_context="UniFi site client discovery")
        if error is not None:
            return self._show_site_form(errors={"base": error})

        if not self._available_clients:
            return self.async_abort(reason="no_clients_available")

        return await self.async_step_devices()

    async def async_step_user(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle the initial step: UniFi controller credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._store_connection_input(user_input, ssl_verify_default=DEFAULT_SETUP_SSL_VERIFY)
            self._site_step_target = "user"

            error = await self._async_load_sites_for_current_connection(log_context="UniFi login")
            if error is not None:
                errors["base"] = error
            elif not self._available_sites:
                return self.async_abort(reason="no_sites_available")
            else:
                return await self.async_step_site()

        return self.async_show_form(step_id="user", data_schema=_build_user_schema(), errors=errors)

    async def async_step_site(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle UniFi site selection."""
        if not self._available_sites:
            return self.async_abort(reason="no_sites_available")

        if user_input is None:
            return await self._async_handle_site_step_without_input()

        site = _get_selected_site(self._available_sites, user_input.get(CONF_SITE))
        if site is None:
            return self._show_site_form(errors={"base": "invalid_site"})

        self._set_selected_site(site)
        if self._site_step_target == "reconfigure":
            return await self._async_finish_reconfigure_site_selection(site)

        return await self._async_finish_user_site_selection()

    async def _async_handle_site_step_without_input(self) -> ConfigFlowResult:
        """Handle the site step when the user has not submitted a site yet."""
        if len(self._available_sites) != 1:
            return self._show_site_form()

        if self._single_site_discovery_error is not None:
            return self._show_site_form(errors={"base": self._single_site_discovery_error})

        return await self.async_step_site({CONF_SITE: next(iter(self._available_sites))})

    async def async_step_reauth(self, entry_data: Mapping[str, object]) -> ConfigFlowResult:
        """Handle reauthentication triggered by ConfigEntryAuthFailed."""
        self._host = str(entry_data[CONF_HOST])
        self._port = _as_int(entry_data[CONF_PORT])
        self._site = str(entry_data.get(CONF_SITE, DEFAULT_SITE))
        self._ssl_verify = bool(entry_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle reauthentication confirmation dialog."""
        errors: dict[str, str] = {}

        if user_input is not None:
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
                unique_id=self._get_reauth_entry().unique_id,
            )
            if error is not None:
                errors["base"] = error
            else:
                assert controller is not None
                await async_close_controller(controller)
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_USERNAME: str(user_input[CONF_USERNAME]),
                        CONF_PASSWORD: str(user_input[CONF_PASSWORD]),
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_build_reauth_schema(),
            description_placeholders={"host": self._host},
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
                ssl_verify_default=bool(current_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)),
            )
            self._site = str(current_data.get(CONF_SITE, DEFAULT_SITE))
            self._site_step_target = "reconfigure"

            error = await self._async_load_sites_for_current_connection(log_context="UniFi reconfigure")
            if error is not None:
                errors["base"] = error
            elif not self._available_sites:
                return self.async_abort(reason="no_sites_available")
            else:
                return await self.async_step_site()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_reconfigure_schema(current_data),
            errors=errors,
        )

    async def async_step_devices(self, user_input: dict[str, object] | None = None) -> ConfigFlowResult:
        """Handle device selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tracked = list(normalize_macs(cast(list[str], user_input.get(CONF_TRACKED_DEVICES, []))))
            if not tracked:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title=format_config_entry_title(self._site_title, self._host),
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

        client_options = dict(sorted(self._available_clients.items(), key=lambda item: item[1].lower()))
        return self.async_show_form(
            step_id="devices",
            data_schema=_build_device_selection_schema(client_options),
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
                params = ControllerConnectionParams.from_mapping(data)
                resolved_site = await resolve_controller_site(
                    self.hass,
                    params,
                    unique_id=self.config_entry.unique_id,
                )
                params = replace(params, site=resolved_site)
                controller = await create_controller(
                    self.hass,
                    params,
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
        client_options = _build_options_client_labels(available_clients, current_tracked)

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
