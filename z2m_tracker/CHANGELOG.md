# Changelog

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
