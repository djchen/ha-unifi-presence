"""Tests for safe integration debug logging defaults."""

import logging
from json import dumps
from unittest.mock import patch

import aiounifi
import pytest
from aiounifi.models.api import ApiRequest
from homeassistant.components.logger.helpers import DATA_LOGGER, LoggerSetting, set_log_levels
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.unifi_presence.const import DOMAIN
from custom_components.unifi_presence.helpers import SiteSnapshotError

from .conftest import _assert_session_cleanup, _attach_mock_session, add_mock_config_entry, make_mock_controller


async def test_integration_debug_does_not_enable_aiounifi(
    hass: HomeAssistant, enable_custom_integrations: None, caplog: pytest.LogCaptureFixture
) -> None:
    """HA's real integration debug setting must not enable secret-bearing library logs."""
    integration_logger = logging.getLogger(f"custom_components.{DOMAIN}")
    dependency_logger = logging.getLogger("aiounifi.interfaces.connectivity")
    original_levels = {
        name: logging.getLogger(name).level
        for name in (
            f"homeassistant.components.{DOMAIN}",
            integration_logger.name,
            "aiounifi",
            "aiounifi.interfaces",
            dependency_logger.name,
        )
    }
    original_logger_class = logging.getLoggerClass()
    try:
        assert await async_setup_component(hass, "logger", {})
        logger_config = hass.data[DATA_LOGGER]
        original_overrides = dict(logger_config.overrides)
        try:
            set_log_levels(
                hass,
                {
                    integration_logger.name: logging.WARNING,
                    "aiounifi": logging.WARNING,
                    "aiounifi.interfaces": logging.NOTSET,
                    dependency_logger.name: logging.NOTSET,
                },
            )
            assert not dependency_logger.isEnabledFor(logging.DEBUG)

            await logger_config.settings.async_update(hass, DOMAIN, LoggerSetting("DEBUG", "none", "integration"))

            assert integration_logger.isEnabledFor(logging.DEBUG)
            assert logging.getLogger("aiounifi").level == logging.WARNING
            assert not dependency_logger.isEnabledFor(logging.DEBUG)
            caplog.clear()
            integration_logger.debug("Integration debug remains available")
            dependency_logger.debug("DUMMY-PASSWORD-AND-SESSION-TOKEN")
            assert "Integration debug remains available" in caplog.text
            assert "DUMMY-PASSWORD-AND-SESSION-TOKEN" not in caplog.text
        finally:
            set_log_levels(hass, original_levels)
            logger_config.overrides.clear()
            logger_config.overrides.update(original_overrides)
    finally:
        logging.setLoggerClass(original_logger_class)


@pytest.mark.parametrize(
    ("failure", "state", "key"),
    [
        ("api.err.Invalid", ConfigEntryState.SETUP_ERROR, "credentials_rejected"),
        ("api.err.LoginRequired", ConfigEntryState.SETUP_ERROR, "credentials_rejected"),
        ("api.err.NoPermission", ConfigEntryState.SETUP_RETRY, "cannot_connect"),
        ("nested_auth", ConfigEntryState.SETUP_ERROR, "credentials_rejected"),
        ("nested_communication", ConfigEntryState.SETUP_RETRY, "cannot_connect"),
        ("nested_structure", ConfigEntryState.SETUP_RETRY, "cannot_verify_site_identity"),
    ],
)
async def test_identity_setup_failure_debug_hides_response_and_causes(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    state: ConfigEntryState,
    key: str,
) -> None:
    """HA's real setup diagnostics must not expose decoder payloads or nested causes."""
    secret = "DUMMY-SITE-RESPONSE-PASSWORD-AND-SESSION-TOKEN"
    entry = add_mock_config_entry(hass, unique_id="5f0123456789abcdef012345")
    original_data = dict(entry.data)
    controller = make_mock_controller()
    controller.connectivity.can_retry_login = False
    session = _attach_mock_session(controller)
    caplog.set_level(logging.WARNING, logger="aiounifi")
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")
    caplog.set_level(logging.DEBUG, logger="homeassistant.config_entries")
    assert logging.getLogger(f"custom_components.{DOMAIN}").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("aiounifi.interfaces.connectivity").isEnabledFor(logging.DEBUG)

    async def fail_request(request: ApiRequest) -> None:
        if failure.startswith("api.err."):
            request.decode(dumps({"meta": {"rc": "error", "msg": failure}, "data": [{"secret": secret}]}).encode())
            pytest.fail("The real aiounifi decoder should reject the error response")
        error_type = {
            "nested_auth": aiounifi.Unauthorized,
            "nested_communication": aiounifi.AiounifiException,
            "nested_structure": SiteSnapshotError,
        }[failure]
        raise error_type("Sanitized request failure") from ValueError(secret)

    controller.request.side_effect = fail_request
    with patch("custom_components.unifi_presence.coordinator.create_controller_for_params", return_value=controller):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is state
    assert entry.error_reason_translation_key == key
    assert entry.error_reason_translation_placeholders == (
        {"host": original_data[CONF_HOST]} if key == "cannot_connect" else None
    )
    assert entry.unique_id == "5f0123456789abcdef012345"
    assert dict(entry.data) == original_data
    controller.request.assert_awaited_once()
    controller.start_websocket.assert_not_awaited()
    _assert_session_cleanup(session)
    diagnostics = [
        record
        for record in caplog.records
        if record.name == "homeassistant.config_entries" and record.getMessage() == "Full exception"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0].exc_info is not None
    assert f"homeassistant.exceptions.ConfigEntry{'AuthFailed' if key == 'credentials_rejected' else 'NotReady'}" in (
        caplog.text
    )
    assert secret not in caplog.text
    assert "Sanitized request failure" not in caplog.text
