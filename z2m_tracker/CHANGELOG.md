# Changelog

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
