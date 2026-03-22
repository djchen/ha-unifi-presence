# AGENTS.md

## Project Overview

**Domain**: `unifi_presence` · User-facing docs in **README.md** · Dev tooling in **pyproject.toml**

## Project Structure

```
ha-unifi-presence/
├── custom_components/unifi_presence/
│   ├── __init__.py        # Setup/unload, WS lifecycle, stale device cleanup
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
- **WebSocket**: Auto-reconnect with backoff, health checks, `_stopped` guard. Modeled after official HA UniFi integration.
- **Device tracker**: `ScannerEntity` + `CoordinatorEntity`. Per-client `DeviceInfo` with MAC identifiers. `has_entity_name = True`, `_attr_name = None`.
- **Init**: Coordinator → WS start → platform forward. Stale device cleanup on reload. `async_remove_config_entry_device` blocks removal of tracked MACs.

## Development

> Always activate venv first: `source .venv/bin/activate`

- **Install**: `pip install ".[dev]" && pre-commit install`
- **Test**: `PYTHONPATH=. pytest tests/ -v` (don't use editable install — py3.14 compat issue)
- **Lint**: `ruff check . && ruff format .`
- **Type check**: `mypy custom_components/`
- **Coverage**: enforced at 95% via pytest-cov (currently 100%)

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
- Mock `create_controller` from `helpers.py`; use `MagicMock` for controller with explicit `AsyncMock()` for async methods
- Controller mocks must include `messages.subscribe = MagicMock(return_value=MagicMock())` and `connectivity = MagicMock()`

### Error Handling
- `ConfigEntryAuthFailed` for persistent auth failures; `UpdateFailed` for transient
- On session expiry: re-auth once, then raise `ConfigEntryAuthFailed`
