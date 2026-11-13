# Changelog

## Unreleased

### Added
- Translations into German, Dutch, French, Spanish, Italian, Polish, Brazilian Portuguese, Swedish, Czech, Russian and Simplified Chinese. They cover the sidebar, the app's options, and the integration's setup, actions and dashboard cards. Each follows your Home Assistant profile language; the sidebar's Settings can pick another. The translations are machine-made, so corrections from native speakers are welcome.
- The dashboard cards are translated too. They read the app's translation files through the integration, so they need this version of the app as well. With an older app they stay in English. Cards added before this version saved an English title in their settings: clear the Title field to get the translated one.

### Fixed
- A regional language such as Portuguese (Portugal) now picks the closest translation (Brazilian Portuguese) instead of falling back to English.

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
