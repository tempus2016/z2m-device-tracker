# Changelog

## Unreleased

- New installs now alert when a Zigbee2MQTT network goes offline. The Network offline check was missing from the default alert list, so it only showed on the Health tab. Existing installs keep their current choice: to get these alerts, set Network offline to Show and notify under Settings, Checks.

## 1.0.0

First public release.

- Device history: every name, rename, model, firmware, area and removal, kept after a device leaves.
- Health checks and alerts: offline, silent, frozen readings, weak or dropping signal, low battery, failed interviews, unidentified devices.
- Coordinator health: Zigbee2MQTT version, coordinator firmware, restarts and outages, with an alert when a network goes offline.
- Radio channels: Zigbee channels checked against your 2.4 GHz Wi-Fi channels.
- Interactive network map with advice, nightly routing scans and before/after comparison between scans.
- Pairing assistant (pair through a chosen router), move a device to another router, reconfigure, rename, set area, OTA updates.
- Battery records, battery type library, shopping list and replacement estimates.
- MQTT discovery sensors attached to each Zigbee2MQTT device, weekly summary, backup and restore, CSV export.
