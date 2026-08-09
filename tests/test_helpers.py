"""Tests for UniFi Presence helper utilities."""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.unifi_presence.helpers import (
    ClientStoreRefreshFailed,
    ClientStoreRefreshPolicy,
    ControllerConnectionParams,
    async_close_controller,
    async_refresh_client_stores,
    build_client_labels_from_stores,
    create_controller,
    create_controller_for_params,
    normalize_mac,
    normalize_macs,
    should_resolve_controller_site,
    site_title,
    tracker_unique_id,
)

from .conftest import _make_mock_client, _mock_controller

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


async def test_create_controller_for_params_uses_legacy_site_resolution(hass: HomeAssistant) -> None:
    """Test the shared controller helper resolves a legacy site on one controller."""
    params = ControllerConnectionParams(
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="site-office-id",
        ssl_verify=False,
    )
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
    params = ControllerConnectionParams(
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="office",
        ssl_verify=False,
    )
    controller = MagicMock()

    with patch("custom_components.unifi_presence.helpers.create_controller", return_value=controller) as create_ctrl:
        result = await create_controller_for_params(hass, params)

    assert result is controller
    create_ctrl.assert_awaited_once_with(hass, params)


async def test_async_refresh_client_stores_allows_cached_discovery_data() -> None:
    """Test setup/options refresh can proceed from cache when both sources fail."""
    controller = _mock_controller(
        clients_all_items=[("aa:bb:cc:dd:ee:ff", _make_mock_client("aa:bb:cc:dd:ee:ff", name="Cached Phone"))]
    )
    controller.clients_all.update = AsyncMock(side_effect=TimeoutError)
    controller.clients.update = AsyncMock(side_effect=TimeoutError)

    await async_refresh_client_stores(
        controller,
        policy=ClientStoreRefreshPolicy.DISCOVERY,
    )

    controller.clients_all.update.assert_awaited_once()
    controller.clients.update.assert_awaited_once()


async def test_async_refresh_client_stores_raises_without_cached_discovery_data() -> None:
    """Test setup/options refresh fails when both sources fail with no cache."""
    controller = _mock_controller(clients_all_items=[], clients_items=[])
    controller.clients_all.update = AsyncMock(side_effect=TimeoutError)
    controller.clients.update = AsyncMock(side_effect=TimeoutError)

    with pytest.raises(ClientStoreRefreshFailed):
        await async_refresh_client_stores(
            controller,
            policy=ClientStoreRefreshPolicy.DISCOVERY,
        )


async def test_async_refresh_client_stores_requires_active_runtime_refresh() -> None:
    """Test runtime refresh keeps active clients as a required source."""
    controller = _mock_controller(clients_all_items=[])
    controller.clients.update = AsyncMock(side_effect=TimeoutError)

    with pytest.raises(TimeoutError):
        await async_refresh_client_stores(
            controller,
            policy=ClientStoreRefreshPolicy.RUNTIME,
        )


def test_build_client_labels_from_stores_active_wins_on_collision() -> None:
    """Test active client labels override historical labels for the same MAC."""
    labels = build_client_labels_from_stores(
        {"aa:bb:cc:dd:ee:ff": _make_mock_client("aa:bb:cc:dd:ee:ff", name="Old Name")}.items(),
        {" AA:BB:CC:DD:EE:FF ": _make_mock_client("aa:bb:cc:dd:ee:ff", name="Current Name")}.items(),
    )

    assert labels == {"aa:bb:cc:dd:ee:ff": "Current Name (aa:bb:cc:dd:ee:ff)"}


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_create_controller_closes_owned_session_on_login_failure(
    hass: HomeAssistant,
    error: type[BaseException],
) -> None:
    """Test SSL-disabled sessions are detached if login does not complete."""
    session = MagicMock()
    session.closed = False
    session.detach = MagicMock()
    controller = MagicMock()
    controller.login = AsyncMock(side_effect=error)

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

    session.detach.assert_called_once_with()


def test_should_resolve_controller_site_false_for_default_site() -> None:
    """Test default-site configs skip legacy normalization."""
    assert (
        should_resolve_controller_site(
            ControllerConnectionParams(
                host="192.168.1.1",
                port=443,
                username="admin",
                password="password",
                site="default",
                ssl_verify=False,
            ),
            unique_id=None,
        )
        is False
    )


async def test_create_controller_for_params_keeps_modern_site_name_without_refresh(hass: HomeAssistant) -> None:
    """Test modern site names bypass extra site resolution work."""
    params = ControllerConnectionParams(
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="office",
        ssl_verify=False,
    )
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
    params = ControllerConnectionParams(
        host="192.168.1.1",
        port=443,
        username="admin",
        password="password",
        site="site-office-id",
        ssl_verify=False,
    )
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
