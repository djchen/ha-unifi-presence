# AGENTS.md

## Project Overview

**UniFi Presence** is a Home Assistant custom integration for presence detection using a UniFi network controller.

For user-facing details (features, requirements, installation, configuration, options/reconfigure behavior, diagnostics, entities, and development commands), treat **README.md** as the canonical source to avoid duplication.

**Domain**: `unifi_presence`

## Project Structure

```
ha-unifi-presence/
├── custom_components/unifi_presence/
│   ├── __init__.py          # Integration setup/unload, WS lifecycle, forwards to device_tracker platform, async_remove_config_entry_device
│   ├── config_flow.py       # 2-step config flow (credentials → device selection) + options/reconfigure/reauth flows
│   ├── const.py             # Constants: domain, config keys, defaults
│   ├── coordinator.py       # DataUpdateCoordinator — WS callback + fallback poll, determines home/away state
│   ├── device_tracker.py    # ScannerEntity per tracked MAC, has_entity_name, DeviceInfo, PARALLEL_UPDATES=0
│   ├── diagnostics.py       # Diagnostics platform — exposes redacted config, device states, WS status
│   ├── helpers.py           # Shared async create_controller factory
│   ├── icons.json           # Entity icon translations (mdi icons per platform/translation_key)
│   ├── manifest.json        # HA integration manifest (HACS-compatible)
│   ├── strings.json         # Translatable strings for config/options/reconfigure/reauth flows + entity/exception translations
│   ├── websocket.py         # WebSocket lifecycle manager — connect, reconnect, health check
│   └── translations/
│       └── en.json          # English translations (mirror of strings.json)
├── tests/                   # Tests
├── .github/workflows/
│   └── validate.yml         # CI: ruff lint, pytest, HACS validation
├── pyproject.toml           # Project config and dev dependencies
├── hacs.json                # HACS metadata
├── README.md                # User-facing documentation
└── LICENSE.md               # Apache-2.0
```

## Key Architecture

- **Config flow** (`config_flow.py`): Two-step setup — first collects UniFi controller credentials and validates them, then fetches all known clients for the user to select which devices to track. If discovery returns no clients, setup aborts with `no_devices_discovered` instead of proceeding to an empty device-selection step. Options flow (subclassing `OptionsFlowWithReload`) allows post-setup changes to tracked devices, fallback poll interval, and away threshold; validates that at least one device is selected; automatically reloads the integration on change. Reconfigure flow allows changing controller host/port/site/username/password/ssl_verify without removing the integration; duplicate-unique-id check runs before credential validation. Reauthentication flow (`async_step_reauth` / `async_step_reauth_confirm`) triggered by `ConfigEntryAuthFailed` allows updating credentials without removing the integration. All form steps include `data_description` for every field.

- **Coordinator** (`coordinator.py`): Subclass of `DataUpdateCoordinator`. Uses WebSocket as primary update mechanism via `process_message()` callback for real-time `sta:sync` client events. Falls back to REST polling (default 300s) to catch missed events. Compares `last_seen` timestamps against the configurable `away_seconds` threshold to determine home/away state. Keeps the existing data object when device states haven't changed to avoid unnecessary entity writes, refreshing `client_info` in-place. Handles session re-authentication on expiry. Uses `frozenset` for O(1) tracked MAC lookups. Exposes a public `controller` property for external access to the cached controller. Uses a shared `_build_client_info()` static method for normalised client info construction.

- **WebSocket** (`websocket.py`): Manages the persistent WebSocket connection to the UniFi controller. Subscribes to `MessageKey.CLIENT` (sta:sync) messages for real-time client state updates. Includes automatic reconnect with backoff on disconnection (using HA's `async_call_later`), periodic health checks, and dispatcher signals for availability changes. Uses a `_stopped` guard flag to prevent post-unload reconnect activity. Tracks `_cancel_retry` timer handle and `_reconnect_task` so `stop()` can cancel them. Modeled after the official HA UniFi integration pattern.

- **Device tracker** (`device_tracker.py`): Each tracked MAC gets a `ScannerEntity` (subclass of `CoordinatorEntity`). Uses `has_entity_name = True` with `_attr_name = None` so the entity inherits the device name. Creates standalone per-client `DeviceInfo` objects with MAC-based identifiers `(DOMAIN, mac)`, `CONNECTION_NETWORK_MAC`, `default_manufacturer` "Ubiquiti Networks", and `default_name` derived from the UniFi client record (no `via_device`). Sets `PARALLEL_UPDATES = 0` (coordinator handles all data fetching). Uses `translation_key = "presence"` for translatable entity names. `unique_id` is the MAC address (inherited from `ScannerEntity`). Reload on options change is handled by `OptionsFlowWithReload` — no manual update listener needed.

- **Init** (`__init__.py`): Sets up the coordinator on entry load, starts the WebSocket connection for real-time updates, stores coordinator in `entry.runtime_data` (typed via `UnifiPresenceConfigEntry`), and forwards setup to the `device_tracker` platform. Registers `EVENT_HOMEASSISTANT_STOP` handler to cleanly stop WebSocket on HA shutdown. Stops WebSocket and cleans up on unload. Implements `async_remove_config_entry_device` to block removal only while a device’s MAC (from `device_entry.connections`) remains in `coordinator.tracked_devices`.

## Canonical References

- **README.md**:
  - Requirements and install steps
  - Configuration / options / reconfigure / reauthentication UX
  - Removal instructions
  - Supported devices and functions
  - How data is updated (push/poll strategy)
  - Entity behavior
  - Use cases and automation examples
  - Known limitations
  - Troubleshooting
  - Local development setup, test, and lint commands
- **`custom_components/unifi_presence/manifest.json`**: integration runtime requirements
- **`pyproject.toml`**: dev tooling and lint/test configuration

> **Important**: Always run `source .venv/bin/activate` before `pytest`, `ruff`, or `pip`. Never use system Python packages.
>
> **Ruff note**: `target-version` is set to `py313` in `pyproject.toml` due to a ruff 0.15.x formatter bug with `py314` that strips parentheses from `except (X, Y):` tuples. Tests must be run with `PYTHONPATH=. pytest tests/ -v` (editable install has py3.14 compat issues).

## Conventions

Follow official Home Assistant developer guidelines.

### Code Style
- `from __future__ import annotations` in every file; `typing.Final` for constants; full type hints
- File header docstrings describe the file's purpose
- f-strings for general formatting; lazy `%s` in log messages; never log credentials
- `_LOGGER.debug` for dev messages, `_LOGGER.info` for user-relevant events; no trailing periods
- Google-style docstrings when extended docs are needed
- Lint: `ruff check .`; format: `ruff format .`; no unused imports; imports sorted (isort/ruff I001)

### Home Assistant Core Compliance
- All I/O must be `async`; never block the event loop
- Store per-entry runtime data in `entry.runtime_data` (typed as `UnifiPresenceConfigEntry`)
- Poll via `DataUpdateCoordinator`; never poll inside entities
- Entities subclass appropriate HA base (`ScannerEntity`, `CoordinatorEntity`)
- Config via `ConfigFlow`/`async_step_user`; options via `OptionsFlow`; no YAML config
- HTTP sessions via `async_get_clientsession()` (SSL) / `async_create_clientsession()` (non-SSL with unsafe CookieJar); controller creation via `create_controller()` in `helpers.py`
- Platform setup via `async_forward_entry_setups()`; cleanup via `async_unload_platforms()`
- `unique_id` must be stable and globally unique (`ScannerEntity` uses `mac_address`)
- Reload on options change via `OptionsFlowWithReload`; all API calls go through `aiounifi`

### Entity Guidelines
- `name` identifies the data point only, not the device or entity type
- No I/O in properties — cache in coordinator's `_async_update_data`
- No `update()` in constructors; use `add_entities(devices, update_before_add=True)` if needed
- UTC timestamps for time attributes; use lifecycle hooks for subscriptions/cleanup
- Never pass `hass` to entities — it is set automatically

### Error Handling & Auth
- `ConfigEntryAuthFailed` for persistent auth failures; `UpdateFailed` for transient errors
- On session expiry, re-auth once; if still rejected, raise `ConfigEntryAuthFailed`

### Permissions
- Entity services auto-check permissions via `async_register_entity_service()`
- Custom services: check `user.permissions.check_entity(entity_id, POLICY_CONTROL)`; admin-only via `async_register_admin_service`; propagate `call.context`

### Schema & Validation
- `voluptuous` schemas; `cv.port` for ports, `cv.multi_select()` for multi-select
- `vol.Required` before `vol.Optional`; defaults in schema (never `default=None` for `cv.string`)
- Numeric fields: `vol.All(int, vol.Range(min=1))`

### Testing
- Uses `pytest-homeassistant-custom-component`; requires `enable_custom_integrations` fixture for config flow tests
- Patch `async_setup_entry` in config flow tests; mock `create_controller` from `helpers.py`
- Use `MagicMock` for controller objects with explicit `AsyncMock()` for async methods (login, start_websocket, clients.update); never use bare `AsyncMock()` for controller objects (causes unawaited coroutine GC warnings)
- All controller mocks must include `messages.subscribe = MagicMock(return_value=MagicMock())` and `connectivity = MagicMock()` for WebSocket setup
- All tests must pass: `PYTHONPATH=. pytest tests/ -v`
- Run ruff before committing: `ruff check . && ruff format .`

## Constants (const.py)

| Constant | Default | Description |
|---|---|---|
| `DEFAULT_AWAY_SECONDS` | 60 | Seconds before marking a device as away |
| `DEFAULT_FALLBACK_POLL_INTERVAL` | 300 | Fallback REST poll interval (seconds); WebSocket is primary |
| `DEFAULT_SITE` | `"default"` | UniFi site name |
| `DEFAULT_PORT` | 443 | UniFi controller port |
| `DEFAULT_SSL_VERIFY` | `False` | SSL certificate verification |
