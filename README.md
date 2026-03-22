# UniFi Presence

A Home Assistant custom integration for presence detection using UniFi network controllers. Track selected devices with real-time WebSocket updates, configurable away thresholds, and fallback polling.

## Why not the official integration?

The official [UniFi Network integration](https://www.home-assistant.io/integrations/unifi/) is comprehensive but creates entities for all devices and network equipment. This integration focuses solely on presence detection and lets you select specific devices to track.

## Features

- **Real-time updates**: WebSocket connection for instant presence detection
- **Device selection**: Choose which devices to track from auto-discovered clients
- **Configurable away threshold**: Set how long before marking a device as away (default: 60s)
- **Fallback polling**: REST polling (default: 300s) catches missed WebSocket events
- **UI-only configuration**: No YAML required
- **Options flow**: Add or remove tracked devices after setup. Adjust away threshold and polling interval.
- **Reconfigure flow**: Change controller settings without removing the integration
- **Diagnostics**: Built-in diagnostics for troubleshooting

## Requirements

- Home Assistant 2026.3.0 or later
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
   - **Site**: Site name (default: `default`)
   - **Verify SSL certificate**: Enable SSL verification (default: disabled)
4. Select devices to track from the discovered client list
5. Click **Submit**

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
4. Update host, port, username, password, site, or SSL verification settings
5. Click **Submit** to save and reload

## Removal
<details>
<summary>Click to show removal instructions</summary>

1. Go to **Settings** → **Devices & Services**
2. Click on the **UniFi Presence** integration card
3. Click **⋮** → **Delete**
4. Confirm deletion — all entities and devices created by this integration will be removed

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
| **Fallback polling** | REST polling catches events missed by WebSocket |
| **Device selection** | Choose specific devices to track during setup or in options |
| **Away threshold** | Configure how long before a device is marked away |
| **Reauthentication** | Update credentials when they expire without removing the integration |
| **Reconfiguration** | Change controller host/port/site/SSL without re-adding |
| **Diagnostics** | Download redacted diagnostics data for troubleshooting |

## How Data is Updated

This integration uses a **push-primary, poll-fallback** strategy:

1. **WebSocket (primary)**: A persistent WebSocket connection to the UniFi controller receives real-time `sta:sync` events whenever a client's state changes. This provides near-instant presence updates.
2. **REST polling (fallback)**: A configurable REST poll (default: every 300 seconds) fetches all tracked clients to catch any events that may have been missed during WebSocket disconnections.
3. **Away detection**: A device is marked `not_home` when `current_time - last_seen > away_seconds` (default: 60 seconds).

If the WebSocket disconnects, the integration automatically reconnects with backoff. During disconnection, the fallback poll ensures presence state remains current.

## Entities

Each tracked device creates a `device_tracker` entity and a matching device-registry entry:

- **Entity ID**: `device_tracker.<device_name_slug>`
- **Friendly name**: Inherits the device name (e.g., `Dan's iPhone`)
- **Device entry**: One per tracked client, keyed by MAC (identifiers `(unifi_presence, <mac>)` and connection `(network_mac, <mac>)`); defaults to manufacturer `Ubiquiti Networks`
- **State**: `home` or `not_home`
- **Attributes**:
  - `source_type`: Always `router`
  - `ip_address`: Current IP address (when connected)
  - `mac_address`: Device MAC address
  - `hostname`: Device hostname (when available)
  - `is_wired`: `true` for ethernet, `false` for wireless
  - `last_seen`: Unix timestamp of last activity

## Reauthentication

If the UniFi controller rejects the stored credentials (e.g., after a password change), the integration will show a **Reconfigure** notification:

1. Click the notification or go to **Settings** → **Devices & Services**
2. Click **Reconfigure** on the UniFi Presence card
3. Enter updated username and password
4. Click **Submit** — the integration reloads automatically

## Use Cases & Automation Examples

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
2. Select a person and add the `device_tracker.my_phone_presence` entity
3. HA will combine GPS and network presence for a more accurate result

## Known Limitations

- **No GPS tracking**: This integration uses network presence only — it cannot determine geographic location or zones.
- **Away detection delay**: Devices are marked away only after the configured `away_seconds` threshold elapses since the last activity seen by the controller. Some devices sleep aggressively and may appear away prematurely.
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
| **Device stuck as "home" or "away"** | Lower the away threshold in options, or check the device's `last_seen` attribute to see if the controller is reporting activity. |
| **WebSocket disconnecting frequently** | Check network stability between HA and the controller. Download diagnostics to confirm WebSocket status. |
| **Entities become unavailable** | The controller is unreachable. Check network connectivity and controller status. The integration will automatically reconnect. |

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

## Development

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

### Testing

```bash
source .venv/bin/activate
PYTHONPATH=. pytest tests/ -v
```

Coverage is enforced at 95% minimum and runs automatically with pytest.

### Linting & Formatting

```bash
source .venv/bin/activate
ruff check .          # Check for issues
ruff format .         # Format code
```

### Type Checking

```bash
source .venv/bin/activate
mypy custom_components/
```

## License

Apache License 2.0 — see [LICENSE.md](LICENSE.md).
