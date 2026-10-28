"""Bounded snapshot authentication against aiounifi's real request/login stack."""

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiounifi
import pytest
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration

from custom_components.unifi_presence.helpers import SiteSnapshotError, async_fetch_site_snapshot


def response(status=HTTPStatus.OK, raw=b"{}"):
    """Build an aiohttp response context without replacing aiounifi transport logic."""
    result = MagicMock(status=status, content_type="application/json", headers={})
    result.read = AsyncMock(return_value=raw)
    result.__aenter__.return_value = result
    return result


@pytest.fixture
def controller():
    """Use the installed Controller, Connectivity, request decoder, and login."""
    result = Controller(Configuration(Mock(), host="controller", username="user", password="password"))
    result.connectivity.can_retry_login = True
    return result


@pytest.mark.parametrize("persistent", [False, True])
async def test_real_401_has_at_most_one_login(controller, persistent):
    """Immediate persistent 401s terminate without advancing time or recursive retry."""
    session = controller.connectivity.config.session
    session.request.side_effect = [
        response(HTTPStatus.UNAUTHORIZED),
        response(HTTPStatus.FOUND),  # Public Controller.login checks UniFi OS first.
        response(),  # Real Connectivity.login resets can_retry_login to True.
        response(HTTPStatus.UNAUTHORIZED) if persistent else response(raw=b'{"data":[]}'),
    ]
    with patch.object(controller, "request", wraps=controller.request) as request:
        if persistent:
            with pytest.raises(aiounifi.LoginRequired):
                await async_fetch_site_snapshot(controller)
        else:
            assert await async_fetch_site_snapshot(controller) == ()
    assert request.await_count == 2
    assert [(call.args[0], call.args[1]) for call in session.request.call_args_list] == [
        ("get", "https://controller:8443/api/self/sites"),
        ("get", "https://controller:8443"),
        ("post", "https://controller:8443/api/login"),
        ("get", "https://controller:8443/api/self/sites"),
    ]
    assert controller.connectivity.can_retry_login is True


async def test_real_401_respects_disabled_login_retry(controller):
    """A caller that disabled login retry gets the first authentication error."""
    controller.connectivity.can_retry_login = False
    session = controller.connectivity.config.session
    session.request.return_value = response(HTTPStatus.UNAUTHORIZED)
    with pytest.raises(aiounifi.LoginRequired):
        await async_fetch_site_snapshot(controller)
    assert session.request.call_count == 1
    assert controller.connectivity.can_retry_login is False


@pytest.mark.parametrize("can_retry_login", [False, True])
@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b'{"data":[]}', None),
        (b'{"meta":{"rc":"error","msg":"api.err.Invalid"}}', aiounifi.Unauthorized),
        (b'{"data":null}', SiteSnapshotError),
    ],
)
async def test_restore_flag_after_response(controller, can_retry_login, raw, error):
    """Successful and rejected responses restore either original flag value."""
    controller.connectivity.can_retry_login = can_retry_login
    session = controller.connectivity.config.session
    session.request.return_value = response(raw=raw)
    if error is None:
        assert await async_fetch_site_snapshot(controller) == ()
    else:
        with pytest.raises(error):
            await async_fetch_site_snapshot(controller)
    assert session.request.call_count == 1
    assert controller.connectivity.can_retry_login is can_retry_login


@pytest.mark.parametrize("status", [HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN])
async def test_restore_flag_after_real_login_failure(controller, status):
    """Login failures propagate without another request or login attempt."""
    session = controller.connectivity.config.session
    session.request.side_effect = [
        response(HTTPStatus.UNAUTHORIZED),
        response(HTTPStatus.FOUND),
        response(status),
    ]
    with pytest.raises(aiounifi.LoginRequired if status == HTTPStatus.UNAUTHORIZED else aiounifi.Forbidden):
        await async_fetch_site_snapshot(controller)
    assert session.request.call_count == 3
    assert controller.connectivity.can_retry_login is True


@pytest.mark.parametrize(
    ("can_retry_login", "phase"),
    [(False, "request"), (True, "request"), (True, "login"), (True, "retry")],
)
@pytest.mark.parametrize("cancel", [False, True])
async def test_restore_flag_after_interruption(controller, can_retry_login, phase, cancel):
    """One total timeout and cancellation restore flags at every suspension point."""
    controller.connectivity.can_retry_login = can_retry_login
    started = asyncio.Event()

    async def blocked():
        assert controller.connectivity.can_retry_login is False
        started.set()
        await asyncio.Event().wait()

    async def request(_request):
        assert controller.connectivity.can_retry_login is False
        if phase != "request" and request_mock.await_count == 1:
            raise aiounifi.LoginRequired
        return await blocked()

    async def login():
        if phase == "login":
            await blocked()
        controller.connectivity.can_retry_login = True

    # Expire the actual, single timeout after reaching the chosen await point.
    # No wall-clock sleeps or time advancement are needed.
    timeout_context = asyncio.timeout(None)
    with (
        patch("custom_components.unifi_presence.helpers.asyncio.timeout", return_value=timeout_context) as timeout,
        patch.object(controller, "request", side_effect=request) as request_mock,
        patch.object(controller, "login", side_effect=login),
    ):
        task = asyncio.create_task(async_fetch_site_snapshot(controller))
        await started.wait()
        if cancel:
            task.cancel()
        else:
            timeout_context.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
    timeout.assert_called_once_with(10)
    assert controller.connectivity.can_retry_login is can_retry_login
