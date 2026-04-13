"""Tests for UniFi Presence helper utilities."""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.helpers import (
    ControllerConnectionParams,
    async_close_controller,
    create_controller,
    normalize_macs,
    tracker_unique_id,
)

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


def test_tracker_unique_id_uses_normalized_mac() -> None:
    """Test tracker unique IDs are site-scoped and normalized."""
    assert tracker_unique_id("default", " AA:BB:CC:DD:EE:FF ") == "default-aa:bb:cc:dd:ee:ff"


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
