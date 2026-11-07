# AGENTS.md

## Commands

- Bootstrap with external uv: `uv sync --locked` (Python is pinned in `.python-version`, uv in `.github/workflows/validate.yml`). Keep `[tool.uv] package = false`: editable installation breaks Home Assistant's `sys.path`-based component discovery.
- Install hooks once: `uv run --locked --no-sync prek install`.
- Local checks: `uv run --locked --no-sync prek run --all-files`, then `uv run --locked --no-sync pytest tests/ -v`. Hooks fix Ruff lint/formatting before strict mypy; rerun after fixes. CI additionally validates HACS and hassfest.
- Standalone typecheck: `uv run --no-config --locked --no-sync mypy --strict custom_components/unifi_presence/`. The hook's `--no-config` is intentional: Home Assistant brings another uv into `.venv`; use it only as a runner.
- Focused tests need `--no-cov` to avoid the whole-integration 98% coverage gate: `uv run --locked --no-sync pytest tests/test_coordinator_heartbeat.py -k expiry -v --no-cov`.
- Tests use official `pytest-homeassistant` (`pytest_homeassistant` imports), not `pytest-homeassistant-custom-component`. Keep its Home Assistant pin; upgrade the test package and lockfile together. Preserve pytest's `-p no:respx`: HA must install its httpx alias before respx imports. Asyncio auto/debug and warnings-as-errors are enabled.
- Use Probatio schemas, not Voluptuous. Ruff bans both `voluptuous` and `from __future__ import annotations`.

## Runtime and ownership

- UI-only integration: `custom_components/unifi_presence/__init__.py` first-refreshes the coordinator, reconciles identity before tracker construction, stores it in `entry.runtime_data`, and forwards only `DEVICE_TRACKER`. `config_flow.py` owns setup/options/reauth/reconfigure.
- Identity is `entry.unique_id` (UniFi `site_id`); `entry.data["site"]` is the request-facing short site name. Tracker IDs are `{site_id}-{normalized_mac}`, falling back to `entry.entry_id` for entries without a unique ID.
- Initial site listing connects with `site=""`; `helpers.create_controller_for_params(..., resolve_legacy_site=True)` resolves legacy stored site IDs to short names for runtime/options/reauth.
- Trackers are entity-registry-only `ScannerEntity` instances. Preserve `_attr_device_info = DeviceInfo()` (opts out of MAC-based device creation), enabled-default/site-scoped-ID overrides, and inherited associated-zone lifecycle; connected state need not be `home`.
- Presence merges WebSocket `sta:sync`, heartbeat expiry, and REST; `last_seen` must never decrease. Heartbeat-only changes use `_publish_local_state_change()`, not `async_set_updated_data()`, to preserve poll timers and `last_update_success`. Away clients stay available; controller failures make them unavailable.
- Session expiry gets one auth retry; success restarts WebSocket with the replacement controller, repeated auth failure raises `ConfigEntryAuthFailed`.
- WebSocket health observes every inbound frame via a temporary `messages.new_data` wrapper, not just subscribed client events. Restore it on exit, await retiring runners, and prevent stopped/stale runners from scheduling retries.
- Serialize polling, options discovery (including post-refresh reads), and identity requests under the coordinator's `_client_store_lock`. Options borrow runtime access through `async_fetch_available_clients()`; close only their fallback controller.
- Shutdown is shared and shielded: stop producers, await WebSocket cancellation, drain the store lock, then detach the controller. Reject late results/publications. Failed platform unload leaves runtime active; partial setup must release it.
- Stored config requires `ssl_verify`. In either SSL mode, controllers own a fresh-cookie-jar session wrapper, sharing only HA's connector. Release with `async_close_controller()` (detach), never close the shared connector.
- `helpers.async_refresh_client_stores()`: `require_active_refresh=False` tolerates communication failures unless both stores fail without cache; `True` requires active `clients.update()` but treats `clients_all` as best-effort. Unexpected errors propagate.
- Discovery retains selected MACs absent from UniFi with `No longer in UniFi Client Devices`. Only explicit deselection removes registry records.

## Site identity recovery

- `site_identity.py` owns restricted Network 11 recovery: absent lowercase ObjectId → lowercase UUID, with exactly one match for the unchanged raw stored short name. No schema/version gate or polling migration.
- Setup snapshots entry identity/data before first refresh, fetches evidence under the lifetime lock, then rechecks entry/lifecycle/ownership. Preflight every affected registry record (including disabled/unselected); keep final checks, migration, and entry update synchronous to avoid competing-flow races.
- Persist old/target identity provenance in the same entry update as recovery; replay remaining registry migrations before tracker construction. Never clear provenance just because in-memory migration succeeded: entry and registry stores save independently.
- Reconfigure preserves site identity across host changes. Network 11 name-based recovery additionally requires the exact stored host/port: read-only preflight, save settings/title, schedule reload, then independently revalidate/commit in setup after successful unload. Keep legacy reconfigure sequencing separate.
- Failed unload requires HA restart on the locked version; disabled entries recover only during setup after enablement. New setup checks exact IDs and identifiable pending recovery at both site selection and final creation, without mutating existing entries.
- Evidence must come from `helpers.async_fetch_site_snapshot()` using public `Controller.request()` and the global `SiteListRequest` path. `sites.update()` accumulates absent IDs and cannot prove replacement. Validate every row, preserve duplicate-name ambiguity, default descriptions on detached copies; classify structural decode errors only in the adapter.
- Snapshot requests disable aiounifi's recursive login retry, permit at most one explicit relogin under one deadline, and restore the flag on every exit. Never log raw responses/errors; suppress exception chains when translating identity-fetch failures to HA setup errors.

## Test and edit traps

- Patch factories at the import site: flows/options use `config_flow.create_controller_for_params`; runtime tests use `coordinator.create_controller_for_params`; helper factory tests use `helpers.create_controller`.
- Site-list tests configure `controller.request`, not only `sites.values()`. Patch runtime evidence at `coordinator.async_fetch_site_snapshot`. Use realistic lowercase ObjectId/UUID fixtures for recovery; ordinary `site-default-id` fixtures are deliberately ineligible.
- Reuse `tests/conftest.py`'s `make_mock_controller()`: stores are dict-like with async `update()` (failures on `update_mock`), but `sites.values()` is synchronous. `_bypass_setup` enables custom integrations and prevents real setup after flow completion.
- Use `tests/websocket_helpers.py` for runner/start waits; coordinator fixtures execute unload callbacks to clean up timers/controllers.
- Keep `custom_components/unifi_presence/strings.json` and `translations/en.json` aligned; picker/error copy assertions live in `tests/test_config_flow_options.py`.
- Keep `aiounifi` out of manifest `loggers`: dependency DEBUG can expose credentials/cookies. `tests/test_logging.py` guards the real HA debug toggle and identity error-chain redaction.

## Metadata and CI

- `pyproject.toml` pins tested aiounifi (`==97`); `manifest.json` declares a runtime minimum (`>=97`). Preserve this distinction and refresh `uv.lock` for dependency changes.
- Keep the HA minimum in `hacs.json`, the test-package floor, and README requirements aligned; Python requirements also live in `pyproject.toml` and `.python-version`.
- Workflow actions are allowlisted and hash-pinned in `.github/zizmor.yml`; its `stale-action-refs` exceptions use workflow line numbers, so check them when moving steps.
- Dispatch `.github/workflows/release.yml` from `main` with a higher `X.Y.Z` version (no `v`). It updates `pyproject.toml`, `manifest.json`, and `uv.lock` and opens `release/vX.Y.Z`; merging the marked release PR creates a draft release.
