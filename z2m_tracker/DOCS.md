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
| `critical_offline_minutes` | `5` | The same for devices marked **Critical device** on their Info tab. |
| `frozen_alert_hours` | `24` | How long a sensor reading must stay the same before it counts as frozen. |
| `battery_alert_percent` | `15` | Battery level for a low battery alert. |
| `networkmap_hour` | `3` | Hour of the nightly routing scan, or `-1` to turn it off. |
| `ha_entities` | `true` | Add tracker sensors to each Zigbee2MQTT device via MQTT discovery. |
| `weekly_summary` | `true` | Send a weekly summary notification. |
| `summary_day` / `summary_hour` | `0` / `9` | When to send it (0 = Monday). |
| `wifi_channels` | empty | Your 2.4 GHz Wi-Fi channels, e.g. `1, 6`, used for the radio channel check. |
| `chatty_per_hour` | `1200` | Messages an hour (averaged over 6 hours) above which a device is flagged as chatty. |
| `ota_window` | empty | Time window for automatic firmware updates, e.g. `02:00-05:00` (local time, may cross midnight). Empty turns automatic updates off. A failed update is retried once, the next night. |
| `ota_min_battery` | `30` | Battery devices below this level are skipped by automatic and bulk updates. **Update anyway** overrides it for one device. |
| `quiet_hours` | `false` | Hold alerts during quiet hours and send one summary when they end. |
| `quiet_start` / `quiet_end` | `23:00` / `07:00` | Quiet hours in the app's time zone, as `HH:MM`. The window can cross midnight. |

## More checks

These checks show in Health but don't notify until you set them to **Show and notify** in Settings → Checks.

| Check | Flags |
| --- | --- |
| Flapping | A device that goes offline 4+ times or leaves the network twice in 24 hours, even when each drop is too short for the Offline alert. |
| Chatty device | Message rate above `chatty_per_hour`, or 5 times its own usual rate (and at least 360 an hour). |
| Battery draining fast | Battery fell at least 3 times faster over the last 14 days than its rate since the last battery change (needs about 5 weeks of history). |
| Binding lost | A binding (to a device, a group, or the coordinator for reporting) that disappeared and didn't come back within 10 minutes. Bindings removed with Zigbee2MQTT's Unbind are logged but not flagged. |
| Single point of failure | A router that, in the latest routing scan, is the only path to the coordinator for other routers. Also listed in the Map tab's advice. |
| Coordinator backup | `coordinator_backup.json` missing, without a network key, not matching the running channel/PAN ID, or more than 3 days old. Only checked when the Zigbee2MQTT data folder is inside the HA config folder. |

The device panel's Info tab also shows message rates, Zigbee2MQTT's own counters (messages, leaves, address changes) and the device's bindings.

The integration can mirror warnings and notify-enabled checks into **Settings → Repairs**. Turn it off in the integration's options.

## Critical devices

Turn on **Critical device** on a device's Info tab for devices that must never fail quietly: smoke and leak sensors, the freezer plug, the heating. A critical device:

- gets an Offline alert after `critical_offline_minutes` (default 5) instead of `offline_alert_minutes`;
- shows a red **Critical** tag in its panel and on the Health tab, and has its own filter in the device list;
- sends its alerts with the title starting **Critical:**, and with `critical: true` in the `z2m_tracker_alert` event;
- alerts straight away during quiet hours.

Alerts to `notify_service` for a critical device also carry the Companion app's high-priority data: `priority: high` and `ttl: 0` (Android, delivered straight away), and `push: interruption-level: time-sensitive` (iOS, shown through Focus modes).

## Quiet hours

With `quiet_hours` on, new issue alerts and Device joined, Device left and Interview failed alerts are held between `quiet_start` and `quiet_end` instead of being sent. Alerts for critical devices still go out straight away.

When quiet hours end, one notification sums up the night, titled "Zigbee overnight: N issues, M resolved". It lists the issues that are still open, the ones that appeared and cleared during the night (with their times), and the held join, leave and interview alerts. Issues listed as still open count as alerted, so they aren't sent again one by one. If nothing happened, no summary is sent. Held alerts are kept in the app's data, so they survive a restart.

## Firmware updates

The **Updates** tab lists every device with a firmware update from Zigbee2MQTT, with the installed and available versions, power and progress. Versions are the numbers Zigbee2MQTT reports; the build name is shown when the device has one.

- **Check now** asks Zigbee2MQTT to check every device that supports updates, one at a time. Sleeping battery devices often don't answer.
- **Update now** and **Update all** add devices to a queue. The queue installs one device at a time.
- With `ota_window` set, updates still waiting are installed inside that window each night, one at a time. A device that fails is tried once more the next night. If that fails too, it stays failed until you press **Retry**. Retry starts the count again.
- Each update is recorded under **Recent updates** (last 200) and in the device's logbook in Home Assistant.

## Data

History is stored in the app's own data folder and is included in Home Assistant backups. Use **Settings → Backup** in the app to download or restore it.
