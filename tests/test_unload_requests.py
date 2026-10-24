"""Real request deadlines must bound the config entry's shutdown drain."""

import asyncio
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from aiohttp import TCPConnector, web
from aiounifi.models.configuration import Configuration
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client as ha_http
from pytest_aiohttp.plugin import AiohttpServer

from custom_components.unifi_presence.helpers import async_close_controller

from .conftest import MOCK_CONFIG_DATA, add_mock_config_entry
from .test_coordinator_shutdown import assert_stopped


@pytest.mark.parametrize("request_kind", ["scheduled", "manual", "options"])
@pytest.mark.parametrize("blocked_store", ["sta", "user"])
@pytest.mark.parametrize("stall_body", [False, True])
async def test_unload_drains_stalled_http_request(  # noqa: PLR0915 - Keep real network and unload ownership together.
    hass: HomeAssistant,
    enable_custom_integrations: None,
    aiohttp_server: AiohttpServer,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
    blocked_store: str,
    stall_body: bool,
) -> None:
    """Unload alone waits for a short request deadline, not HA's later cancellation."""
    started: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()
    other_completed: asyncio.Queue[None] = asyncio.Queue()
    draining = asyncio.Event()
    timed_out: asyncio.Queue[object] = asyncio.Queue()
    stalled = False

    async def handle(request: web.Request) -> web.StreamResponse:
        if stalled and request.path.endswith(f"/{blocked_store}"):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            if stall_body:
                await response.prepare(request)
                await response.write(b'{"meta":')
            else:
                started.put_nowait(None)
            await release.wait()
            return response
        return web.json_response({"meta": {"rc": "ok"}, "data": []})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    server = await aiohttp_server(app)
    connector = TCPConnector()
    monkeypatch.setattr(ha_http, "_async_get_connector", lambda *args, **kwargs: connector)
    shared = ha_http.async_get_clientsession(hass)
    entry = add_mock_config_entry(hass, data={**MOCK_CONFIG_DATA, CONF_HOST: server.host, CONF_PORT: server.port})

    # Exercise real aiounifi stores and HA-created sessions over local HTTP;
    # authentication and the unrelated WebSocket producer need no wire protocol.
    with (
        patch.object(Configuration, "url", new_callable=PropertyMock, return_value=str(server.make_url(""))),
        patch("aiounifi.controller.Controller.login", new_callable=AsyncMock),
        patch("aiounifi.controller.Controller.start_websocket", new_callable=AsyncMock),
        patch("custom_components.unifi_presence.helpers.CONTROLLER_REQUEST_TIMEOUT", 0.05),
        # Keep this test focused on individual HTTP deadlines, not store deadlines.
        patch("custom_components.unifi_presence.helpers.CONTROLLER_STORE_REFRESH_TIMEOUT", 10),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        controller = coordinator.controller
        session = controller.connectivity.config.session
        previous = coordinator.data
        detach = session.detach

        response_read = ha_http.HassClientResponse.read

        async def observe_read(response):
            if stall_body and response.url.path.endswith(f"/{blocked_store}"):
                started.put_nowait(None)
            return await response_read(response)

        connectivity_type = type(controller.connectivity)
        request = connectivity_type._request

        async def observe_request(connectivity, method, url, *args, **kwargs):
            assert connectivity.config.session.timeout.total == 0.05
            try:
                return await request(connectivity, method, url, *args, **kwargs)
            except TimeoutError:
                timed_out.put_nowait(connectivity)
                raise
            finally:
                if not url.endswith(f"/{blocked_store}"):
                    other_completed.put_nowait(None)

        lock = coordinator._client_store_lock
        acquire = lock.acquire

        async def observe_acquire() -> bool:
            if coordinator._stopping and lock.locked():
                draining.set()
            return await acquire()

        monkeypatch.setattr(ha_http.HassClientResponse, "read", observe_read)
        monkeypatch.setattr(connectivity_type, "_request", observe_request)
        monkeypatch.setattr(lock, "acquire", observe_acquire)

        def detach_after_requests() -> None:
            assert not connector._acquired
            detach()

        monkeypatch.setattr(session, "detach", detach_after_requests)
        request_task = unloading = None
        try:
            await hass.async_block_till_done()
            stalled = True
            async with asyncio.timeout(3):
                if request_kind == "scheduled":
                    coordinator._async_unsub_refresh()
                    request_task = entry.async_create_background_task(
                        hass, coordinator._handle_refresh_interval(), "stalled scheduled refresh", eager_start=True
                    )
                else:
                    request_task = hass.async_create_task(
                        hass.config_entries.options.async_init(entry.entry_id)
                        if request_kind == "options"
                        else coordinator.async_refresh()
                    )
                await started.get()
                await other_completed.get()
                assert coordinator._client_store_lock.locked()
                unloading = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
                await draining.wait()
                assert not unloading.done()
                assert not request_task.done()
                assert not session.closed
                assert connector._acquired
                assert timed_out.empty()

                # Advance only after the intended HTTP phase and unload's lock
                # wait are established. The real aiohttp deadline still expires
                # through the event loop; neither the request nor its timer is
                # cancelled/invoked by the test. Keep the configured 50 ms bound.
                assert session.timeout.total == 0.05
                loop = asyncio.get_running_loop()
                real_time = loop.time
                elapsed = session.timeout.total
                with monkeypatch.context() as clock:
                    clock.setattr(loop, "time", lambda: real_time() + elapsed)
                    assert await unloading
                    assert timed_out.get_nowait() is controller.connectivity
                    if request_kind == "options":
                        # Discovery retries with its own fallback controller after
                        # the runtime stops. Exercise its real request deadline too.
                        await started.get()
                        await other_completed.get()
                        assert not request_task.done()
                        assert timed_out.empty()
                        elapsed += session.timeout.total
                    await request_task

            if request_kind == "options":
                fallback = timed_out.get_nowait()
                assert fallback is not controller.connectivity
                assert fallback.config.session.closed
            assert timed_out.empty()
            assert entry.state is ConfigEntryState.NOT_LOADED
            assert not release.is_set()
            assert not hasattr(entry, "runtime_data")
            assert_stopped(coordinator)
            assert coordinator.data is previous
            assert session.closed
            assert not connector._acquired
            assert not connector.closed
            assert not shared.closed
            stalled = False
            async with shared.get(server.make_url("/")) as response:
                assert response.status == 200
                await response.read()
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (request_task, unloading) if task is not None), return_exceptions=True
            )
            await hass.async_block_till_done()
            await async_close_controller(controller)
            shared.detach()
            await connector.close()


@pytest.mark.parametrize("request_kind", ["scheduled", "manual", "discovery"])
@pytest.mark.parametrize("blocked_store", ["sta", "user"])
async def test_unload_bounds_repeated_login_retries(  # noqa: PLR0915 - Keep real retries and unload ownership together.
    hass: HomeAssistant,
    enable_custom_integrations: None,
    aiohttp_server: AiohttpServer,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
    blocked_store: str,
) -> None:
    """Successful logins cannot extend a store update past its overall deadline."""
    retrying = asyncio.Event()
    draining = asyncio.Event()
    rejecting = False
    login_count = client_count = active_requests = 0

    async def handle(request: web.Request) -> web.Response:
        nonlocal login_count, client_count
        if request.path.endswith("/login"):
            login_count += 1
        if rejecting and request.path.endswith(f"/{blocked_store}"):
            client_count += 1
            if client_count >= 3:
                retrying.set()
            return web.Response(status=401)
        return web.json_response({"meta": {"rc": "ok"}, "data": []})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    server = await aiohttp_server(app)
    connector = TCPConnector()
    coordinator = shared = operation = unloading = None
    try:
        monkeypatch.setattr(ha_http, "_async_get_connector", lambda *args, **kwargs: connector)
        shared = ha_http.async_get_clientsession(hass)
        entry = add_mock_config_entry(hass, data={**MOCK_CONFIG_DATA, CONF_HOST: server.host, CONF_PORT: server.port})

        # Keep authentication real: aiounifi's successful login re-enables its retry.
        with (
            patch.object(Configuration, "url", new_callable=PropertyMock, return_value=str(server.make_url(""))),
            patch("aiounifi.controller.Controller.start_websocket", new_callable=AsyncMock),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            coordinator = entry.runtime_data
            controller = coordinator.controller
            session = controller.connectivity.config.session
            assert session.timeout.total == 10
            assert login_count == 1
            previous = coordinator.data
            listener = Mock()
            coordinator.async_add_listener(listener)
            detach = session.detach
            request = type(controller.connectivity)._request

            async def observe_request(connectivity, *args, **kwargs):
                nonlocal active_requests
                active_requests += 1
                try:
                    return await request(connectivity, *args, **kwargs)
                except TimeoutError:
                    pytest.fail("An individual HTTP timeout must not end this retry loop")
                finally:
                    active_requests -= 1

            lock = coordinator._client_store_lock
            acquire = lock.acquire

            async def observe_acquire() -> bool:
                if coordinator._stopping and lock.locked():
                    draining.set()
                return await acquire()

            def detach_after_requests() -> None:
                assert active_requests == 0
                assert not connector._acquired
                detach()

            monkeypatch.setattr(type(controller.connectivity), "_request", observe_request)
            monkeypatch.setattr(lock, "acquire", observe_acquire)
            monkeypatch.setattr(session, "detach", detach_after_requests)
            await hass.async_block_till_done()
            rejecting = True
            deadline = 1
            with patch("custom_components.unifi_presence.helpers.CONTROLLER_STORE_REFRESH_TIMEOUT", deadline):
                async with asyncio.timeout(3):
                    if request_kind == "scheduled":
                        coordinator._async_unsub_refresh()
                        operation = entry.async_create_background_task(
                            hass, coordinator._handle_refresh_interval(), "retrying scheduled refresh", eager_start=True
                        )
                    else:
                        operation = hass.async_create_task(
                            coordinator.async_fetch_available_clients()
                            if request_kind == "discovery"
                            else coordinator.async_refresh()
                        )
                    await retrying.wait()
                    assert login_count >= 3
                    assert not operation.done()
                    unloading = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
                    await draining.wait()
                    assert not unloading.done()
                    assert not session.closed

                    # Expire the operation's real timer only after retries and the
                    # unload wait are established; the HTTP deadline is still far away.
                    loop = asyncio.get_running_loop()
                    real_time = loop.time
                    with monkeypatch.context() as clock:
                        clock.setattr(loop, "time", lambda: real_time() + deadline)
                        assert await unloading
                        assert await operation is None

            counts = (login_count, client_count)
            await hass.async_block_till_done()
            assert (login_count, client_count) == counts
            assert entry.state is ConfigEntryState.NOT_LOADED
            assert not hasattr(entry, "runtime_data")
            assert_stopped(coordinator)
            assert coordinator.data is previous
            listener.assert_not_called()
            if request_kind != "discovery":
                assert isinstance(coordinator.last_exception, asyncio.CancelledError)
            assert session.closed
            assert active_requests == 0
            assert not connector._acquired
            assert not connector.closed
            assert not shared.closed
            rejecting = False
            async with shared.get(server.make_url("/")) as response:
                assert response.status == 200
                await response.read()
    finally:
        if operation is not None and not operation.done():
            operation.cancel()
        await asyncio.gather(*(task for task in (operation, unloading) if task is not None), return_exceptions=True)
        if coordinator is not None:
            await coordinator.async_shutdown()
        if shared is not None:
            shared.detach()
        await connector.close()
