# Z2M Device Tracker

Open **Zigbee Tracker** in the sidebar. Most settings can be changed on its **Settings** tab; the options below are the same settings.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `base_topics` | `zigbee2mqtt` | MQTT base topic of each Zigbee2MQTT instance to track. |
| `z2m_panels` | empty | Links to each Zigbee2MQTT app, as `base_topic=app_slug`, e.g. `zigbee2mqtt=45df7312_zigbee2mqtt`. The slug is in the app's URL. |
| `alerts_enabled` | `true` | Master switch for alerts. |
| `persistent_notifications` | `true` | Show alerts as Home Assistant notifications. |
| `fire_events` | `true` | Fire `z2m_tracker_alert` events for automations. |
| `notify_service` | empty | Also send alerts to a notify service, e.g. `notify.mobile_app_phone`. |
| `alert_on_join` | `true` | Alert when a new device joins. |
| `disabled_checks` | empty | Health checks to turn off completely. |
| `notify_checks` | most checks | Health checks that send alerts. |
| `offline_alert_minutes` | `60` | How long a device must be offline before an alert. |
| `frozen_alert_hours` | `24` | How long a sensor reading must stay the same before it counts as frozen. |
| `battery_alert_percent` | `15` | Battery level for a low battery alert. |
| `networkmap_hour` | `3` | Hour of the nightly routing scan, or `-1` to turn it off. |
| `ha_entities` | `true` | Add tracker sensors to each Zigbee2MQTT device via MQTT discovery. |
| `weekly_summary` | `true` | Send a weekly summary notification. |
| `summary_day` / `summary_hour` | `0` / `9` | When to send it (0 = Monday). |
| `wifi_channels` | empty | Your 2.4 GHz Wi-Fi channels, e.g. `1, 6`, used for the radio channel check. |

## Data

History is stored in the app's own data folder and is included in Home Assistant backups. Use **Settings → Backup** in the app to download or restore it.
