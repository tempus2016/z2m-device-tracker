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
| `offline_alert_minutes` | `60` | Extra wait after Zigbee2MQTT marks a device offline, before an alert. Zigbee2MQTT marks a device offline only after its own availability timeout (10 min for mains devices, 25 h for battery devices by default, or per device in its `configuration.yaml`). With the defaults, a mains device alerts about 70 min after its last message and a battery device about 26 h after. |
| `critical_offline_minutes` | `5` | The same for devices marked **Critical device** on their Info tab. |
| `frozen_alert_hours` | `24` | How long a sensor reading must stay the same before it counts as frozen. A device can have its own value (Health tab, **Limits for this device**). |
| `battery_alert_percent` | `15` | Battery level for a low battery alert. A device can have its own value (Health tab, **Limits for this device**). |
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

## Expected reporting

Zigbee2MQTT decides when a device is offline: by default after 10 minutes for mains devices and 25 hours for battery devices. For a device that should report more often, set your own limit on its **Health** tab under **Expected reporting**, from 30 seconds to 7 days. If the device sends nothing for that long, the **Stopped reporting** check flags it, even while Zigbee2MQTT still shows it online. The tab shows how often the device usually reports; set the limit a little above that.

The check is off for every device until you set a limit, and it notifies by default. It is skipped while that device's Zigbee2MQTT is offline, so a network outage raises one alert, not one per device.

To set it for many devices at once, go to **Settings → Thresholds → Expected reporting** and select **Set for many devices…**. Pick mains devices, battery devices or both, and a multiplier (2×, 3×, 5× or 10×). Each device gets its usual gap between messages times the multiplier, rounded up to a tidy value such as 3 min, 30 min or 6 h. The usual gap comes from the last week's message counts, or the last 6 hours for a device with less history. Untick any device you want to leave alone, then select **Apply**. **Turn off** clears the setting on the ticked devices.

Some devices are left unticked:

- **Reports on change**: devices that only send when something happens, such as door sensors, buttons and plain switches. A device counts as this when it has no regular reading (temperature, humidity, pressure, power, energy, air quality, light level or soil moisture).
- **Not enough data**: devices with too few messages to tell how often they report.

## When a device alerts

`offline_alert_minutes` counts from the moment Zigbee2MQTT marks the device offline, not from its last message. So with the defaults, a mains device alerts about 70 minutes after it goes quiet (10 + 60), and a battery device about 26 hours later (25 h + 60 min). Each device's **Health** tab shows its own steps under **When this device alerts**, from Zigbee2MQTT's availability settings: its own timeout or the mains/battery default, the offline delay, and Expected reporting if set. If Zigbee2MQTT availability is off for the device, only **Not heard from** applies, after 2 days.

## Pairing left open

While pairing (permit join) is open, any nearby Zigbee device can join. The **Pairing left open** check warns and notifies when pairing stays open for 10 minutes, or straight away when something other than the Zigbee Tracker opened it (the Zigbee2MQTT frontend, Home Assistant or an automation). **Close now** on the Health tab closes it. Existing installs get this alert switched on once when they update; turn it off in Settings → Checks if you don't want it.

## More checks

These checks show in Health but don't notify until you set them to **Show and notify** in Settings → Checks.

| Check | Flags |
| --- | --- |
| Flapping | A device that goes offline 4+ times or leaves the network twice in 24 hours, even when each drop is too short for the Offline alert. |
| Chatty device | Message rate above `chatty_per_hour`, or 5 times its own usual rate (and at least 360 an hour). |
| Battery draining fast | Battery fell at least 3 times faster over the last 14 days than its rate since the last battery change (needs about 5 weeks of history). |
| Binding lost | A binding (to a device, a group, or the coordinator for reporting) that disappeared and didn't come back within 10 minutes. Bindings removed with Zigbee2MQTT's Unbind are logged but not flagged. |
| Single point of failure | A router that, in the latest routing scan, is the only path to the coordinator for other routers. Also listed in the Map tab's advice. |
| Changes router often | An end device whose parent router changed 3+ times across the routing scans of the last 7 days (scans it's missing from don't count). The device's History tab lists its parent per scan and the one with the best link. Routers aren't checked: their parent in the scan is only the router they joined through. |
| Router getting slower | A router whose nightly reply time (see Ping below) over the last 3 days is more than 3 times its median over the 14 days before, and above 150 ms. Often a failing plug or too many devices on one router. |
| Coordinator backup | `coordinator_backup.json` missing, without a network key, not matching the running channel/PAN ID, or more than 3 days old. Only checked when the Zigbee2MQTT data folder is inside the HA config folder. |
| Zigbee2MQTT memory | Zigbee2MQTT's own memory use grew more than 50% over the last 7 days (at least 5 days of readings). Usually a leak that ends in a crash; a restart clears it. Uses the `bridge/health` messages Zigbee2MQTT 2.5+ sends every 10 minutes by default (its `health.interval` setting). Health → Coordinators also shows its memory over the last 14 days, MQTT traffic and leaves this week. |
| Group members offline | A Zigbee2MQTT group with members that have been offline for longer than `offline_alert_minutes`. One issue per group, listing the offline members. |
| Daily signal dip | Average link quality across a network drops at least 25% below that day's median in the same 1 to 4 hours on at least 4 of the last 7 days, and at least 30% of its devices dip then too. Usually something that switches on at that time: busy Wi-Fi on an overlapping channel, a microwave, or a USB 3 device near the coordinator. Health → Radio channels shows the hourly chart once a network has a week of link quality from at least 3 devices. |

The Health tab's **Groups** card lists every Zigbee2MQTT group with how many of its members are online. Groups with offline members show each member, with the offline ones in red. A device that dropped out of a group but is still on the network (for example after it left and was re-paired) is flagged for 30 days, until it's back in the group, or until the group is deleted. Leaving a group is only noticed while the app is running.

## Router outages

When a router goes offline, the devices that route through it often follow. The Offline check groups them: a device whose parent router in the last routing scan that saw it is offline (or that router's parent, and so on) shows under that router in Health, and the router gets one issue and one notification, e.g. "Router offline: Landing plug - 6 devices that route through it are offline too". The devices don't notify on their own while the router is down. When the router comes back, devices still offline 5 minutes later become normal issues and notify as usual. Devices that were never in a routing scan, and routers whose Offline check is ignored, aren't grouped. Repairs in Home Assistant follow the same grouping; each device's own entities still show it offline.

The device panel's Info tab also shows message rates, Zigbee2MQTT's own counters (messages, leaves, address changes) and the device's bindings.

## Ping

Mains devices have a **Ping** button on the device panel's Info tab. It reads one attribute (the Basic cluster's ZCL version, the same read Zigbee2MQTT's availability check uses) and shows how long the reply took, or "No reply in 10 s". Nothing is written to the device. Battery devices sleep, so they have no Ping button.

Every night, the hour after the routing scan (04:00 when `networkmap_hour` is `-1`), the app pings each online router once, one at a time, 2 seconds apart. The Trends tab shows the last 14 nights as a bar chart, and the **Router getting slower** check uses them. 30 nights are kept.

Ping uses Zigbee2MQTT's `bridge/request/action` API, so it needs Zigbee2MQTT 2.7 or newer. The time includes the trip through MQTT and Zigbee2MQTT, a few milliseconds.

The integration can mirror warnings and notify-enabled checks into **Settings → Repairs**. Turn it off in the integration's options.

## Pause alerts

Use **⋮ → Pause alerts** before planned work, such as a Zigbee2MQTT upgrade, re-pairing or a power cut. Pick 30 min, 1 hour, 4 hours, until 08:00 tomorrow (the app's time zone), or until you resume. While alerts are paused:

- Health keeps checking and lists issues as normal. A banner on every tab shows when the pause ends and how many new issues are waiting.
- No issue alerts are sent. When the pause ends, or you press **Resume now**, issues that are still there are sent once. Issues that came and went during the pause are never sent.
- Joined, left and interview failed alerts are not sent, and not sent later either. They still show in Activity.
- Notifications already in Home Assistant stay until their issue clears after the pause.
- The weekly summary and **Send test notification** still go out.

The pause survives an app restart. The integration has the actions `z2m_tracker.pause_alerts` (optional `minutes`, empty pauses until resumed) and `z2m_tracker.resume_alerts`. Changing an alert setting during a pause marks the issues already found as sent, the same as it does outside a pause.

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

## Leftover devices in Home Assistant

A device removed from Zigbee2MQTT can stay in Home Assistant with all its entities unavailable, for example when Zigbee2MQTT was stopped or its Home Assistant integration was off at the time. Health lists these under **Leftover in Home Assistant**.

A Home Assistant device is listed when all of these are true:

- It came from Zigbee2MQTT's MQTT discovery (identifier `zigbee2mqtt_<ieee>`). The Zigbee2MQTT bridge device is never listed.
- Its IEEE address is not an active device on any network the app tracks: the app has it as removed, or has never seen it.
- The MQTT integration owns it, and none of its MQTT entities has a live state. A device from a Zigbee2MQTT network the app doesn't track still has live entities, so it isn't listed.

The list is checked once every tracked network has sent its device list, then whenever Home Assistant's device or entity registry changes.

Nothing is removed until you press **Review and remove**, tick the devices and confirm. Devices used by an automation or script start unticked, with a warning. The app checks every ticked device again before removing it, and skips any that is no longer a leftover. Each device is removed from the MQTT integration only (`config/device_registry/remove_config_entry`), which deletes the device and its entities from Home Assistant. Zigbee2MQTT is not changed. If another integration such as Battery Notes is also attached to the device, Home Assistant may keep the device for that integration.

Retained discovery topics: Zigbee2MQTT clears a device's discovery messages when the device is removed, so a leftover normally has none. If one is still retained, Home Assistant's MQTT integration clears it when the device is removed, so the device doesn't come back.

## Firmware updates

The **Updates** tab lists every device with a firmware update from Zigbee2MQTT, with the installed and available versions, power and progress. Versions are the numbers Zigbee2MQTT reports; the build name is shown when the device has one.

- **Check now** asks Zigbee2MQTT to check every device that supports updates, one at a time. Sleeping battery devices often don't answer.
- **Update now** and **Update all** add devices to a queue. The queue installs one device at a time.
- With `ota_window` set, updates still waiting are installed inside that window each night, one at a time. A device that fails is tried once more the next night. If that fails too, it stays failed until you press **Retry**. Retry starts the count again.
- Each update is recorded under **Recent updates** (last 200) and in the device's logbook in Home Assistant.

## Data

History is stored in the app's own data folder and is included in Home Assistant backups. Use **Settings → Backup** in the app to download or restore it.
