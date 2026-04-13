# Copilot Instructions

## Overview
- This repository is a Home Assistant custom integration named `unifi_presence`. It tracks selected UniFi network clients as `device_tracker` entities using a push-primary WebSocket connection plus REST polling fallback.
- Treat this as a Home Assistant integration repo, not a generic Python app: there is no app entrypoint, no Docker/devcontainer, and no local build script.

## Bootstrap And Validation
- Work from the repo root.
- Use the repo Python version from `.python-version`.
- Create and activate the venv before running repo commands:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```
- Install dependencies with:
  ```bash
  pip install ".[dev]"
  pre-commit install
  ```

## Local Commands
- Lint:
  ```bash
  source .venv/bin/activate
  ruff check .
  ruff format --check .
  ```
- Type check:
  ```bash
  source .venv/bin/activate
  mypy --strict custom_components/unifi_presence/
  ```
- Tests:
  ```bash
  source .venv/bin/activate
  PYTHONPATH=. pytest tests/ -v
  ```
- Full local pre-checkin:
  ```bash
  source .venv/bin/activate
  pre-commit run --all-files
  PYTHONPATH=. pytest tests/ -v
  ```

## CI And Required Checks
- CI is defined in `.github/workflows/validate.yml`.
- PRs to `main` run four jobs: `pre-commit`, `test`, `hacs`, and `hassfest`.
- `hacs` is pinned to `hacs/action@22.5.0`.
- `hassfest` is pinned to `home-assistant/actions/hassfest@1.0.0`.

## Architecture And Invariants
- `custom_components/unifi_presence/config_flow.py`: credential entry, site selection, tracked-device selection, options flow, reauth, and reconfigure. Config identity is site-based, using UniFi `site_id` as the unique ID.
- `custom_components/unifi_presence/coordinator.py`: `DataUpdateCoordinator` implementation. WebSocket is primary, fallback poll is secondary. Re-auth once on session expiry, then raise `ConfigEntryAuthFailed` if credentials still fail.
- `custom_components/unifi_presence/websocket.py`: reconnect/backoff/health-check manager. Preserve `_stopped` semantics and stale-session recovery.
- `custom_components/unifi_presence/device_tracker.py`: one `ScannerEntity` per tracked MAC. No device-registry devices are created.
- `custom_components/unifi_presence/helpers.py`: shared MAC identity helpers, controller/session lifecycle helpers, and runtime summary helpers.

## Change Guidance
- Preserve the current behavior split: offline tracked clients are `not_home` but still `available`; only coordinator/controller failures make entities unavailable.
- Preserve site-scoped identity.
- Keep translation-sensitive UI changes in sync across `strings.json` and `translations/en.json`.
- If you change dependency pins or metadata, update `tests/test_project_metadata.py`.
