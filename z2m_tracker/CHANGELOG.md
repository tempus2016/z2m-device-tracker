# Changelog

## 2.0.2

Update the app: two fixes, both in the app. The integration has no changes in this release: its version moves to 2.0.2 only so the two parts keep matching numbers.

### Fixed
- Ignored alerts are kept when the app restarts or updates ([#48](https://github.com/tempus2016/z2m-device-tracker/issues/48)). Since 1.4.0 the app cleared every ignored alert each time it started, so **Settings → Ignored alerts** came back empty after an update and the alerts returned. Alerts you ignored before this fix are already gone and need ignoring once more; from now on they stay ignored.
- Deleting a device's retained availability message now clears its stale Online/Offline state ([discussion #21](https://github.com/tempus2016/z2m-device-tracker/discussions/21)). If Zigbee2MQTT stops checking a device's availability, the last retained `offline` can stay on the broker and the tracker kept showing the device as offline. Removing `zigbee2mqtt/<device>/availability` (in MQTT Explorer, for example) did nothing until something published `online` again. Now the tracker treats the empty message as "no longer known": the offline alert clears, the open outage is closed, and the device shows no availability until Zigbee2MQTT reports one.

## 2.0.1

Update the integration. The app has no changes in this release: its version moves to 2.0.1 only so the two parts keep matching numbers.

### Added
- The integration's battery and maintenance calendars and to-do lists can be turned off ([#43](https://github.com/tempus2016/z2m-device-tracker/issues/43)). Home Assistant adds **Calendar** and **To-do** to the sidebar while any integration provides them, so the tracker added both even for people who don't use them. Turn off **Battery and maintenance calendars and to-do lists** under Settings → Devices & services → Z2M Device Tracker → ⚙. The four entities (`calendar.z2m_tracker_batteries`, `calendar.z2m_tracker_maintenance`, `todo.z2m_tracker_batteries`, `todo.z2m_tracker_maintenance`) are removed, and after a restart of Home Assistant the sidebar items go too, unless another integration still provides a calendar or to-do list. Turning the option back on brings the entities back with the same IDs. It is on by default, so nothing changes until you turn it off. To keep the lists and only hide the sidebar items, long-press the Home Assistant title at the top of the sidebar.

## 2.0.0

The biggest release yet: a new seven-tab layout, community device ratings and 22 new features. Update the app and the integration together. The integration's Repairs wording, the new check names and the summary card's community section need the 2.0.0 app. New checks show on Health but don't notify until you turn them on under Settings → Checks. No settings or history are lost: 2.0.0 reads your 1.x data as it is.

### Rate your devices (new)
- Community device ratings: a device's **Community** tab shows what other Zigbee Tracker users think of its model (Recommended, Mixed or Avoid once 5 people have rated it), their measured uptime, drop-outs, link quality and battery life next to yours, the firmware they rate best and recent comments. **Upkeep → Community** lists your models, the ones nobody has rated yet, and every other rated model with a search box.
- **Share your experience** on the Community tab fills in a GitHub issue form for you: verdict, tags, an optional comment and, if you choose, the stats the tracker measured. The dialog shows exactly what will be posted, nothing is posted until you press Create on GitHub, and device names, areas, IEEE addresses and anything else about your Home Assistant are never included. Ratings are public under CC0.
- A new **Rated poorly by others** check (low priority, never notifies unless you turn it on), and an optional community section on the summary card (`show_community`). New option `community_ratings_fetch` (on) downloads the newest ratings once a day.

### Changed
- New layout: Health, Devices, Network (Map, Coordinator, Broker, Radio, Groups), Upkeep (Batteries, Maintenance, Models, Costs, Community), Updates, Activity and Settings. Health shows problems first, then one line for each part of the app that lives on another tab. On a phone the bottom bar has Health, Devices, Network, Upkeep and More.
- Expected reporting uses profiles (Reports on change, Regular, Chatty, Custom, Off) with a default per device type, instead of typing a silent interval per device. Intervals you set before keep working as Custom.

### Added
- Mesh score: one 0 to 100 score per network at the top of Health, with a 30-day trend, the change since last week and a `sensor.<base topic>_mesh_score` entity.
- Broker statistics on **Network → Broker** from Mosquitto's `$SYS` topics: clients, retained messages, dropped messages and messages per minute over 24 hours. New option `broker_stats` (on).
- Stale retained messages: topics left under a device's old name, removed devices and orphaned Home Assistant discovery configs, with Clear and Clear all. New option `retained_autoclear` (off).
- Retained command check: a `/set` command retained on the broker, which replays every time the broker or Zigbee2MQTT restarts.
- Availability disagreement check: Zigbee2MQTT says offline while the device keeps talking, or online while it has been silent far too long.
- Live messages: a **Messages** tab on each device with its last 50 messages, commands and how long it took to answer.
- Impossible readings check: temperatures outside a range you set, humidity or battery outside 0 to 100%, humidity stuck at 0 or 100%. New options `impossible_temp_min`, `impossible_temp_max` and `humidity_stuck_hours`.
- Payload changes: the device's History tab logs keys it started or stopped sending, and the **Sends different data** check flags a gone key that an automation or script still uses.
- Settings advice: a Health note when Zigbee2MQTT's `last_seen` is off, with a one-click **Turn on**.
- Power loss in an area: 3 or more mains devices in one area going offline together, while a battery device there stays online, become one "Power loss in Garage?" issue and one notification.
- Home Assistant Repairs: a critical device offline, a Zigbee2MQTT network offline and a coordinator backup problem get their own wording and severity and always make the list. New option `ha_repairs` (on).
- Escalation: an alert nobody answers within `escalate_hours` is sent again to a second notify service. New options `escalate_enabled`, `escalate_hours`, `escalate_service` and `escalate_critical_only`.
- Firmware notes: your own notes against a model and firmware version. A noted version is flagged on the device and held back from Update all and the overnight window.
- Warranties: a Warranty until date in a device's asset details, a Warranties list and a **Warranty ending** check.
- Running costs on **Upkeep → Costs**: devices bought, batteries over the last 12 months and an estimate for the next 12, with battery prices per type.
- Print labels: tick devices and print a sheet of labels with name, IEEE address, battery type and a QR code that opens the device (Avery L7651 or 29 mm tape).
- Device age: **Upkeep → Models → By device age**, oldest first, with when the firmware last changed.
- Compare devices: tick two devices and compare them side by side.
- Coordinator backup copies: the last 10 versions of each network's `coordinator_backup.json`, with what changed between them and a Download button.
- All networks: with more than one Zigbee2MQTT, a network picker and an overview card per network that warns about the same device name on two networks.
- Group history: members added and removed, renames and when each Zigbee group was created.
- Battery shop link: an optional cart button next to each battery to buy (`battery_shop_url`).
- A Support this project link in the menu and Settings.
- New option `networkmap_timeout_minutes` (60): how long to wait for a routing scan.

### Fixed
- Commands with `transition`, `brightness_step`, `effect`, `on_time` and similar options are no longer counted as missed ([#37](https://github.com/tempus2016/z2m-device-tracker/issues/37)).
- Routing scans on large networks no longer time out after 10 minutes while Zigbee2MQTT is still scanning, and a second scan or the nightly router ping no longer starts on top of one ([#38](https://github.com/tempus2016/z2m-device-tracker/issues/38)).
- Network events in Activity read "Main network online" instead of "Network network online".

## 1.5.2

Update the app. The integration only changes how the `run_network_scan` action reads a base topic with a space, so updating it is optional.

### Fixed
- A Zigbee2MQTT base topic that starts or ends with a space, such as `Z2MQTT `, now works ([#33](https://github.com/tempus2016/z2m-device-tracker/issues/33)). The app trimmed the space off and listened on the wrong topic, so it never saw the network. Base topics are now used exactly as written, apart from slashes at either end, and the log warns about any leading or trailing space. In the YAML editor, quote such a topic: `- "Z2MQTT "`.
- The routing scan history and router what-if on the Map tab work for a network whose base topic contains a space.
- `z2m_panels` entries match a base topic with a space as written; trimmed entries still match as before.
- The `run_network_scan` action accepts a base topic with a space.

## 1.5.1

Only the app changed. The integration is unchanged apart from its version number, so updating it is optional.

### Fixed
- Battery devices are no longer reported as having no backup route. Zigbee2MQTT's routing scan only shows a battery device's link to its current parent, never the other routers it can hear. The router what-if took that to mean it had nowhere else to go. Battery devices whose parent goes now appear under "Will look for a new parent" instead of "No other router in range". "Where to add a router" counts only routers as having no backup route, because routers report their neighbours in the scan.

## 1.5.0

Update the app and the integration together: the integration's new maintenance to-do list and calendar, and the brand on the battery replaced action, need the 1.5.0 app. New checks show on the Health tab but don't notify until you turn them on under Settings → Checks.

### Added
- What a problem affects: a device with an issue lists the Home Assistant automations, scripts and scenes that use it, with links to their editors, and its alerts get a "Used by" line. Looked up for devices with issues and critical devices; a failed lookup is retried after 1, 5 and 15 minutes.
- Buttons on phone alerts: alerts sent through `notify_service` get Companion app buttons (Mute 24 h, Ping for mains devices, Battery replaced for battery alerts) and open the device when tapped. Each alert carries a one-use code that expires after 7 days. New option `alert_actions` (on) turns them off for notify services that aren't the Companion app.
- Incidents: the Activity tab has an Incidents view that groups problems on one network (3 or more devices, or Zigbee2MQTT going down, within 10 minutes of each other) with a likely cause: Zigbee2MQTT or the coordinator down, a named router offline, or a network-wide drop.
- Effect of an upgrade: Zigbee2MQTT and coordinator firmware upgrades are recorded, and the Coordinators card compares the week before with the week after: devices going offline per day, average link quality, messages per hour, devices leaving per day and parent changes per routing scan. A clearly worse week adds a Worse since upgrade item.
- Slow or missed commands: every `/set` to a device is timed until its state shows what was asked; commands within 2 s count as one. The device's Health tab shows commands, typical reply time and misses with a 7-day chart. New checks Slow to respond and Misses commands.
- Reporting settings: each device's configured attribute reporting is listed in plain words on its Health tab. Settings that drain a battery, flood the mesh or may never report are flagged, and Use recommended sends the fix to Zigbee2MQTT after a confirm. New check Reporting settings.
- What if a router goes: on the Map tab, pick a router to see which devices would move and to which router, and which have no other router in range, from the latest routing scan.
- Where to add a router: the Map tab ranks Home Assistant areas by devices with no backup route, weak links, router hopping and dropouts, and flags the top 3 on the floor plan.
- Pairing helper: the Pair device dialog follows each joining device through its interview, then offers a name and an area. Names come from a naming pattern (`naming_pattern`, tokens `{area}`, `{type}`, `{model}`, `{n}`), set under Settings → Device names. New check Naming, for devices still named by address or off-pattern, with Rename to pattern on the Health tab.
- Maintenance tasks: recurring jobs per device on its Notes tab (test the smoke alarm monthly), with quick picks by device type and Mark done, which goes in the device's logbook. A Maintenance card lists overdue and due-soon tasks. With the integration, a Zigbee maintenance to-do list (tick to mark done) and calendar. Tasks move to a replacement device with its notes. New check Maintenance overdue.
- Battery brands: a battery change can record a brand, picked from a list you keep under Settings → Battery brands. A Battery life by brand card compares how long each brand lasts per battery type, from finished batteries only. The `battery_replaced` action takes an optional `brand`, and the CSV export has a `battery_brand` column.
- Reliability by model: a Health card groups your devices by vendor and model with uptime, dropouts a month, battery life, failed interviews and re-pairs, worst first. Models with one device are never flagged.
- Zigbee2MQTT settings history: changes to Zigbee2MQTT's own settings are listed on the Coordinators card and logged as events; a device's own options go on its logbook. Passwords, the network key, PAN ID and tokens are never stored or shown.
- Prometheus metrics: an optional `/metrics` endpoint on port 9099 with per-device gauges (online, link quality, battery, last seen, messages, uptime, issues) and network totals. Off until `metrics_token` (16 characters or more) is set and the port is mapped.

### Fixed
- The sidebar no longer receives `mqtt_password`.
- A device Home Assistant learns about from a join event, before Zigbee2MQTT lists it, is named by its address instead of after the MQTT broker.
- Network events on the Activity tab no longer run their detail into the network name.

## 1.4.0

Update the app and the integration together. The app now asks Home Assistant for an API token, and an integration older than 1.4.0 has none, so it shows as unavailable until it is updated too.

### Added
- MQTT broker from the app's options: `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`, `mqtt_ssl` and `mqtt_ssl_verify`. A broker set here is used instead of the one Home Assistant offers, so Mosquitto on another machine or in Docker works without the Mosquitto broker app. Its TLS certificate is checked unless `mqtt_ssl_verify` is off. Connection errors point at these options.

### Changed
- The app needs an API token from anything but the sidebar. It makes one on first start and hands it to Home Assistant through Supervisor discovery, which only Home Assistant Core can read. A new install shows the app under Settings → Devices & services → Discovered.
- The integration's actions (OTA update, mute, pause and resume alerts, notes, battery replaced, network scan, reconfigure, import history) and ticking the batteries to-do list are for administrators. Automations and scripts can still call them. Non-administrators get the device list without notes and asset details.
- `notify_service` accepts only notify services. A bare name like `mobile_app_phone` becomes `notify.mobile_app_phone`.
- The MQTT broker app is no longer required to start (`mqtt: want`), because a broker can be set in the options.
- Updated paho-mqtt 2.2.0, PyYAML 6.0.3, websocket-client 1.9.2.

### Fixed
- A device's data could run script in the sidebar. A routing scan with a text link quality was saved as-is and shown as HTML on the floor plan, on Home Assistant's own origin. Every value is now escaped, scans keep only numbers, and the sidebar is served with a Content-Security-Policy that runs only its own script.
- One malformed MQTT message could stop all tracking until the app restarted, and a retained one stopped it on every start. Bad messages are now ignored.
- Text, numbers and addresses from Zigbee2MQTT are checked before they are kept. A device name that isn't text, or a reading of NaN or infinity, could break health checks, alerts and the sidebar for days, even after a restart. Saved data with such values is cleaned on load.
- A device list that would remove more than half of a network is held until it arrives again at least 5 minutes later, so a stray or spoofed message can't mark every device removed and delete their Home Assistant entities.
- Unknown Zigbee2MQTT event types no longer create devices, and the tracker caps names, event text and routing scans, so a flood of messages can't fill the disk.
- Restoring a broken backup no longer wipes the current history: the file is checked before anything is replaced. A backup can't remove other integrations' MQTT entities.
- A corrupt `history.json` is set aside as `history.json.corrupt-<time>` and the app starts empty, instead of failing to start. Saves no longer collide when two run at once.
- Device names can't add links, images or HTML to Home Assistant notifications, Repairs or the batteries to-do list.
- Changes in the sidebar must come from the sidebar: requests that aren't JSON, or that the browser marks as coming from another site, are refused.
- Reconfigure and battery-replaced buttons from Home Assistant run at most once a minute per device.
- CSV export quotes cells a spreadsheet would run as a formula.
- MQTT TLS checks the broker's certificate for brokers outside Home Assistant's own network.
- Repairs mirror at most 50 issues, and the card translations are cached per language the app has.
- The integration's setup rejects an app URL with a user name or password, and fills in the address the app announced.
- The summary card counts offline devices the same way as the Offline devices sensor; it left out unidentified devices.
- A broken link in the sidebar address (`#%E0`) no longer stops the page loading.

## 1.3.0

### Changed
- Health shows one row per device, headed by its worst problem. Other problems sit under it ("Also: Low battery") with their own Ignore; Ignore on the row hides only the headline. Offline rows say the likely cause when the data shows it (a flat battery, or the router it last used) and offer Replace… and Open Zigbee2MQTT. The Health badge, the Needs attention count and the summary count rows.
- The device panel puts the problem first: issue alerts under the status in their severity colour, then one main fix action and a More menu with the rest. Reconfigure is hidden while the device is offline.
- Last seen stays visible: when its column is hidden (side pane open, tablet widths) the time joins the status cell, and every phone row ends with it.
- The map is on the first screen on phones. Its help is a one-line "How the map works", the map options sit behind an Options button, and zoom controls are on the map's corner.
- MQTT discovery includes each device's name, so the tracker's entities stay on properly named devices when Zigbee2MQTT's own Home Assistant discovery is off.

### Fixed
- The app explains MQTT connection problems in plain words (no broker set up, broker not found, nothing listening, no answer, login refused) and says what to do. Each problem is logged once, then at most every 10 minutes. After 2 minutes without a connection Home Assistant gets a notification, which clears when the app connects.
- Stopping the app while it had no MQTT connection crashed before the final save, losing anything recorded since the last 30-second save. It now saves first.
- No more "Service not enabled" errors in the log when the app starts without an MQTT broker.
- With Zigbee2MQTT's Home Assistant discovery off, the tracker's entities were named after the MQTT broker (`binary_sensor.mosquitto_broker_needs_attention_6`).
- The tab bar no longer sits about 34 px high inside the Home Assistant phone app.
- Radio channels shows "not checked yet" instead of a green "No overlap" when no Wi-Fi channels are set.
- Recovery steps at risky moments: a failed settings load offers Retry, the restore confirm offers to download the current data first, Update all lists the devices and the time it will take, and out-of-range settings show a note under the field.
- Devices with no name are labelled "Unnamed device" on the routing map instead of a bare dot.

## 1.2.0

### Added
- Expected reporting: set how often a device should report, from 30 seconds to 7 days, on its Health tab. A device that goes quiet for longer is flagged as Stopped reporting, even while Zigbee2MQTT still shows it online. Settings → Thresholds → Expected reporting → Set for many devices… sets it for mains or battery devices at once, from each device's usual gap times 2, 3, 5 or 10.
- When this device alerts: each device's Health tab shows how long it takes to alert, adding Zigbee2MQTT's availability timeout to `offline_alert_minutes`.
- Router outages: when a router goes offline, the devices behind it are grouped under it as one issue and one notification instead of one each.
- Pause alerts: ⋮ → Pause alerts for 30 min, 1 hour, 4 hours, until 08:00 or until resumed. Issues still open when the pause ends are sent once. The integration has `z2m_tracker.pause_alerts` and `z2m_tracker.resume_alerts`.
- Critical devices: turn on Critical device on a device's Info tab. It alerts after `critical_offline_minutes` (default 5), its titles start with "Critical:", phone notifications are sent high priority / time-sensitive, and it ignores quiet hours.
- Quiet hours: with `quiet_hours` on, alerts between `quiet_start` and `quiet_end` are held and sent as one morning summary.
- Per-device limits: a device's Health tab can override the low battery level and the frozen reading time.
- New checks: Pairing left open (also when something else opened it, with Close now), Changes router often, Router getting slower, Zigbee2MQTT memory, Group members offline, and Daily signal dip. Health has a Groups card listing every Zigbee2MQTT group and its members.
- Ping: mains devices have a Ping button on the Info tab that shows the reply time. Routers are pinged once a night and the Trends tab shows the last 14 nights. Needs Zigbee2MQTT 2.7+.
- Coordinators card: Zigbee2MQTT's own memory, MQTT traffic and leaves this week (Zigbee2MQTT 2.5+), and how it reaches the coordinator (TCP, mDNS or USB) with the adapter, versions and settings under More details.
- Leftover in Home Assistant: Health lists Zigbee2MQTT devices that were removed from Zigbee2MQTT but stayed in Home Assistant with dead entities. Review and remove deletes the ones you tick.
- Battery stock: record the spare batteries you have. The shopping list says how many to buy, and recording a battery change takes from stock.
- Phone layout: a bottom tab bar, problems first on Health, jump chips in Settings and pinch zoom on the map. Taps no longer leave rows highlighted, inputs no longer zoom the page on iOS, and dialogs fit the screen.
- Charts show the date and value under the pointer or finger, on every chart in the sidebar.
- Network map: a Names option (All, Routers only, Off). The floor plan has a Links switch (Off, Problems, All) that defaults to Problems, so big networks stay readable; hovering a room shows its links and fades the rest.
- Translations into German, Dutch, French, Spanish, Italian, Polish, Brazilian Portuguese, Swedish, Czech, Russian and Simplified Chinese. They cover the sidebar, the app's options, and the integration's setup, actions and dashboard cards. Each follows your Home Assistant profile language; the sidebar's Settings can pick another. The translations are machine-made, so corrections from native speakers are welcome. Text added in this release is English only for now.
- The dashboard cards are translated too. They read the app's translation files through the integration, so they need this version of the app as well. With an older app they stay in English. Cards added before this version saved an English title in their settings: clear the Title field to get the translated one.

### Changed
- The replace wizard's Remove the old device option starts ticked only when the old device is offline, and shows in red when it will run.
- The Coordinators card on Health is shorter: five facts per network, the rest under More details. An offline network or one with a memory warning opens expanded.
- Every dropdown in the sidebar draws its own arrow, clear of the border.

### Fixed
- Text and buttons in the sidebar pass WCAG AA contrast in light and dark themes.
- The sidebar works from the keyboard and with screen readers: focus stays put when the view refreshes, the map and signal bars are announced, and titles are real headings. Buttons and chips answer taps across at least 44 × 44 px on touch screens.
- The Health badge matches the Needs attention count. Firmware updates now show on the Updates tab only.
- Settings → Repairs stays empty while Send alerts is off in the app.
- The battery chart no longer reads as 100 % for a device at 0 %, and a replacement logged this morning shows.
- Map names no longer stay open after a tap on a phone.
- A regional language such as Portuguese (Portugal) picks the closest translation (Brazilian Portuguese) instead of falling back to English.

## 1.1.0

### Added
- Updates tab: every device with a Zigbee2MQTT firmware update, with installed and available versions and live progress. Check now, Update now and Update all, installed one device at a time. New options `ota_window` (install overnight) and `ota_min_battery` (skip low batteries). A failed overnight update is retried once, the next night. Results go to Recent updates and to the device's logbook in Home Assistant.
- Trends tab on each device: link quality and battery over 24 hours, 7, 30 or 90 days, with a projection to the 10 % battery date.
- Floor-plan layout for the network map: devices grouped by Home Assistant floor and area, with lines to their routers coloured by link quality and weak areas flagged.
- Network map legend filters: click Coordinator, Router, Battery device or Needs attention to show only that group. Big networks now spread out and the map zooms to fit.
- Device replacement wizard: Replace… on an offline, silent or removed device moves its name, area, bindings, notes, asset details and battery record to a new device, and links the two histories.
- Restore buttons for lost bindings, per binding or all at once, in the device's Bindings list and on the binding-lost health issue.
- Translations: the sidebar follows your Home Assistant profile language, or a language picked in Settings. English ships now; CONTRIBUTING.md explains how to add a language.
- Integration: battery to-do list and replacement calendar. Ticking a battery type records the replacement for each of its devices.
- Integration: actions for automations: `z2m_tracker.battery_replaced`, `mute_device`, `run_network_scan`, `reconfigure`, `ota_update` and `add_note`.
- Integration: three more dashboard cards (batteries, health, network). Tapping a device opens it in the sidebar.
- Integration: Repairs issues for a device can now be fixed from Home Assistant: restore or accept a lost binding, record a battery change, reconfigure, install an update, or mute.
- Integration: Download diagnostics, with addresses, names and notes redacted.

### Fixed
- Devices paired since the app started can have their area set. Before, the area box was greyed out until the next 6-hourly refresh, and the pairing panel dropped the area silently.
- Device links, names and areas follow Home Assistant within seconds of a change, instead of every 6 hours. Area changes made in Home Assistant now show in the app.
- An offline device with no recorded offline time now raises the offline alert.
- Restoring a lost binding to a group works. Zigbee2MQTT only accepts the group's name, not its ID.
- Batteries marked replaced leave the shopping list until the device next reports a level.
- Fonts and translation files are looked up from a fixed list, which clears the CodeQL path-injection alerts.

## 1.0.1

- Links to the wiki, the GitHub page and the releases page, in the ⋮ menu and next to the version in Settings.
- The integration now installs from a `z2m_tracker.zip` file attached to each release, so HACS installs and updates are counted. Nothing changes for you.

## 1.0.0

First public release.

- Device history: every name, rename, model, firmware, area and removal, kept after a device leaves.
- Health checks and alerts: offline, silent, frozen readings, weak or dropping signal, low battery, failed interviews, unidentified devices.
- More checks: flapping devices, chatty devices, fast battery drain, lost bindings, single points of failure (routers), and a missing, stale or mismatched coordinator backup. They show in Health and don't notify until enabled in Settings → Checks.
- Coordinator health: Zigbee2MQTT version, coordinator firmware, restarts and outages, with an alert when a network goes offline. The coordinator card also shows the coordinator backup's age, channel and device count.
- Binding history: added, removed, restored and intentionally unbound bindings are logged per device, and the Info tab lists current and lost bindings.
- Message rates per device (last 6 h, usual, last 24 h), plus Zigbee2MQTT's own message, leave and address-change counters.
- Radio channels: Zigbee channels checked against your 2.4 GHz Wi-Fi channels.
- Interactive network map with advice (including single points of failure), nightly routing scans and before/after comparison between scans.
- Pairing assistant (pair through a chosen router), move a device to another router, reconfigure, rename, set area, OTA updates.
- Battery records, battery type library, shopping list and replacement estimates.
- MQTT discovery sensors attached to each Zigbee2MQTT device, weekly summary, backup and restore, CSV export.
- Optional integration: summary sensors, a dashboard card, and problems mirrored into Settings → Repairs (on by default, cleared automatically when fixed).
- Icons for the app in the app store and for the integration in HACS and on the Devices & services page.
