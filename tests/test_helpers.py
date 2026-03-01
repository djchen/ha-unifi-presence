"""Tests for UniFi Presence helper utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.helpers import create_controller


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
            "custom_components.unifi_presence.helpers.aiounifi.Controller",
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
    configuration.assert_called_once_with(
        session,
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="default",
        ssl_context=True,
    )
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
        patch("custom_components.unifi_presence.helpers.aiounifi.Controller", return_value=controller),
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
