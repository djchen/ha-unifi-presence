"""Tests for UniFi Presence helper utilities."""

import asyncio
import ssl
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import CookieJar
from homeassistant.core import HomeAssistant
from yarl import URL

from custom_components.unifi_presence.helpers import (
    ControllerConnectionParams,
    async_close_controller,
    async_refresh_client_stores,
    create_controller,
    create_controller_for_params,
    normalize_mac,
    normalize_macs,
    site_title,
    tracker_unique_id,
)

from .conftest import _assert_session_cleanup, _attach_mock_session, _make_mock_client, make_mock_controller

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
    assert isinstance(jar, CookieJar)
    controller_url = URL("https://192.168.1.1")
    jar.update_cookies({"session": "test-token"}, response_url=controller_url)
    assert jar.filter_cookies(controller_url)["session"].value == "test-token"
    assert configuration.call_args.kwargs["ssl_context"] is False


async def test_create_controller_closes_ssl_false_owned_session(hass: HomeAssistant) -> None:
    """Test SSL-disabled controllers detach their owned session on cleanup."""
    controller = MagicMock()
    controller.login = AsyncMock()
    session = _attach_mock_session(controller, owned=False)

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
    _assert_session_cleanup(session)


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


async def test_create_controller_for_params_uses_legacy_site_resolution(hass: HomeAssistant) -> None:
    """Test the shared controller helper resolves a legacy site on one controller."""
    params = replace(_SSL_PARAMS, site="site-office-id", ssl_verify=False)
    controller = MagicMock()
    controller.connectivity = SimpleNamespace(config=SimpleNamespace(site=""))
    controller.sites.update = AsyncMock()
    controller.sites.values.return_value = [SimpleNamespace(site_id="site-office-id", name="office")]

    with patch(
        "custom_components.unifi_presence.helpers.create_controller",
        return_value=controller,
    ) as create_ctrl:
        result = await create_controller_for_params(
            hass,
            params,
            unique_id="192.168.1.1_office",
            resolve_legacy_site=True,
        )

    assert result is controller
    create_ctrl.assert_awaited_once()
    assert create_ctrl.await_args.args[1].site == ""
    assert controller.connectivity.config.site == "office"


async def test_create_controller_for_params_skips_resolution_for_new_setup(hass: HomeAssistant) -> None:
    """Test the shared controller helper can create a site-scoped controller directly."""
    params = replace(_SSL_PARAMS, site="office", ssl_verify=False)
    controller = MagicMock()

    with patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller) as create_ctrl:
        result = await create_controller_for_params(hass, params)

    assert result is controller
    create_ctrl.assert_awaited_once_with(hass, params)


async def test_create_controller_for_params_keeps_default_site_without_refresh(hass: HomeAssistant) -> None:
    """Test legacy resolution passes the default site directly without loading sites."""
    params = replace(_SSL_PARAMS, ssl_verify=False)
    controller = MagicMock()
    controller.sites.update = AsyncMock()

    with patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller) as create_ctrl:
        result = await create_controller_for_params(
            hass,
            params,
            unique_id="192.168.1.1_default",
            resolve_legacy_site=True,
        )

    assert result is controller
    create_ctrl.assert_awaited_once_with(hass, params)
    controller.sites.update.assert_not_awaited()


async def test_async_refresh_client_stores_allows_cached_discovery_data() -> None:
    """Test setup/options refresh can proceed from cache when both sources fail."""
    controller = make_mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Cached Phone"))]
    )
    controller.clients_all.update_mock.side_effect = TimeoutError
    controller.clients.update_mock.side_effect = TimeoutError

    await async_refresh_client_stores(
        controller,
        require_active_refresh=False,
    )

    controller.clients_all.update_mock.assert_awaited_once_with()
    controller.clients.update_mock.assert_awaited_once_with()


async def test_async_refresh_client_stores_raises_without_cached_discovery_data() -> None:
    """Test setup/options refresh fails when both sources fail with no cache."""
    controller = make_mock_controller()
    controller.clients_all.update_mock.side_effect = TimeoutError
    controller.clients.update_mock.side_effect = TimeoutError

    with pytest.raises(RuntimeError):
        await async_refresh_client_stores(
            controller,
            require_active_refresh=False,
        )


async def test_async_refresh_client_stores_requires_active_runtime_refresh() -> None:
    """Test runtime refresh keeps active clients as a required source."""
    controller = make_mock_controller()
    controller.clients.update_mock.side_effect = TimeoutError

    with pytest.raises(TimeoutError):
        await async_refresh_client_stores(
            controller,
            require_active_refresh=True,
        )


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_create_controller_closes_owned_session_on_login_failure(
    hass: HomeAssistant,
    error: type[BaseException],
) -> None:
    """Test SSL-disabled sessions are detached if login does not complete."""
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=error)
    session = _attach_mock_session(controller, owned=False)

    with (
        patch("custom_components.unifi_presence.helpers.async_create_clientsession", return_value=session),
        patch("custom_components.unifi_presence.helpers.Configuration"),
        patch("custom_components.unifi_presence.helpers.Controller", return_value=controller),
        pytest.raises(error),
    ):
        await create_controller(
            hass,
            _NO_SSL_PARAMS,
        )

    _assert_session_cleanup(session)


async def test_create_controller_for_params_keeps_modern_site_name_without_refresh(hass: HomeAssistant) -> None:
    """Test modern site names bypass extra site resolution work."""
    params = replace(_SSL_PARAMS, site="office", ssl_verify=False)
    controller = MagicMock()
    controller.connectivity = SimpleNamespace(config=SimpleNamespace(site=""))
    controller.sites = MagicMock()
    controller.sites.update = AsyncMock()

    with patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller):
        result_controller = await create_controller_for_params(
            hass,
            params,
            unique_id="site-office-id",
            resolve_legacy_site=True,
        )

    assert result_controller is controller
    controller.sites.update.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_create_controller_for_params_closes_controller_on_resolution_failure(
    hass: HomeAssistant,
    error: type[BaseException],
) -> None:
    """Test incomplete site resolution closes the already-created controller."""
    params = replace(_SSL_PARAMS, site="site-office-id", ssl_verify=False)
    controller = MagicMock()
    controller.sites = MagicMock()
    controller.sites.update = AsyncMock(side_effect=error)

    with (
        patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller),
        patch("custom_components.unifi_presence.helpers.async_close_controller") as async_close,
        pytest.raises(error),
    ):
        await create_controller_for_params(
            hass,
            params,
            unique_id="192.168.1.1_office",
            resolve_legacy_site=True,
        )

    async_close.assert_awaited_once_with(controller)


async def test_create_controller_for_params_closes_controller_on_site_assignment_failure(
    hass: HomeAssistant,
) -> None:
    """Test site assignment failures close the already-created controller."""

    class FailingConfig:
        @property
        def site(self) -> str:
            return ""

        @site.setter
        def site(self, _value: str) -> None:
            raise RuntimeError("assignment failed")

    params = replace(_SSL_PARAMS, site="site-office-id", ssl_verify=False)
    controller = MagicMock()
    controller.connectivity = SimpleNamespace(config=FailingConfig())
    controller.sites.update = AsyncMock()
    controller.sites.values.return_value = [SimpleNamespace(site_id="site-office-id", name="office")]

    with (
        patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller),
        patch("custom_components.unifi_presence.helpers.async_close_controller") as async_close,
        pytest.raises(RuntimeError, match="assignment failed"),
    ):
        await create_controller_for_params(
            hass,
            params,
            unique_id="192.168.1.1_office",
            resolve_legacy_site=True,
        )

    async_close.assert_awaited_once_with(controller)
