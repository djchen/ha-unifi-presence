"""Config flow for UniFi Presence integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

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
from .helpers import async_close_controller, create_controller, format_config_entry_title, resolve_controller_site

if TYPE_CHECKING:
    from aiounifi.controller import Controller

_LOGGER = logging.getLogger(__name__)


def _site_title(site: Any) -> str:
    """Return the user-facing title for a UniFi site."""
    return str(site.description or site.name)


def _config_entry_title(site: Any, host: str) -> str:
    """Return the config entry title shown in Home Assistant."""
    return format_config_entry_title(_site_title(site), host)


def _normalize_mac(mac: str) -> str:
    """Return a normalized MAC string for config storage and labels."""
    return mac.strip().lower()


def _format_current_client_label(name: str, mac: str) -> str:
    """Return the user-facing label for a current UniFi client."""
    return f"{name} ({mac})"


def _get_selected_site(available_sites: Mapping[str, Any], selected_site: object) -> Any | None:
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
    old_prefix = f"{old_site_id}-"
    new_prefix = f"{new_site_id}-"

    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registry_entry.unique_id.startswith(old_prefix):
            entity_registry.async_update_entity(
                registry_entry.entity_id,
                new_unique_id=f"{new_prefix}{registry_entry.unique_id.removeprefix(old_prefix)}",
            )


def _build_options_client_labels(available_clients: Mapping[str, str], current_tracked: list[str]) -> dict[str, str]:
    """Build ordered option labels for tracked client selection."""
    normalized_available = {_normalize_mac(mac): label for mac, label in available_clients.items()}
    normalized_tracked = [_normalize_mac(mac) for mac in current_tracked]

    preserved_missing = sorted(mac for mac in normalized_tracked if mac not in normalized_available)
    current_clients = sorted(normalized_available.items(), key=lambda item: item[1].lower())

    client_options: dict[str, str] = {}

    for mac in preserved_missing:
        client_options[mac] = f"{mac} (No longer in UniFi Client Devices)"

    for mac, label in current_clients:
        client_options[mac] = label

    return client_options


async def _fetch_sites(controller: Controller) -> dict[str, Any]:
    """Fetch sites from the UniFi controller keyed by site_id."""
    await controller.sites.update()
    return {site.site_id: site for site in controller.sites.values()}


async def _fetch_all_clients(controller: Controller) -> dict[str, str]:
    """Fetch all known clients from the UniFi controller.

    Merges active clients (``controller.clients``) with historical clients
    (``controller.clients_all``).  Both sources are best-effort: if one
    endpoint fails the other is still used.  Active data takes precedence
    so that recently-connected devices that haven't yet appeared in the
    historical endpoint are included.

    Returns a dict of {mac: display_name}.

    Raises:
        Exception: Only if *both* client sources fail to update and
            neither store contains cached data.
    """
    sources_ok = 0
    try:
        await controller.clients_all.update()
        sources_ok += 1
    except Exception:
        _LOGGER.debug("Failed to refresh historical UniFi clients")
    try:
        await controller.clients.update()
        sources_ok += 1
    except Exception:
        _LOGGER.debug("Failed to refresh active UniFi clients")

    if sources_ok == 0:
        # Neither update() succeeded — check if the stores have any cached data
        has_cached = any(True for _ in controller.clients_all) or any(True for _ in controller.clients)
        if not has_cached:
            msg = "Both active and historical client sources failed"
            raise RuntimeError(msg)

    # Merge historical + active; active wins on key collision
    clients: dict[str, str] = {}
    for store in (controller.clients_all, controller.clients):
        for mac, client in store.items():
            mac_lower = mac.lower()
            name = client.name or client.hostname or mac_lower
            clients[mac_lower] = _format_current_client_label(name, mac_lower)
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
        self._ssl_verify: bool = DEFAULT_SSL_VERIFY
        self._site_id: str = ""
        self._site_title: str = ""
        self._available_sites: dict[str, Any] = {}
        self._available_clients: dict[str, str] = {}
        self._site_step_target: str = "user"
        self._single_site_client_error: str | None = None

    def _set_selected_site(self, site: Any) -> None:
        """Persist the currently selected site metadata on the flow."""
        self._site_id = site.site_id
        self._site = site.name
        self._site_title = _site_title(site)

    async def _async_validate_login(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        site: str,
        ssl_verify: bool,
        log_context: str,
    ) -> tuple[Controller | None, str | None]:
        """Attempt controller login and return (controller, error_key)."""
        try:
            controller = await create_controller(
                self.hass,
                host,
                port,
                username,
                password,
                site,
                ssl_verify,
                transient=True,
            )
        except aiounifi.LoginRequired, aiounifi.Unauthorized:
            return None, "invalid_auth"
        except TimeoutError, aiounifi.AiounifiException:
            return None, "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception during %s", log_context)
            return None, "unknown"

        return controller, None

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
                        {site_id: _site_title(site) for site_id, site in self._available_sites.items()}
                    )
                }
            ),
            errors=errors,
        )

    async def _async_finish_reconfigure_site_selection(self, site: Any) -> ConfigFlowResult:
        """Validate and save a selected site during reconfigure."""
        reconfigure_entry = self._get_reconfigure_entry()
        stored_site = reconfigure_entry.data.get(CONF_SITE)
        same_site = self._site_id == reconfigure_entry.unique_id or (
            isinstance(stored_site, str) and stored_site in {self._site_id, self._site}
        )
        if not same_site:
            return self.async_abort(reason="different_site_selected")

        controller, error = await self._async_validate_login(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            site=self._site,
            ssl_verify=self._ssl_verify,
            log_context="UniFi reconfigure site validation",
        )
        if error is not None:
            return self._show_site_form(errors={"base": error})

        assert controller is not None

        try:
            await _fetch_all_clients(controller)
        except Exception:
            _LOGGER.exception("Failed to validate access to the configured UniFi site")
            return self._show_site_form(errors={"base": "cannot_discover_devices"})
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
            title=_config_entry_title(site, self._host),
            data_updates={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SITE: self._site,
                CONF_SSL_VERIFY: self._ssl_verify,
            },
        )

    async def _async_validate_and_fetch_sites(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        site: str,
        ssl_verify: bool,
        log_context: str,
    ) -> str | None:
        """Validate credentials and populate available sites."""
        controller, error = await self._async_validate_login(
            host=host,
            port=port,
            username=username,
            password=password,
            site=site,
            ssl_verify=ssl_verify,
            log_context=log_context,
        )
        if error is not None:
            return error

        assert controller is not None

        try:
            self._available_sites = await _fetch_sites(controller)
            self._available_clients = {}
            self._single_site_client_error = None

            if self._site_step_target == "user" and len(self._available_sites) == 1:
                site_obj = next(iter(self._available_sites.values()))
                self._set_selected_site(site_obj)
                try:
                    self._available_clients = await _fetch_all_clients(controller)
                except Exception:
                    _LOGGER.exception("Failed to fetch client list")
                    self._single_site_client_error = "cannot_discover_devices"
        except Exception:
            _LOGGER.exception("Failed to fetch site list")
            return "cannot_connect"
        finally:
            await async_close_controller(controller)

        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: UniFi controller credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._ssl_verify = user_input.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)
            self._site_step_target = "user"

            error = await self._async_validate_and_fetch_sites(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                site="",
                ssl_verify=self._ssl_verify,
                log_context="UniFi login",
            )
            if error is not None:
                errors["base"] = error
            else:
                if not self._available_sites:
                    return self.async_abort(reason="no_sites_available")
                return await self.async_step_site()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_SSL_VERIFY, default=DEFAULT_SSL_VERIFY): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_site(  # noqa: PLR0911
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle UniFi site selection."""
        if not self._available_sites:
            return self.async_abort(reason="no_sites_available")

        if user_input is not None:
            site = _get_selected_site(self._available_sites, user_input.get(CONF_SITE))
            if site is None:
                return self._show_site_form(errors={"base": "invalid_site"})

            self._set_selected_site(site)

            if self._site_step_target == "reconfigure":
                return await self._async_finish_reconfigure_site_selection(site)

            await self.async_set_unique_id(self._site_id)
            self._abort_if_unique_id_configured()

            if len(self._available_sites) == 1:
                if self._single_site_client_error is not None:
                    return self._show_site_form(errors={"base": self._single_site_client_error})
                if not self._available_clients:
                    return self.async_abort(reason="no_clients_available")
                return await self.async_step_devices()

            controller, error = await self._async_validate_login(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                site=self._site,
                ssl_verify=self._ssl_verify,
                log_context="UniFi site client discovery",
            )
            if error is not None:
                return self._show_site_form(errors={"base": error})

            assert controller is not None

            try:
                self._available_clients = await _fetch_all_clients(controller)
            except Exception:
                _LOGGER.exception("Failed to fetch client list")
                return self._show_site_form(errors={"base": "cannot_discover_devices"})
            finally:
                await async_close_controller(controller)

            if not self._available_clients:
                return self.async_abort(reason="no_clients_available")

            return await self.async_step_devices()

        if len(self._available_sites) == 1:
            return await self.async_step_site({CONF_SITE: next(iter(self._available_sites))})

        return self._show_site_form()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication triggered by ConfigEntryAuthFailed."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        self._site = entry_data.get(CONF_SITE, DEFAULT_SITE)
        self._ssl_verify = entry_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reauthentication confirmation dialog."""
        errors: dict[str, str] = {}

        if user_input is not None:
            controller, error = await self._async_validate_login(
                host=self._host,
                port=self._port,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                site=self._site,
                ssl_verify=self._ssl_verify,
                log_context="UniFi re-authentication",
            )
            if error is not None:
                errors["base"] = error
            else:
                assert controller is not None
                await async_close_controller(controller)
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": self._host},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of controller credentials."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()
        current_data = reconfigure_entry.data

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._ssl_verify = user_input.get(CONF_SSL_VERIFY, current_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY))
            self._site_step_target = "reconfigure"

            error = await self._async_validate_and_fetch_sites(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                site=current_data.get(CONF_SITE, DEFAULT_SITE),
                ssl_verify=self._ssl_verify,
                log_context="UniFi reconfigure",
            )
            if error is not None:
                errors["base"] = error
            else:
                if not self._available_sites:
                    return self.async_abort(reason="no_sites_available")
                return await self.async_step_site()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current_data.get(CONF_HOST, "")): str,
                    vol.Required(CONF_PORT, default=current_data.get(CONF_PORT, DEFAULT_PORT)): cv.port,
                    vol.Required(CONF_USERNAME, default=current_data.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_SSL_VERIFY, default=current_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle device selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tracked = list(user_input.get(CONF_TRACKED_DEVICES, []))

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
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACKED_DEVICES, default=[]): cv.multi_select(client_options),
                }
            ),
            errors=errors,
            description_placeholders={
                "client_count": str(len(client_options)),
            },
        )


class UnifiPresenceOptionsFlow(OptionsFlowWithReload):
    """Handle options for UniFi Presence."""

    async def _async_fetch_available_clients(self) -> tuple[dict[str, str], bool]:
        """Fetch available clients for the options flow."""
        try:
            controller = None
            close_controller = False
            if self.config_entry.state is ConfigEntryState.LOADED:
                coordinator = self.config_entry.runtime_data
                if coordinator is not None and coordinator.controller is not None:
                    controller = coordinator.controller
            if controller is None:
                data = self.config_entry.data
                site = await resolve_controller_site(
                    self.hass,
                    host=data[CONF_HOST],
                    port=data[CONF_PORT],
                    username=data[CONF_USERNAME],
                    password=data[CONF_PASSWORD],
                    site=data.get(CONF_SITE, DEFAULT_SITE),
                    ssl_verify=data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
                    unique_id=self.config_entry.unique_id,
                )
                controller = await create_controller(
                    self.hass,
                    data[CONF_HOST],
                    data[CONF_PORT],
                    data[CONF_USERNAME],
                    data[CONF_PASSWORD],
                    site,
                    data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
                    transient=True,
                )
                close_controller = True
            return await _fetch_all_clients(controller), False
        except Exception:
            _LOGGER.warning("Could not fetch UniFi clients for options flow")
            return {}, True
        finally:
            if close_controller and controller is not None:
                await async_close_controller(controller)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tracked = list(user_input.get(CONF_TRACKED_DEVICES, []))

            if not tracked:
                errors["base"] = "no_tracked_devices"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_TRACKED_DEVICES: tracked,
                        CONF_AWAY_SECONDS: user_input.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS),
                        CONF_FALLBACK_POLL_INTERVAL: user_input.get(
                            CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL
                        ),
                    },
                )

        # Try to reuse the coordinator's authenticated controller, fall back to new login
        available_clients, discovery_failed = await self._async_fetch_available_clients()

        current_options = self.config_entry.options
        current_tracked = [_normalize_mac(mac) for mac in current_options.get(CONF_TRACKED_DEVICES, [])]

        client_options = _build_options_client_labels(available_clients, current_tracked)

        schema_fields: dict[Any, Any] = {}
        if client_options:
            schema_fields[vol.Optional(CONF_TRACKED_DEVICES, default=current_tracked)] = cv.multi_select(client_options)
        else:
            return self.async_abort(reason="cannot_discover_devices" if discovery_failed else "no_devices_discovered")
        schema_fields[
            vol.Optional(
                CONF_AWAY_SECONDS,
                default=current_options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS),
            )
        ] = vol.All(int, vol.Range(min=1))
        schema_fields[
            vol.Optional(
                CONF_FALLBACK_POLL_INTERVAL,
                default=current_options.get(CONF_FALLBACK_POLL_INTERVAL, DEFAULT_FALLBACK_POLL_INTERVAL),
            )
        ] = vol.All(int, vol.Range(min=60))
        if discovery_failed and "base" not in errors:
            errors["base"] = "cannot_discover_devices"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )
