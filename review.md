# Review

## Scope

Reviewed every project file under `custom_components/`, `tests/`, repo docs, and CI/tooling. Traced all real user flows present in this repo: initial setup, device discovery, options, reconfigure, reauth, steady-state polling/websocket updates, diagnostics, unload, and device-removal handling. There is no automation or AI-chat code in this repository to review.

Validated the current baseline with:

- `ruff check .`
- `mypy custom_components/`
- `PYTHONPATH=. pytest tests/ -v`

All three passed locally. `pytest` reported `133 passed` with `98.37%` coverage. I also ran a second challenge pass over the findings and adjusted severity where the original framing was too strong.

## Findings

### High: selected tracker entities do not actually create device entries and can be disabled by default on clean installs

- `UnifiPresenceTracker` inherits `ScannerEntity` and sets `_attr_device_info` in `custom_components/unifi_presence/device_tracker.py:31-58`, but Home Assistant's config-entry `ScannerEntity` does not use that to expose `device_info`.
- The surrounding code assumes the opposite:
  - `custom_components/unifi_presence/__init__.py:39-47` performs stale device cleanup as if this integration owns per-client device-registry entries.
  - `custom_components/unifi_presence/__init__.py:72-87` blocks device removal as if those device entries are guaranteed to exist and belong to this integration.
  - `README.md:126-134` documents per-client device entries with custom identifiers and connections.
- The tests mask the problem instead of catching it:
  - `tests/test_device_tracker.py:116-141` asserts the private `_attr_device_info` field instead of the public HA behavior.
  - `tests/test_init.py:304-319` pre-registers entity-registry entries "so the test environment doesn't disable them", which works around the exact clean-install behavior the integration should be proving.
- Impact: a user can complete setup, select devices, and still not get enabled tracker entities or per-client device entries unless matching MAC-backed device entries already exist from somewhere else.
- Recommendation: either stop modeling this as a `ScannerEntity` with custom device-registry ownership, or redesign around the real `ScannerEntity` constraints and remove the stale-device / custom-device assumptions.

### Medium: websocket state can drift away from the active controller after coordinator reauth

- `custom_components/unifi_presence/coordinator.py:206-224` can discard and replace `self._controller` during a fallback-poll reauth.
- The websocket runner captures one controller when it starts in `custom_components/unifi_presence/websocket.py:134-150`.
- Later websocket subscriptions and health checks ask `self._get_api()` for the current controller in `custom_components/unifi_presence/websocket.py:76-84` and `custom_components/unifi_presence/websocket.py:250-273`.
- After a poll-triggered controller replacement, the running websocket/subscription can still belong to the old controller while health checks inspect the new one. That is an architectural lifecycle split and an obvious source of timing-dependent missed realtime updates.
- I would treat this as a real design flaw and missing test scenario, but not as a proven production outage without a reproducer.
- Recommendation: keep websocket state bound to one controller object, or explicitly restart/resubscribe the websocket whenever the coordinator swaps controllers.

### Medium: setup/options treat historical client fetching as mandatory even when active clients are enough

- `_fetch_all_clients()` in `custom_components/unifi_presence/config_flow.py:43-65` hard-fails if `controller.clients_all.update()` raises.
- The same helper explicitly tolerates `controller.clients.update()` failing in `custom_components/unifi_presence/config_flow.py:54-57`.
- Result: setup/options can abort with `cannot_discover_devices` even when the controller can still provide a usable active-client list.
- This is a real setup-flow robustness issue on controllers where the historical-clients endpoint is flaky, temporarily unavailable, or permission-limited.
- Recommendation: make both sources best-effort, merge whichever datasets are available, and only fail when neither source returns usable clients.

### Low: websocket reachability signal plumbing is dead code

- `custom_components/unifi_presence/coordinator.py:95-99` defines `signal_reachable`.
- `custom_components/unifi_presence/websocket.py:199-205` sends that signal on availability changes.
- Nothing in this repository subscribes to it.
- Recommendation: remove the dispatcher signal entirely, or finish the feature and consume it somewhere meaningful.

### Low: `UnifiPresenceConfigFlow._controller` is unused state

- `custom_components/unifi_presence/config_flow.py:81` stores `self._controller`.
- It is assigned at `custom_components/unifi_presence/config_flow.py:146` and never read afterward.
- Recommendation: delete it and simplify the flow state.

## Test gaps

- Add one full config-entry setup test that does not pre-seed the entity registry and proves that a selected tracker entity is actually created and enabled on a clean install.
- Replace the private `_attr_device_info` assertions in `tests/test_device_tracker.py:116-141` with assertions against the public HA behavior that matters: `device_info`, enabled-by-default behavior, and device attachment behavior.
- Add a lifecycle test that forces coordinator reauth while the websocket task is running and verifies restart/resubscription behavior after the controller object changes.
- Add a config-flow test for `clients_all.update()` failure when `clients.update()` still returns devices.

## Tests that can be trimmed or consolidated

- `tests/test_device_tracker.py:79-155` spends a lot of space checking constants and private implementation details (`source_type`, `_attr_name`, `_attr_translation_key`, `PARALLEL_UPDATES`) while the suite misses the higher-value HA behavior above.
- The host-variant parametrizations in `tests/test_config_flow.py:236-271` and `tests/test_config_flow.py:406-440` are reasonable, but they are mostly proving string passthrough rather than integration logic. If the suite needs to get leaner, those are easier to trim than the missing end-to-end behavior tests.

## Overall

- Mechanically, the repo is in good shape: lint, type checking, and the current test suite all pass.
- The main problem is conceptual, not stylistic: the device-tracker implementation is built around assumptions that do not hold for Home Assistant's `ScannerEntity`, and the current docs/tests reinforce those assumptions instead of catching them.
