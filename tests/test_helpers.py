"""Tests for UniFi Presence helper utilities."""

from __future__ import annotations

import asyncio
import ssl
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.helpers import (
    ControllerConnectionParams,
    async_close_controller,
    build_entry_runtime_summary,
    create_controller,
    format_config_entry_title,
    format_current_client_label,
    format_missing_client_label,
    normalize_mac,
    normalize_macs,
    site_title,
    tracker_unique_id,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_OPTIONS

_SSL_PARAMS = ControllerConnectionParams(
    host="192.168.1.1", port=443, username="admin", password="password", site="default", ssl_verify=True
)
_NO_SSL_PARAMS = ControllerConnectionParams(
    host="192.168.1.1", port=8443, username="admin", password="password", site="office", ssl_verify=False
)


async def test_create_controller_logs_in_with_ssl_verify(hass: HomeAssistant) -> None:
    """Test helper builds controller config, then logs in and returns controller."""
    session = MagicMock()
    config = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock()

    with (
        patch("custom_components.unifi_presence.helpers.async_get_clientsession", return_value=session) as get_session,
        patch("custom_components.unifi_presence.helpers.Configuration", return_value=config) as configuration,
        patch(
            "custom_components.unifi_presence.helpers.Controller",
            return_value=controller,
        ) as controller_factory,
    ):
        result = await create_controller(
            hass,
            _SSL_PARAMS,
        )

    assert result is controller
    get_session.assert_called_once_with(hass)
    # ssl_verify=True should produce a real SSLContext, not the boolean True
    call_kwargs = configuration.call_args.kwargs
    assert isinstance(call_kwargs["ssl_context"], ssl.SSLContext)
    controller_factory.assert_called_once_with(config)
    controller.login.assert_awaited_once()


async def test_create_controller_passes_ssl_false(hass: HomeAssistant) -> None:
    """Test helper uses async_create_clientsession with unsafe CookieJar when SSL disabled."""
    session = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock()

    with (
        patch(
            "custom_components.unifi_presence.helpers.async_create_clientsession", return_value=session
        ) as create_session,
        patch("custom_components.unifi_presence.helpers.Configuration") as configuration,
        patch("custom_components.unifi_presence.helpers.Controller", return_value=controller),
    ):
        await create_controller(
            hass,
            _NO_SSL_PARAMS,
        )

    create_session.assert_called_once()
    call_args = create_session.call_args
    assert call_args.args[0] is hass
    call_kwargs = call_args.kwargs
    assert call_kwargs["verify_ssl"] is False
    assert call_kwargs["auto_cleanup"] is False
    assert "cookie_jar" in call_kwargs
    jar = call_kwargs["cookie_jar"]
    assert getattr(jar, "_unsafe", False) is True
    assert configuration.call_args.kwargs["ssl_context"] is False


async def test_create_controller_closes_ssl_false_owned_session(hass: HomeAssistant) -> None:
    """Test SSL-disabled controllers detach their owned session on cleanup."""
    session = MagicMock()
    session.closed = False
    session.detach = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock()

    with (
        patch(
            "custom_components.unifi_presence.helpers.async_create_clientsession", return_value=session
        ) as create_session,
        patch("custom_components.unifi_presence.helpers.Configuration"),
        patch("custom_components.unifi_presence.helpers.Controller", return_value=controller),
    ):
        result = await create_controller(
            hass,
            _NO_SSL_PARAMS,
        )
        await async_close_controller(result)

    assert result is controller
    assert create_session.call_args.kwargs["auto_cleanup"] is False
    session.detach.assert_called_once_with()


def test_normalize_macs_deduplicates_and_preserves_order() -> None:
    """Test shared MAC normalization trims, lowercases, and deduplicates."""
    assert normalize_macs([" AA:BB:CC:DD:EE:FF ", "", "aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]) == (
        "aa:bb:cc:dd:ee:ff",
        "11:22:33:44:55:66",
    )


def test_normalize_mac_trims_and_lowercases() -> None:
    """Test shared MAC normalization trims whitespace and lowercases."""
    assert normalize_mac(" AA:BB:CC:DD:EE:FF ") == "aa:bb:cc:dd:ee:ff"


def test_tracker_unique_id_uses_normalized_mac() -> None:
    """Test tracker unique IDs are site-scoped and normalized."""
    assert tracker_unique_id("default", " AA:BB:CC:DD:EE:FF ") == "default-aa:bb:cc:dd:ee:ff"


def test_site_title_prefers_description_then_name() -> None:
    """Test site titles use description when present and fall back to name."""
    site = SimpleNamespace(site_id="site-id", name="office", description="Office")
    assert site_title(site) == "Office"

    unnamed_description = SimpleNamespace(site_id="site-id", name="office", description="")
    assert site_title(unnamed_description) == "office"


def test_format_config_entry_title() -> None:
    """Test config entry titles combine site title and host."""
    assert format_config_entry_title("Home", "192.168.1.1") == "Home (192.168.1.1)"


def test_client_label_helpers_normalize_mac() -> None:
    """Test client label helpers normalize MACs before formatting."""
    assert format_current_client_label("Dan Phone", " AA:BB:CC:DD:EE:FF ") == ("Dan Phone (aa:bb:cc:dd:ee:ff)")
    assert format_missing_client_label(" AA:BB:CC:DD:EE:FF ") == (
        "aa:bb:cc:dd:ee:ff (No longer in UniFi Client Devices)"
    )


def test_controller_connection_params_from_mapping_uses_override_site() -> None:
    """Test typed controller params honor an explicit site override."""
    params = ControllerConnectionParams.from_mapping(MOCK_CONFIG_DATA, site="office")

    assert params == ControllerConnectionParams(
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="office",
        ssl_verify=False,
    )


def test_build_entry_runtime_summary_uses_entry_options_without_runtime_data() -> None:
    """Test runtime summary falls back to normalized config-entry options."""
    entry = SimpleNamespace(
        options={
            **MOCK_OPTIONS,
            "tracked_devices": [" AA:BB:CC:DD:EE:FF ", "aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
            "away_seconds": 90,
            "fallback_poll_interval": 600,
        }
    )

    summary = build_entry_runtime_summary(entry)

    assert summary.tracked_macs == (
        "aa:bb:cc:dd:ee:ff",
        "11:22:33:44:55:66",
    )
    assert summary.tracked_device_count == 2
    assert summary.away_seconds == 90
    assert summary.fallback_poll_interval_seconds == 600.0
    assert summary.websocket_connected is False
    assert summary.heartbeat_expiry_count == 0
    assert summary.last_update_success is None


def test_build_entry_runtime_summary_prefers_loaded_coordinator_state() -> None:
    """Test runtime summary uses live coordinator values when available."""
    coordinator = SimpleNamespace(
        tracked_devices=("aa:bb:cc:dd:ee:ff",),
        away_seconds=120,
        update_interval=timedelta(seconds=450),
        heartbeat_expiry_count=3,
        last_update_success=True,
        data=object(),
        controller=object(),
        websocket=SimpleNamespace(available=True),
    )
    entry = SimpleNamespace(options=MOCK_OPTIONS, runtime_data=coordinator)

    summary = build_entry_runtime_summary(entry)

    assert summary.tracked_macs == ("aa:bb:cc:dd:ee:ff",)
    assert summary.tracked_device_count == 1
    assert summary.away_seconds == 120
    assert summary.fallback_poll_interval_seconds == 450.0
    assert summary.websocket_connected is True
    assert summary.heartbeat_expiry_count == 3
    assert summary.last_update_success is True


async def test_create_controller_closes_owned_session_on_login_failure(hass: HomeAssistant) -> None:
    """Test SSL-disabled sessions are detached if login fails."""
    session = MagicMock()
    session.closed = False
    session.detach = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=TimeoutError)

    with (
        patch("custom_components.unifi_presence.helpers.async_create_clientsession", return_value=session),
        patch("custom_components.unifi_presence.helpers.Configuration"),
        patch("custom_components.unifi_presence.helpers.Controller", return_value=controller),
        pytest.raises(TimeoutError),
    ):
        await create_controller(
            hass,
            _NO_SSL_PARAMS,
        )

    session.detach.assert_called_once_with()


async def test_create_controller_closes_owned_session_on_login_cancellation(hass: HomeAssistant) -> None:
    """Test SSL-disabled sessions are detached if login is cancelled."""
    session = MagicMock()
    session.closed = False
    session.detach = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch("custom_components.unifi_presence.helpers.async_create_clientsession", return_value=session),
        patch("custom_components.unifi_presence.helpers.Configuration"),
        patch("custom_components.unifi_presence.helpers.Controller", return_value=controller),
        pytest.raises(asyncio.CancelledError),
    ):
        await create_controller(
            hass,
            _NO_SSL_PARAMS,
        )

    session.detach.assert_called_once_with()
