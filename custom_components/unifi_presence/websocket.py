"""WebSocket lifecycle manager for UniFi Presence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

import aiohttp
import aiounifi
from aiounifi.models.message import Message, MessageKey
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN

if TYPE_CHECKING:
    from aiounifi.controller import Controller

_LOGGER = logging.getLogger(__name__)

RETRY_TIMER = 15
RETRY_MAX = 300
CHECK_WEBSOCKET_INTERVAL = timedelta(minutes=1)


class UnifiPresenceWebsocket:
    """Manage the WebSocket connection to the UniFi controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        get_api: Callable[[], Controller | None],
        signal_reachable: str,
        on_message: Callable[[Message], None],
    ) -> None:
        """Initialize the WebSocket manager."""
        self.hass = hass
        self._get_api = get_api
        self.signal = signal_reachable
        self._on_message = on_message

        self.ws_task: asyncio.Task[None] | None = None
        self._cancel_retry: CALLBACK_TYPE | None = None
        self._cancel_websocket_check: CALLBACK_TYPE | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._unsub_messages: Callable[[], None] | None = None

        self.available = True
        self._stopped = False
        self._retry_delay = RETRY_TIMER

    @callback
    def start(self) -> None:
        """Start WebSocket connection."""
        self._stopped = False
        self._subscribe_messages()
        self._cancel_websocket_check = async_track_time_interval(
            self.hass, self._async_watch_websocket, CHECK_WEBSOCKET_INTERVAL
        )
        self._start_websocket()

    @callback
    def _subscribe_messages(self) -> None:
        """Subscribe to controller messages, replacing any prior subscription."""
        if self._unsub_messages:
            self._unsub_messages()
            self._unsub_messages = None

        api = self._get_api()
        if api is None:
            return

        def _message_handler(message: Message) -> None:
            _LOGGER.debug("WebSocket message received")
            self._on_message(message)

        self._unsub_messages = api.messages.subscribe(_message_handler, MessageKey.CLIENT)

    @callback
    def stop(self) -> None:
        """Stop WebSocket connection."""
        self._stopped = True
        self.available = False

        if self._cancel_retry:
            self._cancel_retry()
            self._cancel_retry = None

        if self._cancel_websocket_check:
            self._cancel_websocket_check()
            self._cancel_websocket_check = None

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
        # Capture task references before stop() clears them
        tasks = [t for t in (self.ws_task, self._reconnect_task) if t is not None]
        self.stop()

        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
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
            api = self._get_api()
            if api is None:
                _LOGGER.warning("No controller available for WebSocket")
                return

            try:
                await api.start_websocket()
            except aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError:
                _LOGGER.error("WebSocket setup failed")
            except aiounifi.WebsocketError:
                _LOGGER.error("WebSocket disconnected")
            except Exception:
                _LOGGER.exception("Unexpected WebSocket error")

            if self._stopped:
                return

            self.available = False
            async_dispatcher_send(self.hass, self.signal)
            self._schedule_retry()

        if not self.available:
            self.available = True
            self._retry_delay = RETRY_TIMER
            async_dispatcher_send(self.hass, self.signal)

        self.ws_task = self.hass.async_create_background_task(_websocket_runner(), name="unifi_presence_websocket")

    @callback
    def _schedule_retry(self) -> None:
        """Schedule a reconnect with exponential backoff."""
        delay = self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, RETRY_MAX)
        _LOGGER.info("Will try to reconnect to UniFi controller in %s seconds", delay)
        self._cancel_retry = async_call_later(self.hass, delay, lambda _now: self._reconnect())

    @callback
    def _reconnect(self) -> None:
        """Reconnect to the UniFi controller."""
        if self._stopped:
            return

        async def _do_reconnect() -> None:
            """Attempt re-authentication and restart WebSocket."""
            api = self._get_api()
            if api is None:
                _LOGGER.debug("No controller available, scheduling retry")
                self._schedule_retry()
                return

            try:
                async with asyncio.timeout(5):
                    await api.login()
            except (
                TimeoutError,
                aiounifi.BadGateway,
                aiounifi.ServiceUnavailable,
                aiounifi.AiounifiException,
                aiohttp.ClientError,
            ) as exc:
                _LOGGER.debug("Schedule reconnect to UniFi controller: %s", exc)
                self._schedule_retry()
            else:
                # Re-subscribe in case the controller was replaced during re-auth
                self._subscribe_messages()
                self._start_websocket()

        self._reconnect_task = self.hass.async_create_background_task(_do_reconnect(), name="unifi_presence_reconnect")

    @callback
    def _async_watch_websocket(self, _now: object) -> None:
        """Log WebSocket health check."""
        api = self._get_api()
        ws_message_received = api.connectivity.ws_message_received if api is not None else "N/A"
        _LOGGER.debug(
            "WebSocket health check — available: %s, last message: %s",
            self.available,
            ws_message_received,
        )
