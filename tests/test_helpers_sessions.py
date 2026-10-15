"""Regression tests for controller authentication session isolation."""

import ssl
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import TCPConnector, web
from aiounifi.controller import Controller
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client as ha_http
from homeassistant.util import dt as dt_util
from pytest_aiohttp.plugin import AiohttpServer
from yarl import URL

from custom_components.unifi_presence.helpers import (
    ControllerConnectionParams,
    async_close_controller,
    create_controller,
)


@dataclass
class _UnifiServer:
    ports: tuple[int, int]
    ssl_context: ssl.SSLContext
    observed: list[tuple[str, str, str]]


@pytest.fixture
async def unifi_server(
    hass: HomeAssistant,
    tmp_path: Path,
    aiohttp_server: AiohttpServer,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
) -> AsyncGenerator[_UnifiServer]:
    """Run a local trusted TLS controller with account-specific dummy tokens."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt_util.utcnow()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "certificate.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=cert_path)
    observed: list[tuple[str, str, str]] = []

    def record(kind: str, request: web.Request) -> None:
        observed.append((kind, request.headers.get("Cookie", ""), request.headers.get("x-csrf-token", "")))

    async def root(request: web.Request) -> web.Response:
        record("root", request)
        return web.Response(text="UniFi OS")

    async def login(request: web.Request) -> web.Response:
        record("login", request)
        username = (await request.json())["username"]
        response = web.json_response(
            {"meta": {"rc": "ok"}, "data": []}, headers={"x-csrf-token": f"DUMMY-CSRF-{username}"}
        )
        response.set_cookie("TOKEN", f"DUMMY-SESSION-{username}", path="/", secure=True, httponly=True)
        return response

    async def clients(request: web.Request) -> web.Response:
        record("api", request)
        return web.json_response({"meta": {"rc": "ok"}, "data": []})

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        record("ws", request)
        response = web.WebSocketResponse()
        await response.prepare(request)
        await response.close()
        return response

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/proxy/network/api/s/{site}/stat/sta", clients)
    app.router.add_get("/proxy/network/wss/s/{site}/events", websocket)
    first = await aiohttp_server(app, ssl=server_context)
    second = await aiohttp_server(app, ssl=server_context)
    assert first.port is not None
    assert second.port is not None

    # Retain real HA session creation, but avoid its mDNS resolver in this local test.
    connector = TCPConnector(ssl=client_context)
    monkeypatch.setattr(ha_http, "_async_get_connector", lambda *args, **kwargs: connector)
    monkeypatch.setattr("custom_components.unifi_presence.helpers.ha_client_context", lambda: client_context)
    try:
        yield _UnifiServer((first.port, second.port), client_context, observed)
    finally:
        for session in hass.data.get(ha_http.DATA_CLIENTSESSION, {}).values():
            session.detach()
        await connector.close()


async def _assert_authentication(controller: Controller, server: _UnifiServer, username: str) -> None:
    """Verify the actual REST and WebSocket wire headers belong to one account."""
    await controller.clients.update()
    assert server.observed[-1][0] == "api"
    assert f"TOKEN=DUMMY-SESSION-{username}" in server.observed[-1][1]
    assert server.observed[-1][2] == f"DUMMY-CSRF-{username}"
    await controller.start_websocket()
    assert server.observed[-1][0] == "ws"
    assert f"TOKEN=DUMMY-SESSION-{username}" in server.observed[-1][1]
    assert server.observed[-1][2] == f"DUMMY-CSRF-{username}"


@pytest.mark.parametrize("ssl_verify", [True, False])
@pytest.mark.parametrize("other_port", [True, False])
async def test_controller_sessions_isolate_authentication(
    hass: HomeAssistant, unifi_server: _UnifiServer, ssl_verify: bool, other_port: bool
) -> None:
    """Keep both accounts and HA's shared cookies isolated, including across ports."""
    shared = ha_http.async_get_clientsession(hass)
    connector = shared.connector
    assert connector is not None
    shared.cookie_jar.update_cookies({"TOKEN": "DUMMY-SHARED"}, URL("https://localhost/"))
    params = ControllerConnectionParams(
        "localhost", unifi_server.ports[0], "A", "DUMMY-PASSWORD", "default", ssl_verify
    )
    controllers: list[Controller] = []
    try:
        a = await create_controller(hass, params)
        controllers.append(a)
        b = await create_controller(hass, replace(params, username="B", port=unifi_server.ports[int(other_port)]))
        controllers.append(b)
        a_session = a.connectivity.config.session
        b_session = b.connectivity.config.session
        if ssl_verify:
            assert a.connectivity.config.ssl_context is unifi_server.ssl_context
            assert unifi_server.ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert unifi_server.ssl_context.check_hostname
        else:
            assert a.connectivity.config.ssl_context is False

        await _assert_authentication(a, unifi_server, "A")
        await _assert_authentication(b, unifi_server, "B")
        await a.login()
        await _assert_authentication(b, unifi_server, "B")
        assert all("DUMMY-SHARED" not in cookie for _, cookie, _ in unifi_server.observed)
        assert a_session is not b_session
        assert a_session is not shared
        assert a_session.cookie_jar is not b_session.cookie_jar
        assert a_session.connector is b_session.connector is connector
    finally:
        for controller in controllers:
            await async_close_controller(controller)
            await async_close_controller(controller)

    assert all(controller.connectivity.config.session.closed for controller in controllers)
    assert not shared.closed
    assert not connector.closed
    async with shared.get(f"https://localhost:{unifi_server.ports[0]}/", ssl=unifi_server.ssl_context) as response:
        await response.read()
    assert unifi_server.observed[-1] == ("root", "TOKEN=DUMMY-SHARED", "")
