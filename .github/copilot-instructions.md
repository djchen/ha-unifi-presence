# Copilot Instructions

## Overview
- This repository is a Home Assistant custom integration named `unifi_presence`. It tracks selected UniFi network clients as `device_tracker` entities using a push-primary WebSocket connection plus REST polling fallback.
- Repo size is small: one Python package in `custom_components/unifi_presence/` and a focused `tests/` suite. Main language is Python 3.14. Home Assistant and `aiounifi` are the main runtime dependencies.
- Treat this as a Home Assistant integration repo, not a generic Python app: there is no app entrypoint, no Docker/devcontainer, and no local build script.

## Bootstrap And Validation
- Always work from the repo root. The repo has `.python-version` set to `3.14.3`, and `pyproject.toml` requires `>=3.14.3`.
- Always create/activate the venv before running repo commands:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```
- Always install dependencies with:
  ```bash
  pip install ".[dev]"
  ```
- Validated: fresh `pip install ".[dev]"` succeeds, but pip backtracks for `pytest-homeassistant-custom-component` and took about 70s in a clean venv. In the existing project venv it took about 3s.
- Validated: creating a venv outside the repo root can pick Python 3.13 and fail with `requires a different Python: 3.13.12 not in '>=3.14.3'`. Mitigation: create the venv from the repo root or explicitly use Python 3.14.3.
- `pip install -e ".[dev]"` currently succeeds, but it also triggers long dependency resolution. Prefer non-editable `pip install ".[dev]"` because that is what README and CI use.
- Optional but validated useful: `pre-commit install` succeeds and installs `.git/hooks/pre-commit`.

## Local Commands
- Lint:
  ```bash
  source .venv/bin/activate
  ruff check .
  ruff format --check .
  ```
  Validated: both succeed in well under 1s.
- Type check:
  ```bash
  source .venv/bin/activate
  mypy --strict custom_components/unifi_presence/
  ```
  Validated: succeeds in about 2s.
- Tests:
  ```bash
  source .venv/bin/activate
  PYTHONPATH=. pytest tests/ -v
  ```
  Validated: full suite passed (`173 passed`) in about 7s and enforces 95% coverage via `pyproject.toml`.
- Also validated: `pytest tests/ -v` passed in the activated repo venv, but `PYTHONPATH=.` is still the documented/CI-safe form. Prefer it.
- Full local pre-checkin equivalent:
  ```bash
  source .venv/bin/activate
  pre-commit run --all-files
  PYTHONPATH=. pytest tests/ -v
  ```
  Validated: both succeed locally.
- There is no meaningful local `run` command for the integration itself. `hass --version` works in the venv, but behavior is tested through pytest rather than by launching Home Assistant manually.

## CI And Required Checks
- CI is defined in `.github/workflows/validate.yml`.
- PRs to `main` run four jobs: `pre-commit`, `test`, `hacs`, and `hassfest`.
- `pre-commit` installs `.[dev]` and runs `pre-commit run --all-files`.
- `test` installs `.[dev]` and runs `PYTHONPATH=. pytest tests/ -v`.
- `hacs` runs `hacs/action@main` for integration validation.
- `hassfest` runs `home-assistant/actions/hassfest@master`.
- Local equivalents for HACS/Hassfest are not configured in this repo, so rely on CI for those two after completing local lint, mypy, and pytest.

## Architecture And Invariants
- `custom_components/unifi_presence/__init__.py`: integration setup/unload, coordinator lifecycle, entity cleanup for deselected MACs, WebSocket startup/shutdown.
- `config_flow.py`: credential entry, site selection, tracked-device selection, options flow, reauth, and reconfigure. Config identity is site-based, using UniFi `site_id` as the unique ID.
- `coordinator.py`: `DataUpdateCoordinator` implementation. WebSocket is primary, fallback poll is secondary. Re-auth once on session expiry, then raise `ConfigEntryAuthFailed` if credentials still fail.
- `websocket.py`: reconnect/backoff/health-check manager. Preserve `_stopped` semantics and stale-session recovery.
- `device_tracker.py`: one `ScannerEntity` per tracked MAC. No device-registry devices are created; entities are entity-registry only.
- `diagnostics.py` and `system_health.py`: redacted diagnostics and Home Assistant system health output.
- `helpers.py`: `create_controller()` factory. If you change controller auth/session behavior, update tests.

## Change Guidance
- Preserve the current behavior split: offline tracked clients are `not_home` but still `available`; only coordinator/controller failures make entities unavailable.
- Preserve the active-plus-historical client merge behavior: active `clients` data wins, `clients_all` is best-effort metadata fallback.
- Preserve site-scoped identity: do not key config entries or entity uniqueness by host aliases.
- Keep translation-sensitive UI changes in sync across `strings.json` and `translations/en.json`.
- If you change dependency pins or quality scale metadata, also update the matching tests in `tests/test_project_metadata.py`.
- For config flow and coordinator tests, mock `create_controller` through the module-local alias and include `messages.subscribe` plus `connectivity` on controller mocks; `tests/conftest.py` shows the expected pattern.

## File Map
- Root: `README.md`, `pyproject.toml`, `.pre-commit-config.yaml`, `.python-version`, `hacs.json`, `.github/workflows/validate.yml`.
- Integration package: `custom_components/unifi_presence/`.
- Tests: `tests/` with focused files matching modules: `test_config_flow.py`, `test_coordinator.py`, `test_websocket.py`, `test_init.py`, `test_device_tracker.py`, `test_diagnostics.py`, `test_system_health.py`, `test_helpers.py`, `test_project_metadata.py`.

Trust these instructions first. Only search the repo when the needed detail is missing here or the code has clearly drifted from these instructions.
