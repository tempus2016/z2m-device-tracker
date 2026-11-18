# Z2M Device Tracker

Open **Zigbee Tracker** in the sidebar. Most settings can be changed on its **Settings** tab; the options below are the same settings.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `base_topics` | `zigbee2mqtt` | MQTT base topic of each Zigbee2MQTT instance to track. Spaces are kept, so a base topic that ends in a space works; in the YAML editor, quote it: `- "Z2MQTT "`. |
| `z2m_panels` | empty | Links to each Zigbee2MQTT app, as `base_topic=app_slug`, e.g. `zigbee2mqtt=45df7312_zigbee2mqtt`. The slug is in the app's URL. |
| `alerts_enabled` | `true` | Master switch for alerts. |
| `persistent_notifications` | `true` | Show alerts as Home Assistant notifications. |
| `fire_events` | `true` | Fire `z2m_tracker_alert` events for automations. |
| `ha_repairs` | `true` | With the integration installed, list problems in Home Assistant's **Settings → Repairs**. Set it in **Settings → Notifications**. See [Repairs](#repairs). |
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
| `networkmap_timeout_minutes` | `60` | How long to wait for Zigbee2MQTT to finish a routing scan (5 to 240). A scan of a network with 100 devices can take over 20 minutes. The scan counts as running until it answers or this limit passes, so no second scan starts and the nightly router ping waits for it. An answer that comes after the limit is still saved. |
| `ha_entities` | `true` | Add tracker sensors to each Zigbee2MQTT device via MQTT discovery. |
| `weekly_summary` | `true` | Send a weekly summary notification. |
| `summary_day` / `summary_hour` | `0` / `9` | When to send it (0 = Monday). |
| `wifi_channels` | empty | Your 2.4 GHz Wi-Fi channels, e.g. `1, 6`, used for the radio channel check. |
| `chatty_per_hour` | `1200` | Messages an hour (averaged over 6 hours) above which a device is flagged as chatty. |
| `ota_window` | empty | Time window for automatic firmware updates, e.g. `02:00-05:00` (local time, may cross midnight). Empty turns automatic updates off. A failed update is retried once, the next night. |
| `ota_min_battery` | `30` | Battery devices below this level are skipped by automatic and bulk updates. **Update anyway** overrides it for one device. |
| `quiet_hours` | `false` | Hold alerts during quiet hours and send one summary when they end. |
| `quiet_start` / `quiet_end` | `23:00` / `07:00` | Quiet hours in the app's time zone, as `HH:MM`. The window can cross midnight. |
| `escalate_enabled` | `false` | Send an alert again to a second notify service when nobody answers it in time. See [Escalation](#escalation). |
| `escalate_hours` | `2` | How long an alert can go unanswered before it escalates (1 to 168 hours). |
| `escalate_service` | empty | The second notify service, e.g. `notify.mobile_app_partner_phone`. Escalation needs one. |
| `escalate_critical_only` | `true` | Only escalate alerts for devices marked **Critical device**. |
| `naming_pattern` | empty | Naming pattern for devices, e.g. `{area} {type} {n}`. Fills in the name of each device that joins in **Pair device**, and drives the **Naming** check. Empty turns it off. See [Device names](#device-names). |
| `battery_shop_url` | empty | Store search address for the cart buttons in **Batteries to buy**, with `{type}` where the battery type goes, e.g. `https://www.amazon.co.uk/s?k={type}` or `https://www.amazon.de/s?k={type}`. Any shop works. Empty hides the buttons. Set it in **Settings → Battery brands**. No affiliate tags are added. |
| `retained_autoclear` | `false` | After a device is renamed, clear the retained messages the broker still keeps under its old name. Off lists them on **Network → Broker** for you to clear. See [Stale retained messages](#stale-retained-messages). |
| `broker_stats` | `true` | Read Mosquitto's `$SYS/broker/#` statistics for **Network → Broker**. See [Broker statistics](#broker-statistics). |
| `impossible_temp_min` / `impossible_temp_max` | `-30` / `60` | The believable temperature range in °C for the **Impossible readings** check. Set them in **Settings → Limits**. |
| `humidity_stuck_hours` | `6` | How long humidity must stay at exactly 0 or 100% before the **Impossible readings** check flags it. |
| `mqtt_host` | empty | Address of your MQTT broker, if it isn't the Mosquitto broker app. Empty uses the broker Home Assistant offers. See [MQTT broker](#mqtt-broker). |
| `mqtt_port` | `1883` | Port of that broker. Usually `1883`, or `8883` with TLS. |
| `mqtt_user` / `mqtt_password` | empty | Login for that broker. Leave empty if it allows anonymous connections. |
| `mqtt_ssl` | `false` | Connect to that broker over TLS. |
| `mqtt_ssl_verify` | `true` | Check the broker's TLS certificate. Turn off for a self-signed certificate. |
| `metrics_token` | empty | Token for the Prometheus endpoint, 16 characters or more. Empty keeps the endpoint off. See [Prometheus metrics](#prometheus-metrics). |
| `community_ratings_fetch` | `true` | Download the newest community device ratings from GitHub once a day. Off uses the copy that comes with the app, updated with each release. See [Community ratings](#community-ratings). |

## Where things are

The sidebar has seven tabs. On a phone the last three sit behind **More** in the bottom bar.

| Tab | What it holds |
| --- | --- |
| **Health** | Problems, worst first, then one line for each part of the app on another tab, so a glance says whether anything there needs a look. |
| **Devices** | Every device, with its own page: info, health, trends, history, battery, messages, community and notes. |
| **Network** | **Map** (routing map, floor plan, what-if), **Coordinator** (each Zigbee2MQTT and its coordinator, and copies of its backup), **Broker** (MQTT broker statistics, stale retained messages and devices left behind in Home Assistant), **Radio** (channels and Wi-Fi overlap) and **Groups**. |
| **Upkeep** | **Batteries** (what to buy, and how long each brand lasts), **Maintenance** (recurring tasks and warranties), **Models** (reliability by model and device age), **Costs** (running costs) and **Community** (community ratings, and your models nobody has rated yet). |
| **Updates** | Firmware updates. |
| **Activity** | The logbook and incidents. |
| **Settings** | Alerts, checks, limits, naming and the rest. |

With more than one Zigbee2MQTT, **Network** starts with a network picker. **All networks** shows a card for each network (devices, how many are online, open problems, channel and Zigbee2MQTT version) and warns when the same device name is used on two networks, because Home Assistant then gives the second device's entities IDs ending in `_2`. Picking one network shows its map, coordinator, broker leftovers, radio channel and groups on their own. The app remembers the choice in this browser.

## Live messages

A device's **Messages** tab shows the last 50 messages to and from it, newest first, and keeps updating while it's open: its state messages, availability changes, and commands sent to its `/set` topic with how long the device took to answer (or that it didn't answer within 10 seconds). Values that changed since the previous state message are highlighted, except link quality and `last_seen`, which change every time. The chips choose which kinds show, **Pause** stops the list moving, and **Copy** puts the shown messages on the clipboard.

The messages are kept in memory only, for every device whether its tab is open or not, and nothing is saved: after a restart the lists start empty. Payloads longer than 2,000 characters are cut short.

## MQTT broker

By default the app uses the broker Home Assistant offers to apps, which is the Mosquitto broker app. To use another broker, for example Mosquitto on a separate server or in Docker, set `mqtt_host` and, if needed, `mqtt_port`, `mqtt_user` and `mqtt_password`, then restart the app. Use the same broker as Zigbee2MQTT. These options are at the bottom of the app's **Configuration** tab, from version 1.4.0.

The app's Home Assistant entities use MQTT discovery, so they appear only if Home Assistant's MQTT integration is connected to the same broker.

Without `mqtt_ssl`, the broker login and all Zigbee messages cross your network unencrypted. For a broker on another machine, turn on `mqtt_ssl` (usually port `8883`) and keep `mqtt_ssl_verify` on. Turn it off only for a self-signed certificate on a network you trust.

## Broker statistics

Mosquitto publishes statistics about itself under `$SYS/broker/` every 10 seconds (its `sys_interval` setting). With `broker_stats` on (the default; **Settings → Broker**), the app reads them and shows at the top of **Network → Broker**:

- **Clients** connected, **Retained** messages held, messages **Dropped** in the last 24 hours, and broker **Uptime**.
- **Messages per minute, last 24 h**: one bar per hour, worked out from Mosquitto's received-messages counter. Hours with dropped messages are red. The chart is kept in memory only, so it starts again when the app restarts.
- A red line when messages were dropped, with the time of the latest drop. Mosquitto drops messages when a client's queue is full (`max_queued_messages`) or it runs short of memory; `$SYS` doesn't say which client.

Health's **Broker** line shows the dropped messages too. Other brokers (EMQX, HiveMQ and others) don't publish Mosquitto's `$SYS` topics; after a minute without any the page says so. A broker whose access list doesn't let the app's login read `$SYS/#` looks the same.

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

Each device has gauges for whether it is online, link quality (latest and 24-hour average), battery level and days left, when it was last seen, messages in the last 24 hours, uptime over 30 days and open issues. Each sample has `ieee`, `name` and `network` labels. `z2m_tracker_device_info` adds the type, power source, vendor, model and area. There are also network totals: devices per status, open issues per check, whether each Zigbee2MQTT is online, each network's mesh score (`z2m_tracker_network_mesh_score`), and whether alerts are paused.

## Mesh score

The top of **Health** shows one score from 0 to 100 for each network, worked out every few minutes from four parts:

| Part | Weight | Full marks when |
| --- | --- | --- |
| Devices online | 40 | Every device on the network is online. Scored as the share that isn't offline. |
| Average link quality | 20 | The 24-hour average link quality across the network's devices is 100 or more. Below that it scales down to 0. |
| Devices with a backup route | 20 | No device depends on a single router. From the last routing scan: a router that loses its path to the coordinator when one other router goes, and the battery devices whose parent is such a router, count as without a backup route. |
| Open problems | 20 | No open warnings on Health for that network. Each warning takes off a share, and all 20 are gone at 5 warnings or a fifth of the devices, whichever is more. Devices offline behind an offline router count once, with the router. |

A part without data yet (no link quality, or no routing scan) is left out, and the others are scaled up to 100. The coordinator isn't counted.

**Details** shows each part with its change since a week ago and the score over the last 30 days. The app keeps one value a day (the day's last) for 90 days in `history.json`.

With `ha_entities` on, each network also gets a sensor through MQTT discovery, on a device named after the base topic, so the first network's is `sensor.zigbee2mqtt_mesh_score`. Its attributes hold the four parts and the score a week ago.

## Expected reporting

Zigbee2MQTT decides when a device is offline: by default after 10 minutes for mains devices and 25 hours for battery devices. The **Stopped reporting** check is quicker: it flags a device that has sent nothing for longer than its **reporting profile** allows, even while Zigbee2MQTT still shows it online. Pick the profile on the device's **Health** tab, under **Expected reporting**:

| Profile | Silent after | For |
| --- | --- | --- |
| Reports on change | 3 h | Devices that send when something happens plus a regular check-in: door and window sensors, leak and smoke alarms, buttons. |
| Regular | 1 h | Sensors that report every few minutes: temperature, humidity, air quality. |
| Chatty | 15 min | Devices that report all the time, such as plugs measuring power. |
| Custom | your own time, 30 seconds to 7 days | Anything else. Devices that had an interval set before profiles existed keep it as Custom. |
| Off | never | |

A device without its own profile uses the **default for its type**, set in **Settings → Checks → Default reporting profile by device type**: door and window, leak, smoke, buttons and motion sensors report on change; temperature, humidity, air and light sensors are regular; plugs and bulbs are chatty; everything else is off. A default only starts once the device has shown it reports often enough for it: over the last week its usual gap between messages, times 3, must fit inside the profile's time. Until then the Health tab says the default is waiting. That keeps a bulb that only reports when switched, or a remote that only sends when pressed, from being flagged by a default nobody picked for it. Picking a profile on the device itself always applies.

The tab shows how often the device usually reports. The check notifies by default. It is skipped while that device's Zigbee2MQTT is offline, so a network outage raises one alert, not one per device.

To set a Custom time for many devices at once, go to **Settings → Thresholds → Expected reporting** and select **Set for many devices…**. Pick mains devices, battery devices or both, and a multiplier (2×, 3×, 5× or 10×). Each device gets its usual gap between messages times the multiplier, rounded up to a tidy value such as 3 min, 30 min or 6 h. The usual gap comes from the last week's message counts, or the last 6 hours for a device with less history. Untick any device you want to leave alone, then select **Apply**. **Turn off** sets the ticked devices to Off.

Some devices are left unticked:

- **Reports on change**: devices that only send when something happens, such as door sensors, buttons and plain switches. A device counts as this when it has no regular reading (temperature, humidity, pressure, power, energy, air quality, light level or soil moisture).
- **Not enough data**: devices with too few messages to tell how often they report.

## When a device alerts

`offline_alert_minutes` counts from the moment Zigbee2MQTT marks the device offline, not from its last message. So with the defaults, a mains device alerts about 70 minutes after it goes quiet (10 + 60), and a battery device about 26 hours later (25 h + 60 min). Each device's **Health** tab shows its own steps under **When this device alerts**, from Zigbee2MQTT's availability settings: its own timeout or the mains/battery default, the offline delay, and Expected reporting if set. If Zigbee2MQTT availability is off for the device, only **Not heard from** applies, after 2 days.

## Pairing left open

While pairing (permit join) is open, any nearby Zigbee device can join. The **Pairing left open** check warns and notifies when pairing stays open for 10 minutes, or straight away when something other than the Zigbee Tracker opened it (the Zigbee2MQTT frontend, Home Assistant or an automation). **Close now** on the Health tab closes it. Existing installs get this alert switched on once when they update; turn it off in Settings → Checks if you don't want it.

## Settings advice

When a Zigbee2MQTT instance has `advanced.last_seen` set to `disable` (read from the settings it publishes in `bridge/info`), Health shows a note under **Worth a look**. Without a `last_seen` time in each message, messages Zigbee2MQTT repeats from its cache look new to the app, so a battery device that has gone quiet can still look alive to the **Not heard from** and **Stopped reporting** checks.

**Turn on** asks you to confirm, then sets `advanced.last_seen: ISO_8601` through Zigbee2MQTT's `bridge/request/options`. Zigbee2MQTT saves it in its `configuration.yaml`, and the change shows in the network's settings log. **Dismiss** hides the note for that network for good. The note never sends a notification.

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
| Impossible readings | A temperature outside `impossible_temp_min` to `impossible_temp_max` (−30 to 60 °C by default), humidity or battery outside 0–100%, at least once in the last 24 hours; or humidity stuck at exactly 0 or 100% for longer than `humidity_stuck_hours`. Shows the value, how many times in 24 hours, and the range of its other readings today and the day before. Often a failing battery or a bad contact. Battery values outside 0–100% are never stored and never count towards battery estimates. |
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
| Warranty ending | The warranty date in the device's asset details is 30 days away or less. See Warranties below. |
| Reporting settings | A device whose attribute reporting drains its battery, floods the mesh, or may never report a measurement. See Reporting settings below. |
| Daily signal dip | Average link quality across a network drops at least 25% below that day's median in the same 1 to 4 hours on at least 4 of the last 7 days, and at least 30% of its devices dip then too. Usually something that switches on at that time: busy Wi-Fi on an overlapping channel, a microwave, or a USB 3 device near the coordinator. Health → Radio channels shows the hourly chart once a network has a week of link quality from at least 3 devices. |
| Worse since an upgrade | After Zigbee2MQTT or the coordinator firmware changes, the week after the upgrade looks clearly worse than the week before, once at least 2 days of it have passed (shown for 30 days). Shows only; it doesn't notify unless you add it to `notify_checks`. See **Effect of an upgrade** below for what is compared and the thresholds. |
| Stale retained messages | Retained MQTT messages that belong to no current device: topics under a device's old name, topics of removed devices, and Home Assistant discovery configs for devices no longer in Zigbee2MQTT. One low-priority line on Health that opens **Network → Broker**. See [Stale retained messages](#stale-retained-messages). |
| Rated poorly by others | Other Zigbee Tracker users rate the device's model **Avoid** in the [community ratings](#community-ratings), from at least 5 reports. Low priority, and never notifies unless you add `community_avoid` to `notify_checks`. |
| Retained command | A command for a current device (`<name>/set`, `<name>/set/<attribute>` or `<name>/<endpoint>/set`) that is retained on the broker. The broker sends it again every time it or Zigbee2MQTT restarts, so a light switches back on or a valve opens by itself. Usually an automation or Node-RED flow that publishes with retain on. Found when the app connects to the broker (retained messages are only marked as retained then). **Clear** publishes an empty retained message to the topic, which deletes it from the broker; nothing is sent to the device. **Ignore** mutes it for that device. |
| Availability disagreement | Zigbee2MQTT's availability for a device contradicts its messages. **Offline** while 3 or more live messages arrived in the last 10 minutes, all after it went offline (and it has been offline for at least 5 minutes): the device is clearly talking, so the Offline check stays quiet for it and this shows instead. **Online** while it has sent nothing for at least 6 hours and 10 times its usual gap between messages (2 days without a week of history), when either Zigbee2MQTT doesn't track its availability or it's a battery device past Zigbee2MQTT's passive timeout plus an hour. Mains devices Zigbee2MQTT pings are not checked, since a ping keeps them online without a message, and devices with their own report interval are left to **Stopped reporting**. Usually a stale retained `<name>/availability` topic, for example one left from when availability was turned on. Live messages are only counted while the app is running. |
| Sends different data | A device stopped sending a value it always sent, and an automation or script in Home Assistant uses that value's entity. See [Payload changes](#payload-changes). |
| Naming | A device still named by its IEEE address (`0x…`), or, when `naming_pattern` is set, a name that doesn't follow it. Low priority. See [Device names](#device-names). |
| Firmware you noted | A device runs a firmware version you wrote a firmware note about. Low priority. See [Firmware notes](#firmware-notes). |

**Network → Groups** lists every Zigbee2MQTT group with how many of its members are online. Groups with offline members show each member, with the offline ones in red. A device that dropped out of a group but is still on the network (for example after it left and was re-paired) is flagged for 30 days, until it's back in the group, or until the group is deleted. Leaving a group is only noticed while the app is running.

Each group also has a **History**: members added and removed (marked when the removed device was replaced), renames, and when the group was created. It starts when the app first sees the group; a group that already existed then has no "created" line. Zigbee2MQTT keeps its group list on the broker, so changes made while the app was stopped are picked up, dated when the app started again. The last 50 changes per group are kept in `history.json`, and the history of a deleted group is dropped 90 days after it was deleted.

## Payload changes

After a firmware update or a change in Zigbee2MQTT's converter for it, a device can start sending different data: a value is renamed, or one disappears. The app learns which values (keys) each device sends from its first 30 messages, then logs a change in the device's History tab, with the keys that are new and the keys that are gone, and the firmware version when the device was updated in the week before.

- A key counts as gone when it was in at least 80% of the learning messages and then misses 20 messages in a row.
- A key counts as new when it turns up in 5 messages within 50. Keys that only appear now and then are remembered and never reported, and `action`, `update`, `last_seen`, `linkquality` and the like are ignored.
- Changes within a day of each other are one entry.

The **Sends different data** check shows on Health for 30 days when a gone key's Home Assistant entity is still used by an automation or script. The app finds the entity from Zigbee2MQTT's discovery ids, so it needs the device to be in Home Assistant through MQTT discovery, and the app to have been running (or HA's registry to still hold the entity) when the key went.

With Zigbee2MQTT's `cache_state` on (the default), Zigbee2MQTT keeps repeating the last cached value of a key the device no longer sends, and keeps it across restarts, so a gone key may only be noticed once that cached value is cleared. New keys are noticed either way.

## Coordinator backup copies

Zigbee2MQTT rewrites `coordinator_backup.json` in place, so a bad backup (a new network key after starting a fresh network, devices missing after an adapter swap) replaces the last good one. When the app can read the file (the Zigbee2MQTT data folder is inside the HA config folder) it keeps its own copies in its data folder, `coordinator_backups/`: a new copy each time the backup's devices, network key, PAN ID or channel change, and one a week when nothing changed. The last 10 copies per network are kept.

**Network → Coordinator** lists them with the date of each backup, how many devices it holds, and what changed from the copy before it: devices added or removed (by name, or IEEE address for a device the app doesn't know), and whether the network key, PAN ID or channel changed. **Download** saves that copy as a `coordinator_backup.json` you can give Zigbee2MQTT to restore the network.

The copies contain the network key, like the original file. The app never logs or shows the key: it only keeps a short hash of it to tell whether it changed, and a copy is sent only when you click Download. The copies are in the app's data folder, so they are part of Home Assistant backups of the app.

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
| May go silent | A measurement with a maximum interval of 65535 (reporting turned off), or a measurement the device has the cluster for but no reporting at all. The second only counts on devices that use configured reporting for something else: many (Aqara, most Tuya) send values without it. It also doesn't count when the value changed in the device's messages in the last 7 days: the device sends it anyway, for example a Tuya plug that sends power with its current and voltage reports. The Health tab then lists it as coming with the device's other reports. | Maximum 1 h. For a missing reporting, **Reconfigure** asks Zigbee2MQTT to set up its defaults again. |

Recommended values change only the part that's wrong and keep the rest. **Use recommended** asks before it sends `bridge/request/device/configure_reporting` to Zigbee2MQTT, then logs a "reporting changed" event in History. A battery device may be asleep: press its button just before you confirm. When the device's message rate over the last 6 hours backs up a finding (more than 60 an hour on battery, 720 an hour on mains), the issue says so. The check is low severity and only shows in Health until you set it to notify in Settings → Checks.

## Router outages

When a router goes offline, the devices that route through it often follow. The Offline check groups them: a device whose parent router in the last routing scan that saw it is offline (or that router's parent, and so on) shows under that router in Health, and the router gets one issue and one notification, e.g. "Router offline: Landing plug - 6 devices that route through it are offline too". The devices don't notify on their own while the router is down. When the router comes back, devices still offline 5 minutes later become normal issues and notify as usual. Devices that were never in a routing scan, and routers whose Offline check is ignored, aren't grouped. Repairs in Home Assistant follow the same grouping; each device's own entities still show it offline.

A power cut looks different: several mains devices in one room drop at the same moment, while the battery devices there carry on. When 3 or more mains-powered devices in the same Home Assistant area go offline within 11 minutes of each other, and at least one battery device in that area is still online, the Offline check folds them into one issue and one notification, e.g. "Power loss in Garage? - 4 mains devices in Garage went offline within 6 s of each other". Router grouping comes first: devices already shown under an offline router aren't counted again. Devices without an area, and devices whose Offline check is ignored, aren't grouped. The 11 minutes cover Zigbee2MQTT's default availability timeout for mains devices (10 minutes): after a power cut it only notices each device is gone when that device next misses a ping, so the drops spread out. With a longer `availability.active.timeout` in Zigbee2MQTT, a power cut may show as separate Offline issues.

The device panel's Info tab also shows message rates, Zigbee2MQTT's own counters (messages, leaves, address changes) and the device's bindings.

## Incidents

The Activity tab can show **Incidents** instead of **All events** (the choice is remembered in the browser). An incident groups related problems on one network into one row, e.g. "Likely cause: router Garage plug offline · 23:10–23:40 · 14 devices affected, all back". Open a row to see its events and jump to the devices.

- Problem events are: a device going offline, a device leaving, an interview failing, Zigbee2MQTT going offline and Zigbee2MQTT restarting. Route changes aren't logged as events, so they don't count.
- A problem event starts an incident, or joins the open one on the same network if it comes within 10 minutes of that incident's last problem event. After 10 quiet minutes the incident takes no new problems, but its devices coming back are still added to it.
- Only incidents with 3 or more devices, or with Zigbee2MQTT going offline or restarting, are shown. One device going offline on its own isn't an incident.
- An incident is ongoing while one of its devices is still offline or Zigbee2MQTT is still down. A device that left and wasn't re-paired counts as not back but doesn't keep the incident ongoing.
- Likely cause, first rule that fits: Zigbee2MQTT went offline or restarted: "Zigbee2MQTT or coordinator down". A router in the incident went down no later than devices that route through it (from the last routing scan) and at least one of them followed: "router X offline". 3 or more mains devices in one Home Assistant area went offline within 60 seconds of each other while a battery device in that area stayed online: "power loss in <area>" (the router rule still wins when the router and the devices behind it cover all of them); the row says whether they came back together, and an open one is also shown at the top of Health. 3 or more devices dropped within a minute of the start through 2 or more different parents: "network-wide drop (interference or coordinator)". Otherwise the cause is unknown.

The last 50 incidents are kept, worked out from the activity log. No notifications are sent for incidents.

## Ping

Mains devices have a **Ping** button on the device panel's Info tab. It reads one attribute (the Basic cluster's ZCL version, the same read Zigbee2MQTT's availability check uses) and shows how long the reply took, or "No reply in 10 s". Nothing is written to the device. Battery devices sleep, so they have no Ping button.

Every night, the hour after the routing scan (04:00 when `networkmap_hour` is `-1`), the app pings each online router once, one at a time, 2 seconds apart. The Trends tab shows the last 14 nights as a bar chart, and the **Router getting slower** check uses them. 30 nights are kept.

Ping uses Zigbee2MQTT's `bridge/request/action` API, so it needs Zigbee2MQTT 2.7 or newer. The time includes the trip through MQTT and Zigbee2MQTT, a few milliseconds.

## Repairs

The integration can mirror warnings and notify-enabled checks into **Settings → Repairs** in Home Assistant. Each one closes itself when the problem is fixed. Three serious problems get their own wording and always make the list, even when many other issues are open:

| Problem | Repairs title | Severity |
|---|---|---|
| A device marked **Critical device** is offline | "Back door is offline (critical device)", with buttons to mute the check | Error |
| A Zigbee2MQTT network is offline | "Zigbee2MQTT offline: Main network" | Error |
| A coordinator backup is missing, old, unreadable or doesn't match the network | "Coordinator backup problem: Main network" | Warning |

Turn Repairs off with **Show problems in HA Repairs** in the app's **Settings → Notifications** (`ha_repairs`), or in the integration's options; either one off keeps Repairs empty. Repairs also stay empty while `alerts_enabled` is off.

## Maintenance tasks

Recurring physical jobs for a device, such as "Test smoke alarm" every month or "Check door sensor magnet" every year. Add them on the device panel's **Notes** tab: a title, how often (1 day to 10 years), when it was last done and an optional note. Smoke, leak, door/window, motion and radiator devices offer a few suggestions; any text works. A device can have up to 20 tasks.

The next due date is the last done date plus the interval, or the day the task was added plus the interval if it was never done. **Mark done** sets today and adds a "maintenance done" entry to the device's logbook. **Upkeep → Maintenance** lists overdue tasks and those due in the next 14 days. An overdue task is the **Maintenance overdue** check; it shows in Health but doesn't notify unless you turn that on in Settings → Checks.

With the integration, `todo.z2m_tracker_maintenance` lists the same tasks (ticking one marks it done) and `calendar.z2m_tracker_maintenance` shows each task's next due date and the day it was last done. Tasks are part of the device's history, so they are in backups.

## Warranties

Set **Warranty until** in a device's asset details on its **Notes** tab (next to the purchase date, shop and price). The Notes tab then says how long the warranty has left. **Upkeep → Maintenance** has a **Warranties** list: warranties ending in the next 60 days, soonest first, then those that ended in the last 90 days. **Details** opens the device's Notes tab. A warranty 30 days or less from its end is the **Warranty ending** check; it shows in Health but doesn't notify unless you turn that on in Settings → Checks.

## Pause alerts

Use **⋮ → Pause alerts** before planned work, such as a Zigbee2MQTT upgrade, re-pairing or a power cut. Pick 30 min, 1 hour, 4 hours, until 08:00 tomorrow (the app's time zone), or until you resume. While alerts are paused:

- Health keeps checking and lists issues as normal. A banner on every tab shows when the pause ends and how many new issues are waiting.
- No issue alerts are sent. When the pause ends, or you press **Resume now**, issues that are still there are sent once. Issues that came and went during the pause are never sent.
- Joined, left and interview failed alerts are not sent, and not sent later either. They still show in Activity.
- Notifications already in Home Assistant stay until their issue clears after the pause.
- The weekly summary and **Send test notification** still go out.

The pause survives an app restart. The integration has the actions `z2m_tracker.pause_alerts` (optional `minutes`, empty pauses until resumed) and `z2m_tracker.resume_alerts`. Changing an alert setting during a pause marks the issues already found as sent, the same as it does outside a pause.

## Running costs

**Upkeep → Costs** adds up what the devices cost and what their batteries cost:

- **Devices bought**: the total of the **Price** in each device's asset details (Notes tab). Prices are free text, so `£14.99`, `14,99 €` and `EUR 20` all work; the currency mark shown is the one most of your prices use. Removed devices count too.
- **Batteries, last 12 months**: battery changes recorded in the last 365 days, times the number of batteries each device takes, times the price per battery type.
- **Batteries, next 12 months (est.)**: for each active battery device, one change when its battery is predicted to run low (if that's within the year), then one per battery life after that. A device without a prediction counts one change per battery life. Battery life is the device's own average time between changes (its install date counts as the first), or the average for its model when it has none. Devices with neither are left out, and the page says how many.
- A table per model and battery type: batteries per year and cost per year for each device.

Set the price of one battery per type in **Settings → Battery brands → Battery prices**. Types without a price are counted but not costed. Battery prices are kept by the app (in its history and backups), not in the app configuration.

## Critical devices

Turn on **Critical device** on a device's Info tab for devices that must never fail quietly: smoke and leak sensors, the freezer plug, the heating. A critical device:

- gets an Offline alert after `critical_offline_minutes` (default 5) instead of `offline_alert_minutes`;
- shows a red **Critical** tag in its panel and on the Health tab, and has its own filter in the device list;
- sends its alerts with the title starting **Critical:**, and with `critical: true` in the `z2m_tracker_alert` event;
- alerts straight away during quiet hours.

Alerts to `notify_service` for a critical device also carry the Companion app's high-priority data: `priority: high` and `ttl: 0` (Android, delivered straight away), and `push: interruption-level: time-sensitive` (iOS, shown through Focus modes).

## Device labels

Tick devices in the **Devices** list (the checkbox at the start of each row, the one in the header ticks every device shown, and Space ticks the focused row), then **Print labels** in the bar that appears. Each label has the device's name, IEEE address, battery type and count, the date the tracker first saw it, and a QR code that opens the device in this app (Home Assistant's `/app/<slug>/<ieee>` page, so it asks you to log in first). The QR code is made in the browser; nothing is sent anywhere.

Two layouts: **Avery L7651** (65 labels of 38.1 × 21.2 mm on A4; **Start at label** skips labels already used on a sheet) and **29 mm tape**, one 62 × 29 mm label per page for label printers such as a Brother QL with 29 mm tape. Print at 100% scale ("Actual size"), not "Fit to page". Open the app from the Home Assistant sidebar before printing: anywhere else, the QR code can only link to the app's temporary ingress address.

## Buttons on phone alerts

Alerts sent to `notify_service` through the Home Assistant Companion app come with buttons (turn them off with `alert_actions`):

- **Mute 24 h** mutes that check on that device for 24 hours, the same as muting it in the device panel. On a router outage alert it mutes the router's Offline check, which also quiets the devices behind it.
- **Ping** pings the device and sends the answer time (or that it didn't answer) as a second notification. Only on mains devices that can be pinged (see [Ping](#ping)).
- **Battery replaced** records a battery change today, the same as the button on the Battery tab (and in Battery Notes, if the device has it). Only on Low battery and Battery running down alerts.

Tapping the alert opens the device in the Zigbee Tracker. Alerts about a whole network, a group, joins and leaves, the quiet hours digest and the weekly summary have no buttons; tapping them opens the Zigbee Tracker.

The app hears the button through Home Assistant's `mobile_app_notification_action` event, so it works without the integration. Each alert's buttons carry a one-time code: a button works once, for 7 days, and only for the device and check it was sent for. Anything else is ignored and logged. Alerts carry a `tag`, so a repeat of the same alert replaces the old one on the phone.

If your notify service isn't the Companion app and rejects the extra data, turn `alert_actions` off.

## Device age

**Upkeep → Models → By device age** lists every active device, oldest first: its age, its firmware version and when the firmware last changed. Age counts from the purchase date in the device's asset details (Notes tab), or else the day the tracker first saw the device; the Notes tab shows it too. A firmware change is noticed while the app runs, when Zigbee2MQTT reports a new build for the device or an update finishes; changes from before this version, or while the app was stopped and the log has since rolled over, aren't known. A device at least 2 years old whose firmware hasn't changed for 2 years is marked **Not updated in 2+ years**. Select a row to open the device.

## Quiet hours

With `quiet_hours` on, new issue alerts and Device joined, Device left and Interview failed alerts are held between `quiet_start` and `quiet_end` instead of being sent. Alerts for critical devices still go out straight away.

When quiet hours end, one notification sums up the night, titled "Zigbee overnight: N issues, M resolved". It lists the issues that are still open, the ones that appeared and cleared during the night (with their times), and the held join, leave and interview alerts. Issues listed as still open count as alerted, so they aren't sent again one by one. If nothing happened, no summary is sent. Held alerts are kept in the app's data, so they survive a restart.

## Escalation

With `escalate_enabled` on and `escalate_service` set, an issue alert that nobody answers within `escalate_hours` is sent again to that second service, titled "Unanswered: ..." and ending "Nobody muted, pinged or fixed this in 2 h." Set it in **Settings → Notifications**.

An alert counts as answered when the device is pinged (from the app or the **Ping** button on a phone alert), when any button on its phone alert is pressed, or when the problem goes away: fixed, muted or ignored. Each problem escalates once; if it clears and comes back, that is a new problem. With `escalate_critical_only` on (the default) only alerts for critical devices escalate. During quiet hours only critical devices escalate; the rest wait until quiet hours end. Nothing escalates while alerts are paused. Join, leave and interview alerts and the overnight summary never escalate. The `z2m_tracker_alert` event fires again with `escalated: true` and the `service` it went to.

## Stale retained messages

The MQTT broker keeps the last retained message on a topic until something deletes it. After a device is renamed or removed, its old topics can stay behind: `zigbee2mqtt/Hall Motion/availability` still saying offline while the renamed device is online, the state of a device removed months ago, or Home Assistant discovery configs that bring back ghost entities on every restart. **Network → Broker** lists them under **Stale retained messages**, with filters for **Old name**, **Removed device** and **HA discovery**.

The app scans the broker with a second, short-lived MQTT connection: 30 seconds after it connects, 20 seconds after a device is renamed or removed, and every 6 hours. **Scan again** starts one by hand. A scan reads the retained messages under each tracked base topic and the discovery configs under `homeassistant/`, and keeps at most 20,000 topics with the first 160 characters of each payload, in memory only. A topic is listed when:

- **Old name**: it sits under a name a device used to have, and no current device or group has that name now.
- **Removed device**: it sits under a name or IEEE address of a device that is no longer in Zigbee2MQTT.
- **HA discovery**: it is a discovery config (`homeassistant/<component>/<ieee>/…/config`) for an IEEE address that isn't an active device, and its payload points at a tracked base topic. Configs that point only at networks the app doesn't track are left alone.

Topics of current devices and groups, `bridge/` topics, and names the app has never seen are never listed. MQTT doesn't record when a message was retained, so the age shown is when the device was renamed or removed.

**Clear** publishes an empty retained message to the topic, which deletes it from the broker. Nothing is sent to the device. **Clear all** clears every message in the current filter. Both ask first, and the app checks each topic against the latest scan before clearing it. Turn on `retained_autoclear` (**Settings → Broker**) to clear a device's old-name topics automatically after each scan. When any stale messages exist, Health shows **Stale retained messages** under **Worth a look**.

## Compare two devices

Tick exactly two devices in the **Devices** list (the checkbox at the start of each row; Space ticks the focused row), then **Compare** in the bar that appears. The dialog puts them side by side: model and firmware, average link quality over 7 days, parent router with its hop count from the last routing scan, how long the last battery pack lasted (between the last two recorded battery dates), uptime over 30 days and messages per hour over the last 24 hours. The worse value is in red where the gap matters: 10 or more link quality, a longer route, a battery pack lasting at least 25% less, or half a point of uptime. Select a device's name to open it.

## Leftover devices in Home Assistant

A device removed from Zigbee2MQTT can stay in Home Assistant with all its entities unavailable, for example when Zigbee2MQTT was stopped or its Home Assistant integration was off at the time. **Network → Broker** lists these under **Leftover in Home Assistant**, and Health links to it.

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

### Firmware notes

**Firmware notes** on the Updates tab are your own notes against a device model and firmware version, e.g. "Drops off the mesh every few days. Stay on 2.1.3." Add, edit and delete them there; the app keeps them in its data (up to 200).

- A device of that model whose firmware build (Zigbee2MQTT's `software_build_id`) or installed OTA file version is the noted version shows the note on its page, and gets a **Firmware you noted** row in Health under **Worth a look**.
- An update on offer to the noted version is marked "You noted problems with ..." and left out of **Update all** and the overnight `ota_window` run. **Update anyway** on that row installs it. Zigbee2MQTT only knows the OTA file version of an update before it's installed, so a note on a build such as `2.1.4` matches an offered update once a device of the same model runs `2.1.4` and reports its file version; a note on the file version itself matches straight away.
- The models come from Zigbee2MQTT's device definitions, so a note covers every device of that model on every network.

## Community ratings

What other Zigbee Tracker users think of each device model, in the spirit of the Battery Notes device library. A device's **Community** tab shows its model's verdict (**Recommended**, **Mixed** or **Avoid**, once at least 5 people have rated it), how many people rated it, the measured numbers they shared (median uptime, drop-outs a month, link quality and battery life) next to your own, the most common tags, the firmware version people rate best, and a few recent comments. **Upkeep → Community** lists every model on your networks first, then every other rated model, with a search box: useful before buying a device.

**Share your experience** on the Community tab rates a model:

1. Pick **Recommend**, **It's OK** or **Avoid**, any tags that fit, and an optional comment.
2. Turn on **Attach my stats** to add the numbers the tracker measured for that model (uptime over 30 days, drop-outs a month, median link quality, battery life between changes). They make the ratings much more useful.
3. The dialog shows exactly what will be posted. **Continue on GitHub** opens a pre-filled GitHub issue in a new tab. You need a GitHub account, and nothing is posted until you press **Create** there.

A report holds the vendor, model, firmware version, Zigbee2MQTT version, adapter type, how many of the model you have and for how many months, your verdict, tags and comment, the stats if you chose to share them, and a reporter code. It never holds device names, areas, IEEE addresses or anything about your Home Assistant. The reporter code is a hash of a random ID kept in the app's data folder and the model, so a newer report from you replaces your older one for the same model; it can't be linked back to you or across models. Reports are public GitHub issues released under CC0.

A bot checks each report and closes the issue. Once a week the reports are rebuilt into `community_ratings.json` (newest report per person per model), which ships with the app and, unless `community_ratings_fetch` is off, is downloaded from GitHub once a day.

## Security

- **Who can use the app.** The sidebar is open to Home Assistant administrators only. The integration in Home Assistant Core reaches the app with an API token: the app makes it on first start (kept in its `/data` folder) and hands it to Home Assistant through Supervisor discovery, which only Core can read. Anything else on Supervisor's network gets a 403. Update the app and the integration together: an older integration has no token and shows as unavailable.
- **The MQTT broker.** The app trusts what arrives under each Zigbee2MQTT base topic, so whoever can publish there can feed it made-up devices. With the Mosquitto broker app, every Home Assistant user account can log in to the broker. To limit that, turn on Mosquitto's `customize` option and give only Zigbee2MQTT write access to `zigbee2mqtt/#` with an ACL file. The app checks what it receives (text, numbers and IEEE addresses), ignores malformed messages, and holds a device list that would remove more than half of a network until Zigbee2MQTT sends it again.
- **Device names in notifications.** Names and details from Zigbee2MQTT can't add links, images or HTML to Home Assistant notifications, Repairs or the to-do list.

## Data

History is stored in the app's own data folder and is included in Home Assistant backups. Use **Settings → Backup** in the app to download or restore it.
