# UniFi Presence

UniFi Presence is a Home Assistant custom integration for tracking people and devices with a UniFi Network controller.

It keeps the setup focused on one thing: choose the clients you care about, then let Home Assistant update their presence with real-time WebSocket events and fallback polling.

## Why not the official integration?

The official [UniFi Network integration](https://www.home-assistant.io/integrations/unifi/) is the better choice if you want the full UniFi feature set in Home Assistant, including infrastructure devices, client entities, controls, sensors, and SSDP discovery on supported UniFi OS consoles.

This project is the simpler "presence only" option. You pick the clients to track, and it creates only `device_tracker` entities for those devices.

Auto-discovery of the controller is intentionally not included. The official integration already handles discovery for supported UniFi OS consoles, and duplicating that here could lead to confusing duplicate setup prompts.

## Features

- Real-time WebSocket updates
- Choose which devices to track
- Adjustable away threshold
- REST polling fallback
- UI-only setup
- Options, reconfigure, and reauth flows
- Diagnostics and system health

## Requirements

- Home Assistant 2026.3.0 or later
- Python 3.14.3 or later
- A UniFi Network controller, either UniFi OS or legacy
- A local UniFi user account with permission to read clients

## Installation

### HACS (Recommended)

1. Open HACS.
2. Click **⋮** in the top right, then choose **Custom repositories**.
3. Add `https://github.com/djchen/ha-unifi-presence`.
4. Set the type to **Integration**.
5. Search for **UniFi Presence**, then download it.
6. Restart Home Assistant.

### Manual

<details>
<summary>Manual installation steps</summary>

1. Download the latest release zip file.
2. Copy `custom_components/unifi_presence` into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

</details>

## Configuration

1. Go to **Settings** → **Devices & Services** → **+ Add Integration**.
2. Search for **UniFi Presence**.
3. Enter your controller details:
   - **Host**: IP address or hostname
   - **Port**: 443 by default, or 8443 for legacy controllers
   - **Username**: A local UniFi username
   - **Password**: The account password
   - **Verify SSL certificate**: Leave this on unless your controller uses a self-signed or otherwise untrusted certificate
4. If the account can access more than one site, choose the site in the second step.
5. Select the devices you want to track.
6. Submit the form.

### Options

After setup, open **Configure** on the integration card to change:

- **Tracked devices**: Add or remove devices
- **Away threshold**: Seconds before a device is marked away (default: 60, minimum: 1)
- **Fallback poll interval**: REST polling interval in seconds (default: 300, minimum: 60)

### Reconfigure

Use reconfigure to change controller connection settings without removing the integration:

1. Go to **Settings** → **Devices & Services**.
2. Open the **⋮** menu on the UniFi Presence card.
3. Select **Reconfigure**.
4. Update the host, port, username, password, or SSL verification settings.
5. If multiple sites are available, confirm the current site.
6. Submit to save and reload.

## Removal
<details>
<summary>Click to show removal instructions</summary>

1. Go to **Settings** → **Devices & Services**.
2. Open the **UniFi Presence** integration card.
3. Click **⋮** → **Delete**.
4. Confirm the deletion. All entities created by the integration will be removed.

</details>

## Supported Devices

Any wired or wireless client that has connected to your UniFi network and appears in the controller's client list:

- Phones, tablets, laptops, and desktops
- IoT devices like smart speakers and cameras
- Any device with a MAC address known to the controller

> **Note**: Access points, switches, and other UniFi infrastructure devices are not tracked.

## Supported Functions

| Function | Description |
|---|---|
| **Presence detection** | Tracks whether selected devices are home or away |
| **Real-time updates** | Uses WebSocket events for fast state changes |
| **Fallback polling** | Refreshes state when WebSocket events are missed |
| **Device selection** | Choose the devices to track |
| **Away threshold** | Control how long inactivity lasts before away |
| **Reauthentication** | Update credentials without removing the integration |
| **Reconfiguration** | Change controller settings without starting over |
| **Diagnostics** | Download redacted troubleshooting data |
| **System health** | View controller and WebSocket status |

## How Data is Updated

This integration uses a **push-first, poll-fallback** strategy:

1. **WebSocket (primary)**: Receives `sta:sync` events whenever a client's state changes.
2. **REST polling (fallback)**: Checks tracked clients every 300 seconds by default.
3. **Away detection**: A device becomes `not_home` when `current_time - last_seen >= away_seconds`.

If the WebSocket disconnects, the integration reconnects automatically. Polling continues in the meantime.

### Offline vs. Unavailable

- **Offline (`not_home`)**: A tracked client stays `home` until its away timer expires, then becomes `not_home`.
- **Unavailable**: The coordinator or controller has a health problem. In that case, all tracked entities become `unavailable` and recover automatically when connectivity returns.

## Entities

Each tracked device creates a `device_tracker` entity:

- **Entity ID**: `device_tracker.<device_name_slug>`
- **Friendly name**: The UniFi client name, for example `Dan's iPhone`
- **Unique ID**: The site ID plus the device MAC address
- **State**: `home` or `not_home`
- **Attributes**:
  - `source_type`: Always `router`
  - `mac`: Device MAC address

> **Note:** This integration follows the official Home Assistant `ScannerEntity` pattern and does not create per-client device-registry entries. Tracker entities appear in the entity registry only.

## Reauthentication

If the controller rejects the saved credentials, Home Assistant will show a **Reauth** notification.

## Examples

> **Note:** The entity IDs below are examples. Check **Settings** → **Devices & Services** → **Entities** for your actual IDs.

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

1. Go to **Settings** → **People**.
2. Select a person and add the `device_tracker.my_phone` entity.
3. Home Assistant combines GPS and network presence for a more accurate result.

## Known Limitations

- **Away detection delay**: Devices only become away after the configured threshold.
- **Clock skew**: Large clock differences can cause incorrect state changes.
- **Single controller**: Each integration instance connects to one controller and one site.
- **Client visibility**: Only devices that have connected before will appear in the list.
- **UniFi OS vs. legacy**: Port defaults differ: 443 for UniFi OS and 8443 for legacy controllers.
- **Self-signed SSL**: Leave SSL verification disabled unless you have a trusted certificate.

## Troubleshooting

| Problem | Solution |
|---|---|
| **"Unable to connect"** during setup | Check the host, port, and network path. Try 8443 for legacy controllers. |
| **"Invalid username or password"** | Use a **local** UniFi account, not a cloud (SSO) account. |
| **No devices discovered** | The controller returned no clients. Make sure the devices have connected before. |
| **"Could not fetch clients"** during setup or options | Client discovery failed for this site. Verify read access and the site name. |
| **Device stuck as "home" or "away"** | Lower the away threshold and confirm the controller still sees the device. |
| **WebSocket disconnecting frequently** | Check the network between Home Assistant and the controller. |
| **Entities become unavailable** | The coordinator cannot reach the controller. The integration will reconnect automatically. |

For persistent issues, [download diagnostics](#diagnostics) and open an issue on [GitHub](https://github.com/djchen/ha-unifi-presence/issues).

## Diagnostics

The integration includes diagnostics data for troubleshooting:

1. Go to **Settings** → **Devices & Services**.
2. Open the UniFi Presence integration.
3. Click **Download Diagnostics**.

Diagnostics include:
- Redacted configuration with credentials masked
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
