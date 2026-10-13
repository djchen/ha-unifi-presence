"""Tests for safe integration debug logging defaults."""

import logging

import pytest
from homeassistant.components.logger.helpers import DATA_LOGGER, LoggerSetting, set_log_levels
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.unifi_presence.const import DOMAIN


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
