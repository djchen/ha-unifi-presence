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
- **Options flow**: Adjust tracked devices and settings after setup
- **Reconfigure flow**: Change controller credentials without removing the integration
- **Diagnostics**: Built-in diagnostics for troubleshooting

## Requirements

- Home Assistant 2026.3.0 or later
- UniFi Network Controller (UniFi OS or legacy)
- Local user account with read access to clients

## Installation

### HACS (Recommended)

1. Open HACS → **Integrations**
2. Click **⋮** (top right) → **Custom repositories**
3. Add repository: `https://github.com/djchen/ha-unifi-presence`
4. Category: **Integration**
5. Click **Add**, then search for **UniFi Presence**
6. Click **Download** and restart Home Assistant

### Manual

1. Copy the `custom_components/unifi_presence` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

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

## Entities

Each tracked device creates a `device_tracker` entity:

- **Entity ID**: `device_tracker.<device_name>` (or `device_tracker.<mac_address>` if no name is available)
- **State**: `home` or `not_home`
- **Attributes**:
  - `source_type`: Always `router`
  - `ip_address`: Current IP address (when connected)
  - `mac_address`: Device MAC address
  - `hostname`: Device hostname (when available)
  - `is_wired`: `true` for ethernet, `false` for wireless
  - `last_seen`: Unix timestamp of last activity

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
pip install -e .
pip install pytest pytest-asyncio pytest-homeassistant-custom-component ruff
```

### Testing

```bash
source .venv/bin/activate
pytest tests/ -v
```

### Linting & Formatting

```bash
source .venv/bin/activate
ruff check .          # Check for issues
ruff format .         # Format code
```

## License

Apache License 2.0 — see [LICENSE.md](LICENSE.md).
