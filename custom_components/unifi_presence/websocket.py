"""WebSocket lifecycle manager for UniFi Presence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import aiounifi
from aiounifi.models.message import Message, MessageKey
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN

if TYPE_CHECKING:
    from aiounifi.controller import Controller

_LOGGER = logging.getLogger(__name__)

RETRY_TIMER = 15
RETRY_MAX = 300
CHECK_WEBSOCKET_INTERVAL = timedelta(minutes=1)
STALE_WEBSOCKET_INTERVAL = timedelta(minutes=5)


class UnifiPresenceWebsocket:
    """Manage the WebSocket connection to the UniFi controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        get_api: Callable[[], Controller | None],
        on_message: Callable[[Message], None],
    ) -> None:
        """Initialize the WebSocket manager."""
        self.hass = hass
        self._get_api = get_api
        self._on_message = on_message

        self.ws_task: asyncio.Task[None] | None = None
        self._cancel_retry: CALLBACK_TYPE | None = None
        self._cancel_websocket_check: CALLBACK_TYPE | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._unsub_messages: Callable[[], None] | None = None
        self._ws_started_at: datetime | None = None

        self.available = False
        self._stopped = False
        self._retry_delay = RETRY_TIMER

    @callback
    def start(self) -> None:
        """Start WebSocket connection."""
        self._stopped = False
        self._replace_message_subscription()
        self._cancel_websocket_check = async_track_time_interval(
            self.hass,
            self._async_watch_websocket,
            CHECK_WEBSOCKET_INTERVAL,
        )
        self._start_websocket_runner()

    @callback
    def _replace_message_subscription(self) -> None:
        """Subscribe to controller messages, replacing any prior subscription."""
        self._clear_message_subscription()

        api = self._get_api()
        if api is None:
            return

        def _message_handler(message: Message) -> None:
            _LOGGER.debug("WebSocket message received")
            self._on_message(message)

        self._unsub_messages = api.messages.subscribe(_message_handler, MessageKey.CLIENT)

    @callback
    def _clear_message_subscription(self) -> None:
        """Clear any current controller message subscription."""
        if self._unsub_messages is not None:
            self._unsub_messages()
            self._unsub_messages = None

    @callback
    def _cancel_background_task(self, task: asyncio.Task[None] | None) -> None:
        """Cancel a background task if it exists."""
        if task is not None:
            task.cancel()

    async def _async_cancel_and_wait(self, task: asyncio.Task[None] | None) -> None:
        """Cancel a task and wait for it unless it is the current task."""
        if task is None:
            return

        task.cancel()
        if task is not asyncio.current_task():
            with suppress(asyncio.CancelledError):
                await task

    @callback
    def stop(self) -> None:
        """Stop WebSocket connection."""
        self._stopped = True
        self.available = False

        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

        if self._cancel_websocket_check is not None:
            self._cancel_websocket_check()
            self._cancel_websocket_check = None

        self._clear_message_subscription()
        self._ws_started_at = None

        self._cancel_background_task(self._reconnect_task)
        self._reconnect_task = None
        self._cancel_background_task(self.ws_task)
        self.ws_task = None

    async def stop_and_wait(self) -> None:
        """Stop WebSocket and await task completion."""
        # Capture task references before stop() clears them, so we can await
        # their completion after the synchronous stop has fired cancellation.
        tasks = [task for task in (self.ws_task, self._reconnect_task) if task is not None]
        self.stop()

        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            if pending:
                _LOGGER.warning("Unloading %s - WebSocket task did not complete in time", DOMAIN)

    @callback
    def _start_websocket_runner(self) -> None:
        """Create the WebSocket runner task."""
        if self._stopped:
            return
        if self.ws_task is not None and not self.ws_task.done():
            return

        self.ws_task = self.hass.async_create_background_task(
            self._async_run_websocket(),
            name="unifi_presence_websocket",
        )

    async def _async_run_websocket(self) -> None:
        """Run the WebSocket connection."""
        api = self._get_api()
        if api is None:
            _LOGGER.warning("No controller available for WebSocket")
            return

        # Intercept messages.new_data to detect the first inbound WS frame.
        #
        # messages.subscribe() cannot be used here because aiounifi's
        # MessageHandler.handler() drops frames whose MessageKey is not in
        # _subscribed_messages before iterating subscribers.  The first frame
        # after connect may carry any key (device:sync, events, etc.), so a
        # subscriber filtering on CLIENT would miss it.
        #
        # connectivity.ws_message_received is never reset between reconnects,
        # so polling it would require timestamp bookkeeping and adds latency
        # vs. the synchronous inline call here.
        #
        # The patch is restored in the finally block below.
        message_handler = api.messages
        original_new_data = cast(Any, message_handler).new_data

        def _handle_message(message: bytes) -> None:
            self._mark_connected()
            original_new_data(message)

        cast(Any, message_handler).new_data = _handle_message
        websocket_task = asyncio.create_task(api.start_websocket())
        self._ws_started_at = datetime.now(UTC)

        try:
            await websocket_task
        except aiohttp.ClientConnectorError as err:
            _LOGGER.error("WebSocket connector failed: %s", err)
        except aiohttp.WSServerHandshakeError as err:
            _LOGGER.error("WebSocket handshake failed with status %s: %s", err.status, err)
        except aiounifi.WebsocketError:
            _LOGGER.error("WebSocket disconnected")
        except asyncio.CancelledError:
            websocket_task.cancel()
            with suppress(asyncio.CancelledError):
                await websocket_task
            raise
        except Exception:
            _LOGGER.exception("Unexpected WebSocket error")
        finally:
            cast(Any, message_handler).new_data = original_new_data
            is_active_runner = self.ws_task is asyncio.current_task()
            if is_active_runner:
                self.ws_task = None

        if not is_active_runner:
            return
        if self._stopped:
            return

        self._set_available(False, force_signal=True)
        self._schedule_retry()

    @callback
    def _schedule_retry(self) -> None:
        """Schedule a reconnect with exponential backoff."""
        if self._cancel_retry is not None:
            return

        delay = self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, RETRY_MAX)
        _LOGGER.info("Will try to reconnect to UniFi controller in %s seconds", delay)

        @callback
        def _run_retry(_now: object) -> None:
            self._cancel_retry = None
            self._schedule_reauth_and_restart()

        self._cancel_retry = async_call_later(self.hass, delay, _run_retry)

    @callback
    def _clear_retry(self) -> None:
        """Cancel any pending reconnect timer."""
        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

    @callback
    def _mark_connected(self) -> None:
        """Mark the WebSocket as healthy after the first inbound frame."""
        if self._stopped or self.available:
            return

        self._set_available(True)
        self._retry_delay = RETRY_TIMER
        self._clear_retry()

    def _is_stale_startup(self, ws_message_received: object) -> bool:
        """Return True if the WS has been running long enough without any messages."""
        return (
            ws_message_received is None
            and self._ws_started_at is not None
            and datetime.now(UTC) - self._ws_started_at > STALE_WEBSOCKET_INTERVAL
        )

    @callback
    def _set_available(self, available: bool, *, force_signal: bool = False) -> None:
        """Update availability state."""
        if self.available == available and not force_signal:
            return

        self.available = available

    async def _async_restart_runner(self) -> None:
        """Cancel the current runner and start a replacement after it settles."""
        previous_task = self.ws_task
        await self._async_cancel_and_wait(previous_task)
        if self._stopped:
            return

        self._ws_started_at = None
        self._replace_message_subscription()
        self._start_websocket_runner()

    @callback
    def _start_reconnect_task(self, coro: Coroutine[object, object, None]) -> None:
        """Replace any existing reconnect task with a new one."""
        self._cancel_background_task(self._reconnect_task)
        self._reconnect_task = self.hass.async_create_background_task(coro, name="unifi_presence_reconnect")

    @callback
    def restart_with_current_controller(self) -> None:
        """Restart the WebSocket using the already-authenticated controller."""
        if self._stopped:
            return

        self._clear_retry()
        self._set_available(False, force_signal=True)

        async def _do_restart() -> None:
            current_task = asyncio.current_task()
            try:
                await self._async_restart_runner()
            finally:
                if self._reconnect_task is current_task:
                    self._reconnect_task = None

        self._start_reconnect_task(_do_restart())

    @callback
    def _schedule_reauth_and_restart(self) -> None:
        """Reauthenticate the controller and restart the WebSocket."""
        if self._stopped:
            return

        self._clear_retry()
        self._set_available(False, force_signal=True)

        async def _do_reconnect() -> None:
            current_task = asyncio.current_task()
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
                await self._async_restart_runner()
            finally:
                if self._reconnect_task is current_task:
                    self._reconnect_task = None

        self._start_reconnect_task(_do_reconnect())

    @callback
    def _async_watch_websocket(self, _now: object) -> None:
        """Log WebSocket health and reconnect stale sessions."""
        api = self._get_api()
        ws_message_received = api.connectivity.ws_message_received if api is not None else None
        _LOGGER.debug(
            "WebSocket health check - available: %s, last message: %s",
            self.available,
            ws_message_received,
        )

        if self._stopped:
            return

        if self.ws_task is None or self.ws_task.done():
            _LOGGER.warning("WebSocket task ended unexpectedly, reconnecting")
            self._schedule_reauth_and_restart()
            return

        if not self.available:
            if self._is_stale_startup(ws_message_received):
                _LOGGER.warning("WebSocket never received a message since startup, reconnecting")
                self._schedule_reauth_and_restart()

            return

        if (
            isinstance(ws_message_received, datetime)
            and datetime.now(UTC) - ws_message_received > STALE_WEBSOCKET_INTERVAL
        ):
            _LOGGER.warning("WebSocket stale, reconnecting")
            self._schedule_reauth_and_restart()
            return

        # Defensive: available should imply ws_message_received is set,
        # but guard against edge cases where the controller was replaced.
        if self._is_stale_startup(ws_message_received):
            _LOGGER.warning("WebSocket never received a message since startup, reconnecting")
            self._schedule_reauth_and_restart()
