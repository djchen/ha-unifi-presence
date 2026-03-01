# UniFi Presence

UniFi Presence is a Home Assistant device-tracker integration that logs into a UniFi controller, lets you pick which client devices to follow from a discovery list, and keeps their home/not_home state updated in real time via WebSocket with a configurable away threshold, fallback REST polling, and full UI flows for setup, options, reconfiguration, and diagnostics.

## Why not the official integration?

The official [UniFi Network integration](https://www.home-assistant.io/integrations/unifi/) is powerful but can be overkill if you only need presence detection. It creates entities for every device ever seen on your network while this integration allows you to choose which devices to track.

## Features

- **Real-time updates**: WebSocket connection to the UniFi controller for instant presence detection
- **UI-configured**: Set up entirely from the Home Assistant UI (no YAML)
- **Auto-discovery**: Discovers all known clients from your UniFi controller for easy selection
- **Fallback polling**: Configurable REST poll interval (default 5 min) catches any missed WebSocket events
- **Configurable away threshold**: Set how many seconds a device is disconnected for before it is marked as away
- **Options flow**: Adjust settings and tracked devices after setup without reconfiguring
- **Reconfigure flow**: Change controller host/port/site/username/password/SSL verify without removing the integration

## Requirements

- Home Assistant 2026.3+
- UniFi Controller (UniFi OS or legacy)
- A local user account on the UniFi controller

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the **⋮** menu (top right) → **Custom repositories**
3. Enter `https://github.com/djchen/ha-unifi-presence` as the repository and select **Integration** as the category
4. Click **Add**
5. Search for **UniFi Presence** in HACS → **Integrations**
6. Click **Download**
7. Restart Home Assistant

### Manual

1. Copy the `custom_components/unifi_presence` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **+ Add Integration**
2. Search for **UniFi Presence**
3. Enter your UniFi controller credentials:
   - **Host**: IP address or hostname of your UniFi controller
   - **Port**: 443 (UniFi OS) or 8443 (legacy controller)
   - **Username**: Local UniFi user
   - **Password**: Password
   - **Site**: Site name (default: `default`)
   - **Verify SSL certificate**: Whether to verify the SSL certificate (default: off)
4. Select devices to track from the discovered client list
5. Click **Submit**

### Options

After setup, click **Configure** on the integration to adjust:

- **Tracked devices**: Add or remove devices
- **Away threshold**: Seconds before marking a device as away (default: 60)
- **Fallback poll interval**: How often to poll as a fallback when WebSocket is active (default: 300s, min: 60s)

### Reconfigure

To change your UniFi controller connection settings without removing the integration:

1. Go to **Settings** → **Devices & Services**
2. Click the **⋮** menu on the UniFi Presence integration
3. Select **Reconfigure**
4. Enter any new controller host, port, username, password, site, or SSL verify values and click **Submit**

## Entities

Each tracked device creates a `device_tracker` entity:

- **Entity ID**: `device_tracker.unifi_presence_<mac>`
- **State**: `home` or `not_home`
- **Attributes**:
  - `source_type`: `router`
  - `ip_address`: Client IP (when available)
  - `mac_address`: Client MAC address
  - `is_wired`: Whether the client is connected via ethernet
  - `last_seen`: Unix timestamp of when the client was last seen

## Development

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install pytest pytest-asyncio pytest-homeassistant-custom-component aiounifi ruff
```

### Testing

```bash
source .venv/bin/activate
pytest tests/ -v
```

### Linting

```bash
source .venv/bin/activate
ruff check .
ruff format .
```

## License

Apache License 2.0 — see [LICENSE.md](LICENSE.md).
