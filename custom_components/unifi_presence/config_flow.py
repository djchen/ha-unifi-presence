"""Config flow for UniFi Presence integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiounifi
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback

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
from .helpers import create_controller

_LOGGER = logging.getLogger(__name__)


async def _fetch_all_clients(controller: aiounifi.Controller) -> dict[str, str]:
    """Fetch all known clients from the UniFi controller.

    Returns a dict of {mac: display_name}.
    """
    await controller.clients_all.update()
    clients: dict[str, str] = {}
    for mac, client in controller.clients_all.items():
        name = client.name or client.hostname or mac
        clients[mac] = f"{name} ({mac})"
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
        self._controller: aiounifi.Controller | None = None
        self._available_clients: dict[str, str] = {}

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
    ) -> tuple[aiounifi.Controller | None, str | None]:
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
            )
        except (aiounifi.LoginRequired, aiounifi.Unauthorized):
            return None, "invalid_auth"
        except aiounifi.AiounifiException:
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: UniFi controller credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._site = user_input.get(CONF_SITE, DEFAULT_SITE)
            self._ssl_verify = user_input.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY)

            controller, error = await self._async_validate_login(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                site=self._site,
                ssl_verify=self._ssl_verify,
                log_context="UniFi login",
            )
            if error is not None:
                errors["base"] = error
            else:
                self._controller = controller
                # Fetch clients for device selection step
                try:
                    self._available_clients = await _fetch_all_clients(self._controller)
                except Exception:
                    _LOGGER.exception("Failed to fetch client list")
                    self._available_clients = {}

                if not self._available_clients:
                    return self.async_abort(reason="no_devices_discovered")

                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_SITE, default=DEFAULT_SITE): str,
                    vol.Optional(CONF_SSL_VERIFY, default=DEFAULT_SSL_VERIFY): bool,
                }
            ),
            errors=errors,
        )

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
            _, error = await self._async_validate_login(
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
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            site = user_input.get(CONF_SITE, current_data.get(CONF_SITE, DEFAULT_SITE))
            ssl_verify = user_input.get(CONF_SSL_VERIFY, current_data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY))
            unique_id = f"{host}_{site}"

            existing_entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
            if existing_entry is not None and existing_entry.entry_id != reconfigure_entry.entry_id:
                errors["base"] = "already_configured"
            else:
                _, error = await self._async_validate_login(
                    host=host,
                    port=port,
                    username=user_input[CONF_USERNAME],
                    password=user_input[CONF_PASSWORD],
                    site=site,
                    ssl_verify=ssl_verify,
                    log_context="UniFi reconfigure",
                )
                if error is not None:
                    errors["base"] = error
                else:
                    updated_data = dict(current_data)
                    updated_data[CONF_HOST] = host
                    updated_data[CONF_PORT] = port
                    updated_data[CONF_USERNAME] = user_input[CONF_USERNAME]
                    updated_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
                    updated_data[CONF_SITE] = site
                    updated_data[CONF_SSL_VERIFY] = ssl_verify
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        unique_id=unique_id,
                        title=f"UniFi Presence ({host})",
                        data=updated_data,
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current_data.get(CONF_HOST, "")): str,
                    vol.Required(CONF_PORT, default=current_data.get(CONF_PORT, DEFAULT_PORT)): cv.port,
                    vol.Required(CONF_USERNAME, default=current_data.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_SITE, default=current_data.get(CONF_SITE, DEFAULT_SITE)): str,
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
                await self.async_set_unique_id(f"{self._host}_{self._site}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"UniFi Presence ({self._host})",
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

        # Build multi-select options from discovered clients
        client_options: dict[str, str] = {}
        if self._available_clients:
            client_options = dict(sorted(self._available_clients.items(), key=lambda x: x[1].lower()))

        schema_fields: dict[Any, Any] = {}
        if client_options:
            schema_fields[vol.Optional(CONF_TRACKED_DEVICES, default=[])] = cv.multi_select(client_options)

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "client_count": str(len(client_options)),
            },
        )


class UnifiPresenceOptionsFlow(OptionsFlowWithReload):
    """Handle options for UniFi Presence."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tracked = list(user_input.get(CONF_TRACKED_DEVICES, []))

            if not tracked:
                errors["base"] = "no_devices"
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
        available_clients: dict[str, str] = {}
        try:
            coordinator = getattr(self.config_entry, "runtime_data", None)
            if coordinator is not None and coordinator.controller is not None:
                controller = coordinator.controller
            else:
                data = self.config_entry.data
                controller = await create_controller(
                    self.hass,
                    data[CONF_HOST],
                    data[CONF_PORT],
                    data[CONF_USERNAME],
                    data[CONF_PASSWORD],
                    data.get(CONF_SITE, DEFAULT_SITE),
                    data.get(CONF_SSL_VERIFY, DEFAULT_SSL_VERIFY),
                )
            available_clients = await _fetch_all_clients(controller)
        except Exception:
            _LOGGER.warning("Could not fetch UniFi clients for options flow")

        current_options = self.config_entry.options
        current_tracked = current_options.get(CONF_TRACKED_DEVICES, [])

        # Build multi-select with current selections pre-checked
        client_options: dict[str, str] = {}
        if available_clients:
            client_options = dict(sorted(available_clients.items(), key=lambda x: x[1].lower()))

        # Add currently tracked MACs that might not be in the discovered list
        for mac in current_tracked:
            if mac not in client_options:
                client_options[mac] = mac

        schema_fields: dict[Any, Any] = {}
        if client_options:
            schema_fields[vol.Optional(CONF_TRACKED_DEVICES, default=current_tracked)] = cv.multi_select(client_options)
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

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )
