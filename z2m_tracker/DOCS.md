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
| `notify_service` | empty | Also send alerts to a notify service, e.g. `notify.mobile_app_phone`. Only `notify.` services are accepted; a bare name gets `notify.` added. |
| `alert_actions` | `true` | Add buttons to alerts sent to `notify_service`, and open the device when the alert is tapped. See [Buttons on phone alerts](#buttons-on-phone-alerts). |
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
| `naming_pattern` | empty | Naming pattern for devices, e.g. `{area} {type} {n}`. Fills in the name of each device that joins in **Pair device**, and drives the **Naming** check. Empty turns it off. See [Device names](#device-names). |
| `mqtt_host` | empty | Address of your MQTT broker, if it isn't the Mosquitto broker app. Empty uses the broker Home Assistant offers. See [MQTT broker](#mqtt-broker). |
| `mqtt_port` | `1883` | Port of that broker. Usually `1883`, or `8883` with TLS. |
| `mqtt_user` / `mqtt_password` | empty | Login for that broker. Leave empty if it allows anonymous connections. |
| `mqtt_ssl` | `false` | Connect to that broker over TLS. |
| `mqtt_ssl_verify` | `true` | Check the broker's TLS certificate. Turn off for a self-signed certificate. |
| `metrics_token` | empty | Token for the Prometheus endpoint, 16 characters or more. Empty keeps the endpoint off. See [Prometheus metrics](#prometheus-metrics). |

## MQTT broker

By default the app uses the broker Home Assistant offers to apps, which is the Mosquitto broker app. To use another broker, for example Mosquitto on a separate server or in Docker, set `mqtt_host` and, if needed, `mqtt_port`, `mqtt_user` and `mqtt_password`, then restart the app. Use the same broker as Zigbee2MQTT. These options are at the bottom of the app's **Configuration** tab, from version 1.4.0.

The app's Home Assistant entities use MQTT discovery, so they appear only if Home Assistant's MQTT integration is connected to the same broker.

Without `mqtt_ssl`, the broker login and all Zigbee messages cross your network unencrypted. For a broker on another machine, turn on `mqtt_ssl` (usually port `8883`) and keep `mqtt_ssl_verify` on. Turn it off only for a self-signed certificate on a network you trust.

## Prometheus metrics

The app can serve its device and network data for Prometheus to collect, for graphs in Grafana or similar tools. It stays off until you set it up:

1. Set `metrics_token` to a long random string (16 characters or more).
2. In the app's **Configuration** tab, under **Network**, give port `9099/tcp` a host port, for example `9099`.
3. Restart the app.

Then add a scrape job to Prometheus:

```yaml
scrape_configs:
  - job_name: z2m_tracker
    authorization:
      credentials: <your metrics_token>
    static_configs:
      - targets: ["homeassistant.local:9099"]
```

Every request must send the token, or it gets `401`. The endpoint serves only `/metrics` and changes nothing. The data includes device names and areas, so keep the token private.

Each device has gauges for whether it is online, link quality (latest and 24-hour average), battery level and days left, when it was last seen, messages in the last 24 hours, uptime over 30 days and open issues. Each sample has `ieee`, `name` and `network` labels. `z2m_tracker_device_info` adds the type, power source, vendor, model and area. There are also network totals: devices per status, open issues per check, whether each Zigbee2MQTT is online, and whether alerts are paused.

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

## Device names

Set a naming pattern in **Settings → Device names** (option `naming_pattern`), for example `{area} {type} {n}`. Tokens:

| Token | Becomes |
| --- | --- |
| `{area}` | The device's Home Assistant area. |
| `{type}` | A short kind worked out from what the device exposes, or else its description: Motion, Presence, Door, Leak, Smoke, Gas, CO, Vibration, Thermostat, Blind, Lock, Fan, Bulb, Button, Plug, Switch, Air, Temp, Light sensor, Repeater. |
| `{model}` | The Zigbee2MQTT model, e.g. `RTCGQ11LM`. |
| `{n}` | The lowest number from 1 that makes the name unique. Without `{n}`, a name that's taken gets ` 2`, ` 3` and so on added. |

A token with no value (no area yet, or a kind that can't be worked out) is left out, along with the space or dash next to it. `/`, `+` and `#` in an area or model are replaced or dropped, since Zigbee2MQTT treats them as part of the MQTT topic. Names are kept unique across all your networks.

While **Pair device** is open, each device that joins shows its progress: joined, interviewing, then identified or failed. Once identified, it gets a name field filled in from the pattern (it updates as you pick an area), an area picker and **Apply**, which renames it in Zigbee2MQTT and Home Assistant and sets its area. A failed interview shows the advice to re-pair it.

The **Naming** check (off for alerts until you turn it on, low priority) lists devices still named by their IEEE address (`0x…`) and, when a pattern is set, active devices whose name doesn't follow it. A device is only checked against the pattern when every token it uses has a value. **Rename to pattern** on the Health card shows each device with its suggested name; untick any you want to keep, confirm, and they're renamed one at a time, Home Assistant included.

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
| Slow to respond | Commands to a device (any `<name>/set` message: Home Assistant, automations, scripts) whose typical (median) reply over the last 24 hours is above 1 s, or at least 3 times its usual over the 7 days before and above 300 ms. Needs 10 commands in the 24 hours. A reply is the device's next state message. |
| Misses commands | At least 20% of the commands sent to a device over the last 24 hours got no state message back within 10 s (at least 10 commands). Commands to a device Zigbee2MQTT already marks offline aren't counted. The device panel's Health tab shows commands, typical reply and missed ones for the last 7 days. |
| Coordinator backup | `coordinator_backup.json` missing, without a network key, not matching the running channel/PAN ID, or more than 3 days old. Only checked when the Zigbee2MQTT data folder is inside the HA config folder. |
| Zigbee2MQTT memory | Zigbee2MQTT's own memory use grew more than 50% over the last 7 days (at least 5 days of readings). Usually a leak that ends in a crash; a restart clears it. Uses the `bridge/health` messages Zigbee2MQTT 2.5+ sends every 10 minutes by default (its `health.interval` setting). Health → Coordinators also shows its memory over the last 14 days, MQTT traffic and leaves this week. |
| Group members offline | A Zigbee2MQTT group with members that have been offline for longer than `offline_alert_minutes`. One issue per group, listing the offline members. |
| Maintenance overdue | A maintenance task on the device is past its due date. See Maintenance tasks below. |
| Reporting settings | A device whose attribute reporting drains its battery, floods the mesh, or may never report a measurement. See Reporting settings below. |
| Daily signal dip | Average link quality across a network drops at least 25% below that day's median in the same 1 to 4 hours on at least 4 of the last 7 days, and at least 30% of its devices dip then too. Usually something that switches on at that time: busy Wi-Fi on an overlapping channel, a microwave, or a USB 3 device near the coordinator. Health → Radio channels shows the hourly chart once a network has a week of link quality from at least 3 devices. |
| Worse since an upgrade | After Zigbee2MQTT or the coordinator firmware changes, the week after the upgrade looks clearly worse than the week before, once at least 2 days of it have passed (shown for 30 days). Shows only; it doesn't notify unless you add it to `notify_checks`. See **Effect of an upgrade** below for what is compared and the thresholds. |
| Naming | A device still named by its IEEE address (`0x…`), or, when `naming_pattern` is set, a name that doesn't follow it. Low priority. See [Device names](#device-names). |

The Health tab's **Groups** card lists every Zigbee2MQTT group with how many of its members are online. Groups with offline members show each member, with the offline ones in red. A device that dropped out of a group but is still on the network (for example after it left and was re-paired) is flagged for 30 days, until it's back in the group, or until the group is deleted. Leaving a group is only noticed while the app is running.

## Effect of an upgrade

When a network's Zigbee2MQTT version or coordinator firmware changes, the app records the upgrade (the last 20 per network, kept in `history.json` and in backups) and compares the week before it with the week after. Health → Coordinators → More details shows the latest upgrade, the before and after numbers, and a short list of earlier upgrades. Numbers appear once there is at least a day of data on both sides; until the week is over the card says how many days the "after" side has so far. The first hour after the upgrade is left out, because the restart itself drops devices for a moment. Two changes within an hour (firmware flashed, then Zigbee2MQTT updated) count as one upgrade.

Only data the app already keeps is used, and a number is left out when its data isn't there:

| Compared | From | Marked worse when |
| --- | --- | --- |
| Devices going offline per day | Offline spells of the network's devices (needs availability in Zigbee2MQTT) | Up by at least 1 a day and at least doubled |
| Average link quality | Hourly link quality, each device counted once | Down by at least 10 |
| Messages per hour | Hourly message counts | Down by at least 30% and at least 10 an hour (up by that much shows as changed, not worse) |
| Devices leaving per day | "Left" events | Up by at least 0.5 a day and at least doubled |
| Parent router changes per routing scan | Routing scans, each compared with the one before (needs 2 scans on each side) | Up by at least 2 per scan and at least 1.5 times |

The same rules the other way round mark a number better. The "before" numbers are worked out when the upgrade is seen, so they only cover what the app saw then: an app installed three days before an upgrade compares three days.

## Reporting settings

Zigbee2MQTT tells each device when to report its values: no more often than a minimum interval, at least once per maximum interval, and whenever the value changes by the reportable change. The device's **Health** tab lists these under **Reporting** in plain words, for example "Temperature: at least every 10 s, at most every 1 h, on a 0.1 °C change". The **Reporting settings** check flags three things:

| Rule | Flags | Recommended |
| --- | --- | --- |
| Drains the battery | A battery device with any maximum interval under 5 minutes, or a minimum interval of 0 on a measurement that changes often (temperature, humidity, pressure, illuminance, CO2, PM2.5, soil moisture). Every report wakes the radio. | Maximum 1 h, minimum 30 s, and a 0.2 °C / 1 % change instead of 0. Battery level: 1 h to 12 h. |
| Floods the mesh | A mains device reporting power, energy, current or voltage with a maximum interval of 5 seconds or less, or on every change (reportable change 0) with a minimum interval under a minute. Every router has to carry that traffic. | Maximum 1 h, minimum 10 s, and a small change instead of 0 (power 5, current 10, voltage 5, energy 10, in the device's own units). |
| May go silent | A measurement with a maximum interval of 65535 (reporting turned off), or a measurement the device has the cluster for but no reporting at all. The second only counts on devices that use configured reporting for something else: many (Aqara, most Tuya) send values without it. | Maximum 1 h. For a missing reporting, **Reconfigure** asks Zigbee2MQTT to set up its defaults again. |

Recommended values change only the part that's wrong and keep the rest. **Use recommended** asks before it sends `bridge/request/device/configure_reporting` to Zigbee2MQTT, then logs a "reporting changed" event in History. A battery device may be asleep: press its button just before you confirm. When the device's message rate over the last 6 hours backs up a finding (more than 60 an hour on battery, 720 an hour on mains), the issue says so. The check is low severity and only shows in Health until you set it to notify in Settings → Checks.

## Router outages

When a router goes offline, the devices that route through it often follow. The Offline check groups them: a device whose parent router in the last routing scan that saw it is offline (or that router's parent, and so on) shows under that router in Health, and the router gets one issue and one notification, e.g. "Router offline: Landing plug - 6 devices that route through it are offline too". The devices don't notify on their own while the router is down. When the router comes back, devices still offline 5 minutes later become normal issues and notify as usual. Devices that were never in a routing scan, and routers whose Offline check is ignored, aren't grouped. Repairs in Home Assistant follow the same grouping; each device's own entities still show it offline.

The device panel's Info tab also shows message rates, Zigbee2MQTT's own counters (messages, leaves, address changes) and the device's bindings.

## Incidents

The Activity tab can show **Incidents** instead of **All events** (the choice is remembered in the browser). An incident groups related problems on one network into one row, e.g. "Likely cause: router Garage plug offline · 23:10–23:40 · 14 devices affected, all back". Open a row to see its events and jump to the devices.

- Problem events are: a device going offline, a device leaving, an interview failing, Zigbee2MQTT going offline and Zigbee2MQTT restarting. Route changes aren't logged as events, so they don't count.
- A problem event starts an incident, or joins the open one on the same network if it comes within 10 minutes of that incident's last problem event. After 10 quiet minutes the incident takes no new problems, but its devices coming back are still added to it.
- Only incidents with 3 or more devices, or with Zigbee2MQTT going offline or restarting, are shown. One device going offline on its own isn't an incident.
- An incident is ongoing while one of its devices is still offline or Zigbee2MQTT is still down. A device that left and wasn't re-paired counts as not back but doesn't keep the incident ongoing.
- Likely cause, first rule that fits: Zigbee2MQTT went offline or restarted: "Zigbee2MQTT or coordinator down". A router in the incident went down no later than devices that route through it (from the last routing scan) and at least one of them followed: "router X offline". 3 or more devices dropped within a minute of the start through 2 or more different parents: "network-wide drop (interference or coordinator)". Otherwise the cause is unknown.

The last 50 incidents are kept, worked out from the activity log. No notifications are sent for incidents.

## Ping

Mains devices have a **Ping** button on the device panel's Info tab. It reads one attribute (the Basic cluster's ZCL version, the same read Zigbee2MQTT's availability check uses) and shows how long the reply took, or "No reply in 10 s". Nothing is written to the device. Battery devices sleep, so they have no Ping button.

Every night, the hour after the routing scan (04:00 when `networkmap_hour` is `-1`), the app pings each online router once, one at a time, 2 seconds apart. The Trends tab shows the last 14 nights as a bar chart, and the **Router getting slower** check uses them. 30 nights are kept.

Ping uses Zigbee2MQTT's `bridge/request/action` API, so it needs Zigbee2MQTT 2.7 or newer. The time includes the trip through MQTT and Zigbee2MQTT, a few milliseconds.

The integration can mirror warnings and notify-enabled checks into **Settings → Repairs**. Turn it off in the integration's options. Repairs also stay empty while `alerts_enabled` is off.

## Maintenance tasks

Recurring physical jobs for a device, such as "Test smoke alarm" every month or "Check door sensor magnet" every year. Add them on the device panel's **Notes** tab: a title, how often (1 day to 10 years), when it was last done and an optional note. Smoke, leak, door/window, motion and radiator devices offer a few suggestions; any text works. A device can have up to 20 tasks.

The next due date is the last done date plus the interval, or the day the task was added plus the interval if it was never done. **Mark done** sets today and adds a "maintenance done" entry to the device's logbook. The Health tab's **Maintenance** card lists overdue tasks and those due in the next 14 days. An overdue task is the **Maintenance overdue** check; it shows in Health but doesn't notify unless you turn that on in Settings → Checks.

With the integration, `todo.z2m_tracker_maintenance` lists the same tasks (ticking one marks it done) and `calendar.z2m_tracker_maintenance` shows each task's next due date and the day it was last done. Tasks are part of the device's history, so they are in backups.

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

## Buttons on phone alerts

Alerts sent to `notify_service` through the Home Assistant Companion app come with buttons (turn them off with `alert_actions`):

- **Mute 24 h** mutes that check on that device for 24 hours, the same as muting it in the device panel. On a router outage alert it mutes the router's Offline check, which also quiets the devices behind it.
- **Ping** pings the device and sends the answer time (or that it didn't answer) as a second notification. Only on mains devices that can be pinged (see [Ping](#ping)).
- **Battery replaced** records a battery change today, the same as the button on the Battery tab (and in Battery Notes, if the device has it). Only on Low battery and Battery running down alerts.

Tapping the alert opens the device in the Zigbee Tracker. Alerts about a whole network, a group, joins and leaves, the quiet hours digest and the weekly summary have no buttons; tapping them opens the Zigbee Tracker.

The app hears the button through Home Assistant's `mobile_app_notification_action` event, so it works without the integration. Each alert's buttons carry a one-time code: a button works once, for 7 days, and only for the device and check it was sent for. Anything else is ignored and logged. Alerts carry a `tag`, so a repeat of the same alert replaces the old one on the phone.

If your notify service isn't the Companion app and rejects the extra data, turn `alert_actions` off.

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

## Security

- **Who can use the app.** The sidebar is open to Home Assistant administrators only. The integration in Home Assistant Core reaches the app with an API token: the app makes it on first start (kept in its `/data` folder) and hands it to Home Assistant through Supervisor discovery, which only Core can read. Anything else on Supervisor's network gets a 403. Update the app and the integration together: an older integration has no token and shows as unavailable.
- **The MQTT broker.** The app trusts what arrives under each Zigbee2MQTT base topic, so whoever can publish there can feed it made-up devices. With the Mosquitto broker app, every Home Assistant user account can log in to the broker. To limit that, turn on Mosquitto's `customize` option and give only Zigbee2MQTT write access to `zigbee2mqtt/#` with an ACL file. The app checks what it receives (text, numbers and IEEE addresses), ignores malformed messages, and holds a device list that would remove more than half of a network until Zigbee2MQTT sends it again.
- **Device names in notifications.** Names and details from Zigbee2MQTT can't add links, images or HTML to Home Assistant notifications, Repairs or the to-do list.

## Data

History is stored in the app's own data folder and is included in Home Assistant backups. Use **Settings → Backup** in the app to download or restore it.
