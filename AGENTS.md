# AGENTS.md

## Local checks

- Bootstrap with external uv (`.github/workflows/validate.yml`) and the Python in `.python-version`: `uv sync --locked`, then `uv run --locked --no-sync prek install`. Keep `package = false`; editable installs interfere with Home Assistant (HA) component discovery.
- Run `uv run --locked --no-sync prek run --all-files` before `uv run --locked --no-sync pytest tests/ -v`. Hooks fix Ruff issues before strict mypy; rerun after fixes. CI also validates HACS and hassfest.
- Focused run: `uv run --locked --no-sync pytest tests/test_coordinator_heartbeat.py -k expiry --no-cov`. The 98% gate is suite-wide. Keep `-p no:respx` so HA installs its httpx alias first.
- Tests use official `pytest-homeassistant` (`pytest_homeassistant` imports), not `pytest-homeassistant-custom-component`. Asyncio auto/debug mode and warnings-as-errors are on.
- Mypy alone: `uv run --no-config --locked --no-sync mypy --strict custom_components/unifi_presence/`. The hook uses HA's bundled uv only as a runner.

## Presence and controller lifetime

- `custom_components/unifi_presence/__init__.py`: first refresh and reconcile identity before assigning `entry.runtime_data`; start the WebSocket after tracker setup. If setup fails after the coordinator exists, shut it down; a failed platform unload leaves it running.
- Override `ScannerEntity` defaults: enable selected trackers, use empty `DeviceInfo()` to prevent MAC-based device registration, and inherit HA's associated-zone state.
- Merge `sta:sync` and REST without moving `last_seen` backwards. Publish heartbeat expiry via `_publish_local_state_change()` to preserve the poll timer and `last_update_success`. Away clients stay available; controller failures make them unavailable.
- Polls require fresh `clients` (`clients_all` is best-effort); discovery can proceed if either refresh succeeds or either store has cached data. `async_refresh_client_stores()` tolerates only communication errors and re-raises unexpected errors; setup and options flows handle discovery failures at the flow boundary. Keep missing selected MACs in the picker: only deselection removes registry records.
- Hold `_client_store_lock` through polls, identity requests and options discovery, including post-refresh reads. Options borrow via `async_fetch_available_clients()` and close only their fallback controller.
- Stored config requires `ssl_verify`. Both SSL modes share HA's connector, never cookies. Never close HA's shared connector. Shield shutdown: stop producers, await WebSocket runner cleanup, then drain `_client_store_lock` before `async_close_controller()` detaches the owned session. After shutdown starts, discard late results without publishing and release any newly acquired controller.
- After a poll's single successful auth retry, restart the WebSocket on the replacement controller; a repeated auth failure raises `ConfigEntryAuthFailed`. The watchdog needs every frame via `messages.new_data`: restore it on every runner exit, await the old runner's cleanup before replacing it, and never let stopped or stale runners schedule retries.

## Site and tracker identity

- `entry.unique_id` is the site ID; `entry.data["site"]` is the request-facing short name. Tracker IDs use `{site_id}-{normalized_mac}`, falling back to `entry.entry_id` for entries without a unique ID.
- List sites initially with `site=""`; reconfigure uses the stored site. Resolve legacy stored IDs via `create_controller_for_params(..., resolve_legacy_site=True)` (default `False`) in runtime, options and reauth (reauth only when a unique ID exists).
- Use the full snapshot returned by `helpers.async_fetch_site_snapshot()` for Network 11 recovery; `sites.update()` retains vanished IDs. `site_identity.resolve_network_11_site()` requires the stored lowercase ObjectId to be absent and exactly one site to match the unchanged short name. That site's ID must be a lowercase UUID.
- Commit Network 11 identity recovery only during entry setup, never during polling. Eligibility depends on site identity evidence, not the config entry's schema version.
- Snapshots turn off aiounifi's automatic login retry; after `LoginRequired`, relogin at most once within the deadline, and only if `can_retry_login` was true on entry. Restore the flag on every exit. Validate every row, keep duplicate names (they mean ambiguity), and classify only structural decode errors in the request adapter.
- Snapshot entry data/ID before first refresh; recheck after fetching sites. Check shutdown, competing owners and all affected trackers (even disabled/unselected) before writing, with no intervening await.
- Persist the migration marker with the new identity; replay before creating trackers. Keep the marker after success: HA saves entries and registry records independently.
- Reconfigure may change endpoints for the same site ID. Network 11 recovery by name requires the exact stored host/port: preflight, save settings, schedule reload; setup revalidates and commits. Legacy reconfigure can migrate immediately. On the pinned HA version, a failed unload never commits deferred recovery and needs an HA restart; disabled entries recover only in setup after being enabled.
- New flows check exact and pending owners, including disabled entries, at both site selection and final creation.

## Test fixtures

- In `tests/conftest.py`, `_bypass_setup` stubs entry setup for flow tests; use `enable_custom_integrations` for real setup. `assert_no_reconfigure_mutations()` checks entries, registry and reload on rejection.
- Patch `config_flow.create_controller_for_params` for flows/options, `coordinator.create_controller_for_params` and `coordinator.async_fetch_site_snapshot` for runtime, `helpers.create_controller` for factory tests. Set `make_mock_controller()` store failures on `update_mock`.
- Use `tests/site_identity_helpers.py` for recovery: `site-default-id` is ineligible; mock `controller.request` for Network 11 snapshot evidence; legacy site resolution reads `sites.values()`. Use `tests/websocket_helpers.py` for runner waits.

## Coupled edits

- Use Probatio for flow schemas; Ruff bans Voluptuous and `from __future__ import annotations`. Change `strings.json` and `translations/en.json` together.
- Keep `aiounifi` out of manifest `loggers`; never log raw site snapshot responses or errors, and suppress site-fetch exception chains. HA debug output can expose credentials or cookies (`tests/test_logging.py`).
- `manifest.json` sets the aiounifi minimum; `pyproject.toml` pins the tested version. Align the official `pytest-homeassistant` floor with `hacs.json` and README; update `uv.lock` without overriding the test package's HA pin.
- Workflow actions must be allowlisted in `.github/zizmor.yml` and pinned to full commit hashes. When moving the HACS or hassfest steps in `validate.yml`, update their line-number exceptions in `.github/zizmor.yml`.
- Dispatch `release.yml` from `main` with a higher `X.Y.Z` (no `v`). It bumps metadata; merging its PR with the body marker intact creates the draft release.
