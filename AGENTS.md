# AGENTS.md

## Repo

- Home Assistant custom integration only: runtime code is under `custom_components/unifi_presence`, tests under `tests`; there is no standalone app, dev server, YAML setup path, controller auto-discovery, or non-`device_tracker` platform.
- `__init__.py` creates `UnifiPresenceCoordinator`, performs first refresh, wires `UnifiPresenceWebsocket`, then forwards only `Platform.DEVICE_TRACKER`; `config_flow.py` owns setup/options/reauth/reconfigure.

## Commands

- Use uv `0.11.16` and Python `3.14.5`: `uv sync --locked`; `[tool.uv] package = false` prevents editable installation because Home Assistant custom-component tests scan `sys.path` as component directories.
- Install hooks once: `uv run --locked --no-sync pre-commit install`
- CI-like local order: `uv run --locked --no-sync pre-commit run --all-files` then `uv run --locked --no-sync pytest tests/ -v`; CI also runs HACS, hassfest, and zizmor via `.github/workflows/validate.yml`.
- Pre-commit may edit files: Ruff runs with `--fix`, then `ruff-format`, then `mypy --strict custom_components/unifi_presence/`.
- Focused tests usually need `--no-cov` because `pyproject.toml` enforces 98% coverage, e.g. `uv run --locked --no-sync pytest tests/test_coordinator_heartbeat.py -k expiry -v --no-cov`.

## Architecture Traps

- Config-entry identity is the UniFi `site_id`; tracker unique IDs are `{site_id}-{normalized_mac}`. `entry.data["site"]` stores the UniFi short site name used in controller requests, not identity.
- Reconfigure preserves site identity across host changes, aborts if the resolved site changes, and migrates legacy tracker unique IDs when an old entry gains a real `site_id`.
- Initial setup lists sites by connecting with `site=""`, then stores the selected site's short `name`; runtime/reconfigure may resolve legacy stored site IDs back to short names before controller use.
- Trackers are entity-registry-only `ScannerEntity` instances, one per selected MAC. Do not add device-registry devices or UniFi infrastructure devices.
- Presence is push-first: WebSocket `sta:sync` updates + local heartbeat expiry + REST fallback polling. Heartbeat-only expiry must not reset refresh timers or flip `last_update_success` back to `True`.
- Offline clients are `not_home` but still `available`; only coordinator/controller failures make entities unavailable.
- Coordinator reauths once on UniFi session expiry, restarts WebSocket on recovery, then raises `ConfigEntryAuthFailed` if credentials still fail.
- WebSocket health is set by the temporary `messages.new_data` wrapper after the first inbound frame; preserve `_stopped`, watchdog, reconnect, stale-runner, and reauth-restart semantics.
- Client discovery preserves selected MACs no longer returned by UniFi and labels them `No longer in UniFi Client Devices`; only explicit deselection should remove entity-registry entries.
- Flow discovery errors only when both active and historical client sources fail with no cache; coordinator fallback treats `clients_all` as best-effort but requires active `clients.update()`.
- Stored config data is expected to include `ssl_verify`; do not reintroduce missing-field fallbacks for legacy entries.
- When `ssl_verify` is `false`, `create_controller()` owns an aiohttp session on the controller; release it through `async_close_controller()` (which detaches the Home Assistant shared-connector session wrapper).
- Python 3.14 PEP 758 syntax is valid here: `except A, B:` is intentional; use parentheses only when binding with `as`.

## Tests And Edit Traps

- Patch factories where the code under test imports them (`config_flow.create_controller`, `coordinator.create_controller`, `config_flow.create_controller_with_resolved_site`); patching `helpers.create_controller` misses most call sites.
- Config-flow tests use `enable_custom_integrations`; many also use `_bypass_setup` so entry creation does not run real integration setup.
- Controller mocks need async `login`, `clients.update`, and `clients_all.update`; flow tests also need `sites.update`/`sites.values`; WebSocket tests need `messages.subscribe = MagicMock(return_value=MagicMock())`, `messages.new_data`, and `connectivity = MagicMock()`.
- UI copy or flow-error edits must keep `strings.json` and `translations/en.json` keys aligned; tests assert high-churn picker/error keys and stale removals.
- Keep release metadata in sync: `pyproject.toml`, `manifest.json`, and `uv.lock` version; `aiounifi==91` in `pyproject.toml` and `manifest.json`; `hacs.json` HA minimum vs. `README.md`; and `manifest.json` `quality_scale` vs. `custom_components/unifi_presence/quality_scale.yaml` (`tests/test_project_metadata.py`, `.github/workflows/release.yml`).
