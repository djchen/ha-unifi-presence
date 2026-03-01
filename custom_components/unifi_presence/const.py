"""Constants for the UniFi Presence integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "unifi_presence"

CONF_SITE: Final = "site"
CONF_SSL_VERIFY: Final = "ssl_verify"
CONF_AWAY_SECONDS: Final = "away_seconds"
CONF_FALLBACK_POLL_INTERVAL: Final = "fallback_poll_interval"
CONF_TRACKED_DEVICES: Final = "tracked_devices"

DEFAULT_SITE: Final = "default"
DEFAULT_PORT: Final = 443
DEFAULT_SSL_VERIFY: Final = False
DEFAULT_AWAY_SECONDS: Final = 60
DEFAULT_FALLBACK_POLL_INTERVAL: Final = 300
