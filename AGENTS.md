# AGENTS.md

## Project Overview

**Domain**: `unifi_presence` · User-facing docs in **README.md** · Dev tooling in **pyproject.toml**

## Project Structure

```text
ha-unifi-presence/
├── custom_components/unifi_presence/
│   ├── __init__.py        # Setup/unload, WS lifecycle
│   ├── config_flow.py     # Credentials → device selection + options/reconfigure/reauth
│   ├── const.py           # Constants and defaults
│   ├── coordinator.py     # DataUpdateCoordinator — WS push + REST poll fallback
│   ├── device_tracker.py  # ScannerEntity per tracked MAC
│   ├── diagnostics.py     # Redacted config + runtime state
│   ├── helpers.py         # create_controller() factory
│   ├── websocket.py       # WS connect, reconnect, health check
│   ├── icons.json         # MDI icons
│   ├── manifest.json      # HA/HACS manifest
│   ├── strings.json       # Translatable UI strings
│   └── translations/en.json
├── tests/
├── .github/workflows/validate.yml  # CI: ruff, pytest, mypy, HACS, hassfest
├── .pre-commit-config.yaml         # ruff + mypy hooks
├── pyproject.toml
└── README.md
```

## Architecture

- **Config flow**: 2-step (credentials → device selection). Options via `OptionsFlowWithReload`. Reconfigure and reauth flows. Aborts on no clients discovered.
- **Coordinator**: WS primary (`process_message` for `sta:sync`), REST poll fallback. Re-auths on session expiry. `frozenset` for O(1) MAC lookups. Skips entity writes when state unchanged.
  - **Active + historical merge**: Each poll does a best-effort `clients_all.update()` (historical store, failure non-fatal) followed by a required `clients.update()` (active store). For each tracked MAC: active clients use live `last_seen`; offline clients pull metadata from `clients_all`, then from prior coordinator data, then fall back to the bare MAC address.
  - **Availability**: Reflects coordinator/controller health only. An individual offline client is `not_home` + `available=True`. Entities become `unavailable` only when the coordinator itself cannot fetch data (e.g., controller unreachable, auth failure).
- **WebSocket**: Auto-reconnect with backoff, health checks, `_stopped` guard. Stale startup detection reconnects if no message has been received since startup and the connection age exceeds `STALE_WEBSOCKET_INTERVAL`. Modeled after official HA UniFi integration.
- **Device tracker**: `ScannerEntity` + `CoordinatorEntity`. No per-client device entries. `has_entity_name = True` to match the official HA UniFi integration, with the displayed name derived from coordinator `client_info`.
- **Init**: Coordinator → WS start → platform forward.

## Development

> Always activate venv first: `source .venv/bin/activate`

- **Install**: `pip install ".[dev]" && pre-commit install`
- **Test**: `PYTHONPATH=. pytest tests/ -v` (don't use editable install — py3.14 compat issue)
- **Lint**: `ruff check . && ruff format .`
- **Type check**: `mypy --strict custom_components/unifi_presence/`
- **Coverage**: enforced at 95% via pytest-cov

## Conventions

Follow official HA developer guidelines. Project-specific notes:

### Code Style
- `from __future__ import annotations` in every file; full type hints
- Lazy `%s` in log messages; never log credentials
- Google-style docstrings; file-level docstrings describe purpose
- **Python 3.14+ specific**: Embrace the latest syntax features seamlessly. [PEP 758](https://peps.python.org/pep-0758/) allows catching multiple exceptions without parentheses — do **not** confuse this with the legacy Python 2 `except Type, variable:` binding form.

  ```python
  # PEP 758 — catch multiple exceptions (Python 3.14+)
  except ConnectionError, TimeoutError:
      ...

  # Binding the exception to a variable still requires `as` + parentheses
  except (ConnectionError, TimeoutError) as err:
      log(err)
  ```

  The old Python 2 form `except Exception, e:` (where `e` captured the exception) is **not** valid in Python 3 and looks deceptively similar — always use `as` for binding.

### Testing
- `pytest-homeassistant-custom-component`; `enable_custom_integrations` fixture for config flow tests
- Mock `create_controller` via the module-local alias (e.g. `custom_components.unifi_presence.config_flow.create_controller` or `custom_components.unifi_presence.coordinator.create_controller`); use `MagicMock` for controller with explicit `AsyncMock()` for async methods
- Controller mocks must include `messages.subscribe = MagicMock(return_value=MagicMock())` and `connectivity = MagicMock()`

### Error Handling
- `ConfigEntryAuthFailed` for persistent auth failures; `UpdateFailed` for transient
- On session expiry: re-auth once, then raise `ConfigEntryAuthFailed`
