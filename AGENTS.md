# AGENTS.md

## Repo

- Home Assistant custom integration only; code lives in `custom_components/unifi_presence`. There is no standalone app entrypoint or dev server.

## Setup And Verify

- Use Python `3.14.3` from `.python-version`; activate `.venv` before repo commands: `source .venv/bin/activate`
- Install dev deps/hooks: `pip install ".[dev]" && pre-commit install`
- CI-equivalent local check: `pre-commit run --all-files` then `PYTHONPATH=. pytest tests/ -v`; CI also runs HACS and hassfest.
- Pre-commit runs Ruff fixes/format plus `mypy --strict custom_components/unifi_presence/`; run standalone mypy only for focused type checks.
- Focused pytest still needs `PYTHONPATH=.` and usually `--no-cov` because project addopts enforce 98% coverage: `PYTHONPATH=. pytest tests/test_coordinator_heartbeat.py -k expiry -v --no-cov`

## Architecture Traps

- Config-entry identity is UniFi `site_id`; tracker unique IDs are `{site_id}-{normalized_mac}`. `entry.data["site"]` stores the UniFi site short name used for controller requests, so do not key identity off host.
- Reconfigure preserves site identity across host changes and migrates legacy tracker unique IDs when an older entry gains a real `site_id`.
- `device_tracker` is the only platform: one entity per tracked MAC, entity-registry only, no device-registry devices.
- Presence is push-first: WebSocket `sta:sync` updates + local heartbeat expiry + REST fallback polling. Heartbeat-only expiry must not reset refresh timers or flip `last_update_success` back to `True`.
- Offline clients being `not_home` but still `available` is intentional; only coordinator/controller failures make entities unavailable.
- Coordinator reauths once on UniFi session expiry, then raises `ConfigEntryAuthFailed` if credentials still fail.
- WebSocket health is not true until the first inbound frame; preserve `_stopped`, watchdog, reconnect, and reauth-restart semantics.
- When `ssl_verify` is `false`, `create_controller()` owns a dedicated aiohttp session; release it with `async_close_controller()` rather than tearing down Home Assistant's shared session.
- Python 3.14 PEP 758 syntax is valid here: `except A, B:` is intentional; use parentheses only when binding with `as`.

## Tests And Edit Traps

- Patch controller factories at the module-local import used by the code under test (`config_flow.create_controller`, `coordinator.create_controller`, `config_flow.create_controller_with_resolved_site`, etc.); patching `helpers.create_controller` will miss those call sites.
- Config-flow tests need `enable_custom_integrations`; many flow tests also use `_bypass_setup` so entry creation does not run real integration setup.
- Controller mocks need async `login`/`clients.update`/`clients_all.update`; flow tests also need `sites.update`/`sites.values`, and WebSocket tests need `messages.subscribe = MagicMock(return_value=MagicMock())` plus `connectivity = MagicMock()`.
- UI text or flow-error changes must keep `custom_components/unifi_presence/strings.json` and `custom_components/unifi_presence/translations/en.json` aligned; tests assert high-churn keys and stale removals.
- Keep metadata in sync: `manifest.json` and `pyproject.toml` must agree on version, `aiounifi` is pinned in both, and `manifest.json` `quality_scale` is checked against `custom_components/unifi_presence/quality_scale.yaml` and `tests/test_project_metadata.py`.
