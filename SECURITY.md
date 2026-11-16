# Security

## What the app can reach

The app runs as a Home Assistant app (add-on) with the access its `config.yaml` asks for:

- **MQTT**, through the broker Home Assistant is set up with. It reads every message under
  each Zigbee2MQTT base topic it tracks, and publishes to them when you ask it to: rename a
  device, set an area, reconfigure, start a routing scan, open pairing through a router, or
  start an OTA update. It also publishes its own MQTT discovery sensors and an availability
  topic under `z2m_tracker/`.
- **The Supervisor and Home Assistant APIs**, with the token the Supervisor gives every app.
  It uses them to send notifications, fire `z2m_tracker_alert` events, call the notify
  service you name, read areas, and save its own options.
- **Your Home Assistant configuration folder, read-only.** Supervisor can only map the whole
  folder, so the app can read everything in it, including `secrets.yaml`, `.storage` (login
  tokens and integration settings) and Zigbee2MQTT's `coordinator_backup.json`, which holds the
  Zigbee network key. It uses only a few files: Zigbee2MQTT's own (`configuration.yaml`,
  `database.db`, state and log files) for the history import, HA's device, entity and area
  registries, and the coordinator backup's date and whether it has a key (never the key itself).
  Nothing in that folder is written, no request can name a file to read, and none of it is
  sent anywhere.

Its history, metrics and scans are stored in the app's own `/data` folder, which Home
Assistant includes in backups.

## Who can talk to it

The web interface listens on port 8099 inside the Supervisor's network and is not published
on the host. Two kinds of request are accepted; anything else gets a 403:

- **From Supervisor's ingress proxy** (the sidebar). Supervisor has already checked the Home
  Assistant login, and the panel is for administrators only, so it has the same reach as any
  administrator of that Home Assistant.
- **With the app's API token** (the integration). The app makes a random token on first start,
  keeps it in `/data`, and hands it to Home Assistant Core through Supervisor discovery, which
  only Core can read. Core shares its network address with every app that uses host
  networking, so the address alone isn't trusted.

Changes (POST requests) must be JSON and must not be marked by the browser as coming from
another site, so a page elsewhere on your network can't make a signed-in browser change
settings. The sidebar is served with a Content-Security-Policy that only runs its own script.

## What it trusts from MQTT

Anything that can publish under a Zigbee2MQTT base topic can feed the app made-up devices,
names and readings. With the Mosquitto broker app, every Home Assistant user account can log
in to the broker, so restrict publishing to `zigbee2mqtt/#` with Mosquitto's ACLs if your
Home Assistant has users you don't fully trust. The app limits the damage: it keeps only text,
finite numbers and valid IEEE addresses, ignores malformed messages, escapes device data
everywhere it is shown (the sidebar, notifications, Repairs, the to-do list, CSV exports), and
holds a device list that would remove more than half of a network until it arrives again.

The app makes no requests to the internet. The battery library and the manufacturer (OUI)
list are carried in the image rather than fetched.

## The integration and the card

The integration talks only to the app's address on the Supervisor network
(`http://local-z2m-tracker:8099` unless you change it), sending the app's API token, and turns
what it reads into sensors. Its actions (OTA update, mute, pause alerts, notes, battery
changes, import) and ticking the batteries to-do list are for administrators; automations and
scripts can still call them. Non-administrators can read the device list the cards use, without
notes or asset details. The card reads those sensors and draws them in your browser.

## Reporting something

Please report privately through [GitHub's advisory form][advisory] rather than opening an
issue, and give it a few days before saying anything publicly.

Useful things to include: what an attacker would gain, how to reproduce it, and the app,
integration and Home Assistant versions. A proof of concept is welcome but not required.

[advisory]: https://github.com/tempus2016/z2m-device-tracker/security/advisories/new
