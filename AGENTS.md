# AGENTS.md

## Runtime

- UI-only Home Assistant custom integration. `custom_components/unifi_presence/__init__.py` first-refreshes the coordinator, stores it in `entry.runtime_data`, wires WebSocket, and forwards only `Platform.DEVICE_TRACKER`. `config_flow.py` owns setup/options/reauth/reconfigure.

## Commands

- Bootstrap with external uv and Python from `.python-version`: `uv sync --locked`. CI pins uv in `.github/workflows/validate.yml`. Keep `[tool.uv] package = false`: Home Assistant tests scan `sys.path` as component directories, so editable installation breaks discovery.
- Install hooks once: `uv run --locked --no-sync prek install`
- Local CI checks: `uv run --locked --no-sync prek run --all-files`, then `uv run --locked --no-sync pytest tests/ -v`. Prek fixes Ruff issues and formatting before strict mypy; CI also runs HACS, hassfest, and zizmor.
- The local mypy hook intentionally uses `uv run --no-config --locked --no-sync mypy --strict custom_components/unifi_presence/`: Home Assistant brings uv into `.venv`; the hook uses it only as a runner.
- Focused tests need `--no-cov` to avoid the whole-integration 98% coverage gate: `uv run --locked --no-sync pytest tests/test_coordinator_heartbeat.py -k expiry -v --no-cov`.
- Pytest enables asyncio auto mode/debug and treats warnings as errors. Ruff bans `from __future__ import annotations` on this Python baseline.

## Architecture Traps

- Identity is `entry.unique_id` (UniFi `site_id`); `entry.data["site"]` is the request-facing short site name. Tracker IDs are `{site_id}-{normalized_mac}`, falling back to `entry.entry_id` for entries without a unique ID.
- Reconfigure preserves site identity across host changes and migrates legacy tracker IDs. Preflight all ID conflicts before mutation; keep the final ownership check, registry migration, and entry update synchronous to prevent competing flows claiming the same ID.
- Initial site listing connects with `site=""`; `helpers.create_controller_for_params(..., resolve_legacy_site=True)` resolves legacy stored site IDs to short names for runtime/options/reauth.
- Trackers are entity-registry-only `ScannerEntity` instances for selected clients. Preserve the MAC-registration opt-out, enabled-default and site-scoped unique-ID overrides, and inherited associated-zone lifecycle; connected state need not be `home`.
- Presence combines WebSocket `sta:sync`, local heartbeat expiry, and REST fallback; `last_seen` must never move backward across those sources. Heartbeat-only changes use `_publish_local_state_change()`, not `async_set_updated_data()`, to preserve poll timers and `last_update_success`. Away clients stay available; controller failures make them unavailable.
- Coordinator retries authentication once on session expiry: successful recovery restarts WebSocket with the new controller; repeated auth failure raises `ConfigEntryAuthFailed`.
- WebSocket health comes from any inbound frame via the temporary `messages.new_data` wrapper, not only subscribed `sta:sync` events. Restore the wrapper on exit; replacement runners must await cancellation, and stopped/stale runners must not schedule retries.
- Discovery retains selected MACs absent from UniFi with the `No longer in UniFi Client Devices` label. Only explicit deselection removes entity-registry entries.
- `helpers.async_refresh_client_stores()` centralizes communication-error handling: `require_active_refresh=False` tolerates failed stores unless both fail with no cache; `True` requires `clients.update()` but treats `clients_all` as best-effort. Unexpected exceptions propagate.
- Stored config must include `ssl_verify`. In either SSL mode, `helpers.create_controller()` owns a session wrapper with a fresh cookie jar; only the connector is shared with HA. Release through `async_close_controller()`, which detaches without closing the shared connector.
- Options borrow the runtime controller; close only their fallback controller. Failed platform unload leaves runtime active; successful unload awaits WebSocket stop before coordinator shutdown. Partial setup must release its controller.

## Tests And Edit Traps

- Patch factories at the import site: flows/options use `config_flow.create_controller_for_params`; runtime tests use `coordinator.create_controller_for_params`; helper factory tests use `helpers.create_controller`.
- Reuse `tests/conftest.py`'s `make_mock_controller()` and coordinator fixtures: client stores are dict-like with async `update()` (failures go on `update_mock`), while `sites.values()` is synchronous. Flow fixture `_bypass_setup` enables custom integrations and prevents real setup after entry creation.
- Use `tests/websocket_helpers.py` for WebSocket mocks and task/start waits; coordinator fixtures run unload callbacks to clean up timers/controllers.
- Keep `custom_components/unifi_presence/strings.json` and `translations/en.json` aligned; picker/error copy assertions live in `tests/test_config_flow_options.py`.
- Keep `aiounifi` out of the manifest's `loggers`: its debug output can expose credentials and cookies. `tests/test_logging.py` verifies HA's integration debug toggle does not enable dependency DEBUG logging.

## Metadata And CI

- `pyproject.toml` pins the tested `aiounifi` version (`==96`); the integration's `manifest.json` declares a runtime minimum (`>=96`). Preserve that distinction when updating them, and refresh `uv.lock` for dependency changes.
- Keep the minimum Home Assistant version in `hacs.json` aligned with the requirement documented in `README.md`.
- Workflow actions are allowlisted and hash-pinned in `.github/zizmor.yml`; its `stale-action-refs` exceptions use workflow line numbers, so check them when moving steps.
- Dispatch `.github/workflows/release.yml` from `main` with a higher `X.Y.Z` version (no `v`). It updates `pyproject.toml`, `manifest.json`, and `uv.lock` and opens `release/vX.Y.Z`; merging the marked release PR creates a draft release.
