"""Tests for UniFi Presence helper utilities."""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.helpers import async_close_controller, create_controller


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
            host="192.168.1.1",
            port=443,
            username="admin",
            password="password",
            site="default",
            ssl_verify=True,
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
            host="192.168.1.1",
            port=8443,
            username="admin",
            password="password",
            site="office",
            ssl_verify=False,
        )

    create_session.assert_called_once()
    call_args = create_session.call_args
    assert call_args.args[0] is hass
    call_kwargs = call_args.kwargs
    assert call_kwargs["verify_ssl"] is False
    assert "cookie_jar" in call_kwargs
    jar = call_kwargs["cookie_jar"]
    assert getattr(jar, "_unsafe", False) is True
    assert configuration.call_args.kwargs["ssl_context"] is False


async def test_create_controller_closes_transient_ssl_false_session(hass: HomeAssistant) -> None:
    """Test transient SSL-disabled controllers own and close their session."""
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
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
            host="192.168.1.1",
            port=8443,
            username="admin",
            password="password",
            site="office",
            ssl_verify=False,
            transient=True,
        )
        await async_close_controller(result)

    assert result is controller
    assert create_session.call_args.kwargs["auto_cleanup"] is False
    session.close.assert_awaited_once()


async def test_create_controller_closes_transient_session_on_login_failure(hass: HomeAssistant) -> None:
    """Test transient SSL-disabled sessions are cleaned up if login fails."""
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
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
            host="192.168.1.1",
            port=8443,
            username="admin",
            password="password",
            site="office",
            ssl_verify=False,
            transient=True,
        )

    session.close.assert_awaited_once()
