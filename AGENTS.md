# AGENTS.md

## Repo

- Home Assistant custom integration only: runtime code is under `custom_components/unifi_presence`, tests under `tests`; there is no standalone app, dev server, YAML setup path, controller auto-discovery, or non-`device_tracker` platform.
- `__init__.py` creates `UnifiPresenceCoordinator`, performs first refresh, wires `UnifiPresenceWebsocket`, then forwards only `Platform.DEVICE_TRACKER`; `config_flow.py` owns setup/options/reauth/reconfigure.

## Commands

- Use Python `3.14.5`, selected by `.python-version`. Locally installed uv is bootstrap tooling; CI explicitly pins uv `0.12.5`. Set up dependencies with `uv sync --locked`; `[tool.uv] package = false` prevents editable installation because Home Assistant custom-component tests scan `sys.path` as component directories.
- Install hooks once: `uv run --locked --no-sync prek install`
- Local CI parity: `uv run --locked --no-sync prek run --all-files` and `uv run --locked --no-sync pytest tests/ -v`; `.github/workflows/validate.yml` also runs HACS, hassfest, and zizmor.
- Workflow edits must satisfy `.github/zizmor.yml`: `uses:` actions are allowlisted and hash-pinned; update the allowlist deliberately when adding actions.
- Prek may edit files: Ruff runs with `--fix`, then `ruff-format`, then `mypy --strict custom_components/unifi_presence/`.
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
- `async_refresh_client_stores()` centralizes `UNIFI_COMMUNICATION_EXCEPTIONS` handling: `DISCOVERY` tolerates one failed client store and only errors when both stores fail with no cache; `RUNTIME` treats `clients_all` as best-effort but requires `clients.update()`.
- Stored config data is expected to include `ssl_verify`; do not reintroduce missing-field fallbacks for legacy entries.
- When `ssl_verify` is `false`, `create_controller()` owns an aiohttp session on the controller; release it through `async_close_controller()` (which detaches the Home Assistant shared-connector session wrapper).
- The options flow borrows the loaded coordinator controller when possible; close only the fallback controller the flow creates itself.
- Preserve setup/unload cleanup ordering: a failed platform unload leaves runtime active; successful unload awaits WebSocket stop before coordinator shutdown, and partial setup must release its controller.

## Tests And Edit Traps

- Patch controller factories where the code under test imports them: flows/options use `config_flow.create_controller_for_params`, coordinator/init/diagnostics/system-health tests use `coordinator.create_controller_for_params`, and helper tests patch `helpers.create_controller` or `helpers.create_controller_with_resolved_site`.
- Config-flow tests use `enable_custom_integrations`; many also use `_bypass_setup` so entry creation does not run real integration setup.
- Controller mocks need dict-like `clients`/`clients_all` stores (`items`, `get`, iteration) with async `update`; flow tests also need async `login`, `sites.update`/`sites.values`; WebSocket tests need `messages.subscribe = MagicMock(return_value=MagicMock())`, `messages.new_data`, and `connectivity = MagicMock()`.
- UI copy or flow-error edits must keep `strings.json` and `translations/en.json` keys aligned; tests assert high-churn picker/error keys and stale removals.
- Keep the minimum Home Assistant version in `hacs.json` aligned with the requirement documented in `README.md`.
- Releases start by dispatching `.github/workflows/release.yml` from `main` with an `X.Y.Z` version; it opens `release/vX.Y.Z` after updating `pyproject.toml`, `custom_components/unifi_presence/manifest.json`, and `uv.lock`, and merging that PR creates a draft release.
