# UniFi Presence

A Home Assistant custom integration for presence detection using UniFi network controllers. Track selected devices with real-time WebSocket updates, configurable away thresholds, and fallback polling.

## Why not the official integration?

The official [UniFi Network integration](https://www.home-assistant.io/integrations/unifi/) is broader and imports both UniFi infrastructure devices and network clients into Home Assistant. This integration is focused only on presence detection and includes an explicit per-device selection step, so you can track just the clients you care about.

This integration intentionally does not implement discovery. UniFi hardware discovery would overlap with the official UniFi integration and can create duplicate discovery prompts for the same controller, so setup stays manual by design.

## Features

- **Real-time updates**: WebSocket connection for instant presence detection
- **Device selection**: Choose which devices to track from auto-discovered clients
- **Configurable away threshold**: Set how long before marking a device as away (default: 60s)
- **Fallback polling**: REST polling (default: 300s) catches missed WebSocket events and refreshes offline metadata
- **UI-only configuration**: No YAML required
- **Options flow**: Add or remove tracked devices after setup. Adjust away threshold and polling interval.
- **Reconfigure flow**: Change controller settings without removing the integration
- **Diagnostics**: Built-in diagnostics for troubleshooting
- **System health**: Built-in system health summary for controller and WebSocket status

## Requirements

- Home Assistant 2026.3.0 or later
- Python 3.14.3 or later
- UniFi Network Controller (UniFi OS or legacy)
- Local UniFi user account with read access to clients

## Installation

### HACS (Recommended)

1. Open HACS
2. Click **⋮** (top right) → **Custom repositories**
3. Add Repository: `https://github.com/djchen/ha-unifi-presence`
4. Select Type: **Integration**
5. Click **Add**, then search for **UniFi Presence**
6. Click **Download** and restart Home Assistant

### Manual

<details>
<summary>Manual installation steps</summary>

1. Download the latest release zip file
2. Extract `custom_components/unifi_presence` to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

</details>

## Configuration

1. Go to **Settings** → **Devices & Services** → **+ Add Integration**
2. Search for **UniFi Presence**
3. Enter your UniFi controller credentials:
   - **Host**: IP address or hostname
   - **Port**: Default is 443 (use 8443 for legacy controllers)
   - **Username**: Local UniFi username
   - **Password**: Password for the account
   - **Verify SSL certificate**: Enable SSL verification (default: disabled)
4. If multiple UniFi sites are accessible, select the site in the second step. If only one site is available, the integration skips directly to device selection.
5. Select devices to track from the discovered client list
6. Click **Submit**

### Options

After setup, click **Configure** on the integration card to adjust:

- **Tracked devices**: Add or remove devices from the list
- **Away threshold**: Seconds before marking a device as away (default: 60, min: 1)
- **Fallback poll interval**: REST polling interval in seconds (default: 300, min: 60)

### Reconfigure

Change controller connection settings without removing the integration:

1. Go to **Settings** → **Devices & Services**
2. Click **⋮** on the UniFi Presence integration card
3. Select **Reconfigure**
4. Update host, port, username, password, or SSL verification settings for the existing site
5. If multiple UniFi sites are accessible, confirm the existing site in the second step
6. Click **Submit** to save and reload

## Removal
<details>
<summary>Click to show removal instructions</summary>

1. Go to **Settings** → **Devices & Services**
2. Click on the **UniFi Presence** integration card
3. Click **⋮** → **Delete**
4. Confirm deletion — all entities created by this integration will be removed

</details>

## Supported Devices

Any client device (wireless or wired) that has connected to your UniFi network and appears in the controller's client list:

- Phones, tablets, laptops, desktops
- IoT devices (smart speakers, cameras, etc.)
- Any device with a MAC address tracked by the UniFi controller

> **Note**: Access points, switches, and other UniFi infrastructure devices are **not** tracked — only client devices.

## Supported Functions

| Function | Description |
|---|---|
| **Presence detection** | Tracks whether selected devices are home or away |
| **Real-time updates** | WebSocket connection delivers instant state changes |
| **Fallback polling** | REST polling catches events missed by WebSocket and refreshes offline metadata |
| **Device selection** | Choose specific devices to track during setup or in options |
| **Away threshold** | Configure how long before a device is marked away |
| **Reauthentication** | Update credentials when they expire without removing the integration |
| **Reconfiguration** | Change controller host/port/credentials/SSL for the existing site without re-adding; if multiple sites are accessible, confirm the existing site in a second step |
| **Diagnostics** | Download redacted diagnostics data for troubleshooting |
| **System health** | View controller, coordinator, and WebSocket summary information |

## How Data is Updated

This integration uses a **push-primary, poll-fallback** strategy:

1. **WebSocket (primary)**: A persistent WebSocket connection to the UniFi controller receives real-time `sta:sync` events whenever a client's state changes. This provides near-instant presence updates.
2. **REST polling (fallback)**: A configurable REST poll (default: every 300 seconds) fetches all tracked clients to catch any events that may have been missed during WebSocket disconnections.
3. **Away detection**: A device is marked `not_home` when `current_time - last_seen >= away_seconds` (default: 60 seconds). The coordinator maintains a heartbeat-style expiry timer for tracked clients, so away transitions continue to happen even if no second WebSocket message arrives for that client.

If the WebSocket disconnects, the integration automatically reconnects with backoff. During disconnection, the fallback poll still ensures presence state remains current and refreshes the latest known client activity.

### Offline vs. Unavailable

- **Offline (not_home)**: A tracked client that is no longer in the controller's active client list stays `home` until its configured away heartbeat expires. Once it expires, the client is marked `not_home`. The integration resolves metadata (display name) from the controller's historical client store (`clients_all`) when it has a usable name/hostname, falling back to the last-known name or the raw MAC address.
- **Unavailable**: Indicates a coordinator or controller health problem (e.g., the controller is unreachable, or authentication failed). All tracked entities become `unavailable` during these conditions and recover automatically once connectivity is restored.

## Entities

Each tracked device creates a `device_tracker` entity:

- **Entity ID**: `device_tracker.<device_name_slug>` (derived from the UniFi client name)
- **Friendly name**: The device name as reported by the UniFi controller (e.g., `Dan's iPhone`)
- **Unique ID**: The UniFi site ID and device MAC address
- **State**: `home` or `not_home`
- **Attributes**:
  - `source_type`: Always `router`
  - `mac`: Device MAC address

> **Note:** This integration follows the official HA `ScannerEntity` pattern and does not create per-client device-registry entries. Tracker entities appear in the entity registry only.

## Reauthentication

If the UniFi controller rejects the stored credentials (e.g., after a password change), the integration will show a **Reauth** notification:

1. Click the notification or go to **Settings** → **Devices & Services**
2. Click **Reauthenticate** on the UniFi Presence card
3. Enter updated username and password
4. Click **Submit** — the integration reloads automatically

## Use Cases & Automation Examples

> **Note:** The entity IDs below (e.g., `device_tracker.my_phone`) are examples. Your actual entity IDs are derived from the UniFi client names and may differ. Check **Settings → Devices & Services → Entities** for the exact IDs.

### Arrive home — turn on lights

```yaml
automation:
  - alias: "Turn on lights when I arrive"
    trigger:
      - platform: state
        entity_id: device_tracker.my_phone
        to: "home"
    action:
      - service: light.turn_on
        target:
          area_id: living_room
```

### Leave home — lock doors

```yaml
automation:
  - alias: "Lock doors when everyone leaves"
    trigger:
      - platform: state
        entity_id:
          - device_tracker.alice_phone
          - device_tracker.bob_phone
    condition:
      - condition: state
        entity_id: device_tracker.alice_phone
        state: "not_home"
      - condition: state
        entity_id: device_tracker.bob_phone
        state: "not_home"
    action:
      - service: lock.lock
        target:
          entity_id: lock.front_door
```

### Use in a Person entity

Assign the device tracker to a [Person](https://www.home-assistant.io/integrations/person/) for zone-aware presence:

1. Go to **Settings** → **People**
2. Select a person and add the `device_tracker.my_phone` entity
3. HA will combine GPS and network presence for a more accurate result

## Known Limitations

- **Away detection delay**: Devices are marked away only after the configured `away_seconds` threshold elapses since the last activity seen by the controller. A coordinator heartbeat monitor enforces that expiry continuously, but some devices sleep aggressively and may still appear away prematurely.
- **Clock skew**: Presence detection compares the Home Assistant server's clock with the controller's `last_seen` timestamps. If the two clocks diverge significantly, devices may be incorrectly marked home or away.
- **Single controller**: Each integration instance connects to one UniFi controller and site. Add multiple instances for multiple controllers.
- **Client visibility**: Only devices that have previously connected to the UniFi network appear in the client list. New devices must connect at least once before they can be tracked.
- **UniFi OS / legacy differences**: Port defaults differ (443 for UniFi OS, 8443 for legacy). Ensure the correct port is configured.
- **Self-signed SSL**: Most UniFi controllers use self-signed certificates. Keep "Verify SSL certificate" disabled unless you have installed a trusted certificate.

## Troubleshooting

| Problem | Solution |
|---|---|
| **"Unable to connect"** during setup | Verify the host, port, and that the controller is reachable from your HA instance. Try port 8443 for legacy controllers. |
| **"Invalid username or password"** | Ensure you are using a **local** UniFi account, not a Ubiquiti cloud (SSO) account. |
| **No devices discovered** | The controller returned no clients. Ensure devices have connected to this controller and site at least once. |
| **"Could not fetch clients"** during setup or options | Connected to the controller, but client discovery failed for this site. Verify the user account has read access and the site name is correct. |
| **Device stuck as "home" or "away"** | Lower the away threshold in options and confirm the controller is still reporting the device in the UniFi client list. |
| **WebSocket disconnecting frequently** | Check network stability between HA and the controller. Download diagnostics to confirm WebSocket status. |
| **Entities become unavailable** | The coordinator cannot reach the controller. Check network connectivity and controller status. The integration will automatically reconnect. Individual offline clients show `not_home`, not `unavailable`. |

For persistent issues, [download diagnostics](#diagnostics) and open an issue on [GitHub](https://github.com/djchen/ha-unifi-presence/issues).

## Diagnostics

The integration provides diagnostics data for troubleshooting:

1. Go to **Settings** → **Devices & Services**
2. Click on the UniFi Presence integration
3. Click **Download Diagnostics**

Diagnostics include:
- Redacted configuration (credentials masked)
- Tracked device count and states
- Away threshold and poll interval settings
- WebSocket connection status
- Heartbeat expiry count for tracked clients still within the away window

## System Health

The integration also adds a system health summary to Home Assistant. It reports:

- Number of configured and loaded UniFi Presence entries
- Number of entries with successful coordinator updates
- Number of active WebSocket connections
- Number of active heartbeat expiries currently being tracked
- Total tracked device count
- Configured controller host and site targets

## Development

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install ".[dev]"
pre-commit install
```

### Testing

```bash
source .venv/bin/activate
PYTHONPATH=. pytest tests/ -v
```

Coverage is enforced at 98% minimum and runs automatically with pytest.

### Linting & Formatting

```bash
source .venv/bin/activate
ruff check .
ruff format --check .
```

### Type Checking

```bash
source .venv/bin/activate
mypy --strict custom_components/unifi_presence/
```

## License

Apache License 2.0 — see [LICENSE.md](LICENSE.md).
