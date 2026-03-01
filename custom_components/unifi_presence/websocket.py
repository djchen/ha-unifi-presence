"""WebSocket lifecycle manager for UniFi Presence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiohttp
import aiounifi
from aiounifi.models.message import Message, MessageKey
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RETRY_TIMER = 15


class UnifiPresenceWebsocket:
    """Manage the WebSocket connection to the UniFi controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: aiounifi.Controller,
        signal_reachable: str,
        on_message: Callable[[Message], None],
    ) -> None:
        """Initialize the WebSocket manager."""
        self.hass = hass
        self.api = api
        self.signal = signal_reachable
        self._on_message = on_message

        self.ws_task: asyncio.Task | None = None
        self._cancel_retry: CALLBACK_TYPE | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._unsub_messages: Callable[[], None] | None = None

        self.available = True
        self._stopped = False

    @callback
    def start(self) -> None:
        """Start WebSocket connection."""
        self._stopped = False

        def _message_handler(message: Message) -> None:
            _LOGGER.debug("WebSocket message received")
            self._on_message(message)

        self._unsub_messages = self.api.messages.subscribe(_message_handler, MessageKey.CLIENT)
        self._start_websocket()

    @callback
    def stop(self) -> None:
        """Stop WebSocket connection."""
        self._stopped = True
        self.available = False

        if self._cancel_retry:
            self._cancel_retry()
            self._cancel_retry = None

        if self._unsub_messages:
            self._unsub_messages()
            self._unsub_messages = None

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self.ws_task is not None:
            self.ws_task.cancel()

    async def stop_and_wait(self) -> None:
        """Stop WebSocket and await task completion."""
        self.stop()

        if self.ws_task is not None:
            _, pending = await asyncio.wait([self.ws_task], timeout=10)
            if pending:
                _LOGGER.warning(
                    "Unloading %s — WebSocket task did not complete in time",
                    DOMAIN,
                )

    @callback
    def _start_websocket(self) -> None:
        """Create the WebSocket runner task."""

        async def _websocket_runner() -> None:
            """Run the WebSocket connection."""
            try:
                await self.api.start_websocket()
            except aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError:
                _LOGGER.error("WebSocket setup failed")
            except aiounifi.WebsocketError:
                _LOGGER.error("WebSocket disconnected")

            if self._stopped:
                return

            self.available = False
            async_dispatcher_send(self.hass, self.signal)
            self._cancel_retry = async_call_later(self.hass, RETRY_TIMER, lambda _now: self._reconnect(log=True))

        if not self.available:
            self.available = True
            async_dispatcher_send(self.hass, self.signal)

        self.ws_task = self.hass.loop.create_task(_websocket_runner())

    @callback
    def _reconnect(self, log: bool = False) -> None:
        """Reconnect to the UniFi controller."""
        if self._stopped:
            return

        async def _do_reconnect() -> None:
            """Attempt re-authentication and restart WebSocket."""
            try:
                async with asyncio.timeout(5):
                    await self.api.login()
            except (
                TimeoutError,
                aiounifi.BadGateway,
                aiounifi.ServiceUnavailable,
                aiounifi.AiounifiException,
            ) as exc:
                _LOGGER.debug("Schedule reconnect to UniFi controller: %s", exc)
                self._cancel_retry = async_call_later(self.hass, RETRY_TIMER, lambda _now: self._reconnect())
            else:
                self._start_websocket()

        if log:
            _LOGGER.info("Will try to reconnect to UniFi controller")

        self._reconnect_task = self.hass.loop.create_task(_do_reconnect())
